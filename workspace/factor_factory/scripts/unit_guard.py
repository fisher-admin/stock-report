#!/usr/bin/env python3
"""Unit/magnitude guard for the production caches the greenfield stack reads.

Units in the caches (verified 2026-10-15): daily.amount = 千元, daily.vol = 手, daily_basic.circ_mv = 万元.
`config/universe_rules.yaml` still says `unit: yuan` for the liquidity metric: that label is wrong, so a
silent change of units (or someone "fixing" the config by multiplying) would reshape the universe.
The guard asserts the relationships that hold only for the right units:

  amount*1000 / (vol*100)      ≈ price                      (median ratio to close in 0.9-1.1)
  amount*1000 / (circ_mv*1e4)  ≈ turnover_rate (%) / 100    (median ratio in 0.8-1.25)
  median amount (千元), median circ_mv (万元), median turnover_rate_f within sane A-share bands

  python unit_guard.py [--date YYYYMMDD]    # default: latest session with both caches; exit 1 on failure
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
BANDS = {
    "amount_median_kyuan": (1e4, 5e5),         # 0.1亿 - 5亿 per name per day
    "circ_mv_median_wan": (1e5, 2e6),          # 10亿 - 200亿
    "turnover_f_median_pct": (0.3, 20.0),
    "amount_vol_price_ratio": (0.9, 1.1),
    "amount_circmv_turnover_ratio": (0.8, 1.25),
}


def check_frame(daily: pd.DataFrame, basic: pd.DataFrame) -> dict:
    m = daily.merge(basic[["ts_code", "circ_mv", "turnover_rate", "turnover_rate_f"]], on="ts_code", how="inner")
    m = m[(m["vol"] > 0) & (m["amount"] > 0) & (m["close"] > 0) & (m["circ_mv"] > 0) & (m["turnover_rate"] > 0)]
    vals = {
        "amount_median_kyuan": float(daily.loc[daily["amount"] > 0, "amount"].median()),
        "circ_mv_median_wan": float(basic["circ_mv"].median()),
        "turnover_f_median_pct": float(basic["turnover_rate_f"].median()),
        "amount_vol_price_ratio": float((m["amount"] * 1e3 / (m["vol"] * 100) / m["close"]).median()),
        "amount_circmv_turnover_ratio": float((m["amount"] * 1e3 / (m["circ_mv"] * 1e4) * 100 / m["turnover_rate"]).median()),
    }
    out = {}
    for k, v in vals.items():
        lo, hi = BANDS[k]
        out[k] = {"value": round(v, 4), "band": [lo, hi], "ok": bool(np.isfinite(v) and lo <= v <= hi)}
    return {"rows": int(len(m)), "checks": out, "ok": all(c["ok"] for c in out.values())}


def run(date: str | None = None) -> dict:
    import research_panel_v2 as rp
    if date is None:
        have = sorted(p.stem.split("_")[-1] for p in rp.BACKTEST_CACHE.glob("daily_2*.parquet"))
        for d in reversed(have):
            if (rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet").exists():
                date = d
                break
    if date is None:
        return {"ok": False, "reason": "no date with both daily and daily_basic caches"}
    daily = pd.read_parquet(rp.BACKTEST_CACHE / f"daily_{date}.parquet")
    basic = pd.read_parquet(rp.DAILY_BASIC_DIR / f"daily_basic_{date}.parquet")
    return {"date": date, **check_frame(daily, basic)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    res = run(ap.parse_args(argv).date)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
