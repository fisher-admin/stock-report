#!/usr/bin/env python3
"""Phase B 数据接入:股东户数(集中度)历史拉取+缓存(2026-06-29)。

时点干净:stock_zh_a_gdhs 带【公告日期】→按公告日对齐,无前视。
信号方向先验:户数【减少】=筹码集中=看多(documented A股因子)→ 因子= -户数增减比例。

输出:stock_data/03-working/fundamental_cache/holder_count.parquet
列:ts_code, ann_date, end_date, holder_chg_pct(户数增减比例%)
"""
from __future__ import annotations

import argparse
import re
import signal
import time
from pathlib import Path

import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
OUT = WORKSPACE / "stock_data" / "03-working" / "fundamental_cache" / "holder_count.parquet"


class _TO(Exception):
    pass


def _alarm(s, f):
    raise _TO()


def periods(start_year: int) -> list[str]:
    out = []
    for y in range(start_year, 2027):
        for mmdd in ("0331", "0630", "0930", "1231"):
            out.append(f"{y}{mmdd}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2021)
    args = ap.parse_args()
    import akshare as ak
    signal.signal(signal.SIGALRM, _alarm)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for per in periods(args.start):
        signal.alarm(45)
        try:
            df = ak.stock_zh_a_gdhs(symbol=per)
            signal.alarm(0)
        except _TO:
            print(f"  {per}: 超时跳过"); continue
        except Exception as e:
            signal.alarm(0)
            print(f"  {per}: {type(e).__name__} {str(e)[:50]}"); continue
        if df is None or len(df) == 0:
            print(f"  {per}: 空"); continue
        d = df.copy()
        d["ts_code"] = d["代码"].map(lambda x: (re.search(r"(\d{6})", str(x)) or [None, None])[1] if re.search(r"(\d{6})", str(x)) else None)
        d["ann_date"] = pd.to_datetime(d.get("公告日期"), errors="coerce").dt.strftime("%Y%m%d")
        d["holder_chg_pct"] = pd.to_numeric(d.get("股东户数-增减比例"), errors="coerce")
        d["end_date"] = per
        keep = d.dropna(subset=["ts_code", "ann_date", "holder_chg_pct"])[["ts_code", "ann_date", "end_date", "holder_chg_pct"]]
        frames.append(keep)
        print(f"  {per}: {len(keep)} 条")
        time.sleep(0.3)
    if not frames:
        print("无数据"); return 1
    allp = pd.concat(frames, ignore_index=True)
    allp = allp.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(["ts_code", "end_date"], keep="first")
    allp.to_parquet(OUT, index=False)
    print(f"\n✅ 写出 {OUT}  共 {len(allp)} 条  {allp['ann_date'].min()}..{allp['ann_date'].max()}  唯一股 {allp['ts_code'].nunique()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
