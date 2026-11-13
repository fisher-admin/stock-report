#!/usr/bin/env python3
"""时间留出集输入准备(不读取任何标签统计, 只做格式转换与拼接)。

输入:
  --tdx DIR      VPS tdx_pull_minute5.py 的输出(<ts_code>.csv.gz)
输出(全部在 outputs/overnight_v0/holdout_time/ 下):
  minute/minute_5min_<code>.parquet   与 stk_mins 缓存同格式(trade_time, close, open, high, low, vol, amount), 自 2026-03-01 起
  panel.parquet                       与 research_panel_v2 所需列同名: name, market_board, avg_amount_lookback(60 日均成交额, 千元, 与面板口径一致),
                                      float_share, circ_mv(daily_basic), sw2021_l1(面板最后已知值前推)
  events/                             训练期缓存 + ext_ 补充文件拼接; forecast/dividend 只保留主板代码(与训练时的覆盖一致)
  research_codes.txt                  研究 800 中的主板+创业板代码(市场级变量只用它们计算, 与训练口径一致)
排除: 留出 800 只股票(minute_cache_holdout)、科创板、北交所。
"""
from __future__ import annotations
import argparse, glob, gzip, os, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow.parquet as pq
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import WS, OUT, PANEL, DAILY_DIR  # noqa: E402

H = OUT / "holdout_time"; EV0 = OUT / "events"
START = "2026-03-01"

def codes_of(d, pat):
    return {os.path.basename(f)[12:-8].replace("_", ".") for f in glob.glob(str(d / pat))}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--tdx", required=True); a = ap.parse_args()
    (H / "minute").mkdir(parents=True, exist_ok=True); (H / "events").mkdir(exist_ok=True)
    hold = codes_of(WS / "stock_data/01-root/minute_cache_holdout", "minute_5min_*.parquet")
    research = codes_of(WS / "stock_data/01-root/minute_cache", "minute_5min_*.parquet")
    ok = lambda c: c[:2] in ("60", "00", "30") and c not in hold  # noqa: E731
    files = sorted(f for f in glob.glob(os.path.join(a.tdx, "*.csv.gz")) if ok(os.path.basename(f)[:9]))
    n = 0
    for f in files:
        code = os.path.basename(f)[:9]; out = H / "minute" / f"minute_5min_{code.replace('.', '_')}.parquet"
        if out.exists() or not (DAILY_DIR / f"{code}.parquet").exists(): continue
        d = pd.read_csv(f, dtype={"trade_time": str})
        if d.empty: continue
        d = d[d.trade_time >= START]
        if d.empty: continue
        d = pd.DataFrame({"ts_code": code, "trade_time": d.trade_time + ":00", "close": d.close, "open": d.open, "high": d.high, "low": d.low,
                          "vol": d.vol_shares, "amount": d.amount_yuan})
        d.to_parquet(out); n += 1
    print("minute files written", n)
    codes = sorted(codes_of(H / "minute", "minute_5min_*.parquet"))
    (H / "research_codes.txt").write_text("\n".join(c for c in codes if c in research))
    # ---- panel
    pan = pq.read_table(PANEL, columns=["ts_code", "trade_date", "name", "sw2021_l1"]).to_pandas()
    last = pan[pan.trade_date >= "20250101"].sort_values("trade_date").groupby("ts_code").last()[["name", "sw2021_l1"]]
    rows = []
    for c in codes:
        d = pq.read_table(DAILY_DIR / f"{c}.parquet", columns=["trade_date", "amount"]).to_pandas().sort_values("trade_date").drop_duplicates("trade_date")
        d["avg_amount_lookback"] = d.amount.rolling(60, min_periods=40).mean(); d["ts_code"] = c
        rows.append(d[d.trade_date >= START.replace("-", "")][["ts_code", "trade_date", "avg_amount_lookback"]])
    P = pd.concat(rows, ignore_index=True)
    db = pd.concat([pd.read_parquet(f, columns=["ts_code", "trade_date", "float_share", "circ_mv"]) for f in sorted(glob.glob(str(WS / "factor_factory/data/daily_basic_hist/daily_basic_2026*.parquet")))])
    P = P.merge(db.astype({"trade_date": str}), on=["ts_code", "trade_date"], how="left")
    st = pd.concat([pd.read_parquet(f, columns=["ts_code", "trade_date", "name"]) for f in sorted(glob.glob(str(WS / "factor_factory/data/price_hist/stock_st/stock_st_2026*.parquet")))]).astype({"trade_date": str})
    P = P.merge(st.rename(columns={"name": "st_name"}), on=["ts_code", "trade_date"], how="left")
    P = P.join(last, on="ts_code")
    P["name"] = np.where(P.st_name.notna(), P.st_name, P.name.fillna("").str.replace("ST", "", regex=False))
    P["market_board"] = np.where(P.ts_code.str[:2] == "30", "创业板", "主板")
    P.drop(columns="st_name").to_parquet(H / "panel.parquet"); print("panel", P.shape, P.trade_date.min(), P.trade_date.max(), P.sw2021_l1.notna().mean().round(3))
    # ---- events
    for f in sorted(EV0.glob("*.parquet")):
        if f.name.startswith("ext_"): continue
        d = pd.read_parquet(f); e = EV0 / f"ext_{f.name}"
        if e.exists(): d = pd.concat([d, pd.read_parquet(e)], ignore_index=True).drop_duplicates()
        if f.stem in ("forecast", "dividend"): d = d[d.ts_code.astype(str).str[:2].isin(["60", "00"])]
        for c in d.columns:
            if str(d[c].dtype) == "string" or d[c].dtype == object: d[c] = d[c].astype("string")
        d.to_parquet(H / "events" / f.name)
    print("events", sorted(p.name for p in (H / "events").glob("*.parquet")))

if __name__ == "__main__":
    main()
