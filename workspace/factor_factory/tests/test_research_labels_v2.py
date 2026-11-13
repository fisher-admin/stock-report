#!/usr/bin/env python3
"""Unit tests for D4 label conventions and the portfolio simulator (synthetic paths only)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import research_labels_v2 as rl  # noqa: E402

N = 40


def mats_for(path: np.ndarray, can_buy=None, can_sell=None, traded=None) -> dict:
    """One code; adj_open == adj_close == path (no overnight move)."""
    a = path.reshape(-1, 1).astype(float)
    tr = np.ones_like(a, dtype=bool) if traded is None else traded.reshape(-1, 1)
    last = np.array([len(path) - 1 - int(np.argmax(tr[::-1, 0]))])
    adj_close = np.where(tr, a, np.nan)
    mark = adj_close.copy()
    for i in range(1, len(mark)):
        if not np.isfinite(mark[i, 0]):
            mark[i, 0] = mark[i - 1, 0]
    return {"adj_open": np.where(tr, a, np.nan), "adj_close": adj_close, "mark_open": mark,
            "can_buy": (tr if can_buy is None else can_buy.reshape(-1, 1)) & tr,
            "can_sell": (tr if can_sell is None else can_sell.reshape(-1, 1)) & tr,
            "traded": tr, "last_trade": last, "c2c": np.zeros_like(a)}


class LabelTests(unittest.TestCase):
    def test_normal_exit_is_open_t1_to_open_t1_plus_h(self):
        path = 1.0 + 0.01 * np.arange(N)
        g, delay, kind = rl.label_one(mats_for(path), 3, 0, 5, N, delisted=False)
        self.assertEqual(kind, "normal")
        self.assertAlmostEqual(g, path[9] / path[4] - 1)
        self.assertEqual(delay, 0)

    def test_limit_up_entry_is_blocked(self):
        cb = np.ones(N, dtype=bool)
        cb[4] = False
        g, _, kind = rl.label_one(mats_for(np.ones(N), can_buy=cb), 3, 0, 5, N, delisted=False)
        self.assertEqual(kind, "entry_blocked")
        self.assertTrue(np.isnan(g))

    def test_limit_down_exit_is_delayed_to_first_sellable_open(self):
        path = np.ones(N)
        path[9:] = 0.5
        path[12:] = 0.4
        cs = np.ones(N, dtype=bool)
        cs[9:12] = False  # three limit-down opens
        g, delay, kind = rl.label_one(mats_for(path, can_sell=cs), 3, 0, 5, N, delisted=False)
        self.assertEqual(kind, "delayed")
        self.assertEqual(delay, 3)
        self.assertAlmostEqual(g, 0.4 - 1)  # loser keeps its full realized loss (no censoring)

    def test_delisting_marks_last_traded_close(self):
        path = np.linspace(1.0, 0.3, N)
        tr = np.ones(N, dtype=bool)
        tr[7:] = False  # stops trading for good before the 5-day exit
        g, _, kind = rl.label_one(mats_for(path, traded=tr), 3, 0, 5, N, delisted=True)
        self.assertEqual(kind, "delist")
        self.assertAlmostEqual(g, path[6] / path[4] - 1)

    def test_beyond_sample(self):
        _, _, kind = rl.label_one(mats_for(np.ones(N)), N - 4, 0, 5, N, delisted=False)
        self.assertEqual(kind, "beyond_sample")


class PortfolioTests(unittest.TestCase):
    def test_single_name_buy_and_hold_pays_one_buy(self):
        path = 1.0 + 0.01 * np.arange(N)
        m = mats_for(path)
        sessions = [f"2024{i:04d}" for i in range(N)]
        cost = rl.CostModel()
        plan = [(t, {0: 1.0}) for t in range(0, 30, 5)]
        df = rl.portfolio_periods(plan, m, sessions, 5, cost)
        final = df["equity_after_trades"].iloc[-1]
        expect = (1 - cost.buy()) * path[26] / path[1]  # one buy at open 1, held to open 26, no sells
        self.assertAlmostEqual(final, expect, places=9)
        self.assertEqual(df["blocked_buys"].sum(), 0)

    def test_delisted_holding_is_written_off_at_last_close(self):
        path = np.linspace(1.0, 0.5, N)
        tr = np.ones(N, dtype=bool)
        tr[12:] = False
        m = mats_for(path, traded=tr)
        sessions = [f"2024{i:04d}" for i in range(N)]
        df = rl.portfolio_periods([(0, {0: 1.0}), (5, {0: 1.0}), (15, {0: 1.0})], m, sessions, 5, rl.CostModel())
        self.assertGreaterEqual(int(df["delist_liquidations"].iloc[-1]), 1)
        self.assertEqual(int(df["n_hold"].iloc[-1]), 0)


if __name__ == "__main__":
    unittest.main()
