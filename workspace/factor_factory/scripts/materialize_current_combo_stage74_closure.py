#!/usr/bin/env python3
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EV = ROOT / 'outputs' / 'evolution'
TZ = ZoneInfo('Asia/Shanghai')

def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def dump(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def mt(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None

def val(d: dict[str, Any], *keys, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
    return cur if cur is not None else default

def pct(x: Any) -> str:
    try: return f'{float(x):.4%}'
    except Exception: return 'NA'

def main() -> int:
    now = datetime.now(TZ).isoformat(timespec='seconds')
    paths = {
        'checkpoint_started': EV/'stage74_checkpoint_started.json',
        'checkpoint_greenfield_running': EV/'stage74_checkpoint_greenfield_running.json',
        'factor_iteration_pool_delta': EV/'stage74_factor_iteration_pool_delta.json',
        'factor_lineage_delta': EV/'stage74_factor_lineage_delta.json',
        'replay': EV/'stage74_current_combo_replay.json',
        'portfolio': EV/'stage74_current_combo_portfolio.json',
        'nested': EV/'stage74_current_combo_nested.json',
        'walkforward': EV/'stage74_current_combo_walkforward.json',
        'independent_oos_holdout': EV/'stage74_current_combo_independent_oos_holdout.json',
        'qlib_recorder_sync_latest': EV/'qlib_recorder_sync_latest.json',
        'stage74_qlib_recorder_sync': EV/'stage74_qlib_recorder_sync.json',
        'factor_promotion_decision': EV/'factor_promotion_decision.json',
        'factor_validation_report': EV/'factor_validation_report.json',
        'portfolio_backtest_report': EV/'portfolio_backtest_report.json',
        'overfit_audit_report': EV/'overfit_audit_report.json',
        'factor_iteration_pool': EV/'factor_iteration_pool.json',
        'factor_lineage': EV/'factor_lineage.json',
        'current_market_fingerprint': EV/'current_market_fingerprint.json',
        'regime_analog_windows': EV/'regime_analog_windows.json',
    }
    replay = read(paths['replay']); portfolio = read(paths['portfolio']); nested = read(paths['nested']); walkforward = read(paths['walkforward'])
    oos = read(paths['independent_oos_holdout']); qlib = read(paths['qlib_recorder_sync_latest'])
    pool_delta = read(paths['factor_iteration_pool_delta']); lineage_delta = read(paths['factor_lineage_delta'])
    independent = val(oos, 'evaluation_partitions','independent_oos','by_label_mode','close_to_close', default={})
    holdout = val(oos, 'evaluation_partitions','holdout_validation','by_label_mode','close_to_close', default={})
    recorder = qlib.get('qlib_recorder') or {}
    signal = {
        'generated_at': now,
        'stage': 'stage74',
        'decision': 'release_continue_after_stage74_factor_iteration_lineage_refresh',
        'promotion_freeze_lifted': bool(val(oos, 'verdict','sufficient_to_lift_promotion_freeze', default=False)),
        'selected_factor_ids_from_pool_delta': pool_delta.get('selected_factor_ids', []),
        'metrics': {
            'portfolio_primary_combo_total_return': portfolio.get('primary_combo_total_return'),
            'nested_primary_excess_total_return': nested.get('primary_excess_total_return'),
            'walkforward_primary_excess_total_return': walkforward.get('primary_excess_total_return'),
            'independent_oos_total_return': val(independent, 'combo_metrics','total_return'),
            'independent_oos_excess_total_return': independent.get('excess_total_return'),
            'holdout_total_return': val(holdout, 'combo_metrics','total_return'),
            'holdout_excess_total_return': holdout.get('excess_total_return'),
        },
        'qlib_recorder': recorder,
    }
    for key in ['factor_promotion_decision','factor_validation_report','portfolio_backtest_report','overfit_audit_report','factor_iteration_pool','factor_lineage']:
        payload = read(paths[key])
        payload['generated_at'] = now
        payload['stage74_closure_signal'] = signal
        payload.setdefault('source_artifacts', {})['stage74_factor_iteration_pool_delta'] = str(paths['factor_iteration_pool_delta'])
        payload['source_artifacts']['stage74_factor_lineage_delta'] = str(paths['factor_lineage_delta'])
        payload['source_artifacts']['stage74_independent_oos_holdout'] = str(paths['independent_oos_holdout'])
        payload['source_artifacts']['stage74_qlib_recorder_sync_latest'] = str(paths['qlib_recorder_sync_latest'])
        if key == 'factor_promotion_decision':
            gc = payload.setdefault('global_constraints', {})
            gc['promotion_freeze'] = not signal['promotion_freeze_lifted']
            gc['stage74_factor_iteration_lineage_refresh_completed'] = True
            gc['stage74_independent_oos_holdout_completed'] = True
            gc['stage74_qlib_recorder_completed'] = bool(recorder.get('recorder_id'))
            payload['round_decision'] = signal['decision']
        dump(paths[key], payload)
    control = {
        'report_type': 'current_combo_stage74_control_sync',
        'generated_at': now,
        'decision': signal['decision'],
        'promotion_freeze_lifted': signal['promotion_freeze_lifted'],
        'stage74_signal': signal,
        'source_paths': {k: str(v) for k, v in paths.items()},
        'source_mtimes': {k: mt(v) for k, v in paths.items()},
        'closure_checks': {
            'factor_iteration_pool_delta_exists': paths['factor_iteration_pool_delta'].exists(),
            'factor_lineage_delta_exists': paths['factor_lineage_delta'].exists(),
            'stage74_replay_exists': paths['replay'].exists(),
            'stage74_portfolio_exists': paths['portfolio'].exists(),
            'stage74_nested_exists': paths['nested'].exists(),
            'stage74_walkforward_exists': paths['walkforward'].exists(),
            'stage74_independent_oos_holdout_exists': paths['independent_oos_holdout'].exists(),
            'qlib_recorder_id': recorder.get('recorder_id'),
        },
        'summary': f"Stage74 closed factor pool/lineage delta + combo validation + qlib recorder={recorder.get('recorder_id')}; independent OOS {pct(signal['metrics']['independent_oos_total_return'])}/excess {pct(signal['metrics']['independent_oos_excess_total_return'])}; holdout {pct(signal['metrics']['holdout_total_return'])}/excess {pct(signal['metrics']['holdout_excess_total_return'])}.",
    }
    out = EV/'current_combo_stage74_control_sync.json'
    dump(out, control)
    print(json.dumps({'status':'ok','control_sync':str(out),'recorder_id':recorder.get('recorder_id'),'promotion_freeze_lifted':signal['promotion_freeze_lifted']}, ensure_ascii=False, indent=2))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
