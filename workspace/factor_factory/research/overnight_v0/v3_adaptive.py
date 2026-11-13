#!/usr/bin/env python3
"""v3: 在"纯隔夜模型"选股不变的前提下, 按市场阶段动态选择卖出窗口。

底盘: 14:55 买入; 纯隔夜回归模型预测 > 成本的股票里取前 5(与 v2 的 R|gap 完全相同)。只改"什么时候卖"。
市场阶段信号(14:45 可知, 无未来信息): 每天取模型预测最高 10% 的研究股票("热门组"), 记它们次日 开盘->09:35 / ->10:00 / ->10:30 相对开盘卖的额外收益;
  对买入日 t, S_x(L) = 买入日早于 t 的最近 L 个交易日该额外收益的均值(最后一个对应今天早盘, 14:45 已知)。
策略(先写定, 不调参):
  fixed_*   固定卖出时点(对照)
  A_L       S_1000(L) > 0 则 10:00 卖, 否则开盘卖; L = 10 / 20 / 40
  W_20      滑动窗口: 在 {开盘, 09:35, 10:00, 10:30} 中选 S_x(20) 最大的(开盘 = 0)
  Wm_20     同上, 但要求 S_x(20) > 5bp 才离开开盘卖(留噪声余量)
  M         学习型: 日级 LightGBM(强正则)用 24 个市场变量 + S_x(5/10/20/40) 预测次日热门组 开盘->10:00 的额外收益, 预测 > 0 则 10:00 卖;
            逐月滚动, 至少 12 个月训练, 所以只在 2025 年起有结果
成本余量: 另报开仓门槛 预测 > 成本 + 10bp / + 20bp。
数据集: dev(研究股逐月滚动 2024-2025, 2026H1); h800(留出股, 第 2 次使用); time(2026-07-02..10-08, 第 3 次使用)。后两者只算复核。
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np, pandas as pd, lightgbm as lgb
sys.path.insert(0, str(Path(__file__).parent))
import stage3_model as s3  # noqa: E402
from overnight_factor_model import nw_t, block_lcb, OUT  # noqa: E402
from v2_timing_grid import legs  # noqa: E402
COST = s3.COST; XS = ["x0935", "x1000", "x1030"]; YC = ["y_gap"] + ["y_" + x for x in XS]

def with_legs(P, d):
    T = legs(pd.read_parquet(d / "v2_timing.parquet")); P = P.drop(columns=[c for c in YC if c in P]).merge(T[["ts_code", "trade_date", "a_e1455", "b_xopen"] + ["b_" + x for x in XS]], on=["ts_code", "trade_date"], how="left")
    P["y_gap"] = (1 + P.a_e1455) * (1 + P.b_xopen) - 1
    for x in XS: P["y_" + x] = (1 + P.a_e1455) * (1 + P["b_" + x]) - 1
    return P

def datasets():
    dev = with_legs(pd.read_parquet(OUT / "v2_predictions.parquet").rename(columns={"pR_gap": "p"})[["ts_code", "trade_date", "filled", "p"]], OUT)
    h8 = with_legs(pd.read_parquet(OUT / "holdout_800/v2_wf800_predictions.parquet").rename(columns={"pR_gap": "p"})[["ts_code", "trade_date", "filled", "p"]], OUT / "holdout_800")
    H = OUT / "holdout_time"; s3.OUT = H; w, _ = s3.build(); man = json.loads((OUT / "frozen_v2/manifest.json").read_text())
    w["p"] = lgb.Booster(model_file=str(OUT / "frozen_v2/model_gap.txt")).predict(w[man["features"]].values.astype(np.float32))
    tm = with_legs(w[["ts_code", "trade_date", "filled", "p"]], H); research = set((H / "research_codes.txt").read_text().split())
    return dev, h8, tm, research

def drift_table(R):
    R = R[R.y_gap.notna()].copy(); R["hot"] = R.groupby("trade_date").p.rank(pct=True) >= 0.9; h = R[R.hot]
    return pd.DataFrame({x: ((1 + h["y_" + x]) / (1 + h.y_gap) - 1).groupby(h.trade_date).mean() for x in XS}).sort_index()

def signals(D):
    S = {L: D.rolling(L, min_periods=max(5, L // 2)).mean().shift(1) for L in (5, 10, 20, 40)}   # shift(1): 买入日 t 只用早于 t 的买入日
    pol = pd.DataFrame(index=D.index)
    for L in (10, 20, 40): pol[f"A_{L}"] = np.where(S[L].x1000 > 0, "x1000", "gap")
    s = S[20].assign(gap=0.0); pol["W_20"] = s[["gap"] + XS].idxmax(axis=1).where(s[XS].notna().all(axis=1), "gap")
    s2 = (S[20] - 0.0005).assign(gap=0.0); pol["Wm_20"] = s2[["gap"] + XS].idxmax(axis=1).where(s2[XS].notna().all(axis=1), "gap")
    for x in ["gap"] + XS: pol["fixed_" + x] = x
    F = pd.concat([S[L].add_suffix(f"_L{L}") for L in S], axis=1)
    return pol, F

def learned(D, F):
    dfs = []
    for d in (OUT, OUT / "holdout_time"):
        x = pd.read_parquet(d / "day_frame.parquet"); dfs.append(x[[c for c in s3.DAY_FEATS if c in x]])
    day = pd.concat(dfs); day = day[~day.index.duplicated(keep="first")].sort_index()
    X = F.join(day, how="left"); y = D.x1000; out = pd.Series("gap", index=D.index, dtype=object)
    prm = dict(objective="regression", learning_rate=0.03, num_leaves=4, min_data_in_leaf=30, feature_fraction=0.6, bagging_fraction=0.7, bagging_freq=1, lambda_l2=20.0, verbose=-1, seed=20261113, num_threads=4)
    for m in sorted({d[:6] for d in D.index if d >= "20250101"}):
        tr = X.index[(X.index < m + "01")][:-1]; te = X.index[X.index.str[:6] == m]
        if len(tr) < 200: continue
        b = lgb.train(prm, lgb.Dataset(X.loc[tr].values.astype(np.float32), y.loc[tr].clip(-0.03, 0.03).values), 150)
        out.loc[te] = np.where(b.predict(X.loc[te].values.astype(np.float32)) > 0, "x1000", "gap")
    return out

def port(s, exits, days, margin=0.0, K=5):
    c = s[s.p > COST + margin].sort_values(["trade_date", "p"], ascending=[True, False]).groupby("trade_date").head(K).copy()
    ex = c.trade_date.map(exits).fillna("gap"); r = pd.Series(np.select([ex == x for x in ["gap"] + XS], [c["y_" + x] for x in ["gap"] + XS], default=np.nan), index=c.index)
    ok = c.filled & r.notna(); n = pd.Series(np.where(ok, r - COST, 0.0), index=c.trade_date).groupby(level=0).sum().div(K).reindex(days).fillna(0.0); f = r[ok]
    if not len(f): return {"trades": 0}
    return {"trades": int(len(f)), "gross_bp": round(float(f.mean()) * 1e4, 1), "win_net": round(float((f > COST).mean()), 3), "net_bp_day": round(float(n.mean()) * 1e4, 2), "t": round(nw_t(n.values), 2),
            "lcb": round(block_lcb(n.values) * 1e4, 2) if len(days) >= 60 else None, "late_share": round(float((ex[ok] != "gap").mean()), 2)}

def main():
    dev, h8, tm, research = datasets(); tr = tm[tm.ts_code.isin(research)]
    R = pd.concat([dev[dev.trade_date <= "20260630"], tr[tr.trade_date > "20260630"]]); D = drift_table(R); pol, F = signals(D); pol["M"] = learned(D, F)
    segs = [("dev_2024_25", dev, "20240101", "20251231"), ("dev_2025", dev, "20250101", "20251231"), ("dev_2026H1", dev, "20260101", "20260630"),
            ("h800_2024_25", h8, "20240101", "20251231"), ("h800_2025", h8, "20250101", "20251231"), ("h800_2026H1", h8, "20260101", "20260630"),
            ("time_research", tr, "20260702", "20261231"), ("time_all", tm, "20260702", "20261231")]
    res = {"spec": "overnight_v3_adaptive_exit_v1", "drift_hot_bp_by_half": {k: {x: round(float(v[x].mean()) * 1e4, 1) for x in XS} for k, v in D.groupby(D.index.str[:4] + np.where(D.index.str[4:6] <= "06", "H1", "H2"))},
           "exit_choice_share": {p: pol.loc[pol.index >= "20240201", p].value_counts(normalize=True).round(2).to_dict() for p in ("A_20", "W_20", "Wm_20", "M")}, "cells": {}}
    print("热门组相对开盘卖的额外收益(bp):", res["drift_hot_bp_by_half"]); print("各策略选择占比:", res["exit_choice_share"])
    order = ["fixed_gap", "fixed_x0935", "fixed_x1000", "fixed_x1030", "A_10", "A_20", "A_40", "W_20", "Wm_20", "M"]
    for nm, s, lo, hi in segs:
        s = s[(s.trade_date >= lo) & (s.trade_date <= hi)]; days = sorted(s.trade_date.unique())
        res["cells"][nm] = {p: port(s, pol[p], days) for p in order}
        for mg in (0.0010, 0.0020):
            for p in ("fixed_gap", "W_20"): res["cells"][nm][f"{p}|+{int(mg * 1e4)}bp"] = port(s, pol[p], days, mg)
        print("\n==", nm, len(days)); print(pd.DataFrame(res["cells"][nm]).T.to_string())
    (OUT / "v3_adaptive.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float)); pol.to_parquet(OUT / "v3_exit_policy.parquet")

if __name__ == "__main__":
    main()
