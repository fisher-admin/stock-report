import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lifecycle_monitor as lm  # noqa: E402


def test_spearman_ic_known_values_and_min_n():
    x = pd.Series(np.arange(200.0))
    assert abs(lm.spearman_ic(x, x * 3 + 1) - 1.0) < 1e-12
    assert abs(lm.spearman_ic(x, -x) + 1.0) < 1e-12
    assert lm.spearman_ic(x.iloc[:50], x.iloc[:50]) is None          # below MIN_N
    y = x.copy(); y.iloc[:150] = np.nan
    assert lm.spearman_ic(x, y) is None                                # NaNs shrink the sample below MIN_N


def test_flags_fire_only_with_enough_history():
    rng = np.random.default_rng(1)
    good, bad = list(rng.normal(0.08, 0.05, 30)), list(rng.normal(-0.02, 0.05, 30))
    assert lm.rolling_flags(good, 5, 0.081)["flags"] == []
    assert set(lm.rolling_flags(bad, 5, 0.081)["flags"]) == {"ic_not_positive", "ic_below_backtest"}
    assert lm.rolling_flags(bad[:10], 5, 0.081)["flags"] == []         # < ROLL days: no gap/t flags
    assert lm.rolling_flags([0.02] * 10 + [-0.01] * 6, 5, None)["flags"] == ["negative_streak"]
    assert "negative_streak" not in lm.rolling_flags([0.02] * 10 + [-0.01] * 6, 1, None)["flags"]  # h=1 too noisy


def test_none_days_are_skipped():
    r = lm.rolling_flags([None, 0.05, None, 0.03], 5, None)
    assert r["n"] == 2 and abs(r["mean"] - 0.04) < 1e-12


def test_regime_of_matches_forward_shadow_rule():
    assert lm.regime_of(0.004) == "up"
    assert lm.regime_of(0.0) == "down" and lm.regime_of(-0.01) == "down"
    assert lm.regime_of(None) is None and lm.regime_of(float("nan")) is None


def test_regime_baseline_stops_false_alarm_in_rising_market():
    # 20 rising-market days whose IC sits at the up-regime baseline (+0.019): the pooled +0.081 baseline flags it,
    # the regime-matched baseline does not.
    rng = np.random.default_rng(7)
    ics = list(rng.normal(0.019, 0.04, 20))
    regs = ["up"] * 20
    rb = {"up": 0.019, "down": 0.159}
    assert "ic_below_backtest" in lm.rolling_flags(ics, 5, 0.081)["flags"]
    r = lm.rolling_flags(ics, 5, 0.081, regs, rb)
    assert r["baseline_mode"] == "regime" and "ic_below_backtest" not in r["flags"]
    assert abs(r["baseline_expected"] - 0.019) < 1e-12 and abs(r["gap_z"]) < 2
    assert r["regime"]["up"]["roll_n"] == 20 and r["regime"]["down"]["roll_n"] == 0
    assert r["regime"]["down"]["roll_mean"] is None and r["regime"]["up"]["baseline"] == 0.019


def test_regime_baseline_still_flags_a_real_decay_in_falling_market():
    # Falling-market days should earn ~+0.159; +0.05 passes the pooled check but fails the matched one.
    rng = np.random.default_rng(3)
    ics = list(rng.normal(0.05, 0.04, 20))
    rb = {"up": 0.019, "down": 0.159}
    assert "ic_below_backtest" not in lm.rolling_flags(ics[:19] + [0.3], 5, 0.081)["flags"]
    r = lm.rolling_flags(ics, 5, 0.081, ["down"] * 20, rb)
    assert "ic_below_backtest" in r["flags"] and abs(r["baseline_expected"] - 0.159) < 1e-12


def test_regime_mix_counts_and_per_day_matching():
    ics = [0.02, None, 0.15, 0.01, 0.16] * 5           # 20 valid days, 10 per regime once None days drop
    regs = ["up", "up", "down", "up", "down"] * 5
    r = lm.rolling_flags(ics, 5, 0.081, regs, {"up": 0.019, "down": 0.159})
    assert r["n"] == 20 and r["regime"]["up"]["n"] == 10 and r["regime"]["down"]["n"] == 10
    assert abs(r["regime"]["up"]["mean"] - 0.015) < 1e-12 and abs(r["regime"]["down"]["mean"] - 0.155) < 1e-12
    assert abs(r["baseline_expected"] - (0.019 + 0.159) / 2) < 1e-12   # None-IC days dropped before matching
    assert r["gap_z"] < 0                                # each day 0.004 below its own regime's baseline
    # untagged days fall back to the pooled baseline; incomplete regime baselines → pooled mode
    r2 = lm.rolling_flags([0.05] * 19 + [0.06], 5, 0.081, [None] * 20, {"up": 0.019, "down": 0.159})
    assert abs(r2["baseline_expected"] - 0.081) < 1e-12
    assert lm.rolling_flags([0.05] * 20, 5, 0.081, ["up"] * 20, {"up": 0.019})["baseline_mode"] == "pooled"


def test_load_regime_baselines_and_holdout_guard(tmp_path):
    import json
    grp = {"上涨": {"days": 682, "mean": 0.0191}, "下跌": {"days": 613, "mean": 0.1593}}
    doc = {"window": "20210225–20260630", "horizon": 5, "exploratory": True,
           "results": {"comp_rank_ew6": {"by_regime": {"ex-ante · x": {}, "ex-post · 未来 5 日市场": grp}},
                       "vpd_10": {"by_regime": {"ex-post · 未来 5 日市场": {"上涨": {"days": 3, "mean": None}}}}}}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(doc, ensure_ascii=False))
    b, meta = lm.load_regime_baselines(p)
    assert b == {"comp_rank_ew6": {5: {"up": 0.0191, "down": 0.1593, "up_days": 682, "down_days": 613}}}
    assert meta["used"] and meta["horizons"] == [5]
    p.write_text(json.dumps({**doc, "window": "20210225–20260815"}, ensure_ascii=False))   # reaches holdout
    b, meta = lm.load_regime_baselines(p)
    assert b == {} and not meta["used"] and "holdout" in meta["reason"]
    b, meta = lm.load_regime_baselines(tmp_path / "missing.json")
    assert b == {} and not meta["used"]


def test_exporter_keeps_regime_fields():
    import importlib.util
    p = Path(__file__).resolve().parents[2] / "skills/stock-system-orchestrator/scripts/export_factor_lifecycle.py"
    spec = importlib.util.spec_from_file_location("export_factor_lifecycle", p)
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    h5 = {"baseline_mode": "regime", "baseline_expected": 0.06, "regime": {"up": {"roll_n": 14}, "down": {"roll_n": 6}}}
    src = {"contract_version": "lifecycle_monitor_v1", "status": "ok", "results": {"comp_rank_ew6": {"by_h": {"h5": h5}}},
           "regime_days": {"h5": {"up": 14, "down": 6}}, "regime_baseline": {"used": True, "horizons": [5]}, "generated_at": "x"}
    doc = ex.build(src, "2026-10-14T00:00:00+08:00")
    assert doc["regime_days"] == src["regime_days"] and doc["regime_baseline"]["used"]
    assert doc["results"]["comp_rank_ew6"]["by_h"]["h5"]["regime"]["down"]["roll_n"] == 6
    assert "generated_at" in doc and doc["generated_at"] != "x"
