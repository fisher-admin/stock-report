#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sanitize_public_report as sanitizer


PRIVATE_HOME = "/" + "Users/alice"
OTHER_PRIVATE_HOME = "/" + "Users/bob"


class PublicReportSanitizerTests(unittest.TestCase):
    def test_replaces_nested_local_paths_and_preserves_report_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "data" / "latest" / "state.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "database": f"{PRIVATE_HOME}/.openclaw/workspace/stock_data/a.db",
                        "source": f"{PRIVATE_HOME}/.openclaw/workspace/stock-report/data/x.json",
                        "report_root": f"{PRIVATE_HOME}/.openclaw/workspace/stock-report",
                        "candidate": "000001.SZ",
                    }
                ),
                encoding="utf-8",
            )

            result = sanitizer.sanitize_public_tree(
                repo,
                private_roots=[
                    (f"{PRIVATE_HOME}/.openclaw/workspace/stock-report", "report://"),
                    (f"{PRIVATE_HOME}/.openclaw/workspace", "workspace://"),
                    (PRIVATE_HOME, "home://"),
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
                json.dumps({"path": f"{OTHER_PRIVATE_HOME}/private/report.json"}),
                encoding="utf-8",
            )

            with self.assertRaises(sanitizer.PublicReportSafetyError):
                sanitizer.sanitize_public_tree(
                    repo,
                    private_roots=[(PRIVATE_HOME, "home://")],
                )

    def test_removes_row_level_history_and_database_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "data" / "latest" / "review.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "date_stats": [{"recommend_date": "20260811", "return_pct": 0.5}],
                        "stock_rows": [{"stock_code": "000001"}],
                        "latest_sample": [{"stock_code": "000001"}],
                        "db_path": "workspace://private/recommendations.db",
                    }
                ),
                encoding="utf-8",
            )

            result = sanitizer.sanitize_public_tree(repo, private_roots=[])
            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(result["local_only_fields_removed"], 3)
        self.assertEqual(
            payload,
            {"date_stats": [{"recommend_date": "20260811", "return_pct": 0.5}]},
        )


if __name__ == "__main__":
    unittest.main()
