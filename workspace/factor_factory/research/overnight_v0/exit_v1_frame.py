#!/usr/bin/env python3
"""个股级动态离场 exit_v1 · 样本构造(research-only, 2026-11-14)。只做格式转换与对齐, 不看任何收益统计。

样本: exit_v1/candidates.parquet 里的股票日 T(纯隔夜做多模型 gap 的逐月滚动样本外预测 > 成本, 或当日前 20 名):
  dev    研究股票 2024-01..2026-06(v2_predictions, 逐月滚动)
  wf800  留出 800 只股票 2024-01..2026-06(v2_wf800_predictions, 用研究股票训练的逐月模型)
  time   冻结 gap 模型在 2026-07-02..10-08(研究股票与其他非留出股票各取前 20)
入场: T 日 14:55 线均价(与 v2 相同, a_e1455 = close_T / e1455 - 1)。
离场日 N 的 1 分钟分时(tdx /api/minute, 09:31..15:00 每分钟末价与成交量), 只取 09:31..10:30 用于决策与结算。

每行输出(收益都按"T 日 14:55 买入"计, y_x = (1 + a_e1455) * (P_x / pre_close_N) - 1):
  y_open   开盘集合竞价价(日线 open)
  y_m{k}   第 k 分钟末价(k = 1 表示 09:31, 5 = 09:35, 10 = 09:40, 30 = 10:00, 60 = 10:30)
  y_v0940  09:31..09:40 成交量加权均价; y_v1000 09:41..10:00 成交量加权均价(与 5 分钟线口径 x1000 对应)
决策特征(只用到各自决策时点为止的信息):
  D0(09:25 集合竞价结束): gap = open / pre_close_N - 1, 以及 T 日已知的一切(p_gap, a_e1455, 板块, 涨跌停幅度)
  D5(09:35): r_o5 = m5 / open - 1, hi_o5 / lo_o5(开盘后 5 分钟最高/最低相对开盘), vol5_share = 前 5 分钟量 / T 日全天量,
             up_lim_hit5(5 分钟内是否触及涨停), dn_lim_hit5
跌停一字(开盘 = 跌停且前 5 分钟最高 = 最低)标 locked_dn_open, 评估时不允许在当分钟卖出。
输出: outputs/overnight_v0/exit_v1/frame.parquet
"""
from __future__ import annotations
import gzip, os, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import OUT, DAILY_DIR  # noqa: E402

E = OUT / "exit_v1"
K_MIN = [1, 2, 3, 5, 6, 10, 11, 15, 16, 30, 31, 60]


def read_minute1(path):
    rows = {}
    with gzip.open(path, "rt") as g:
        for line in g:
            x = line.rstrip("\n").split(",")
            if len(x) < 2 + 480:
                continue
            p = np.array(x[2:242], float) / 1000; v = np.array(x[242:482], float)
            rows[(x[0], x[1])] = (p, v)
    return rows


def main():
    C = pd.read_parquet(E / "candidates.parquet")
    M = read_minute1(sys.argv[1] if len(sys.argv) > 1 else E / "minute1.csv.gz")
    T = pd.concat([pd.read_parquet(OUT / f, columns=["ts_code", "trade_date", "a_e1455"]) for f in
                   ("v2_timing.parquet", "holdout_800/v2_timing.parquet", "holdout_time/v2_timing.parquet")]).drop_duplicates(["ts_code", "trade_date"])
    C = C.merge(T, on=["ts_code", "trade_date"], how="left")
    out = []
    for code, g in C.groupby("ts_code"):
        f = DAILY_DIR / f"{code}.parquet"
        if not f.exists():
            continue
        d = pd.read_parquet(f, columns=["trade_date", "open", "high", "low", "close", "pre_close", "vol"]).astype({"trade_date": str}).drop_duplicates("trade_date").set_index("trade_date")
        for r in g.itertuples(index=False):
            key = (code, r.n_date)
            if key not in M or r.n_date not in d.index or r.trade_date not in d.index:
                continue
            p, v = M[key]
            if np.isnan(p[:60]).any() or (p[:60] <= 0).any():
                continue
            dn, dt = d.loc[r.n_date], d.loc[r.trade_date]
            pc, op = float(dn.pre_close), float(dn.open)
            lim = 0.20 if code[:2] == "30" else 0.10
            up, lo_ = round(pc * (1 + lim) + 1e-9, 2), round(pc * (1 - lim) + 1e-9, 2)
            o = {"ts_code": code, "trade_date": r.trade_date, "n_date": r.n_date, "set": r.set, "p_gap": r.p_gap, "a_e1455": r.a_e1455,
                 "pre_close_N": pc, "open_N": op, "gap": op / pc - 1, "lim_pct": lim, "vol_T_hand": float(dt.vol), "close_T": float(dt.close)}
            o["y_open"] = (1 + r.a_e1455) * (op / pc) - 1
            for k in K_MIN:
                o[f"y_m{k}"] = (1 + r.a_e1455) * (p[k - 1] / pc) - 1
            for nm, a, b in (("v0940", 0, 10), ("v1000", 10, 30)):
                vv = v[a:b]; o[f"y_{nm}"] = (1 + r.a_e1455) * ((np.dot(p[a:b], vv) / vv.sum() if vv.sum() > 0 else p[a:b].mean()) / pc) - 1
            p5 = p[:5]
            o.update({"r_o5": p[4] / op - 1, "r_o1": p[0] / op - 1, "hi_o5": p5.max() / op - 1, "lo_o5": p5.min() / op - 1,
                      "vol1_share": v[0] / max(float(dt.vol), 1), "vol5_share": v[:5].sum() / max(float(dt.vol), 1),
                      "up_lim_hit5": float(p5.max() >= up - 1e-6), "dn_lim_hit5": float(p5.min() <= lo_ + 1e-6),
                      "locked_dn_open": bool(op <= lo_ + 1e-6 and p5.max() <= lo_ + 1e-6), "locked_up_open": bool(op >= up - 1e-6 and p5.min() >= up - 1e-6),
                      "r_o10": p[9] / op - 1, "r_o30": p[29] / op - 1})
            out.append(o)
    F = pd.DataFrame(out)
    F.to_parquet(E / "frame.parquet")
    print(F.shape, F.groupby("set").size().to_dict(), "matched share", round(len(F) / len(C), 3))
    print(F[["gap", "r_o5", "vol5_share", "locked_dn_open", "locked_up_open"]].describe().T[["count", "mean", "50%"]].to_string())


if __name__ == "__main__":
    main()
