#!/usr/bin/env python3
"""Phase B 因子集组合回测(2026-06-29):把多个【公告日PIT】事件因子合成因子集,测组合净边际。

通用引擎:每个因子=(parquet, 信号列, 先验方向)。每月调仓日 T,每股取各因子公告日∈(T-lookback,T)的
最近信号→行业中性z×先验方向。composite=每股可得因子z的均值(允许部分缺失)。
报告:逐因子 IC + composite IC + composite 长多 top decile 净超额(扣费)+ 安慰剂。
方向先验固定不挑(守纪律,即便单因子样本内略负也不翻号)。

用法:python3 phase_b_factorset_backtest.py [--lookback 120] [--top-frac 0.1] [--hold 20] [--cost-bps 30]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
FCACHE = WORKSPACE / "stock_data" / "03-working" / "fundamental_cache"
TRADING_DAYS = 244

# 因子集:(名称, parquet, 信号列, 先验方向)。方向按经济学先验固定。
FACTORS = [
    ("PEAD盈余漂移", FCACHE / "pead_preannounce.parquet", "growth_pct", +1),   # 同比越高越好
    ("户数集中",     FCACHE / "holder_count.parquet",      "holder_chg_pct", -1),  # 户数减少=集中=看多
]


def _add_calendar_fwd(df: pd.DataFrame, hold: int) -> pd.DataFrame:
    cal = sorted(df["trade_date"].unique())
    ncal = len(cal)
    posmap = {d: i for i, d in enumerate(cal)}
    df = df.copy()
    df["pos"] = df["trade_date"].map(posmap).astype(int)
    nxt = df[["ts_code", "pos", "open"]].rename(columns={"open": "entry_open"})
    nxt = nxt.assign(pos=nxt["pos"] - 1)
    df = df.merge(nxt, on=["ts_code", "pos"], how="left").reset_index(drop=True)
    cff = (df.pivot_table(index="ts_code", columns="pos", values="close", aggfunc="last")
             .reindex(columns=range(ncal)).ffill(axis=1))
    code_row = {c: i for i, c in enumerate(cff.index)}
    cvals = cff.to_numpy()
    rows = df["ts_code"].map(code_row).to_numpy()
    ep = df["pos"].to_numpy() + hold
    valid = ep < ncal
    ec = np.full(len(df), np.nan)
    ec[valid] = cvals[rows[valid], ep[valid]]
    df["fwd"] = (ec - df["entry_open"].to_numpy(dtype=float)) / df["entry_open"].to_numpy(dtype=float)
    return df


def ind_z(sub: pd.DataFrame, col: str, sign: int) -> pd.Series:
    x = sub[col].astype(float)
    lo, hi = x.quantile(0.01), x.quantile(0.99)
    x = x.clip(lo, hi)
    x = x - sub.assign(_x=x).groupby("industry")["_x"].transform("mean")
    sd = x.std()
    z = (x - x.mean()) / sd if sd and sd > 1e-12 else x * 0.0
    return z * sign


def build_day(day: pd.DataFrame, facs, T, lo, min_stocks: int):
    """给单日 day(已 reset_index)叠加各因子行业中性z + composite。位置安全。返回 (day, zcol名单)。"""
    day = day.reset_index(drop=True)
    zcols = []
    for nm, d, col, sign in facs:
        win = d[(d["ann_dt"] < T) & (d["ann_dt"] >= lo)]
        if len(win) == 0:
            continue
        sig = win.sort_values("ann_dt").drop_duplicates("ts_code", keep="last")[["ts_code", col]]
        sub = day.merge(sig.rename(columns={"ts_code": "code6"}), on="code6", how="left")  # 同序同长(右键唯一)
        valid = sub.index[sub[col].notna()]
        if len(valid) < min_stocks:
            continue
        zfull = pd.Series(np.nan, index=sub.index)
        zfull.loc[valid] = ind_z(sub.loc[valid], col, sign)
        day[nm + "_z"] = zfull.values
        zcols.append(nm + "_z")
    if zcols:
        day["_comp"] = day[zcols].mean(axis=1)
    return day, zcols


def stats(ics, rets, cpy):
    n = len(ics); m = np.mean(ics); sd = np.std(ics, ddof=1) if n > 1 else 0
    icir = m / sd if sd > 1e-12 else 0; t = icir * (n ** 0.5)
    pos = 100.0 * np.mean([x > 0 for x in ics]) if ics else 0
    mr = np.mean(rets); sdr = np.std(rets, ddof=1) if len(rets) > 1 else 0
    ann = mr * cpy * 100; sharpe = (mr / sdr * cpy ** 0.5) if sdr > 1e-12 else 0
    tret = mr / (sdr / np.sqrt(len(rets))) if sdr > 1e-12 else 0
    hit = 100.0 * np.mean([r > 0 for r in rets])
    return dict(ic=m, icir=icir, t=t, pos=pos, ann=ann, sharpe=sharpe, tret=tret, hit=hit, n=len(rets))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=120)
    ap.add_argument("--top-frac", type=float, default=0.1)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=30.0)
    ap.add_argument("--min-stocks", type=int, default=40)
    args = ap.parse_args()
    cost = args.cost_bps / 10000.0
    H = args.hold
    cpy = TRADING_DAYS / H

    facs = []
    for name, path, col, sign in FACTORS:
        if not path.exists():
            print(f"⚠️  跳过 {name}: 缺 {path.name}"); continue
        d = pd.read_parquet(path).dropna(subset=[col, "ann_date"])
        d["ann_dt"] = pd.to_datetime(d["ann_date"], format="%Y%m%d", errors="coerce")
        d = d.dropna(subset=["ann_dt"])
        facs.append((name, d, col, sign))
        print(f"{name}: {len(d)} 条 {d['ann_date'].min()}..{d['ann_date'].max()} 唯一股 {d['ts_code'].nunique()}")
    if not facs:
        print("无因子数据"); return 1

    cols = ["trade_date", "ts_code", "industry", "open", "close", "universe_flag"]
    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    cols = [c for c in cols if c in names]
    df = pd.read_parquet(PANEL, columns=cols, engine="pyarrow")
    df = df[df["universe_flag"].fillna(0).astype(float) > 0].drop(columns=["universe_flag"])
    df["trade_date"] = df["trade_date"].astype(str)
    df["industry"] = df["industry"].fillna("NA")
    df["code6"] = df["ts_code"].str.extract(r"(\d{6})")[0]
    df = df.dropna(subset=["code6"])
    df = _add_calendar_fwd(df, H)
    dates = sorted(df["trade_date"].unique())
    rebal = dates[::H]
    print(f"面板 {dates[0]}..{dates[-1]} 月度 {len(rebal)}期 lookback={args.lookback} top{args.top_frac:.0%} 成本{args.cost_bps}bp\n")

    per_ic = {nm: [] for nm, _, _, _ in facs}
    comp_ic, comp_ret, rebal_used = [], [], []
    for dt in rebal:
        T = pd.to_datetime(dt, format="%Y%m%d"); lo = T - pd.Timedelta(days=args.lookback)
        day0 = df[df["trade_date"] == dt][["code6", "industry", "fwd"]].dropna(subset=["fwd"]).copy()
        if len(day0) < args.min_stocks:
            continue
        day, zcols = build_day(day0, facs, T, lo, args.min_stocks)
        if not zcols:
            continue
        # 逐因子 IC(仅该因子有信号的股)
        for zc in zcols:
            nm = zc[:-2]
            ssub = day[[zc, "fwd"]].dropna()
            if len(ssub) >= args.min_stocks:
                ic = ssub[zc].rank().corr(ssub["fwd"].rank())
                if pd.notna(ic):
                    per_ic[nm].append(ic)
        cs = day.dropna(subset=["_comp", "fwd"])
        if len(cs) < args.min_stocks:
            continue
        ic = cs["_comp"].rank().corr(cs["fwd"].rank())
        if pd.notna(ic):
            comp_ic.append(ic)
        bench = cs["fwd"].mean()
        k = max(1, int(len(cs) * args.top_frac))
        comp_ret.append((cs.nlargest(k, "_comp")["fwd"].mean() - bench) - cost)
        rebal_used.append(dt)

    print("=== 逐因子 月度 rank IC(先验方向已对齐) ===")
    for nm in per_ic:
        ics = per_ic[nm]
        if len(ics) < 5:
            print(f"  {nm:<12} 样本不足({len(ics)})"); continue
        m = np.mean(ics); sd = np.std(ics, ddof=1); icir = m / sd if sd > 1e-12 else 0
        print(f"  {nm:<12} IC {m:+.4f}  ICIR {icir:+.3f}  t {icir*len(ics)**0.5:+.2f}  %正 {100*np.mean([x>0 for x in ics]):.0f}%  期 {len(ics)}")

    if len(comp_ret) >= 4:
        s = stats(comp_ic, comp_ret, cpy)
        print(f"\n=== 因子集 composite 长多 top{args.top_frac:.0%} 净超额(月度,扣{args.cost_bps}bp) ===")
        print(f"  composite IC {s['ic']:+.4f} (t{s['t']:+.2f})  净超额年化 {s['ann']:+.2f}%  Sharpe {s['sharpe']:+.2f}  周期t {s['tret']:+.2f}  命中 {s['hit']:.0f}%  周期 {s['n']}")
        # 安慰剂
        rng = np.random.RandomState(20260629)
        plac = []
        for _ in range(20):
            pr = []
            for dt in rebal_used:
                T = pd.to_datetime(dt, format="%Y%m%d"); lo = T - pd.Timedelta(days=args.lookback)
                day0 = df[df["trade_date"] == dt][["code6", "industry", "fwd"]].dropna(subset=["fwd"]).copy()
                if len(day0) < args.min_stocks:
                    continue
                day, zcols = build_day(day0, facs, T, lo, args.min_stocks)
                if not zcols:
                    continue
                day["_comp"] = rng.permutation(day["_comp"].values)
                cs = day.dropna(subset=["_comp", "fwd"])
                if len(cs) < args.min_stocks:
                    continue
                k = max(1, int(len(cs) * args.top_frac))
                pr.append((cs.nlargest(k, "_comp")["fwd"].mean() - cs["fwd"].mean()) - cost)
            if len(pr) >= 4:
                plac.append(np.mean(pr) * cpy * 100)
        if plac:
            pm, psd = np.mean(plac), np.std(plac); z = (s["ann"] - pm) / psd if psd > 1e-9 else 0
            print(f"  安慰剂: 随机 {pm:+.2f}%±{psd:.2f} | 真实 {s['ann']:+.2f}% → {z:+.1f}σ  {'✅显著优于随机' if z>2 else '⚪与随机不可区分'}")

    print(f"\n口径:公告日严格PIT、日历对齐月度前向收益(去生存者偏差)、行业中性z、先验方向固定不挑、扣{args.cost_bps}bp、基准=有信号股均值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
