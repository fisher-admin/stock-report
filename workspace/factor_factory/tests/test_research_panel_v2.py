#!/usr/bin/env python3
"""Unit tests for Research Panel v2 builder rules and validation checks (synthetic data only)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import research_panel_v2 as rp  # noqa: E402
import validate_research_panel_v2 as vp  # noqa: E402

SESSIONS = [f"202601{d:02d}" for d in range(5, 31) if d not in (10, 11, 17, 18, 24, 25)]


def synthetic_panel(n_codes: int = 150, seed: int = 0, leak: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_codes):
        code = f"{600000 + i:06d}.SH"
        pb = rng.lognormal(0, 0.5)
        for d in SESSIONS:
            pb *= float(np.exp(rng.normal(0, 0.01)))
            rows.append({"ts_code": code, "trade_date": d, "pct_chg": float(rng.normal(0, 2)), "pb": pb,
                         "turnover_rate_f": float(rng.lognormal(0, 0.3)), "circ_mv": float(rng.lognormal(12, 1)),
                         "winner_rate": float(rng.uniform(0, 100)), "rsi_6": float(rng.uniform(0, 100))})
    p = pd.DataFrame(rows)
    if leak:
        # pb secretly carries tomorrow's return: the shift test must flag it.
        nxt = p.groupby("ts_code")["pct_chg"].shift(-1)
        p["pb"] = nxt.fillna(0.0)
    return p


class LagRuleTests(unittest.TestCase):
    def test_lag_uses_official_calendar_not_own_rows(self):
        frame = pd.DataFrame({"ts_code": ["A", "A", "A"], "trade_date": [SESSIONS[0], SESSIONS[1], SESSIONS[3]],
                              "dv_ttm": [1.0, 2.0, 4.0]})
        out = rp.lag_by_session(frame, ["dv_ttm"], SESSIONS, 1).set_index("trade_date")["dv_ttm_lag1"]
        self.assertEqual(out[SESSIONS[1]], 1.0)
        self.assertEqual(out[SESSIONS[2]], 2.0)  # a suspension day still gets T-1's value
        # SESSIONS[3]'s lag would be SESSIONS[2], where A has no row: it must not reach back to SESSIONS[1].
        self.assertNotIn(SESSIONS[3], out.index)

    def test_lag_never_exposes_same_day_value(self):
        frame = pd.DataFrame({"ts_code": ["A"] * 4, "trade_date": SESSIONS[:4], "dv_ttm": [1.0, 2.0, 3.0, 4.0]})
        out = rp.lag_by_session(frame, ["dv_ttm"], SESSIONS, 1).set_index("trade_date")["dv_ttm_lag1"]
        merged = frame.set_index("trade_date").join(out)
        self.assertFalse((merged["dv_ttm"] == merged["dv_ttm_lag1"]).any())


class StructureTests(unittest.TestCase):
    def test_forward_and_raw_revisable_columns_fail(self):
        p = pd.DataFrame({"ts_code": ["A"], "trade_date": [SESSIONS[0]], "next_return_1d": [0.1], "dv_ttm": [1.0]})
        r = vp.check_structure(p, SESSIONS)
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("next_return_1d", r["forbidden_columns"])
        self.assertIn("dv_ttm", r["raw_revisable_columns_present"])

    def test_duplicate_keys_fail(self):
        p = pd.DataFrame({"ts_code": ["A", "A"], "trade_date": [SESSIONS[0]] * 2})
        self.assertEqual(vp.check_structure(p, SESSIONS)["status"], "FAIL")

    def test_misaligned_daily_basic_close_fails(self):
        p = pd.DataFrame({"close": [10.0, 10.0], "db_close": [10.0, 11.0], "src_block": ["x", "x"]})
        self.assertEqual(vp.check_pit_alignment(p)["status"], "FAIL")


class LookaheadShiftTests(unittest.TestCase):
    def test_clean_panel_passes_and_positive_control_fires(self):
        r = vp.check_lookahead_shift(synthetic_panel(), SESSIONS, every=1)
        self.assertTrue(r["positive_control_detected"])
        self.assertFalse(r["factors"]["pb"]["suspected_leak"])

    def test_planted_leak_is_flagged(self):
        r = vp.check_lookahead_shift(synthetic_panel(leak=True), SESSIONS, every=1)
        self.assertTrue(r["factors"]["pb"]["suspected_leak"])
        self.assertEqual(r["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
