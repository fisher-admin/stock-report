"""2026-10-03: Stage 1's internal retry deadline must end inside the
orchestrator's dataPreparation timeout (previously 7200s vs 2400s)."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_pipeline  # noqa: E402


class Stage1BudgetAlignmentTests(unittest.TestCase):
    def test_default_budget_is_clamped_below_stage_timeout(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCLAW_STAGE1_MAX_WAIT_SECONDS", None)
            env = run_pipeline.build_run_env("run", "t", "20261008")
        budget = int(env["OPENCLAW_STAGE1_MAX_WAIT_SECONDS"])
        self.assertLess(budget, run_pipeline.STAGE_TIMEOUT_SECONDS["dataPreparation"])
        self.assertEqual(budget, run_pipeline.STAGE_TIMEOUT_SECONDS["dataPreparation"] - 180)

    def test_oversized_override_is_clamped_but_smaller_override_kept(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_STAGE1_MAX_WAIT_SECONDS": "7200"}):
            env = run_pipeline.build_run_env("run", "t")
        self.assertEqual(int(env["OPENCLAW_STAGE1_MAX_WAIT_SECONDS"]), run_pipeline.STAGE_TIMEOUT_SECONDS["dataPreparation"] - 180)
        with mock.patch.dict(os.environ, {"OPENCLAW_STAGE1_MAX_WAIT_SECONDS": "900"}):
            env = run_pipeline.build_run_env("run", "t")
        self.assertEqual(env["OPENCLAW_STAGE1_MAX_WAIT_SECONDS"], "900")


if __name__ == "__main__":
    unittest.main()
