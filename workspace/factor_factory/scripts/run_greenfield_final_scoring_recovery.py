#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from greenfield_multifactor_lib import (
    add_cross_sectional_zscores,
    combine_factor_scores,
    current_state_from_panel,
    dump_json,
    load_config,
    read_parquet_with_retries,
    write_parquet_atomic,
)
from run_greenfield_multifactor_backtest import train_factor_scores, select_weights

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
PARALLEL_ROOT = ROOT / 'outputs' / 'evolution' / 'parallel_lines'
QLIB_RECORDER_SCRIPT = ROOT / 'scripts' / 'qlib_adapter' / 'record_greenfield_parallel_lines_round.py'
QLIB_PYTHON = ROOT / 'qlib_lab' / 'venv' / 'bin' / 'python'
TZ = ZoneInfo('Asia/Shanghai')
PARENT_RUN_NAME = '20260422_three_line_longrun'
DEFAULT_RUN_NAME = '20260422_sharded_final_scoring_recovery'
ROOT_SCHEMA = '2026-04-22.greenfield_parallel_lines.v2'
LINE_SCHEMA = '2026-04-22.final_scoring_recovery.line.v1'
SHARD_SCHEMA = '2026-04-22.final_scoring_recovery.shard.v1'

LINE_PLANS: dict[str, dict[str, Any]] = {
    'line_b_downside_rotation_c2c': {
        'priority': 1,
        'shard_count': 6,
        'shard_key': 'downside_control_bucket_x_sector_bucket',
        'primary_bucket_count': 3,
        'sector_bucket_count': 2,
        'bucket_hint': 'downside_control bucket x sector bucket',
    },
    'line_a_liquid_sector_balance': {
        'priority': 2,
        'shard_count': 4,
        'shard_key': 'liquidity_bucket_x_sector_bucket',
        'primary_bucket_count': 2,
        'sector_bucket_count': 2,
        'bucket_hint': 'liquidity bucket x sector bucket',
    },
    'line_c_diversified_followthrough': {
        'priority': 3,
        'shard_count': 6,
        'shard_key': 'dominant_factor_family_bucket_x_sector_bucket',
        'primary_bucket_count': 3,
        'sector_bucket_count': 2,
        'bucket_hint': 'factor-family bucket x sector bucket',
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Recover missing final-scoring terminal artifacts with shard-level persistence and timeouts.')
    parser.add_argument('--mode', choices=['orchestrate', 'line', 'shard'], default='orchestrate')
    parser.add_argument('--run-name', default=DEFAULT_RUN_NAME)
    parser.add_argument('--parent-run-name', default=PARENT_RUN_NAME)
    parser.add_argument('--parent-run-dir', default=None)
    parser.add_argument('--line-config', action='append', default=[])
    parser.add_argument('--max-line-workers', type=int, default=2)
    parser.add_argument('--poll-seconds', type=int, default=15)
    parser.add_argument('--shard-timeout-seconds', type=int, default=600)
    parser.add_argument('--config', default=None)
    parser.add_argument('--line-name', default=None)
    parser.add_argument('--run-dir', default=None)
    parser.add_argument('--line-dir', default=None)
    parser.add_argument('--shard-id', type=int, default=None)
    parser.add_argument('--summary-output', default=None)
    parser.add_argument('--progress-output', default=None)
    parser.add_argument('--progress-log', default=None)
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + '\n')


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def line_name_from_config(config_path: Path) -> str:
    cfg = load_config(config_path)
    return str(cfg.scenario_meta.get('line_id') or config_path.stem)


def line_paths(run_dir: Path, line_name: str) -> dict[str, Path]:
    line_dir = run_dir / line_name
    line_dir.mkdir(parents=True, exist_ok=True)
    shard_prefix = lambda shard_id: f'{shard_id:02d}'
    return {
        'line_dir': line_dir,
        'progress': line_dir / 'progress.json',
        'precomputed_state': line_dir / 'precomputed_state.json',
        'latest_rows_source': line_dir / 'latest_rows_source.parquet',
        'shard_plan': line_dir / 'shard_plan.json',
        'factor_scores': line_dir / 'precomputed_factor_scores.json',
        'weights': line_dir / 'precomputed_weights.json',
        'portfolio_spec': line_dir / 'portfolio_spec.json',
        'current_selection_snapshot': line_dir / 'current_selection_snapshot.json',
        'validation_summary': line_dir / 'validation_summary.json',
        'line_result': run_dir / f'{line_name}.json',
        'stdout_log': line_dir / 'worker_stdout.log',
        'stderr_log': line_dir / 'worker_stderr.log',
    }


def shard_paths(line_dir: Path, shard_id: int) -> dict[str, Path]:
    suffix = f'{shard_id:02d}'
    return {
        'score_parquet': line_dir / f'scores_shard_{suffix}.parquet',
        'score_json': line_dir / f'scores_shard_{suffix}.json',
        'rank_json': line_dir / f'rank_shard_{suffix}.json',
    }


def validate_parent(parent_run_dir: Path, config_paths: list[Path]) -> dict[str, Any]:
    manifest = read_json(parent_run_dir / 'manifest.json')
    timeout_closure = manifest.get('timeout_closure') or {}
    if timeout_closure.get('status') != 'timeout_closure':
        raise SystemExit(f'parent run is not timeout_closure: {parent_run_dir}')

    lines: list[dict[str, Any]] = []
    for config_path in config_paths:
        line_name = line_name_from_config(config_path)
        payload = read_json(parent_run_dir / f'{line_name}.json')
        if not payload:
            raise SystemExit(f'missing parent line payload: {line_name}')
        if payload.get('last_completed_stage') != 'analog_validation':
            raise SystemExit(f'parent line {line_name} last_completed_stage != analog_validation')
        lines.append({
            'line_name': line_name,
            'status': payload.get('status'),
            'closure_status': payload.get('closure_status'),
            'last_completed_stage': payload.get('last_completed_stage'),
            'config_path': str(config_path),
            'output_root': payload.get('output_root'),
            'missing_terminal_artifacts': payload.get('missing_terminal_artifacts') or [],
        })
    return {
        'manifest': manifest,
        'timeout_closure_report': read_json(parent_run_dir / 'timeout_closure_report.json'),
        'lines': lines,
    }


def quantile_bucket(series: pd.Series, bucket_count: int) -> pd.Series:
    if bucket_count <= 1:
        return pd.Series(np.zeros(len(series), dtype=int), index=series.index)
    numeric = pd.to_numeric(series, errors='coerce')
    ranks = numeric.rank(method='first', pct=True)
    raw = np.floor((ranks.fillna(0.0) * bucket_count) - 1e-9)
    bucket = pd.Series(raw, index=series.index).clip(lower=0, upper=bucket_count - 1).astype(int)
    bucket.loc[numeric.isna()] = 0
    return bucket


def stable_bucket(value: Any, bucket_count: int) -> int:
    if bucket_count <= 1:
        return 0
    text = str(value or 'unknown')
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()
    return int(digest[:8], 16) % bucket_count


def build_dominant_family_series(
    df: pd.DataFrame,
    weights: dict[str, float],
    factor_meta: list[dict[str, Any]],
) -> tuple[pd.Series, dict[str, int]]:
    families = sorted({str(item['family']) for item in factor_meta}) or ['unknown']
    family_to_bucket = {family: idx % 3 for idx, family in enumerate(families)}
    contribution_map: dict[str, pd.Series] = {}
    for item in factor_meta:
        family = str(item['family'])
        factor_id = str(item['factor_id'])
        if factor_id not in weights:
            continue
        z_col = f'{factor_id}__z'
        if z_col not in df.columns:
            continue
        contrib = df[z_col].abs() * abs(float(weights[factor_id]))
        if family not in contribution_map:
            contribution_map[family] = contrib
        else:
            contribution_map[family] = contribution_map[family].add(contrib, fill_value=0.0)
    if not contribution_map:
        dominant = pd.Series([families[0]] * len(df), index=df.index, dtype='object')
        return dominant, family_to_bucket
    contribution_df = pd.DataFrame(contribution_map)
    dominant = contribution_df.idxmax(axis=1)
    dominant = dominant.fillna(families[0]).astype(str)
    return dominant, family_to_bucket


def assign_shards(
    latest_rows: pd.DataFrame,
    line_name: str,
    weights: dict[str, float],
    factor_meta: list[dict[str, Any]],
    shard_count: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = latest_rows.copy()
    plan = LINE_PLANS[line_name]
    sector_bucket_count = int(plan['sector_bucket_count'])
    df['sector_bucket'] = df['industry'].fillna('未知').astype(str).map(lambda x: stable_bucket(x, sector_bucket_count)).astype(int)

    if line_name == 'line_a_liquid_sector_balance':
        df['primary_bucket'] = quantile_bucket(df.get('avg_amount_lookback'), int(plan['primary_bucket_count']))
        df['primary_bucket_label'] = df['primary_bucket'].map(lambda x: f'liquidity_bucket_{x}')
    elif line_name == 'line_b_downside_rotation_c2c':
        bucket_cols = [col for col in ['g_drawdown_resilience__z', 'g_low_vol_leader__z'] if col in df.columns]
        if not bucket_cols:
            bucket_cols = [col for col in ['g_drawdown_resilience', 'g_low_vol_leader'] if col in df.columns]
        if bucket_cols:
            downside_score = df[bucket_cols].apply(pd.to_numeric, errors='coerce').mean(axis=1)
        else:
            downside_score = pd.Series(np.zeros(len(df), dtype=float), index=df.index)
        df['primary_bucket'] = quantile_bucket(downside_score, int(plan['primary_bucket_count']))
        df['primary_bucket_label'] = df['primary_bucket'].map(lambda x: f'downside_bucket_{x}')
    else:
        dominant_family, family_to_bucket = build_dominant_family_series(df, weights, factor_meta)
        df['dominant_family'] = dominant_family
        df['primary_bucket'] = df['dominant_family'].map(lambda x: family_to_bucket.get(str(x), 0)).astype(int)
        df['primary_bucket_label'] = df['dominant_family'].map(lambda x: f'dominant_family_{x}')

    df['shard_id'] = (df['primary_bucket'] * sector_bucket_count + df['sector_bucket']).astype(int)
    if shard_count > 0:
        df['shard_id'] = df['shard_id'] % shard_count
    df['shard_label'] = df.apply(
        lambda row: f"{row['primary_bucket_label']}__sector_bucket_{int(row['sector_bucket'])}",
        axis=1,
    )

    shard_rows: list[dict[str, Any]] = []
    for shard_id in range(shard_count):
        subset = df.loc[df['shard_id'] == shard_id]
        label = str(subset['shard_label'].mode().iloc[0]) if not subset.empty else f'empty_shard_{shard_id}'
        shard_rows.append({
            'shard_id': shard_id,
            'status': 'pending',
            'row_count': int(len(subset)),
            'shard_label': label,
            'started_at': None,
            'finished_at': None,
            'score_parquet': str(shard_paths(Path('.'), shard_id)['score_parquet'].name),
            'score_json': str(shard_paths(Path('.'), shard_id)['score_json'].name),
            'rank_json': str(shard_paths(Path('.'), shard_id)['rank_json'].name),
            'last_file_at': None,
        })
    shard_plan = {
        'line_name': line_name,
        'generated_at': iso_now(),
        'shard_count': shard_count,
        'shard_key': plan['shard_key'],
        'bucket_hint': plan['bucket_hint'],
        'shards': shard_rows,
    }
    return df, shard_plan


def build_validation_summary(walkforward_report: dict[str, Any], analog_report: dict[str, Any], run_context: dict[str, Any], recovery_meta: dict[str, Any]) -> dict[str, Any]:
    wf_agg = (walkforward_report.get('aggregate') or {}).get('excess') or {}
    analog_agg = analog_report.get('aggregate') or {}
    return {
        'mainline': 'greenfield_multifactor',
        'run_context': run_context,
        'legacy_prebreakout_role': 'reference_only',
        'walkforward_close_to_close_excess_total_return': wf_agg.get('close_to_close_total_return'),
        'walkforward_open_to_close_excess_total_return': wf_agg.get('open_to_close_total_return'),
        'analog_close_to_close_total_return': ((analog_agg.get('close_to_close') or {}).get('total_return')),
        'analog_open_to_close_total_return': ((analog_agg.get('open_to_close') or {}).get('total_return')),
        'recovery': recovery_meta,
    }


def build_portfolio_spec(
    latest_date: str,
    current_regime_id: str,
    run_context: dict[str, Any],
    family_budget: dict[str, float],
    weights: dict[str, float],
    selected_factors: list[dict[str, Any]],
    recovery_meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        'schema_version': '2026-04-19.greenfield_portfolio_spec.v1',
        'trade_date': latest_date,
        'current_regime_id': current_regime_id,
        'run_context': run_context,
        'family_budget': family_budget,
        'weights': weights,
        'selected_factors': selected_factors,
        'legacy_reference_only': ['prebreakout_v41'],
        'recovery': recovery_meta,
    }


def build_current_selection_snapshot(
    latest_date: str,
    run_context: dict[str, Any],
    weights: dict[str, float],
    selected_factors: list[dict[str, Any]],
    current_selection: list[dict[str, Any]],
    recovery_meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        'trade_date': latest_date,
        'run_context': run_context,
        'weights': weights,
        'selected_factors': selected_factors,
        'top20': current_selection,
        'recovery': recovery_meta,
    }


def selected_score_columns(selection_cols: list[str], factor_meta: list[dict[str, Any]], weights: dict[str, float]) -> list[str]:
    selected_factor_ids = [str(item['factor_id']) for item in factor_meta if str(item['factor_id']) in weights]
    extra = [f'{factor_id}__z' for factor_id in selected_factor_ids]
    out = ['trade_date', 'shard_id', 'primary_bucket', 'sector_bucket', 'shard_label', 'composite_score']
    for name in selection_cols + extra:
        if name not in out:
            out.append(name)
    return out


def write_empty_shard_artifacts(
    line_dir: Path,
    shard_id: int,
    score_columns: list[str],
    status: str,
    line_name: str,
    error_message: str | None = None,
) -> dict[str, Any]:
    paths = shard_paths(line_dir, shard_id)
    empty_df = pd.DataFrame(columns=score_columns)
    write_parquet_atomic(empty_df, paths['score_parquet'], index=False)
    score_payload = {
        'schema_version': SHARD_SCHEMA,
        'generated_at': iso_now(),
        'line_name': line_name,
        'shard_id': shard_id,
        'status': status,
        'row_count': 0,
        'score_parquet': str(paths['score_parquet']),
        'error_message': error_message,
        'top_candidates': [],
    }
    rank_payload = {
        'schema_version': SHARD_SCHEMA,
        'generated_at': iso_now(),
        'line_name': line_name,
        'shard_id': shard_id,
        'status': status,
        'ranked_row_count': 0,
        'top_candidates': [],
        'error_message': error_message,
    }
    write_json(paths['score_json'], score_payload)
    write_json(paths['rank_json'], rank_payload)
    return {
        'status': status,
        'row_count': 0,
        'score_parquet': str(paths['score_parquet']),
        'score_json': str(paths['score_json']),
        'rank_json': str(paths['rank_json']),
    }


def read_line_progress(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    payload = read_json(path)
    if not payload:
        return fallback
    return payload


def emit_root_progress(run_dir: Path, progress_output: Path, progress_log: Path, run_name: str, line_progress_payloads: list[dict[str, Any]], stage: str) -> None:
    generated_at = iso_now()
    heartbeat_seconds = 60
    expected_next = (datetime.fromisoformat(generated_at) + timedelta(seconds=heartbeat_seconds)).isoformat(timespec='seconds')
    completed = [item['line_name'] for item in line_progress_payloads if item.get('status') in {'ok', 'error'}]
    ok_lines = [item['line_name'] for item in line_progress_payloads if item.get('status') == 'ok']
    error_lines = [item['line_name'] for item in line_progress_payloads if item.get('status') == 'error']
    status = 'running' if len(completed) < len(line_progress_payloads) else ('ok' if not error_lines else 'error')
    payload = {
        'schema_version': ROOT_SCHEMA,
        'generated_at': generated_at,
        'status': status,
        'stage': stage,
        'run_name': run_name,
        'run_dir': str(run_dir),
        'progress_output': str(progress_output),
        'progress_log': str(progress_log),
        'heartbeat_seconds': heartbeat_seconds,
        'expected_next_update_before': expected_next,
        'line_count': len(line_progress_payloads),
        'completed_lines': completed,
        'ok_lines': ok_lines,
        'error_lines': error_lines,
        'lines': line_progress_payloads,
    }
    write_json(progress_output, payload)
    append_log(progress_log, {
        'generated_at': generated_at,
        'status': status,
        'stage': stage,
        'completed_lines': completed,
        'ok_lines': ok_lines,
        'error_lines': error_lines,
    })
    write_json(run_dir / 'live_monitor_snapshot.json', payload)


def line_fallback_payload(line_name: str, config_path: Path, line_dir: Path) -> dict[str, Any]:
    return {
        'line_name': line_name,
        'config_path': str(config_path),
        'output_root': str(line_dir),
        'status': 'pending',
        'stage': 'pending',
        'started_at': None,
        'finished_at': None,
        'recovery_output_root': str(line_dir),
        'shard_stats': {'pending': 0, 'ok': 0, 'error': 0, 'timeout': 0, 'empty': 0},
        'shards': [],
    }


def build_line_result_payload(progress: dict[str, Any]) -> dict[str, Any]:
    payload = dict(progress)
    payload['generated_at'] = iso_now()
    return payload


def mirror_terminal_artifacts(dest_root: Path, portfolio_spec: dict[str, Any], selection_snapshot: dict[str, Any], validation_summary: dict[str, Any]) -> None:
    dest_root.mkdir(parents=True, exist_ok=True)
    dump_json(dest_root / 'portfolio_spec.json', portfolio_spec)
    dump_json(dest_root / 'current_selection_snapshot.json', selection_snapshot)
    dump_json(dest_root / 'validation_summary.json', validation_summary)


def line_concentration_stats(portfolio_spec: dict[str, Any]) -> dict[str, Any]:
    weights = portfolio_spec.get('weights') or {}
    abs_weights = [abs(float(v)) for v in weights.values() if v is not None]
    if not abs_weights:
        return {
            'selected_factor_count': int(len(portfolio_spec.get('selected_factors') or [])),
            'max_weight': None,
            'effective_factor_count': 0.0,
        }
    max_weight = float(max(abs_weights))
    total_sq = sum(value * value for value in abs_weights)
    effective_factor_count = float(1.0 / total_sq) if total_sq > 0 else 0.0
    return {
        'selected_factor_count': int(len(portfolio_spec.get('selected_factors') or [])),
        'max_weight': max_weight,
        'effective_factor_count': effective_factor_count,
    }


def overfit_risk_label(validation_summary: dict[str, Any], concentration: dict[str, Any]) -> str:
    wf_c2c = float(validation_summary.get('walkforward_close_to_close_excess_total_return') or 0.0)
    analog_c2c = float(validation_summary.get('analog_close_to_close_total_return') or 0.0)
    max_weight = concentration.get('max_weight')
    effective_factor_count = float(concentration.get('effective_factor_count') or 0.0)
    selected_factor_count = int(concentration.get('selected_factor_count') or 0)
    if wf_c2c <= 0 or analog_c2c <= 0:
        return 'high'
    if max_weight is not None and max_weight >= 0.45:
        return 'high'
    if selected_factor_count < 3 or effective_factor_count < 2.5:
        return 'high'
    if max_weight is not None and max_weight >= 0.35:
        return 'medium'
    if effective_factor_count < 4.0:
        return 'medium'
    return 'low'


def next_round_guidance(line_name: str, shard_summary: dict[str, Any], concentration: dict[str, Any]) -> list[str]:
    guidance: list[str] = []
    timeout_count = int(shard_summary.get('timeout', 0))
    max_weight = concentration.get('max_weight')
    if line_name == 'line_b_downside_rotation_c2c':
        guidance.append('继续沿 downside-control x sector bucket 缩小高换手 shard；优先对 timeout 或高波动 shard 二次切分。')
        if max_weight is not None and max_weight >= 0.45:
            guidance.append('把单因子权重上限继续压到 0.40 以下，避免 c2c 优势被单因子集中度放大。')
    elif line_name == 'line_a_liquid_sector_balance':
        guidance.append('围绕 liquidity bucket 再细分低流动性行业桶，优先保留 liquid_leadership / sector_rotation 的共同交集。')
        if max_weight is not None and max_weight >= 0.40:
            guidance.append('提高 diversification_support 种子预算，让 line_a 不再回缩到少数大权重因子。')
    else:
        guidance.append('沿 dominant factor-family bucket 继续缩小 liquidity_followthrough 相关行业桶，控制 family 偏置。')
        if max_weight is not None and max_weight >= 0.40:
            guidance.append('继续收紧 factor-family bucket 内的最大权重，优先压缩单一家族贡献。')
    if timeout_count > 0:
        guidance.append(f'本轮出现 {timeout_count} 个 shard timeout；下一轮先拆这些 shard，而不是整线重跑。')
    return guidance


def build_line_comparison(run_dir: Path, line_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for payload in line_payloads:
        line_name = str(payload.get('line_name') or 'unknown')
        line_dir = Path(str(payload.get('output_root') or ''))
        validation_summary = payload.get('validation_summary') or read_json(line_dir / 'validation_summary.json')
        portfolio_spec = payload.get('portfolio_spec') or read_json(line_dir / 'portfolio_spec.json')
        concentration = line_concentration_stats(portfolio_spec)
        risk = overfit_risk_label(validation_summary, concentration)
        shadow_candidate = bool(
            payload.get('status') == 'ok'
            and float(validation_summary.get('walkforward_close_to_close_excess_total_return') or 0.0) > 0
            and float(validation_summary.get('analog_close_to_close_total_return') or 0.0) > 0
            and risk != 'high'
        )
        shard_stats = payload.get('shard_stats') or {}
        rows.append({
            'line_name': line_name,
            'status': payload.get('status'),
            'walkforward_close_to_close_excess_total_return': validation_summary.get('walkforward_close_to_close_excess_total_return'),
            'walkforward_open_to_close_excess_total_return': validation_summary.get('walkforward_open_to_close_excess_total_return'),
            'analog_close_to_close_total_return': validation_summary.get('analog_close_to_close_total_return'),
            'analog_open_to_close_total_return': validation_summary.get('analog_open_to_close_total_return'),
            'concentration': concentration,
            'overfit_risk': risk,
            'shadow_candidate': shadow_candidate,
            'freeze': not shadow_candidate,
            'shard_stats': shard_stats,
            'next_round_guidance': next_round_guidance(line_name, shard_stats, concentration),
        })
    rows.sort(
        key=lambda item: (
            -(float(item.get('analog_close_to_close_total_return') or 0.0)),
            -(float(item.get('walkforward_close_to_close_excess_total_return') or 0.0)),
            float((item.get('concentration') or {}).get('max_weight') or 999.0),
            str(item.get('overfit_risk') or 'z'),
        )
    )
    return {
        'generated_at': iso_now(),
        'run_name': run_dir.name,
        'comparison_priority': ['analog_close_to_close_total_return', 'walkforward_close_to_close_excess_total_return', 'concentration.max_weight', 'overfit_risk'],
        'lines': rows,
        'shadow_candidates': [item['line_name'] for item in rows if item['shadow_candidate']],
        'frozen_lines': [item['line_name'] for item in rows if item['freeze']],
    }


def build_final_report(run_dir: Path, line_comparison: dict[str, Any]) -> tuple[dict[str, Any], str]:
    report_json = {
        'generated_at': iso_now(),
        'run_name': run_dir.name,
        'shadow_candidates': line_comparison.get('shadow_candidates') or [],
        'frozen_lines': line_comparison.get('frozen_lines') or [],
        'lines': line_comparison.get('lines') or [],
    }
    lines = [
        '# final_scoring_recovery_report',
        '',
        f"- Run: `{run_dir.name}`",
        f"- 生成时间: `{report_json['generated_at']}`",
        f"- shadow_candidate: `{', '.join(report_json['shadow_candidates']) if report_json['shadow_candidates'] else '无'}`",
        f"- 冻结: `{', '.join(report_json['frozen_lines']) if report_json['frozen_lines'] else '无'}`",
        '',
        '## 分线结论',
    ]
    for item in report_json['lines']:
        concentration = item.get('concentration') or {}
        lines.extend([
            f"### {item['line_name']}",
            f"- 状态: `{item.get('status')}`",
            f"- walkforward c2c excess: `{item.get('walkforward_close_to_close_excess_total_return')}`",
            f"- analog c2c total_return: `{item.get('analog_close_to_close_total_return')}`",
            f"- 集中度 max_weight: `{concentration.get('max_weight')}` / 有效因子数: `{concentration.get('effective_factor_count')}`",
            f"- overfit 风险: `{item.get('overfit_risk')}`",
            f"- shadow_candidate: `{item.get('shadow_candidate')}`",
            '- 下一轮收缩建议:',
        ])
        for guidance in item.get('next_round_guidance') or []:
            lines.append(f'  - {guidance}')
        lines.append('')
    return report_json, '\n'.join(lines).strip() + '\n'


def sync_qlib(run_dir: Path, progress_output: Path, progress_log: Path, run_name: str) -> dict[str, Any]:
    recorder_python = str(QLIB_PYTHON if QLIB_PYTHON.exists() else Path(sys.executable))
    summary_output = run_dir / 'qlib_recorder_sync.json'
    cmd = [
        recorder_python,
        str(QLIB_RECORDER_SCRIPT),
        '--run-dir',
        str(run_dir),
        '--progress-output',
        str(progress_output),
        '--progress-log',
        str(progress_log),
        '--round-name',
        run_name,
        '--summary-output',
        str(summary_output),
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    result = {
        'command': cmd,
        'returncode': proc.returncode,
        'stdout_tail': (proc.stdout or '').splitlines()[-120:],
        'stderr_tail': (proc.stderr or '').splitlines()[-120:],
    }
    write_json(run_dir / 'qlib_recorder_step.json', result)
    if summary_output.exists():
        return read_json(summary_output)
    return result


def orchestrate(args: argparse.Namespace) -> int:
    parent_run_dir = Path(args.parent_run_dir).expanduser().resolve() if args.parent_run_dir else (PARALLEL_ROOT / args.parent_run_name)
    parent_manifest = read_json(parent_run_dir / 'manifest.json')
    if not parent_manifest:
        raise SystemExit(f'missing parent manifest: {parent_run_dir}')
    config_paths = [Path(item).expanduser().resolve() for item in (args.line_config or parent_manifest.get('config_paths') or [])]
    if not config_paths:
        raise SystemExit('no config paths available for recovery run')
    config_paths.sort(key=lambda path: LINE_PLANS.get(line_name_from_config(path), {}).get('priority', 99))
    parent_validation = validate_parent(parent_run_dir, config_paths)

    run_dir = (PARALLEL_ROOT / args.run_name).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    progress_output = Path(args.progress_output).expanduser().resolve() if args.progress_output else (run_dir / 'progress.json')
    progress_log = Path(args.progress_log).expanduser().resolve() if args.progress_log else (run_dir / 'progress.log')
    manifest = {
        'run_name': args.run_name,
        'run_dir': str(run_dir),
        'started_at': iso_now(),
        'mode': 'final_scoring_recovery',
        'parent_run_name': args.parent_run_name,
        'parent_run_dir': str(parent_run_dir),
        'parent_timeout_closure_status': (parent_validation['manifest'].get('timeout_closure') or {}).get('status'),
        'config_paths': [str(path) for path in config_paths],
        'shard_timeout_seconds': args.shard_timeout_seconds,
        'max_line_workers': args.max_line_workers,
        'line_execution_order': [line_name_from_config(path) for path in config_paths],
        'line_plans': {
            line_name_from_config(path): LINE_PLANS.get(line_name_from_config(path), {})
            for path in config_paths
        },
        'validated_parent_lines': parent_validation['lines'],
        'progress_output': str(progress_output),
        'progress_log': str(progress_log),
        'recorder_name': args.run_name,
    }
    write_json(run_dir / 'manifest.json', manifest)

    workers: dict[str, dict[str, Any]] = {}
    queue = list(config_paths)
    line_names = [line_name_from_config(path) for path in config_paths]
    for line_name in line_names:
        line_dir = run_dir / line_name
        line_dir.mkdir(parents=True, exist_ok=True)
        fallback = line_fallback_payload(line_name, next(path for path in config_paths if line_name_from_config(path) == line_name), line_dir)
        write_json(line_dir / 'progress.json', fallback)

    while queue or workers:
        while queue and len(workers) < max(1, args.max_line_workers):
            config_path = queue.pop(0)
            line_name = line_name_from_config(config_path)
            paths = line_paths(run_dir, line_name)
            stdout_handle = paths['stdout_log'].open('a', encoding='utf-8')
            stderr_handle = paths['stderr_log'].open('a', encoding='utf-8')
            cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                '--mode',
                'line',
                '--config',
                str(config_path),
                '--run-dir',
                str(run_dir),
                '--line-name',
                line_name,
                '--parent-run-dir',
                str(parent_run_dir),
                '--shard-timeout-seconds',
                str(args.shard_timeout_seconds),
            ]
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=stdout_handle, stderr=stderr_handle)
            workers[line_name] = {
                'config_path': config_path,
                'proc': proc,
                'stdout_handle': stdout_handle,
                'stderr_handle': stderr_handle,
                'started_at': iso_now(),
            }

        line_progress_payloads: list[dict[str, Any]] = []
        for config_path in config_paths:
            line_name = line_name_from_config(config_path)
            payload = read_line_progress(run_dir / line_name / 'progress.json', line_fallback_payload(line_name, config_path, run_dir / line_name))
            line_progress_payloads.append(payload)
        emit_root_progress(run_dir, progress_output, progress_log, args.run_name, line_progress_payloads, stage='running')

        completed_line_names: list[str] = []
        for line_name, info in workers.items():
            if info['proc'].poll() is None:
                continue
            completed_line_names.append(line_name)
        for line_name in completed_line_names:
            info = workers.pop(line_name)
            info['stdout_handle'].close()
            info['stderr_handle'].close()
        if queue or workers:
            time.sleep(max(1, args.poll_seconds))

    line_progress_payloads = [
        read_line_progress(run_dir / line_name_from_config(config_path) / 'progress.json', line_fallback_payload(line_name_from_config(config_path), config_path, run_dir / line_name_from_config(config_path)))
        for config_path in config_paths
    ]
    line_comparison = build_line_comparison(run_dir, line_progress_payloads)
    write_json(run_dir / 'line_comparison.json', line_comparison)
    report_json, report_md = build_final_report(run_dir, line_comparison)
    write_json(run_dir / 'final_scoring_recovery_report.json', report_json)
    (run_dir / 'final_scoring_recovery_report.md').write_text(report_md, encoding='utf-8')

    qlib_result = sync_qlib(run_dir, progress_output, progress_log, args.run_name)
    final_payloads = [
        read_line_progress(run_dir / line_name_from_config(config_path) / 'progress.json', line_fallback_payload(line_name_from_config(config_path), config_path, run_dir / line_name_from_config(config_path)))
        for config_path in config_paths
    ]
    emit_root_progress(run_dir, progress_output, progress_log, args.run_name, final_payloads, stage='completed')
    latest = read_json(progress_output)
    latest['line_comparison'] = line_comparison
    latest['final_scoring_recovery_report'] = report_json
    latest['qlib_recorder_sync'] = qlib_result
    write_json(progress_output, latest)
    write_json(run_dir / 'live_monitor_snapshot.json', latest)
    print(json.dumps({
        'status': latest.get('status'),
        'run_name': args.run_name,
        'run_dir': str(run_dir),
        'shadow_candidates': report_json['shadow_candidates'],
        'frozen_lines': report_json['frozen_lines'],
    }, ensure_ascii=False))
    return 0 if latest.get('status') == 'ok' else 1


def run_line(args: argparse.Namespace) -> int:
    if not args.config or not args.run_dir:
        raise SystemExit('line mode requires --config and --run-dir')
    config_path = Path(args.config).expanduser().resolve()
    run_dir = Path(args.run_dir).expanduser().resolve()
    parent_run_dir = Path(args.parent_run_dir).expanduser().resolve() if args.parent_run_dir else (PARALLEL_ROOT / PARENT_RUN_NAME)
    cfg = load_config(config_path)
    line_name = args.line_name or str(cfg.scenario_meta.get('line_id') or config_path.stem)
    plan = LINE_PLANS[line_name]
    paths = line_paths(run_dir, line_name)

    parent_payload = read_json(parent_run_dir / f'{line_name}.json')
    if not parent_payload:
        raise SystemExit(f'missing parent line payload for {line_name}')
    if parent_payload.get('last_completed_stage') != 'analog_validation':
        raise SystemExit(f'parent line {line_name} is not recoverable from analog_validation')

    walkforward_report = read_json(cfg.output_root / 'walkforward_report.json')
    analog_report = read_json(cfg.output_root / 'analog_validation.json')
    hypotheses = read_json(cfg.output_root / 'market_state_hypotheses.json')
    if not walkforward_report or not analog_report or not hypotheses:
        raise SystemExit(f'missing preserved artifacts under {cfg.output_root}')

    progress: dict[str, Any] = {
        'schema_version': LINE_SCHEMA,
        'generated_at': iso_now(),
        'line_name': line_name,
        'config_path': str(config_path),
        'run_name': run_dir.name,
        'run_dir': str(run_dir),
        'line_dir': str(paths['line_dir']),
        'output_root': str(paths['line_dir']),
        'source_output_root': str(cfg.output_root),
        'status': 'running',
        'stage': 'precompute_loading',
        'started_at': iso_now(),
        'finished_at': None,
        'priority': plan['priority'],
        'shard_count': plan['shard_count'],
        'shard_timeout_seconds': args.shard_timeout_seconds,
        'shard_key': plan['shard_key'],
        'bucket_hint': plan['bucket_hint'],
        'parent_last_completed_stage': parent_payload.get('last_completed_stage'),
        'walkforward_report': str(cfg.output_root / 'walkforward_report.json'),
        'analog_validation_report': str(cfg.output_root / 'analog_validation.json'),
        'portfolio_spec_path': str(paths['portfolio_spec']),
        'current_selection_snapshot_path': str(paths['current_selection_snapshot']),
        'validation_summary_path': str(paths['validation_summary']),
        'shard_stats': {'pending': plan['shard_count'], 'ok': 0, 'error': 0, 'timeout': 0, 'empty': 0},
        'shards': [],
        'errors': [],
        'last_new_shard_file_at': None,
    }
    write_json(paths['progress'], progress)

    try:
        panel = read_parquet_with_retries(cfg.panel_path)
        factor_meta = cfg.factor_registry['backtestable_factors']
        factor_cols = [str(item['factor_id']) for item in factor_meta]
        panel['trade_date'] = panel['trade_date'].astype(str)
        family_budget = hypotheses['family_budgets']
        current_state = current_state_from_panel(panel[['trade_date', 'regime_id', 'regime_confidence', 'style_regime_id', 'sector_regime_id']].drop_duplicates())
        current_regime_id = hypotheses.get('current_market_state', {}).get('primary_regime') or current_state.get('regime_id') or 'shrink_uptrend'
        run_context = {
            'config_path': str(config_path),
            'scenario_meta': cfg.scenario_meta,
            'scoring_controls': cfg.scoring_controls,
            'selection_controls': cfg.selection_controls,
            'recovery_run_name': run_dir.name,
            'parent_run_name': parent_run_dir.name,
        }
        trade_dates = sorted(panel['trade_date'].unique().tolist())
        latest_date = trade_dates[-1]
        full_train = panel.loc[panel['trade_date'] < latest_date].copy()
        latest_rows = panel.loc[panel['trade_date'] == latest_date].copy()
        missing_latest_z = [f'{fid}__z' for fid in factor_cols if f'{fid}__z' not in latest_rows.columns]
        if missing_latest_z:
            latest_rows = add_cross_sectional_zscores(latest_rows, factor_cols)
        progress['latest_date'] = latest_date
        progress['latest_row_count'] = int(len(latest_rows))
        progress['train_row_count'] = int(len(full_train))
        progress['stage'] = 'precompute_factor_scores'
        progress['generated_at'] = iso_now()
        write_json(paths['progress'], progress)

        score_started = time.time()
        factor_scores = train_factor_scores(full_train, factor_meta, cfg.top_n, cfg.round_trip_cost, current_regime_id, cfg)
        factor_score_seconds = round(time.time() - score_started, 2)
        weights, selected_factors = select_weights(full_train, factor_scores, family_budget, cfg)

        selection_cols = ['ts_code', 'name', 'industry', 'market', 'composite_score'] + factor_cols
        score_columns = selected_score_columns(selection_cols, factor_meta, weights)
        kept_columns = [
            'trade_date', 'ts_code', 'name', 'industry', 'market', 'avg_amount_lookback', 'sector_regime_id', 'style_regime_id',
            *factor_cols,
            *[f'{factor_id}__z' for factor_id in factor_cols if f'{factor_id}__z' in latest_rows.columns],
        ]
        kept_columns = [col for col in kept_columns if col in latest_rows.columns]
        latest_rows_source = latest_rows[kept_columns].copy()
        latest_rows_source, shard_plan = assign_shards(latest_rows_source, line_name, weights, factor_meta, int(plan['shard_count']))
        write_parquet_atomic(latest_rows_source, paths['latest_rows_source'], index=False)
        write_json(paths['shard_plan'], shard_plan)
        write_json(paths['factor_scores'], {
            'generated_at': iso_now(),
            'line_name': line_name,
            'factor_scores': factor_scores,
            'factor_score_seconds': factor_score_seconds,
        })
        write_json(paths['weights'], {
            'generated_at': iso_now(),
            'line_name': line_name,
            'weights': weights,
            'selected_factors': selected_factors,
        })
        precomputed_state = {
            'generated_at': iso_now(),
            'line_name': line_name,
            'config_path': str(config_path),
            'latest_date': latest_date,
            'weights': weights,
            'selected_factors': selected_factors,
            'factor_meta': factor_meta,
            'factor_cols': factor_cols,
            'selection_cols': selection_cols,
            'score_columns': score_columns,
            'current_regime_id': current_regime_id,
            'family_budget': family_budget,
            'run_context': run_context,
            'source_output_root': str(cfg.output_root),
        }
        write_json(paths['precomputed_state'], precomputed_state)

        progress['stage'] = 'shards_running'
        progress['generated_at'] = iso_now()
        progress['factor_score_seconds'] = factor_score_seconds
        progress['selected_factor_count'] = int(len(selected_factors))
        progress['max_weight'] = max((abs(float(v)) for v in weights.values()), default=0.0)
        progress['shards'] = shard_plan['shards']
        write_json(paths['progress'], progress)

        shard_counts = Counter()
        for shard in progress['shards']:
            shard_id = int(shard['shard_id'])
            shard['started_at'] = iso_now()
            progress['generated_at'] = iso_now()
            progress['stage'] = f'shard_{shard_id:02d}'
            write_json(paths['progress'], progress)

            if int(shard['row_count']) == 0:
                result = write_empty_shard_artifacts(paths['line_dir'], shard_id, score_columns, 'empty', line_name)
                shard['status'] = 'empty'
                shard['finished_at'] = iso_now()
                shard['last_file_at'] = iso_now()
                shard.update(result)
                shard_counts['empty'] += 1
                progress['last_new_shard_file_at'] = shard['last_file_at']
                progress['generated_at'] = iso_now()
                progress['shard_stats'] = {
                    'pending': max(0, plan['shard_count'] - sum(shard_counts.values())),
                    'ok': shard_counts['ok'],
                    'error': shard_counts['error'],
                    'timeout': shard_counts['timeout'],
                    'empty': shard_counts['empty'],
                }
                write_json(paths['progress'], progress)
                continue

            cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                '--mode',
                'shard',
                '--line-dir',
                str(paths['line_dir']),
                '--shard-id',
                str(shard_id),
            ]
            status = 'ok'
            worker_meta: dict[str, Any] = {'command': cmd}
            try:
                proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=args.shard_timeout_seconds)
                worker_meta['returncode'] = proc.returncode
                worker_meta['stdout_tail'] = (proc.stdout or '').splitlines()[-40:]
                worker_meta['stderr_tail'] = (proc.stderr or '').splitlines()[-40:]
                if proc.returncode != 0:
                    status = 'error'
                    write_empty_shard_artifacts(paths['line_dir'], shard_id, score_columns, 'error', line_name, error_message='shard worker failed')
                score_payload = read_json(shard_paths(paths['line_dir'], shard_id)['score_json'])
                if score_payload:
                    status = str(score_payload.get('status') or status)
            except subprocess.TimeoutExpired as exc:
                status = 'timeout'
                worker_meta['returncode'] = None
                worker_meta['stdout_tail'] = ((exc.stdout or '') if isinstance(exc.stdout, str) else '').splitlines()[-40:]
                worker_meta['stderr_tail'] = ((exc.stderr or '') if isinstance(exc.stderr, str) else '').splitlines()[-40:]
                write_empty_shard_artifacts(paths['line_dir'], shard_id, score_columns, 'timeout', line_name, error_message='no shard files landed within timeout window')
            except Exception as exc:  # noqa: BLE001
                status = 'error'
                worker_meta['returncode'] = None
                worker_meta['stdout_tail'] = []
                worker_meta['stderr_tail'] = [f'{type(exc).__name__}: {exc}']
                write_empty_shard_artifacts(paths['line_dir'], shard_id, score_columns, 'error', line_name, error_message=str(exc))

            score_payload = read_json(shard_paths(paths['line_dir'], shard_id)['score_json'])
            rank_payload = read_json(shard_paths(paths['line_dir'], shard_id)['rank_json'])
            score_parquet_path = shard_paths(paths['line_dir'], shard_id)['score_parquet']
            last_file_at = iso_now()
            if score_parquet_path.exists():
                last_file_at = datetime.fromtimestamp(score_parquet_path.stat().st_mtime, TZ).isoformat(timespec='seconds')
            shard['status'] = status
            shard['finished_at'] = iso_now()
            shard['last_file_at'] = last_file_at
            shard['score_parquet'] = str(score_parquet_path)
            shard['score_json'] = str(shard_paths(paths['line_dir'], shard_id)['score_json'])
            shard['rank_json'] = str(shard_paths(paths['line_dir'], shard_id)['rank_json'])
            shard['worker'] = worker_meta
            shard['ranked_row_count'] = int(rank_payload.get('ranked_row_count') or score_payload.get('row_count') or 0)
            progress['last_new_shard_file_at'] = last_file_at
            shard_counts[status] += 1
            progress['generated_at'] = iso_now()
            progress['shard_stats'] = {
                'pending': max(0, plan['shard_count'] - sum(shard_counts.values())),
                'ok': shard_counts['ok'],
                'error': shard_counts['error'],
                'timeout': shard_counts['timeout'],
                'empty': shard_counts['empty'],
            }
            write_json(paths['progress'], progress)

        score_frames: list[pd.DataFrame] = []
        for shard in progress['shards']:
            if shard.get('status') in {'error', 'timeout'}:
                continue
            parquet_path = Path(str(shard.get('score_parquet') or ''))
            if not parquet_path.exists():
                continue
            shard_df = pd.read_parquet(parquet_path)
            if shard_df.empty:
                continue
            score_frames.append(shard_df)

        merged = pd.concat(score_frames, ignore_index=True) if score_frames else pd.DataFrame(columns=score_columns)
        if not merged.empty:
            merged = merged.sort_values('composite_score', ascending=False).reset_index(drop=True)
        current_selection = merged[[col for col in selection_cols if col in merged.columns]].head(cfg.top_n).to_dict(orient='records')
        recovery_meta = {
            'run_name': run_dir.name,
            'parent_run_name': parent_run_dir.name,
            'line_name': line_name,
            'shard_key': plan['shard_key'],
            'shard_count': plan['shard_count'],
            'shard_stats': progress['shard_stats'],
            'source_output_root': str(cfg.output_root),
            'recovery_output_root': str(paths['line_dir']),
            'completed_at': iso_now(),
        }
        portfolio_spec = build_portfolio_spec(latest_date, current_regime_id, run_context, family_budget, weights, selected_factors, recovery_meta)
        selection_snapshot = build_current_selection_snapshot(latest_date, run_context, weights, selected_factors, current_selection, recovery_meta)
        validation_summary = build_validation_summary(walkforward_report, analog_report, run_context, recovery_meta)
        dump_json(paths['portfolio_spec'], portfolio_spec)
        dump_json(paths['current_selection_snapshot'], selection_snapshot)
        dump_json(paths['validation_summary'], validation_summary)
        mirror_terminal_artifacts(cfg.output_root, portfolio_spec, selection_snapshot, validation_summary)

        progress['validation_summary'] = validation_summary
        progress['portfolio_spec'] = portfolio_spec
        progress['current_selection_snapshot'] = selection_snapshot
        progress['status'] = 'error' if progress['shard_stats']['timeout'] > 0 or progress['shard_stats']['error'] > 0 else 'ok'
        progress['stage'] = 'completed'
        progress['generated_at'] = iso_now()
        progress['finished_at'] = iso_now()
        write_json(paths['progress'], progress)
        write_json(paths['line_result'], build_line_result_payload(progress))
        print(json.dumps({
            'line_name': line_name,
            'status': progress['status'],
            'latest_date': latest_date,
            'shard_stats': progress['shard_stats'],
            'portfolio_spec': str(paths['portfolio_spec']),
        }, ensure_ascii=False))
        return 0 if progress['status'] == 'ok' else 1
    except Exception as exc:  # noqa: BLE001
        progress['status'] = 'error'
        progress['stage'] = 'failed'
        progress['generated_at'] = iso_now()
        progress['finished_at'] = iso_now()
        progress['errors'].append({'type': type(exc).__name__, 'message': str(exc)})
        write_json(paths['progress'], progress)
        write_json(paths['line_result'], build_line_result_payload(progress))
        print(json.dumps({'line_name': line_name, 'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        return 1


def run_shard(args: argparse.Namespace) -> int:
    if not args.line_dir or args.shard_id is None:
        raise SystemExit('shard mode requires --line-dir and --shard-id')
    line_dir = Path(args.line_dir).expanduser().resolve()
    shard_id = int(args.shard_id)
    state = read_json(line_dir / 'precomputed_state.json')
    if not state:
        raise SystemExit(f'missing precomputed_state.json under {line_dir}')
    latest_source = line_dir / 'latest_rows_source.parquet'
    if not latest_source.exists():
        raise SystemExit(f'missing latest_rows_source.parquet under {line_dir}')
    df = pd.read_parquet(latest_source)
    subset = df.loc[df['shard_id'] == shard_id].copy()
    line_name = str(state.get('line_name') or line_dir.name)
    score_columns = [str(col) for col in (state.get('score_columns') or [])]
    weights = {str(k): float(v) for k, v in (state.get('weights') or {}).items()}
    selection_cols = [str(col) for col in (state.get('selection_cols') or [])]
    paths = shard_paths(line_dir, shard_id)

    if subset.empty:
        write_empty_shard_artifacts(line_dir, shard_id, score_columns, 'empty', line_name)
        print(json.dumps({'line_name': line_name, 'shard_id': shard_id, 'status': 'empty'}, ensure_ascii=False))
        return 0

    scored = combine_factor_scores(subset, weights, output_col='composite_score')
    scored = scored.sort_values('composite_score', ascending=False).reset_index(drop=True)
    available_score_columns = [col for col in score_columns if col in scored.columns]
    score_df = scored[available_score_columns].copy()
    write_parquet_atomic(score_df, paths['score_parquet'], index=False)

    top_candidates = scored[[col for col in selection_cols if col in scored.columns]].head(20).to_dict(orient='records')
    score_payload = {
        'schema_version': SHARD_SCHEMA,
        'generated_at': iso_now(),
        'line_name': line_name,
        'shard_id': shard_id,
        'status': 'ok',
        'row_count': int(len(score_df)),
        'score_parquet': str(paths['score_parquet']),
        'top_candidates': top_candidates[:10],
        'max_composite_score': float(pd.to_numeric(score_df['composite_score'], errors='coerce').max()) if 'composite_score' in score_df.columns and not score_df.empty else None,
        'min_composite_score': float(pd.to_numeric(score_df['composite_score'], errors='coerce').min()) if 'composite_score' in score_df.columns and not score_df.empty else None,
    }
    rank_payload = {
        'schema_version': SHARD_SCHEMA,
        'generated_at': iso_now(),
        'line_name': line_name,
        'shard_id': shard_id,
        'status': 'ok',
        'ranked_row_count': int(len(score_df)),
        'top_candidates': top_candidates,
        'score_parquet': str(paths['score_parquet']),
    }
    write_json(paths['score_json'], score_payload)
    write_json(paths['rank_json'], rank_payload)
    print(json.dumps({'line_name': line_name, 'shard_id': shard_id, 'status': 'ok', 'row_count': len(score_df)}, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    if args.mode == 'orchestrate':
        return orchestrate(args)
    if args.mode == 'line':
        return run_line(args)
    return run_shard(args)


if __name__ == '__main__':
    raise SystemExit(main())
