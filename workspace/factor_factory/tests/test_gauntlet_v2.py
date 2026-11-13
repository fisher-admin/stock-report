#!/usr/bin/env python3
"""Unit tests for gauntlet v2 statistics and portfolio rules (synthetic data only)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gauntlet_v2 as gv  # noqa: E402


class StatsTests(unittest.TestCase):
    def test_bh_fdr_rejects_nulls_keeps_strong(self):
        p = {"strong": 1e-6, "weak": 0.04, **{f"null{i}": 0.3 + i / 100 for i in range(20)}}
        out = gv.bh_fdr(p, q=0.10)
        self.assertTrue(out["strong"])
        self.assertFalse(out["weak"])  # 0.04 > 0.10 * 2 / 22
        self.assertFalse(any(out[f"null{i}"] for i in range(20)))

    def test_newey_west_inflates_for_overlap(self):
        rng = np.random.default_rng(0)
        x = np.convolve(rng.normal(0.02, 1, 600), np.ones(5) / 5, mode="valid")  # overlapping 5-day sums
        naive = x.mean() / (x.std(ddof=0) / np.sqrt(len(x)))
        self.assertLess(abs(gv.newey_west_t(x, 4)), abs(naive))

    def test_one_sided_p_penalizes_wrong_sign(self):
        self.assertLess(gv.t_to_p_one_sided(3.0), 0.002)
        self.assertGreater(gv.t_to_p_one_sided(-3.0), 0.99)  # significant but wrong direction
        self.assertAlmostEqual(gv.t_to_p_one_sided(0.0), 0.5)

    def test_rank_ic_perfect(self):
        d = np.repeat(np.arange(3), 200)
        f = np.tile(np.arange(200.0), 3)
        ic = gv.daily_rank_ic(d, f, f * 2 + 1)
        self.assertTrue(np.allclose(ic.to_numpy(), 1.0))


class RuleTests(unittest.TestCase):
    def day(self, n=1000):
        return pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(n)], "_s": np.arange(n, 0, -1.0),
                             "industry": [f"I{i % 10}" for i in range(n)]})

    def test_avoid_losers_drops_bottom_30pct(self):
        kept = gv.rule_avoid_losers(self.day(), set())
        self.assertEqual(len(kept), 700)

    def test_buffered_topk_keeps_incumbents_inside_exit_band(self):
        day = self.day()
        incumbent = day.loc[300, "ts_code"]  # rank pct 0.30: outside entry (15%) but inside exit (35%)
        chosen = gv.rule_buffered_topk(day, {incumbent})
        self.assertIn(incumbent, chosen)
        self.assertEqual(len(chosen), 100)

    def test_buffered_topk_industry_cap(self):
        day = self.day()
        day["industry"] = "ONE"
        self.assertEqual(len(gv.rule_buffered_topk(day, set())), 15)


if __name__ == "__main__":
    unittest.main()
