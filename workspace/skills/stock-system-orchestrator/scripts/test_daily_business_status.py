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


if __name__ == "__main__":
    unittest.main()
