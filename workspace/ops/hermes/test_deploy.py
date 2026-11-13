#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy  # noqa: E402


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.dst = root / "src", root / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        (self.src / "a.sh").write_text("echo a\n")
        (self.src / "b.sh").write_text("echo b\n")
        (self.dst / "a.sh").write_text("echo a\n")
        (self.dst / "b.sh").write_text("echo old\n")
        (self.dst / "b.sh.bak-1").write_text("x\n")  # unmanaged: ignored

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_reports_changed_and_missing_only(self):
        (self.src / "c.sh").write_text("echo c\n")
        self.assertEqual(deploy.drift(self.src, self.dst), [("b.sh", "changed"), ("c.sh", "missing")])

    def test_deploy_backs_up_replaced_file_and_leaves_identical_ones(self):
        a_mtime = (self.dst / "a.sh").stat().st_mtime_ns
        self.assertEqual(deploy.deploy(self.src, self.dst, stamp="T"), ["b.sh"])
        self.assertEqual((self.dst / "b.sh").read_text(), "echo b\n")
        self.assertEqual((self.dst / "b.sh.bak-T").read_text(), "echo old\n")
        self.assertEqual((self.dst / "a.sh").stat().st_mtime_ns, a_mtime)
        self.assertTrue((self.dst / "b.sh").stat().st_mode & 0o100)
        self.assertEqual(deploy.drift(self.src, self.dst), [])


if __name__ == "__main__":
    unittest.main()
