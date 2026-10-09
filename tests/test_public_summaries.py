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
    def test_mixed_costs_are_still_deducted_and_units_are_explicit(self):
        self.assertTrue(hasattr(summaries, 'cost_methodology'))
        result = summaries.cost_methodology([
            {'round_trip_cost': 0.003}, {'round_trip_cost': 0.0026}])
        self.assertIs(result['cost_included'], True)
        self.assertIsNone(result['round_trip_cost'])
        self.assertEqual(result['round_trip_costs'], [0.0026, 0.003])
        self.assertEqual(result['cost_unit'], 'ratio')
        self.assertEqual(result['cost_status'], 'mixed')

    def test_zero_unknown_invalid_and_not_deducted_are_distinct(self):
        self.assertTrue(hasattr(summaries, 'cost_methodology'))
        self.assertEqual(summaries.cost_methodology([{'round_trip_cost': 0}])['round_trip_cost'], 0)
        for value in (None, True, -0.1, float('nan'), float('inf'), '0.003'):
            with self.subTest(value=value):
                result = summaries.cost_methodology([{'round_trip_cost': value}])
                self.assertIsNone(result['cost_included'])
                self.assertIsNone(result['round_trip_cost'])
        self.assertIs(summaries.cost_methodology([{'round_trip_cost': .003, 'cost_included': False}])['cost_included'], False)

    def test_partial_cost_metadata_does_not_claim_all_rows_deducted(self):
        self.assertTrue(hasattr(summaries, 'cost_methodology'))
        result = summaries.cost_methodology([{'round_trip_cost': .003}, {}])
        self.assertIsNone(result['cost_included'])
        self.assertEqual(result['cost_status'], 'unknown')

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


class AiEligibilityTests(unittest.TestCase):
    """20261009: future-backfilled / evidence-less AI rows must not enter AI statistics."""

    @staticmethod
    def rows():
        def row(view, score, ret, eligible, reason=None):
            return {"recommend_date": "20261008", "strategy_id": "s", "sector_name": "银行", "ai_view": view,
                    "ai_score": score, "next_day_return_pct": ret, "ai_effectiveness_eligible": eligible,
                    "ai_exclusion_reason": reason}
        return [
            row("持有/加仓", 85, 1.0, 1),
            row("观察", 65, -0.5, 1),
            row("买入", 95, 9.0, 0, "future_backfill"),  # written after the outcome was known
            row("买入", 92, 8.0, 0, "missing_evidence_time"),
        ]

    def test_attribution_ai_groupings_use_only_eligible_rows(self):
        attr = summaries.attribution_for(self.rows())
        self.assertEqual(attr["ai_eligible_rows"], 2)
        self.assertEqual(sum(g["recommendation_count"] for g in attr["ai_view_stats"]), 2)
        self.assertEqual(sum(g["recommendation_count"] for g in attr["score_bucket_stats"]), 2)
        self.assertEqual(attr["settled_rows"], 4)  # non-AI attribution still sees every settled row

    def test_sentiment_excludes_ineligible_ai_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "review_state_unified.json"
            dest = Path(tmp) / "sentiment_state.json"
            source.write_text(json.dumps({"trade_date": "20261009", "stock_rows": self.rows()}, ensure_ascii=False),
                              encoding="utf-8")
            summaries.summarize_sentiment(source, dest)
            out = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(out["sample_count"], 2)
        self.assertEqual(out["excluded_ineligible_ai_rows"], 2)
        self.assertEqual(out["avg_ai_score"], 75.0)

    def test_legacy_rows_without_flag_are_kept(self):
        self.assertTrue(summaries._ai_eligible({"ai_view": "观察"}))
        self.assertFalse(summaries._ai_eligible({"ai_effectiveness_eligible": None}))


if __name__ == "__main__":
    unittest.main()
