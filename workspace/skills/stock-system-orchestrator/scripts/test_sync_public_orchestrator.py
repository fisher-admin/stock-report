import tempfile
import unittest
from pathlib import Path

from sync_public_orchestrator import ALLOWLIST_FILES, sanitize_for_public, sync_public


class SyncPublicOrchestratorTests(unittest.TestCase):
    def test_sanitization_preserves_python_and_removes_private_root(self):
        source = "ROOT = '/Users/fisher/.openclaw'\nWORKSPACE = '/Users/fisher/.openclaw/workspace'\n"
        output = sanitize_for_public(source)
        self.assertNotIn("/Users/fisher", output)
        namespace = {}
        exec(compile(output, "public-source", "exec"), namespace)
        self.assertEqual(namespace["WORKSPACE"], "./workspace")

    def test_qfq_fallback_dependency_is_in_public_bundle(self):
        self.assertIn("qfq_price_fallback.py", ALLOWLIST_FILES)

    def test_public_mirror_matches_generated_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / "src"
            pub = Path(tmp_dir) / "pub"
            manifest = Path(tmp_dir) / "manifest.json"
            src.mkdir(parents=True)
            pub.mkdir(parents=True)

            for f in ALLOWLIST_FILES:
                (src / f).write_text(f"# production {f}", encoding="utf-8")

            # 1. Sync
            res_sync = sync_public(src, pub, manifest, check_only=False)
            self.assertTrue(res_sync["ok"])
            self.assertNotIn(str(Path(tmp_dir)), manifest.read_text())

            # 2. Check passes
            res_check1 = sync_public(src, pub, manifest, check_only=True)
            self.assertTrue(res_check1["ok"])

            # 3. Modify destination creates drift
            (pub / "dual_track_publication.py").write_text("drift", encoding="utf-8")
            res_check2 = sync_public(src, pub, manifest, check_only=True)
            self.assertFalse(res_check2["ok"])
            self.assertEqual(len(res_check2["diffs"]), 1)



    def test_main_cli_sync_and_check(self):
        import sync_public_orchestrator as spo
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / "src"
            pub = Path(tmp_dir) / "pub"
            manifest = Path(tmp_dir) / "manifest.json"
            src.mkdir(parents=True)
            pub.mkdir(parents=True)

            for f in ALLOWLIST_FILES:
                (src / f).write_text(f"# source {f}", encoding="utf-8")

            with mock.patch("sync_public_orchestrator.DEFAULT_SOURCE_ROOT", src), \
                 mock.patch("sync_public_orchestrator.DEFAULT_PUBLIC_ROOT", pub), \
                 mock.patch("sync_public_orchestrator.DEFAULT_MANIFEST_PATH", manifest), \
                 mock.patch("sync_public_orchestrator.DEFAULT_REV", None):  # temp src is not a git repo

                # 1. Sync via CLI
                with mock.patch("sys.argv", ["sync_public_orchestrator.py"]):
                    ret = spo.main()
                    self.assertEqual(ret, 0)

                # 2. Parity check via CLI
                with mock.patch("sys.argv", ["sync_public_orchestrator.py", "--check"]):
                    ret = spo.main()
                    self.assertEqual(ret, 0)

                # 3. Modify destination to create drift
                (pub / "dual_track_publication.py").write_text("drifted", encoding="utf-8")
                with mock.patch("sys.argv", ["sync_public_orchestrator.py", "--check"]):
                    ret = spo.main()
                    self.assertEqual(ret, 1)

    def test_committed_revision_excludes_uncommitted_edits(self):
        import subprocess

        def git(repo, *args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                           env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})

        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / "src"
            pub = Path(tmp_dir) / "pub"
            src.mkdir()
            for f in ALLOWLIST_FILES:
                (src / f).write_text(f"# committed {f}\nP = '/Users/fisher/.openclaw/workspace/x'\n", encoding="utf-8")
            git(src, "init", "-q")
            git(src, "add", "-A")
            git(src, "commit", "-qm", "base")
            (src / "dual_track_publication.py").write_text("# uncommitted work in progress\n", encoding="utf-8")

            res = sync_public(src, pub, Path(tmp_dir) / "manifest.json", check_only=False, rev="HEAD")
            self.assertTrue(res["ok"], res["diffs"])
            self.assertRegex(res["source_rev"], r"^[0-9a-f]{40}$")
            mirrored = (pub / "dual_track_publication.py").read_text(encoding="utf-8")
            self.assertIn("# committed", mirrored)
            self.assertNotIn("uncommitted", mirrored)
            self.assertNotIn("/Users/fisher", mirrored)
            manifest = (Path(tmp_dir) / "manifest.json").read_text(encoding="utf-8")
            self.assertIn(res["source_rev"], manifest)

            self.assertTrue(sync_public(src, pub, None, check_only=True, rev="HEAD")["ok"])
            self.assertFalse(sync_public(src, pub, None, check_only=True, rev=None)["ok"])  # worktree differs

    def test_only_subset_leaves_other_mirror_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / "src"
            pub = Path(tmp_dir) / "pub"
            src.mkdir()
            pub.mkdir()
            for f in ALLOWLIST_FILES:
                (src / f).write_text(f"# source {f}", encoding="utf-8")
            (pub / "sanitize_public_report.py").write_text("# hand-edited public variant", encoding="utf-8")
            res = sync_public(src, pub, None, check_only=False, only=["maintenance_mode.py"])
            self.assertTrue(res["ok"])
            self.assertEqual(res["synced"], ["maintenance_mode.py"])
            self.assertEqual((pub / "sanitize_public_report.py").read_text(), "# hand-edited public variant")
            with self.assertRaises(ValueError):
                sync_public(src, pub, None, only=["not_allowlisted.py"])

    def test_new_resolver_and_maintenance_modules_are_mirrored(self):
        for name in ("domestic_index_close.py", "maintenance_mode.py", "committed_public_boundary.py", "generate_latest_states.py",
                     "validate_publication_contract.py", "publish_stock_report_v2.sh"):
            self.assertIn(name, ALLOWLIST_FILES)


if __name__ == "__main__":
    unittest.main()
