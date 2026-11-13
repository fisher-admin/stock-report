#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
PARALLEL_ROOT = EVOLUTION / 'parallel_lines'
CANONICAL_GREENFIELD = ROOT / 'outputs' / 'greenfield_multifactor'
TZ = ZoneInfo('Asia/Shanghai')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Launch multi-line greenfield experiment runs in parallel')
    parser.add_argument('--line-config', action='append', required=True, help='YAML config path for one experimental line; pass three times for three-line parallel runs')
    parser.add_argument('--run-name', default=None, help='Optional run name / directory name')
    parser.add_argument('--progress-output', default=str(PARALLEL_ROOT / 'three_line_longrun_latest.json'))
    parser.add_argument('--progress-log', default=str(PARALLEL_ROOT / 'three_line_longrun_latest.log'))
    parser.add_argument('--heartbeat-seconds', type=int, default=300, help='Emit heartbeat progress updates while lines are still running')
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        return yaml.safe_load(fh) or {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + '\n')


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


def bootstrap_static_artifacts(output_root: Path) -> dict[str, Any]:
    copied: list[str] = []
    for name in ['factor_pool_snapshot.json', 'current_factor_leaders.json', 'factor_pool_engineering_audit.md']:
        src = CANONICAL_GREENFIELD / name
        if not src.exists():
            continue
        dst = output_root / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(dst))
    return {'copied_artifacts': copied}


def line_name_from_config(config_path: Path, cfg: dict[str, Any]) -> str:
    meta = cfg.get('scenario_meta') or {}
    return str(meta.get('line_id') or meta.get('scenario_name') or config_path.stem)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def output_root_from_cfg(cfg: dict[str, Any], config_path: Path) -> Path:
    value = ((cfg.get('paths') or {}).get('output_root')) or ''
    if not value:
        raise ValueError(f'missing paths.output_root in {config_path}')
    return Path(value).expanduser().resolve()


def build_pending_payload(config_path: Path, run_dir: Path) -> dict[str, Any]:
    cfg = load_yaml(config_path)
    return {
        'line_name': line_name_from_config(config_path, cfg),
        'config_path': str(config_path),
        'output_root': str(output_root_from_cfg(cfg, config_path)),
        'run_dir': str(run_dir),
        'scenario_meta': cfg.get('scenario_meta') or {},
        'status': 'pending',
        'started_at': None,
        'finished_at': None,
    }


def run_line(config_path: Path, run_dir: Path) -> dict[str, Any]:
    cfg = load_yaml(config_path)
    line_name = line_name_from_config(config_path, cfg)
    output_root = output_root_from_cfg(cfg, config_path)

    payload: dict[str, Any] = {
        'line_name': line_name,
        'config_path': str(config_path),
        'output_root': str(output_root),
        'started_at': iso_now(),
        'run_dir': str(run_dir),
        'scenario_meta': cfg.get('scenario_meta') or {},
    }
    payload['bootstrap'] = bootstrap_static_artifacts(output_root)
    payload['market_hypotheses_step'] = run_step([sys.executable, str(ROOT / 'scripts' / 'run_greenfield_market_hypotheses.py'), '--config', str(config_path)])
    if payload['market_hypotheses_step']['returncode'] != 0:
        payload['status'] = 'error'
        payload['finished_at'] = iso_now()
        write_json(run_dir / f'{line_name}.json', payload)
        return payload

    payload['backtest_step'] = run_step([sys.executable, str(ROOT / 'scripts' / 'run_greenfield_multifactor_backtest.py'), '--config', str(config_path)])
    payload['status'] = 'ok' if payload['backtest_step']['returncode'] == 0 else 'error'

    summary_path = output_root / 'validation_summary.json'
    portfolio_path = output_root / 'portfolio_spec.json'
    selection_path = output_root / 'current_selection_snapshot.json'
    if summary_path.exists():
        payload['validation_summary'] = load_json(summary_path)
    if portfolio_path.exists():
        payload['portfolio_spec'] = load_json(portfolio_path)
    if selection_path.exists():
        payload['current_selection_snapshot'] = load_json(selection_path)

    payload['finished_at'] = iso_now()
    write_json(run_dir / f'{line_name}.json', payload)
    return payload


def emit_progress(
    progress_output: Path,
    progress_log: Path,
    run_name: str,
    run_dir: Path,
    line_payloads: list[dict[str, Any]],
    status: str,
    stage: str,
    heartbeat_seconds: int,
) -> None:
    generated_at = iso_now()
    heartbeat_deadline = (datetime.fromisoformat(generated_at) + timedelta(seconds=max(1, heartbeat_seconds))).isoformat(timespec='seconds')
    summary = {
        'schema_version': '2026-04-22.greenfield_parallel_lines.v1',
        'generated_at': generated_at,
        'status': status,
        'stage': stage,
        'run_name': run_name,
        'run_dir': str(run_dir),
        'heartbeat_seconds': heartbeat_seconds,
        'expected_next_update_before': heartbeat_deadline,
        'line_count': len(line_payloads),
        'completed_lines': [item.get('line_name') for item in line_payloads if item.get('status') in {'ok', 'error'}],
        'ok_lines': [item.get('line_name') for item in line_payloads if item.get('status') == 'ok'],
        'error_lines': [item.get('line_name') for item in line_payloads if item.get('status') == 'error'],
        'lines': line_payloads,
    }
    write_json(progress_output, summary)
    append_log(progress_log, {
        'generated_at': summary['generated_at'],
        'status': status,
        'stage': stage,
        'heartbeat_seconds': heartbeat_seconds,
        'expected_next_update_before': heartbeat_deadline,
        'completed_lines': summary['completed_lines'],
        'ok_lines': summary['ok_lines'],
        'error_lines': summary['error_lines'],
    })


def main() -> int:
    args = parse_args()
    config_paths = [Path(item).expanduser().resolve() for item in args.line_config]
    run_name = args.run_name or datetime.now(TZ).strftime('%Y%m%dT%H%M%S_three_line_longrun')
    run_dir = PARALLEL_ROOT / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    progress_output = Path(args.progress_output).expanduser().resolve()
    progress_log = Path(args.progress_log).expanduser().resolve()

    manifest = {
        'run_name': run_name,
        'run_dir': str(run_dir),
        'config_paths': [str(path) for path in config_paths],
        'started_at': iso_now(),
        'heartbeat_seconds': args.heartbeat_seconds,
    }
    write_json(run_dir / 'manifest.json', manifest)

    line_payloads = {
        str(path): build_pending_payload(path, run_dir)
        for path in config_paths
    }
    emit_progress(
        progress_output,
        progress_log,
        run_name,
        run_dir,
        sorted(line_payloads.values(), key=lambda item: item.get('line_name', '')),
        status='running',
        stage='started',
        heartbeat_seconds=args.heartbeat_seconds,
    )

    with ThreadPoolExecutor(max_workers=min(3, len(config_paths))) as executor:
        future_map = {executor.submit(run_line, path, run_dir): path for path in config_paths}
        pending = set(future_map)
        while pending:
            done, pending = wait(pending, timeout=max(1, args.heartbeat_seconds), return_when=FIRST_COMPLETED)
            if not done:
                emit_progress(
                    progress_output,
                    progress_log,
                    run_name,
                    run_dir,
                    sorted(line_payloads.values(), key=lambda item: item.get('line_name', '')),
                    status='running',
                    stage='heartbeat',
                    heartbeat_seconds=args.heartbeat_seconds,
                )
                continue
            for future in done:
                result = future.result()
                line_payloads[str(future_map[future])] = result
                emit_progress(
                    progress_output,
                    progress_log,
                    run_name,
                    run_dir,
                    sorted(line_payloads.values(), key=lambda item: item.get('line_name', '')),
                    status='running',
                    stage=f"line_complete:{result.get('line_name')}",
                    heartbeat_seconds=args.heartbeat_seconds,
                )

    line_payloads_list = sorted(line_payloads.values(), key=lambda item: item.get('line_name', ''))
    final_status = 'ok' if line_payloads_list and all(item.get('status') == 'ok' for item in line_payloads_list) else 'error'
    emit_progress(
        progress_output,
        progress_log,
        run_name,
        run_dir,
        line_payloads_list,
        status=final_status,
        stage='completed',
        heartbeat_seconds=args.heartbeat_seconds,
    )
    print(json.dumps({
        'status': final_status,
        'run_name': run_name,
        'run_dir': str(run_dir),
        'progress_output': str(progress_output),
    }, ensure_ascii=False))
    return 0 if final_status == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
