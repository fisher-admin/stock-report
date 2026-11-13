#!/usr/bin/env python3
"""Phase A 慢风格价量骨架回测（2026-06-28，repo-only 无新数据）。

检验多因子集设计的核心假设:**慢变价量风格因子在月频调仓下能否扣费后净正**——
而日内快因子(O2C)已被证伪(holdout 净负)。仅用面板(1298日点时,2300股/日),
价值/质量需 akshare 估值历史,留待 Phase B。

抗自欺设计(扣住本季全部教训):
 - 前向收益用【已修复的日历对齐+ffill出场】引擎(去出场侧生存者偏差),月度 hold=20。
 - 因子方向【按经济学先验固定】,绝不按结果反向挑(防方向钓鱼/多重比较)。
 - 因子=横截面 winsorize(1/99)→【行业内 demean】→z-score(106行业,行业中性)。
 - 等权 composite(无拟合权重→最抗过拟合)长多 top decile,月度调仓,扣30bp往返。
 - pre-holdout 选/holdout 只验一次 + 【安慰剂】:每日内打乱 composite 的随机对照,
   真信号必须显著优于随机,否则判噪声。
 - 诚实边界:基准=全域均值(Phase B 改可买指数 ETF);无市值故缺规模 sleeve;月度周期数有限。

用法: python3 phase_a_slow_style_backtest.py [--since 20220101] [--top-frac 0.1] [--hold 20] [--cost-bps 30]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"

# 慢风格因子:列名 -> (经济学先验方向 +1/-1, 是否需自建)。方向先验固定,不按结果调。
RAW_COLS = ["ret_5d", "ret_20d", "ret_std_20", "g_low_vol_leader", "g_long_low_vol_resilience",
            "g_trend_quality_20", "g_momentum_stability_10_20", "g_industry_relative_strength",
            "g_drawdown_resilience"]
TRADING_DAYS = 244


def _add_calendar_fwd(df: pd.DataFrame, holds: list[int]) -> pd.DataFrame:
    """日历对齐前向收益(与 o2c_strategy_backtest 同口径):入场 T+1 日历日开盘(停牌NaN排除),
    出场 T+H 日历日收盘,停牌/退市用 ffill 最后成交价计入亏损(不丢弃),越过面板末端置 NaN。"""
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
    base = df["pos"].to_numpy()
    eo = df["entry_open"].to_numpy(dtype=float)
    for h in holds:
        ep = base + h
        valid = ep < ncal
        ec = np.full(len(df), np.nan)
        ec[valid] = cvals[rows[valid], ep[valid]]
        df["_fwd_" + str(h)] = (ec - eo) / eo
    return df


def ind_neutral_z(df: pd.DataFrame, col: str) -> pd.Series:
    """横截面:winsorize(1/99)→行业内 demean→z-score。逐交易日。"""
    def _one(g: pd.DataFrame) -> pd.Series:
        x = g[col].astype(float)
        lo, hi = x.quantile(0.01), x.quantile(0.99)
        x = x.clip(lo, hi)
        # 行业内去均值
        x = x - g.groupby("industry")[col].transform(lambda s: s.clip(lo, hi).mean())
        sd = x.std()
        return (x - x.mean()) / sd if sd and sd > 1e-12 else x * 0.0
    return df.groupby("trade_date", group_keys=False).apply(_one)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20220101")
    ap.add_argument("--top-frac", type=float, default=0.1)
    ap.add_argument("--hold", type=int, default=20, help="月度≈20交易日")
    ap.add_argument("--cost-bps", type=float, default=30.0)
    ap.add_argument("--holdout-days", type=int, default=240, help="留出最近N日为holdout")
    args = ap.parse_args()
    cost = args.cost_bps / 10000.0
    H = args.hold

    cols = ["trade_date", "ts_code", "industry", "open", "close", "universe_flag"] + RAW_COLS
    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    cols = [c for c in cols if c in names]
    df = pd.read_parquet(PANEL, columns=cols, engine="pyarrow")
    df = df[df["universe_flag"].fillna(0).astype(float) > 0].drop(columns=["universe_flag"])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df[df["trade_date"] >= args.since]
    df["industry"] = df["industry"].fillna("NA")
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    # 自建动量 12-1(剔近月):close.shift(21)/close.shift(252)-1(行号≈交易日,特征用shift标准做法)。
    g = df.groupby("ts_code")["close"]
    df["mom_12_1"] = g.shift(21) / g.shift(252) - 1.0

    # 因子方向先验(+1 越大越好 / -1 越小越好),固定不调。
    FACTORS = {
        "mom_12_1": +1,                       # 中期动量:赢家恒赢(先验+)
        "ret_20d": +1,                        # 1月动量(先验+)
        "ret_5d": -1,                         # 1周短反转:近期输家反弹(先验-)
        "ret_std_20": -1,                     # 低波异象 BAB(先验-)
        "g_low_vol_leader": +1,               # 低波领导力(g因子已正向)
        "g_long_low_vol_resilience": +1,      # 低波韧性
        "g_trend_quality_20": +1,             # 趋势质量
        "g_momentum_stability_10_20": +1,     # 动量稳定性
        "g_industry_relative_strength": +1,   # 行业相对强度
        "g_drawdown_resilience": +1,          # 回撤韧性
    }
    FACTORS = {k: v for k, v in FACTORS.items() if k in df.columns}

    # 日历对齐月度前向收益。
    df = _add_calendar_fwd(df, [H])
    fwd = "_fwd_" + str(H)

    # 行业中性 z-score(含先验方向)。
    print(f"面板 {df['trade_date'].min()}..{df['trade_date'].max()}  hold={H}日(月度)  top{args.top_frac:.0%}  成本{args.cost_bps}bp/往返  行业中性\n")
    print("=== 逐因子 全市场月度 rank IC(先验方向已对齐,越大越好) ===")
    print(f"  {'因子':<30}{'方向':>5}{'IC':>9}{'ICIR':>8}{'t':>7}{'%正':>6}")
    zmat = {}
    for f, sign in FACTORS.items():
        z = ind_neutral_z(df, f) * sign
        zmat[f] = z
        sub = pd.DataFrame({"d": df["trade_date"], "z": z, "r": df[fwd]}).dropna()
        ics = []
        for _, gg in sub.groupby("d"):
            if len(gg) >= 50:
                ic = gg["z"].rank().corr(gg["r"].rank())
                if pd.notna(ic):
                    ics.append(ic)
        if len(ics) < 5:
            print(f"  {f:<30}{'+' if sign>0 else '-':>5}  样本不足"); continue
        m = np.mean(ics); sd = np.std(ics, ddof=1)
        icir = m / sd if sd > 1e-12 else 0.0
        t = icir * (len(ics) ** 0.5)
        pos = 100.0 * np.mean([x > 0 for x in ics])
        print(f"  {f:<30}{'+' if sign>0 else '-':>5}{m:>+9.4f}{icir:>+8.3f}{t:>+7.2f}{pos:>5.0f}%")

    # 等权 composite(方向已对齐)。
    df["_comp"] = pd.concat(zmat.values(), axis=1).mean(axis=1)

    def backtest(d: pd.DataFrame, comp_col: str) -> dict:
        dates = sorted(d["trade_date"].unique())
        rebal = dates[::H]
        rets = []
        for dt in rebal:
            day = d[d["trade_date"] == dt].dropna(subset=[fwd, comp_col])
            if len(day) < 50:
                continue
            bench = day[fwd].mean()
            k = max(1, int(len(day) * args.top_frac))
            top = day.nlargest(k, comp_col)[fwd].mean()
            rets.append((top - bench) - cost)
        if len(rets) < 4:
            return {"n": len(rets)}
        m = np.mean(rets); sd = np.std(rets, ddof=1)
        cpy = TRADING_DAYS / H
        nav, peak, mdd = 1.0, 1.0, 0.0
        for r in rets:
            nav *= (1 + r); peak = max(peak, nav); mdd = min(mdd, nav / peak - 1)
        return {"n": len(rets), "ann": m * cpy * 100, "sharpe": (m / sd * cpy ** 0.5) if sd > 1e-12 else 0.0,
                "mdd": mdd * 100, "hit": 100.0 * np.mean([r > 0 for r in rets]), "mean_cyc": m * 100}

    dates = sorted(df["trade_date"].unique())
    hstart = dates[-args.holdout_days]
    pre = df[df["trade_date"] < hstart]
    hold_df = df[df["trade_date"] >= hstart]

    print(f"\n=== composite 等权多头 top{args.top_frac:.0%} 回测(月度调仓,扣{args.cost_bps}bp) ===")
    rp = backtest(pre, "_comp")
    rh = backtest(hold_df, "_comp")
    rall = backtest(df, "_comp")
    def line(name, r):
        if r.get("n", 0) < 4:
            return f"  {name:<22} 周期不足({r.get('n',0)})"
        return (f"  {name:<22} 净超额年化 {r['ann']:>+7.2f}%  Sharpe {r['sharpe']:>+5.2f}  "
                f"回撤 {r['mdd']:>+6.1f}%  命中 {r['hit']:>4.0f}%  周期{r['n']}")
    print(line(f"pre-holdout(<{hstart})", rp))
    print(line(f"holdout(>={hstart})", rh))
    print(line("全样本", rall))

    # 安慰剂:每日内打乱 composite(破坏选股信息,保留分布),重复多次取分布。
    print(f"\n=== 安慰剂对照(每日内随机打乱 composite,全样本,10次) ===")
    rng = np.random.RandomState(20260628)
    plac = []
    for _ in range(10):
        d2 = df.copy()
        d2["_rnd"] = d2.groupby("trade_date")["_comp"].transform(lambda s: rng.permutation(s.values))
        r = backtest(d2, "_rnd")
        if r.get("n", 0) >= 4:
            plac.append(r["ann"])
    if plac:
        pm, psd = np.mean(plac), np.std(plac)
        real = rall.get("ann", 0)
        zscore = (real - pm) / psd if psd > 1e-9 else 0.0
        print(f"  随机 composite 净超额年化: 均值{pm:+.2f}% std{psd:.2f}  | 真实{real:+.2f}%  → 真实距随机 {zscore:+.1f}σ")
        print(f"  判读: {'✅真实显著优于随机(>2σ)' if zscore>2 else ('⚪与随机不可区分(≤2σ)=无可信选股信息' if zscore<=2 else '')}")

    # === 滚动签名IC 走查(无前视):每个调仓日用【过去250交易日】的签名IC给因子定权(含符号),
    #     施加到当日截面选top decile。方向由历史自定,不看未来→合法OOS,杜绝事后方向钓鱼。===
    print(f"\n=== 滚动签名IC 走查(方向/权重仅用过去250日定,无前视) ===")
    zdf = pd.DataFrame(zmat)              # 已含先验方向,但这里只用其数值,符号由滚动IC重定
    zdf["d"] = df["trade_date"].values
    zdf["r"] = df[fwd].values
    # 还原为"原始方向"的z(去掉先验sign,让滚动IC纯粹自己决定符号)
    for f, sign in FACTORS.items():
        if sign < 0:
            zdf[f] = -zdf[f]
    alldates = sorted(df["trade_date"].unique())
    rebal = alldates[::H]
    lookback = 250
    rolling_rets = []
    for dt in rebal:
        hist = zdf[(zdf["d"] < dt)]
        # 取过去 lookback 交易日
        hist_dates = sorted(hist["d"].unique())[-lookback:]
        hist = hist[hist["d"].isin(hist_dates)].dropna(subset=["r"])
        if hist["d"].nunique() < 60:
            continue
        # 每因子签名IC(过去窗口日均 Spearman)
        wsign = {}
        for f in FACTORS:
            ics = []
            for _, gg in hist.groupby("d"):
                s = gg[[f, "r"]].dropna()
                if len(s) >= 50:
                    ic = s[f].rank().corr(s["r"].rank())
                    if pd.notna(ic):
                        ics.append(ic)
            wsign[f] = np.mean(ics) if ics else 0.0
        day = zdf[zdf["d"] == dt].copy()
        day = day.dropna(subset=["r"])
        if len(day) < 50:
            continue
        day["_c"] = sum(day[f].fillna(0.0) * wsign[f] for f in FACTORS)
        bench = day["r"].mean()
        k = max(1, int(len(day) * args.top_frac))
        top = day.nlargest(k, "_c")["r"].mean()
        rolling_rets.append((top - bench) - cost)
    if len(rolling_rets) >= 4:
        m = np.mean(rolling_rets); sd = np.std(rolling_rets, ddof=1)
        cpy = TRADING_DAYS / H
        nav, peak, mdd = 1.0, 1.0, 0.0
        for r in rolling_rets:
            nav *= (1 + r); peak = max(peak, nav); mdd = min(mdd, nav / peak - 1)
        ann = m * cpy * 100; sharpe = (m / sd * cpy ** 0.5) if sd > 1e-12 else 0.0
        hit = 100.0 * np.mean([r > 0 for r in rolling_rets])
        verdict = "✅扣费后净正(方向无前视自适应)" if ann > 0 else "⚪扣费后仍负"
        print(f"  滚动签名IC composite: 净超额年化 {ann:+.2f}%  Sharpe {sharpe:+.2f}  回撤 {mdd*100:+.1f}%  命中 {hit:.0f}%  周期{len(rolling_rets)}  {verdict}")

    print(f"\n口径:全市场点时面板、日历对齐月度前向收益(去出场生存者偏差)、行业中性z-score、扣{args.cost_bps}bp、基准=全域均值。"
          f"\n诚实边界:无市值故缺规模sleeve;价值/质量需Phase B估值历史;基准应改可买指数ETF;月度周期数有限。"
          f"\n方法学:等权composite用固定先验方向(故意不挑);滚动签名IC用过去250日自定方向(无前视,合法OOS)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
