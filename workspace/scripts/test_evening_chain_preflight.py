#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evening_chain_preflight as preflight  # noqa: E402


class EveningChainPreflightTests(unittest.TestCase):
    def test_silent_when_every_check_passes(self):
        results = preflight.run_checks((("a", lambda: []), ("b", lambda: [])))
        self.assertEqual(preflight.render(results), "")

    def test_findings_are_listed_with_count(self):
        text = preflight.render({"environment": ["Python 环境 ai 不可用"], "disk": ["磁盘剩余 2.1 GB"]})
        self.assertIn("发现 2 个问题", text)
        self.assertIn("- Python 环境 ai 不可用", text)

    def test_broken_check_becomes_a_finding_not_a_crash(self):
        def broken():
            raise OSError("boom")

        results = preflight.run_checks((("locks", broken),))
        self.assertIn("预检项 locks 自身出错", results["locks"][0])

    def test_stale_git_lock_reported_only_without_running_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            lock = repo / ".git/index.lock"
            lock.write_text("")
            old = time.time() - 2 * preflight.STALE_LOCK_SECONDS
            os.utime(lock, (old, old))
            with mock.patch.object(preflight, "GIT_REPOS", (repo,)), \
                    mock.patch.object(preflight, "PIPELINE_LOCK", repo / "none"):
                with mock.patch.object(preflight, "_git_running", return_value=False):
                    self.assertEqual(len(preflight.check_locks()), 1)
                with mock.patch.object(preflight, "_git_running", return_value=True):
                    self.assertEqual(preflight.check_locks(), [])

    def test_fresh_git_lock_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            (repo / ".git/index.lock").write_text("")
            with mock.patch.object(preflight, "GIT_REPOS", (repo,)), \
                    mock.patch.object(preflight, "PIPELINE_LOCK", repo / "none"):
                self.assertEqual(preflight.check_locks(), [])

    def test_dead_pipeline_lock_owner_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            lockdir = Path(tmp) / "stock_pipeline_gate.lockdir"
            lockdir.mkdir()
            (lockdir / "owner.txt").write_text("999999|Thu|token\n")
            with mock.patch.object(preflight, "GIT_REPOS", ()), mock.patch.object(preflight, "PIPELINE_LOCK", lockdir):
                self.assertIn("999999", preflight.check_locks()[0])

    def test_low_disk_is_reported(self):
        usage = mock.Mock(free=int(1.5 * 1024 ** 3))
        with mock.patch.object(preflight.shutil, "disk_usage", return_value=usage):
            self.assertIn("1.5 GB", preflight.check_disk()[0])

    def test_wrapper_drift_is_reported_with_deploy_command(self):
        preflight.check_wrappers()  # imports ops/hermes/deploy
        deploy = sys.modules["deploy"]
        with mock.patch.object(deploy, "drift", return_value=[("a.sh", "changed"), ("b.sh", "missing")]):
            issue = preflight.check_wrappers()[0]
        self.assertIn("a.sh（与仓库不一致）", issue)
        self.assertIn("b.sh（缺失）", issue)
        self.assertIn("deploy.py deploy", issue)
        with mock.patch.object(deploy, "drift", return_value=[]):
            self.assertEqual(preflight.check_wrappers(), [])


if __name__ == "__main__":
    unittest.main()
