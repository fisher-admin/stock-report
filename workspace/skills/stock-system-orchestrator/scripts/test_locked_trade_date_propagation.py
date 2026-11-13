"""2026-10-03: the publisher must receive the locked batch date, never the
originally requested wall-clock date (Oct 2: 20260930 data published as 20261002)."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import stage6_deploy_and_notify as s6  # noqa: E402


class LockedTradeDatePropagationTests(unittest.TestCase):
    def test_publisher_env_overrides_stale_requested_date(self):
        verdict = {"final_action": {"action": "observe_only"}}
        stale_env = {"OPENCLAW_TARGET_TRADE_DATE": "20261002", "GIT_TERMINAL_PROMPT": "0"}
        with mock.patch.object(s6, "git_network_env", return_value=dict(stale_env)), \
             mock.patch.object(s6, "run_managed", return_value=subprocess.CompletedProcess([], 1, "", "x")) as run, \
             mock.patch("pathlib.Path.exists", return_value=True):
            s6.deploy_published_repo(verdict, run_id="stock-20261002T113007Z", trade_date="20260930")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["OPENCLAW_TARGET_TRADE_DATE"], "20260930")
        self.assertEqual(env["TRADE_DATE"], "20260930")

    def test_halt_verdict_never_reaches_publisher(self):
        with mock.patch.object(s6, "run_managed") as run, mock.patch("pathlib.Path.exists", return_value=True):
            result = s6.deploy_published_repo({"final_action": {"action": "halt"}}, run_id="r", trade_date="20260930")
        self.assertFalse(result["ok"])
        run.assert_not_called()


class PipelineGateAutoSessionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent / "scripts"))
        import stock_pipeline_gate  # noqa: PLC0415

        self.gate = stock_pipeline_gate

    def test_closed_or_unknown_auto_date_is_refused(self):
        import exchange_calendar  # noqa: PLC0415

        with mock.patch.object(exchange_calendar, "classify_date", return_value={"status": "closed"}):
            report = self.gate.check_auto_session("20261002")
        self.assertEqual(report["primary_failure_code"], "SESSION_NOT_OPEN")
        with mock.patch.object(exchange_calendar, "classify_date", return_value={"status": "error", "reason": "missing"}):
            report = self.gate.check_auto_session("20261008")
        self.assertEqual(report["primary_failure_code"], "CALENDAR_UNAVAILABLE")
        with mock.patch.object(exchange_calendar, "classify_date", return_value={"status": "open"}):
            self.assertIsNone(self.gate.check_auto_session("20261008"))


if __name__ == "__main__":
    unittest.main()
