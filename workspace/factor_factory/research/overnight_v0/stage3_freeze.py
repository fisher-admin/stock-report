#!/usr/bin/env python3
"""冻结阶段 3 主模型(G1, K=5, 预测值 > 成本才开仓), 供时间留出集一次性检验与 VPS 影子运行。

冻结内容: LightGBM 回归模型(训练截至 2026-06-29, 末日清洗), 特征列表, 截面秩变换规则, 开仓规则, 规格哈希。
冻结之后: 2026-07-02 起的任何标签只允许读取一次(time_holdout_eval.py), 模型与规则不得再改; 若改, 必须换新版本号并重新寻找干净留出。
"""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from stage3_model import build, SPEC, STOCK_FEATS, EVENT_FLAGS, DAY_FEATS, COST, OUT  # noqa: E402

FROZEN = OUT / "frozen_G1_v1"
RULE = {"model": "G1", "K": 5, "open_if": "pred > cost(19.2bp)", "slot": "1/K 等权, 未用或未成交槽位留现金", "entry": "14:50-14:55 VWAP, 保护价 14:45 价 x1.003",
        "exit": "T+1 09:30-09:40 VWAP", "universe": "沪深主板 + 创业板, ADV20>=1亿, 14:40-14:45 成交>=300万, 未封涨停"}

def main():
    import lightgbm as lgb
    w, _ = build()
    Z = ["z_" + c for c in STOCK_FEATS] + ["z_e_tug_60"]; DF = ["day_" + c for c in DAY_FEATS]; X = Z + EVENT_FLAGS + DF
    tr = w[w.trade_date <= "20260630"]; last = tr.trade_date.max(); tr = tr[tr.trade_date < last]
    prm = dict(SPEC["lgb_params"]); nround = prm.pop("num_boost_round"); prm["verbose"] = -1; prm["num_threads"] = 8
    b = lgb.train(prm, lgb.Dataset(tr[X].values.astype(np.float32), tr.y.clip(-0.06, 0.06).values), nround)
    FROZEN.mkdir(exist_ok=True); b.save_model(str(FROZEN / "model_G1.txt"))
    man = {"version": "overnight_G1_v1", "frozen_at": time.strftime("%Y-%m-%d %H:%M %Z"), "stage3_spec_hash": SPEC["spec_hash"], "rule": RULE, "features": X,
           "train": {"first": tr.trade_date.min(), "last": tr.trade_date.max(), "rows": int(len(tr)), "stocks": int(tr.ts_code.nunique())},
           "model_sha256": hashlib.sha256((FROZEN / "model_G1.txt").read_bytes()).hexdigest(), "cost": COST,
           "evidence_main_2024_25": "475 笔/112 天, 毛 +54.7bp, 净胜率 57.7%, 日均净 +6.95bp, 下界 +1.88bp; 剔除 2024-09-24~10-18 后毛 +41.7bp, 下界 +0.47bp",
           "holdout_plan": "2026-07-02 起, 剔除留出 800 只股票, 只读一次"}
    (FROZEN / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1)); print(json.dumps(man, ensure_ascii=False)[:600])

if __name__ == "__main__":
    main()
