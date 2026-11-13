#!/usr/bin/env python3
"""O2C 清洁因子组合回测 + 交叉验证（2026-06-28 收口）。

在全市场点时面板上，把已坐实的清洁因子合成 composite，做【多日持有的多头 top 分位组合】，
算【扣费后相对全域等权均值的净超额】，并用 walk-forward + 最终 holdout 严格交叉验证，选最优组合。

抗自欺设计（扣住本轮全部教训）：
  - 全市场点时面板(2300只/日×1298日)、入场 T+1 开盘(无未来函数)、前向收益由 open[T+1]→close[T+H] 现算。
  - 因子截面 z-score（每日全域），composite=权重和；默认【等权】(无拟合→最抗过拟合)，--ic 用滚动IC权重对照。
  - 每 H 日调一次仓 → 换手成本按周期摊薄(双边 COST_BPS/往返)。
  - 基准=全域等权均值(隔离选股 alpha)；净超额=组合−基准−成本。
  - 留出最近 HOLDOUT_DAYS 交易日为【最终 holdout】，组合网格【只在 pre-holdout 上选】，选定后在 holdout 上只验一次。
  - naive baseline(等权全清洁因子)同样在 holdout 上报，防"网格选择"的运气。

用法：python3 o2c_strategy_backtest.py [--ic] [--top-frac 0.2] [--holdout-days 120]
"""
from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
CLEAN_FACTORS = ["g_intraday_vwap_deviation", "g_close_strength_ratio", "g_intraday_range_expansion"]
COST_BPS = 30                 # 由 --cost-bps 覆盖；往返双边总成本(基点)
COST_DEC = COST_BPS / 10000.0
HOLDS = [3, 5, 10]
TRADING_DAYS = 244


def cs_zscore(s: pd.Series) -> pd.Series:
    m, sd = s.mean(), s.std()
    return (s - m) / sd if sd and sd > 1e-12 else s * 0.0


def load_panel(since: str | None) -> pd.DataFrame:
    import pyarrow.parquet as pq
    names = pq.ParquetFile(PANEL).schema.names
    cols = ["trade_date", "ts_code", "open", "close"] + [f for f in CLEAN_FACTORS if f in names]
    if "universe_flag" in names:
        cols.append("universe_flag")
    df = pd.read_parquet(PANEL, columns=cols, engine="pyarrow")
    if "universe_flag" in df.columns:
        df = df[df["universe_flag"].fillna(0).astype(float) > 0].drop(columns=["universe_flag"])
    df["trade_date"] = df["trade_date"].astype(str)
    if since:
        df = df[df["trade_date"] >= since]
    # 日历对齐的前向收益（修复 shift(-hold) 按行号的 bug + 出场侧生存者偏差）。
    df = _add_calendar_fwd(df, HOLDS)
    # 每日截面 z-score 因子。
    for f in CLEAN_FACTORS:
        if f in df.columns:
            df[f + "_z"] = df.groupby("trade_date")[f].transform(cs_zscore)
    return df


def _add_calendar_fwd(df: pd.DataFrame, holds: list[int]) -> pd.DataFrame:
    """按【市场日历】(非个股行号)算前向收益，并对停牌/退市出场用前向填充收盘，杜绝出场侧生存者偏差。

    - 入场 = T+1 日历日的真实开盘；若个股 T+1 停牌(无开盘行)→entry_open=NaN→排除(买不到,合理,非偏差)。
    - 出场 = T+H 日历日的收盘；个股该日停牌则取其【≤该日的最后成交价】(ffill,=被套住的市值标记),
      退市则=最终价(亏损被计入,而非旧 shift 把 NaN 行静默丢弃→高估)。仅当 T+H 越过面板末端才置 NaN(未来未结算)。
    """
    cal = sorted(df["trade_date"].unique())
    ncal = len(cal)
    posmap = {d: i for i, d in enumerate(cal)}
    df = df.copy()
    df["pos"] = df["trade_date"].map(posmap).astype(int)
    # 入场开盘：T+1 日历日的真实开盘价（个股口径，缺则 NaN=停牌不可买）。
    nxt = df[["ts_code", "pos", "open"]].rename(columns={"open": "entry_open"})
    nxt = nxt.assign(pos=nxt["pos"] - 1)
    df = df.merge(nxt, on=["ts_code", "pos"], how="left").reset_index(drop=True)
    # 全日历 ffill 收盘矩阵（个股×日历位），供出场用。
    cff = (df.pivot_table(index="ts_code", columns="pos", values="close", aggfunc="last")
             .reindex(columns=range(ncal)).ffill(axis=1))
    code_row = {c: i for i, c in enumerate(cff.index)}
    cvals = cff.to_numpy()
    rows = df["ts_code"].map(code_row).to_numpy()
    base_pos = df["pos"].to_numpy()
    eo = df["entry_open"].to_numpy(dtype=float)
    for h in holds:
        ep = base_pos + h
        valid = ep < ncal
        ec = np.full(len(df), np.nan)
        ec[valid] = cvals[rows[valid], ep[valid]]
        df["_fwd_" + str(h)] = (ec - eo) / eo
    return df


def fwd_return(df: pd.DataFrame, hold: int) -> pd.Series:
    """日历对齐的前向收益（已在 _add_calendar_fwd 预算为列 _fwd_<hold>）。"""
    col = "_fwd_" + str(hold)
    if col in df.columns:
        return df[col]
    # 子集若未携带该列(理论上都带)，回退到日历重算。
    return _add_calendar_fwd(df, [hold])["_fwd_" + str(hold)]


def backtest(df: pd.DataFrame, factors: list[str], weights: dict[str, float] | None,
             hold: int, top_frac: float) -> dict:
    """返回每周期净超额序列 + 汇总。weights=None 则等权。"""
    zcols = [f + "_z" for f in factors]
    w = {c: (weights[factors[i]] if weights else 1.0) for i, c in enumerate(zcols)}
    d = df.copy()
    d["_fwd"] = fwd_return(d, hold)
    d["_composite"] = sum(d[c].fillna(0.0) * w[c] for c in zcols)
    dates = sorted(d["trade_date"].unique())
    rebal = dates[::hold]  # 每 H 个交易日建一次仓
    cyc = []  # (date, net_excess)
    for dt in rebal:
        day = d[d["trade_date"] == dt].dropna(subset=["_fwd", "_composite"])
        if len(day) < 50:
            continue
        bench = day["_fwd"].mean()  # 全域等权均值
        k = max(1, int(len(day) * top_frac))
        top = day.nlargest(k, "_composite")["_fwd"].mean()
        net_excess = (top - bench) - COST_DEC  # 扣一次往返费
        cyc.append((dt, net_excess))
    if len(cyc) < 4:
        return {"n": len(cyc)}
    rets = [x[1] for x in cyc]
    n = len(rets)
    m = sum(rets) / n
    sd = (sum((x - m) ** 2 for x in rets) / (n - 1)) ** 0.5
    cyc_per_year = TRADING_DAYS / hold
    ann = m * cyc_per_year
    sharpe = (m / sd * (cyc_per_year ** 0.5)) if sd > 1e-12 else 0.0
    # 净值 & 最大回撤
    nav, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        nav *= (1 + r)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
    hit = 100.0 * sum(1 for r in rets if r > 0) / n
    return {"n": n, "ann_net_excess_pct": round(ann * 100, 2), "sharpe": round(sharpe, 2),
            "mdd_pct": round(mdd * 100, 1), "hit_pct": round(hit, 1),
            "mean_cyc_pct": round(m * 100, 3), "rets": rets, "dates": [x[0] for x in cyc]}


def rolling_ic_weights(df: pd.DataFrame, factors: list[str], hold: int, as_of: str, lookback_days: int = 250) -> dict:
    """as_of 之前 lookback 交易日内，每因子对 hold 日前向收益的平均 IC，归一化为非负权重。"""
    dates = [x for x in sorted(df["trade_date"].unique()) if x < as_of][-lookback_days:]
    if not dates:
        return {f: 1.0 for f in factors}
    sub = df[df["trade_date"].isin(dates)].copy()
    sub["_fwd"] = fwd_return(sub, hold)
    ics = {}
    for f in factors:
        per = []
        for _, g in sub.groupby("trade_date"):
            s = g[[f + "_z", "_fwd"]].dropna()
            if len(s) >= 50:
                ic = s[f + "_z"].rank().corr(s["_fwd"].rank())
                if pd.notna(ic):
                    per.append(ic)
        ics[f] = (sum(per) / len(per)) if per else 0.0
    pos = {f: max(0.0, ics[f]) for f in factors}
    tot = sum(pos.values()) or 1.0
    return {f: pos[f] / tot for f in factors}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ic", action="store_true", help="额外评估 IC 权重(滚动,OOS)对照")
    ap.add_argument("--top-frac", type=float, default=0.2)
    ap.add_argument("--holdout-days", type=int, default=120)
    ap.add_argument("--since", default="20220101")
    ap.add_argument("--cost-bps", type=float, default=30.0, help="往返双边总成本(基点)，A股实盘约13-20bp")
    args = ap.parse_args()
    global COST_BPS, COST_DEC
    COST_BPS = args.cost_bps
    COST_DEC = COST_BPS / 10000.0
    df = load_panel(args.since)
    dates = sorted(df["trade_date"].unique())
    holdout_start = dates[-args.holdout_days]
    pre = df[df["trade_date"] < holdout_start]
    print(f"面板 {dates[0]}..{dates[-1]} {len(dates)}日 | pre-holdout<{holdout_start} | holdout 最近{args.holdout_days}日 | top {args.top_frac:.0%} | 成本{COST_BPS}bp/往返\n")

    # 因子集候选（逐步加因子）。
    sets = []
    for k in range(1, len(CLEAN_FACTORS) + 1):
        sets.append(CLEAN_FACTORS[:k])
    configs = list(itertools.product(sets, HOLDS))

    print("=== 网格(等权) — 仅在 pre-holdout 上评估、用于选最优 ===")
    print(f"  {'因子集':<22}{'持有':>5}{'净超额年化':>11}{'Sharpe':>8}{'回撤':>8}{'命中':>7}{'周期':>6}")
    grid = []
    for fset, hold in configs:
        r = backtest(pre, fset, None, hold, args.top_frac)
        if r.get("n", 0) < 4:
            continue
        tag = "+".join(f.replace("g_intraday_", "").replace("g_", "") for f in fset)
        grid.append((tag, fset, hold, r))
        print(f"  {tag:<22}{hold:>4}日{r['ann_net_excess_pct']:>+10.2f}%{r['sharpe']:>+8.2f}{r['mdd_pct']:>+7.1f}%{r['hit_pct']:>6.0f}%{r['n']:>6}")

    best = max(grid, key=lambda x: x[3]["sharpe"]) if grid else None
    if not best:
        print("无足够样本"); return 1
    print(f"\n→ pre-holdout 最优(按 Sharpe)：[{best[0]}] 持有 {best[2]}日，"
          f"净超额年化 {best[3]['ann_net_excess_pct']:+.2f}%、Sharpe {best[3]['sharpe']:+.2f}")

    # 在 holdout 上只验一次（最优配置 + naive 全因子等权基线）。
    print(f"\n=== 最终 holdout 验证（{holdout_start}..{dates[-1]}，从未参与选择）===")
    hb = backtest(df[df["trade_date"] >= holdout_start], best[1], None, best[2], args.top_frac)
    naive = backtest(df[df["trade_date"] >= holdout_start], CLEAN_FACTORS, None, best[2], args.top_frac)
    def line(name, r):
        if r.get("n", 0) < 3:
            return f"  {name:<26} holdout 周期不足({r.get('n',0)})"
        return f"  {name:<26} 净超额年化 {r['ann_net_excess_pct']:>+7.2f}%  Sharpe {r['sharpe']:>+5.2f}  回撤 {r['mdd_pct']:>+5.1f}%  命中 {r['hit_pct']:>4.0f}%  周期{r['n']}"
    print(line(f"最优[{best[0]}]·{best[2]}日", hb))
    print(line(f"naive等权全清洁·{best[2]}日", naive))

    if args.ic:
        print("\n=== IC权重(滚动OOS) 对照：最优因子集、最优持有 ===")
        fset, hold = best[1], best[2]
        # 走查：对 holdout 每个建仓日，用其之前 250 日 IC 定权
        hd = df[df["trade_date"] >= holdout_start]
        rebal = sorted(hd["trade_date"].unique())[::hold]
        cyc = []
        for dt in rebal:
            w = rolling_ic_weights(df, fset, hold, dt)
            # 用 as-of 之前数据定的权重，算该建仓日的 net_excess（OOS）。
            day = df[df["trade_date"] == dt].copy()
            day["_fwd"] = fwd_return(df, hold).loc[day.index]
            zc = [f + "_z" for f in fset]
            day["_c"] = sum(day[c].fillna(0) * w[fset[i]] for i, c in enumerate(zc))
            day = day.dropna(subset=["_fwd", "_c"])
            if len(day) < 50:
                continue
            k = max(1, int(len(day) * args.top_frac))
            ne = (day.nlargest(k, "_c")["_fwd"].mean() - day["_fwd"].mean()) - COST_DEC
            cyc.append(ne)
        if len(cyc) >= 3:
            m = sum(cyc) / len(cyc)
            sd = (sum((x - m) ** 2 for x in cyc) / (len(cyc) - 1)) ** 0.5
            cpy = TRADING_DAYS / hold
            print(f"  IC权重·{hold}日  净超额年化 {m*cpy*100:+.2f}%  Sharpe {(m/sd*cpy**0.5 if sd>1e-12 else 0):+.2f}  周期{len(cyc)}")

    print(f"\n口径：全市场点时面板、入场T+1开盘、每{best[2]}日调仓、扣双边{COST_BPS}bp、基准=全域等权均值(选股alpha)。"
          f"\n诚实边界：holdout 仅 {args.holdout_days} 日(周期数少)、单资产无对冲、成本/涨跌停可成交未精算；holdout 为唯一一次外推证据，不可反复试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
