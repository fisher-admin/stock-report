#!/usr/bin/env python3
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EV = ROOT / 'outputs' / 'evolution'
GREEN = ROOT / 'outputs' / 'greenfield_multifactor'
TZ = ZoneInfo('Asia/Shanghai')

def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def mt(path: Path):
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None

def main() -> int:
    now = datetime.now(TZ).isoformat(timespec='seconds')
    pool = read(EV/'factor_iteration_pool.json')
    lineage = read(EV/'factor_lineage.json')
    snapshot = read(GREEN/'factor_pool_snapshot.json')
    leaders = read(GREEN/'current_factor_leaders.json')
    validation = read(GREEN/'validation_summary.json')
    portfolio = read(GREEN/'portfolio_spec.json')
    factor_board = pool.get('factor_board') or pool.get('factors') or []
    selected = portfolio.get('selected_factors') or leaders.get('top_factors') or []
    selected_ids = [str(x.get('factor_id')) for x in selected if isinstance(x, dict) and x.get('factor_id')]
    board_ids = [str(x.get('factor_id')) for x in factor_board if isinstance(x, dict) and x.get('factor_id')]
    delta = {
        'report_type': 'stage74_factor_iteration_pool_delta',
        'generated_at': now,
        'round_name': 'stage74_20260501_factor_iteration_lineage_refresh',
        'source_paths': {
            'factor_iteration_pool': str(EV/'factor_iteration_pool.json'),
            'factor_lineage': str(EV/'factor_lineage.json'),
            'factor_pool_snapshot': str(GREEN/'factor_pool_snapshot.json'),
            'current_factor_leaders': str(GREEN/'current_factor_leaders.json'),
            'greenfield_validation_summary': str(GREEN/'validation_summary.json'),
            'greenfield_portfolio_spec': str(GREEN/'portfolio_spec.json'),
        },
        'source_mtimes': {
            'factor_iteration_pool': mt(EV/'factor_iteration_pool.json'),
            'factor_lineage': mt(EV/'factor_lineage.json'),
            'factor_pool_snapshot': mt(GREEN/'factor_pool_snapshot.json'),
            'current_factor_leaders': mt(GREEN/'current_factor_leaders.json'),
            'greenfield_validation_summary': mt(GREEN/'validation_summary.json'),
            'greenfield_portfolio_spec': mt(GREEN/'portfolio_spec.json'),
        },
        'pool_summary': pool.get('pool_summary') or {},
        'factor_board_count': len(factor_board),
        'factor_board_ids_sample': board_ids[:30],
        'selected_factor_ids': selected_ids,
        'greenfield_mainline': validation.get('mainline'),
        'greenfield_decision': validation.get('decision') or validation.get('verdict'),
        'lineage_summary': {
            'top_level_keys': sorted(lineage.keys())[:40] if isinstance(lineage, dict) else [],
            'generated_at': lineage.get('generated_at') if isinstance(lineage, dict) else None,
        },
        'stage74_action': 'refresh_factor_iteration_pool_and_lineage_delta_before_combo_revalidation',
    }
    lineage_delta = {
        'report_type': 'stage74_factor_lineage_delta',
        'generated_at': now,
        'round_name': delta['round_name'],
        'source_paths': delta['source_paths'],
        'source_mtimes': delta['source_mtimes'],
        'selected_factor_ids': selected_ids,
        'lineage_prior_keys': delta['lineage_summary']['top_level_keys'],
        'closure_requirement': 'must be followed by stage74 combo replay/portfolio/nested/walkforward/independent_oos and recorder sync',
    }
    dump(EV/'stage74_factor_iteration_pool_delta.json', delta)
    dump(EV/'stage74_factor_lineage_delta.json', lineage_delta)
    print(json.dumps({'status':'ok','pool_delta':str(EV/'stage74_factor_iteration_pool_delta.json'),'lineage_delta':str(EV/'stage74_factor_lineage_delta.json'),'selected_factor_ids':selected_ids}, ensure_ascii=False, indent=2))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
