#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import pandas as pd

from run_current_combo_replay import (
    LABEL_MODE_TO_COLUMN,
    build_comparisons,
    build_direction_consistency,
    collect_weight_map_factor_union,
    infer_label_mode,
    read_json,
    run_replay,
    zscore_by_date,
)

ROOT_DEFAULT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION_DIRNAME = 'outputs/evolution'
TZ = ZoneInfo('Asia/Shanghai')
TOP_LEVEL_REPORT = 'analog_validation_report.json'
HORIZON_TO_DAYS = {
    '5d': 5,
    '20d': 20,
    '60d': 60,
}
SERIES_ORDER = [
    'close_to_close_combo',
    'close_to_close_base',
    'open_to_close_combo',
    'open_to_close_base',
    'close_to_open_combo',
    'close_to_open_base',
]
REPORT_STEM_BY_MODE = {
    'close_to_close': 'current_combo_analog_replay_report',
    'open_to_close': 'current_combo_analog_replay_report.o2c_canonical',
    'close_to_open': 'current_combo_analog_replay_report.c2o_canonical',
}
WEIGHT_PATH_BY_MODE = {
    'close_to_close': 'outputs/weight_maps/regime_weight_map_phase1.json',
    'open_to_close': 'outputs/weight_maps/regime_weight_map_phase1.o2c_full45_33d.json',
    'close_to_open': 'outputs/weight_maps/regime_weight_map_phase1.c2o_full45_33d.json',
}
AUDIT_PATH_BY_MODE = {
    'close_to_close': 'outputs/audit_reports/factor_audit_phase1.json',
    'open_to_close': 'outputs/audit_reports/factor_audit_phase1.o2c_full45_33d.json',
    'close_to_open': 'outputs/audit_reports/factor_audit_phase1.c2o_full45_33d.json',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Generate return-level analog replay for current_combo canonical weight maps')
    parser.add_argument('--root', default=str(ROOT_DEFAULT))
    parser.add_argument('--analog-windows-path', default=None)
    parser.add_argument('--label-mode', action='append', choices=sorted(LABEL_MODE_TO_COLUMN), help='Repeat to limit run to selected label modes. Default: all three label modes.')
    return parser.parse_args()


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def iso_mtime(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None


def refresh_source_mtimes(payload: dict) -> dict:
    source_mtimes = payload.setdefault('source_mtimes', {})
    for key, raw_path in (payload.get('source_paths', {}) or {}).items():
        if isinstance(raw_path, dict):
            continue
        try:
            path = Path(raw_path).expanduser()
        except TypeError:
            continue
        if path.exists():
            source_mtimes[key] = iso_mtime(path)
    return source_mtimes


def fmt_pct(value: float | None) -> str:
    if value is None:
        return 'NA'
    return f'{float(value):.4%}'


def direction_of(value: float | None) -> str:
    if value is None:
        return 'na'
    if value > 1e-12:
        return 'positive'
    if value < -1e-12:
        return 'negative'
    return 'flat'


def stat_block(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            'sample_count': 0,
            'mean': None,
            'median': None,
            'positive_ratio': None,
            'min': None,
            'max': None,
        }
    values = [float(v) for v in values]
    return {
        'sample_count': len(values),
        'mean': float(sum(values) / len(values)),
        'median': float(median(values)),
        'positive_ratio': float(sum(v > 0 for v in values) / len(values)),
        'min': float(min(values)),
        'max': float(max(values)),
    }


def enrich_stat_block(values: list[float]) -> dict[str, float | int | None]:
    stats = stat_block(values)
    positive_ratio = stats.get('positive_ratio')
    stats['non_positive_ratio'] = (
        round(1.0 - float(positive_ratio), 4)
        if positive_ratio is not None
        else None
    )
    return stats


def effective_factor_proxy_ids(window: dict) -> list[str]:
    return [
        str(item.get('factor_id'))
        for item in (window.get('effective_factor_proxies') or [])
        if item.get('factor_id')
    ]


def summarise_top_similarity_window(window: dict) -> dict:
    similar_reasons = window.get('similar_reasons') or {}
    return {
        'trade_date': window.get('trade_date'),
        'window_start': window.get('window_start'),
        'window_end': window.get('window_end'),
        'similarity_score': window.get('similarity_score'),
        'distance': window.get('distance'),
        'regime_id': window.get('regime_id'),
        'regime_confidence': window.get('regime_confidence'),
        'same_primary_regime': bool(similar_reasons.get('same_primary_regime')),
        'same_style_regime': bool(similar_reasons.get('same_style_regime')),
        'subsequent_market_proxy_returns': window.get('subsequent_market_proxy_returns', {}),
        'effective_factor_proxy_ids': effective_factor_proxy_ids(window),
    }


def build_market_proxy_forward_summary(windows: list[dict]) -> dict[str, dict[str, float | int | None]]:
    summary: dict[str, dict[str, float | int | None]] = {}
    for horizon in HORIZON_TO_DAYS:
        values = [
            float(window['subsequent_market_proxy_returns'][horizon])
            for window in windows
            if (window.get('subsequent_market_proxy_returns') or {}).get(horizon) is not None
        ]
        summary[horizon] = enrich_stat_block(values)
    return summary


def build_directionality_hint(
    all_windows_summary: dict[str, dict[str, float | int | None]],
    same_primary_summary: dict[str, dict[str, float | int | None]],
) -> dict[str, dict]:
    hints: dict[str, dict] = {}
    for horizon in HORIZON_TO_DAYS:
        all_stats = all_windows_summary.get(horizon, {})
        same_stats = same_primary_summary.get(horizon, {})
        effective_stats = same_stats if (same_stats.get('sample_count') or 0) > 0 else all_stats
        sample_count = int(effective_stats.get('sample_count') or 0)
        mean = effective_stats.get('mean')
        positive_ratio = effective_stats.get('positive_ratio')
        if sample_count == 0 or mean is None or positive_ratio is None:
            assessment = 'insufficient_samples'
            hint = f'{horizon} analog 样本不足，不能给出稳定方向提示。'
        elif positive_ratio >= 0.7 and mean > 0:
            assessment = 'positive_bias'
            hint = f'{horizon} analog 偏正向，但仍只是类比提示，不是独立 OOS。'
        elif positive_ratio <= 0.4 and mean <= 0:
            assessment = 'neutral_to_weak'
            hint = f'{horizon} analog 偏中性到偏弱，不能把类比均值误读为确定性。'
        else:
            assessment = 'mixed_or_divergent'
            hint = f'{horizon} analog 分歧较大，只能提供情境参考，不能作为方向放行依据。'
        hints[horizon] = {
            'horizon': horizon,
            'assessment': assessment,
            'hint': hint,
            'all_windows': all_stats,
            'same_primary_only': same_stats,
        }
    return hints


def build_analog_sample_summary(analog_windows: list[dict]) -> dict:
    regime_distribution: dict[str, int] = {}
    same_primary_windows = [
        window
        for window in analog_windows
        if bool((window.get('similar_reasons') or {}).get('same_primary_regime'))
    ]
    same_style_windows = [
        window
        for window in analog_windows
        if bool((window.get('similar_reasons') or {}).get('same_style_regime'))
    ]
    for window in analog_windows:
        regime_id = str(window.get('regime_id') or 'unknown')
        regime_distribution[regime_id] = regime_distribution.get(regime_id, 0) + 1
    all_summary = build_market_proxy_forward_summary(analog_windows)
    same_primary_summary = build_market_proxy_forward_summary(same_primary_windows)
    directionality_hint = build_directionality_hint(all_summary, same_primary_summary)
    return {
        'analog_window_count': len(analog_windows),
        'same_primary_regime_count': len(same_primary_windows),
        'same_style_regime_count': len(same_style_windows),
        'regime_distribution': regime_distribution,
        'top_similarity_windows': [summarise_top_similarity_window(window) for window in analog_windows[:5]],
        'forward_market_proxy_summary_all_windows': all_summary,
        'forward_market_proxy_summary_same_primary_only': same_primary_summary,
        'directionality_hint': directionality_hint,
        'interpretation': (
            '当前 analog 仍只是 market-proxy 情境提示；'
            '其中 5d/20d/60d 的方向感应以 top similarity windows 的 subsequent_market_proxy_returns 为准，'
            '不能替代组合收益级 replay、独立 holdout 或独立 OOS。'
        ),
    }


def build_current_regime_union_overlap(
    analog_windows: list[dict],
    latest_weight_plan_factor_ids: list[str],
    regime_union_factor_ids: list[str],
) -> dict:
    factor_ids = list(dict.fromkeys([*latest_weight_plan_factor_ids, *regime_union_factor_ids]))
    freq = {factor_id: 0 for factor_id in factor_ids}
    for window in analog_windows:
        for factor_id in set(effective_factor_proxy_ids(window)):
            if factor_id in freq:
                freq[factor_id] += 1
    overlap_factor_ids = [factor_id for factor_id in latest_weight_plan_factor_ids if freq.get(factor_id, 0) > 0]
    current_only_factor_ids = [factor_id for factor_id in regime_union_factor_ids if factor_id not in latest_weight_plan_factor_ids]
    union_only_no_window_coverage_factor_ids = [factor_id for factor_id in current_only_factor_ids if freq.get(factor_id, 0) == 0]
    fully_covered = all(freq.get(factor_id, 0) > 0 for factor_id in latest_weight_plan_factor_ids)
    return {
        'latest_weight_plan_factor_ids': latest_weight_plan_factor_ids,
        'current_regime_union_factor_ids': regime_union_factor_ids,
        'analog_effective_factor_proxy_frequency': freq,
        'overlap_factor_ids': overlap_factor_ids,
        'current_only_factor_ids': current_only_factor_ids,
        'proxy_only_factor_ids': union_only_no_window_coverage_factor_ids,
        'coverage_assessment': (
            f'latest weight plan {len(overlap_factor_ids)}/{len(latest_weight_plan_factor_ids)} 已被 '
            f'{len(analog_windows)}/{len(analog_windows)} analog windows 覆盖；'
            + (
                f'union-only 且 0/{len(analog_windows)} windows 命中的因子={union_only_no_window_coverage_factor_ids}。'
                if union_only_no_window_coverage_factor_ids
                else '未发现 union-only 但完全无窗口覆盖的因子。'
            )
        ),
        'union_only_no_window_coverage_factor_ids': union_only_no_window_coverage_factor_ids,
        'latest_weight_plan_fully_covered': fully_covered,
    }


def build_latest_weight_plan_window_alignment(
    analog_windows: list[dict],
    latest_weight_plan_factor_ids: list[str],
    regime_union_factor_ids: list[str],
) -> dict:
    window_matrix = []
    fully_aligned_window_count = 0
    same_primary_full_alignment_count = 0
    same_style_full_alignment_count = 0
    for window in analog_windows:
        effective_ids = effective_factor_proxy_ids(window)
        similar_reasons = window.get('similar_reasons') or {}
        missing_latest = [factor_id for factor_id in latest_weight_plan_factor_ids if factor_id not in effective_ids]
        missing_regime_union = [factor_id for factor_id in regime_union_factor_ids if factor_id not in effective_ids]
        latest_weight_plan_fully_covered = len(missing_latest) == 0
        same_primary = bool(similar_reasons.get('same_primary_regime'))
        same_style = bool(similar_reasons.get('same_style_regime'))
        if latest_weight_plan_fully_covered:
            fully_aligned_window_count += 1
            if same_primary:
                same_primary_full_alignment_count += 1
            if same_style:
                same_style_full_alignment_count += 1
        window_matrix.append({
            'trade_date': window.get('trade_date'),
            'window_start': window.get('window_start'),
            'window_end': window.get('window_end'),
            'regime_id': window.get('regime_id'),
            'regime_confidence': window.get('regime_confidence'),
            'similarity_score': window.get('similarity_score'),
            'distance': window.get('distance'),
            'same_primary_regime': same_primary,
            'same_style_regime': same_style,
            'subsequent_market_proxy_returns': window.get('subsequent_market_proxy_returns', {}),
            'effective_factor_proxy_ids': effective_ids,
            'latest_weight_plan_overlap_factor_ids': [factor_id for factor_id in latest_weight_plan_factor_ids if factor_id in effective_ids],
            'missing_latest_weight_plan_factor_ids': missing_latest,
            'regime_union_overlap_factor_ids': [factor_id for factor_id in regime_union_factor_ids if factor_id in effective_ids],
            'missing_regime_union_factor_ids': missing_regime_union,
            'latest_weight_plan_fully_covered': latest_weight_plan_fully_covered,
            'f_price_vs_ma20_proxy_only': 'f_price_vs_ma20' in missing_regime_union,
        })
    return {
        'latest_weight_plan_factor_ids': latest_weight_plan_factor_ids,
        'regime_union_factor_ids': regime_union_factor_ids,
        'proxy_window_count': len(analog_windows),
        'fully_aligned_window_count': fully_aligned_window_count,
        'same_primary_full_alignment_count': same_primary_full_alignment_count,
        'same_style_full_alignment_count': same_style_full_alignment_count,
        'window_matrix': window_matrix,
    }


def build_panel(root: Path, factor_union: list[str]) -> pd.DataFrame:
    panel_path = root / 'data' / 'factors' / 'panel_phase1.parquet'
    regime_path = root / 'data' / 'regimes' / 'regime_labels_phase1.parquet'
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
    regime = pd.read_parquet(regime_path, columns=['trade_date', 'regime_id'])
    panel['trade_date'] = panel['trade_date'].astype(str)
    regime['trade_date'] = regime['trade_date'].astype(str)
    replay_panel = panel.merge(regime, on='trade_date', how='inner')
    replay_panel['base_factor_score_norm_z'] = zscore_by_date(replay_panel, 'base_factor_score_norm')
    for factor_id in factor_union:
        replay_panel[f'{factor_id}_z'] = zscore_by_date(replay_panel, factor_id)
    return replay_panel


def build_series_payload(results: dict[str, dict], ordered_dates: list[str]) -> dict[str, dict]:
    payload: dict[str, dict] = {}
    for series_name, series_data in results.items():
        effective_dates = list(series_data.get('effective_trade_dates', []))
        payload[series_name] = {
            'metrics': series_data.get('metrics', {}),
            'effective_trade_dates': effective_dates,
            'excluded_dates': [d for d in ordered_dates if d not in set(effective_dates)],
            'daily_records': series_data.get('daily_records', []),
        }
    return payload


def build_horizon_payload(
    replay_panel: pd.DataFrame,
    trade_dates: list[str],
    trade_index: dict[str, int],
    anchor_trade_date: str,
    regime_map: dict,
    factor_signs: dict[str, float],
    label_mode: str,
) -> dict[str, dict | None]:
    label_specs = [
        ('close_to_close_combo', 'next_return_close_to_close_1d', 'combo_score'),
        ('close_to_close_base', 'next_return_close_to_close_1d', 'base_score_only'),
        ('open_to_close_combo', 'next_return_open_to_close_1d', 'combo_score'),
        ('open_to_close_base', 'next_return_open_to_close_1d', 'base_score_only'),
        ('close_to_open_combo', 'next_return_close_to_open_1d', 'combo_score'),
        ('close_to_open_base', 'next_return_close_to_open_1d', 'base_score_only'),
    ]
    payloads: dict[str, dict | None] = {}
    if anchor_trade_date not in trade_index:
        return {name: None for name in HORIZON_TO_DAYS}
    start_idx = trade_index[anchor_trade_date]
    for horizon_name, horizon_days in HORIZON_TO_DAYS.items():
        future_dates = trade_dates[start_idx:start_idx + horizon_days]
        if len(future_dates) < horizon_days:
            payloads[horizon_name] = None
            continue
        subset = replay_panel[replay_panel['trade_date'].isin(future_dates)].copy()
        replay_results = run_replay(subset, regime_map, factor_signs, label_specs)
        comparisons = build_comparisons(replay_results)
        direction = build_direction_consistency(comparisons, replay_results, label_mode)
        payloads[horizon_name] = {
            'requested_trade_days': horizon_days,
            'series': build_series_payload(replay_results, future_dates),
            'comparisons': comparisons,
            'direction_consistency': direction,
            'effective_trade_days_by_series': {
                key: replay_results[key]['metrics']['days'] for key in replay_results
            },
        }
    return payloads


def aggregate_windows(windows: list[dict]) -> dict[str, dict]:
    aggregate: dict[str, dict] = {}
    for horizon_name in HORIZON_TO_DAYS:
        horizon_bucket: dict[str, list[float]] = {
            f'{mode}_combo_total_return': [] for mode in LABEL_MODE_TO_COLUMN
        }
        horizon_bucket.update({
            f'{mode}_base_total_return': [] for mode in LABEL_MODE_TO_COLUMN
        })
        horizon_bucket.update({
            f'{mode}_excess_total_return': [] for mode in LABEL_MODE_TO_COLUMN
        })
        for window in windows:
            horizon_payload = (window.get('subsequent_portfolio_returns') or {}).get(horizon_name)
            if not horizon_payload:
                continue
            series = horizon_payload.get('series', {})
            comparisons = horizon_payload.get('comparisons', {})
            for mode in LABEL_MODE_TO_COLUMN:
                combo_key = f'{mode}_combo'
                base_key = f'{mode}_base'
                combo_total = ((series.get(combo_key) or {}).get('metrics') or {}).get('total_return')
                base_total = ((series.get(base_key) or {}).get('metrics') or {}).get('total_return')
                excess_total = comparisons.get(f'{mode}_excess_total_return')
                if combo_total is not None:
                    horizon_bucket[f'{mode}_combo_total_return'].append(float(combo_total))
                if base_total is not None:
                    horizon_bucket[f'{mode}_base_total_return'].append(float(base_total))
                if excess_total is not None:
                    horizon_bucket[f'{mode}_excess_total_return'].append(float(excess_total))
        aggregate[horizon_name] = {
            key: stat_block(values)
            for key, values in horizon_bucket.items()
        }
    return aggregate


def build_mode_markdown(report: dict) -> str:
    by_horizon = report.get('aggregate_portfolio_return_summary', {})
    label_mode = str(report['label_mode'])
    combo_key = f'{label_mode}_combo_total_return'
    base_key = f'{label_mode}_base_total_return'
    excess_key = f'{label_mode}_excess_total_return'
    lines = [
        '# Current Combo Analog Replay Report',
        '',
        f"- 生成时间：{report['generated_at']}",
        f"- label_mode：{label_mode}",
        f"- latest_weight_plan_factor_ids={report['latest_weight_plan_factor_ids']}",
        f"- analog windows={report['window_count']}",
        f"- 结论：{report['summary']['minimal_credible_conclusion']}",
        '',
        '## 按 horizon 汇总（窗口级 total_return 分布）',
    ]
    for horizon_name in HORIZON_TO_DAYS:
        agg = by_horizon.get(horizon_name, {})
        lines.append(
            f"- {horizon_name}: combo(primary)={fmt_pct((agg.get(combo_key) or {}).get('mean'))}, "
            f"base(primary)={fmt_pct((agg.get(base_key) or {}).get('mean'))}, "
            f"excess(primary)={fmt_pct((agg.get(excess_key) or {}).get('mean'))}, "
            f"sample_count={(agg.get(combo_key) or {}).get('sample_count', 0)}"
        )
    lines.extend([
        '',
        '## 关键 caveat',
        *[f'- {item}' for item in report.get('summary', {}).get('key_caveats', [])],
        '',
    ])
    return '\n'.join(lines)


def update_analog_validation_report(
    analog_report_path: Path,
    analog_windows_path: Path,
    mode_reports: dict[str, dict],
    mode_paths: dict[str, Path],
) -> dict:
    now = iso_now()
    analog_report = read_json(analog_report_path) if analog_report_path.exists() else {}
    evolution_dir = analog_report_path.parent
    fingerprint_path = evolution_dir / 'current_market_fingerprint.json'
    claim_limit_path = evolution_dir / 'current_combo_claim_limit_audit.json'
    holdout_reserve_path = evolution_dir / 'current_combo_holdout_reserve.json'
    comparison_path = evolution_dir / 'multi_label_canonical_comparison.json'
    current_market = read_json(fingerprint_path) if fingerprint_path.exists() else {}
    claim_limit = read_json(claim_limit_path) if claim_limit_path.exists() else {}
    holdout_reserve = read_json(holdout_reserve_path) if holdout_reserve_path.exists() else {}
    comparison = read_json(comparison_path) if comparison_path.exists() else {}
    prev_generated_at = analog_report.get('generated_at')
    analog_windows_payload = read_json(analog_windows_path) if analog_windows_path.exists() else {}
    analog_windows = list(analog_windows_payload.get('analog_windows', []) or [])
    window_count_min = min((report.get('window_count', 0) for report in mode_reports.values()), default=0)
    completed_modes = list(mode_reports.keys())
    completed_modes_text = ' / '.join(completed_modes) if completed_modes else '无'
    comparison_labels = comparison.get('labels', {}) if isinstance(comparison, dict) else {}
    c2c_comparison = comparison_labels.get('close_to_close', {}) if isinstance(comparison_labels, dict) else {}
    latest_overlap = comparison.get('latest_weight_plan_overlap', {}) if isinstance(comparison, dict) else {}
    common_latest_factor_ids = latest_overlap.get('common_factor_ids', []) if isinstance(latest_overlap, dict) else []
    sign_unstable_factor_ids = comparison.get('sign_unstable_factor_ids', []) if isinstance(comparison, dict) else []
    latest_weight_plan_factor_ids = c2c_comparison.get('latest_weight_plan_factor_ids') or mode_reports.get('close_to_close', {}).get('latest_weight_plan_factor_ids', [])
    regime_union_factor_ids = (mode_reports.get('close_to_close', {}) or {}).get('regime_union_factor_ids', [])
    analog_sample_summary = build_analog_sample_summary(analog_windows)
    current_regime_union_overlap = build_current_regime_union_overlap(
        analog_windows,
        latest_weight_plan_factor_ids,
        regime_union_factor_ids,
    )
    latest_weight_plan_window_alignment = build_latest_weight_plan_window_alignment(
        analog_windows,
        latest_weight_plan_factor_ids,
        regime_union_factor_ids,
    )
    claim_scope = claim_limit.get('label_mode_claim_scope', {}) if isinstance(claim_limit, dict) else {}
    holdout_counts = [
        int(item.get('remaining_labeled_holdout_days_count'))
        for item in claim_scope.values()
        if isinstance(item, dict) and item.get('remaining_labeled_holdout_days_count') is not None
    ]
    current_labeled_holdout_count = holdout_reserve.get('current_labeled_holdout_days_count')
    if current_labeled_holdout_count is None and holdout_counts:
        current_labeled_holdout_count = min(holdout_counts)
    reserve_dates = [str(x) for x in (holdout_reserve.get('reserved_holdout_dates') or []) if x]
    reserve_now_labeled = [str(x) for x in (holdout_reserve.get('reserved_holdout_dates_now_labeled') or []) if x]
    reserve_now_labeled_text = ' / '.join(reserve_now_labeled) if reserve_now_labeled else '无'
    holdout_phrase = (
        f'当前 labeled holdout={current_labeled_holdout_count}'
        if current_labeled_holdout_count is not None
        else '当前 labeled holdout=未知'
    )
    if reserve_dates:
        holdout_phrase += f"；holdout reserve={' / '.join(reserve_dates)}"
    if reserve_now_labeled:
        holdout_phrase += f'；reserved and now labeled holdout={reserve_now_labeled_text}'
    current_section = {
        'generated_at': now,
        'completed_label_modes': completed_modes,
        'all_label_modes_present': sorted(completed_modes) == sorted(LABEL_MODE_TO_COLUMN.keys()),
        'usable_portfolio_replay_sample_count': window_count_min,
        'report_paths': {mode: str(path) for mode, path in mode_paths.items()},
        'by_label_mode': {
            mode: {
                'generated_at': report.get('generated_at'),
                'label_mode': mode,
                'label_column': report.get('label_column'),
                'selection_mode': report.get('selection_mode'),
                'latest_weight_plan_factor_ids': report.get('latest_weight_plan_factor_ids', []),
                'window_count': report.get('window_count', 0),
                'aggregate_portfolio_return_summary': report.get('aggregate_portfolio_return_summary', {}),
                'minimal_credible_conclusion': report.get('summary', {}).get('minimal_credible_conclusion'),
                'report_path': str(mode_paths[mode]),
            }
            for mode, report in mode_reports.items()
        },
    }
    analog_report['schema_version'] = '2026-04-20.analog_validation_report.v5'
    analog_report['generated_at'] = now
    analog_report['supersedes_report_generated_at'] = prev_generated_at
    analog_report['status'] = 'current_combo_return_level_analog_replay_present_but_not_independent_oos'
    analog_report['rewrite_reason'] = [
        '旧 analog report 已停留在 market-proxy-only 口径，且 latest_selected_factor_ids 与当前 canonical 权重图不再一致。',
        '最新 regime_analog_windows.json 已按 current_market_fingerprint 对齐，effective factor proxies 已切到当前 latest weight plan / regime union 口径。',
        f'本次重写明确声明：该报告已升级为 current_combo 的 {completed_modes_text} 收益级 analog replay，但仍不是独立 holdout / 独立 OOS。',
        f'补充 latest weight plan 与 return-level analog replay 明细：当前可复用窗口数={window_count_min}，并回写各 label_mode replay artifact 路径。',
        '保留 market-proxy 选窗层作为情境相似度依据，但把组合收益层改为 frozen-current-plan 的历史窗口 replay，避免把 proxy 选窗误读成 portfolio-return 缺失。',
    ]
    analog_report.setdefault('source_paths', {})['regime_analog_windows'] = str(analog_windows_path)
    analog_report['source_paths']['current_combo_analog_replay_reports'] = {mode: str(path) for mode, path in mode_paths.items()}
    analog_report['source_paths']['current_combo_claim_limit_audit'] = str(claim_limit_path)
    analog_report['source_paths']['current_combo_holdout_reserve'] = str(holdout_reserve_path)
    analog_report['source_paths']['multi_label_canonical_comparison'] = str(comparison_path)
    refresh_source_mtimes(analog_report)
    analog_report.setdefault('source_mtimes', {})['regime_analog_windows'] = iso_mtime(analog_windows_path)
    analog_report['source_mtimes']['current_combo_analog_replay_reports'] = {mode: iso_mtime(path) for mode, path in mode_paths.items()}
    analog_report['source_mtimes']['current_combo_claim_limit_audit'] = iso_mtime(claim_limit_path)
    analog_report['source_mtimes']['current_combo_holdout_reserve'] = iso_mtime(holdout_reserve_path)
    analog_report['source_mtimes']['multi_label_canonical_comparison'] = iso_mtime(comparison_path)
    analog_report['current_combo_return_level_analog_replay'] = current_section
    trend_state = current_market.get('trend_state', {}) if isinstance(current_market, dict) else {}
    broad_market_proxy = trend_state.get('broad_market_proxy', {}) if isinstance(trend_state, dict) else {}
    market_regime_summary = current_market.get('market_regime_summary', {}) if isinstance(current_market, dict) else {}
    data_status = current_market.get('data_status', {}) if isinstance(current_market, dict) else {}
    analog_report['current_market_proxy_context'] = {
        'trade_date': current_market.get('trade_date'),
        'current_proxy_regime': market_regime_summary.get('current_proxy_regime'),
        'current_proxy_regime_confidence': market_regime_summary.get('current_proxy_regime_confidence'),
        'market_state_quality_ok': data_status.get('market_state_quality_ok'),
        'market_state_quality_errors': data_status.get('market_state_quality_errors', []),
        'broad_market_proxy_returns': {
            '5d': broad_market_proxy.get('return_5d'),
            '20d': broad_market_proxy.get('return_20d'),
            '60d': broad_market_proxy.get('return_60d'),
            '120d': broad_market_proxy.get('return_120d'),
        },
        'breadth_ratio': broad_market_proxy.get('breadth_ratio'),
        'is_high_volume_state': broad_market_proxy.get('is_high_volume_state'),
        'morning_brief_regime': trend_state.get('morning_brief_regime'),
        'focus_sectors': current_market.get('style_and_sector', {}).get('focus_sectors', []),
    }
    analog_report['analog_sample_summary'] = analog_sample_summary
    analog_report['current_regime_union_overlap'] = current_regime_union_overlap
    analog_report['latest_weight_plan_window_alignment'] = latest_weight_plan_window_alignment
    analog_report['canonical_refresh_basis'] = {
        'fingerprint_generated_at': current_market.get('generated_at'),
        'analog_windows_generated_at': analog_windows_payload.get('generated_at'),
        'audit_mtime': c2c_comparison.get('audit_mtime') or (mode_reports.get('close_to_close', {}).get('source_mtimes') or {}).get('factor_audit'),
        'weight_mtime': c2c_comparison.get('weight_mtime') or (mode_reports.get('close_to_close', {}).get('source_mtimes') or {}).get('weight_map'),
        'latest_selection_mode': mode_reports.get('close_to_close', {}).get('selection_mode'),
        'latest_regime_from_weight_map': c2c_comparison.get('latest_regime'),
        'latest_weight_plan_factor_ids': latest_weight_plan_factor_ids,
        'latest_regime_union_factor_ids': regime_union_factor_ids,
        'benchmark_eligible_trade_days': c2c_comparison.get('benchmark_eligible_trade_days'),
        'promotion_freeze': True,
    }
    validation_scope = analog_report.setdefault('validation_scope', {})
    validation_scope['report_is_market_proxy_analog_not_portfolio_replay'] = False
    validation_scope['portfolio_return_replay_included'] = True
    validation_scope['current_support_pool_plus_fallback_portfolio_backtest_completed'] = True
    validation_scope['independent_oos_completed'] = False
    validation_scope['analog_validation_completed_flag'] = True
    validation_scope['formal_current_combo_backtest_present'] = True
    validation_scope['alternate_canonical_portfolio_replay_included'] = True
    validation_scope['official_vs_alternate_scope_note'] = '当前 analog 已升级为 current_combo 的 c2c / o2c / c2o 三口径 frozen-current-plan 收益级 replay；但它仍是类比验证，不是独立 holdout / 独立 OOS。'
    validation_scope['claim_limit'] = '当前 report 已提供 c2c / o2c / c2o 三口径收益级 analog replay，可作为组合收益层补充验证；但它仍不是独立 labeled holdout 或独立 OOS。'
    multi_label_context = analog_report.setdefault('multi_label_canonical_context', {})
    multi_label_context['coverage_limit_note'] = (
        'analog windows 仍使用 market-proxy similarity 做历史选窗；'
        f'但当前报告已补出 {completed_modes_text} 的 portfolio-return replay。'
        '它仍不提供独立 OOS、真实历史权重重建或真实交易约束回放。'
    )
    validation_status = analog_report.setdefault('validation_status', {})
    validation_status['analog_validation_completed'] = True
    validation_status['independent_validation_completed'] = False
    validation_status['usable_portfolio_replay_sample_count'] = window_count_min
    validation_status['proxy_analog_window_count'] = len(analog_windows)
    validation_status['decision_grade'] = 'hold_return_level_replay_present_no_independent_oos'
    decision_impact = analog_report.setdefault('decision_impact', {})
    decision_impact['directional_takeaway'] = {
        horizon: payload['hint']
        for horizon, payload in analog_sample_summary.get('directionality_hint', {}).items()
    }
    decision_impact['promotion_freeze_should_remain'] = True
    decision_impact['why_freeze_cannot_be_lifted'] = [
        f'虽然 analog 已升级为 c2c / o2c / c2o 三口径收益级 replay，但{holdout_phrase}；这仍不能被包装成独立 OOS。',
        '收益级 analog replay 是 frozen-current-plan 的历史类比验证，不是独立 labeled holdout、独立 OOS 或 end-to-end backtest。',
        f'latest_weight_plan 仍三向分歧，common_latest_weight_plan_factor_ids={common_latest_factor_ids or []}，sign_unstable_factor_ids={sign_unstable_factor_ids or []}，不能把 c2c 优势外推成跨口径稳定优势。',
    ]
    decision_impact['blocked_claims'] = [
        '不能宣称已经获得独立 OOS。',
        '不能据此解除 promotion freeze。',
        '不能把 close_to_close 的结果直接外推成 open_to_close / close_to_open 的可交易稳定优势。',
    ]
    risk_and_limitations = [
        line for line in analog_report.setdefault('risk_and_limitations', [])
        if 'holdout=0' not in str(line)
        and 'claim-limit 继续显示三口径 holdout=0' not in str(line)
        and 'top-10 analog 中' not in str(line)
    ]
    analog_report['risk_and_limitations'] = risk_and_limitations
    top_windows = analog_windows[:2]
    top_window_text = ' / '.join(
        f"{window.get('trade_date')}({window.get('regime_id')})"
        for window in top_windows
        if window.get('trade_date')
    ) or '无'
    extra_lines = [
        '收益级 analog replay 使用的是 frozen current/canonical weight map 与 full-sample sign，在历史 analog windows 上做 panel 标签离线重放；它不是历史窗口内真实当时组合的已实现收益归因。',
        '收益级 analog replay 仍不含涨跌停、成交量约束、冲击成本、真实撮合与组合容量约束。',
        '即便收益级 analog replay 已覆盖 c2c / o2c / c2o，它仍不能替代独立 labeled holdout、独立 OOS、end-to-end backtest 或 nested walk-forward。',
        f"top-{len(analog_windows)} analog 中有 {analog_sample_summary.get('same_primary_regime_count', 0)}/{len(analog_windows)} 与当前同 primary regime、{analog_sample_summary.get('same_style_regime_count', 0)}/{len(analog_windows)} 同 style；当前最相近窗口为 {top_window_text}。",
        f'claim-limit 当前已确认 {holdout_phrase}；nested / walkforward 虽已补齐三口径，但仍与同一 clean window 共样本，不能替代独立 holdout / 独立 OOS。',
    ]
    for line in extra_lines:
        if line not in risk_and_limitations:
            risk_and_limitations.append(line)
    evidence_path = analog_report.setdefault('evidence_path', [])
    for path in mode_paths.values():
        if str(path) not in evidence_path:
            evidence_path.append(str(path))
    dump_json(analog_report_path, analog_report)
    return analog_report


def mode_weight_audit_paths(root: Path, label_mode: str) -> tuple[Path, Path]:
    return root / WEIGHT_PATH_BY_MODE[label_mode], root / AUDIT_PATH_BY_MODE[label_mode]


def run_for_mode(root: Path, analog_windows_payload: dict, label_mode: str) -> tuple[dict, Path, Path]:
    evolution = root / EVOLUTION_DIRNAME
    weight_path, audit_path = mode_weight_audit_paths(root, label_mode)
    weights = read_json(weight_path)
    audit = read_json(audit_path)
    effective_label_mode = infer_label_mode(weights, audit, label_mode)
    factor_union = collect_weight_map_factor_union(weights)
    replay_panel = build_panel(root, factor_union)
    trade_dates = sorted(replay_panel['trade_date'].astype(str).unique())
    trade_index = {trade_date: idx for idx, trade_date in enumerate(trade_dates)}
    regime_map = {row['regime_id']: row for row in weights.get('regime_weight_map', [])}
    factor_signs = {
        row['factor_id']: (1.0 if float(row.get('rank_ic_mean', 0.0) or 0.0) >= 0 else -1.0)
        for row in audit.get('reports', [])
        if row.get('factor_id')
    }
    latest_weight_plan = weights.get('latest_weight_plan', {}) or {}
    latest_weight_plan_factor_ids = [
        str(item.get('factor_id')) for item in latest_weight_plan.get('selected_factors', []) if item.get('factor_id')
    ]
    base_factor_id = 'prebreakout_v41'
    base_weight = float(weights.get('base_weight', 0.4) or 0.4)
    windows_out: list[dict] = []
    for window in analog_windows_payload.get('analog_windows', []):
        anchor_trade_date = str(window.get('trade_date') or '')
        if anchor_trade_date not in trade_index:
            continue
        horizon_payloads = build_horizon_payload(
            replay_panel=replay_panel,
            trade_dates=trade_dates,
            trade_index=trade_index,
            anchor_trade_date=anchor_trade_date,
            regime_map=regime_map,
            factor_signs=factor_signs,
            label_mode=effective_label_mode,
        )
        windows_out.append({
            **window,
            'label_mode': effective_label_mode,
            'canonical_label_column': LABEL_MODE_TO_COLUMN[effective_label_mode],
            'selection_mode': weights.get('selection_mode'),
            'latest_weight_map_trade_date': weights.get('latest_regime', {}).get('trade_date'),
            'latest_weight_plan_regime': latest_weight_plan.get('regime_id'),
            'latest_weight_plan_factor_ids': latest_weight_plan_factor_ids,
            'regime_union_factor_ids': factor_union,
            'historical_weight_plan': {
                'label_mode': effective_label_mode,
                'base_factor_id': base_factor_id,
                'base_weight': base_weight,
                'selected_factors': latest_weight_plan.get('selected_factors', []),
                'regime_weight_map_source': str(weight_path),
                'factor_signs': {factor_id: factor_signs.get(factor_id, 1.0) for factor_id in factor_union},
            },
            'historical_weight_plan_factor_ids': latest_weight_plan_factor_ids,
            'subsequent_portfolio_returns': horizon_payloads,
        })
    aggregate_portfolio_return_summary = aggregate_windows(windows_out)
    report = {
        'report_type': 'current_combo_analog_replay_report',
        'generated_at': iso_now(),
        'schema_version': '2026-04-20.current_combo_analog_replay_report.v1',
        'label_mode': effective_label_mode,
        'label_column': LABEL_MODE_TO_COLUMN[effective_label_mode],
        'selection_mode': weights.get('selection_mode'),
        'source_paths': {
            'analog_windows': str((root / EVOLUTION_DIRNAME / 'regime_analog_windows.json').resolve()),
            'weight_map': str(weight_path),
            'factor_audit': str(audit_path),
            'panel': str((root / 'data' / 'factors' / 'panel_phase1.parquet').resolve()),
            'regime': str((root / 'data' / 'regimes' / 'regime_labels_phase1.parquet').resolve()),
        },
        'source_mtimes': {
            'analog_windows': iso_mtime(root / EVOLUTION_DIRNAME / 'regime_analog_windows.json'),
            'weight_map': iso_mtime(weight_path),
            'factor_audit': iso_mtime(audit_path),
            'panel': iso_mtime(root / 'data' / 'factors' / 'panel_phase1.parquet'),
            'regime': iso_mtime(root / 'data' / 'regimes' / 'regime_labels_phase1.parquet'),
        },
        'latest_weight_plan_factor_ids': latest_weight_plan_factor_ids,
        'regime_union_factor_ids': factor_union,
        'window_count': len(windows_out),
        'windows': windows_out,
        'aggregate_portfolio_return_summary': aggregate_portfolio_return_summary,
        'summary': {
            'current_combo_return_level_analog_replay_present': True,
            'sufficient_to_lift_promotion_freeze': False,
            'minimal_credible_conclusion': (
                f'当前 {effective_label_mode} canonical weight map 已在 {len(windows_out)} 个 analog windows 上补出收益级 replay；'
                f"primary 5d/20d/60d combo 窗口均值分别为 {fmt_pct((aggregate_portfolio_return_summary.get('5d', {}).get(f'{effective_label_mode}_combo_total_return') or {}).get('mean'))} / "
                f"{fmt_pct((aggregate_portfolio_return_summary.get('20d', {}).get(f'{effective_label_mode}_combo_total_return') or {}).get('mean'))} / "
                f"{fmt_pct((aggregate_portfolio_return_summary.get('60d', {}).get(f'{effective_label_mode}_combo_total_return') or {}).get('mean'))}；"
                '但这仍只是 frozen-current-plan 的历史类比收益验证，不是独立 holdout / 独立 OOS，不能解除 promotion freeze。'
            ),
            'key_caveats': [
                '收益级 analog replay 使用的是当前 canonical weight map 与 full-sample factor sign，在历史 analog windows 上做离线重放。',
                '收益级 analog replay 仍不含涨跌停、成交量约束、冲击成本、真实撮合与容量约束。',
                '收益级 analog replay 不能替代独立 labeled holdout、独立 OOS、end-to-end backtest 或 nested walk-forward。',
            ],
        },
    }
    report_path = evolution / f"{REPORT_STEM_BY_MODE[effective_label_mode]}.json"
    md_path = evolution / f"{REPORT_STEM_BY_MODE[effective_label_mode]}.md"
    dump_json(report_path, report)
    md_path.write_text(build_mode_markdown(report), encoding='utf-8')
    return report, report_path, md_path


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    evolution = root / EVOLUTION_DIRNAME
    evolution.mkdir(parents=True, exist_ok=True)
    analog_windows_path = Path(args.analog_windows_path).expanduser().resolve() if args.analog_windows_path else evolution / 'regime_analog_windows.json'
    analog_windows_payload = read_json(analog_windows_path)
    modes = args.label_mode or list(LABEL_MODE_TO_COLUMN.keys())

    mode_reports: dict[str, dict] = {}
    mode_paths: dict[str, Path] = {}
    md_paths: dict[str, Path] = {}
    for label_mode in modes:
        report, report_path, md_path = run_for_mode(root, analog_windows_payload, label_mode)
        mode_reports[label_mode] = report
        mode_paths[label_mode] = report_path
        md_paths[label_mode] = md_path

    analog_report = update_analog_validation_report(
        analog_report_path=evolution / TOP_LEVEL_REPORT,
        analog_windows_path=analog_windows_path,
        mode_reports=mode_reports,
        mode_paths=mode_paths,
    )

    print(json.dumps({
        'status': 'ok',
        'analog_validation_report': str(evolution / TOP_LEVEL_REPORT),
        'reports': {mode: str(path) for mode, path in mode_paths.items()},
        'markdown_reports': {mode: str(path) for mode, path in md_paths.items()},
        'completed_label_modes': list(mode_reports.keys()),
        'portfolio_return_replay_included': analog_report.get('validation_scope', {}).get('portfolio_return_replay_included'),
        'usable_portfolio_replay_sample_count': analog_report.get('validation_status', {}).get('usable_portfolio_replay_sample_count'),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
