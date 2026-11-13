#!/usr/bin/env python3
"""隔夜多因子统计模型(research-only, 2026-11-12)。

问题: 14:45 可得的因子能否在截面上挑出"尾盘买(14:50-14:55 VWAP)->次晨卖(09:30-09:40 VWAP)"
扣费后期望为正的股票? 因子有哪些、权重多少?

设计在跑数前写死(SPEC), 分三段:
  开发期   20220104-20251231: 单因子 IC; 模型权重只用开发期内、且严格早于预测月的数据
  样本外   20240101-20251231: 滚动(24个月训练, 按月重估, 末日清洗)前5名组合
  最终留出 20260101-20260702: 设计冻结后只评估一次(--final)
留出800只股票(minute_cache_holdout)完全不碰, 留给正式闯关。
"""
from __future__ import annotations
import glob, hashlib, json, os, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow.parquet as pq

WS = Path(os.environ.get("STOCK_WS") or Path(__file__).resolve().parents[3])  # 本机=~/.openclaw/workspace, VPS=/srv/hermes-stock/workspace
# 以下路径可用环境变量覆盖(时间留出集/VPS 影子运行用同一套代码, 只换输入输出目录)
MIN_DIR = Path(os.environ.get("OVN_MIN_DIR") or WS / "stock_data/01-root/minute_cache")
DAILY_DIR = Path(os.environ.get("OVN_DAILY_DIR") or WS / "factor_factory/data/price_hist/daily")
PANEL = Path(os.environ.get("OVN_PANEL") or WS / "factor_factory/data/research_panel_v2.parquet")
OUT = Path(os.environ.get("OVN_OUT") or WS / "factor_factory/outputs/overnight_v0")
FEATS = ["ret_cut", "ret_open", "gap_today", "ret_pm", "ret_tail45", "ret_last5", "clv", "vwap_dev", "range_1445",
         "rv", "turn_cut", "last5_amt_share", "touched_limit", "sector_rel_ret",
         "ret_5d", "ret_20d", "vol_20", "on_mean_20", "on_mean_60", "on_vol_20", "on_q05_60", "id_mean_20",
         "log_adv20", "log_circ_mv", "log_px"]
SPEC = {
    "spec_id": "overnight_factor_model_v0", "features": FEATS,
    "transform": "per-day cross-sectional rank to [-0.5,0.5]; missing -> 0",
    "target": "y = close/entry_vwap * open1/pre_close1 * exit_vwap1/open1 - 1 (no price protection, winsor +-0.12), cross-sectionally demeaned for fitting",
    "fit_universe": "panel_v2 eligible main board in research-800, ADV20>=1亿, amt_1440_1445>=300万, not locked limit-up, gap_days<=3",
    "models": {"FM": "daily cross-sectional ridge(lambda=1e-3*n) coefficients averaged over trailing 24 months (purge last day)",
               "ICW": "weights = trailing 24-month mean daily rank IC of each feature"},
    "portfolio": "top5 by prediction, equal slots; entry filled iff bar[14:55].low <= px1445*1.003, fill=min(vwap,cap); unfilled slot = cash",
    "primary_cell": "FM x U_liquid(ADV20>=3亿 & amt_last5>=1500万) x top5 x no gate",
    "secondary_cells": ["ICW", "broad universe", "breadth_1445>=0.6 gate(该门槛此前已在全样本上看过, 属已污染披露)", "top-decile"],
    "cost_round_trip": 0.00192, "admission_lcb": 0.001,
    "periods": {"dev": ["20220104", "20251231"], "oos": ["20240101", "20251231"], "final": ["20260101", "20260702"]},
}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

def nw_t(x, lag=5):
    x = np.asarray(x, float); x = x[~np.isnan(x)]; n = len(x)
    if n < 20: return float("nan")
    e = x - x.mean(); v = float(np.dot(e, e)) / n
    for k in range(1, lag + 1): v += 2 * (1 - k / (lag + 1)) * float(np.dot(e[k:], e[:-k])) / n
    return float(x.mean() / np.sqrt(v / n)) if v > 0 else float("nan")

def block_lcb(x, block=5, B=1000, seed=20261112):
    x = np.asarray(x, float); n = len(x)
    if n < 60: return float("nan")
    rng = np.random.default_rng(seed); nb = int(np.ceil(n / block)); m = np.empty(B)
    for b in range(B):
        st = rng.integers(0, n - block + 1, nb); m[b] = x[(st[:, None] + np.arange(block)[None, :]).ravel()[:n]].mean()
    return float(np.quantile(m, 0.05))

def load_stock(code):
    m = pq.read_table(MIN_DIR / f"minute_5min_{code.replace('.', '_')}.parquet").to_pandas()
    m["d"] = m.trade_time.str[:10].str.replace("-", ""); m["t"] = m.trade_time.str[11:16]
    m = m.drop_duplicates(["d", "t"])
    P = {c: m.pivot(index="d", columns="t", values=c) for c in ["close", "high", "low", "vol", "amount"]}
    need = ["09:35", "09:40", "11:30", "14:00", "14:40", "14:45", "14:55"]
    if any(t not in P["close"].columns for t in need): return None
    pre = [t for t in P["close"].columns if t <= "14:45"]
    o = pd.DataFrame(index=P["close"].index)
    px = P["close"]["14:45"]; o["px"] = px
    o["hi"] = P["high"][pre].max(axis=1); o["lo"] = P["low"][pre].min(axis=1)
    o["cum_amt"] = P["amount"][pre].sum(axis=1, min_count=1); o["cum_vol"] = P["vol"][pre].sum(axis=1, min_count=1)
    o["amt_last5"] = P["amount"]["14:45"]
    def vw(ts):
        a = P["amount"][ts].sum(axis=1, min_count=1); v = P["vol"][ts].sum(axis=1, min_count=1); w = a / v.where(v > 0)
        lo = P["low"][ts].min(axis=1); hi = P["high"][ts].max(axis=1)
        typ = (P["high"][ts].mean(axis=1) + P["low"][ts].mean(axis=1) + P["close"][ts].mean(axis=1)) / 3
        return w.where((w >= lo * 0.999) & (w <= hi * 1.001), typ), lo
    o["entry_vwap"], o["entry_lo"] = vw(["14:55"]); o["exit_vwap"], _ = vw(["09:35", "09:40"]); o["vwap_1445"], _ = vw(pre)
    o["ret_pm"] = px / P["close"]["11:30"] - 1; o["ret_tail45"] = px / P["close"]["14:00"] - 1; o["ret_last5"] = px / P["close"]["14:40"] - 1
    o["rv"] = o.cum_amt / o.cum_amt.shift(1).rolling(20, min_periods=15).median()
    d = pq.read_table(DAILY_DIR / f"{code}.parquet", columns=["trade_date", "open", "close", "pre_close"]).to_pandas().sort_values("trade_date").set_index("trade_date")
    r = d.close / d.pre_close - 1; on = d.open / d.pre_close - 1; idr = d.close / d.open - 1
    d["gap_today"] = on
    d["ret_5d"] = (1 + r).shift(1).rolling(5).apply(np.prod, raw=True) - 1
    d["ret_20d"] = (1 + r).shift(1).rolling(20).apply(np.prod, raw=True) - 1
    d["vol_20"] = r.shift(1).rolling(20).std()
    d["on_mean_20"] = on.rolling(20).mean(); d["on_mean_60"] = on.rolling(60).mean()      # 含当日跳空(14:45 已知)
    d["on_vol_20"] = on.rolling(20).std(); d["on_q05_60"] = on.rolling(60).quantile(0.05)
    d["id_mean_20"] = idr.shift(1).rolling(20).mean()
    for c in ["open", "pre_close", "close"]: d["n_" + c] = d[c].shift(-1)
    d["n_date"] = pd.Series(d.index, index=d.index).shift(-1)
    o = o.join(d, how="inner")
    o = o.join(o[["exit_vwap"]].rename(columns={"exit_vwap": "x_vwap"}), on="n_date")
    o["ts_code"] = code
    return o.reset_index(names="trade_date")

def build():
    cache = OUT / "factor_frame.parquet"
    if cache.exists(): return pd.read_parquet(cache)
    pan = pq.read_table(PANEL, columns=["ts_code", "trade_date", "market_board", "avg_amount_lookback", "float_share", "circ_mv", "sw2021_l1"]).to_pandas()
    pan = pan[pan.market_board == "主板"].sort_values(["ts_code", "trade_date"])
    for c in ["float_share", "circ_mv"]: pan[c + "_lag"] = pan.groupby("ts_code")[c].shift(1)
    codes = sorted(os.path.basename(f)[12:-8].replace("_", ".") for f in glob.glob(str(MIN_DIR / "minute_5min_*.parquet")))
    codes = [c for c in codes if c[:2] in ("60", "00") and (DAILY_DIR / f"{c}.parquet").exists()]
    df = pd.concat([x for x in (load_stock(c) for c in codes) if x is not None], ignore_index=True)
    df = df.merge(pan, on=["ts_code", "trade_date"], how="inner")
    cal = sorted(df.trade_date.unique()); nxt = dict(zip(cal[:-1], cal[1:]))
    df["mkt_next"] = df.trade_date.map(nxt); df = df[df.mkt_next.notna() & df.n_date.notna()].copy()
    df["gap_days"] = (pd.to_datetime(df.mkt_next) - pd.to_datetime(df.trade_date)).dt.days
    df["ret_cut"] = df.px / df.pre_close - 1; df["ret_open"] = df.px / df.open - 1
    rg = df.hi - df.lo; df["clv"] = ((df.px - df.lo) / rg).where(rg > 0)
    df["vwap_dev"] = df.px / df.vwap_1445 - 1; df["range_1445"] = rg / df.pre_close
    df["turn_cut"] = df.cum_vol / (df.float_share_lag * 1e4); df["last5_amt_share"] = df.amt_last5 / df.cum_amt
    df["lim_up"] = (df.pre_close * 1.1 + 1e-9).round(2); df["locked"] = df.px >= df.lim_up - 1e-6
    df["touched_limit"] = (df.hi >= df.lim_up - 1e-6).astype(float)
    df["log_adv20"] = np.log(df.avg_amount_lookback.clip(lower=1)); df["log_circ_mv"] = np.log(df.circ_mv_lag); df["log_px"] = np.log(df.px)
    df["sector_rel_ret"] = df.ret_cut - df.groupby(["trade_date", "sw2021_l1"]).ret_cut.transform("mean")
    b = df.groupby("trade_date").ret_cut.agg(lambda s: (s > 0).sum() / max(((s > 0) | (s < 0)).sum(), 1)).rename("breadth_1445")
    df = df.join(b, on="trade_date")
    susp = df.n_date != df.mkt_next
    seg3 = np.where(susp | df.x_vwap.isna(), df.n_close / df.n_open - 1, df.x_vwap / df.n_open - 1)
    df["seg1n"] = df.close / df.entry_vwap - 1; df["seg2"] = df.n_open / df.n_pre_close - 1; df["seg3"] = seg3
    df["y"] = ((1 + df.seg1n) * (1 + df.seg2) * (1 + df.seg3) - 1).clip(-0.12, 0.12)
    cap = df.px * 1.003; df["filled"] = df.entry_lo <= cap
    df["y_fill"] = (df.close / np.minimum(df.entry_vwap, cap)) * (1 + df.seg2) * (1 + df.seg3) - 1
    df["elig"] = (df.avg_amount_lookback >= 1e5) & (df.amt_last5 >= 3e6) & (~df.locked) & (df.gap_days <= 3) & df.y.notna() & df.px.notna()
    df["U"] = df.elig & (df.avg_amount_lookback >= 3e5) & (df.amt_last5 >= 1.5e7)
    df = df[df.elig].copy()
    for f in FEATS: df["z_" + f] = (df.groupby("trade_date")[f].rank(pct=True) - 0.5).fillna(0.0)
    keep = ["ts_code", "trade_date", "y", "y_fill", "filled", "seg1n", "seg2", "seg3", "U", "breadth_1445"] + FEATS + ["z_" + f for f in FEATS]
    df[keep].to_parquet(cache); return df[keep]

def main():
    final = "--final" in sys.argv
    df = build(); Z = ["z_" + f for f in FEATS]
    dev = df[df.trade_date <= SPEC["periods"]["dev"][1]]
    res = {"spec": SPEC, "sample": {"rows": int(len(df)), "names_per_day": round(len(df) / df.trade_date.nunique(), 1),
                                    "U_names_per_day": round(df.U.sum() / df.trade_date.nunique(), 1),
                                    "y_mean_bp": round(df.groupby("trade_date").y.mean().mean() * 1e4, 2), "y_xs_sd_bp": round(df.groupby("trade_date").y.std().mean() * 1e4, 1)}}
    # ---- A. 单因子(开发期)
    uni = {}
    for f in FEATS:
        g = dev.groupby("trade_date")
        ic = g.apply(lambda s: s["z_" + f].corr(s.y.rank())); ic2 = g.apply(lambda s: s["z_" + f].corr(s.seg2.rank()))
        top = dev[dev["z_" + f] > 0.3].groupby("trade_date").y.mean(); bot = dev[dev["z_" + f] < -0.3].groupby("trade_date").y.mean()
        uni[f] = {"ic": round(float(ic.mean()), 4), "ic_t": round(nw_t(ic.values), 2), "ic_overnight_seg": round(float(ic2.mean()), 4),
                  "top20_gross_bp": round(float(top.mean()) * 1e4, 1), "bot20_gross_bp": round(float(bot.mean()) * 1e4, 1),
                  "spread_t": round(nw_t((top - bot).dropna().values), 2), "ic_pos_year_share": round(float((ic.groupby(ic.index.str[:4]).mean() > 0).mean()), 2)}
    res["univariate_dev"] = dict(sorted(uni.items(), key=lambda kv: -abs(kv[1]["ic_t"])))
    res["feature_corr_top"] = {f"{a}|{b}": round(float(c), 2) for (a, b), c in dev[FEATS].rank().corr().where(np.triu(np.ones((len(FEATS),) * 2), 1) > 0).stack().items() if abs(c) > 0.6}
    # ---- B. 每日截面系数与 IC
    dates = sorted(df.trade_date.unique()); betas = {}; ics = {}
    for d, s in df.groupby("trade_date"):
        X = s[Z].values; y = s.y.values - s.y.mean(); n = len(s)
        if n < 40: continue
        betas[d] = np.linalg.solve(X.T @ X + 1e-3 * n * np.eye(len(Z)), X.T @ y)
        yr = pd.Series(y).rank().values; ics[d] = np.array([np.corrcoef(X[:, j], yr)[0, 1] if X[:, j].std() > 0 else 0.0 for j in range(len(Z))])
    Bd = pd.DataFrame(betas, index=FEATS).T.sort_index(); Id = pd.DataFrame(ics, index=FEATS).T.sort_index()
    lo, hi = SPEC["periods"]["final"] if final else SPEC["periods"]["oos"]
    months = sorted({d[:6] for d in dates if lo <= d <= hi}); pred = []
    wlog = {}
    for mth in months:
        start = mth + "01"; tr0 = (pd.Timestamp(start) - pd.DateOffset(months=24)).strftime("%Y%m%d")
        tr = [d for d in Bd.index if tr0 <= d < start][:-1]           # 末日清洗: 标签跨入预测月
        if len(tr) < 400: continue
        wf = Bd.loc[tr].mean().values; wi = Id.loc[tr].mean().values; wlog[mth] = wf
        s = df[df.trade_date.str[:6] == mth].copy()
        s["p_FM"] = s[Z].values @ wf; s["p_ICW"] = s[Z].values @ wi; pred.append(s)
    P = pd.concat(pred)
    W = pd.DataFrame(wlog, index=FEATS).T
    res["fm_weights_bp_per_full_rank"] = {f: {"mean": round(float(W[f].mean()) * 1e4, 1), "min": round(float(W[f].min()) * 1e4, 1), "max": round(float(W[f].max()) * 1e4, 1),
                                               "t_full_dev": round(nw_t(Bd.loc[Bd.index <= SPEC['periods']['dev'][1], f].values), 2)} for f in FEATS}
    # ---- C. 组合
    cost = SPEC["cost_round_trip"]; alld = [d for d in dates if lo <= d <= hi and d[:6] in wlog]
    def port(model, uni_col, k=5, gate=None):
        s = P if uni_col is None else P[P[uni_col]]
        if gate: s = s[s.breadth_1445 >= gate]
        top = s.sort_values(["trade_date", "p_" + model], ascending=[True, False]).groupby("trade_date").head(k)
        slot_g = np.where(top.filled, top.y_fill, 0.0); slot_n = np.where(top.filled, top.y_fill - cost, 0.0)
        g = pd.Series(slot_g, index=top.trade_date).groupby(level=0).sum().div(k).reindex(alld).fillna(0.0)
        n_ = pd.Series(slot_n, index=top.trade_date).groupby(level=0).sum().div(k).reindex(alld).fillna(0.0)
        f = top[top.filled]
        o = {"days": len(alld), "active_days": int((top.groupby("trade_date").size() > 0).sum()), "fills": int(len(f)), "fill_rate": round(float(top.filled.mean()), 3),
             "gross_bp_per_day": round(float(g.mean()) * 1e4, 1), "gross_t": round(nw_t(g.values), 2),
             "net_bp_per_day": round(float(n_.mean()) * 1e4, 1), "net_t": round(nw_t(n_.values), 2), "net_lcb95_bp": round(block_lcb(n_.values) * 1e4, 1),
             "trade_gross_bp": round(float(f.y_fill.mean()) * 1e4, 1), "trade_win_net": round(float((f.y_fill > cost).mean()), 3),
             "seg_bp": {k2: round(float(f[k2].mean()) * 1e4, 1) for k2 in ["seg1n", "seg2", "seg3"]},
             "ES95_trade_pct": round(float(f.y_fill[f.y_fill <= f.y_fill.quantile(0.05)].mean()) * 100, 2), "worst_day_pct": round(float(n_.min()) * 100, 2),
             "by_year_net_bp": {y: round(float(v.mean()) * 1e4, 1) for y, v in n_.groupby(n_.index.str[:4])}}
        return o
    cells = {}
    for model in ["FM", "ICW"]:
        for un, uc in [("U_liquid", "U"), ("broad", None)]:
            cells[f"{model}|{un}|top5"] = port(model, uc)
            cells[f"{model}|{un}|top5|breadth>=0.6"] = port(model, uc, gate=0.6)
    cells["FM|U_liquid|top1"] = port("FM", "U", k=1); cells["FM|broad|top20"] = port("FM", None, k=20)
    res["portfolio"] = cells
    # 基准: 同宇宙随机(=全体均值)
    res["baseline_universe_gross_bp"] = {"U_liquid": round(float(P[P.U].groupby("trade_date").y.mean().mean()) * 1e4, 1), "broad": round(float(P.groupby("trade_date").y.mean().mean()) * 1e4, 1)}
    # ---- D. 预测分位校准
    cal = {}
    for un, s in [("U_liquid", P[P.U]), ("broad", P)]:
        q = s.groupby("trade_date").p_FM.rank(pct=True); bins = pd.cut(q, [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0])
        cal[un] = {str(k): {"n": int(len(v)), "gross_bp": round(float(v.groupby("trade_date").y.mean().mean()) * 1e4, 1), "overnight_bp": round(float(v.seg2.mean()) * 1e4, 1), "win": round(float((v.y > cost).mean()), 3)}
                   for k, v in s.groupby(bins, observed=True)}
        icd = s.groupby("trade_date").apply(lambda x: x.p_FM.rank().corr(x.y.rank()))
        cal[un]["model_rank_ic"] = round(float(icd.mean()), 4); cal[un]["model_ic_t"] = round(nw_t(icd.values), 2)
    res["calibration_FM"] = cal
    name = "overnight_factor_model_final.json" if final else "overnight_factor_model_oos.json"
    (OUT / name).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    pr = {k: res[k] for k in ["sample", "baseline_universe_gross_bp"]}; print(SPEC["spec_hash"], "final" if final else "oos"); print(pr)
    if not final:
        print("\n== 单因子(开发期) =="); [print(f"{f:16s}", v) for f, v in res["univariate_dev"].items()]
        print("corr>0.6:", res["feature_corr_top"])
    print("\n== FM权重(bp/满秩差) =="); [print(f"{f:16s}", v) for f, v in sorted(res["fm_weights_bp_per_full_rank"].items(), key=lambda kv: -abs(kv[1]["mean"]))]
    print("\n== 组合 =="); [print(k, "\n   ", v) for k, v in cells.items()]
    print("\n== 校准 =="); [print(un, v) for un, v in cal.items()]

if __name__ == "__main__":
    main()
