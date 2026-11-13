#!/usr/bin/env python3
"""Backfill per-code history for securities delisted inside the research window.

Survivorship repair for Research Panel v2: the lab long panel was built from a
currently-listed stock list, so no delisted security ever appears in it.

For each delisted code (SH/SZ main + ChiNext, delist_date in window) fetch, once:
  daily, stk_factor, cyq_perf (ts_code range queries) and namechange (full history).
Writes factor_factory/data/delisted_hist/<api>/<ts_code>.parquet plus manifest.jsonl.
Resumable (existing files are skipped), rate-limited, read-only on production paths.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(WORKSPACE / "skills/stock-analyzer"))
sys.path.insert(0, str(ROOT / "scripts"))

import research_panel_v2 as rp  # noqa: E402

OUT = ROOT / "data/delisted_hist"
APIS = ("daily", "stk_factor", "cyq_perf", "namechange")
WINDOW_START = "20210129"
# 60-session liquidity lookback before the first panel day.
FETCH_START = "20200901"


def delisted_codes(window_end: str) -> pd.DataFrame:
    paths = sorted(rp.STOCK_WORKING.glob(rp.STATIC_BUNDLE_GLOB), key=lambda p: p.stat().st_mtime)
    st = pd.read_parquet(paths[-1])
    d = st[(st["current_list_status"] == "D") & st["delist_date"].notna()].copy()
    d["delist_date"] = d["delist_date"].astype(str)
    d = d[(d["delist_date"] > WINDOW_START) & (d["delist_date"] <= window_end)]
    board_ok = ~(d["ts_code"].str.endswith(".BJ") | d["ts_code"].str.startswith(("688", "689")))
    return d[board_ok].sort_values("ts_code").reset_index(drop=True)


def fetch_one(client, api: str, row: pd.Series) -> pd.DataFrame:
    code = row["ts_code"]
    if api == "namechange":
        return client.namechange(ts_code=code)
    start = max(FETCH_START, str(row["list_date"] or FETCH_START))
    return getattr(client, api)(ts_code=code, start_date=start, end_date=row["delist_date"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window-end", default="20261009")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=0, help="only the first N codes (smoke test)")
    args = ap.parse_args(argv)
    import pipeline as pl

    client = pl.init_tushare()
    codes = delisted_codes(args.window_end)
    if args.limit:
        codes = codes.head(args.limit)
    for api in APIS:
        (OUT / api).mkdir(parents=True, exist_ok=True)
    manifest = OUT / "manifest.jsonl"
    done = fetched = failed = 0
    for _, row in codes.iterrows():
        for api in APIS:
            path = OUT / api / f"{row['ts_code']}.parquet"
            if path.exists():
                done += 1
                continue
            started = time.monotonic()
            rec = {"ts_code": row["ts_code"], "api": api, "delist_date": row["delist_date"],
                   "at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
            try:
                df = fetch_one(client, api, row)
                df = pd.DataFrame() if df is None else df
                df["provenance"] = "rds_delisted_backfill"
                df.to_parquet(path, index=False)
                rec.update(ok=True, rows=int(len(df)))
                fetched += 1
            except Exception as exc:  # recorded and retried on the next run
                rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
                failed += 1
            rec["seconds"] = round(time.monotonic() - started, 2)
            with manifest.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            time.sleep(args.sleep)
    print(json.dumps({"codes": int(len(codes)), "fetched": fetched, "skipped_existing": done, "failed": failed}))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
