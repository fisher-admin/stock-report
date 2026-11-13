#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("health_alert.py")
SPEC = importlib.util.spec_from_file_location("stock_health_alert", MODULE_PATH)
health_alert = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(health_alert)


class HealthAlertRuntimeTests(unittest.TestCase):
    def test_hermes_runtime_bypasses_retired_openclaw_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            flag = Path(tmpdir) / "stock_alerts.disabled"
            flag.write_text("disabled", encoding="utf-8")

            self.assertTrue(health_alert.alerts_disabled(disable_flag=flag, runtime_owner="openclaw"))
            self.assertFalse(health_alert.alerts_disabled(disable_flag=flag, runtime_owner="hermes"))

    def test_hermes_runtime_prints_but_does_not_send_directly(self) -> None:
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"STOCK_HEALTH_RUNTIME": "hermes"}), mock.patch.object(
            health_alert, "collect_issues", return_value=["测试告警"]
        ), mock.patch.object(health_alert, "send_discord") as send, mock.patch.object(
            sys, "argv", ["health_alert.py"]
        ), contextlib.redirect_stdout(output):
            health_alert.main()

        self.assertIn("测试告警", output.getvalue())
        self.assertIn("delegated-to-hermes", output.getvalue())
        send.assert_not_called()

    def test_deprecated_governance_and_archived_s3_do_not_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def fake_load(path):
                name = Path(path).name
                if name == "governance_evaluation_latest.json":
                    return {
                        "deprecated": True,
                        "verdicts": {
                            "prebreakout_v41": {
                                "flags": ["governance_eval_deprecated", "do_not_use_for_promotion"]
                            }
                        },
                    }
                if name == "s3_tracking_status.json":
                    return {"watchlist_generated_at": "2026-01-01 00:00:00"}
                return {}

            with mock.patch.object(health_alert, "WS", root), mock.patch.object(
                health_alert, "HEALTH", root / "health"
            ), mock.patch.object(health_alert, "WORKING_REPO", root / "working"), mock.patch.object(
                health_alert, "load", side_effect=fake_load
            ), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STOCK_MONITOR_ARCHIVED_S3", None)
                issues = health_alert.collect_issues()

        self.assertFalse(any("governance_eval_deprecated" in issue for issue in issues))
        self.assertFalse(any("S3观察看板停更" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
