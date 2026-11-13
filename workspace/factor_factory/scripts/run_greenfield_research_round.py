#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
GREENFIELD = ROOT / 'outputs' / 'greenfield_multifactor'
Q_PYTHON = ROOT / 'qlib_lab' / 'venv' / 'bin' / 'python'
TZ = ZoneInfo('Asia/Shanghai')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Launch an official greenfield research round and sync canonical outputs')
    parser.add_argument('--round-name', default=None, help='Override round name / run directory / recorder name')
    parser.add_argument('--progress-output', default=str(EVOLUTION / 'greenfield_research_progress_latest.json'))
    parser.add_argument('--progress-log', default=str(EVOLUTION / 'greenfield_research_progress_latest.log'))
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def iso_mtime(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None


def run_step(cmd: list[str], cwd: Path = ROOT) -> dict[str, Any]:
    started_at = iso_now()
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    return {
        'command': cmd,
        'started_at': started_at,
        'finished_at': iso_now(),
        'returncode': proc.returncode,
        'stdout_tail': (proc.stdout or '').splitlines()[-120:],
        'stderr_tail': (proc.stderr or '').splitlines()[-120:],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def append_progress_log(paths: list[Path], line: str) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')


def build_progress_snapshot(
    round_name: str,
    run_dir: Path,
    launcher_summary: dict[str, Any],
    stage: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = {
        'schema_version': '2026-04-22.greenfield_research_progress.v1',
        'round_name': round_name,
        'generated_at': iso_now(),
        'status': status,
        'stage': stage,
        'official_root': str(ROOT),
        'run_dir': str(run_dir),
        'completed_labels': [
            step.get('label')
            for step in launcher_summary.get('steps', [])
            if step.get('result', {}).get('status') == 'ok'
        ],
        'step_status': {
            step.get('label'): step.get('result', {}).get('status', 'error' if 'error' in step else 'unknown')
            for step in launcher_summary.get('steps', [])
        },
        'sync_step_returncode': (launcher_summary.get('sync_step') or {}).get('returncode'),
        'readiness_refresh_returncode': (launcher_summary.get('readiness_refresh_step') or {}).get('returncode'),
        'qlib_recorder_returncode': (launcher_summary.get('qlib_recorder_step') or {}).get('returncode'),
        'official_targets': {
            'factor_iteration_pool': str(EVOLUTION / 'factor_iteration_pool.json'),
            'factor_lineage': str(EVOLUTION / 'factor_lineage.json'),
            'portfolio_backtest_report': str(EVOLUTION / 'portfolio_backtest_report.json'),
            'overfit_audit_report': str(EVOLUTION / 'overfit_audit_report.json'),
            'factor_promotion_decision': str(EVOLUTION / 'factor_promotion_decision.json'),
            'greenfield_research_round_latest': str(EVOLUTION / 'greenfield_research_round_latest.json'),
            'qlib_recorder_sync_latest': str(EVOLUTION / 'qlib_recorder_sync_latest.json'),
        },
    }
    if extra:
        progress.update(extra)
    return progress


def emit_progress(
    progress_json_paths: list[Path],
    progress_log_paths: list[Path],
    round_name: str,
    run_dir: Path,
    launcher_summary: dict[str, Any],
    stage: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = build_progress_snapshot(round_name, run_dir, launcher_summary, stage, status, extra)
    for path in progress_json_paths:
        write_json(path, payload)
    append_progress_log(progress_log_paths, json.dumps({
        'generated_at': payload['generated_at'],
        'stage': stage,
        'status': status,
        'completed_labels': payload.get('completed_labels', []),
        'sync_step_returncode': payload.get('sync_step_returncode'),
        'readiness_refresh_returncode': payload.get('readiness_refresh_returncode'),
        'qlib_recorder_returncode': payload.get('qlib_recorder_returncode'),
    }, ensure_ascii=False))


def worker_a(run_dir: Path) -> dict[str, Any]:
    out = GREENFIELD / 'market_state_hypotheses.json'
    payload = {
        'worker': 'A_market_hypotheses',
        'generated_at': iso_now(),
        'artifacts_before': {'market_state_hypotheses_mtime': iso_mtime(out)},
    }
    payload['step'] = run_step([sys.executable, str(ROOT / 'scripts' / 'run_greenfield_market_hypotheses.py')])
    payload['artifacts_after'] = {'market_state_hypotheses_mtime': iso_mtime(out)}
    payload['status'] = 'ok' if payload['step']['returncode'] == 0 else 'error'
    write_json(run_dir / 'A_market_hypotheses.json', payload)
    return payload


def worker_b(run_dir: Path) -> dict[str, Any]:
    panel = ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'
    snapshot = GREENFIELD / 'factor_pool_snapshot.json'
    payload = {
        'worker': 'B_factor_pool',
        'generated_at': iso_now(),
        'artifacts_before': {
            'panel_mtime': iso_mtime(panel),
            'factor_pool_snapshot_mtime': iso_mtime(snapshot),
        },
    }
    payload['step'] = run_step([sys.executable, str(ROOT / 'scripts' / 'build_greenfield_multifactor_pool.py')])
    payload['artifacts_after'] = {
        'panel_mtime': iso_mtime(panel),
        'factor_pool_snapshot_mtime': iso_mtime(snapshot),
    }
    payload['status'] = 'ok' if payload['step']['returncode'] == 0 else 'error'
    write_json(run_dir / 'B_factor_pool.json', payload)
    return payload


def worker_c(run_dir: Path, start_ts: float) -> dict[str, Any]:
    hypotheses = GREENFIELD / 'market_state_hypotheses.json'
    snapshot = GREENFIELD / 'factor_pool_snapshot.json'
    payload = {
        'worker': 'C_validation_backtest',
        'generated_at': iso_now(),
        'wait_for': [str(hypotheses), str(snapshot)],
        'start_ts': start_ts,
    }
    wait_log: list[dict[str, Any]] = []
    deadline = time.time() + 900
    while time.time() < deadline:
        ready = (
            hypotheses.exists() and snapshot.exists() and
            hypotheses.stat().st_mtime >= start_ts - 1 and snapshot.stat().st_mtime >= start_ts - 1
        )
        wait_log.append({
            'checked_at': iso_now(),
            'market_hypotheses_mtime': iso_mtime(hypotheses),
            'factor_pool_snapshot_mtime': iso_mtime(snapshot),
            'ready': ready,
        })
        if ready:
            break
        time.sleep(3)
    payload['wait_log_tail'] = wait_log[-20:]
    payload['step'] = run_step([sys.executable, str(ROOT / 'scripts' / 'run_greenfield_multifactor_backtest.py')])
    payload['artifacts_after'] = {
        'walkforward_report_mtime': iso_mtime(GREENFIELD / 'walkforward_report.json'),
        'analog_validation_mtime': iso_mtime(GREENFIELD / 'analog_validation.json'),
        'portfolio_spec_mtime': iso_mtime(GREENFIELD / 'portfolio_spec.json'),
        'validation_summary_mtime': iso_mtime(GREENFIELD / 'validation_summary.json'),
    }
    payload['status'] = 'ok' if payload['step']['returncode'] == 0 else 'error'
    write_json(run_dir / 'C_validation_backtest.json', payload)
    return payload


def main() -> int:
    args = parse_args()
    round_name = args.round_name or datetime.now(TZ).strftime('%Y%m%dT%H%M%S_greenfield_research_round')
    run_dir = EVOLUTION / 'subagent_runs' / round_name
    run_dir.mkdir(parents=True, exist_ok=True)
    progress_json_paths = [Path(args.progress_output), run_dir / 'progress.json']
    progress_log_paths = [Path(args.progress_log), run_dir / 'progress.log']
    start_ts = time.time()
    launcher_summary: dict[str, Any] = {
        'round_name': round_name,
        'generated_at': iso_now(),
        'official_root': str(ROOT),
        'run_dir': str(run_dir),
        'steps': [],
    }
    emit_progress(
        progress_json_paths,
        progress_log_paths,
        round_name,
        run_dir,
        launcher_summary,
        stage='started',
        status='running',
        extra={
            'message': 'progress json/log flushed before worker launch',
            'pending_labels': ['A', 'B', 'C', 'sync', 'readiness_refresh', 'qlib_recorder'],
        },
    )

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {
            executor.submit(worker_a, run_dir): 'A',
            executor.submit(worker_b, run_dir): 'B',
            executor.submit(worker_c, run_dir, start_ts): 'C',
        }
        for future in as_completed(future_map):
            label = future_map[future]
            try:
                launcher_summary['steps'].append({'label': label, 'result': future.result()})
                emit_progress(
                    progress_json_paths,
                    progress_log_paths,
                    round_name,
                    run_dir,
                    launcher_summary,
                    stage=f'worker_{label}_complete',
                    status='running',
                    extra={
                        'last_completed_label': label,
                        'pending_labels': sorted({'A', 'B', 'C'} - {step.get('label') for step in launcher_summary['steps']}),
                    },
                )
            except Exception as exc:  # pragma: no cover - defensive
                launcher_summary['steps'].append({'label': label, 'error': {'type': type(exc).__name__, 'message': str(exc)}})
                emit_progress(
                    progress_json_paths,
                    progress_log_paths,
                    round_name,
                    run_dir,
                    launcher_summary,
                    stage=f'worker_{label}_error',
                    status='error',
                    extra={'error': {'label': label, 'type': type(exc).__name__, 'message': str(exc)}},
                )

    sync_cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'sync_greenfield_mainline_outputs.py'),
        '--round-name',
        round_name,
        '--summary-output',
        str(run_dir / 'sync_greenfield_mainline_outputs.json'),
    ]
    launcher_summary['sync_step'] = run_step(sync_cmd)
    emit_progress(
        progress_json_paths,
        progress_log_paths,
        round_name,
        run_dir,
        launcher_summary,
        stage='sync_complete',
        status='running' if launcher_summary['sync_step']['returncode'] == 0 else 'error',
    )

    readiness_cmd = [sys.executable, str(ROOT / 'scripts' / 'generate_validation_readiness_label_gap_report.py')]
    launcher_summary['readiness_refresh_step'] = run_step(readiness_cmd)
    emit_progress(
        progress_json_paths,
        progress_log_paths,
        round_name,
        run_dir,
        launcher_summary,
        stage='readiness_refresh_complete',
        status='running' if launcher_summary['readiness_refresh_step']['returncode'] == 0 else 'error',
    )

    recorder_python = str(Q_PYTHON if Q_PYTHON.exists() else Path(sys.executable))
    recorder_cmd = [
        recorder_python,
        str(ROOT / 'scripts' / 'qlib_adapter' / 'record_factor_factory_round.py'),
        '--round-name',
        round_name,
        '--summary-output',
        str(run_dir / 'qlib_recorder_sync.json'),
    ]
    launcher_summary['qlib_recorder_step'] = run_step(recorder_cmd)
    qlib_run_summary = run_dir / 'qlib_recorder_sync.json'
    qlib_latest = EVOLUTION / 'qlib_recorder_sync_latest.json'
    if qlib_run_summary.exists():
        qlib_latest.write_text(qlib_run_summary.read_text(encoding='utf-8'), encoding='utf-8')
    emit_progress(
        progress_json_paths,
        progress_log_paths,
        round_name,
        run_dir,
        launcher_summary,
        stage='qlib_recorder_complete',
        status='running' if launcher_summary['qlib_recorder_step']['returncode'] == 0 else 'error',
    )

    launcher_summary['artifacts'] = {
        'market_state_hypotheses': str(GREENFIELD / 'market_state_hypotheses.json'),
        'factor_pool_snapshot': str(GREENFIELD / 'factor_pool_snapshot.json'),
        'walkforward_report': str(GREENFIELD / 'walkforward_report.json'),
        'analog_validation': str(GREENFIELD / 'analog_validation.json'),
        'portfolio_spec': str(GREENFIELD / 'portfolio_spec.json'),
        'factor_iteration_pool': str(EVOLUTION / 'factor_iteration_pool.json'),
        'factor_lineage': str(EVOLUTION / 'factor_lineage.json'),
        'factor_validation_report': str(EVOLUTION / 'factor_validation_report.json'),
        'portfolio_backtest_report': str(EVOLUTION / 'portfolio_backtest_report.json'),
        'overfit_audit_report': str(EVOLUTION / 'overfit_audit_report.json'),
        'factor_promotion_decision': str(EVOLUTION / 'factor_promotion_decision.json'),
        'qlib_recorder_sync': str(run_dir / 'qlib_recorder_sync.json'),
    }
    launcher_summary['completed_at'] = iso_now()

    all_ok = all(
        step.get('result', {}).get('status') == 'ok'
        for step in launcher_summary['steps']
        if 'result' in step
    )
    launcher_summary['status'] = 'ok' if all_ok and all(
        launcher_summary[key]['returncode'] == 0
        for key in ['sync_step', 'readiness_refresh_step', 'qlib_recorder_step']
    ) else 'error'

    write_json(run_dir / 'launcher_summary.json', launcher_summary)
    write_json(EVOLUTION / 'greenfield_research_round_latest.json', launcher_summary)
    emit_progress(
        progress_json_paths,
        progress_log_paths,
        round_name,
        run_dir,
        launcher_summary,
        stage='completed',
        status=launcher_summary['status'],
        extra={'launcher_summary_path': str(run_dir / 'launcher_summary.json')},
    )
    print(json.dumps({
        'status': launcher_summary['status'],
        'round_name': round_name,
        'run_dir': str(run_dir),
        'launcher_summary': str(run_dir / 'launcher_summary.json'),
    }, ensure_ascii=False))
    return 0 if launcher_summary['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
