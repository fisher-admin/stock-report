#!/usr/bin/env python3
"""冻结模型 overnight_G1_v1 的时间留出集一次性检验。

运行前必须已执行 holdout_prepare.py 并在 OVN_* 环境变量指向 holdout_time 的条件下构造好 frame_wide/day_frame/event_flags/frame_cde。
规则与口径全部取自 frozen_G1_v1/manifest.json, 本脚本不做任何调参。
预先写定的两个口径:
  主口径   研究 800 中的主板+创业板股票(与训练分布一致, 纯时间留出)
  次口径   全部非留出 800 的主板+创业板股票(时间 + 截面同时外推)
只读一次: 首次运行写 READ_LOG.json; 再次运行需 --force, 且会在日志中留痕。
"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, WS  # noqa: E402
from stage3_model import build, COST  # noqa: E402

FROZEN = WS / "factor_factory/outputs/overnight_v0/frozen_G1_v1"
H = Path(os.environ["OVN_OUT"])

def port(s, K, days):
    c = s[s.p > COST].sort_values(["trade_date", "p"], ascending=[True, False]).groupby("trade_date").head(K)
    n = pd.Series(np.where(c.filled, c.y_fill - COST, 0.0), index=c.trade_date).groupby(level=0).sum().div(K).reindex(days).fillna(0.0)
    f = c[c.filled]
    o = {"days": len(days), "active_days": int(c.trade_date.nunique()), "trades": int(len(f)), "net_bp_per_day": round(float(n.mean()) * 1e4, 2),
         "net_t": round(nw_t(n.values), 2), "net_lcb95_bp": round(block_lcb(n.values) * 1e4, 2) if len(days) >= 60 else None}
    if len(f):
        o.update({"trade_gross_bp": round(float(f.y_fill.mean()) * 1e4, 1), "trade_median_bp": round(float(f.y_fill.median()) * 1e4, 1),
                  "trade_win_net": round(float((f.y_fill > COST).mean()), 3), "seg_bp": [round(float(f[k].mean()) * 1e4, 1) for k in ("seg1n", "seg2", "seg3")],
                  "by_month": {m: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for m, v in f.groupby(f.trade_date.str[:6]).y_fill},
                  "trades_list": f[["trade_date", "ts_code", "p", "y_fill"]].assign(p=lambda x: (x.p * 1e4).round(1), y_fill=lambda x: (x.y_fill * 1e4).round(1)).values.tolist()})
    return o

def main():
    log = H / "READ_LOG.json"
    if log.exists() and "--force" not in sys.argv: sys.exit("时间留出集已读过一次; 如确需重读请加 --force(会留痕)")
    import lightgbm as lgb
    man = json.loads((FROZEN / "manifest.json").read_text()); b = lgb.Booster(model_file=str(FROZEN / "model_G1.txt"))
    w, _ = build(); X = man["features"]
    w = w[(w.trade_date >= "20260702")].copy(); w["p"] = b.predict(w[X].values.astype(np.float32))
    w = w[w.y_fill.notna() | ~w.filled]
    research = set((H / "research_codes.txt").read_text().split())
    days = sorted(w.trade_date.unique()); K = man["rule"]["K"]
    res = {"model": man["version"], "model_sha256": man["model_sha256"], "read_at": time.strftime("%Y-%m-%d %H:%M %Z"), "window": [days[0], days[-1]], "n_days": len(days)}
    for nm, s in [("primary_research_codes", w[w.ts_code.isin(research)]), ("secondary_all_non_holdout", w)]:
        res[nm] = {"stocks_per_day": round(len(s) / len(days), 1), "universe_gross_bp": round(float(s.groupby("trade_date").y.mean().mean()) * 1e4, 1),
                   "always_top5": port(s.assign(p=s.p + 1.0), K, days), "frozen_rule": port(s, K, days)}
        res[nm]["always_top5"].pop("trades_list", None)
    (H / "holdout_eval.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    log.write_text(json.dumps({"reads": json.loads(log.read_text())["reads"] + [res["read_at"]] if log.exists() else [res["read_at"]]}, ensure_ascii=False))
    for nm in ("primary_research_codes", "secondary_all_non_holdout"):
        r = res[nm]; print("\n==", nm, "stocks/day", r["stocks_per_day"], "universe", r["universe_gross_bp"])
        for k in ("frozen_rule", "always_top5"): print(k, {kk: v for kk, v in r[k].items() if kk != "trades_list"})

if __name__ == "__main__":
    main()
