#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evening_chain_preflight as preflight  # noqa: E402


class EveningChainPreflightTests(unittest.TestCase):
    def test_silent_when_every_check_passes(self):
        results = preflight.run_checks((("a", lambda: []), ("b", lambda: [])))
        self.assertEqual(preflight.render(results), "")

    def test_findings_are_listed_with_count(self):
        text = preflight.render({"environment": ["Python 环境 ai 不可用"], "disk": ["磁盘剩余 2.1 GB"]})
        self.assertIn("发现 2 个问题", text)
        self.assertIn("- Python 环境 ai 不可用", text)

    def test_broken_check_becomes_a_finding_not_a_crash(self):
        def broken():
            raise OSError("boom")

        results = preflight.run_checks((("locks", broken),))
        self.assertIn("预检项 locks 自身出错", results["locks"][0])

    def test_stale_git_lock_reported_only_without_running_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            lock = repo / ".git/index.lock"
            lock.write_text("")
            old = time.time() - 2 * preflight.STALE_LOCK_SECONDS
            os.utime(lock, (old, old))
            with mock.patch.object(preflight, "GIT_REPOS", (repo,)), \
                    mock.patch.object(preflight, "PIPELINE_LOCK", repo / "none"):
                with mock.patch.object(preflight, "_git_running", return_value=False):
                    self.assertEqual(len(preflight.check_locks()), 1)
                with mock.patch.object(preflight, "_git_running", return_value=True):
                    self.assertEqual(preflight.check_locks(), [])

    def test_fresh_git_lock_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / ".git/index.lock").write_text("")
            with mock.patch.object(preflight, "GIT_REPOS", (repo,)), \
                    mock.patch.object(preflight, "PIPELINE_LOCK", repo / "none"):
                self.assertEqual(preflight.check_locks(), [])

    def test_dead_pipeline_lock_owner_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            lockdir = Path(tmp) / "stock_pipeline_gate.lockdir"
            lockdir.mkdir()
            (lockdir / "owner.txt").write_text("999999|Thu|token\n")
            with mock.patch.object(preflight, "GIT_REPOS", ()), mock.patch.object(preflight, "PIPELINE_LOCK", lockdir):
                self.assertIn("999999", preflight.check_locks()[0])

    def test_low_disk_is_reported(self):
        usage = mock.Mock(free=int(1.5 * 1024 ** 3))
        with mock.patch.object(preflight.shutil, "disk_usage", return_value=usage):
            self.assertIn("1.5 GB", preflight.check_disk()[0])

    def test_wrapper_drift_is_reported_with_deploy_command(self):
        preflight.check_wrappers()  # imports ops/hermes/deploy
        deploy = sys.modules["deploy"]
        with mock.patch.object(deploy, "drift", return_value=[("a.sh", "changed"), ("b.sh", "missing")]):
            issue = preflight.check_wrappers()[0]
        self.assertIn("a.sh（与仓库不一致）", issue)
        self.assertIn("b.sh（缺失）", issue)
        self.assertIn("deploy.py deploy", issue)
        with mock.patch.object(deploy, "drift", return_value=[]):
            self.assertEqual(preflight.check_wrappers(), [])

    def test_zero_paid_retired_uses_zero_paid_checks(self):
        with mock.patch.object(preflight, "_is_v43_retired", return_value=True), \
                mock.patch.object(preflight, "ZERO_PAID_CHECKS", (("tdx_api", lambda: []),)):
            results = preflight.run_checks()
        self.assertEqual(results, {"tdx_api": []})

    def test_zero_paid_checks_include_em_ann_store_and_price_crosscheck(self):
        names = [n for n, _ in preflight.ZERO_PAID_CHECKS]
        self.assertIn("em_ann_store", names)
        self.assertIn("price_crosscheck", names)

    def test_em_ann_store_missing_fresh_and_stale(self):
        now = datetime(2026, 11, 26, 16, 30)
        core_ok = {
            "forecast": {"last_ok_at": "2026-11-25 22:50:06", "through": "20261125"},
            "express": {"last_ok_at": "2026-11-25 22:50:13", "through": "20261125"},
            "report": {"last_ok_at": "2026-11-25 22:50:22", "through": "20261125"},
            "income": {"last_ok_at": "2026-11-25 22:50:29", "through": "20261125"},
            "cashflow": {"last_ok_at": "2026-11-26 11:52:21", "through": "20261126"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with mock.patch.object(preflight, "EM_ANN_STORE_STATE", state):
                self.assertIn("缺失", preflight.check_em_ann_store(now)[0])
                # All 5 core tables fresh + appoint 7 days old (<= 9 days Monday cadence) -> clean
                state.write_text(json.dumps({
                    **core_ok,
                    "appoint": {"last_ok_at": "2026-11-19 22:50:55"},
                    "cninfo_corrections": {"through": "20261125"},
                }), encoding="utf-8")
                self.assertEqual(preflight.check_em_ann_store(now), [])
                # appoint > 9 days -> stale
                state.write_text(json.dumps({
                    **core_ok,
                    "appoint": {"last_ok_at": "2026-11-16 22:50:55"},
                }), encoding="utf-8")
                issues = preflight.check_em_ann_store(now)
                self.assertEqual(len(issues), 1)
                self.assertIn("appoint", issues[0])
                # missing core key (e.g. cashflow) -> reported
                missing_cf = {k: v for k, v in core_ok.items() if k != "cashflow"}
                state.write_text(json.dumps(missing_cf), encoding="utf-8")
                issues = preflight.check_em_ann_store(now)
                self.assertEqual(len(issues), 1)
                self.assertIn("cashflow", issues[0])
                # core table > 5 days -> stale
                stale_inc = {**core_ok, "income": {"last_ok_at": "2026-11-20 22:50:29"}}
                state.write_text(json.dumps(stale_inc), encoding="utf-8")
                issues = preflight.check_em_ann_store(now)
                self.assertEqual(len(issues), 1)
                self.assertIn("income", issues[0])
                # unparseable last_ok_at
                bad_ts = {**core_ok, "forecast": {"last_ok_at": "garbage"}}
                state.write_text(json.dumps(bad_ts), encoding="utf-8")
                self.assertIn("无法解析", preflight.check_em_ann_store(now)[0])
                state.write_text("{not json", encoding="utf-8")
                self.assertIn("损坏", preflight.check_em_ann_store(now)[0])

    def test_price_crosscheck_newest_verdict_and_exdiv(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "price_crosscheck"
            with mock.patch.object(preflight, "CROSSCHECK_DIR", d):
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("缺少价格交叉核对报告", issues[0])  # missing dir fails closed
                d.mkdir()
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("缺少价格交叉核对报告", issues[0])  # empty dir fails closed
                (d / "crosscheck_20261124.json").write_text(json.dumps({"verdict": "mismatch"}), encoding="utf-8")
                (d / "crosscheck_20261125.json").write_text(
                    json.dumps({"verdict": "ok", "pre_close_exdiv_ok": None, "pre_close_exdiv_status": "events_missing"}),
                    encoding="utf-8",
                )
                self.assertEqual(preflight.check_price_crosscheck(), [])  # only the newest counts
                (d / "crosscheck_20261126.json").write_text(
                    json.dumps({"verdict": "ok", "pre_close_exdiv_ok": False, "pre_close_exdiv_status": "mismatch"}),
                    encoding="utf-8",
                )
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("pre_close_exdiv_ok=False", issues[0])
                (d / "crosscheck_20261127.json").write_text(
                    json.dumps({"verdict": "ok", "pre_close_exdiv_ok": None, "pre_close_exdiv_status": "error:RuntimeError"}),
                    encoding="utf-8",
                )
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("error:RuntimeError", issues[0])
                (d / "crosscheck_20261128.json").write_text(json.dumps({"verdict": "degraded"}), encoding="utf-8")
                self.assertIn("crosscheck_20261128.json", preflight.check_price_crosscheck()[0])
                (d / "crosscheck_20261129.json").write_text("{bad", encoding="utf-8")
                self.assertIn("损坏", preflight.check_price_crosscheck()[0])
                (d / "crosscheck_20261130.json").write_text(
                    json.dumps({
                        "verdict": "ok",
                        "tdx_source": ["tdx_api"],
                        "pre_close_exdiv_ok": None,
                        "pre_close_exdiv_status": "events_missing",
                    }),
                    encoding="utf-8",
                )
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("events_missing", issues[0])
                (d / "crosscheck_20261201.json").write_text(
                    json.dumps({
                        "verdict": "ok",
                        "pre_close_exdiv_ok": True,
                        "pre_close_exdiv_status": "ok",
                        "pre_close_exdiv_unavailable_rows": 3,
                    }),
                    encoding="utf-8",
                )
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("unavailable_rows=3", issues[0])
                # Legacy payload without pre_close_exdiv_* keys (e.g. historical crosscheck_20261125.json) passes when clean
                (d / "crosscheck_20261202.json").write_text(
                    json.dumps({"verdict": "ok", "tdx_source": ["tdx_api"]}),
                    encoding="utf-8",
                )
                self.assertEqual(preflight.check_price_crosscheck(), [])
                # tdx_api + pre_close_exdiv_ok: None (with exdiv keys present) fails closed
                (d / "crosscheck_20261203.json").write_text(
                    json.dumps({
                        "verdict": "ok",
                        "tdx_source": ["tdx_api"],
                        "pre_close_exdiv_ok": None,
                    }),
                    encoding="utf-8",
                )
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("pre_close_exdiv_ok=None", issues[0])
                # tdx_api + pre_close_exdiv_status present without pre_close_exdiv_ok=True also fails closed
                (d / "crosscheck_20261204.json").write_text(
                    json.dumps({
                        "verdict": "ok",
                        "tdx_source": "tdx_api",
                        "pre_close_exdiv_status": "skipped",
                    }),
                    encoding="utf-8",
                )
                issues = preflight.check_price_crosscheck()
                self.assertEqual(len(issues), 1)
                self.assertIn("pre_close_exdiv_ok=None", issues[0])

    def test_shadow_contracts_includes_v2_scripts_and_spec_hash(self):
        import hashlib
        for expected in (
            "factor_factory/contracts/forward_shadow_v2.json",
            "factor_factory/scripts/aegis_scorer.py",
            "factor_factory/scripts/forward_shadow.py",
            "factor_factory/scripts/forward_shadow_multi.py",
            "factor_factory/scripts/forward_shadow_v2.py",
            "factor_factory/scripts/core7p_native_scorer.py",
            "factor_factory/scripts/zero_paid_fin_v2.py",
        ):
            self.assertIn(expected, preflight.FROZEN_SHADOW_CONTRACTS)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            v2_rel = "factor_factory/contracts/forward_shadow_v2.json"
            v2_file = ws / v2_rel
            v2_file.parent.mkdir(parents=True)
            body = {"spec_id": "forward_shadow_v2", "k": 1}
            good_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
            v2_file.write_text(json.dumps({**body, "spec_hash": good_hash, "frozen_at": "2026-11-26"}), encoding="utf-8")
            with mock.patch.object(preflight, "WORKSPACE", ws),                     mock.patch.object(preflight, "FROZEN_SHADOW_CONTRACTS", (v2_rel,)),                     mock.patch.object(preflight.subprocess, "run", return_value=mock.Mock(returncode=0)):
                self.assertEqual(preflight.check_shadow_contracts(), [])
                v2_file.write_text(json.dumps({**body, "spec_hash": "0000000000000000"}), encoding="utf-8")
                issues = preflight.check_shadow_contracts()
                self.assertEqual(len(issues), 1)
                self.assertIn("spec_hash", issues[0])

    def test_calendar_check_reports_missing_2027_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Path(tmp)
            (h / "trading_calendar.json").write_text('{"open_dates": ["20261120"]}', encoding="utf-8")
            with mock.patch.object(preflight, "HEALTH_DIR", h):
                issues = preflight.check_calendar()
            self.assertEqual(len(issues), 1)
            self.assertIn("2027", issues[0])
            (h / "calendar_years").mkdir()
            (h / "calendar_years/calendar_2027.json").write_text('{"open_dates": ["20270104"]}', encoding="utf-8")
            with mock.patch.object(preflight, "HEALTH_DIR", h):
                self.assertEqual(preflight.check_calendar(), [])


if __name__ == "__main__":
    unittest.main()
