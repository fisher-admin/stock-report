#!/usr/bin/env python3
"""Render outputs/gauntlet_v2/phase2_batch<N>_results.json as a Markdown summary table."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/gauntlet_v2"


def f(x, nd=3, sign=True):
    if x is None:
        return "–"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def render(n: int) -> str:
    r = json.loads((OUT / f"phase2_batch{n}_results.json").read_text())
    lines = [f"# Phase 2 — Batch {n} results", "",
             f"Generated {r['generated_at']} · panel `{r['panel_sha256'][:12]}` · labels `{r['labels_sha256'][:12]}` · "
             f"holdout ≥20260701 locked · BH-FDR q=0.10 over {r['fdr_family_size']} research tests in the registry.", "",
             "IC = mean daily rank IC vs h-day forward excess (direction-adjusted), NW t in parentheses. "
             "Tails = mean excess of vigintile 1 / 20 (bp per holding period). "
             "P1 = avoid-the-losers (drop bottom 30%, EW); P2 = buffered top-100; both net of costs vs the investable EW "
             "benchmark with the same rebalance (%/yr, NW t).", "",
             "| Factor | h | Dir | Discovery IC (t) | Validation IC (t) | Residual IC (t) | V1 / V20 bp | P1 %/yr (t) | P2 %/yr (t) | Placebo σ | FDR | Verdict | Failed gates |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in r["rows"]:
        name = x["factor"].removeprefix("p2_").rsplit("_h", 1)[0]
        lines.append(
            f"| {name} | {x['h']} | {x['dir']:+d} | {f(x['disc_ic'])} ({f(x['disc_t'], 2)}) | {f(x['val_ic'])} ({f(x['val_t'], 2)}) | "
            f"{f(x['resid_ic'])} ({f(x['resid_t'], 2)}) | {f(x['bottom_bps'], 1)} / {f(x['top_bps'], 1)} | "
            f"{f(x['p1_ann'], 2)} ({f(x['p1_t'], 2)}) | {f(x['p2_ann'], 2)} ({f(x['p2_t'], 2)}) | "
            f"{f(x['placebo_sigma'], 1)} | {'pass' if x['fdr_pass'] else 'fail'} | **{x['final_verdict']}** | "
            f"{', '.join(g.replace('g3_portfolios', 'portfolio').replace('g1_ic', 'IC').replace('g4_residual', 'residual').replace('g5_placebo', 'placebo') for g in x['failed_gates']) or '–'} |")
    passed = [x["factor"] for x in r["rows"] if x["final_verdict"] == "PASS"]
    lines += ["", f"**Passed all gates + FDR:** {', '.join(passed) if passed else 'none'}"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    n = int(sys.argv[1])
    md = render(n)
    (OUT / f"PHASE2_BATCH{n}.md").write_text(md, encoding="utf-8")
    print(md)
