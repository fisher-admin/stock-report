#!/usr/bin/env python3
"""从本机 tdx-api 拉指定 (股票, 日期) 的 1 分钟分时(只读接口, 仅依赖标准库), 供个股级动态离场研究使用。

接口 /api/minute?code=XXXXXX&date=YYYYMMDD: 240 个点(09:31..15:00), Price 为该分钟末价 x1000, Number 为该分钟成交量(手); 2022 年起可取。
输入: 每行 "ts_code,YYYYMMDD" 的清单; 输出: <out>/minute1.csv.gz, 每行 ts_code,date,p1..p240(价格),v1..v240(手), 可断点续拉。
用法: python3 tdx_pull_minute1.py --list FILE --out DIR [--workers 12]
"""
from __future__ import annotations
import argparse, gzip, json, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("TDX_API", "http://127.0.0.1:18080")

def one(k):
    code, d = k
    for i in range(3):
        try:
            with urllib.request.urlopen(f"{BASE}/api/minute?code={code[:6]}&date={d}", timeout=30) as r:
                L = (json.load(r).get("data") or {}).get("List") or []
            return code, d, L
        except Exception:  # noqa: BLE001
            time.sleep(1 + i)
    return code, d, None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--list", required=True); ap.add_argument("--out", required=True); ap.add_argument("--workers", type=int, default=12); a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); f = os.path.join(a.out, "minute1.csv.gz"); done = set()
    if os.path.exists(f):
        with gzip.open(f, "rt") as g: done = {tuple(x.split(",", 2)[:2]) for x in g}
    keys = [tuple(x.strip().split(",")) for x in open(a.list) if x.strip()]; keys = [k for k in keys if k not in done]
    print(f"todo={len(keys)} done={len(done)} start {time.strftime('%F %T')}", flush=True); n = bad = 0
    with gzip.open(f, "at") as g, ThreadPoolExecutor(a.workers) as ex:
        for code, d, L in ex.map(one, keys):
            n += 1
            if not L or len(L) < 200: bad += 1
            else: g.write(",".join([code, d] + [str(x["Price"]) for x in L[:240]] + [str(x["Number"]) for x in L[:240]]) + "\n")
            if n % 2000 == 0: print(n, "bad", bad, time.strftime("%T"), flush=True)
    print(f"done n={n} bad={bad} {time.strftime('%F %T')}", flush=True)

if __name__ == "__main__":
    main()
