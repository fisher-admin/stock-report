#!/usr/bin/env python3
"""2026-10-13: a failed evening chain made every midday run abort with
DATA_STALE although only the selection date lagged."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_midday_analysis as midday  # noqa: E402

TODAY = datetime.now().strftime("%Y%m%d")


def unified(strategy_date: str) -> dict:
    return {"strategy_heatmap": {"latest_recommend_date": strategy_date}}


class MiddayFreshnessDegradeTests(unittest.TestCase):
    def test_only_selection_lagging_returns_its_date(self):
        lag = midday.assert_midday_freshness(
            TODAY, {"trade_date": "20261012"}, {"latest_trade_date": "20261012"}, unified("20261009"), None)
        self.assertEqual(lag, "20261009")

    def test_coherent_inputs_have_no_lag(self):
        lag = midday.assert_midday_freshness(
            TODAY, {"trade_date": "20261022"}, {"latest_trade_date": "20261022"}, unified("20261022"), None)
        self.assertIsNone(lag)

    def test_market_side_disagreement_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, "DATA_STALE"):
            midday.assert_midday_freshness(
                TODAY, {"trade_date": "20261012"}, {"latest_trade_date": "20261009"}, unified("20261009"), None)

    def test_selection_ahead_of_market_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, "DATA_STALE"):
            midday.assert_midday_freshness(
                TODAY, {"trade_date": "20261012"}, {"latest_trade_date": "20261012"}, unified("20261013"), None)

    def test_past_trade_date_stays_strict(self):
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            midday.assert_midday_freshness(
                "20000103", {"trade_date": "20000103"}, {"latest_trade_date": "20000103"}, unified("19991231"), None)


if __name__ == "__main__":
    unittest.main()
