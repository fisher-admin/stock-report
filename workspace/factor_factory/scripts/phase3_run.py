#!/usr/bin/env python3
"""Phase 3: pre-registered multi-factor composites of the six Phase 2 robust survivors.

Survivors and their Phase 2 pre-registered directions (unchanged):
  ivol_60 (-1), turn_20 (-1), turn_abn_5_60 (-1), val_comp_indneu (+1), vpd_10 (+1), turn_cv_20 (-1)

comp_rank_ew6   per date, mean of the six direction-adjusted cross-sectional percentile ranks
                (universe rows; >= 4 of 6 present). No style stripping before forming.
comp_ortho_ew6  pillars ivol_60 and turn_20 kept intact; each of the other four is regressed
                cross-sectionally on [1, ivol_60, turn_20] ranks per date and its residual
                re-ranked; weights = max(IC-IR, 0) of each component measured on DISCOVERY dates
                only (20210201-20241231, horizon-matched); >= 4 components present, weights
                renormalized over the available ones. Discovery IC is in-sample by construction.

  --write-specs  freeze outputs/gauntlet_v2/phase3_specs.json (commit before --run)
  --run          refuses to run if the frozen specs differ; holdout >= 20260701 stays locked
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gauntlet_v2 as gv  # noqa: E402
import phase2_factors as pf  # noqa: E402
import phase2_run as p2  # noqa: E402

SURVIVORS = [(pf.ivol_60, -1), (pf.turn_20, -1), (pf.turn_abn_5_60, -1), (pf.val_comp_indneu, 1),
             (pf.vpd_10, 1), (pf.turn_cv_20, -1)]
PILLARS = ("ivol_60", "turn_20")
MIN_PRESENT = 4
SPEC_PATH = gv.OUT / "phase3_specs.json"

DEFS = {
    "comp_rank_ew6": ("Equal-weight rank composite of the six robust survivors beats the investable EW "
                      "benchmark in the avoid-the-losers portfolio.",
                      "mean over k of pct_rank_d(dir_k * f_k) on universe rows; >=4 of 6 present"),
    "comp_ortho_ew6": ("Pillar-preserving orthogonalized, discovery-IC-IR-weighted composite of the six "
                       "survivors beats the investable EW benchmark in the avoid-the-losers portfolio.",
                       "pillars ivol_60, turn_20 kept; others residualized on [1, pillar ranks] per date and "
                       "re-ranked; weights max(IC-IR,0) on discovery dates (h-matched); >=4 present"),
}


def specs() -> list[gv.FactorSpec]:
    return [gv.FactorSpec(f"p3_{name}_h{h}", "P3_composite", hyp, 1, h, "research", definition)
            for name, (hyp, definition) in DEFS.items() for h in (5, 20)]


def frozen() -> list[dict]:
    return [{**asdict(s), "spec_hash": s.spec_hash()} for s in specs()]


def component_ranks(ctx: gv.Context) -> pd.DataFrame:
    d = ctx.panel["trade_date"].to_numpy()
    out = {}
    for fn, direction in SURVIVORS:
        v = direction * ctx.wide_to_rows(fn(ctx))
        out[fn.__name__] = pd.Series(v).groupby(d).rank(pct=True).to_numpy()
    return pd.DataFrame(out)


def corr_matrix(ctx: gv.Context, ranks: pd.DataFrame) -> pd.DataFrame:
    d = ctx.panel["trade_date"].to_numpy()
    m = (d >= gv.DISCOVERY[0]) & (d <= gv.DISCOVERY[1])
    x = ranks[m].assign(_d=d[m]).dropna()
    mats = [g.drop(columns="_d").corr(method="spearman").to_numpy() for _, g in x.groupby("_d") if len(g) >= 100]
    return pd.DataFrame(np.mean(mats, axis=0), index=ranks.columns, columns=ranks.columns)


def comp_rank_ew6(ranks: pd.DataFrame) -> np.ndarray:
    n = ranks.notna().sum(axis=1)
    return ranks.mean(axis=1, skipna=True).where(n >= MIN_PRESENT).to_numpy()


def comp_ortho_ew6(ctx: gv.Context, ranks: pd.DataFrame, h: int) -> tuple[np.ndarray, dict]:
    d = ctx.panel["trade_date"].to_numpy()
    z = ranks - 0.5
    comps = {p: z[p] for p in PILLARS}
    others = [c for c in ranks.columns if c not in PILLARS]
    base = z[list(PILLARS)]
    for c in others:
        frame = pd.concat([z[c].rename("y"), base], axis=1).assign(_d=d)

        def resid(g: pd.DataFrame) -> pd.Series:
            g = g.dropna()
            if len(g) < 50:
                return pd.Series(np.nan, index=g.index)
            X = np.column_stack([np.ones(len(g)), g[list(PILLARS)].to_numpy()])
            beta, *_ = np.linalg.lstsq(X, g["y"].to_numpy(), rcond=None)
            return pd.Series(g["y"].to_numpy() - X @ beta, index=g.index)

        e = frame.groupby("_d", group_keys=False)[["y", *PILLARS]].apply(resid).reindex(frame.index)
        comps[c] = e.groupby(d).rank(pct=True) - 0.5
    comps = pd.DataFrame(comps)[list(ranks.columns)]
    # discovery-only IC-IR weights, horizon-matched
    y = gv.excess(ctx, h)
    disc = (d >= gv.DISCOVERY[0]) & (d <= gv.DISCOVERY[1]) & np.isfinite(ctx.labels[f"fwd_open_{h}"].to_numpy())
    weights = {}
    for c in comps.columns:
        ic = gv.daily_rank_ic(d[disc], comps[c].to_numpy()[disc], y[disc])
        weights[c] = max(float(ic.mean() / ic.std()) if ic.std() > 0 else 0.0, 0.0)
    if sum(weights.values()) <= 0:
        weights = {c: 1.0 for c in comps.columns}
    w = pd.Series(weights)
    present = comps.notna()
    num = comps.fillna(0.0).mul(w, axis=1).sum(axis=1)
    den = present.mul(w, axis=1).sum(axis=1)
    out = (num / den.where(den > 0)).where(present.sum(axis=1) >= MIN_PRESENT)
    tot = sum(weights.values())
    return out.to_numpy(), {k: round(v / tot, 4) for k, v in weights.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-specs", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args(argv)
    gv.OUT.mkdir(parents=True, exist_ok=True)
    if a.write_specs:
        SPEC_PATH.write_text(json.dumps({"frozen_at": datetime.now(gv.TZ).isoformat(), "holdout_start": gv.HOLDOUT_START,
                                         "survivors": [(fn.__name__, dr) for fn, dr in SURVIVORS], "pillars": PILLARS,
                                         "min_present": MIN_PRESENT, "p_sided": gv.P_SIDED, "specs": frozen()},
                                        ensure_ascii=False, indent=2))
        print(f"wrote {SPEC_PATH} ({len(frozen())} specs)")
    if not a.run:
        return 0
    if json.loads(SPEC_PATH.read_text())["specs"] != frozen():
        raise SystemExit("frozen Phase 3 specs differ from code: refusing to run")
    ctx = gv.load_context()
    p2.attach_inputs(ctx)
    ranks = component_ranks(ctx)
    cm = corr_matrix(ctx, ranks)
    styles = gv.baseline_styles(ctx)
    cache: dict = {}
    rows, extras = [], {}
    rank_comp = comp_rank_ew6(ranks)
    for spec in specs():
        h = spec.horizon
        if spec.factor_id.startswith("p3_comp_rank_ew6"):
            vals = rank_comp
        else:
            vals, wts = comp_ortho_ew6(ctx, ranks, h)
            extras[f"ortho_weights_h{h}"] = wts
        wide = pf.rows_to_wide(ctx, vals)
        res = gv.evaluate(ctx, spec, wide, styles=styles, bench_cache=cache)
        r = p2.row(res)
        r["p1_yearly_excess_pct"] = res["g3_portfolios"]["P1_avoid_losers"]["yearly_excess_pct"]
        r["p2_yearly_excess_pct"] = res["g3_portfolios"]["P2_buffered_top100"]["yearly_excess_pct"]
        r["p1_turnover"] = res["g3_portfolios"]["P1_avoid_losers"]["avg_turnover"]
        r["residual_corr_with_styles"] = res["g4_residual"]["rank_corr_with_styles"]
        rows.append(r)
        print(f"{r['factor']:<24} disc t {r['disc_t']:+6.2f} val t {r['val_t']:+6.2f} resid t {r['resid_t']:+6.2f} "
              f"P1 {r['p1_ann']:+6.2f}% (t {r['p1_t']:+.2f}) P2 {r['p2_ann']:+6.2f}% {r['gate_verdict']}", flush=True)
    recs = [x for x in gv.registry_records() if x.get("kind") == "result" and x.get("role") == "research"]
    fam = {x["registration_id"]: x["p_value"] for x in recs}
    passed = gv.bh_fdr(fam)
    for r in rows:
        r["fdr_family_size"] = len(fam)
        r["fdr_pass"] = bool(passed.get(r["registration_id"], False))
        r["final_verdict"] = "PASS" if (r["gate_verdict"] == "PASS" and r["fdr_pass"]) else "REJECT"
    out = {"batch": 3, "generated_at": datetime.now(gv.TZ).isoformat(), "panel_sha256": ctx.panel_sha,
           "labels_sha256": ctx.labels_sha, "fdr_family_size": len(fam),
           "discovery_rank_corr": cm.round(3).to_dict(), **extras, "rows": rows}
    (gv.OUT / "phase3_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(cm.round(3).to_string())
    print(json.dumps(extras))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
