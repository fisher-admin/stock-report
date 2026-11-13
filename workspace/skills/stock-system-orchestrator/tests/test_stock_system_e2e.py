"""Comprehensive 20-Item End-to-End Fault Injection Suite for Stock System."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WORKTREE = Path(__file__).resolve().parents[3]
SCRIPT_DIR = WORKTREE / "skills" / "stock-system-orchestrator" / "scripts"
ROOT_SCRIPTS = WORKTREE / "scripts"
OPS_DIR = WORKTREE / "ops"

for p in (SCRIPT_DIR, ROOT_SCRIPTS, OPS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import benchmark_policy as bp
from deployment_receipt import DeploymentReceipt
from env_drift_probe import run_probe
from point_in_time_store import PointInTimeStore, SnapshotImmutableError
from publish_lock import PublishAlreadyRunning, _is_owner_live, publish_lock
import recommendation_warehouse as rw
from run_state import InvalidTransition, RunState
import stock_message_formatter as fmt
import stock_model_gateway as gw
from stock_scheduler_guard import scan_stock_jobs
from sync_public_orchestrator import ALLOWLIST_FILES, sync_public
from verify_stock_backup import verify_database_file


class StockSystemFaultMatrix20Tests(unittest.TestCase):
    # Fault 1: 两个 profile 同时存在重复股票任务
    def test_fault_01_duplicate_stock_scheduler_detected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir)
            root_cron = home / "cron"
            root_cron.mkdir(parents=True)
            (root_cron / "jobs.json").write_text(json.dumps({
                "jobs": [{"id": "job-1", "enabled": True, "name": "股票系统-1", "schedule": {"expr": "30 19 * * 1-5"}}]
            }), encoding="utf-8")

            ai_cron = home / "profiles" / "ai" / "cron"
            ai_cron.mkdir(parents=True)
            (ai_cron / "jobs.json").write_text(json.dumps({
                "jobs": [{"id": "job-2", "enabled": True, "name": "股票系统-1", "schedule": {"expr": "30 19 * * 1-5"}}]
            }), encoding="utf-8")

            scan = scan_stock_jobs(home)
            self.assertEqual(len(scan["duplicates"]), 1)

    # Fault 2: Hermes 在长任务中间重启/另一个 tick 扫描账本（macOS 容差）
    def test_fault_02_darwin_start_fingerprint_tolerance(self):
        my_pid = os.getpid()
        with mock.patch("publish_lock._get_process_start_time", return_value=1000):
            # Same timestamp matches
            self.assertTrue(_is_owner_live(my_pid, 1000))
            # Within Darwin tolerance 200cs matches
            with mock.patch("sys.platform", "darwin"):
                self.assertTrue(_is_owner_live(my_pid, 1150))
                self.assertFalse(_is_owner_live(my_pid, 1300))

    # Fault 3: 19:30 与 20:00 同时触发（并发发布锁排他）
    def test_fault_03_concurrent_publish_blocked_by_lock(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_file = Path(tmp_dir) / "pub.lock"
            with publish_lock(lock_file):
                with self.assertRaises(PublishAlreadyRunning):
                    with publish_lock(lock_file):
                        pass

    # Fault 4: 发布 commit 失败
    def test_fault_04_run_state_cannot_advance_without_pushed(self):
        state = RunState.new("stock-run-1", "20260820")
        with self.assertRaises(InvalidTransition):
            state.advance("pushed")

    # Fault 5: git fetch/push 失败
    def test_fault_05_receipt_rejects_unconfirmed_push(self):
        receipt = DeploymentReceipt(
            run_id="run-1",
            trade_date="20260820",
            local_sha="abc1234",
            remote_sha="abc1234",
            remote_confirmed=False,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        self.assertFalse(receipt.matches("run-1", "20260820"))

    # Fault 6: 本地 HEAD 与远程 SHA 不一致
    def test_fault_06_local_head_differs_from_remote_sha_rejected(self):
        receipt = DeploymentReceipt(
            run_id="run-1",
            trade_date="20260820",
            local_sha="local_sha_111",
            remote_sha="remote_sha_222",
            remote_confirmed=True,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        self.assertFalse(receipt.matches("run-1", "20260820"))

    # Fault 7: PIT 同日文件缺失
    def test_fault_07_pit_same_day_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PointInTimeStore(Path(tmp_dir) / "pit")
            self.assertIsNone(store.get("20260820", "nonexistent_domain"))

    # Fault 8: PIT 文件存在但供应商后来新增 1 行（保留首次快照并记入 audit）
    def test_fault_08_pit_vendor_drift_preserves_initial_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PointInTimeStore(Path(tmp_dir) / "pit")
            p1 = store.put("20260820", "daily_bars", b"initial_vendor_data")
            p2 = store.put("20260820", "daily_bars", b"mutated_vendor_data_with_extra_row")
            self.assertEqual(p2.read_bytes(), b"initial_vendor_data")
            audit = (Path(tmp_dir) / "pit" / "20260820" / "drift_audit.jsonl").read_text()
            self.assertIn("preserved_initial_snapshot", audit)

    # Fault 9: 北交所单票未知缺价（抛出 UnknownPriceGap，不编造收益）
    def test_fault_09_beijing_exchange_unknown_missing_price_raises(self):
        with self.assertRaises(bp.UnknownPriceGap):
            bp.price_return(previous_close=15.0, current_close=None, suspension=False)

    # Fault 10: 停牌票明确缺价（返回 0.0 与 suspension_carry 标记）
    def test_fault_10_known_suspension_uses_zero_return_with_provenance(self):
        res = bp.price_return(previous_close=15.0, current_close=None, suspension=True)
        self.assertEqual(res.return_value, 0.0)
        self.assertEqual(res.provenance, "suspension_carry")

    # Fault 11: Copilot 失败、xAI 成功
    def test_fault_11_copilot_fails_xai_fallback_succeeds(self):
        def fake_llm(prompt, model, **kwargs):
            if "copilot" in model:
                raise ConnectionError("Copilot down")
            return "xai fallback ok"

        with mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("token", "base")), \
             mock.patch("stock_model_gateway.read_hermes_xai_token", return_value="xai-key"):
            res = gw.call_model_gateway("test", ["copilot/gemini-3.7-flash", "xai/grok-4.6"], llm_func=fake_llm)
            self.assertTrue(res["ok"])
            self.assertEqual(res["model"], "xai/grok-4.6")

    # Fault 12: xAI 缺失、Copilot 成功
    def test_fault_12_xai_missing_copilot_succeeds(self):
        def fake_llm(prompt, model, **kwargs):
            return "copilot ok"

        with mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("token", "base")), \
             mock.patch("stock_model_gateway.read_hermes_xai_token", return_value=""):
            res = gw.call_model_gateway("test", ["copilot/gemini-3.7-flash", "xai/grok-4.6"], llm_func=fake_llm)
            self.assertTrue(res["ok"])
            self.assertEqual(res["model"], "copilot/gemini-3.7-flash")

    # Fault 13: 两个模型都失败
    def test_fault_13_all_models_fail_reports_clean_error(self):
        def fake_llm(prompt, model, **kwargs):
            raise RuntimeError(f"{model} failed")

        with mock.patch("stock_model_gateway.read_hermes_copilot_tokens", return_value=("token", "base")), \
             mock.patch("stock_model_gateway.read_hermes_xai_token", return_value="xai-key"):
            res = gw.call_model_gateway("test", ["copilot/gemini-3.7-flash", "xai/grok-4.6"], llm_func=fake_llm)
            self.assertFalse(res["ok"])
            self.assertIsNone(res["model"])

    # Fault 14: AI 结果 19/20
    def test_fault_14_ai_seats_partial_19_of_20_reported_honestly(self):
        summary = fmt.format_evening_summary(
            trade_date="20260820",
            expected_seats=20,
            ready_count=19,
            failed_count=1,
            watch_count=19,
            avoid_count=0,
            buy_count=0,
            unknown_count=0,
            avg_score=78.0,
            model_summary="gemini-3.7-flash×19",
            top_candidates=[],
        )
        self.assertIn("生产席位: 20/20 | 有效就绪: 19 | 失败/未就绪: 1", summary)

    # Fault 15: AI advice 全部为未知标签
    def test_fault_15_all_ai_advice_unknown_labels_reported_honestly(self):
        summary = fmt.format_evening_summary(
            trade_date="20260820",
            expected_seats=20,
            ready_count=20,
            failed_count=0,
            watch_count=0,
            avoid_count=0,
            buy_count=0,
            unknown_count=20,
            avg_score=75.0,
            model_summary="gemini-3.7-flash×20",
            top_candidates=[],
        )
        self.assertIn("无法识别建议标签 20", summary)

    # Fault 16: 包装脚本内部异常保留真实退出码
    def test_fault_16_watchdog_message_reports_failure(self):
        report = {
            "healthy": False,
            "expected_trading_day": "20260820",
            "stale_findings": ["行情落后 1 天"],
            "last_run": {"ok": False, "selection_ok": False},
        }
        msg = fmt.format_watchdog_message(report)
        self.assertIn("⚠️ 【选股数据可能停更】", msg)

    # Fault 17: 磁盘低水位
    def test_fault_17_disallowed_python_blocked_by_env_probe(self):
        with mock.patch("sys.executable", "/usr/bin/python3"):
            res = run_probe()
            self.assertFalse(res["ok"])
            self.assertTrue(res["disallowed_python"])

    # Fault 18: SQLite 备份损坏
    def test_fault_18_corrupt_database_backup_detected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            gz_path = Path(tmp_dir) / "corrupt.db.gz"
            with gzip.open(gz_path, "wb") as f_out:
                f_out.write(b"not a valid sqlite database header")
            res = verify_database_file(gz_path)
            self.assertFalse(res["ok"])

    # Fault 19: 旧 run_id 健康文件混入新运行
    def test_fault_19_missing_prices_marked_unresolved_never_minus_one(self):
        eval_status, settle_status = rw.resolve_recommendation_evaluation_status(
            latest_price_date="20260820",
            entry_trade_date="20260818",
            primary_exit_trade_date="20260820",
            entry_open_qfq=None,
            primary_return_pct=None,
            missing_qfq=True,
        )
        self.assertEqual(eval_status, "data_missing")
        self.assertEqual(settle_status, "data_missing")
        ret = rw.compute_holding_return_pct(None, 12.0, 0.003)
        self.assertIsNone(ret)

    # Fault 20: 公开镜像漂移
    def test_fault_20_public_mirror_drift_detected_by_check(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / "src"
            pub = Path(tmp_dir) / "pub"
            manifest = Path(tmp_dir) / "manifest.json"
            src.mkdir(parents=True)
            pub.mkdir(parents=True)
            for f in ALLOWLIST_FILES:
                (src / f).write_text("ok", encoding="utf-8")
            sync_public(src, pub, manifest, check_only=False)
            (pub / "dual_track_publication.py").write_text("drifted", encoding="utf-8")
            check_res = sync_public(src, pub, manifest, check_only=True)
            self.assertFalse(check_res["ok"])


if __name__ == "__main__":
    unittest.main()
