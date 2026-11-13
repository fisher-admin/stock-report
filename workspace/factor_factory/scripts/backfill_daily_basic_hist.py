#!/usr/bin/env python3
"""Backfill historical daily_basic (valuation / market cap / turnover / SW L1) for greenfield research.

2026-10-10 greenfield plan Phase 0 (docs/superpowers/plans/2026-10-10-greenfield-factor-mining-plan.md).

Research store only: factor_factory/data/daily_basic_hist/daily_basic_<YYYYMMDD>.parquet, marked
provenance=rds_backfill.  It never writes the production PIT captures
(stock_data/03-working/fundamental_cache/pit_market/), because values fetched today about past dates are
not point-in-time captures; the overlap with real captures is cross-checked instead (--verify-overlap).

Survivorship: offset pages of 5,000 per date, reconciled against the full L+D+P universe
(listed on that date, not yet delisted); remaining unseen codes are requested one by one (capped).

Resumable (existing files are skipped), atomic writes, 1 req/s pacing comes from the provider client.
Manifest: factor_factory/data/daily_basic_hist/manifest.jsonl (one line per date).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

WS = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", Path.home() / ".openclaw" / "workspace"))
sys.path.insert(0, str(WS / "skills" / "stock-analyzer"))

from credentials import (  # noqa: E402
    get_promax_api_key, get_promax_api_url, get_rds_api_key, get_rds_api_url, get_stock_data_provider,
)
from data_provider import DataProviderError, create_data_client  # noqa: E402

OUT = WS / "factor_factory" / "data" / "daily_basic_hist"
PIT_MARKET = WS / "stock_data" / "03-working" / "fundamental_cache" / "pit_market"
REQUIRED = ["ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f", "pe_ttm", "pb",
            "ps_ttm", "dv_ttm", "total_mv", "circ_mv"]
CHUNK = 200
BULK_LIMIT = 5000  # RDS daily-basic rejects limits above 5,000
MAX_SINGLES = 60   # single-code reconciliation cap per date (1 req/s); excess is logged, not fetched
BSE_OPEN = "20211115"  # Beijing Stock Exchange first trading day


def make_client():
    return create_data_client(provider=get_stock_data_provider(), rds_key=get_rds_api_key(),
                              promax_key=get_promax_api_key(), rds_url=get_rds_api_url(),
                              promax_url=get_promax_api_url())


def open_dates(client, start: str, end: str) -> list[str]:
    cal = client.query("trade_cal", exchange="SSE", start_date=start, end_date=end)
    cal = cal[pd.to_numeric(cal["is_open"], errors="coerce") == 1]
    return sorted(str(d) for d in cal["cal_date"].astype(str).unique())


def full_universe(client) -> pd.DataFrame:
    """Listed + delisted + paused securities with listing/delisting dates (survivorship-safe universe)."""
    frames = []
    for status in ("L", "D", "P"):
        f = client.stock_basic(list_status=status, fields="ts_code,list_date,delist_date")
        if not f.empty:
            frames.append(f)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")
    for col in ("list_date", "delist_date"):
        # fillna before astype: newer pandas keeps NaN as missing under the str dtype
        out[col] = out.get(col, pd.Series("", index=out.index)).fillna("").astype(str).replace({"None": "", "nan": "", "NaT": ""})
    return out[["ts_code", "list_date", "delist_date"]]


def fetch_date(client, d: str, universe: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Offset pages of 5,000 until a short page, then single-code requests for codes still unseen.

    Provider quirks (probed 2026-10-10): multi-code batches with a historical trade_date return an
    all-or-nothing empty response, while offset pages and single-code queries work.  Page ordering is
    not guaranteed stable, so the union is reconciled against the full L+D+P universe active on d.
    A security without a row on d (suspended) is simply absent, which is correct for daily_basic.
    """
    pages, offset = [], 0
    while True:
        try:
            page = client._direct("daily_basic", {"trade_date": d, "limit": BULK_LIMIT, "offset": offset},
                                  providers=["rds"])
        except DataProviderError as exc:
            if "empty response" in str(exc) and offset > 0:
                break
            raise
        pages.append(page)
        if len(page) < BULK_LIMIT or offset >= 4 * BULK_LIMIT:
            break
        offset += BULK_LIMIT
    df = pd.concat(pages, ignore_index=True)
    paged_rows = len(df)
    df = df.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    got = set(df["ts_code"].astype(str))
    active = universe[(universe["list_date"] != "") & (universe["list_date"] <= d)
                      & ((universe["delist_date"] == "") | (universe["delist_date"] > d))]
    unseen = [c for c in active["ts_code"].astype(str) if c not in got]
    # Explain absences before spending 1 req/s singles: suspended that day, or BSE listings dated before
    # the exchange opened (NEEQ-era list_date).  Only unexplained codes are fetched one by one.
    suspended: set[str] = set()
    if unseen:
        try:
            sus = client._direct("suspend_d", {"trade_date": d, "suspend_type": "S"}, providers=["rds"])
            suspended = set(sus["ts_code"].astype(str)) if not sus.empty else set()
        except DataProviderError:
            suspended = set()
    explained = {c for c in unseen if c in suspended or (c.endswith(".BJ") and d < BSE_OPEN)}
    unexplained = [c for c in unseen if c not in explained]
    found = []
    for code in unexplained[:MAX_SINGLES]:
        try:
            f = client._direct("daily_basic", {"trade_date": d, "ts_code": code}, providers=["rds"])
        except DataProviderError as exc:
            if "empty response" not in str(exc):
                raise
            continue
        if not f.empty:
            found.append(f)
    if found:
        df = pd.concat([df, *found], ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    df = df.reset_index(drop=True)
    meta = {"rows": int(len(df)), "pages": len(pages), "paged_rows": int(paged_rows),
            "page_duplicates": int(paged_rows - len(got)), "active_universe": int(len(active)),
            "unseen_total": len(unseen), "unseen_suspended": len(unseen) - len(unexplained) - sum(1 for c in explained if c not in suspended),
            "unseen_bse_pre_open": sum(1 for c in explained if c not in suspended), "unseen_unexplained": len(unexplained),
            "unseen_requested": min(len(unexplained), MAX_SINGLES),
            "unseen_found": int(sum(len(f) for f in found)),
            "provider": "+".join(sorted(set(df["source_provider"].astype(str)))) if "source_provider" in df else ""}
    return df, meta


def validate(df: pd.DataFrame, d: str) -> list[str]:
    errs = [f"missing column {c}" for c in REQUIRED if c not in df.columns]
    if df.empty:
        errs.append("no rows")
    elif not (df["trade_date"].astype(str) == d).all():
        errs.append("trade_date mismatch")
    if not errs and df["total_mv"].isna().mean() > 0.02:
        errs.append(f"total_mv missing {df['total_mv'].isna().mean():.1%}")
    return errs


def write(df: pd.DataFrame, d: str) -> Path:
    path = OUT / f"daily_basic_{d}.parquet"
    df = df.copy()
    df["provenance"] = "rds_backfill"
    df["fetched_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    return path


def verify_overlap(dates: list[str]) -> dict:
    """Compare backfilled files with real PIT captures on overlapping dates."""
    report = {}
    for d in dates:
        a, b = OUT / f"daily_basic_{d}.parquet", PIT_MARKET / f"daily_basic_{d}.parquet"
        if not (a.exists() and b.exists()):
            continue
        x = pd.read_parquet(a).set_index("ts_code")
        y = pd.read_parquet(b).set_index("ts_code")
        common = x.index.intersection(y.index)
        diffs = {}
        for col in ("total_mv", "pe_ttm", "pb", "turnover_rate_f", "dv_ttm"):
            if col in x and col in y:
                xa = pd.to_numeric(x.loc[common, col], errors="coerce")
                ya = pd.to_numeric(y.loc[common, col], errors="coerce")
                both = xa.notna() & ya.notna()
                rel = ((xa[both] - ya[both]).abs() / ya[both].abs().clip(lower=1e-9))
                diffs[col] = {"n": int(both.sum()), "share_rel_diff_gt_1e-4": round(float((rel > 1e-4).mean()), 5)}
        report[d] = {"backfill_rows": len(x), "pit_rows": len(y), "common": len(common), "diffs": diffs}
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--end", default="")
    ap.add_argument("--dates", default="", help="comma-separated explicit dates (smoke test)")
    ap.add_argument("--verify-overlap", action="store_true")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.verify_overlap:
        dates = sorted(p.name[12:20] for p in OUT.glob("daily_basic_*.parquet"))
        print(json.dumps(verify_overlap(dates), ensure_ascii=False, indent=1))
        return 0
    client = make_client()
    end = args.end or datetime.now().strftime("%Y%m%d")
    dates = [d.strip() for d in args.dates.split(",") if d.strip()] or open_dates(client, args.start, end)
    universe = full_universe(client)
    todo = [d for d in dates if not (OUT / f"daily_basic_{d}.parquet").exists()]
    print(f"dates={len(dates)} todo={len(todo)} universe(L+D+P)={len(universe)}", flush=True)
    failed = []
    with open(OUT / "manifest.jsonl", "a", encoding="utf-8") as man:
        for n, d in enumerate(todo, 1):
            t0, last_err = time.time(), ""
            for attempt in range(3):
                try:
                    df, meta = fetch_date(client, d, universe)
                    errs = validate(df, d)
                    if errs:
                        raise DataProviderError("; ".join(errs))
                    write(df, d)
                    man.write(json.dumps({"date": d, **meta, "secs": round(time.time() - t0, 1),
                                          "at": datetime.now().astimezone().isoformat(timespec="seconds")}) + "\n")
                    man.flush()
                    if n % 25 == 0 or n == len(todo):
                        print(f"[{n}/{len(todo)}] {d} rows={meta['rows']} pages={meta['pages']} dup={meta['page_duplicates']} unseen={meta['unseen_total']} sus={meta['unseen_suspended']} unexplained={meta['unseen_unexplained']} found={meta['unseen_found']}", flush=True)
                    break
                except Exception as exc:  # noqa: BLE001 — record, back off, retry
                    last_err = f"{type(exc).__name__}: {exc}"
                    time.sleep(5 * (attempt + 1))
            else:
                failed.append(d)
                man.write(json.dumps({"date": d, "error": last_err[:300]}) + "\n")
                man.flush()
                print(f"FAILED {d}: {last_err[:200]}", flush=True)
    print(json.dumps({"done": len(todo) - len(failed), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
