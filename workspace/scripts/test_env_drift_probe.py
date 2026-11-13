import unittest
from pathlib import Path
from unittest import mock

import env_drift_probe as probe


class EnvDriftProbeTests(unittest.TestCase):
    def test_disallowed_python_environment_blocks_probe(self):
        with mock.patch("sys.executable", "/usr/bin/python3"):
            res = probe.run_probe()
            self.assertFalse(res["ok"])
            self.assertTrue(res["disallowed_python"])

    def test_probe_checks_locked_environments(self):
        res = probe.run_probe()
        self.assertIn("locked_environments", res)
        self.assertIn("orchestrator", res["locked_environments"])
        self.assertIn("ai", res["locked_environments"])
        self.assertIn("qlib", res["locked_environments"])


if __name__ == "__main__":
    unittest.main()
