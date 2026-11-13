#!/usr/bin/env python3
"""从 VPS 本机 tdx-api(127.0.0.1:18080) 拉取沪深主板 + 创业板股票(不含科创板/北交所)全部 5 分钟线(只读接口, 仅依赖标准库)。

口径(2026-11-13 实测, 与 stk_mins 缓存对照 000006.SZ 20250610):
  - Time 为右端标签; 当日首根 09:35 已并入开盘集合竞价(= 缓存 09:30 + 09:35 两根之和), 共 48 根
  - Open/High/Low/Close 为价格 x1000; Volume 单位为手(x100 = 股); Amount 为元 x1000
  - 收盘价逐根一致; 盘中个别根的成交量在 5 分钟边界上与 stk_mins 有小幅差异
  - 分钟线不复权
历史深度: 约 2024-10-25 起(通达信服务器侧限制)。
输出: <out>/<ts_code>.csv.gz, 列 trade_time,open,high,low,close,vol_shares,amount_yuan
用法: python3 tdx_pull_minute5.py --out DIR [--workers 2] [--codes 600000.SH,...] [--sleep 0.2]
"""
from __future__ import annotations
import argparse, csv, gzip, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("TDX_API", "http://127.0.0.1:18080")

def get(path, timeout=120, tries=4):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=timeout) as r: return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = e; time.sleep(2 * (i + 1))
    raise RuntimeError(f"{path}: {err}")

def universe_codes():
    d = get("/api/stock-codes")["data"]["list"]
    out = []
    for c in d:
        ex, num = c[:2], c[2:]
        if ex == "sh" and num[:2] == "60": out.append(num + ".SH")
        elif ex == "sz" and num[:3] in ("000", "001", "002", "003", "300", "301", "302"): out.append(num + ".SZ")   # 深市主板 + 创业板
    return sorted(set(out))

def pull(code, out, sleep):
    f = os.path.join(out, code + ".csv.gz")
    if os.path.exists(f) and os.path.getsize(f) > 100: return code, "cached", 0
    t = time.time()
    try:
        r = get(f"/api/kline-all?code={code[:6]}&type=minute5")
        L = (r.get("data") or {}).get("list") or []
    except Exception as e:  # noqa: BLE001
        return code, f"ERR {str(e)[:80]}", 0
    tmp = f + ".tmp"
    with gzip.open(tmp, "wt", newline="") as g:
        w = csv.writer(g); w.writerow(["trade_time", "open", "high", "low", "close", "vol_shares", "amount_yuan"])
        for x in L:
            w.writerow([x["Time"][:16].replace("T", " "), x["Open"] / 1000, x["High"] / 1000, x["Low"] / 1000, x["Close"] / 1000,
                        int(x["Volume"]) * 100, round(x["Amount"] / 1000, 2)])
    os.replace(tmp, f); time.sleep(sleep)
    return code, f"ok {len(L)} {time.time() - t:.1f}s", len(L)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--codes", default=""); ap.add_argument("--sleep", type=float, default=0.2); a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    codes = a.codes.split(",") if a.codes else universe_codes()
    print(f"codes={len(codes)} start {time.strftime('%F %T')}", flush=True)
    n = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for code, st, k in ex.map(lambda c: pull(c, a.out, a.sleep), codes):
            n += 1
            if not st.startswith(("ok", "cached")) or n % 100 == 0: print(n, code, st, flush=True)
    print(f"done {time.strftime('%F %T')}", flush=True)

if __name__ == "__main__":
    main()
