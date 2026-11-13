#!/usr/bin/env python3
"""把 G1 v1 的下行识别能力换算成"隔夜避让"的价值(给已持仓或当日本就要买入的股票用)。

动作: 14:55 卖出(或推迟买入) -> 次日 09:35-09:40 买回。 每次避让的价值 = -y - COST(往返 19.2bp); 推迟买入不产生额外成本, 价值 = -y。
可执行性: 14:45 跌停封死的股票卖不出, 不计入。
规则在看数字之前写定:
  abs1   预测 < -COST          (主规则, 与做多规则 预测 > COST 对称)
  abs2   预测 < -2*COST
  q10/q05/q02   当日合格股里预测最低的 10% / 5% / 2%
窗口: dev = 逐月滚动样本外 2024-2025(stage3_predictions.p_G1); ref = 2026H1; hold = 冻结模型在 2026-07-02..10-08(该窗口的十分位结果已在事后诊断中看过, 只能算复核, 不是干净检验)。
用法: risk_overlay_eval.py dev | hold(需 OVN_* 指向 holdout_time)
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, WS, OUT  # noqa: E402
from stage3_model import COST  # noqa: E402

RULES = {"abs1": lambda s: s.p < -COST, "abs2": lambda s: s.p < -2 * COST,
         "q10": lambda s: s.pr <= 0.10, "q05": lambda s: s.pr <= 0.05, "q02": lambda s: s.pr <= 0.02}

def stats(s, rule):
    days = sorted(s.trade_date.unique()); N = s.groupby("trade_date").size()
    f = s[rule(s) & ~s.locked_dn]
    if not len(f): return {"n": 0}
    v = -f.y - COST
    dsum = v.groupby(f.trade_date).sum().reindex(days).fillna(0.0)
    book = (dsum / N.reindex(days)).values                       # 等权持有全部合格股的人, 每天因避让多赚多少
    dm = (-f.y).groupby(f.trade_date).mean()
    return {"days": len(days), "flag_days": int(f.trade_date.nunique()), "n": int(len(f)), "flag_per_day": round(len(f) / len(days), 1),
            "flag_share": round(len(f) / len(s), 4), "blocked_limit_down": int((rule(s) & s.locked_dn).sum()),
            "y_mean_bp": round(float(f.y.mean()) * 1e4, 1), "y_median_bp": round(float(f.y.median()) * 1e4, 1),
            "stepaside_net_bp": round(float(v.mean()) * 1e4, 1), "stepaside_win": round(float((v > 0).mean()), 3),
            "delay_buy_gain_bp": round(float(-f.y.mean()) * 1e4, 1), "delay_buy_win": round(float((f.y < 0).mean()), 3),
            "day_mean_t": round(nw_t((dm - COST).values), 2), "flag_day_win": round(float((dm > COST).mean()), 3),
            "book_add_bp_per_day": round(float(book.mean()) * 1e4, 2), "book_add_t": round(nw_t(book), 2),
            "book_add_lcb95_bp": round(block_lcb(book) * 1e4, 2) if len(days) >= 60 else None,
            "seg_bp": [round(float(f[k].mean()) * 1e4, 1) for k in ("seg1n", "seg2", "seg3")],
            "by_board": {b: [int(len(x)), round(float((-x.y - COST).mean()) * 1e4, 1)] for b, x in f.groupby("market_board")},
            "by_period": {k: [int(len(x)), round(float((-x.y - COST).mean()) * 1e4, 1)] for k, x in f.groupby(f.trade_date.str[:6] if len(days) < 120 else f.trade_date.str[:4])},
            "top10day_share": round(float(dsum.sort_values(ascending=False).head(10).sum() / dsum.sum()), 3) if dsum.sum() > 0 else None}

def main():
    mode = sys.argv[1]
    if mode == "dev":
        P = pd.read_parquet(OUT / "stage3_predictions.parquet").rename(columns={"p_G1": "p"})
        w = pd.read_parquet(OUT / "frame_wide.parquet", columns=["ts_code", "trade_date", "elig", "locked_dn", "market_board", "seg1n", "seg2", "seg3"])
        s = P.merge(w[w.elig], on=["ts_code", "trade_date"], how="inner"); wins = {"dev_2024_2025": ("20240101", "20251231"), "ref_2026H1": ("20260101", "20260701")}
    else:
        import lightgbm as lgb
        from stage3_model import build
        F = WS / "factor_factory/outputs/overnight_v0/frozen_G1_v1"; man = json.loads((F / "manifest.json").read_text())
        s, _ = build(); s = s[s.trade_date >= "20260702"].copy(); s["p"] = lgb.Booster(model_file=str(F / "model_G1.txt")).predict(s[man["features"]].values.astype(np.float32))
        wins = {"hold_20260702_1008": ("20260702", "20261231")}
    s = s[s.y.notna()].copy(); s["pr"] = s.groupby("trade_date").p.rank(pct=True)
    res = {"spec": "overnight_risk_overlay_v1", "cost": COST, "mode": mode}
    for wn, (lo, hi) in wins.items():
        x = s[(s.trade_date >= lo) & (s.trade_date <= hi)]
        res[wn] = {"universe_y_bp": round(float(x.groupby("trade_date").y.mean().mean()) * 1e4, 1), **{r: stats(x, f) for r, f in RULES.items()}}
        print("\n==", wn, "universe", res[wn]["universe_y_bp"])
        for r in RULES:
            o = res[wn][r]; print(r, {k: o.get(k) for k in ("n", "flag_per_day", "flag_days", "y_mean_bp", "stepaside_net_bp", "stepaside_win", "day_mean_t", "book_add_bp_per_day", "book_add_lcb95_bp", "blocked_limit_down", "seg_bp", "by_period", "by_board", "top10day_share")})
    (OUT / f"risk_overlay_{mode}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))

if __name__ == "__main__":
    main()
