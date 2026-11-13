#!/usr/bin/env python3
"""全市场因子评估引擎（2026-06-28 优化③）：在【全可投域】上算每个因子的真实选股力。

对抗复核(wf_99eace9d)指出：之前的「选中集内 IC」(top20 内部)是陷阱，必须用全市场横截面 IC + 分位净收益。
本引擎直接读 factor_factory 的全市场点时面板 greenfield_multifactor_panel.parquet
（2300 只/日 × 1298 日 × 因子 + 已算好的前向收益 next_return_*），因此：
  - 全市场横截面，非 top20 选中集 → 解决截断/collider 偏差。
  - 大样本(数十万股·日) → 解决小样本不显著。
  - 面板因子=系统点时计算值、前向收益=已实现标签 → 无重算复权前视（日内因子尤其干净）。

每因子输出：
  - 全市场 Rank IC（每日 Spearman(因子, 前向收益)）、ICIR=mean/std、t≈ICIR·√n、%正日。
  - 五分位多空：每日按因子分5档，Q5−Q1 前向收益差（市场中性，无需基准）；年化 + 扣双边费用净值。
  - FDR(Benjamini-Hochberg) 多重比较校正后是否显著。
诚实披露：horizon=1 日则无重叠；多日 horizon 需 Newey-West（本版对 1 日 horizon 无需）。

用法：python3 full_market_factor_eval.py [--return-col next_return_open_to_close_1d] [--since 20250101] [--factors a,b,c]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
# O2C 当前在用的 6 因子（最该先验证的）。
O2C_FACTORS = [
    "g_intraday_range_expansion", "g_intraday_vwap_deviation", "g_close_strength_ratio",
    "g_chip_pullback_support", "g_long_cost_concentration", "g_volume_price_divergence",
]
COST_BPS = 30  # 五分位多空双边交易成本(基点)，A股 T+1 开盘进/出，含佣金印花滑点的保守估计


def quintile_long_short(g: pd.DataFrame, fcol: str, rcol: str, q: int = 5) -> float | None:
    sub = g[[fcol, rcol]].dropna()
    if len(sub) < q * 4:
        return None
    try:
        sub = sub.assign(_b=pd.qcut(sub[fcol].rank(method="first"), q, labels=False))
    except ValueError:
        return None
    top = sub.loc[sub["_b"] == q - 1, rcol].mean()
    bot = sub.loc[sub["_b"] == 0, rcol].mean()
    return float(top - bot)


def fdr_bh(pvals: list[float], alpha: float = 0.05) -> list[bool]:
    idx = sorted(range(len(pvals)), key=lambda i: pvals[i])
    m = len(pvals)
    passed = [False] * m
    thresh = 0
    for rank, i in enumerate(idx, start=1):
        if pvals[i] <= rank / m * alpha:
            thresh = rank
    for rank, i in enumerate(idx, start=1):
        if rank <= thresh:
            passed[i] = True
    return passed


def _norm_cdf(x: float) -> float:
    import math
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--return-col", default="next_return_open_to_close_1d", help="前向收益列(O2C日内默认 T+1开→收)")
    ap.add_argument("--since", default=None, help="只评估 >= 该日(yyyymmdd)")
    ap.add_argument("--factors", default=None, help="逗号分隔的因子列；默认 O2C 6 因子")
    ap.add_argument("--all-g", action="store_true", help="评估面板里全部 g_ 因子(不含 __z)")
    args = ap.parse_args()
    if not PANEL.exists():
        print(f"panel not found: {PANEL}"); return 1

    # 用 pyarrow schema 拿全列名（避免读全表）
    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    if args.factors:
        factors = [f for f in args.factors.split(",") if f in names]
    elif args.all_g:
        factors = [c for c in names if c.startswith("g_") and not c.endswith("__z")]
    else:
        factors = [f for f in O2C_FACTORS if f in names]
    if not factors:
        print("no valid factor columns"); return 1
    rcol = args.return_col
    need = ["trade_date", "ts_code", rcol] + factors
    if "universe_flag" in names:
        need.append("universe_flag")
    print(f"panel={PANEL.name} factors={len(factors)} return={rcol} reading {len(need)} cols ...")
    df = pd.read_parquet(PANEL, engine="pyarrow", columns=need)
    if "universe_flag" in df.columns:
        df = df[df["universe_flag"].fillna(0).astype(float) > 0]
    df = df.dropna(subset=[rcol])
    df["trade_date"] = df["trade_date"].astype(str)
    if args.since:
        df = df[df["trade_date"] >= args.since]
    ndays = df["trade_date"].nunique()
    print(f"评估窗口：{df['trade_date'].min()}..{df['trade_date'].max()}  {ndays} 日  全市场样本 {len(df):,} 股·日\n")

    rows = []
    for f in factors:
        ics, lss = [], []
        for _, g in df.groupby("trade_date"):
            sub = g[[f, rcol]].dropna()
            if len(sub) < 30:
                continue
            ic = sub[f].rank().corr(sub[rcol].rank())  # Spearman = Pearson on ranks
            if pd.notna(ic):
                ics.append(float(ic))
            ls = quintile_long_short(g, f, rcol)
            if ls is not None:
                lss.append(ls)
        n = len(ics)
        if n < 5:
            continue
        m = sum(ics) / n
        sd = (sum((x - m) ** 2 for x in ics) / (n - 1)) ** 0.5
        icir = m / sd if sd > 1e-12 else 0.0
        t = icir * (n ** 0.5)
        import math
        pval = 2 * (1 - _norm_cdf(abs(t)))
        pos = 100.0 * sum(1 for x in ics if x > 0) / n
        # 前向收益列为【小数】(0.018=1.8%)。成本 30bp=0.003 小数。统一在小数域算，展示再×100 成 %。
        cost_dec = COST_BPS / 10000.0
        ls_mean = (sum(lss) / len(lss)) if lss else None       # 每日 Q5-Q1 多空收益(小数)
        ls_net = (ls_mean - cost_dec) if ls_mean is not None else None  # 扣单日双边费后(小数)
        ls_ann = (ls_mean * 244) if ls_mean is not None else None       # 毛年化(小数, 日频满换手)
        rows.append({"factor": f, "n": n, "ic": round(m, 4), "icir": round(icir, 3),
                     "t": round(t, 2), "p": pval, "pos": round(pos, 1),
                     "ls_day": round(ls_mean * 100, 4) if ls_mean is not None else None,         # %
                     "ls_net_day": round(ls_net * 100, 4) if ls_net is not None else None,       # %
                     "ls_ann": round(ls_ann * 100, 2) if ls_ann is not None else None})          # %
    if not rows:
        print("无足够样本"); return 1
    passed = fdr_bh([r["p"] for r in rows])
    for r, ok in zip(rows, passed):
        r["fdr_sig"] = ok
    rows.sort(key=lambda r: r["ic"], reverse=True)
    print(f"{'因子':<30}{'日数':>5}{'IC':>9}{'ICIR':>7}{'t':>6}{'%正':>5}{'多空/日':>9}{'毛年化':>9}{'扣费净/日':>10}{'FDR':>5}  判读")
    for r in rows:
        verdict = "✅毛有效·但日频扣费亏" if (r["fdr_sig"] and r["ic"] > 0.015 and r["ls_day"] and r["ls_day"] > 0 and (r["ls_net_day"] or 0) <= 0) else \
                  ("✅真·选股力(扣费仍赚)" if (r["fdr_sig"] and r["ls_net_day"] and r["ls_net_day"] > 0) else \
                  ("🔴真·反向" if (r["fdr_sig"] and r["ic"] < -0.015) else "⚪噪声/毛收益≈0"))
        ls_s = f"{r['ls_day']:+.3f}%" if r["ls_day"] is not None else "—"
        ann_s = f"{r['ls_ann']:+.1f}%" if r["ls_ann"] is not None else "—"
        net_s = f"{r['ls_net_day']:+.3f}%" if r["ls_net_day"] is not None else "—"
        sig_s = "是" if r["fdr_sig"] else "否"
        print(f"  {r['factor']:<28}{r['n']:>5}{r['ic']:>+9.4f}{r['icir']:>+7.3f}{r['t']:>+6.2f}{r['pos']:>4.0f}%"
              f"{ls_s:>9}{ann_s:>9}{net_s:>10}{sig_s:>5}  {verdict}")
    print(f"\n口径：全市场横截面 Rank IC(非选中集内)；Q5-Q1=五分位多空每日收益(市场中性)；扣费按双边 {COST_BPS}bp；"
          f"FDR(BH α=0.05)多重比较校正；horizon={rcol}。")
    print("判读：✅=FDR显著+正IC+扣费后多空仍赚→真有选股力可加权；🔴=FDR显著反向→应砍/反号；⚪=无效或扣费后不赚→噪声。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
