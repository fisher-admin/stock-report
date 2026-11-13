#!/usr/bin/env python3
"""隔夜方案 V0.1 的探索性回放(research-only, 2026-11-12)。

对《A股隔夜交易研究参数_v01.json》的 stable_M 假设及其强制对照组, 用现有
5min 缓存(研究800样本, 2022-01-04~2026-07-02)按"14:45 冻结 -> 14:50-14:55 买入
-> 次日 09:30-09:40 卖出"做三段收益分解。规格在跑数之前写死于 SPEC, 只跑一次。

性质: 探索性(未进 setup_registry/gauntlet, 未触碰 holdout 800)。
与原方案的已知偏差(数据所限, 全部如实披露):
  - 5min bar 而非 1min/盘口: 入场=bar[14:55](14:50-14:55) VWAP; 出场=bar[09:35]+bar[09:40] VWAP
  - 无买卖价差数据: max_relative_bid_ask_spread 未施加
  - 无事件数据: public_event_E 未检验
  - 股池=panel_v2 合格行(非ST/上市天数等) ∩ 研究800 ∩ 主板; ADV20 用 panel avg_amount_lookback
  - G 分数未复算(缺指数分钟线); 仅用样本内 14:45 广度 B 做披露
不产出任何生产文件。
"""
from __future__ import annotations
import glob, hashlib, json, os, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow.parquet as pq

WS = Path(os.environ.get("STOCK_WS") or Path(__file__).resolve().parents[3])  # 本机=~/.openclaw/workspace, VPS=/srv/hermes-stock/workspace
MIN_DIR = WS / "stock_data/01-root/minute_cache"
DAILY_DIR = WS / "factor_factory/data/price_hist/daily"
PANEL = WS / "factor_factory/data/research_panel_v2.parquet"
OUT = WS / "factor_factory/outputs/overnight_v0"

SPEC = {
    "spec_id": "overnight_v0_explore_m",
    "universe": {"board": "主板(60/00)", "adv20_min_cny": 3e8, "amt_1440_1445_min_cny": 1.5e7,
                 "exclude": ["locked_limit_up_at_1445", "holiday_gap_days>3"]},
    "groups": {
        "U_liquid": "universe only",
        "M_stable": "ret_to_cutoff in [0,0.06] & CLV>=0.55 & RV in [0.6,2.5]",
        "M0_no_ret_filter": "CLV>=0.55 & RV in [0.6,2.5]",
        "X6_online": "ret in [0.03,0.05] & RV>1 & turnover_to_cutoff in [0.05,0.10] & circ_mv 50-200亿 & px>VWAP",
    },
    "entry": "bar[14:55] VWAP; filled iff bar low <= decision_px*1.003; fill=min(vwap, cap)",
    "exit": "next session bar[09:35]+bar[09:40] VWAP; pinned at limit-down or suspended -> restricted, marked at that/next session close",
    "costs_round_trip": {"base_100k": 0.00192, "system_frozen_med": 0.0026, "small_10k_split": 0.00352},
    "stats": "daily EW mean (days with >=3 fills), Newey-West lag5 t, day-block bootstrap one-sided 95% LCB (block 5, 1000, seed 20261112)",
    "admission_threshold_lcb": 0.001,
}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

def nw_t(x, lag=5):
    x = np.asarray(x, float); n = len(x)
    if n < 10: return float("nan")
    e = x - x.mean(); v = (e @ e) / n
    for k in range(1, lag + 1):
        v += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / np.sqrt(v / n)) if v > 0 else float("nan")

def block_lcb(x, block=5, B=1000, seed=20261112):
    x = np.asarray(x, float); n = len(x)
    if n < 60: return float("nan")
    rng = np.random.default_rng(seed); nb = int(np.ceil(n / block)); means = np.empty(B)
    for b in range(B):
        starts = rng.integers(0, n - block + 1, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]
        means[b] = x[idx].mean()
    return float(np.quantile(means, 0.05))

def load_stock(code):
    f = MIN_DIR / f"minute_5min_{code.replace('.', '_')}.parquet"
    m = pq.read_table(f).to_pandas()
    m["d"] = m.trade_time.str[:10].str.replace("-", ""); m["t"] = m.trade_time.str[11:16]
    m = m.drop_duplicates(["d", "t"])
    P = {c: m.pivot(index="d", columns="t", values=c) for c in ["close", "open", "high", "low", "vol", "amount"]}
    need = ["09:30", "09:35", "09:40", "14:40", "14:45", "14:55", "15:00"]
    if any(t not in P["close"].columns for t in need): return None
    pre = [t for t in P["close"].columns if t <= "14:45"]
    o = pd.DataFrame(index=P["close"].index)
    o["decision_px"] = P["close"]["14:45"]
    o["hi_1445"] = P["high"][pre].max(axis=1); o["lo_1445"] = P["low"][pre].min(axis=1)
    o["cum_amt"] = P["amount"][pre].sum(axis=1, min_count=1); o["cum_vol"] = P["vol"][pre].sum(axis=1, min_count=1)
    o["amt_last5"] = P["amount"]["14:45"]
    def vw(ts):
        a = P["amount"][ts].sum(axis=1, min_count=1); v = P["vol"][ts].sum(axis=1, min_count=1)
        w = a / v.where(v > 0)
        lo = P["low"][ts].min(axis=1); hi = P["high"][ts].max(axis=1)
        bad = ~((w >= lo * 0.999) & (w <= hi * 1.001))      # 单位错位批次(见 S7 data_incident) -> 用典型价
        typ = (P["high"][ts].mean(axis=1) + P["low"][ts].mean(axis=1) + P["close"][ts].mean(axis=1)) / 3
        return w.where(~bad, typ), lo, hi, bad
    o["entry_vwap"], o["entry_lo"], _, o["entry_bad"] = vw(["14:55"])
    o["exit_vwap"], _, o["exit_hi"], o["exit_bad"] = vw(["09:35", "09:40"])
    o["vwap_1445"], _, _, _ = vw(pre)
    o["rv"] = o["cum_amt"] / o["cum_amt"].shift(1).rolling(20, min_periods=15).median()
    d = pq.read_table(DAILY_DIR / f"{code}.parquet", columns=["trade_date", "open", "high", "low", "close", "pre_close"]).to_pandas()
    d = d.sort_values("trade_date").set_index("trade_date")
    for c in ["open", "pre_close", "close"]: d["n_" + c] = d[c].shift(-1)
    d["n_date"] = pd.Series(d.index, index=d.index).shift(-1)
    for c in ["open", "pre_close", "close"]: d["nn_" + c] = d[c].shift(-2)   # 受限仓顺延估值
    o = o.join(d, how="inner")
    ex = o[["exit_vwap", "exit_hi"]].copy(); ex.columns = ["x_vwap", "x_hi"]
    o = o.join(ex, on="n_date")
    o["ts_code"] = code
    return o.reset_index(names="trade_date")

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pan = pq.read_table(PANEL, columns=["ts_code", "trade_date", "market_board", "avg_amount_lookback", "float_share", "circ_mv", "sw2021_l1"]).to_pandas()
    pan = pan[pan.market_board == "主板"]
    pan = pan.sort_values(["ts_code", "trade_date"])
    for c in ["float_share", "circ_mv"]: pan[c + "_lag"] = pan.groupby("ts_code")[c].shift(1)
    codes = sorted(os.path.basename(f)[12:-8].replace("_", ".") for f in glob.glob(str(MIN_DIR / "minute_5min_*.parquet")))
    codes = [c for c in codes if c[:2] in ("60", "00") and (DAILY_DIR / f"{c}.parquet").exists()]
    frames = [x for x in (load_stock(c) for c in codes) if x is not None]
    df = pd.concat(frames, ignore_index=True)
    df = df.merge(pan, on=["ts_code", "trade_date"], how="inner")          # panel 合格行 = 非ST/上市天数等
    cal = sorted(df.trade_date.unique()); nxt = dict(zip(cal[:-1], cal[1:]))
    df["mkt_next"] = df.trade_date.map(nxt)
    df = df[df.mkt_next.notna() & df.n_date.notna()].copy()
    gap = (pd.to_datetime(df.mkt_next) - pd.to_datetime(df.trade_date)).dt.days
    df["gap_days"] = gap; df["weekday"] = pd.to_datetime(df.trade_date).dt.weekday
    df["ret_cut"] = df.decision_px / df.pre_close - 1
    rng_ = (df.hi_1445 - df.lo_1445)
    df["clv"] = ((df.decision_px - df.lo_1445) / rng_).where(rng_ > 0)
    df["lim_up"] = (df.pre_close * 1.1 + 1e-9).round(2); df["n_lim_dn"] = (df.n_pre_close * 0.9 + 1e-9).round(2)
    df["locked"] = df.decision_px >= df.lim_up - 1e-6
    df["turn_cut"] = df.cum_vol / (df.float_share_lag * 1e4)
    # 样本内 14:45 广度(披露用)
    b = df.groupby("trade_date").ret_cut.agg(lambda s: (s > 0).sum() / max(((s > 0) | (s < 0)).sum(), 1)).rename("breadth_1445")
    df = df.join(b, on="trade_date")
    U = (df.avg_amount_lookback >= 3e5) & (df.amt_last5 >= 1.5e7) & (~df.locked) & (df.gap_days <= 3) & df.decision_px.notna() & df.entry_vwap.notna()
    M0 = U & (df.clv >= 0.55) & df.rv.between(0.6, 2.5)
    M = M0 & df.ret_cut.between(0, 0.06)
    X6 = U & df.ret_cut.between(0.03, 0.05) & (df.rv > 1) & df.turn_cut.between(0.05, 0.10) & df.circ_mv_lag.between(5e5, 2e6) & (df.decision_px > df.vwap_1445)
    groups = {"U_liquid": U, "M_stable": M, "M0_no_ret_filter": M0, "X6_online": X6}
    # 执行
    cap = df.decision_px * 1.003
    df["filled"] = df.entry_lo <= cap
    df["fill_px"] = np.minimum(df.entry_vwap, cap)
    df["suspended"] = df.n_date != df.mkt_next
    df["pinned"] = (~df.suspended) & (df.x_hi <= df.n_lim_dn + 1e-6)
    df["restricted"] = df.suspended | df.pinned | df.x_vwap.isna()
    df["seg1"] = df.close / df.fill_px - 1
    df["seg2"] = df.n_open / df.n_pre_close - 1
    df["seg3"] = np.where(df.restricted, df.n_close / df.n_open - 1, df.x_vwap / df.n_open - 1)
    df["total"] = (1 + df.seg1) * (1 + df.seg2) * (1 + df.seg3) - 1
    df["total_noprot"] = (df.close / df.entry_vwap) * (1 + df.seg2) * (1 + df.seg3) - 1
    df["year"] = df.trade_date.str[:4]
    df[["ts_code","trade_date","ret_cut","clv","rv","breadth_1445","filled","restricted","seg1","seg2","seg3","total","total_noprot","entry_vwap","close","n_open","n_pre_close","x_vwap","weekday","gap_days","avg_amount_lookback","amt_last5","locked"]].assign(U=U.values,M=M.values).to_parquet(OUT / "trades_explore.parquet")
    res = {"spec": SPEC, "sample": {"stocks": len(codes), "rows": int(len(df)), "dates": [cal[0], cal[-1]], "n_dates": len(cal),
                                    "bad_vwap_entry": int(df.entry_bad.sum()), "bad_vwap_exit": int(df.exit_bad.sum())}, "groups": {}}
    def summarize(sub, col="total"):
        t = sub[sub.filled & sub[col].notna()] if col == "total" else sub[sub[col].notna()]
        day = t.groupby("trade_date")[col].agg(["mean", "size"]); day = day[day["size"] >= 3]["mean"]
        o = {"n_candidates": int(len(sub)), "n_trades": int(len(t)), "n_days": int(len(day)), "avg_names_per_day": round(len(t) / max(len(day), 1), 1)}
        if len(day) < 30: return o
        o["gross_day_mean"] = float(day.mean()); o["gross_t_nw5"] = nw_t(day.values); o["day_win_rate"] = float((day > 0).mean())
        o["trade_win_rate_gross"] = float((t[col] > 0).mean())
        for k, c in SPEC["costs_round_trip"].items():
            net = day - c
            o[f"net_{k}"] = {"day_mean": float(net.mean()), "t_nw5": nw_t(net.values), "lcb95_block": block_lcb(net.values),
                             "trade_win_rate": float((t[col] - c > 0).mean())}
        q = t[col].quantile(0.05); o["trade_ES95"] = float(t[col][t[col] <= q].mean()); o["worst_day"] = float(day.min())
        return o
    for g, mask in groups.items():
        sub = df[mask]
        r = summarize(sub)
        f = sub[sub.filled]
        r["fill_rate"] = float(sub.filled.mean()) if len(sub) else None
        r["unfilled_gross_if_chased"] = float(sub[~sub.filled].total_noprot.mean()) if (~sub.filled).any() else None
        r["segments_trade_mean"] = {k: float(f[k].mean()) for k in ["seg1", "seg2", "seg3", "total"]}
        r["restricted_rate"] = float(f.restricted.mean()) if len(f) else None
        r["pinned_limit_down_rate"] = float(f.pinned.mean()) if len(f) else None
        r["no_price_protection"] = {k: v for k, v in summarize(sub, "total_noprot").items() if k in ("gross_day_mean", "gross_t_nw5", "net_base_100k")}
        r["by_year"] = {y: {"n": int(len(s[s.filled])), "gross": float(s[s.filled].groupby("trade_date").total.mean().mean()),
                            "seg2_c2o": float(s[s.filled].seg2.mean())} for y, s in sub.groupby("year")}
        r["by_weekday_gross"] = {int(w): float(s[s.filled].groupby("trade_date").total.mean().mean()) for w, s in sub.groupby("weekday")}
        br = pd.cut(sub.breadth_1445, [0, 0.25, 0.4, 0.6, 1.01], right=False)
        r["by_breadth_1445_gross"] = {str(k): {"n": int(len(s[s.filled])), "days": int(s.trade_date.nunique()),
                                               "gross": float(s[s.filled].groupby("trade_date").total.mean().mean()) if len(s) else None}
                                      for k, s in sub.groupby(br, observed=True)}
        res["groups"][g] = r
    # 配对增量: M 相对 U、M 相对 M0 (同日)
    def paired(a, b):
        da = df[groups[a] & df.filled].groupby("trade_date").total.mean(); db = df[groups[b] & df.filled].groupby("trade_date").total.mean()
        d = (da - db).dropna()
        return {"n_days": int(len(d)), "mean_diff": float(d.mean()), "t_nw5": nw_t(d.values)}
    res["paired"] = {"M_minus_U": paired("M_stable", "U_liquid"), "M_minus_M0": paired("M_stable", "M0_no_ret_filter"), "X6_minus_U": paired("X6_online", "U_liquid")}
    (OUT / "explore_overnight_m.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    print(json.dumps(res, ensure_ascii=False, indent=1, default=float))

if __name__ == "__main__":
    sys.exit(main())
