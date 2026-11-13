#!/usr/bin/env python3
"""C/D/E 族单因子评估(research-only, 2026-11-13)。规范见 RESEARCH_SPEC_stage2.md。

对每个因子:
  方向: 用 2022-2023 日度秩 IC 的符号定向(之后各段不再改方向)
  IC: 开发期 2022-2025 均值与 NW t, 逐年
  多头端: 每日按定向得分取前 10% 与前 5 名, 报告成交笔的毛均值、按日均值 t、净胜率、笔数、天数、相对同日股池超额
  分段: k1_dev(2022-2025 全部日) / k1_wf(2024-01 至 2026-06 全部日, 与口径2 同期) / k1_ref(2026H1) / k2_Ra / k2_Rb
  五分位: 开发期与 Rb 日的五分位毛均值(单调性)
用法: python3 factor_eval.py [--families C,D,E]
"""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, OUT  # noqa: E402

COST = 0.00192
FAM = {
    "C": ["c_ind_touch_1445", "c_ind_touch_share", "c_ind_up_prev", "c_ind_height_prev", "c_self_up_prev", "c_self_height_prev", "c_lag_vs_leaders", "sector_rel_ret"],
    "D": ["last5_amt_share", "d_amt_share_last30", "ret_last5", "d_ret_last30", "ret_tail45", "d_vwap30_dev", "d_pv_corr_pm", "d_up_amt_share_pm",
          "d_tail_flow", "d_rv_last30", "d_prev_close_auc", "d_prev_last5_ret"],
    "E": ["on_mean_20", "on_mean_60", "e_on_mean_120", "e_on_mean_250", "on_q05_60", "on_vol_20", "id_mean_20", "e_id_mean_60", "e_on_id_corr_60",
          "e_on_pos_share_60", "e_tug_60"],
}
SPEC = {"spec_id": "overnight_factor_eval_cde_v1", "universe": "研究800中的沪深主板+创业板, elig", "orientation": "sign of mean daily rank IC 2022-2023", "long_end": ["top10pct", "top5"],
        "periods": {"dev": ["20220104", "20251231"], "wf": ["20240101", "20260701"], "ref": ["20260101", "20260701"]},
        "regimes": {"Ra": "M5_rank_inloop_top5 pred>0", "Rb": "M1_breadth pred>=train q75"}, "cost": COST,
        "gate": "top10pct 开发期 毛>=40bp & 净胜率>=50% & >=300笔 & >=100天; 族级止损看口径2 毛<20bp"}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

def load():
    w = pd.read_parquet(OUT / "frame_wide.parquet"); w = w[w.elig].copy()
    f = pd.read_parquet(OUT / "frame_cde.parquet")
    w = w.merge(f, on=["ts_code", "trade_date"], how="left")
    g = w.groupby("trade_date")
    w["e_tug_60"] = g.on_mean_60.rank(pct=True) - g.e_id_mean_60.rank(pct=True)
    w["d_tail_flow"] = np.sign(w.d_ret_last30) * g.d_amt_share_last30.rank(pct=True)
    w["ubar"] = g.y.transform("mean")
    w["c_lag_vs_leaders"] = np.where(w.c_ind_touch_1445 > 0, w.lim_pct - w.ret_cut, np.nan)   # 与 factors_cde 同定义(按板块涨停幅度)
    return w

def stats(s):
    f = s[s.filled]
    if len(f) == 0: return {"trades": 0}
    dm = f.groupby("trade_date").y_fill.mean(); ex = (f.y_fill - f.ubar).groupby(f.trade_date).mean()
    return {"trades": int(len(f)), "days": int(dm.size), "gross_bp": round(float(f.y_fill.mean()) * 1e4, 1),
            "t": round(nw_t(dm.values), 2) if dm.size >= 20 else None, "win_net": round(float((f.y_fill > COST).mean()), 3),
            "excess_bp": round(float(ex.mean()) * 1e4, 1), "excess_t": round(nw_t(ex.values), 2) if ex.size >= 20 else None,
            "seg2_bp": round(float(f.seg2.mean()) * 1e4, 1)}

def main():
    fams = (sys.argv[sys.argv.index("--families") + 1].split(",") if "--families" in sys.argv else list(FAM))
    w = load(); R = pd.read_parquet(OUT / "regime_filter.parquet")
    Ra = set(R.index[R.p_M5_rank_inloop_top5 > 0]); Rb = set(R.index[R.p_M1_breadth >= R.q75_M1_breadth])
    P = SPEC["periods"]; res = {"spec": SPEC, "universe": {"rows": int(len(w)), "days": int(w.trade_date.nunique())}, "factors": {}}
    segs = {"k1_dev": (w.trade_date >= P["dev"][0]) & (w.trade_date <= P["dev"][1]), "k1_wf": (w.trade_date >= P["wf"][0]) & (w.trade_date <= P["wf"][1]),
            "k1_ref": (w.trade_date >= P["ref"][0]) & (w.trade_date <= P["ref"][1]), "k2_Ra": w.trade_date.isin(Ra), "k2_Rb": w.trade_date.isin(Rb)}
    res["baseline"] = {k: stats(w[m]) for k, m in segs.items()}
    for fam in fams:
        for c in FAM[fam]:
            if c not in w or w[c].notna().mean() < 0.3: res["factors"][c] = {"family": fam, "status": "missing"}; continue
            x = w[["ts_code", "trade_date", c, "y", "y_fill", "filled", "seg2", "ubar"]].dropna(subset=[c]).copy()
            g = x.groupby("trade_date")
            ic = g.apply(lambda s: s[c].rank().corr(s.y.rank()) if s[c].nunique() > 3 else np.nan).dropna()
            sgn = 1.0 if ic[(ic.index >= "20220101") & (ic.index <= "20231231")].mean() >= 0 else -1.0
            x["s"] = sgn * x[c] + 1e-9 * np.random.default_rng(0).standard_normal(len(x))   # 微扰打破并列
            x["q"] = x.groupby("trade_date").s.rank(pct=True); x["rk"] = x.groupby("trade_date").s.rank(ascending=False, method="first")
            o = {"family": fam, "orientation": int(sgn), "coverage": round(float(w[c].notna().mean()), 3),
                 "ic_dev": round(float(ic[ic.index <= P["dev"][1]].mean()), 4), "ic_dev_t": round(nw_t(ic[ic.index <= P["dev"][1]].values), 2),
                 "ic_by_year": {y: round(float(v.mean()), 3) for y, v in ic.groupby(ic.index.str[:4])}}
            for k, m in segs.items():
                xm = x[m.reindex(x.index).fillna(False).values]
                o[k] = {"top10": stats(xm[xm.q > 0.9]), "top5": stats(xm[xm.rk <= 5])}
            for k in ("k1_dev", "k2_Rb"):
                xm = x[segs[k].reindex(x.index).fillna(False).values]
                o[f"quintile_gross_bp_{k}"] = [round(float(v.groupby("trade_date").y.mean().mean()) * 1e4, 1) for _, v in xm.groupby(pd.cut(xm.q, [0, .2, .4, .6, .8, 1.0]), observed=True)]
            d = o["k1_dev"]["top10"]
            o["gate_pass_dev_top10"] = bool(d.get("trades", 0) >= 300 and d.get("days", 0) >= 100 and d["gross_bp"] >= 40 and d["win_net"] >= 0.5)
            res["factors"][c] = o
    name = "factor_eval_" + "".join(fams) + ".json"
    (OUT / name).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    print(SPEC["spec_hash"], res["universe"]); [print("baseline", k, v) for k, v in res["baseline"].items()]
    for c, o in res["factors"].items():
        if o.get("status"): print(c, o["status"]); continue
        def L(d): return f"n={d.get('trades')} d={d.get('days')} g={d.get('gross_bp')} t={d.get('t')} win={d.get('win_net')} ex={d.get('excess_bp')}({d.get('excess_t')})"
        print(f"\n[{o['family']}] {c} sgn={o['orientation']} ic={o['ic_dev']}(t{o['ic_dev_t']}) yr={o['ic_by_year']} gate={o['gate_pass_dev_top10']}")
        for k in ("k1_dev", "k1_wf", "k1_ref", "k2_Ra", "k2_Rb"): print(f"   {k:7s} top10 {L(o[k]['top10'])} | top5 {L(o[k]['top5'])}")
        print("   quint dev", o["quintile_gross_bp_k1_dev"], " Rb", o["quintile_gross_bp_k2_Rb"])

if __name__ == "__main__":
    main()
