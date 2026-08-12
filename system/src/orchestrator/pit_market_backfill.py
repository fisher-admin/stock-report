#!/usr/bin/env python3
"""Build immutable exact-date market/universe snapshots for honest research."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path(__file__).resolve().parents[3]))
).resolve()

import sys

STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"
if str(STOCK_ANALYZER) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pit_market_snapshot as pms  # noqa: E402
from trading_calendar_store import load_open_trade_dates  # noqa: E402


class BackfillError(RuntimeError):
    pass


def _date8(value: Any, *, field: str) -> str:
    text = str(value or "").replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        raise BackfillError(f"{field} must be YYYYMMDD")
    return text


def select_trade_dates(
    open_trade_dates: Iterable[str],
    *,
    start_date: str,
    end_date: str,
) -> list[str]:
    start = _date8(start_date, field="start_date")
    end = _date8(end_date, field="end_date")
    if start > end:
        raise BackfillError("start_date must not be after end_date")
    return sorted(
        {
            _date8(value, field="open_trade_date")
            for value in open_trade_dates
            if start <= str(value).replace("-", "")[:8] <= end
        }
    )


def _validate_existing_snapshot(output_dir: Path, trade_date: str) -> None:
    universe_path = output_dir / f"universe_{trade_date}.parquet"
    daily_path = output_dir / f"daily_basic_{trade_date}.parquet"
    metadata_path = output_dir / f"pit_market_snapshot_{trade_date}.json"
    try:
        universe = pd.read_parquet(universe_path)
        daily = pd.read_parquet(daily_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BackfillError(f"invalid existing PIT snapshot for {trade_date}") from exc
    universe_required = {
        "trade_date",
        "ts_code",
        "list_status_at_date",
        "is_st",
        "is_suspended",
        "universe_flag",
        "tradable",
        "used_proxy",
        "completeness",
    }
    daily_required = {
        "trade_date",
        "ts_code",
        "pe_ttm",
        "pb",
        "total_mv",
        "circ_mv",
        "used_proxy",
        "completeness",
    }
    if universe_required - set(universe.columns) or daily_required - set(daily.columns):
        raise BackfillError(f"existing PIT snapshot is missing required fields for {trade_date}")
    for name, frame in (("universe", universe), ("daily_basic", daily)):
        payload_dates = set(
            frame["trade_date"].astype(str).str.replace("-", "", regex=False)
        )
        if payload_dates != {trade_date}:
            raise BackfillError(f"{name} payload date mismatch for {trade_date}")
        if frame["ts_code"].astype(str).duplicated().any():
            raise BackfillError(f"{name} contains duplicate securities for {trade_date}")
        if frame["used_proxy"].fillna(True).astype(bool).any():
            raise BackfillError(f"{name} contains proxy rows for {trade_date}")
        if not (frame["completeness"].astype(str) == "complete").all():
            raise BackfillError(f"{name} is incomplete for {trade_date}")
    if str(metadata.get("trade_date") or "") != trade_date:
        raise BackfillError(f"metadata date mismatch for {trade_date}")
    if bool(metadata.get("used_proxy")) or str(metadata.get("completeness")) != "complete":
        raise BackfillError(f"metadata is incomplete or proxy-derived for {trade_date}")


def backfill_pit_market_snapshots(
    *,
    client: Any,
    trade_dates: Iterable[str],
    output_dir: Path,
) -> dict[str, Any]:
    dates = sorted({_date8(value, field="trade_date") for value in trade_dates})
    if not dates:
        raise BackfillError("no open trade dates selected for PIT backfill")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    verified = 0
    for trade_date in dates:
        expected_paths = [
            output_dir / f"universe_{trade_date}.parquet",
            output_dir / f"daily_basic_{trade_date}.parquet",
            output_dir / f"pit_market_snapshot_{trade_date}.json",
        ]
        if all(path.exists() for path in expected_paths):
            _validate_existing_snapshot(output_dir, trade_date)
            verified += 1
            continue
        snapshot = pms.collect_pit_market_snapshot(client, trade_date)
        pms.write_pit_market_snapshot(snapshot, output_dir, immutable=True)
        _validate_existing_snapshot(output_dir, trade_date)
        created += 1
    return {
        "status": "ok",
        "first_trade_date": dates[0],
        "last_trade_date": dates[-1],
        "requested_dates": len(dates),
        "created_dates": created,
        "verified_existing_dates": verified,
        "output_dir": str(output_dir),
        "used_proxy": False,
        "completeness": "complete",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Immutable point-in-time market snapshot backfill")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--workspace", default=str(WORKSPACE))
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    calendar_path = workspace / "stock_data/03-working/health/trading_calendar.json"
    dates = select_trade_dates(
        load_open_trade_dates(calendar_path),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    from pipeline import init_tushare

    result = backfill_pit_market_snapshots(
        client=init_tushare(),
        trade_dates=dates,
        output_dir=workspace / "stock_data/03-working/fundamental_cache/pit_market",
    )
    manifest_path = (
        workspace
        / "stock_data/03-working/fundamental_cache/pit_market"
        / f"backfill_{result['first_trade_date']}_{result['last_trade_date']}.json"
    )
    pms._write_json_atomic(result, manifest_path)
    result["manifest_path"] = str(manifest_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
