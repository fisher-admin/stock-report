import unittest
from datetime import datetime, time

import data_freshness_watchdog as watchdog


class WatchdogAuxiliaryTests(unittest.TestCase):
    def test_missing_delivery_and_stale_research_are_not_healthy(self):
        findings = watchdog.check_auxiliary_runs(
            expected_trade_date="20260828",
            last_run={"ok": True, "run_id": "run-28", "trade_date": "20260828", "publish_ok": True},
            delivery={"run_id": "run-27", "trade_date": "20260827"},
            post_research={"run_id": "run-27", "trade_date": "20260827", "all_ok": True},
            factory_status={"run_id": "run-28", "trade_date": "20260828", "state": "running"},
        )
        self.assertTrue(any("播报" in item for item in findings))
        self.assertTrue(any("后置研究" in item for item in findings))
        self.assertTrue(any("running" in item or "运行中" in item for item in findings))

    def test_matching_auxiliary_states_are_clean(self):
        findings = watchdog.check_auxiliary_runs(
            expected_trade_date="20260828",
            last_run={"ok": True, "run_id": "run-28", "trade_date": "20260828", "publish_ok": True},
            delivery={"run_id": "run-28", "trade_date": "20260828"},
            post_research={"run_id": "run-28", "trade_date": "20260828", "all_ok": True},
            factory_status={"run_id": "run-28", "trade_date": "20260828", "state": "completed"},
        )
        self.assertEqual(findings, [])

    def test_recent_running_auxiliary_job_is_not_reported_as_failed(self):
        findings = watchdog.check_auxiliary_runs(
            expected_trade_date="20260828",
            last_run={"ok": True, "run_id": "run-28", "trade_date": "20260828", "publish_ok": True},
            delivery={"run_id": "run-28", "trade_date": "20260828"},
            post_research={
                "run_id": "run-28",
                "trade_date": "20260828",
                "state": "running",
                "started_at": "2026-08-28 21:00:00",
                "all_ok": None,
            },
            factory_status={
                "run_id": "run-28",
                "trade_date": "20260828",
                "state": "running",
                "started_at": "2026-08-28 21:00:00",
            },
            now_local=datetime.fromisoformat("2026-08-28T21:05:00+08:00"),
            running_grace_seconds=3600,
        )
        self.assertEqual(findings, [])

    def test_research_scheduled_later_is_not_reported_early(self):
        findings = watchdog.check_auxiliary_runs(
            expected_trade_date="20260903",
            last_run={"ok": True, "run_id": "run-03", "trade_date": "20260903", "publish_ok": True},
            delivery={"run_id": "run-03", "trade_date": "20260903"},
            post_research={"run_id": "run-02", "trade_date": "20260902", "all_ok": True},
            factory_status={"run_id": "run-02", "trade_date": "20260902", "state": "completed"},
            now_local=datetime.fromisoformat("2026-09-03T21:05:00+08:00"),
            post_research_due_time=time(22, 15),
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
