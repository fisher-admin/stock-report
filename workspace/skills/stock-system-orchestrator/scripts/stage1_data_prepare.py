#!/usr/bin/env python3
"""Stage 1: block until the latest required market-day cache is ready."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from python_env_bootstrap import bootstrap_openclaw_venv
from point_in_time_store import PointInTimeStore

ENV_BOOTSTRAP = bootstrap_openclaw_venv()
import tushare as ts

# Some recent lightweight Tushare installations expose ``pro_api`` but omit
# the legacy module-level ``set_token`` helper.  Stage 1 only needs that call
# for the legacy client; replacement providers authenticate with their own
# key headers.  Keep the compatibility hook so both paths can start.
if not hasattr(ts, "set_token"):
    ts.set_token = lambda _token: None

from orchestrator_common import WORKSPACE, HEALTH_DIR, CACHE_DIR
from trading_calendar_store import persist_trading_calendar

CACHE = CACHE_DIR
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
PAT = re.compile(r'^(stk_factor|daily|cyq_perf)_(\d{8})\.parquet$')
SCRIPT_DIR = WORKSPACE / 'skills/stock-analyzer'
sys.path.insert(0, str(SCRIPT_DIR))
import pipeline as pl  # noqa: E402
import backtest as bt  # noqa: E402
sys.path.insert(0, str(WORKSPACE / 'skills/stock-system-orchestrator/scripts'))
from orchestrator_common import latest_common_trade_date, write_canonical_object, write_health_snapshot  # noqa: E402

DATA_TYPES = ('stk_factor', 'daily', 'cyq_perf')
MIN_ROW_COUNT = int(os.environ.get('OPENCLAW_STAGE1_MIN_ROW_COUNT', '1000'))
RETRY_INTERVAL_SECONDS = int(os.environ.get('OPENCLAW_STAGE1_RETRY_INTERVAL_SECONDS', '600'))
MAX_WAIT_SECONDS = int(os.environ.get('OPENCLAW_STAGE1_MAX_WAIT_SECONDS', '7200'))
REQUIRED_CYQ_COLUMNS = {'ts_code', 'cost_15pct', 'cost_50pct', 'cost_85pct', 'weight_avg', 'winner_rate'}
FATAL_QUALITY_ERRORS = {
    'trading calendar unavailable or invalid',
}


def collect_cache_dates() -> dict[str, set[str]]:
    by = {'stk_factor': set(), 'daily': set(), 'cyq_perf': set()}
    for f in CACHE.glob('*.parquet'):
        m = PAT.match(f.name)
        if m:
            by[m.group(1)].add(m.group(2))
    return by


def is_degraded_fallback_source(source: str) -> bool:
    text = str(source or '').strip().lower()
    if not text:
        return False
    # RDS is the primary provider and Promax is the per-interface fallback;
    # neither should make an otherwise healthy run appear degraded.
    primary_providers = {
        'rds',
        'promax',
        'rds+promax',
        'promax+rds',
        'rds_primary_promax_fallback',
        'tdx_api',  # 2026-11-14: tdx-api 日线为首选源，与 RDS 逐位一致
    }
    if text in primary_providers:
        return False
    return not text.startswith('tushare')


def resolve_provider_metadata(df, *, default_provider: str = 'tushare_cached_fetch') -> tuple[str, bool]:
    if df is None or len(df) == 0:
        return default_provider, False

    providers: list[str] = []
    if 'source_provider' in df.columns:
        providers.extend([str(item) for item in df['source_provider'].dropna().unique().tolist() if str(item)])
    if 'fallback_source' in df.columns:
        providers.extend([str(item) for item in df['fallback_source'].dropna().unique().tolist() if str(item)])

    if not providers:
        return default_provider, False

    ordered = list(dict.fromkeys(providers))
    provider = '+'.join(ordered)
    degraded = any(is_degraded_fallback_source(item) for item in ordered)
    return provider, degraded


def _akshare_open_trade_date(date_str: str) -> tuple[str | None, str | None]:
    """免费交易日历兜底（akshare，无需 Tushare token）。返回 (yyyymmdd, source) 或 (None, None)。
    根因修复 2026-06-24：Tushare token 过期会让 pro.trade_cal 抛错，原逻辑直接静默退回缓存旧交易日，
    把「昨天」当成今天继续跑。此兜底在 Tushare 日历失败时用 akshare 解析真实交易日，避免静默落后一天。"""
    if not (isinstance(date_str, str) and len(date_str) == 8 and date_str.isdigit()):
        return None, None
    try:
        import akshare as ak  # noqa: PLC0415  懒加载，仅在需要兜底时引入
        df = ak.tool_trade_date_hist_sina()
        dates = sorted({str(d).replace('-', '') for d in df['trade_date'].astype(str)})
    except Exception:
        return None, None
    if not dates:
        return None, None
    if date_str in set(dates):
        return date_str, 'akshare_calendar_same_day'
    prev = [d for d in dates if d <= date_str]
    if prev:
        return prev[-1], 'akshare_calendar_previous_open_day'
    return None, None


def latest_open_trade_date(pro, date_str: str | None = None) -> tuple[str | None, dict]:
    requested_source = 'argument' if date_str else None
    env_trade_date = str(os.environ.get('OPENCLAW_TARGET_TRADE_DATE') or '').strip()
    debug: dict[str, str | None] = {
        'requested_date': date_str,
        'requested_date_source': requested_source,
        'calendar_trade_date': None,
        'cached_latest_common_trade_date': None,
        'selection_source': None,
        'calendar_error': None,
    }
    cached_latest, _ = latest_common_trade_date()
    debug['cached_latest_common_trade_date'] = cached_latest
    if date_str is None:
        if env_trade_date and re.fullmatch(r'\d{8}', env_trade_date):
            date_str = env_trade_date
            debug['requested_date_source'] = 'env_target_trade_date'
        else:
            date_str = datetime.now().strftime('%Y%m%d')
            debug['requested_date_source'] = 'now'
    debug['requested_date'] = date_str
    # 2026-06-28 去 Tushare 化：免费 akshare(新浪)日历改为【主用】，不再先调 Tushare。
    ak_date, ak_src = _akshare_open_trade_date(date_str)
    if ak_date:
        debug['calendar_trade_date'] = ak_date
        debug['selection_source'] = ak_src
        return ak_date, debug
    # akshare 不可用时，优先使用已经初始化的配置数据客户端；只有旧 Tushare
    # 客户端才要求 legacy token，避免 RDS/Promax 被错误地视为“无数据源”。
    provider_mode = str(getattr(pl, "STOCK_DATA_PROVIDER", "") or "").strip().lower()
    replacement_client = provider_mode in {"auto", "rds", "promax", "rds_promax"}
    if pro is not None and (str(os.environ.get('TUSHARE_TOKEN') or '').strip() or replacement_client):
        try:
            cal = pro.trade_cal(exchange='SSE', start_date=date_str, end_date=date_str, fields='cal_date,is_open,pretrade_date')
            if cal is not None and len(cal) > 0 and int(cal.iloc[0]['is_open']) == 1:
                debug['calendar_trade_date'] = str(cal.iloc[0]['cal_date'])
                debug['selection_source'] = 'calendar_same_day'
                return str(cal.iloc[0]['cal_date']), debug
            base_date = datetime.strptime(date_str, '%Y%m%d') if len(date_str) == 8 else datetime.now()
            start = base_date.replace(day=1).strftime('%Y%m%d')
            cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=date_str, fields='cal_date,is_open')
            if cal is not None and len(cal) > 0:
                open_days = cal[cal['is_open'] == 1].sort_values('cal_date', ascending=False)
                if len(open_days) > 0:
                    debug['calendar_trade_date'] = str(open_days.iloc[0]['cal_date'])
                    debug['selection_source'] = 'calendar_previous_open_day'
                    return str(open_days.iloc[0]['cal_date']), debug
        except Exception as exc:
            debug['calendar_error'] = str(exc)
    if not debug.get('calendar_error'):
        debug['calendar_error'] = 'akshare 日历不可用且无可用日历数据源'
    if cached_latest:
        debug['selection_source'] = 'cache_fallback_after_calendar_error' if debug.get('calendar_error') else 'cache_fallback'
        return cached_latest, debug
    return None, debug


def inspect_cache_quality(target_trade_date: str, refreshed: dict[str, dict]) -> tuple[bool, list[str]]:
    issues: list[str] = []

    for data_type in DATA_TYPES:
        meta = refreshed.get(data_type) or {}
        if not meta.get('cache_exists_after'):
            issues.append(f'{data_type} cache missing for {target_trade_date}')
            continue
        if int(meta.get('row_count', 0) or 0) < MIN_ROW_COUNT:
            issues.append(
                f'{data_type} row_count too small for {target_trade_date}: {meta.get("row_count", 0)}'
            )

    cyq_file = CACHE / f'cyq_perf_{target_trade_date}.parquet'
    if cyq_file.exists():
        try:
            cyq = pd.read_parquet(cyq_file)
            missing_cols = sorted(REQUIRED_CYQ_COLUMNS - set(cyq.columns))
            if missing_cols:
                issues.append(f'cyq_perf missing required columns: {missing_cols}')
            if 'winner_rate' in cyq.columns and float(cyq['winner_rate'].nunique(dropna=True)) <= 2:
                issues.append('cyq_perf winner_rate dispersion abnormal')
            if 'weight_avg' in cyq.columns and float(cyq['weight_avg'].nunique(dropna=True)) <= 50:
                issues.append('cyq_perf weight_avg dispersion abnormal')
        except Exception as exc:
            issues.append(f'cyq_perf quality inspection failed: {exc}')

    issues.extend(completeness_issues(target_trade_date, refreshed))
    return not issues, issues


# 2026-10-30: a truncated provider response (stk_factor 20261012: 4139 of 5561
# rows; 20260717: 79.5%) passed the bare MIN_ROW_COUNT floor.  Over 197 cached
# days the normal ratio to same-day daily rows is >= 0.98 (stk_factor) and
# >= 0.962 (cyq_perf), so these floors only catch genuinely partial files.
COMPLETENESS_FLOORS = {'stk_factor': 0.98, 'cyq_perf': 0.95}
INCOMPLETE_MARKER = 'incomplete_vs_daily'


def completeness_issues(trade_date: str, refreshed: dict[str, dict]) -> list[str]:
    daily_rows = int((refreshed.get('daily') or {}).get('row_count') or 0)
    if daily_rows < MIN_ROW_COUNT:
        return []  # the daily floor itself is reported above
    issues = []
    for data_type, floor in COMPLETENESS_FLOORS.items():
        rows = int((refreshed.get(data_type) or {}).get('row_count') or 0)
        if rows and rows < floor * daily_rows:
            issues.append(f'{data_type} {INCOMPLETE_MARKER} for {trade_date}: {rows}/{daily_rows} rows')
    return issues


def quarantine_incomplete_caches(trade_date: str, quality_errors: list[str]) -> list[str]:
    """Move a partial cache file aside so the next attempt refetches it.

    bt.get_cached returns an existing file without asking the provider again,
    so a truncated file would otherwise be retried, and gap-filled, forever.
    """
    moved = []
    for data_type in COMPLETENESS_FLOORS:
        if not any(error.startswith(f'{data_type} {INCOMPLETE_MARKER}') for error in quality_errors):
            continue
        path = CACHE / f'{data_type}_{trade_date}.parquet'
        if path.exists():
            target = path.with_name(f'{path.name}.incomplete-{datetime.now().strftime("%Y%m%d%H%M%S")}')
            os.replace(path, target)
            moved.append(str(target))
    return moved


def apply_quality_guards(
    *,
    quality_ok: bool,
    quality_errors: list[str],
    refreshed: dict[str, dict],
    calendar_ok: bool,
) -> tuple[bool, list[str]]:
    errors = list(quality_errors or [])
    cyq_provider = str((refreshed.get('cyq_perf') or {}).get('provider') or '')
    cyq_proxy_derived = 'proxy' in cyq_provider.lower()
    if not calendar_ok:
        errors.append('trading calendar unavailable or invalid')
    if cyq_proxy_derived:
        errors.append('cyq proxy forbidden')
    errors.extend(incompatible_substitute_errors(refreshed))
    deduped = list(dict.fromkeys(errors))
    return bool(quality_ok and not deduped), deduped


# 2026-10-03: fallbacks that are reachable but NOT field-compatible with the
# selection contract.  They may be cached for diagnostics, but they must never
# satisfy same-day readiness (no quiet substitution of incompatible fields).
INCOMPATIBLE_SUBSTITUTES = {
    'stk_factor': {
        'local_history_derived_technical':
            'stk_factor incompatible substitute: close_qfq/indicators derived from unadjusted daily close',
    },
    'cyq_perf': {
        'akshare_stock_cyq_em':
            'cyq_perf incompatible substitute: 90% band edges (5th/95th pct) mapped into cost_15pct/cost_85pct',
    },
}


def incompatible_substitute_errors(refreshed: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    for data_type, markers in INCOMPATIBLE_SUBSTITUTES.items():
        provider = str((refreshed.get(data_type) or {}).get('provider') or '').lower()
        for marker, message in markers.items():
            if marker in provider:
                errors.append(message)
    return errors


def has_fatal_quality_errors(quality_errors: list[str]) -> bool:
    return any(str(error) in FATAL_QUALITY_ERRORS for error in (quality_errors or []))


def write_health(target_trade_date: str, payload: dict) -> Path:
    out = HEALTH_DIR / f'data_ready_{target_trade_date}.json'
    write_health_snapshot(out, payload, trade_date=target_trade_date, latest_name='data_preparation_run.json')
    if target_trade_date != 'missing':
        write_canonical_object(
            'market_state',
            target_trade_date,
            {
                'target_trade_date': target_trade_date,
                'data_preparation': {
                    'ok': payload.get('ok'),
                    'same_day_ready': payload.get('same_day_ready'),
                    'quality_ok': payload.get('quality_ok'),
                    'degraded': payload.get('degraded'),
                    'cyq_perf_fallback': payload.get('cyq_perf_fallback'),
                    'cyq_perf_proxy_derived': payload.get('cyq_perf_proxy_derived'),
                    'cyq_perf_provider': payload.get('cyq_perf_provider'),
                    'latest_by_type': payload.get('latest_by_type'),
                    'quality_errors': payload.get('quality_errors'),
                },
            },
        )
    return out


COMMON_PIT_MARKET = WORKSPACE / 'stock_data' / '03-working' / 'fundamental_cache' / 'pit_market' / 'v2'
PIT_SNAPSHOT_MIN_SECONDS = 600


def freeze_pit_market_snapshot(pro, trade_date: str, *, store: Path | None = None, collect=None) -> dict:
    """Fetch and freeze the day's PIT universe/daily_basic once, in the evening.

    Before 2026-10-30 the same day file was fetched separately by the 22:15
    short-track job and by the next 08:15 event-drift job; whichever finished
    first froze it.  When the research job timed out (10-26..29) the 23:30
    forward shadow had no daily_basic and lost four forward days.  Freezing it
    here makes every consumer read one evening copy.  First write wins, exactly
    like the consumers' own freeze; an existing day file is never refetched.
    """
    store = COMMON_PIT_MARKET if store is None else store
    targets = {name: store / f'{name}_{trade_date}.parquet' for name in ('universe', 'daily_basic')}
    if all(path.exists() for path in targets.values()):
        return {'status': 'already_frozen', 'paths': {k: str(v) for k, v in targets.items()}}
    if collect is None:
        import pit_market_snapshot as pms  # noqa: PLC0415 - heavy, only needed here
        collect = pms.collect_pit_market_snapshot
    snapshot = collect(pro, trade_date)
    if str(snapshot.get('trade_date')) != trade_date:
        raise ValueError(f"PIT snapshot date {snapshot.get('trade_date')} != {trade_date}")
    store.mkdir(parents=True, exist_ok=True)
    written = []
    for name, path in targets.items():
        if path.exists():
            continue  # first write wins; consumers record later differences as revisions
        tmp = path.with_name(f'.{path.name}.stage1.tmp')
        snapshot[name].reset_index(drop=True).to_parquet(tmp, index=False)
        os.replace(tmp, path)
        written.append(name)
    return {'status': 'frozen', 'written': written, 'rows': {k: int(len(snapshot[k])) for k in targets},
            'paths': {k: str(v) for k, v in targets.items()}}


GAP_FILL_MAX_DAYS = int(os.environ.get('OPENCLAW_STAGE1_GAP_FILL_MAX_DAYS', '10'))
GAP_FILL_BUDGET_SECONDS = int(os.environ.get('OPENCLAW_STAGE1_GAP_FILL_BUDGET_SECONDS', '900'))


def missed_trade_days(target_trade_date: str, open_dates: list[str], cached: dict[str, set[str]],
                      *, max_days: int = GAP_FILL_MAX_DAYS) -> list[str]:
    """Open days before the target whose caches are incomplete, oldest first.

    A failed evening leaves holes that downstream evaluators (event drift,
    short-track settlement, forward shadow) hit for days afterwards; the target
    run only fetches its own date, so nothing else ever closes them.
    """
    if max_days <= 0:
        return []
    window = sorted(d for d in set(open_dates or []) if d < target_trade_date)[-max_days:]
    return [d for d in window if any(d not in cached.get(data_type, set()) for data_type in DATA_TYPES)]


def _parquet_rows(path: Path) -> int:
    import pyarrow.parquet as pq  # noqa: PLC0415 - metadata only, no data load
    return int(pq.ParquetFile(path).metadata.num_rows)


def quarantine_partial_window(days: list[str]) -> list[str]:
    """Quarantine already-cached partial files so the gap fill refetches them."""
    moved: list[str] = []
    for day in days:
        daily = CACHE / f'daily_{day}.parquet'
        if not daily.exists():
            continue
        try:
            refreshed = {data_type: {'row_count': _parquet_rows(CACHE / f'{data_type}_{day}.parquet')}
                         for data_type in DATA_TYPES if (CACHE / f'{data_type}_{day}.parquet').exists()}
        except Exception:
            continue  # unreadable files are left for the normal quality path
        moved.extend(quarantine_incomplete_caches(day, completeness_issues(day, refreshed)))
    return moved


def fill_missed_trade_days(pro, target_trade_date: str, calendar_payload: dict | None, *,
                           budget_seconds: float = GAP_FILL_BUDGET_SECONDS, clock=time.monotonic) -> dict:
    """Best-effort backfill of earlier missed days; never blocks the target day.

    Uses the same fetch, proxy and quality rules as the target day, writes only
    per-date evidence, and never touches *_latest pointers.
    """
    open_dates = list((calendar_payload or {}).get('open_dates') or [])
    window = sorted(d for d in set(open_dates) if d < target_trade_date)[-GAP_FILL_MAX_DAYS:]
    quarantined = quarantine_partial_window(window)
    days = missed_trade_days(target_trade_date, open_dates, collect_cache_dates())
    result: dict = {'candidates': days, 'filled': [], 'failed': {}, 'skipped_budget': [],
                    'quarantined': quarantined}
    if not days:
        return result
    started = clock()
    pit_store = PointInTimeStore(WORKSPACE / "stock_data" / "01-raw" / "pit_store")
    for day in days:
        if clock() - started >= budget_seconds:
            result['skipped_budget'].append(day)
            continue
        refreshed: dict[str, dict] = {}
        errors: list[str] = []
        for data_type in DATA_TYPES:
            cache_file = CACHE / f'{data_type}_{day}.parquet'
            existed = cache_file.exists()
            try:
                df = bt.get_cached(pro, day, data_type)
                provider, degraded_source = resolve_provider_metadata(df)
                if cache_file.exists() and 'proxy' not in provider.lower():
                    pit_store.put(day, data_type, cache_file.read_bytes())
                refreshed[data_type] = {
                    'trade_date': day, 'cache_exists_before': existed,
                    'cache_exists_after': cache_file.exists(),
                    'row_count': int(len(df)) if df is not None else 0,
                    'fetched': (not existed) and cache_file.exists(),
                    'provider': provider, 'degraded_source': degraded_source,
                }
            except Exception as exc:
                errors.append(f'{data_type}: {exc}')
                refreshed[data_type] = {'trade_date': day, 'cache_exists_before': existed,
                                        'cache_exists_after': cache_file.exists(), 'row_count': 0}
        quality_ok, quality_errors = inspect_cache_quality(day, refreshed)
        quality_ok, quality_errors = apply_quality_guards(
            quality_ok=quality_ok, quality_errors=quality_errors, refreshed=refreshed, calendar_ok=True)
        ok = quality_ok and not errors
        write_health_snapshot(HEALTH_DIR / f'data_ready_{day}.json', {
            'stage': 'dataPreparation', 'mode': 'gap_fill', 'triggered_by_target': target_trade_date,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'target_trade_date': day,
            'ok': ok, 'quality_ok': quality_ok, 'same_day_ready': ok, 'refreshed': refreshed,
            'refresh_errors': errors, 'quality_errors': quality_errors,
        }, trade_date=day)
        if ok:
            result['filled'].append(day)
        else:
            result['failed'][day] = (errors + quality_errors)[:5]
            quarantine_incomplete_caches(day, quality_errors)
    return result


def materialize_trading_calendar(pro, target_trade_date: str, *, output_path: Path | None = None) -> dict:
    return persist_trading_calendar(pro, path=output_path, as_of_date=target_trade_date)


def main() -> int:
    pro = pl.init_tushare()

    target_trade_date, trade_date_debug = latest_open_trade_date(pro)
    deadline_at = time.time() + max(MAX_WAIT_SECONDS, 0)
    attempt = 0

    bt.ENABLE_AKSHARE_CYQ_FALLBACK = True

    if not target_trade_date:
        payload = {
            'stage': 'dataPreparation',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'target_trade_date': None,
            'trade_date_resolution': trade_date_debug,
            'attempt': attempt,
            'max_wait_seconds': MAX_WAIT_SECONDS,
            'retry_interval_seconds': RETRY_INTERVAL_SECONDS,
            'ok': False,
            'quality_ok': False,
            'quality_errors': ['trade date resolution failed'],
            'notes': ['No valid trade date could be resolved from the exchange calendar.'],
        }
        out = write_health('missing', payload)
        print(f'[stage1] wrote readiness file: {out}')
        return 1

    calendar_payload = None
    calendar_error = None

    while True:
        attempt += 1
        if calendar_payload is None:
            try:
                calendar_payload = materialize_trading_calendar(pro, target_trade_date)
                calendar_error = None
            except Exception as exc:
                calendar_error = str(exc)
        before = collect_cache_dates()
        refreshed: dict[str, dict] = {}
        refresh_errors: dict[str, str] = {}

        pit_store = PointInTimeStore(WORKSPACE / "stock_data" / "01-raw" / "pit_store")
        for data_type in DATA_TYPES:
            cache_file = CACHE / f'{data_type}_{target_trade_date}.parquet'
            existed = cache_file.exists()
            try:
                df = bt.get_cached(pro, target_trade_date, data_type)
                provider, degraded_source = resolve_provider_metadata(df)
                if cache_file.exists() and 'proxy' not in provider.lower():
                    try:
                        pit_store.put(target_trade_date, data_type, cache_file.read_bytes())
                    except Exception as pit_exc:
                        refresh_errors[f"pit_{data_type}"] = f"PIT store error: {pit_exc}"
                refreshed[data_type] = {
                    'trade_date': target_trade_date,
                    'cache_exists_before': existed,
                    'cache_exists_after': cache_file.exists(),
                    'row_count': int(len(df)) if df is not None else 0,
                    'fetched': (not existed) and cache_file.exists(),
                    'provider': provider,
                    'degraded_source': degraded_source,
                }
            except Exception as exc:
                refresh_errors[data_type] = str(exc)
                refreshed[data_type] = {
                    'trade_date': target_trade_date,
                    'cache_exists_before': existed,
                    'cache_exists_after': cache_file.exists(),
                    'row_count': 0,
                    'fetched': False,
                    'provider': 'tushare_cached_fetch',
                    'degraded_source': False,
                }

        # A later fallback may have fetched an earlier missing dataset. Re-read
        # that evidence before deciding whether to retry or stop.
        for data_type in DATA_TYPES:
            meta = refreshed[data_type]
            cache_file = CACHE / f'{data_type}_{target_trade_date}.parquet'
            if not meta['cache_exists_after'] and cache_file.exists():
                df = pd.read_parquet(cache_file)
                provider, degraded_source = resolve_provider_metadata(df)
                meta.update(cache_exists_after=True, row_count=len(df), fetched=True,
                            provider=provider, degraded_source=degraded_source)
                refresh_errors.pop(data_type, None)
        after = collect_cache_dates()
        common = sorted(after['stk_factor'] & after['daily'] & after['cyq_perf'])
        latest = common[-1] if common else None
        degraded = bool((after['stk_factor'] & after['daily']) and not common)
        degraded = degraded or any(bool((refreshed.get(data_type) or {}).get('degraded_source')) for data_type in DATA_TYPES)
        quality_ok, quality_errors = inspect_cache_quality(target_trade_date, refreshed)
        cyq_provider = str((refreshed.get('cyq_perf') or {}).get('provider') or '')
        cyq_proxy_derived = 'proxy' in cyq_provider.lower()
        quality_ok, quality_errors = apply_quality_guards(
            quality_ok=quality_ok,
            quality_errors=quality_errors,
            refreshed=refreshed,
            calendar_ok=(calendar_payload is not None),
        )
        # Target-day readiness: all three cache types for THIS trade_date exist with quality.
        # Do NOT require target to be the global latest — that blocked historical backfill
        # whenever a newer day was already cached (e.g. backfilling 20260728 after 20260807).
        target_caches_present = all(target_trade_date in after[data_type] for data_type in DATA_TYPES)
        same_day_ready = bool(target_caches_present and quality_ok)
        # Live "wait for today's open data" still works: when target is today and caches missing, not ready.
        ok = same_day_ready and quality_ok and not quality_errors and not any(k.startswith('pit_') for k in refresh_errors)
        next_retry_at = None if ok else datetime.now() + timedelta(seconds=RETRY_INTERVAL_SECONDS)

        payload = {
            'stage': 'dataPreparation',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'target_trade_date': target_trade_date,
            'trade_date_resolution': trade_date_debug,
            'trading_calendar': calendar_payload,
            'trading_calendar_ok': calendar_payload is not None,
            'trading_calendar_error': calendar_error,
            'attempt': attempt,
            'max_wait_seconds': MAX_WAIT_SECONDS,
            'retry_interval_seconds': RETRY_INTERVAL_SECONDS,
            'deadline_at': datetime.fromtimestamp(deadline_at).strftime('%Y-%m-%d %H:%M:%S'),
            'latest_by_type': {k: (sorted(v)[-1] if v else None) for k, v in after.items()},
            'latest_common_trade_date': latest,
            'ok': ok,
            'quality_ok': quality_ok,
            'same_day_ready': same_day_ready,
            'degraded': degraded,
            'cyq_perf_proxy_derived': cyq_proxy_derived,
            'cyq_perf_provider': cyq_provider,
            'refreshed': refreshed,
            'refresh_errors': refresh_errors,
            'quality_errors': quality_errors,
            'before_latest_by_type': {k: (sorted(v)[-1] if v else None) for k, v in before.items()},
            'next_retry_at': next_retry_at.strftime('%Y-%m-%d %H:%M:%S') if next_retry_at else None,
            'notes': [
                'Stage 1 blocks downstream execution until same-day daily/stk_factor/cyq_perf caches are all ready.',
                'If the latest trade-day cache is not qualified, Stage 1 keeps retrying on a fixed interval.',
                'daily may fall back to same-day market snapshots (for example efinance / Eastmoney-style feeds) when Tushare bulk fetch is unavailable.',
                'stk_factor may fall back to locally derived technical indicators built from the latest daily cache panel.',
                'cyq_perf may fall back to ts_code-batched Tushare requests when trade_date bulk fetch is unsupported.',
                'CYQ data marked as proxy may be retained only for diagnostics; it is never accepted for readiness, scoring, backtests, factor evaluation, or AI evidence.',
                'If no provider can supply current-trade-date critical caches, Stage 1 fails closed instead of downgrading to previous-day data.',
            ],
        }
        out = write_health(target_trade_date, payload)
        print(f'[stage1] wrote readiness file: {out}')

        if ok:
            # Stay inside the orchestrator's stage timeout: the target day is
            # already ready, so a killed gap fill would turn success into failure.
            budget = min(GAP_FILL_BUDGET_SECONDS, deadline_at - time.time() - 60)
            try:
                gap_fill = fill_missed_trade_days(pro, target_trade_date, calendar_payload,
                                                  budget_seconds=max(budget, 0))
            except Exception as exc:
                gap_fill = {'error': str(exc)}
            if gap_fill.get('candidates') or gap_fill.get('error'):
                payload['gap_fill'] = gap_fill
                print(f'[stage1] gap fill for missed days: {json.dumps(gap_fill, ensure_ascii=False)}')
            # Non-blocking: v4.3 selection does not read it; downstream consumers fall back to their own fetch.
            if deadline_at - time.time() >= PIT_SNAPSHOT_MIN_SECONDS:
                try:
                    payload['pit_snapshot'] = freeze_pit_market_snapshot(pro, target_trade_date)
                except Exception as exc:
                    payload['pit_snapshot'] = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'[:500]}
            else:
                payload['pit_snapshot'] = {'status': 'skipped_budget'}
            print(f"[stage1] PIT snapshot: {json.dumps(payload['pit_snapshot'], ensure_ascii=False)}")
            write_health(target_trade_date, payload)
            return 0

        moved = quarantine_incomplete_caches(target_trade_date, payload.get('quality_errors') or [])
        if moved:
            print(f'[stage1] quarantined partial cache for refetch: {moved}')

        if has_fatal_quality_errors(payload.get('quality_errors') or []):
            payload['next_retry_at'] = None
            payload['notes'].append('Fatal quality guard triggered; Stage 1 failed closed without retry.')
            out = write_health(target_trade_date, payload)
            print(f'[stage1] wrote fatal failure file: {out}')
            return 1

        if time.time() + RETRY_INTERVAL_SECONDS > deadline_at:
            payload['next_retry_at'] = None
            payload['notes'].append(
                'Current-trade-date cyq_perf unavailable before deadline; Stage 1 failed closed without using previous-day data.'
            )
            out = write_health(target_trade_date, payload)
            print(f'[stage1] wrote terminal failure file: {out}')
            return 1

        print(
            f'[stage1] latest data for {target_trade_date} not ready; '
            f'sleeping {RETRY_INTERVAL_SECONDS}s before retry {attempt + 1}'
        )
        time.sleep(RETRY_INTERVAL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
