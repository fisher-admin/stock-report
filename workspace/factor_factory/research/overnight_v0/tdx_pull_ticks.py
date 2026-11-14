#!/usr/bin/env python3
"""从本机 tdx-api 拉指定 (股票, 日期) 的分笔成交并就地压缩成"每分钟聚合"(只读接口, 仅依赖标准库)。

接口 /api/trade?code=XXXXXX&date=YYYYMMDD: 通达信 3 秒分笔, Time 精确到分钟(分钟起点, 09:30:00 = 09:30:00..09:30:59 内的成交),
  Price 为价格 x1000, Volume 为手, Status 0 = 主动买, 1 = 主动卖, 2 = 中性(09:25 开盘竞价、15:00 收盘竞价等), 5 = 盘后定价; 2021 年起可取。
每行输出一个股票日: ts_code, date, 然后是 JSON:
  auc = [09:25 竞价价, 量(手)],  m = {"HH:MM": [末价, 最高, 最低, 主买额, 主卖额, 中性额, 笔数, 大单主买额, 大单主卖额, 超大单主买额, 超大单主卖额]}
  金额单位: 元; 大单 = 单笔 >= 20 万元, 超大单 = 单笔 >= 100 万元; 只保留 09:30..14:59 的连续竞价分钟与 15:00 收盘竞价(记在 "15:00")。
用法: python3 tdx_pull_ticks.py --list FILE --out DIR [--workers 8]
"""
from __future__ import annotations
import argparse, gzip, json, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("TDX_API", "http://127.0.0.1:18080")


def agg(L):
    auc, m = None, {}
    for x in L:
        t = x["Time"][11:16]; p = x["Price"] / 1000; v = x["Volume"]; a = p * v * 100; s = x["Status"]
        if t == "09:25":
            auc = [p, v]; continue
        if not ("09:30" <= t <= "15:00") or s == 5:
            continue
        r = m.get(t)
        if r is None:
            r = m[t] = [p, p, p, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0]
        r[0] = p; r[1] = max(r[1], p); r[2] = min(r[2], p); r[6] += 1
        k = 3 if s == 0 else 4 if s == 1 else 5
        r[k] += a
        if s in (0, 1) and a >= 2e5:
            r[7 + s] += a
            if a >= 1e6:
                r[9 + s] += a
    return {"auc": auc, "m": {k: [round(x, 2) if isinstance(x, float) else x for x in v] for k, v in m.items()}}


def one(k):
    code, d = k
    for i in range(3):
        try:
            with urllib.request.urlopen(f"{BASE}/api/trade?code={code[:6]}&date={d}", timeout=60) as r:
                L = (json.load(r).get("data") or {}).get("List") or []
            return code, d, agg(L) if L else None
        except Exception:  # noqa: BLE001
            time.sleep(2 + 2 * i)
    return code, d, None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--list", required=True); ap.add_argument("--out", required=True); ap.add_argument("--workers", type=int, default=8); a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); f = os.path.join(a.out, "ticks_min.jsonl.gz"); done = set()
    if os.path.exists(f):
        with gzip.open(f, "rt") as g:
            for x in g:
                try: done.add(tuple(json.loads(x)[:2]))
                except Exception: pass   # noqa: E701
    keys = [tuple(x.strip().split(",")) for x in open(a.list) if x.strip()]; keys = [k for k in keys if k not in done]
    print(f"todo={len(keys)} done={len(done)} start {time.strftime('%F %T')}", flush=True); n = bad = 0
    with gzip.open(f, "at") as g, ThreadPoolExecutor(a.workers) as ex:
        for code, d, r in ex.map(one, keys):
            n += 1
            if not r or len(r["m"]) < 200: bad += 1
            if r: g.write(json.dumps([code, d, r], separators=(",", ":")) + "\n")
            if n % 2000 == 0: print(n, "bad", bad, time.strftime("%T"), flush=True)
    print(f"done n={n} bad={bad} {time.strftime('%F %T')}", flush=True)


if __name__ == "__main__":
    main()
