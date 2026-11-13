#!/usr/bin/env python3
"""Record greenfield parallel-lines experiment outputs into the local Qlib recorder."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
PARALLEL_ROOT = ROOT / 'outputs/evolution/parallel_lines'
QLIB_MLRUNS = ROOT / 'qlib_lab/mlruns'
EXPERIMENT_NAME = 'factor_factory_rounds'
TZ = ZoneInfo('Asia/Shanghai')

CORE_LINE_ARTIFACTS = [
    'market_state_hypotheses.json',
    'market_state_hypotheses.md',
    'walkforward_report.json',
    'analog_validation.json',
    'portfolio_spec.json',
    'current_selection_snapshot.json',
    'validation_summary.json',
    'factor_pool_snapshot.json',
    'current_factor_leaders.json',
    'factor_pool_engineering_audit.md',
]


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def slugify(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]+', '_', value).strip('_').lower() or 'line'


def expand_artifact_paths(paths: list[Path]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            candidates = sorted(item for item in path.rglob('*') if item.is_file())
        else:
            candidates = [path]
        for candidate in candidates:
            text = str(candidate)
            if text in seen:
                continue
            seen.add(text)
            expanded.append(text)
    return expanded


def collect_line_artifacts(line_payload: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    output_root = Path(str(line_payload.get('output_root') or '')).expanduser()
    if output_root.exists():
        for name in CORE_LINE_ARTIFACTS:
            candidate = output_root / name
            if candidate.exists():
                paths.append(candidate)
    return paths


def fallback_line_payloads(run_dir: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob('*.json')):
        if path.name in {'manifest.json', 'qlib_recorder_sync.json'}:
            continue
        payload = load_json(path)
        if payload:
            payloads.append(payload)
    return payloads


def build_payload(run_dir: Path, progress_output: Path, progress_log: Path, round_name: str) -> dict[str, Any]:
    latest = load_json(progress_output)
    manifest = load_json(run_dir / 'manifest.json')
    line_payloads = latest.get('lines') or fallback_line_payloads(run_dir)
    run_name = str(latest.get('run_name') or manifest.get('run_name') or run_dir.name)

    params = {
        'round_name': round_name,
        'recorded_at': iso_now(),
        'run_type': 'greenfield_parallel_lines',
        'run_name': run_name,
        'parallel_status': str(latest.get('status') or 'unknown'),
        'parallel_stage': str(latest.get('stage') or 'unknown'),
        'legacy_prebreakout_role': 'reference_only',
        'line_count': str(len(line_payloads) or len(manifest.get('config_paths') or [])),
        'line_names': ','.join(sorted(str(item.get('line_name') or 'unknown') for item in line_payloads)),
    }

    metrics: dict[str, float] = {
        'line_count': float(len(line_payloads) or len(manifest.get('config_paths') or [])),
        'ok_count': float(len(latest.get('ok_lines') or [item.get('line_name') for item in line_payloads if item.get('status') == 'ok'])),
        'error_count': float(len(latest.get('error_lines') or [item.get('line_name') for item in line_payloads if item.get('status') == 'error'])),
        'completed_count': float(len(latest.get('completed_lines') or [item.get('line_name') for item in line_payloads if item.get('status') in {'ok', 'error'}])),
    }

    artifact_paths: list[Path] = [progress_output, progress_log, run_dir / 'manifest.json', run_dir]

    for line_payload in line_payloads:
        line_name = str(line_payload.get('line_name') or 'line')
        slug = slugify(line_name)
        status = str(line_payload.get('status') or 'unknown')
        metrics[f'{slug}_status_ok'] = 1.0 if status == 'ok' else 0.0
        metrics[f'{slug}_status_error'] = 1.0 if status == 'error' else 0.0
        metrics[f'{slug}_status_pending'] = 1.0 if status == 'pending' else 0.0

        validation_summary = line_payload.get('validation_summary') or {}
        if not validation_summary:
            summary_path = Path(str(line_payload.get('output_root') or '')) / 'validation_summary.json'
            validation_summary = load_json(summary_path)
        for key in [
            'walkforward_close_to_close_excess_total_return',
            'walkforward_open_to_close_excess_total_return',
            'analog_close_to_close_total_return',
            'analog_open_to_close_total_return',
        ]:
            value = validation_summary.get(key)
            if value is not None:
                metrics[f'{slug}_{key}'] = float(value)

        artifact_paths.extend(collect_line_artifacts(line_payload))
        line_result_path = run_dir / f'{line_name}.json'
        if line_result_path.exists():
            artifact_paths.append(line_result_path)

    return {
        'params': params,
        'metrics': metrics,
        'artifact_paths': expand_artifact_paths(artifact_paths),
    }


def record_to_qlib(payload: dict[str, Any], recorder_name: str, mlruns_uri: str) -> dict[str, Any]:
    import qlib
    from qlib.workflow import R

    qlib.init(
        provider_uri=str(ROOT / 'qlib_lab/qlib_data'),
        exp_manager={
            'class': 'MLflowExpManager',
            'module_path': 'qlib.workflow.expm',
            'kwargs': {
                'uri': mlruns_uri,
                'default_exp_name': EXPERIMENT_NAME,
            },
        },
        logging_level='WARNING',
    )

    with R.start(experiment_name=EXPERIMENT_NAME, recorder_name=recorder_name):
        R.log_params(**payload['params'])
        R.log_metrics(**payload['metrics'])
        for artifact in payload['artifact_paths']:
            R.log_artifact(artifact, artifact_path='greenfield_parallel_lines')
        recorder = R.get_recorder()
        return {
            'experiment_name': EXPERIMENT_NAME,
            'recorder_id': recorder.id,
            'recorder_name': recorder.name,
            'artifact_count': len(payload['artifact_paths']),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--progress-output', type=Path, default=PARALLEL_ROOT / 'three_line_longrun_latest.json')
    parser.add_argument('--progress-log', type=Path, default=PARALLEL_ROOT / 'three_line_longrun_latest.log')
    parser.add_argument('--round-name', default=f'greenfield_parallel_lines_{datetime.now(TZ).strftime("%Y%m%dT%H%M%S")}')
    parser.add_argument('--mlruns-uri', default=f'file:{QLIB_MLRUNS}')
    parser.add_argument('--summary-output', type=Path, default=PARALLEL_ROOT / 'qlib_parallel_lines_recorder_latest.json')
    parser.add_argument('--dry-run', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    progress_output = args.progress_output.expanduser().resolve()
    progress_log = args.progress_log.expanduser().resolve()
    payload = build_payload(run_dir, progress_output, progress_log, args.round_name)
    if args.dry_run:
        result = {
            'dry_run': True,
            'payload': payload,
        }
    else:
        result = {
            'dry_run': False,
            'payload_summary': {
                'param_count': len(payload['params']),
                'metric_count': len(payload['metrics']),
                'artifact_count': len(payload['artifact_paths']),
            },
            'qlib_recorder': record_to_qlib(payload, args.round_name, args.mlruns_uri),
        }
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    run_dir_summary = run_dir / 'qlib_recorder_sync.json'
    run_dir_summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
