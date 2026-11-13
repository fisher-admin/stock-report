#!/usr/bin/env python3
"""阶段 3: A+B+C+D+E 合成与定权(research-only, 2026-11-13)。设计在跑数前写定(SPEC)。

思路: 直接预测每个股票日的"绝对"三段毛收益 y(不做截面去均值), 让模型同时学到择日(市场级变量)与选股(个股变量)以及两者的交互。
  当日开仓规则: 只买预测值 > 成本 + 安全边际 的股票, 至多 K 只, 等权槽位, 未成交留现金。
模型:
  L0 基准   截面 ridge(去均值目标)滚动 24 个月 + A 族 Rb 开仓日  (= 第九节模型 + 择日)
  G1 主模型 LightGBM 回归, 目标 y(缩尾 ±6%), 特征 = 个股 C/D/E/原 25 因子(截面秩) + B 事件标记 + A 族市场变量(原值), 逐月扩展窗口重训
  G2 对照   LightGBM 二分类, 目标 1[y > 成本], 输出概率; 开仓 = 概率 > 0.5 的前 K
样本段: 训练起点 2022-01; 主评估 2024-01 至 2025-12(滚动样本外); 2026H1 仅参考; 2026-07 之后与留出 800 均不读取。
关卡(阶段 3): 主评估段前 K 组合扣费后日均净收益的单侧 95% 置信下界 > 0。
"""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, FEATS, OUT  # noqa: E402

COST = 0.00192
STOCK_FEATS = FEATS + ["d_amt_share_last30", "d_ret_last30", "d_vwap30_dev", "d_pv_corr_pm", "d_up_amt_share_pm", "d_rv_last30", "d_prev_close_auc", "d_prev_last5_ret",
                       "e_on_mean_120", "e_on_mean_250", "e_id_mean_60", "e_on_id_corr_60", "e_on_pos_share_60",
                       "c_ind_touch_1445", "c_ind_touch_share", "c_ind_up_prev", "c_ind_height_prev", "c_self_up_prev", "c_self_height_prev", "c_lag_vs_leaders"]
EVENT_FLAGS = ["B1b_report_eve_pos_forecast", "B1c_report_eve_neg_forecast", "B1a_report_eve_all", "B2a_exdiv_eve_all", "B4a_pos_forecast_lastnight", "B4b_repurchase_plan_lastnight", "B4c_holder_increase_lastnight"]
DAY_FEATS = ["breadth_1445", "mkt_ret_cut", "mkt_gap", "mkt_ret_open", "mkt_clv", "mkt_vwap_dev", "mkt_last5_share", "mkt_rv", "xs_disp", "lim_touched_share", "lim_locked_share",
             "ybar_lag1", "mkt_ret_5d", "mkt_ret_20d", "mkt_vol_20", "pre_holiday", "month_end", "is_mon", "is_fri", "basis_IF_lag1", "basis_IC_lag1", "spx_ret_prev", "hsi_ret_lag1", "c_mkt_touch_1445"]
SPEC = {"spec_id": "overnight_stage3_v0", "stock_feats": STOCK_FEATS, "event_flags": EVENT_FLAGS, "day_feats": DAY_FEATS,
        "lgb_params": {"objective": "regression", "learning_rate": 0.03, "num_leaves": 15, "min_data_in_leaf": 400, "feature_fraction": 0.7, "bagging_fraction": 0.7,
                       "bagging_freq": 1, "lambda_l2": 10.0, "num_boost_round": 300, "seed": 20261113},
        "target": "y clipped to [-6%, +6%] (G1); 1[y>cost] (G2)", "refit": "monthly expanding window from 2022-01, purge last train day",
        "portfolio": {"K": [3, 5, 10], "open_rule_G1": "pred > cost + margin, margin in {0, 10bp, 20bp}", "open_rule_G2": "prob > 0.5/0.55", "slot": "equal 1/K, unfilled/unused slot = cash"},
        "periods": {"train_start": "20220104", "main": ["20240101", "20251231"], "reference": ["20260101", "20260701"]},
        "gate": "main 段 net LCB95 > 0 (block bootstrap 5d)", "cost": COST}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

def build():
    w = pd.read_parquet(OUT / "frame_wide.parquet"); w = w[w.elig].copy()
    w = w.merge(pd.read_parquet(OUT / "frame_cde.parquet"), on=["ts_code", "trade_date"], how="left")
    D = pd.read_parquet(OUT / "day_frame.parquet")
    for c in DAY_FEATS:
        if c not in D: D[c] = np.nan
    if "c_mkt_touch_1445" in w: D["c_mkt_touch_1445"] = w.groupby("trade_date").c_mkt_touch_1445.first()
    w = w.join(D[[c for c in DAY_FEATS if c != "breadth_1445" or True]].add_prefix("day_"), on="trade_date")
    ev = None
    fl = pd.read_parquet(OUT / "event_flags.parquet") if (OUT / "event_flags.parquet").exists() else None
    if fl is not None: w = w.merge(fl, on=["ts_code", "trade_date"], how="left")
    for e in EVENT_FLAGS:
        w[e] = w[e].fillna(0.0) if e in w else 0.0
    g = w.groupby("trade_date")
    for c in STOCK_FEATS:
        if c not in w: w[c] = np.nan
        w["z_" + c] = g[c].rank(pct=True) - 0.5
    w["z_e_tug_60"] = g.on_mean_60.rank(pct=True) - g.e_id_mean_60.rank(pct=True)
    return w, ev

def portfolio(P, col, rule, K, lo, hi):
    s = P[(P.trade_date >= lo) & (P.trade_date <= hi)]; days = sorted(s.trade_date.unique())
    c = s[rule(s)].sort_values(["trade_date", col], ascending=[True, False]).groupby("trade_date").head(K)
    gs = np.where(c.filled, c.y_fill, 0.0); ns = np.where(c.filled, c.y_fill - COST, 0.0)
    g = pd.Series(gs, index=c.trade_date).groupby(level=0).sum().div(K).reindex(days).fillna(0.0)
    n = pd.Series(ns, index=c.trade_date).groupby(level=0).sum().div(K).reindex(days).fillna(0.0)
    f = c[c.filled]
    o = {"days": len(days), "active_days": int(c.trade_date.nunique()), "trades": int(len(f)), "net_bp_per_day": round(float(n.mean()) * 1e4, 2),
         "net_t": round(nw_t(n.values), 2), "net_lcb95_bp": round(block_lcb(n.values) * 1e4, 2)}
    if len(f):
        o.update({"trade_gross_bp": round(float(f.y_fill.mean()) * 1e4, 1), "trade_win_net": round(float((f.y_fill > COST).mean()), 3),
                  "avg_win_bp": round(float((f.y_fill - COST)[f.y_fill > COST].mean()) * 1e4, 1) if (f.y_fill > COST).any() else None,
                  "avg_loss_bp": round(float((f.y_fill - COST)[f.y_fill <= COST].mean()) * 1e4, 1) if (f.y_fill <= COST).any() else None,
                  "seg_bp": [round(float(f[k].mean()) * 1e4, 1) for k in ("seg1n", "seg2", "seg3")],
                  "by_year": {y: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for y, v in f.groupby(f.trade_date.str[:4]).y_fill}})
    return o

def main():
    import lightgbm as lgb
    w, _ = build()
    Z = ["z_" + c for c in STOCK_FEATS] + ["z_e_tug_60"]; DF = ["day_" + c for c in DAY_FEATS]; X = Z + EVENT_FLAGS + DF
    prm = dict(SPEC["lgb_params"]); nround = prm.pop("num_boost_round"); prm["verbose"] = -1; prm["num_threads"] = 8
    months = sorted({d[:6] for d in w.trade_date.unique() if d >= SPEC["periods"]["main"][0] and d <= SPEC["periods"]["reference"][1]})
    preds = []; imp = []
    for m in months:
        tr = w[w.trade_date < m + "01"]; last = tr.trade_date.max(); tr = tr[tr.trade_date < last]
        te = w[w.trade_date.str[:6] == m].copy()
        yr = tr.y.clip(-0.06, 0.06)
        b1 = lgb.train(prm, lgb.Dataset(tr[X].values.astype(np.float32), yr.values), nround)
        p2 = dict(prm, objective="binary"); b2 = lgb.train(p2, lgb.Dataset(tr[X].values.astype(np.float32), (tr.y > COST).astype(float).values), nround)
        te["p_G1"] = b1.predict(te[X].values.astype(np.float32)); te["p_G2"] = b2.predict(te[X].values.astype(np.float32))
        # L0: 截面 ridge(24 个月) on Z
        tr24 = tr[tr.trade_date >= (pd.Timestamp(m + "01") - pd.DateOffset(months=24)).strftime("%Y%m%d")]
        bs = []
        for _, s in tr24.groupby("trade_date"):
            Xs = s[Z].fillna(0).values; ys = s.y.values - s.y.mean(); n = len(s)
            if n >= 40: bs.append(np.linalg.solve(Xs.T @ Xs + 1e-3 * n * np.eye(len(Z)), Xs.T @ ys))
        te["p_L0"] = te[Z].fillna(0).values @ np.mean(bs, axis=0)
        imp.append(pd.Series(b1.feature_importance("gain"), index=X, name=m))
        preds.append(te); print(m, len(tr), len(te), flush=True)
    P = pd.concat(preds)
    R = pd.read_parquet(OUT / "regime_filter.parquet"); Rb = set(R.index[R.p_M1_breadth >= R.q75_M1_breadth]); Ra = set(R.index[R.p_M5_rank_inloop_top5 > 0])
    res = {"spec": SPEC, "cells": {}}
    for seg, (lo, hi) in [("main_2024_25", SPEC["periods"]["main"]), ("reference_2026H1", SPEC["periods"]["reference"])]:
        cells = {}
        for K in SPEC["portfolio"]["K"]:
            for mg in (0.0, 0.0010, 0.0020):
                cells[f"G1|K{K}|pred>cost+{int(mg*1e4)}bp"] = portfolio(P, "p_G1", lambda s, mg=mg: s.p_G1 > COST + mg, K, lo, hi)
            for th in (0.5, 0.55):
                cells[f"G2|K{K}|prob>{th}"] = portfolio(P, "p_G2", lambda s, th=th: s.p_G2 > th, K, lo, hi)
            cells[f"L0|K{K}|Rb"] = portfolio(P, "p_L0", lambda s: s.trade_date.isin(Rb), K, lo, hi)
            cells[f"L0|K{K}|Ra"] = portfolio(P, "p_L0", lambda s: s.trade_date.isin(Ra), K, lo, hi)
            cells[f"G1|K{K}|always_topK"] = portfolio(P, "p_G1", lambda s: s.p_G1 > -1, K, lo, hi)
        res["cells"][seg] = cells
    # 预测值校准(主段): 按 G1 预测分箱的实现均值
    m = P[(P.trade_date >= SPEC["periods"]["main"][0]) & (P.trade_date <= SPEC["periods"]["main"][1])]
    bins = [-1, -0.002, 0, 0.001, 0.0019, 0.003, 0.005, 1]
    res["calibration_G1_main"] = {str(k): {"n": int(len(v)), "days": int(v.trade_date.nunique()), "real_gross_bp": round(float(v.y.mean()) * 1e4, 1), "win_net": round(float((v.y > COST).mean()), 3)}
                                  for k, v in m.groupby(pd.cut(m.p_G1, bins), observed=True)}
    pb = [0, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 1]
    res["calibration_G2_main"] = {str(k): {"n": int(len(v)), "real_gross_bp": round(float(v.y.mean()) * 1e4, 1), "win_net": round(float((v.y > COST).mean()), 3)}
                                  for k, v in m.groupby(pd.cut(m.p_G2, pb), observed=True)}
    ic = m.groupby("trade_date").apply(lambda s: s.p_G1.rank().corr(s.y.rank())); res["G1_rank_ic_main"] = [round(float(ic.mean()), 4), round(nw_t(ic.values), 2)]
    I = pd.concat(imp, axis=1).mean(axis=1).sort_values(ascending=False); res["G1_gain_importance_top25"] = {k: round(float(v / I.sum()), 4) for k, v in I.head(25).items()}
    P[["ts_code", "trade_date", "p_G1", "p_G2", "p_L0", "y", "y_fill", "filled"]].to_parquet(OUT / "stage3_predictions.parquet")
    (OUT / "stage3_model.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    print(SPEC["spec_hash"])
    for seg, cells in res["cells"].items():
        print("\n==", seg)
        for k, v in cells.items(): print(f"{k:28s}", {kk: v.get(kk) for kk in ("active_days", "trades", "trade_gross_bp", "trade_win_net", "net_bp_per_day", "net_t", "net_lcb95_bp", "by_year")})
    print("\ncal G1", json.dumps(res["calibration_G1_main"], ensure_ascii=False)); print("cal G2", json.dumps(res["calibration_G2_main"], ensure_ascii=False))
    print("IC", res["G1_rank_ic_main"]); print("imp", res["G1_gain_importance_top25"])

if __name__ == "__main__":
    main()
