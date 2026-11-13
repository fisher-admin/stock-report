#!/usr/bin/env python3
"""Phase E · S3 top-N 精化回测(2026-07-04,预注册见 registry.topN_refinement_prereg,规则冻结在先)。

把两冻结格(高|弱|低 ∪ 高|弱|高)的宽篮子收敛为每日 top-N:
  R_liq: 按流动性(avg_amount_lookback)降序 —— 只取最可成交的一片,不声称格内选股力;
  R_oversold: 按 late_strength 升序(尾盘最弱=最超卖) —— 一个明说的经济学赌注。
N∈{10,20,30,50},决策N=20。统计以【日组合收益】为单位(t 跨日,规避同日截面相关)。
research(800研究样本)8配置全入账 → 择优单一配置 → holdout 二次看确认(如实披露非独立)。
成本: med主/p75敏感(B1实测滑点表,行级)。复用 setup_gauntlet.load_setup_s3 全部口径。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_gauntlet as sg  # noqa: E402

WS = Path("/Users/fisher/.openclaw/workspace")
PANEL = WS / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet"
OUT_MD = WS / "stock_data" / "03-working" / "gauntlet_reports" / "setup_S3_topN_20260704.md"
OUT_JSON = WS / "stock_data" / "03-working" / "gauntlet_reports" / "setup_S3_topN_20260704.json"
RANKINGS = ("R_liq", "R_oversold")
N_GRID = (10, 20, 30, 50)
DECISION_N = 20


def load_events(cost_mode: str, holdout: bool) -> pd.DataFrame:
    """复用官方装载器;holdout=True 时monkeypatch特征路径(其余口径一字不差)。"""
    orig = sg.INTRADAY_SHAPE_FEATURES_PATH
    if holdout:
        sg.INTRADAY_SHAPE_FEATURES_PATH = WS / "stock_data" / "03-working" / "intraday_shape_features_holdout.parquet"
    try:
        slip = sg.SlippageTable(sg.SLIPPAGE_PATH)
        res = sg.load_setup_s3(cost_mode, slip, random_decision_feature=False, seed=20260704)
    finally:
        sg.INTRADAY_SHAPE_FEATURES_PATH = orig
    ev = res["event_df"].copy()
    # 两冻结格并集
    ev = ev[(ev["am_share"] == "高") & (ev["late_sign"] == "弱")]
    cost_col = next(c for c in ("_cost_dec", "cost_dec", "cost", "cost_med_dec") if c in ev.columns)
    ev = ev.rename(columns={cost_col: "cost_dec"})
    keep = ["ts_code", "trade_date", "o2c", "cost_dec", "late_strength", "smooth"]
    ev = ev[[c for c in keep if c in ev.columns]].dropna(subset=["o2c", "cost_dec"])
    return ev


def add_liquidity(ev: pd.DataFrame) -> pd.DataFrame:
    liq = pd.read_parquet(PANEL, columns=["ts_code", "trade_date", "avg_amount_lookback"])
    liq["trade_date"] = liq["trade_date"].astype(str)
    return ev.merge(liq, on=["ts_code", "trade_date"], how="left")


def daily_topn_series(ev: pd.DataFrame, ranking: str, n: int, rng: np.random.RandomState | None = None) -> pd.Series:
    d = ev.copy()
    if ranking == "R_liq":
        key = "avg_amount_lookback"; asc = False
    else:  # R_oversold
        key = "late_strength"; asc = True
    if rng is not None:  # 安慰剂: 同日打乱排序变量
        d[key] = d.groupby("trade_date")[key].transform(lambda s: pd.Series(rng.permutation(s.values), index=s.index))
    d = d.dropna(subset=[key])
    d["_rk"] = d.groupby("trade_date")[key].rank(ascending=asc, method="first")
    top = d[d["_rk"] <= n]
    net = top["o2c"] - top["cost_dec"]
    return net.groupby(top["trade_date"]).mean()  # 日组合净收益(等权)


def series_stats(s: pd.Series) -> dict:
    x = s.dropna().to_numpy()
    n = len(x)
    if n < 30:
        return {"n_days": n, "insufficient": True}
    m = x.mean(); sd = x.std(ddof=1)
    t = m / (sd / np.sqrt(n)) if sd > 1e-12 else 0.0
    yr = pd.Series(x, index=pd.to_datetime(s.dropna().index, format="%Y%m%d")).groupby(lambda i: i.year)
    yearly = {str(y): round(g.mean() * 100, 4) for y, g in yr}
    pos_years = sum(1 for v in yearly.values() if v > 0)
    contrib = {y: g.sum() for y, g in yr}
    tot = sum(contrib.values())
    max_share = max((v / tot for v in contrib.values()), default=0) if tot > 0 else 1.0
    return {"n_days": n, "day_net_mean_pct": round(m * 100, 4), "t": round(t, 2),
            "hit_days_pct": round(100 * (x > 0).mean(), 1), "ann_pct": round(m * 244 * 100, 2),
            "yearly_mean_pct": yearly, "pos_years": pos_years,
            "max_year_contrib": round(max_share, 3) if tot > 0 else None}


def main() -> int:
    print("== RESEARCH(800研究样本) ==")
    ev_med = add_liquidity(load_events("med", holdout=False))
    ev_p75 = load_events("p75", holdout=False)[["ts_code", "trade_date", "cost_dec"]].rename(columns={"cost_dec": "cost_p75"})
    ev = ev_med.merge(ev_p75, on=["ts_code", "trade_date"], how="left")
    print(f"两格并集事件 {len(ev):,}  日 {ev['trade_date'].nunique()}")

    baseline = series_stats((ev["o2c"] - ev["cost_dec"]).groupby(ev["trade_date"]).mean())
    print("篮子基线(med):", {k: baseline[k] for k in ("day_net_mean_pct", "t", "ann_pct")})

    results = {}
    rng0 = np.random.RandomState(20260704)
    for r in RANKINGS:
        for n in N_GRID:
            s = daily_topn_series(ev, r, n)
            st = series_stats(s)
            # p75 同名单
            evp = ev.copy(); evp["cost_dec"] = evp["cost_p75"]
            st_p75 = series_stats(daily_topn_series(evp, r, n))
            # 安慰剂 x10(仅med)
            plac = []
            for _ in range(10):
                ps = daily_topn_series(ev, r, n, rng=rng0)
                plac.append(ps.dropna().mean())
            pm, psd = float(np.mean(plac)), float(np.std(plac))
            z = (st.get("day_net_mean_pct", 0) / 100 - pm) / psd if psd > 1e-12 else 0.0
            passed = (st.get("day_net_mean_pct", -1) > 0 and st.get("t", 0) > 2
                      and st.get("pos_years", 0) >= 2 and st.get("max_year_contrib") is not None
                      and st["max_year_contrib"] <= 0.70 and z > 2)
            results[f"{r}_N{n}"] = {"med": st, "p75_day_net_mean_pct": st_p75.get("day_net_mean_pct"),
                                    "p75_t": st_p75.get("t"), "placebo_z": round(z, 2), "pass": bool(passed)}
            print(f"  {r} N={n}: med日净{st.get('day_net_mean_pct')}% t={st.get('t')} 年化{st.get('ann_pct')}% "
                  f"| p75日净{st_p75.get('day_net_mean_pct')}% | 安慰剂z={z:.1f} | {'PASS' if passed else 'FAIL'}")

    # 择优: 优先N=20里pass的,按t取最高;无则全配置里pass的
    cand = [(k, v) for k, v in results.items() if v["pass"]]
    pref = [c for c in cand if c[0].endswith(f"N{DECISION_N}")] or cand
    best = max(pref, key=lambda kv: kv[1]["med"]["t"]) if pref else None
    verdict = {"research_results": results, "baseline_basket_med": baseline,
               "best_config": best[0] if best else None}

    if best:
        r, n = best[0].rsplit("_N", 1)
        n = int(n)
        print(f"\n== HOLDOUT 确认(二次看,如实披露): {best[0]} ==")
        hv_med = add_liquidity(load_events("med", holdout=True))
        hv_p75 = load_events("p75", holdout=True)[["ts_code", "trade_date", "cost_dec"]].rename(columns={"cost_dec": "cost_p75"})
        hv = hv_med.merge(hv_p75, on=["ts_code", "trade_date"], how="left")
        h_st = series_stats(daily_topn_series(hv, r, n))
        hvp = hv.copy(); hvp["cost_dec"] = hvp["cost_p75"]
        h_p75 = series_stats(daily_topn_series(hvp, r, n))
        h_pass = h_st.get("day_net_mean_pct", -1) > 0 and h_st.get("t", 0) >= 2
        verdict["holdout"] = {"config": best[0], "med": h_st, "p75": h_p75, "pass": bool(h_pass),
                              "caveat": "holdout已用于篮子判定,本次为第二次看(嵌套精化,透明披露,不算独立)"}
        print(f"  holdout med日净{h_st.get('day_net_mean_pct')}% t={h_st.get('t')} → {'PASS' if h_pass else 'FAIL'}")
        print(f"  holdout p75日净{h_p75.get('day_net_mean_pct')}% t={h_p75.get('t')}(敏感性)")
    else:
        verdict["holdout"] = None
        print("\n无配置通过research标准,不做holdout(按预注册)。")

    OUT_JSON.write_text(json.dumps(verdict, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# S3 top-N 精化回测(20260704,预注册规则)", "",
             f"篮子基线(med): 日净{baseline.get('day_net_mean_pct')}% t={baseline.get('t')} 年化{baseline.get('ann_pct')}%", "",
             "| 配置 | med日净% | t | 年化% | 胜率日% | p75日净% | 安慰剂z | 判 |", "|---|---|---|---|---|---|---|---|"]
    for k, v in verdict["research_results"].items():
        m = v["med"]
        lines.append(f"| {k} | {m.get('day_net_mean_pct')} | {m.get('t')} | {m.get('ann_pct')} | "
                     f"{m.get('hit_days_pct')} | {v.get('p75_day_net_mean_pct')} | {v['placebo_z']} | {'PASS' if v['pass'] else 'FAIL'} |")
    if verdict.get("holdout"):
        h = verdict["holdout"]
        lines += ["", f"## HOLDOUT确认({h['config']},二次看): med日净{h['med'].get('day_net_mean_pct')}% "
                  f"t={h['med'].get('t')} → {'PASS' if h['pass'] else 'FAIL'};p75日净{h['p75'].get('day_net_mean_pct')}%", h["caveat"]]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
