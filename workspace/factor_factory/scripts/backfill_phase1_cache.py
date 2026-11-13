#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backfill historical backtest_cache data required by factor_factory Phase 1.

Targets:
- stock_data/03-working/backtest_cache/daily_YYYYMMDD.parquet
- stock_data/03-working/backtest_cache/stk_factor_YYYYMMDD.parquet
- stock_data/03-working/backtest_cache/cyq_perf_YYYYMMDD.parquet

Behavior:
- pulls SSE open trading days from Tushare trade_cal
- skips existing cache files by default
- writes rolling progress JSON so long runs can be monitored/resumed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path("/Users/fisher/.openclaw/workspace")
STOCK_ANALYZER_DIR = WORKSPACE / "skills" / "stock-analyzer"
if str(STOCK_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER_DIR))

import pipeline as pl  # noqa: E402
import backtest  # noqa: E402

DATA_TYPES = ("daily", "stk_factor", "cyq_perf")


@dataclass
class RuntimeConfig:
    factor_root: Path
    cache_dir: Path
    progress_path: Path
    summary_dir: Path
    start_date: str
    end_date: str
    sleep_seconds: float
    stop_after: int | None
    only_types: tuple[str, ...]
    overwrite: bool


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_phase1_defaults(path: Path) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"stock_data_root", "start_date", "end_date"}:
            defaults[key] = value
    return defaults


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill factor_factory phase1 cache history")
    parser.add_argument("--root", default=str(WORKSPACE / "factor_factory"), help="factor_factory root")
    parser.add_argument("--start-date", default=None, help="YYYYMMDD inclusive")
    parser.add_argument("--end-date", default=None, help="YYYYMMDD inclusive")
    parser.add_argument("--sleep", type=float, default=0.0, help="extra sleep seconds after each date")
    parser.add_argument("--stop-after", type=int, default=None, help="stop after N trade dates (for smoke test)")
    parser.add_argument("--types", default=",".join(DATA_TYPES), help="comma-separated subset of daily,stk_factor,cyq_perf")
    parser.add_argument("--overwrite", action="store_true", help="refetch even if cache exists")
    return parser.parse_args()


def load_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    factor_root = Path(args.root).expanduser().resolve()
    defaults = read_phase1_defaults(factor_root / "config" / "phase1.yaml")
    start_date = (args.start_date or defaults.get("start_date") or "20210101").replace("-", "")
    end_date = (args.end_date or defaults.get("end_date") or datetime.now().strftime("%Y%m%d")).replace("-", "")
    types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    invalid = [t for t in types if t not in DATA_TYPES]
    if invalid:
        raise ValueError(f"invalid data types: {invalid}")
    summary_dir = factor_root / "outputs" / "audit_reports"
    summary_dir.mkdir(parents=True, exist_ok=True)
    progress_path = summary_dir / "factor_factory_cache_backfill_progress.json"
    stock_data_root = Path(defaults.get("stock_data_root", str(WORKSPACE / "stock_data")))
    return RuntimeConfig(
        factor_root=factor_root,
        cache_dir=stock_data_root / "03-working" / "backtest_cache",
        progress_path=progress_path,
        summary_dir=summary_dir,
        start_date=start_date,
        end_date=end_date,
        sleep_seconds=float(args.sleep or 0.0),
        stop_after=args.stop_after,
        only_types=types,
        overwrite=bool(args.overwrite),
    )


def list_open_days(pro: Any, start_date: str, end_date: str) -> list[str]:
    cal = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date, fields="cal_date,is_open")
    if cal is None or cal.empty:
        raise RuntimeError("trade_cal returned empty result")
    return sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())


def cache_file(cache_dir: Path, trade_date: str, data_type: str) -> Path:
    return cache_dir / f"{data_type}_{trade_date}.parquet"


def write_progress(cfg: RuntimeConfig, payload: dict[str, Any]) -> None:
    cfg.progress_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    cfg = load_runtime_config(args)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    pro = pl.init_tushare()
    open_days = list_open_days(pro, cfg.start_date, cfg.end_date)
    if cfg.stop_after:
        open_days = open_days[: cfg.stop_after]

    status_by_type: dict[str, dict[str, int]] = {t: defaultdict(int) for t in cfg.only_types}
    failures: list[dict[str, Any]] = []
    started_at = utc_now_iso()
    processed_dates = 0

    print(json.dumps({
        "status": "started",
        "start_date": cfg.start_date,
        "end_date": cfg.end_date,
        "trade_dates": len(open_days),
        "types": list(cfg.only_types),
        "overwrite": cfg.overwrite,
        "cache_dir": str(cfg.cache_dir),
        "progress_path": str(cfg.progress_path),
    }, ensure_ascii=False, indent=2), flush=True)

    for idx, trade_date in enumerate(open_days, start=1):
        processed_dates += 1
        date_result: dict[str, Any] = {"trade_date": trade_date, "results": {}}
        print(f"[backfill] {idx}/{len(open_days)} {trade_date}", flush=True)
        for data_type in cfg.only_types:
            path = cache_file(cfg.cache_dir, trade_date, data_type)
            if path.exists() and not cfg.overwrite:
                status_by_type[data_type]["skipped_existing"] += 1
                size = path.stat().st_size
                date_result["results"][data_type] = {"status": "skipped_existing", "path": str(path), "size": size}
                print(f"  - {data_type}: skip existing ({size} bytes)", flush=True)
                continue

            t0 = time.time()
            df = backtest.get_cached(pro, trade_date, data_type)
            elapsed = round(time.time() - t0, 2)
            if df is None or len(df) == 0:
                status_by_type[data_type]["failed"] += 1
                failure = {"trade_date": trade_date, "data_type": data_type, "elapsed_seconds": elapsed}
                failures.append(failure)
                date_result["results"][data_type] = {"status": "failed", "elapsed_seconds": elapsed}
                print(f"  - {data_type}: FAILED ({elapsed}s)", flush=True)
            else:
                out_path = cache_file(cfg.cache_dir, trade_date, data_type)
                size = out_path.stat().st_size if out_path.exists() else None
                status_by_type[data_type]["fetched"] += 1
                date_result["results"][data_type] = {
                    "status": "fetched",
                    "rows": int(len(df)),
                    "elapsed_seconds": elapsed,
                    "path": str(out_path),
                    "size": size,
                }
                print(f"  - {data_type}: fetched rows={len(df)} elapsed={elapsed}s", flush=True)

        progress = {
            "status": "running",
            "started_at": started_at,
            "updated_at": utc_now_iso(),
            "start_date": cfg.start_date,
            "end_date": cfg.end_date,
            "processed_dates": processed_dates,
            "total_dates": len(open_days),
            "current_trade_date": trade_date,
            "types": list(cfg.only_types),
            "status_by_type": {k: dict(v) for k, v in status_by_type.items()},
            "recent_date_result": date_result,
            "failures_tail": failures[-20:],
        }
        write_progress(cfg, progress)
        if cfg.sleep_seconds > 0:
            time.sleep(cfg.sleep_seconds)

    finished_at = utc_now_iso()
    summary = {
        "status": "ok" if not failures else "partial",
        "started_at": started_at,
        "finished_at": finished_at,
        "start_date": cfg.start_date,
        "end_date": cfg.end_date,
        "processed_dates": processed_dates,
        "total_dates": len(open_days),
        "types": list(cfg.only_types),
        "status_by_type": {k: dict(v) for k, v in status_by_type.items()},
        "failure_count": len(failures),
        "failures": failures,
        "progress_path": str(cfg.progress_path),
    }
    summary_path = cfg.summary_dir / f"factor_factory_cache_backfill_summary_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    summary["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_progress(cfg, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
