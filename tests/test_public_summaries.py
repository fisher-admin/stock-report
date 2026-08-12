from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_view_summaries", ROOT / "generate_view_summaries.py"
)
summaries = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(summaries)


def _review_payload() -> dict:
    return {
        "generated_at": "2026-08-12T10:00:00+08:00",
        "trade_date": "20260811",
        "strategies": {
            "prebreakout_v43_control": {
                "strategy_id": "prebreakout_v43_control",
                "strategy_name": "control",
                "db_path": "/Users/example/.openclaw/private.db",
                "date_stats": [{"recommend_date": "20260811"}],
                "notes": {"private_path": "/Users/example/private.json"},
                "total_rows": 1,
                "unique_stock_count": 1,
            }
        },
        "daily_comparison": [
            {
                "recommend_date": "20260811",
                "strategy_id": "prebreakout_v43_control",
                "sample_count": 1,
                "avg_next_day_return_pct": 0.5,
            }
        ],
        "stock_rows": [
            {
                "recommend_date": "20260811",
                "strategy_id": "prebreakout_v43_control",
                "stock_code": "000001",
                "sector_name": "银行",
                "next_day_return_pct": 0.5,
                "round_trip_cost": 0.003,
            }
        ],
    }


class PublicSummaryTests(unittest.TestCase):
    def test_review_summary_contains_aggregates_but_no_stock_rows_or_private_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "review_state_unified.json"
            dest = root / "review_track_latest.json"
            source.write_text(json.dumps(_review_payload()), encoding="utf-8")

            info = summaries.summarize_review_track(source, dest)
            public = json.loads(dest.read_text(encoding="utf-8"))

        self.assertEqual(info["rows_in"], 1)
        self.assertEqual(info["rows_out"], 0)
        self.assertNotIn("stock_rows", public)
        self.assertNotIn("db_path", public["strategies"]["prebreakout_v43_control"])
        self.assertNotIn("notes", public["strategies"]["prebreakout_v43_control"])
        self.assertEqual(public["detail_storage"], "local_only")
        self.assertEqual(len(public["daily_comparison"]), 1)
        self.assertTrue(public["methodology"]["cost_included"])
        self.assertEqual(public["methodology"]["round_trip_cost"], 0.003)

    def test_main_never_exports_recommendation_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            analytics = root / "data/recommendation_analytics"
            latest = root / "data/latest"
            analytics.mkdir(parents=True)
            latest.mkdir(parents=True)
            (latest / "review_state_unified.json").write_text(
                json.dumps(_review_payload()), encoding="utf-8"
            )
            original_root = summaries.REPO_ROOT
            original_analytics = summaries.ANALYTICS_DIR
            original_latest = summaries.LATEST_DIR
            try:
                summaries.REPO_ROOT = root
                summaries.ANALYTICS_DIR = analytics
                summaries.LATEST_DIR = latest
                exit_code = summaries.main()
            finally:
                summaries.REPO_ROOT = original_root
                summaries.ANALYTICS_DIR = original_analytics
                summaries.LATEST_DIR = original_latest

            self.assertEqual(exit_code, 0)
            self.assertFalse((latest / "recommendation_history.csv").exists())


if __name__ == "__main__":
    unittest.main()
