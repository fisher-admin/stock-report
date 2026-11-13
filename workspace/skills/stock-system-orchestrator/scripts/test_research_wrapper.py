import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
WRAPPER = WORKSPACE / "ops/hermes/stock/post_publish_research.sh"

class ResearchWrapperTests(unittest.TestCase):
    def run_wrapper(self, exit_code, prelude=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "skills/stock-system-orchestrator/scripts/run_post_publish_research.py"
            script.parent.mkdir(parents=True)
            script.write_text(prelude + f"\nprint('test stage result: official price unavailable')\nraise SystemExit({exit_code})\n")
            (script.parent / "exchange_calendar.py").write_text("raise SystemExit(0)\n")  # open trade day
            env = {**os.environ, "STOCK_SYSTEM_WORKSPACE_FORCE": str(root), "STOCK_SYSTEM_PYTHON_FORCE": sys.executable}
            return subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=10)

    def test_success_is_silent(self):
        result = self.run_wrapper(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_wrapper_stamps_monotonic_start_and_enables_existing_target_alerts(self):
        result = self.run_wrapper(0, "import os,time; assert 0 < float(os.environ['STOCK_RESEARCH_STARTED_MONOTONIC']) <= time.monotonic(); assert os.environ['STOCK_RESEARCH_ALERTS'] == '1'")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failure_preserves_exit_code_and_actionable_message(self):
        result = self.run_wrapper(9)
        self.assertEqual(result.returncode, 9)
        self.assertIn("official price unavailable", result.stdout)
        self.assertIn("9", result.stdout)

if __name__ == "__main__":
    unittest.main()
