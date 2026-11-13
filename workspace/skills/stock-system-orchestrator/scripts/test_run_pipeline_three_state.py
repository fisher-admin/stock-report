#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import run_pipeline
from run_pipeline import compute_three_state_status


class MarketGateHaltTests(unittest.TestCase):
    @staticmethod
    def verdict(blockers: list[dict]) -> dict:
        return {
            "final_action": {"action": "halt"},
            "gates": {
                "market_gate": {
                    "summary": "市场高风险，直接阻断执行。",
                    "evidence": {"regime": "高风险", "risk_score": 85},
                    "blockers": blockers,
                }
            },
        }

    def test_market_block_skips_self_healing(self):
        payload: dict = {}
        verdict = self.verdict([{"code": "MARKET_BLOCKED"}])
        with mock.patch.object(run_pipeline, "load_system_verdict", return_value=verdict), \
                mock.patch.object(run_pipeline, "run_remediation_group") as remediate, \
                mock.patch.object(run_pipeline, "notify_warning") as warn:
            self.assertFalse(run_pipeline.maybe_self_heal({}, payload))
        remediate.assert_not_called()
        warn.assert_not_called()
        self.assertEqual(payload["self_healing"]["policy_halt"]["risk_score"], 85)
        self.assertEqual(payload["self_healing"]["attempts"], [])

    def test_mixed_blockers_still_self_heal(self):
        verdict = self.verdict([{"code": "MARKET_BLOCKED"}, {"code": "PRICE_DATE_STALE"}])
        self.assertIsNone(run_pipeline.policy_halt_for_verdict(verdict))


class ThreeStateStatusTests(unittest.TestCase):
    def test_terminal_failure_is_recorded_before_pipeline_exit(self):
        payload = {"ok": False, "status": "running"}
        with mock.patch.object(run_pipeline, "now_utc_iso", return_value="2026-09-03T01:00:00Z"):
            run_pipeline.mark_pipeline_failed(
                payload,
                failed_stage="strategySuite",
                failure_category="stage_timeout",
            )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed_stage"], "strategySuite")
        self.assertEqual(payload["completed_at"], "2026-09-03T01:00:00Z")
        self.assertEqual(payload["finished_at"], "2026-09-03T01:00:00Z")

    def test_terminal_success_is_recorded_after_publish(self):
        payload = {"ok": True, "status": "running"}
        with mock.patch.object(run_pipeline, "now_utc_iso", return_value="2026-09-03T01:00:00Z"):
            run_pipeline.mark_pipeline_succeeded(payload)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["completed_at"], "2026-09-03T01:00:00Z")

    def test_deploy_failure_does_not_erase_selection_success(self):
        payload = {
            "include_core_selection": True,
            "stages": [
                {"stage": "dataPreparation", "ok": True},
                {"stage": "coreSelection", "ok": True},
                {"stage": "strategySuite", "ok": True},
                {"stage": "validation", "ok": True},
                {"stage": "latestStateSync", "ok": True},
                {"stage": "prebreakoutShadowResearch", "ok": False},
            ],
            "deploy": {"ok": False, "error": "SSL"},
        }
        three = compute_three_state_status(payload)
        self.assertTrue(three["selection_ok"])
        self.assertFalse(three["research_ok"])
        self.assertFalse(three["publish_ok"])
        self.assertFalse(three["ok"])

    def test_all_green(self):
        payload = {
            "include_core_selection": False,
            "stages": [
                {"stage": "dataPreparation", "ok": True},
                {"stage": "strategySuite", "ok": True},
                {"stage": "validation", "ok": True},
                {"stage": "latestStateSync", "ok": True},
                {"stage": "prebreakoutDailyPan", "ok": True},
                {"stage": "prebreakoutShadowResearch", "ok": True},
            ],
            "deploy": {"ok": True},
        }
        three = compute_three_state_status(payload)
        self.assertTrue(three["selection_ok"])
        self.assertTrue(three["research_ok"])
        self.assertTrue(three["publish_ok"])
        self.assertTrue(three["ok"])

    def test_selection_fails_when_core_stage_fails(self):
        payload = {
            "include_core_selection": True,
            "stages": [
                {"stage": "dataPreparation", "ok": True},
                {"stage": "coreSelection", "ok": False},
                {"stage": "strategySuite", "ok": True},
                {"stage": "validation", "ok": True},
                {"stage": "latestStateSync", "ok": True},
            ],
            "deploy": {"ok": True},
        }
        three = compute_three_state_status(payload)
        self.assertFalse(three["selection_ok"])
        self.assertTrue(three["publish_ok"])

    def test_timeout_output_bytes_are_recorded_without_secondary_crash(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["fake"], timeout=1, output=b"partial\xff", stderr=b"error\xfe"
        )
        with mock.patch.object(run_pipeline.subprocess, "run", side_effect=timeout):
            result = run_pipeline.run_script("probe", Path("/tmp/fake.py"), {}, [])
        self.assertTrue(result["timeout"])
        self.assertIn("partial", result["stdout_tail"])
        self.assertIn("error", result["stderr_tail"])


if __name__ == "__main__":
    unittest.main()
