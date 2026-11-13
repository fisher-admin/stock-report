#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 heuristic weight search for factor_factory."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    m = max(values)
    exps = [math.exp(v - m) for v in values]
    total = sum(exps) or 1.0
    return [v / total for v in exps]


def factor_score(item: dict[str, Any]) -> float:
    lift = item.get('lift') or {}
    mc = item.get('monte_carlo') or {}
    base_metrics = item.get('base_metrics') or {}
    combo_metrics = item.get('combo_metrics') or {}
    split_summary = item.get('split_summary') or {}
    sharpe_lift = float(lift.get('sharpe_lift', 0.0) or 0.0)
    win_rate_lift = float(lift.get('win_rate_lift', 0.0) or 0.0)
    rank_ic_abs = float(item.get('rank_ic_abs', abs(item.get('rank_ic_mean', 0.0)) or 0.0) or 0.0)
    mc_guard = float(mc.get('mc_sharpe_p05', 0.0) or 0.0)
    calmar_lift = float(item.get('calmar_lift', (combo_metrics.get('calmar', 0.0) or 0.0) - (base_metrics.get('calmar', 0.0) or 0.0)) or 0.0)
    stability = (
        float(split_summary.get('better_sharpe_ratio', 0.0) or 0.0) * 0.5
        + float(split_summary.get('better_win_rate_ratio', 0.0) or 0.0) * 0.3
        + float(split_summary.get('better_drawdown_ratio', 0.0) or 0.0) * 0.2
    )
    return sharpe_lift * 0.42 + calmar_lift * 0.22 + win_rate_lift * 0.16 + rank_ic_abs * 4.0 + mc_guard * 0.05 + stability * 0.35


PREFERRED_FAMILIES = {
    'volume_uptrend': ['price_volume_momentum', 'liquidity_turnover_distribution', 'chip_structure_delta'],
    'shrink_uptrend': ['volatility_compression', 'chip_structure_delta', 'price_volume_momentum'],
    'volume_downtrend': ['fundamentals_event_filters', 'chip_structure_delta', 'volatility_compression'],
    'shrink_downtrend': ['fundamentals_event_filters', 'volatility_compression', 'chip_structure_delta'],
    'high_vol_chop': ['volatility_compression', 'fundamentals_event_filters', 'chip_structure_delta'],
    'small_cap_rotation': ['liquidity_turnover_distribution', 'price_volume_momentum', 'chip_structure_delta'],
    'large_cap_rotation': ['fundamentals_event_filters', 'chip_structure_delta', 'volatility_compression'],
    'sector_concentration': ['price_volume_momentum', 'chip_structure_delta', 'liquidity_turnover_distribution'],
    'sector_dispersion': ['volatility_compression', 'fundamentals_event_filters', 'liquidity_turnover_distribution'],
}

REGIME_AUX_BUDGET = {
    'volume_uptrend': 0.60,
    'shrink_uptrend': 0.45,
    'volume_downtrend': 0.12,
    'shrink_downtrend': 0.18,
    'high_vol_chop': 0.20,
    'small_cap_rotation': 0.55,
    'large_cap_rotation': 0.35,
    'sector_concentration': 0.50,
    'sector_dispersion': 0.28,
}

DEFENSIVE_REGIMES = {'volume_downtrend', 'shrink_downtrend', 'high_vol_chop', 'large_cap_rotation'}
DEFENSIVE_COVERAGE_FAMILIES = {'volatility_compression', 'fundamentals_event_filters'}
SOURCE_BONUS = {
    'whitelist': 0.18,
    'support_pool': 0.08,
    'fallback_coldlist': 0.0,
}


def regime_priority(item: dict[str, Any], regime_id: str, preferred_set: set[str]) -> float:
    gate = item.get('gate_details') or {}
    split_summary = item.get('split_summary') or {}
    family = item.get('family', 'unknown')
    source = item.get('selection_source', 'fallback_coldlist')
    score = factor_score(item)
    priority = score + SOURCE_BONUS.get(source, 0.0)
    if family in preferred_set:
        priority += 0.18
    if regime_id in DEFENSIVE_REGIMES:
        if family in DEFENSIVE_COVERAGE_FAMILIES:
            priority += 0.16
        if gate.get('drawdown_pass'):
            priority += 0.10
        calmar_lift = float(item.get('calmar_lift', 0.0) or 0.0)
        if calmar_lift > 0.0:
            priority += min(calmar_lift, 1.0) * 0.08
        priority += float(split_summary.get('better_drawdown_ratio', 0.0) or 0.0) * 0.05
    return priority


def select_regime_candidates(all_candidates: list[dict[str, Any]], regime_id: str, preferred_set: set[str], max_factors: int = 3) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    if regime_id in DEFENSIVE_REGIMES:
        coverage_pool = [
            item
            for item in all_candidates
            if item.get('family') in DEFENSIVE_COVERAGE_FAMILIES and (item.get('gate_details') or {}).get('drawdown_pass')
        ]
        coverage_pool = sorted(coverage_pool, key=lambda x: regime_priority(x, regime_id, preferred_set), reverse=True)
        if coverage_pool:
            first = coverage_pool[0]
            chosen.append(first)
            seen.add(first['factor_id'])

    while len(chosen) < max_factors:
        chosen_families = {item.get('family', 'unknown') for item in chosen}
        best_item = None
        best_score = float('-inf')
        for item in all_candidates:
            fid = item['factor_id']
            if fid in seen:
                continue
            score = regime_priority(item, regime_id, preferred_set)
            if item.get('family') in chosen_families:
                score -= 0.10
            if regime_id in DEFENSIVE_REGIMES and item.get('family') not in preferred_set:
                score -= 0.06
            if best_item is None or score > best_score:
                best_item = item
                best_score = score
        if best_item is None:
            break
        chosen.append(best_item)
        seen.add(best_item['factor_id'])
    return chosen




def split_train_validation_test_dates(dates: list[Any], train_ratio: float = 0.4, val_ratio: float = 0.3) -> dict[str, Any]:
    sorted_dates = sorted([str(d) for d in dates if d])
    n = len(sorted_dates)
    if n < 90:
        return {
            'sample_count': n,
            'promotion_allowed': False,
            'reason': 'insufficient_dates_for_train_validation_test',
            'train_dates': [],
            'validation_dates': [],
            'test_dates': [],
            'test_set_used_for_selection': False,
        }
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))
    return {
        'sample_count': n,
        'promotion_allowed': True,
        'train_dates': sorted_dates[:train_end],
        'validation_dates': sorted_dates[train_end:val_end],
        'test_dates': sorted_dates[val_end:],
        'test_set_used_for_selection': False,
    }


def compute_max_drawdown_from_returns(returns: list[float]) -> float:
    if not returns:
        return 0.0
    equity = []
    cur = 1.0
    for r in returns:
        cur *= (1.0 + float(r))
        equity.append(cur)
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return float(mdd)


def combo_block_bootstrap(daily_returns: list[float], n_sims: int = 500, block_size: int = 5, seed: int = 42) -> dict[str, Any]:
    vals = [float(v) for v in daily_returns if v is not None and math.isfinite(float(v))]
    n = len(vals)
    if n < 2:
        return {'combo_mc_sharpe_p05': None, 'combo_mc_sharpe_p50': None, 'combo_mc_total_return_p05': None, 'combo_mc_max_drawdown_p95': None, 'combo_mc_n_sims': n_sims, 'block_size': block_size, 'sample_count': n, 'passed': False, 'mode': 'daily_return_bootstrap'}
    import random
    rng = random.Random(seed)
    sharpes=[]; totals=[]; drawdowns=[]
    for _ in range(n_sims):
        sample=[]
        while len(sample) < n:
            start = rng.randrange(n)
            for j in range(block_size):
                sample.append(vals[(start + j) % n])
                if len(sample) >= n:
                    break
        mean=sum(sample)/n
        if n > 1:
            var=sum((x-mean)**2 for x in sample)/(n-1)
        else:
            var=0.0
        std=math.sqrt(var) if var > 0 else 0.0
        sharpes.append(mean/std*math.sqrt(252) if std > 0 else 0.0)
        total=1.0
        for r in sample:
            total *= (1.0 + r)
        totals.append(total-1.0)
        drawdowns.append(compute_max_drawdown_from_returns(sample))
    def pct(arr, q):
        arr=sorted(arr)
        if not arr: return None
        k=(len(arr)-1)*q/100.0
        lo=int(math.floor(k)); hi=int(math.ceil(k))
        if lo == hi: return float(arr[lo])
        return float(arr[lo]*(hi-k)+arr[hi]*(k-lo))
    p05=pct(sharpes,5)
    return {'combo_mc_sharpe_p05': p05, 'combo_mc_sharpe_p50': pct(sharpes,50), 'combo_mc_total_return_p05': pct(totals,5), 'combo_mc_max_drawdown_p95': pct(drawdowns,95), 'combo_mc_n_sims': n_sims, 'block_size': block_size, 'sample_count': n, 'passed': bool(p05 is not None and p05 > 0), 'mode': 'daily_return_bootstrap'}


def extract_latest_daily_returns(items: list[dict[str, Any]]) -> list[float]:
    rows=[]
    for item in items:
        for split in item.get('split_reports', []) or []:
            metrics = split.get('combo_metrics') or {}
            ret = metrics.get('total_return')
            days = int(metrics.get('days', 1) or 1)
            if ret is not None:
                # Convert split total return to one average daily proxy for bootstrap if raw daily series is unavailable.
                daily = (1.0 + float(ret)) ** (1.0 / max(days, 1)) - 1.0
                rows.append(daily)
    return rows


def marginal_contribution_from_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        lift = item.get('lift') or {}
        rows.append({
            'factor_id': item.get('factor_id'),
            'delta_sharpe': float(lift.get('sharpe_lift', 0.0) or 0.0),
            'delta_total_return': float((item.get('combo_metrics') or {}).get('total_return', 0.0) or 0.0) - float((item.get('base_metrics') or {}).get('total_return', 0.0) or 0.0),
            'delta_max_drawdown': float(lift.get('drawdown_delta', 0.0) or 0.0),
            'negative_contribution': bool(float(lift.get('sharpe_lift', 0.0) or 0.0) < 0),
        })
    return rows


def collect_audit_dates(audit_report: dict[str, Any]) -> list[str]:
    dates: set[str] = set()
    for item in audit_report.get('reports', []) or []:
        for split in item.get('split_reports', []) or []:
            for key in ('train_start', 'train_end', 'test_end'):
                if split.get(key):
                    dates.add(str(split.get(key)))
    return sorted(dates)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run heuristic regime weight search')
    parser.add_argument('--root', default='/Users/fisher/.openclaw/workspace/factor_factory', help='factor_factory root')
    parser.add_argument(
        '--audit-report',
        default='',
        help='Optional explicit audit report path; defaults to outputs/audit_reports/factor_audit_phase1[.<suffix>].json',
    )
    parser.add_argument(
        '--output-suffix',
        default='',
        help='Optional suffix for alternate label-mode artifacts, e.g. open_to_close_cmp',
    )
    parser.add_argument(
        '--output-path',
        default='',
        help='Optional explicit output path for the generated weight map JSON',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    phase1 = load_yaml(root / 'config' / 'phase1.yaml')
    output_suffix = str(args.output_suffix or '').strip()

    if args.audit_report:
        audit_path = Path(args.audit_report).expanduser().resolve()
    elif output_suffix:
        audit_path = root / 'outputs' / 'audit_reports' / f'factor_audit_phase1.{output_suffix}.json'
    else:
        audit_path = root / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json'
    if not audit_path.exists():
        raise FileNotFoundError(f'audit report not found: {audit_path}')

    base_weight = float(phase1['base_factor']['fixed_weight'])
    catalog = json.loads((root / 'outputs' / 'candidate_lists' / 'phase1_candidate_catalog.json').read_text())
    audit_report = json.loads(audit_path.read_text())
    regime_summary = json.loads((root / 'outputs' / 'regime_maps' / 'regime_label_phase1.json').read_text())

    family_map = {item['id']: item.get('family', 'unknown') for item in catalog.get('candidate_factors', [])}
    reports = audit_report.get('reports', [])
    whitelist = [dict(item, family=family_map.get(item['factor_id'], 'unknown'), selection_source='whitelist') for item in reports if item.get('status') == 'whitelist']
    support_pool = sorted(
        [dict(item, family=family_map.get(item['factor_id'], 'unknown'), selection_source='support_pool') for item in reports if item.get('support_pool_candidate')],
        key=factor_score,
        reverse=True,
    )
    cold_ranked = sorted(
        [dict(item, family=family_map.get(item['factor_id'], 'unknown'), selection_source='fallback_coldlist') for item in reports if item.get('status') != 'whitelist'],
        key=factor_score,
        reverse=True,
    )
    fallback_pool: list[dict[str, Any]] = []
    candidate_map: dict[str, dict[str, Any]] = {}
    # Phase1 quality gate: support_pool 仅允许 research_preview，不参与正式 release 晋升；coldlist 禁止 fallback。
    formal_pool = whitelist
    research_pool = support_pool if whitelist else []
    for item in formal_pool + research_pool:
        candidate_map[item['factor_id']] = item
    all_candidates = list(candidate_map.values())

    weight_map_rows = []
    global_mode = 'whitelist' if whitelist else 'hold_no_qualified_factors'
    if not whitelist:
        output = {
            'status': 'ok',
            'base_factor_id': phase1['base_factor']['id'],
            'base_weight': base_weight,
            'audit_context': {
                'label_mode': audit_report.get('label_mode'),
                'label_column': audit_report.get('label_column'),
                'benchmark_eligible_trade_days': audit_report.get('benchmark_eligible_trade_days'),
                'output_suffix': audit_report.get('output_suffix'),
                'audit_report_path': str(audit_path),
            },
            'selection_mode': 'hold_no_qualified_factors',
            'decision': 'hold_no_qualified_factors',
            'whitelist_count': 0,
            'support_pool_count': len(support_pool),
            'fallback_pool_count': 0,
            'latest_regime': regime_summary.get('latest', {}) or {},
            'latest_weight_plan': None,
            'regime_weight_map': [],
            'promotion_gate': {
                'qualified_factor_count': 0,
                'mc_sharpe_p05': None,
                'wf_better_sharpe_ratio': None,
                'independent_oos_days': 0,
                'holdout_days': 0,
                'test_set_used_for_selection': False,
                'net_return': None,
                'passed': False,
                'decision': 'hold_no_qualified_factors',
                'reason': 'whitelist_empty_coldlist_fallback_disabled',
            },
        }
        if args.output_path:
            out_path = Path(args.output_path).expanduser().resolve()
        elif output_suffix:
            out_path = root / 'outputs' / 'weight_maps' / f'regime_weight_map_phase1.{output_suffix}.json'
        else:
            out_path = root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'status':'ok','output_path':str(out_path),'selection_mode':'hold_no_qualified_factors','audit_report_path':str(audit_path)}, ensure_ascii=False, indent=2))
        return 0
    for regime_id in regime_summary.get('regime_ids', []):
        preferred = PREFERRED_FAMILIES.get(regime_id, [])
        preferred_set = set(preferred)
        chosen = select_regime_candidates(all_candidates, regime_id, preferred_set, max_factors=3)

        if whitelist:
            if any(item.get('selection_source') == 'fallback_coldlist' for item in chosen):
                selection_mode = 'whitelist_blend'
                global_mode = 'whitelist_blend'
            elif any(item.get('selection_source') == 'support_pool' for item in chosen):
                selection_mode = 'whitelist_blend'
                if global_mode == 'whitelist':
                    global_mode = 'whitelist_blend'
            else:
                selection_mode = 'whitelist'
        else:
            selection_mode = 'fallback_coldlist'

        aux_budget = min(0.60, max(0.0, REGIME_AUX_BUDGET.get(regime_id, 0.30)))
        if not chosen:
            aux_budget = 0.0
        preferred_selected = [item for item in chosen if item.get('family') in preferred_set]
        nonpreferred_selected = [item for item in chosen if item.get('family') not in preferred_set]
        weight_map = {}
        if preferred_selected and nonpreferred_selected:
            preferred_budget = aux_budget * 0.75
            nonpreferred_budget = aux_budget - preferred_budget
        elif preferred_selected:
            preferred_budget = aux_budget
            nonpreferred_budget = 0.0
        else:
            preferred_budget = 0.0
            nonpreferred_budget = aux_budget

        if preferred_selected:
            preferred_scores = [math.log1p(max(1e-6, factor_score(item))) for item in preferred_selected]
            for item, weight in zip(preferred_selected, softmax(preferred_scores)):
                weight_map[item['factor_id']] = weight * preferred_budget
        if nonpreferred_selected:
            nonpreferred_scores = [math.log1p(max(1e-6, factor_score(item))) for item in nonpreferred_selected]
            for item, weight in zip(nonpreferred_selected, softmax(nonpreferred_scores)):
                weight_map[item['factor_id']] = weight * nonpreferred_budget

        selected = []
        for item in chosen:
            weight = weight_map.get(item['factor_id'], 0.0)
            selected.append({
                'factor_id': item['factor_id'],
                'family': item.get('family', 'unknown'),
                'selection_source': item.get('selection_source'),
                'score': round(factor_score(item), 6),
                'weight': round(weight, 6),
                'rank_ic_abs': round(float(item.get('rank_ic_abs', abs(item.get('rank_ic_mean', 0.0)) or 0.0)), 6),
                'sharpe_lift': round(float((item.get('lift') or {}).get('sharpe_lift', 0.0) or 0.0), 6),
                'calmar_lift': round(float(item.get('calmar_lift', 0.0) or 0.0), 6),
            })

        weight_map_rows.append({
            'regime_id': regime_id,
            'base_weight': round(base_weight, 6),
            'aux_weight_budget': round(aux_budget, 6),
            'cash_buffer': round(max(0.0, 1.0 - base_weight - aux_budget), 6),
            'selected_factors': selected,
            'selection_mode': selection_mode,
            'preferred_families': preferred,
        })

    latest_regime = regime_summary.get('latest', {}) or {}
    latest_weight = next((row for row in weight_map_rows if row['regime_id'] == latest_regime.get('regime_id')), None)
    latest_items = []
    if latest_weight:
        ids = {item.get('factor_id') for item in latest_weight.get('selected_factors', [])}
        latest_items = [item for item in all_candidates if item.get('factor_id') in ids]
    tvt_policy = split_train_validation_test_dates(collect_audit_dates(audit_report))
    combo_bootstrap = combo_block_bootstrap(extract_latest_daily_returns(latest_items))
    marginal_contribution = marginal_contribution_from_reports(latest_items)
    negative_contrib = [row['factor_id'] for row in marginal_contribution if row.get('negative_contribution')]
    promotion_passed = bool(
        len(whitelist) >= 2
        and combo_bootstrap.get('passed')
        and tvt_policy.get('promotion_allowed')
        and not negative_contrib
    )
    output = {
        'status': 'ok',
        'base_factor_id': phase1['base_factor']['id'],
        'base_weight': base_weight,
        'audit_context': {
            'label_mode': audit_report.get('label_mode'),
            'label_column': audit_report.get('label_column'),
            'benchmark_eligible_trade_days': audit_report.get('benchmark_eligible_trade_days'),
            'output_suffix': audit_report.get('output_suffix'),
            'audit_report_path': str(audit_path),
        },
        'selection_mode': global_mode,
        'whitelist_count': len(whitelist),
        'support_pool_count': len(support_pool),
        'fallback_pool_count': len(fallback_pool),
        'latest_regime': latest_regime,
        'latest_weight_plan': latest_weight,
        'regime_weight_map': weight_map_rows,
        'train_validation_test_policy': tvt_policy,
        'bootstrap_summary': combo_bootstrap,
        'marginal_contribution': marginal_contribution,
        'promotion_gate': {
            'qualified_factor_count': len(whitelist),
            'combo_mc_sharpe_p05': combo_bootstrap.get('combo_mc_sharpe_p05'),
            'wf_better_sharpe_ratio': min([float((item.get('split_summary') or {}).get('better_sharpe_ratio', 0.0) or 0.0) for item in latest_items], default=None),
            'independent_oos_days': 0,
            'holdout_days': 0,
            'total_independent_days': 0,
            'test_set_used_for_selection': False,
            'net_return': None,
            'negative_contribution_factors': negative_contrib,
            'passed': promotion_passed,
            'decision': 'promote_candidate' if promotion_passed else 'hold_research_only',
            'reason': 'phase3_combo_proxy_gate; independent_oos_holdout_gate_pending_external_report',
        },
    }

    if args.output_path:
        out_path = Path(args.output_path).expanduser().resolve()
    elif output_suffix:
        out_path = root / 'outputs' / 'weight_maps' / f'regime_weight_map_phase1.{output_suffix}.json'
    else:
        out_path = root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({
        'status': 'ok',
        'output_path': str(out_path),
        'audit_report_path': str(audit_path),
        'selection_mode': output['selection_mode'],
        'regime_count': len(weight_map_rows),
        'label_mode': output['audit_context'].get('label_mode'),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
