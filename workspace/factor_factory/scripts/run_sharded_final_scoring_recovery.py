#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
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
    load_json,
    read_parquet_with_retries,
    write_parquet_atomic,
)
from run_greenfield_multifactor_backtest import select_weights, train_factor_scores

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
PARALLEL_ROOT = ROOT / 'outputs' / 'evolution' / 'parallel_lines'
PARENT_RUN_NAME = '20260422_three_line_longrun'
DEFAULT_RUN_NAME = '20260422_sharded_final_scoring_recovery'
PARENT_RUN_DIR = PARALLEL_ROOT / PARENT_RUN_NAME
PARENT_TIMEOUT_REPORT = PARENT_RUN_DIR / 'timeout_closure_report.json'
PARENT_RECORDER_ID = '0fcb5b0e8655448d9e2650360bdae058'
TZ = ZoneInfo('Asia/Shanghai')

LINE_SPECS = [
    {
        'line_name': 'line_b_downside_rotation_c2c',
        'config_path': str(ROOT / 'config' / 'greenfield_parallel_lines' / 'line_b_downside_rotation_c2c.yaml'),
        'shard_count': 6,
        'bucket_mode': 'downside_x_sector',
        'bucket_description': 'downside-control bucket x sector bucket',
        'priority': 1,
    },
    {
        'line_name': 'line_a_liquid_sector_balance',
        'config_path': str(ROOT / 'config' / 'greenfield_parallel_lines' / 'line_a_liquid_sector_balance.yaml'),
        'shard_count': 4,
        'bucket_mode': 'liquidity_x_sector_packed',
        'bucket_description': 'liquidity tercile x sector bucket packed into 4 shards',
        'priority': 2,
    },
    {
        'line_name': 'line_c_diversified_followthrough',
        'config_path': str(ROOT / 'config' / 'greenfield_parallel_lines' / 'line_c_diversified_followthrough.yaml'),
        'shard_count': 6,
        'bucket_mode': 'factor_family_x_sector',
        'bucket_description': 'factor-family bucket x sector bucket',
        'priority': 3,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Recover missing terminal artifacts via sharded final scoring only')
    parser.add_argument('--run-name', default=DEFAULT_RUN_NAME)
    parser.add_argument('--parent-run-dir', type=Path, default=PARENT_RUN_DIR)
    parser.add_argument('--parent-timeout-report', type=Path, default=PARENT_TIMEOUT_REPORT)
    parser.add_argument('--shard-timeout-seconds', type=int, default=600)
    parser.add_argument('--heartbeat-seconds', type=int, default=60)
    parser.add_argument('--line-workers', type=int, default=3)
    parser.add_argument('--progress-output', type=Path, default=PARALLEL_ROOT / 'sharded_final_scoring_recovery_latest.json')
    parser.add_argument('--progress-log', type=Path, default=PARALLEL_ROOT / 'sharded_final_scoring_recovery_latest.log')
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + '\n')


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(out) else out


def sign_divergence(a: float, b: float) -> bool:
    if abs(a) <= 1e-12 or abs(b) <= 1e-12:
        return False
    return (a > 0) != (b > 0)


def max_abs_weight(weights: dict[str, float]) -> float:
    return max((abs(safe_float(v)) for v in weights.values()), default=0.0)


def weight_hhi(weights: dict[str, float]) -> float:
    return float(sum(abs(safe_float(v)) ** 2 for v in weights.values()))


def unique_factor_families(selected_factors: list[dict[str, Any]]) -> list[str]:
    families: list[str] = []
    for item in selected_factors:
        family = str(item.get('family') or '').strip()
        if family and family not in families:
            families.append(family)
    return families


def load_parent_timeout_report(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    if payload.get('decision') != 'timeout_closure':
        raise ValueError(f'parent timeout closure report is not timeout_closure: {path}')
    per_line = payload.get('per_line') or []
    if not per_line:
        raise ValueError(f'parent timeout closure report missing per_line entries: {path}')
    invalid = [item.get('line_name') for item in per_line if item.get('last_completed_stage') != 'analog_validation']
    if invalid:
        raise ValueError(f'parent timeout closure report has non-analog terminal stage for: {invalid}')
    return payload


def build_pending_payload(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    cfg = load_config(spec['config_path'])
    return {
        'line_name': spec['line_name'],
        'config_path': spec['config_path'],
        'output_root': str(cfg.output_root),
        'run_dir': str(run_dir),
        'status': 'pending',
        'started_at': None,
        'finished_at': None,
        'scenario_meta': cfg.scenario_meta,
        'shard_count': int(spec['shard_count']),
        'bucket_mode': spec['bucket_mode'],
        'bucket_description': spec['bucket_description'],
        'priority': spec['priority'],
    }


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
        'schema_version': '2026-04-22.sharded_final_scoring_recovery.v1',
        'generated_at': generated_at,
        'status': status,
        'stage': stage,
        'run_name': run_name,
        'run_dir': str(run_dir),
        'heartbeat_seconds': heartbeat_seconds,
        'expected_next_update_before': heartbeat_deadline,
        'parent_run_name': PARENT_RUN_NAME,
        'line_count': len(line_payloads),
        'completed_lines': [item.get('line_name') for item in line_payloads if item.get('status') in {'ok', 'error'}],
        'ok_lines': [item.get('line_name') for item in line_payloads if item.get('status') == 'ok'],
        'error_lines': [item.get('line_name') for item in line_payloads if item.get('status') == 'error'],
        'lines': line_payloads,
    }
    dump_json(progress_output, summary)
    append_log(progress_log, {
        'generated_at': generated_at,
        'status': status,
        'stage': stage,
        'completed_lines': summary['completed_lines'],
        'ok_lines': summary['ok_lines'],
        'error_lines': summary['error_lines'],
    })


def line_live_payload(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    progress_path = run_dir / spec['line_name'] / 'progress.json'
    if progress_path.exists():
        try:
            payload = load_json(progress_path)
            return {
                'line_name': spec['line_name'],
                'config_path': spec['config_path'],
                'output_root': payload.get('output_root'),
                'run_dir': str(run_dir),
                'status': payload.get('status', 'running'),
                'started_at': payload.get('started_at'),
                'finished_at': payload.get('finished_at'),
                'stage': payload.get('stage'),
                'scenario_meta': payload.get('scenario_meta') or {},
                'shard_count': payload.get('shard_count', spec['shard_count']),
                'bucket_mode': spec['bucket_mode'],
                'bucket_description': spec['bucket_description'],
                'priority': spec['priority'],
                'successful_shards': payload.get('successful_shards', 0),
                'timed_out_shards': payload.get('timed_out_shards', []),
                'line_dir': str(progress_path.parent),
            }
        except Exception:
            pass
    return build_pending_payload(spec, run_dir)


def rank_quantile_buckets(series: pd.Series, bucket_count: int) -> pd.Series:
    filled = pd.to_numeric(series, errors='coerce')
    if filled.dropna().empty:
        return pd.Series(np.zeros(len(series), dtype=int), index=series.index)
    filled = filled.fillna(float(filled.median()))
    ranked = filled.rank(method='first')
    try:
        buckets = pd.qcut(ranked, q=bucket_count, labels=False, duplicates='drop')
    except ValueError:
        buckets = None
    if buckets is None:
        order = np.arange(len(series), dtype=int)
        return pd.Series(order % max(1, bucket_count), index=series.index, dtype=int)
    bucket_series = pd.Series(buckets, index=series.index).fillna(0).astype(int)
    unique = sorted(bucket_series.unique().tolist())
    remap = {old: idx for idx, old in enumerate(unique)}
    return bucket_series.map(remap).astype(int)


def balanced_sector_buckets(df: pd.DataFrame, bucket_count: int = 2) -> tuple[pd.Series, dict[str, Any]]:
    industry_series = df.get('industry')
    if industry_series is None:
        industry_series = pd.Series(['未知'] * len(df), index=df.index)
    industry_series = industry_series.fillna('未知').astype(str)
    counts = industry_series.value_counts().sort_values(ascending=False)
    bucket_loads = [0 for _ in range(bucket_count)]
    mapping: dict[str, int] = {}
    for industry, count in counts.items():
        bucket_id = min(range(bucket_count), key=lambda idx: (bucket_loads[idx], idx))
        mapping[str(industry)] = int(bucket_id)
        bucket_loads[bucket_id] += int(count)
    return industry_series.map(mapping).astype(int), {
        'bucket_count': bucket_count,
        'industry_to_sector_bucket': mapping,
        'bucket_loads': bucket_loads,
    }


def pack_combinations_to_shards(primary_bucket: pd.Series, sector_bucket: pd.Series, shard_count: int) -> tuple[pd.Series, dict[str, Any]]:
    combos = pd.DataFrame({'primary_bucket': primary_bucket.astype(int), 'sector_bucket': sector_bucket.astype(int)})
    combo_counts = combos.value_counts().reset_index(name='row_count')
    loads = [0 for _ in range(shard_count)]
    combo_to_shard: dict[str, int] = {}
    for row in combo_counts.sort_values(['row_count', 'primary_bucket', 'sector_bucket'], ascending=[False, True, True]).itertuples(index=False):
        combo_key = f'{int(row.primary_bucket)}|{int(row.sector_bucket)}'
        shard_id = min(range(shard_count), key=lambda idx: (loads[idx], idx))
        combo_to_shard[combo_key] = int(shard_id)
        loads[shard_id] += int(row.row_count)
    shard_ids = [combo_to_shard[f'{int(p)}|{int(s)}'] for p, s in zip(primary_bucket.tolist(), sector_bucket.tolist())]
    return pd.Series(shard_ids, index=primary_bucket.index, dtype=int), {
        'combo_to_shard': combo_to_shard,
        'estimated_shard_loads': loads,
    }


def family_signal(df: pd.DataFrame, family_factor_ids: list[str], budget: float = 1.0) -> pd.Series:
    z_cols = [f'{factor_id}__z' for factor_id in family_factor_ids if f'{factor_id}__z' in df.columns]
    if not z_cols:
        raw_cols = [factor_id for factor_id in family_factor_ids if factor_id in df.columns]
        if not raw_cols:
            return pd.Series(np.zeros(len(df)), index=df.index, dtype=float)
        series = df[raw_cols].apply(pd.to_numeric, errors='coerce').fillna(0.0).abs().mean(axis=1)
        return series.astype(float) * float(budget)
    series = df[z_cols].apply(pd.to_numeric, errors='coerce').fillna(0.0).abs().mean(axis=1)
    return series.astype(float) * float(budget)


def bucket_latest_rows(
    line_name: str,
    current_scored: pd.DataFrame,
    factor_meta: list[dict[str, Any]],
    family_budget: dict[str, float],
    shard_count: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    bucketed = current_scored.copy()
    sector_bucket, sector_meta = balanced_sector_buckets(bucketed, bucket_count=2)
    bucketed['sector_bucket'] = sector_bucket.astype(int)

    family_map: dict[str, list[str]] = {}
    for item in factor_meta:
        family_map.setdefault(str(item['family']), []).append(str(item['factor_id']))

    if line_name == 'line_b_downside_rotation_c2c':
        signal = family_signal(bucketed, family_map.get('downside_control', []), budget=1.0)
        primary_bucket = rank_quantile_buckets(signal, bucket_count=3)
        bucketed['primary_bucket_label'] = primary_bucket.map(lambda x: f'downside_bucket_{int(x)}')
        bucketed['primary_bucket_id'] = primary_bucket.astype(int)
        bucketed['shard_id'] = (primary_bucket.astype(int) * 2 + bucketed['sector_bucket'].astype(int)).astype(int)
        return bucketed, {
            'primary_bucket_name': 'downside_control_bucket',
            'primary_bucket_strategy': 'mean abs z-score of downside_control family',
            'sector_bucket_strategy': sector_meta,
            'shard_mapping': 'primary_bucket_id * 2 + sector_bucket',
            'bucket_description': 'downside-control bucket x sector bucket',
        }

    if line_name == 'line_c_diversified_followthrough':
        budget_order = sorted(
            family_budget.items(),
            key=lambda item: (-safe_float(item[1]), str(item[0])),
        )
        top_families = [family for family, _ in budget_order[:3]]
        signal_frame = pd.DataFrame(index=bucketed.index)
        for family in top_families:
            signal_frame[family] = family_signal(bucketed, family_map.get(family, []), budget=safe_float(family_budget.get(family), 0.0))
        if signal_frame.empty:
            primary_bucket = pd.Series(np.zeros(len(bucketed), dtype=int), index=bucketed.index)
            family_labels = {0: 'factor_family_bucket_0'}
        else:
            primary_names = signal_frame.idxmax(axis=1)
            family_to_bucket = {family: idx for idx, family in enumerate(top_families)}
            primary_bucket = primary_names.map(family_to_bucket).astype(int)
            family_labels = {idx: family for family, idx in family_to_bucket.items()}
        bucketed['primary_bucket_id'] = primary_bucket.astype(int)
        bucketed['primary_bucket_label'] = primary_bucket.map(lambda x: str(family_labels.get(int(x), f'factor_family_bucket_{int(x)}')))
        bucketed['shard_id'] = (primary_bucket.astype(int) * 2 + bucketed['sector_bucket'].astype(int)).astype(int)
        return bucketed, {
            'primary_bucket_name': 'factor_family_bucket',
            'primary_bucket_strategy': 'dominant weighted family signal using top-3 family budgets',
            'factor_family_bucket_labels': family_labels,
            'top_family_budgets': {family: safe_float(family_budget.get(family), 0.0) for family in top_families},
            'sector_bucket_strategy': sector_meta,
            'shard_mapping': 'primary_bucket_id * 2 + sector_bucket',
            'bucket_description': 'factor-family bucket x sector bucket',
        }

    liquidity_source = bucketed['avg_amount_lookback'] if 'avg_amount_lookback' in bucketed.columns else bucketed.get('g_liquid_leadership__z', pd.Series(np.zeros(len(bucketed)), index=bucketed.index))
    primary_bucket = rank_quantile_buckets(pd.to_numeric(liquidity_source, errors='coerce'), bucket_count=3)
    bucketed['primary_bucket_id'] = primary_bucket.astype(int)
    bucketed['primary_bucket_label'] = primary_bucket.map(lambda x: f'liquidity_tercile_{int(x)}')
    shard_ids, pack_meta = pack_combinations_to_shards(primary_bucket.astype(int), bucketed['sector_bucket'].astype(int), shard_count=shard_count)
    bucketed['shard_id'] = shard_ids.astype(int)
    return bucketed, {
        'primary_bucket_name': 'liquidity_tercile',
        'primary_bucket_strategy': 'avg_amount_lookback quantile bucket',
        'sector_bucket_strategy': sector_meta,
        'pack_strategy': pack_meta,
        'bucket_description': 'liquidity tercile x sector bucket packed into 4 shards',
    }


def shard_rank_records(df: pd.DataFrame, factor_cols: list[str], limit: int = 50) -> list[dict[str, Any]]:
    cols = ['trade_date', 'ts_code', 'name', 'industry', 'market', 'composite_score', 'primary_bucket_label', 'primary_bucket_id', 'sector_bucket', 'shard_id']
    keep_cols = [col for col in cols + factor_cols if col in df.columns]
    ranked = df.sort_values('composite_score', ascending=False)[keep_cols].head(limit).copy()
    return ranked.to_dict(orient='records')


def write_line_progress(line_dir: Path, payload: dict[str, Any]) -> None:
    dump_json(line_dir / 'progress.json', payload)


def materialize_shard(
    line_dir: Path,
    line_name: str,
    shard_id: int,
    shard_df: pd.DataFrame,
    factor_cols: list[str],
    shard_meta: dict[str, Any],
) -> dict[str, Any]:
    started_at = iso_now()
    score_columns = [
        'trade_date',
        'ts_code',
        'name',
        'industry',
        'market',
        'composite_score',
        'primary_bucket_label',
        'primary_bucket_id',
        'sector_bucket',
        'shard_id',
    ]
    available_factor_cols = [col for col in factor_cols if col in shard_df.columns]
    available_z_cols = [f'{col}__z' for col in factor_cols if f'{col}__z' in shard_df.columns]
    keep_cols = [col for col in score_columns + available_factor_cols + available_z_cols if col in shard_df.columns]
    shard_export = shard_df[keep_cols].sort_values('composite_score', ascending=False).reset_index(drop=True)

    parquet_path = line_dir / f'scores_shard_{shard_id:02d}.parquet'
    summary_path = line_dir / f'scores_shard_{shard_id:02d}.json'
    rank_path = line_dir / f'rank_shard_{shard_id:02d}.json'

    write_parquet_atomic(shard_export, parquet_path, index=False)

    shard_summary = {
        'line_name': line_name,
        'shard_id': shard_id,
        'status': 'ok',
        'started_at': started_at,
        'finished_at': iso_now(),
        'row_count': int(len(shard_export)),
        'top_score': safe_float(shard_export['composite_score'].iloc[0], 0.0) if not shard_export.empty else 0.0,
        'top_ts_code': str(shard_export['ts_code'].iloc[0]) if not shard_export.empty else None,
        'top_industry': str(shard_export['industry'].iloc[0]) if not shard_export.empty else None,
        'bucket_meta': shard_meta,
        'paths': {
            'parquet': str(parquet_path),
            'summary': str(summary_path),
            'rank': str(rank_path),
        },
    }
    dump_json(summary_path, shard_summary)
    dump_json(rank_path, {
        'line_name': line_name,
        'shard_id': shard_id,
        'generated_at': iso_now(),
        'bucket_meta': shard_meta,
        'rank_count': min(50, int(len(shard_export))),
        'ranked_records': shard_rank_records(shard_df, factor_cols=factor_cols, limit=50),
    })
    return shard_summary


def build_validation_summary(
    line_name: str,
    walkforward_report: dict[str, Any],
    analog_report: dict[str, Any],
    weights: dict[str, float],
    selected_factors: list[dict[str, Any]],
    successful_shards: int,
    timed_out_shards: list[int],
) -> dict[str, Any]:
    walk_excess = (((walkforward_report.get('aggregate') or {}).get('excess')) or {})
    analog_agg = analog_report.get('aggregate') or {}
    walk_c2c = safe_float(walk_excess.get('close_to_close_total_return'))
    walk_o2c = safe_float(walk_excess.get('open_to_close_total_return'))
    analog_c2c = safe_float(((analog_agg.get('close_to_close') or {}).get('total_return')))
    analog_o2c = safe_float(((analog_agg.get('open_to_close') or {}).get('total_return')))
    current_count = int(len(weights))
    max_weight = max_abs_weight(weights)
    hhi = weight_hhi(weights)
    divergence_count = int(sign_divergence(walk_c2c, walk_o2c)) + int(sign_divergence(analog_c2c, analog_o2c))
    overfit_gap = abs(walk_c2c - analog_c2c)
    overfit_risk_score = float(divergence_count + hhi + min(overfit_gap, 2.0))
    if overfit_risk_score >= 2.2:
        overfit_risk_level = 'high'
    elif overfit_risk_score >= 1.2:
        overfit_risk_level = 'medium'
    else:
        overfit_risk_level = 'low'
    return {
        'mainline': 'greenfield_multifactor',
        'line_name': line_name,
        'legacy_prebreakout_role': 'reference_only',
        'walkforward_close_to_close_excess_total_return': walk_c2c,
        'walkforward_open_to_close_excess_total_return': walk_o2c,
        'analog_close_to_close_total_return': analog_c2c,
        'analog_open_to_close_total_return': analog_o2c,
        'current_selected_factor_count': current_count,
        'current_selected_factor_families': unique_factor_families(selected_factors),
        'current_max_single_factor_weight': max_weight,
        'current_weight_hhi': hhi,
        'walkforward_cross_leg_divergence': sign_divergence(walk_c2c, walk_o2c),
        'analog_cross_leg_divergence': sign_divergence(analog_c2c, analog_o2c),
        'overfit_gap_close_to_close': overfit_gap,
        'overfit_risk_score': overfit_risk_score,
        'overfit_risk_level': overfit_risk_level,
        'sharded_recovery': {
            'successful_shards': int(successful_shards),
            'timed_out_shards': [int(item) for item in timed_out_shards],
        },
    }


def top20_sector_summary(current_selection: list[dict[str, Any]]) -> dict[str, Any]:
    industries: dict[str, int] = {}
    sector_buckets: dict[str, int] = {}
    primary_buckets: dict[str, int] = {}
    for item in current_selection:
        industry = str(item.get('industry') or '未知')
        industries[industry] = industries.get(industry, 0) + 1
        sector_bucket = str(item.get('sector_bucket'))
        sector_buckets[sector_bucket] = sector_buckets.get(sector_bucket, 0) + 1
        primary_bucket = str(item.get('primary_bucket_label') or 'unknown')
        primary_buckets[primary_bucket] = primary_buckets.get(primary_bucket, 0) + 1
    return {
        'industry_counts': dict(sorted(industries.items(), key=lambda item: (-item[1], item[0]))),
        'sector_bucket_counts': dict(sorted(sector_buckets.items(), key=lambda item: (-item[1], item[0]))),
        'primary_bucket_counts': dict(sorted(primary_buckets.items(), key=lambda item: (-item[1], item[0]))),
    }


def render_line_report_md(line_payloads: list[dict[str, Any]], line_comparison: dict[str, Any], final_report: dict[str, Any]) -> str:
    lines = [
        f'# Final Scoring Recovery Report - {final_report["run_name"]}',
        '',
        f'- generated_at: {final_report["generated_at"]}',
        f'- parent_run_name: {final_report["parent_run_name"]}',
        f'- parent_timeout_closure_verified: {final_report["timeout_closure_verified"]}',
        f'- qlib_sync_summary: {final_report.get("qlib_sync_summary_path") or "pending"}',
        '',
        '## Line comparison priority',
    ]
    for item in line_comparison.get('ranked_lines', []):
        lines.extend([
            f'- {item["line_name"]}: composite_rank_score={item["composite_rank_score"]:.4f}; '
            f'walk_c2c={item["walkforward_close_to_close_excess_total_return"]:.4f}; '
            f'analog_c2c={item["analog_close_to_close_total_return"]:.4f}; '
            f'max_weight={item["current_max_single_factor_weight"]:.4f}; '
            f'overfit={item["overfit_risk_level"]}',
        ])
    lines.extend(['', '## Decisions'])
    for decision in final_report.get('line_decisions', []):
        lines.extend([
            f'- {decision["line_name"]}: {decision["decision"]}',
            f'  - rationale: {decision["rationale"]}',
            f'  - next_factor_families: {", ".join(decision.get("next_factor_families_to_keep", [])) or "none"}',
            f'  - next_sector_buckets: {", ".join(decision.get("next_sector_buckets_to_keep", [])) or "none"}',
        ])
    lines.extend(['', '## Per-line shard outcomes'])
    for payload in line_payloads:
        lines.extend([
            f'- {payload["line_name"]}: status={payload["status"]}; '
            f'successful_shards={payload.get("successful_shards", 0)}; '
            f'timed_out_shards={payload.get("timed_out_shards", [])}',
        ])
    lines.append('')
    return '\n'.join(lines) + '\n'


def line_decisions_from_comparison(line_payloads: list[dict[str, Any]], line_comparison: dict[str, Any]) -> list[dict[str, Any]]:
    payload_by_name = {item['line_name']: item for item in line_payloads}
    ranked = line_comparison.get('ranked_lines', [])
    decisions: list[dict[str, Any]] = []
    top_name = ranked[0]['line_name'] if ranked else None
    for item in ranked:
        payload = payload_by_name[item['line_name']]
        current_selection = ((payload.get('current_selection_snapshot') or {}).get('top20')) or []
        top20_summary = top20_sector_summary(current_selection)
        sector_bucket_keep = list(top20_summary.get('sector_bucket_counts', {}).keys())[:2]
        factor_keep = list(item.get('current_selected_factor_families', []))[:3]
        eligible = (
            item['line_name'] == top_name
            and safe_float(item.get('walkforward_close_to_close_excess_total_return')) > 0
            and safe_float(item.get('analog_close_to_close_total_return')) > 0
            and safe_float(item.get('current_max_single_factor_weight')) <= 0.55
        )
        if eligible:
            decision = 'shadow_candidate'
            rationale = 'c2c 与 analog 同时领先，且当前集中度仍在可控区间。'
        else:
            decision = 'freeze'
            rationale = '当前线在 c2c/analog/集中度/overfit 综合排序中不占优，下一轮先缩桶后再评估。'
        decisions.append({
            'line_name': item['line_name'],
            'decision': decision,
            'rationale': rationale,
            'next_factor_families_to_keep': factor_keep,
            'next_sector_buckets_to_keep': sector_bucket_keep,
            'top20_sector_summary': top20_summary,
        })
    return decisions


def build_line_comparison(line_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for payload in line_payloads:
        summary = payload.get('validation_summary') or {}
        rows.append({
            'line_name': payload['line_name'],
            'walkforward_close_to_close_excess_total_return': safe_float(summary.get('walkforward_close_to_close_excess_total_return')),
            'walkforward_open_to_close_excess_total_return': safe_float(summary.get('walkforward_open_to_close_excess_total_return')),
            'analog_close_to_close_total_return': safe_float(summary.get('analog_close_to_close_total_return')),
            'analog_open_to_close_total_return': safe_float(summary.get('analog_open_to_close_total_return')),
            'current_selected_factor_count': int(summary.get('current_selected_factor_count') or 0),
            'current_selected_factor_families': list(summary.get('current_selected_factor_families') or []),
            'current_max_single_factor_weight': safe_float(summary.get('current_max_single_factor_weight')),
            'current_weight_hhi': safe_float(summary.get('current_weight_hhi')),
            'overfit_risk_score': safe_float(summary.get('overfit_risk_score')),
            'overfit_risk_level': str(summary.get('overfit_risk_level') or 'unknown'),
            'walkforward_cross_leg_divergence': bool(summary.get('walkforward_cross_leg_divergence')),
            'analog_cross_leg_divergence': bool(summary.get('analog_cross_leg_divergence')),
            'successful_shards': int(payload.get('successful_shards') or 0),
            'timed_out_shards': [int(item) for item in payload.get('timed_out_shards') or []],
        })
    if not rows:
        return {'generated_at': iso_now(), 'ranked_lines': []}

    walk_vals = [item['walkforward_close_to_close_excess_total_return'] for item in rows]
    analog_vals = [item['analog_close_to_close_total_return'] for item in rows]

    def minmax(value: float, values: list[float]) -> float:
        lo = min(values)
        hi = max(values)
        if abs(hi - lo) <= 1e-12:
            return 1.0
        return (value - lo) / (hi - lo)

    ranked_rows = []
    for item in rows:
        concentration_score = max(0.0, 1.0 - item['current_max_single_factor_weight'])
        overfit_score = max(0.0, 1.0 - min(1.0, item['overfit_risk_score'] / 3.0))
        composite = (
            0.45 * minmax(item['walkforward_close_to_close_excess_total_return'], walk_vals)
            + 0.25 * minmax(item['analog_close_to_close_total_return'], analog_vals)
            + 0.15 * concentration_score
            + 0.15 * overfit_score
        )
        ranked_rows.append({**item, 'concentration_score': concentration_score, 'overfit_score': overfit_score, 'composite_rank_score': float(composite)})
    ranked_rows.sort(key=lambda item: item['composite_rank_score'], reverse=True)
    for idx, item in enumerate(ranked_rows, start=1):
        item['rank'] = idx
    return {
        'generated_at': iso_now(),
        'priority_dimensions': ['walkforward_close_to_close_excess_total_return', 'analog_close_to_close_total_return', 'current_max_single_factor_weight', 'overfit_risk_score'],
        'ranked_lines': ranked_rows,
    }


def process_line(spec: dict[str, Any], run_dir_str: str, shard_timeout_seconds: int) -> dict[str, Any]:
    run_dir = Path(run_dir_str)
    line_name = str(spec['line_name'])
    line_dir = run_dir / line_name
    line_dir.mkdir(parents=True, exist_ok=True)
    started_at = iso_now()
    cfg = load_config(spec['config_path'])

    progress_payload: dict[str, Any] = {
        'line_name': line_name,
        'config_path': spec['config_path'],
        'output_root': str(cfg.output_root),
        'scenario_meta': cfg.scenario_meta,
        'run_dir': str(run_dir),
        'line_dir': str(line_dir),
        'status': 'running',
        'stage': 'bootstrap',
        'started_at': started_at,
        'finished_at': None,
        'shard_count': int(spec['shard_count']),
        'successful_shards': 0,
        'timed_out_shards': [],
        'shards': {},
    }
    write_line_progress(line_dir, progress_payload)
    dump_json(line_dir / 'manifest.json', {
        'run_name': run_dir.name,
        'line_name': line_name,
        'config_path': spec['config_path'],
        'output_root': str(cfg.output_root),
        'shard_count': int(spec['shard_count']),
        'bucket_mode': spec['bucket_mode'],
        'bucket_description': spec['bucket_description'],
        'started_at': started_at,
        'parent_run_name': PARENT_RUN_NAME,
    })

    try:
        walkforward_report = load_json(cfg.output_root / 'walkforward_report.json')
        analog_report = load_json(cfg.output_root / 'analog_validation.json')
        hypotheses = load_json(cfg.output_root / 'market_state_hypotheses.json')

        progress_payload['stage'] = 'load_panel'
        write_line_progress(line_dir, progress_payload)
        panel = read_parquet_with_retries(cfg.panel_path)
        factor_meta = list(cfg.factor_registry['backtestable_factors'])
        factor_cols = [str(item['factor_id']) for item in factor_meta]
        missing_z = [f'{factor_id}__z' for factor_id in factor_cols if f'{factor_id}__z' not in panel.columns]
        if missing_z:
            panel = add_cross_sectional_zscores(panel, factor_cols)
        panel['trade_date'] = panel['trade_date'].astype(str)

        family_budget = dict(hypotheses.get('family_budgets') or {})
        current_state = current_state_from_panel(panel[['trade_date', 'regime_id', 'regime_confidence', 'style_regime_id', 'sector_regime_id']].drop_duplicates())
        current_regime_id = hypotheses.get('current_market_state', {}).get('primary_regime') or current_state.get('regime_id') or 'shrink_uptrend'
        run_context = {
            'config_path': str(Path(spec['config_path']).expanduser().resolve()),
            'scenario_meta': cfg.scenario_meta,
            'scoring_controls': cfg.scoring_controls,
            'selection_controls': cfg.selection_controls,
            'recovery_run_name': run_dir.name,
            'recovery_parent_run_name': PARENT_RUN_NAME,
            'recovery_mode': 'sharded_final_scoring_only',
        }

        trade_dates = sorted(panel['trade_date'].unique().tolist())
        latest_date = trade_dates[-1]
        full_train = panel.loc[panel['trade_date'] < latest_date].copy()
        latest_rows = panel.loc[panel['trade_date'] == latest_date].copy()

        progress_payload['stage'] = 'train_factor_scores'
        progress_payload['latest_date'] = latest_date
        progress_payload['panel_rows'] = int(len(panel))
        progress_payload['train_rows'] = int(len(full_train))
        progress_payload['latest_rows'] = int(len(latest_rows))
        write_line_progress(line_dir, progress_payload)

        train_started = time.monotonic()
        factor_scores = train_factor_scores(full_train, factor_meta, cfg.top_n, cfg.round_trip_cost, current_regime_id, cfg)
        weights, selected_factors = select_weights(full_train, factor_scores, family_budget, cfg)
        train_elapsed = time.monotonic() - train_started
        dump_json(line_dir / 'weight_plan.json', {
            'line_name': line_name,
            'generated_at': iso_now(),
            'current_regime_id': current_regime_id,
            'latest_date': latest_date,
            'weights': weights,
            'selected_factors': selected_factors,
            'top_factor_scores': [
                {
                    'factor_id': item['factor_id'],
                    'family': item['family'],
                    'direction': item['direction'],
                    'direction_label': item['direction_label'],
                    'train_score': safe_float(item['train_score']),
                }
                for item in factor_scores[:10]
            ],
            'train_factor_scores_elapsed_seconds': round(train_elapsed, 3),
        })

        progress_payload['stage'] = 'score_latest_rows'
        progress_payload['weights'] = weights
        progress_payload['selected_factors'] = selected_factors
        progress_payload['train_factor_scores_elapsed_seconds'] = round(train_elapsed, 3)
        write_line_progress(line_dir, progress_payload)

        current_scored = combine_factor_scores(latest_rows, weights, output_col='composite_score')
        current_scored['trade_date'] = latest_date
        bucketed_rows, bucket_meta = bucket_latest_rows(
            line_name=line_name,
            current_scored=current_scored,
            factor_meta=factor_meta,
            family_budget=family_budget,
            shard_count=int(spec['shard_count']),
        )
        dump_json(line_dir / 'shard_plan.json', {
            'line_name': line_name,
            'generated_at': iso_now(),
            'latest_date': latest_date,
            'shard_count': int(spec['shard_count']),
            'bucket_mode': spec['bucket_mode'],
            'bucket_description': spec['bucket_description'],
            'bucket_meta': bucket_meta,
        })

        progress_payload['stage'] = 'materialize_shards'
        progress_payload['bucket_meta'] = bucket_meta
        write_line_progress(line_dir, progress_payload)

        shard_futures = {}
        shard_results: dict[int, dict[str, Any]] = {}
        successful_shard_ids: list[int] = []
        timed_out_shard_ids: list[int] = []
        shard_started_monotonic: dict[Any, float] = {}

        with ThreadPoolExecutor(max_workers=min(4, max(1, int(spec['shard_count'])))) as executor:
            for shard_id in range(int(spec['shard_count'])):
                shard_df = bucketed_rows.loc[bucketed_rows['shard_id'] == shard_id].copy()
                shard_meta = {
                    'primary_bucket_labels': sorted(shard_df['primary_bucket_label'].astype(str).unique().tolist()) if not shard_df.empty else [],
                    'primary_bucket_ids': sorted({int(item) for item in shard_df['primary_bucket_id'].tolist()}) if not shard_df.empty else [],
                    'sector_buckets': sorted({int(item) for item in shard_df['sector_bucket'].tolist()}) if not shard_df.empty else [],
                    'row_count': int(len(shard_df)),
                }
                future = executor.submit(
                    materialize_shard,
                    line_dir=line_dir,
                    line_name=line_name,
                    shard_id=shard_id,
                    shard_df=shard_df,
                    factor_cols=factor_cols,
                    shard_meta=shard_meta,
                )
                shard_futures[future] = shard_id
                shard_started_monotonic[future] = time.monotonic()

            pending = set(shard_futures)
            while pending:
                done, pending = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
                now = time.monotonic()
                for future in list(pending):
                    if now - shard_started_monotonic[future] <= shard_timeout_seconds:
                        continue
                    shard_id = shard_futures[future]
                    timed_out_shard_ids.append(int(shard_id))
                    progress_payload['shards'][str(shard_id)] = {
                        'status': 'timeout',
                        'timeout_seconds': shard_timeout_seconds,
                        'recorded_at': iso_now(),
                    }
                    pending.remove(future)
                for future in done:
                    shard_id = shard_futures[future]
                    try:
                        result = future.result()
                        shard_results[shard_id] = result
                        successful_shard_ids.append(int(shard_id))
                        progress_payload['shards'][str(shard_id)] = result
                    except Exception as exc:
                        progress_payload['shards'][str(shard_id)] = {
                            'status': 'error',
                            'error': str(exc),
                            'traceback': traceback.format_exc().splitlines()[-40:],
                            'recorded_at': iso_now(),
                        }
                progress_payload['successful_shards'] = int(len(successful_shard_ids))
                progress_payload['timed_out_shards'] = sorted(int(item) for item in timed_out_shard_ids)
                write_line_progress(line_dir, progress_payload)

        successful_rows = bucketed_rows.loc[bucketed_rows['shard_id'].isin(successful_shard_ids)].copy()
        current_scored_all = successful_rows.sort_values('composite_score', ascending=False).reset_index(drop=True)
        selection_cols = ['trade_date', 'ts_code', 'name', 'industry', 'market', 'composite_score', 'primary_bucket_label', 'primary_bucket_id', 'sector_bucket', 'shard_id']
        selection_cols.extend([col for col in factor_cols if col in current_scored_all.columns])
        current_selection = current_scored_all[selection_cols].head(cfg.top_n).to_dict(orient='records')

        portfolio_spec = {
            'schema_version': '2026-04-22.greenfield_portfolio_spec_sharded_recovery.v1',
            'trade_date': latest_date,
            'current_regime_id': current_regime_id,
            'run_context': run_context,
            'family_budget': family_budget,
            'weights': weights,
            'selected_factors': selected_factors,
            'legacy_reference_only': ['prebreakout_v41'],
            'recovery': {
                'run_name': run_dir.name,
                'parent_run_name': PARENT_RUN_NAME,
                'bucket_mode': spec['bucket_mode'],
                'bucket_description': spec['bucket_description'],
                'shard_count': int(spec['shard_count']),
                'successful_shards': sorted(int(item) for item in successful_shard_ids),
                'timed_out_shards': sorted(int(item) for item in timed_out_shard_ids),
                'bucket_meta': bucket_meta,
            },
        }

        current_selection_snapshot = {
            'trade_date': latest_date,
            'current_regime_id': current_regime_id,
            'run_context': run_context,
            'weights': weights,
            'selected_factors': selected_factors,
            'top20': current_selection,
            'top20_sector_summary': top20_sector_summary(current_selection),
            'recovery': {
                'run_name': run_dir.name,
                'successful_shards': sorted(int(item) for item in successful_shard_ids),
                'timed_out_shards': sorted(int(item) for item in timed_out_shard_ids),
            },
        }

        validation_summary = build_validation_summary(
            line_name=line_name,
            walkforward_report=walkforward_report,
            analog_report=analog_report,
            weights=weights,
            selected_factors=selected_factors,
            successful_shards=len(successful_shard_ids),
            timed_out_shards=timed_out_shard_ids,
        )
        validation_summary['run_context'] = run_context

        dump_json(cfg.output_root / 'portfolio_spec.json', portfolio_spec)
        dump_json(cfg.output_root / 'current_selection_snapshot.json', current_selection_snapshot)
        dump_json(cfg.output_root / 'validation_summary.json', validation_summary)

        dump_json(line_dir / 'portfolio_spec.json', portfolio_spec)
        dump_json(line_dir / 'current_selection_snapshot.json', current_selection_snapshot)
        dump_json(line_dir / 'validation_summary.json', validation_summary)

        finished_at = iso_now()
        progress_payload['status'] = 'ok'
        progress_payload['stage'] = 'complete'
        progress_payload['finished_at'] = finished_at
        progress_payload['successful_shards'] = int(len(successful_shard_ids))
        progress_payload['timed_out_shards'] = sorted(int(item) for item in timed_out_shard_ids)
        write_line_progress(line_dir, progress_payload)

        result = {
            'line_name': line_name,
            'config_path': spec['config_path'],
            'output_root': str(cfg.output_root),
            'run_dir': str(run_dir),
            'line_dir': str(line_dir),
            'scenario_meta': cfg.scenario_meta,
            'status': 'ok',
            'started_at': started_at,
            'finished_at': finished_at,
            'current_regime_id': current_regime_id,
            'latest_date': latest_date,
            'weights': weights,
            'selected_factors': selected_factors,
            'successful_shards': int(len(successful_shard_ids)),
            'timed_out_shards': sorted(int(item) for item in timed_out_shard_ids),
            'validation_summary': validation_summary,
            'portfolio_spec': portfolio_spec,
            'current_selection_snapshot': current_selection_snapshot,
            'bucket_mode': spec['bucket_mode'],
            'bucket_description': spec['bucket_description'],
        }
        dump_json(run_dir / f'{line_name}.json', result)
        return result
    except Exception as exc:
        finished_at = iso_now()
        progress_payload['status'] = 'error'
        progress_payload['stage'] = 'error'
        progress_payload['finished_at'] = finished_at
        progress_payload['error'] = {
            'message': str(exc),
            'traceback': traceback.format_exc().splitlines()[-80:],
        }
        write_line_progress(line_dir, progress_payload)
        result = {
            'line_name': line_name,
            'config_path': spec['config_path'],
            'output_root': str(cfg.output_root),
            'run_dir': str(run_dir),
            'line_dir': str(line_dir),
            'scenario_meta': cfg.scenario_meta,
            'status': 'error',
            'started_at': started_at,
            'finished_at': finished_at,
            'error': {
                'message': str(exc),
                'traceback': traceback.format_exc().splitlines()[-80:],
            },
            'successful_shards': int(progress_payload.get('successful_shards') or 0),
            'timed_out_shards': sorted(int(item) for item in progress_payload.get('timed_out_shards') or []),
        }
        dump_json(run_dir / f'{line_name}.json', result)
        return result


def main() -> int:
    args = parse_args()
    run_dir = PARALLEL_ROOT / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    parent_run_dir = args.parent_run_dir.expanduser().resolve()
    parent_timeout_report = args.parent_timeout_report.expanduser().resolve()
    progress_output = args.progress_output.expanduser().resolve()
    progress_log = args.progress_log.expanduser().resolve()

    timeout_report = load_parent_timeout_report(parent_timeout_report)
    verification = {
        'verified_at': iso_now(),
        'parent_run_name': timeout_report.get('run_name'),
        'parent_run_dir': str(parent_run_dir),
        'parent_status': timeout_report.get('decision'),
        'verified_last_completed_stages': {
            item['line_name']: item['last_completed_stage']
            for item in timeout_report.get('per_line', [])
        },
        'formal_parent_recorder_id': timeout_report.get('evidence', {}).get('formal_qlib_recorder_id') or PARENT_RECORDER_ID,
    }
    dump_json(run_dir / 'timeout_closure_verification.json', verification)

    manifest = {
        'schema_version': '2026-04-22.sharded_final_scoring_recovery.manifest.v1',
        'run_name': args.run_name,
        'run_dir': str(run_dir),
        'parent_run_name': PARENT_RUN_NAME,
        'parent_run_dir': str(parent_run_dir),
        'parent_timeout_report': str(parent_timeout_report),
        'started_at': iso_now(),
        'line_specs': LINE_SPECS,
        'line_order': [item['line_name'] for item in LINE_SPECS],
        'shard_timeout_seconds': int(args.shard_timeout_seconds),
        'heartbeat_seconds': int(args.heartbeat_seconds),
        'qlib_parent_recorder_id_preserved_timeout_closure': PARENT_RECORDER_ID,
    }
    dump_json(run_dir / 'manifest.json', manifest)

    line_payloads = {spec['line_name']: build_pending_payload(spec, run_dir) for spec in LINE_SPECS}
    emit_progress(
        progress_output=progress_output,
        progress_log=progress_log,
        run_name=args.run_name,
        run_dir=run_dir,
        line_payloads=[line_payloads[spec['line_name']] for spec in LINE_SPECS],
        status='running',
        stage='started',
        heartbeat_seconds=int(args.heartbeat_seconds),
    )

    with ProcessPoolExecutor(max_workers=max(1, int(args.line_workers))) as executor:
        future_map = {
            executor.submit(process_line, spec, str(run_dir), int(args.shard_timeout_seconds)): spec
            for spec in LINE_SPECS
        }
        pending = set(future_map)
        while pending:
            done, pending = wait(pending, timeout=max(1, int(args.heartbeat_seconds)), return_when=FIRST_COMPLETED)
            if not done:
                live_payloads = [line_live_payload(spec, run_dir) for spec in LINE_SPECS]
                emit_progress(
                    progress_output=progress_output,
                    progress_log=progress_log,
                    run_name=args.run_name,
                    run_dir=run_dir,
                    line_payloads=live_payloads,
                    status='running',
                    stage='heartbeat',
                    heartbeat_seconds=int(args.heartbeat_seconds),
                )
                continue
            for future in done:
                result = future.result()
                line_payloads[result['line_name']] = result
                emit_progress(
                    progress_output=progress_output,
                    progress_log=progress_log,
                    run_name=args.run_name,
                    run_dir=run_dir,
                    line_payloads=[line_payloads[spec['line_name']] for spec in LINE_SPECS],
                    status='running',
                    stage=f'line_complete:{result.get("line_name")}',
                    heartbeat_seconds=int(args.heartbeat_seconds),
                )

    final_payloads = [line_payloads[spec['line_name']] for spec in LINE_SPECS]
    line_comparison = build_line_comparison(final_payloads)
    dump_json(run_dir / 'line_comparison.json', line_comparison)

    final_report = {
        'report_type': 'sharded_final_scoring_recovery',
        'generated_at': iso_now(),
        'run_name': args.run_name,
        'run_dir': str(run_dir),
        'parent_run_name': PARENT_RUN_NAME,
        'parent_run_dir': str(parent_run_dir),
        'timeout_closure_verified': True,
        'timeout_closure_verification_path': str(run_dir / 'timeout_closure_verification.json'),
        'line_comparison_path': str(run_dir / 'line_comparison.json'),
        'qlib_parent_recorder_id_preserved_timeout_closure': PARENT_RECORDER_ID,
        'line_payload_paths': [str(run_dir / f'{spec["line_name"]}.json') for spec in LINE_SPECS],
    }
    final_report['line_decisions'] = line_decisions_from_comparison(final_payloads, line_comparison)
    final_report['shadow_candidates'] = [item['line_name'] for item in final_report['line_decisions'] if item['decision'] == 'shadow_candidate']
    final_report['frozen_lines'] = [item['line_name'] for item in final_report['line_decisions'] if item['decision'] == 'freeze']
    dump_json(run_dir / 'final_scoring_recovery_report.json', final_report)
    (run_dir / 'final_scoring_recovery_report.md').write_text(
        render_line_report_md(final_payloads, line_comparison, final_report),
        encoding='utf-8',
    )

    final_status = 'ok' if final_payloads and all(item.get('status') == 'ok' for item in final_payloads) else 'error'
    emit_progress(
        progress_output=progress_output,
        progress_log=progress_log,
        run_name=args.run_name,
        run_dir=run_dir,
        line_payloads=final_payloads,
        status=final_status,
        stage='complete',
        heartbeat_seconds=int(args.heartbeat_seconds),
    )
    return 0 if final_status == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
