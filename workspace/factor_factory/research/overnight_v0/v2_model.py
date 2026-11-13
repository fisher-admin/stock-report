#!/usr/bin/env python3
"""v2 第二步: 让预测目标与离场时点一致, 并加一个专门识别头部的排序模型。全部在研究股票、逐月滚动样本外上做。

目标(买入均为 14:55 线均价): gap = 到次日开盘价(纯隔夜段); x0935 = 到 09:35 线均价; x1000 = 到 09:45-10:00 均价; x1030 = 到 10:30。
模型:
  R  与 v1 相同的 LightGBM 回归(同参数、同特征), 目标换成对应时点的收益(截尾 ±6%), 开仓 = 预测 > 成本, 每日至多 5 只。
  H  LightGBM lambdarank(按交易日分组, 标签 = 当日收益十分位 0-9, 截断 5), 只负责排头部; 是否开仓仍由 R 决定(当日 R 预测 > 成本的股票里按 H 排序取前 5)。
评估: 每个模型按它自己的离场时点结算, 扣 19.2bp; 主段 2024-2025, 参考 2026H1。2026-07 之后不在这里使用。
选型规则(先写定): 主段净值下界最高、且 2026H1 净均值为正、且剔除 2024-09-24..10-18 后主段净均值仍为正的那个(目标, 模型)进入冻结。
"""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, OUT  # noqa: E402
from stage3_model import build, COST, STOCK_FEATS, EVENT_FLAGS, DAY_FEATS, SPEC as S3  # noqa: E402
from v2_timing_grid import legs  # noqa: E402
TARGETS = {"gap": "xopen", "x0935": "x0935", "x1000": "x1000", "x1030": "x1030"}
SPEC = {"spec_id": "overnight_v2_model_v1", "targets": TARGETS, "entry": "e1455", "lgb_R": S3["lgb_params"], "K": 5,
        "lgb_H": {"objective": "lambdarank", "lambdarank_truncation_level": 5, "learning_rate": 0.03, "num_leaves": 15, "min_data_in_leaf": 400, "feature_fraction": 0.7,
                  "bagging_fraction": 0.7, "bagging_freq": 1, "lambda_l2": 10.0, "num_boost_round": 200, "seed": 20261113}, "cost": COST}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()[:16]

def port(s, ycol, rank, K, days):
    c = s[s.pR > COST].sort_values(["trade_date", rank], ascending=[True, False]).groupby("trade_date").head(K)
    ok = c.filled & c[ycol].notna(); r = c[ycol]
    n = pd.Series(np.where(ok, r - COST, 0.0), index=c.trade_date).groupby(level=0).sum().div(K).reindex(days).fillna(0.0); f = r[ok]
    if not len(f): return {"trades": 0}
    return {"active_days": int(c.trade_date.nunique()), "trades": int(len(f)), "gross_bp": round(float(f.mean()) * 1e4, 1), "median_bp": round(float(f.median()) * 1e4, 1),
            "win_net": round(float((f > COST).mean()), 3), "net_bp_day": round(float(n.mean()) * 1e4, 2), "t": round(nw_t(n.values), 2),
            "lcb": round(block_lcb(n.values) * 1e4, 2) if len(days) >= 60 else None,
            "by_year": {y: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for y, v in f.groupby(c.trade_date[ok].str[:4])},
            "top10day_share": round(float(n.sort_values(ascending=False).head(10).sum() / n.sum()), 2) if n.sum() > 0 else None}

def main():
    import lightgbm as lgb
    w, _ = build(); T = legs(pd.read_parquet(OUT / "v2_timing.parquet"))
    w = w.merge(T[["ts_code", "trade_date", "a_e1455"] + ["b_" + x for x in set(TARGETS.values())]], on=["ts_code", "trade_date"], how="left")
    for t, x in TARGETS.items(): w["y_" + t] = (1 + w.a_e1455) * (1 + w["b_" + x]) - 1
    X = ["z_" + c for c in STOCK_FEATS] + ["z_e_tug_60"] + EVENT_FLAGS + ["day_" + c for c in DAY_FEATS]
    pR = dict(SPEC["lgb_R"]); nR = pR.pop("num_boost_round"); pR.update(verbose=-1, num_threads=8)
    pH = dict(SPEC["lgb_H"]); nH = pH.pop("num_boost_round"); pH.update(verbose=-1, num_threads=8)
    months = sorted({d[:6] for d in w.trade_date.unique() if "20240101" <= d <= "20260630"}); out = []
    for m in months:
        tr = w[w.trade_date < m + "01"]; tr = tr[tr.trade_date < tr.trade_date.max()].sort_values("trade_date"); te = w[w.trade_date.str[:6] == m].copy()
        Xtr = tr[X].values.astype(np.float32); Xte = te[X].values.astype(np.float32)
        for t in TARGETS:
            y = tr["y_" + t]; ok = y.notna().values
            te["pR_" + t] = lgb.train(pR, lgb.Dataset(Xtr[ok], y[ok].clip(-0.06, 0.06).values), nR).predict(Xte)
            g = tr[ok]; lab = (g.groupby("trade_date")["y_" + t].rank(pct=True) * 10).clip(upper=9.999).astype(int)
            te["pH_" + t] = lgb.train(pH, lgb.Dataset(Xtr[ok], lab.values, group=g.groupby("trade_date", sort=False).size().values), nH).predict(Xte)
        out.append(te[["ts_code", "trade_date", "filled"] + [c for c in te if c.startswith(("pR_", "pH_", "y_"))]]); print(m, len(tr), len(te), flush=True)
    P = pd.concat(out); P.to_parquet(OUT / "v2_predictions.parquet")
    res = {"spec": SPEC, "cells": {}, "deciles_bp": {}}
    ex = lambda s: s[(s.trade_date < "20240924") | (s.trade_date > "20241018")]  # noqa: E731
    for seg, lo, hi in [("main_2024_25", "20240101", "20251231"), ("main_ex924", "20240101", "20251231"), ("ref_2026H1", "20260101", "20260630")]:
        s = P[(P.trade_date >= lo) & (P.trade_date <= hi)]
        if seg == "main_ex924": s = ex(s)
        days = sorted(s.trade_date.unique()); res["cells"][seg] = {}
        for t in TARGETS:
            q = s.assign(pR=s["pR_" + t])
            res["cells"][seg][f"R|{t}"] = port(q, "y_" + t, "pR_" + t, 5, days); res["cells"][seg][f"H|{t}"] = port(q, "y_" + t, "pH_" + t, 5, days)
            if seg != "main_ex924":
                for k in ("pR_", "pH_"):
                    d = q.assign(b=q.groupby("trade_date")[k + t].transform(lambda v: pd.qcut(v.rank(method="first"), 20, labels=False)))
                    res["deciles_bp"][f"{seg}|{k}{t}"] = [round(float(v) * 1e4, 1) for v in d.groupby(["trade_date", "b"])["y_" + t].mean().unstack().mean().iloc[[0, 9, 17, 18, 19]]]
        print("\n==", seg); print(pd.DataFrame(res["cells"][seg]).T[["active_days", "trades", "gross_bp", "median_bp", "win_net", "net_bp_day", "t", "lcb", "top10day_share"]].to_string())
    print("\n5% 分组(最低, 中, 最高三组) 各目标收益 bp:"); [print(k, v) for k, v in res["deciles_bp"].items()]
    (OUT / "v2_model.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))

if __name__ == "__main__":
    main()
