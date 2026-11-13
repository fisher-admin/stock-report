#!/usr/bin/env python3
"""多重检验审计：gauntlet 注册表的 BH-FDR 稳健性 + 前瞻账本的 Deflated Sharpe。

gauntlet_v2 已对注册表内全部 research 检验做 BH-FDR（q=0.10）。本审计不改动任何冻结口径，只回答三个问题：
  1. 通过的因子在更严的校正（Holm / Bonferroni）下还活着吗？
  2. 它们能扛多大的"隐藏试验数"？  breakeven_family = floor(rank · q / p)：把族扩到这个规模之前仍通过 BH
     （假设新增检验的 p 都不优于它；超过该规模即应怀疑）。
  3. 前瞻影子账本（books）的逐期超额收益，在扣除"试了这么多次"之后还显著吗？  Deflated Sharpe（Bailey–López de Prado）
     需 ≥ 10 个已结算期才计算，试验数取注册表 research 族大小 + --extra-trials。

另报：前瞻判据 "两本账本任一 t ≥ 2.0" 相当于 2 检验 Bonferroni 的单侧 α ≈ 0.0455，已足够严，无需上调。

  python multiple_testing_audit.py [--extra-trials N]   # 写 outputs/gauntlet_v2/multiple_testing_audit.{json,md}
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import NormalDist

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "outputs/gauntlet_v2/registry.jsonl"
SETTLED = ROOT / "data/forward_shadow/settled.jsonl"
OUT = ROOT / "outputs/gauntlet_v2"
Q = 0.10
N01 = NormalDist()
EULER = 0.5772156649015329


def adjust(pvals: dict[str, float]) -> dict[str, dict]:
    """BH q-values (monotone), Holm and Bonferroni adjusted p, and BH breakeven family size per test."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    q = [min(1.0, p * m / (i + 1)) for i, (_, p) in enumerate(items)]
    for i in range(m - 2, -1, -1):
        q[i] = min(q[i], q[i + 1])
    holm, run = [], 0.0
    for i, (_, p) in enumerate(items):
        run = max(run, min(1.0, p * (m - i)))
        holm.append(run)
    out = {}
    for i, (k, p) in enumerate(items):
        rank = sum(1 for _, pp in items if pp <= p)
        out[k] = {"p": p, "bh_q": q[i], "holm_p": holm[i], "bonferroni_p": min(1.0, p * m),
                  "breakeven_family": None if p <= 0 else int(math.floor(rank * Q / p))}
    return out


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, trials: int, sr_var: float | None = None) -> float | None:
    """Probability that the true Sharpe exceeds the best-of-`trials` null expectation (per-period SR, not annualized)."""
    if n < 3 or trials < 1:
        return None
    var = sr_var if sr_var is not None else 1.0 / (n - 1)
    if trials == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var) * ((1 - EULER) * N01.inv_cdf(1 - 1.0 / trials) + EULER * N01.inv_cdf(1 - 1.0 / (trials * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    if denom <= 0:
        return None
    return float(N01.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)))


def _moments(x: np.ndarray) -> tuple[float, float]:
    s = x.std(ddof=0)
    if s == 0:
        return 0.0, 3.0
    z = (x - x.mean()) / s
    return float((z ** 3).mean()), float((z ** 4).mean())


def registry_audit(extra: int) -> dict:
    pre, res = {}, {}
    for ln in REGISTRY.read_text().splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("kind") == "preregistration":
            pre[r["registration_id"]] = r.get("spec", {})
        elif r.get("kind") == "result" and r.get("role") == "research" and "p_value" in r:
            res[r["registration_id"]] = r
    pv = {rid: float(r["p_value"]) for rid, r in res.items()}
    adj = adjust(pv)
    rows = []
    for rid, a in sorted(adj.items(), key=lambda kv: kv[1]["p"]):
        spec = pre.get(rid, {})
        rows.append({"registration_id": rid, "factor_id": spec.get("factor_id"), "horizon": spec.get("horizon"),
                     "verdict": res[rid].get("verdict"), **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in a.items()},
                     "survives_bh": a["bh_q"] <= Q, "survives_holm": a["holm_p"] <= Q,
                     "survives_bonferroni": a["bonferroni_p"] <= Q,
                     "survives_with_extra_trials": (a["breakeven_family"] is None or a["breakeven_family"] >= len(pv) + extra)})
    return {"family_size": len(pv), "extra_trials_assumed": extra, "q": Q, "tests": rows,
            "survivors_bh": [r["factor_id"] or r["registration_id"] for r in rows if r["survives_bh"]]}


def books_dsr(trials: int) -> dict:
    if not SETTLED.exists():
        return {"status": "no_settled_periods"}
    recs = [json.loads(x) for x in SETTLED.read_text().splitlines() if x.strip()]
    out = {}
    for book in sorted({r["book"] for r in recs}):
        ex = np.array([r["net_excess"] for r in recs if r["book"] == book], dtype=float)
        if len(ex) < 10 or ex.std(ddof=1) == 0:
            out[book] = {"periods": int(len(ex)), "status": "insufficient (<10 periods)"}
            continue
        sr = float(ex.mean() / ex.std(ddof=1))
        sk, ku = _moments(ex)
        out[book] = {"periods": int(len(ex)), "sr_per_period": round(sr, 4), "trials": trials,
                     "dsr_prob": deflated_sharpe(sr, len(ex), sk, ku, trials)}
    return out


def _be(v) -> str:
    return "∞（p 下溢）" if v is None or v > 10**6 else str(v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra-trials", type=int, default=0, help="additional untracked trials to assume in the family")
    a = ap.parse_args(argv)
    reg = registry_audit(a.extra_trials)
    doc = {"generated_at": datetime.now().astimezone().isoformat(), "registry": reg,
           "forward_books_dsr": books_dsr(reg["family_size"] + a.extra_trials),
           "forward_criterion_note": "前瞻判据：两本账本任一 NW t ≥ 2.0 ⇒ 2 检验 Bonferroni 单侧 α ≈ 0.0455，足够严"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "multiple_testing_audit.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str))
    lines = [f"# 多重检验审计（{doc['generated_at'][:16]}）", "",
             f"注册表 research 族大小 {reg['family_size']}（另设额外试验 {a.extra_trials}），BH q = {Q}。", "",
             "| 因子 | h | 判定 | p | BH q | Holm p | Bonferroni p | BH 存活 | 可扛族规模 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in reg["tests"]:
        if r["survives_bh"] or r["bh_q"] < 0.5:
            lines.append(f"| {r['factor_id']} | {r['horizon']} | {r['verdict']} | {r['p']:.2e} | {r['bh_q']:.3g} | "
                         f"{r['holm_p']:.3g} | {r['bonferroni_p']:.3g} | {'是' if r['survives_bh'] else '否'} | "
                         f"{_be(r['breakeven_family'])} |")
    n_surv = len(reg["survivors_bh"])
    lines += ["", f"**结论：** {n_surv}/{reg['family_size']} 个检验在 BH 下存活，且多数 p 值下溢到 0——IC 层面的多重检验校正"
              "基本不构成约束；真正起筛选作用的是 g3/g4 闸门（残差 IC、组合净超额）与 holdout。FDR 通过不代表因子有独立 alpha。"]
    lines += ["", f"前瞻账本 Deflated Sharpe：{json.dumps(doc['forward_books_dsr'], ensure_ascii=False)}", "", doc["forward_criterion_note"]]
    (OUT / "multiple_testing_audit.md").write_text("\n".join(lines) + "\n")
    print(f"multiple_testing_audit: family={reg['family_size']} BH 存活 {len(reg['survivors_bh'])} 个: {', '.join(map(str, reg['survivors_bh']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
