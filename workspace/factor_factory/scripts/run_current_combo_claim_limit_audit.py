#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
REPLAY_PATH = EVOLUTION / 'current_combo_replay_report.json'
FORMAL_PATH = EVOLUTION / 'current_combo_portfolio_backtest.json'
O2C_REPLAY_PATH = EVOLUTION / 'current_combo_replay_report.o2c_canonical.json'
O2C_FORMAL_PATH = EVOLUTION / 'current_combo_portfolio_backtest.o2c_canonical.json'
C2O_REPLAY_PATH = EVOLUTION / 'current_combo_replay_report.c2o_canonical.json'
C2O_FORMAL_PATH = EVOLUTION / 'current_combo_portfolio_backtest.c2o_canonical.json'
PORTFOLIO_REPORT_PATH = EVOLUTION / 'portfolio_backtest_report.json'
ANALOG_WINDOWS_PATH = EVOLUTION / 'regime_analog_windows.json'
ANALOG_REPORT_PATH = EVOLUTION / 'analog_validation_report.json'
WEIGHT_MAP_PATH = ROOT / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json'
FACTOR_AUDIT_PATH = ROOT / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json'
PANEL_PATH = ROOT / 'data' / 'factors' / 'panel_phase1.parquet'
PHASE_PATH = ROOT / 'config' / 'phase1.yaml'
OUTPUT_JSON = EVOLUTION / 'current_combo_claim_limit_audit.json'
OUTPUT_MD = EVOLUTION / 'current_combo_claim_limit_audit.md'
TZ = ZoneInfo('Asia/Shanghai')

LABEL_MODE_TO_COLUMN = {
    'close_to_close': 'next_return_close_to_close_1d',
    'open_to_close': 'next_return_open_to_close_1d',
    'close_to_open': 'next_return_close_to_open_1d',
}
LABEL_MODE_TO_REPLAY_PATH = {
    'close_to_close': REPLAY_PATH,
    'open_to_close': O2C_REPLAY_PATH,
    'close_to_open': C2O_REPLAY_PATH,
}
LABEL_MODE_TO_FORMAL_PATH = {
    'close_to_close': FORMAL_PATH,
    'open_to_close': O2C_FORMAL_PATH,
    'close_to_open': C2O_FORMAL_PATH,
}
LABEL_MODE_TO_SERIES_KEY = {
    label_mode: f'{label_mode}_combo'
    for label_mode in LABEL_MODE_TO_COLUMN
}
LABEL_MODE_TO_FORMAL_METRIC_KEY = {
    'close_to_close': 'close_to_close_combo',
    'open_to_close': 'open_to_close_combo',
    'close_to_open': 'close_to_open_combo',
}
LABEL_MODE_TO_EXCESS_KEY = {
    'close_to_close': 'close_to_close_excess_total_return',
    'open_to_close': 'open_to_close_excess_total_return',
    'close_to_open': 'close_to_open_excess_total_return',
}
PRIMARY_LABEL_MODE = 'close_to_close'
LABEL_MODE_TO_SHORT = {
    'close_to_close': 'c2c',
    'open_to_close': 'o2c',
    'close_to_open': 'c2o',
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def fmt_pct(x: float) -> str:
    return f'{x:.4%}'


def fmt_ratio(num: int, den: int) -> str:
    if den <= 0:
        return '0/0'
    return f'{num}/{den} ({num / den:.2%})'


def direction_of(value: float) -> str:
    eps = 1e-12
    if value > eps:
        return 'positive'
    if value < -eps:
        return 'negative'
    return 'flat'


def to_ts(value: str) -> pd.Timestamp:
    return pd.to_datetime(str(value), format='%Y%m%d', errors='coerce')


def build_monthly_splits(panel_dates: pd.Series, phase_cfg: dict) -> list[dict[str, pd.Timestamp]]:
    dates = pd.Series(sorted(panel_dates.dropna().unique()))
    if dates.empty:
        return []
    min_date = dates.min()
    max_date = dates.max()
    anchor = pd.Timestamp(min_date).replace(day=1)
    train_months = int(phase_cfg['walk_forward']['train_months'])
    validation_months = int(phase_cfg['walk_forward']['validation_months'])
    test_months = int(phase_cfg['walk_forward']['test_months'])
    step_months = int(phase_cfg['walk_forward']['step_months'])
    splits: list[dict[str, pd.Timestamp]] = []
    while True:
        train_start = anchor
        train_end = train_start + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        val_end = train_end + pd.DateOffset(months=validation_months)
        test_end = val_end + pd.DateOffset(months=test_months)
        if test_end > max_date + pd.Timedelta(days=1):
            break
        splits.append(
            {
                'train_start': train_start,
                'train_end': train_end,
                'val_end': val_end,
                'test_end': test_end,
            }
        )
        anchor = anchor + pd.DateOffset(months=step_months)
    return splits


def compute_daily_rank_ic(df: pd.DataFrame, factor_col: str, return_col: str) -> float | None:
    rows: list[float] = []
    for _, day in df.groupby('trade_date', sort=True):
        subset = day[[factor_col, return_col]].dropna()
        if len(subset) < 20:
            continue
        factor_rank = subset[factor_col].rank()
        return_rank = subset[return_col].rank()
        if factor_rank.nunique(dropna=True) <= 1 or return_rank.nunique(dropna=True) <= 1:
            continue
        ic = factor_rank.corr(return_rank)
        if pd.notna(ic):
            rows.append(float(ic))
    if not rows:
        return None
    return float(sum(rows) / len(rows))


def sign_of(value: float | None) -> str:
    if value is None:
        return 'na'
    return 'positive' if value >= 0 else 'negative'


def count_present_keys(rows: list[dict], candidate_keys: set[str]) -> int:
    hits = 0
    for row in rows:
        if any(key in row and row.get(key) is not None for key in candidate_keys):
            hits += 1
    return hits


def format_label_modes(modes: list[str]) -> str:
    labels = [LABEL_MODE_TO_SHORT.get(str(mode), str(mode)) for mode in modes if mode]
    return ' / '.join(labels) if labels else '无'


def build_return_level_analog_context(analog_report: dict, fallback_window_count: int) -> dict[str, object]:
    validation_scope = analog_report.get('validation_scope', {}) or {}
    replay = analog_report.get('current_combo_return_level_analog_replay', {}) or {}
    validation_status = analog_report.get('validation_status', {}) or {}
    analog_sample_summary = analog_report.get('analog_sample_summary', {}) or {}
    completed_label_modes = [str(mode) for mode in replay.get('completed_label_modes', []) if mode]
    completed_label_modes_text = format_label_modes(completed_label_modes)
    analog_window_count = int(
        analog_sample_summary.get('analog_window_count')
        or validation_status.get('proxy_analog_window_count')
        or fallback_window_count
        or 0
    )
    usable_portfolio_replay_sample_count = int(
        replay.get('usable_portfolio_replay_sample_count')
        or validation_status.get('usable_portfolio_replay_sample_count')
        or 0
    )
    return_level_analog_claim_allowed = bool(
        validation_scope.get('portfolio_return_replay_included')
        and validation_scope.get('analog_validation_completed_flag')
    )
    independent_oos_completed = bool(
        validation_scope.get('independent_oos_completed')
        or validation_status.get('independent_validation_completed')
    )
    if return_level_analog_claim_allowed:
        claim_limit = validation_scope.get('claim_limit') or (
            f'当前 report 已提供 {completed_label_modes_text} 三口径收益级 analog replay，可作为组合收益层补充验证；'
            '但它仍不是独立 labeled holdout 或独立 OOS。'
        )
    else:
        claim_limit = (
            'analog windows 目前只有 market-proxy forward returns；'
            '尚未提供可复用的组合收益级 replay，因此不能升级为收益级 analog validation。'
        )
    scope_note = validation_scope.get('official_vs_alternate_scope_note') or claim_limit
    return {
        'return_level_analog_claim_allowed': return_level_analog_claim_allowed,
        'completed_label_modes': completed_label_modes,
        'completed_label_modes_text': completed_label_modes_text,
        'usable_portfolio_replay_sample_count': usable_portfolio_replay_sample_count,
        'independent_oos_completed': independent_oos_completed,
        'analog_window_count': analog_window_count,
        'claim_limit': claim_limit,
        'scope_note': scope_note,
    }


def summarize_upstream_overlap(
    effective_dates: list[str],
    splits: list[dict[str, pd.Timestamp]],
) -> tuple[dict[str, object], list[pd.Timestamp], list[str]]:
    effective_ts = [to_ts(d) for d in effective_dates]
    overlap_counter = Counter()
    overlap_per_date: dict[str, int] = {d: 0 for d in effective_dates}
    for split in splits:
        for d_str, d_ts in zip(effective_dates, effective_ts):
            if d_ts > split['val_end'] and d_ts <= split['test_end']:
                overlap_per_date[d_str] += 1
    for count in overlap_per_date.values():
        overlap_counter[count] += 1
    last_split = splits[-1] if splits else None
    last_split_dates = []
    if last_split:
        last_split_dates = [
            d
            for d, d_ts in zip(effective_dates, effective_ts)
            if d_ts > last_split['val_end'] and d_ts <= last_split['test_end']
        ]
    summary = {
        'effective_replay_days': len(effective_dates),
        'effective_days_overlapping_any_upstream_test_fold': sum(1 for v in overlap_per_date.values() if v > 0),
        'effective_days_overlapping_last_upstream_test_fold': len(last_split_dates),
        'effective_date_test_fold_overlap_counter': {str(k): v for k, v in sorted(overlap_counter.items())},
        'effective_dates_outside_any_test_fold': sorted([d for d, c in overlap_per_date.items() if c == 0]),
        'effective_dates_outside_last_test_fold': sorted([d for d in effective_dates if d not in set(last_split_dates)]),
    }
    return summary, effective_ts, last_split_dates


def build_factor_lineage_for_mode(
    panel: pd.DataFrame,
    replay_payload: dict,
    effective_ts: list[pd.Timestamp],
    label_mode: str,
) -> dict[str, dict[str, float | str | None]]:
    return_col = LABEL_MODE_TO_COLUMN[label_mode]
    pre_replay_panel = panel.loc[panel['trade_date'] < min(effective_ts)].copy() if effective_ts else panel.iloc[0:0].copy()
    replay_panel = panel.loc[panel['trade_date'].isin(effective_ts)].copy()
    factor_lineage: dict[str, dict[str, float | str | None]] = {}
    current_factors = replay_payload.get('current_combo_definition', {}).get('regime_union_factor_ids', [])
    for factor_id in current_factors:
        full_same = compute_daily_rank_ic(panel, factor_id, return_col)
        factor_lineage[str(factor_id)] = {
            'sign_used_by_formal_backtest_from_canonical_same_label_audit': sign_of(full_same),
            'full_panel_rank_ic_mean_close_to_close': compute_daily_rank_ic(panel, factor_id, 'next_return_close_to_close_1d'),
            'pre_replay_rank_ic_mean_close_to_close': compute_daily_rank_ic(pre_replay_panel, factor_id, 'next_return_close_to_close_1d'),
            'replay_window_rank_ic_mean_close_to_close': compute_daily_rank_ic(replay_panel, factor_id, 'next_return_close_to_close_1d'),
            'full_panel_rank_ic_mean_open_to_close': compute_daily_rank_ic(panel, factor_id, 'next_return_open_to_close_1d'),
            'replay_window_rank_ic_mean_open_to_close': compute_daily_rank_ic(replay_panel, factor_id, 'next_return_open_to_close_1d'),
            'full_panel_rank_ic_mean_close_to_open': compute_daily_rank_ic(panel, factor_id, 'next_return_close_to_open_1d'),
            'replay_window_rank_ic_mean_close_to_open': compute_daily_rank_ic(replay_panel, factor_id, 'next_return_close_to_open_1d'),
        }
    return factor_lineage


def build_markdown(report: dict) -> str:
    summary = report['summary']
    backtest = report['formal_backtest_snapshot']
    holdout = report['independent_oos_blocker']
    overlap = report['upstream_data_reuse']
    analog = report['analog_return_level_blocker']
    analog_heading = '## analog 收益级验证现状' if analog['return_level_analog_claim_allowed'] else '## 为什么 analog 仍不是收益级验证'
    analog_scope = analog.get('report_scope_echo', {})
    lines = [
        '# Current Combo Claim-Limit Audit',
        '',
        f"- 生成时间：{report['generated_at']}",
        f"- 结论：{summary['headline']}",
        '',
        '## Formal backtest 快照',
    ]
    for label_mode, payload in backtest['by_label_mode'].items():
        label_text = label_mode.replace('_', '-')
        lines.extend(
            [
                f"- {label_text} combo 总收益：{fmt_pct(payload['combo_total_return'])}（方向={payload['combo_direction']}）",
                f"- {label_text} 超额：{fmt_pct(payload['excess_total_return'])}（方向={payload['excess_direction']}）",
            ]
        )
    lines.extend(
        [
            '',
            '## 为什么现在不能升级为 independent OOS',
            f"- clean eligible trade days：{holdout['eligible_trade_days']}",
        ]
    )
    for label_mode, payload in holdout['by_label_mode'].items():
        label_text = label_mode.replace('_', '-')
        lines.append(
            f"- {label_text} 有标签且已被 formal replay 用掉的天数：{payload['used_labeled_days']} / {payload['labeled_eligible_days']}"
        )
    lines.extend(
        [
            f"- 还能留下来的独立 labeled holdout days：{holdout['remaining_labeled_holdout_days_count']}（{', '.join(holdout['remaining_labeled_holdout_days']) if holdout['remaining_labeled_holdout_days'] else '无'}）",
            f"- 无法用作收益级 holdout 的剩余 eligible 日期：{', '.join(holdout['eligible_but_unlabeled_dates']) if holdout['eligible_but_unlabeled_dates'] else '无'}",
            '',
            '## upstream 数据复用量化',
            f"- replay effective dates 落入任意 factor-audit test fold：{fmt_ratio(overlap['effective_days_overlapping_any_upstream_test_fold'], overlap['effective_replay_days'])}",
            f"- replay effective dates 落入最后一个 factor-audit test fold：{fmt_ratio(overlap['effective_days_overlapping_last_upstream_test_fold'], overlap['effective_replay_days'])}",
            f"- 不在最后一个 test fold 的日期：{', '.join(overlap['effective_dates_outside_last_test_fold']) if overlap['effective_dates_outside_last_test_fold'] else '无'}",
        ]
    )
    for label_mode, payload in overlap.get('by_label_mode', {}).items():
        label_text = label_mode.replace('_', '-')
        lines.extend(
            [
                f"- {label_text} 任意/最后一个 test fold overlap：{fmt_ratio(payload['effective_days_overlapping_any_upstream_test_fold'], payload['effective_replay_days'])} / {fmt_ratio(payload['effective_days_overlapping_last_upstream_test_fold'], payload['effective_replay_days'])}",
                f"  - 不在最后一个 test fold 的日期：{', '.join(payload['effective_dates_outside_last_test_fold']) if payload['effective_dates_outside_last_test_fold'] else '无'}",
            ]
        )
    lines.extend(
        [
            f"- 注：{overlap['note']}",
            '',
            analog_heading,
            f"- analog windows：{analog['analog_window_count']}",
            f"- 有 market proxy 5d/20d/60d forward return 的窗口：{analog['windows_with_market_proxy_5d']}/{analog['analog_window_count']}、{analog['windows_with_market_proxy_20d']}/{analog['analog_window_count']}、{analog['windows_with_market_proxy_60d']}/{analog['analog_window_count']}",
            f"- 有 portfolio-return replay 覆盖的窗口：{analog['windows_with_portfolio_return_payload']}/{analog['analog_window_count']}",
            f"- 已完成收益级 replay 的 label_mode：{analog.get('completed_label_modes_text', '无')}（usable_portfolio_replay_sample_count={analog.get('usable_portfolio_replay_sample_count', 0)}）",
            f"- 有 historical weight-plan / realized attribution 字段的窗口：{analog['windows_with_historical_weight_payload']}/{analog['analog_window_count']} / {analog['windows_with_realized_factor_attribution_payload']}/{analog['analog_window_count']}",
            f"- latest weight plan proxy 覆盖：{analog['latest_weight_plan_proxy_covered_windows']}/{analog['analog_window_count']}",
            f"- validation_scope：portfolio_return_replay_included={analog_scope.get('portfolio_return_replay_included')}、analog_validation_completed_flag={analog_scope.get('analog_validation_completed_flag')}、independent_oos_completed={analog_scope.get('independent_oos_completed')}",
            f"- claim_limit：{analog['claim_limit']}",
            f"- scope_note：{analog.get('scope_note', '无')}",
            '',
            '## 可宣称边界',
            f"- independent OOS：{summary['independent_oos_claim_allowed']}",
            f"- 收益级 analog validation：{summary['return_level_analog_claim_allowed']}",
            f"- 允许的最强表述：{summary['allowed_claim']}",
        ]
    )
    return '\n'.join(lines) + '\n'


def main() -> None:
    now = datetime.now(TZ).isoformat(timespec='seconds')
    replay_payloads = {
        label_mode: load_json(path)
        for label_mode, path in LABEL_MODE_TO_REPLAY_PATH.items()
    }
    formal_payloads = {
        label_mode: load_json(path)
        for label_mode, path in LABEL_MODE_TO_FORMAL_PATH.items()
    }
    replay = replay_payloads[PRIMARY_LABEL_MODE]
    formal = formal_payloads[PRIMARY_LABEL_MODE]
    portfolio_report = load_json(PORTFOLIO_REPORT_PATH)
    analog_windows = load_json(ANALOG_WINDOWS_PATH)
    analog_report = load_json(ANALOG_REPORT_PATH)
    weight_map = load_json(WEIGHT_MAP_PATH)
    factor_audit = load_json(FACTOR_AUDIT_PATH)
    phase_cfg = yaml.safe_load(PHASE_PATH.read_text(encoding='utf-8'))

    effective_dates_by_mode = {
        label_mode: replay_payloads[label_mode]['replay_results'][LABEL_MODE_TO_SERIES_KEY[label_mode]]['effective_trade_dates']
        for label_mode in LABEL_MODE_TO_COLUMN
    }
    effective_sets_by_mode = {
        label_mode: set(dates)
        for label_mode, dates in effective_dates_by_mode.items()
    }
    primary_effective_dates = list(effective_dates_by_mode[PRIMARY_LABEL_MODE])
    eligible_dates = replay['benchmark_eligible_summary']['eligible_dates']

    all_mode_factor_ids = sorted(
        {
            str(factor_id)
            for payload in replay_payloads.values()
            for factor_id in payload.get('current_combo_definition', {}).get('regime_union_factor_ids', [])
            if factor_id is not None
        }
    )
    panel = pd.read_parquet(
        PANEL_PATH,
        columns=[
            'trade_date',
            'next_return_close_to_close_1d',
            'next_return_open_to_close_1d',
            'next_return_close_to_open_1d',
            *all_mode_factor_ids,
        ],
    )
    panel['trade_date'] = pd.to_datetime(panel['trade_date'].astype(str), format='%Y%m%d', errors='coerce')
    eligible_ts = [to_ts(d) for d in eligible_dates]
    eligible_panel = panel.loc[panel['trade_date'].isin(eligible_ts)].copy()

    label_rows = {}
    for trade_date, day in eligible_panel.groupby('trade_date', sort=True):
        key = trade_date.strftime('%Y%m%d')
        label_rows[key] = {
            'close_to_close_rows': int(day['next_return_close_to_close_1d'].notna().sum()),
            'open_to_close_rows': int(day['next_return_open_to_close_1d'].notna().sum()),
            'close_to_open_rows': int(day['next_return_close_to_open_1d'].notna().sum()),
        }

    labeled_days_by_mode = {
        label_mode: sorted(
            [
                d for d in eligible_dates
                if label_rows.get(d, {}).get(f'{label_mode}_rows', 0) > 0
            ]
        )
        for label_mode in LABEL_MODE_TO_COLUMN
    }
    remaining_holdout_by_mode = {
        label_mode: sorted(
            [d for d in labeled_days if d not in effective_sets_by_mode[label_mode]]
        )
        for label_mode, labeled_days in labeled_days_by_mode.items()
    }
    unlabeled_eligible_dates = sorted(
        [
            d
            for d in eligible_dates
            if all(label_rows.get(d, {}).get(f'{label_mode}_rows', 0) == 0 for label_mode in LABEL_MODE_TO_COLUMN)
        ]
    )

    splits = build_monthly_splits(panel['trade_date'], phase_cfg)
    primary_overlap_summary, _, primary_last_split_dates = summarize_upstream_overlap(primary_effective_dates, splits)
    last_split = splits[-1] if splits else None

    upstream_data_reuse_by_mode = {}
    for label_mode, replay_payload in replay_payloads.items():
        mode_effective_dates = list(effective_dates_by_mode[label_mode])
        mode_overlap_summary, mode_effective_ts, mode_last_split_dates = summarize_upstream_overlap(mode_effective_dates, splits)
        mode_overlap_summary['score_sign_source_reuses_replay_window_days'] = len(mode_effective_dates)
        mode_overlap_summary['score_sign_source_reuse_ratio'] = 1.0 if mode_effective_dates else 0.0
        mode_overlap_summary['lineage_note'] = (
            '对应 label_mode 的 canonical replay/backtest 直接承接同口径 full-sample rank_ic_mean 作为辅助因子 sign；'
            f"同时 {len(mode_last_split_dates)}/{len(mode_effective_dates)} 个 formal replay 日期已经位于最后一个 upstream factor-audit test fold 内。"
        )
        mode_overlap_summary['selected_factor_rank_ic_lineage'] = build_factor_lineage_for_mode(
            panel,
            replay_payload,
            mode_effective_ts,
            label_mode,
        )
        upstream_data_reuse_by_mode[label_mode] = mode_overlap_summary

    factor_lineage = dict(upstream_data_reuse_by_mode[PRIMARY_LABEL_MODE]['selected_factor_rank_ic_lineage'])

    windows = analog_windows.get('analog_windows', [])
    proxy_return_keys = {'subsequent_market_proxy_returns'}
    portfolio_return_keys = {
        'subsequent_portfolio_returns',
        'portfolio_return_replay',
        'portfolio_replay_returns',
        'portfolio_returns',
        'combo_returns',
        'current_combo_returns',
    }
    historical_weight_keys = {
        'historical_weight_plan',
        'historical_weight_plan_factor_ids',
        'window_specific_weight_plan',
        'realized_weight_plan',
    }
    realized_attr_keys = {
        'historical_realized_factor_attribution',
        'realized_factor_attribution',
        'window_specific_realized_factor_attribution',
    }
    windows_with_proxy_5d = sum(1 for w in windows if w.get('subsequent_market_proxy_returns', {}).get('5d') is not None)
    windows_with_proxy_20d = sum(1 for w in windows if w.get('subsequent_market_proxy_returns', {}).get('20d') is not None)
    windows_with_proxy_60d = sum(1 for w in windows if w.get('subsequent_market_proxy_returns', {}).get('60d') is not None)
    windows_with_portfolio_return_payload = count_present_keys(windows, portfolio_return_keys)
    windows_with_historical_weight_payload = count_present_keys(windows, historical_weight_keys)
    windows_with_realized_factor_attribution_payload = count_present_keys(windows, realized_attr_keys)
    windows_explicitly_proxy_not_reconstructed = sum(
        1
        for w in windows
        if 'not reconstructed historical window-specific realized factor attribution' in str(w.get('method_caveat', ''))
    )
    analog_context = build_return_level_analog_context(analog_report, len(windows))

    latest_weight_plan_factor_ids = [
        str(item['factor_id'])
        for item in weight_map.get('latest_weight_plan', {}).get('selected_factors', [])
    ]
    latest_weight_plan_proxy_covered_windows = sum(
        1
        for w in windows
        if set(latest_weight_plan_factor_ids).issubset(
            {str(item['factor_id']) for item in w.get('effective_factor_proxies', [])}
        )
    )

    label_mode_claim_scope = {}
    proxy_covered_windows_by_label_mode = {}
    reserved_holdout_union: set[str] = set()
    reserved_holdout_pending_union: set[str] = set()
    reserved_holdout_now_labeled_union: set[str] = set()
    for label_mode in LABEL_MODE_TO_COLUMN:
        replay_payload = replay_payloads[label_mode]
        formal_payload = formal_payloads[label_mode]
        metric_key = LABEL_MODE_TO_FORMAL_METRIC_KEY[label_mode]
        excess_key = LABEL_MODE_TO_EXCESS_KEY[label_mode]
        metric_scope = replay_payload.get('benchmark_eligible_summary', {}) or {}
        reserved_holdout_dates = sorted(
            set(metric_scope.get('reserved_holdout_dates', []))
        )
        reserved_holdout_pending_labels = sorted(
            set(reserved_holdout_dates) - set(labeled_days_by_mode[label_mode])
        )
        reserved_holdout_now_labeled = sorted(
            set(reserved_holdout_dates) & set(labeled_days_by_mode[label_mode])
        )
        reserved_holdout_union.update(reserved_holdout_dates)
        reserved_holdout_pending_union.update(reserved_holdout_pending_labels)
        reserved_holdout_now_labeled_union.update(reserved_holdout_now_labeled)
        metric_scope = replay_payload.get('benchmark_eligible_summary', {}) or {}
        mode_latest_plan_factor_ids = [
            str(factor_id)
            for factor_id in replay_payload.get('current_combo_definition', {}).get('latest_weight_plan_factor_ids', [])
            if factor_id is not None
        ]
        mode_proxy_covered_windows = sum(
            1
            for w in windows
            if set(mode_latest_plan_factor_ids).issubset(
                {str(item['factor_id']) for item in w.get('effective_factor_proxies', [])}
            )
        )
        proxy_covered_windows_by_label_mode[label_mode] = mode_proxy_covered_windows
        label_mode_claim_scope[label_mode] = {
            'label_mode': label_mode,
            'label_column': LABEL_MODE_TO_COLUMN[label_mode],
            'selection_mode': replay_payload.get('current_combo_definition', {}).get('selection_mode'),
            'latest_weight_plan_factor_ids': mode_latest_plan_factor_ids,
            'regime_union_factor_ids': replay_payload.get('current_combo_definition', {}).get('regime_union_factor_ids', []),
            'replay_path': str(LABEL_MODE_TO_REPLAY_PATH[label_mode]),
            'formal_backtest_path': str(LABEL_MODE_TO_FORMAL_PATH[label_mode]),
            'replay_generated_at': replay_payload.get('generated_at'),
            'formal_backtest_generated_at': formal_payload.get('generated_at'),
            'eligible_trade_days': len(metric_scope.get('eligible_dates', [])),
            'consumable_eligible_trade_days': int(metric_scope.get('consumable_eligible_trade_days') or len(metric_scope.get('consumable_eligible_dates', [])) or len(metric_scope.get('eligible_dates', []))),
            'consumable_eligible_dates': list(metric_scope.get('consumable_eligible_dates', metric_scope.get('eligible_dates', []))),
            'labeled_eligible_days': len(labeled_days_by_mode[label_mode]),
            'used_labeled_days': len(effective_sets_by_mode[label_mode]),
            'remaining_labeled_holdout_days': remaining_holdout_by_mode[label_mode],
            'remaining_labeled_holdout_days_count': len(remaining_holdout_by_mode[label_mode]),
            'eligible_but_unlabeled_dates': sorted(
                set(metric_scope.get('eligible_dates', [])) - set(labeled_days_by_mode[label_mode])
            ),
            'reserved_holdout_dates': reserved_holdout_dates,
            'reserved_holdout_dates_pending_labels': reserved_holdout_pending_labels,
            'reserved_holdout_dates_now_labeled': reserved_holdout_now_labeled,
            'effective_replay_days': len(effective_dates_by_mode[label_mode]),
            'primary_combo_total_return': formal_payload['minimum_required_metrics'][metric_key]['total_return'],
            'primary_excess_total_return': formal_payload['comparisons'][excess_key],
            'primary_combo_direction': direction_of(formal_payload['minimum_required_metrics'][metric_key]['total_return']),
            'primary_excess_direction': direction_of(formal_payload['comparisons'][excess_key]),
            'proxy_covered_windows': mode_proxy_covered_windows,
            'sufficient_to_claim_independent_oos': False,
        }

    remaining_labeled_holdout_days = sorted({
        day
        for days in remaining_holdout_by_mode.values()
        for day in days
    })
    reserved_holdout_dates = sorted(reserved_holdout_union)
    reserved_holdout_dates_pending_labels = sorted(reserved_holdout_pending_union)
    reserved_holdout_dates_now_labeled = sorted(reserved_holdout_now_labeled_union)
    reserved_holdout_text = ', '.join(reserved_holdout_dates) if reserved_holdout_dates else '无'
    reserved_pending_text = ', '.join(reserved_holdout_dates_pending_labels) if reserved_holdout_dates_pending_labels else '无'
    reserved_now_labeled_text = ', '.join(reserved_holdout_dates_now_labeled) if reserved_holdout_dates_now_labeled else '无'
    if reserved_holdout_dates:
        if reserved_holdout_dates_pending_labels:
            reserve_status_summary = (
                f'已显式预留 future labeled holdout reserve={reserved_holdout_text}；'
                f'待 next-day label={reserved_pending_text}；已形成 labeled holdout={reserved_now_labeled_text}'
            )
            reserve_headline_clause = '并显式写入 future labeled holdout reserve；'
            reserve_claim_tail = '但当前 reserve 里仍待 next-day label 的日期还不是已形成的独立 labeled holdout，不能宣称独立 OOS或解除 promotion freeze。'
        else:
            reserve_status_summary = (
                f'reserved_holdout_dates={reserved_holdout_text}；reserved_holdout_dates_pending_labels=无；'
                f'reserved_holdout_dates_now_labeled={reserved_now_labeled_text}'
            )
            reserve_headline_clause = '并显式写入 reserved and now labeled holdout；'
            reserve_claim_tail = '但 reserve 中已形成 labeled holdout 的日期仍不足以单独构成独立 OOS，不能宣称独立 OOS或解除 promotion freeze。'
    else:
        reserve_status_summary = '当前尚未显式预留 future labeled holdout reserve'
        reserve_headline_clause = ''
        reserve_claim_tail = '当前也没有显式预留 holdout reserve，不能宣称独立 OOS或解除 promotion freeze。'
    unlabeled_summary = (
        f"剩余 {', '.join(unlabeled_eligible_dates)} 只有 benchmark_eligible、没有 next-day 标签"
        if unlabeled_eligible_dates
        else '当前也没有额外 benchmark_eligible 但缺 next-day 标签的日期'
    )
    formal_backtest_by_label_mode = {
        label_mode: {
            'combo_total_return': label_mode_claim_scope[label_mode]['primary_combo_total_return'],
            'excess_total_return': label_mode_claim_scope[label_mode]['primary_excess_total_return'],
            'combo_direction': label_mode_claim_scope[label_mode]['primary_combo_direction'],
            'excess_direction': label_mode_claim_scope[label_mode]['primary_excess_direction'],
            'effective_replay_days': label_mode_claim_scope[label_mode]['effective_replay_days'],
            'latest_weight_plan_factor_ids': label_mode_claim_scope[label_mode]['latest_weight_plan_factor_ids'],
            'selection_mode': label_mode_claim_scope[label_mode]['selection_mode'],
        }
        for label_mode in LABEL_MODE_TO_COLUMN
    }
    independent_oos_by_label_mode = {
        label_mode: {
            'labeled_eligible_days': label_mode_claim_scope[label_mode]['labeled_eligible_days'],
            'used_labeled_days': label_mode_claim_scope[label_mode]['used_labeled_days'],
            'remaining_labeled_holdout_days': label_mode_claim_scope[label_mode]['remaining_labeled_holdout_days'],
            'remaining_labeled_holdout_days_count': label_mode_claim_scope[label_mode]['remaining_labeled_holdout_days_count'],
            'eligible_but_unlabeled_dates': label_mode_claim_scope[label_mode]['eligible_but_unlabeled_dates'],
            'reserved_holdout_dates': label_mode_claim_scope[label_mode]['reserved_holdout_dates'],
            'reserved_holdout_dates_pending_labels': label_mode_claim_scope[label_mode]['reserved_holdout_dates_pending_labels'],
            'reserved_holdout_dates_now_labeled': label_mode_claim_scope[label_mode]['reserved_holdout_dates_now_labeled'],
        }
        for label_mode in LABEL_MODE_TO_COLUMN
    }
    formal_metrics = formal['minimum_required_metrics']
    comparisons = formal['comparisons']
    direction = formal['direction_consistency']

    report = {
        'report_type': 'current_combo_claim_limit_audit',
        'schema_version': '2026-04-19.current_combo_claim_limit_audit.v2',
        'generated_at': now,
        'source_paths': {
            'current_combo_replay_report': str(REPLAY_PATH),
            'current_combo_portfolio_backtest': str(FORMAL_PATH),
            'portfolio_backtest_report': str(PORTFOLIO_REPORT_PATH),
            'regime_analog_windows': str(ANALOG_WINDOWS_PATH),
            'analog_validation_report': str(ANALOG_REPORT_PATH),
            'weight_map': str(WEIGHT_MAP_PATH),
            'factor_audit': str(FACTOR_AUDIT_PATH),
            'panel': str(PANEL_PATH),
            'phase1_yaml': str(PHASE_PATH),
            'current_combo_holdout_reserve': replay_payloads[PRIMARY_LABEL_MODE].get('source_paths', {}).get('current_combo_holdout_reserve'),
            'current_combo_replay_report_open_to_close': str(O2C_REPLAY_PATH),
            'current_combo_replay_report_close_to_open': str(C2O_REPLAY_PATH),
            'current_combo_portfolio_backtest_open_to_close': str(O2C_FORMAL_PATH),
            'current_combo_portfolio_backtest_close_to_open': str(C2O_FORMAL_PATH),
        },
        'summary': {
            'headline': (
                '已把 current_combo 的 c2c / o2c / c2o 组合级结果接入 claim-limit，'
                + reserve_headline_clause
                + ('analog 也已升级为收益级 replay；' if analog_context['return_level_analog_claim_allowed'] else '')
                + f'但三口径仍共用同一 33 天 clean window，current labeled holdout 仍为 {len(remaining_labeled_holdout_days)}，independent OOS 仍不能成立。'
            ),
            'new_combo_level_evidence_created': True,
            'evidence_type': 'blocker_claim_limit_quantification',
            'independent_oos_claim_allowed': False,
            'return_level_analog_claim_allowed': analog_context['return_level_analog_claim_allowed'],
            'allowed_claim': (
                f'当前可宣称“current_combo 三口径 formal current-weight-map backtest 已齐、claim-limit 已按 label_mode 量化 current labeled holdout={len(remaining_labeled_holdout_days)}，且 {reserve_status_summary}'
                + (f'；analog_validation 已提供 {analog_context["completed_label_modes_text"]} 收益级 analog replay”；' if analog_context['return_level_analog_claim_allowed'] else '”；')
                + reserve_claim_tail
            ),
        },
        'label_mode_claim_scope': label_mode_claim_scope,
        'formal_backtest_snapshot': {
            'effective_replay_days': len(primary_effective_dates),
            'eligible_trade_days': len(eligible_dates),
            'close_to_close_combo_total_return': formal_metrics['close_to_close_combo']['total_return'],
            'open_to_close_combo_total_return': formal_metrics['open_to_close_combo']['total_return'],
            'close_to_open_combo_total_return': formal_payloads['close_to_open']['minimum_required_metrics']['close_to_open_combo']['total_return'],
            'close_to_close_excess_total_return': comparisons['close_to_close_excess_total_return'],
            'open_to_close_excess_total_return': comparisons['open_to_close_excess_total_return'],
            'close_to_open_excess_total_return': formal_payloads['close_to_open']['comparisons']['close_to_open_excess_total_return'],
            'close_to_close_combo_direction': direction_of(formal_metrics['close_to_close_combo']['total_return']),
            'open_to_close_combo_direction': direction_of(formal_metrics['open_to_close_combo']['total_return']),
            'close_to_open_combo_direction': direction_of(formal_payloads['close_to_open']['minimum_required_metrics']['close_to_open_combo']['total_return']),
            'close_to_close_excess_direction': direction_of(comparisons['close_to_close_excess_total_return']),
            'open_to_close_excess_direction': direction_of(comparisons['open_to_close_excess_total_return']),
            'close_to_open_excess_direction': direction_of(formal_payloads['close_to_open']['comparisons']['close_to_open_excess_total_return']),
            'direction_consistent': bool(direction['combo_direction_consistent'] and direction['excess_direction_consistent']),
            'by_label_mode': formal_backtest_by_label_mode,
        },
        'independent_oos_blocker': {
            'eligible_trade_days': len(eligible_dates),
            'eligible_date_range': {
                'start': eligible_dates[0] if eligible_dates else None,
                'end': eligible_dates[-1] if eligible_dates else None,
            },
            **{
                label_mode: {
                    'labeled_eligible_days': independent_oos_by_label_mode[label_mode]['labeled_eligible_days'],
                    'used_labeled_days': independent_oos_by_label_mode[label_mode]['used_labeled_days'],
                    'remaining_labeled_holdout_days': independent_oos_by_label_mode[label_mode]['remaining_labeled_holdout_days'],
                }
                for label_mode in LABEL_MODE_TO_COLUMN
            },
            'by_label_mode': independent_oos_by_label_mode,
            'remaining_labeled_holdout_days': remaining_labeled_holdout_days,
            'remaining_labeled_holdout_days_count': len(remaining_labeled_holdout_days),
            'eligible_but_unlabeled_dates': unlabeled_eligible_dates,
            'reserved_holdout_dates': reserved_holdout_dates,
            'reserved_holdout_dates_pending_labels': reserved_holdout_dates_pending_labels,
            'reserved_holdout_dates_now_labeled': reserved_holdout_dates_now_labeled,
            'why_not_independent_oos': (
                'c2c / o2c / c2o 三口径 current_combo formal replay 都已消耗同一 clean window 内全部 '
                f"{len(label_mode_claim_scope['close_to_close']['remaining_labeled_holdout_days']) + label_mode_claim_scope['close_to_close']['used_labeled_days']} 个有 next-day 标签的 eligible trade days；"
                f"当前 current labeled holdout={len(remaining_labeled_holdout_days)}；{reserve_status_summary}；"
                f'{unlabeled_summary}，因此当前仍不能把 reserve 包装成已形成的独立 labeled holdout。'
            ),
        },
        'upstream_data_reuse': {
            'walk_forward_split_count': len(splits),
            'last_upstream_test_fold': {
                'train_start': last_split['train_start'].strftime('%Y-%m-%d') if last_split else None,
                'train_end': last_split['train_end'].strftime('%Y-%m-%d') if last_split else None,
                'val_end': last_split['val_end'].strftime('%Y-%m-%d') if last_split else None,
                'test_end': last_split['test_end'].strftime('%Y-%m-%d') if last_split else None,
            },
            'effective_replay_days': primary_overlap_summary['effective_replay_days'],
            'effective_days_overlapping_any_upstream_test_fold': primary_overlap_summary['effective_days_overlapping_any_upstream_test_fold'],
            'effective_days_overlapping_last_upstream_test_fold': primary_overlap_summary['effective_days_overlapping_last_upstream_test_fold'],
            'effective_date_test_fold_overlap_counter': primary_overlap_summary['effective_date_test_fold_overlap_counter'],
            'effective_dates_outside_any_test_fold': primary_overlap_summary['effective_dates_outside_any_test_fold'],
            'effective_dates_outside_last_test_fold': primary_overlap_summary['effective_dates_outside_last_test_fold'],
            'score_sign_source_reuses_replay_window_days': upstream_data_reuse_by_mode[PRIMARY_LABEL_MODE]['score_sign_source_reuses_replay_window_days'],
            'score_sign_source_reuse_ratio': upstream_data_reuse_by_mode[PRIMARY_LABEL_MODE]['score_sign_source_reuse_ratio'],
            'lineage_note': (
                'run_current_combo_replay.py 直接使用 factor_audit_phase1.json 的 full-sample rank_ic_mean 作为辅助因子 sign；'
                f"同时 {len(primary_last_split_dates)}/{len(primary_effective_dates)} 个 formal replay 日期已经位于最后一个 upstream factor-audit test fold 内。"
            ),
            'selected_factor_rank_ic_lineage': factor_lineage,
            'by_label_mode': upstream_data_reuse_by_mode,
            'applies_to_label_modes': list(LABEL_MODE_TO_COLUMN),
            'alternate_label_modes_not_quantified_here': [],
            'note': 'upstream test-fold overlap 与 sign lineage 已按 c2c / o2c / c2o 三口径量化；顶层字段保留 close_to_close 兼容摘要，细节见 by_label_mode。',
        },
        'analog_return_level_blocker': {
            'analog_window_count': analog_context['analog_window_count'],
            'windows_with_market_proxy_5d': windows_with_proxy_5d,
            'windows_with_market_proxy_20d': windows_with_proxy_20d,
            'windows_with_market_proxy_60d': windows_with_proxy_60d,
            'windows_with_portfolio_return_payload': (
                analog_context['usable_portfolio_replay_sample_count']
                if analog_context['return_level_analog_claim_allowed']
                else windows_with_portfolio_return_payload
            ),
            'windows_with_historical_weight_payload': windows_with_historical_weight_payload,
            'windows_with_realized_factor_attribution_payload': windows_with_realized_factor_attribution_payload,
            'windows_explicitly_proxy_not_reconstructed': windows_explicitly_proxy_not_reconstructed,
            'latest_weight_plan_factor_ids': latest_weight_plan_factor_ids,
            'latest_weight_plan_proxy_covered_windows': latest_weight_plan_proxy_covered_windows,
            'latest_weight_plan_factor_ids_by_label_mode': {
                label_mode: label_mode_claim_scope[label_mode]['latest_weight_plan_factor_ids']
                for label_mode in LABEL_MODE_TO_COLUMN
            },
            'latest_weight_plan_proxy_covered_windows_by_label_mode': proxy_covered_windows_by_label_mode,
            'return_level_analog_claim_allowed': analog_context['return_level_analog_claim_allowed'],
            'completed_label_modes': analog_context['completed_label_modes'],
            'completed_label_modes_text': analog_context['completed_label_modes_text'],
            'usable_portfolio_replay_sample_count': analog_context['usable_portfolio_replay_sample_count'],
            'independent_oos_completed': analog_context['independent_oos_completed'],
            'claim_limit': analog_context['claim_limit'],
            'scope_note': analog_context['scope_note'],
            'report_scope_echo': analog_report.get('validation_scope', {}),
        },
        'portfolio_context_echo': {
            'portfolio_report_claim_limit': portfolio_report.get('oos_validation_assessment', {}).get('claim_limit'),
            'formal_backtest_claim_limit': formal.get('claim_limit'),
            'canonical_selection_mode': replay['current_combo_definition']['selection_mode'],
            'latest_weight_plan_factor_ids': replay['current_combo_definition']['latest_weight_plan_factor_ids'],
            'regime_union_factor_ids': replay['current_combo_definition']['regime_union_factor_ids'],
            'factor_audit_report_count': len(factor_audit.get('reports', [])),
            'multi_label_claim_scope_quantified': True,
            'multi_label_claim_scope_summary': {
                label_mode: label_mode_claim_scope[label_mode]['remaining_labeled_holdout_days_count']
                for label_mode in LABEL_MODE_TO_COLUMN
            },
        },
    }

    dump_json(OUTPUT_JSON, report)
    OUTPUT_MD.write_text(build_markdown(report), encoding='utf-8')
    print(
        json.dumps(
            {
                'status': 'ok',
                'json': str(OUTPUT_JSON),
                'md': str(OUTPUT_MD),
                'remaining_labeled_holdout_days_count': report['independent_oos_blocker']['remaining_labeled_holdout_days_count'],
                'effective_days_overlapping_last_upstream_test_fold': report['upstream_data_reuse']['effective_days_overlapping_last_upstream_test_fold'],
                'analog_windows_with_portfolio_return_payload': report['analog_return_level_blocker']['windows_with_portfolio_return_payload'],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
