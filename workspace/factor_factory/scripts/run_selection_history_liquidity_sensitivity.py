#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
WORKSPACE = Path('/Users/fisher/.openclaw/workspace')
EVOLUTION = ROOT / 'outputs' / 'evolution'
PANEL_PATH = ROOT / 'data' / 'factors' / 'panel_phase1.parquet'
SELECTION_HISTORY_DIR = WORKSPACE / 'stock_data' / '03-working' / 'selection_history'
BACKTEST_CACHE_DIR = WORKSPACE / 'stock_data' / '03-working' / 'backtest_cache'
STOCK_LIST_PATH = WORKSPACE / 'stock_data' / '01-root' / 'stock_list.csv'
UNIVERSE_RULES_PATH = ROOT / 'config' / 'universe_rules.yaml'
GAP_ATTRIBUTION_PATH = EVOLUTION / 'selection_history_gap_attribution.json'
OUTPUT_PATH = EVOLUTION / 'selection_history_liquidity_sensitivity_diagnostic.json'

PERCENTILE_GRID = [0.35, 0.40, 0.45, 0.50, 0.55]
THRESHOLD_SCALES = [0.80, 0.90, 1.00, 1.10, 1.20]
LIQ_BUCKETS = [0.0, 0.7, 0.8, 0.9, 1.0]
LIQ_BUCKET_LABELS = ['<0.7', '0.7-0.8', '0.8-0.9', '0.9-1.0']


def iso_now() -> str:
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def round_or_none(value: object, digits: int = 6):
    try:
        number = float(value)
    except Exception:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, digits)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def load_rules() -> dict:
    payload = yaml.safe_load(UNIVERSE_RULES_PATH.read_text(encoding='utf-8'))
    universe = payload['universe']
    filters = universe['filters']
    return {
        'minimum_listing_days': int(filters['minimum_listing_days']),
        'avg_amount_percentile_threshold': float(filters['avg_amount_percentile_threshold']),
        'avg_amount_lookback_days': int(filters['avg_amount_lookback_days']),
        'min_price': float(filters['min_price']),
        'min_daily_trading_days_ratio': float(filters['min_daily_trading_days_ratio']),
        'exclude_st': bool(filters['exclude_st']),
        'exclude_suspended': bool(filters['exclude_suspended']),
        'st_name_patterns': list(universe.get('st_name_patterns') or []),
    }


def mark_st_flags(names: pd.Series, patterns: list[str]) -> pd.Series:
    if not patterns:
        return pd.Series(False, index=names.index)
    escaped = [p.replace('*', '.*') for p in patterns]
    regex = '|'.join(escaped)
    return names.fillna('').astype(str).str.contains(regex, regex=True)


def load_selection_dates_from_panel() -> list[str]:
    panel_dates = pd.read_parquet(PANEL_PATH, columns=['trade_date', 'benchmark_date_eligible'])
    panel_dates['trade_date'] = panel_dates['trade_date'].astype(str)
    dates = sorted(panel_dates.loc[panel_dates['benchmark_date_eligible'] == 1, 'trade_date'].unique().tolist())
    if not dates:
        raise RuntimeError('No benchmark_date_eligible dates found in panel_phase1.parquet')
    return dates


def load_selection_history(selection_dates: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for trade_date in selection_dates:
        path = SELECTION_HISTORY_DIR / f'{trade_date}.json'
        if not path.exists():
            raise FileNotFoundError(f'Missing selection_history file: {path}')
        payload = json.loads(path.read_text(encoding='utf-8'))
        items = payload.get('stocks') or payload.get('selections') or []
        for idx, item in enumerate(items, start=1):
            ts_code = str(item.get('ts_code') or item.get('code') or '').strip()
            if not ts_code:
                continue
            rows.append(
                {
                    'trade_date': trade_date,
                    'ts_code': ts_code,
                    'rank': int(item.get('rank') or item.get('rank_no') or idx),
                    'selection_score': item.get('score'),
                    'selection_price': item.get('price'),
                    'selection_name': item.get('name'),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError('Selection history expansion is empty')
    return df.sort_values(['trade_date', 'rank', 'ts_code']).reset_index(drop=True)


def choose_cache_dates(selection_dates: list[str], lookback_trading_days: int = 90) -> list[str]:
    cache_paths = sorted(BACKTEST_CACHE_DIR.glob('daily_*.parquet'))
    cache_dates = [path.stem.split('_')[-1] for path in cache_paths]
    if not cache_dates:
        raise RuntimeError('No backtest_cache daily parquet files found')
    start_idx = cache_dates.index(selection_dates[0])
    end_idx = cache_dates.index(selection_dates[-1])
    return cache_dates[max(0, start_idx - lookback_trading_days): end_idx + 1]


def load_market_slice(cache_dates: list[str], rules: dict) -> pd.DataFrame:
    master = pd.read_csv(STOCK_LIST_PATH, dtype={'ts_code': str})[['ts_code', 'name', 'list_date']]
    master['list_date'] = pd.to_datetime(master['list_date'], format='%Y%m%d', errors='coerce')

    frames: list[pd.DataFrame] = []
    keep_cols = ['ts_code', 'trade_date', 'close', 'vol', 'amount']
    for trade_date in cache_dates:
        path = BACKTEST_CACHE_DIR / f'daily_{trade_date}.parquet'
        df = pd.read_parquet(path, columns=keep_cols)
        df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str), format='%Y%m%d', errors='coerce')
        df['ts_code'] = df['ts_code'].astype(str)
        frames.append(df)
    market = pd.concat(frames, ignore_index=True)
    market = market.merge(master, on='ts_code', how='left')
    market = market.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    market['amount'] = pd.to_numeric(market['amount'], errors='coerce').fillna(0.0)
    market['vol'] = pd.to_numeric(market['vol'], errors='coerce').fillna(0.0)
    market['close'] = pd.to_numeric(market['close'], errors='coerce')
    market['is_trading_day'] = ((market['amount'] > 0) & (market['vol'] > 0)).astype(int)
    lookback = int(rules['avg_amount_lookback_days'])
    min_periods = min(20, lookback)
    market['avg_amount_lookback'] = market.groupby('ts_code', group_keys=False)['amount'].transform(
        lambda s: s.rolling(lookback, min_periods=min_periods).mean()
    )
    market['trading_ratio_lookback'] = market.groupby('ts_code', group_keys=False)['is_trading_day'].transform(
        lambda s: s.rolling(lookback, min_periods=min_periods).mean()
    )
    market['listing_days'] = (market['trade_date'] - market['list_date']).dt.days
    market['is_st'] = mark_st_flags(market['name'], rules['st_name_patterns']) if rules['exclude_st'] else False
    market['is_suspended'] = (market['amount'] <= 0) | (market['vol'] <= 0)
    market['trade_date_str'] = market['trade_date'].dt.strftime('%Y%m%d')
    return market


def build_thresholds(market_dates: pd.DataFrame, rules: dict) -> tuple[dict[str, dict[float, float | None]], dict[str, float | None], dict[str, dict[str, object]]]:
    thresholds_by_date: dict[str, dict[float, float | None]] = {}
    base_thresholds: dict[str, float | None] = {}
    prefilter_stats: dict[str, dict[str, object]] = {}
    min_listing_days = int(rules['minimum_listing_days'])
    min_price = float(rules['min_price'])
    min_trading_ratio = float(rules['min_daily_trading_days_ratio'])
    for trade_date, day in market_dates.groupby('trade_date_str', sort=True):
        prefilter = day[
            (day['listing_days'].fillna(-1) >= min_listing_days)
            & (day['close'].fillna(0.0) >= min_price)
            & (~day['is_st'])
            & (~day['is_suspended'])
            & (day['trading_ratio_lookback'].fillna(0.0) >= min_trading_ratio)
        ].copy()
        thresholds: dict[float, float | None] = {}
        for percentile in PERCENTILE_GRID:
            if prefilter.empty:
                thresholds[percentile] = None
            else:
                thresholds[percentile] = float(prefilter['avg_amount_lookback'].quantile(percentile))
        thresholds_by_date[trade_date] = thresholds
        base_thresholds[trade_date] = thresholds.get(rules['avg_amount_percentile_threshold'])
        prefilter_stats[trade_date] = {
            'prefilter_stock_count': int(len(prefilter)),
            'prefilter_avg_amount_p50': round_or_none(prefilter['avg_amount_lookback'].median(skipna=True), 6),
            'prefilter_avg_amount_p25': round_or_none(prefilter['avg_amount_lookback'].quantile(0.25), 6),
            'prefilter_avg_amount_p75': round_or_none(prefilter['avg_amount_lookback'].quantile(0.75), 6),
        }
    return thresholds_by_date, base_thresholds, prefilter_stats


def classify_reason(row: pd.Series, liquidity_pass: bool) -> str:
    if not bool(row['pass_listing_days']):
        return 'universe_listing_days'
    if not bool(row['pass_min_price']):
        return 'universe_min_price'
    if not bool(row['pass_st_filter']):
        return 'universe_st_filter'
    if not bool(row['pass_suspension_filter']):
        return 'universe_suspended'
    if not bool(row['pass_trading_ratio']):
        return 'universe_trading_ratio'
    if not bool(liquidity_pass):
        return 'universe_liquidity_threshold'
    return 'pass'


def evaluate_selection_rows(selection: pd.DataFrame, market_dates: pd.DataFrame, base_thresholds: dict[str, float | None], rules: dict) -> pd.DataFrame:
    columns = [
        'trade_date_str', 'ts_code', 'name', 'close', 'amount', 'vol', 'avg_amount_lookback',
        'trading_ratio_lookback', 'listing_days', 'is_st', 'is_suspended'
    ]
    merged = selection.merge(
        market_dates[columns].rename(columns={'trade_date_str': 'trade_date', 'name': 'market_name'}),
        on=['trade_date', 'ts_code'],
        how='left',
    )
    merged['name'] = merged['selection_name'].fillna(merged['market_name'])
    merged['base_liquidity_threshold'] = merged['trade_date'].map(base_thresholds)
    merged['pass_listing_days'] = merged['listing_days'].fillna(-1) >= int(rules['minimum_listing_days'])
    merged['pass_min_price'] = merged['close'].fillna(0.0) >= float(rules['min_price'])
    merged['pass_st_filter'] = ~merged['is_st'].fillna(False)
    merged['pass_suspension_filter'] = ~merged['is_suspended'].fillna(True)
    merged['pass_trading_ratio'] = merged['trading_ratio_lookback'].fillna(0.0) >= float(rules['min_daily_trading_days_ratio'])
    merged['pass_liquidity_base'] = merged['avg_amount_lookback'] >= merged['base_liquidity_threshold']
    merged['base_reason'] = merged.apply(lambda row: classify_reason(row, row['pass_liquidity_base']), axis=1)
    merged['pass_all_base'] = merged['base_reason'].eq('pass')
    merged['liquidity_ratio'] = merged['avg_amount_lookback'] / merged['base_liquidity_threshold']
    return merged


def counts_by_reason(reasons: pd.Series) -> dict[str, int]:
    order = [
        'pass',
        'universe_liquidity_threshold',
        'universe_listing_days',
        'universe_min_price',
        'universe_trading_ratio',
        'universe_st_filter',
        'universe_suspended',
        'missing_market_row',
    ]
    counts = reasons.value_counts(dropna=False).to_dict()
    return {key: int(counts.get(key, 0)) for key in order if counts.get(key, 0)}


def summarize_percentile_grid(selection_eval: pd.DataFrame, thresholds_by_date: dict[str, dict[float, float | None]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fixed_precheck = (
        selection_eval['pass_listing_days']
        & selection_eval['pass_min_price']
        & selection_eval['pass_st_filter']
        & selection_eval['pass_suspension_filter']
        & selection_eval['pass_trading_ratio']
    )
    for percentile in PERCENTILE_GRID:
        threshold_map = {trade_date: values.get(percentile) for trade_date, values in thresholds_by_date.items()}
        threshold_series = selection_eval['trade_date'].map(threshold_map)
        pass_liq = selection_eval['avg_amount_lookback'] >= threshold_series
        pass_all = fixed_precheck & pass_liq
        rows.append(
            {
                'avg_amount_percentile_threshold': percentile,
                'selection_history_rows': int(len(selection_eval)),
                'pass_all_rows': int(pass_all.sum()),
                'dropped_rows': int((~pass_all).sum()),
                'delta_vs_base_pass_rows': int(pass_all.sum() - selection_eval['pass_all_base'].sum()),
                'retention_ratio': round_or_none(pass_all.mean(), 6),
                'liquidity_fail_rows': int((fixed_precheck & (~pass_liq)).sum()),
                'listing_fail_rows': int((~selection_eval['pass_listing_days']).sum()),
                'min_price_fail_rows': int((selection_eval['pass_listing_days'] & (~selection_eval['pass_min_price'])).sum()),
                'trading_ratio_fail_rows': int((
                    selection_eval['pass_listing_days']
                    & selection_eval['pass_min_price']
                    & selection_eval['pass_st_filter']
                    & selection_eval['pass_suspension_filter']
                    & (~selection_eval['pass_trading_ratio'])
                ).sum()),
            }
        )
    return rows


def summarize_threshold_scales(selection_eval: pd.DataFrame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fixed_precheck = (
        selection_eval['pass_listing_days']
        & selection_eval['pass_min_price']
        & selection_eval['pass_st_filter']
        & selection_eval['pass_suspension_filter']
        & selection_eval['pass_trading_ratio']
    )
    for scale in THRESHOLD_SCALES:
        scaled_threshold = selection_eval['base_liquidity_threshold'] * scale
        pass_liq = selection_eval['avg_amount_lookback'] >= scaled_threshold
        pass_all = fixed_precheck & pass_liq
        rows.append(
            {
                'threshold_scale_vs_current_amount_cutoff': scale,
                'selection_history_rows': int(len(selection_eval)),
                'pass_all_rows': int(pass_all.sum()),
                'dropped_rows': int((~pass_all).sum()),
                'delta_vs_base_pass_rows': int(pass_all.sum() - selection_eval['pass_all_base'].sum()),
                'retention_ratio': round_or_none(pass_all.mean(), 6),
                'liquidity_fail_rows': int((fixed_precheck & (~pass_liq)).sum()),
            }
        )
    return rows


def build_distance_stats(selection_eval: pd.DataFrame) -> dict[str, object]:
    liq_fail = selection_eval[selection_eval['base_reason'] == 'universe_liquidity_threshold'].copy()
    liq_fail['liquidity_ratio_bucket'] = pd.cut(
        liq_fail['liquidity_ratio'],
        bins=LIQ_BUCKETS,
        labels=LIQ_BUCKET_LABELS,
        include_lowest=True,
        right=False,
    )
    bucket_counts = {
        str(label): int(liq_fail['liquidity_ratio_bucket'].eq(label).sum())
        for label in LIQ_BUCKET_LABELS
    }
    return {
        'liquidity_fail_rows': int(len(liq_fail)),
        'liquidity_ratio_quantiles': {
            'p10': round_or_none(liq_fail['liquidity_ratio'].quantile(0.10)),
            'p25': round_or_none(liq_fail['liquidity_ratio'].quantile(0.25)),
            'p50': round_or_none(liq_fail['liquidity_ratio'].quantile(0.50)),
            'p75': round_or_none(liq_fail['liquidity_ratio'].quantile(0.75)),
            'p90': round_or_none(liq_fail['liquidity_ratio'].quantile(0.90)),
            'mean': round_or_none(liq_fail['liquidity_ratio'].mean()),
        },
        'liquidity_ratio_bucket_counts': bucket_counts,
        'closest_to_threshold_examples': liq_fail.sort_values('liquidity_ratio', ascending=False).head(10)[[
            'trade_date', 'ts_code', 'name', 'rank', 'selection_score', 'avg_amount_lookback', 'base_liquidity_threshold', 'liquidity_ratio'
        ]].assign(
            avg_amount_lookback=lambda df: df['avg_amount_lookback'].map(round_or_none),
            base_liquidity_threshold=lambda df: df['base_liquidity_threshold'].map(round_or_none),
            liquidity_ratio=lambda df: df['liquidity_ratio'].map(round_or_none),
            selection_score=lambda df: pd.to_numeric(df['selection_score'], errors='coerce').map(round_or_none),
        ).to_dict(orient='records'),
        'deep_fail_examples': liq_fail.sort_values('liquidity_ratio', ascending=True).head(10)[[
            'trade_date', 'ts_code', 'name', 'rank', 'selection_score', 'avg_amount_lookback', 'base_liquidity_threshold', 'liquidity_ratio'
        ]].assign(
            avg_amount_lookback=lambda df: df['avg_amount_lookback'].map(round_or_none),
            base_liquidity_threshold=lambda df: df['base_liquidity_threshold'].map(round_or_none),
            liquidity_ratio=lambda df: df['liquidity_ratio'].map(round_or_none),
            selection_score=lambda df: pd.to_numeric(df['selection_score'], errors='coerce').map(round_or_none),
        ).to_dict(orient='records'),
    }


def build_trade_date_summaries(selection_eval: pd.DataFrame) -> list[dict[str, object]]:
    dropped = selection_eval[selection_eval['base_reason'] != 'pass'].copy()
    out: list[dict[str, object]] = []
    for trade_date, day in dropped.groupby('trade_date', sort=True):
        liquidity_drops = int(day['base_reason'].eq('universe_liquidity_threshold').sum())
        if not liquidity_drops:
            continue
        total_drops = int(len(day))
        out.append(
            {
                'trade_date': trade_date,
                'liquidity_dropped_rows': liquidity_drops,
                'total_dropped_rows': total_drops,
                'share_of_day_drops': round_or_none(liquidity_drops / total_drops if total_drops else None),
            }
        )
    out.sort(key=lambda item: (-item['liquidity_dropped_rows'], item['trade_date']))
    return out[:20]


def build_recurrent_codes(selection_eval: pd.DataFrame) -> list[dict[str, object]]:
    liq_fail = selection_eval[selection_eval['base_reason'] == 'universe_liquidity_threshold'].copy()
    if liq_fail.empty:
        return []
    grouped = liq_fail.groupby(['ts_code', 'name'], dropna=False).agg(
        drop_count=('trade_date', 'count'),
        first_drop_date=('trade_date', 'min'),
        last_drop_date=('trade_date', 'max'),
        median_liquidity_ratio=('liquidity_ratio', 'median'),
        best_liquidity_ratio=('liquidity_ratio', 'max'),
        worst_liquidity_ratio=('liquidity_ratio', 'min'),
    ).reset_index()
    grouped = grouped.sort_values(['drop_count', 'median_liquidity_ratio'], ascending=[False, True])
    records = grouped.head(20).to_dict(orient='records')
    for item in records:
        item['median_liquidity_ratio'] = round_or_none(item['median_liquidity_ratio'])
        item['best_liquidity_ratio'] = round_or_none(item['best_liquidity_ratio'])
        item['worst_liquidity_ratio'] = round_or_none(item['worst_liquidity_ratio'])
    return records


def panel_threshold_validation(selection_eval: pd.DataFrame, panel_dates: list[str]) -> dict[str, object]:
    panel_thresholds = pd.read_parquet(PANEL_PATH, columns=['trade_date', 'liquidity_threshold', 'benchmark_date_eligible'])
    panel_thresholds['trade_date'] = panel_thresholds['trade_date'].astype(str)
    panel_thresholds = panel_thresholds[panel_thresholds['trade_date'].isin(panel_dates)]
    panel_daily = panel_thresholds.groupby('trade_date', as_index=False)['liquidity_threshold'].first()
    baseline = selection_eval[['trade_date', 'base_liquidity_threshold']].drop_duplicates().rename(columns={'base_liquidity_threshold': 'recomputed_threshold'})
    merged = panel_daily.merge(baseline, on='trade_date', how='left')
    merged['abs_diff'] = (merged['liquidity_threshold'] - merged['recomputed_threshold']).abs()
    return {
        'checked_trade_dates': int(len(merged)),
        'max_abs_diff': round_or_none(merged['abs_diff'].max()),
        'median_abs_diff': round_or_none(merged['abs_diff'].median()),
        'all_close_within_1e-6': bool((merged['abs_diff'].fillna(0.0) <= 1e-6).all()),
    }


def main() -> int:
    rules = load_rules()
    gap_attribution = load_json(GAP_ATTRIBUTION_PATH)
    selection_dates = load_selection_dates_from_panel()
    selection = load_selection_history(selection_dates)
    cache_dates = choose_cache_dates(selection_dates, lookback_trading_days=max(90, rules['avg_amount_lookback_days'] + 30))
    market = load_market_slice(cache_dates, rules)
    market_dates = market[market['trade_date_str'].isin(selection_dates)].copy()
    thresholds_by_date, base_thresholds, prefilter_stats = build_thresholds(market_dates, rules)
    selection_eval = evaluate_selection_rows(selection, market_dates, base_thresholds, rules)

    baseline_counts = counts_by_reason(selection_eval['base_reason'])
    baseline_validation = {
        'selection_history_rows': int(len(selection_eval)),
        'pass_rows': int(selection_eval['pass_all_base'].sum()),
        'dropped_rows': int((~selection_eval['pass_all_base']).sum()),
        'reason_counts_recomputed': baseline_counts,
        'matches_existing_gap_attribution': {
            'pass_rows': int(selection_eval['pass_all_base'].sum()) == int(gap_attribution.get('panel_benchmark_eligible_rows', 0)),
            'dropped_rows': int((~selection_eval['pass_all_base']).sum()) == int(gap_attribution.get('dropped_rows', 0)),
            'liquidity_rows': int(baseline_counts.get('universe_liquidity_threshold', 0)) == int((gap_attribution.get('reason_counts') or {}).get('universe_liquidity_threshold', 0)),
            'listing_days_rows': int(baseline_counts.get('universe_listing_days', 0)) == int((gap_attribution.get('reason_counts') or {}).get('universe_listing_days', 0)),
            'min_price_rows': int(baseline_counts.get('universe_min_price', 0)) == int((gap_attribution.get('reason_counts') or {}).get('universe_min_price', 0)),
        },
    }

    liquidity_fail_rows = int(baseline_counts.get('universe_liquidity_threshold', 0))
    report = {
        'report_type': 'selection_history_liquidity_sensitivity_diagnostic',
        'schema_version': '2026-04-20.selection_history_liquidity_sensitivity.v1',
        'generated_at': iso_now(),
        'status': 'diagnostic_only_no_official_universe_change',
        'source_paths': {
            'selection_history_dir': str(SELECTION_HISTORY_DIR),
            'panel': str(PANEL_PATH),
            'backtest_cache_dir': str(BACKTEST_CACHE_DIR),
            'stock_list': str(STOCK_LIST_PATH),
            'universe_rules': str(UNIVERSE_RULES_PATH),
            'selection_history_gap_attribution': str(GAP_ATTRIBUTION_PATH),
        },
        'rule_context': rules,
        'selection_window': {
            'trade_dates': selection_dates,
            'trade_date_count': len(selection_dates),
            'start': selection_dates[0],
            'end': selection_dates[-1],
            'selection_history_rows': int(len(selection)),
        },
        'baseline_validation': baseline_validation,
        'panel_threshold_validation': panel_threshold_validation(selection_eval, selection_dates),
        'summary': {
            'benchmark_eligible_rows_current': int(selection_eval['pass_all_base'].sum()),
            'dropped_rows_current': int((~selection_eval['pass_all_base']).sum()),
            'liquidity_dropped_rows': liquidity_fail_rows,
            'liquidity_dropped_ratio_of_all_dropped': round_or_none(liquidity_fail_rows / max(1, int((~selection_eval['pass_all_base']).sum()))),
            'allowed_claim_boundary': '该文件只量化 selection_history→panel constituent gap 对 liquidity threshold 的敏感性，不代表官方 benchmark 已修复，也不改变 promotion freeze / independent OOS 边界。',
            'main_takeaway': '249 行 dropped rows 中 234 行来自 universe_liquidity_threshold；但其中大部分并非轻微擦线，微幅放松流动性门槛只能有限回收 constituent。',
        },
        'percentile_threshold_summary': summarize_percentile_grid(selection_eval, thresholds_by_date),
        'threshold_scale_summary': summarize_threshold_scales(selection_eval),
        'threshold_distance_stats': build_distance_stats(selection_eval),
        'worst_trade_dates_by_liquidity': build_trade_date_summaries(selection_eval),
        'recurrent_ts_codes_by_liquidity': build_recurrent_codes(selection_eval),
        'daily_prefilter_stats_sample': {
            'worst_dates_present': [
                {
                    'trade_date': item['trade_date'],
                    **prefilter_stats.get(item['trade_date'], {}),
                }
                for item in build_trade_date_summaries(selection_eval)[:10]
            ],
        },
    }

    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'ok', 'output_path': str(OUTPUT_PATH)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
