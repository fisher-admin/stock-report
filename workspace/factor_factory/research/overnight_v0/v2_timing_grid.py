#!/usr/bin/env python3
"""v2 第一步: 固定 v1 逐月滚动样本外的选股(预测 > 成本, 每日至多 5 只), 只换买入/卖出时点, 看哪一段留得住。

只用研究股票、2024-2025 样本外(主)与 2026H1(参考)。2026-07 之后的窗口不在这里使用。
每笔扣 19.2bp。次日开盘一字跌停卖不出时, 该笔按次日收盘价卖出。
条件离场(先写定): cond_gap = 高开 >= 1% 则开盘卖, 否则 10:00 均价卖; cond_weak = 开盘低于昨收则开盘卖, 否则 09:40 卖。
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, OUT  # noqa: E402
from stage3_model import COST  # noqa: E402
ES = ["e1450", "e1455", "e1500", "eclose"]; XS = ["xopen", "x0935", "x0940", "x1000", "x1030", "x1130", "xclose", "cond_gap", "cond_weak"]

def legs(t):
    stuck = t.n_flat0935.fillna(False).astype(bool) & (t.b_xopen < -0.045)
    for x in ("xopen", "x0935", "x0940", "x1000", "x1030", "x1130"): t["b_" + x] = np.where(stuck, t.b_xclose, t["b_" + x])
    t["b_cond_gap"] = np.where(t.b_xopen >= 0.01, t.b_xopen, t.b_x1000); t["b_cond_weak"] = np.where(t.b_xopen < 0, t.b_xopen, t.b_x0940)
    return t

def cell(s, c, e, x, K, days):
    r = (1 + c["a_" + e]) * (1 + c["b_" + x]) - 1; ok = c.filled & r.notna()
    n = pd.Series(np.where(ok, r - COST, 0.0), index=c.trade_date).groupby(level=0).sum().div(K).reindex(days).fillna(0.0)
    f = r[ok]
    return {"trades": int(ok.sum()), "gross_bp": round(float(f.mean()) * 1e4, 1), "median_bp": round(float(f.median()) * 1e4, 1), "win_net": round(float((f > COST).mean()), 3),
            "net_bp_day": round(float(n.mean()) * 1e4, 2), "t": round(nw_t(n.values), 2), "lcb": round(block_lcb(n.values) * 1e4, 2),
            "by_year": {y: round(float(v.mean()) * 1e4, 1) for y, v in f.groupby(c.trade_date[ok].str[:4])}}

def main():
    P = pd.read_parquet(OUT / "stage3_predictions.parquet"); T = legs(pd.read_parquet(OUT / "v2_timing.parquet"))
    s = P.merge(T, on=["ts_code", "trade_date"], how="inner"); K = 5; res = {"spec": "overnight_v2_timing_grid_v1", "selection": "v1 walk-forward p_G1 > cost, top5"}
    for wn, lo, hi in [("dev_2024_2025", "20240101", "20251231"), ("ref_2026H1", "20260101", "20260630")]:
        w = s[(s.trade_date >= lo) & (s.trade_date <= hi)]; days = sorted(w.trade_date.unique())
        c = w[w.p_G1 > COST].sort_values(["trade_date", "p_G1"], ascending=[True, False]).groupby("trade_date").head(K)
        res[wn] = {"grid": {f"{e}->{x}": cell(w, c, e, x, K, days) for e in ES for x in XS},
                   "universe_leg_bp": {k: round(float(w.groupby("trade_date")[k].mean().mean()) * 1e4, 1) for k in [f"a_{e}" for e in ES] + [f"b_{x}" for x in XS]},
                   "selected_leg_bp": {k: round(float(c[k].mean()) * 1e4, 1) for k in [f"a_{e}" for e in ES] + [f"b_{x}" for x in XS]}}
        print("\n==", wn, "selected", len(c)); print("universe legs", res[wn]["universe_leg_bp"]); print("selected legs", res[wn]["selected_leg_bp"])
        print(pd.DataFrame({x: {e: res[wn]["grid"][f"{e}->{x}"]["gross_bp"] for e in ES} for x in XS}).to_string())
        print("net bp/day:"); print(pd.DataFrame({x: {e: res[wn]["grid"][f"{e}->{x}"]["net_bp_day"] for e in ES} for x in XS}).to_string())
        print("lcb:"); print(pd.DataFrame({x: {e: res[wn]["grid"][f"{e}->{x}"]["lcb"] for e in ES} for x in XS}).to_string())
        print("win_net:"); print(pd.DataFrame({x: {e: res[wn]["grid"][f"{e}->{x}"]["win_net"] for e in ES} for x in XS}).to_string())
    (OUT / "v2_timing_grid.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))

if __name__ == "__main__":
    main()
