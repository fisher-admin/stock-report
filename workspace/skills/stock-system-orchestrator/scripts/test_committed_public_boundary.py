#!/usr/bin/env python3
"""Pre-commit public boundary audit (20261009)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import committed_public_boundary as cpb  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class CommittedPublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "config").mkdir()
        (self.repo / "data/latest").mkdir(parents=True)
        (self.repo / "config/public-result-allowlist.txt").write_text(
            "# comment\ndata/latest/run_manifest.json\n", encoding="utf-8")
        git(self.repo, "init", "-q")

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, rel: str, payload) -> None:
        path = self.repo / rel
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        git(self.repo, "add", "-f", rel)

    def test_allowlisted_clean_index_passes(self):
        self.stage("data/latest/run_manifest.json", {"run_id": "x"})
        self.assertEqual(cpb.audit_index(self.repo), {"ok": True, "checked": 1, "violations": []})

    def test_non_allowlisted_file_is_rejected(self):
        self.stage("data/latest/run_manifest.json", {"run_id": "x"})
        self.stage("data/latest/review_state_unified.json", {"stock_rows": [{"stock_code": "000001"}]})
        result = cpb.audit_index(self.repo)
        self.assertFalse(result["ok"])
        self.assertTrue(any("review_state_unified.json: not in" in v for v in result["violations"]))
        self.assertTrue(any("local-only field $.stock_rows" in v for v in result["violations"]))

    def test_private_paths_and_db_path_are_rejected(self):
        self.stage("data/latest/run_manifest.json", {"db_path": "x", "src": "/" + "Users/alice/.openclaw/a.db"})
        violations = cpb.audit_index(self.repo)["violations"]
        self.assertTrue(any("private absolute path" in v for v in violations))
        self.assertTrue(any("local-only field $.db_path" in v for v in violations))

    def test_audits_staged_content_not_the_working_tree(self):
        self.stage("data/latest/run_manifest.json", {"db_path": "x"})
        (self.repo / "data/latest/run_manifest.json").write_text("{}", encoding="utf-8")  # unstaged fix
        self.assertFalse(cpb.audit_index(self.repo)["ok"])

    def test_untracked_local_files_are_not_audited(self):
        self.stage("data/latest/run_manifest.json", {"run_id": "x"})
        (self.repo / "data/latest/local_only.json").write_text('{"stock_rows": []}', encoding="utf-8")
        self.assertTrue(cpb.audit_index(self.repo)["ok"])

    def test_missing_allowlist_is_a_usage_error(self):
        (self.repo / "config/public-result-allowlist.txt").unlink()
        self.assertEqual(cpb.main(["--repo", str(self.repo)]), 2)


if __name__ == "__main__":
    unittest.main()
