#!/usr/bin/env python3
"""A 族: 择日层(research-only, 2026-11-12)。规范见 RESEARCH_SPEC_stage2.md。

问题: T 日 14:45 之前可知的市场级变量, 能否事前识别"今晚全股池隔夜毛收益高于成本"的日子?
评估只认时间序列滚动样本外(逐月扩展窗口), 主评估段 2024-2025; 2026H1 仅作参考。
产出: outputs/overnight_v0/{frame_wide.parquet, day_frame.parquet, day_timing.json, regime_filter.parquet}
"""
from __future__ import annotations
import glob, hashlib, json, os, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow.parquet as pq
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import load_stock, nw_t, block_lcb, FEATS, MIN_DIR, DAILY_DIR, PANEL, OUT  # noqa: E402

EV = Path(os.environ.get("OVN_EVENTS") or OUT / "events")
BOARDS = ("主板", "创业板")
COST = 0.00192
SPEC = {
    "spec_id": "overnight_day_timing_v0",
    "universe_boards": "沪深主板 + 创业板(2026-11-13 fisher 指定; 不含科创板/北交所); 涨跌停幅度: 创业板 20%, 主板 10%, 主板 ST 5%",
    "target": "ybar = 当日股池(研究800 中的主板+创业板, ADV20>=1亿, 14:40-14:45 成交>=300万, 未封板)等权三段合计毛收益(y_fill 口径不用, 用 y)",
    "screen_period": ["20220104", "20231231"], "wf_main": ["20240101", "20251231"], "wf_reference": ["20260101", "20260701"],
    "models": {"M1_breadth": "ybar ~ breadth_1445", "M2_inloop_top5": "每个训练窗内按 |corr t| 取前5, OLS",
               "M3_fixed_screen": "2022-2023 初筛 |t|>=3 的变量(至多5个, 按|t|), OLS",
               "M4_fixed_screen_t2": "2022-2023 初筛 |t|>=2 的变量(至多5个), OLS; 仅在 M3 变量不足2个时作为补充",
               "M5_rank_inloop_top5": "尝试2(首轮发现皮尔逊相关被极端日主导后追加): 变量换成相对训练窗的分位秩, 目标缩尾到训练窗 2%/98%, 训练窗内按 |spearman| 取前5, OLS",
               "M6_rank_fixed": "同 M5 的变换; 变量固定为 2022-2023 上 spearman |t|>=2 的前5个"},
    "open_rule": "pred > cost(19.2bp); 备选: pred >= 训练期预测值的 75 分位",
    "refit": "monthly expanding window, train = all days strictly before month start", "max_vars": 5, "cost": COST,
}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

def build_wide(live=None):
    """live=None: 研究口径(原样)。live={"date": T, "next": 下一交易日, "codes": [...], "map": 可选并行 map}: 前瞻影子口径,
    不读写缓存, 保留尚无次日数据的 T 日行(标签为空), T 日合格条件不要求标签。"""
    cache = OUT / "frame_wide.parquet"
    if live is None and cache.exists(): return pd.read_parquet(cache)
    pan = pq.read_table(PANEL, columns=["ts_code", "trade_date", "name", "market_board", "avg_amount_lookback", "float_share", "circ_mv", "sw2021_l1"]).to_pandas()
    pan = pan[pan.market_board.isin(BOARDS)].sort_values(["ts_code", "trade_date"])
    for c in ["float_share", "circ_mv"]: pan[c + "_lag"] = pan.groupby("ts_code")[c].shift(1)
    codes = sorted(os.path.basename(f)[12:-8].replace("_", ".") for f in glob.glob(str(MIN_DIR / "minute_5min_*.parquet")))
    codes = [c for c in codes if c[:2] in ("60", "00", "30") and (DAILY_DIR / f"{c}.parquet").exists()]   # 沪深主板 + 创业板; 不含科创板 688、北交所
    if live is not None: codes = list(live["codes"])
    df = pd.concat([x for x in (live or {}).get("map", map)(load_stock, codes) if x is not None], ignore_index=True)
    df = df.merge(pan, on=["ts_code", "trade_date"], how="inner")
    cal = sorted(df.trade_date.unique()); nxt = dict(zip(cal[:-1], cal[1:]))
    if live is not None: nxt[live["date"]] = live["next"]
    is_live = (df.trade_date == live["date"]) if live is not None else pd.Series(False, index=df.index)
    df["mkt_next"] = df.trade_date.map(nxt); keep_rows = (df.mkt_next.notna() & df.n_date.notna()) | is_live
    is_live = is_live[keep_rows]; df = df[keep_rows].copy()
    df["gap_days"] = (pd.to_datetime(df.mkt_next) - pd.to_datetime(df.trade_date)).dt.days
    df["ret_cut"] = df.px / df.pre_close - 1; df["ret_open"] = df.px / df.open - 1
    rg = df.hi - df.lo; df["clv"] = ((df.px - df.lo) / rg).where(rg > 0)
    df["vwap_dev"] = df.px / df.vwap_1445 - 1; df["range_1445"] = rg / df.pre_close
    df["turn_cut"] = df.cum_vol / (df.float_share_lag * 1e4); df["last5_amt_share"] = df.amt_last5 / df.cum_amt
    df["lim_pct"] = np.where(df.market_board == "创业板", 0.20, np.where(df.name.astype(str).str.contains("ST"), 0.05, 0.10))
    df["lim_up"] = (df.pre_close * (1 + df.lim_pct) + 1e-9).round(2); df["lim_dn"] = (df.pre_close * (1 - df.lim_pct) + 1e-9).round(2)
    df["locked"] = df.px >= df.lim_up - 1e-6; df["locked_dn"] = df.px <= df.lim_dn + 1e-6
    df["touched_limit"] = (df.hi >= df.lim_up - 1e-6).astype(float)
    df["log_adv20"] = np.log(df.avg_amount_lookback.clip(lower=1)); df["log_circ_mv"] = np.log(df.circ_mv_lag); df["log_px"] = np.log(df.px)
    df["sector_rel_ret"] = df.ret_cut - df.groupby(["trade_date", "sw2021_l1"]).ret_cut.transform("mean")
    susp = df.n_date != df.mkt_next
    seg3 = np.where(susp | df.x_vwap.isna(), df.n_close / df.n_open - 1, df.x_vwap / df.n_open - 1)
    df["seg1n"] = df.close / df.entry_vwap - 1; df["seg2"] = df.n_open / df.n_pre_close - 1; df["seg3"] = seg3
    df["y"] = ((1 + df.seg1n) * (1 + df.seg2) * (1 + df.seg3) - 1).clip(-0.12, 0.12)
    cap = df.px * 1.003; df["filled"] = df.entry_lo <= cap
    df["y_fill"] = (df.close / np.minimum(df.entry_vwap, cap)) * (1 + df.seg2) * (1 + df.seg3) - 1
    df["c2c"] = df.close / df.pre_close - 1
    df["elig"] = (df.avg_amount_lookback >= 1e5) & (df.amt_last5 >= 3e6) & (~df.locked) & (df.y.notna() | is_live) & df.px.notna()
    df["U"] = df.elig & (df.avg_amount_lookback >= 3e5) & (df.amt_last5 >= 1.5e7)
    keep = ["ts_code", "trade_date", "market_board", "lim_pct", "mkt_next", "gap_days", "sw2021_l1", "y", "y_fill", "filled", "seg1n", "seg2", "seg3", "elig", "U", "locked", "locked_dn",
            "c2c", "avg_amount_lookback", "amt_last5"] + FEATS
    if live is not None: return df[keep]
    df[keep].to_parquet(cache); return df[keep]

def build_days(w):
    g = w.groupby("trade_date"); e = w[w.elig].groupby("trade_date"); u = w[w.U].groupby("trade_date")
    D = pd.DataFrame({"n_all": g.size(), "n_elig": e.size(), "ybar": e.y.mean(), "ybar_U": u.y.mean(), "seg2bar": e.seg2.mean(), "gap_days": g.gap_days.first()})
    D["breadth_1445"] = g.ret_cut.apply(lambda s: (s > 0).sum() / max(((s > 0) | (s < 0)).sum(), 1))
    for c, n in [("ret_cut", "mkt_ret_cut"), ("gap_today", "mkt_gap"), ("ret_open", "mkt_ret_open"), ("ret_pm", "mkt_ret_pm"),
                 ("ret_tail45", "mkt_ret_tail45"), ("ret_last5", "mkt_ret_last5"), ("clv", "mkt_clv"), ("vwap_dev", "mkt_vwap_dev")]:
        D[n] = g[c].mean()
    D["xs_disp"] = g.ret_cut.std(); D["mkt_rv"] = g.rv.median(); D["mkt_last5_share"] = g.last5_amt_share.median(); D["mkt_range"] = g.range_1445.median()
    D["lim_locked_share"] = g.locked.mean(); D["lim_touched_share"] = g.touched_limit.mean(); D["lim_dn_share"] = g.locked_dn.mean()
    D["seal_rate"] = (g.locked.sum() / g.touched_limit.sum().where(g.touched_limit.sum() > 0)).fillna(0.0)
    D["strong_share"] = g.ret_cut.apply(lambda s: (s > 0.05).mean()); D["weak_share"] = g.ret_cut.apply(lambda s: (s < -0.05).mean())
    D = D.sort_index()
    # 滞后的市场隔夜统计(今日跳空在 09:30 已知; 昨日标签在今日 09:40 已实现)
    D["on_mkt_ma5"] = D.mkt_gap.rolling(5).mean(); D["on_mkt_ma20"] = D.mkt_gap.rolling(20).mean()
    D["ybar_lag1"] = D.ybar.shift(1); D["ybar_ma5_lag"] = D.ybar.shift(1).rolling(5).mean(); D["ybar_ma20_lag"] = D.ybar.shift(1).rolling(20).mean()
    c2c = g.c2c.mean().sort_index()
    D["mkt_ret_lag1"] = c2c.shift(1); D["mkt_ret_5d"] = c2c.shift(1).rolling(5).sum(); D["mkt_ret_20d"] = c2c.shift(1).rolling(20).sum()
    D["mkt_vol_20"] = c2c.shift(1).rolling(20).std(); D["breadth_lag1"] = D.breadth_1445.shift(1)
    # 日历
    dt = pd.to_datetime(D.index); D["is_mon"] = (dt.dayofweek == 0).astype(float); D["is_fri"] = (dt.dayofweek == 4).astype(float)
    D["pre_holiday"] = (D.gap_days > 3).astype(float)
    mon = pd.Series(D.index.str[:6], index=D.index); D["month_end"] = (mon != mon.shift(-1)).astype(float); D["month_start"] = (mon != mon.shift(1)).astype(float)
    # 指数/期货/外盘: 只用 T-1 及更早的收盘值
    if (EV / "index_daily.parquet").exists():
        ix = pd.read_parquet(EV / "index_daily.parquet").astype({"trade_date": str}); fu = pd.read_parquet(EV / "fut_daily.parquet").astype({"trade_date": str})
        px = ix.pivot_table(index="trade_date", columns="ts_code", values="close").sort_index(); fp = fu.pivot_table(index="trade_date", columns="ts_code", values="close").sort_index()
        for f, i, n in [("IF.CFX", "000300.SH", "basis_IF_lag1"), ("IC.CFX", "000905.SH", "basis_IC_lag1")]:
            b = (fp[f] / px[i] - 1).dropna(); D[n] = b.shift(1).reindex(D.index); D[n.replace("lag1", "chg_lag1")] = b.diff().shift(1).reindex(D.index)
        r = px["000852.SH"].pct_change() - px["000300.SH"].pct_change(); D["small_minus_large_5d"] = r.rolling(5).sum().shift(1).reindex(D.index)
        gl = pd.read_parquet(EV / "index_global.parquet").astype({"trade_date": str}).drop_duplicates(["ts_code", "trade_date"]) if (EV / "index_global.parquet").exists() else pd.DataFrame(columns=["ts_code", "trade_date", "pct_chg"])
        for code, n in [("SPX", "spx_ret_prev"), ("IXIC", "ixic_ret_prev"), ("HSI", "hsi_ret_lag1"), ("XIN9", "xin9_ret_lag1")]:
            s = gl[gl.ts_code == code].set_index("trade_date").sort_index().pct_chg.astype(float) / 100
            if len(s) < 100: continue
            # 取交易日期严格早于 T 的最近一根(美股 D 日收盘 = 北京 D+1 凌晨, 早于 T 开盘)
            pos = np.searchsorted(s.index.values, D.index.values, side="left") - 1
            D[n] = np.where(pos >= 0, s.values[np.clip(pos, 0, len(s) - 1)], np.nan)
    return D

def tcorr(x, y):
    m = x.notna() & y.notna(); n = int(m.sum())
    if n < 60 or x[m].std() == 0: return 0.0, 0.0
    r = float(np.corrcoef(x[m], y[m])[0, 1]); return r, r * np.sqrt((n - 2) / max(1 - r * r, 1e-9))

def ols_fit(X, y):
    mu = X.mean(0); sd = X.std(0); sd[sd == 0] = 1; Z = np.column_stack([np.ones(len(X)), (X - mu) / sd])
    b = np.linalg.lstsq(Z, y, rcond=None)[0]; return mu, sd, b

def evaluate(D, flag, lo, hi, col="ybar"):
    s = D[(D.index >= lo) & (D.index <= hi)]; op = s[flag.reindex(s.index).fillna(False).values]
    daily_net = pd.Series(0.0, index=s.index); daily_net.loc[op.index] = op[col] - COST
    o = {"days": int(len(s)), "open_days": int(len(op)), "open_share": round(len(op) / max(len(s), 1), 3)}
    if len(op) >= 5:
        o.update({"open_gross_bp": round(float(op[col].mean()) * 1e4, 1), "open_gross_t": round(nw_t(op[col].values), 2) if len(op) >= 20 else None,
                  "open_day_hit_gt_cost": round(float((op[col] > COST).mean()), 3), "closed_gross_bp": round(float(s.drop(op.index)[col].mean()) * 1e4, 1) if len(op) < len(s) else None,
                  "open_U_gross_bp": round(float(op.ybar_U.mean()) * 1e4, 1), "open_seg2_bp": round(float(op.seg2bar.mean()) * 1e4, 1),
                  "net_bp_per_calendar_day": round(float(daily_net.mean()) * 1e4, 2), "net_lcb95_bp_per_calendar_day": round(block_lcb(daily_net.values) * 1e4, 2) if len(s) >= 60 else None,
                  "open_net_lcb95_bp": round(block_lcb((op[col] - COST).values) * 1e4, 1) if len(op) >= 60 else None,
                  "by_year_open_gross_bp": {y: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for y, v in op[col].groupby(op.index.str[:4])}})
    return o

def main():
    w = build_wide()
    sub = os.environ.get("OVN_DAYFEAT_CODES")   # 市场级变量只用这些股票计算(时间留出集用研究 800, 与训练口径一致)
    D = build_days(w[w.ts_code.isin(set(Path(sub).read_text().split()))] if sub else w); D.to_parquet(OUT / "day_frame.parquet")
    if "--build-only" in sys.argv: print("built", w.shape, D.shape); return
    cands = [c for c in D.columns if c not in ("n_all", "n_elig", "ybar", "ybar_U", "seg2bar", "gap_days")]
    res = {"spec": SPEC, "sample": {"days": int(len(D)), "first": D.index[0], "last": D.index[-1], "ybar_bp": round(float(D.ybar.mean()) * 1e4, 1), "ybar_sd_bp": round(float(D.ybar.std()) * 1e4, 1),
                                    "share_days_gt_cost": round(float((D.ybar > COST).mean()), 3), "ybar_ac1": round(float(D.ybar.autocorr(1)), 3), "candidates": cands}}
    # ---- 1. 初筛(仅 2022-2023) + 各段单变量(披露用, 不用于选变量)
    scr = D[D.index <= SPEC["screen_period"][1]]; uni = {}
    for c in cands:
        r, t = tcorr(scr[c], scr.ybar); row = {"corr_2022_23": round(r, 3), "t_2022_23": round(t, 2)}
        rs, ts = tcorr(scr[c].rank(), scr.ybar.rank()); row["spearman_2022_23"] = round(rs, 3); row["spearman_t_2022_23"] = round(ts, 2)
        row["spearman_by_year"] = {y: round(float(g[c].corr(g.ybar, method="spearman")), 2) for y, g in D.groupby(D.index.str[:4])}
        for nm, (a, b) in [("2024_25", SPEC["wf_main"]), ("2026H1", SPEC["wf_reference"])]:
            s = D[(D.index >= a) & (D.index <= b)]; r2, t2 = tcorr(s[c], s.ybar); row[f"corr_{nm}"] = round(r2, 3); row[f"t_{nm}"] = round(t2, 2)
        # 多头端: 该变量处于(按 2022-23 相关符号定向后)训练期前 25% 的日子的 ybar
        sgn = 1 if r >= 0 else -1; thr = (sgn * scr[c]).quantile(0.75); top = scr[(sgn * scr[c]) >= thr]
        row["top25_ybar_bp_2022_23"] = round(float(top.ybar.mean()) * 1e4, 1) if len(top) else None
        mn = D[(D.index >= SPEC["wf_main"][0]) & (D.index <= SPEC["wf_main"][1])]; top2 = mn[(sgn * mn[c]) >= thr]
        row["top25_ybar_bp_2024_25_fixed_thr"] = [int(len(top2)), round(float(top2.ybar.mean()) * 1e4, 1) if len(top2) else None]
        uni[c] = row
    res["univariate"] = dict(sorted(uni.items(), key=lambda kv: -abs(kv[1]["t_2022_23"])))
    ranked = [c for c, v in res["univariate"].items()]
    fix3 = [c for c in ranked if abs(uni[c]["t_2022_23"]) >= 3.0][:5]; fix2 = [c for c in ranked if abs(uni[c]["t_2022_23"]) >= 2.0][:5]
    rk = sorted(cands, key=lambda c: -abs(uni[c]["spearman_t_2022_23"])); fix6 = [c for c in rk if abs(uni[c]["spearman_t_2022_23"]) >= 2.0][:5]
    res["fixed_sets"] = {"M3": fix3, "M4": fix2, "M6": fix6}
    # ---- 2. 逐月扩展窗口滚动
    months = sorted({d[:6] for d in D.index if d >= SPEC["wf_main"][0]}); P = []; sel_log = {}; sel_log5 = {}
    for mth in months:
        tr = D[D.index < mth + "01"]; te = D[D.index.str[:6] == mth].copy()
        sets = {"M1_breadth": ["breadth_1445"], "M3_fixed_screen": fix3, "M4_fixed_screen_t2": fix2}
        tt = {c: abs(tcorr(tr[c], tr.ybar)[1]) for c in cands}; sets["M2_inloop_top5"] = sorted(tt, key=lambda c: -tt[c])[:5]; sel_log[mth] = sets["M2_inloop_top5"]
        for nm, vs in sets.items():
            if not vs: te["p_" + nm] = np.nan; te["q75_" + nm] = np.nan; continue
            t2 = tr.dropna(subset=vs + ["ybar"]); mu, sd, b = ols_fit(t2[vs].values, t2.ybar.values)
            f = lambda X: np.column_stack([np.ones(len(X)), (np.nan_to_num(X, nan=0.0) - mu) / sd]) @ b  # noqa: E731
            Xte = te[vs].values.copy(); Xte = np.where(np.isnan(Xte), mu, Xte)
            te["p_" + nm] = f(Xte); te["q75_" + nm] = np.quantile(f(t2[vs].values), 0.75)
        ts_ = {c: abs(tcorr(tr[c].rank(), tr.ybar.rank())[1]) for c in cands}; rsets = {"M5_rank_inloop_top5": sorted(ts_, key=lambda c: -ts_[c])[:5], "M6_rank_fixed": fix6}
        sel_log5[mth] = rsets["M5_rank_inloop_top5"]
        for nm, vs in rsets.items():
            if not vs: te["p_" + nm] = np.nan; te["q75_" + nm] = np.nan; continue
            t2 = tr.dropna(subset=["ybar"]); lo_, hi_ = t2.ybar.quantile([0.02, 0.98]); yw = t2.ybar.clip(lo_, hi_).values
            def pr(col, x):  # 相对训练窗的分位秩; 缺失 -> 0.5
                ref = np.sort(t2[col].dropna().values); q = np.searchsorted(ref, x, side="right") / max(len(ref), 1); return np.where(np.isnan(x), 0.5, q)
            Xtr = np.column_stack([pr(c, t2[c].values) for c in vs]); Xte = np.column_stack([pr(c, te[c].values) for c in vs])
            Ztr = np.column_stack([np.ones(len(Xtr)), Xtr]); bb = np.linalg.lstsq(Ztr, yw, rcond=None)[0]
            te["p_" + nm] = np.column_stack([np.ones(len(Xte)), Xte]) @ bb; te["q75_" + nm] = np.quantile(Ztr @ bb, 0.75)
        P.append(te)
    P = pd.concat(P)
    res["M5_selection_frequency"] = pd.Series([c for v in sel_log5.values() for c in v]).value_counts().to_dict()
    res["M2_selection_frequency"] = pd.Series([c for v in sel_log.values() for c in v]).value_counts().to_dict()
    out = {}
    for seg, (a, b) in [("wf_main_2024_25", SPEC["wf_main"]), ("wf_reference_2026H1", SPEC["wf_reference"])]:
        s = P[(P.index >= a) & (P.index <= b)]; o = {"M0_always": evaluate(P, pd.Series(True, index=P.index), a, b)}
        for nm in SPEC["models"]:
            if s["p_" + nm].isna().all(): o[nm] = "no variables"; continue
            r, t = tcorr(s["p_" + nm], s.ybar)
            o[nm] = {"pred_corr": round(r, 3), "pred_corr_t": round(t, 2),
                     "open_if_pred_gt_cost": evaluate(P, P["p_" + nm] > COST, a, b), "open_if_pred_ge_train_q75": evaluate(P, P["p_" + nm] >= P["q75_" + nm], a, b),
                     "open_if_pred_gt_0": evaluate(P, P["p_" + nm] > 0, a, b)}
        o["ref_breadth>=0.6(已污染门槛)"] = evaluate(P, P.breadth_1445 >= 0.6, a, b)
        out[seg] = o
    res["walk_forward"] = out
    keep = [c for c in P.columns if c.startswith("p_") or c.startswith("q75_")] + ["ybar", "ybar_U", "breadth_1445"]
    P[keep].to_parquet(OUT / "regime_filter.parquet")
    (OUT / "day_timing.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    print(SPEC["spec_hash"], res["sample"]); print("M5 freq:", res["M5_selection_frequency"])
    print("\n== 单变量(按 2022-23 |t| 排序) =="); [print(f"{c:22s}", v) for c, v in res["univariate"].items()]
    print("\nfixed:", res["fixed_sets"], "\nM2 freq:", res["M2_selection_frequency"])
    for seg, o in out.items():
        print("\n==", seg)
        for nm, v in o.items(): print(nm, json.dumps(v, ensure_ascii=False, default=float))

if __name__ == "__main__":
    main()
