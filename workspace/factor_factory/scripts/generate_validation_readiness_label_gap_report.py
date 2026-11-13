#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
WORKSPACE = Path('/Users/fisher/.openclaw/workspace')
EVOLUTION = ROOT / 'outputs' / 'evolution'
GREENFIELD = ROOT / 'outputs' / 'greenfield_multifactor'
PANEL_PATH = ROOT / 'data' / 'factors' / 'panel_phase1.parquet'
REGIME_PATH = ROOT / 'data' / 'regimes' / 'regime_labels_phase1.parquet'
CURRENT_MARKET_PATH = EVOLUTION / 'current_market_fingerprint.json'
ANALOG_WINDOWS_PATH = EVOLUTION / 'regime_analog_windows.json'
ANALOG_REPORT_PATH = EVOLUTION / 'analog_validation_report.json'
ITERATION_POOL_PATH = EVOLUTION / 'factor_iteration_pool.json'
LINEAGE_PATH = EVOLUTION / 'factor_lineage.json'
VALIDATION_PATH = EVOLUTION / 'factor_validation_report.json'
GAP_ATTRIBUTION_PATH = EVOLUTION / 'selection_history_gap_attribution.json'
HOLDOUT_RESERVE_PATH = EVOLUTION / 'current_combo_holdout_reserve.json'
GREENFIELD_SNAPSHOT_PATH = GREENFIELD / 'factor_pool_snapshot.json'
GREENFIELD_LEADERS_PATH = GREENFIELD / 'current_factor_leaders.json'
BACKTEST_CACHE_DIR = WORKSPACE / 'stock_data' / '03-working' / 'backtest_cache'
SELECTION_HISTORY_DIR = WORKSPACE / 'stock_data' / '03-working' / 'selection_history'
OUTPUT_JSON = EVOLUTION / 'validation_readiness_and_label_gap_report.json'
OUTPUT_MD = EVOLUTION / 'validation_readiness_and_label_gap_report.md'
TZ = ZoneInfo('Asia/Shanghai')
LABEL_COLUMNS = {
    'close_to_close': 'next_return_close_to_close_1d',
    'open_to_close': 'next_return_open_to_close_1d',
    'close_to_open': 'next_return_close_to_open_1d',
}


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def iso_mtime(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds') if path.exists() else None


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def next_calendar_date(trade_date: str) -> str | None:
    if not trade_date:
        return None
    return (datetime.strptime(trade_date, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')


def build_report() -> dict:
    now = iso_now()
    panel = pd.read_parquet(PANEL_PATH, columns=['trade_date', 'benchmark_eligible', *LABEL_COLUMNS.values()])
    panel['trade_date'] = panel['trade_date'].astype(str)
    panel['benchmark_eligible'] = panel['benchmark_eligible'].fillna(False).astype(bool)
    eligible = panel[panel['benchmark_eligible']].copy()
    eligible['any_label_missing'] = eligible[list(LABEL_COLUMNS.values())].isna().any(axis=1)

    panel_max_trade_date = str(panel['trade_date'].max()) if not panel.empty else None
    eligible_max_trade_date = str(eligible['trade_date'].max()) if not eligible.empty else None
    next_daily_date = next_calendar_date(panel_max_trade_date) if panel_max_trade_date else None
    next_daily_path = BACKTEST_CACHE_DIR / f'daily_{next_daily_date}.parquet' if next_daily_date else None

    latest_eligible = eligible[eligible['trade_date'] == eligible_max_trade_date].copy() if eligible_max_trade_date else eligible.iloc[0:0].copy()
    latest_missing = latest_eligible[latest_eligible['any_label_missing']].copy()
    label_missing_counts = {
        label_mode: int(latest_missing[column].isna().sum())
        for label_mode, column in LABEL_COLUMNS.items()
    }
    latest_labeled_counts = {
        label_mode: int(latest_eligible[column].notna().sum())
        for label_mode, column in LABEL_COLUMNS.items()
    }

    missing_by_date = (
        eligible.groupby('trade_date')[list(LABEL_COLUMNS.values())]
        .apply(lambda frame: pd.Series({mode: int(frame[col].isna().sum()) for mode, col in LABEL_COLUMNS.items()}))
        .reset_index()
        .sort_values('trade_date')
    )
    missing_by_date_records = missing_by_date.tail(8).to_dict(orient='records') if not missing_by_date.empty else []

    regime = pd.read_parquet(REGIME_PATH, columns=['trade_date'])
    regime['trade_date'] = regime['trade_date'].astype(str)
    regime_max_trade_date = str(regime['trade_date'].max()) if not regime.empty else None

    current_market = load_json(CURRENT_MARKET_PATH)
    analog_windows = load_json(ANALOG_WINDOWS_PATH)
    analog_report = load_json(ANALOG_REPORT_PATH)
    iteration_pool = load_json(ITERATION_POOL_PATH)
    lineage = load_json(LINEAGE_PATH)
    validation = load_json(VALIDATION_PATH)
    gap_attribution = load_json(GAP_ATTRIBUTION_PATH)
    holdout_reserve = load_json(HOLDOUT_RESERVE_PATH)
    greenfield_snapshot = load_json(GREENFIELD_SNAPSHOT_PATH)
    greenfield_leaders = load_json(GREENFIELD_LEADERS_PATH)

    reason_counts = gap_attribution.get('reason_counts', {}) if isinstance(gap_attribution, dict) else {}
    gap_summary = gap_attribution.get('short_summary') or gap_attribution.get('coverage_risk_summary')
    if not gap_summary and gap_attribution:
        dropped_rows = gap_attribution.get('dropped_rows')
        if dropped_rows is not None:
            gap_summary = (
                f"{dropped_rows} 行 selection_history→panel constituent gap，"
                f"其中 {reason_counts.get('universe_liquidity_threshold', 0)} 行 liquidity_threshold、"
                f"{reason_counts.get('universe_listing_days', 0)} 行 listing_days、"
                f"{reason_counts.get('universe_min_price', 0)} 行 min_price。"
            )

    selection_history_files = sorted(SELECTION_HISTORY_DIR.glob('*.json'))
    selection_history_latest = selection_history_files[-1].stem if selection_history_files else None
    daily_20260421_path = BACKTEST_CACHE_DIR / 'daily_20260421.parquet'

    next_day_label_blocked = bool(next_daily_path and not next_daily_path.exists() and len(latest_missing) > 0)
    audit_weight_ready = bool(next_daily_path and next_daily_path.exists() and len(latest_missing) == 0)
    blocker_summary = (
        f'{next_daily_path.name} 缺失，导致 {eligible_max_trade_date} 的 benchmark_eligible 行仍缺 next-day label。'
        if next_day_label_blocked and next_daily_path is not None and eligible_max_trade_date
        else '最新 benchmark_eligible 行 next-day label 已齐，可单独复核是否推进 audit/weight。'
        if audit_weight_ready
        else '当前仍需先补齐最新 benchmark_eligible 行的 next-day label，再判断 audit/weight。'
    )

    current_trade_date = current_market.get('trade_date')
    current_summary_basis = (current_market.get('data_status') or {}).get('current_summary_basis')
    analog_trade_date = analog_windows.get('current_trade_date')
    greenfield_latest_trade_date = (
        (greenfield_snapshot.get('latest_trade_date_snapshot') or {}).get('trade_date')
        or greenfield_leaders.get('trade_date')
    )

    return {
        'report_type': 'validation_readiness_and_label_gap_report',
        'generated_at': now,
        'official_root': str(ROOT),
        'panel_status': {
            'path': str(PANEL_PATH),
            'mtime': iso_mtime(PANEL_PATH),
            'max_trade_date': panel_max_trade_date,
            'benchmark_eligible_max_trade_date': eligible_max_trade_date,
            'benchmark_eligible_trade_dates': int(eligible['trade_date'].nunique()) if not eligible.empty else 0,
            'benchmark_eligible_rows': int(len(eligible)),
        },
        'regime_status': {
            'path': str(REGIME_PATH),
            'mtime': iso_mtime(REGIME_PATH),
            'max_trade_date': regime_max_trade_date,
            'rows': int(len(regime)),
        },
        'market_context_refresh': {
            'current_market_fingerprint_mtime': iso_mtime(CURRENT_MARKET_PATH),
            'current_market_trade_date': current_trade_date,
            'current_summary_basis': current_summary_basis,
            'regime_analog_windows_mtime': iso_mtime(ANALOG_WINDOWS_PATH),
            'regime_analog_current_trade_date': analog_trade_date,
            'analog_validation_report_mtime': iso_mtime(ANALOG_REPORT_PATH),
            'analog_validation_completed_label_modes': ((analog_report.get('current_combo_return_level_analog_replay') or {}).get('completed_label_modes') or []),
        },
        'factor_pool_refresh': {
            'factor_iteration_pool_mtime': iso_mtime(ITERATION_POOL_PATH),
            'factor_iteration_pool_as_of': iteration_pool.get('as_of'),
            'factor_lineage_mtime': iso_mtime(LINEAGE_PATH),
            'factor_lineage_as_of': lineage.get('as_of'),
            'greenfield_snapshot_mtime': iso_mtime(GREENFIELD_SNAPSHOT_PATH),
            'greenfield_latest_trade_date': greenfield_latest_trade_date,
            'greenfield_trade_dates': ((greenfield_snapshot.get('history_range') or {}).get('trade_dates')),
            'greenfield_backtestable_factor_count': greenfield_snapshot.get('backtestable_factor_count'),
            'greenfield_current_only_factor_count': greenfield_snapshot.get('current_only_factor_count'),
        },
        'label_gap': {
            'selection_history_latest_date': selection_history_latest,
            'next_daily_path': str(next_daily_path) if next_daily_path else None,
            'next_daily_exists': bool(next_daily_path and next_daily_path.exists()),
            'daily_20260421_exists': daily_20260421_path.exists(),
            'latest_benchmark_eligible_trade_date': eligible_max_trade_date,
            'latest_benchmark_eligible_rows': int(len(latest_eligible)),
            'latest_rows_with_any_missing_label': int(len(latest_missing)),
            'latest_labeled_counts': latest_labeled_counts,
            'latest_missing_label_counts': label_missing_counts,
            'missing_by_recent_trade_date': missing_by_date_records,
            'selection_history_gap_attribution_mtime': iso_mtime(GAP_ATTRIBUTION_PATH),
            'selection_history_gap_summary': gap_summary,
            'holdout_reserve_mtime': iso_mtime(HOLDOUT_RESERVE_PATH),
            'reserved_holdout_dates': holdout_reserve.get('reserved_holdout_dates', []),
            'reserved_holdout_dates_now_labeled': holdout_reserve.get('reserved_holdout_dates_now_labeled', []),
            'current_labeled_holdout_days_count': holdout_reserve.get('current_labeled_holdout_days_count'),
            'blocker_summary': blocker_summary,
        },
        'validation_readiness': {
            'factor_validation_report_mtime': iso_mtime(VALIDATION_PATH),
            'factor_validation_generated_at': validation.get('generated_at'),
            'context_refresh_ready': current_trade_date == panel_max_trade_date == regime_max_trade_date,
            'audit_weight_ready': audit_weight_ready,
            'audit_weight_blocked': not audit_weight_ready,
            'blocker': blocker_summary,
            'must_not_run_before_unblock': [
                str(ROOT / 'scripts' / 'run_factor_audit.py'),
                str(ROOT / 'scripts' / 'run_weight_search.py'),
            ],
            'recommended_next_step': (
                f'先等待 {next_daily_path.name} 落地并让 {eligible_max_trade_date} 拿到 c2c/o2c/c2o next-day label，再决定是否重跑 audit/weight。'
                if next_daily_path is not None and not next_daily_path.exists() and eligible_max_trade_date
                else f'{next_daily_path.name} 已落地，但 panel 尚未把 {eligible_max_trade_date} 的 c2c/o2c/c2o next-day label回写完成；先重建 panel 再复核 audit/weight。'
                if next_daily_path is not None and next_daily_path.exists() and len(latest_missing) > 0 and eligible_max_trade_date
                else '最新 panel 标签已齐，可再做 audit/weight 启动条件复核。'
            ),
        },
    }


def render_markdown(report: dict) -> str:
    panel_status = report['panel_status']
    regime_status = report['regime_status']
    market_context = report['market_context_refresh']
    factor_pool = report['factor_pool_refresh']
    label_gap = report['label_gap']
    readiness = report['validation_readiness']
    return '\n'.join([
        '# Validation Readiness & Label Gap Report',
        '',
        f"- generated_at: {report['generated_at']}",
        f"- panel max trade_date: {panel_status['max_trade_date']}",
        f"- regime max trade_date: {regime_status['max_trade_date']}",
        f"- current_market_fingerprint trade_date: {market_context['current_market_trade_date']}",
        f"- regime_analog_windows current_trade_date: {market_context['regime_analog_current_trade_date']}",
        '',
        '## Context refresh',
        f"- current_market_fingerprint mtime: {market_context['current_market_fingerprint_mtime']}",
        f"- regime_analog_windows mtime: {market_context['regime_analog_windows_mtime']}",
        f"- analog_validation_report mtime: {market_context['analog_validation_report_mtime']}",
        f"- current_summary_basis: {market_context['current_summary_basis']}",
        '',
        '## Factor pool refresh',
        f"- factor_iteration_pool as_of: {factor_pool['factor_iteration_pool_as_of']}",
        f"- factor_lineage as_of: {factor_pool['factor_lineage_as_of']}",
        f"- greenfield latest_trade_date: {factor_pool['greenfield_latest_trade_date']}",
        f"- greenfield backtestable/current_only factors: {factor_pool['greenfield_backtestable_factor_count']} / {factor_pool['greenfield_current_only_factor_count']}",
        '',
        '## Latest label gap',
        f"- latest benchmark_eligible trade_date: {label_gap['latest_benchmark_eligible_trade_date']}",
        f"- latest benchmark_eligible rows: {label_gap['latest_benchmark_eligible_rows']}",
        f"- latest labeled counts: {label_gap['latest_labeled_counts']}",
        f"- latest missing label counts: {label_gap['latest_missing_label_counts']}",
        f"- next daily path: {label_gap['next_daily_path']}",
        f"- next daily exists: {label_gap['next_daily_exists']}",
        f"- blocker: {label_gap['blocker_summary']}",
        '',
        '## Readiness verdict',
        f"- context_refresh_ready: {readiness['context_refresh_ready']}",
        f"- audit_weight_ready: {readiness['audit_weight_ready']}",
        f"- audit_weight_blocked: {readiness['audit_weight_blocked']}",
        f"- must_not_run_before_unblock: {', '.join(readiness['must_not_run_before_unblock'])}",
        f"- recommended_next_step: {readiness['recommended_next_step']}",
        '',
    ])


def main() -> int:
    report = build_report()
    dump_json(OUTPUT_JSON, report)
    OUTPUT_MD.write_text(render_markdown(report), encoding='utf-8')
    print(json.dumps({
        'status': 'ok',
        'json': str(OUTPUT_JSON),
        'md': str(OUTPUT_MD),
        'audit_weight_ready': report['validation_readiness']['audit_weight_ready'],
        'next_daily_exists': report['label_gap']['next_daily_exists'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
