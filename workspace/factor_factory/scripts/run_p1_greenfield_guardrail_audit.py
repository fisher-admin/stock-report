#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
GF = ROOT / 'outputs' / 'greenfield_multifactor'
EV = ROOT / 'outputs' / 'evolution'
TZ = ZoneInfo('Asia/Shanghai')


def load(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def sf(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def selected_direction_stats(items):
    counts = Counter()
    factor_dirs = defaultdict(Counter)
    factor_weights = defaultdict(list)
    factor_fold_count = Counter()
    for item in items:
        selected = item.get('selected_factors') or []
        for s in selected:
            fid = str(s.get('factor_id'))
            direction_label = str(s.get('direction_label') or ('short' if sf(s.get('actual_weight')) < 0 else 'long'))
            counts[direction_label] += 1
            factor_dirs[fid][direction_label] += 1
            if s.get('actual_weight') is not None:
                factor_weights[fid].append(sf(s.get('actual_weight')))
            factor_fold_count[fid] += 1
    out = {
        'direction_counts': dict(counts),
        'factor_direction_counts': {k: dict(v) for k, v in sorted(factor_dirs.items())},
        'factor_avg_weight': {k: mean(v) for k, v in factor_weights.items() if v},
        'factor_fold_count': dict(factor_fold_count),
    }
    instability = {}
    for fid, c in factor_dirs.items():
        if len(c) > 1:
            instability[fid] = dict(c)
    out['direction_instability_by_factor'] = instability
    return out


def fold_concentration(folds):
    rows = []
    bad = []
    for fold in folds:
        fid_count = int((fold.get('selection_context') or {}).get('selected_factor_count') or len(fold.get('selected_factors') or []))
        family_count = int((fold.get('selection_context') or {}).get('selected_family_count') or len({s.get('family') for s in fold.get('selected_factors') or []}))
        weights = {str(k): abs(sf(v)) for k, v in (fold.get('weights') or {}).items()}
        max_abs = max(weights.values()) if weights else 0.0
        row = {
            'fold_id': fold.get('fold_id'),
            'test_start': fold.get('test_start'),
            'test_end': fold.get('test_end'),
            'selected_factor_count': fid_count,
            'selected_family_count': family_count,
            'max_abs_weight': max_abs,
            'c2c_total_return': sf(((fold.get('summary') or {}).get('close_to_close') or {}).get('total_return')),
            'c2c_excess_total_return': sf(((fold.get('summary') or {}).get('excess') or {}).get('close_to_close_total_return')),
        }
        rows.append(row)
        if fid_count < 3 or family_count < 2 or max_abs > 0.70:
            bad.append(row)
    return {
        'folds': rows,
        'bad_folds': bad,
        'bad_fold_ratio': len(bad) / max(1, len(rows)),
        'min_selected_factor_count': min([r['selected_factor_count'] for r in rows], default=0),
        'min_selected_family_count': min([r['selected_family_count'] for r in rows], default=0),
        'max_single_fold_abs_weight': max([r['max_abs_weight'] for r in rows], default=0.0),
    }


def main() -> int:
    validation = load(GF / 'validation_summary.json')
    walk = load(GF / 'walkforward_report.json')
    analog = load(GF / 'analog_validation.json')
    portfolio = load(GF / 'portfolio_spec.json')
    folds = walk.get('folds') or []
    windows = analog.get('windows') or []

    weights = {str(k): sf(v) for k, v in (portfolio.get('weights') or {}).items()}
    abs_total = sum(abs(v) for v in weights.values()) or 1.0
    negative_share = sum(abs(v) for v in weights.values() if v < 0) / abs_total
    positive_share = sum(abs(v) for v in weights.values() if v > 0) / abs_total

    walk_agg = walk.get('aggregate') or {}
    analog_agg = analog.get('aggregate') or {}
    c2c = walk_agg.get('close_to_close') or {}
    c2c_base = walk_agg.get('baseline_close_to_close') or {}
    excess = walk_agg.get('excess') or {}
    analog_c2c = analog_agg.get('close_to_close') or {}

    dir_fold = selected_direction_stats(folds)
    dir_window = selected_direction_stats(windows)
    conc = fold_concentration(folds)

    guardrails = {
        'short_exposure_cap': {
            'pass': negative_share <= 0.80,
            'negative_weight_share': negative_share,
            'positive_weight_share': positive_share,
            'threshold': 0.80,
        },
        'direction_stability': {
            'pass': len(dir_fold['direction_instability_by_factor']) == 0,
            'fold_direction_stats': dir_fold,
            'analog_direction_stats': dir_window,
        },
        'fold_concentration': {
            'pass': conc['bad_fold_ratio'] <= 0.25,
            **conc,
        },
        'return_plausibility': {
            'pass': sf(c2c.get('total_return')) <= 20.0,
            'c2c_total_return': sf(c2c.get('total_return')),
            'c2c_excess_total_return': sf(excess.get('close_to_close_total_return')),
            'threshold_total_return': 20.0,
        },
        'walkforward_absolute_and_excess_c2c': {
            'pass': sf(c2c.get('total_return')) > 0 and sf(excess.get('close_to_close_total_return')) > 0,
            'c2c_total_return': sf(c2c.get('total_return')),
            'baseline_c2c_total_return': sf(c2c_base.get('total_return')),
            'c2c_excess_total_return': sf(excess.get('close_to_close_total_return')),
        },
        'analog_c2c_support': {
            'pass': sf(analog_c2c.get('total_return')) > 0 and sf(analog_c2c.get('sharpe')) > 0,
            'analog_c2c_total_return': sf(analog_c2c.get('total_return')),
            'analog_c2c_sharpe': sf(analog_c2c.get('sharpe')),
            'analog_days': int(analog_c2c.get('days') or 0),
        },
    }
    failed = [k for k, v in guardrails.items() if not bool(v.get('pass'))]
    decision = 'block_promotion_guardrail_failed' if failed else 'guardrail_pass_research_candidate'
    payload = {
        'schema_version': '2026-05-02.p1_greenfield_guardrail_audit.v1',
        'generated_at': iso_now(),
        'status': 'ok',
        'decision': decision,
        'promotion_freeze_lifted': False,
        'failed_guardrails': failed,
        'source_paths': {
            'validation_summary': str(GF / 'validation_summary.json'),
            'walkforward_report': str(GF / 'walkforward_report.json'),
            'analog_validation': str(GF / 'analog_validation.json'),
            'portfolio_spec': str(GF / 'portfolio_spec.json'),
        },
        'scenario_meta': (validation.get('run_context') or {}).get('scenario_meta') or {},
        'current_portfolio': {
            'trade_date': portfolio.get('trade_date'),
            'current_regime_id': portfolio.get('current_regime_id'),
            'weights': weights,
            'negative_weight_share': negative_share,
            'selected_factor_count': (portfolio.get('selection_context') or {}).get('selected_factor_count'),
            'selected_family_count': (portfolio.get('selection_context') or {}).get('selected_family_count'),
        },
        'guardrails': guardrails,
    }
    out = EV / 'p1_greenfield_guardrail_audit.json'
    dump(out, payload)
    md = EV / 'p1_greenfield_guardrail_audit.md'
    lines = [
        '# P1 Greenfield Guardrail Audit', '',
        f"- generated_at: {payload['generated_at']}",
        f"- decision: `{decision}`",
        f"- promotion_freeze_lifted: `{payload['promotion_freeze_lifted']}`",
        f"- failed_guardrails: `{failed}`", '',
        '## Current portfolio', '',
        f"- negative_weight_share: `{negative_share}`",
        f"- weights: `{weights}`", '',
        '## Guardrails', '',
    ]
    for k, v in guardrails.items():
        lines.append(f"- {k}: pass=`{v.get('pass')}`")
    md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'ok', 'decision': decision, 'failed_guardrails': failed, 'json': str(out), 'md': str(md)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
