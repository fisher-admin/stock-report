#!/usr/bin/env python3
"""B 族: 事件因子(research-only, 2026-11-12)。规范见 RESEARCH_SPEC_stage2.md, 时间戳审查见 factor_cards/B_timestamp_audit.md。

对每个事件子集, 在固定执行协议(14:50-14:55 买, 次日 09:30-09:40 卖)下报告多头端:
  口径1 = 全部交易日;  口径2 = 仅 A 族滚动样本外判定"可开仓"的日子(2024-01 起)。
公告日期 D 的含义: 公告在 eff(D) 的前一交易日收盘后至 eff(D) 开盘前发布, eff(D)=不早于 D 的首个交易日。
  tonight(D): T 的下一交易日 == eff(D)  -> 内容在 T 14:45 不可知, 只有"事前排定"的事件可用
  lastnight(D): eff(D) == T              -> 可用, 属"第二个隔夜段"
"""
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, OUT  # noqa: E402

EV = Path(os.environ.get("OVN_EVENTS") or OUT / "events"); CARDS = OUT / "factor_cards"; CARDS.mkdir(exist_ok=True)
COST = 0.00192
POS = {"预增", "略增", "扭亏", "续盈"}; NEG = {"预减", "略减", "首亏", "续亏"}
SPEC = {"spec_id": "overnight_event_factors_v0", "cost": COST, "dev": ["20220104", "20251231"], "reference": ["20260101", "20260701"],
        "sample_primary": "frame_wide.elig (ADV20>=1亿, 14:40-14:45 成交>=300万, 未封板), 仅成交笔(保护价 1.003)",
        "sample_wide": "研究800主板全部未封板行, 14:40-14:45 成交>=100万",
        "regime_filters": {"Ra": "M5_rank_inloop_top5 预测>0", "Rb": "M1_breadth 预测>=训练期75分位"},
        "gate": "毛均值>=40bp & 净胜率>=50% & >=300笔 & >=100天(开发期); 族级止损以口径2毛均值<20bp 为准"}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

def rd(name):
    f = EV / f"{name}.parquet"
    if not f.exists(): return None
    d = pd.read_parquet(f)
    for c in d.columns:
        if str(d[c].dtype) == "string": d[c] = d[c].astype(object).where(d[c].notna(), None)
    return d

def main():
    w = pd.read_parquet(OUT / "frame_wide.parquet"); R = None if "--flags-only" in sys.argv else pd.read_parquet(OUT / "regime_filter.parquet")
    cal = np.array(sorted(pd.read_parquet(EV / "index_daily.parquet").trade_date.astype(str).unique()))
    def eff(d):       # 不早于 d 的首个交易日
        d = pd.Series(d).astype(object); ok = d.notna(); out = pd.Series([None] * len(d), index=d.index, dtype=object)
        pos = np.searchsorted(cal, d[ok].astype(str).values, side="left"); good = pos < len(cal)
        out[ok] = np.where(good, cal[np.clip(pos, 0, len(cal) - 1)], None); return out.values
    def prev_td(e):   # e 的前一交易日
        e = pd.Series(e).astype(object); ok = e.notna(); out = pd.Series([None] * len(e), index=e.index, dtype=object)
        pos = np.searchsorted(cal, e[ok].astype(str).values, side="left") - 1
        out[ok] = np.where(pos >= 0, cal[np.clip(pos, 0, len(cal) - 1)], None); return out.values
    codes = set(w.ts_code.unique()); key = ["ts_code", "trade_date"]
    flags = {}   # name -> DataFrame[ts_code, trade_date] (+ optional extra cols)
    meta = {}
    def add(name, df, kind, extra=None):
        df = df[df.ts_code.isin(codes) & df.trade_date.notna()]; cols = key + (extra or [])
        flags[name] = df[cols].drop_duplicates(key); meta[name] = kind

    # ---------- B1 定期报告披露前夜(事前排定)
    dd = rd("disclosure_date")
    if dd is not None:
        dd = dd[dd.actual_date.notna()].copy(); dd["e"] = eff(dd.actual_date); dd["trade_date"] = prev_td(dd.e)
        # 可知性: 计划披露日未被修正(modify_date 为空)且计划==实际, 计划公告日严格早于 T
        dd["known"] = dd.modify_date.isna() & (dd.pre_date == dd.actual_date) & (dd.ann_date.astype(str) < dd.trade_date.astype(str))
        meta["_disclosure_known_share"] = round(float(dd.known.mean()), 4)
        k = dd[dd.known]
        add("B1a_report_eve_all", k, "scheduled")
        for q, nm in [("1231", "annual"), ("0331", "q1"), ("0630", "h1"), ("0930", "q3")]:
            add(f"B1a_report_eve_{nm}", k[k.end_date.astype(str).str[4:] == q], "scheduled")
        fc = rd("forecast")
        if fc is not None:
            fc = fc[fc.ann_date.notna()].copy(); fc["fe"] = eff(fc.ann_date)
            f1 = fc.sort_values("ann_date").drop_duplicates(["ts_code", "end_date"], keep="last")[["ts_code", "end_date", "type", "fe", "p_change_min", "p_change_max"]]
            m = k.merge(f1, on=["ts_code", "end_date"], how="inner"); m = m[m.fe.astype(str) <= m.trade_date.astype(str)]   # 预告已在 T 日或之前生效
            add("B1b_report_eve_pos_forecast", m[m.type.isin(POS)], "scheduled")
            add("B1c_report_eve_neg_forecast", m[m.type.isin(NEG)], "scheduled")
            add("B1d_report_eve_big_pos_forecast", m[m.type.isin({"预增", "扭亏"}) & (m.p_change_min.astype(float) >= 50)], "scheduled")
            nf = k.merge(f1[["ts_code", "end_date"]].assign(has=1), on=["ts_code", "end_date"], how="left"); add("B1e_report_eve_no_forecast", nf[nf.has.isna()], "scheduled")
            # 第二个隔夜段: 预告昨晚发布
            fc["trade_date"] = fc.fe
            add("B4a_pos_forecast_lastnight", fc[fc.type.isin(POS)], "second_night"); add("B4a2_big_pos_forecast_lastnight", fc[fc.type.isin({"预增", "扭亏"}) & (fc.p_change_min.astype(float) >= 50)], "second_night")
            fc["trade_date"] = prev_td(fc.fe)
            add("X1_pos_forecast_tonight", fc[fc.type.isin(POS)], "FORBIDDEN_control"); add("X1b_neg_forecast_tonight", fc[fc.type.isin(NEG)], "FORBIDDEN_control")
    # ---------- B2 除权除息前一日(事前排定)
    dv = rd("dividend")
    if dv is not None:
        dv = dv[(dv.div_proc == "实施") & dv.ex_date.notna()].copy(); dv["trade_date"] = prev_td(eff(dv.ex_date))
        dv["known"] = dv.imp_ann_date.notna() & (pd.Series(eff(dv.imp_ann_date), index=dv.index).astype(str) <= dv.trade_date.astype(str))
        meta["_dividend_known_share"] = round(float(dv.known.mean()), 4)
        dv = dv[dv.known]; dv["cash"] = dv.cash_div_tax.astype(float).fillna(0); dv["stk"] = dv.stk_div.astype(float).fillna(0)
        dv = dv.groupby(key, as_index=False).agg(cash=("cash", "sum"), stk=("stk", "sum"))
        add("B2a_exdiv_eve_all", dv, "scheduled", ["cash", "stk"]); add("B2b_exdiv_eve_stock_div", dv[dv.stk > 0], "scheduled", ["cash", "stk"])
    # ---------- B3 指数调样生效前一日(事前排定): 6/12 月第二个周五的下一交易日生效
    iw = rd("index_weight")
    if iw is not None:
        iw["ym"] = iw.trade_date.astype(str).str[:6]; last = iw.groupby(["index_code", "ym"]).trade_date.transform("max"); iw = iw[iw.trade_date == last]
        adds, dels = [], []
        for (ic), g in iw.groupby("index_code"):
            by = {ym: set(x.con_code) for ym, x in g.groupby("ym")}
            for ym in sorted(by):
                if ym[4:] not in ("06", "12"): continue
                pm = f"{ym[:4]}{int(ym[4:]) - 1:02d}"
                if pm not in by: continue
                fr = [d for d in pd.date_range(ym + "01", periods=28) if d.dayofweek == 4][1].strftime("%Y%m%d")
                T = cal[np.searchsorted(cal, fr, side="right") - 1]     # 第二个周五(若休市取其前最近交易日)
                adds += [(c, T) for c in by[ym] - by[pm]]; dels += [(c, T) for c in by[pm] - by[ym]]
        add("B3a_index_add_eve", pd.DataFrame(adds, columns=key), "scheduled"); add("B3b_index_del_eve", pd.DataFrame(dels, columns=key), "scheduled")
    # ---------- B4 昨晚公告(第二个隔夜段) 与 X 禁用对照(今晚公告)
    rp = rd("repurchase")
    if rp is not None:
        rp = rp[rp.ann_date.notna()].copy(); rp["e"] = eff(rp.ann_date); p0 = rp[rp.proc.isin(["预案", "提议"])]
        add("B4b_repurchase_plan_lastnight", p0.assign(trade_date=p0.e), "second_night"); add("X2_repurchase_plan_tonight", p0.assign(trade_date=prev_td(p0.e)), "FORBIDDEN_control")
    ht = rd("stk_holdertrade")
    if ht is not None:
        ht = ht[ht.ann_date.notna() & (ht.in_de == "IN")].copy(); ht["e"] = eff(ht.ann_date)
        add("B4c_holder_increase_lastnight", ht.assign(trade_date=ht.e), "second_night"); add("X3_holder_increase_tonight", ht.assign(trade_date=prev_td(ht.e)), "FORBIDDEN_control")
    tl = rd("top_list")
    if tl is not None:
        tl = tl.copy(); tl["td"] = tl.trade_date.astype(str); nxt = dict(zip(cal[:-1], cal[1:])); tl["trade_date"] = tl.td.map(nxt)
        add("B4d_toplist_netbuy_prevday", tl[tl.net_amount.astype(float) > 0], "second_night"); add("B4e_toplist_any_prevday", tl, "second_night")
        add("X4_toplist_netbuy_sameday", tl[tl.net_amount.astype(float) > 0].assign(trade_date=tl.td), "FORBIDDEN_control")
    ti = rd("top_inst")
    if ti is not None:
        ti = ti[ti.exalter == "机构专用"].copy(); ti["td"] = ti.trade_date.astype(str); nxt = dict(zip(cal[:-1], cal[1:]))
        g = ti.groupby(["ts_code", "td"], as_index=False).net_buy.sum(); g["trade_date"] = g.td.map(nxt)
        add("B4f_inst_netbuy_prevday", g[g.net_buy > 0], "second_night")

    # ---------- 落盘可交易事件标记(供阶段 3 合成; 禁用对照 X* 不落盘)
    fw = None
    for name, fl in flags.items():
        if meta[name] == "FORBIDDEN_control": continue
        t = fl[key].assign(**{name: 1.0}); fw = t if fw is None else fw.merge(t, on=key, how="outer")
    if fw is not None: fw.fillna(0.0).to_parquet(OUT / "event_flags.parquet")
    if "--flags-only" in sys.argv: print("flags", None if fw is None else fw.shape); return
    # ---------- 评估
    w["wide"] = (~w.locked) & w.y.notna() & (w.amt_last5 >= 1e6)
    ubar = w[w.elig].groupby("trade_date").y.mean().rename("ubar"); w = w.join(ubar, on="trade_date")
    Ra = set(R.index[(R.p_M5_rank_inloop_top5 > 0)]); Rb = set(R.index[(R.p_M1_breadth >= R.q75_M1_breadth)]); wf_days = set(R.index)
    def stats(s, tax=None):
        f = s[s.filled]; n = len(f)
        if n == 0: return {"trades": 0}
        dm = f.groupby("trade_date").y_fill.mean(); ex = (f.y_fill - f.ubar).groupby(f.trade_date).mean()
        o = {"trades": int(n), "days": int(dm.size), "gross_bp": round(float(f.y_fill.mean()) * 1e4, 1),
             "day_avg_gross_bp": round(float(dm.mean()) * 1e4, 1), "t": round(nw_t(dm.values), 2) if dm.size >= 20 else None,
             "win_net": round(float((f.y_fill > COST).mean()), 3),
             "avg_win_bp": round(float((f.y_fill - COST)[f.y_fill > COST].mean()) * 1e4, 1) if (f.y_fill > COST).any() else None,
             "avg_loss_bp": round(float((f.y_fill - COST)[f.y_fill <= COST].mean()) * 1e4, 1) if (f.y_fill <= COST).any() else None,
             "excess_vs_same_day_universe_bp": round(float(ex.mean()) * 1e4, 1), "excess_t": round(nw_t(ex.values), 2) if ex.size >= 20 else None,
             "seg_bp": [round(float(f[c].mean()) * 1e4, 1) for c in ("seg1n", "seg2", "seg3")], "p_overnight_ge_1pct": round(float((f.seg2 >= 0.01).mean()), 3)}
        if tax is not None and n: o["gross_after_div_tax_bp"] = round(float((f.y_fill - 0.2 * f.cash / f.px_proxy).mean()) * 1e4, 1)
        return o
    res = {"spec": SPEC, "meta": {k: v for k, v in meta.items() if k.startswith("_")}, "events": {}}
    base = w[w.elig]; res["baseline_all_elig"] = {"dev": stats(base[base.trade_date <= SPEC["dev"][1]]), "regime_Ra": stats(base[base.trade_date.isin(Ra)]), "regime_Rb": stats(base[base.trade_date.isin(Rb)])}
    w["px_proxy"] = np.exp(w.log_px)
    for name, fl in sorted(flags.items()):
        s = w.merge(fl, on=key, how="inner"); tax = True if name.startswith("B2") else None
        o = {"kind": meta[name], "event_rows_in_research_universe": int(len(s))}
        for uni, mask in [("elig", s.elig), ("wide", s.wide)]:
            x = s[mask]; dev = x[x.trade_date <= SPEC["dev"][1]]
            o[uni] = {"k1_dev_all_days": stats(dev, tax), "k1_ref_2026H1": stats(x[x.trade_date > SPEC["dev"][1]], tax),
                      "k1_wf_days_2024_2026H1": stats(x[x.trade_date.isin(wf_days)], tax),
                      "k2_regime_Ra": stats(x[x.trade_date.isin(Ra)], tax), "k2_regime_Rb": stats(x[x.trade_date.isin(Rb)], tax),
                      "dev_by_year_gross_bp": {y: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for y, v in dev[dev.filled].groupby(dev[dev.filled].trade_date.str[:4]).y_fill}}
        d = o["elig"]["k1_dev_all_days"]
        o["gate_pass_dev_elig"] = bool(d.get("trades", 0) >= 300 and d.get("days", 0) >= 100 and d["gross_bp"] >= 40 and d["win_net"] >= 0.5)
        res["events"][name] = o
    (OUT / "event_factors.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    print(SPEC["spec_hash"], res["meta"]); print("baseline", json.dumps(res["baseline_all_elig"], ensure_ascii=False))
    def line(d): return "n=%5s d=%4s g=%6s t=%5s win=%5s ex=%6s seg=%s" % (d.get("trades"), d.get("days"), d.get("gross_bp"), d.get("t"), d.get("win_net"), d.get("excess_vs_same_day_universe_bp"), d.get("seg_bp")) if d.get("trades") else "n=0"
    for name, o in res["events"].items():
        print(f"\n{name} [{o['kind']}] rows={o['event_rows_in_research_universe']} gate={o['gate_pass_dev_elig']}")
        for uni in ("elig", "wide"):
            for k in ("k1_dev_all_days", "k1_ref_2026H1", "k1_wf_days_2024_2026H1", "k2_regime_Ra", "k2_regime_Rb"): print(f"  {uni:5s} {k:24s}", line(o[uni][k]), o[uni][k].get("gross_after_div_tax_bp", ""))
            print("        by_year", o[uni]["dev_by_year_gross_bp"])

if __name__ == "__main__":
    main()
