import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_business_status as dbs  # noqa: E402

NOW = datetime(2026, 10, 8, 20, 46)


class DailyBusinessStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Path(self.tmp.name)
        (self.h / "trading_calendar.json").write_text(json.dumps({
            "source": "rds_trade_cal", "generated_at": "2026-10-08 19:30:00", "from": "20260801",
            "to": "20261129", "open_dates": ["20260930", "20261008"]}))
        self.jobs = self.h / "jobs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, name, payload):
        path = self.h / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False))

    def status(self, rc=0):
        return dbs.build_status("20261008", health_dir=self.h, hermes_jobs=self.jobs, closure_rc=rc,
                                calendar_path=self.h / "trading_calendar.json", now=NOW)

    def test_upstream_failure_is_reported_not_masked(self):
        self.w("orchestrator_run.json", {"generated_at": "2026-10-08 19:30:07", "run_id": "r", "trade_date": "20261008",
                                         "selection_ok": False, "status": "failed", "failed_stage": "dataPreparation",
                                         "stages": [{"stage": "dataPreparation", "ok": False}]})
        self.w("publication_validation.json", {"checked_at": "2026-10-08T19:42:00+08:00", "passed": False,
                                               "blockers": ["VERDICT_HALT"]})
        text = dbs.render(self.status())
        self.assertIn("选股：❌ 失败（阶段 dataPreparation", text)
        self.assertIn("被最终发布检查拦截：VERDICT_HALT", text)
        self.assertIn("今日未播报", text)
        self.assertIn("收盘核算：✅ 完成", text)

    def test_success_requires_remote_confirmed_receipt_and_hermes_record(self):
        self.w("orchestrator_run.json", {"generated_at": "2026-10-08 19:30:07", "run_id": "r", "trade_date": "20261008",
                                         "selection_ok": True, "status": "succeeded", "stages": []})
        self.w("publication_validation.json", {"checked_at": "2026-10-08T19:42:00+08:00", "passed": True,
                                               "mode": "new_batch", "pushed_sha": "abc123", "push_outcome": "pushed"})
        self.w("deployment_receipt.json", {"remote_confirmed": True, "local_sha": "abc123", "remote_sha": "abc123"})
        self.w("delivery_receipt.json", {"status": "handed_to_hermes_stdout", "handed_at": "2026-10-08T20:00:03+08:00",
                                         "recorded_at": "2026-10-08T20:00:03+08:00", "trade_date": "20261008"})
        self.jobs.write_text(json.dumps({"jobs": [{"name": dbs.BROADCAST_JOB_NAME, "last_run_at": "2026-10-08T20:00:05+08:00",
                                                    "last_status": "ok", "last_delivery_error": None}]}))
        text = dbs.render(self.status())
        self.assertIn("已推送并远端确认 abc123", text)
        self.assertIn("Hermes 记录无投递错误（无消息ID）", text)
        self.jobs.write_text(json.dumps({"jobs": [{"name": dbs.BROADCAST_JOB_NAME, "last_run_at": "2026-10-08T20:00:05+08:00",
                                                    "last_status": "ok", "last_delivery_error": "discord timeout"}]}))
        self.assertIn("Hermes 记录异常", dbs.render(self.status()))

    def test_receipt_mismatch_is_not_reported_as_confirmed(self):
        self.w("publication_validation.json", {"checked_at": "2026-10-08T19:42:00+08:00", "passed": True,
                                               "mode": "new_batch", "pushed_sha": "abc123"})
        self.w("deployment_receipt.json", {"remote_confirmed": True, "local_sha": "zzz", "remote_sha": "zzz"})
        self.assertIn("远端回执不一致", dbs.render(self.status()))

    def test_closure_failure_and_missing_run(self):
        text = dbs.render(self.status(rc=1))
        self.assertIn("今日没有主管线运行记录", text)
        self.assertIn("收盘核算：⚠️ 异常（退出码 1）", text)

    def test_cli_writes_durable_record(self):
        proc = subprocess.run([sys.executable, str(Path(dbs.__file__)), "--date", "20261008", "--closure-rc", "0",
                               "--health-dir", str(self.h), "--hermes-jobs", str(self.jobs)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.h / "daily_business_status/20261008.json").is_file())

    def test_zero_paid_retired_mode_renders_cleanly(self):
        ws = self.h / "ws"
        h = ws / "stock_data/03-working/health"
        h.mkdir(parents=True, exist_ok=True)
        (ws / "config").mkdir(parents=True, exist_ok=True)
        (ws / "config/v43_retirement.json").write_text(json.dumps({"retired": True}))
        (h / "trading_calendar.json").write_text(json.dumps({
            "source": "selfheal", "generated_at": "2026-11-20 19:30:00", "from": "20260801",
            "to": "20270119", "open_dates": ["20261119", "20261120"]}))
        (h / "zero_paid_stage1.json").write_text(json.dumps({
            "target_trade_date": "20261120", "ok": True, "seconds": 120.5,
            "steps": {"daily": {"rows": 5572}, "pit_snapshot": {"rows": {"universe": 5924}}}}))
        (h / "zero_paid_daily").mkdir(parents=True, exist_ok=True)
        (h / "zero_paid_daily/run_20261120.json").write_text(json.dumps({
            "date": "20261120", "problems": [], "reconcile": {"verdict": "ok"},
            "prices": {"verdict": "ok"}, "em_ann": {"status": "ok"}}))
        (h / "ew6_publish").mkdir(parents=True, exist_ok=True)
        (h / "ew6_publish/publication_validation.json").write_text(json.dumps({
            "passed": True, "batch": "ew6_20261120", "mode": "v43_retired_ew6",
            "pushed_sha": "55d733bf28f8d2ccd498d1424966b39a9c7e4241", "pushed_at": "2026-11-20T23:45:28+08:00"}))
        (h / "deployment_receipt.json").write_text(json.dumps({
            "remote_confirmed": True, "local_sha": "55d733bf28f8d2ccd498d1424966b39a9c7e4241"}))
        fs_dir = ws / "factor_factory/data/forward_shadow"
        fs_dir.mkdir(parents=True, exist_ok=True)
        (fs_dir / "state.json").write_text(json.dumps({
            "signal_date": "20261120", "status": "recorded", "is_rebalance": False,
            "universe": 2248, "composite_coverage": 0.9987}))
        jobs = self.h / "jobs_zp.json"
        jobs.write_text(json.dumps({"jobs": [{
            "name": "股票系统-多账本前向影子multi_v1·晚间",
            "last_run_at": "2026-11-20T23:50:16+08:00", "last_status": "ok", "last_error": None}]}))
        st = dbs.build_status("20261120", health_dir=h, hermes_jobs=jobs, closure_rc=0,
                              calendar_path=h / "trading_calendar.json", now=datetime(2026, 11, 20, 23, 55))
        self.assertEqual(st["mode"], "zero_paid_v43_retired")
        text = dbs.render(st)
        self.assertIn("零付费模式", text)
        self.assertIn("盘后准备(stage1)：✅ 完成", text)
        self.assertIn("零付费核对(A1/A4/A5)：✅ 全部通过", text)
        self.assertIn("EW6 影子记账：✅ 已记录", text)
        self.assertIn("页面发布(#/today EW6)：✅ 已推送并远端确认 55d733bf28f8", text)
        self.assertIn("多账本影子(multi_v1)：✅ 已就绪", text)
        self.assertNotIn("今日没有主管线运行记录", text)
        self.assertNotIn("远端回执不一致", text)

    def test_default_hermes_jobs_prefers_hermes_home(self):
        import os
        from unittest import mock
        (self.h / "cron").mkdir()
        (self.h / "cron/jobs.json").write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.h)}):
            self.assertEqual(dbs.default_hermes_jobs(), self.h / "cron/jobs.json")


if __name__ == "__main__":
    unittest.main()
