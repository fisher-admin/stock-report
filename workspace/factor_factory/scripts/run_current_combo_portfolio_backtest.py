#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DEFAULT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
TZ = ZoneInfo('Asia/Shanghai')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Promote current combo replay into a formal portfolio backtest artifact')
    parser.add_argument('--root', default=str(ROOT_DEFAULT))
    parser.add_argument('--replay-path', default=None)
    parser.add_argument('--output-stem', default='current_combo_portfolio_backtest')
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build_equity_curve(records: list[dict]) -> list[dict]:
    nav = 1.0
    rows = []
    for row in records:
        nav *= 1.0 + float(row.get('net_return', 0.0))
        rows.append(
            {
                'date': row.get('date'),
                'regime_id': row.get('regime_id'),
                'gross_return': float(row.get('gross_return', 0.0)),
                'net_return': float(row.get('net_return', 0.0)),
                'turnover': float(row.get('turnover', 0.0)),
                'nav': nav,
            }
        )
    return rows


def write_curve_csv(path: Path, curves: dict[str, list[dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ['series', 'date', 'regime_id', 'gross_return', 'net_return', 'turnover', 'nav']
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for series, rows in curves.items():
            for row in rows:
                writer.writerow({'series': series, **row})


def build_summary(
    replay: dict,
    replay_path: Path,
    curve_json_path: Path,
    curve_csv_path: Path,
) -> tuple[dict, dict[str, list[dict]]]:
    replay_results = replay['replay_results']
    curves = {
        key: build_equity_curve(value.get('daily_records', []))
        for key, value in replay_results.items()
    }
    source_mtimes: dict[str, str] = {}
    stale_keys = {
        'factor_validation_report',
        'portfolio_backtest_report',
        'overfit_audit_report',
        'factor_promotion_decision',
    }
    for key, raw_path in (replay.get('source_paths', {}) or {}).items():
        if key in stale_keys:
            continue
        try:
            source_mtimes[key] = datetime.fromtimestamp(Path(raw_path).expanduser().stat().st_mtime, TZ).isoformat(timespec='seconds')
        except Exception:
            if replay.get('source_mtimes', {}).get(key):
                source_mtimes[key] = replay['source_mtimes'][key]
    source_mtimes['current_combo_replay_report'] = datetime.fromtimestamp(replay_path.stat().st_mtime, TZ).isoformat(timespec='seconds')

    comparisons = replay['comparisons']
    direction = replay['direction_consistency']
    comparisons_without_backfill = replay.get('sensitivity', {}).get('comparisons_without_backfill', {})
    direction_consistent = bool(direction.get('combo_direction_consistent') and direction.get('excess_direction_consistent'))
    primary_label_mode = str(replay.get('current_combo_definition', {}).get('canonical_label_mode') or 'close_to_close')
    primary_combo_key = f'{primary_label_mode}_combo'
    primary_base_key = f'{primary_label_mode}_base'
    primary_combo_total_return = replay_results[primary_combo_key]['metrics']['total_return']
    eligible_trade_days = int(replay['benchmark_eligible_summary']['eligible_trade_days'])
    effective_primary_days = int(replay['benchmark_eligible_summary']['effective_replay_days'][primary_combo_key])

    def fmt_pct(value: float | None) -> str:
        if value is None:
            return 'NA'
        return f'{float(value):.4%}'

    verdict = {
        'current_combo_has_portfolio_level_backtest_evidence': 'yes_formal_current_weight_map_backtest_but_not_independent_oos',
        'current_combo_has_independent_end_to_end_backtest': False,
        'close_to_close_vs_open_to_close_direction_consistent': direction_consistent,
        'close_to_close_vs_open_to_close_absolute_direction_consistent': bool(direction.get('combo_direction_consistent')),
        'close_to_close_vs_open_to_close_excess_direction_consistent': bool(direction.get('excess_direction_consistent')),
        'primary_label_mode': primary_label_mode,
        'primary_label_column': replay.get('replay_assumptions', {}).get('primary_label_column'),
        'primary_vs_close_to_close_combo_direction_consistent': bool(direction.get('primary_vs_close_to_close_combo_direction_consistent')),
        'primary_vs_close_to_close_excess_direction_consistent': bool(direction.get('primary_vs_close_to_close_excess_direction_consistent')),
        'sufficient_to_lift_promotion_freeze': False,
        'minimal_credible_conclusion': (
            f'当前 {primary_label_mode} canonical 组合已补出 formal current-weight-map backtest：'
            f'primary combo 总收益 {fmt_pct(primary_combo_total_return)}、相对 base 超额 {fmt_pct(comparisons.get(f"{primary_label_mode}_excess_total_return"))}；'
            f'cross-check 为 c2c={fmt_pct(replay_results["close_to_close_combo"]["metrics"]["total_return"])} / '
            f'o2c={fmt_pct(replay_results["open_to_close_combo"]["metrics"]["total_return"])} / '
            f'c2o={fmt_pct(replay_results["close_to_open_combo"]["metrics"]["total_return"])}。'
            f'即便剔除回填日后 primary 超额仍为 {fmt_pct(comparisons_without_backfill.get(f"{primary_label_mode}_excess_total_return"))}，'
            f'该证据仍只覆盖 {eligible_trade_days} 个 eligible trade days / {effective_primary_days} 个有效回放日，'
            '不能替代独立 end-to-end backtest、独立 OOS 或 nested validation；收益级 analog replay 虽已具备，但仍只是类比验证而非独立 OOS，因此不足以解除 promotion freeze。'
        ),
        'key_reasons': [
            f'formal current-weight-map backtest 已落地，但仍只覆盖 {eligible_trade_days} 个 eligible trade days / {effective_primary_days} 个有效回放日。',
            'close-to-close / open-to-close / close-to-open 三口径下的绝对收益与超额收益仍存在结构差异。',
            '当前证据仍不是独立 end-to-end backtest、独立 OOS 或 nested validation。',
            'benchmark_eligible 覆盖仍短；收益级 analog replay 虽已具备，但仍不能替代独立 OOS。',
        ],
    }

    summary = {
        'generated_at': datetime.now(TZ).isoformat(timespec='seconds'),
        'report_type': 'current_combo_portfolio_backtest',
        'schema_version': '2026-04-19.current_combo_portfolio_backtest.v2',
        'source_paths': {
            'current_combo_replay_report': str(replay_path),
            'weight_map': replay['source_paths']['weight_map'],
            'factor_audit': replay['source_paths']['factor_audit'],
            'current_market_fingerprint': replay['source_paths']['current_market_fingerprint'],
        },
        'source_mtimes': source_mtimes,
        'combo_definition': replay['current_combo_definition'],
        'benchmark_eligible_summary': replay['benchmark_eligible_summary'],
        'assumptions': replay['replay_assumptions'],
        'formal_backtest_scope': {
            'eligible_trade_days': replay['benchmark_eligible_summary']['eligible_trade_days'],
            'effective_replay_days': replay['benchmark_eligible_summary']['effective_replay_days'],
            'cost_model': {
                'round_trip_cost': replay['replay_assumptions']['round_trip_cost'],
                'included_in_metrics': True,
            },
            'execution_labels': replay['replay_assumptions']['label_columns'],
            'primary_label_mode': primary_label_mode,
            'primary_label_column': replay.get('replay_assumptions', {}).get('primary_label_column'),
            'note': 'This is a formalized artifact built from the latest current_combo replay pipeline; it is still not an independent holdout / nested validation backtest.',
        },
        'series_metrics': {
            key: value['metrics'] for key, value in replay_results.items()
        },
        'primary_backtest_signal': {
            'label_mode': primary_label_mode,
            'combo_key': primary_combo_key,
            'base_key': primary_base_key,
            'combo_total_return': primary_combo_total_return,
            'base_total_return': replay_results[primary_base_key]['metrics']['total_return'],
            'excess_total_return': comparisons.get(f'{primary_label_mode}_excess_total_return'),
            'effective_replay_days': effective_primary_days,
        },
        'equity_curve_files': {
            'csv': str(curve_csv_path),
            'json': str(curve_json_path),
        },
        'equity_curve_preview': {
            key: rows[:3] + rows[-3:] if len(rows) > 6 else rows
            for key, rows in curves.items()
        },
        'comparisons': comparisons,
        'direction_consistency': direction,
        'sensitivity': replay['sensitivity'],
        'verdict': verdict,
        'minimum_required_metrics': {
            key: {
                'net_value_end': curves[key][-1]['nav'] if curves[key] else 1.0,
                **replay_results[key]['metrics'],
            }
            for key in replay_results
        },
        'claim_limit': 'Formal artifact and return-level analog replay are now present, but no independent OOS / nested walk-forward / independent holdout has been completed yet.',
    }
    return summary, curves


if __name__ == '__main__':
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    evolution = root / 'outputs' / 'evolution'
    evolution.mkdir(parents=True, exist_ok=True)
    replay_path = Path(args.replay_path).expanduser().resolve() if args.replay_path else evolution / 'current_combo_replay_report.json'
    report_path = evolution / f'{args.output_stem}.json'
    curve_json_path = evolution / f'{args.output_stem}_equity_curves.json'
    curve_csv_path = evolution / f'{args.output_stem}_equity_curves.csv'

    replay = load_json(replay_path)
    summary, curves = build_summary(replay, replay_path, curve_json_path, curve_csv_path)
    dump_json(report_path, summary)
    dump_json(curve_json_path, curves)
    write_curve_csv(curve_csv_path, curves)
    print(json.dumps({
        'status': 'ok',
        'report_path': str(report_path),
        'curve_json_path': str(curve_json_path),
        'curve_csv_path': str(curve_csv_path),
        'primary_label_mode': summary['primary_backtest_signal']['label_mode'],
        'primary_combo_total_return': summary['primary_backtest_signal']['combo_total_return'],
        'close_to_close_combo_total_return': summary['series_metrics']['close_to_close_combo']['total_return'],
        'open_to_close_combo_total_return': summary['series_metrics']['open_to_close_combo']['total_return'],
        'close_to_open_combo_total_return': summary['series_metrics']['close_to_open_combo']['total_return'],
    }, ensure_ascii=False, indent=2))
