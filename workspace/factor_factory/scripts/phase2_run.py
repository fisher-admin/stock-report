#!/usr/bin/env python3
"""Phase 2 batch runner: freeze pre-registered specs, then evaluate them through gauntlet v2.

  --write-specs  writes outputs/gauntlet_v2/phase2_batch<N>_specs.json (commit before --run)
  --run          refuses to run if the frozen specs differ from the code's specs
Batch-end FDR is Benjamini-Hochberg over every research result in the registry.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gauntlet_v2 as gv  # noqa: E402
import phase2_factors as pf  # noqa: E402

BATCHES = {1: pf.BATCH1, 2: pf.BATCH2}


def spec_path(n: int) -> Path:
    return gv.OUT / f"phase2_batch{n}_specs.json"


def frozen(n: int) -> list[dict]:
    from dataclasses import asdict
    return [{**asdict(s), "spec_hash": s.spec_hash()} for s, _ in pf.specs_for(BATCHES[n])]


def attach_inputs(ctx: gv.Context) -> None:
    cols = ["close", "amount", "winner_rate", "cost_5pct", "cost_50pct", "cost_95pct", "weight_avg"]
    p = pd.read_parquet(gv.DATA / "research_panel_v2.parquet", columns=["ts_code", *cols])
    if not (p["ts_code"].to_numpy() == ctx.panel["ts_code"].to_numpy()).all():
        raise RuntimeError("panel order changed")
    ctx.panel_amount = p["amount"].astype(float).to_numpy()
    ctx.panel_chip = p[cols].astype(float).reset_index(drop=True)


def row(res: dict) -> dict:
    g1, g2, g3, g4, g5 = (res[k] for k in ("g1_ic", "g2_tails", "g3_portfolios", "g4_residual", "g5_placebo"))
    p1, p2 = g3["P1_avoid_losers"], g3["P2_buffered_top100"]
    return {"factor": res["spec"]["factor_id"], "h": res["spec"]["horizon"], "dir": res["spec"]["direction"],
            "disc_ic": g1["discovery"]["mean_ic"], "disc_t": g1["discovery"]["nw_t"],
            "val_ic": g1["validation"]["mean_ic"], "val_t": g1["validation"]["nw_t"],
            "all_t": g1["nw_t"], "p": g1["p_value"],
            "resid_ic": g4["residual_ic"], "resid_t": g4["residual_nw_t"],
            "bottom_bps": g2["bottom_vigintile_bps"], "top_bps": g2["top_vigintile_bps"],
            "p1_ann": p1["ann_excess_pct"], "p1_t": p1["t"], "p2_ann": p2["ann_excess_pct"], "p2_t": p2["t"],
            "placebo_sigma": g5["sigma"], "placebo_pass": g5["pass"],
            "gate_verdict": res["verdict"], "failed_gates": res["failed_gates"],
            "registration_id": res["registration_id"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, choices=[1, 2], required=True)
    ap.add_argument("--write-specs", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args(argv)
    gv.OUT.mkdir(parents=True, exist_ok=True)
    if a.write_specs:
        spec_path(a.batch).write_text(json.dumps({"frozen_at": datetime.now(gv.TZ).isoformat(),
                                                  "holdout_start": gv.HOLDOUT_START, "fdr_q": gv.FDR_Q,
                                                  "specs": frozen(a.batch)}, ensure_ascii=False, indent=2))
        print(f"wrote {spec_path(a.batch)} ({len(frozen(a.batch))} specs)")
    if not a.run:
        return 0
    saved = json.loads(spec_path(a.batch).read_text())["specs"]
    if saved != frozen(a.batch):
        raise SystemExit("frozen specs differ from code: refusing to run (a changed definition is a new factor_id)")
    ctx = gv.load_context()
    attach_inputs(ctx)
    styles = gv.baseline_styles(ctx)
    cache: dict = {}
    rows, wide_cache = [], {}
    for spec, fn in pf.specs_for(BATCHES[a.batch]):
        if fn.__name__ not in wide_cache:
            wide_cache.clear()
            wide_cache[fn.__name__] = fn(ctx)
        res = gv.evaluate(ctx, spec, wide_cache[fn.__name__], styles=styles, bench_cache=cache)
        r = row(res)
        rows.append(r)
        print(f"{r['factor']:<32} disc t {r['disc_t']:+6.2f} val t {r['val_t']:+6.2f} resid t {r['resid_t']:+6.2f} "
              f"P1 {r['p1_ann']:+6.2f}% (t {r['p1_t']:+.2f}) P2 {r['p2_ann']:+6.2f}% {r['gate_verdict']}", flush=True)
    # batch-end BH over the whole research family in the registry
    recs = [x for x in gv.registry_records() if x.get("kind") == "result" and x.get("role") == "research"]
    fam = {x["registration_id"]: x["p_value"] for x in recs}
    passed = gv.bh_fdr(fam)
    for r in rows:
        r["fdr_family_size"] = len(fam)
        r["fdr_pass"] = bool(passed.get(r["registration_id"], False))
        r["final_verdict"] = "PASS" if (r["gate_verdict"] == "PASS" and r["fdr_pass"]) else "REJECT"
    out = {"batch": a.batch, "generated_at": datetime.now(gv.TZ).isoformat(), "panel_sha256": ctx.panel_sha,
           "labels_sha256": ctx.labels_sha, "fdr_family_size": len(fam), "rows": rows}
    (gv.OUT / f"phase2_batch{a.batch}_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"passed": [r["factor"] for r in rows if r["final_verdict"] == "PASS"], "family": len(fam)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
