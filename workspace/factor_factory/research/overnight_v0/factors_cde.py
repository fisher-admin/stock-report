#!/usr/bin/env python3
"""C/D/E 族因子构造(research-only, 2026-11-13)。全部在 T 日 14:45 或更早可知。

D 尾盘资金(来自 5 分钟线, 14:45 及之前的根):
  d_amt_share_last30  14:20-14:45 六根成交额 / 当日截至 14:45 成交额
  d_amt_share_last5   = last5_amt_share(已有, 14:40-14:45)
  d_ret_last30        px1445 / close(14:15) - 1
  d_vwap30_dev        px1445 / vwap(14:20-14:45) - 1
  d_pv_corr_pm        午后各根收益与成交额的相关(13:05-14:45)
  d_up_amt_share_pm   午后收阳根成交额占比
  d_tail_flow         d_ret_last30 的符号 x d_amt_share_last30 的截面秩(量价配合)
  d_rv_last30         近 30 分钟成交额 / 过去 20 日同时段中位数
  d_prev_close_auc    T-1 日 15:00 根成交额 / T-1 全日成交额(收盘竞价+尾 3 分钟)
  d_prev_last5_ret    T-1 日 close / close(14:55) - 1
E 隔夜-日内结构(日线, 截至 T 日开盘):
  e_on_mean_120/250   隔夜收益滚动均值(含 T 日跳空)
  e_id_mean_60        T-1 及以前 60 日日内收益均值
  e_on_id_corr_60     T-1 及以前 60 日隔夜与日内收益的相关
  e_on_pos_share_60   近 60 日隔夜为正的比例
  e_tug_60            截面秩(on_mean_60) - 截面秩(id_mean_60)  (拔河强度, 在评估时计算)
C 板块扩散(limit_list_d 全市场, 申万一级, 仅用 first_time<=14:45 与 T-1 信息):
  c_ind_touch_1445    同行业(全市场)截至 14:45 曾触及涨停的家数
  c_ind_touch_share   上述家数 / 行业股票数
  c_ind_up_prev       同行业 T-1 收盘涨停家数
  c_ind_height_prev   同行业 T-1 最高连板数
  c_mkt_touch_1445    全市场截至 14:45 触板家数(市场级, 进 A 族对照)
  c_lag_vs_leaders    个股 14:45 涨幅 - 同行业触板股 14:45 前的平均(=10%近似), 即补涨空间; 仅对行业有触板时定义
  c_self_up_prev      个股 T-1 是否涨停收盘; c_self_height_prev 个股 T-1 连板数
"""
from __future__ import annotations
import glob, os, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow.parquet as pq
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import MIN_DIR, DAILY_DIR, PANEL, OUT  # noqa: E402

EV = Path(os.environ.get("OVN_EVENTS") or OUT / "events")

def d_feats_one(code):
    m = pq.read_table(MIN_DIR / f"minute_5min_{code.replace('.', '_')}.parquet").to_pandas()
    m["d"] = m.trade_time.str[:10].str.replace("-", ""); m["t"] = m.trade_time.str[11:16]; m = m.drop_duplicates(["d", "t"])
    P = {c: m.pivot(index="d", columns="t", values=c) for c in ["close", "open", "amount", "vol"]}
    cols = P["close"].columns
    need = ["14:15", "14:45", "13:05", "14:55", "15:00"]
    if any(t not in cols for t in need): return None
    pre = [t for t in cols if t <= "14:45"]; l30 = [t for t in cols if "14:20" <= t <= "14:45"]; pm = [t for t in cols if "13:05" <= t <= "14:45"]
    o = pd.DataFrame(index=P["close"].index)
    cum = P["amount"][pre].sum(axis=1, min_count=1); a30 = P["amount"][l30].sum(axis=1, min_count=1); v30 = P["vol"][l30].sum(axis=1, min_count=1)
    px = P["close"]["14:45"]
    o["d_amt_share_last30"] = a30 / cum
    o["d_ret_last30"] = px / P["close"]["14:15"] - 1
    vw = a30 / v30.where(v30 > 0); o["d_vwap30_dev"] = (px / vw - 1).where((vw > px * 0.9) & (vw < px * 1.1))
    r = P["close"][pm] / P["open"][pm] - 1; a = P["amount"][pm]
    rc = r.sub(r.mean(axis=1), axis=0); ac = a.sub(a.mean(axis=1), axis=0)
    o["d_pv_corr_pm"] = (rc * ac).sum(axis=1) / np.sqrt((rc ** 2).sum(axis=1) * (ac ** 2).sum(axis=1)).replace(0, np.nan)
    o["d_up_amt_share_pm"] = a.where(r > 0, 0).sum(axis=1) / a.sum(axis=1).replace(0, np.nan)
    o["d_rv_last30"] = a30 / a30.shift(1).rolling(20, min_periods=15).median()
    day_amt = P["amount"].sum(axis=1, min_count=1)
    o["d_prev_close_auc"] = (P["amount"]["15:00"] / day_amt).shift(1)
    o["d_prev_last5_ret"] = (P["close"]["15:00"] / P["close"]["14:55"] - 1).shift(1)
    o["ts_code"] = code
    return o.reset_index(names="trade_date")

def e_feats_one(code):
    d = pq.read_table(DAILY_DIR / f"{code}.parquet", columns=["trade_date", "open", "close", "pre_close"]).to_pandas().sort_values("trade_date").drop_duplicates("trade_date").set_index("trade_date")
    on = d.open / d.pre_close - 1; idr = (d.close / d.open - 1); idl = idr.shift(1)
    o = pd.DataFrame(index=d.index)
    o["e_on_mean_120"] = on.rolling(120, min_periods=80).mean(); o["e_on_mean_250"] = on.rolling(250, min_periods=160).mean()
    o["e_id_mean_60"] = idl.rolling(60, min_periods=40).mean()
    o["e_on_id_corr_60"] = on.shift(1).rolling(60, min_periods=40).corr(idl)
    o["e_on_pos_share_60"] = (on > 0).astype(float).rolling(60, min_periods=40).mean()
    o["ts_code"] = code
    return o.reset_index()

def c_feats(keys):
    ll = pd.read_parquet(EV / "limit_list_d.parquet"); ll["trade_date"] = ll.trade_date.astype(str); ll["ts_code"] = ll.ts_code.astype(str)
    pan = pq.read_table(PANEL, columns=["ts_code", "trade_date", "sw2021_l1"]).to_pandas()
    pan = pan[pan.trade_date >= "20211201"]
    ind = pan.dropna(subset=["sw2021_l1"])
    ll = ll.merge(ind, on=["ts_code", "trade_date"], how="left")
    ft = pd.to_numeric(ll.first_time, errors="coerce")
    up = ll[ll.limit.isin(["U", "Z"])].copy(); up["touch1445"] = ft.loc[up.index] <= 144500
    size = ind.groupby(["trade_date", "sw2021_l1"]).size().rename("ind_n")
    t = up[up.touch1445].groupby(["trade_date", "sw2021_l1"]).size().rename("c_ind_touch_1445")
    U = ll[ll.limit == "U"]
    upp = U.groupby(["trade_date", "sw2021_l1"]).size().rename("up_cnt"); hgt = U.groupby(["trade_date", "sw2021_l1"]).limit_times.max().rename("height")
    cal = sorted(pan.trade_date.unique()); nxt = dict(zip(cal[:-1], cal[1:]))
    prev = pd.concat([upp, hgt], axis=1).reset_index(); prev["trade_date"] = prev.trade_date.map(nxt); prev = prev.dropna(subset=["trade_date"]).set_index(["trade_date", "sw2021_l1"])
    mk = up[up.touch1445].groupby("trade_date").size().rename("c_mkt_touch_1445")
    selfp = U[["ts_code", "trade_date", "limit_times"]].copy(); selfp["trade_date"] = selfp.trade_date.map(nxt)
    selfp = selfp.dropna().rename(columns={"limit_times": "c_self_height_prev"}); selfp["c_self_up_prev"] = 1.0
    k = keys.merge(ind, on=["ts_code", "trade_date"], how="left")
    k = k.join(size, on=["trade_date", "sw2021_l1"]).join(t, on=["trade_date", "sw2021_l1"]).join(prev, on=["trade_date", "sw2021_l1"]).join(mk, on="trade_date")
    k["c_ind_touch_1445"] = k.c_ind_touch_1445.fillna(0); k["c_ind_touch_share"] = k.c_ind_touch_1445 / k.ind_n
    k["c_ind_up_prev"] = k.up_cnt.fillna(0); k["c_ind_height_prev"] = k.height.fillna(0); k["c_mkt_touch_1445"] = k.c_mkt_touch_1445.fillna(0)
    k = k.merge(selfp[["ts_code", "trade_date", "c_self_up_prev", "c_self_height_prev"]].drop_duplicates(["ts_code", "trade_date"]), on=["ts_code", "trade_date"], how="left")
    k["c_self_up_prev"] = k.c_self_up_prev.fillna(0); k["c_self_height_prev"] = k.c_self_height_prev.fillna(0)
    return k[["ts_code", "trade_date", "c_ind_touch_1445", "c_ind_touch_share", "c_ind_up_prev", "c_ind_height_prev", "c_mkt_touch_1445", "c_self_up_prev", "c_self_height_prev"]]

def main():
    w = pd.read_parquet(OUT / "frame_wide.parquet"); codes = sorted(w.ts_code.unique()); keys = w[["ts_code", "trade_date"]]
    D = pd.concat([x for x in (d_feats_one(c) for c in codes) if x is not None], ignore_index=True)
    E = pd.concat([e_feats_one(c) for c in codes], ignore_index=True)
    f = keys.merge(D, on=["ts_code", "trade_date"], how="left").merge(E, on=["ts_code", "trade_date"], how="left")
    if (EV / "limit_list_d.parquet").exists():
        f = f.merge(c_feats(keys), on=["ts_code", "trade_date"], how="left")
        f = f.merge(w[["ts_code", "trade_date", "ret_cut", "lim_pct"]], on=["ts_code", "trade_date"])
        f["c_lag_vs_leaders"] = np.where(f.c_ind_touch_1445 > 0, f.lim_pct - f.ret_cut, np.nan); f = f.drop(columns=["ret_cut", "lim_pct"])   # 距本板涨停的空间
    f.to_parquet(OUT / "frame_cde.parquet"); print(f.shape); print(f.describe().T[["count", "mean", "50%"]].to_string())

if __name__ == "__main__":
    main()
