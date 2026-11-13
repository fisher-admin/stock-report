#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from greenfield_multifactor_lib import (
    MaterializationSelectionOverlay,
    add_cross_sectional_zscores,
    add_forward_labels,
    annualized_return,
    annualized_sharpe,
    build_walkforward_slices,
    calmar_ratio,
    combine_factor_scores,
    compute_factor_diagnostics,
    current_state_from_panel,
    dump_json,
    effective_top_n,
    evaluate_portfolio,
    load_config,
    load_json,
    max_drawdown,
    normalize_weights,
    read_parquet_with_retries,
    select_ranked_holdings,
    selection_overlay_summary,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run greenfield multi-factor walk-forward and analog validation')
    parser.add_argument('--config', default='/Users/fisher/.openclaw/workspace/factor_factory/config/greenfield_multifactor_v1.yaml')
    parser.add_argument(
        '--materialization-only',
        action='store_true',
        help='Skip walkforward/analog replay and only materialize the current shadow portfolio artifacts.',
    )
    return parser.parse_args()


def equal_weight_metrics(df: pd.DataFrame, return_col: str) -> dict[str, Any]:
    daily = (
        df[['trade_date', return_col]]
        .dropna()
        .groupby('trade_date', sort=True)[return_col]
        .mean()
        .reset_index(name='net_return')
    )
    if daily.empty:
        return {'daily': [], 'metrics': {'days': 0, 'total_return': 0.0, 'annualized_return': 0.0, 'sharpe': 0.0, 'max_drawdown': 0.0, 'calmar': 0.0, 'win_rate': 0.0}}
    ret = daily['net_return']
    metrics = {
        'days': int(len(daily)),
        'total_return': float((1.0 + ret).prod() - 1.0),
        'annualized_return': annualized_return(ret),
        'sharpe': annualized_sharpe(ret),
        'max_drawdown': max_drawdown(ret),
        'calmar': calmar_ratio(ret),
        'win_rate': float((ret > 0).mean()),
    }
    return {'daily': daily.to_dict(orient='records'), 'metrics': metrics}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(out) else out


def _positive_clip(value: Any, cap: float) -> float:
    return min(max(_safe_float(value), 0.0), cap)


def _negative_clip(value: Any, cap: float) -> float:
    return min(max(-_safe_float(value), 0.0), cap)


def _scoring_controls(cfg) -> dict[str, Any]:
    return cfg.scoring_controls or {}


def _selection_controls(cfg) -> dict[str, Any]:
    return cfg.selection_controls or {}


def _materialization_controls(cfg) -> dict[str, Any]:
    return getattr(cfg, 'materialization_controls', {}) or {}


def _regime_controls(cfg) -> dict[str, Any]:
    return getattr(cfg, 'regime_controls', {}) or {}


class SelectionConstraintError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _signed_factor_col(factor_id: str, direction: int) -> str:
    return f'{factor_id}__z' if direction >= 0 else f'{factor_id}__z__neg'


def _factor_family_map(cfg) -> dict[str, str]:
    families: dict[str, str] = {}
    for section in ('backtestable_factors', 'current_only_factors'):
        for item in cfg.factor_registry.get(section, []) or []:
            factor_id = str(item.get('factor_id') or '').strip()
            family = str(item.get('family') or '').strip()
            if factor_id:
                families[factor_id] = family or 'unknown'
    return families


def _coerce_direction(value: Any, fallback_weight: float) -> tuple[int, str]:
    if isinstance(value, str):
        label = value.strip().lower()
        if label == 'short':
            return -1, 'short'
        if label == 'long':
            return 1, 'long'
    if value is not None:
        numeric = int(np.sign(_safe_float(value)))
        if numeric != 0:
            return numeric, 'long' if numeric > 0 else 'short'
    return (1, 'long') if fallback_weight >= 0 else (-1, 'short')


def _unique_families_from_meta(selected_meta: list[dict[str, Any]], weights: dict[str, float] | None = None) -> list[str]:
    active = set(weights or {}) if weights is not None else None
    families = {
        str(item.get('family') or 'unknown')
        for item in selected_meta
        if active is None or str(item.get('factor_id')) in active
    }
    return sorted(families)


def _selected_factor_ids(selected_meta: list[dict[str, Any]], weights: dict[str, float] | None = None) -> list[str]:
    active = set(weights or {}) if weights is not None else None
    factor_ids = {
        str(item.get('factor_id'))
        for item in selected_meta
        if str(item.get('factor_id')) and (active is None or str(item.get('factor_id')) in active)
    }
    return sorted(factor_ids)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(parsed) else parsed


def _holdout_controls(cfg) -> dict[str, Any]:
    controls = _selection_controls(cfg)
    raw = controls.get('holdout_confirmation') or {}
    if not isinstance(raw, dict):
        return {'enabled': False}
    return raw


def _holdout_enabled(cfg) -> bool:
    return _truthy(_holdout_controls(cfg).get('enabled'))


def _context_name_from_holdout_df(holdout_df: pd.DataFrame | None) -> str | None:
    if holdout_df is None or holdout_df.empty:
        return None
    value = holdout_df.attrs.get('holdout_context')
    return str(value) if value else None


def _factor_holdout_confirmation(
    holdout_df: pd.DataFrame | None,
    factor_col: str,
    top_n: int,
    round_trip_cost: float,
    selection_overlay: MaterializationSelectionOverlay | None,
) -> dict[str, Any]:
    if holdout_df is None or holdout_df.empty:
        return {'available': False, 'reason': 'no_holdout_rows'}
    if factor_col not in holdout_df.columns:
        return {'available': False, 'reason': 'missing_factor_col'}
    if holdout_df['trade_date'].nunique() < 1:
        return {'available': False, 'reason': 'no_holdout_dates'}
    c2c = compute_factor_diagnostics(
        holdout_df,
        factor_col,
        'next_return_close_to_close_1d',
        top_n,
        round_trip_cost,
        selection_overlay=selection_overlay,
    )
    o2c = compute_factor_diagnostics(
        holdout_df,
        factor_col,
        'next_return_open_to_close_1d',
        top_n,
        round_trip_cost,
        selection_overlay=selection_overlay,
    )
    c2c_metrics = c2c.get('portfolio_metrics') or {}
    o2c_metrics = o2c.get('portfolio_metrics') or {}
    baseline_c2c = equal_weight_metrics(holdout_df, 'next_return_close_to_close_1d').get('metrics') or {}
    baseline_o2c = equal_weight_metrics(holdout_df, 'next_return_open_to_close_1d').get('metrics') or {}
    c2c_total = _safe_float(c2c_metrics.get('total_return'))
    o2c_total = _safe_float(o2c_metrics.get('total_return'))
    baseline_c2c_total = _safe_float(baseline_c2c.get('total_return'))
    baseline_o2c_total = _safe_float(baseline_o2c.get('total_return'))
    return {
        'available': True,
        'context': _context_name_from_holdout_df(holdout_df),
        'date_count': int(holdout_df['trade_date'].nunique()),
        'start': str(holdout_df['trade_date'].min()),
        'end': str(holdout_df['trade_date'].max()),
        'close_to_close_total_return': c2c_total,
        'open_to_close_total_return': o2c_total,
        'baseline_close_to_close_total_return': baseline_c2c_total,
        'baseline_open_to_close_total_return': baseline_o2c_total,
        'close_to_close_excess_total_return': c2c_total - baseline_c2c_total,
        'open_to_close_excess_total_return': o2c_total - baseline_o2c_total,
        'close_to_close_sharpe': _safe_float(c2c_metrics.get('sharpe')),
        'open_to_close_sharpe': _safe_float(o2c_metrics.get('sharpe')),
        'baseline_close_to_close_sharpe': _safe_float(baseline_c2c.get('sharpe')),
        'baseline_open_to_close_sharpe': _safe_float(baseline_o2c.get('sharpe')),
        'close_to_close_rank_ic_mean': _safe_float(c2c.get('rank_ic_mean')),
        'open_to_close_rank_ic_mean': _safe_float(o2c.get('rank_ic_mean')),
    }


def _holdout_passes(confirmation: dict[str, Any], cfg) -> tuple[bool, list[str]]:
    controls = _holdout_controls(cfg)
    if not _truthy(controls.get('enabled')):
        return True, []
    if not confirmation.get('available'):
        return False, [str(confirmation.get('reason') or 'holdout_unavailable')]
    min_c2c = _safe_float(controls.get('min_c2c_total_return'), 0.0)
    min_o2c = _safe_float(controls.get('min_o2c_total_return'), 0.0)
    require_positive_c2c = _truthy(controls.get('require_positive_c2c', True))
    require_positive_o2c = _truthy(controls.get('require_positive_o2c', True))
    failures: list[str] = []
    if require_positive_c2c:
        c2c_total = _safe_float(confirmation.get('close_to_close_total_return'))
        c2c_rank_ic = _safe_float(confirmation.get('close_to_close_rank_ic_mean'))
        # P1 guardrail: rank-IC-only evidence is useful research signal, but it is
        # not independent holdout confirmation for a fixed-long close-to-close
        # portfolio. Earlier builds allowed negative c2c total return through
        # when rank IC was positive, which kept losing fold/analog contexts alive.
        if c2c_total <= min_c2c:
            failures.append('holdout_c2c_not_positive')
            if c2c_rank_ic > 0.0:
                failures.append('weak_holdout_c2c_rank_ic_only_total_return_negative')
        min_c2c_excess = _optional_float(controls.get('min_c2c_excess_total_return_vs_baseline'))
        if min_c2c_excess is not None:
            c2c_excess = _safe_float(confirmation.get('close_to_close_excess_total_return'))
            if c2c_excess <= min_c2c_excess:
                failures.append('holdout_c2c_not_baseline_beating')
    if require_positive_o2c:
        o2c_total = _safe_float(confirmation.get('open_to_close_total_return'))
        o2c_rank_ic = _safe_float(confirmation.get('open_to_close_rank_ic_mean'))
        if o2c_total <= min_o2c and o2c_rank_ic <= 0.0:
            failures.append('holdout_o2c_not_positive')
    return not failures, failures


def _skip_context_without_holdout_confirmation(cfg) -> bool:
    controls = _holdout_controls(cfg)
    return _truthy(controls.get('skip_context_without_holdout_confirmed_c2c', False))


def _is_no_holdout_confirmed_selection_error(exc: SelectionConstraintError, cfg) -> bool:
    if exc.code != 'selection_unmet_constraints' or not _holdout_enabled(cfg):
        return False
    if int(exc.details.get('selected_factor_count') or 0) != 0:
        return False
    return _skip_context_without_holdout_confirmation(cfg)


def skipped_context_record(
    kind: str,
    idx: int,
    train_dates: list[str],
    test_dates: list[str],
    exc: SelectionConstraintError,
) -> dict[str, Any]:
    return {
        f'{kind}_id': idx,
        'status': 'skipped_no_independent_holdout_confirmed_c2c_positive_factors',
        'train_start': train_dates[0] if train_dates else None,
        'train_end': train_dates[-1] if train_dates else None,
        'test_start': test_dates[0] if test_dates else None,
        'test_end': test_dates[-1] if test_dates else None,
        'weights': {},
        'selected_factors': [],
        'selection_context': {
            'selection_mode': 'scenario_split_skip',
            'skip_reason': 'no independent trailing holdout c2c-positive factor survived fixed-long selector',
            'selection_error': exc.code,
            'selection_error_details': exc.details,
        },
        'summary': {
            'name': kind,
            'status': 'skipped',
            'reason': 'no_holdout_confirmed_c2c_positive_factors',
        },
    }


def _split_train_holdout(train_df: pd.DataFrame, cfg, context: str) -> tuple[pd.DataFrame, pd.DataFrame | None, dict[str, Any]]:
    controls = _holdout_controls(cfg)
    if not _truthy(controls.get('enabled')):
        return train_df, None, {'enabled': False}
    min_train_days = max(1, int(controls.get('min_train_days_after_split', max(40, cfg.min_train_days))))
    requested_holdout_days = max(1, int(controls.get('holdout_days', cfg.validation_days)))
    unique_dates = sorted(str(x) for x in train_df['trade_date'].dropna().astype(str).unique())
    if len(unique_dates) <= min_train_days + requested_holdout_days:
        max_holdout = max(0, len(unique_dates) - min_train_days)
        holdout_days = min(requested_holdout_days, max_holdout)
    else:
        holdout_days = requested_holdout_days
    if holdout_days <= 0:
        return train_df, None, {
            'enabled': True,
            'status': 'insufficient_dates_for_holdout_split',
            'available_dates': len(unique_dates),
            'min_train_days_after_split': min_train_days,
            'requested_holdout_days': requested_holdout_days,
        }
    holdout_dates = set(unique_dates[-holdout_days:])
    fit_df = train_df.loc[~train_df['trade_date'].astype(str).isin(holdout_dates)].copy()
    holdout_df = train_df.loc[train_df['trade_date'].astype(str).isin(holdout_dates)].copy()
    if fit_df['trade_date'].nunique() < min_train_days or holdout_df.empty:
        return train_df, None, {
            'enabled': True,
            'status': 'insufficient_dates_for_holdout_split',
            'available_dates': len(unique_dates),
            'fit_days': int(fit_df['trade_date'].nunique()),
            'holdout_days': int(holdout_df['trade_date'].nunique()),
            'min_train_days_after_split': min_train_days,
        }
    holdout_df.attrs['holdout_context'] = context
    return fit_df, holdout_df, {
        'enabled': True,
        'status': 'active',
        'context': context,
        'fit_start': unique_dates[0],
        'fit_end': sorted(fit_df['trade_date'].astype(str).unique())[-1],
        'holdout_start': sorted(holdout_dates)[0],
        'holdout_end': sorted(holdout_dates)[-1],
        'fit_days': int(fit_df['trade_date'].nunique()),
        'holdout_days': int(holdout_df['trade_date'].nunique()),
    }


def _load_locked_candidate_payload(cfg) -> tuple[dict[str, Any] | None, Path | None, dict[str, Any] | None]:
    controls = _materialization_controls(cfg)
    source = str(controls.get('locked_weight_source') or '').strip()
    if not source:
        return None, None, None

    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise SelectionConstraintError(
            'locked_weight_source_missing',
            f'locked weight source not found: {source_path}',
            {'source_path': str(source_path)},
        )

    payload = load_json(source_path)
    candidate_id = str(controls.get('locked_candidate_id') or '').strip()

    def _pick_candidate(items: list[dict[str, Any]] | None, wanted_id: str) -> dict[str, Any] | None:
        records = items or []
        if wanted_id:
            return next((item for item in records if str(item.get('candidate_id') or '') == wanted_id), None)
        return records[0] if records else None

    candidate = None
    if 'seed_candidate_ranked' in payload or 'candidate_specs' in payload:
        ranked = payload.get('seed_candidate_ranked') or []
        detailed_specs = payload.get('candidate_specs') or []
        ranked_candidate = _pick_candidate(ranked, candidate_id)
        detailed_candidate = _pick_candidate(
            detailed_specs,
            candidate_id or str((ranked_candidate or {}).get('candidate_id') or '').strip(),
        )
        if ranked_candidate and detailed_candidate:
            candidate = {**detailed_candidate, **ranked_candidate}
        else:
            candidate = ranked_candidate or detailed_candidate
        if candidate and 'weights' not in candidate:
            normalized_weights = candidate.get('normalized_weights') or {}
            if normalized_weights:
                candidate = {**candidate, 'weights': normalized_weights}
        if candidate and 'selected_factor_count' not in candidate:
            selected_factors = candidate.get('selected_factors') or []
            if selected_factors:
                candidate = {**candidate, 'selected_factor_count': len(selected_factors)}
        if candidate and 'selected_family_count' not in candidate:
            selected_factors = candidate.get('selected_factors') or []
            families = {
                str(item.get('family') or 'unknown')
                for item in selected_factors
                if str(item.get('factor_id') or '').strip()
            }
            if families:
                candidate = {**candidate, 'selected_family_count': len(families)}
    elif 'weights' in payload:
        candidate = payload

    if not candidate:
        raise SelectionConstraintError(
            'locked_candidate_missing',
            f'locked candidate {candidate_id or "<default>"} missing in {source_path}',
            {'source_path': str(source_path), 'candidate_id': candidate_id or None},
        )
    return candidate, source_path, payload


def _load_locked_candidate(cfg) -> dict[str, Any] | None:
    candidate, source_path, payload = _load_locked_candidate_payload(cfg)
    if candidate is None or source_path is None or payload is None:
        return None

    controls = _materialization_controls(cfg)
    candidate_id = str(controls.get('locked_candidate_id') or '').strip()
    weights = {str(fid): float(value) for fid, value in (candidate.get('weights') or {}).items() if str(fid)}
    if not weights:
        raise SelectionConstraintError(
            'locked_candidate_empty',
            f'locked candidate {candidate_id or candidate.get("candidate_id") or "<default>"} has no weights',
            {'source_path': str(source_path), 'candidate_id': candidate_id or candidate.get('candidate_id')},
        )

    family_map = _factor_family_map(cfg)
    pool_by_factor = {
        str(item.get('factor_id')): item
        for item in payload.get('candidate_pool', []) or []
        if str(item.get('factor_id') or '')
    }
    selected_meta = []
    for factor_id, weight in weights.items():
        pool_item = pool_by_factor.get(factor_id, {})
        direction, direction_label = _coerce_direction(pool_item.get('direction') or pool_item.get('direction_label'), weight)
        family = str(pool_item.get('family') or family_map.get(factor_id) or 'unknown')
        selected_meta.append({
            'factor_id': factor_id,
            'family': family,
            'train_score': _safe_float(pool_item.get('train_score'), abs(weight)),
            'budget_share': abs(weight),
            'direction': direction,
            'direction_label': direction_label,
            'selection_reason': 'locked_candidate_seed',
            'locked_weight_source': str(source_path),
            'locked_candidate_id': candidate.get('candidate_id') or candidate_id or 'locked_weights',
            'actual_weight': float(weight),
        })

    selected_meta.sort(key=lambda x: abs(_safe_float(x.get('actual_weight'))), reverse=True)
    factor_ids = _selected_factor_ids(selected_meta, weights)
    family_ids = _unique_families_from_meta(selected_meta, weights)
    return {
        'weights': weights,
        'selected_meta': selected_meta,
        'context': {
            'source_path': str(source_path),
            'candidate_id': candidate.get('candidate_id') or candidate_id or 'locked_weights',
            'candidate_regime_id': payload.get('current_regime_id'),
            'selection_mode': 'locked_candidate_seed',
            'selected_factor_count': int(candidate.get('selected_factor_count') or len(factor_ids)),
            'selected_family_count': int(candidate.get('selected_family_count') or len(family_ids)),
            'max_single_factor_weight': max(abs(v) for v in weights.values()),
            'effective_factor_count': float(candidate.get('effective_factor_count') or (1.0 / sum(abs(v) ** 2 for v in normalize_abs_weights(weights).values()))),
        },
    }


def _resolve_materialization_selection_overlay(cfg) -> tuple[MaterializationSelectionOverlay | None, dict[str, Any]]:
    controls = _materialization_controls(cfg)
    apply_declared_overlay = _truthy(controls.get('apply_declared_overlay'))
    candidate, source_path, _payload = _load_locked_candidate_payload(cfg)

    overlay: MaterializationSelectionOverlay | None = None
    if candidate is not None:
        raw_overlay = candidate.get('risk_overlay') or {}
        portfolio_shape = raw_overlay.get('portfolio_shape') or {}
        gap_risk_filter = raw_overlay.get('gap_risk_filter') or {}
        industry_blacklist = raw_overlay.get('industry_blacklist') or portfolio_shape.get('industry_blacklist') or []
        deduped_blacklist = tuple(dict.fromkeys(str(item).strip() for item in industry_blacklist if str(item).strip()))
        if raw_overlay:
            overlay = MaterializationSelectionOverlay(
                source_path=str(source_path) if source_path is not None else None,
                candidate_id=str(candidate.get('candidate_id') or controls.get('locked_candidate_id') or '').strip() or None,
                contract_version='2026-04-25.greenfield_declared_overlay.v1',
                top_n=_optional_positive_int(portfolio_shape.get('top_n')),
                max_names_per_industry=_optional_positive_int(portfolio_shape.get('max_names_per_industry')),
                min_listing_days=_optional_positive_int(portfolio_shape.get('min_listing_days')),
                industry_blacklist=deduped_blacklist,
                gap_risk_filter_factor_id=str(gap_risk_filter.get('factor_id') or '').strip() or None,
                gap_risk_filter_operator=str(gap_risk_filter.get('operator') or '>=').strip() or '>=',
                gap_risk_filter_min_zscore=_optional_float(gap_risk_filter.get('min_zscore')),
            )

    if apply_declared_overlay and overlay is None:
        raise SelectionConstraintError(
            'declared_overlay_missing',
            'apply_declared_overlay requested but locked candidate has no declared risk_overlay contract',
            {
                'locked_weight_source': str(source_path) if source_path is not None else None,
                'locked_candidate_id': str(controls.get('locked_candidate_id') or '').strip() or None,
            },
        )

    status = 'no_declared_overlay'
    active_overlay = None
    if overlay is not None and apply_declared_overlay:
        status = 'declared_overlay_enforced'
        active_overlay = overlay
    elif overlay is not None:
        status = 'declared_overlay_detected_not_enforced'

    return active_overlay, {
        'status': status,
        'apply_declared_overlay': apply_declared_overlay,
        'declared_overlay': selection_overlay_summary(overlay, default_top_n=cfg.top_n),
    }


def _resolve_regime_context(panel_current_state: dict[str, Any], hypotheses: dict[str, Any], locked_candidate: dict[str, Any] | None, cfg) -> tuple[str, dict[str, Any]]:
    controls = _regime_controls(cfg)
    mode = str(controls.get('official_regime_mode') or 'hypotheses_primary').strip().lower()
    hypotheses_regime = str(hypotheses.get('current_market_state', {}).get('primary_regime') or '').strip() or None
    panel_regime = str(panel_current_state.get('regime_id') or '').strip() or None
    explicit_regime = str(controls.get('locked_regime_id') or '').strip() or None

    if explicit_regime:
        official_regime_id = explicit_regime
        source = 'locked_regime_id'
    elif mode == 'panel_current':
        official_regime_id = panel_regime or hypotheses_regime or 'shrink_uptrend'
        source = 'panel_current' if panel_regime else 'hypotheses_primary_fallback'
    else:
        official_regime_id = hypotheses_regime or panel_regime or 'shrink_uptrend'
        source = 'hypotheses_primary' if hypotheses_regime else 'panel_current_fallback'

    candidate_regime_id = None
    if locked_candidate:
        candidate_regime_id = str(locked_candidate.get('context', {}).get('candidate_regime_id') or '').strip() or None

    conversion = {
        'status': 'not_applicable',
        'from_candidate_regime_id': candidate_regime_id,
        'to_official_regime_id': official_regime_id,
    }
    if candidate_regime_id and candidate_regime_id != official_regime_id:
        conversion['status'] = 'converted'
    elif candidate_regime_id and candidate_regime_id == official_regime_id:
        conversion['status'] = 'aligned'

    regime_context = {
        'official_regime_mode': mode,
        'source': source,
        'official_current_regime_id': official_regime_id,
        'panel_current_regime_id': panel_regime,
        'hypotheses_primary_regime_id': hypotheses_regime,
        'conversion': conversion,
    }
    return official_regime_id, regime_context


def _selection_failure_payload(
    reason_code: str,
    reason_message: str,
    selected_meta: list[dict[str, Any]],
    weights: dict[str, float],
    min_selected_factors: int,
    min_selected_families: int,
    max_single_factor_weight: float | None = None,
) -> dict[str, Any]:
    selected_factor_ids = _selected_factor_ids(selected_meta, weights)
    selected_family_ids = _unique_families_from_meta(selected_meta, weights)
    payload = {
        'reason_code': reason_code,
        'reason_message': reason_message,
        'selected_factor_count': len(selected_factor_ids),
        'selected_factors': selected_factor_ids,
        'selected_family_count': len(selected_family_ids),
        'selected_families': selected_family_ids,
        'min_selected_factors': int(min_selected_factors),
        'min_selected_families': int(min_selected_families),
    }
    if weights:
        payload['max_actual_weight'] = max(abs(v) for v in weights.values())
    if max_single_factor_weight is not None:
        payload['max_single_factor_weight'] = float(max_single_factor_weight)
    return payload


def _enforce_minimum_selection(
    selected_meta: list[dict[str, Any]],
    weights: dict[str, float],
    min_selected_factors: int,
    min_selected_families: int,
) -> None:
    failure = _selection_failure_payload(
        reason_code='selection_unmet_constraints',
        reason_message='selected factors/families do not meet configured minimums',
        selected_meta=selected_meta,
        weights=weights,
        min_selected_factors=min_selected_factors,
        min_selected_families=min_selected_families,
    )
    if failure['selected_factor_count'] < min_selected_factors or failure['selected_family_count'] < min_selected_families:
        raise SelectionConstraintError(
            'selection_unmet_constraints',
            'selected factors/families do not meet configured minimums',
            failure,
        )


def score_factor_candidate(diags: dict[str, Any], cfg) -> tuple[float, float, dict[str, Any]]:
    controls = _scoring_controls(cfg)
    objective = str(controls.get('objective') or 'legacy_abs_ic')
    full_c2c = diags['full_close_to_close']
    full_o2c = diags['full_open_to_close']
    reg_c2c = diags['regime_close_to_close']
    reg_o2c = diags['regime_open_to_close']

    if objective == 'legacy_abs_ic':
        score = (
            3.0 * reg_c2c['rank_ic_abs']
            + 3.0 * reg_o2c['rank_ic_abs']
            + 2.0 * full_c2c['rank_ic_abs']
            + 2.0 * full_o2c['rank_ic_abs']
            + 0.55 * reg_c2c['portfolio_metrics']['sharpe']
            + 0.55 * reg_o2c['portfolio_metrics']['sharpe']
            + 0.25 * full_c2c['portfolio_metrics']['sharpe']
            + 0.25 * full_o2c['portfolio_metrics']['sharpe']
            + 0.10 * reg_c2c['portfolio_metrics']['calmar']
            + 0.10 * reg_o2c['portfolio_metrics']['calmar']
            + 0.05 * reg_c2c['portfolio_metrics']['win_rate']
            + 0.05 * reg_o2c['portfolio_metrics']['win_rate']
        )
        stability_penalty = abs(full_c2c['portfolio_metrics']['sharpe'] - full_o2c['portfolio_metrics']['sharpe']) * 0.15
        return float(score - stability_penalty), float(stability_penalty), {
            'objective': objective,
            'c2c_weight': 0.5,
            'o2c_weight': 0.5,
            'cross_leg_penalty': 0.15,
        }

    if objective == 'c2c_priority':
        default_c2c_weight = 0.75
        default_cross_penalty = 0.45
        default_negative_ic_penalty = 2.5
        default_negative_sharpe_penalty = 0.85
    elif objective == 'o2c_priority':
        default_c2c_weight = 0.05
        default_cross_penalty = 0.05
        default_negative_ic_penalty = 0.3
        default_negative_sharpe_penalty = 0.10
    else:
        objective = 'dual_leg_stable'
        default_c2c_weight = 0.60
        default_cross_penalty = 0.35
        default_negative_ic_penalty = 2.0
        default_negative_sharpe_penalty = 0.70

    c2c_weight = _safe_float(controls.get('c2c_weight'), default_c2c_weight)
    o2c_weight = _safe_float(controls.get('o2c_weight'), max(0.0, 1.0 - c2c_weight))
    regime_weight = _safe_float(controls.get('regime_weight'), 1.35)
    full_weight = _safe_float(controls.get('full_weight'), 1.00)
    rank_ic_weight = _safe_float(controls.get('rank_ic_weight'), 3.0)
    sharpe_weight = _safe_float(controls.get('sharpe_weight'), 0.55)
    calmar_weight = _safe_float(controls.get('calmar_weight'), 0.10)
    win_rate_weight = _safe_float(controls.get('win_rate_weight'), 0.05)
    negative_ic_penalty = _safe_float(controls.get('negative_ic_penalty'), default_negative_ic_penalty)
    negative_sharpe_penalty = _safe_float(controls.get('negative_sharpe_penalty'), default_negative_sharpe_penalty)
    cross_leg_penalty = _safe_float(controls.get('cross_leg_penalty'), default_cross_penalty)

    def leg_score(diag: dict[str, Any], leg_weight: float, scope_weight: float) -> float:
        metrics = diag['portfolio_metrics']
        positive = (
            rank_ic_weight * _positive_clip(diag['rank_ic_mean'], 0.20)
            + sharpe_weight * _positive_clip(metrics.get('sharpe'), 4.0)
            + calmar_weight * _positive_clip(metrics.get('calmar'), 6.0)
            + win_rate_weight * _positive_clip(metrics.get('win_rate'), 1.0)
        )
        negative = (
            negative_ic_penalty * _negative_clip(diag['rank_ic_mean'], 0.20)
            + negative_sharpe_penalty * _negative_clip(metrics.get('sharpe'), 4.0)
        )
        return scope_weight * leg_weight * (positive - negative)

    score = (
        leg_score(reg_c2c, c2c_weight, regime_weight)
        + leg_score(reg_o2c, o2c_weight, regime_weight)
        + leg_score(full_c2c, c2c_weight, full_weight)
        + leg_score(full_o2c, o2c_weight, full_weight)
    )
    stability_penalty = cross_leg_penalty * (
        abs(_safe_float(reg_c2c['portfolio_metrics'].get('sharpe')) - _safe_float(reg_o2c['portfolio_metrics'].get('sharpe')))
        + abs(_safe_float(full_c2c['portfolio_metrics'].get('sharpe')) - _safe_float(full_o2c['portfolio_metrics'].get('sharpe')))
    )
    return float(score - stability_penalty), float(stability_penalty), {
        'objective': objective,
        'c2c_weight': c2c_weight,
        'o2c_weight': o2c_weight,
        'regime_weight': regime_weight,
        'full_weight': full_weight,
        'rank_ic_weight': rank_ic_weight,
        'sharpe_weight': sharpe_weight,
        'calmar_weight': calmar_weight,
        'win_rate_weight': win_rate_weight,
        'negative_ic_penalty': negative_ic_penalty,
        'negative_sharpe_penalty': negative_sharpe_penalty,
        'cross_leg_penalty': cross_leg_penalty,
    }


def train_factor_scores(
    train_df: pd.DataFrame,
    factor_meta: list[dict[str, Any]],
    top_n: int,
    round_trip_cost: float,
    current_regime_id: str,
    cfg,
    selection_overlay: MaterializationSelectionOverlay | None = None,
    holdout_df: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    regime_df = train_df.loc[train_df['regime_id'] == current_regime_id].copy()
    if regime_df['trade_date'].nunique() < 40:
        regime_df = train_df.copy()

    direction_mode = str((_scoring_controls(cfg).get('direction_mode') or 'fixed_long')).strip().lower()
    if direction_mode == 'best_of_long_short':
        for item in factor_meta:
            fid = item['factor_id']
            neg_col = _signed_factor_col(fid, -1)
            base_col = _signed_factor_col(fid, 1)
            if neg_col not in train_df.columns:
                train_df[neg_col] = -pd.to_numeric(train_df[base_col], errors='coerce')
            if neg_col not in regime_df.columns:
                regime_df[neg_col] = -pd.to_numeric(regime_df[base_col], errors='coerce')

    rows = []
    for item in factor_meta:
        fid = item['factor_id']
        directions = [(1, 'long')]
        if direction_mode == 'best_of_long_short':
            directions.append((-1, 'short'))

        best_candidate: dict[str, Any] | None = None
        for direction, direction_label in directions:
            factor_col = _signed_factor_col(fid, direction)
            full_c2c = compute_factor_diagnostics(
                train_df,
                factor_col,
                'next_return_close_to_close_1d',
                top_n,
                round_trip_cost,
                selection_overlay=selection_overlay,
            )
            full_o2c = compute_factor_diagnostics(
                train_df,
                factor_col,
                'next_return_open_to_close_1d',
                top_n,
                round_trip_cost,
                selection_overlay=selection_overlay,
            )
            reg_c2c = compute_factor_diagnostics(
                regime_df,
                factor_col,
                'next_return_close_to_close_1d',
                top_n,
                round_trip_cost,
                selection_overlay=selection_overlay,
            )
            reg_o2c = compute_factor_diagnostics(
                regime_df,
                factor_col,
                'next_return_open_to_close_1d',
                top_n,
                round_trip_cost,
                selection_overlay=selection_overlay,
            )
            holdout_confirmation = _factor_holdout_confirmation(
                holdout_df,
                factor_col,
                top_n,
                round_trip_cost,
                selection_overlay=selection_overlay,
            ) if _holdout_enabled(cfg) else {'available': False, 'reason': 'disabled'}
            holdout_pass, holdout_failures = _holdout_passes(holdout_confirmation, cfg)
            diagnostics = {
                'full_close_to_close': full_c2c,
                'full_open_to_close': full_o2c,
                'regime_close_to_close': reg_c2c,
                'regime_open_to_close': reg_o2c,
                'holdout_confirmation': holdout_confirmation,
                'holdout_pass': holdout_pass,
                'holdout_failures': holdout_failures,
            }
            train_score, stability_penalty, scoring_meta = score_factor_candidate(diagnostics, cfg)
            if _holdout_enabled(cfg):
                # P1 v8 strict baseline-aware objective: after v6/v7 analog passed
                # but walkforward still lagged equal-weight, rank candidates by
                # independent holdout c2c excess and explicitly penalize baseline
                # laggards.  This uses train-window trailing holdout only; it is a
                # selector objective, not a promotion waiver or test-window fit.
                holdout_c2c_excess = _safe_float(holdout_confirmation.get('close_to_close_excess_total_return'))
                reward_weight = _safe_float((_scoring_controls(cfg)).get('holdout_c2c_excess_reward_weight'), 8.0)
                penalty_weight = _safe_float((_scoring_controls(cfg)).get('holdout_c2c_excess_penalty_weight'), 10.0)
                train_score += reward_weight * _positive_clip(holdout_c2c_excess, 0.50)
                train_score -= penalty_weight * _negative_clip(holdout_c2c_excess, 0.50)
                # P1 v11 supply repair: strict baseline-beating holdout evidence
                # should be able to enter the selector even when the broader fit
                # window score is noisy/negative.  This is not a relaxation: the
                # floor is applied only after _holdout_passes() confirms positive
                # C2C total return and positive excess versus equal-weight on the
                # independent trailing holdout.  Laggards still receive the hard
                # negative penalty below and cannot satisfy fixed-long guardrails.
                holdout_c2c_total = _safe_float(holdout_confirmation.get('close_to_close_total_return'))
                score_floor = _safe_float((_scoring_controls(cfg)).get('holdout_baseline_beating_score_floor'), 0.35)
                if holdout_pass and holdout_c2c_total > 0.0 and holdout_c2c_excess > 0.0:
                    train_score = max(
                        float(train_score),
                        score_floor
                        + reward_weight * _positive_clip(holdout_c2c_excess, 0.50)
                        + 0.50 * _positive_clip(holdout_c2c_total, 0.50),
                    )
                scoring_meta = {
                    **scoring_meta,
                    'holdout_c2c_excess_reward_weight': reward_weight,
                    'holdout_c2c_excess_penalty_weight': penalty_weight,
                    'holdout_baseline_beating_score_floor': score_floor,
                    'holdout_close_to_close_total_return': holdout_c2c_total,
                    'holdout_close_to_close_excess_total_return': holdout_c2c_excess,
                    'holdout_baseline_objective': 'strict_positive_excess_over_equal_weight_with_positive_floor',
                }
            if _holdout_enabled(cfg) and not holdout_pass:
                train_score = -abs(float(train_score)) - 1000.0
            candidate = {
                'factor_id': fid,
                'family': item['family'],
                'direction': direction,
                'direction_label': direction_label,
                'score_column': factor_col,
                'train_score': float(train_score),
                'holdout_confirmation': holdout_confirmation,
                'holdout_pass': holdout_pass,
                'holdout_failures': holdout_failures,
                'diagnostics': {
                    **diagnostics,
                    'stability_penalty': stability_penalty,
                    'scoring_meta': scoring_meta,
                },
            }
            if best_candidate is None or candidate['train_score'] > best_candidate['train_score']:
                best_candidate = candidate

        assert best_candidate is not None
        rows.append(best_candidate)
    return sorted(rows, key=lambda x: x['train_score'], reverse=True)


def correlation_guard(train_df: pd.DataFrame, selected: list[dict[str, Any]], candidate: dict[str, Any], threshold: float = 0.90) -> bool:
    if not selected:
        return True
    cols = [candidate['score_column']] + [item['score_column'] for item in selected]
    subset = train_df[cols].dropna()
    if len(subset) < 200:
        return True
    corr = subset.corr().iloc[0, 1:]
    return bool((corr.abs() < threshold).all())


def normalize_abs_weights(raw_weights: dict[str, float]) -> dict[str, float]:
    nonzero = {k: float(v) for k, v in raw_weights.items() if abs(float(v)) > 1e-12}
    total = sum(abs(v) for v in nonzero.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in nonzero.items()}


def cap_weight_concentration(weights: dict[str, float], max_single_factor_weight: float) -> dict[str, float]:
    normalized = normalize_abs_weights(weights)
    if not normalized:
        return {}
    effective_cap = float(max_single_factor_weight)
    if effective_cap <= 0:
        raise SelectionConstraintError(
            'invalid_max_single_factor_weight',
            f'max_single_factor_weight must be positive, got {effective_cap}',
            {'max_single_factor_weight': effective_cap},
        )
    if effective_cap >= 0.999999:
        return normalized
    if len(normalized) * effective_cap < 1.0 - 1e-12:
        raise SelectionConstraintError(
            'concentration_cap_infeasible',
            'strict single-factor cap is infeasible for the selected factor count; refusing 1/N relaxation',
            {
                'selected_factor_count': len(normalized),
                'max_single_factor_weight': effective_cap,
                'required_minimum_cap': 1.0 / max(1, len(normalized)),
                'weights': normalized,
            },
        )

    fixed: dict[str, float] = {}
    remaining = normalized.copy()
    while remaining:
        overweight = {k: v for k, v in remaining.items() if abs(v) > effective_cap + 1e-12}
        if not overweight:
            break
        for key, value in overweight.items():
            fixed[key] = effective_cap if value >= 0 else -effective_cap
            remaining.pop(key, None)
        remaining_capacity = 1.0 - sum(abs(v) for v in fixed.values())
        if remaining_capacity <= 1e-12 or not remaining:
            break
        total_remaining = sum(abs(v) for v in remaining.values())
        if total_remaining <= 1e-12:
            break
        remaining = {k: (v / total_remaining) * remaining_capacity for k, v in remaining.items()}

    capped = normalize_abs_weights({**fixed, **remaining})
    if any(abs(v) > effective_cap + 1e-9 for v in capped.values()):
        raise SelectionConstraintError(
            'concentration_cap_violation',
            'strict single-factor cap would be violated after normalization',
            {
                'weights': capped,
                'max_single_factor_weight': effective_cap,
            },
        )
    return capped


def add_support_candidates(
    train_df: pd.DataFrame,
    factor_scores: list[dict[str, Any]],
    chosen_items: list[dict[str, Any]],
    selected_meta: list[dict[str, Any]],
    raw_weights: dict[str, float],
    min_selected_factors: int,
    min_selected_families: int,
    correlation_threshold: float,
) -> None:
    if not raw_weights:
        return

    selected_families = {item['family'] for item in chosen_items}
    if len(chosen_items) >= min_selected_factors and len(selected_families) >= min_selected_families:
        return

    remaining = [
        item for item in factor_scores
        if item['factor_id'] not in {chosen['factor_id'] for chosen in chosen_items}
        and item['train_score'] > 0
    ]
    seeds: list[dict[str, Any]] = []
    temp_selected = chosen_items.copy()
    temp_families = selected_families.copy()
    for item in remaining:
        need_family = len(temp_families) < min_selected_families and item['family'] not in temp_families
        need_factor = len(temp_selected) < min_selected_factors
        if not need_family and not need_factor:
            continue
        if not correlation_guard(train_df, temp_selected, item, correlation_threshold):
            continue
        seeds.append(item)
        temp_selected.append(item)
        temp_families.add(item['family'])
        if len(temp_selected) >= min_selected_factors and len(temp_families) >= min_selected_families:
            break

    if not seeds:
        return

    seed_budget = min(0.25, 0.06 * len(seeds))
    scale = max(0.0, 1.0 - seed_budget)
    for fid in list(raw_weights):
        raw_weights[fid] *= scale

    seed_scores = normalize_weights({item['factor_id']: max(item['train_score'], 1e-6) for item in seeds})
    if not seed_scores:
        uniform = 1.0 / len(seeds)
        seed_scores = {item['factor_id']: uniform for item in seeds}

    for item in seeds:
        alloc = seed_budget * seed_scores[item['factor_id']]
        raw_weights[item['factor_id']] = raw_weights.get(item['factor_id'], 0.0) + item['direction'] * alloc
        chosen_items.append(item)
        selected_meta.append({
            'factor_id': item['factor_id'],
            'family': item['family'],
            'train_score': item['train_score'],
            'budget_share': alloc,
            'direction': item['direction'],
            'direction_label': item['direction_label'],
            'selection_reason': 'diversification_support',
            'holdout_confirmation': item.get('holdout_confirmation'),
            'holdout_pass': item.get('holdout_pass'),
            'holdout_failures': item.get('holdout_failures'),
        })


def select_weights(
    train_df: pd.DataFrame,
    factor_scores: list[dict[str, Any]],
    family_budget: dict[str, float],
    cfg,
    locked_candidate: dict[str, Any] | None = None,
) -> tuple[dict[str, float], list[dict[str, Any]], dict[str, Any]]:
    controls = _selection_controls(cfg)
    max_factors_per_family = max(1, int(controls.get('max_factors_per_family', 2)))
    correlation_threshold = _safe_float(controls.get('correlation_threshold'), 0.90)
    min_selected_factors = max(0, int(controls.get('min_selected_factors', 0)))
    min_selected_families = max(0, int(controls.get('min_selected_families', 0)))
    max_single_factor_weight = _safe_float(controls.get('max_single_factor_weight'), 1.0)
    holdout_available_families = {
        str(item.get('family') or 'unknown')
        for item in factor_scores
        if item.get('holdout_pass') and _safe_float((item.get('holdout_confirmation') or {}).get('close_to_close_total_return')) > 0.0
    }
    if len(holdout_available_families) >= 2:
        # P1 v6: when the trailing holdout actually offers independent c2c-positive
        # families, a one-family/one-factor survivor is concentration, not a pass.
        min_selected_factors = max(min_selected_factors, 2)
        min_selected_families = max(min_selected_families, 2)

    if locked_candidate is not None:
        raw_weights = {str(k): float(v) for k, v in (locked_candidate.get('weights') or {}).items()}
        selected_meta = [dict(item) for item in locked_candidate.get('selected_meta', [])]
        _enforce_minimum_selection(
            selected_meta,
            raw_weights,
            min_selected_factors=min_selected_factors,
            min_selected_families=min_selected_families,
        )
        final_weights = cap_weight_concentration(raw_weights, max_single_factor_weight)
        selected_meta = [
            {
                **item,
                'actual_weight': final_weights.get(item['factor_id'], 0.0),
            }
            for item in selected_meta
            if item['factor_id'] in final_weights
        ]
        selected_meta.sort(key=lambda x: abs(_safe_float(x.get('actual_weight'))), reverse=True)
        return final_weights, selected_meta, dict(locked_candidate.get('context') or {})

    selected_meta: list[dict[str, Any]] = []
    chosen_items: list[dict[str, Any]] = []
    raw_weights: dict[str, float] = {}
    leftover_budget = 0.0
    by_family: dict[str, list[dict[str, Any]]] = {}
    for item in factor_scores:
        by_family.setdefault(item['family'], []).append(item)

    for family, budget in family_budget.items():
        candidates = []
        for item in by_family.get(family, []):
            if item['train_score'] <= 0:
                continue
            if not correlation_guard(train_df, chosen_items, item, correlation_threshold):
                continue
            candidates.append(item)
            if len(candidates) >= max_factors_per_family:
                break
        if not candidates:
            leftover_budget += budget
            continue

        local = normalize_weights({c['factor_id']: max(c['train_score'], 1e-6) for c in candidates})
        for item in candidates:
            alloc = budget * local[item['factor_id']]
            raw_weights[item['factor_id']] = raw_weights.get(item['factor_id'], 0.0) + item['direction'] * alloc
            chosen_items.append(item)
            selected_meta.append({
                'factor_id': item['factor_id'],
                'family': family,
                'train_score': item['train_score'],
                'budget_share': alloc,
                'direction': item['direction'],
                'direction_label': item['direction_label'],
                'selection_reason': 'family_budget',
                'holdout_confirmation': item.get('holdout_confirmation'),
                'holdout_pass': item.get('holdout_pass'),
                'holdout_failures': item.get('holdout_failures'),
            })

    if leftover_budget > 0 and raw_weights:
        score_map = {item['factor_id']: max(item['train_score'], 1e-6) for item in chosen_items}
        redist = normalize_weights(score_map)
        for item in chosen_items:
            alloc = leftover_budget * redist[item['factor_id']]
            raw_weights[item['factor_id']] = raw_weights.get(item['factor_id'], 0.0) + item['direction'] * alloc

    add_support_candidates(
        train_df,
        factor_scores,
        chosen_items,
        selected_meta,
        raw_weights,
        min_selected_factors=min_selected_factors,
        min_selected_families=min_selected_families,
        correlation_threshold=correlation_threshold,
    )

    allow_forced_support = bool(controls.get('allow_forced_support', False))
    if allow_forced_support and raw_weights:
        selected_ids = {item['factor_id'] for item in chosen_items}
        selected_families = {item['family'] for item in chosen_items}
        forced_candidates = [
            item for item in factor_scores
            if item['factor_id'] not in selected_ids
            and item.get('holdout_pass')
            and _safe_float((item.get('holdout_confirmation') or {}).get('close_to_close_total_return')) > 0.0
            and _safe_float((item.get('holdout_confirmation') or {}).get('close_to_close_excess_total_return')) > 0.0
        ]
        forced_candidates.sort(key=lambda item: item['train_score'], reverse=True)
        forced_items: list[dict[str, Any]] = []
        for item in forced_candidates:
            need_factor = len(chosen_items) + len(forced_items) < min_selected_factors
            need_family = len(selected_families | {f['family'] for f in forced_items}) < min_selected_families and item['family'] not in (selected_families | {f['family'] for f in forced_items})
            if not need_factor and not need_family:
                break
            if not correlation_guard(train_df, chosen_items + forced_items, item, correlation_threshold):
                continue
            forced_items.append(item)
            selected_ids.add(item['factor_id'])
        if forced_items:
            forced_budget = min(_safe_float(controls.get('forced_support_budget'), 0.18), 0.30)
            scale = max(0.0, 1.0 - forced_budget)
            for fid in list(raw_weights):
                raw_weights[fid] *= scale
            forced_scores = normalize_weights({item['factor_id']: max(item['train_score'], 1e-6) for item in forced_items})
            if not forced_scores:
                forced_scores = {item['factor_id']: 1.0 / len(forced_items) for item in forced_items}
            for item in forced_items:
                alloc = forced_budget * forced_scores[item['factor_id']]
                raw_weights[item['factor_id']] = raw_weights.get(item['factor_id'], 0.0) + item['direction'] * alloc
                chosen_items.append(item)
                selected_meta.append({
                    'factor_id': item['factor_id'],
                    'family': item['family'],
                    'train_score': item['train_score'],
                    'budget_share': alloc,
                    'direction': item['direction'],
                    'direction_label': item['direction_label'],
                    'selection_reason': 'forced_guardrail_support',
                    'holdout_confirmation': item.get('holdout_confirmation'),
                    'holdout_pass': item.get('holdout_pass'),
                    'holdout_failures': item.get('holdout_failures'),
                })

    _enforce_minimum_selection(
        selected_meta,
        raw_weights,
        min_selected_factors=min_selected_factors,
        min_selected_families=min_selected_families,
    )

    final_weights = cap_weight_concentration(raw_weights, max_single_factor_weight)
    selected_meta = [
        {
            **item,
            'actual_weight': final_weights.get(item['factor_id'], 0.0),
        }
        for item in selected_meta
        if item['factor_id'] in final_weights
    ]
    selected_meta.sort(key=lambda x: abs(_safe_float(x.get('actual_weight'))), reverse=True)
    return final_weights, selected_meta, {
        'selection_mode': 'family_budget_search',
        'selected_factor_count': len(_selected_factor_ids(selected_meta, final_weights)),
        'selected_family_count': len(_unique_families_from_meta(selected_meta, final_weights)),
    }


def fold_summary(name: str, result_c2c: dict[str, Any], result_o2c: dict[str, Any], baseline_c2c: dict[str, Any], baseline_o2c: dict[str, Any]) -> dict[str, Any]:
    return {
        'name': name,
        'close_to_close': result_c2c['metrics'],
        'open_to_close': result_o2c['metrics'],
        'baseline_close_to_close': baseline_c2c['metrics'],
        'baseline_open_to_close': baseline_o2c['metrics'],
        'excess': {
            'close_to_close_total_return': result_c2c['metrics']['total_return'] - baseline_c2c['metrics']['total_return'],
            'open_to_close_total_return': result_o2c['metrics']['total_return'] - baseline_o2c['metrics']['total_return'],
            'close_to_close_sharpe': result_c2c['metrics']['sharpe'] - baseline_c2c['metrics']['sharpe'],
            'open_to_close_sharpe': result_o2c['metrics']['sharpe'] - baseline_o2c['metrics']['sharpe'],
        },
    }


def aggregate_daily_records(daily_frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not daily_frames:
        return pd.DataFrame(columns=['trade_date', 'net_return'])
    combined = pd.concat(daily_frames, ignore_index=True)
    combined = combined.sort_values('trade_date').reset_index(drop=True)
    return combined


def metrics_from_daily(daily: pd.DataFrame) -> dict[str, Any]:
    if daily.empty:
        return {'days': 0, 'total_return': 0.0, 'annualized_return': 0.0, 'sharpe': 0.0, 'max_drawdown': 0.0, 'calmar': 0.0, 'win_rate': 0.0, 'avg_turnover': 0.0}
    ret = pd.to_numeric(daily['net_return'], errors='coerce').fillna(0.0)
    turnover = pd.to_numeric(daily.get('turnover'), errors='coerce').fillna(0.0) if 'turnover' in daily.columns else pd.Series(np.zeros(len(daily)))
    return {
        'days': int(len(daily)),
        'total_return': float((1.0 + ret).prod() - 1.0),
        'annualized_return': annualized_return(ret),
        'sharpe': annualized_sharpe(ret),
        'max_drawdown': max_drawdown(ret),
        'calmar': calmar_ratio(ret),
        'win_rate': float((ret > 0).mean()),
        'avg_turnover': float(turnover.mean()),
    }


def run_fold(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    factor_meta: list[dict[str, Any]],
    family_budget: dict[str, float],
    cfg,
    current_regime_id: str,
    locked_candidate: dict[str, Any] | None = None,
    selection_overlay: MaterializationSelectionOverlay | None = None,
) -> dict[str, Any]:
    fit_df, holdout_df, holdout_context = _split_train_holdout(train_df, cfg, context='fold_or_analog')
    factor_scores = train_factor_scores(
        fit_df,
        factor_meta,
        cfg.top_n,
        cfg.round_trip_cost,
        current_regime_id,
        cfg,
        selection_overlay=selection_overlay,
        holdout_df=holdout_df,
    )
    weights, selected_meta, selection_context = select_weights(fit_df, factor_scores, family_budget, cfg, locked_candidate=locked_candidate)
    if holdout_context.get('enabled'):
        selection_context = {**selection_context, 'holdout_confirmation': holdout_context}
    scored_test = combine_factor_scores(test_df, weights, output_col='composite_score')
    rebalance_interval = getattr(cfg, 'rebalance_interval', 1)  # 默认每天调仓
    result_c2c = evaluate_portfolio(
        scored_test,
        'composite_score',
        'next_return_close_to_close_1d',
        cfg.top_n,
        cfg.round_trip_cost,
        selection_overlay=selection_overlay,
        rebalance_interval=rebalance_interval,
    )
    result_o2c = evaluate_portfolio(
        scored_test,
        'composite_score',
        'next_return_open_to_close_1d',
        cfg.top_n,
        cfg.round_trip_cost,
        selection_overlay=selection_overlay,
        rebalance_interval=rebalance_interval,
    )
    baseline_c2c = equal_weight_metrics(test_df, 'next_return_close_to_close_1d')
    baseline_o2c = equal_weight_metrics(test_df, 'next_return_open_to_close_1d')
    return {
        'weights': weights,
        'selected_factors': selected_meta,
        'selection_context': selection_context,
        'factor_scores': factor_scores,
        'test_scored': scored_test,
        'summary': fold_summary('fold', result_c2c, result_o2c, baseline_c2c, baseline_o2c),
        'result_c2c': result_c2c,
        'result_o2c': result_o2c,
        'baseline_c2c': baseline_c2c,
        'baseline_o2c': baseline_o2c,
    }


def dump_failure_artifacts(
    cfg,
    run_context: dict[str, Any],
    family_budget: dict[str, float],
    current_regime_id: str,
    latest_date: str,
    exc: SelectionConstraintError,
) -> None:
    failure = {
        'schema_version': '2026-04-23.greenfield_selection_failure.v1',
        'status': exc.code,
        'message': str(exc),
        'current_regime_id': current_regime_id,
        'run_context': run_context,
        'details': exc.details,
    }
    dump_json(cfg.output_root / 'selection_failure.json', failure)
    portfolio_spec = {
        'schema_version': '2026-04-19.greenfield_portfolio_spec.v1',
        'status': exc.code,
        'trade_date': latest_date,
        'current_regime_id': current_regime_id,
        'run_context': run_context,
        'family_budget': family_budget,
        'weights': {},
        'selected_factors': [],
        'blocked_by': [exc.code],
        'failure': failure,
        'legacy_reference_only': ['prebreakout_v41'],
    }
    dump_json(cfg.output_root / 'portfolio_spec.json', portfolio_spec)
    dump_json(cfg.output_root / 'current_selection_snapshot.json', {
        'trade_date': latest_date,
        'status': exc.code,
        'run_context': run_context,
        'weights': {},
        'selected_factors': [],
        'top20': [],
        'failure': failure,
    })
    dump_json(cfg.output_root / 'validation_summary.json', {
        'mainline': 'greenfield_multifactor',
        'status': exc.code,
        'run_context': run_context,
        'current_regime_id': current_regime_id,
        'blocked_by': [exc.code],
        'failure': failure,
    })


def _materialize_locked_candidate_current_portfolio(
    cfg,
    panel: pd.DataFrame,
    factor_cols: list[str],
    family_budget: dict[str, float],
    current_regime_id: str,
    run_context: dict[str, Any],
    latest_date: str,
    locked_candidate: dict[str, Any],
    selection_overlay: MaterializationSelectionOverlay | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    weights, selected_meta, selection_context = select_weights(
        pd.DataFrame(),
        [],
        family_budget,
        cfg,
        locked_candidate=locked_candidate,
    )
    latest_rows = panel.loc[panel['trade_date'] == latest_date].copy()
    current_scored = combine_factor_scores(latest_rows, weights, output_col='composite_score').sort_values('composite_score', ascending=False)
    selection_cols = ['ts_code', 'name', 'industry', 'market', 'composite_score'] + factor_cols
    available_selection_cols = [col for col in selection_cols if col in current_scored.columns]
    current_selection_frame = select_ranked_holdings(
        current_scored,
        score_col='composite_score',
        top_n=effective_top_n(cfg.top_n, selection_overlay),
        selection_overlay=selection_overlay,
    )
    current_selection = current_selection_frame[available_selection_cols].to_dict(orient='records') if not current_selection_frame.empty else []

    overlay_summary = selection_overlay_summary(selection_overlay, default_top_n=cfg.top_n)
    final_result = {
        'weights': weights,
        'selected_factors': selected_meta,
        'selection_context': selection_context,
        'test_scored': current_scored,
    }
    portfolio_spec = {
        'schema_version': '2026-04-19.greenfield_portfolio_spec.v1',
        'trade_date': latest_date,
        'current_regime_id': current_regime_id,
        'run_context': run_context,
        'family_budget': family_budget,
        'weights': final_result['weights'],
        'selected_factors': final_result['selected_factors'],
        'selection_context': final_result.get('selection_context') or {},
        'selection_overlay': overlay_summary,
        'legacy_reference_only': ['prebreakout_v41'],
    }
    current_selection_snapshot = {
        'trade_date': latest_date,
        'run_context': run_context,
        'weights': final_result['weights'],
        'selected_factors': final_result['selected_factors'],
        'selection_context': final_result.get('selection_context') or {},
        'selection_overlay': overlay_summary,
        'top20': current_selection,
    }
    dump_json(cfg.output_root / 'portfolio_spec.json', portfolio_spec)
    dump_json(cfg.output_root / 'current_selection_snapshot.json', current_selection_snapshot)
    return final_result, portfolio_spec, current_selection_snapshot


def materialize_current_portfolio(
    cfg,
    panel: pd.DataFrame,
    factor_meta: list[dict[str, Any]],
    factor_cols: list[str],
    family_budget: dict[str, float],
    current_regime_id: str,
    run_context: dict[str, Any],
    latest_date: str,
    locked_candidate: dict[str, Any] | None = None,
    selection_overlay: MaterializationSelectionOverlay | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    full_train = panel.loc[panel['trade_date'] < latest_date].copy()
    latest_rows = panel.loc[panel['trade_date'] == latest_date].copy()
    try:
        final_result = run_fold(
            full_train,
            latest_rows,
            factor_meta,
            family_budget,
            cfg,
            current_regime_id,
            locked_candidate=locked_candidate,
            selection_overlay=selection_overlay,
        )
    except SelectionConstraintError as exc:
        exc.details.setdefault('stage', 'final_materialization')
        raise

    current_scored = final_result['test_scored'].sort_values('composite_score', ascending=False)
    selection_cols = ['ts_code', 'name', 'industry', 'market', 'composite_score'] + factor_cols
    current_selection_frame = select_ranked_holdings(
        current_scored,
        score_col='composite_score',
        top_n=effective_top_n(cfg.top_n, selection_overlay),
        selection_overlay=selection_overlay,
    )
    current_selection = current_selection_frame[selection_cols].to_dict(orient='records') if not current_selection_frame.empty else []

    overlay_summary = selection_overlay_summary(selection_overlay, default_top_n=cfg.top_n)
    portfolio_spec = {
        'schema_version': '2026-04-19.greenfield_portfolio_spec.v1',
        'trade_date': latest_date,
        'current_regime_id': current_regime_id,
        'run_context': run_context,
        'family_budget': family_budget,
        'weights': final_result['weights'],
        'selected_factors': final_result['selected_factors'],
        'selection_context': final_result.get('selection_context') or {},
        'selection_overlay': overlay_summary,
        'legacy_reference_only': ['prebreakout_v41'],
    }
    current_selection_snapshot = {
        'trade_date': latest_date,
        'run_context': run_context,
        'weights': final_result['weights'],
        'selected_factors': final_result['selected_factors'],
        'selection_context': final_result.get('selection_context') or {},
        'selection_overlay': overlay_summary,
        'top20': current_selection,
    }
    dump_json(cfg.output_root / 'portfolio_spec.json', portfolio_spec)
    dump_json(cfg.output_root / 'current_selection_snapshot.json', current_selection_snapshot)
    return final_result, portfolio_spec, current_selection_snapshot


def main() -> int:
    args = parse_args()
    cfg = load_config(Path(args.config))
    factor_meta = cfg.factor_registry['backtestable_factors']
    factor_cols = [item['factor_id'] for item in factor_meta]
    hypotheses_path_str = getattr(cfg, 'hypotheses_path', None) or (cfg.output_root / 'market_state_hypotheses.json')
    hypotheses_path = Path(hypotheses_path_str)
    if not hypotheses_path.exists():
        raise SystemExit(f'missing hypotheses file: {hypotheses_path}')
    if not cfg.panel_path.exists():
        raise SystemExit(f'missing panel file: {cfg.panel_path}')

    panel_read_kwargs: dict[str, Any] = {}
    if args.materialization_only:
        materialization_cols = sorted({
            'trade_date',
            'ts_code',
            'name',
            'industry',
            'market',
            'listing_days',
            'open',
            'close',
            'regime_id',
            'regime_confidence',
            'style_regime_id',
            'sector_regime_id',
            *factor_cols,
            *(f'{fid}__z' for fid in factor_cols),
        })
        panel_read_kwargs['columns'] = materialization_cols

    panel = read_parquet_with_retries(cfg.panel_path, **panel_read_kwargs)
    required_forward_labels = [
        'next_return_close_to_close_1d',
        'next_return_open_to_close_1d',
        'next_return_close_to_open_1d',
    ]
    if any(col not in panel.columns for col in required_forward_labels):
        panel = add_forward_labels(panel)
    missing_z = [f'{fid}__z' for fid in factor_cols if f'{fid}__z' not in panel.columns]
    if missing_z:
        panel = add_cross_sectional_zscores(panel, factor_cols)
    panel['trade_date'] = panel['trade_date'].astype(str)
    # Filter panel by start_date if configured
    if cfg.start_date:
        panel = panel.loc[panel['trade_date'] >= cfg.start_date].copy()
    hypotheses = load_json(hypotheses_path)
    family_budget = hypotheses['family_budgets']
    current_state = current_state_from_panel(panel[['trade_date', 'regime_id', 'regime_confidence', 'style_regime_id', 'sector_regime_id']].drop_duplicates())
    locked_candidate = _load_locked_candidate(cfg)
    selection_overlay, materialization_overlay = _resolve_materialization_selection_overlay(cfg)
    current_regime_id, regime_context = _resolve_regime_context(current_state, hypotheses, locked_candidate, cfg)
    run_context = {
        'config_path': str(Path(args.config).expanduser().resolve()),
        'scenario_meta': cfg.scenario_meta,
        'scoring_controls': cfg.scoring_controls,
        'selection_controls': cfg.selection_controls,
        'materialization_controls': _materialization_controls(cfg),
        'materialization_overlay': materialization_overlay,
        'regime_controls': _regime_controls(cfg),
        'regime_alignment': regime_context,
    }
    if locked_candidate is not None:
        run_context['locked_weight_input'] = locked_candidate['context']

    trade_dates = sorted(panel['trade_date'].unique().tolist())
    folds = build_walkforward_slices(trade_dates, cfg.train_days, cfg.validation_days, cfg.test_days, cfg.step_days, cfg.min_train_days)
    if len(folds) > 8:
        folds = folds[-8:]
    latest_date = trade_dates[-1]

    if args.materialization_only:
        try:
            if locked_candidate is not None:
                materialization_mode = 'locked_candidate_current_only_fast_path'
                final_result, _portfolio_spec, current_selection_snapshot = _materialize_locked_candidate_current_portfolio(
                    cfg,
                    panel,
                    factor_cols,
                    family_budget,
                    current_regime_id,
                    run_context,
                    latest_date,
                    locked_candidate=locked_candidate,
                    selection_overlay=selection_overlay,
                )
            else:
                materialization_mode = 'full_replay_materialization_only'
                final_result, _portfolio_spec, current_selection_snapshot = materialize_current_portfolio(
                    cfg,
                    panel,
                    factor_meta,
                    factor_cols,
                    family_budget,
                    current_regime_id,
                    run_context,
                    latest_date,
                    locked_candidate=locked_candidate,
                    selection_overlay=selection_overlay,
                )
            verdict = {
                'mainline': 'greenfield_multifactor',
                'status': 'materialization_only_ok',
                'materialization_mode': materialization_mode,
                'run_context': run_context,
                'current_regime_id': current_regime_id,
                'selection_context': final_result.get('selection_context') or {},
                'selection_overlay': current_selection_snapshot.get('selection_overlay') or {},
                'shadow_only': True,
                'do_not_write_qlib_or_control_sync': True,
                'next_required_stage': 'independent_oos_holdout_and_audit',
            }
            dump_json(cfg.output_root / 'validation_summary.json', verdict)
            print({
                'status': 'materialization_only_ok',
                'portfolio_spec': str(cfg.output_root / 'portfolio_spec.json'),
                'current_selection': str(cfg.output_root / 'current_selection_snapshot.json'),
                'validation_summary': str(cfg.output_root / 'validation_summary.json'),
            })
            return 0
        except SelectionConstraintError as exc:
            dump_failure_artifacts(cfg, run_context, family_budget, current_regime_id, latest_date, exc)
            print({
                'status': exc.code,
                'failure': str(cfg.output_root / 'selection_failure.json'),
                'portfolio_spec': str(cfg.output_root / 'portfolio_spec.json'),
                'current_selection': str(cfg.output_root / 'current_selection_snapshot.json'),
                'validation_summary': str(cfg.output_root / 'validation_summary.json'),
            })
            return 2

    try:
        walkforward_folds = []
        c2c_daily_frames = []
        o2c_daily_frames = []
        baseline_c2c_daily_frames = []
        baseline_o2c_daily_frames = []

        for idx, fold in enumerate(folds, start=1):
            train_df = panel.loc[panel['trade_date'].isin(fold['train_dates'])].copy()
            test_df = panel.loc[panel['trade_date'].isin(fold['test_dates'])].copy()
            try:
                result = run_fold(
                    train_df,
                    test_df,
                    factor_meta,
                    family_budget,
                    cfg,
                    current_regime_id,
                    locked_candidate=locked_candidate,
                    selection_overlay=selection_overlay,
                )
            except SelectionConstraintError as exc:
                exc.details.setdefault('stage', f'walkforward_fold_{idx}')
                if _is_no_holdout_confirmed_selection_error(exc, cfg):
                    walkforward_folds.append(skipped_context_record('fold', idx, fold['train_dates'], fold['test_dates'], exc))
                    continue
                raise
            walkforward_folds.append({
                'fold_id': idx,
                'train_start': fold['train_dates'][0],
                'train_end': fold['train_dates'][-1],
                'test_start': fold['test_dates'][0],
                'test_end': fold['test_dates'][-1],
                'weights': result['weights'],
                'selected_factors': result['selected_factors'],
                'selection_context': result.get('selection_context') or {},
                'summary': result['summary'],
            })
            c2c = result['result_c2c']['daily'].copy()
            c2c['fold_id'] = idx
            c2c_daily_frames.append(c2c)
            o2c = result['result_o2c']['daily'].copy()
            o2c['fold_id'] = idx
            o2c_daily_frames.append(o2c)
            baseline_c2c = pd.DataFrame(result['baseline_c2c']['daily'])
            if not baseline_c2c.empty:
                baseline_c2c['fold_id'] = idx
                baseline_c2c_daily_frames.append(baseline_c2c)
            baseline_o2c = pd.DataFrame(result['baseline_o2c']['daily'])
            if not baseline_o2c.empty:
                baseline_o2c['fold_id'] = idx
                baseline_o2c_daily_frames.append(baseline_o2c)

        agg_c2c = aggregate_daily_records(c2c_daily_frames)
        agg_o2c = aggregate_daily_records(o2c_daily_frames)
        agg_b_c2c = aggregate_daily_records(baseline_c2c_daily_frames)
        agg_b_o2c = aggregate_daily_records(baseline_o2c_daily_frames)

        walkforward_report = {
            'schema_version': '2026-04-19.greenfield_walkforward.v1',
            'current_regime_id': current_regime_id,
            'run_context': run_context,
            'fold_count': len(walkforward_folds),
            'family_budget': family_budget,
            'folds': walkforward_folds,
            'aggregate': {
                'close_to_close': metrics_from_daily(agg_c2c),
                'open_to_close': metrics_from_daily(agg_o2c),
                'baseline_close_to_close': metrics_from_daily(agg_b_c2c),
                'baseline_open_to_close': metrics_from_daily(agg_b_o2c),
            },
        }
        walkforward_report['aggregate']['excess'] = {
            'close_to_close_total_return': walkforward_report['aggregate']['close_to_close']['total_return'] - walkforward_report['aggregate']['baseline_close_to_close']['total_return'],
            'open_to_close_total_return': walkforward_report['aggregate']['open_to_close']['total_return'] - walkforward_report['aggregate']['baseline_open_to_close']['total_return'],
            'close_to_close_sharpe': walkforward_report['aggregate']['close_to_close']['sharpe'] - walkforward_report['aggregate']['baseline_close_to_close']['sharpe'],
            'open_to_close_sharpe': walkforward_report['aggregate']['open_to_close']['sharpe'] - walkforward_report['aggregate']['baseline_open_to_close']['sharpe'],
        }
        dump_json(cfg.output_root / 'walkforward_report.json', walkforward_report)

        analog_source = load_json(cfg.regime_analog_windows_path)
        analog_results = []
        analog_daily_c2c = []
        analog_daily_o2c = []
        for item in analog_source.get('analog_windows', [])[: cfg.analog_max_windows]:
            window_start = str(item.get('window_start') or item.get('trade_date'))
            window_end = str(item.get('window_end') or item.get('trade_date'))
            window_dates = [d for d in trade_dates if window_start <= d <= window_end]
            if not window_dates:
                continue
            start_idx = trade_dates.index(window_dates[0])
            lookback_start_idx = max(0, start_idx - cfg.analog_lookback_days)
            lookback_dates = trade_dates[lookback_start_idx:start_idx]
            if len(lookback_dates) < cfg.analog_min_lookback_days:
                continue
            train_df = panel.loc[panel['trade_date'].isin(lookback_dates)].copy()
            test_df = panel.loc[panel['trade_date'].isin(window_dates)].copy()
            try:
                result = run_fold(
                    train_df,
                    test_df,
                    factor_meta,
                    family_budget,
                    cfg,
                    current_regime_id,
                    locked_candidate=locked_candidate,
                    selection_overlay=selection_overlay,
                )
            except SelectionConstraintError as exc:
                exc.details.setdefault('stage', f'analog_window_{item.get("trade_date") or window_start}')
                if _is_no_holdout_confirmed_selection_error(exc, cfg):
                    skipped = skipped_context_record('window', len(analog_results) + 1, lookback_dates, window_dates, exc)
                    skipped.update({
                        'trade_date': item.get('trade_date'),
                        'window_start': window_start,
                        'window_end': window_end,
                        'similarity_score': item.get('similarity_score'),
                        'regime_id': item.get('regime_id'),
                    })
                    analog_results.append(skipped)
                    continue
                raise
            analog_results.append({
                'trade_date': item.get('trade_date'),
                'window_start': window_start,
                'window_end': window_end,
                'similarity_score': item.get('similarity_score'),
                'regime_id': item.get('regime_id'),
                'weights': result['weights'],
                'selected_factors': result['selected_factors'],
                'selection_context': result.get('selection_context') or {},
                'summary': result['summary'],
            })
            c2c = result['result_c2c']['daily'].copy()
            c2c['window_trade_date'] = item.get('trade_date')
            analog_daily_c2c.append(c2c)
            o2c = result['result_o2c']['daily'].copy()
            o2c['window_trade_date'] = item.get('trade_date')
            analog_daily_o2c.append(o2c)

        analog_report = {
            'schema_version': '2026-04-19.greenfield_analog_validation.v1',
            'current_regime_id': current_regime_id,
            'run_context': run_context,
            'window_count': len(analog_results),
            'windows': analog_results,
            'aggregate': {
                'close_to_close': metrics_from_daily(aggregate_daily_records(analog_daily_c2c)),
                'open_to_close': metrics_from_daily(aggregate_daily_records(analog_daily_o2c)),
            },
        }
        dump_json(cfg.output_root / 'analog_validation.json', analog_report)

        final_result, _portfolio_spec, _current_selection_snapshot = materialize_current_portfolio(
            cfg,
            panel,
            factor_meta,
            factor_cols,
            family_budget,
            current_regime_id,
            run_context,
            latest_date,
            locked_candidate=locked_candidate,
            selection_overlay=selection_overlay,
        )

        pd.concat([
            agg_c2c.assign(series='walkforward_close_to_close'),
            agg_o2c.assign(series='walkforward_open_to_close'),
        ], ignore_index=True).to_csv(cfg.output_root / 'walkforward_daily_returns.csv', index=False)

        verdict = {
            'mainline': 'greenfield_multifactor',
            'status': 'ok',
            'run_context': run_context,
            'current_regime_id': current_regime_id,
            'selection_context': final_result.get('selection_context') or {},
            'legacy_prebreakout_role': 'reference_only',
            'walkforward_close_to_close_excess_total_return': walkforward_report['aggregate']['excess']['close_to_close_total_return'],
            'walkforward_open_to_close_excess_total_return': walkforward_report['aggregate']['excess']['open_to_close_total_return'],
            'analog_close_to_close_total_return': analog_report['aggregate']['close_to_close']['total_return'],
            'analog_open_to_close_total_return': analog_report['aggregate']['open_to_close']['total_return'],
        }
        dump_json(cfg.output_root / 'validation_summary.json', verdict)
        print({
            'status': 'ok',
            'walkforward_report': str(cfg.output_root / 'walkforward_report.json'),
            'analog_report': str(cfg.output_root / 'analog_validation.json'),
            'portfolio_spec': str(cfg.output_root / 'portfolio_spec.json'),
            'current_selection': str(cfg.output_root / 'current_selection_snapshot.json'),
        })
        return 0
    except SelectionConstraintError as exc:
        dump_failure_artifacts(cfg, run_context, family_budget, current_regime_id, latest_date, exc)
        print({
            'status': exc.code,
            'failure': str(cfg.output_root / 'selection_failure.json'),
            'portfolio_spec': str(cfg.output_root / 'portfolio_spec.json'),
            'current_selection': str(cfg.output_root / 'current_selection_snapshot.json'),
        })
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
