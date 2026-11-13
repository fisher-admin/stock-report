#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DEFAULT = Path('/Users/fisher/.openclaw/workspace/factor_factory')


def load_replay_module(root: Path):
    script_path = root / 'scripts' / 'run_current_combo_replay.py'
    spec = importlib.util.spec_from_file_location('run_current_combo_replay', script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'failed to load replay module: {script_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build_segment_summary(module, replay_results: dict[str, Any], dates: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        'dates': dates,
        'days': len(dates),
        'by_label_mode': {},
    }
    for mode in ['close_to_close', 'open_to_close', 'close_to_open']:
        combo_key = f'{mode}_combo'
        base_key = f'{mode}_base'
        combo_rows = [row for row in replay_results[combo_key]['daily_records'] if row['date'] in dates]
        base_rows = [row for row in replay_results[base_key]['daily_records'] if row['date'] in dates]
        combo_metrics = module.build_metrics(combo_rows)
        base_metrics = module.build_metrics(base_rows)
        summary['by_label_mode'][mode] = {
            'combo_metrics': combo_metrics,
            'base_metrics': base_metrics,
            'excess_total_return': combo_metrics['total_return'] - base_metrics['total_return'],
            'effective_trade_dates': [row['date'] for row in combo_rows],
        }
    return summary


def build_markdown(report: dict[str, Any]) -> str:
    parts = report['evaluation_partitions']
    verdict = report['verdict']
    lines = [
        '# Current Combo Stage72 Independent OOS / Holdout Validation',
        '',
        f"- reserved_union={report['reserved_union_dates']}",
        f"- legacy_grandfathered={parts['grandfathered_reserved']['dates']}",
        f"- independent_oos={parts['independent_oos']['dates']}",
        f"- holdout_validation={parts['holdout_validation']['dates']}",
        '',
        '## Close-to-close',
    ]
    for name in ['independent_oos', 'holdout_validation']:
        item = parts[name]['by_label_mode']['close_to_close']
        lines.extend([
            f"### {name}",
            f"- combo_total_return={item['combo_metrics']['total_return']:.4%}",
            f"- base_total_return={item['base_metrics']['total_return']:.4%}",
            f"- excess_total_return={item['excess_total_return']:.4%}",
            f"- sharpe={item['combo_metrics']['sharpe']:.3f}",
            f"- max_drawdown={item['combo_metrics']['max_drawdown']:.4%}",
            f"- effective_trade_dates={item['effective_trade_dates']}",
            '',
        ])
    lines.extend([
        '## Verdict',
        f"- close_to_close_independent_oos_positive={verdict['close_to_close_independent_oos_positive']}",
        f"- close_to_close_holdout_positive={verdict['close_to_close_holdout_positive']}",
        f"- sufficient_to_lift_promotion_freeze={verdict['sufficient_to_lift_promotion_freeze']}",
        f"- summary={verdict['summary']}",
        '',
        '## Context',
        f"- walkforward_reference={report['context']['walkforward_reference']}",
        f"- nested_reference={report['context']['nested_reference']}",
        f"- note={report['context']['note']}",
        '',
    ])
    return '\n'.join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Evaluate stage72 independent OOS and holdout for current combo')
    parser.add_argument('--root', default=str(ROOT_DEFAULT))
    parser.add_argument('--reserve-path', required=True)
    parser.add_argument('--output-stem', default='current_combo_stage72_independent_oos_holdout')
    parser.add_argument('--weight-path', default=None)
    parser.add_argument('--audit-path', default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    module = load_replay_module(root)
    reserve_path = Path(args.reserve_path).expanduser().resolve()
    reserve = read_json(reserve_path)
    evolution = root / 'outputs' / 'evolution'
    replay_report_path = evolution / 'current_combo_replay_report.json'
    replay_report = read_json(replay_report_path)

    weight_path = Path(args.weight_path).expanduser().resolve() if args.weight_path else Path(replay_report['source_paths']['weight_map'])
    audit_path = Path(args.audit_path).expanduser().resolve() if args.audit_path else Path(replay_report['source_paths']['factor_audit'])
    panel_path = Path(replay_report['source_paths']['panel'])
    regime_path = Path(replay_report['source_paths']['regime'])

    weights = module.read_json(weight_path)
    audit = module.read_json(audit_path)
    regime_map = {row['regime_id']: row for row in weights['regime_weight_map']}
    factor_union = module.collect_weight_map_factor_union(weights)
    factor_signs = {
        row['factor_id']: (1.0 if float(row.get('rank_ic_mean', 0.0) or 0.0) >= 0 else -1.0)
        for row in audit.get('reports', [])
    }

    panel = pd.read_parquet(
        panel_path,
        columns=[
            'trade_date',
            'ts_code',
            'base_factor_score_norm',
            'next_return_close_to_close_1d',
            'next_return_open_to_close_1d',
            'next_return_close_to_open_1d',
            *factor_union,
        ],
    )
    panel['trade_date'] = panel['trade_date'].astype(str)
    regime = pd.read_parquet(regime_path, columns=['trade_date', 'regime_id'])
    regime['trade_date'] = regime['trade_date'].astype(str)

    reserved_union_dates = [str(x) for x in reserve.get('reserved_holdout_dates', []) if x]
    partition_dates = sorted(set(
        [str(x) for x in reserve.get('grandfathered_reserved_dates', []) if x]
        + [str(x) for x in reserve.get('independent_oos_dates', []) if x]
        + [str(x) for x in reserve.get('holdout_validation_dates', []) if x]
    ))
    eval_panel = panel.loc[panel['trade_date'].isin(partition_dates)].copy().merge(regime, on='trade_date', how='inner')
    eval_panel['base_factor_score_norm_z'] = module.zscore_by_date(eval_panel, 'base_factor_score_norm')
    for factor_id in factor_union:
        eval_panel[f'{factor_id}_z'] = module.zscore_by_date(eval_panel, factor_id)

    label_specs = [
        ('close_to_close_combo', 'next_return_close_to_close_1d', 'combo_score'),
        ('close_to_close_base', 'next_return_close_to_close_1d', 'base_score_only'),
        ('open_to_close_combo', 'next_return_open_to_close_1d', 'combo_score'),
        ('open_to_close_base', 'next_return_open_to_close_1d', 'base_score_only'),
        ('close_to_open_combo', 'next_return_close_to_open_1d', 'combo_score'),
        ('close_to_open_base', 'next_return_close_to_open_1d', 'base_score_only'),
    ]
    replay_results = module.run_replay(eval_panel, regime_map, factor_signs, label_specs)

    partitions = {
        'grandfathered_reserved': build_segment_summary(module, replay_results, [str(x) for x in reserve.get('grandfathered_reserved_dates', []) if x]),
        'independent_oos': build_segment_summary(module, replay_results, [str(x) for x in reserve.get('independent_oos_dates', []) if x]),
        'holdout_validation': build_segment_summary(module, replay_results, [str(x) for x in reserve.get('holdout_validation_dates', []) if x]),
    }
    c2c_oos = partitions['independent_oos']['by_label_mode']['close_to_close']
    c2c_holdout = partitions['holdout_validation']['by_label_mode']['close_to_close']
    verdict = {
        'close_to_close_independent_oos_positive': bool(c2c_oos['combo_metrics']['total_return'] > 0 and c2c_oos['excess_total_return'] > 0),
        'close_to_close_holdout_positive': bool(c2c_holdout['combo_metrics']['total_return'] > 0 and c2c_holdout['excess_total_return'] > 0),
    }
    independent_days = int(partitions['independent_oos'].get('days', 0) or 0)
    holdout_days = int(partitions['holdout_validation'].get('days', 0) or 0)
    total_independent_days = independent_days + holdout_days
    sample_gate_pass = (
        independent_days >= MIN_INDEPENDENT_OOS_DAYS
        and holdout_days >= MIN_HOLDOUT_DAYS
        and total_independent_days >= MIN_TOTAL_INDEPENDENT_DAYS
    )
    verdict['sample_gate'] = {
        'independent_oos_days': independent_days,
        'holdout_days': holdout_days,
        'total_independent_days': total_independent_days,
        'min_independent_oos_days': MIN_INDEPENDENT_OOS_DAYS,
        'min_holdout_days': MIN_HOLDOUT_DAYS,
        'min_total_independent_days': MIN_TOTAL_INDEPENDENT_DAYS,
        'passed': sample_gate_pass,
    }
    verdict['sufficient_to_lift_promotion_freeze'] = bool(
        verdict['close_to_close_independent_oos_positive']
        and verdict['close_to_close_holdout_positive']
        and sample_gate_pass
    )
    if not sample_gate_pass:
        verdict['freeze_reason'] = 'insufficient_independent_oos_sample'
        verdict['evidence_grade'] = 'short_sample_positive_reference'
    verdict['summary'] = (
        'close-to-close 独立 OOS 与独立 holdout 均转正且样本量达标，可考虑解除 promotion_freeze。'
        if verdict['sufficient_to_lift_promotion_freeze']
        else '独立 OOS / holdout 未同时满足收益与最小样本量门槛，promotion_freeze 仍需保留；短样本正向结果仅作 research reference。'
    )

    report = {
        'report_type': 'current_combo_stage72_independent_oos_holdout_validation',
        'generated_at': pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),
        'source_paths': {
            'reserve_partition': str(reserve_path),
            'replay_report': str(replay_report_path),
            'weight_map': str(weight_path),
            'factor_audit': str(audit_path),
            'panel': str(panel_path),
            'regime': str(regime_path),
        },
        'reserved_union_dates': reserved_union_dates,
        'evaluation_partitions': partitions,
        'verdict': verdict,
        'context': {
            'walkforward_reference': str(evolution / 'current_combo_walkforward_validation.json'),
            'nested_reference': str(evolution / 'current_combo_nested_validation.json'),
            'note': 'Stage72 将最近已标注 clean days 从 replay/backtest/nested/walkforward 中剥离，再单独做 close-to-close 独立 OOS / holdout 评估。',
        },
    }

    output_json = evolution / f'{args.output_stem}.json'
    output_md = evolution / f'{args.output_stem}.md'
    dump_json(output_json, report)
    output_md.write_text(build_markdown(report) + '\n', encoding='utf-8')
    print(json.dumps({
        'status': 'ok',
        'json': str(output_json),
        'md': str(output_md),
        'close_to_close_independent_oos_positive': verdict['close_to_close_independent_oos_positive'],
        'close_to_close_holdout_positive': verdict['close_to_close_holdout_positive'],
        'sufficient_to_lift_promotion_freeze': verdict['sufficient_to_lift_promotion_freeze'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
