#!/usr/bin/env python3
"""D5 calibration: does gauntlet v2 reproduce the reference verdicts on record?

Reference verdicts (factor_alpha_lab reports/STRATEGY_REPORT.md, 2026-10-09; plan §1):
  R1  The daily close-to-close EW universe mean overstates the investable EW benchmark
      (5-day rebalance, same costs) by about 6%/yr.
  R2  vpd_20 = -corr(price, volume, 20d) carries positive 5-day rank IC (raw 2021-25 t 6.65),
      and the edge sits in the bottom tail (lab composite: bottom vigintile -66 bp, top +10 bp).
  R3  rev_5 (5-day reversal) was rejected (IS t 0.56).
Controls:
  C0  pure noise must be rejected (and must not beat its own placebo)
  C1  a planted leak (the 5-day label plus noise) must clear the IC and placebo gates

Panel v2 differs from the lab panel (delisted names restored, uncensored labels), so the
criteria are directional, with tolerance bands where the record gives a magnitude.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gauntlet_v2 as gv  # noqa: E402


def factor_vpd20(ctx: gv.Context) -> np.ndarray:
    ac = pd.DataFrame(ctx.mats["adj_close"])
    vol = pd.DataFrame(ctx.mats["vol"]).where(ac.notna())
    return -(ac.rolling(20, min_periods=15).corr(vol)).to_numpy()


def factor_rev5(ctx: gv.Context) -> np.ndarray:
    ac = pd.DataFrame(ctx.mats["adj_close"]).ffill()
    return (-(ac / ac.shift(5) - 1)).to_numpy()


def factor_noise(ctx: gv.Context) -> np.ndarray:
    return np.random.default_rng(7).normal(size=ctx.mats["adj_close"].shape)


def factor_leak(ctx: gv.Context) -> np.ndarray:
    w = np.full(ctx.mats["adj_close"].shape, np.nan)
    t = ctx.panel["trade_date"].map(ctx.s_idx).to_numpy()
    j = ctx.panel["ts_code"].map(ctx.c_idx).to_numpy()
    y = ctx.labels["fwd_open_5"].to_numpy()
    w[t, j] = y + np.random.default_rng(11).normal(scale=np.nanstd(y) * 3, size=len(y))
    return w


def r1_c2c_overstatement(ctx: gv.Context) -> dict:
    c2c = pd.read_parquet(gv.DATA / "research_c2c_ew_reference_v2.parquet").set_index("trade_date")["c2c_ew_ret"]
    bench = pd.read_parquet(gv.DATA / "research_benchmark_v2.parquet")
    b5 = bench[(bench["horizon"] == 5) & (bench["signal_date"] < gv.HOLDOUT_START)].iloc[1:]
    first, last = b5["signal_date"].iloc[0], b5["signal_date"].iloc[-1]
    c = c2c[(c2c.index >= first) & (c2c.index <= last)].dropna()
    c2c_ann = float((1 + c).prod() ** (252 / len(c)) - 1)
    inv_ann = float((1 + b5["period_ret"]).prod() ** ((252 / 5) / len(b5)) - 1)
    gap = (c2c_ann - inv_ann) * 100
    return {"c2c_ew_ann_pct": round(c2c_ann * 100, 2), "investable_ew_5d_ann_pct": round(inv_ann * 100, 2),
            "overstatement_pct_per_yr": round(gap, 2), "record": "about +6%/yr",
            "reproduced": bool(3.0 <= gap <= 9.0)}


def main() -> int:
    ctx = gv.load_context()
    styles = gv.baseline_styles(ctx)
    cache: dict = {}
    specs = [
        (gv.FactorSpec("calib_vpd_20", "price_volume", "-corr(adj_close, vol, 20d); lab R2", 1, 5, "calibration",
                       "-rolling_corr(adj_close, vol, 20, min 15)"), factor_vpd20),
        (gv.FactorSpec("calib_rev_5", "reversal", "5-day reversal; lab R3 rejected", 1, 5, "calibration",
                       "-(adj_close/adj_close.shift(5)-1)"), factor_rev5),
        (gv.FactorSpec("control_noise", "control", "N(0,1) noise; must be rejected", 1, 5, "control",
                       "rng(7).normal"), factor_noise),
        (gv.FactorSpec("control_leak", "control", "fwd_open_5 + 3sd noise; must clear IC/placebo", 1, 5, "control",
                       "label + rng(11).normal(3*sd)"), factor_leak),
    ]
    results = {"R1": r1_c2c_overstatement(ctx)}
    for spec, fn in specs:
        res = gv.evaluate(ctx, spec, fn(ctx), styles=styles, bench_cache=cache)
        results[spec.factor_id] = res
        print(spec.factor_id, res["verdict"], res["failed_gates"], round(res["g1_ic"]["mean_ic"], 4),
              round(res["g1_ic"]["nw_t"], 2), flush=True)
    vpd, rev, noise, leak = (results[k] for k in ("calib_vpd_20", "calib_rev_5", "control_noise", "control_leak"))
    checks = {
        "R1_c2c_overstates_investable": results["R1"]["reproduced"],
        "R2_vpd_positive_ic": bool(vpd["g1_ic"]["nw_t"] >= 2.0 and vpd["g1_ic"]["mean_ic"] > 0),
        "R2_vpd_edge_in_bottom_tail": bool(vpd["g2_tails"]["edge_in_bottom_tail"]),
        "R3_rev5_rejected": rev["verdict"] == "REJECT",
        "C0_noise_rejected": bool(noise["verdict"] == "REJECT" and not noise["g5_placebo"]["pass"]),
        "C1_leak_clears_ic_and_placebo": bool(leak["g1_ic"]["pass"] and leak["g5_placebo"]["pass"]),
    }
    report = {"generated_at": datetime.now(gv.TZ).isoformat(), "panel_sha256": ctx.panel_sha,
              "labels_sha256": ctx.labels_sha, "checks": checks, "all_reproduced": all(checks.values()),
              "results": results}
    gv.OUT.mkdir(parents=True, exist_ok=True)
    (gv.OUT / "calibration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(checks, indent=2))
    return 0 if report["all_reproduced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
