#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair missing cache files listed in factor_factory backfill progress failures."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path('/Users/fisher/.openclaw/workspace')
STOCK_ANALYZER_DIR = WORKSPACE / 'skills' / 'stock-analyzer'
if str(STOCK_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER_DIR))

import pipeline as pl  # noqa: E402
import backtest  # noqa: E402

PROGRESS_PATH = WORKSPACE / 'factor_factory' / 'outputs' / 'audit_reports' / 'factor_factory_cache_backfill_progress.json'
CACHE_DIR = WORKSPACE / 'stock_data' / '03-working' / 'backtest_cache'


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_progress() -> dict:
    if not PROGRESS_PATH.exists():
        return {}
    return json.loads(PROGRESS_PATH.read_text())


def missing_failures(progress: dict) -> list[dict]:
    failures = progress.get('failures_tail') or progress.get('failures') or []
    out = []
    seen = set()
    for item in failures:
        trade_date = str(item.get('trade_date', ''))
        data_type = str(item.get('data_type', ''))
        if not trade_date or not data_type:
            continue
        key = (trade_date, data_type)
        if key in seen:
            continue
        seen.add(key)
        cache_file = CACHE_DIR / f'{data_type}_{trade_date}.parquet'
        if not cache_file.exists():
            out.append({'trade_date': trade_date, 'data_type': data_type, 'cache_file': str(cache_file)})
    return out


def repair_once() -> dict:
    progress = read_progress()
    status = progress.get('status')
    targets = missing_failures(progress)
    results = []
    if not targets:
        return {
            'status': 'idle',
            'progress_status': status,
            'checked_at': utc_now_iso(),
            'repaired': 0,
            'results': results,
        }

    pro = pl.init_tushare()
    for item in targets:
        trade_date = item['trade_date']
        data_type = item['data_type']
        t0 = time.time()
        df = backtest.get_cached(pro, trade_date, data_type)
        cache_file = Path(item['cache_file'])
        ok = df is not None and len(df) > 0 and cache_file.exists()
        results.append({
            'trade_date': trade_date,
            'data_type': data_type,
            'ok': ok,
            'rows': 0 if df is None else int(len(df)),
            'elapsed_seconds': round(time.time() - t0, 2),
            'exists': cache_file.exists(),
            'size': cache_file.stat().st_size if cache_file.exists() else None,
        })

    return {
        'status': 'repaired' if any(r['ok'] for r in results) else 'checked',
        'progress_status': status,
        'checked_at': utc_now_iso(),
        'repaired': sum(1 for r in results if r['ok']),
        'targets': len(targets),
        'results': results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Repair missing backfill failures')
    parser.add_argument('--loop', action='store_true', help='keep repairing until progress status is not running')
    parser.add_argument('--interval', type=int, default=90, help='seconds between loop iterations')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        payload = repair_once()
        should_print = (not args.loop) or payload.get('status') != 'idle'
        if should_print:
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        if not args.loop:
            return 0
        progress = read_progress()
        if progress.get('status') != 'running':
            return 0
        time.sleep(max(1, args.interval))


if __name__ == '__main__':
    raise SystemExit(main())
