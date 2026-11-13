"""Pure-math checks for scripts/tier_monitor.py (no production data)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tier_monitor as tm  # noqa: E402


def test_stock_returns_cost_and_blocked_buy():
    o = pd.Series({"A": 10.0, "B": 10.0, "C": 10.0})
    c = pd.Series({"A": 11.0, "B": 9.0, "C": 12.0})
    can = pd.Series({"A": True, "B": True, "C": False})  # C opened limit-up: not buyable
    r = tm.stock_returns(o, c, can)
    assert abs(r["A"] - (0.10 - tm.ROUND_TRIP_COST)) < 1e-12
    assert abs(r["B"] - (-0.10 - tm.ROUND_TRIP_COST)) < 1e-12
    assert np.isnan(r["C"])
    m, n_ok, n = tm.cohort_mean(r, ["A", "B", "C", "MISSING"])
    assert n_ok == 2 and n == 4 and abs(m - (-tm.ROUND_TRIP_COST)) < 1e-12


def test_nw_t_matches_plain_t_at_lag0_and_needs_samples():
    x = [0.01, 0.02, -0.005, 0.015, 0.0]
    a = np.array(x)
    plain = a.mean() / (a.std(ddof=0) / np.sqrt(len(a)))
    assert abs(tm.nw_t(x, 0) - plain) < 1e-9
    assert tm.nw_t([0.1, 0.2], 0) is None


def test_summarize_spread_and_verdict_gate():
    days = [{"date": f"d{i}", "ret": {"DOUBLE": 0.01, "NON_DOUBLE": 0.0, "KEEP": 0.0, "DANGER": None,
                                      "V43_ALL": 0.002, "BENCH": 0.001},
             "trades": {"DOUBLE": [0.01, -0.01, 0.03]}} for i in range(5)]
    days[0]["ret"]["DOUBLE"] = None  # empty DOUBLE day: excluded from DOUBLE stats and spreads
    s = tm.summarize(days, 5)
    assert s["DOUBLE"]["days"] == 4 and abs(s["DOUBLE"]["mean_excess"] - 0.009) < 1e-12
    assert s["DANGER"]["days"] == 0 and s["DANGER"]["mean_net"] is None
    sp = tm.spread(days, "DOUBLE", "NON_DOUBLE", 5)
    assert sp["days"] == 4 and sp["lag"] == 4
    assert tm.verdict(sp)["status"] == "insufficient_sample"
    big = {"days": 60, "mean": 0.004, "nw_t": 2.3}
    assert tm.verdict(big)["status"] == "pass"
    assert tm.verdict({**big, "nw_t": 1.2})["status"] == "fail"


def test_spec_frozen_matches_code():
    import json
    saved = json.loads(tm.SPEC_PATH.read_text())
    assert saved["spec_hash"] == tm.spec_hash()
