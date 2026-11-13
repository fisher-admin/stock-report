#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wait for historical cache backfill to finish, close residual gaps, then run factor_factory Phase 1."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path('/Users/fisher/.openclaw/workspace')
FACTOR_ROOT = WORKSPACE / 'factor_factory'
PROGRESS_PATH = FACTOR_ROOT / 'outputs' / 'audit_reports' / 'factor_factory_cache_backfill_progress.json'
CACHE_DIR = WORKSPACE / 'stock_data' / '03-working' / 'backtest_cache'
OPENCLAW_PY = '/Users/fisher/.openclaw/venv/bin/python'
SYSTEM_PY = '/usr/bin/python3'
BACKFILL_SCRIPT = FACTOR_ROOT / 'scripts' / 'backfill_phase1_cache.py'
REPAIR_SCRIPT = FACTOR_ROOT / 'scripts' / 'repair_backfill_failures.py'
PIPELINE_SCRIPT = FACTOR_ROOT / 'run_factor_factory_pipeline.py'
REGIME_SCRIPT = FACTOR_ROOT / 'scripts' / 'run_regime_labeling.py'
WEIGHT_SCRIPT = FACTOR_ROOT / 'scripts' / 'run_weight_search.py'
ATTRIBUTION_SCRIPT = FACTOR_ROOT / 'scripts' / 'render_attribution_report.py'
STOCK_ANALYZER_DIR = WORKSPACE / 'skills' / 'stock-analyzer'
if str(STOCK_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER_DIR))
import pipeline as pl  # noqa: E402


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def list_open_days() -> list[str]:
    pro = pl.init_tushare()
    cal = pro.trade_cal(exchange='SSE', start_date='20210101', end_date='20260401', fields='cal_date,is_open')
    return sorted(cal[cal['is_open'] == 1]['cal_date'].astype(str).tolist())


def missing_counts(open_days: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prefix in ['daily', 'stk_factor', 'cyq_perf']:
        existing = {p.stem.split('_')[-1] for p in CACHE_DIR.glob(f'{prefix}_*.parquet')}
        missing = [d for d in open_days if d not in existing]
        out[prefix] = {
            'existing': len(existing),
            'missing': len(missing),
            'first_missing': missing[:5],
            'last_missing': missing[-5:],
        }
    return out


def total_missing(summary: dict[str, Any]) -> int:
    return sum(int(v.get('missing', 0)) for v in summary.values())


def run_logged(name: str, command: list[str], logs_dir: Path) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    proc = subprocess.run(command, capture_output=True, text=True)
    finished = datetime.now(timezone.utc)
    log_path = logs_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{name}.log"
    payload = {
        'name': name,
        'command': command,
        'returncode': proc.returncode,
        'started_at': started.isoformat(),
        'finished_at': finished.isoformat(),
        'stdout': proc.stdout,
        'stderr': proc.stderr,
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'name': name,
        'returncode': proc.returncode,
        'log_path': str(log_path),
        'stdout_tail': '\n'.join((proc.stdout or '').splitlines()[-30:]),
        'stderr_tail': '\n'.join((proc.stderr or '').splitlines()[-30:]),
    }


def latest_pipeline_summary() -> Path | None:
    files = sorted((FACTOR_ROOT / 'outputs' / 'audit_reports').glob('factor_factory_pipeline_summary_*.json'))
    return files[-1] if files else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Finish factor_factory phase1 after backfill')
    parser.add_argument('--poll-interval', type=int, default=60)
    parser.add_argument('--max-repair-rounds', type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logs_dir = FACTOR_ROOT / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    open_days = list_open_days()
    steps: list[dict[str, Any]] = []

    print(json.dumps({
        'status': 'waiting_backfill',
        'started_at': utc_now_iso(),
        'progress_path': str(PROGRESS_PATH),
        'poll_interval_seconds': args.poll_interval,
    }, ensure_ascii=False, indent=2), flush=True)

    while True:
        progress = read_json(PROGRESS_PATH)
        if progress.get('status') != 'running':
            break
        time.sleep(max(5, args.poll_interval))

    print(json.dumps({
        'status': 'backfill_stopped',
        'progress_status': read_json(PROGRESS_PATH).get('status'),
        'timestamp': utc_now_iso(),
    }, ensure_ascii=False, indent=2), flush=True)

    gap_summary = missing_counts(open_days)
    for round_idx in range(1, args.max_repair_rounds + 1):
        missing = total_missing(gap_summary)
        print(json.dumps({
            'status': 'gap_check',
            'round': round_idx,
            'missing_total': missing,
            'gap_summary': gap_summary,
        }, ensure_ascii=False, indent=2), flush=True)
        if missing == 0:
            break

        steps.append(run_logged(f'catchup-backfill-round{round_idx}', [OPENCLAW_PY, str(BACKFILL_SCRIPT)], logs_dir))
        steps.append(run_logged(f'repair-round{round_idx}', [OPENCLAW_PY, str(REPAIR_SCRIPT)], logs_dir))
        gap_summary = missing_counts(open_days)
    else:
        gap_summary = missing_counts(open_days)

    if total_missing(gap_summary) != 0:
        print(json.dumps({
            'status': 'blocked_missing_cache',
            'gap_summary': gap_summary,
            'steps': steps,
        }, ensure_ascii=False, indent=2), flush=True)
        return 2

    pipeline_step = run_logged('phase1-pipeline', [SYSTEM_PY, str(PIPELINE_SCRIPT), '--python', SYSTEM_PY], logs_dir)
    steps.append(pipeline_step)

    if pipeline_step['returncode'] == 0:
        steps.append(run_logged('phase1-regime-labeling', [SYSTEM_PY, str(REGIME_SCRIPT)], logs_dir))
        steps.append(run_logged('phase1-weight-search', [SYSTEM_PY, str(WEIGHT_SCRIPT)], logs_dir))
        steps.append(run_logged('phase1-attribution-report', [SYSTEM_PY, str(ATTRIBUTION_SCRIPT)], logs_dir))

    latest = latest_pipeline_summary()
    whitelist_path = FACTOR_ROOT / 'outputs' / 'candidate_lists' / 'factor_whitelist_phase1.json'
    coldlist_path = FACTOR_ROOT / 'outputs' / 'candidate_lists' / 'factor_coldlist_phase1.json'
    regime_path = FACTOR_ROOT / 'outputs' / 'regime_maps' / 'regime_label_phase1.json'
    weight_path = FACTOR_ROOT / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json'
    attribution_path = FACTOR_ROOT / 'outputs' / 'attribution_reports' / 'phase1_attribution_report.json'
    whitelist = json.loads(whitelist_path.read_text()) if whitelist_path.exists() else []
    coldlist = json.loads(coldlist_path.read_text()) if coldlist_path.exists() else []
    latest_summary = read_json(latest) if latest else {}
    regime_summary = read_json(regime_path) if regime_path.exists() else {}
    weight_summary = read_json(weight_path) if weight_path.exists() else {}

    final_status = 'done'
    if pipeline_step['returncode'] != 0:
        final_status = 'pipeline_error'
    elif any(step['returncode'] != 0 for step in steps if step['name'] != 'phase1-pipeline'):
        final_status = 'post_phase_error'

    print(json.dumps({
        'status': final_status,
        'finished_at': utc_now_iso(),
        'pipeline_summary_path': str(latest) if latest else None,
        'pipeline_status': latest_summary.get('status'),
        'failed_stage': latest_summary.get('failed_stage'),
        'whitelist_count': len(whitelist),
        'coldlist_count': len(coldlist),
        'latest_regime': regime_summary.get('latest'),
        'weight_selection_mode': weight_summary.get('selection_mode'),
        'attribution_report_path': str(attribution_path) if attribution_path.exists() else None,
        'steps': steps,
    }, ensure_ascii=False, indent=2), flush=True)
    return 0 if final_status == 'done' else 1


if __name__ == '__main__':
    raise SystemExit(main())
