#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
GREENFIELD = ROOT / 'outputs' / 'greenfield_multifactor'
TZ = ZoneInfo('Asia/Shanghai')

SOURCE_FILES = {
    'market_hypotheses': GREENFIELD / 'market_state_hypotheses.json',
    'factor_pool_snapshot': GREENFIELD / 'factor_pool_snapshot.json',
    'current_factor_leaders': GREENFIELD / 'current_factor_leaders.json',
    'walkforward_report': GREENFIELD / 'walkforward_report.json',
    'analog_validation': GREENFIELD / 'analog_validation.json',
    'portfolio_spec': GREENFIELD / 'portfolio_spec.json',
    'current_selection_snapshot': GREENFIELD / 'current_selection_snapshot.json',
    'validation_summary': GREENFIELD / 'validation_summary.json',
    'current_market_fingerprint': EVOLUTION / 'current_market_fingerprint.json',
    'regime_analog_windows': EVOLUTION / 'regime_analog_windows.json',
}

OUTPUT_FILES = {
    'candidate_factor_hypotheses': EVOLUTION / 'candidate_factor_hypotheses.json',
    'factor_iteration_pool': EVOLUTION / 'factor_iteration_pool.json',
    'factor_lineage': EVOLUTION / 'factor_lineage.json',
    'factor_validation_report': EVOLUTION / 'factor_validation_report.json',
    'portfolio_backtest_report': EVOLUTION / 'portfolio_backtest_report.json',
    'overfit_audit_report': EVOLUTION / 'overfit_audit_report.json',
    'factor_promotion_decision': EVOLUTION / 'factor_promotion_decision.json',
    'daily_factor_evolution_digest': EVOLUTION / 'daily_factor_evolution_digest.md',
    'factor_pool_engineering_audit': GREENFIELD / 'factor_pool_engineering_audit.md',
    'validation_verdict': GREENFIELD / 'validation_verdict.md',
    'market_state_refinement': GREENFIELD / 'market_state_refinement.md',
}


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def iso_mtime(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def normalize_json(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, list):
        return [normalize_json(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_json(item) for key, item in value.items()}
    return value


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_json(payload)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def fmt_pct(value: Any) -> str:
    if value is None:
        return 'NA'
    try:
        return f'{float(value):.2%}'
    except Exception:
        return str(value)


def fmt_num(value: Any, digits: int = 4) -> str:
    if value is None:
        return 'NA'
    try:
        return f'{float(value):.{digits}f}'
    except Exception:
        return str(value)


def aggregate_or_empty(report: dict[str, Any], key: str) -> dict[str, Any]:
    return ((report.get('aggregate') or {}).get(key) or {}) if isinstance(report, dict) else {}


def get_total_return(report: dict[str, Any], key: str) -> float | None:
    value = aggregate_or_empty(report, key).get('total_return')
    return float(value) if value is not None else None


def get_sharpe(report: dict[str, Any], key: str) -> float | None:
    value = aggregate_or_empty(report, key).get('sharpe')
    return float(value) if value is not None else None


def extract_factor_family_map(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get('factor_id')): str(item.get('family'))
        for item in snapshot.get('factor_stats', [])
        if item.get('factor_id')
    }


def extract_family_to_hypotheses(hypotheses_payload: dict[str, Any]) -> dict[str, list[str]]:
    family_map: dict[str, list[str]] = defaultdict(list)
    for item in hypotheses_payload.get('hypotheses', []) or []:
        family = item.get('family')
        hypothesis_id = item.get('hypothesis_id')
        if family and hypothesis_id:
            family_map[str(family)].append(str(hypothesis_id))
    return dict(family_map)


def selection_counts(walkforward: dict[str, Any], analog: dict[str, Any], current_selection: dict[str, Any]) -> tuple[Counter, Counter, Counter]:
    walkforward_counter: Counter[str] = Counter()
    analog_counter: Counter[str] = Counter()
    current_counter: Counter[str] = Counter()
    for fold in walkforward.get('folds', []) or []:
        for item in fold.get('selected_factors', []) or []:
            fid = item.get('factor_id')
            if fid:
                walkforward_counter[str(fid)] += 1
    for window in analog.get('windows', []) or []:
        for item in window.get('selected_factors', []) or []:
            fid = item.get('factor_id')
            if fid:
                analog_counter[str(fid)] += 1
    for item in current_selection.get('selected_factors', []) or []:
        fid = item.get('factor_id')
        if fid:
            current_counter[str(fid)] += 1
    return walkforward_counter, analog_counter, current_counter


def stage_for_factor(fid: str, walkforward_counter: Counter, analog_counter: Counter, current_selected_ids: set[str]) -> str:
    if fid in current_selected_ids:
        return 'portfolio_candidate'
    if analog_counter.get(fid, 0) >= 2 or walkforward_counter.get(fid, 0) >= 2:
        return 'shadow_candidate'
    return 'arena_tested'


def build_candidate_factor_hypotheses(now: str, hypotheses: dict[str, Any]) -> dict[str, Any]:
    return {
        'schema_version': '2026-04-21.candidate_factor_hypotheses.greenfield.v1',
        'generated_at': now,
        'mainline': 'greenfield_multifactor',
        'trade_date': hypotheses.get('trade_date'),
        'legacy_reference_only': ['prebreakout_v41'],
        'source_paths': {
            'market_state_hypotheses': str(SOURCE_FILES['market_hypotheses']),
            'current_market_fingerprint': str(SOURCE_FILES['current_market_fingerprint']),
            'regime_analog_windows': str(SOURCE_FILES['regime_analog_windows']),
        },
        'source_mtimes': {
            key: iso_mtime(path)
            for key, path in {
                'market_state_hypotheses': SOURCE_FILES['market_hypotheses'],
                'current_market_fingerprint': SOURCE_FILES['current_market_fingerprint'],
                'regime_analog_windows': SOURCE_FILES['regime_analog_windows'],
            }.items()
        },
        'current_market_state': hypotheses.get('current_market_state', {}),
        'analog_summary': hypotheses.get('analog_summary', {}),
        'family_budgets': hypotheses.get('family_budgets', {}),
        'hypotheses': hypotheses.get('hypotheses', []),
        'execution_principles': hypotheses.get('execution_principles', []),
    }


def build_factor_iteration_pool(
    now: str,
    round_name: str,
    hypotheses: dict[str, Any],
    snapshot: dict[str, Any],
    portfolio: dict[str, Any],
    current_selection: dict[str, Any],
    validation_summary: dict[str, Any],
    walkforward_counter: Counter,
    analog_counter: Counter,
) -> dict[str, Any]:
    current_selected_ids = {str(item.get('factor_id')) for item in portfolio.get('selected_factors', []) or [] if item.get('factor_id')}
    family_counts = dict(Counter(str(item.get('family')) for item in snapshot.get('factor_stats', []) if item.get('family')))
    factor_board = []
    for item in snapshot.get('factor_stats', []) or []:
        fid = str(item.get('factor_id'))
        stage = stage_for_factor(fid, walkforward_counter, analog_counter, current_selected_ids)
        factor_board.append({
            'factor_id': fid,
            'family': item.get('family'),
            'stage': stage,
            'coverage_ratio': item.get('coverage_ratio'),
            'usable_dates': item.get('usable_dates'),
            'latest_non_null_count': item.get('latest_non_null_count'),
            'walkforward_selection_count': int(walkforward_counter.get(fid, 0)),
            'analog_selection_count': int(analog_counter.get(fid, 0)),
            'current_portfolio_selected': fid in current_selected_ids,
        })
    return {
        'schema_version': '2026-04-21.factor_iteration_pool.greenfield.v1',
        'as_of': now,
        'round_name': round_name,
        'mainline': 'greenfield_multifactor',
        'legacy_reference': {
            'prebreakout_v41': 'historical_reference_only',
            'selection_history': 'excluded_from_new_mainline_training_and_weight_search',
        },
        'source_artifacts': {
            key: str(path)
            for key, path in SOURCE_FILES.items()
            if path.exists()
        },
        'pool_summary': {
            'candidate_factor_count': int(snapshot.get('backtestable_factor_count') or len(snapshot.get('factor_stats', []) or [])),
            'current_only_factor_count': int(snapshot.get('current_only_factor_count') or 0),
            'family_counts': family_counts,
            'history_range': snapshot.get('history_range', {}),
            'regime_coverage': snapshot.get('regime_coverage', {}),
            'selection_mode': 'greenfield_independent_panel',
            'current_regime_id': portfolio.get('current_regime_id'),
            'current_trade_date': portfolio.get('trade_date'),
            'current_selection_factor_ids': sorted(current_selected_ids),
            'current_selection_factor_count': len(current_selected_ids),
            'walkforward_fold_count': int(walkforward_counter.total() and len((read_json(SOURCE_FILES['walkforward_report']).get('folds') or [])) or 0),
            'validation_summary': validation_summary,
        },
        'family_budgets': hypotheses.get('family_budgets', {}),
        'current_selection': {
            'weights': current_selection.get('weights', {}),
            'selected_factors': current_selection.get('selected_factors', []),
            'top20_preview': (current_selection.get('top20') or [])[:5],
        },
        'factor_status_board': factor_board,
    }


def build_factor_lineage(
    now: str,
    round_name: str,
    hypotheses: dict[str, Any],
    snapshot: dict[str, Any],
    leaders: dict[str, Any],
    portfolio: dict[str, Any],
    walkforward_counter: Counter,
    analog_counter: Counter,
) -> dict[str, Any]:
    family_to_hypotheses = extract_family_to_hypotheses(hypotheses)
    factor_family_map = extract_factor_family_map(snapshot)
    current_selected_ids = {str(item.get('factor_id')) for item in portfolio.get('selected_factors', []) or [] if item.get('factor_id')}
    hypotheses_to_factors = []
    for item in hypotheses.get('hypotheses', []) or []:
        family = str(item.get('family')) if item.get('family') else None
        mapped = sorted(fid for fid, fam in factor_family_map.items() if family and fam == family)
        hypotheses_to_factors.append({
            'hypothesis_id': item.get('hypothesis_id'),
            'priority': item.get('priority'),
            'family': family,
            'action': item.get('action'),
            'mapped_factor_ids': mapped,
        })
    factor_routes = []
    for fid, family in sorted(factor_family_map.items()):
        leader_preview = (((leaders.get('backtestable_factors') or {}).get(fid) or {}).get('top10') or [])[:3]
        factor_routes.append({
            'factor_id': fid,
            'family': family,
            'linked_hypothesis_ids': family_to_hypotheses.get(family, []),
            'lineage_stage': stage_for_factor(fid, walkforward_counter, analog_counter, current_selected_ids),
            'walkforward_selection_count': int(walkforward_counter.get(fid, 0)),
            'analog_selection_count': int(analog_counter.get(fid, 0)),
            'current_portfolio_selected': fid in current_selected_ids,
            'current_leader_preview': leader_preview,
        })
    return {
        'schema_version': '2026-04-21.factor_lineage.greenfield.v1',
        'as_of': now,
        'round_name': round_name,
        'mainline': 'greenfield_multifactor',
        'current_system': {
            'architecture_id': 'greenfield_multifactor_independent_panel',
            'status': 'active_research_mainline',
            'legacy_reference_only': ['prebreakout_v41'],
            'selection_history_used_for_training': False,
            'current_trade_date': portfolio.get('trade_date'),
            'current_regime_id': portfolio.get('current_regime_id'),
            'current_selected_factor_ids': sorted(current_selected_ids),
        },
        'source_artifacts': {
            key: str(path)
            for key, path in SOURCE_FILES.items()
            if path.exists()
        },
        'hypothesis_to_factor_map': hypotheses_to_factors,
        'factor_routes': factor_routes,
        'current_only_sidecar_factors': list((leaders.get('current_only_factors') or {}).keys()),
    }


def build_factor_validation_report(
    now: str,
    round_name: str,
    hypotheses: dict[str, Any],
    walkforward: dict[str, Any],
    analog: dict[str, Any],
    portfolio: dict[str, Any],
    validation_summary: dict[str, Any],
) -> dict[str, Any]:
    wf_c2c = aggregate_or_empty(walkforward, 'close_to_close')
    wf_o2c = aggregate_or_empty(walkforward, 'open_to_close')
    wf_b_c2c = aggregate_or_empty(walkforward, 'baseline_close_to_close')
    wf_b_o2c = aggregate_or_empty(walkforward, 'baseline_open_to_close')
    wf_excess = ((walkforward.get('aggregate') or {}).get('excess') or {})
    analog_c2c = aggregate_or_empty(analog, 'close_to_close')
    analog_o2c = aggregate_or_empty(analog, 'open_to_close')

    gaps: list[str] = []
    if (validation_summary.get('walkforward_close_to_close_excess_total_return') or 0) < 0:
        gaps.append('walk-forward 的 close-to-close 超额仍为负，新主线隔夜腿尚未完成可推广验证。')
    if len(portfolio.get('selected_factors', []) or []) <= 1:
        gaps.append('当前组合权重塌缩为单因子，说明家族预算尚未转化成稳定的多因子组合。')
    if (validation_summary.get('analog_close_to_close_total_return') or 0) < 0:
        gaps.append('analog validation 的 close-to-close 聚合收益为负，shadow 阶段仍需继续打磨。')
    if not gaps:
        gaps.append('walk-forward 与 analog 已形成初步正反馈，但 promotion 前仍需继续扩大样本外覆盖。')

    if (validation_summary.get('walkforward_open_to_close_excess_total_return') or 0) > 0 and (validation_summary.get('walkforward_close_to_close_excess_total_return') or 0) < 0:
        overall = 'mixed_open_to_close_strong_but_close_to_close_unresolved'
    elif (validation_summary.get('walkforward_open_to_close_excess_total_return') or 0) > 0:
        overall = 'yellow_continue_shadow_research'
    else:
        overall = 'red_rework_required'

    return {
        'schema_version': '2026-04-21.factor_validation_report.greenfield.v1',
        'generated_at': now,
        'round_name': round_name,
        'mainline': 'greenfield_multifactor',
        'legacy_prebreakout_role': 'reference_only',
        'source_artifacts': {
            key: str(path)
            for key, path in SOURCE_FILES.items()
            if path.exists()
        },
        'validation_scope': {
            'independent_greenfield_panel': True,
            'legacy_selection_history_used': False,
            'legacy_prebreakout_optimized': False,
            'label_modes_covered': ['close_to_close', 'open_to_close'],
            'walkforward_completed': True,
            'analog_validation_completed': True,
        },
        'current_market_state': hypotheses.get('current_market_state', {}),
        'walkforward_validation': {
            'fold_count': walkforward.get('fold_count'),
            'close_to_close': wf_c2c,
            'open_to_close': wf_o2c,
            'baseline_close_to_close': wf_b_c2c,
            'baseline_open_to_close': wf_b_o2c,
            'excess': wf_excess,
        },
        'analog_validation': {
            'window_count': analog.get('window_count'),
            'close_to_close': analog_c2c,
            'open_to_close': analog_o2c,
        },
        'overall_assessment': overall,
        'three_most_severe_gaps': gaps[:3],
        'validation_summary': validation_summary,
    }


def build_portfolio_backtest_report(
    now: str,
    round_name: str,
    hypotheses: dict[str, Any],
    walkforward: dict[str, Any],
    analog: dict[str, Any],
    portfolio: dict[str, Any],
    current_selection: dict[str, Any],
) -> dict[str, Any]:
    return {
        'report_type': 'portfolio_backtest_report',
        'generated_at': now,
        'round_name': round_name,
        'mainline': 'greenfield_multifactor',
        'legacy_reference_only': ['prebreakout_v41'],
        'source_paths': {
            key: str(path)
            for key, path in SOURCE_FILES.items()
            if path.exists()
        },
        'source_mtimes': {
            key: iso_mtime(path)
            for key, path in SOURCE_FILES.items()
            if path.exists()
        },
        'current_market_state': hypotheses.get('current_market_state', {}),
        'current_portfolio_structure': {
            'trade_date': portfolio.get('trade_date'),
            'current_regime_id': portfolio.get('current_regime_id'),
            'family_budget': portfolio.get('family_budget', {}),
            'weights': portfolio.get('weights', {}),
            'selected_factors': portfolio.get('selected_factors', []),
            'selected_factor_count': len(portfolio.get('selected_factors', []) or []),
            'top20_preview': (current_selection.get('top20') or [])[:5],
        },
        'walkforward_aggregate': walkforward.get('aggregate', {}),
        'analog_aggregate': analog.get('aggregate', {}),
        'conclusion': {
            'summary': (
                '新主线当前在 open-to-close 上表现明显强于等权基线，但 close-to-close 仍待修复；'
                '因此本轮只进入 portfolio_candidate / shadow 迭代，不做正式 promotion。'
            )
        },
    }


def build_overfit_audit_report(
    now: str,
    round_name: str,
    walkforward: dict[str, Any],
    analog: dict[str, Any],
    portfolio: dict[str, Any],
    walkforward_counter: Counter,
    analog_counter: Counter,
) -> dict[str, Any]:
    weights = {str(k): float(v) for k, v in (portfolio.get('weights') or {}).items()}
    max_weight = max(weights.values()) if weights else 0.0
    current_factor_count = len(weights)
    fold_factor_counts = [len(fold.get('selected_factors', []) or []) for fold in walkforward.get('folds', []) or []]
    c2c_excess = ((walkforward.get('aggregate') or {}).get('excess') or {}).get('close_to_close_total_return')
    o2c_excess = ((walkforward.get('aggregate') or {}).get('excess') or {}).get('open_to_close_total_return')
    analog_c2c = get_total_return(analog, 'close_to_close')
    analog_o2c = get_total_return(analog, 'open_to_close')
    high_concentration = current_factor_count <= 1 or max_weight >= 0.65
    cross_leg_divergence = (c2c_excess is not None and o2c_excess is not None and c2c_excess < 0 < o2c_excess)
    analog_divergence = (analog_c2c is not None and analog_o2c is not None and analog_c2c < 0 < analog_o2c)
    risk_level = 'high' if high_concentration or cross_leg_divergence or analog_divergence else 'medium'

    recurring = []
    for fid in sorted(set(walkforward_counter) | set(analog_counter) | set(weights)):
        recurring.append({
            'factor_id': fid,
            'walkforward_selection_count': int(walkforward_counter.get(fid, 0)),
            'analog_selection_count': int(analog_counter.get(fid, 0)),
            'current_weight': weights.get(fid),
        })

    summary_bits = []
    if high_concentration:
        summary_bits.append('当前组合过度集中于单一因子')
    if cross_leg_divergence:
        summary_bits.append('walk-forward 的 c2c 与 o2c 收益方向分裂')
    if analog_divergence:
        summary_bits.append('analog 的 c2c 与 o2c 收益方向分裂')
    if not summary_bits:
        summary_bits.append('暂无极端结构性过拟合告警，但仍需继续观察')

    return {
        'report_type': 'overfit_audit_report',
        'generated_at': now,
        'round_name': round_name,
        'mainline': 'greenfield_multifactor',
        'overall_risk_level': risk_level,
        'legacy_prebreakout_role': 'reference_only',
        'concentration_risk': {
            'current_selected_factor_count': current_factor_count,
            'max_single_factor_weight': max_weight,
            'fold_selected_factor_counts': fold_factor_counts,
            'high_concentration': high_concentration,
        },
        'cross_leg_instability': {
            'walkforward_close_to_close_excess_total_return': c2c_excess,
            'walkforward_open_to_close_excess_total_return': o2c_excess,
            'analog_close_to_close_total_return': analog_c2c,
            'analog_open_to_close_total_return': analog_o2c,
            'walkforward_cross_leg_divergence': cross_leg_divergence,
            'analog_cross_leg_divergence': analog_divergence,
        },
        'recurring_factor_exposure': recurring,
        'overall_verdict': {
            'promotion_freeze_recommended': True,
            'summary': '；'.join(summary_bits) + '，因此本轮继续停留在 shadow / portfolio_candidate 阶段。',
        },
    }


def build_factor_promotion_decision(
    now: str,
    round_name: str,
    portfolio: dict[str, Any],
    walkforward_counter: Counter,
    analog_counter: Counter,
    validation_summary: dict[str, Any],
) -> dict[str, Any]:
    current_selected = portfolio.get('selected_factors', []) or []
    shadow_candidates = []
    for item in current_selected:
        fid = str(item.get('factor_id'))
        shadow_candidates.append({
            'factor_id': fid,
            'family': item.get('family'),
            'current_stage': 'portfolio_candidate',
            'walkforward_selection_count': int(walkforward_counter.get(fid, 0)),
            'analog_selection_count': int(analog_counter.get(fid, 0)),
            'reason': '已进入当前组合权重，但 walk-forward c2c 与 analog c2c 尚未转正，暂不 promotion。',
        })

    watch = []
    for fid, count in walkforward_counter.most_common(8):
        if any(fid == str(item.get('factor_id')) for item in shadow_candidates):
            continue
        watch.append({
            'factor_id': fid,
            'current_stage': 'shadow_candidate',
            'walkforward_selection_count': int(count),
            'analog_selection_count': int(analog_counter.get(fid, 0)),
        })

    decision = 'stay_in_shadow_and_iterate'
    if (validation_summary.get('walkforward_close_to_close_excess_total_return') or 0) > 0 and (validation_summary.get('analog_close_to_close_total_return') or 0) > 0:
        decision = 'eligible_for_shadow_scale_up_not_promotion'

    return {
        'report_type': 'factor_promotion_decision',
        'generated_at': now,
        'round_name': round_name,
        'policy': 'greenfield_shadow_ladder_v1',
        'global_constraints': {
            'legacy_prebreakout_reference_only': True,
            'independent_greenfield_panel': True,
            'walkforward_completed': True,
            'analog_validation_completed': True,
            'promotion_freeze': True,
        },
        'round_decision': decision,
        'promote': [],
        'keep_active': [],
        'shadow_candidates': shadow_candidates,
        'watch': watch,
        'legacy_reference_only': [
            {
                'factor_id': 'prebreakout_v41',
                'role': 'historical_reference_old_baseline_candidate_control',
            }
        ],
        'summary': {
            'walkforward_close_to_close_excess_total_return': validation_summary.get('walkforward_close_to_close_excess_total_return'),
            'walkforward_open_to_close_excess_total_return': validation_summary.get('walkforward_open_to_close_excess_total_return'),
            'analog_close_to_close_total_return': validation_summary.get('analog_close_to_close_total_return'),
            'analog_open_to_close_total_return': validation_summary.get('analog_open_to_close_total_return'),
        },
    }


def build_digest(
    now: str,
    hypotheses: dict[str, Any],
    snapshot: dict[str, Any],
    portfolio: dict[str, Any],
    validation_summary: dict[str, Any],
    overfit: dict[str, Any],
    promotion: dict[str, Any],
) -> str:
    selected = portfolio.get('selected_factors', []) or []
    selected_ids = [str(item.get('factor_id')) for item in selected if item.get('factor_id')]
    return '\n'.join([
        '# Daily Factor Evolution Digest',
        '',
        f'- generated_at: {now}',
        f"- 当前主线: greenfield_multifactor（prebreakout_v41 仅历史参考）",
        f"- 当前交易日: {portfolio.get('trade_date')}",
        f"- 当前 regime: {portfolio.get('current_regime_id')}",
        f"- 因子池: {snapshot.get('backtestable_factor_count')} 个 backtestable + {snapshot.get('current_only_factor_count')} 个 current-only",
        f"- 当前组合入选因子: {', '.join(selected_ids) if selected_ids else '无'}",
        f"- walk-forward c2c 超额: {fmt_pct(validation_summary.get('walkforward_close_to_close_excess_total_return'))}",
        f"- walk-forward o2c 超额: {fmt_pct(validation_summary.get('walkforward_open_to_close_excess_total_return'))}",
        f"- analog c2c 聚合收益: {fmt_pct(validation_summary.get('analog_close_to_close_total_return'))}",
        f"- analog o2c 聚合收益: {fmt_pct(validation_summary.get('analog_open_to_close_total_return'))}",
        f"- 过拟合风险: {overfit.get('overall_risk_level')}",
        f"- promotion 决策: {promotion.get('round_decision')}",
        '',
        '## 主线纠偏',
        '- prebreakout_v41 只保留为历史参考 / 旧基准 / 候选和对照。',
        '- 新主线仅使用独立 greenfield panel，不读取 legacy selection_history 做训练或权重优化。',
        '',
        '## 本轮三并行结论',
        '- A：已基于 current_market_fingerprint / regime_analog_windows 产出新的市场假设。',
        '- B：已重建独立新因子池，并把 lineage/iteration pool 切到 greenfield 主线。',
        '- C：已生成新组合权重、组合级 walk-forward / analog 验证与过拟合审计。',
        '',
    ]) + '\n'


def build_factor_pool_engineering_audit_md(snapshot: dict[str, Any], portfolio: dict[str, Any]) -> str:
    selected_ids = [str(item.get('factor_id')) for item in (portfolio.get('selected_factors') or []) if item.get('factor_id')]
    return '\n'.join([
        '# Greenfield Factor Pool Engineering Audit',
        '',
        f"- 最新交易日: {portfolio.get('trade_date')}",
        f"- 历史覆盖: {(snapshot.get('history_range') or {}).get('start_date')} -> {(snapshot.get('history_range') or {}).get('end_date')}",
        f"- 回测因子数: {snapshot.get('backtestable_factor_count')}",
        f"- current-only 因子数: {snapshot.get('current_only_factor_count')}",
        f"- 当前组合入选: {', '.join(selected_ids) if selected_ids else '无'}",
        '',
        '## 约束',
        '- prebreakout_v41 不写入 greenfield panel。',
        '- selection_history 不参与新主线训练。',
        '- current-only 质量因子只做 sidecar，不伪造历史。',
        '',
    ]) + '\n'


def build_validation_verdict_md(validation_summary: dict[str, Any], overfit: dict[str, Any], promotion: dict[str, Any]) -> str:
    return '\n'.join([
        '# Greenfield Validation Verdict',
        '',
        f"- walk-forward c2c 超额: {fmt_pct(validation_summary.get('walkforward_close_to_close_excess_total_return'))}",
        f"- walk-forward o2c 超额: {fmt_pct(validation_summary.get('walkforward_open_to_close_excess_total_return'))}",
        f"- analog c2c 聚合收益: {fmt_pct(validation_summary.get('analog_close_to_close_total_return'))}",
        f"- analog o2c 聚合收益: {fmt_pct(validation_summary.get('analog_open_to_close_total_return'))}",
        f"- overfit 风险等级: {overfit.get('overall_risk_level')}",
        f"- promotion 决策: {promotion.get('round_decision')}",
        '',
        '## 结论',
        '- open-to-close 侧已有明显正信号，但 close-to-close 仍需继续打磨。',
        '- 当前组合集中度偏高，不能直接 promotion。',
        '- 本轮维持 shadow / portfolio_candidate，继续扩充多因子稳定性。',
        '',
    ]) + '\n'


def build_market_state_refinement_md(hypotheses: dict[str, Any]) -> str:
    state = hypotheses.get('current_market_state', {}) or {}
    focus = ', '.join(state.get('focus_sectors') or [])
    return '\n'.join([
        '# Greenfield Market State Refinement',
        '',
        f"- trade_date: {state.get('trade_date')}",
        f"- primary_regime: {state.get('primary_regime')}",
        f"- style_regime: {state.get('style_regime')}",
        f"- sector_regime: {state.get('sector_regime')}",
        f"- breadth_ratio: {fmt_num(state.get('breadth_ratio'), 3)}",
        f"- amount_total_percentile_80d: {fmt_num(state.get('amount_total_percentile_80d'), 3)}",
        f"- focus_sectors: {focus}",
        '',
        '## 主线',
        '- liquid leadership / sector rotation / trend quality / downside control 为当前优先家族。',
        '- chip 相关只做确认层。',
        '- prebreakout_v41 不再作为当前主线中心。',
        '',
    ]) + '\n'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Sync greenfield research round into official evolution outputs')
    parser.add_argument('--round-name', default=f'greenfield_round_{datetime.now(TZ).strftime("%Y%m%dT%H%M%S")}')
    parser.add_argument('--summary-output', default=str(EVOLUTION / 'greenfield_mainline_sync_latest.json'))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = iso_now()

    missing = [str(path) for path in SOURCE_FILES.values() if not path.exists()]
    if missing:
        raise SystemExit('missing required source artifacts: ' + ', '.join(missing))

    hypotheses = read_json(SOURCE_FILES['market_hypotheses'])
    snapshot = read_json(SOURCE_FILES['factor_pool_snapshot'])
    leaders = read_json(SOURCE_FILES['current_factor_leaders'])
    walkforward = read_json(SOURCE_FILES['walkforward_report'])
    analog = read_json(SOURCE_FILES['analog_validation'])
    portfolio = read_json(SOURCE_FILES['portfolio_spec'])
    current_selection = read_json(SOURCE_FILES['current_selection_snapshot'])
    validation_summary = read_json(SOURCE_FILES['validation_summary'])

    walkforward_counter, analog_counter, _ = selection_counts(walkforward, analog, current_selection)

    candidate_factor_hypotheses = build_candidate_factor_hypotheses(now, hypotheses)
    factor_iteration_pool = build_factor_iteration_pool(
        now,
        args.round_name,
        hypotheses,
        snapshot,
        portfolio,
        current_selection,
        validation_summary,
        walkforward_counter,
        analog_counter,
    )
    factor_lineage = build_factor_lineage(
        now,
        args.round_name,
        hypotheses,
        snapshot,
        leaders,
        portfolio,
        walkforward_counter,
        analog_counter,
    )
    factor_validation_report = build_factor_validation_report(
        now,
        args.round_name,
        hypotheses,
        walkforward,
        analog,
        portfolio,
        validation_summary,
    )
    portfolio_backtest_report = build_portfolio_backtest_report(
        now,
        args.round_name,
        hypotheses,
        walkforward,
        analog,
        portfolio,
        current_selection,
    )
    overfit_audit_report = build_overfit_audit_report(
        now,
        args.round_name,
        walkforward,
        analog,
        portfolio,
        walkforward_counter,
        analog_counter,
    )
    factor_promotion_decision = build_factor_promotion_decision(
        now,
        args.round_name,
        portfolio,
        walkforward_counter,
        analog_counter,
        validation_summary,
    )

    dump_json(OUTPUT_FILES['candidate_factor_hypotheses'], candidate_factor_hypotheses)
    dump_json(OUTPUT_FILES['factor_iteration_pool'], factor_iteration_pool)
    dump_json(OUTPUT_FILES['factor_lineage'], factor_lineage)
    dump_json(OUTPUT_FILES['factor_validation_report'], factor_validation_report)
    dump_json(OUTPUT_FILES['portfolio_backtest_report'], portfolio_backtest_report)
    dump_json(OUTPUT_FILES['overfit_audit_report'], overfit_audit_report)
    dump_json(OUTPUT_FILES['factor_promotion_decision'], factor_promotion_decision)

    write_text(
        OUTPUT_FILES['daily_factor_evolution_digest'],
        build_digest(now, hypotheses, snapshot, portfolio, validation_summary, overfit_audit_report, factor_promotion_decision),
    )
    write_text(OUTPUT_FILES['factor_pool_engineering_audit'], build_factor_pool_engineering_audit_md(snapshot, portfolio))
    write_text(OUTPUT_FILES['validation_verdict'], build_validation_verdict_md(validation_summary, overfit_audit_report, factor_promotion_decision))
    write_text(OUTPUT_FILES['market_state_refinement'], build_market_state_refinement_md(hypotheses))

    summary = {
        'status': 'ok',
        'generated_at': now,
        'round_name': args.round_name,
        'written_outputs': {key: str(path) for key, path in OUTPUT_FILES.items()},
        'current_trade_date': portfolio.get('trade_date'),
        'current_selected_factors': [item.get('factor_id') for item in portfolio.get('selected_factors', []) or []],
        'walkforward_close_to_close_excess_total_return': validation_summary.get('walkforward_close_to_close_excess_total_return'),
        'walkforward_open_to_close_excess_total_return': validation_summary.get('walkforward_open_to_close_excess_total_return'),
        'analog_close_to_close_total_return': validation_summary.get('analog_close_to_close_total_return'),
        'analog_open_to_close_total_return': validation_summary.get('analog_open_to_close_total_return'),
        'overfit_risk_level': overfit_audit_report.get('overall_risk_level'),
        'promotion_round_decision': factor_promotion_decision.get('round_decision'),
    }
    dump_json(Path(args.summary_output), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
