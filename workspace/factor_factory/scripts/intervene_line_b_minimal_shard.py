#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from greenfield_multifactor_lib import (
    add_cross_sectional_zscores,
    combine_factor_scores,
    dump_json,
    load_config,
    load_json,
    write_parquet_atomic,
)
from run_sharded_final_scoring_recovery import family_signal, rank_quantile_buckets

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
DEFAULT_RUN_DIR = ROOT / 'outputs' / 'evolution' / 'parallel_lines' / '20260422_sharded_final_scoring_recovery'
DEFAULT_LINE_NAME = 'line_b_downside_rotation_c2c'
DEFAULT_CONFIG_PATH = ROOT / 'config' / 'greenfield_parallel_lines' / 'line_b_downside_rotation_c2c.yaml'
TZ = ZoneInfo('Asia/Shanghai')


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Emergency minimal line_b shard intervention')
    parser.add_argument('--run-dir', type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument('--line-name', default=DEFAULT_LINE_NAME)
    parser.add_argument('--config-path', type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument('--bucket-count', type=int, default=3)
    parser.add_argument('--pending-shards', type=int, default=5)
    return parser.parse_args()


def load_weight_source(line_dir: Path, output_root: Path) -> tuple[dict[str, float], dict[str, Any]]:
    progress_path = line_dir / 'progress.json'
    if progress_path.exists():
        progress = load_json(progress_path)
        weights = {str(k): float(v) for k, v in (progress.get('weights') or {}).items()}
        if weights:
            return weights, {
                'type': 'line_progress',
                'path': str(progress_path),
                'generated_at': progress.get('finished_at') or progress.get('started_at'),
            }
    walkforward_path = output_root / 'walkforward_report.json'
    if walkforward_path.exists():
        walkforward = load_json(walkforward_path)
        folds = walkforward.get('folds') or []
        if folds:
            last_fold = folds[-1]
            weights = {str(k): float(v) for k, v in (last_fold.get('weights') or {}).items()}
            if weights:
                return weights, {
                    'type': 'walkforward_last_fold',
                    'path': str(walkforward_path),
                    'fold_id': last_fold.get('fold_id'),
                    'test_end': last_fold.get('test_end'),
                }
    raise RuntimeError('unable to locate usable weights from progress.json or walkforward_report.json')


def stable_industry_choice(series: pd.Series) -> tuple[str, int]:
    counts = series.fillna('未知').astype(str).value_counts()
    if counts.empty:
        raise RuntimeError('candidate shard has no industry labels')
    ranked = sorted(((int(count), str(industry)) for industry, count in counts.items()), key=lambda item: (-item[0], item[1]))
    top_count, top_industry = ranked[0]
    return top_industry, top_count


def pending_score_payload(line_name: str, shard_id: int, generated_at: str, note: str) -> dict[str, Any]:
    return {
        'line_name': line_name,
        'shard_id': shard_id,
        'status': 'pending',
        'generated_at': generated_at,
        'note': note,
    }


def pending_rank_payload(line_name: str, shard_id: int, generated_at: str, note: str) -> dict[str, Any]:
    return {
        'line_name': line_name,
        'shard_id': shard_id,
        'status': 'pending',
        'generated_at': generated_at,
        'rank_count': 0,
        'ranked_records': [],
        'note': note,
    }


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    config_path = args.config_path.expanduser().resolve()
    cfg = load_config(config_path)
    line_dir = run_dir / args.line_name
    line_dir.mkdir(parents=True, exist_ok=True)

    intervention_path = line_dir / 'minimal_shard_intervention.json'
    timeout_report_path = line_dir / 'shard_timeout_report.json'
    score_parquet_path = line_dir / 'scores_shard_000.parquet'
    score_json_path = line_dir / 'scores_shard_000.json'
    rank_json_path = line_dir / 'rank_shard_000.json'
    minimal_plan_path = line_dir / 'minimal_shard_plan.json'

    started_at = iso_now()
    try:
        factor_meta = list(cfg.factor_registry.get('backtestable_factors') or [])
        factor_cols = [str(item['factor_id']) for item in factor_meta]
        family_map: dict[str, list[str]] = {}
        for item in factor_meta:
            family_map.setdefault(str(item['family']), []).append(str(item['factor_id']))

        weights, weight_source = load_weight_source(line_dir, cfg.output_root)
        selected_factor_ids = sorted(weights.keys())

        columns = [
            'trade_date',
            'ts_code',
            'name',
            'industry',
            'market',
            'avg_amount_lookback',
        ] + factor_cols
        panel = pd.read_parquet(cfg.panel_path, columns=columns)
        panel['trade_date'] = panel['trade_date'].astype(str)
        latest_date = str(panel['trade_date'].max())
        latest_rows = panel.loc[panel['trade_date'] == latest_date].copy()
        latest_rows = add_cross_sectional_zscores(latest_rows, factor_cols)

        latest_rows['downside_signal'] = family_signal(latest_rows, family_map.get('downside_control', []), budget=1.0)
        latest_rows['downside_bucket_id'] = rank_quantile_buckets(latest_rows['downside_signal'], bucket_count=args.bucket_count)
        latest_rows['liquidity_bucket_id'] = rank_quantile_buckets(pd.to_numeric(latest_rows['avg_amount_lookback'], errors='coerce'), bucket_count=args.bucket_count)

        strongest_downside_bucket = int(latest_rows['downside_bucket_id'].max())
        max_liquidity_bucket = int(latest_rows['liquidity_bucket_id'].max())
        candidates = latest_rows.loc[
            (latest_rows['downside_bucket_id'] == strongest_downside_bucket)
            & (latest_rows['liquidity_bucket_id'] == max_liquidity_bucket)
        ].copy()
        if candidates.empty:
            raise RuntimeError('strongest downside bucket x max liquidity bucket produced no rows')

        chosen_industry, chosen_industry_count = stable_industry_choice(candidates['industry'])
        subset = candidates.loc[candidates['industry'].fillna('未知').astype(str) == chosen_industry].copy()
        if subset.empty:
            raise RuntimeError('largest industry sample inside strongest downside/max liquidity bucket produced no rows')

        subset['primary_bucket_label'] = f'downside_bucket_{strongest_downside_bucket}'
        subset['primary_bucket_id'] = int(strongest_downside_bucket)
        subset['liquidity_bucket_label'] = f'liquidity_bucket_{max_liquidity_bucket}'
        subset['liquidity_bucket_id'] = int(max_liquidity_bucket)
        subset['industry_sample_label'] = chosen_industry
        subset['shard_id'] = 0

        scored = combine_factor_scores(subset, weights, output_col='composite_score').sort_values('composite_score', ascending=False).reset_index(drop=True)
        scored['industry_sample_rank'] = scored.index + 1

        z_cols = [f'{factor_id}__z' for factor_id in selected_factor_ids if f'{factor_id}__z' in scored.columns]
        parquet_columns = [
            'trade_date',
            'ts_code',
            'name',
            'industry',
            'market',
            'avg_amount_lookback',
            'downside_signal',
            'downside_bucket_id',
            'liquidity_bucket_id',
            'primary_bucket_label',
            'primary_bucket_id',
            'liquidity_bucket_label',
            'industry_sample_label',
            'industry_sample_rank',
            'shard_id',
            'composite_score',
        ] + factor_cols + z_cols
        parquet_columns = [col for col in parquet_columns if col in scored.columns]
        score_df = scored[parquet_columns].copy()
        write_parquet_atomic(score_df, score_parquet_path, index=False)

        rank_columns = [
            'trade_date',
            'ts_code',
            'name',
            'industry',
            'market',
            'avg_amount_lookback',
            'downside_signal',
            'downside_bucket_id',
            'liquidity_bucket_id',
            'primary_bucket_label',
            'primary_bucket_id',
            'liquidity_bucket_label',
            'industry_sample_label',
            'industry_sample_rank',
            'shard_id',
            'composite_score',
        ] + factor_cols
        rank_columns = [col for col in rank_columns if col in scored.columns]
        ranked_records = scored[rank_columns].head(50).to_dict(orient='records')

        finished_at = iso_now()
        score_payload = {
            'line_name': args.line_name,
            'shard_id': 0,
            'status': 'ok',
            'started_at': started_at,
            'finished_at': finished_at,
            'row_count': int(len(score_df)),
            'top_score': float(scored.iloc[0]['composite_score']) if not scored.empty else None,
            'top_ts_code': None if scored.empty else str(scored.iloc[0]['ts_code']),
            'top_industry': None if scored.empty else str(scored.iloc[0]['industry']),
            'latest_date': latest_date,
            'weights': weights,
            'weight_source': weight_source,
            'bucket_meta': {
                'selection_mode': 'minimal_acceptance_shard_only',
                'primary_bucket_name': 'downside_control_bucket',
                'primary_bucket_id': strongest_downside_bucket,
                'primary_bucket_label': f'downside_bucket_{strongest_downside_bucket}',
                'primary_bucket_strategy': 'strongest downside-control bucket by cross-sectional family signal',
                'liquidity_bucket_id': max_liquidity_bucket,
                'liquidity_bucket_label': f'liquidity_bucket_{max_liquidity_bucket}',
                'liquidity_bucket_strategy': 'max avg_amount_lookback quantile bucket',
                'industry_sample_label': chosen_industry,
                'industry_sample_count': int(chosen_industry_count),
                'candidate_rows_before_industry_filter': int(len(candidates)),
                'row_count': int(len(score_df)),
            },
            'paths': {
                'parquet': str(score_parquet_path),
                'summary': str(score_json_path),
                'rank': str(rank_json_path),
            },
        }
        rank_payload = {
            'line_name': args.line_name,
            'shard_id': 0,
            'generated_at': finished_at,
            'status': 'ok',
            'rank_count': int(len(ranked_records)),
            'bucket_meta': score_payload['bucket_meta'],
            'ranked_records': ranked_records,
            'score_parquet': str(score_parquet_path),
        }
        dump_json(score_json_path, score_payload)
        dump_json(rank_json_path, rank_payload)

        pending_note = 'minimal acceptance path intentionally deferred; only shard_000 was executed for immediate recovery.'
        pending_shards: list[int] = []
        for offset in range(1, args.pending_shards + 1):
            pending_shards.append(offset)
            dump_json(line_dir / f'scores_shard_{offset:03d}.json', pending_score_payload(args.line_name, offset, finished_at, pending_note))
            dump_json(line_dir / f'rank_shard_{offset:03d}.json', pending_rank_payload(args.line_name, offset, finished_at, pending_note))

        minimal_plan = {
            'line_name': args.line_name,
            'generated_at': finished_at,
            'status': 'ok',
            'mode': 'minimal_acceptance_shard_only',
            'completed_shards': [0],
            'pending_shards': pending_shards,
            'artifact_paths': {
                'scores_shard_000_parquet': str(score_parquet_path),
                'scores_shard_000_json': str(score_json_path),
                'rank_shard_000_json': str(rank_json_path),
            },
            'selection_criteria': score_payload['bucket_meta'],
            'note': 'Other shards were explicitly marked pending in the 3-digit namespace to avoid re-entering full train_factor_scores.',
        }
        dump_json(minimal_plan_path, minimal_plan)

        intervention_payload = {
            'status': 'ok',
            'started_at': started_at,
            'finished_at': finished_at,
            'line_name': args.line_name,
            'run_dir': str(run_dir),
            'line_dir': str(line_dir),
            'config_path': str(config_path),
            'action': 'materialize_exact_000_minimal_shard',
            'weights': weights,
            'weight_source': weight_source,
            'latest_date': latest_date,
            'minimal_plan_path': str(minimal_plan_path),
            'artifacts': minimal_plan['artifact_paths'],
            'selection_criteria': score_payload['bucket_meta'],
            'observed_existing_full_run': {
                'line_progress_path': str(line_dir / 'progress.json'),
                'note': 'This intervention backfills the exact 3-digit minimal shard artifacts without restarting full-line training.',
            },
        }
        dump_json(intervention_path, intervention_payload)
        timeout_report_path.unlink(missing_ok=True)

        print(json.dumps({
            'status': 'ok',
            'line_name': args.line_name,
            'row_count': int(len(score_df)),
            'latest_date': latest_date,
            'industry': chosen_industry,
            'artifacts': minimal_plan['artifact_paths'],
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        failed_at = iso_now()
        timeout_report = {
            'report_type': 'shard_timeout_report',
            'status': 'error',
            'line_name': args.line_name,
            'started_at': started_at,
            'failed_at': failed_at,
            'run_dir': str(run_dir),
            'line_dir': str(line_dir),
            'requested_action': 'materialize scores_shard_000/rank_shard_000 only',
            'qlib_sync_status': 'sharded_recovery_blocked',
            'error_type': type(exc).__name__,
            'error_message': str(exc),
            'traceback': traceback.format_exc(),
        }
        dump_json(timeout_report_path, timeout_report)
        dump_json(intervention_path, {
            'status': 'error',
            'started_at': started_at,
            'failed_at': failed_at,
            'line_name': args.line_name,
            'qlib_sync_status': 'sharded_recovery_blocked',
            'timeout_report_path': str(timeout_report_path),
            'error_type': type(exc).__name__,
            'error_message': str(exc),
        })
        print(json.dumps({'status': 'error', 'line_name': args.line_name, 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
