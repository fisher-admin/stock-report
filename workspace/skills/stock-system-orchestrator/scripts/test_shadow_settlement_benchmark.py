#!/usr/bin/env python3
"""Settlement benchmark coverage rule (20261009).

Real 20260810-20260910 windows: the shadow picks had complete prices but 2-8 of
~5,340 all-A constituents (suspensions) did not, and the all-or-nothing benchmark
turned every row into data_missing, so no shadow ever matured a sample day.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import shadow_portfolio_evaluator as spe  # noqa: E402
import test_shadow_portfolio_accounting as accounting  # noqa: E402

OPEN_DATES = accounting.OPEN_DATES
UNIVERSE_SIZE = 400


def _codes():
    picks = ["000001.SZ", "600000.SH"]
    others = [f"{300000 + i:06d}.SZ" for i in range(UNIVERSE_SIZE - len(picks))]
    return picks + others


def _prices(missing_exit=()):
    rows = []
    for code in _codes():
        for trade_date in OPEN_DATES[1:]:
            if trade_date == OPEN_DATES[-1] and code in missing_exit:
                continue  # e.g. suspended on the exit day
            rows.append({"trade_date": trade_date, "ts_code": code, "open_qfq": 10.0, "close_qfq": 11.0})
    return pd.DataFrame(rows)


def _universe(membership_date=OPEN_DATES[1]):
    codes = _codes()
    return pd.DataFrame({"trade_date": [membership_date] * len(codes), "ts_code": codes,
                         "universe_flag": [1] * len(codes), "tradable": [1] * len(codes)})


def _settle(prices, universe):
    ledger = spe.pending_rows_from_snapshot(accounting._snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)
    return spe.settle_ledger(ledger, prices=prices, pit_universe=universe,
                             open_trade_dates=OPEN_DATES, as_of_date=OPEN_DATES[-1])


class SettlementBenchmarkCoverageTests(unittest.TestCase):
    def test_a_few_unpriced_constituents_no_longer_void_settlement(self):
        missing = [f"{300000 + i:06d}.SZ" for i in range(3)]  # 3/400 = 0.75% unpriced
        result = _settle(_prices(missing), _universe())
        self.assertEqual(set(result["settlement_status"]), {"settled"})
        self.assertAlmostEqual(float(result["benchmark_return_5d"].iloc[0]), 0.10, places=9)
        self.assertAlmostEqual(float(result["return_5d_net"].iloc[0]), 0.10 - 0.003, places=9)

    def test_coverage_below_threshold_still_fails_closed(self):
        missing = [f"{300000 + i:06d}.SZ" for i in range(6)]  # 6/400 = 1.5% unpriced
        result = _settle(_prices(missing), _universe())
        self.assertEqual(set(result["settlement_status"]), {"data_missing"})
        self.assertTrue(result["benchmark_return_5d"].isna().all())

    def test_missing_pick_price_still_marks_row_data_missing(self):
        result = _settle(_prices(["000001.SZ"]), _universe())
        by_code = result.set_index("ts_code")["settlement_status"].to_dict()
        self.assertEqual(by_code["000001.SZ"], "data_missing")
        self.assertEqual(by_code["600000.SH"], "settled")

    def test_membership_falls_back_to_last_available_universe_day(self):
        # no universe snapshot on the entry day itself (the real 0831/0907/0911/0924 windows)
        result = _settle(_prices(), _universe(membership_date=OPEN_DATES[0]))
        self.assertEqual(set(result["settlement_status"]), {"settled"})

    def test_no_membership_at_all_fails_closed(self):
        empty = _universe().iloc[0:0]
        self.assertIsNone(spe._benchmark_return(spe._normalized_prices(_prices()),
                                                spe._normalized_universe(empty), OPEN_DATES[1], OPEN_DATES[-1]))


if __name__ == "__main__":
    unittest.main()
