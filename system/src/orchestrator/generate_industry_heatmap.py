#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
from orchestrator_common import PUBLISHED_REPO, WORKSPACE

ANALYTICS_DIR = PUBLISHED_REPO / 'data' / 'recommendation_analytics'
LOCAL_WAREHOUSE_EXPORT_DIR = WORKSPACE / 'stock_data' / '03-working' / 'recommendation_warehouse' / 'exports'
DETAIL_JSON = LOCAL_WAREHOUSE_EXPORT_DIR / 'prebreakout_recommendations.json'
SUMMARY_JSON = LOCAL_WAREHOUSE_EXPORT_DIR / 'prebreakout_summary.json'
OUT_JSON = ANALYTICS_DIR / 'industry_heatmap.json'


def pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    numeric = pd.to_numeric(series, errors='coerce')
    return numeric.rank(pct=True, method='average', ascending=ascending)


def main() -> int:
    detail = json.loads(DETAIL_JSON.read_text(encoding='utf-8'))
    summary = json.loads(SUMMARY_JSON.read_text(encoding='utf-8'))
    rows = detail.get('rows', [])
    if not rows:
        raise RuntimeError('prebreakout_recommendations.json rows empty')

    df = pd.DataFrame(rows)
    df['recommend_date'] = df['recommend_date'].astype(str)
    df['sector_name'] = df['sector_name'].fillna('未知')
    for col in ['ai_score', 'next_day_return_pct', 'cumulative_return_pct', 'cumulative_recommend_count', 'rank_no']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    grouped = (
        df.groupby(['recommend_date', 'sector_name'], dropna=False)
        .agg(
            recommendation_count=('stock_code', 'count'),
            unique_stock_count=('stock_code', 'nunique'),
            avg_ai_score=('ai_score', 'mean'),
            avg_next_day_return_pct=('next_day_return_pct', 'mean'),
            avg_cumulative_return_pct=('cumulative_return_pct', 'mean'),
            avg_repeat_count=('cumulative_recommend_count', 'mean'),
            best_rank=('rank_no', 'min'),
        )
        .reset_index()
        .sort_values(['recommend_date', 'recommendation_count', 'avg_cumulative_return_pct'], ascending=[True, False, False])
    )

    grouped['count_pct'] = grouped.groupby('recommend_date')['recommendation_count'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['ai_score_pct'] = grouped.groupby('recommend_date')['avg_ai_score'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['next_day_pct'] = grouped.groupby('recommend_date')['avg_next_day_return_pct'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['cum_return_pct'] = grouped.groupby('recommend_date')['avg_cumulative_return_pct'].transform(lambda s: pct_rank(s, ascending=True))
    grouped['repeat_pct'] = grouped.groupby('recommend_date')['avg_repeat_count'].transform(lambda s: pct_rank(s, ascending=True))

    grouped['heat_base'] = (
        0.30 * grouped['count_pct'].fillna(0)
        + 0.20 * grouped['ai_score_pct'].fillna(0)
        + 0.20 * grouped['next_day_pct'].fillna(0)
        + 0.20 * grouped['cum_return_pct'].fillna(0)
        + 0.10 * grouped['repeat_pct'].fillna(0)
    )
    grouped = grouped.sort_values(['sector_name', 'recommend_date']).reset_index(drop=True)
    grouped['heat_ema_5'] = grouped.groupby('sector_name')['heat_base'].transform(lambda s: s.ewm(span=5, adjust=False).mean())
    grouped['heat_delta_1d'] = grouped.groupby('sector_name')['heat_ema_5'].diff()
    grouped['heat_rank'] = grouped.groupby('recommend_date')['heat_ema_5'].rank(method='average', ascending=False)
    grouped['heat_slope_3d'] = grouped.groupby('sector_name')['heat_ema_5'].transform(lambda s: s.diff(3) / 3.0)
    grouped['heat_slope_5d'] = grouped.groupby('sector_name')['heat_ema_5'].transform(lambda s: s.diff(5) / 5.0)
    grouped['cumret_slope_3d'] = grouped.groupby('sector_name')['avg_cumulative_return_pct'].transform(lambda s: s.diff(3) / 3.0)
    grouped['rank_change_3d'] = grouped.groupby('sector_name')['heat_rank'].transform(lambda s: s.shift(3) - s)

    def classify(row):
        slope3 = row.get('heat_slope_3d')
        slope5 = row.get('heat_slope_5d')
        delta1 = row.get('heat_delta_1d')
        if pd.isna(slope3) or pd.isna(slope5):
            return '数据不足'
        if slope3 > 0.03 and slope5 > 0.015:
            return '升温'
        if slope3 < -0.03 and slope5 < -0.015:
            return '降温'
        if pd.notna(delta1) and abs(delta1) > 0.06 and slope5 * delta1 < 0:
            return '拐点'
        return '平稳'

    grouped['trend_signal'] = grouped.apply(classify, axis=1)

    rep = (
        df.sort_values(['recommend_date', 'sector_name', 'rank_no', 'ai_score'], ascending=[True, True, True, False])
          .groupby(['recommend_date', 'sector_name'], as_index=False)
          .first()[['recommend_date','sector_name','stock_code','stock_name','ai_view','ai_score']]
          .rename(columns={'stock_code':'represent_stock_code','stock_name':'represent_stock_name','ai_view':'represent_ai_view','ai_score':'represent_ai_score'})
    )
    grouped = grouped.merge(rep, on=['recommend_date','sector_name'], how='left')

    latest_date = summary.get('latest_recommend_date')
    latest_rows = grouped.loc[grouped['recommend_date'] == str(latest_date)].copy()
    latest_rows = latest_rows.sort_values(['heat_ema_5', 'recommendation_count'], ascending=[False, False])
    top_sectors = latest_rows['sector_name'].head(20).tolist()

    grouped = grouped.astype(object).where(pd.notna(grouped), None)
    output = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy_id': 'prebreakout_v41',
        'strategy_name': '启动前夕 v4.3 对照',
        'date_range': summary.get('date_range'),
        'latest_recommend_date': latest_date,
        'latest_price_date': summary.get('latest_price_date'),
        'top_sectors_latest': top_sectors,
        'rows': grouped.to_dict(orient='records'),
    }
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(f'created={OUT_JSON}')
    print(f'rows={len(grouped)}')
    print(f'latest={latest_date}')
    print(f'top_sectors={top_sectors[:10]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
