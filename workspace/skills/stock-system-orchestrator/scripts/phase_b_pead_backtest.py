#!/usr/bin/env python3
"""Phase B 旗舰:PEAD(业绩预告盈余漂移)因子回测(2026-06-29)。

检验设计真正押注的【正交基本面 alpha】:盈余预告意外(净利润同比)能否在 A股长多+成本下净正——
这是全球最稳健异象之一,且本机数据带【公告日期】→严格 PIT,无前视。

口径(扣住全部教训):
 - 信号=业绩预告净利润同比 growth_pct,按【公告日期】对齐:每月调仓日 T,每股取其
   公告日 ∈ (T-LOOKBACK日, T-1日] 的最近一次预告(事件后漂移窗口),信号严格已知、无前视。
 - 横截面 winsorize(1/99)→行业内 demean→z-score(方向先验:同比越高越好,固定不挑)。
 - 全市场月度(hold=20)rank IC + 长多 top decile 净超额(基准=当期有信号股等权均值)−30bp。
 - 前向收益用已修复的日历对齐+ffill出场引擎(去出场生存者偏差)。
 - 安慰剂:每期内打乱信号的随机对照,真信号须显著优于随机。
诚实边界:PEAD 信号稀疏(仅近期预告股有信号);基准=有信号股均值(隔离PEAD选股);
  月度周期数受预告季节性限制;基准应改可买指数ETF。

用法:python3 phase_b_pead_backtest.py [--lookback 120] [--top-frac 0.1] [--hold 20] [--cost-bps 30]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
PEAD = WORKSPACE / "stock_data" / "03-working" / "fundamental_cache" / "pead_preannounce.parquet"
TRADING_DAYS = 244


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


def ind_z(sub: pd.DataFrame, col: str) -> pd.Series:
    x = sub[col].astype(float)
    lo, hi = x.quantile(0.01), x.quantile(0.99)
    x = x.clip(lo, hi)
    x = x - sub.assign(_x=x).groupby("industry")["_x"].transform("mean")
    sd = x.std()
    return (x - x.mean()) / sd if sd and sd > 1e-12 else x * 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=120, help="预告公告后漂移窗口(日历日)")
    ap.add_argument("--top-frac", type=float, default=0.1)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=30.0)
    ap.add_argument("--min-stocks", type=int, default=40, help="每期最少有信号股数")
    args = ap.parse_args()
    cost = args.cost_bps / 10000.0
    H = args.hold

    if not PEAD.exists():
        print(f"PEAD 数据不存在: {PEAD} (先跑 ingest_pead_preannounce.py)"); return 1
    pead = pd.read_parquet(PEAD).dropna(subset=["growth_pct", "ann_date"])
    pead["ann_dt"] = pd.to_datetime(pead["ann_date"], format="%Y%m%d", errors="coerce")
    pead = pead.dropna(subset=["ann_dt"])
    print(f"PEAD: {len(pead)} 条  {pead['ann_date'].min()}..{pead['ann_date'].max()}  唯一股 {pead['ts_code'].nunique()}")

    cols = ["trade_date", "ts_code", "industry", "open", "close", "universe_flag"]
    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    cols = [c for c in cols if c in names]
    df = pd.read_parquet(PANEL, columns=cols, engine="pyarrow")
    df = df[df["universe_flag"].fillna(0).astype(float) > 0].drop(columns=["universe_flag"])
    df["trade_date"] = df["trade_date"].astype(str)
    df["industry"] = df["industry"].fillna("NA")
    # ts_code 统一 6 位
    df["code6"] = df["ts_code"].str.extract(r"(\d{6})")[0]
    df = df.dropna(subset=["code6"])
    df = _add_calendar_fwd(df, H)
    df["trade_dt"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")

    dates = sorted(df["trade_date"].unique())
    rebal = dates[::H]
    print(f"面板 {dates[0]}..{dates[-1]}  月度调仓 {len(rebal)} 期  lookback={args.lookback}日  top{args.top_frac:.0%}  成本{args.cost_bps}bp\n")

    ics, rets, n_sig = [], [], []
    rebal_used = []
    for dt in rebal:
        T = pd.to_datetime(dt, format="%Y%m%d")
        lo = T - pd.Timedelta(days=args.lookback)
        # 公告日严格 < 调仓日(用 T 当日信息也可,但取 < T 更保守无前视)
        win = pead[(pead["ann_dt"] < T) & (pead["ann_dt"] >= lo)]
        if len(win) == 0:
            continue
        sig = win.sort_values("ann_dt").drop_duplicates("ts_code", keep="last")[["ts_code", "growth_pct"]]
        day = df[df["trade_date"] == dt][["code6", "industry", "fwd"]].dropna(subset=["fwd"])
        m = day.merge(sig.rename(columns={"ts_code": "code6"}), on="code6", how="inner")
        if len(m) < args.min_stocks:
            continue
        m = m.copy()
        m["z"] = ind_z(m, "growth_pct")
        m = m.dropna(subset=["z"])
        if len(m) < args.min_stocks:
            continue
        ic = m["z"].rank().corr(m["fwd"].rank())
        if pd.notna(ic):
            ics.append(ic)
        bench = m["fwd"].mean()
        k = max(1, int(len(m) * args.top_frac))
        top = m.nlargest(k, "z")["fwd"].mean()
        rets.append((top - bench) - cost)
        n_sig.append(len(m))
        rebal_used.append(dt)

    if len(rets) < 4:
        print(f"有效周期不足({len(rets)})"); return 1
    n = len(ics)
    m_ic = np.mean(ics); sd_ic = np.std(ics, ddof=1)
    icir = m_ic / sd_ic if sd_ic > 1e-12 else 0.0
    t_ic = icir * (n ** 0.5)
    pos = 100.0 * np.mean([x > 0 for x in ics])
    print("=== PEAD 全市场月度 rank IC(方向先验:同比越高越好) ===")
    print(f"  IC {m_ic:+.4f}  ICIR {icir:+.3f}  t {t_ic:+.2f}  %正 {pos:.0f}%  有效期 {n}  日均有信号股 {int(np.mean(n_sig))}")

    mr = np.mean(rets); sdr = np.std(rets, ddof=1)
    cpy = TRADING_DAYS / H
    nav, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        nav *= (1 + r); peak = max(peak, nav); mdd = min(mdd, nav / peak - 1)
    ann = mr * cpy * 100
    sharpe = (mr / sdr * cpy ** 0.5) if sdr > 1e-12 else 0.0
    hit = 100.0 * np.mean([r > 0 for r in rets])
    t_ret = mr / (sdr / np.sqrt(len(rets))) if sdr > 1e-12 else 0.0
    print(f"\n=== PEAD 长多 top{args.top_frac:.0%} 净超额(月度,扣{args.cost_bps}bp,基准=有信号股均值) ===")
    print(f"  净超额年化 {ann:+.2f}%  Sharpe {sharpe:+.2f}  周期均值t {t_ret:+.2f}  回撤 {mdd*100:+.1f}%  命中 {hit:.0f}%  周期 {len(rets)}")

    # 安慰剂
    rng = np.random.RandomState(20260629)
    plac = []
    for _ in range(20):
        pr = []
        for dt in rebal_used:
            T = pd.to_datetime(dt, format="%Y%m%d"); lo = T - pd.Timedelta(days=args.lookback)
            win = pead[(pead["ann_dt"] < T) & (pead["ann_dt"] >= lo)]
            sig = win.sort_values("ann_dt").drop_duplicates("ts_code", keep="last")[["ts_code", "growth_pct"]]
            day = df[df["trade_date"] == dt][["code6", "industry", "fwd"]].dropna(subset=["fwd"])
            mm = day.merge(sig.rename(columns={"ts_code": "code6"}), on="code6", how="inner")
            if len(mm) < args.min_stocks:
                continue
            mm = mm.copy(); mm["z"] = rng.permutation(ind_z(mm, "growth_pct").values)
            bench = mm["fwd"].mean(); k = max(1, int(len(mm) * args.top_frac))
            pr.append((mm.nlargest(k, "z")["fwd"].mean() - bench) - cost)
        if len(pr) >= 4:
            plac.append(np.mean(pr) * cpy * 100)
    if plac:
        pm, psd = np.mean(plac), np.std(plac)
        z = (ann - pm) / psd if psd > 1e-9 else 0.0
        print(f"\n=== 安慰剂(随机打乱信号,20次) ===")
        print(f"  随机净超额年化 均值{pm:+.2f}% std{psd:.2f} | 真实{ann:+.2f}% → {z:+.1f}σ  "
              f"{'✅真实显著优于随机(>2σ)' if z > 2 else '⚪与随机不可区分'}")

    print(f"\n口径:全市场点时面板、PEAD按公告日严格对齐(无前视)、日历对齐月度前向收益、行业中性z、扣{args.cost_bps}bp、基准=有信号股均值。"
          f"\n诚实边界:PEAD信号稀疏(仅近期预告股);基准非可买指数;月度周期受预告季节性限制;无一致预期故用同比绝对增速(意外度近似)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
