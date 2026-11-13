#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
TZ = ZoneInfo('Asia/Shanghai')

CLAIM_PATH = EVOLUTION / 'current_combo_claim_limit_audit.json'
CLAIM_MD_PATH = EVOLUTION / 'current_combo_claim_limit_audit.md'
NESTED_PATH = EVOLUTION / 'current_combo_nested_validation.json'
NESTED_MD_PATH = EVOLUTION / 'current_combo_nested_validation.md'
NESTED_LABEL_MODE_PATHS = {
    'close_to_close': NESTED_PATH,
    'open_to_close': EVOLUTION / 'current_combo_nested_validation.o2c_canonical.json',
    'close_to_open': EVOLUTION / 'current_combo_nested_validation.c2o_canonical.json',
}
WALKFORWARD_LABEL_MODE_PATHS = {
    'close_to_close': EVOLUTION / 'current_combo_walkforward_validation.json',
    'open_to_close': EVOLUTION / 'current_combo_walkforward_validation.o2c_canonical.json',
    'close_to_open': EVOLUTION / 'current_combo_walkforward_validation.c2o_canonical.json',
}
VALIDATION_PATH = EVOLUTION / 'factor_validation_report.json'
PORTFOLIO_PATH = EVOLUTION / 'portfolio_backtest_report.json'
OVERFIT_PATH = EVOLUTION / 'overfit_audit_report.json'
PROMOTION_PATH = EVOLUTION / 'factor_promotion_decision.json'
ITERATION_POOL_PATH = EVOLUTION / 'factor_iteration_pool.json'
FACTOR_LINEAGE_PATH = EVOLUTION / 'factor_lineage.json'
DIGEST_PATH = EVOLUTION / 'daily_factor_evolution_digest.md'
CURRENT_MARKET_PATH = EVOLUTION / 'current_market_fingerprint.json'
ANALOG_WINDOWS_PATH = EVOLUTION / 'regime_analog_windows.json'
ANALOG_REPORT_PATH = EVOLUTION / 'analog_validation_report.json'
ALIGNMENT_PATH = EVOLUTION / 'latest_weight_plan_analog_alignment.json'
COMPARISON_PATH = EVOLUTION / 'multi_label_canonical_comparison.json'
PANEL_PATH = ROOT / 'data' / 'factors' / 'panel_phase1.parquet'
SELECTION_HISTORY_DIR = Path('/Users/fisher/.openclaw/workspace/stock_data/03-working/selection_history')
SELECTION_GAP_ATTRIBUTION_PATH = EVOLUTION / 'selection_history_gap_attribution.json'
HOLDOUT_RESERVE_PATH = EVOLUTION / 'current_combo_holdout_reserve.json'

LABEL_CONFIG = {
    'close_to_close': {
        'label_column': 'next_return_close_to_close_1d',
        'result_key': 'close_to_close_combo',
        'replay_path': EVOLUTION / 'current_combo_replay_report.json',
        'backtest_path': EVOLUTION / 'current_combo_portfolio_backtest.json',
        'weight_map_path': ROOT / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json',
        'factor_audit_path': ROOT / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json',
    },
    'open_to_close': {
        'label_column': 'next_return_open_to_close_1d',
        'result_key': 'open_to_close_combo',
        'replay_path': EVOLUTION / 'current_combo_replay_report.o2c_canonical.json',
        'backtest_path': EVOLUTION / 'current_combo_portfolio_backtest.o2c_canonical.json',
        'weight_map_path': ROOT / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.o2c_full45_33d.json',
        'factor_audit_path': ROOT / 'outputs' / 'audit_reports' / 'factor_audit_phase1.o2c_full45_33d.json',
    },
    'close_to_open': {
        'label_column': 'next_return_close_to_open_1d',
        'result_key': 'close_to_open_combo',
        'replay_path': EVOLUTION / 'current_combo_replay_report.c2o_canonical.json',
        'backtest_path': EVOLUTION / 'current_combo_portfolio_backtest.c2o_canonical.json',
        'weight_map_path': ROOT / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.c2o_full45_33d.json',
        'factor_audit_path': ROOT / 'outputs' / 'audit_reports' / 'factor_audit_phase1.c2o_full45_33d.json',
    },
}
EXCESS_KEY = {
    'close_to_close': 'close_to_close_excess_total_return',
    'open_to_close': 'open_to_close_excess_total_return',
    'close_to_open': 'close_to_open_excess_total_return',
}
LABEL_MODE_TO_SHORT = {
    'close_to_close': 'c2c',
    'open_to_close': 'o2c',
    'close_to_open': 'c2o',
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def iso_mtime(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None


def direction_of(value: float | None) -> str:
    if value is None:
        return 'na'
    value = float(value)
    if value > 1e-12:
        return 'positive'
    if value < -1e-12:
        return 'negative'
    return 'flat'


def fmt_pct(value: float | None) -> str:
    if value is None:
        return 'NA'
    return f'{float(value):.4%}'


def load_optional_json(path: Path) -> dict:
    return load_json(path) if path.exists() else {}


def canonical_refresh_basis_patch_from_comparison(comparison: dict) -> dict[str, object]:
    labels = comparison.get('labels', {}) if isinstance(comparison, dict) else {}
    c2c = labels.get('close_to_close', {}) if isinstance(labels, dict) else {}
    o2c = labels.get('open_to_close', {}) if isinstance(labels, dict) else {}
    c2o = labels.get('close_to_open', {}) if isinstance(labels, dict) else {}
    return {
        'audit_mtime': c2c.get('audit_mtime'),
        'weight_mtime': c2c.get('weight_mtime'),
        'label_mode': c2c.get('label_mode') or 'close_to_close',
        'label_column': c2c.get('label_column'),
        'benchmark_eligible_trade_days': c2c.get('benchmark_eligible_trade_days'),
        'benchmark_eligible_row_count': c2c.get('benchmark_eligible_row_count'),
        'multi_label_audit_mtimes': {
            label: payload.get('audit_mtime')
            for label, payload in labels.items()
            if isinstance(payload, dict) and payload.get('audit_mtime')
        },
        'multi_label_weight_mtimes': {
            label: payload.get('weight_mtime')
            for label, payload in labels.items()
            if isinstance(payload, dict) and payload.get('weight_mtime')
        },
        'open_to_close_audit_mtime': o2c.get('audit_mtime'),
        'open_to_close_weight_mtime': o2c.get('weight_mtime'),
        'close_to_open_audit_mtime': c2o.get('audit_mtime'),
        'close_to_open_weight_mtime': c2o.get('weight_mtime'),
        'multi_label_comparison_mtime': iso_mtime(COMPARISON_PATH),
    }


def format_mode_list(modes: list[str]) -> str:
    return ' / '.join(modes) if modes else '无'


def format_label_modes(modes: list[str]) -> str:
    labels = [LABEL_MODE_TO_SHORT.get(str(mode), str(mode)) for mode in modes if mode]
    return ' / '.join(labels) if labels else '无'


def build_analog_scope_summary(analog_report: dict) -> dict[str, object]:
    validation_scope = analog_report.get('validation_scope', {}) or {}
    replay = analog_report.get('current_combo_return_level_analog_replay', {}) or {}
    validation_status = analog_report.get('validation_status', {}) or {}
    analog_sample_summary = analog_report.get('analog_sample_summary', {}) or {}
    completed_label_modes = [str(mode) for mode in replay.get('completed_label_modes', []) if mode]
    completed_label_modes_text = format_label_modes(completed_label_modes)
    analog_window_count = int(
        analog_sample_summary.get('analog_window_count')
        or validation_status.get('proxy_analog_window_count')
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
        blocker_summary = (
            f'analog_validation 已具备 {completed_label_modes_text} 收益级 analog replay，'
            '但它仍是 frozen-current-plan 的历史类比验证，不是独立 holdout / 独立 OOS'
        )
        gap_bullet = 'analog_validation_report.json 已提供收益级 analog replay，但该证据仍不是独立 holdout / 独立 OOS。'
        allowed_usage_line = f'用 analog_validation_report 作为 {completed_label_modes_text} 收益级 analog replay 的组合收益层补充验证（非独立 OOS）'
        engineering_todo = '在已具备收益级 analog replay 的基础上，优先等待/补齐 20260417+ 的 independent holdout 供给，并继续把 nested / walkforward 与该 holdout 联动；如仍需增强，再考虑历史权重重建与真实交易约束。'
        digest_line = (
            f'- analog_validation 已具备 {completed_label_modes_text} 收益级 analog replay '
            f'（usable windows={usable_portfolio_replay_sample_count}/{analog_window_count}），但仍不是独立 OOS。'
        )
        portfolio_gap_text = '收益级 analog replay 已具备，但它仍是 frozen-current-plan 的历史类比验证，不能替代独立 end-to-end OOS。'
        formal_backtest_claim_limit = 'Formal artifact 与 label-specific nested 已按当前控制面追平；收益级 analog replay 已具备，但仍没有独立 labeled holdout / end-to-end OOS。'
    else:
        claim_limit = 'analog_validation 仍只有 market-proxy overlap，尚未提供可复用的组合收益级 replay。'
        blocker_summary = 'analog_validation 仍只是 market-proxy'
        gap_bullet = 'analog_validation_report.json 仍明确只是 market-proxy analog，不是组合收益 replay。'
        allowed_usage_line = '用 analog_validation_report 作为市场情境与因子 overlap 的 proxy 参考'
        engineering_todo = '把 analog_validation 从 market-proxy overlap 扩成收益级 analog replay。'
        digest_line = '- analog_validation 仍只是 market-proxy，不是收益级 analog replay。'
        portfolio_gap_text = '也没有独立 end-to-end OOS 或收益级 analog replay。'
        formal_backtest_claim_limit = 'Formal artifact 与 label-specific nested 已按当前控制面追平；但仍没有独立 labeled holdout / end-to-end OOS / portfolio-level analog replay。'
    scope_note = validation_scope.get('official_vs_alternate_scope_note') or claim_limit
    return {
        'return_level_analog_claim_allowed': return_level_analog_claim_allowed,
        'completed_label_modes': completed_label_modes,
        'completed_label_modes_text': completed_label_modes_text,
        'analog_window_count': analog_window_count,
        'usable_portfolio_replay_sample_count': usable_portfolio_replay_sample_count,
        'independent_oos_completed': independent_oos_completed,
        'claim_limit': claim_limit,
        'scope_note': scope_note,
        'blocker_summary': blocker_summary,
        'gap_bullet': gap_bullet,
        'allowed_usage_line': allowed_usage_line,
        'engineering_todo': engineering_todo,
        'digest_line': digest_line,
        'portfolio_gap_text': portfolio_gap_text,
        'formal_backtest_claim_limit': formal_backtest_claim_limit,
    }


def validation_mode_summary(report: dict, label_mode: str, path: Path) -> dict:
    aggregate = report.get('aggregate_oos', {})
    combo_total = (aggregate.get(f'{label_mode}_combo') or {}).get('total_return')
    excess_total = (aggregate.get(label_mode) or {}).get('excess_total_return')
    return {
        'path': str(path),
        'generated_at': report.get('generated_at'),
        'report_type': report.get('report_type'),
        'fold_scheme': report.get('fold_scheme'),
        'evidence_label': report.get('evidence_label'),
        'validation_scope': report.get('validation_scope'),
        'effective_clean_days': (report.get('sample_coverage') or {}).get('effective_clean_days'),
        'outer_oos_days': (report.get('sample_coverage') or {}).get('outer_oos_days'),
        'template_switch_count': (report.get('template_selection_summary') or {}).get('template_switch_count'),
        'primary_combo_total_return': combo_total,
        'primary_excess_total_return': excess_total,
        'primary_combo_direction': direction_of(combo_total),
        'primary_excess_direction': direction_of(excess_total),
        'sufficient_to_lift_promotion_freeze': (report.get('verdict') or {}).get('sufficient_to_lift_promotion_freeze'),
    }


def collect_nested_reports() -> tuple[dict[str, dict], list[str], list[str], dict[str, dict]]:
    reports: dict[str, dict] = {}
    summaries: dict[str, dict] = {}
    available_modes: list[str] = []
    for label_mode in LABEL_CONFIG:
        path = NESTED_LABEL_MODE_PATHS[label_mode]
        if not path.exists():
            continue
        payload = load_json(path)
        if not payload:
            continue
        reports[label_mode] = payload
        available_modes.append(label_mode)
        summaries[label_mode] = validation_mode_summary(payload, label_mode, path)
    missing_modes = [label_mode for label_mode in LABEL_CONFIG if label_mode not in reports]
    return reports, available_modes, missing_modes, summaries


def collect_walkforward_reports() -> tuple[dict[str, dict], list[str], list[str], dict[str, dict]]:
    reports: dict[str, dict] = {}
    summaries: dict[str, dict] = {}
    available_modes: list[str] = []
    for label_mode in LABEL_CONFIG:
        path = WALKFORWARD_LABEL_MODE_PATHS[label_mode]
        if not path.exists():
            continue
        payload = load_json(path)
        if not payload:
            continue
        reports[label_mode] = payload
        available_modes.append(label_mode)
        summaries[label_mode] = validation_mode_summary(payload, label_mode, path)
    missing_modes = [label_mode for label_mode in LABEL_CONFIG if label_mode not in reports]
    return reports, available_modes, missing_modes, summaries


def proxy_coverage_count(analog_windows: dict, factor_ids: list[str]) -> int:
    target = set(map(str, factor_ids))
    if not target:
        return 0
    covered = 0
    for window in analog_windows.get('analog_windows', []):
        current = {str(item.get('factor_id')) for item in window.get('effective_factor_proxies', []) if item.get('factor_id')}
        if target.issubset(current):
            covered += 1
    return covered


def label_mode_claim_scope(panel: pd.DataFrame, comparison: dict, analog_windows: dict) -> tuple[dict, list[str], list[str], list[str], list[str], list[str]]:
    scopes: dict[str, dict] = {}
    eligible_dates_union: list[str] = []
    remaining_union: set[str] = set()
    unlabeled_union: set[str] = set()
    reserved_union: set[str] = set()
    reserved_pending_union: set[str] = set()
    reserved_now_labeled_union: set[str] = set()
    for mode, cfg in LABEL_CONFIG.items():
        replay = load_json(cfg['replay_path'])
        backtest = load_json(cfg['backtest_path'])
        result_key = cfg['result_key']
        benchmark_summary = replay['benchmark_eligible_summary']
        eligible_dates = list(benchmark_summary['eligible_dates'])
        consumable_eligible_dates = list(benchmark_summary.get('consumable_eligible_dates', eligible_dates))
        reserved_holdout_dates = sorted(set(benchmark_summary.get('reserved_holdout_dates', [])))
        eligible_dates_union = eligible_dates_union or eligible_dates
        labeled_days = sorted(
            d for d, value in panel.groupby('trade_date')[cfg['label_column']].apply(lambda s: int(s.notna().sum())).to_dict().items()
            if value > 0 and d in eligible_dates
        )
        used_days = list(replay['replay_results'][result_key]['effective_trade_dates'])
        remaining_days = sorted(set(labeled_days) - set(used_days))
        unlabeled_dates = sorted(set(eligible_dates) - set(labeled_days))
        reserved_holdout_pending_labels = sorted(set(reserved_holdout_dates) - set(labeled_days))
        reserved_holdout_now_labeled = sorted(set(reserved_holdout_dates) & set(labeled_days))
        remaining_union.update(remaining_days)
        unlabeled_union.update(unlabeled_dates)
        reserved_union.update(reserved_holdout_dates)
        reserved_pending_union.update(reserved_holdout_pending_labels)
        reserved_now_labeled_union.update(reserved_holdout_now_labeled)
        latest_factor_ids = comparison['labels'][mode]['latest_weight_plan_factor_ids']
        primary_combo = backtest['minimum_required_metrics'][result_key]['total_return']
        primary_excess = backtest['comparisons'][EXCESS_KEY[mode]]
        without_backfill = (
            backtest.get('sensitivity', {})
            .get('comparisons_without_backfill', {})
            .get(EXCESS_KEY[mode])
        )
        scopes[mode] = {
            'label_mode': mode,
            'label_column': cfg['label_column'],
            'selection_mode': replay['current_combo_definition']['selection_mode'],
            'latest_weight_plan_factor_ids': latest_factor_ids,
            'regime_union_factor_ids': replay['current_combo_definition']['regime_union_factor_ids'],
            'replay_path': str(cfg['replay_path']),
            'formal_backtest_path': str(cfg['backtest_path']),
            'weight_map_path': str(cfg['weight_map_path']),
            'factor_audit_path': str(cfg['factor_audit_path']),
            'replay_generated_at': replay['generated_at'],
            'formal_backtest_generated_at': backtest['generated_at'],
            'eligible_trade_days': len(eligible_dates),
            'consumable_eligible_trade_days': int(benchmark_summary.get('consumable_eligible_trade_days') or len(consumable_eligible_dates)),
            'consumable_eligible_dates': consumable_eligible_dates,
            'labeled_eligible_days': len(labeled_days),
            'used_labeled_days': len(used_days),
            'remaining_labeled_holdout_days': remaining_days,
            'remaining_labeled_holdout_days_count': len(remaining_days),
            'eligible_but_unlabeled_dates': unlabeled_dates,
            'reserved_holdout_dates': reserved_holdout_dates,
            'reserved_holdout_dates_pending_labels': reserved_holdout_pending_labels,
            'reserved_holdout_dates_now_labeled': reserved_holdout_now_labeled,
            'effective_replay_days': len(used_days),
            'primary_combo_total_return': primary_combo,
            'primary_excess_total_return': primary_excess,
            'primary_combo_direction': direction_of(primary_combo),
            'primary_excess_direction': direction_of(primary_excess),
            'primary_without_backfill_excess_total_return': without_backfill,
            'proxy_covered_windows': proxy_coverage_count(analog_windows, latest_factor_ids),
            'sufficient_to_claim_independent_oos': False,
        }
    return (
        scopes,
        sorted(remaining_union),
        sorted(unlabeled_union),
        sorted(reserved_union),
        sorted(reserved_pending_union),
        sorted(reserved_now_labeled_union),
    )


def benchmark_constituent_retention(panel: pd.DataFrame) -> dict:
    benchmark_panel = panel.copy()
    benchmark_panel['benchmark_eligible'] = benchmark_panel['benchmark_eligible'].fillna(0).astype(int)
    panel_counts = (
        benchmark_panel.loc[benchmark_panel['benchmark_eligible'] == 1]
        .groupby('trade_date')
        .size()
        .to_dict()
    )

    daily_rows: list[dict] = []
    raw_total_rows = 0
    retained_total_rows = 0
    for path in sorted(SELECTION_HISTORY_DIR.glob('*.json')):
        payload = load_optional_json(path)
        raw_codes = [
            str(item.get('ts_code') or item.get('code') or '').strip()
            for item in (payload.get('stocks') or [])
            if str(item.get('ts_code') or item.get('code') or '').strip()
        ]
        trade_date = path.stem
        raw_count = len(raw_codes)
        retained_count = int(panel_counts.get(trade_date, 0))
        raw_total_rows += raw_count
        retained_total_rows += retained_count
        daily_rows.append(
            {
                'trade_date': trade_date,
                'selection_history_rows': raw_count,
                'panel_benchmark_rows': retained_count,
                'dropped_rows': max(raw_count - retained_count, 0),
            }
        )

    worst_days = [
        item for item in sorted(
            daily_rows,
            key=lambda item: (item['dropped_rows'], item['selection_history_rows'], item['trade_date']),
            reverse=True,
        )
        if item['dropped_rows'] > 0
    ][:5]
    retained_ratio = retained_total_rows / raw_total_rows if raw_total_rows else None
    return {
        'selection_history_trade_days': len(daily_rows),
        'selection_history_total_rows': raw_total_rows,
        'panel_benchmark_total_rows': retained_total_rows,
        'dropped_rows_vs_selection_history': max(raw_total_rows - retained_total_rows, 0),
        'retained_ratio': retained_ratio,
        'worst_dropped_trade_dates': worst_days,
        'summary': (
            f'selection_history 原始成分共 {raw_total_rows} 行，panel benchmark_eligible 仅保留 {retained_total_rows} 行，'
            f'保留率={fmt_pct(retained_ratio)}。'
        ),
    }


def build_selection_gap_context(selection_gap: dict, retention_audit: dict) -> dict[str, object]:
    reason_counts = selection_gap.get('reason_counts', {}) if isinstance(selection_gap, dict) else {}
    dropped_rows = int(selection_gap.get('dropped_rows') or retention_audit.get('dropped_rows_vs_selection_history') or 0)
    liquidity_rows = int(reason_counts.get('universe_liquidity_threshold') or 0)
    listing_days_rows = int(reason_counts.get('universe_listing_days') or 0)
    min_price_rows = int(reason_counts.get('universe_min_price') or 0)
    mapping_reference_gaps = list(selection_gap.get('mapping_reference_gaps') or []) if isinstance(selection_gap, dict) else []
    stock_list_master_gap_rows = len(mapping_reference_gaps)
    base_summary = (
        f'{dropped_rows} 行缺口已量化为 {liquidity_rows} liquidity + '
        f'{listing_days_rows} listing_days + {min_price_rows} min_price'
        if reason_counts
        else retention_audit.get('summary', '')
    )
    if base_summary.endswith('。'):
        base_summary = base_summary[:-1]
    master_gap_note = (
        f'其中 listing_days 里有 {stock_list_master_gap_rows} 行来自 stock_list.csv 主数据缺口（list_date 缺失）。'
        if stock_list_master_gap_rows
        else ''
    )
    short_summary = f'{base_summary}；{master_gap_note}' if master_gap_note else f'{base_summary}。'
    coverage_risk_summary = f'{short_summary.rstrip("。")}；当前 clean benchmark 仍不是 33×20 的全量 base 成分回放。'
    if stock_list_master_gap_rows:
        next_step = (
            f'优先补 stock_list.csv 中 {stock_list_master_gap_rows} 个 list_date 主数据缺口，区分真实 listing_days 不足与主数据缺口导致的伪 listing_days fail。'
        )
    elif liquidity_rows or min_price_rows:
        next_step = '把主阻塞明确收敛到独立 labeled holdout 供给；rolling / walkforward gate 已存在，selection_history→panel 剩余缺口已以 liquidity/min_price 等 universe 规则为主，reference/master data 仅保留为低优先级口径清理。'
    else:
        next_step = '继续核实 selection_history→panel constituent gap 的上游 reference/master data 完整性。'
    return {
        'path': str(SELECTION_GAP_ATTRIBUTION_PATH),
        'present': bool(selection_gap),
        'dropped_rows': dropped_rows,
        'reason_counts': reason_counts,
        'stock_list_master_gap_rows': stock_list_master_gap_rows,
        'mapping_reference_gaps': mapping_reference_gaps,
        'short_summary': short_summary,
        'coverage_risk_summary': coverage_risk_summary,
        'master_gap_note': master_gap_note,
        'digest_artifact_line': f'- 已补充 selection_history→panel gap 细粒度归因：{short_summary}',
        'digest_gap_line': f'- selection_history {coverage_risk_summary}',
        'next_step': next_step,
    }


def rewrite_claim_markdown(claim: dict) -> str:
    overlap = claim.get('upstream_data_reuse', {})
    analog = claim.get('analog_return_level_blocker', {})
    analog_heading = '## analog 收益级验证现状' if analog.get('return_level_analog_claim_allowed') else '## analog 仍不是收益级验证'
    blocks = [
        '# Current Combo Claim-Limit Audit',
        '',
        f"- 生成时间：{claim['generated_at']}",
        f"- 结论：{claim['summary']['headline']}",
        '',
        '## 三口径 claim scope',
    ]
    for mode in ['close_to_close', 'open_to_close', 'close_to_open']:
        item = claim['label_mode_claim_scope'][mode]
        blocks.extend([
            (
                f"- {mode}: combo={fmt_pct(item['primary_combo_total_return'])}, "
                f"excess={fmt_pct(item['primary_excess_total_return'])}, "
                f"holdout={item['remaining_labeled_holdout_days_count']}, "
                f"latest_plan={item['latest_weight_plan_factor_ids']}"
            ),
            (
                f"  - labeled/used={item['used_labeled_days']}/{item['labeled_eligible_days']} | "
                f"unlabeled={item['eligible_but_unlabeled_dates'] or ['无']} | "
                f"reserved={item.get('reserved_holdout_dates') or ['无']} | "
                f"pending_labels={item.get('reserved_holdout_dates_pending_labels') or ['无']} | "
                f"reserved_now_labeled={item.get('reserved_holdout_dates_now_labeled') or ['无']}"
            ),
        ])
    blocks.extend([
        '',
        '## 统一阻塞',
        f"- 共用 eligible trade days：{claim['independent_oos_blocker']['eligible_trade_days']}",
        f"- 剩余独立 labeled holdout days：{claim['independent_oos_blocker']['remaining_labeled_holdout_days_count']}",
        f"- 无标签 eligible 日期：{claim['independent_oos_blocker']['eligible_but_unlabeled_dates'] or ['无']}",
        f"- 已预留 holdout reserve：{claim['independent_oos_blocker'].get('reserved_holdout_dates') or ['无']}",
        f"- reserve 中待 next-day label：{claim['independent_oos_blocker'].get('reserved_holdout_dates_pending_labels') or ['无']}",
        f"- reserve 中已形成 labeled holdout：{claim['independent_oos_blocker'].get('reserved_holdout_dates_now_labeled') or ['无']}",
        f"- 为什么仍不能宣称 independent OOS：{claim['independent_oos_blocker']['why_not_independent_oos']}",
        '',
        '## upstream 数据复用量化',
    ])
    if overlap.get('by_label_mode'):
        for mode in ['close_to_close', 'open_to_close', 'close_to_open']:
            item = overlap['by_label_mode'].get(mode)
            if not item:
                continue
            blocks.append(
                f"- {mode}: any_test_fold={item['effective_days_overlapping_any_upstream_test_fold']}/{item['effective_replay_days']}, "
                f"last_test_fold={item['effective_days_overlapping_last_upstream_test_fold']}/{item['effective_replay_days']}, "
                f"outside_last_test_fold={item['effective_dates_outside_last_test_fold'] or ['无']}"
            )
    else:
        blocks.append(
            f"- close_to_close: any_test_fold={overlap.get('effective_days_overlapping_any_upstream_test_fold', 0)}/{overlap.get('effective_replay_days', 0)}, "
            f"last_test_fold={overlap.get('effective_days_overlapping_last_upstream_test_fold', 0)}/{overlap.get('effective_replay_days', 0)}, "
            f"outside_last_test_fold={overlap.get('effective_dates_outside_last_test_fold', []) or ['无']}"
        )
    blocks.extend([
        f"- 注：{overlap.get('note', '无')}",
        '',
        analog_heading,
        f"- windows_with_portfolio_return_payload={analog.get('windows_with_portfolio_return_payload')}",
        f"- usable_portfolio_replay_sample_count={analog.get('usable_portfolio_replay_sample_count')}",
        f"- completed_label_modes={analog.get('completed_label_modes', [])}",
        f"- claim_limit={analog.get('claim_limit', '无')}",
        f"- scope_note={analog.get('scope_note', '无')}",
        f"- latest_weight_plan_proxy_covered_windows_by_label_mode={json.dumps(analog.get('latest_weight_plan_proxy_covered_windows_by_label_mode', {}), ensure_ascii=False)}",
        '',
        '## 可宣称边界',
        f"- independent OOS：{claim['summary']['independent_oos_claim_allowed']}",
        f"- 收益级 analog validation：{claim['summary']['return_level_analog_claim_allowed']}",
        f"- 允许表述：{claim['summary']['allowed_claim']}",
        '',
    ])
    return '\n'.join(blocks)


def rewrite_nested_markdown(nested: dict) -> str:
    verdict = nested['verdict']
    gap = nested['alternate_label_mode_gap']
    lines = [
        '# Current Combo Nested Validation Report',
        '',
        f"- 生成时间：{nested['generated_at']}",
        f"- 验证口径：{nested['validation_scope']}",
        f"- applies_to_label_modes={nested['applies_to_label_modes']}",
    ]
    reports = nested.get('label_mode_nested_reports', {})
    if reports:
        lines.extend(['', '## label-specific nested 覆盖'])
        for label_mode in LABEL_CONFIG:
            item = reports.get(label_mode)
            if not item:
                continue
            lines.append(
                f"- {label_mode}: combo={fmt_pct(item['primary_combo_total_return'])}, "
                f"excess={fmt_pct(item['primary_excess_total_return'])}, "
                f"outer_oos_days={item['outer_oos_days']}, "
                f"template_switch_count={item['template_switch_count']}, "
                f"path={item['path']}"
            )
    else:
        agg = nested['aggregate_oos']
        lines.extend(
            [
                '',
                '## c2c micro nested 聚合结果',
                f"- c2c combo={fmt_pct(agg['close_to_close_combo']['total_return'])}, excess={fmt_pct(agg['close_to_close']['excess_total_return'])}, days={agg['close_to_close_combo']['days']}",
                f"- o2c cross-check={fmt_pct(agg['open_to_close_combo']['total_return'])}, excess={fmt_pct(agg['open_to_close']['excess_total_return'])}, days={agg['open_to_close_combo']['days']}",
            ]
        )
    lines.extend(
        [
            '',
            '## alternate label mode gap',
            f"- 状态：{gap['status']}",
            f"- 缺失 label-specific nested modes：{gap['missing_label_specific_nested_modes']}",
            f"- common_latest_weight_plan_factor_ids：{gap['common_latest_weight_plan_factor_ids']}",
            f"- sign_unstable_factor_ids：{gap['sign_unstable_factor_ids']}",
            f"- 结论：{gap['summary']}",
            '',
            '## 放行结论',
            f"- sufficient_to_lift_promotion_freeze={verdict['sufficient_to_lift_promotion_freeze']}",
            f"- summary={verdict['summary']}",
            '',
        ]
    )
    return '\n'.join(lines)


def main() -> int:
    now = iso_now()
    claim = load_json(CLAIM_PATH)
    nested = load_json(NESTED_PATH)
    validation = load_json(VALIDATION_PATH)
    portfolio = load_json(PORTFOLIO_PATH)
    overfit = load_json(OVERFIT_PATH)
    promotion = load_json(PROMOTION_PATH)
    pool = load_json(ITERATION_POOL_PATH)
    lineage = load_json(FACTOR_LINEAGE_PATH)
    current_market = load_optional_json(CURRENT_MARKET_PATH)
    analog_windows = load_json(ANALOG_WINDOWS_PATH)
    analog_report = load_json(ANALOG_REPORT_PATH)
    comparison = load_json(COMPARISON_PATH)
    canonical_basis_patch = canonical_refresh_basis_patch_from_comparison(comparison)
    selection_gap = load_optional_json(SELECTION_GAP_ATTRIBUTION_PATH)
    primary_audit_path = LABEL_CONFIG['close_to_close']['factor_audit_path']
    primary_weight_path = LABEL_CONFIG['close_to_close']['weight_map_path']
    primary_audit_mtime = canonical_basis_patch.get('audit_mtime') or iso_mtime(primary_audit_path)
    primary_weight_mtime = canonical_basis_patch.get('weight_mtime') or iso_mtime(primary_weight_path)
    portfolio_source_paths = portfolio.setdefault('source_paths', {})
    attribution_path_str = portfolio_source_paths.get('attribution_report') or portfolio_source_paths.get('attrib')
    attribution_path = Path(attribution_path_str).expanduser().resolve() if attribution_path_str else None
    attribution_mtime = iso_mtime(attribution_path) if attribution_path is not None and attribution_path.exists() else None
    attribution_aligned = bool(
        attribution_path is not None
        and attribution_path.exists()
        and primary_audit_path.exists()
        and primary_weight_path.exists()
        and attribution_path.stat().st_mtime >= max(primary_audit_path.stat().st_mtime, primary_weight_path.stat().st_mtime)
    )

    panel = pd.read_parquet(
        PANEL_PATH,
        columns=['trade_date', 'benchmark_eligible', 'next_return_close_to_close_1d', 'next_return_open_to_close_1d', 'next_return_close_to_open_1d'],
    )
    panel['trade_date'] = panel['trade_date'].astype(str)

    claim_scope, remaining_union, unlabeled_union, reserved_union, reserved_pending_union, reserved_now_labeled_union = label_mode_claim_scope(panel, comparison, analog_windows)
    replay_payloads = {
        mode: load_json(cfg['replay_path'])
        for mode, cfg in LABEL_CONFIG.items()
    }
    backtest_payloads = {
        mode: load_json(cfg['backtest_path'])
        for mode, cfg in LABEL_CONFIG.items()
    }
    primary_replay = replay_payloads['close_to_close']
    primary_backtest = backtest_payloads['close_to_close']
    retention_audit = benchmark_constituent_retention(panel)
    selection_gap_context = build_selection_gap_context(selection_gap, retention_audit)
    nested_reports, available_nested_modes, missing_nested_modes, nested_mode_reports = collect_nested_reports()
    walkforward_reports, available_walkforward_modes, missing_walkforward_modes, walkforward_mode_reports = collect_walkforward_reports()
    available_nested_modes = available_nested_modes or ['close_to_close']
    missing_nested_modes = [label_mode for label_mode in LABEL_CONFIG if label_mode not in available_nested_modes]
    available_nested_modes_text = format_mode_list(available_nested_modes)
    missing_nested_modes_text = format_mode_list(missing_nested_modes)
    available_walkforward_modes_text = format_mode_list(available_walkforward_modes)
    missing_walkforward_modes_text = format_mode_list(missing_walkforward_modes)
    walkforward_complete = bool(walkforward_reports) and not missing_walkforward_modes
    primary_scope = claim_scope.get('close_to_close', {})
    benchmark_days = primary_scope.get('eligible_trade_days')
    benchmark_days_text = str(benchmark_days) if benchmark_days is not None else 'NA'
    replay_days = primary_scope.get('effective_replay_days')
    replay_days_text = str(replay_days) if replay_days is not None else 'NA'
    same_clean_window_text = f'同一 {benchmark_days_text} 天 clean window'
    replay_usage_text = f'{replay_days_text}/{replay_days_text}' if replay_days is not None else 'NA/NA'
    unlabeled_dates_text = ' / '.join(unlabeled_union) if unlabeled_union else '无'
    reserve_dates_text = ' / '.join(reserved_union) if reserved_union else '无'
    reserve_pending_text = ' / '.join(reserved_pending_union) if reserved_pending_union else '无'
    reserve_now_labeled_text = ' / '.join(reserved_now_labeled_union) if reserved_now_labeled_union else '无'
    unlabeled_clause = (
        f'剩余 {unlabeled_dates_text} 只有 benchmark_eligible、没有 next-day 标签'
        if unlabeled_union
        else '当前也没有额外 benchmark_eligible 但缺 next-day 标签的日期'
    )
    if reserved_union:
        if reserved_pending_union:
            reserve_headline_fragment = f'并显式预留 holdout reserve={reserve_dates_text}；'
            holdout_status_summary = (
                f'current labeled holdout={len(remaining_union)}；已显式预留 holdout reserve={reserve_dates_text}；'
                f'待 next-day label={reserve_pending_text}；已形成 labeled holdout={reserve_now_labeled_text}'
            )
            holdout_allowed_claim_tail = '但 reserve 中待 next-day label 的日期仍不是已形成的独立 labeled holdout，不能升级为独立 OOS或解除 promotion freeze。'
            holdout_oos_tail = '因此当前仍不能把 reserve 包装成已形成的独立 labeled holdout / 独立 OOS。'
            holdout_engineering_todo = '已显式预留 holdout reserve；下一步是补齐/等待 next-day 标签并继续确保该 reserve 不被 replay/backtest/nested/walkforward 消耗。'
            holdout_reserve_digest_line = (
                f'- 已同步 current_combo_holdout_reserve.json：{reserve_dates_text} 已显式预留为 holdout reserve；'
                f'当前 pending label={reserve_pending_text}，已形成 labeled holdout={reserve_now_labeled_text}。'
            )
            holdout_reserve_reason = (
                f'按 current_combo claim-limit 控制面显式预留 holdout reserve={reserve_dates_text}；'
                f'其中待 next-day label={reserve_pending_text}，已形成 labeled holdout={reserve_now_labeled_text}；'
                '这些日期继续排除在 replay/backtest/nested/walkforward 消耗之外。'
            )
            holdout_reserve_boundary = (
                f'current labeled holdout={len(remaining_union)}；reserved_holdout_dates_pending_labels={reserve_pending_text}；'
                f'reserved_holdout_dates_now_labeled={reserve_now_labeled_text}；该 reserve 仍不构成 independent OOS，不放松 promotion freeze。'
            )
        else:
            reserve_headline_fragment = f'并显式同步 reserved and now labeled holdout={reserve_dates_text}；'
            holdout_status_summary = (
                f'current labeled holdout={len(remaining_union)}；已显式预留 holdout reserve={reserve_dates_text}；'
                f'reserved_holdout_dates_pending_labels=无；reserved_holdout_dates_now_labeled={reserve_now_labeled_text}'
            )
            holdout_allowed_claim_tail = '但 reserve 中已形成 labeled holdout 的日期仍不能被包装成独立 OOS或解除 promotion freeze。'
            holdout_oos_tail = '因此当前仍不能把 reserve 中已形成 labeled holdout 的日期包装成独立 labeled holdout / 独立 OOS。'
            holdout_engineering_todo = '已形成 reserved and now labeled holdout；下一步是继续确保该 holdout 不被 replay/backtest/nested/walkforward 消耗，并优先等待/补齐 20260417+ 的后续 independent holdout 供给。'
            holdout_reserve_digest_line = (
                f'- 已同步 current_combo_holdout_reserve.json：{reserve_dates_text} 当前已成为 reserved and now labeled holdout；'
                f'pending label=无，已形成 labeled holdout={reserve_now_labeled_text}。'
            )
            holdout_reserve_reason = (
                f'按 current_combo claim-limit 控制面同步：{reserve_dates_text} 已成为 reserved and now labeled holdout；'
                f'current labeled holdout={len(remaining_union)}，reserved_holdout_dates_now_labeled={reserve_now_labeled_text}；'
                '这些日期继续排除在 replay/backtest/nested/walkforward 消耗之外。'
            )
            holdout_reserve_boundary = (
                f'current labeled holdout={len(remaining_union)}；reserved_holdout_dates_pending_labels=无；'
                f'reserved_holdout_dates_now_labeled={reserve_now_labeled_text}；该 reserve / 已形成 holdout 仍不构成 independent OOS，不放松 promotion freeze。'
            )
    dump_json(HOLDOUT_RESERVE_PATH, {
        'schema_version': '2026-04-20.current_combo_holdout_reserve.v1',
        'generated_at': now,
        'reserved_holdout_dates': reserved_union,
        'reserved_holdout_dates_pending_labels': reserved_pending_union,
        'reserved_holdout_dates_now_labeled': reserved_now_labeled_union,
        'current_labeled_holdout_days': remaining_union,
        'current_labeled_holdout_days_count': len(remaining_union),
        'reason': holdout_reserve_reason,
        'claim_boundary': holdout_reserve_boundary,
        'source_evidence': [
            '/Users/fisher/.openclaw/workspace/memory/core/ACTIVE_TASKS.md',
            str(CLAIM_PATH),
            str(WALKFORWARD_LABEL_MODE_PATHS['close_to_close']),
            str(WALKFORWARD_LABEL_MODE_PATHS['open_to_close']),
            str(WALKFORWARD_LABEL_MODE_PATHS['close_to_open']),
        ],
    })
    holdout_release_summary = f'{holdout_status_summary}；{unlabeled_clause}'
    walkforward_scope_label = 'same-window 组合级 walkforward 骨架验证'
    replay_usage_text = f'{replay_days_text}/{replay_days_text}' if replay_days is not None else 'NA/NA'
    claim_limit_by_mode_text = ' / '.join(
        f"{LABEL_MODE_TO_SHORT.get(mode, mode)}={item['remaining_labeled_holdout_days_count']}"
        for mode, item in claim_scope.items()
    )
    analog_scope = build_analog_scope_summary(analog_report)
    nested_scope_summary = (
        f'current_combo_nested_validation 已覆盖 {available_nested_modes_text} 的 label-specific nested；'
        if missing_nested_modes
        else 'current_combo_nested_validation 已覆盖 c2c / o2c / c2o 三口径 label-specific nested；'
    )
    walkforward_scope_summary = (
        f'current_combo_walkforward_validation 已覆盖 c2c / o2c / c2o 三口径{walkforward_scope_label}；'
        if walkforward_complete
        else (
            f'current_combo_walkforward_validation 已覆盖 {available_walkforward_modes_text} 的{walkforward_scope_label}；'
            if walkforward_reports
            else f'current_combo_walkforward_validation 仍未形成可复用的三口径{walkforward_scope_label}；'
        )
    )
    nested_gap_summary = (
        f'{nested_scope_summary}{missing_nested_modes_text} 仍缺 label-specific nested，且 {analog_scope["blocker_summary"]}。'
        if missing_nested_modes
        else f'三口径 nested 已全部具备，但 {holdout_status_summary}，且 {analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
    )
    walkforward_gap_summary = (
        f'三口径 walkforward 已全部具备，但它仍与{same_clean_window_text}共样本，不能替代独立 labeled holdout / 独立 OOS。'
        if walkforward_complete
        else (
            f'walkforward 当前仅覆盖 {available_walkforward_modes_text}，{missing_walkforward_modes_text} 仍缺 label-specific walkforward；'
            '现阶段仍不能把它包装成独立 labeled holdout / 独立 OOS。'
            if walkforward_reports
            else f'current_combo 仍缺三口径{walkforward_scope_label} artifact；现阶段不能把 nested 误写成真正 rolling OOS。'
        )
    )
    primary_holdout_gap = (
        f'三口径 walkforward 已全部具备，但{same_clean_window_text}内全部 {replay_days_text} 个有 next-day 标签的 effective replay days 已被用尽；{holdout_release_summary}。'
        if walkforward_complete
        else f'当前 clean window 仍只有 {benchmark_days_text} 个 benchmark eligible days，且 c2c / o2c / c2o 三口径都已用完全部 {replay_days_text} 个有 next-day 标签的 effective replay days；{holdout_release_summary}。'
    )
    engineering_todo_nested = (
        f"把 current_combo_nested_validation 补齐 {missing_nested_modes_text} 的 label-specific nested 证据，完成三口径放行链闭环。"
        if missing_nested_modes
        else (
            '继续把 nested / walkforward 结果与独立 holdout 联动，避免三口径 validation 仍停留在同一 clean-window 内自证。'
            if walkforward_complete
            else '继续把 nested 结果与独立 holdout / rolling OOS 联动，避免三口径 validation 仍停留在同一 clean-window 内自证。'
        )
    )
    selection_gap_next_step = (
        '把主阻塞明确收敛到独立 labeled holdout；walkforward 已补出，selection_history→panel 剩余缺口已以 liquidity/min_price 等 universe 规则为主，reference/master data 仅保留为低优先级口径清理。'
        if walkforward_complete and 'rolling OOS' in selection_gap_context.get('next_step', '')
        else selection_gap_context.get('next_step', '')
    )
    release_gap_summary = walkforward_gap_summary if walkforward_reports else nested_gap_summary

    def build_formal_backtest_summary(mode: str) -> dict[str, object]:
        item = claim_scope[mode]
        cross_checks = ' / '.join(
            f"{LABEL_MODE_TO_SHORT[label_mode]}={fmt_pct(claim_scope[label_mode]['primary_combo_total_return'])}"
            for label_mode in LABEL_CONFIG
            if label_mode in claim_scope
        )
        return {
            'path': item['formal_backtest_path'],
            'generated_at': item['formal_backtest_generated_at'],
            'combo_total_return': item['primary_combo_total_return'],
            'excess_total_return': item['primary_excess_total_return'],
            'effective_replay_days': item['effective_replay_days'],
            'combo_direction_vs_base': item['primary_combo_direction'],
            'excess_direction_vs_base': item['primary_excess_direction'],
            'sufficient_to_lift_promotion_freeze': False,
            'minimal_credible_conclusion': (
                f"当前 {mode} canonical 组合已补出 formal current-weight-map backtest：primary combo 总收益 {fmt_pct(item['primary_combo_total_return'])}、"
                f"相对 base 超额 {fmt_pct(item['primary_excess_total_return'])}；cross-check 为 {cross_checks}。"
                f"即便剔除回填日后 primary 超额仍为 {fmt_pct(item['primary_without_backfill_excess_total_return'])}，"
                f"该证据仍只覆盖 {item['eligible_trade_days']} 个 eligible trade days / {item['effective_replay_days']} 个有效回放日，"
                '不能替代独立 end-to-end backtest、独立 OOS 或 nested validation；'
                '收益级 analog replay 虽已具备，但仍只是类比验证而非独立 OOS，因此不足以解除 promotion freeze。'
            ),
        }

    formal_backtest_by_label_mode = {
        mode: build_formal_backtest_summary(mode)
        for mode in LABEL_CONFIG
        if mode in claim_scope
    }
    primary_replay_results = primary_replay['replay_results']
    primary_replay_comparisons = primary_replay['comparisons']
    primary_replay_without_backfill = primary_replay.get('sensitivity', {}).get('comparisons_without_backfill', {})
    primary_replay_without_backfill_metrics = primary_replay.get('sensitivity', {}).get('without_backfill_dates', {})
    primary_minimal_replay_signal = {
        'generated_at': primary_replay.get('generated_at'),
        'close_to_close_combo_total_return': primary_replay_results['close_to_close_combo']['metrics']['total_return'],
        'close_to_close_base_total_return': primary_replay_results['close_to_close_base']['metrics']['total_return'],
        'close_to_close_excess_total_return': primary_replay_comparisons['close_to_close_excess_total_return'],
        'open_to_close_combo_total_return': primary_replay_results['open_to_close_combo']['metrics']['total_return'],
        'open_to_close_base_total_return': primary_replay_results['open_to_close_base']['metrics']['total_return'],
        'open_to_close_excess_total_return': primary_replay_comparisons['open_to_close_excess_total_return'],
        'close_to_close_without_backfill_dates_total_return': primary_replay_without_backfill_metrics['close_to_close_combo']['metrics']['total_return'],
        'close_to_close_without_backfill_dates_excess_total_return': primary_replay_without_backfill['close_to_close_excess_total_return'],
        'open_to_close_without_backfill_dates_total_return': primary_replay_without_backfill_metrics['open_to_close_combo']['metrics']['total_return'],
        'open_to_close_without_backfill_dates_excess_total_return': primary_replay_without_backfill['open_to_close_excess_total_return'],
        'direction_consistent': bool(
            primary_replay['direction_consistency']['combo_direction_consistent']
            and primary_replay['direction_consistency']['excess_direction_consistent']
        ),
    }
    current_combo_multi_label_backtest_signal = {
        'generated_at': now,
        'by_label_mode': {
            mode: dict(payload)
            for mode, payload in formal_backtest_by_label_mode.items()
        },
        'summary': (
            'current_combo 的 c2c / o2c / c2o formal backtest、claim-limit、nested'
            + (' 与 walkforward' if walkforward_reports else '')
            + ' 与收益级 analog replay 已全部落地；当前主缺口已收敛为 independent holdout 供给优先，'
            + 'rolling / walkforward gate 已存在，但结果不足且与同一 clean window 共样本，不能充当独立 OOS。'
        ),
    }
    c2c_formal = formal_backtest_by_label_mode.get('close_to_close')
    o2c_formal = formal_backtest_by_label_mode.get('open_to_close')
    c2c_vs_o2c_digest_line = None
    if c2c_formal and o2c_formal:
        c2c_days = c2c_formal.get('effective_replay_days')
        o2c_days = o2c_formal.get('effective_replay_days')
        days_text = (
            f'同为 {c2c_days} 个有效回放日。'
            if c2c_days == o2c_days and c2c_days is not None
            else f'有效回放日分别为 c2c={c2c_days} / o2c={o2c_days}。'
        )
        c2c_vs_o2c_digest_line = (
            f"- c2c vs o2c 正式组合对照：c2c combo={fmt_pct(c2c_formal['combo_total_return'])}、"
            f"excess={fmt_pct(c2c_formal['excess_total_return'])}；"
            f"o2c combo={fmt_pct(o2c_formal['combo_total_return'])}、"
            f"excess={fmt_pct(o2c_formal['excess_total_return'])}；{days_text}"
        )

    claim['schema_version'] = '2026-04-19.current_combo_claim_limit_audit.v2'
    claim['generated_at'] = now
    claim.setdefault('source_paths', {})['multi_label_canonical_comparison'] = str(COMPARISON_PATH)
    claim['source_paths']['current_combo_replay_report_open_to_close'] = str(LABEL_CONFIG['open_to_close']['replay_path'])
    claim['source_paths']['current_combo_replay_report_close_to_open'] = str(LABEL_CONFIG['close_to_open']['replay_path'])
    claim['source_paths']['current_combo_portfolio_backtest_open_to_close'] = str(LABEL_CONFIG['open_to_close']['backtest_path'])
    claim['source_paths']['current_combo_portfolio_backtest_close_to_open'] = str(LABEL_CONFIG['close_to_open']['backtest_path'])
    claim['source_paths']['current_combo_walkforward_validation_by_label_mode'] = {
        mode: str(path)
        for mode, path in WALKFORWARD_LABEL_MODE_PATHS.items()
        if path.exists()
    }
    claim['source_paths']['sync_multi_label_combo_validation_context'] = str(Path(__file__).resolve())
    walkforward_claim_fragment = f'，walkforward 已覆盖 {available_walkforward_modes_text}' if walkforward_reports else ''
    if analog_scope['return_level_analog_claim_allowed']:
        claim['summary']['headline'] = (
            '已把 current_combo 的 c2c / o2c / c2o 组合级结果接入 claim-limit，'
            + ('walkforward 也已补出；' if walkforward_complete else '')
            + reserve_headline_fragment
            + 'analog 也已升级为收益级 replay；'
            + f'但三口径仍共用{same_clean_window_text}，{holdout_status_summary}，independent OOS 仍不能成立。'
        )
        claim['summary']['allowed_claim'] = (
            f'当前可宣称“current_combo 三口径 formal current-weight-map backtest、claim-limit 与 label-specific nested 已覆盖 {available_nested_modes_text}'
            f'{walkforward_claim_fragment}，且 {holdout_status_summary}；analog_validation 已提供 {analog_scope["completed_label_modes_text"]} 收益级 analog replay”；'
            + holdout_allowed_claim_tail
        )
    else:
        claim['summary']['headline'] = (
            '已把 current_combo 的 c2c / o2c / c2o 组合级结果接入 claim-limit；'
            + reserve_headline_fragment
            + f'但三口径仍共用{same_clean_window_text}，{holdout_status_summary}，independent OOS 与收益级 analog validation 仍不能成立。'
        )
        claim['summary']['allowed_claim'] = (
            f'当前只能宣称“current_combo 三口径 formal current-weight-map backtest、claim-limit 与 label-specific nested 已覆盖 {available_nested_modes_text}'
            f'{walkforward_claim_fragment}，且 {holdout_status_summary}”；不能升级为独立 OOS、收益级 analog validation，或解除 promotion freeze。'
        )
    claim['summary']['return_level_analog_claim_allowed'] = analog_scope['return_level_analog_claim_allowed']
    claim['label_mode_claim_scope'] = claim_scope
    claim['walkforward_validation'] = {
        'available_label_modes': available_walkforward_modes,
        'missing_label_modes': missing_walkforward_modes,
        'label_mode_reports': walkforward_mode_reports,
        'summary': walkforward_gap_summary,
    }
    claim['formal_backtest_snapshot']['by_label_mode'] = {
        mode: {
            'combo_total_return': item['primary_combo_total_return'],
            'excess_total_return': item['primary_excess_total_return'],
            'effective_replay_days': item['effective_replay_days'],
            'latest_weight_plan_factor_ids': item['latest_weight_plan_factor_ids'],
            'selection_mode': item['selection_mode'],
        }
        for mode, item in claim_scope.items()
    }
    claim['independent_oos_blocker']['close_to_open'] = {
        'labeled_eligible_days': claim_scope['close_to_open']['labeled_eligible_days'],
        'used_labeled_days': claim_scope['close_to_open']['used_labeled_days'],
        'remaining_labeled_holdout_days': claim_scope['close_to_open']['remaining_labeled_holdout_days'],
    }
    claim['independent_oos_blocker']['by_label_mode'] = {
        mode: {
            'labeled_eligible_days': item['labeled_eligible_days'],
            'used_labeled_days': item['used_labeled_days'],
            'remaining_labeled_holdout_days_count': item['remaining_labeled_holdout_days_count'],
            'eligible_but_unlabeled_dates': item['eligible_but_unlabeled_dates'],
            'reserved_holdout_dates': item['reserved_holdout_dates'],
            'reserved_holdout_dates_pending_labels': item['reserved_holdout_dates_pending_labels'],
            'reserved_holdout_dates_now_labeled': item['reserved_holdout_dates_now_labeled'],
        }
        for mode, item in claim_scope.items()
    }
    claim['independent_oos_blocker']['remaining_labeled_holdout_days'] = remaining_union
    claim['independent_oos_blocker']['remaining_labeled_holdout_days_count'] = len(remaining_union)
    claim['independent_oos_blocker']['eligible_but_unlabeled_dates'] = unlabeled_union
    claim['independent_oos_blocker']['reserved_holdout_dates'] = reserved_union
    claim['independent_oos_blocker']['reserved_holdout_dates_pending_labels'] = reserved_pending_union
    claim['independent_oos_blocker']['reserved_holdout_dates_now_labeled'] = reserved_now_labeled_union
    claim['independent_oos_blocker']['why_not_independent_oos'] = (
        f'c2c / o2c / c2o 三口径 current_combo formal replay 都已消耗同一 clean window 内全部 {replay_days_text} 个有 next-day 标签的 eligible trade days；'
        f'{holdout_release_summary}，{holdout_oos_tail}'
    )
    claim['upstream_data_reuse']['applies_to_label_modes'] = list(claim_scope.keys())
    if claim['upstream_data_reuse'].get('by_label_mode'):
        claim['upstream_data_reuse']['alternate_label_modes_not_quantified_here'] = []
        claim['upstream_data_reuse']['note'] = 'upstream test-fold overlap 与 sign lineage 已按 c2c / o2c / c2o 三口径量化；顶层字段保留 close_to_close 兼容摘要，细节见 by_label_mode。'
    else:
        claim['upstream_data_reuse']['alternate_label_modes_not_quantified_here'] = ['open_to_close', 'close_to_open']
        claim['upstream_data_reuse']['note'] = '本段 upstream test-fold overlap 与 sign lineage 仍按 c2c current combo 口径计算；本轮新增的是按 label_mode 区分的 holdout / claim scope，而不是重跑 o2c/c2o upstream overlap。'
    claim['analog_return_level_blocker']['latest_weight_plan_factor_ids_by_label_mode'] = {mode: item['latest_weight_plan_factor_ids'] for mode, item in claim_scope.items()}
    claim['analog_return_level_blocker']['latest_weight_plan_proxy_covered_windows_by_label_mode'] = {mode: item['proxy_covered_windows'] for mode, item in claim_scope.items()}
    claim['analog_return_level_blocker']['return_level_analog_claim_allowed'] = analog_scope['return_level_analog_claim_allowed']
    claim['analog_return_level_blocker']['completed_label_modes'] = analog_scope['completed_label_modes']
    claim['analog_return_level_blocker']['completed_label_modes_text'] = analog_scope['completed_label_modes_text']
    claim['analog_return_level_blocker']['usable_portfolio_replay_sample_count'] = analog_scope['usable_portfolio_replay_sample_count']
    claim['analog_return_level_blocker']['analog_window_count'] = analog_scope['analog_window_count']
    claim['analog_return_level_blocker']['windows_with_portfolio_return_payload'] = analog_scope['usable_portfolio_replay_sample_count']
    claim['analog_return_level_blocker']['claim_limit'] = analog_scope['claim_limit']
    claim['analog_return_level_blocker']['scope_note'] = analog_scope['scope_note']
    claim['analog_return_level_blocker']['report_scope_echo'] = analog_report.get('validation_scope', {})
    claim['portfolio_context_echo']['multi_label_claim_scope_quantified'] = True
    claim['portfolio_context_echo']['multi_label_claim_scope_summary'] = {mode: item['remaining_labeled_holdout_days_count'] for mode, item in claim_scope.items()}
    claim['portfolio_context_echo']['portfolio_report_claim_limit'] = (
        f'current_combo_claim_limit_audit 已显式量化：c2c / o2c / c2o 三口径都已用完 {replay_usage_text} 个有标签 clean days；'
        f'current_combo_nested_validation 已覆盖 {available_nested_modes_text}'
        + (f'，但 {missing_nested_modes_text} 仍缺 label-specific nested，因此仍不能宣称独立 OOS。' if missing_nested_modes else f'，且 {analog_scope["blocker_summary"]}，因此仍不能宣称独立 OOS。')
    )
    claim['portfolio_context_echo']['formal_backtest_claim_limit'] = analog_scope['formal_backtest_claim_limit']
    dump_json(CLAIM_PATH, claim)
    CLAIM_MD_PATH.write_text(rewrite_claim_markdown(claim), encoding='utf-8')

    nested['schema_version'] = '2026-04-20.current_combo_nested_validation.v3'
    nested['generated_at'] = now
    nested['validation_scope'] = (
        'micro_nested_validation_on_clean_window_existing_templates_only_multi_label_partial'
        if missing_nested_modes
        else 'micro_nested_validation_on_clean_window_existing_templates_only_multi_label_complete'
    )
    nested.setdefault('source_paths', {})['current_combo_replay_report_open_to_close'] = str(LABEL_CONFIG['open_to_close']['replay_path'])
    nested['source_paths']['current_combo_replay_report_close_to_open'] = str(LABEL_CONFIG['close_to_open']['replay_path'])
    nested['source_paths']['current_combo_portfolio_backtest_open_to_close'] = str(LABEL_CONFIG['open_to_close']['backtest_path'])
    nested['source_paths']['current_combo_portfolio_backtest_close_to_open'] = str(LABEL_CONFIG['close_to_open']['backtest_path'])
    nested['source_paths']['multi_label_canonical_comparison'] = str(COMPARISON_PATH)
    nested['source_paths']['sync_multi_label_combo_validation_context'] = str(Path(__file__).resolve())
    nested['source_paths']['nested_label_mode_reports'] = {
        label_mode: str(NESTED_LABEL_MODE_PATHS[label_mode])
        for label_mode in available_nested_modes
        if NESTED_LABEL_MODE_PATHS[label_mode].exists()
    }
    nested.setdefault('source_mtimes', {})['current_combo_replay_report'] = iso_mtime(LABEL_CONFIG['close_to_close']['replay_path'])
    nested['source_mtimes']['current_combo_portfolio_backtest'] = iso_mtime(LABEL_CONFIG['close_to_close']['backtest_path'])
    nested['source_mtimes']['current_combo_replay_report_open_to_close'] = iso_mtime(LABEL_CONFIG['open_to_close']['replay_path'])
    nested['source_mtimes']['current_combo_replay_report_close_to_open'] = iso_mtime(LABEL_CONFIG['close_to_open']['replay_path'])
    nested['source_mtimes']['current_combo_portfolio_backtest_open_to_close'] = iso_mtime(LABEL_CONFIG['open_to_close']['backtest_path'])
    nested['source_mtimes']['current_combo_portfolio_backtest_close_to_open'] = iso_mtime(LABEL_CONFIG['close_to_open']['backtest_path'])
    nested['source_mtimes']['multi_label_canonical_comparison'] = iso_mtime(COMPARISON_PATH)
    nested['source_mtimes']['nested_label_mode_reports'] = {
        label_mode: iso_mtime(NESTED_LABEL_MODE_PATHS[label_mode])
        for label_mode in available_nested_modes
        if NESTED_LABEL_MODE_PATHS[label_mode].exists()
    }
    nested['applies_to_label_modes'] = available_nested_modes
    nested['label_mode_formal_backtest_context'] = {
        mode: {
            'selection_mode': item['selection_mode'],
            'latest_weight_plan_factor_ids': item['latest_weight_plan_factor_ids'],
            'primary_combo_total_return': item['primary_combo_total_return'],
            'primary_excess_total_return': item['primary_excess_total_return'],
            'effective_replay_days': item['effective_replay_days'],
            'remaining_labeled_holdout_days_count': item['remaining_labeled_holdout_days_count'],
        }
        for mode, item in claim_scope.items()
    }
    nested['label_mode_nested_reports'] = nested_mode_reports
    nested['alternate_label_mode_gap'] = {
        'status': (
            'alternate_label_mode_combo_backtests_completed_but_nested_partially_missing'
            if missing_nested_modes
            else 'all_label_mode_nested_completed_but_other_release_blockers_remain'
        ),
        'missing_label_specific_nested_modes': missing_nested_modes,
        'common_latest_weight_plan_factor_ids': comparison['latest_weight_plan_overlap']['common_factor_ids'],
        'sign_unstable_factor_ids': comparison['sign_unstable_factor_ids'],
        'multi_label_canonical_comparison_path': str(COMPARISON_PATH),
        'summary': nested_gap_summary,
    }
    if missing_nested_modes:
        nested['verdict']['summary'] = (
            f'current_combo_nested_validation 现已覆盖 {available_nested_modes_text} 的 label-specific nested；'
            f'{missing_nested_modes_text} 仍缺放行链。'
            f'当前证据仍共用{same_clean_window_text}，{holdout_release_summary}，'
            f'{analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
        )
        nested['verdict']['claim_limitations'] = [
            '这是 clean window 上的微型 nested validation 汇总，不是完整独立 research pipeline。',
            '上游 factor discovery / canonical weight_map 仍复用了既有全样本产物。',
            f'{missing_nested_modes_text} 虽已有组合级 replay/backtest，但本报告尚未对这些 label_mode 单独重跑 nested folds。',
            f'{holdout_release_summary}，且 {analog_scope["blocker_summary"]}。',
        ]
    else:
        nested['verdict']['summary'] = (
            'current_combo_nested_validation 已覆盖 c2c / o2c / c2o 三口径 label-specific nested；'
            f'但三口径仍共用{same_clean_window_text}、{holdout_release_summary}，'
            f'{analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
        )
        nested['verdict']['claim_limitations'] = [
            '这是 clean window 上的微型 nested validation 汇总，不是完整独立 research pipeline。',
            '上游 factor discovery / canonical weight_map 仍复用了既有全样本产物。',
            f'即便三口径 nested 已齐，{holdout_release_summary}。',
            f'{analog_scope["blocker_summary"]}，因此仍不能宣称独立 OOS 或解除 promotion freeze。',
        ]
    dump_json(NESTED_PATH, nested)
    NESTED_MD_PATH.write_text(rewrite_nested_markdown(nested), encoding='utf-8')

    validation['generated_at'] = now
    validation_basis = validation.setdefault('canonical_refresh_basis', {})
    validation_basis.update({
        key: value
        for key, value in canonical_basis_patch.items()
        if value not in (None, {}, [])
    })
    validation_basis['claim_limit_audit_mtime'] = iso_mtime(CLAIM_PATH)
    validation_basis['micro_nested_validation_mtime'] = iso_mtime(NESTED_PATH)
    validation_basis['walkforward_validation_mtimes'] = {
        mode: iso_mtime(path)
        for mode, path in WALKFORWARD_LABEL_MODE_PATHS.items()
        if path.exists()
    }
    validation_basis['replay_mtime'] = iso_mtime(LABEL_CONFIG['close_to_close']['replay_path'])
    validation_basis['formal_backtest_mtime'] = iso_mtime(LABEL_CONFIG['close_to_close']['backtest_path'])
    validation_basis['current_combo_portfolio_backtest_mtime'] = iso_mtime(LABEL_CONFIG['close_to_close']['backtest_path'])
    validation_basis['current_combo_portfolio_backtest_open_to_close_mtime'] = iso_mtime(LABEL_CONFIG['open_to_close']['backtest_path'])
    validation_basis['current_combo_portfolio_backtest_close_to_open_mtime'] = iso_mtime(LABEL_CONFIG['close_to_open']['backtest_path'])
    validation_basis['portfolio_report_mtime'] = now
    validation_basis['overfit_report_mtime'] = now
    validation_basis['promotion_report_mtime'] = now
    validation_basis['control_plane_sync_at'] = now
    validation['three_most_severe_gaps'] = [
        primary_holdout_gap,
        'current_combo 的 c2c / o2c / c2o 三口径 formal backtest 与 claim-limit 已全部量化，但 latest_weight_plan 仍三向分歧，common_latest_weight_plan_factor_ids=[]。',
        release_gap_summary,
    ]
    selection_backfill = validation.setdefault('infrastructure_progress', {}).setdefault('selection_history_backfill', {})
    selection_backfill['panel_constituent_retention'] = retention_audit
    selection_backfill['panel_constituent_gap_attribution'] = {
        'path': selection_gap_context['path'],
        'dropped_rows': selection_gap_context['dropped_rows'],
        'reason_counts': selection_gap_context['reason_counts'],
        'stock_list_master_gap_rows': selection_gap_context['stock_list_master_gap_rows'],
        'mapping_reference_gaps': selection_gap_context['mapping_reference_gaps'],
    }
    selection_backfill['stock_list_master_gap_rows'] = selection_gap_context['stock_list_master_gap_rows']
    selection_backfill['master_data_gap_note'] = selection_gap_context['master_gap_note']
    selection_backfill['residual_risk'] = (
        f"selection_history 原始成分 {retention_audit['selection_history_total_rows']} 行里，仅 {retention_audit['panel_benchmark_total_rows']} 行进入 panel benchmark_eligible；"
        '当前 clean benchmark 仍不是 33×20 的全量 base 成分回放。'
        + (f"{selection_gap_context['master_gap_note']}" if selection_gap_context['master_gap_note'] else '')
    )
    infra = validation.setdefault('infrastructure_progress', {}).setdefault('return_label_infrastructure', {})
    infra['residual_risk'] = f'三套 canonical audit / weight 与 claim-limit 已齐，但 {release_gap_summary}'
    validation.setdefault('multi_label_canonical_audit', {})['summary'] = (
        'open_to_close 与 close_to_open 已补出独立 canonical audit + weight map，o2c/c2o 的 current_combo replay/backtest 也已落地；'
        f'claim-limit 已确认 {holdout_status_summary}，{nested_scope_summary}{walkforward_scope_summary}'
    )
    validation['multi_label_canonical_audit']['current_combo_formal_backtests_by_label_mode'] = formal_backtest_by_label_mode
    validation.setdefault('multi_label_canonical_context', {}).setdefault('combo_level_backtest_status', {})['note'] = (
        f"o2c/c2o 的 current_combo replay/formal backtest 已落地；current_combo_nested_validation 已覆盖 {available_nested_modes_text}；"
        + (f"{walkforward_scope_summary}" if walkforward_reports else '')
        + '收益级 analog replay 已具备，但真正剩余缺口仍是独立 holdout / 独立 OOS。'
    )
    validation.setdefault('close_to_close_vs_open_to_close_comparison', {}).setdefault('evidence', {}).update({
        'close_to_close_combo_total_return': primary_replay_results['close_to_close_combo']['metrics']['total_return'],
        'close_to_close_base_total_return': primary_replay_results['close_to_close_base']['metrics']['total_return'],
        'open_to_close_combo_total_return': primary_replay_results['open_to_close_combo']['metrics']['total_return'],
        'open_to_close_base_total_return': primary_replay_results['open_to_close_base']['metrics']['total_return'],
        'close_to_close_excess_total_return': primary_replay_comparisons['close_to_close_excess_total_return'],
        'open_to_close_excess_total_return': primary_replay_comparisons['open_to_close_excess_total_return'],
        'combo_c2c_minus_o2c_total_return': primary_replay_comparisons['combo_c2c_minus_o2c_total_return'],
        'close_to_close_without_backfill_dates_excess_total_return': primary_replay_without_backfill['close_to_close_excess_total_return'],
    })
    validation['multi_label_canonical_context']['combo_level_backtest_status']['walkforward_validation_by_label_mode'] = walkforward_mode_reports
    validation.setdefault('portfolio_level_validation_gaps', {})['summary'] = (
        f'current_combo 三口径 formal backtest 与 claim-limit 已齐，但 {release_gap_summary}'
    )
    validation['portfolio_level_validation_gaps']['gaps'] = [
        'current_combo_portfolio_backtest.json / .o2c_canonical.json / .c2o_canonical.json 已提供三口径正式组合级 backtest，但它们仍共享同一 33 天 clean window，不能充当独立 OOS。',
        f'current_combo_claim_limit_audit.json 已显式记录 {holdout_status_summary}；当前仍不能把 reserve 事后包装成独立样本外。',
        (
            f"current_combo_nested_validation.json 已明确 applies_to_label_modes={available_nested_modes}；"
            f"{missing_nested_modes_text} 仍缺 label-specific nested。"
            if missing_nested_modes
            else f"current_combo_nested_validation.json 已明确 applies_to_label_modes={available_nested_modes}，但这仍不能替代独立 holdout；收益级 analog replay 也仍不是独立 OOS。"
        ),
        (
            f"current_combo_walkforward_validation 已覆盖 {available_walkforward_modes_text}；{missing_walkforward_modes_text} 仍缺 label-specific walkforward。"
            if walkforward_reports and missing_walkforward_modes
            else (
                f'current_combo_walkforward_validation 已具备 c2c / o2c / c2o 三口径{walkforward_scope_label}，但它仍与{same_clean_window_text}共样本，不能替代独立 labeled holdout / 独立 OOS。'
                if walkforward_reports
                else f'current_combo_walkforward_validation 仍未形成三口径{walkforward_scope_label} artifact。'
            )
        ),
        analog_scope['gap_bullet'],
    ]
    validation['engineering_todo'] = [
        {'priority': 'P0', 'task': holdout_engineering_todo},
        {'priority': 'P0', 'task': engineering_todo_nested},
        {'priority': 'P1', 'task': analog_scope['engineering_todo']},
    ]
    release = validation.setdefault('release_recommendation', {})
    release['reason'] = (
        f'三口径 combo backtest 与 claim-limit 已齐，但 {holdout_release_summary}，{release_gap_summary}'
    )
    allowed_usage = release.setdefault('allowed_usage', [])
    allowed_usage[:] = [
        analog_scope['allowed_usage_line'] if 'analog_validation_report' in item else item
        for item in allowed_usage
    ]
    if analog_scope['allowed_usage_line'] not in allowed_usage:
        allowed_usage.append(analog_scope['allowed_usage_line'])
    blocked_usage = release.setdefault('blocked_usage', [])
    blocked_usage[:] = [
        item for item in blocked_usage
        if '尚未 label-specific' not in item
        and 'current_combo_claim_limit_audit / current_combo_nested_validation' not in item
        and 'analog 仍非收益级 replay' not in item
        and '当前尚未显式预留 future labeled holdout reserve' not in item
        and 'remaining labeled holdout=' not in item
        and 'current labeled holdout=' not in item
        and '待 next-day label=20260416' not in item
        and '已形成 labeled holdout=无' not in item
    ]
    current_release_blocker = (
        f'在 {missing_nested_modes_text} 仍缺 label-specific nested 时解除 promotion freeze'
        if missing_nested_modes
        else (
            f'在 walkforward 已补出但 {holdout_status_summary}、且 analog 虽已具备收益级 replay但仍非独立 OOS 时解除 promotion freeze'
            if walkforward_complete
            else f'在三口径仍共用同一 clean window、{holdout_status_summary} 且 analog 虽已具备收益级 replay但仍非独立 OOS时解除 promotion freeze'
        )
    )
    if current_release_blocker not in blocked_usage:
        blocked_usage.append(current_release_blocker)
    blocked_usage[:] = list(dict.fromkeys(blocked_usage))
    latest_stage_poll = validation.setdefault('latest_stage_poll', {})
    latest_stage_poll['polled_at'] = now
    latest_stage_poll['current_combo_claim_limit_audit'] = {
        'mtime': iso_mtime(CLAIM_PATH),
        'remaining_labeled_holdout_days_count': claim['independent_oos_blocker']['remaining_labeled_holdout_days_count'],
        'eligible_but_unlabeled_dates': claim['independent_oos_blocker']['eligible_but_unlabeled_dates'],
        'reserved_holdout_dates': claim['independent_oos_blocker']['reserved_holdout_dates'],
        'reserved_holdout_dates_pending_labels': claim['independent_oos_blocker']['reserved_holdout_dates_pending_labels'],
        'reserved_holdout_dates_now_labeled': claim['independent_oos_blocker']['reserved_holdout_dates_now_labeled'],
    }
    latest_stage_poll['current_combo_nested_validation'] = {
        'mtime': iso_mtime(NESTED_PATH),
        'fold_count': (nested.get('sample_coverage') or {}).get('fold_count'),
        'outer_oos_days': (nested.get('sample_coverage') or {}).get('outer_oos_days'),
        'sufficient_to_lift_promotion_freeze': (nested.get('verdict') or {}).get('sufficient_to_lift_promotion_freeze'),
        'applies_to_label_modes': list(available_nested_modes),
    }
    latest_stage_poll['current_combo_walkforward_validation'] = {
        'available_label_modes': list(available_walkforward_modes),
        'label_mode_reports': walkforward_mode_reports,
        'summary': walkforward_gap_summary,
    }
    latest_stage_poll['current_combo_portfolio_backtest'] = {
        'mtime': iso_mtime(LABEL_CONFIG['close_to_close']['backtest_path']),
        'eligible_trade_days': claim_scope['close_to_close']['eligible_trade_days'],
        'effective_replay_days': claim_scope['close_to_close']['effective_replay_days'],
        'close_to_close_total_return': claim_scope['close_to_close']['primary_combo_total_return'],
        'open_to_close_total_return': claim_scope['open_to_close']['primary_combo_total_return'],
        'close_to_open_total_return': claim_scope['close_to_open']['primary_combo_total_return'],
    }
    dump_json(VALIDATION_PATH, validation)

    portfolio['generated_at'] = now
    portfolio_source_paths['current_combo_holdout_reserve'] = str(HOLDOUT_RESERVE_PATH)
    portfolio.setdefault('source_mtimes', {})['audit'] = primary_audit_mtime
    portfolio['source_mtimes']['weight'] = primary_weight_mtime
    portfolio['source_mtimes']['current_combo_holdout_reserve'] = iso_mtime(HOLDOUT_RESERVE_PATH)
    portfolio['source_mtimes']['current_market_fingerprint'] = iso_mtime(CURRENT_MARKET_PATH)
    portfolio['source_mtimes']['regime_analog_windows'] = iso_mtime(ANALOG_WINDOWS_PATH)
    portfolio['source_mtimes']['latest_weight_plan_analog_alignment'] = iso_mtime(ALIGNMENT_PATH)
    portfolio['source_mtimes']['current_combo_replay_report'] = iso_mtime(LABEL_CONFIG['close_to_close']['replay_path'])
    portfolio['source_mtimes']['current_combo_replay_report_open_to_close'] = iso_mtime(LABEL_CONFIG['open_to_close']['replay_path'])
    portfolio['source_mtimes']['current_combo_replay_report_close_to_open'] = iso_mtime(LABEL_CONFIG['close_to_open']['replay_path'])
    portfolio['source_mtimes']['current_combo_portfolio_backtest'] = iso_mtime(LABEL_CONFIG['close_to_close']['backtest_path'])
    portfolio['source_mtimes']['current_combo_portfolio_backtest_open_to_close'] = iso_mtime(LABEL_CONFIG['open_to_close']['backtest_path'])
    portfolio['source_mtimes']['current_combo_portfolio_backtest_close_to_open'] = iso_mtime(LABEL_CONFIG['close_to_open']['backtest_path'])
    portfolio['source_mtimes']['analog_validation_report'] = iso_mtime(ANALOG_REPORT_PATH)
    portfolio['source_mtimes']['current_combo_claim_limit_audit'] = iso_mtime(CLAIM_PATH)
    portfolio['source_mtimes']['current_combo_nested_validation'] = iso_mtime(NESTED_PATH)
    portfolio['source_mtimes']['current_combo_nested_validation_open_to_close'] = iso_mtime(NESTED_LABEL_MODE_PATHS['open_to_close'])
    portfolio['source_mtimes']['current_combo_nested_validation_close_to_open'] = iso_mtime(NESTED_LABEL_MODE_PATHS['close_to_open'])
    portfolio['source_mtimes']['current_combo_walkforward_validation'] = iso_mtime(WALKFORWARD_LABEL_MODE_PATHS['close_to_close'])
    portfolio['source_mtimes']['current_combo_walkforward_validation_open_to_close'] = iso_mtime(WALKFORWARD_LABEL_MODE_PATHS['open_to_close'])
    portfolio['source_mtimes']['current_combo_walkforward_validation_close_to_open'] = iso_mtime(WALKFORWARD_LABEL_MODE_PATHS['close_to_open'])
    portfolio['source_mtimes']['multi_label_canonical_comparison'] = iso_mtime(COMPARISON_PATH)
    attribution_alignment = portfolio.setdefault('dependency_assessment', {}).setdefault('attribution_alignment', {})
    attribution_alignment['attrib_mtime'] = attribution_mtime
    attribution_alignment['audit_mtime'] = primary_audit_mtime
    attribution_alignment['weight_mtime'] = primary_weight_mtime
    attribution_alignment['aligned_with_latest_weight_map'] = attribution_aligned
    if attribution_mtime:
        if attribution_aligned:
            attribution_alignment['reason'] = (
                f'phase1_attribution_report.json 已在 {attribution_mtime} 跟随当前 canonical audit/weight 刷新；'
                '当前 attribution 不再滞后，但仍只是说明性风险摘要，不替代收益级验证。'
            )
        else:
            attribution_alignment['reason'] = (
                f'phase1_attribution_report.json mtime={attribution_mtime}，当前 canonical audit/weight mtime='
                f'{primary_audit_mtime} / {primary_weight_mtime}；attribution 仍落后于最新 canonical，'
                '因此只能作为说明性风险摘要，不能视为已与当前 canonical 完全对齐。'
            )
    else:
        attribution_alignment['reason'] = '当前未发现可用 attribution_report 路径，无法确认 attribution 是否与最新 canonical 对齐。'
    portfolio.setdefault('available_backtest_evidence', {})['evidence_gap'] = (
        f'current_combo 的 c2c / o2c / c2o 三口径 formal backtest 已全部落地，claim-limit 已确认 {holdout_status_summary}；但 {release_gap_summary} {analog_scope["portfolio_gap_text"]}'
    )
    portfolio.setdefault('oos_validation_assessment', {})['claim_limit'] = (
        f'current_combo_claim_limit_audit 已显式量化：c2c / o2c / c2o 三口径都已用完 {replay_usage_text} 个有标签 clean days；{holdout_release_summary}；current_combo_nested_validation 现已覆盖 {available_nested_modes_text}'
        + (f'，但 {missing_nested_modes_text} 仍缺 label-specific nested，因此仍不能宣称独立 OOS。' if missing_nested_modes else '')
        + (f'；{walkforward_gap_summary}' if walkforward_reports else '')
        + (f'；且 {analog_scope["blocker_summary"]}，因此仍不能宣称独立 OOS。' if not missing_nested_modes else '')
    )
    portfolio.setdefault('multi_label_canonical_context', {}).setdefault('combo_level_replay_status', {})['combo_backtest_metrics_by_label_mode'] = {
        mode: dict(payload)
        for mode, payload in formal_backtest_by_label_mode.items()
    }
    portfolio['multi_label_canonical_context']['comparison_generated_at'] = comparison.get('generated_at')
    portfolio['multi_label_canonical_context']['official_formal_label_mode'] = comparison['labels']['close_to_close'].get('label_mode')
    portfolio['multi_label_canonical_context']['official_latest_weight_plan_factor_ids'] = comparison['labels']['close_to_close'].get('latest_weight_plan_factor_ids', [])
    portfolio['multi_label_canonical_context']['alternate_latest_weight_plan_factor_ids'] = {
        'open_to_close': comparison['labels']['open_to_close'].get('latest_weight_plan_factor_ids', []),
        'close_to_open': comparison['labels']['close_to_open'].get('latest_weight_plan_factor_ids', []),
    }
    portfolio['multi_label_canonical_context']['common_latest_weight_plan_factor_ids'] = comparison['latest_weight_plan_overlap']['common_factor_ids']
    portfolio['multi_label_canonical_context']['sign_unstable_factor_ids'] = comparison['sign_unstable_factor_ids']
    current_portfolio_structure = portfolio.setdefault('current_portfolio_structure', {})
    current_portfolio_structure['latest_regime'] = comparison['labels']['close_to_close'].get(
        'latest_regime', current_portfolio_structure.get('latest_regime', {})
    )
    latest_weight_plan = comparison['labels']['close_to_close'].get('latest_weight_plan')
    if latest_weight_plan:
        current_portfolio_structure['latest_weight_plan'] = latest_weight_plan
    portfolio['oos_validation_assessment']['analog_validation_completed'] = analog_scope['return_level_analog_claim_allowed']
    portfolio['oos_validation_assessment']['attribution_synced_with_latest_audit_weight'] = attribution_aligned
    portfolio['oos_validation_assessment']['alternate_label_mode_gap'] = release_gap_summary
    portfolio['oos_validation_assessment']['nested_applies_to_label_modes'] = nested['applies_to_label_modes']
    portfolio['oos_validation_assessment']['walkforward_validation_present'] = bool(walkforward_reports)
    portfolio['oos_validation_assessment']['walkforward_applies_to_label_modes'] = available_walkforward_modes
    portfolio['oos_validation_assessment']['walkforward_label_mode_reports'] = walkforward_mode_reports
    portfolio['oos_validation_assessment']['claim_limit_by_label_mode'] = {mode: item['remaining_labeled_holdout_days_count'] for mode, item in claim_scope.items()}
    portfolio.setdefault('conclusion', {})['effective_oos_claim_allowed'] = False
    portfolio['conclusion']['analog_validation_claim_allowed'] = analog_scope['return_level_analog_claim_allowed']
    portfolio['conclusion']['summary'] = (
        f'current_combo 的 c2c / o2c / c2o formal backtest 已全部落地，claim-limit 已确认 {holdout_release_summary}；但 {release_gap_summary} {analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
        + (f"另 {selection_gap_context['coverage_risk_summary']}" if selection_gap_context['present'] else '')
    )
    portfolio.setdefault('analog_proxy_context', {})['staleness_assessment'] = (
        'latest_weight_plan_analog_alignment 仍只度量 analog proxy windows 对 latest weight plan 的覆盖关系；'
        f'但 analog_validation_report 已另外提供 {analog_scope["completed_label_modes_text"] or "c2c / o2c / c2o"} 收益级 analog replay。'
        '二者都仍不是独立 holdout / 独立 OOS。'
    )
    dump_json(PORTFOLIO_PATH, portfolio)

    overfit['generated_at'] = now
    overfit.setdefault('missing_validation_stack', {})['analog_validation_completed'] = analog_scope['return_level_analog_claim_allowed']
    overfit['missing_validation_stack']['nested_walk_forward_completed'] = walkforward_complete
    overfit['missing_validation_stack']['attribution_synced_with_latest_audit_weight'] = attribution_aligned
    overfit.setdefault('missing_validation_stack', {})['detail'] = [
        'current_combo 的 c2c / o2c / c2o formal backtest 已全部落地。',
        f'current_combo_claim_limit_audit 已显式确认 {holdout_release_summary}，不能把 reserve 事后包装成独立 labeled OOS。',
        (
            f'current_combo_nested_validation 现已覆盖 {available_nested_modes_text}；{missing_nested_modes_text} 仍无 label-specific nested。'
            if missing_nested_modes
            else 'current_combo_nested_validation 已覆盖 c2c / o2c / c2o，但这仍不能替代独立 holdout / 收益级 analog replay。'
        ),
        walkforward_gap_summary,
        analog_scope['gap_bullet'],
    ]
    overfit.setdefault('multi_label_canonical_risk', {})['risk_statement'] = (
        f'三口径 current_combo replay/formal backtest 已落地，latest plan 三向分歧不再只是 audit 层假设；但 common_latest_weight_plan_factor_ids 仍为空，{holdout_release_summary}，且 {release_gap_summary} {analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
    )
    overfit['current_combo_minimal_replay_signal'] = primary_minimal_replay_signal
    overfit['current_combo_formal_backtest_signal'] = {
        'generated_at': claim_scope['close_to_close']['formal_backtest_generated_at'],
        'path': str(LABEL_CONFIG['close_to_close']['backtest_path']),
        'close_to_close_combo_total_return': primary_backtest['series_metrics']['close_to_close_combo']['total_return'],
        'close_to_close_combo_sharpe': primary_backtest['series_metrics']['close_to_close_combo']['sharpe'],
        'close_to_close_combo_max_drawdown': primary_backtest['series_metrics']['close_to_close_combo']['max_drawdown'],
        'open_to_close_combo_total_return': primary_backtest['series_metrics']['open_to_close_combo']['total_return'],
        'close_to_close_base_total_return': primary_backtest['series_metrics']['close_to_close_base']['total_return'],
        'open_to_close_base_total_return': primary_backtest['series_metrics']['open_to_close_base']['total_return'],
        'direction_consistent': primary_backtest['verdict']['close_to_close_vs_open_to_close_direction_consistent'],
        'by_label_mode': {
            mode: dict(payload)
            for mode, payload in formal_backtest_by_label_mode.items()
        },
    }
    overfit['multi_label_canonical_risk']['combo_backtest_metrics_by_label_mode'] = {
        mode: dict(payload)
        for mode, payload in formal_backtest_by_label_mode.items()
    }
    overfit['multi_label_canonical_risk']['comparison_generated_at'] = comparison.get('generated_at')
    overfit['multi_label_canonical_risk']['latest_weight_plan_by_label_mode'] = {
        label: info.get('latest_weight_plan_factor_ids', [])
        for label, info in comparison['labels'].items()
    }
    overfit['multi_label_canonical_risk']['common_latest_weight_plan_factor_ids'] = comparison['latest_weight_plan_overlap']['common_factor_ids']
    overfit['multi_label_canonical_risk']['sign_unstable_factor_ids'] = comparison['sign_unstable_factor_ids']
    overfit['overall_verdict']['summary'] = (
        f'过拟合冻结继续维持：虽然三口径 current_combo formal backtest 与 claim-limit 已齐，但 {holdout_release_summary}、common_latest_weight_plan_factor_ids 仍为空、{release_gap_summary} {analog_scope["blocker_summary"]}，因此 promotion freeze 不能放松。'
        + (f"另需注意：{selection_gap_context['master_gap_note']}" if selection_gap_context['master_gap_note'] else '')
    )
    artifact_consistency = overfit.setdefault('artifact_consistency_risk', {})
    artifact_consistency['audit_mtime'] = primary_audit_mtime
    artifact_consistency['weight_mtime'] = primary_weight_mtime
    artifact_consistency['analog_windows_mtime'] = iso_mtime(ANALOG_WINDOWS_PATH)
    artifact_consistency['holdout_reserve_mtime'] = iso_mtime(HOLDOUT_RESERVE_PATH)
    if attribution_mtime:
        artifact_consistency['attrib_mtime'] = attribution_mtime
    artifact_consistency['detail'] = (
        f'最新 canonical audit/weight 已追平到 {benchmark_days_text} 天 clean window；current_combo_portfolio_backtest、claim-limit、nested'
        + (' 与 walkforward' if walkforward_reports else '')
        + ' 也都已刷新到当前口径。'
        + (
            f' phase1_attribution_report 仍停留在 {attribution_mtime}，尚未追平到当前 canonical audit/weight '
            f'({primary_audit_mtime} / {primary_weight_mtime})。'
            if attribution_mtime and not attribution_aligned
            else ' attribution 也已与当前 canonical mtime 对齐。'
            if attribution_mtime and attribution_aligned
            else ' 当前未发现可用 attribution_report 路径。'
        )
        + ' 收益级 analog replay 已具备，但独立 labeled holdout / 独立 OOS 仍缺。'
        + (f"同时需注意：{selection_gap_context['master_gap_note']}" if selection_gap_context['master_gap_note'] else '')
    )
    overfit_evidence_path = overfit.setdefault('evidence_path', [])
    for extra_path in [
        str(HOLDOUT_RESERVE_PATH),
        *[str(path) for path in WALKFORWARD_LABEL_MODE_PATHS.values() if path.exists()],
    ]:
        if extra_path not in overfit_evidence_path:
            overfit_evidence_path.append(extra_path)
    dump_json(OVERFIT_PATH, overfit)

    promotion['generated_at'] = now
    promotion_basis = promotion.setdefault('canonical_refresh_basis', {})
    promotion_basis.update({
        key: value
        for key, value in canonical_basis_patch.items()
        if key in {
            'audit_mtime',
            'weight_mtime',
            'label_mode',
            'label_column',
            'benchmark_eligible_trade_days',
            'benchmark_eligible_row_count',
            'multi_label_audit_mtimes',
            'multi_label_weight_mtimes',
            'open_to_close_audit_mtime',
            'open_to_close_weight_mtime',
            'close_to_open_audit_mtime',
            'close_to_open_weight_mtime',
            'multi_label_comparison_mtime',
        }
        and value not in (None, {}, [])
    })
    promotion_basis['claim_limit_audit_mtime'] = iso_mtime(CLAIM_PATH)
    promotion_basis['micro_nested_validation_mtime'] = iso_mtime(NESTED_PATH)
    promotion_basis['walkforward_validation_mtimes'] = {
        mode: iso_mtime(path)
        for mode, path in WALKFORWARD_LABEL_MODE_PATHS.items()
        if path.exists()
    }
    promotion_basis['formal_backtest_mtime'] = iso_mtime(LABEL_CONFIG['close_to_close']['backtest_path'])
    promotion_basis['open_to_close_formal_backtest_mtime'] = iso_mtime(LABEL_CONFIG['open_to_close']['backtest_path'])
    promotion_basis['close_to_open_formal_backtest_mtime'] = iso_mtime(LABEL_CONFIG['close_to_open']['backtest_path'])
    promotion.setdefault('global_constraints', {})['nested_walk_forward_complete'] = walkforward_complete
    promotion.setdefault('global_constraints', {})['analog_validation_complete'] = analog_scope['return_level_analog_claim_allowed']
    promotion.setdefault('summary', {})['summary'] = (
        f'promotion freeze 不变；current_combo 三口径 formal backtest 与 claim-limit 已齐，但 {holdout_release_summary}，{release_gap_summary} {analog_scope["blocker_summary"]}，因此当前因子继续只保留 watch。'
        + (f"另 {selection_gap_context['coverage_risk_summary']}" if selection_gap_context['present'] else '')
    )
    promotion.setdefault('multi_label_promotion_blocker', {})['why_blocking'] = (
        f'o2c/c2o 的 current_combo replay 与 formal backtest 已经落地，claim-limit 已确认 {holdout_release_summary}；'
        f'但这并没有提供独立 holdout，且 nested 当前仅覆盖 {available_nested_modes_text}'
        + (f'，{missing_nested_modes_text} 仍缺 label-specific 放行链。' if missing_nested_modes else '')
        + (f'；{walkforward_gap_summary}' if walkforward_reports else '')
        + (f'；且 {analog_scope["blocker_summary"]}。' if not missing_nested_modes else '')
    )
    promotion['multi_label_promotion_blocker']['comparison_generated_at'] = comparison.get('generated_at')
    promotion['multi_label_promotion_blocker']['official_formal_label_mode'] = comparison['labels']['close_to_close'].get('label_mode')
    promotion['multi_label_promotion_blocker']['official_latest_weight_plan_factor_ids'] = comparison['labels']['close_to_close'].get('latest_weight_plan_factor_ids', [])
    promotion['multi_label_promotion_blocker']['alternate_latest_weight_plan_factor_ids'] = {
        'open_to_close': comparison['labels']['open_to_close'].get('latest_weight_plan_factor_ids', []),
        'close_to_open': comparison['labels']['close_to_open'].get('latest_weight_plan_factor_ids', []),
    }
    promotion['multi_label_promotion_blocker']['common_latest_weight_plan_factor_ids'] = comparison['latest_weight_plan_overlap']['common_factor_ids']
    promotion['multi_label_promotion_blocker']['sign_unstable_factor_ids'] = comparison['sign_unstable_factor_ids']
    promotion['multi_label_promotion_blocker']['nested_applies_to_label_modes'] = nested['applies_to_label_modes']
    promotion['multi_label_promotion_blocker']['walkforward_applies_to_label_modes'] = available_walkforward_modes
    promotion['current_combo_replay_basis'] = {
        **promotion.get('current_combo_replay_basis', {}),
        'generated_at': claim_scope['close_to_close']['formal_backtest_generated_at'],
        'close_to_close_combo_total_return': formal_backtest_by_label_mode['close_to_close']['combo_total_return'],
        'open_to_close_combo_total_return': formal_backtest_by_label_mode['open_to_close']['combo_total_return'],
        'close_to_close_excess_total_return': formal_backtest_by_label_mode['close_to_close']['excess_total_return'],
        'open_to_close_excess_total_return': formal_backtest_by_label_mode['open_to_close']['excess_total_return'],
        'by_label_mode': formal_backtest_by_label_mode,
        'walkforward_by_label_mode': walkforward_mode_reports,
    }
    for item in promotion.get('watch', []):
        factor_id = item.get('factor_id')
        if factor_id == 'f_chip_support_proxy':
            item['reason'] = '当前 f_chip_support_proxy 已有 formal current-weight-map 组合级 backtest 证据，analog_validation 也已提供收益级 analog replay；但这些仍不是独立 OOS / 独立 holdout，audit 状态也仍为 cold_storage，因此继续保留 watch。'
        elif factor_id == 'f_gap_return':
            item['reason'] = '当前 f_gap_return 已有 formal current-weight-map 组合级 backtest 证据，且是 latest shrink_uptrend 正式权重计划中唯一 support_pool 因子；analog_validation 也已提供收益级 analog replay，但它仍缺独立 OOS / 独立 holdout，audit 状态也仍为 cold_storage，因此继续保留 watch。'
        elif factor_id == 'f_atr_proxy_14':
            item['reason'] = '当前 f_atr_proxy_14 是 latest shrink_uptrend 正式权重计划的一部分，analog_validation 也已给出收益级 analog replay；但它在 factor_audit 中仍为 rejected_correlation，且这些证据仍不是独立 OOS / 独立 holdout，不能 keep_active 更不能 promote。'
    for item in promotion.get('demote', []):
        factor_id = item.get('factor_id')
        if factor_id == 'f_volume_ratio_5':
            item['reason'] = (
                '它已不在当前 official close_to_close latest_weight_plan 内；虽然 close_to_open latest_weight_plan 仍包含它，'
                '但这只说明标签口径切换会重排因子，不构成 official close_to_close keep_active/promotion 依据，旧版 active/watch 解释需要撤回。'
            )
        elif factor_id in {'f_realized_vol_10', 'f_ret_10_slope', 'f_up_day_ratio_10'}:
            item['reason'] = (
                '它出现在上一版 official close_to_close 辅助层/演化结论中，但已不在当前 official close_to_close latest_weight_plan 内；'
                '即便 open_to_close / close_to_open 的候选或 union 仍可能出现相邻暴露，也不构成 official close_to_close keep_active/promotion 依据，旧版 active/watch 解释需要撤回。'
            )
    dump_json(PROMOTION_PATH, promotion)

    pool.setdefault('pool_summary', {})['global_validation_caveat'] = (
        f'multi-label canonical 与 current_combo 三口径 replay/formal backtest 已全部落地，claim-limit 已确认 {holdout_status_summary}；但 {release_gap_summary} {analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
    )
    c2c_comparison = comparison['labels']['close_to_close']
    canonical_catchup_note = (
        f"{c2c_comparison.get('weight_mtime') or c2c_comparison.get('audit_mtime') or now} canonical catch-up confirmed："
        f"factor_audit_phase1.json 已追平到 {c2c_comparison.get('benchmark_eligible_trade_days', 'NA')} 个 benchmark_eligible trade days / {c2c_comparison.get('benchmark_eligible_row_count', 'NA')} 行，"
        f"regime_weight_map_phase1.json 也已切到同一口径；latest {(c2c_comparison.get('latest_regime') or {}).get('regime_id') or 'unknown_regime'} 正式计划仍为 {' / '.join(c2c_comparison.get('latest_weight_plan_factor_ids') or []) or 'NA'}。"
    )
    selection_layers = pool.setdefault('pool_summary', {}).setdefault('selection_layers', {})
    selection_layers['note'] = canonical_catchup_note
    clean_gap = pool.setdefault('pool_summary', {}).setdefault('clean_benchmark_gap_diagnostic', {})
    clean_gap['path'] = selection_gap_context['path']
    clean_gap['dropped_rows'] = selection_gap_context['dropped_rows']
    clean_gap['reason_counts'] = selection_gap_context['reason_counts']
    clean_gap['stock_list_master_gap_rows'] = selection_gap_context['stock_list_master_gap_rows']
    clean_gap['mapping_reference_gaps'] = selection_gap_context['mapping_reference_gaps']
    clean_gap['summary'] = selection_gap_context['coverage_risk_summary']
    pool.setdefault('pool_summary', {})['latest_minimal_replay_signal'] = {
        'close_to_close_excess_total_return': primary_minimal_replay_signal['close_to_close_excess_total_return'],
        'open_to_close_excess_total_return': primary_minimal_replay_signal['open_to_close_excess_total_return'],
        'close_to_close_without_backfill_total_return': primary_minimal_replay_signal['close_to_close_without_backfill_dates_total_return'],
    }
    pool['pool_summary']['current_combo_multi_label_backtest_signal'] = current_combo_multi_label_backtest_signal
    pool_alt_stage = pool.setdefault('pool_summary', {}).setdefault('alternate_label_mode_stage', {})
    pool_alt_stage['generated_at'] = comparison.get('generated_at', now)
    for mode in ['open_to_close', 'close_to_open']:
        label_info = comparison['labels'][mode]
        target = pool_alt_stage.setdefault(mode, {})
        target.update({
            'audit': label_info['audit_path'],
            'weight_map': label_info['weight_path'],
            'audit_path': label_info['audit_path'],
            'weight_path': label_info['weight_path'],
            'audit_mtime': label_info['audit_mtime'],
            'weight_mtime': label_info['weight_mtime'],
            'label_mode': label_info['label_mode'],
            'label_column': label_info['label_column'],
            'benchmark_eligible_trade_days': label_info['benchmark_eligible_trade_days'],
            'benchmark_eligible_row_count': label_info['benchmark_eligible_row_count'],
            'factor_count': label_info['factor_count'],
            'whitelist_count': label_info['whitelist_count'],
            'coldlist_count': label_info['coldlist_count'],
            'support_pool_count': label_info['support_pool_count'],
            'fallback_pool_count': label_info['fallback_pool_count'],
            'selection_mode': label_info['selection_mode'],
            'latest_weight_plan_factor_ids': label_info['latest_weight_plan_factor_ids'],
            'latest_weight_plan_factor_union': label_info.get('latest_weight_plan_factor_union', label_info['latest_weight_plan_factor_ids']),
        })
    pool_alt_stage['summary'] = (
        f"三套 canonical 已齐并追平到 {comparison['labels']['close_to_close'].get('benchmark_eligible_trade_days', 'NA')} 个 benchmark eligible days / "
        f"{comparison['labels']['close_to_close'].get('benchmark_eligible_row_count', 'NA')} 行："
        f"c2c={comparison['labels']['close_to_close'].get('latest_weight_plan_factor_ids', [])}，"
        f"o2c={comparison['labels']['open_to_close'].get('latest_weight_plan_factor_ids', [])}，"
        f"c2o={comparison['labels']['close_to_open'].get('latest_weight_plan_factor_ids', [])}；"
        f"common_latest_weight_plan_factor_ids={comparison['latest_weight_plan_overlap'].get('common_factor_ids', [])}。"
    )
    pool_basis = pool.setdefault('canonical_refresh_basis', {})
    pool_basis.update({
        key: value
        for key, value in canonical_basis_patch.items()
        if key in {
            'audit_mtime',
            'weight_mtime',
            'label_mode',
            'label_column',
            'benchmark_eligible_trade_days',
            'benchmark_eligible_row_count',
            'multi_label_audit_mtimes',
            'multi_label_weight_mtimes',
            'open_to_close_audit_mtime',
            'open_to_close_weight_mtime',
            'close_to_open_audit_mtime',
            'close_to_open_weight_mtime',
            'multi_label_comparison_mtime',
        }
        and value not in (None, {}, [])
    })
    pool_basis['portfolio_report_mtime'] = iso_mtime(PORTFOLIO_PATH)
    pool_basis['promotion_report_mtime'] = iso_mtime(PROMOTION_PATH)
    pool_basis['overfit_report_mtime'] = iso_mtime(OVERFIT_PATH)
    pool_basis['walkforward_validation_mtimes'] = {
        mode: iso_mtime(path)
        for mode, path in WALKFORWARD_LABEL_MODE_PATHS.items()
        if path.exists()
    }
    pool_basis['open_to_close_latest_weight_plan_factor_union'] = comparison['labels']['open_to_close'].get('latest_weight_plan_factor_union', comparison['labels']['open_to_close'].get('latest_weight_plan_factor_ids', []))
    lineage.setdefault('label_specific_combo_validation', {})
    for payload in (pool, lineage):
        payload['as_of' if 'as_of' in payload else 'generated_at' if 'generated_at' in payload else 'updated_at'] = now
        payload.setdefault('source_artifacts', {})['sync_multi_label_combo_validation_context'] = str(Path(__file__).resolve())
        payload['source_artifacts']['current_combo_claim_limit_audit'] = str(CLAIM_PATH)
        payload['source_artifacts']['current_combo_nested_validation'] = str(NESTED_PATH)
        payload['source_artifacts']['current_combo_nested_validation_open_to_close'] = str(NESTED_LABEL_MODE_PATHS['open_to_close'])
        payload['source_artifacts']['current_combo_nested_validation_close_to_open'] = str(NESTED_LABEL_MODE_PATHS['close_to_open'])
        payload['source_artifacts']['current_combo_walkforward_validation'] = str(WALKFORWARD_LABEL_MODE_PATHS['close_to_close'])
        payload['source_artifacts']['current_combo_walkforward_validation_open_to_close'] = str(WALKFORWARD_LABEL_MODE_PATHS['open_to_close'])
        payload['source_artifacts']['current_combo_walkforward_validation_close_to_open'] = str(WALKFORWARD_LABEL_MODE_PATHS['close_to_open'])
        payload['label_specific_combo_validation'] = {
            'generated_at': now,
            'claim_limit_by_label_mode': {mode: item['remaining_labeled_holdout_days_count'] for mode, item in claim_scope.items()},
            'nested_applies_to_label_modes': nested['applies_to_label_modes'],
            'nested_label_mode_reports': nested_mode_reports,
            'walkforward_applies_to_label_modes': available_walkforward_modes,
            'walkforward_label_mode_reports': walkforward_mode_reports,
            'common_latest_weight_plan_factor_ids': comparison['latest_weight_plan_overlap']['common_factor_ids'],
            'sign_unstable_factor_ids': comparison['sign_unstable_factor_ids'],
            'return_level_analog_claim_allowed': analog_scope['return_level_analog_claim_allowed'],
            'analog_completed_label_modes': analog_scope['completed_label_modes'],
            'analog_usable_portfolio_replay_sample_count': analog_scope['usable_portfolio_replay_sample_count'],
            'analog_claim_limit': analog_scope['claim_limit'],
        }
    benchmark_window = lineage.setdefault('factor_pool_lineage', {}).setdefault('audit_round3', {})
    benchmark_window['source'] = str(primary_audit_path)
    benchmark_window['audit_report_mtime'] = primary_audit_mtime
    benchmark_window['weight_map_mtime'] = primary_weight_mtime
    gap_attr = benchmark_window.setdefault('panel_constituent_gap_attribution', {})
    gap_attr['path'] = selection_gap_context['path']
    gap_attr['dropped_rows'] = selection_gap_context['dropped_rows']
    gap_attr['reason_counts'] = selection_gap_context['reason_counts']
    gap_attr['stock_list_master_gap_rows'] = selection_gap_context['stock_list_master_gap_rows']
    gap_attr['mapping_reference_gaps'] = selection_gap_context['mapping_reference_gaps']
    gap_attr['summary'] = selection_gap_context['coverage_risk_summary']

    selection_gap_detail = (
        f"selection_history_gap_attribution.json 已把 selection_history→panel 的 {selection_gap_context['dropped_rows']} 行 constituent gap 归因为 universe exclusion："
        f"{selection_gap_context['reason_counts'].get('universe_liquidity_threshold', 0)} 行 liquidity_threshold、"
        f"{selection_gap_context['reason_counts'].get('universe_listing_days', 0)} 行 listing_days、"
        f"{selection_gap_context['reason_counts'].get('universe_min_price', 0)} 行 min_price，"
        f"mapping_reference_gaps={selection_gap_context['stock_list_master_gap_rows']}；"
        '当前 clean benchmark 仍不是 33×20 的全量 base 成分回放。'
    )
    current_system = lineage.setdefault('current_system', {})
    current_system_evidence = current_system.setdefault('evidence', [])
    current_system_gap_evidence = {
        'path': str(SELECTION_GAP_ATTRIBUTION_PATH),
        'lines': '1-22, 656-662',
        'detail': selection_gap_detail,
    }
    current_system_weight_evidence = {
        'path': str(LABEL_CONFIG['close_to_close']['weight_map_path']),
        'lines': '3-13, 27-64',
        'detail': canonical_catchup_note,
    }
    for evidence in current_system_evidence:
        if str(evidence.get('path')) == str(SELECTION_GAP_ATTRIBUTION_PATH):
            evidence.update(current_system_gap_evidence)
            break
    else:
        current_system_evidence.append(current_system_gap_evidence)
    for evidence in current_system_evidence:
        if str(evidence.get('path')) == str(LABEL_CONFIG['close_to_close']['weight_map_path']):
            evidence.update(current_system_weight_evidence)
            break
    else:
        current_system_evidence.append(current_system_weight_evidence)

    benchmark_window['note'] = canonical_catchup_note
    selection_relationships = lineage.setdefault('factor_pool_lineage', {}).setdefault('selection_relationships', {})
    selection_relationships['latest_regime'] = comparison['labels']['close_to_close'].get(
        'latest_regime', selection_relationships.get('latest_regime', {})
    )
    latest_weight_plan = comparison['labels']['close_to_close'].get('latest_weight_plan')
    if latest_weight_plan:
        selection_relationships['latest_weight_plan'] = latest_weight_plan

    pool.setdefault('latest_stage_poll', {})['polled_at'] = now
    pool['latest_stage_poll']['active_processes'] = []
    pool['latest_stage_poll']['current_combo_claim_limit_audit'] = validation['latest_stage_poll']['current_combo_claim_limit_audit']
    pool['latest_stage_poll']['current_combo_nested_validation'] = validation['latest_stage_poll']['current_combo_nested_validation']
    pool['latest_stage_poll']['current_combo_walkforward_validation'] = validation['latest_stage_poll']['current_combo_walkforward_validation']
    pool['latest_stage_poll']['conclusion'] = (
        f'三套 canonical audit / weight、三口径 formal combo、claim-limit、nested'
        + ('、walkforward' if walkforward_reports else '')
        + ' 与收益级 analog replay 已全部落地；'
        + f'但 common_latest_weight_plan_factor_ids=[]、{holdout_release_summary}，且 {release_gap_summary} {analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
    )

    for item in lineage.get('conflict_points', []):
        title = item.get('title')
        if title == '收益口径与回填日对结果高度敏感':
            item['current_state'] = (
                f"当前三口径 formal combo 已落地：c2c 超额 {fmt_pct(formal_backtest_by_label_mode['close_to_close']['excess_total_return'])}、"
                f"o2c 超额 {fmt_pct(formal_backtest_by_label_mode['open_to_close']['excess_total_return'])}、"
                f"c2o 超额 {fmt_pct(formal_backtest_by_label_mode['close_to_open']['excess_total_return'])}；"
                f"绝对收益为 c2c {fmt_pct(formal_backtest_by_label_mode['close_to_close']['combo_total_return'])} / "
                f"o2c {fmt_pct(formal_backtest_by_label_mode['open_to_close']['combo_total_return'])} / "
                f"c2o {fmt_pct(formal_backtest_by_label_mode['close_to_open']['combo_total_return'])}，"
                '说明收益口径切换仍会显著改变组合表现。'
            )
        elif title == '最新 fallback-only 组合缺同步 end-to-end 验证':
            item['current_state'] = (
                f"已补齐三口径 formal current-weight-map backtest、claim-limit、nested"
                + ('、walkforward' if walkforward_reports else '')
                + ' 与收益级 analog replay；'
                f"claim-limit 确认 {holdout_status_summary}，"
                f"nested 当前覆盖 {available_nested_modes_text}"
                + (f"，walkforward 当前覆盖 {available_walkforward_modes_text}" if walkforward_reports else '')
                + '，但独立 holdout / OOS 仍缺。'
            )
            item['impact'] = 'promotion freeze 必须继续，当前 lineage 只能作为过渡态记录。'

    lineage_basis = lineage.setdefault('canonical_refresh_basis', {})
    validation_basis = validation.get('canonical_refresh_basis', {})
    for key in [
        'audit_mtime',
        'weight_mtime',
        'formal_backtest_mtime',
        'claim_limit_audit_mtime',
        'micro_nested_validation_mtime',
        'label_mode',
        'benchmark_eligible_trade_days',
        'benchmark_eligible_row_count',
        'multi_label_audit_mtimes',
        'multi_label_weight_mtimes',
        'open_to_close_audit_mtime',
        'open_to_close_weight_mtime',
        'close_to_open_attempt_log_mtime',
        'close_to_open_audit_mtime',
        'close_to_open_weight_mtime',
        'multi_label_comparison_mtime',
        'portfolio_report_mtime',
        'overfit_report_mtime',
        'promotion_report_mtime',
        'walkforward_validation_mtimes',
        'control_plane_sync_at',
    ]:
        if key in validation_basis:
            lineage_basis[key] = validation_basis[key]
    lineage_basis['replay_mtime'] = iso_mtime(LABEL_CONFIG['close_to_close']['replay_path'])
    label_sensitivity_path = Path(
        (portfolio.get('source_paths', {}) or {}).get('current_combo_label_sensitivity', '')
    ) if (portfolio.get('source_paths', {}) or {}).get('current_combo_label_sensitivity') else None
    if label_sensitivity_path is not None:
        lineage_basis['label_sensitivity_mtime'] = iso_mtime(label_sensitivity_path)

    lineage_stage_poll = lineage.setdefault('latest_stage_poll', {})
    lineage_stage_poll['polled_at'] = now
    lineage_stage_poll['active_processes'] = []
    lineage_stage_poll['current_combo_claim_limit_audit'] = validation['latest_stage_poll']['current_combo_claim_limit_audit']
    lineage_stage_poll['current_combo_nested_validation'] = validation['latest_stage_poll']['current_combo_nested_validation']
    lineage_stage_poll['current_combo_walkforward_validation'] = validation['latest_stage_poll']['current_combo_walkforward_validation']
    lineage_stage_poll['current_combo_portfolio_backtest'] = validation['latest_stage_poll']['current_combo_portfolio_backtest']
    lineage_stage_poll['conclusion'] = (
        f'lineage 已确认三口径 latest_weight_plan 没有共同因子，sign_unstable_factor_ids={comparison["sign_unstable_factor_ids"]}；'
        f'当前主阻塞已收敛为 {holdout_release_summary}，且 {release_gap_summary} {analog_scope["blocker_summary"]}。'
    )

    alt_stage = lineage.setdefault('alternate_label_mode_stage', {})
    alt_stage['generated_at'] = now
    alt_stage['summary'] = (
        f"lineage 已确认三口径 latest plan 完全分叉：c2c_only={comparison['latest_weight_plan_overlap'].get('close_to_close_only', [])}，"
        f"o2c_only={comparison['latest_weight_plan_overlap'].get('open_to_close_only', [])}，"
        f"c2o_only={comparison['latest_weight_plan_overlap'].get('close_to_open_only', [])}；"
        f"当前 current_combo 的三口径 formal backtest、claim-limit、nested"
        + ('、walkforward' if walkforward_reports else '')
        + f" 与收益级 analog replay 已全部接入控制面，但 {release_gap_summary}"
    )
    alt_stage['combo_backtests_by_label_mode'] = {
        mode: dict(payload)
        for mode, payload in formal_backtest_by_label_mode.items()
    }
    alt_stage['walkforward_by_label_mode'] = walkforward_mode_reports

    pool.setdefault('latest_stage_poll', {})['polled_at'] = now
    pool['latest_stage_poll']['active_processes'] = []
    pool['latest_stage_poll']['current_combo_claim_limit_audit'] = validation['latest_stage_poll']['current_combo_claim_limit_audit']
    pool['latest_stage_poll']['current_combo_nested_validation'] = validation['latest_stage_poll']['current_combo_nested_validation']
    pool['latest_stage_poll']['current_combo_walkforward_validation'] = validation['latest_stage_poll']['current_combo_walkforward_validation']
    pool['latest_stage_poll']['conclusion'] = (
        f'三套 canonical audit / weight、三口径 formal combo、claim-limit、nested'
        + ('、walkforward' if walkforward_reports else '')
        + ' 与收益级 analog replay 已全部落地；'
        + f'但 common_latest_weight_plan_factor_ids=[]、{holdout_release_summary}，且 {release_gap_summary} {analog_scope["blocker_summary"]}，因此 promotion freeze 继续维持。'
    )
    dump_json(ITERATION_POOL_PATH, pool)
    dump_json(FACTOR_LINEAGE_PATH, lineage)

    validation['canonical_refresh_basis']['portfolio_report_mtime'] = iso_mtime(PORTFOLIO_PATH)
    validation['canonical_refresh_basis']['overfit_report_mtime'] = iso_mtime(OVERFIT_PATH)
    validation['canonical_refresh_basis']['promotion_report_mtime'] = iso_mtime(PROMOTION_PATH)
    dump_json(VALIDATION_PATH, validation)

    nested_artifact_line = (
        f"- 已补出 {NESTED_LABEL_MODE_PATHS['close_to_open']}，并把 current_combo_nested_validation 的控制面 applies_to_label_modes 扩到 {available_nested_modes}；{missing_nested_modes_text} 仍缺 label-specific nested。"
        if 'close_to_open' in available_nested_modes and missing_nested_modes
        else (
            f"- current_combo_nested_validation 已覆盖 {available_nested_modes_text}；三口径 nested 仍未形成独立 labeled holdout / 独立 OOS 放行。"
            if not missing_nested_modes
            else '- current_combo_nested_validation 仍只覆盖 c2c skeleton，o2c/c2o 仍缺 label-specific nested。'
        )
    )
    walkforward_artifact_line = (
        f"- current_combo_walkforward_validation 已覆盖 {available_walkforward_modes_text} 的{walkforward_scope_label}；但它仍与{same_clean_window_text}共样本，不能替代独立 labeled holdout / 独立 OOS。"
        if walkforward_reports and not missing_walkforward_modes
        else (
            f"- current_combo_walkforward_validation 已覆盖 {available_walkforward_modes_text} 的{walkforward_scope_label}；{missing_walkforward_modes_text} 仍缺 label-specific walkforward。"
            if walkforward_reports
            else '- 本轮尚未形成新的 current_combo_walkforward_validation 产物。'
        )
    )
    market_trade_date = current_market.get('trade_date') or '未知'
    market_state = (
        current_market.get('trend_state', {}).get('morning_brief_regime')
        or current_market.get('market_regime_summary', {}).get('morning_brief_regime')
        or '未知'
    )
    focus_sectors = current_market.get('style_and_sector', {}).get('focus_sectors') or []
    factor_factory_state = current_market.get('factor_factory_state', {})
    factor_count = factor_factory_state.get('factor_count', pool.get('pool_summary', {}).get('candidate_factor_count'))
    whitelist_count = factor_factory_state.get('whitelist_count', comparison['labels']['close_to_close'].get('whitelist_count'))
    support_pool_count = factor_factory_state.get('support_pool_count', comparison['labels']['close_to_close'].get('support_pool_count'))
    fallback_pool_count = factor_factory_state.get('fallback_pool_count', comparison['labels']['close_to_close'].get('fallback_pool_count'))
    latest_regime = (
        factor_factory_state.get('latest_weight_map_regime')
        or (comparison['labels']['close_to_close'].get('latest_regime') or {}).get('regime_id')
        or '未知'
    )
    promotion_freeze = (promotion.get('global_constraints') or {}).get('promotion_freeze')
    digest_lines = [
        '# Daily Factor Evolution Digest',
        '',
        f'- 生成时间：{now}',
        f'- 当前交易日：{market_trade_date}',
        f'- 市场状态：{market_state}',
        f"- 关注行业：{', '.join(focus_sectors) if focus_sectors else '暂无'}",
        '',
        '## 当前组合状态',
        f'- factor_count={factor_count}',
        f'- whitelist_count={whitelist_count}',
        f'- support_pool_count={support_pool_count}',
        f'- fallback_pool_count={fallback_pool_count}',
        f'- latest_regime={latest_regime}',
        f'- promotion_freeze={promotion_freeze}',
        '',
        '## 本轮新增实质产物',
        f'- 已把 current_combo_claim_limit_audit 的 holdout 与 upstream_data_reuse 扩成三口径量化：remaining_labeled_holdout_days_count={claim_limit_by_mode_text}，且 c2c / o2c / c2o 的 upstream overlap/sign lineage 已写入 by_label_mode。',
        holdout_reserve_digest_line,
        f'- 已把 portfolio_backtest_report / overfit_audit_report / factor_lineage 中沿用旧 canonical 的 audit/weight mtime 追平到当前 33 天 canonical，并重判 attribution 是否已与最新 audit/weight 对齐（当前 aligned={attribution_aligned}）。',
        nested_artifact_line,
        walkforward_artifact_line,
        selection_gap_context['digest_artifact_line'] if selection_gap_context['present'] else (
            f"- 新增 selection_history→panel benchmark 成分保留率诊断：原始 {retention_audit['selection_history_total_rows']} 行，"
            f"进入 benchmark_eligible 的只有 {retention_audit['panel_benchmark_total_rows']} 行（{fmt_pct(retention_audit['retained_ratio'])}）；"
            f"缺口最大的日期={retention_audit['worst_dropped_trade_dates'][0]['trade_date']}（dropped={retention_audit['worst_dropped_trade_dates'][0]['dropped_rows']}）。"
            if retention_audit['worst_dropped_trade_dates']
            else f"- 新增 selection_history→panel benchmark 成分保留率诊断：原始 {retention_audit['selection_history_total_rows']} 行，进入 benchmark_eligible 的只有 {retention_audit['panel_benchmark_total_rows']} 行（{fmt_pct(retention_audit['retained_ratio'])}）。"
        ),
        '- 已同步刷新 factor_validation_report / portfolio_backtest_report / overfit_audit_report / factor_promotion_decision / factor_iteration_pool / factor_lineage。',
        '',
        '## 三口径最小可信结论',
        f"- c2c：combo={fmt_pct(claim_scope['close_to_close']['primary_combo_total_return'])}，excess={fmt_pct(claim_scope['close_to_close']['primary_excess_total_return'])}，latest_plan={claim_scope['close_to_close']['latest_weight_plan_factor_ids']}。",
        f"- o2c：combo={fmt_pct(claim_scope['open_to_close']['primary_combo_total_return'])}，excess={fmt_pct(claim_scope['open_to_close']['primary_excess_total_return'])}，latest_plan={claim_scope['open_to_close']['latest_weight_plan_factor_ids']}。",
        f"- c2o：combo={fmt_pct(claim_scope['close_to_open']['primary_combo_total_return'])}，excess={fmt_pct(claim_scope['close_to_open']['primary_excess_total_return'])}，latest_plan={claim_scope['close_to_open']['latest_weight_plan_factor_ids']}。",
        *([c2c_vs_o2c_digest_line] if c2c_vs_o2c_digest_line else []),
        '',
        '## 当前最关键验证缺口',
        f'- {primary_holdout_gap}',
        selection_gap_context['digest_gap_line'],
        f'- {release_gap_summary}',
        analog_scope['digest_line'],
        '',
        '## 下一步',
        f"- {selection_gap_next_step}",
        f"- {holdout_engineering_todo}",
        f"- {engineering_todo_nested}",
        f"- {analog_scope['engineering_todo']}",
        '',
    ]
    DIGEST_PATH.write_text('\n'.join(digest_lines), encoding='utf-8')

    print(json.dumps({
        'status': 'ok',
        'claim_limit': str(CLAIM_PATH),
        'nested': str(NESTED_PATH),
        'validation': str(VALIDATION_PATH),
        'portfolio': str(PORTFOLIO_PATH),
        'overfit': str(OVERFIT_PATH),
        'promotion': str(PROMOTION_PATH),
        'iteration_pool': str(ITERATION_POOL_PATH),
        'factor_lineage': str(FACTOR_LINEAGE_PATH),
        'digest': str(DIGEST_PATH),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
