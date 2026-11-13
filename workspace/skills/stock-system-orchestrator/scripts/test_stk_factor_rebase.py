#!/usr/bin/env python3
"""Phase 2 (2026-10-30): incremental short-track stk_factor history."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import stk_factor_rebase as rb  # noqa: E402


def row(code, date, raw, adj, base, pre_raw=None, pre_adj=None, macd=0.2):
    """Provider-shaped row: qfq against ``base``, hfq = raw x adj."""
    pre_raw = raw if pre_raw is None else pre_raw
    pre_adj = adj if pre_adj is None else pre_adj
    return {"ts_code": code, "trade_date": date, "open": raw, "high": raw, "low": raw, "close": raw,
            "adj_factor": adj, "close_hfq": round(raw * adj, 5), "pre_close_hfq": round(pre_raw * pre_adj, 5),
            "open_qfq": round(raw * adj / base, 5), "high_qfq": round(raw * adj / base, 5),
            "low_qfq": round(raw * adj / base, 5), "close_qfq": round(raw * adj / base, 5),
            "macd_dif": round(macd * adj / base, 3), "macd_dea": round(macd * adj / base / 2, 3)}


class RebaseMathTests(unittest.TestCase):
    def test_unchanged_base_keeps_values_bit_for_bit(self):
        cached = pd.DataFrame([row("000001.SZ", "20261028", 10.0, 2.0, 2.0)])
        target = pd.Series({"000001.SZ": 2.0})
        out, unusable = rb.rebase_frame(cached, target)
        self.assertEqual(unusable, set())
        pd.testing.assert_frame_equal(out, cached)

    def test_dividend_after_cached_day_is_rebased_like_a_refetch(self):
        # Cached on 10-28 against base 2.0; an ex-dividend on 10-29 moved the base to 2.2.
        cached = pd.DataFrame([row("000001.SZ", "20261028", 10.0, 2.0, 2.0)])
        refetch = pd.DataFrame([row("000001.SZ", "20261028", 10.0, 2.0, 2.2)])
        out, _ = rb.rebase_frame(cached, pd.Series({"000001.SZ": 2.2}))
        for column in rb.PRICE_COLUMNS:
            self.assertAlmostEqual(out.loc[0, column], refetch.loc[0, column], places=5)
        self.assertAlmostEqual(out.loc[0, "macd_dif"], refetch.loc[0, "macd_dif"], delta=0.0011)

    def test_missing_target_base_requires_refetch(self):
        cached = pd.DataFrame([row("000001.SZ", "20261028", 10.0, 2.0, 2.0)])
        _, unusable = rb.rebase_frame(cached, pd.Series(dtype=float))
        self.assertEqual(unusable, {"000001.SZ"})

    def test_continuous_hfq_across_dividend_is_not_broken(self):
        frames = {
            "20261028": pd.DataFrame([row("000001.SZ", "20261028", 10.0, 2.0, 2.2)]),
            # ex-dividend: adjusted pre-close 9.0909 x new adj 2.2 == 10 x 2.0
            "20261029": pd.DataFrame([row("000001.SZ", "20261029", 9.5, 2.2, 2.2, pre_raw=10.0 * 2.0 / 2.2, pre_adj=2.2)]),
        }
        self.assertEqual(rb.broken_hfq_chain(frames, ["20261028", "20261029"]), set())

    def test_rescaled_adjustment_history_is_broken(self):
        # Beijing 920 migration: the provider rescaled every adj factor by ~13x.
        frames = {
            "20261028": pd.DataFrame([row("920751.BJ", "20261028", 14.0, 1.0636, 1.0636)]),
            "20261029": pd.DataFrame([row("920751.BJ", "20261029", 14.2, 14.1338, 14.1338, pre_raw=14.0, pre_adj=14.1338)]),
        }
        self.assertEqual(rb.broken_hfq_chain(frames, ["20261028", "20261029"]), {"920751.BJ"})

    def test_replace_codes_swaps_only_those_rows(self):
        day = pd.DataFrame([row("000001.SZ", "20261028", 10.0, 2.0, 2.0), row("000002.SZ", "20261028", 5.0, 1.0, 1.0)])
        fresh = pd.DataFrame([row("000002.SZ", "20261028", 5.0, 1.0, 1.25), row("000002.SZ", "20261029", 5.1, 1.0, 1.25)])
        out = rb.replace_codes(day, fresh, {"000002.SZ"}, "20261028")
        self.assertEqual(out["ts_code"].tolist(), ["000001.SZ", "000002.SZ"])
        self.assertAlmostEqual(out.loc[1, "close_qfq"], 4.0)
        self.assertEqual(out.loc[0, "close_qfq"], day.loc[0, "close_qfq"])

    def test_row_missing_between_two_present_days_is_refetched(self):
        a = row("000001.SZ", "d1", 10.0, 1.0, 1.0)
        frames = {"d1": pd.DataFrame([a]), "d2": pd.DataFrame([{**a, "ts_code": "000002.SZ"}]), "d3": pd.DataFrame([a])}
        self.assertEqual(rb.gap_codes(frames, ["d1", "d2", "d3"]), {"000001.SZ", "000002.SZ"})

    def test_new_listing_first_day_is_refetched(self):
        a = row("000001.SZ", "d1", 10.0, 1.0, 1.0)
        new = {**a, "ts_code": "301716.SZ"}
        frames = {"d1": pd.DataFrame([a]), "d2": pd.DataFrame([a]), "d3": pd.DataFrame([a, new])}
        self.assertEqual(rb.gap_codes(frames, ["d1", "d2", "d3"]), {"301716.SZ"})


class RunnerFallbackTests(unittest.TestCase):
    def test_incremental_failure_falls_back_to_full_refetch(self):
        import short_track_shadow_runner as runner

        with mock.patch.object(runner, "_incremental_stk_factor_history", side_effect=RuntimeError("boom")), \
                mock.patch.object(runner, "_cached_official_history", return_value="full") as full:
            self.assertEqual(runner.fetch_official_stk_factor_history(None, ["d"], cache_dirs=[]), "full")
        full.assert_called_once()

    def test_switch_disables_incremental(self):
        import short_track_shadow_runner as runner

        with mock.patch.dict("os.environ", {"STOCK_SHORT_TRACK_INCREMENTAL": "0"}), \
                mock.patch.object(runner, "_incremental_stk_factor_history") as incremental, \
                mock.patch.object(runner, "_cached_official_history", return_value="full"):
            runner.fetch_official_stk_factor_history(None, ["d"], cache_dirs=[])
        incremental.assert_not_called()


if __name__ == "__main__":
    unittest.main()
