#!/usr/bin/env python3
"""build_summary performance aggregates use the newest settled date (20261009).

Before: performance/ai_view/score-bucket stats filtered on the newest recommend
date, which is never settled when the evening run publishes -> averages NULL and
unsettled rows counted as hit-rate misses (0.0%).
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import recommendation_warehouse as rw  # noqa: E402
import test_recommendation_warehouse_integrity as integrity  # noqa: E402


class SummaryPerformanceDateTests(unittest.TestCase):
    def setUp(self):
        helper = integrity.RecommendationWarehouseIntegrityTests()
        helper._cleanups = []
        self.addCleanup(helper.doCleanups)
        self.conn = helper.make_conn()
        rows = []
        for date in ("20261008", "20261009"):
            for rank, code in enumerate(("000001", "600000", "300750"), start=1):
                rows.append(rw.prepare_recommendation_fact_record(helper.make_item(
                    recommend_date=date, stock_code=code, ts_code=f"{code}.SZ", rank_no=rank,
                    stock_name=f"S{code}"), strict=True))
        rw.upsert_recommendations(self.conn, rows)
        settled = {"000001": 1.5, "600000": -0.5, "300750": 2.0}
        for code, ret in settled.items():
            self.conn.execute(
                """UPDATE recommendation_fact
                   SET next_trade_date='20261009', next_day_price=10.0, next_day_return_pct=?,
                       ai_view='观察', ai_score=?, ai_effectiveness_eligible=?
                   WHERE recommend_date='20261008' AND stock_code=?""",
                (ret, 70 if code != "300750" else 90, 0 if code == "300750" else 1, code),
            )
        self.conn.commit()

    def summary(self):
        return rw.build_summary(self.conn, strategy_id=rw.O2C_STRATEGY_ID, strategy_name="t")

    def test_performance_uses_latest_settled_date_not_unsettled_newest_list(self):
        s = self.summary()
        self.assertEqual(s["latest_recommend_date"], "20261009")  # freshness contract unchanged
        self.assertEqual(s["performance_date"], "20261008")
        perf = s["performance"]
        self.assertEqual(perf["sample_count"], 3)
        self.assertAlmostEqual(perf["avg_next_day_return_pct"], 1.0, places=4)
        self.assertAlmostEqual(perf["next_day_hit_rate_pct"], 66.67, places=2)

    def test_ai_stats_use_settled_eligible_rows_only(self):
        s = self.summary()
        self.assertEqual(sum(r["recommendation_count"] for r in s["ai_view_stats"]), 2)  # 300750 ineligible
        self.assertAlmostEqual(s["ai_view_stats"][0]["avg_next_day_return_pct"], 0.5, places=4)
        self.assertEqual(sum(r["recommendation_count"] for r in s["score_bucket_stats"]), 2)

    def test_nothing_settled_yet_gives_empty_not_zero_hit_rate(self):
        self.conn.execute("UPDATE recommendation_fact SET next_day_return_pct=NULL, next_day_price=NULL")
        self.conn.commit()
        perf = self.summary()["performance"]
        self.assertEqual(perf["sample_count"], 0)
        self.assertIsNone(perf["avg_next_day_return_pct"])
        self.assertIsNone(perf["next_day_hit_rate_pct"])  # unknown, not "0% hit rate"


if __name__ == "__main__":
    unittest.main()
