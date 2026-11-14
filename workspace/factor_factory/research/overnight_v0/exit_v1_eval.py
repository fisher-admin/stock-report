#!/usr/bin/env python3
"""个股级动态离场 exit_v1 · 评估(research-only, 2026-11-14)。规则与选型标准在看数字前写定(本文件 SPEC)。

问题: 买入 = T 日 14:55 线均价不变, 次日能否按个股自己的开盘状态逐只决定"开盘卖 / 09:36 卖 / 拿到 10:00", 比"一律开盘卖"更好?

可执行性(写定):
  - "开盘卖"只能在集合竞价里挂单。看到开盘价之后才决定的卖出, 最早按 09:31 末价成交。
    因此 D0 规则用"竞价限价单"实现: 以 昨收 x (1 + θ) 挂卖单, 开盘价 >= 该价即按开盘价成交, 否则留到 D5 再定。
  - D5 决策(09:35 之后)按 09:36 末价成交(y_m6); 拿到 10:00 按 09:41-10:00 成交量加权均价(y_v1000)。
  - 一字跌停(locked_dn_open)当分钟卖不出: 这类股票任何规则都按 10:00 均价结算(与基线同口径处理, 不做额外假设)。
基线: B0 一律开盘卖(y_open); B1 一律 09:31 卖(y_m1); B2 一律拿到 10:00(y_v1000)。
规则族:
  R_auc(θ)        开盘 >= 昨收 x (1+θ) 时竞价成交; 否则 09:36 卖。                  θ in {-1%, 0, +1%, +2%}
  R_auc_mom(θ)    同上, 但未成交的股票在 09:35 看开盘后 5 分钟: r_o5 > 0 拿到 10:00, 否则 09:36 卖。
  R_mom           全部留到 09:35: r_o5 > 0 拿到 10:00, 否则 09:36 卖(不参与竞价)。
  L               学习规则(两段 LightGBM 小树, 逐月扩展窗口, 只用 dev 集训练):
                    D0 模型预测 Δ0 = y_v1000 - y_open(用 T 日信息 + 开盘跳空 + 竞价成交量占比), 预测 <= 0 -> 竞价卖(以开盘价成交; 实现上等价于"按模型给出的跳空阈值挂限价单");
                    其余到 D5 模型预测 Δ5 = y_v1000 - y_m6(再加开盘后 5 分钟特征), > 0 拿到 10:00, 否则 09:36 卖。
                  注: D0 模型用到了开盘价, 只有当它的决策可以写成"开盘价 >= 某个价格就卖"时才能在竞价里执行; 评估时检查其单调性(跳空越高越该卖), 不满足则 L 记为不可执行。
样本口径: 主口径 = 每日 gap 模型预测 > 成本的前 5 只(即做多账本真实会买的股票); 次口径 = 全部候选(预测 > 成本或当日前 20)。
时段: dev 主段 2024-07..2025-12(L 的前 6 个月只用于训练), dev 参考 2026H1; wf800 = 同时段的留出 800 只股票; time = 2026-07-02..10-08。
统计: 每笔相对 B0 的增益, 先按日平均再做 Newey-West t 与 5 日块自助法 95% 下界; 另报每笔绝对毛收益与扣 19.2bp 后的组合日均净收益。
选型(写定): 只用 dev 主段; 候选 = 上面所有规则; 入选条件 = 主口径增益下界 > 0 且 2024H2、2025 两段增益均为正; 入选者中取主口径增益均值最高的一个。
  然后 wf800 与 time 各读一次(READ_LOG)。time 窗口此前已用于 v1/v2 的检验, 只算半干净; 真正干净的检验来自前瞻影子在冻结日之后记录的早盘 1 分钟路径。
"""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import nw_t, block_lcb, OUT  # noqa: E402

E = OUT / "exit_v1"; COST = 0.00192
THETAS = [-0.01, 0.0, 0.01, 0.02]
F0 = ["gap", "p_gap", "a_e1455", "lim_pct", "vol1_share", "gap_xs_rank", "gap_mkt"]
F5 = F0 + ["r_o1", "r_o5", "hi_o5", "lo_o5", "vol5_share", "up_lim_hit5", "dn_lim_hit5", "r_o5_mkt"]
LGB = {"objective": "regression", "learning_rate": 0.03, "num_leaves": 7, "min_data_in_leaf": 200, "feature_fraction": 0.8, "bagging_fraction": 0.8,
       "bagging_freq": 1, "lambda_l2": 10.0, "verbose": -1, "num_threads": 4, "seed": 20261114}
NROUND = 150
SPEC = {"spec_id": "overnight_exit_v1", "thetas": THETAS, "F0": F0, "F5": F5, "lgb": LGB, "nround": NROUND, "cost": COST,
        "main": ["20240701", "20251231"], "ref": ["20260101", "20260630"], "time": ["20260702", "20261231"],
        "select": "dev main, primary sample, gain LCB>0 and 2024H2 & 2025 gains >0, max mean gain"}
SPEC["spec_hash"] = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()[:16]


def prep(F):
    F = F.copy()
    F["gap_xs_rank"] = F.groupby(["set", "trade_date"]).gap.rank(pct=True)
    F["gap_mkt"] = F.groupby(["set", "trade_date"]).gap.transform("mean")
    F["r_o5_mkt"] = F.groupby(["set", "trade_date"]).r_o5.transform("mean")
    F["primary"] = False
    top = F[F.p_gap > COST].sort_values(["set", "trade_date", "p_gap"], ascending=[True, True, False]).groupby(["set", "trade_date"]).head(5).index
    F.loc[top, "primary"] = True
    lock = F.locked_dn_open
    for c in ("y_open", "y_m1", "y_m6"):
        F[c + "_x"] = np.where(lock, F.y_v1000, F[c])                     # 一字跌停卖不出: 统一按 10:00 均价
    F["d0"] = F.y_v1000 - F.y_open; F["d5"] = F.y_v1000 - F.y_m6
    return F


def policies(F, pL0=None, pL5=None):
    P = {"B0_open": F.y_open_x, "B1_m1": F.y_m1_x, "B2_v1000": F.y_v1000}
    for th in THETAS:
        sold = F.gap >= th
        P[f"R_auc({th:+.0%})"] = np.where(sold, F.y_open_x, F.y_m6_x)
        P[f"R_auc_mom({th:+.0%})"] = np.where(sold, F.y_open_x, np.where(F.r_o5 > 0, F.y_v1000, F.y_m6_x))
    P["R_mom"] = np.where(F.r_o5 > 0, F.y_v1000, F.y_m6_x)
    if pL0 is not None:
        P["L"] = np.where(pL0 <= 0, F.y_open_x, np.where(pL5 > 0, F.y_v1000, F.y_m6_x))
    return pd.DataFrame(P, index=F.index)


def fit_L(F):
    """dev 集逐月扩展窗口; 返回 dev 的样本外预测与 2026-06 截止的最终模型(给 wf800/time 用)。"""
    import lightgbm as lgb
    dev = F[F.set == "dev"].sort_values("trade_date")
    months = sorted({d[:6] for d in dev.trade_date if d >= "20240701"})
    p0 = pd.Series(np.nan, index=F.index); p5 = pd.Series(np.nan, index=F.index)
    def tr(x):
        b0 = lgb.train(LGB, lgb.Dataset(x[F0].values.astype(np.float32), x.d0.clip(-0.05, 0.05).values), NROUND)
        b5 = lgb.train(LGB, lgb.Dataset(x[F5].values.astype(np.float32), x.d5.clip(-0.05, 0.05).values), NROUND)
        return b0, b5
    for m in months:
        x = dev[dev.n_date < m + "01"]
        te = dev[dev.trade_date.str[:6] == m]
        b0, b5 = tr(x)
        p0.loc[te.index] = b0.predict(te[F0].values.astype(np.float32)); p5.loc[te.index] = b5.predict(te[F5].values.astype(np.float32))
    b0, b5 = tr(dev[dev.n_date <= "20260630"])
    oth = F[F.set != "dev"]
    p0.loc[oth.index] = b0.predict(oth[F0].values.astype(np.float32)); p5.loc[oth.index] = b5.predict(oth[F5].values.astype(np.float32))
    # 单调性检查: 只改变跳空, 其余特征固定, D0 预测是否随跳空单调不增(越高开越该卖)
    probe = dev.sample(min(2000, len(dev)), random_state=1)[F0].copy(); grid = np.linspace(-0.04, 0.06, 21); viol = 0
    for _, row in probe.iterrows():
        X = np.tile(row.values, (len(grid), 1)).astype(np.float32); X[:, F0.index("gap")] = grid
        sell = (b0.predict(X) <= 0).astype(int); viol += int(not np.all(np.diff(sell) >= 0))      # 跳空升高时"竞价卖"只能由否变是
    imp = {k: round(float(v), 1) for k, v in zip(F5, b5.feature_importance("gain"))}
    return p0, p5, {"d0_monotone_violation_share": round(viol / len(probe), 3), "d5_gain_importance": imp}


def stats(Fp, Pp, lo, hi):
    m = (Fp.trade_date >= lo) & (Fp.trade_date <= hi)
    f, p = Fp[m], Pp[m]
    if not len(f):
        return {"n": 0}
    days = sorted(f.trade_date.unique()); o = {"n": int(len(f)), "days": len(days)}
    base = p["B0_open"]
    for c in p.columns:
        g = (p[c] - base).groupby(f.trade_date).mean().reindex(days)
        r = {"gross_bp": round(float(p[c].mean()) * 1e4, 1), "gain_vs_B0_bp": round(float((p[c] - base).mean()) * 1e4, 2),
             "gain_t": round(nw_t(g.values), 2) if len(days) >= 20 else None, "gain_lcb_bp": round(block_lcb(g.values) * 1e4, 2) if len(days) >= 60 else None,
             "win_net": round(float((p[c] > COST).mean()), 3)}
        if c != "B0_open":
            half = f.trade_date.str[:4] + np.where(f.trade_date.str[4:6] <= "06", "H1", "H2")
            r["gain_by_half_bp"] = {k: round(float(v.mean()) * 1e4, 1) for k, v in (p[c] - base).groupby(half)}
        o[c] = r
    return o


def book(Fp, Pp, lo, hi):
    """主口径(每日前 5, 1/5 槽位)扣费后日均净收益, 与 v2 报告同口径(未开仓日记 0)。"""
    m = (Fp.trade_date >= lo) & (Fp.trade_date <= hi) & Fp.primary
    f, p = Fp[m], Pp[m]
    out = {}
    for c in p.columns:
        out[c] = round(float(((p[c] - COST).groupby(f.trade_date).sum() / 5).sum() / max(len(set(Fp[(Fp.trade_date >= lo) & (Fp.trade_date <= hi)].trade_date)), 1)) * 1e4, 2)
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dev"
    F = prep(pd.read_parquet(E / "frame.parquet"))
    p0, p5, diag = fit_L(F)
    P = policies(F, p0, p5)
    res = {"spec": SPEC, "mode": mode, "at": time.strftime("%Y-%m-%d %H:%M"), "L_diag": diag}
    if mode == "dev":
        d = F.set == "dev"
        for nm, (lo, hi) in (("main", SPEC["main"]), ("ref_2026H1", SPEC["ref"])):
            res[nm] = {"primary": stats(F[d & F.primary], P[d & F.primary], lo, hi), "all": stats(F[d], P[d], lo, hi), "book_net_bp_per_day": book(F[d], P[d], lo, hi)}
        mp = res["main"]["primary"]; ok = []
        for c in P.columns:
            if c.startswith("B0"):
                continue
            r = mp.get(c, {}); h = r.get("gain_by_half_bp", {})
            if c == "L" and diag["d0_monotone_violation_share"] > 0.05:
                continue
            if (r.get("gain_lcb_bp") or -1) > 0 and h.get("2024H2", -1) > 0 and h.get("2025H1", -1) + h.get("2025H2", -1) > 0:
                ok.append((r["gain_vs_B0_bp"], c))
        res["selected"] = max(ok)[1] if ok else None
        (E / "exit_v1_dev.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
    else:
        sel = json.loads((E / "exit_v1_dev.json").read_text()).get("selected")
        log = E / "READ_LOG.json"; L = json.loads(log.read_text()) if log.exists() else []
        if any(x["mode"] == mode for x in L) and "--force" not in sys.argv:
            sys.exit(f"{mode} 已读过一次")
        s = F.set == mode; lo, hi = (SPEC["main"][0], SPEC["ref"][1]) if mode == "wf800" else SPEC["time"]
        cols = ["B0_open", "B1_m1", "B2_v1000"] + ([sel] if sel else []) + [c for c in P.columns if c.startswith("R_") or c == "L"]
        cols = list(dict.fromkeys(cols))
        res["selected"] = sel
        res["primary"] = stats(F[s & F.primary], P.loc[s & F.primary, cols], lo, hi); res["all"] = stats(F[s], P.loc[s, cols], lo, hi)
        res["book_net_bp_per_day"] = book(F[s], P.loc[s, cols], lo, hi)
        (E / f"exit_v1_{mode}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
        log.write_text(json.dumps(L + [{"mode": mode, "at": res["at"], "selected": sel}], ensure_ascii=False))
    print(SPEC["spec_hash"], mode, "selected:", res.get("selected"), json.dumps(diag, ensure_ascii=False)[:300])
    for seg in ("main", "ref_2026H1") if mode == "dev" else ("",):
        r = res[seg] if seg else res
        for smp in ("primary", "all"):
            print(f"\n== {seg} {smp} n={r[smp].get('n')} days={r[smp].get('days')}")
            for c, v in r[smp].items():
                if isinstance(v, dict):
                    print(f"{c:18s} gross {v['gross_bp']:7.1f}  gain {v['gain_vs_B0_bp']:7.2f}  t {v['gain_t']}  lcb {v['gain_lcb_bp']}  win {v['win_net']}  {v.get('gain_by_half_bp', '')}")
        print("book net bp/day:", r["book_net_bp_per_day"])


if __name__ == "__main__":
    main()
