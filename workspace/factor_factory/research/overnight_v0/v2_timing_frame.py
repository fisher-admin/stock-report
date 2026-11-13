#!/usr/bin/env python3
"""v2 进出场时点网格的原料: 每个股票日的 买入腿(买入价->当日收盘) 与 卖出腿(次日昨收->次日各时点卖出价)。

任意 (买入时点 E, 卖出时点 X) 的收益 = (1 + a_E) * (1 + b_X) - 1。次日价格用次日昨收做除权衔接。
买入: e1450 / e1455 / e1500 = 对应 5 分钟 K 线的成交均价; eclose = 收盘价(收盘集合竞价)。
卖出: xopen = 开盘价(开盘集合竞价); x0935 = 09:35 线均价; x0940 = 09:35+09:40 均价(v1 口径); x1000 = 09:45..10:00 均价;
      x1030 / x1130 = 该时点收盘价; xclose = 次日收盘价。
不可卖: 次日开盘一字跌停(开盘价 = 跌停价 且 09:35 线最高 = 最低)记 xlock, 评估时单列。
输出 OUT/v2_timing.parquet。只读分钟线与日线, 不依赖任何模型。
"""
from __future__ import annotations
import glob, os, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow.parquet as pq
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import MIN_DIR, DAILY_DIR, OUT  # noqa: E402

def one(code):
    m = pq.read_table(MIN_DIR / f"minute_5min_{code.replace('.', '_')}.parquet").to_pandas()
    m["d"] = m.trade_time.str[:10].str.replace("-", ""); m["t"] = m.trade_time.str[11:16]; m = m.drop_duplicates(["d", "t"])
    P = {c: m.pivot(index="d", columns="t", values=c) for c in ["close", "high", "low", "vol", "amount"]}
    need = ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00", "10:30", "11:30", "14:50", "14:55", "15:00"]
    if any(t not in P["close"].columns for t in need): return None
    def vw(ts):
        a = P["amount"][ts].sum(axis=1, min_count=1); v = P["vol"][ts].sum(axis=1, min_count=1); w = a / v.where(v > 0)
        lo = P["low"][ts].min(axis=1); hi = P["high"][ts].max(axis=1); typ = (P["high"][ts].mean(axis=1) + P["low"][ts].mean(axis=1) + P["close"][ts].mean(axis=1)) / 3
        return w.where((w >= lo * 0.999) & (w <= hi * 1.001), typ)
    o = pd.DataFrame(index=P["close"].index)
    o["e1450"] = vw(["14:50"]); o["e1455"] = vw(["14:55"]); o["e1500"] = vw(["15:00"])
    o["x0935"] = vw(["09:35"]); o["x0940"] = vw(["09:35", "09:40"]); o["x1000"] = vw(["09:45", "09:50", "09:55", "10:00"])
    o["x1030"] = P["close"]["10:30"]; o["x1130"] = P["close"]["11:30"]; o["flat0935"] = P["high"]["09:35"] == P["low"]["09:35"]
    d = pq.read_table(DAILY_DIR / f"{code}.parquet", columns=["trade_date", "open", "close", "pre_close"]).to_pandas().sort_values("trade_date").drop_duplicates("trade_date").set_index("trade_date")
    o = o.join(d, how="inner"); nd = pd.Series(o.index, index=o.index).shift(-1)
    r = pd.DataFrame({"ts_code": code, "trade_date": o.index, "n_date": nd.values}, index=o.index)
    for e in ("e1450", "e1455", "e1500"): r["a_" + e] = o.close / o[e] - 1
    r["a_eclose"] = 0.0
    npc = o.pre_close.shift(-1)
    for x in ("x0935", "x0940", "x1000", "x1030", "x1130"): r["b_" + x] = o[x].shift(-1) / npc - 1
    r["b_xopen"] = o.open.shift(-1) / npc - 1; r["b_xclose"] = o.close.shift(-1) / npc - 1
    r["n_open_ret"] = r.b_xopen; r["n_flat0935"] = o.flat0935.shift(-1)
    return r.reset_index(drop=True)

def main():
    codes = sorted(os.path.basename(f)[12:-8].replace("_", ".") for f in glob.glob(str(MIN_DIR / "minute_5min_*.parquet")))
    codes = [c for c in codes if c[:2] in ("60", "00", "30") and (DAILY_DIR / f"{c}.parquet").exists()]
    df = pd.concat([x for x in (one(c) for c in codes) if x is not None], ignore_index=True)
    df.to_parquet(OUT / "v2_timing.parquet"); print("v2_timing", df.shape, df.trade_date.min(), df.trade_date.max(), df.ts_code.nunique())

if __name__ == "__main__":
    main()
