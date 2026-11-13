#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
PARALLEL_ROOT = EVOLUTION / 'parallel_lines'
RECORDER_SCRIPT = ROOT / 'scripts' / 'qlib_adapter' / 'record_greenfield_parallel_lines_round.py'
QLIB_PYTHON = ROOT / 'qlib_lab' / 'venv' / 'bin' / 'python'
TZ = ZoneInfo('Asia/Shanghai')
REQUIRED_TERMINAL_ARTIFACTS = [
    'validation_summary.json',
    'portfolio_spec.json',
    'current_selection_snapshot.json',
]
FRESH_RUNNING_ARTIFACTS = [
    'market_state_hypotheses.json',
    'market_state_hypotheses.md',
    'walkforward_report.json',
    'analog_validation.json',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Monitor a greenfield parallel-lines run and keep latest JSON/log fresh.')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--progress-output', type=Path, default=PARALLEL_ROOT / 'three_line_longrun_latest.json')
    parser.add_argument('--progress-log', type=Path, default=PARALLEL_ROOT / 'three_line_longrun_latest.log')
    parser.add_argument('--poll-seconds', type=int, default=300)
    parser.add_argument('--round-name', default=None, help='Recorder round name; defaults to run_name from manifest')
    parser.add_argument('--qlib-summary-output', type=Path, default=PARALLEL_ROOT / 'qlib_parallel_lines_recorder_latest.json')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--force-recorder', action='store_true')
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + '\n')


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        return yaml.safe_load(fh) or {}


def line_name_from_config(config_path: Path, cfg: dict[str, Any]) -> str:
    meta = cfg.get('scenario_meta') or {}
    return str(meta.get('line_id') or meta.get('scenario_name') or config_path.stem)


def output_root_from_cfg(cfg: dict[str, Any], config_path: Path) -> Path:
    value = ((cfg.get('paths') or {}).get('output_root')) or ''
    if not value:
        raise ValueError(f'missing paths.output_root in {config_path}')
    return Path(value).expanduser().resolve()


def parse_ps() -> list[dict[str, Any]]:
    proc = subprocess.run(
        ['ps', '-Ao', 'pid=,ppid=,etime=,%cpu=,%mem=,command='],
        text=True,
        capture_output=True,
        check=True,
    )
    rows: list[dict[str, Any]] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, elapsed, cpu, mem, command = parts
        rows.append({
            'pid': int(pid),
            'ppid': int(ppid),
            'elapsed': elapsed,
            'cpu_percent': float(cpu),
            'mem_percent': float(mem),
            'command': command,
        })
    return rows


def artifact_entry(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        'name': path.name,
        'size': stat.st_size,
        'mtime': datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(timespec='seconds'),
    }


def fresh_artifacts(output_root: Path, started_at: datetime, names: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name in names:
        path = output_root / name
        if not path.exists():
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, TZ)
        if mtime >= started_at:
            items.append({
                'name': name,
                'size': stat.st_size,
                'mtime': mtime.isoformat(timespec='seconds'),
            })
    return items


def collect_output_snapshot(output_root: Path, started_at: datetime) -> dict[str, Any]:
    if not output_root.exists():
        return {
            'artifact_count': 0,
            'files': [],
            'fresh_running_artifacts': [],
            'fresh_terminal_artifacts': [],
            'latest_artifact_mtime': None,
        }
    files = sorted((item for item in output_root.iterdir() if item.is_file()), key=lambda item: item.name)
    entries = [artifact_entry(item) for item in files]
    latest_artifact_mtime = max((entry['mtime'] for entry in entries), default=None)
    return {
        'artifact_count': len(entries),
        'files': entries,
        'fresh_running_artifacts': fresh_artifacts(output_root, started_at, FRESH_RUNNING_ARTIFACTS),
        'fresh_terminal_artifacts': fresh_artifacts(output_root, started_at, REQUIRED_TERMINAL_ARTIFACTS),
        'latest_artifact_mtime': latest_artifact_mtime,
    }


def load_line_descriptors(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for item in manifest.get('config_paths') or []:
        config_path = Path(str(item)).expanduser().resolve()
        cfg = load_yaml(config_path)
        descriptors.append({
            'config_path': str(config_path),
            'line_name': line_name_from_config(config_path, cfg),
            'output_root': str(output_root_from_cfg(cfg, config_path)),
            'scenario_meta': cfg.get('scenario_meta') or {},
        })
    return sorted(descriptors, key=lambda entry: entry['line_name'])


def line_processes(rows: list[dict[str, Any]], config_path: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in rows:
        command = row['command']
        if config_path not in command:
            continue
        if 'run_greenfield_multifactor_backtest.py' not in command and 'run_greenfield_market_hypotheses.py' not in command:
            continue
        matches.append(row)
    return sorted(matches, key=lambda entry: entry['pid'])


def launcher_processes(rows: list[dict[str, Any]], run_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in rows:
        command = row['command']
        if 'run_greenfield_parallel_lines.py' not in command:
            continue
        if f'--run-name {run_name}' not in command and run_name not in command:
            continue
        matches.append(row)
    return sorted(matches, key=lambda entry: entry['pid'])


def infer_line_payload(descriptor: dict[str, Any], run_dir: Path, started_at: datetime, rows: list[dict[str, Any]]) -> dict[str, Any]:
    line_name = descriptor['line_name']
    line_result_path = run_dir / f'{line_name}.json'
    output_root = Path(descriptor['output_root'])
    snapshot = collect_output_snapshot(output_root, started_at)
    processes = line_processes(rows, descriptor['config_path'])

    if line_result_path.exists():
        payload = load_json(line_result_path)
        payload['processes'] = processes
        payload['artifact_count'] = snapshot['artifact_count']
        payload['files'] = snapshot['files']
        payload['fresh_running_artifacts'] = snapshot['fresh_running_artifacts']
        payload['fresh_terminal_artifacts'] = snapshot['fresh_terminal_artifacts']
        payload['latest_artifact_mtime'] = snapshot['latest_artifact_mtime']
        return payload

    payload: dict[str, Any] = {
        'line_name': line_name,
        'config_path': descriptor['config_path'],
        'output_root': descriptor['output_root'],
        'run_dir': str(run_dir),
        'scenario_meta': descriptor['scenario_meta'],
        'started_at': started_at.isoformat(timespec='seconds'),
        'finished_at': None,
        'processes': processes,
        'artifact_count': snapshot['artifact_count'],
        'files': snapshot['files'],
        'fresh_running_artifacts': snapshot['fresh_running_artifacts'],
        'fresh_terminal_artifacts': snapshot['fresh_terminal_artifacts'],
        'latest_artifact_mtime': snapshot['latest_artifact_mtime'],
    }

    if processes:
        payload['status'] = 'running'
    elif snapshot['fresh_terminal_artifacts']:
        payload['status'] = 'ok'
        payload['finished_at'] = max(item['mtime'] for item in snapshot['fresh_terminal_artifacts'])
        payload['inference_note'] = 'status inferred from fresh terminal artifacts because line result JSON is not yet present'
    elif snapshot['fresh_running_artifacts']:
        payload['status'] = 'running'
        payload['inference_note'] = 'status inferred from fresh in-flight artifacts after run start'
    else:
        payload['status'] = 'pending'

    return payload


def build_progress_payload(run_dir: Path, progress_output: Path, progress_log: Path, poll_seconds: int) -> dict[str, Any]:
    manifest = load_json(run_dir / 'manifest.json')
    if not manifest:
        raise FileNotFoundError(f'manifest missing under {run_dir}')

    run_name = str(manifest.get('run_name') or run_dir.name)
    started_at = datetime.fromisoformat(str(manifest['started_at']))
    descriptors = load_line_descriptors(manifest)
    rows = parse_ps()
    launchers = launcher_processes(rows, run_name)
    line_payloads = [infer_line_payload(item, run_dir, started_at, rows) for item in descriptors]
    line_payloads = sorted(line_payloads, key=lambda entry: entry['line_name'])

    completed_lines = [item['line_name'] for item in line_payloads if item.get('status') in {'ok', 'error'}]
    ok_lines = [item['line_name'] for item in line_payloads if item.get('status') == 'ok']
    error_lines = [item['line_name'] for item in line_payloads if item.get('status') == 'error']
    running = any(item.get('status') not in {'ok', 'error'} for item in line_payloads)
    status = 'running' if running else ('ok' if line_payloads and len(error_lines) == 0 else 'error')
    stage = 'heartbeat' if running else 'completed'
    generated_at = iso_now()
    expected_next_update_before = (datetime.fromisoformat(generated_at) + timedelta(seconds=max(1, poll_seconds))).isoformat(timespec='seconds')

    return {
        'schema_version': '2026-04-22.greenfield_parallel_lines.v1',
        'generated_at': generated_at,
        'status': status,
        'stage': stage,
        'run_name': run_name,
        'run_dir': str(run_dir),
        'progress_output': str(progress_output),
        'progress_log': str(progress_log),
        'heartbeat_seconds': poll_seconds,
        'expected_next_update_before': expected_next_update_before,
        'line_count': len(line_payloads),
        'completed_lines': completed_lines,
        'ok_lines': ok_lines,
        'error_lines': error_lines,
        'launcher_processes': launchers,
        'launcher_pid': launchers[0]['pid'] if launchers else None,
        'lines': line_payloads,
        'monitor': {
            'pid': os.getpid(),
            'record_greenfield_parallel_lines_round': str(RECORDER_SCRIPT),
        },
    }


def emit_progress(run_dir: Path, progress_output: Path, progress_log: Path, payload: dict[str, Any]) -> None:
    write_json(progress_output, payload)
    write_json(run_dir / 'live_monitor_snapshot.json', payload)
    append_log(progress_log, {
        'generated_at': payload['generated_at'],
        'status': payload['status'],
        'stage': payload['stage'],
        'heartbeat_seconds': payload['heartbeat_seconds'],
        'expected_next_update_before': payload['expected_next_update_before'],
        'launcher_pid': payload.get('launcher_pid'),
        'completed_lines': payload['completed_lines'],
        'ok_lines': payload['ok_lines'],
        'error_lines': payload['error_lines'],
    })


def sync_qlib(run_dir: Path, payload: dict[str, Any], round_name: str, qlib_summary_output: Path, force_recorder: bool) -> dict[str, Any] | None:
    summary_path = run_dir / 'qlib_recorder_sync.json'
    if summary_path.exists() and not force_recorder:
        return load_json(summary_path)
    recorder_python = str(QLIB_PYTHON if QLIB_PYTHON.exists() else Path(sys.executable))
    cmd = [
        recorder_python,
        str(RECORDER_SCRIPT),
        '--run-dir',
        str(run_dir),
        '--progress-output',
        str(payload['progress_output']),
        '--progress-log',
        str(payload['progress_log']),
        '--round-name',
        round_name,
        '--summary-output',
        str(qlib_summary_output),
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    result = {
        'command': cmd,
        'returncode': proc.returncode,
        'stdout_tail': (proc.stdout or '').splitlines()[-120:],
        'stderr_tail': (proc.stderr or '').splitlines()[-120:],
    }
    write_json(run_dir / 'qlib_recorder_step.json', result)
    return load_json(summary_path) if summary_path.exists() else result


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    progress_output = args.progress_output.expanduser().resolve()
    progress_log = args.progress_log.expanduser().resolve()
    qlib_summary_output = args.qlib_summary_output.expanduser().resolve()

    while True:
        payload = build_progress_payload(run_dir, progress_output, progress_log, args.poll_seconds)
        emit_progress(run_dir, progress_output, progress_log, payload)
        if payload['status'] != 'running':
            round_name = args.round_name or payload['run_name']
            qlib_result = sync_qlib(run_dir, payload, round_name, qlib_summary_output, args.force_recorder)
            if qlib_result is not None:
                latest = load_json(progress_output)
                latest['qlib_recorder_sync'] = qlib_result
                write_json(progress_output, latest)
                write_json(run_dir / 'live_monitor_snapshot.json', latest)
            return 0 if payload['status'] == 'ok' else 1
        if args.once:
            return 0
        time.sleep(max(1, args.poll_seconds))


if __name__ == '__main__':
    raise SystemExit(main())
