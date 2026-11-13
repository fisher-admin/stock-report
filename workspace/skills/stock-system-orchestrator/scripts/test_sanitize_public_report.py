#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sanitize_public_report as sanitizer


class PublicReportSanitizerTests(unittest.TestCase):
    def test_replaces_nested_local_paths_and_preserves_report_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "data" / "latest" / "state.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "database": "/Users/alice/.openclaw/workspace/stock_data/a.db",
                        "source": "/Users/alice/.openclaw/workspace/stock-report/data/x.json",
                        "report_root": "/Users/alice/.openclaw/workspace/stock-report",
                        "candidate": "000001.SZ",
                    }
                ),
                encoding="utf-8",
            )

            result = sanitizer.sanitize_public_tree(
                repo,
                private_roots=[
                    ("/Users/alice/.openclaw/workspace/stock-report", "report://"),
                    ("/Users/alice/.openclaw/workspace", "workspace://"),
                    ("/Users/alice", "home://"),
                ],
            )
            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(payload["database"], "workspace://stock_data/a.db")
        self.assertEqual(payload["source"], "report://data/x.json")
        self.assertEqual(payload["report_root"], "report://")
        self.assertEqual(payload["candidate"], "000001.SZ")

    def test_rejects_nonempty_credential_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "data" / "state.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"api_key": "must-not-publish"}), encoding="utf-8")

            with self.assertRaises(sanitizer.PublicReportSafetyError):
                sanitizer.sanitize_public_tree(repo, private_roots=[])

    def test_rejects_unknown_private_home_paths_after_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "data" / "state.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps({"path": "/Users/bob/private/report.json"}),
                encoding="utf-8",
            )

            with self.assertRaises(sanitizer.PublicReportSafetyError):
                sanitizer.sanitize_public_tree(
                    repo,
                    private_roots=[("/Users/alice", "home://")],
                )


class LocalOnlyFieldTests(unittest.TestCase):
    """20261009: per-stock history / db_path never enter committed public JSON."""

    def _repo(self, tmpdir: str) -> Path:
        import subprocess
        repo = Path(tmpdir)
        (repo / "data" / "latest").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / ".gitignore").write_text("data/latest/review_state_unified.json\n", encoding="utf-8")
        return repo

    def test_published_files_lose_local_only_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._repo(tmpdir)
            target = repo / "data" / "latest" / "research_state.json"
            target.write_text(json.dumps({"review": {"latest_sample": [{"stock_code": "000001"}],
                                                     "db_path": "x.db", "performance": {"n": 1}}}),
                              encoding="utf-8")
            result = sanitizer.sanitize_public_tree(repo, private_roots=[])
            payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"review": {"performance": {"n": 1}}})
        self.assertEqual(result["local_only_fields_removed"], 2)

    def test_git_ignored_local_inputs_keep_their_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = self._repo(tmpdir)
            local = repo / "data" / "latest" / "review_state_unified.json"
            local.write_text(json.dumps({"stock_rows": [{"stock_code": "000001"}]}), encoding="utf-8")
            (repo / "data" / "latest" / "run_manifest.json").write_text("{}", encoding="utf-8")
            sanitizer.sanitize_public_tree(repo, private_roots=[])
            payload = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual(payload["stock_rows"], [{"stock_code": "000001"}])


if __name__ == "__main__":
    unittest.main()
