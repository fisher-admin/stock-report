import json
import tempfile
import unittest
from pathlib import Path

try:
    from stock_scheduler_guard import scan_stock_jobs, job_fingerprint
except ImportError:
    scan_stock_jobs = None
    job_fingerprint = None


def stock_job(job_id: str, name: str, expr: str) -> dict:
    return {
        "id": job_id,
        "name": name,
        "enabled": True,
        "schedule": {"kind": "cron", "expr": expr},
        "script": "stock/stock_pipeline_gate.sh",
        "workdir": "/Users/fisher/.openclaw/workspace",
    }


def write_jobs(home: Path, jobs: list[dict]) -> None:
    path = home / "cron/jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8")


class StockSchedulerGuardTests(unittest.TestCase):
    def test_duplicate_enabled_stock_jobs_across_profiles_fail(self):
        if scan_stock_jobs is None:
            self.fail("scan_stock_jobs is not implemented yet")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root"
            ai = root / "profiles" / "ai"
            write_jobs(root, [stock_job("root-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")])
            write_jobs(ai, [stock_job("ai-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")])
            report = scan_stock_jobs(root)
            self.assertFalse(report["ok"])
            self.assertEqual(report["duplicates"][0]["profiles"], ["ai", "root"])

    def test_single_profile_stock_jobs_pass(self):
        if scan_stock_jobs is None:
            self.fail("scan_stock_jobs is not implemented yet")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root"
            ai = root / "profiles" / "ai"
            write_jobs(root, [stock_job("root-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")])
            job_ai = stock_job("ai-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")
            job_ai["enabled"] = False
            write_jobs(ai, [job_ai])
            report = scan_stock_jobs(root)
            self.assertTrue(report["ok"])
            self.assertEqual(report["duplicates"], [])



    def test_main_cli_returns_78_on_duplicate(self):
        import io
        import stock_scheduler_guard as guard
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root"
            ai = root / "profiles" / "ai"
            write_jobs(root, [stock_job("root-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")])
            write_jobs(ai, [stock_job("ai-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")])

            stderr_buf = io.StringIO()
            with mock.patch("sys.argv", ["stock_scheduler_guard.py", "--hermes-home", str(root)]),                  mock.patch("sys.stderr", stderr_buf):
                ret = guard.main()
                self.assertEqual(ret, 78)
                self.assertIn("Duplicate enabled stock jobs found", stderr_buf.getvalue())

    def test_main_cli_returns_0_when_clean(self):
        import stock_scheduler_guard as guard
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root"
            write_jobs(root, [stock_job("root-1", "股票系统-A股选股与双轨观察", "30 19 * * 1-5")])

            with mock.patch("sys.argv", ["stock_scheduler_guard.py", "--hermes-home", str(root)]):
                ret = guard.main()
                self.assertEqual(ret, 0)

if __name__ == "__main__":
    unittest.main()
