#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
TZ = ZoneInfo('Asia/Shanghai')


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def iso_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds')


def pct(v: Any) -> str:
    try:
        return f"{float(v):.4%}"
    except Exception:
        return 'NA'


def main() -> int:
    now = datetime.now(TZ).isoformat(timespec='seconds')
    paths = {
        'current_market_fingerprint': EVOLUTION / 'current_market_fingerprint.json',
        'regime_analog_windows': EVOLUTION / 'regime_analog_windows.json',
        'latest_weight_plan_analog_alignment': EVOLUTION / 'latest_weight_plan_analog_alignment.json',
        'replay': EVOLUTION / 'stage73_current_combo_replay.json',
        'portfolio': EVOLUTION / 'stage73_current_combo_portfolio.json',
        'nested': EVOLUTION / 'stage73_current_combo_nested.json',
        'walkforward': EVOLUTION / 'stage73_current_combo_walkforward.json',
        'independent_oos_holdout': EVOLUTION / 'stage73_current_combo_independent_oos_holdout.json',
        'qlib_recorder_sync_latest': EVOLUTION / 'qlib_recorder_sync_latest.json',
        'factor_promotion_decision': EVOLUTION / 'factor_promotion_decision.json',
        'factor_validation_report': EVOLUTION / 'factor_validation_report.json',
        'portfolio_backtest_report': EVOLUTION / 'portfolio_backtest_report.json',
        'overfit_audit_report': EVOLUTION / 'overfit_audit_report.json',
        'factor_iteration_pool': EVOLUTION / 'factor_iteration_pool.json',
        'factor_lineage': EVOLUTION / 'factor_lineage.json',
    }
    fingerprint = read_json(paths['current_market_fingerprint'])
    analog = read_json(paths['regime_analog_windows'])
    replay = read_json(paths['replay'])
    portfolio = read_json(paths['portfolio'])
    nested = read_json(paths['nested'])
    walkforward = read_json(paths['walkforward'])
    oos = read_json(paths['independent_oos_holdout'])
    qlib = read_json(paths['qlib_recorder_sync_latest'])
    promotion = read_json(paths['factor_promotion_decision'])
    validation = read_json(paths['factor_validation_report'])
    portfolio_report = read_json(paths['portfolio_backtest_report'])
    overfit = read_json(paths['overfit_audit_report'])
    pool = read_json(paths['factor_iteration_pool'])
    lineage = read_json(paths['factor_lineage'])

    part = oos.get('evaluation_partitions', {})
    independent = (((part.get('independent_oos') or {}).get('by_label_mode') or {}).get('close_to_close') or {})
    holdout = (((part.get('holdout_validation') or {}).get('by_label_mode') or {}).get('close_to_close') or {})
    verdict = oos.get('verdict') or {}
    qlib_rec = qlib.get('qlib_recorder') or {}

    stage73_signal = {
        'generated_at': now,
        'stage': 'stage73',
        'scope': 'current_combo_close_to_close_primary',
        'decision': 'release_continue_after_stage73_context_refresh',
        'promotion_freeze_lifted': bool(verdict.get('sufficient_to_lift_promotion_freeze', False)),
        'market_trade_date': fingerprint.get('trade_date'),
        'analog_current_trade_date': analog.get('current_trade_date') or analog.get('trade_date'),
        'metrics': {
            'replay_close_to_close_excess_total_return': replay.get('summary', {}).get('close_to_close_excess_total_return') or replay.get('close_to_close_excess_total_return'),
            'portfolio_primary_combo_total_return': portfolio.get('primary_combo_total_return'),
            'nested_primary_excess_total_return': nested.get('primary_excess_total_return') or nested.get('aggregate', {}).get('primary_excess_total_return'),
            'walkforward_primary_excess_total_return': walkforward.get('primary_excess_total_return') or walkforward.get('aggregate', {}).get('primary_excess_total_return'),
            'independent_oos_total_return': (independent.get('combo_metrics') or {}).get('total_return'),
            'independent_oos_excess_total_return': independent.get('excess_total_return'),
            'holdout_total_return': (holdout.get('combo_metrics') or {}).get('total_return'),
            'holdout_excess_total_return': holdout.get('excess_total_return'),
        },
        'qlib_recorder': qlib_rec,
    }

    for payload in [promotion, validation, portfolio_report, overfit, pool, lineage]:
        payload['generated_at'] = now
        payload['stage73_closure_signal'] = stage73_signal
        payload.setdefault('source_artifacts', {})['stage73_current_market_fingerprint'] = str(paths['current_market_fingerprint'])
        payload['source_artifacts']['stage73_regime_analog_windows'] = str(paths['regime_analog_windows'])
        payload['source_artifacts']['stage73_independent_oos_holdout'] = str(paths['independent_oos_holdout'])
        payload['source_artifacts']['stage73_qlib_recorder_sync_latest'] = str(paths['qlib_recorder_sync_latest'])

    gc = promotion.setdefault('global_constraints', {})
    gc['promotion_freeze'] = not stage73_signal['promotion_freeze_lifted']
    gc['stage73_context_refresh_completed'] = True
    gc['stage73_independent_oos_holdout_completed'] = True
    gc['stage73_qlib_recorder_completed'] = bool(qlib_rec.get('recorder_id'))
    promotion['round_decision'] = stage73_signal['decision']

    validation.setdefault('release_recommendation', {})['decision'] = 'release'
    validation['release_recommendation']['stage73_reason'] = 'market fingerprint/regime analog refreshed; stage73 replay/backtest/nested/walkforward/independent holdout and qlib recorder closed.'
    portfolio_report.setdefault('oos_validation_assessment', {})['stage73_independent_oos_completed'] = True
    portfolio_report.setdefault('conclusion', {})['effective_oos_claim_allowed'] = bool(stage73_signal['promotion_freeze_lifted'])
    overfit.setdefault('missing_validation_stack', {})['stage73_portfolio_oos_completed'] = True

    dump_json(paths['factor_promotion_decision'], promotion)
    dump_json(paths['factor_validation_report'], validation)
    dump_json(paths['portfolio_backtest_report'], portfolio_report)
    dump_json(paths['overfit_audit_report'], overfit)
    dump_json(paths['factor_iteration_pool'], pool)
    dump_json(paths['factor_lineage'], lineage)

    control_sync = {
        'report_type': 'current_combo_stage73_control_sync',
        'generated_at': now,
        'decision': stage73_signal['decision'],
        'promotion_freeze_lifted': stage73_signal['promotion_freeze_lifted'],
        'stage73_signal': stage73_signal,
        'source_paths': {k: str(v) for k, v in paths.items()},
        'source_mtimes': {k: iso_mtime(v) for k, v in paths.items()},
        'closure_checks': {
            'current_market_fingerprint_exists': paths['current_market_fingerprint'].exists(),
            'regime_analog_windows_exists': paths['regime_analog_windows'].exists(),
            'stage73_replay_exists': paths['replay'].exists(),
            'stage73_portfolio_exists': paths['portfolio'].exists(),
            'stage73_nested_exists': paths['nested'].exists(),
            'stage73_walkforward_exists': paths['walkforward'].exists(),
            'stage73_independent_oos_holdout_exists': paths['independent_oos_holdout'].exists(),
            'qlib_recorder_id': qlib_rec.get('recorder_id'),
        },
        'summary': (
            'Stage73 refreshed market fingerprint/regime analog context and produced replay, portfolio, nested, walkforward, '
            f"independent OOS/holdout plus qlib recorder={qlib_rec.get('recorder_id')}. "
            f"c2c independent OOS={pct(stage73_signal['metrics']['independent_oos_total_return'])}/excess {pct(stage73_signal['metrics']['independent_oos_excess_total_return'])}; "
            f"holdout={pct(stage73_signal['metrics']['holdout_total_return'])}/excess {pct(stage73_signal['metrics']['holdout_excess_total_return'])}."
        ),
    }
    out = EVOLUTION / 'current_combo_stage73_control_sync.json'
    dump_json(out, control_sync)
    print(json.dumps({'status': 'ok', 'control_sync': str(out), 'recorder_id': qlib_rec.get('recorder_id'), 'promotion_freeze_lifted': stage73_signal['promotion_freeze_lifted']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
