#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from orchestrator_common import PUBLISHED_REPO, WORKSPACE

WORKING = WORKSPACE / 'stock_data' / '03-working'
CACHE_DIR = WORKING / 'backtest_cache'
ANALYTICS_DIR = PUBLISHED_REPO / 'data' / 'recommendation_analytics'
ENRICHED = WORKING / 'alpha_mining' / 'tushare_pro_demo' / 'tushare_enriched_latest.parquet'
INDUSTRY_RAW = WORKING / 'alpha_mining' / 'tushare_pro_demo' / 'raw' / 'industry_20260313.parquet'
OUT_JSON = ANALYTICS_DIR / 'market_industry_heatmap.json'


def pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    numeric = pd.to_numeric(series, errors='coerce')
    return numeric.rank(pct=True, method='average', ascending=ascending)


def load_industry_map() -> pd.DataFrame:
    if ENRICHED.exists():
        frame = pd.read_parquet(ENRICHED)
        cols = ['ts_code', 'industry_name']
        if all(c in frame.columns for c in cols):
            out = frame[cols].dropna().drop_duplicates('ts_code').copy()
            out['industry_name'] = out['industry_name'].replace('', pd.NA).fillna('未知')
            return out
    frame = pd.read_parquet(INDUSTRY_RAW)
    frame = frame.rename(columns={'industry': 'industry_name'})
    out = frame[['ts_code', 'industry_name']].dropna().drop_duplicates('ts_code').copy()
    out['industry_name'] = out['industry_name'].replace('', pd.NA).fillna('未知')
    return out


def load_market_rows(limit_dates: int = 30) -> pd.DataFrame:
    files = sorted(CACHE_DIR.glob('daily_*.parquet'))
    target_trade_date = str(os.environ.get('OPENCLAW_TARGET_TRADE_DATE') or '').strip()
    if len(target_trade_date) == 8 and target_trade_date.isdigit():
        files = [path for path in files if path.stem.rsplit('_', 1)[-1] <= target_trade_date]
    files = files[-limit_dates:]
    frames = []
    for path in files:
        df = pd.read_parquet(path)
        need = ['ts_code', 'trade_date', 'close', 'pct_chg', 'amount', 'vol']
        missing = [c for c in need if c not in df.columns]
        if missing:
            continue
        frames.append(df[need].copy())
    if not frames:
        raise RuntimeError('no daily parquet files loaded')
    merged = pd.concat(frames, ignore_index=True)
    merged['trade_date'] = merged['trade_date'].astype(str)
    for col in ['close', 'pct_chg', 'amount', 'vol']:
        merged[col] = pd.to_numeric(merged[col], errors='coerce')
    return merged


def build_hot_stocks(df: pd.DataFrame) -> pd.DataFrame:
    latest = df.sort_values(['trade_date', 'industry_name', 'pct_chg', 'amount'], ascending=[True, True, False, False])
    hot = latest.groupby(['trade_date', 'industry_name'], as_index=False).head(3).copy()
    hot['hot_stock'] = hot['ts_code']
    return hot.groupby(['trade_date', 'industry_name']).apply(
        lambda g: [
            {
                'ts_code': r.ts_code,
                'pct_chg': None if pd.isna(r.pct_chg) else round(float(r.pct_chg), 4),
                'amount': None if pd.isna(r.amount) else round(float(r.amount), 2),
            }
            for r in g.itertuples()
        ]
    ).reset_index(name='hot_stocks')


def main() -> int:
    industry = load_industry_map()
    market = load_market_rows(limit_dates=30)
    df = market.merge(industry, on='ts_code', how='left')
    df['industry_name'] = df['industry_name'].fillna('未知')

    grouped = (
        df.groupby(['trade_date', 'industry_name'], dropna=False)
        .agg(
            stock_count=('ts_code', 'count'),
            up_count=('pct_chg', lambda s: int((pd.to_numeric(s, errors='coerce') > 0).sum())),
            strong_count=('pct_chg', lambda s: int((pd.to_numeric(s, errors='coerce') >= 5).sum())),
            avg_pct_chg=('pct_chg', 'mean'),
            total_amount=('amount', 'sum'),
            avg_amount=('amount', 'mean'),
        )
        .reset_index()
        .sort_values(['trade_date', 'total_amount'], ascending=[True, False])
    )
    grouped['up_ratio'] = grouped['up_count'] / grouped['stock_count'].where(grouped['stock_count'] > 0, 1)
    grouped['strong_ratio'] = grouped['strong_count'] / grouped['stock_count'].where(grouped['stock_count'] > 0, 1)
    grouped['amount_share'] = grouped.groupby('trade_date')['total_amount'].transform(lambda s: s / s.sum())
    grouped['return_rank_pct'] = grouped.groupby('trade_date')['avg_pct_chg'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['up_rank_pct'] = grouped.groupby('trade_date')['up_ratio'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['amount_rank_pct'] = grouped.groupby('trade_date')['amount_share'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['strong_rank_pct'] = grouped.groupby('trade_date')['strong_ratio'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['market_heat_base'] = (
        0.35 * grouped['return_rank_pct'].fillna(0)
        + 0.25 * grouped['amount_rank_pct'].fillna(0)
        + 0.20 * grouped['up_rank_pct'].fillna(0)
        + 0.20 * grouped['strong_rank_pct'].fillna(0)
    )
    grouped = grouped.sort_values(['industry_name', 'trade_date']).reset_index(drop=True)
    grouped['market_heat_ema_5'] = grouped.groupby('industry_name')['market_heat_base'].transform(lambda s: s.ewm(span=5, adjust=False).mean())
    grouped['market_heat_delta_1d'] = grouped.groupby('industry_name')['market_heat_ema_5'].diff()
    grouped['market_heat_slope_3d'] = grouped.groupby('industry_name')['market_heat_ema_5'].transform(lambda s: s.diff(3) / 3.0)
    grouped['market_heat_rank'] = grouped.groupby('trade_date')['market_heat_ema_5'].rank(method='average', ascending=False)

    def classify(row):
        s3 = row.get('market_heat_slope_3d')
        d1 = row.get('market_heat_delta_1d')
        if pd.isna(s3):
            return '数据不足'
        if s3 > 0.03:
            return '升温'
        if s3 < -0.03:
            return '降温'
        if pd.notna(d1) and abs(d1) > 0.06:
            return '拐点'
        return '平稳'

    grouped['trend_signal'] = grouped.apply(classify, axis=1)
    hot = build_hot_stocks(df)
    grouped = grouped.merge(hot, on=['trade_date', 'industry_name'], how='left')
    grouped = grouped.astype(object).where(pd.notna(grouped), None)

    latest = grouped['trade_date'].max()
    top_sectors = grouped[grouped['trade_date'] == latest].sort_values('market_heat_ema_5', ascending=False)['industry_name'].head(20).tolist()
    output = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'kind': 'market_industry_heatmap',
        'latest_trade_date': latest,
        'date_range': {
            'from': grouped['trade_date'].min(),
            'to': latest,
            'date_count': int(grouped['trade_date'].nunique()),
        },
        'top_sectors_latest': top_sectors,
        'rows': grouped.to_dict(orient='records'),
    }
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(f'created={OUT_JSON}')
    print(f'rows={len(grouped)} latest={latest} sectors={len(top_sectors)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
