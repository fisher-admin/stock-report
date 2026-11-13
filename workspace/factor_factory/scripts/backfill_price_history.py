#!/usr/bin/env python3
"""Backfill full daily price history (ts_code range) for every code in Research Panel v2.

D4 labels need prices on days a security is *outside* the universe (a held name that
drops out, a name heading into delisting); the panel only stores universe rows.
Also backfills stock_st by trade date (PIT ST list, needed for ST price limits).

Writes factor_factory/data/price_hist/{daily/<ts_code>.parquet, stock_st/stock_st_<date>.parquet}.
Resumable; stops before the production window (default 19:15 Asia/Shanghai).
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

OUT = ROOT / "data/price_hist"
DELISTED_DAILY = ROOT / "data/delisted_hist/daily"
START, END = "20201101", "20261009"
TZ = ZoneInfo("Asia/Shanghai")


def past_stop(stop_at: str) -> bool:
    return datetime.now(TZ).strftime("%H:%M") >= stop_at


def panel_codes(panel_path: Path) -> list[str]:
    codes = set(pd.read_parquet(panel_path, columns=["ts_code"])["ts_code"].astype(str))
    codes |= {p.stem for p in DELISTED_DAILY.glob("*.parquet")}
    return sorted(codes)


def log(rec: dict) -> None:
    with (OUT / "manifest.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--panel", default=str(ROOT / "data/research_panel_v2.parquet"))
    ap.add_argument("--stop-at", default="19:15")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--what", choices=["daily", "stock_st", "both"], default="both")
    args = ap.parse_args(argv)
    import pipeline as pl

    client = pl.init_tushare()
    (OUT / "daily").mkdir(parents=True, exist_ok=True)
    (OUT / "stock_st").mkdir(parents=True, exist_ok=True)
    stats = {"fetched": 0, "reused_delisted": 0, "skipped": 0, "failed": 0, "stopped_early": False}
    jobs: list[tuple[str, str]] = []
    if args.what in ("stock_st", "both"):
        jobs += [("stock_st", d) for d in rp.official_sessions(END) if d >= START]
    if args.what in ("daily", "both"):
        jobs += [("daily", c) for c in panel_codes(Path(args.panel))]
    for kind, key in jobs:
        path = OUT / kind / (f"{key}.parquet" if kind == "daily" else f"stock_st_{key}.parquet")
        if path.exists():
            stats["skipped"] += 1
            continue
        if kind == "daily" and (DELISTED_DAILY / f"{key}.parquet").exists():
            stats["reused_delisted"] += 1  # read from delisted_hist at label time
            continue
        if past_stop(args.stop_at):
            stats["stopped_early"] = True
            break
        t0 = time.monotonic()
        rec = {"kind": kind, "key": key, "at": datetime.now(TZ).isoformat()}
        try:
            if kind == "daily":
                df = client.daily(ts_code=key, start_date=START, end_date=END)
            else:
                df = client.stock_st(trade_date=key)
            df = pd.DataFrame() if df is None else df
            df["provenance"] = "rds_price_hist_backfill"
            df.to_parquet(path, index=False)
            rec.update(ok=True, rows=int(len(df)))
            stats["fetched"] += 1
        except Exception as exc:
            rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
            stats["failed"] += 1
        rec["seconds"] = round(time.monotonic() - t0, 2)
        log(rec)
        time.sleep(args.sleep)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
