#!/usr/bin/env python
"""Spike-5 数据准备: 从 tushare 代理拉小样本真实数据到 spike 本地 data/ (additive, 不碰生产).

产出(全部落在 spike5_parity/data/ 内):
  - sample_daily.parquet      30 只股票 2024-01-01 ~ 2026-07-10 日线(raw, 未复权)
  - sample_adj_factor.parquet 同窗口复权因子(tushare 累计因子, 语义≡Lean PriceScaleFactor)
  - trade_cal.parquet         交易日历(SSE)

复跑: /Users/fisher/.openclaw/venv/bin/python fetch_sample_data.py
"""
import os
import time

import pandas as pd
import tushare as ts

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
SECRETS = "/Users/fisher/.openclaw/workspace/.secrets"

START, END = "20240101", "20260710"

# 30 只跨板块样本(主板/创业板/科创板), 含高分红银行/白酒(保证窗口内有除权事件)
SAMPLE_CODES = [
    "000001.SZ", "000002.SZ", "000063.SZ", "000333.SZ", "000651.SZ",
    "000858.SZ", "002027.SZ", "002415.SZ", "002594.SZ", "002714.SZ",
    "300015.SZ", "300059.SZ", "300124.SZ", "300750.SZ", "300760.SZ",
    "600000.SH", "600036.SH", "600519.SH", "600887.SH", "600900.SH",
    "601012.SH", "601318.SH", "601398.SH", "601668.SH", "601888.SH",
    "601988.SH", "603288.SH", "688111.SH", "688981.SH", "605117.SH",
]


def get_pro():
    token = open(os.path.join(SECRETS, "tushare_token")).read().strip()
    url = open(os.path.join(SECRETS, "tushare_api_url")).read().strip()
    pro = ts.pro_api(token)
    pro._DataApi__http_url = url  # 必须走代理, 直连官方必失败
    return pro


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    pro = get_pro()

    cal = pro.trade_cal(exchange="SSE", start_date=START, end_date=END)
    cal = cal.sort_values("cal_date").reset_index(drop=True)
    cal.to_parquet(os.path.join(DATA_DIR, "trade_cal.parquet"))
    n_open = int((cal["is_open"] == 1).sum())
    print(f"trade_cal: {len(cal)} rows, {n_open} open days")

    dailies, adjs = [], []
    for code in SAMPLE_CODES:
        d = pro.daily(ts_code=code, start_date=START, end_date=END)
        a = pro.adj_factor(ts_code=code, start_date=START, end_date=END)
        dailies.append(d)
        adjs.append(a)
        print(f"{code}: daily={len(d)} adj={len(a)}")
        time.sleep(0.25)

    daily = pd.concat(dailies, ignore_index=True).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    adj = pd.concat(adjs, ignore_index=True).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    daily.to_parquet(os.path.join(DATA_DIR, "sample_daily.parquet"))
    adj.to_parquet(os.path.join(DATA_DIR, "sample_adj_factor.parquet"))
    print(f"sample_daily: {daily.shape}, dates {daily.trade_date.min()}..{daily.trade_date.max()}")
    print(f"sample_adj_factor: {adj.shape}")

    # 除权事件探测: 复权因子发生变化的 (ts_code, trade_date)
    adj_w = adj.pivot(index="trade_date", columns="ts_code", values="adj_factor").sort_index()
    changed = (adj_w.diff().abs() > 1e-9)
    ev = changed.stack()
    ev = ev[ev]
    print(f"除权事件数(窗口内 adj_factor 变化): {len(ev)}")
    print(ev.groupby(level=1).size().sort_values(ascending=False).head(10))


if __name__ == "__main__":
    main()
