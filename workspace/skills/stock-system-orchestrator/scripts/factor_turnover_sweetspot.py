#!/usr/bin/env python3
"""换手甜点扫描（2026-06-28 优化④）：对清洁因子，找"持有几日"能让五分位多空【扣费后】转正。

动机：full_market_factor_eval 证明 O2C 的 g_intraday_vwap_deviation 等因子有真实毛多空(+30%/年)，
但日频换手 30bp/日成本(≈-73%/年)把它吃光→净负，这正是三策略整体负 IR 的根因。
若降换手(持有 H 日、每 H 日才调一次仓)，成本按 H 摊薄，毛收益若能持续到 H 日，净有望转正。

口径：入场=T+1 开盘，持有 H 个交易日后于该日收盘出场(每 H 日一个周期)。
  每周期 Q5-Q1 毛收益(市场中性) − 一次往返成本(COST_BPS) = 周期净；年化净 = 周期净 ×(244/H)。
  全市场点时面板(open/close/因子)，2300 只/日 × 1298 日，无重算前视。

用法：python3 factor_turnover_sweetspot.py --factor g_intraday_vwap_deviation [--since 20240101]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
HOLDS = [1, 2, 3, 5, 10, 20]
COST_BPS = 30          # 一次往返(进+出)双边成本，基点
COST_DEC = COST_BPS / 10000.0


def _calendar_fwd(df: pd.DataFrame, hold: int) -> pd.Series:
    """日历对齐前向收益(修复 shift 按行号 bug + 出场侧生存者偏差)：
    入场=T+1 日历日真实开盘(停牌则NaN排除,买不到);出场=T+H 日历日收盘,停牌/退市用 ffill 最后成交价(亏损计入,不丢弃)。"""
    cal = sorted(df["trade_date"].unique())
    ncal = len(cal)
    posmap = {d: i for i, d in enumerate(cal)}
    d = df.copy()
    d["pos"] = d["trade_date"].map(posmap).astype(int)
    nxt = d[["ts_code", "pos", "open"]].rename(columns={"open": "entry_open"})
    nxt = nxt.assign(pos=nxt["pos"] - 1)
    d = d.merge(nxt, on=["ts_code", "pos"], how="left").reset_index(drop=True)
    cff = (d.pivot_table(index="ts_code", columns="pos", values="close", aggfunc="last")
             .reindex(columns=range(ncal)).ffill(axis=1))
    code_row = {c: i for i, c in enumerate(cff.index)}
    cvals = cff.to_numpy()
    rows = d["ts_code"].map(code_row).to_numpy()
    ep = d["pos"].to_numpy() + hold
    valid = ep < ncal
    ec = np.full(len(d), np.nan)
    ec[valid] = cvals[rows[valid], ep[valid]]
    d["fwd"] = (ec - d["entry_open"].to_numpy(dtype=float)) / d["entry_open"].to_numpy(dtype=float)
    return d.set_index(["ts_code", "trade_date"])["fwd"]


def ls_for_hold(df: pd.DataFrame, fcol: str, hold: int) -> dict:
    """df 需含 ts_code/trade_date/open/close/<fcol>，已按 universe 过滤、按 (code,date) 升序。"""
    g = df.sort_values(["ts_code", "trade_date"]).copy()
    fwd = _calendar_fwd(g, hold).reset_index()
    g = g.merge(fwd, on=["ts_code", "trade_date"], how="left")
    g = g.dropna(subset=["fwd", fcol])
    # 每个"建仓日"算一次 Q5-Q1（市场中性）；为模拟每 H 日调一次仓，建仓日取 trade_date 每隔 H 个交易日。
    dates = sorted(g["trade_date"].unique())
    rebal = set(dates[::hold])  # 每 H 个交易日建一次仓
    ics, lss = [], []
    for d, day in g[g["trade_date"].isin(rebal)].groupby("trade_date"):
        sub = day[[fcol, "fwd"]].dropna()
        if len(sub) < 50:
            continue
        ic = sub[fcol].rank().corr(sub["fwd"].rank())
        if pd.notna(ic):
            ics.append(float(ic))
        try:
            b = pd.qcut(sub[fcol].rank(method="first"), 5, labels=False)
        except ValueError:
            continue
        top = sub.loc[b == 4, "fwd"].mean()
        bot = sub.loc[b == 0, "fwd"].mean()
        lss.append(float(top - bot))
    n = len(lss)
    if n < 4:
        return {"hold": hold, "cycles": n}
    gross = sum(lss) / n                      # 每周期(H日)毛多空(小数)
    net = gross - COST_DEC                    # 扣一次往返费
    ann_gross = gross * (244 / hold)
    ann_net = net * (244 / hold)
    ic_m = sum(ics) / len(ics) if ics else None
    return {"hold": hold, "cycles": n, "ic": round(ic_m, 4) if ic_m is not None else None,
            "gross_cycle_pct": round(gross * 100, 3), "net_cycle_pct": round(net * 100, 3),
            "ann_gross_pct": round(ann_gross * 100, 1), "ann_net_pct": round(ann_net * 100, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor", default="g_intraday_vwap_deviation")
    ap.add_argument("--since", default=None)
    args = ap.parse_args()
    cols = ["trade_date", "ts_code", "open", "close", args.factor]
    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    if args.factor not in names:
        print(f"factor not in panel: {args.factor}"); return 1
    if "universe_flag" in names:
        cols.append("universe_flag")
    df = pd.read_parquet(PANEL, columns=cols, engine="pyarrow")
    if "universe_flag" in df.columns:
        df = df[df["universe_flag"].fillna(0).astype(float) > 0]
    df["trade_date"] = df["trade_date"].astype(str)
    if args.since:
        df = df[df["trade_date"] >= args.since]
    print(f"因子={args.factor}  窗口 {df['trade_date'].min()}..{df['trade_date'].max()}  双边成本 {COST_BPS}bp/往返\n")
    print(f"{'持有(日)':>8}{'周期数':>7}{'IC':>9}{'毛/周期':>9}{'净/周期':>9}{'毛年化':>9}{'净年化':>9}  判读")
    best = None
    for h in HOLDS:
        r = ls_for_hold(df, args.factor, h)
        if r.get("cycles", 0) < 4:
            print(f"{h:>8}{r.get('cycles',0):>7}  样本不足"); continue
        v = "✅扣费后净正(可交易方向)" if r["ann_net_pct"] > 0 else "⚪扣费后仍负"
        if r["ann_net_pct"] > 0 and (best is None or r["ann_net_pct"] > best["ann_net_pct"]):
            best = r
        ic_s = f"{r['ic']:+.4f}" if r["ic"] is not None else "—"
        print(f"{h:>8}{r['cycles']:>7}{ic_s:>9}"
              f"{r['gross_cycle_pct']:>+8.3f}%{r['net_cycle_pct']:>+8.3f}%{r['ann_gross_pct']:>+8.1f}%{r['ann_net_pct']:>+8.1f}%  {v}")
    print()
    if best:
        print(f"→ 换手甜点：持有 {best['hold']} 日，扣费后净年化 {best['ann_net_pct']:+.1f}%（毛 {best['ann_gross_pct']:+.1f}%）。"
              f"即该因子在降换手到 {best['hold']} 日时方向上可交易。")
    else:
        print("→ 各持有期扣费后均为负：该因子毛收益不足以覆盖成本，需更强信号或更低成本。")
    print(f"口径：全市场五分位多空(市场中性)、入场T+1开盘、每{'/'.join(map(str,HOLDS))}日调仓、双边{COST_BPS}bp。仅毛收益线性外推假设,真实多日信号或衰减,需OOS确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
