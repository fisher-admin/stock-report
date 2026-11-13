"""Exercise the publisher's actual shell transport function without a network."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


class PublisherNetworkPolicyTests(unittest.TestCase):
    def test_all_remote_operations_use_bounded_noninteractive_http1_transport(self):
        source = Path(__file__).with_name("publish_stock_report_v2.sh").read_text()
        match = re.search(r"network_git\(\) \{\n.*?\n\}", source, re.S)
        self.assertIsNotNone(match, "publisher must share one explicit network transport policy")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "git"
            fake.write_text(f"#!{sys.executable}\nimport json,os,sys\nprint(json.dumps([sys.argv[1:],os.environ.get('GIT_TERMINAL_PROMPT')]))\nsys.exit(int(os.environ.get('FAKE_GIT_EXIT','0')))\n")
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GIT_TERMINAL_PROMPT="1")
            for operation in ("fetch", "pull", "push", "ls-remote"):
                with self.subTest(operation=operation):
                    proc = subprocess.run(["bash", "-c", match.group() + f"\nnetwork_git {operation} origin"], env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    args, prompt = json.loads(proc.stdout)
                    self.assertIn("http.version=HTTP/1.1", args)
                    self.assertIn("http.lowSpeedLimit=1", args)
                    self.assertIn("http.lowSpeedTime=20", args)
                    self.assertEqual(args[-2:], [operation, "origin"])
                    self.assertEqual(prompt, "0")
            env["FAKE_GIT_EXIT"] = "128"
            failed = subprocess.run(["bash", "-c", match.group() + "\nnetwork_git fetch origin"], env=env, capture_output=True, timeout=5)
            self.assertEqual(failed.returncode, 128)
        for line in source.splitlines():
            self.assertIsNone(re.search(r"(?:^\s*|\bif\s+|\$\()git\s+(?:-C\s+\"[^\"]+\"\s+)?(?:fetch|pull|push|ls-remote)\b", line.split("#", 1)[0]), line)


if __name__ == "__main__":
    unittest.main()
