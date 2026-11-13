import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cost_impact as ci  # noqa: E402
import unit_guard as ug  # noqa: E402


def _frames(amount_scale=1.0, mv_scale=1.0):
    n = 200
    rng = np.random.default_rng(0)
    close = rng.uniform(5, 40, n)
    vol = rng.uniform(2e4, 2e5, n)                       # 手
    amount = vol * 100 * close / 1e3 * rng.uniform(0.97, 1.03, n)  # 千元
    circ_mv = rng.uniform(2e5, 2e6, n)                   # 万元
    turn = amount * 1e3 / (circ_mv * 1e4) * 100
    d = pd.DataFrame({"ts_code": range(n), "close": close, "vol": vol, "amount": amount * amount_scale})
    b = pd.DataFrame({"ts_code": range(n), "circ_mv": circ_mv * mv_scale, "turnover_rate": turn,
                      "turnover_rate_f": turn * 2.4})
    return d, b


def test_guard_passes_on_correct_units_and_fails_on_scaled_units():
    assert ug.check_frame(*_frames())["ok"]
    assert not ug.check_frame(*_frames(amount_scale=1000))["ok"]   # amount silently in 元
    assert not ug.check_frame(*_frames(mv_scale=1e4))["ok"]        # circ_mv silently in 元


def test_impact_sqrt_rule_and_capacity_flag():
    adv = pd.Series({"A": 1e8, "B": 1e7, "C": np.nan})
    r = ci.round_trip_impact(1e6, adv)       # A: p=1% -> 10bp/leg ; B: p=10% -> ~31.6bp/leg
    assert abs(r["A"] - 20e-4) < 1e-9 and abs(r["B"] - 2 * 10 * np.sqrt(10) / 1e4) < 1e-9 and np.isnan(r["C"])
    assert ci.round_trip_impact(1e9, pd.Series({"X": 1e6}))["X"] == 2 * ci.CAP_BPS / 1e4
    b = ci.breaches(1e6, adv)
    assert (not b["A"]) and b["B"]
