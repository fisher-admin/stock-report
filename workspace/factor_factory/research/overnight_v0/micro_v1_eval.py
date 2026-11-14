#!/usr/bin/env python3
"""micro_v1 · 评估(规格见 RESEARCH_SPEC_micro_v1.md, 关卡写定不改)。

用法: micro_v1_eval.py dev            只读 dev(研究股票), 逐月扩展窗口样本外 2024-07..2026-06, 判关卡
      micro_v1_eval.py wf800|time     仅当 dev 有协议过关时可运行, 各读一次(READ_LOG)
"""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, OUT  # noqa: E402
from micro_v1_frame import FEATS  # noqa: E402

M = OUT / "micro_v1"; COST = 0.00192; K = 5
LGB = {"objective": "regression", "learning_rate": 0.03, "num_leaves": 7, "min_data_in_leaf": 200, "feature_fraction": 0.8, "bagging_fraction": 0.8,
       "bagging_freq": 1, "lambda_l2": 10.0, "verbose": -1, "num_threads": 4, "seed": 20261114}
NROUND = 150
X = ["pR_gap", "r_pR"] + ["r_" + f for f in FEATS] + ["head_pR_mean"]
SPEC = {"spec_id": "overnight_micro_v1", "feats": FEATS, "X": X, "lgb": LGB, "nround": NROUND, "K": K, "cost": COST,
        "oos": ["20240701", "20260630"], "main": ["20240701", "20251231"], "ref": ["20260101", "20260630"]}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()[:16]


def labels():
    """y_A = 14:55 线均价买、次日开盘卖(与影子一致, filled = 保护价成交); y_B = 收盘竞价买、次日开盘卖(收盘封涨停视为买不到)。"""
    out = []
    for s, pf, tf, wf in (("dev", "v2_predictions.parquet", "v2_timing.parquet", "frame_wide.parquet"),
                          ("wf800", "holdout_800/v2_wf800_predictions.parquet", "holdout_800/v2_timing.parquet", "holdout_800/frame_wide.parquet"),
                          ("time", "exit_v1/time_holdout_gap_pred.parquet", "holdout_time/v2_timing.parquet", "holdout_time/frame_wide.parquet")):
        P = pd.read_parquet(OUT / pf, columns=["ts_code", "trade_date", "filled"])
        T = pd.read_parquet(OUT / tf, columns=["ts_code", "trade_date", "a_e1455", "b_xopen"])
        W = pd.read_parquet(OUT / wf, columns=["ts_code", "trade_date", "lim_pct", "c2c"])
        x = P.merge(T, on=["ts_code", "trade_date"], how="left").merge(W, on=["ts_code", "trade_date"], how="left")
        x["y_A"] = (1 + x.a_e1455) * (1 + x.b_xopen) - 1; x["y_B"] = x.b_xopen
        x["fill_A"] = x.filled.astype(bool); x["fill_B"] = ~(x.c2c >= x.lim_pct - 0.0005)
        x["set"] = s; out.append(x[["ts_code", "trade_date", "set", "y_A", "y_B", "fill_A", "fill_B"]])
    return pd.concat(out)


def frame(proto):
    H = pd.read_parquet(M / "head.parquet")
    F = pd.read_parquet(M / f"feat_{proto}.parquet")
    D = H.merge(F, on=["ts_code", "trade_date"], how="left").merge(labels(), on=["ts_code", "trade_date", "set"], how="left")
    g = D.groupby(["set", "trade_date"])
    D["r_pR"] = g.pR_gap.rank(pct=True); D["head_pR_mean"] = g.pR_gap.transform("mean")
    for f in FEATS:
        D["r_" + f] = g[f].rank(pct=True)
    D["y"] = D["y_" + proto]; D["fill"] = D["fill_" + proto].fillna(False)
    return D[D.y.notna()].copy()


def fit_predict(D, final=False):
    import lightgbm as lgb
    dev = D[D.set == "dev"]; p = pd.Series(np.nan, index=D.index)
    tr_ = lambda x: lgb.train(LGB, lgb.Dataset(x[X].values.astype(np.float32), x.y.clip(-0.06, 0.06).values), NROUND)  # noqa: E731
    if not final:
        for m in sorted({d[:6] for d in dev.trade_date if d >= SPEC["oos"][0]}):
            tr = dev[dev.trade_date < m + "01"]; tr = tr[tr.trade_date < tr.trade_date.max()]
            te = dev[dev.trade_date.str[:6] == m]; p.loc[te.index] = tr_(tr).predict(te[X].values.astype(np.float32))
        return p, None
    tr = dev[dev.trade_date <= "20260630"]; tr = tr[tr.trade_date < tr.trade_date.max()]; b = tr_(tr)
    oth = D[D.set != "dev"]; p.loc[oth.index] = b.predict(oth[X].values.astype(np.float32))
    return p, b


def evaluate(D, lo, hi):
    s = D[(D.trade_date >= lo) & (D.trade_date <= hi) & D.p1.notna()]
    days = sorted(s.trade_date.unique()); half = lambda d: d.str[:4] + np.where(d.str[4:6] <= "06", "H1", "H2")  # noqa: E731
    ic0 = s.groupby("trade_date").apply(lambda z: z.pR_gap.rank().corr(z.y.rank())); ic1 = s.groupby("trade_date").apply(lambda z: z.p1.rank().corr(z.y.rank()))
    dic = (ic1 - ic0).dropna()
    t0 = s.sort_values(["trade_date", "pR_gap"], ascending=[True, False]).groupby("trade_date").head(K)
    t1 = s.sort_values(["trade_date", "p1"], ascending=[True, False]).groupby("trade_date").head(K)
    f0, f1 = t0[t0.fill], t1[t1.fill]
    dg = (f1.groupby("trade_date").y.mean() - f0.groupby("trade_date").y.mean()).dropna()
    def book(col):
        c = s[s[col] > COST].sort_values(["trade_date", col], ascending=[True, False]).groupby("trade_date").head(K); f = c[c.fill]
        n = (f.y - COST).groupby(f.trade_date).sum().div(K).reindex(days).fillna(0.0)
        return {"open_days": int(c.trade_date.nunique()), "trades": int(len(f)), "gross_bp": round(float(f.y.mean()) * 1e4, 1) if len(f) else None,
                "median_bp": round(float(f.y.median()) * 1e4, 1) if len(f) else None, "win_net": round(float((f.y > COST).mean()), 3) if len(f) else None,
                "net_bp_day": round(float(n.mean()) * 1e4, 2), "net_t": round(nw_t(n.values), 2), "net_lcb": round(block_lcb(n.values) * 1e4, 2) if len(days) >= 60 else None,
                "by_half_gross": {k: [int(len(v)), round(float(v.mean()) * 1e4, 1)] for k, v in f.y.groupby(half(f.trade_date))}}
    o = {"days": len(days), "rows": int(len(s)),
         "ic_M0": round(float(ic0.mean()), 4), "ic_M1": round(float(ic1.mean()), 4), "ic_diff": round(float(dic.mean()), 4), "ic_diff_t": round(nw_t(dic.values), 2),
         "head_top5_gross_M0": round(float(f0.y.mean()) * 1e4, 1), "head_top5_gross_M1": round(float(f1.y.mean()) * 1e4, 1),
         "head_top5_diff_bp": round(float(dg.mean()) * 1e4, 1), "head_top5_diff_t": round(nw_t(dg.values), 2),
         "head_top5_diff_by_half": {k: round(float(v.mean()) * 1e4, 1) for k, v in dg.groupby(half(pd.Series(dg.index, index=dg.index)))},
         "book_M0": book("pR_gap"), "book_M1": book("p1")}
    # 预测幅度校准: M1 预测分箱 -> 实现
    b = pd.cut(s.p1, [-1, 0, 0.00192, 0.004, 0.008, 1]); o["calib_M1"] = {str(k): [int(len(v)), round(float(v.y.mean()) * 1e4, 1)] for k, v in s.groupby(b, observed=True)}
    b = pd.cut(s.pR_gap, [-1, 0, 0.00192, 0.004, 0.008, 1]); o["calib_M0"] = {str(k): [int(len(v)), round(float(v.y.mean()) * 1e4, 1)] for k, v in s.groupby(b, observed=True)}
    return o


def gates(o_main, o_ref, o_all):
    g1 = o_main["ic_diff_t"] >= 2 and o_ref["ic_diff"] > 0
    h = o_all["head_top5_diff_by_half"]; y25 = np.mean([v for k, v in h.items() if k.startswith("2025")]) if any(k.startswith("2025") for k in h) else -1
    g2 = o_all["head_top5_diff_bp"] >= 10 and h.get("2024H2", -1) > 0 and y25 > 0 and h.get("2026H1", -1) > 0
    b0, b1 = o_all["book_M0"], o_all["book_M1"]
    g3 = (b1["gross_bp"] or -1e9) >= (b0["gross_bp"] or 0) + 10 and b1["open_days"] >= 0.7 * b0["open_days"]
    return {"g1_rank": bool(g1), "g2_head_magnitude": bool(g2), "g3_book": bool(g3), "pass": bool(g1 and g2 and g3)}


def univariate(D, lo, hi):
    s = D[(D.set == "dev") & (D.trade_date >= lo) & (D.trade_date <= hi)]; out = {}
    for f in FEATS:
        ic = s.groupby("trade_date").apply(lambda z: z[f].rank().corr(z.y.rank())).dropna()
        out[f] = [round(float(ic.mean()), 4), round(nw_t(ic.values), 2)]
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1][1])))


def main():
    mode = sys.argv[1]; res = {"spec": SPEC, "mode": mode, "at": time.strftime("%Y-%m-%d %H:%M")}
    if mode == "dev":
        for proto in ("A", "B"):
            D = frame(proto); D["p1"], _ = fit_predict(D); d = D[D.set == "dev"]
            r = {"main": evaluate(d, *SPEC["main"]), "ref": evaluate(d, *SPEC["ref"]), "all": evaluate(d, *SPEC["oos"]), "univariate_head_ic_oos": univariate(D, *SPEC["oos"])}
            r["gates"] = gates(r["main"], r["ref"], r["all"]); res[proto] = r
        ok = [p for p in ("A", "B") if res[p]["gates"]["pass"]]
        res["selected"] = max(ok, key=lambda p: res[p]["all"]["book_M1"]["gross_bp"]) if ok else None
        (M / "micro_v1_dev.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    else:
        dv = json.loads((M / "micro_v1_dev.json").read_text()); sel = dv.get("selected")
        if not sel: sys.exit("dev 未过关, 规格关闭, 不读留出")
        log = M / "READ_LOG.json"; L = json.loads(log.read_text()) if log.exists() else []
        if any(x["mode"] == mode for x in L): sys.exit(f"{mode} 已读过一次")
        D = frame(sel); D["p1"], b = fit_predict(D, final=True); s = D[D.set == mode]
        lo, hi = ("20240701", "20260630") if mode == "wf800" else ("20260702", "20261231")
        res[sel] = {"all": evaluate(s, lo, hi)}; res["selected"] = sel
        (M / f"micro_v1_{mode}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
        log.write_text(json.dumps(L + [{"mode": mode, "at": res["at"], "protocol": sel}], ensure_ascii=False))
        if mode == "time":
            b.save_model(str(M / f"model_micro_{sel}.txt"))
    print(SPEC["spec_hash"], mode, "selected:", res.get("selected"))
    for p in ("A", "B"):
        if p not in res: continue
        for seg, o in res[p].items():
            if seg in ("gates", "univariate_head_ic_oos"):
                print(p, seg, json.dumps(o, ensure_ascii=False)); continue
            print(f"\n== {p} {seg}: days {o['days']} rows {o['rows']} | IC M0 {o['ic_M0']} M1 {o['ic_M1']} diff {o['ic_diff']} t {o['ic_diff_t']} | head top5 M0 {o['head_top5_gross_M0']} M1 {o['head_top5_gross_M1']} diff {o['head_top5_diff_bp']} t {o['head_top5_diff_t']} {o['head_top5_diff_by_half']}")
            for k in ("book_M0", "book_M1"): print("  ", k, json.dumps(o[k], ensure_ascii=False))
            print("   calib M0", o["calib_M0"]); print("   calib M1", o["calib_M1"])


if __name__ == "__main__":
    main()
