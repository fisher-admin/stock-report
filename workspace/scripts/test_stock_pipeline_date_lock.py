#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ANALYZER = SCRIPTS.parent / "skills/stock-analyzer"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ANALYZER))

import stock_pipeline_gate as gate  # noqa: E402
import run_strategy_suite as suite  # noqa: E402


class DateLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = os.environ.pop("OPENCLAW_TARGET_TRADE_DATE", None)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("OPENCLAW_TARGET_TRADE_DATE", None)
        else:
            os.environ["OPENCLAW_TARGET_TRADE_DATE"] = self._old

    def test_cli_wins(self) -> None:
        date, source = gate.resolve_target_trade_date("20260817", now_local=datetime(2026, 8, 17, 20, 50))
        self.assertEqual((date, source), ("20260817", "cli"))

    def test_post_close_defaults_to_today(self) -> None:
        date, source = gate.resolve_target_trade_date(None, now_local=datetime(2026, 8, 17, 20, 15))
        self.assertEqual((date, source), ("20260817", "auto-post-close-today"))

    def test_pre_close_does_not_imply_today(self) -> None:
        date, source = gate.resolve_target_trade_date(None, now_local=datetime(2026, 8, 17, 10, 0))
        self.assertNotEqual(date, "20260817")
        self.assertTrue(source.startswith("auto-pre-close-rerun:") or source == "")

    def test_strategy_suite_refuses_missing_target(self) -> None:
        os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260817"
        with self.assertRaisesRegex(RuntimeError, "拒绝回退到旧日"):
            suite.resolve_signal_trade_date(["20260813", "20260814"])

    def test_strategy_suite_clips_to_target(self) -> None:
        os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260814"
        dates, latest = suite.resolve_signal_trade_date(["20260813", "20260814", "20260817"])
        self.assertEqual(latest, "20260814")
        self.assertEqual(dates, ["20260813", "20260814"])


class GateReportTests(unittest.TestCase):
    def test_deploy_failure_cannot_be_reported_as_success(self) -> None:
        run_id = "stock-20260818T113011Z"
        run = {"run_id": run_id, "trade_date": "20260818"}
        proc = subprocess.CompletedProcess(["run_pipeline.py"], 1, stdout="", stderr="")
        orchestrator = {
            "run_id": run_id,
            "run": run,
            "selection_ok": True,
            "research_ok": False,
            "publish_ok": False,
            "warnings": ["optional research failed"],
            "deploy": {"deploy": {"error": "v2 publisher failed"}},
        }
        ai_publish = {
            "run": run,
            "published": True,
            "publish_mode": "full",
            "checks": {"ai_status_counts": {"ready": 20, "pending": 0, "total": 20}},
        }
        validation = {"run": run, "ok": True, "publish_ready": True, "ai_complete": True}
        recommendation_sync = {"run": run, "ok": True}

        report, current_run_id = gate.build_report(
            proc,
            orchestrator,
            ai_publish,
            validation,
            recommendation_sync,
        )

        self.assertEqual(current_run_id, run_id)
        self.assertFalse(report["ok"])
        self.assertTrue(report["selection_ok"])
        self.assertFalse(report["research_ok"])
        self.assertFalse(report["publish_ok"])
        self.assertFalse(report["published"])
        self.assertIn("PUBLISH_BLOCKED", report["failure_codes"])
        self.assertTrue(any("v2 publisher failed" in cause for cause in report["root_causes"]))


if __name__ == "__main__":
    unittest.main()
