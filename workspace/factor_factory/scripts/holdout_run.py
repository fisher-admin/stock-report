#!/usr/bin/env python3
"""One-time holdout evaluation of comp_rank_ew6 (primary h=5; h=20 secondary diagnostic only).

Holdout = signal dates >= gv.HOLDOUT_START (20260701, locked since D5) through the panel end.
comp_rank_ew6 has no fitted parameters, so the Phase 3 definition is applied unchanged.

Pre-registered gates (primary, h=5) — frozen in outputs/gauntlet_v2/holdout_spec.json:
  H1 signal survival   mean daily rank IC > +0.02 and NW t >= 1.50 (direction-adjusted, one-sided)
  H2 bottom tail       vigintile-1 mean excess < 0 bp and V1 < V20
  H3 non-collapse      P1 avoid-the-losers net excess NW t > -1.0 and P1 ann. excess > P2 ann. excess
  bonus                P1 net excess > 0 over the window (reported, not a gate)
Verdict: PASS if H1, H2 and H3 all hold.

  --write-spec   freeze the spec (commit it before --run)
  --run          refuses unless the spec is committed and unchanged, and refuses if a holdout
                 result for this registration already exists in the registry (exactly once)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gauntlet_v2 as gv  # noqa: E402
import phase2_factors as pf  # noqa: E402
import phase2_run as p2  # noqa: E402
import phase3_run as p3  # noqa: E402
import research_labels_v2 as rl  # noqa: E402

SPEC_PATH = gv.OUT / "holdout_spec.json"
REG_ID = "holdout_comp_rank_ew6_v1"
SPEC = {
    "registration_id": REG_ID,
    "candidate": "p3_comp_rank_ew6_h5",
    "secondary_diagnostic": "p3_comp_rank_ew6_h20 (no gates; non-truncated dates only)",
    "composite_definition": p3.DEFS["comp_rank_ew6"][1],
    "phase3_spec_hashes": {s.factor_id: s.spec_hash() for s in p3.specs() if "rank_ew6" in s.factor_id},
    "window": {"signal_from": gv.HOLDOUT_START, "signal_to": "panel end (20261009)",
               "labels": "rows with a finite fwd_open_h only (right edge excluded)"},
    "gates": {
        "H1": {"mean_ic_gt": 0.02, "nw_t_ge": 1.50},
        "H2": {"v1_excess_bps_lt": 0.0, "v1_lt_v20": True},
        "H3": {"p1_net_excess_t_gt": -1.0, "p1_ann_excess_gt_p2": True},
        "bonus": {"p1_net_excess_gt": 0.0},
    },
    "verdict_rule": "PASS iff H1 and H2 and H3",
    "cost_model": asdict(rl.CostModel()),
    "runs_allowed": 1,
}


@dataclass(frozen=True)
class ZeroCost(rl.CostModel):
    commission_bps: float = 0.0
    transfer_bps: float = 0.0
    slippage_bps: float = 0.0

    def sell(self, trade_date: str) -> float:  # gross: no stamp duty either
        return 0.0


def spec_hash() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()[:16]


def committed_and_clean(path: Path) -> bool:
    ws = ROOT.parent
    rel = str(path.relative_to(ws))
    tracked = subprocess.run(["git", "-C", str(ws), "ls-files", "--error-unmatch", rel], capture_output=True).returncode == 0
    clean = subprocess.run(["git", "-C", str(ws), "diff", "--quiet", "HEAD", "--", rel]).returncode == 0
    return tracked and clean


def holdout_ctx(ctx: gv.Context) -> gv.Context:
    keep = (ctx.panel["trade_date"] >= gv.HOLDOUT_START).to_numpy()
    sub = gv.Context(ctx.panel[keep].reset_index(drop=True), ctx.labels[keep].reset_index(drop=True), ctx.sessions,
                     ctx.codes, ctx.mats, panel_sha=ctx.panel_sha, labels_sha=ctx.labels_sha, allow_holdout=True)
    return sub


def evaluate_h(hctx: gv.Context, wide: np.ndarray, h: int, styles_h: pd.DataFrame, primary: bool) -> dict:
    spec = gv.FactorSpec(f"holdout_comp_rank_ew6_h{h}", "P3_composite", "holdout", 1, h, "holdout",
                         p3.DEFS["comp_rank_ew6"][1])
    f = hctx.wide_to_rows(wide)
    m = gv._mask(hctx, h)
    d = hctx.panel["trade_date"].to_numpy()
    ic = gv.daily_rank_ic(d[m], f[m], gv.excess(hctx, h)[m])
    g1 = {"mean_ic": float(ic.mean()), "nw_t": gv.newey_west_t(ic.to_numpy(), h - 1), "days": int(len(ic)),
          "first_signal": str(ic.index.min()), "last_signal": str(ic.index.max())}
    g2 = gv.g2_tails(hctx, f, spec)
    g4 = gv.g4_residual(hctx, f, spec, styles_h)
    out = {"h": h, "effective_signal_days": g1["days"], "ic": g1,
           "residual_ic": {"mean": g4["residual_ic"], "nw_t": g4["residual_nw_t"]}, "tails": g2}
    if primary or h == 20:
        net, gross = {}, {}
        for label, cost in (("net", rl.CostModel()), ("gross", ZeroCost())):
            res = gv.g3_portfolios(hctx, f, spec, cost=cost)
            (net if label == "net" else gross).update(res)
        out["portfolios"] = {
            "P1_net": net["P1_avoid_losers"], "P2_net": net["P2_buffered_top100"],
            "P1_gross": gross["P1_avoid_losers"], "P2_gross": gross["P2_buffered_top100"],
        }
    return out


def gates(primary: dict) -> dict:
    g = SPEC["gates"]
    ic, tails, port = primary["ic"], primary["tails"], primary["portfolios"]
    h1 = ic["mean_ic"] > g["H1"]["mean_ic_gt"] and ic["nw_t"] >= g["H1"]["nw_t_ge"]
    h2 = tails["bottom_vigintile_bps"] < g["H2"]["v1_excess_bps_lt"] and tails["bottom_vigintile_bps"] < tails["top_vigintile_bps"]
    p1, p2n = port["P1_net"], port["P2_net"]
    h3 = (p1["t"] > g["H3"]["p1_net_excess_t_gt"]) and (p1["ann_excess_pct"] > p2n["ann_excess_pct"])
    return {"H1_signal_survival": bool(h1), "H2_bottom_tail": bool(h2), "H3_non_collapse": bool(h3),
            "bonus_p1_net_excess_positive": bool(p1["ann_excess_pct"] > 0),
            "verdict": "PASS" if (h1 and h2 and h3) else "FAIL"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-spec", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args(argv)
    gv.OUT.mkdir(parents=True, exist_ok=True)
    if a.write_spec:
        SPEC_PATH.write_text(json.dumps({**SPEC, "spec_hash": spec_hash(),
                                         "frozen_at": datetime.now(gv.TZ).isoformat()}, ensure_ascii=False, indent=2))
        print(f"wrote {SPEC_PATH} hash {spec_hash()}")
    if not a.run:
        return 0
    saved = json.loads(SPEC_PATH.read_text())
    if saved.get("spec_hash") != spec_hash():
        raise SystemExit("holdout spec differs from code: refusing")
    if not committed_and_clean(SPEC_PATH):
        raise SystemExit("holdout spec is not committed (or modified): refusing")
    if any(r.get("kind") == "holdout_result" and r.get("registration_id") == REG_ID for r in gv.registry_records()):
        raise SystemExit("holdout already evaluated for this registration: refusing (runs_allowed = 1)")
    gv._append({"kind": "holdout_unlock", "registration_id": REG_ID, "spec_hash": spec_hash(),
                "at": datetime.now(gv.TZ).isoformat(), "code_commit": gv.rp.git_commit()})
    ctx = gv.load_context(allow_holdout=True)
    p2.attach_inputs(ctx)
    ranks = p3.component_ranks(ctx)
    wide = pf.rows_to_wide(ctx, p3.comp_rank_ew6(ranks))
    styles = gv.baseline_styles(ctx)
    keep = (ctx.panel["trade_date"] >= gv.HOLDOUT_START).to_numpy()
    hctx = holdout_ctx(ctx)
    styles_h = styles[keep].reset_index(drop=True)
    primary = evaluate_h(hctx, wide, 5, styles_h, primary=True)
    secondary = evaluate_h(hctx, wide, 20, styles_h, primary=False)
    verdict = gates(primary)
    out = {"registration_id": REG_ID, "spec_hash": spec_hash(), "generated_at": datetime.now(gv.TZ).isoformat(),
           "panel_sha256": ctx.panel_sha, "labels_sha256": ctx.labels_sha, "gates": verdict,
           "primary_h5": primary, "secondary_h20_diagnostic": secondary}
    (gv.OUT / "holdout_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    gv._append({"kind": "holdout_result", "registration_id": REG_ID, "spec_hash": spec_hash(),
                "at": datetime.now(gv.TZ).isoformat(), "verdict": verdict["verdict"], "gates": verdict,
                "summary": {"ic": primary["ic"]["mean_ic"], "ic_t": primary["ic"]["nw_t"],
                            "v1": primary["tails"]["bottom_vigintile_bps"], "v20": primary["tails"]["top_vigintile_bps"],
                            "p1_ann": primary["portfolios"]["P1_net"]["ann_excess_pct"],
                            "p1_t": primary["portfolios"]["P1_net"]["t"]}})
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
