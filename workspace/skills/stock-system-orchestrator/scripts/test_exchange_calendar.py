import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exchange_calendar as ec  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "exchange_calendar.py"
OPEN = ["20260928", "20260929", "20260930", "20261008", "20261009"]


def write_calendar(path: Path, **overrides):
    payload = {
        "source": "rds_trade_cal",
        "generated_at": "2026-10-02 19:30:16",
        "from": "20250826",
        "to": "20261129",
        "open_dates": OPEN,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cal = write_calendar(self.dir / "trading_calendar.json")
        self.now = datetime(2026, 10, 3, 8, 0, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_and_closed_days(self):
        self.assertEqual(ec.classify_date("20260930", calendar_path=self.cal, now=self.now)["status"], "open")
        closed = ec.classify_date("20261002", calendar_path=self.cal, now=self.now)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["previous_open_date"], "20260930")
        self.assertEqual(closed["next_open_date"], "20261008")

    def test_missing_calendar_is_error_not_closed(self):
        result = ec.classify_date("20261002", calendar_path=self.dir / "nope.json", now=self.now)
        self.assertEqual(result["status"], "error")

    def test_corrupt_calendar_is_error(self):
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(ec.classify_date("20261002", calendar_path=bad, now=self.now)["status"], "error")

    def test_invalid_shapes_are_errors(self):
        cases = [
            {"open_dates": []},
            {"open_dates": ["2026-10-08"]},
            {"source": ""},
            {"from": "20261130"},
            {"generated_at": "yesterday"},
            {"open_dates": ["20270101"]},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                path = write_calendar(self.dir / "case.json", **overrides)
                self.assertEqual(ec.classify_date("20261002", calendar_path=path, now=self.now)["status"], "error")

    def test_stale_calendar_is_error(self):
        path = write_calendar(self.dir / "stale.json", generated_at="2026-07-01 19:30:00")
        result = ec.classify_date("20261002", calendar_path=path, now=self.now)
        self.assertEqual(result["status"], "error")
        self.assertIn("stale", result["reason"])

    def test_uncovered_date_is_error(self):
        path = write_calendar(self.dir / "short.json", to="20261001")
        self.assertEqual(ec.classify_date("20261002", calendar_path=path, now=self.now)["status"], "error")

    def test_require_open_session(self):
        ec.require_open_session("20261008", calendar_path=self.cal, now=self.now)
        with self.assertRaisesRegex(ec.CalendarError, "SESSION_NOT_OPEN"):
            ec.require_open_session("20261001", calendar_path=self.cal, now=self.now)
        with self.assertRaisesRegex(ec.CalendarError, "CALENDAR_UNAVAILABLE"):
            ec.require_open_session("20261001", calendar_path=self.dir / "x.json", now=self.now)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.health = Path(self.tmp.name)
        self.cal = write_calendar(self.health / "trading_calendar.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_closed_day_records_durable_skip(self):
        rc = ec.gate("morning_brief", date="20261002", calendar_path=self.cal, health_dir=self.health,
                     now=datetime(2026, 10, 2, 8, 0))
        self.assertEqual(rc, ec.EXIT_CLOSED)
        record = json.loads((self.health / "business_outcomes/20261002/morning_brief.json").read_text())
        self.assertEqual(record["outcome"], "skipped_market_closed")
        self.assertEqual(record["next_open_date"], "20261008")

    def test_open_day_writes_nothing(self):
        rc = ec.gate("morning_brief", date="20261008", calendar_path=self.cal, health_dir=self.health,
                     now=datetime(2026, 10, 8, 8, 0))
        self.assertEqual(rc, ec.EXIT_OPEN)
        self.assertFalse((self.health / "business_outcomes").exists())

    def test_calendar_error_records_and_fails(self):
        rc = ec.gate("midday_analysis", date="20261008", calendar_path=self.health / "missing.json",
                     health_dir=self.health, now=datetime(2026, 10, 8, 12, 0))
        self.assertEqual(rc, ec.EXIT_ERROR)
        record = json.loads((self.health / "business_outcomes/20261008/midday_analysis.json").read_text())
        self.assertEqual(record["outcome"], "calendar_error")

    def test_cli_uses_workspace_env(self):
        workspace = Path(self.tmp.name) / "ws"
        health = workspace / "stock_data/03-working/health"
        health.mkdir(parents=True)
        write_calendar(health / "trading_calendar.json", generated_at=datetime.now(ec.SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                       open_dates=["20250826"], to="20991231")
        env = {**os.environ, "STOCK_SYSTEM_WORKSPACE": str(workspace)}
        proc = subprocess.run([sys.executable, str(SCRIPT), "gate", "--job", "daily_closure"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, ec.EXIT_CLOSED, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        today = datetime.now(ec.SHANGHAI).strftime("%Y%m%d")
        self.assertTrue((health / f"business_outcomes/{today}/daily_closure.json").is_file())

    def test_cli_rejects_bad_job_name(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "gate", "--job", "../x"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, ec.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()
