from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sanitize_published_data", ROOT / "sanitize_published_data.py"
)
sanitizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sanitizer)


class PublishedDataSanitizerTests(unittest.TestCase):
    def test_removes_row_details_and_machine_specific_metadata(self):
        payload = {
            "date_stats": [{"recommend_date": "20260811", "return_pct": 0.5}],
            "latest_sample": [{"stock_code": "000001"}],
            "stock_rows": [{"stock_code": "000001"}],
            "db_path": "/Users/example/private/recommendations.db",
            "nested": {
                "source_file": "~/.openclaw/private/result.json",
                "public_ref": "data/latest/result.json",
                "status": "ok",
            },
        }

        clean = sanitizer.sanitize_value(payload)

        self.assertNotIn("latest_sample", clean)
        self.assertNotIn("stock_rows", clean)
        self.assertNotIn("db_path", clean)
        self.assertNotIn("source_file", clean["nested"])
        self.assertEqual(clean["nested"]["public_ref"], "data/latest/result.json")
        self.assertEqual(clean["nested"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
