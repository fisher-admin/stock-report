#!/usr/bin/env python3
"""Phase C 短线启动验证(2026-06-29):三段收益分解 + P0 行业滞后补涨(lead-lag)。

执行研究议程 short_term_launch_research_agenda_v1:
 (1) 三段分解:把次日收益拆 C2O(隔夜)/O2C(日内)/C2C,在【全域】与【涨停子集】上看edge落哪段、能否成交。
 (2) P0 lead-lag:行业龙头领涨·本股滞后补涨。lag_gap=同业amount加权收益(剔本股)−本股当日涨幅。
     测 lag_gap 对【可成交 O2C 段(T+1开盘买→收盘卖)】的预测力 + 行业中性消融 + 反向安慰剂。
 可成交口径:一字/顶死高开(次日C2O>+7%)剔为不可买;停牌/退市由面板前向收益已含。
面板已有 next_return_close_to_open_1d / open_to_close_1d / close_to_close_1d(三段,小数)。

用法:python3 phase_c_launch_validation.py [--cost-bps 30] [--top-frac 0.1]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
C2O, O2C, C2C = "next_return_close_to_open_1d", "next_return_open_to_close_1d", "next_return_close_to_close_1d"


def daily_ic(df, sig, ret):
    ics = []
    for _, g in df.groupby("trade_date"):
        s = g[[sig, ret]].dropna()
        if len(s) >= 50:
            ic = s[sig].rank().corr(s[ret].rank())
            if pd.notna(ic):
                ics.append(ic)
    if len(ics) < 10:
        return None
    m = np.mean(ics); sd = np.std(ics, ddof=1)
    return dict(ic=m, icir=m/sd if sd > 1e-12 else 0, t=(m/sd*len(ics)**0.5) if sd > 1e-12 else 0,
                pos=100*np.mean([x > 0 for x in ics]), n=len(ics))


def backtest_o2c(df, sig, cost, top_frac, fillable_only=True):
    """每日按 sig 选 top_frac,T+1开盘买→收盘卖(O2C),扣 cost。fillable_only:剔除次日高开>7%不可买。"""
    rets = []
    for dt, g in df.groupby("trade_date"):
        d = g.dropna(subset=[sig, O2C, C2O]).copy()
        if fillable_only:
            d = d[d[C2O] <= 0.07]  # 顶死高开/一字 不可在开盘买入
        if len(d) < 50:
            continue
        bench = d[O2C].mean()
        k = max(1, int(len(d) * top_frac))
        top = d.nlargest(k, sig)[O2C].mean()
        rets.append((top - bench) - cost)
    if len(rets) < 10:
        return None
    m = np.mean(rets); sd = np.std(rets, ddof=1)
    return dict(per_trade_net_pct=m*100, t=(m/(sd/np.sqrt(len(rets)))) if sd > 1e-12 else 0,
                hit=100*np.mean([r > 0 for r in rets]), ann_pct=m*244*100, n=len(rets))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=30.0)
    ap.add_argument("--top-frac", type=float, default=0.1)
    ap.add_argument("--since", default="20220101")
    args = ap.parse_args()
    cost = args.cost_bps / 10000.0

    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    need = ["trade_date", "ts_code", "industry", "pct_chg", "amount", "universe_flag", C2O, O2C, C2C]
    need = [c for c in need if c in names]
    df = pd.read_parquet(PANEL, columns=need, engine="pyarrow")
    df = df[df["universe_flag"].fillna(0).astype(float) > 0].drop(columns=["universe_flag"])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df[df["trade_date"] >= args.since]
    df["industry"] = df["industry"].fillna("NA")
    print(f"面板 {df['trade_date'].min()}..{df['trade_date'].max()} {df['trade_date'].nunique()}日 {len(df):,}行 | pct_chg范围[{df['pct_chg'].min():.1f},{df['pct_chg'].max():.1f}]")
    # 单位:pct_chg为百分数,next_return_*为小数
    print(f"次日三段(小数)均值: C2O隔夜{df[C2O].mean()*100:+.3f}% O2C日内{df[O2C].mean()*100:+.3f}% C2C{df[C2C].mean()*100:+.3f}%\n")

    # ===== (1) 三段分解:全域 vs 涨停子集 =====
    print("=== 三段收益分解(次日,均值%) ===")
    print(f"  {'子集':<22}{'样本':>9}{'C2O隔夜':>10}{'O2C日内':>10}{'C2C全天':>10}")
    def seg(name, sub):
        if len(sub) < 50: return
        print(f"  {name:<22}{len(sub):>9,}{sub[C2O].mean()*100:>+9.3f}%{sub[O2C].mean()*100:>+9.3f}%{sub[C2C].mean()*100:>+9.3f}%")
    seg("全域", df)
    seg("涨停(pct_chg≥9.5)", df[df["pct_chg"] >= 9.5])
    seg("大涨(7≤pct<9.5)", df[(df["pct_chg"] >= 7) & (df["pct_chg"] < 9.5)])
    seg("下跌(pct_chg<0)", df[df["pct_chg"] < 0])
    print("  判读:涨停子集若C2O大正、O2C≈0或负→edge在不可成交的隔夜跳空(同评委实测)。\n")

    # ===== (2) P0 lead-lag =====
    # 同业 amount 加权收益(剔本股):sum(pct*amt)-own / (sum(amt)-own_amt)
    g = df.groupby(["trade_date", "industry"])
    df["_pa"] = df["pct_chg"] * df["amount"]
    sum_pa = g["_pa"].transform("sum")
    sum_a = g["amount"].transform("sum")
    cnt = g["amount"].transform("count")
    ind_ret_exself = (sum_pa - df["_pa"]) / (sum_a - df["amount"]).replace(0, np.nan)
    df["ind_ret_d"] = ind_ret_exself
    df["lag_gap"] = df["ind_ret_d"] - df["pct_chg"]      # >0: 同业涨,本股没跟 → 赌补涨
    # 同业次日各段(剔本股均值)→ 行业中性forward
    for seg_col, nm in [(O2C, "ind_o2c"), (C2O, "ind_c2o")]:
        s_sum = g[seg_col].transform("sum")
        df[nm] = (s_sum - df[seg_col]) / (cnt - 1).replace(0, np.nan)
    df["excess_o2c"] = df[O2C] - df["ind_o2c"]           # 超越同业的日内补涨(行业中性)

    # 只在 lag_gap>0(同业涨本股没跟)且本股当日未涨停(<7,留补涨空间)激活
    act = df[(df["lag_gap"] > 0) & (df["pct_chg"] < 7)].copy()
    print(f"=== P0 lead-lag 行业滞后补涨(激活子集:lag_gap>0且本股当日pct<7) 样本 {len(act):,} ===")

    print("\n  [A] lag_gap 对次日三段的全市场IC(看补涨落哪段):")
    for ret, lab in [(C2O, "C2O隔夜"), (O2C, "O2C日内·可成交"), (C2C, "C2C全天")]:
        r = daily_ic(act, "lag_gap", ret)
        if r:
            print(f"     vs {lab:<14} IC {r['ic']:+.4f}  t {r['t']:+.2f}  %正 {r['pos']:.0f}%  日 {r['n']}")

    print("\n  [B] 行业中性消融(lag_gap 对'超越同业的O2C'):")
    r = daily_ic(act, "lag_gap", "excess_o2c")
    if r:
        print(f"     vs O2C超额(行业中性) IC {r['ic']:+.4f}  t {r['t']:+.2f}  %正 {r['pos']:.0f}%")
    print("     (若中性后IC明显塌缩→lag_gap主要吃beta/普涨,非真补涨alpha)")

    print(f"\n  [C] 可成交回测(T+1开盘买→收盘卖O2C,剔高开>7%,扣{args.cost_bps}bp,日度top{args.top_frac:.0%}):")
    for sig, lab in [("lag_gap", "真实 lag_gap")]:
        r = backtest_o2c(act, sig, cost, args.top_frac)
        if r:
            print(f"     {lab:<16} 每笔净 {r['per_trade_net_pct']:+.3f}%  t {r['t']:+.2f}  命中 {r['hit']:.0f}%  年化 {r['ann_pct']:+.1f}%  笔 {r['n']}")
    # 安慰剂:反向 lag_gap(追涨:本股涨过同业)
    act["neg_lag"] = -act["lag_gap"]
    r = backtest_o2c(act, "neg_lag", cost, args.top_frac)
    if r:
        print(f"     {'安慰剂:反向(追涨)':<16} 每笔净 {r['per_trade_net_pct']:+.3f}%  t {r['t']:+.2f}  命中 {r['hit']:.0f}%  (应明显更差)")
    # 安慰剂:随机
    rng = np.random.RandomState(20260629)
    act["rnd"] = rng.permutation(act["lag_gap"].values)
    r = backtest_o2c(act, "rnd", cost, args.top_frac)
    if r:
        print(f"     {'安慰剂:随机':<16} 每笔净 {r['per_trade_net_pct']:+.3f}%  t {r['t']:+.2f}  命中 {r['hit']:.0f}%")

    print(f"\n口径:全市场点时面板、可成交T+1开盘O2C、剔高开>7%、扣{args.cost_bps}bp、基准=可成交股O2C均值。"
          f"\n判定:仅当真实lag_gap每笔净>0且t>2、行业中性后IC不塌、反向安慰剂明显更差,才算P0未被证伪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
