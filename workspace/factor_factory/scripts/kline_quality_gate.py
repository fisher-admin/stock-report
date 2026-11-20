#!/usr/bin/env python3
"""K-line quality gate (pre-write check) for one session's OHLCV, used by forward_shadow_multi_v1.

A row is bad if any of: vol NaN or < 0; a traded row (vol > 0) with any of open/high/low/close/pre_close NaN or <= 0; high < max(open, close) or low > min(open, close);
vol < 0; for a traded row |close/pre_close - 1| > limit + 0.5% (limit 20% for 300/301, else 10%; ST 5% rows are
inside 10% so they never trip this); open outside [low, high].
The day fails if coverage of the universe < 98% or bad rows > 0.5% of the universe (or any duplicated code).
Pure function, no writes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_COVERAGE = 0.98
MAX_BAD_SHARE = 0.005


def check_day(px: pd.DataFrame | None, universe: pd.Series | list[str]) -> dict:
    uni = pd.Index([str(c) for c in universe])
    if px is None or px.empty:
        return {"ok": False, "reason": "no_price_file", "coverage": 0.0, "n_bad": None}
    if px.index.has_duplicates:
        return {"ok": False, "reason": "duplicated_codes", "coverage": None, "n_bad": None}
    f = px.reindex(uni)
    have = f["close"].notna()
    cov = float(have.mean()) if len(uni) else 0.0
    g = f[have]
    num = g[["open", "high", "low", "close", "pre_close", "vol"]].apply(pd.to_numeric, errors="coerce")
    traded = num["vol"] > 0                     # NaN vol -> not traded here, but flagged below
    fin_pos = np.isfinite(num[["open", "high", "low", "close", "pre_close"]]).all(axis=1) & \
        (num[["open", "high", "low", "close", "pre_close"]] > 0).all(axis=1)
    lim = np.where(g.index.str.startswith(("300", "301")), 0.20, 0.10)
    jump = (num["close"] / num["pre_close"] - 1).abs()
    bad = (
        num["vol"].isna()                                                    # NaN volume
        | (num["vol"] < 0)
        | (traded & ~fin_pos)                                                # traded row with NaN / <= 0 price field
        | (traded & fin_pos & (num["high"] + 1e-9 < np.maximum(num["open"], num["close"])))
        | (traded & fin_pos & (num["low"] - 1e-9 > np.minimum(num["open"], num["close"])))
        | (traded & fin_pos & (jump > lim + 0.005))
    )
    n_bad = int(bad.sum())
    ok = cov >= MIN_COVERAGE and n_bad <= MAX_BAD_SHARE * len(uni)
    return {"ok": bool(ok), "reason": None if ok else ("coverage" if cov < MIN_COVERAGE else "bad_rows"),
            "coverage": round(cov, 4), "n_bad": n_bad, "bad_examples": g.index[bad].tolist()[:10]}
