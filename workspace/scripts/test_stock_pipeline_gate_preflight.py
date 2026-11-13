#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import stock_pipeline_gate as gate  # noqa: E402


def failed_preflight_orchestrator() -> dict:
    # Shape of the 2026-10-20..22 manifests after ~/.local/share/uv was wiped.
    return {
        "run_id": "stock-20261022T113018Z",
        "ok": False,
        "status": "failed",
        "warnings": [],
        "preflight": {
            "ok": False,
            "reason": "python environment drift probe failed; see preflight.env_drift_probe.report",
            "env_drift_probe": {
                "ok": False,
                "report": {
                    "ok": False,
                    "locked_environments": {
                        "orchestrator": {"ok": True, "path": "/venv/bin/python"},
                        "ai": {
                            "ok": False,
                            "path": "/workspace/daily_stock_analysis/venv/bin/python3",
                            "error": "executable_not_found_or_not_executable",
                        },
                    },
                    "modules": {"pandas": {"ok": True}},
                },
            },
        },
    }


class PreflightRootCauseTests(unittest.TestCase):
    def test_preflight_failure_names_the_broken_environment(self) -> None:
        causes = gate.collect_root_causes(failed_preflight_orchestrator(), {}, {}, {}, "stock-20261022T113018Z")
        self.assertTrue(causes[0].startswith("preflight: python environment drift probe failed"))
        self.assertIn(
            "locked_env ai: executable_not_found_or_not_executable "
            "(/workspace/daily_stock_analysis/venv/bin/python3)",
            causes,
        )
        self.assertFalse(any("remote publication did not complete" in cause for cause in causes))

    def test_preflight_failure_is_classified_before_publish_blocked(self) -> None:
        report = {
            "ok": False,
            "published": False,
            "pipeline_returncode": 1,
            "root_causes": gate.collect_root_causes(failed_preflight_orchestrator(), {}, {}, {}, ""),
        }
        codes = gate.classify_failure_codes(report, {}, {}, {"ok": True})
        self.assertEqual(codes[:2], ["ENVIRONMENT_DRIFT", "PREFLIGHT_FAILED"])

    def test_deploy_fallback_kept_when_nothing_else_explains_failure(self) -> None:
        causes = gate.collect_root_causes({"run_id": "x", "publish_ok": False}, {}, {}, {}, "x")
        self.assertEqual(causes, ["deploy: remote publication did not complete"])


class MarketHaltTests(unittest.TestCase):
    # Shape of the 2026-10-26 manifest (morning + midday regime 高风险, risk 85).
    HALTED = {
        "run_id": "stock-20261026T113001Z",
        "status": "failed",
        "failure_category": "market_halt",
        "failed_stage": "marketGate",
        "publish_ok": False,
        "error": "MARKET_HALTED",
        "self_healing": {"policy_halt": {"codes": ["MARKET_BLOCKED"], "regime": "高风险", "risk_score": 85}},
    }

    def test_market_halt_is_reported_as_business_rule(self) -> None:
        proc = subprocess.CompletedProcess(["run_pipeline.py"], 4, "", "")
        report, _ = gate.build_report(proc, self.HALTED, {}, {}, {})
        self.assertEqual(report["failure_codes"], ["MARKET_HALTED"])
        self.assertEqual(report["primary_failure_code"], "MARKET_HALTED")
        self.assertEqual(report["root_causes"], ["market_gate: 市场高风险，风险分 85，按规则暂停今日选股与发布（非系统故障）"])

    def test_other_failures_are_not_market_halts(self) -> None:
        self.assertIsNone(gate.market_halt(failed_preflight_orchestrator()))


if __name__ == "__main__":
    unittest.main()
