#!/usr/bin/env python3
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EV = ROOT / 'outputs' / 'evolution'
WM = ROOT / 'outputs' / 'weight_maps'
GREEN = ROOT / 'outputs' / 'greenfield_multifactor'
TZ = ZoneInfo('Asia/Shanghai')

def read(p: Path) -> Any:
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}

def dump(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def mt(p: Path) -> str | None:
    return datetime.fromtimestamp(p.stat().st_mtime, TZ).isoformat(timespec='seconds') if p.exists() else None

def main() -> int:
    now = datetime.now(TZ).isoformat(timespec='seconds')
    pool = read(EV/'factor_iteration_pool.json')
    lineage = read(EV/'factor_lineage.json')
    w = read(WM/'regime_weight_map_phase1.json')
    leaders = read(GREEN/'current_factor_leaders.json')
    snapshot = read(GREEN/'factor_pool_snapshot.json')
    latest_plan = w.get('latest_weight_plan') or {}
    selected = latest_plan.get('selected_factors') or []
    green_backtestable = leaders.get('backtestable_factors') or {}
    green_ids = list(green_backtestable.keys())
    current_combo_ids = [x.get('factor_id') for x in selected if isinstance(x, dict)]
    payload = {
        'report_type': 'stage75_factor_iteration_pool_delta',
        'generated_at': now,
        'round_name': 'stage75_20260501_factor_weight_oos_closure',
        'source_paths': {
            'factor_iteration_pool': str(EV/'factor_iteration_pool.json'),
            'factor_lineage': str(EV/'factor_lineage.json'),
            'regime_weight_map_phase1': str(WM/'regime_weight_map_phase1.json'),
            'greenfield_current_factor_leaders': str(GREEN/'current_factor_leaders.json'),
            'greenfield_factor_pool_snapshot': str(GREEN/'factor_pool_snapshot.json'),
        },
        'source_mtimes': {
            'factor_iteration_pool': mt(EV/'factor_iteration_pool.json'),
            'factor_lineage': mt(EV/'factor_lineage.json'),
            'regime_weight_map_phase1': mt(WM/'regime_weight_map_phase1.json'),
            'greenfield_current_factor_leaders': mt(GREEN/'current_factor_leaders.json'),
            'greenfield_factor_pool_snapshot': mt(GREEN/'factor_pool_snapshot.json'),
        },
        'canonical_current_combo_weight': {
            'latest_regime': w.get('latest_regime'),
            'latest_weight_plan': latest_plan,
            'factor_ids': current_combo_ids,
            'base_factor_id': w.get('base_factor_id'),
            'base_weight': w.get('base_weight'),
            'selection_mode': w.get('selection_mode'),
        },
        'greenfield_pool_extension': {
            'trade_date': leaders.get('trade_date'),
            'backtestable_factor_count': len(green_ids),
            'backtestable_factor_ids': green_ids,
            'snapshot_meta': {
                'panel_path': snapshot.get('panel_path'),
                'latest_trade_date_snapshot': snapshot.get('latest_trade_date_snapshot'),
                'current_only_quality_meta': snapshot.get('current_only_quality_meta'),
            },
        },
        'pool_summary': pool.get('pool_summary') or {},
        'stage75_action': 'explicitly materialize current combo weight evidence plus greenfield factor pool extension before combo validation',
    }
    lineage_delta = {
        'report_type': 'stage75_factor_lineage_delta',
        'generated_at': now,
        'round_name': payload['round_name'],
        'source_paths': payload['source_paths'],
        'source_mtimes': payload['source_mtimes'],
        'canonical_current_combo_factor_ids': current_combo_ids,
        'greenfield_extension_factor_ids': green_ids,
        'prior_lineage_keys': sorted(lineage.keys())[:60] if isinstance(lineage, dict) else [],
        'stage75_action': 'lineage extends stage74 by pinning canonical current_combo weights and greenfield candidate pool context',
    }
    weight_evidence = {
        'report_type': 'stage75_combo_weight_evidence',
        'generated_at': now,
        'round_name': payload['round_name'],
        'weight_source_path': str(WM/'regime_weight_map_phase1.json'),
        'weight_source_mtime': mt(WM/'regime_weight_map_phase1.json'),
        'latest_regime': w.get('latest_regime'),
        'latest_weight_plan': latest_plan,
        'selected_factor_ids': current_combo_ids,
        'note': 'This is an explicit stage75 weight artifact; current combo validation scripts consume the canonical weight map unchanged.',
    }
    dump(EV/'stage75_factor_iteration_pool_delta.json', payload)
    dump(EV/'stage75_factor_lineage_delta.json', lineage_delta)
    dump(EV/'stage75_combo_weight_evidence.json', weight_evidence)
    print(json.dumps({'status':'ok','pool_delta':str(EV/'stage75_factor_iteration_pool_delta.json'),'lineage_delta':str(EV/'stage75_factor_lineage_delta.json'),'weight_evidence':str(EV/'stage75_combo_weight_evidence.json'),'selected_factor_ids':current_combo_ids}, ensure_ascii=False, indent=2))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
