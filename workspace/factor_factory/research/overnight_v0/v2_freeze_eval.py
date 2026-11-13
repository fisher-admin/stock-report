#!/usr/bin/env python3
"""v2 冻结与验证。候选在看验证数据之前写定: 主选 R|x1000(14:55 买, 次日 09:45-10:00 卖), 次选 R|gap(14:55 买, 次日开盘卖)。
规则: 预测 > 成本, 每日至多 5 只, 每笔扣 19.2bp。
验证链(各读一次, 记 READ_LOG):
  wf800  截面留出: 逐月滚动模型(只用研究股票训练)套到从未使用过的 800 只留出股票上, 2024-2025 与 2026H1。检验"选股是否能外推到没见过的股票"。
  time   时间留出: 冻结模型(训练截至 2026-06-29)在 2026-07-02..10-08。该窗口 v1 已读过且已知其开盘后回吐, 因此只算半干净的复核。
  真正干净的检验只能来自 2026-10-09 之后的数据与前瞻影子。
用法: v2_freeze_eval.py wf800 | freeze | time
"""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
import numpy as np, pandas as pd, lightgbm as lgb
sys.path.insert(0, str(Path(__file__).parent))
import stage3_model as s3  # noqa: E402
from overnight_factor_model import OUT  # noqa: E402
from v2_timing_grid import legs  # noqa: E402
from v2_model import port, SPEC, TARGETS  # noqa: E402
CAND = ["x1000", "gap"]; COST = s3.COST; FZ = OUT / "frozen_v2"
X = ["z_" + c for c in s3.STOCK_FEATS] + ["z_e_tug_60"] + s3.EVENT_FLAGS + ["day_" + c for c in s3.DAY_FEATS]
PR = dict(SPEC["lgb_R"]); NR = PR.pop("num_boost_round"); PR.update(verbose=-1, num_threads=8)

def frame(d):
    s3.OUT = d; w, _ = s3.build(); T = legs(pd.read_parquet(d / "v2_timing.parquet"))
    w = w.merge(T[["ts_code", "trade_date", "a_e1455"] + ["b_" + TARGETS[t] for t in CAND]], on=["ts_code", "trade_date"], how="left")
    for t in CAND: w["y_" + t] = (1 + w.a_e1455) * (1 + w["b_" + TARGETS[t]]) - 1
    return w

def fit(tr, t):
    ok = tr["y_" + t].notna().values
    return lgb.train(PR, lgb.Dataset(tr[X].values.astype(np.float32)[ok], tr["y_" + t][ok].clip(-0.06, 0.06).values), NR)

def report(P, segs, tag, out, extra=None):
    res = {"spec_hash": SPEC["spec_hash"], "candidates": CAND, "read_at": time.strftime("%Y-%m-%d %H:%M %Z"), **(extra or {})}
    for seg, lo, hi, sub in segs:
        s = P[(P.trade_date >= lo) & (P.trade_date <= hi)]
        if sub is not None: s = s[s.ts_code.isin(sub)]
        days = sorted(s.trade_date.unique()); res[seg] = {"days": len(days), "stocks_per_day": round(len(s) / max(len(days), 1), 1)}
        for t in CAND:
            q = s.assign(pR=s["pR_" + t]); res[seg][t] = port(q, "y_" + t, "pR_" + t, 5, days)
            res[seg][t]["universe_bp"] = round(float(q.groupby("trade_date")["y_" + t].mean().mean()) * 1e4, 1)
            res[seg][t]["rank_ic"] = round(float(q.groupby("trade_date").apply(lambda d: d.pR.rank().corr(d["y_" + t].rank())).mean()), 4)
            res[seg][t]["by_month"] = None
            c = q[q.pR > COST].sort_values(["trade_date", "pR"], ascending=[True, False]).groupby("trade_date").head(5); c = c[c.filled & c["y_" + t].notna()]
            if len(days) < 120 and len(c): res[seg][t]["by_month"] = {m: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for m, v in c.groupby(c.trade_date.str[:6])["y_" + t]}
            print(tag, seg, t, {k: v for k, v in res[seg][t].items()})
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    lg = out.parent / "READ_LOG_v2.json"; old = json.loads(lg.read_text()) if lg.exists() else []
    lg.write_text(json.dumps(old + [{"tag": tag, "at": res["read_at"]}], ensure_ascii=False))

def main():
    mode = sys.argv[1]
    if mode == "wf800":
        H = OUT / "holdout_800"; w = frame(OUT); h = frame(H); outs = []
        for m in sorted({d[:6] for d in h.trade_date.unique() if "20240101" <= d <= "20260630"}):
            tr = w[w.trade_date < m + "01"]; tr = tr[tr.trade_date < tr.trade_date.max()]; te = h[h.trade_date.str[:6] == m].copy()
            for t in CAND: te["pR_" + t] = fit(tr, t).predict(te[X].values.astype(np.float32))
            outs.append(te[["ts_code", "trade_date", "filled"] + ["pR_" + t for t in CAND] + ["y_" + t for t in CAND]]); print(m, len(te), flush=True)
        P = pd.concat(outs); P.to_parquet(H / "v2_wf800_predictions.parquet")
        report(P, [("main_2024_25", "20240101", "20251231", None), ("ref_2026H1", "20260101", "20260630", None)], "wf800", H / "v2_wf800_eval.json")
    elif mode == "freeze":
        w = frame(OUT); tr = w[w.trade_date <= "20260629"]; tr = tr[tr.trade_date < tr.trade_date.max()]; FZ.mkdir(exist_ok=True); man = {"version": "overnight_v2_R_v1", "features": X, "train_end": tr.trade_date.max(), "rule": {"K": 5, "open": "pred > cost", "cost": COST, "entry": "e1455"}, "models": {}}
        for t in CAND:
            b = fit(tr, t); p = FZ / f"model_{t}.txt"; b.save_model(str(p)); man["models"][t] = {"exit": TARGETS[t], "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        (FZ / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1)); print("frozen", {t: v["sha256"][:12] for t, v in man["models"].items()}, "train_end", man["train_end"], len(tr))
    elif mode == "time":
        H = OUT / "holdout_time"; h = frame(H); h = h[h.trade_date >= "20260702"].copy(); man = json.loads((FZ / "manifest.json").read_text())
        for t in CAND: h["pR_" + t] = lgb.Booster(model_file=str(FZ / f"model_{t}.txt")).predict(h[man["features"]].values.astype(np.float32))
        research = set((H / "research_codes.txt").read_text().split())
        report(h, [("primary_research_codes", "20260702", "20261231", research), ("secondary_all_non_holdout", "20260702", "20261231", None)], "time", H / "v2_time_eval.json", {"model_sha": {t: v["sha256"] for t, v in man["models"].items()}})

if __name__ == "__main__":
    main()
