#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
WORKSPACE = Path('/Users/fisher/.openclaw/workspace')
STOCK_DATA = WORKSPACE / 'stock_data' / '03-working'
BACKTEST_CACHE = STOCK_DATA / 'backtest_cache'
HEALTH = STOCK_DATA / 'health'
CANONICAL = HEALTH / 'canonical'
REPORTS = WORKSPACE / 'stock-report' / 'data' / 'recommendation_analytics'
EVOLUTION = ROOT / 'outputs' / 'evolution'
EVOLUTION.mkdir(parents=True, exist_ok=True)

FEATURE_COLS = [
    'market_return_mean',
    'breadth_ratio',
    'amount_ratio_median',
    'hl_range_mean',
    'small_minus_large',
    'top5_contribution_share',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build factor evolution context artifacts')
    parser.add_argument(
        '--target-trade-date',
        default='',
        help='Optional YYYYMMDD trade date to pin current_market_fingerprint/regime_analog_windows. Defaults to latest available raw-cache trade date.',
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def round_float(value: Any, digits: int = 6) -> Any:
    try:
        value = float(value)
    except Exception:
        return value
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def percentile_rank(series: pd.Series, lookback: int) -> float | None:
    window = series.dropna().tail(min(lookback, len(series.dropna())))
    if window.empty:
        return None
    return float((window <= window.iloc[-1]).mean())


def max_drawdown(series: pd.Series) -> float | None:
    s = series.dropna()
    if len(s) < 2:
        return None
    peak = s.cummax()
    dd = s / peak - 1.0
    return float(dd.min())


def choose_primary_regime(mean_ret: float, amount_ratio: float, hl_mean: float, breadth: float) -> tuple[str, float]:
    if abs(mean_ret) < 0.002 and hl_mean >= 0.05:
        return 'high_vol_chop', min(0.95, 0.55 + (hl_mean - 0.05) * 4)
    if amount_ratio >= 1.05 and mean_ret >= 0.002:
        return 'volume_uptrend', min(0.95, 0.55 + (amount_ratio - 1.05) * 1.5 + mean_ret * 25)
    if amount_ratio >= 1.05 and mean_ret < 0.002:
        return 'volume_downtrend', min(0.95, 0.55 + (amount_ratio - 1.05) * 1.2 + (0.55 - breadth) * 1.5)
    if amount_ratio < 1.05 and mean_ret >= 0.0:
        return 'shrink_uptrend', min(0.95, 0.45 + (1.05 - amount_ratio) * 0.8 + breadth * 0.5)
    return 'shrink_downtrend', 0.45 + min(0.4, (0.55 - breadth) * 1.4 + abs(mean_ret) * 20)


def choose_style_regime(small_minus_large: float) -> tuple[str, float]:
    if small_minus_large >= 0.004:
        return 'small_cap_rotation', min(0.9, 0.55 + small_minus_large * 25)
    if small_minus_large <= -0.004:
        return 'large_cap_rotation', min(0.9, 0.55 + abs(small_minus_large) * 25)
    target = 'small_cap_rotation' if small_minus_large >= 0 else 'large_cap_rotation'
    return target, 0.35 + min(0.15, abs(small_minus_large) * 10)


def choose_sector_regime(top5_share: float) -> tuple[str, float]:
    if top5_share >= 0.42:
        return 'sector_concentration', min(0.95, 0.55 + (top5_share - 0.42) * 2)
    if top5_share <= 0.25:
        return 'sector_dispersion', min(0.95, 0.55 + (0.25 - top5_share) * 2)
    if top5_share >= 0.335:
        return 'sector_concentration', 0.45 + (top5_share - 0.335) * 1.2
    return 'sector_dispersion', 0.45 + (0.335 - top5_share) * 1.2


def build_recent_market_summary(last_hist_date: str, lookback_files: int = 80) -> pd.DataFrame:
    paths = sorted(BACKTEST_CACHE.glob('daily_*.parquet'))[-lookback_files:]
    if not paths:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pct_chg', 'amount']
    for path in paths:
        try:
            df = pd.read_parquet(path, columns=cols)
        except Exception:
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df['trade_date'] = df['trade_date'].astype(str)
    df = df.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    for col in ['open', 'high', 'low', 'close', 'pct_chg', 'amount']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['ret_1d'] = df['pct_chg'].fillna(0.0) / 100.0
    df['amount_ma_20'] = df.groupby('ts_code')['amount'].transform(lambda s: s.rolling(20, min_periods=5).mean())
    df['avg_amount_lookback'] = df['amount_ma_20']
    df['hl_range'] = ((df['high'] - df['low']) / df['close'].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    df['intraday_return'] = (df['close'] / df['open'].replace(0, np.nan) - 1.0).replace([np.inf, -np.inf], np.nan)
    df['high_spike'] = (df['high'] / df['open'].replace(0, np.nan) - 1.0).replace([np.inf, -np.inf], np.nan)
    df['close_max_60'] = df.groupby('ts_code')['close'].transform(lambda s: s.rolling(60, min_periods=20).max())
    df['close_min_60'] = df.groupby('ts_code')['close'].transform(lambda s: s.rolling(60, min_periods=20).min())

    rows: list[dict[str, Any]] = []
    for trade_date, day in df.groupby('trade_date', sort=True):
        mean_ret = float(day['ret_1d'].mean())
        median_ret = float(day['ret_1d'].median())
        breadth = float((day['ret_1d'] > 0).mean())
        amount_ratio = float((day['amount'] / day['amount_ma_20']).replace([np.inf, -np.inf], np.nan).median(skipna=True) or 0.0)
        hl_mean = float(day['hl_range'].mean())

        q_low = day['avg_amount_lookback'].quantile(0.25)
        q_high = day['avg_amount_lookback'].quantile(0.75)
        small_bucket = day[day['avg_amount_lookback'] <= q_low]
        large_bucket = day[day['avg_amount_lookback'] >= q_high]
        small_minus_large = float(small_bucket['ret_1d'].mean() - large_bucket['ret_1d'].mean()) if not small_bucket.empty and not large_bucket.empty else 0.0

        contribution = (day['amount'].abs().fillna(0.0) * day['ret_1d'].abs().fillna(0.0)).sort_values(ascending=False)
        top5_share = float(contribution.head(5).sum() / contribution.sum()) if contribution.sum() > 0 else 0.0

        primary_id, primary_conf = choose_primary_regime(mean_ret, amount_ratio, hl_mean, breadth)
        style_id, style_conf = choose_style_regime(small_minus_large)
        sector_id, sector_conf = choose_sector_regime(top5_share)
        overall_conf = float(np.clip(np.mean([primary_conf, style_conf, sector_conf]), 0.0, 1.0))

        reversal_flag = ((day['high_spike'].fillna(0.0) > 0.02) & (day['intraday_return'].fillna(0.0) < 0)).mean()
        rows.append(
            {
                'trade_date': trade_date,
                'regime_id': primary_id,
                'regime_confidence': overall_conf,
                'market_return_mean': mean_ret,
                'market_return_median': median_ret,
                'breadth_ratio': breadth,
                'amount_ratio_median': amount_ratio,
                'hl_range_mean': hl_mean,
                'small_minus_large': small_minus_large,
                'top5_contribution_share': top5_share,
                'regime_components': json.dumps(
                    {
                        'primary': {'id': primary_id, 'confidence': primary_conf},
                        'style': {'id': style_id, 'confidence': style_conf},
                        'sector': {'id': sector_id, 'confidence': sector_conf},
                    },
                    ensure_ascii=False,
                ),
                'stock_count': int(len(day)),
                'up_count': int((day['ret_1d'] > 0).sum()),
                'down_count': int((day['ret_1d'] < 0).sum()),
                'limit_up_count_proxy': int((day['pct_chg'].fillna(0.0) >= 9.8).sum()),
                'limit_down_count_proxy': int((day['pct_chg'].fillna(0.0) <= -9.8).sum()),
                'new_high_count_proxy_60d': int((day['close'] >= day['close_max_60']).fillna(False).sum()),
                'new_low_count_proxy_60d': int((day['close'] <= day['close_min_60']).fillna(False).sum()),
                'intraday_reversal_freq_proxy': float(reversal_flag),
                'amount_total': float(day['amount'].fillna(0.0).sum()),
            }
        )

    summary = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    return summary[summary['trade_date'] > last_hist_date].reset_index(drop=True)


def similarity_windows(regime_hist: pd.DataFrame, current_row: dict[str, Any], weight_map: dict[str, Any], audit: dict[str, Any]) -> list[dict[str, Any]]:
    hist = regime_hist.copy()
    current_trade_date = str(current_row.get('trade_date') or '')
    if current_trade_date:
        hist = hist[hist['trade_date'].astype(str) != current_trade_date].copy()
    hist = hist.sort_values('trade_date').reset_index(drop=True)
    if hist.empty:
        return []
    hist_numeric = hist[FEATURE_COLS].apply(pd.to_numeric, errors='coerce')
    std = hist_numeric.std().replace(0, 1.0).fillna(1.0)
    current_vec = pd.Series({k: float(current_row.get(k, 0.0) or 0.0) for k in FEATURE_COLS})

    reports = {r.get('factor_id'): r for r in audit.get('reports', [])}
    cold_reports = [r for r in audit.get('reports', []) if r.get('status') != 'whitelist']
    cold_reports = sorted(cold_reports, key=lambda r: (float(r.get('sharpe_lift', -999)), float(r.get('calmar_lift', -999))))
    worst_cold_ids = [r.get('factor_id') for r in cold_reports[:6] if r.get('factor_id')]
    regime_map = {item.get('regime_id'): item for item in weight_map.get('regime_weight_map', [])}
    market_proxy = (1.0 + hist['market_return_mean'].astype(float).fillna(0.0)).cumprod() * 100.0

    rows: list[dict[str, Any]] = []
    for idx, row in hist.iterrows():
        z = ((hist_numeric.loc[idx] - current_vec) / std).abs()
        distance = float(np.sqrt(np.square(z).sum()))
        similarity = float(1.0 / (1.0 + distance))
        same_regime = row.get('regime_id') == current_row.get('regime_id')
        same_style = False
        try:
            cur_comp = json.loads(current_row.get('regime_components', '{}'))
            hist_comp = json.loads(row.get('regime_components', '{}'))
            same_style = cur_comp.get('style', {}).get('id') == hist_comp.get('style', {}).get('id')
        except Exception:
            pass

        future = {}
        for horizon in [5, 20, 60]:
            if idx + horizon < len(market_proxy):
                future[horizon] = float(market_proxy.iloc[idx + horizon] / market_proxy.iloc[idx] - 1.0)
            else:
                future[horizon] = None

        matched_dims = [
            {'feature': feat, 'abs_z_diff': round_float(z[feat], 4)}
            for feat in sorted(FEATURE_COLS, key=lambda c: z[c])[:3]
        ]
        diverged_dims = [
            {'feature': feat, 'abs_z_diff': round_float(z[feat], 4)}
            for feat in sorted(FEATURE_COLS, key=lambda c: z[c], reverse=True)[:2]
        ]

        rm = regime_map.get(row.get('regime_id'), {})
        selected = rm.get('selected_factors', [])
        effective = [
            {
                'factor_id': item.get('factor_id'),
                'selection_source': item.get('selection_source'),
                'weight': round_float(item.get('weight'), 6),
            }
            for item in selected
        ]
        reference = 'high' if similarity >= 0.68 and same_regime else 'medium' if similarity >= 0.62 else 'low'
        rows.append(
            {
                'trade_date': str(row.get('trade_date')),
                'window_start': str(hist.iloc[max(0, idx - 2)]['trade_date']),
                'window_end': str(hist.iloc[min(len(hist) - 1, idx + 2)]['trade_date']),
                'similarity_score': round_float(similarity, 6),
                'distance': round_float(distance, 6),
                'regime_id': row.get('regime_id'),
                'regime_confidence': round_float(row.get('regime_confidence'), 6),
                'similar_reasons': {
                    'same_primary_regime': same_regime,
                    'same_style_regime': same_style,
                    'matched_dimensions': matched_dims,
                    'diverged_dimensions': diverged_dims,
                },
                'subsequent_market_proxy_returns': {
                    '5d': round_float(future[5], 6),
                    '20d': round_float(future[20], 6),
                    '60d': round_float(future[60], 6),
                },
                'effective_factor_proxies': effective,
                'ineffective_factor_proxies': worst_cold_ids[:3],
                'current_reference_level': reference,
                'method_caveat': 'effective/ineffective factors are current regime-map proxies, not reconstructed historical window-specific realized factor attribution.',
            }
        )
    rows.sort(key=lambda x: x['similarity_score'], reverse=True)
    return rows[:10]


def build_hypotheses(current: dict[str, Any], morning: dict[str, Any], weight_map: dict[str, Any]) -> list[dict[str, Any]]:
    focus = morning.get('focus_sectors', [])[:5]
    top_sectors = current.get('style_and_sector', {}).get('heatmap_top_sectors', [])[:5]
    breadth = current.get('trend_state', {}).get('broad_market_proxy', {}).get('breadth_ratio') or 0.0
    amount_pct = current.get('turnover_and_liquidity', {}).get('broad_market_proxy', {}).get('amount_total_percentile_80d') or 0.0
    high_position = current.get('index_positions', {}).get('broad_market_equal_weight_proxy', {}).get('close_percentile_1y') or 0.0
    small_minus_large = current.get('style_and_sector', {}).get('broad_market_proxy', {}).get('small_minus_large') or 0.0
    fallback_count = weight_map.get('fallback_pool_count', 0)
    latest_selected = weight_map.get('latest_weight_plan', {}).get('selected_factors', []) or []
    latest_factor_ids = [str(item.get('factor_id')) for item in latest_selected if item.get('factor_id')]
    support_ids = [str(item.get('factor_id')) for item in latest_selected if item.get('selection_source') == 'support_pool' and item.get('factor_id')]
    fallback_ids = [
        str(item.get('factor_id'))
        for item in latest_selected
        if str(item.get('selection_source') or '').startswith('fallback') and item.get('factor_id')
    ]
    latest_factor_desc = ' / '.join(latest_factor_ids) if latest_factor_ids else 'latest_weight_plan 尚未产出'
    support_desc = ' / '.join(support_ids) if support_ids else '当前尚无 support_pool 因子'
    fallback_desc = ' / '.join(fallback_ids) if fallback_ids else '当前尚无 fallback 因子'

    return [
        {
            'hypothesis_id': 'h01-tech-breadth-momentum-confirmation',
            'target_family': '价格动量与趋势',
            'action_bias': 'selective_increase' if breadth < 0.5 else 'increase',
            'priority': 'P0',
            'rationale': f'当前全市场广度={round_float(breadth,4)}并不算强，但资金焦点集中在{focus or top_sectors}；说明更像结构性科技/设备主线，而不是全面普涨，不宜只吃单一启动信号。',
            'candidate_signals': ['短中期动量', '突破强度', '相对强度', '52周高点强度'],
            'validation_hint': '先做 close-to-close 与 open-to-close 双口径对照，再看样本外是否仍优于基准。',
        },
        {
            'hypothesis_id': 'h02-liquidity-followthrough-after-expansion',
            'target_family': '流动性与成交结构',
            'action_bias': 'increase',
            'priority': 'P0',
            'rationale': f'成交强度分位={round_float(amount_pct,4)}，当前 latest_weight_plan={latest_factor_desc}；其中 support_pool={support_desc}、fallback={fallback_desc}，说明量能跟随应围绕最新活跃层而不是旧版 f_volume_ratio_5 / 20 叙述继续扩展。',
            'candidate_signals': ['成交额变化', '量能压缩后放大', '换手率变化', '资金拥挤度'],
            'validation_hint': f'重点比较 latest_weight_plan 中与量能/启动相关因子的 open-to-close 可交易收益与 close-to-close 差异；当前优先核验 {support_desc}。',
        },
        {
            'hypothesis_id': 'h03-style-rotation-overlay',
            'target_family': '行业与市场状态',
            'action_bias': 'increase' if small_minus_large >= 0 else 'decrease_small_cap_increase_large_cap',
            'priority': 'P1',
            'rationale': f'small_minus_large={round_float(small_minus_large,6)}；当前 style 更接近{"小盘/成长轮动" if small_minus_large >= 0 else "大盘/权重修复"}，需要单独建 style overlay，而不是把风格切换混进通用动量因子。',
            'candidate_signals': ['大盘/小盘强弱', '成长/价值切换', '行业强弱扩散'],
            'validation_hint': '在 analog windows 中比较 style rotation 对未来 20/60 日表现的解释力。',
        },
        {
            'hypothesis_id': 'h04-high-position-risk-filter',
            'target_family': '低波动与防守',
            'action_bias': 'increase',
            'priority': 'P0',
            'rationale': f'当前 broad market 1y 位置分位={round_float(high_position,4)}，高位环境下必须先补风险过滤，再谈 aggressive promote。',
            'candidate_signals': ['低波动', '低回撤', '高位防守能力', '抗跌能力'],
            'validation_hint': '先解决 f_atr_proxy_14 的 fallback 残留依赖，再看能否升为干净 support/whitelist 候选。',
        },
        {
            'hypothesis_id': 'h05-sector-dispersion-without-single-theme-capture',
            'target_family': '行业与市场状态',
            'action_bias': 'increase',
            'priority': 'P1',
            'rationale': '当前 regime map latest 含 sector_dispersion 成分，说明组合不能被单一科技主题绑架，需做行业暴露约束。',
            'candidate_signals': ['行业集中度约束', '主题扩散确认', '行业相对强弱'],
            'validation_hint': '组合级回测必须加入行业中性或行业暴露对照。',
        },
        {
            'hypothesis_id': 'h06-gap-signal-execution-decomposition',
            'target_family': '启动前夕与事件结构',
            'action_bias': 'watch',
            'priority': 'P0',
            'rationale': '当前 f_gap_return 很强，但它与 prebreakout_v41 一起造成高集中；必须先分解成交时点收益，不可直接继续加权。',
            'candidate_signals': ['缺口修复', '突破前成交确认', '事件后资金确认'],
            'validation_hint': '强制并排输出 close-to-close / open-to-close / close-to-open 三口径。',
        },
        {
            'hypothesis_id': 'h07-chip-support-on-pullback-not-breakout',
            'target_family': '筹码与成本结构',
            'action_bias': 'watch',
            'priority': 'P1',
            'rationale': '筹码家族当前更多停留在 support/fallback 层，适合当回踩确认工具，不适合在验证体系未修前直接主导组合。',
            'candidate_signals': ['成本带宽', '获利盘比例', '成本中枢偏离', '筹码集中/发散'],
            'validation_hint': '优先核验 f_chip_support_proxy / f_chip_band_width 在 defensive 与 sector-dispersion 窗口中的真实 OOS。',
        },
        {
            'hypothesis_id': 'h08-quality-and-cashflow-data-gap-should-not-be-faked',
            'target_family': '质量与盈利能力',
            'action_bias': 'build_data_first',
            'priority': 'P0',
            'rationale': '当前 fundamentals_event_filters 多是 proxy，不是真实财务因子。质量/现金流家族必须先补数据，不要用 proxy 冒充完成覆盖。',
            'candidate_signals': ['ROE', '净利率', '现金流质量', '盈利稳定性'],
            'validation_hint': '先做数据可用性报告，再决定是否纳入 core/active。',
        },
        {
            'hypothesis_id': 'h09-valuation-family-keep-watch-until-real-source',
            'target_family': '估值与价值',
            'action_bias': 'watch',
            'priority': 'P1',
            'rationale': '当前估值环境本地缺完整指数/行业估值分位，不能把 pseudo valuation proxy 当作真正价值因子。',
            'candidate_signals': ['PE/PB/PS', '行业相对估值', '估值分位数', '低估值修复'],
            'validation_hint': '缺真实估值源前只进 watch，不参与 promote。',
        },
        {
            'hypothesis_id': 'h10-fallback-reduction-before-whitelist-expansion',
            'target_family': '组合结构约束',
            'action_bias': 'decrease_fallback_first',
            'priority': 'P0',
            'rationale': f'当前 fallback_pool_count={fallback_count}，防守行情仍依赖 fallback_coldlist；先收口 fallback，再扩 whitelist 更稳。',
            'candidate_signals': ['defensive overlay', 'support-to-whitelist promotion gate', 'portfolio concentration cap'],
            'validation_hint': 'promotion freeze 不解除前，不新增正式 promote。',
        },
    ]


def render_daily_digest(current: dict[str, Any], analogs: dict[str, Any], hypotheses: dict[str, Any], validation: dict[str, Any], portfolio: dict[str, Any], overfit: dict[str, Any], promotion: dict[str, Any]) -> str:
    top_analogs = analogs.get('analog_windows', [])[:3]
    top_lines = '\n'.join(
        f"- {item['trade_date']} | 相似度 {item['similarity_score']} | regime={item['regime_id']} | 5/20/60d={item['subsequent_market_proxy_returns']['5d']}/{item['subsequent_market_proxy_returns']['20d']}/{item['subsequent_market_proxy_returns']['60d']}"
        for item in top_analogs
    ) or '- 暂无'
    top_hyp = '\n'.join(
        f"- {item['hypothesis_id']} | {item['target_family']} | {item['action_bias']} | {item['rationale']}"
        for item in hypotheses.get('hypotheses', [])[:5]
    ) or '- 暂无'
    focus = current.get('style_and_sector', {}).get('focus_sectors', [])
    factor_state = current.get('factor_factory_state', {})
    severe_gaps = validation.get('three_most_severe_gaps', [])
    severe_gap_lines = '\n'.join(f'- {x}' for x in severe_gaps) or '- 暂无'
    return f"# Daily Factor Evolution Digest\n\n- 生成时间：{current.get('generated_at')}\n- 当前交易日：{current.get('trade_date')}\n- 市场状态：{current.get('market_regime_summary', {}).get('morning_brief_regime')}\n- 关注行业：{', '.join(focus) if focus else '暂无'}\n\n## 当前组合状态\n- factor_count={factor_state.get('factor_count')}\n- whitelist_count={factor_state.get('whitelist_count')}\n- support_pool_count={factor_state.get('support_pool_count')}\n- fallback_pool_count={factor_state.get('fallback_pool_count')}\n- latest_regime={factor_state.get('latest_weight_map_regime')}\n- promotion_freeze={promotion.get('global_constraints', {}).get('promotion_freeze')}\n\n## 今日最关键验证缺口\n{severe_gap_lines}\n\n## Top 3 历史相似窗口\n{top_lines}\n\n## 当前优先假设\n{top_hyp}\n\n## 组合级风险\n- {overfit.get('concentration_risk', {}).get('conclusion')}\n- {overfit.get('fallback_coldlist_residual_dependency', {}).get('conclusion')}\n- {portfolio.get('current_portfolio_structure', {}).get('selection_mode')} 仍未完成组合级 OOS / analog validation。\n\n## 下一步\n- 先修 benchmark_eligible 与收益/成交口径。\n- 再补最终组合级回测、独立 OOS、analog validation。\n- promotion freeze 解除前，不把任何候选因子宣布为正式晋升成功。\n"


def render_weekly_review(current: dict[str, Any], validation: dict[str, Any], overfit: dict[str, Any]) -> str:
    return f"# Weekly Factor Evolution Review\n\n## 本周状态（初始化草案）\n- 生成时间：{current.get('generated_at')}\n- 周度结论：当前仍处于“验证体系重建期”，不具备宣称系统进化有效的证据。\n\n## 已确认事实\n- 当前系统仍是 prebreakout_v41 fixed-base 架构。\n- base benchmark 被污染，真实 clean 对照仅覆盖极少日期。\n- 组合级 OOS / nested walk-forward / analog validation 尚未完成。\n\n## 本周主要风险\n- {validation.get('three_most_severe_gaps', ['暂无'])[0]}\n- {overfit.get('concentration_risk', {}).get('conclusion')}\n- {overfit.get('fallback_coldlist_residual_dependency', {}).get('conclusion')}\n\n## 本周最低完成标准\n- 输出 clean benchmark eligibility 掩码方案。\n- 补齐 close-to-close vs open-to-close 对照。\n- 产出最终组合级可运行回测。\n- 至少完成一版 analog validation 可运行骨架。\n"


def render_skill_draft() -> str:
    return "# Skill Draft: factor-evolution-lab\n\n## 目标\n构建 A 股多因子组合自进化系统：自动更新市场画像、历史相似窗口、因子迭代池、组合级验证与反过拟合审计。\n\n## 输入\n- factor_factory canonical audit / weight / attribution\n- market_state / market_snapshot / market_morning_brief / market_industry_heatmap\n- recommendation warehouse / selection_history\n- strategy_backtests / strategy_suite_run\n\n## 核心工作流\n1. 读取当前状态与上一轮产物\n2. 更新 current_market_fingerprint\n3. 更新 regime_analog_windows\n4. 更新 factor_iteration_pool / factor_lineage\n5. 生成 candidate_factor_hypotheses\n6. 运行 factor_validation_report / portfolio_backtest_report / overfit_audit_report\n7. 输出 factor_promotion_decision\n8. 渲染 daily / weekly digest\n\n## 当前已知强约束\n- clean benchmark 未修复前，promotion freeze 默认开启\n- close-to-close vs open-to-close 必须并排输出\n- 没有组合级 OOS / analog validation 不允许宣称进化有效\n- 不允许单一辅助因子长期占辅助层超过 50%\n\n## 后续待补\n- 守护任务 30m 运行协议\n- 真实指数/估值数据接入\n- 最终组合级回测执行器\n- nested walk-forward / analog validation 执行器\n"


def main() -> int:
    args = parse_args()
    generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
    audit = load_json(ROOT / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json')
    weight_map = load_json(ROOT / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json')
    attribution = load_json(ROOT / 'outputs' / 'attribution_reports' / 'phase1_attribution_report.json')
    factor_validation = load_json(EVOLUTION / 'factor_validation_report.json')
    portfolio = load_json(EVOLUTION / 'portfolio_backtest_report.json')
    overfit = load_json(EVOLUTION / 'overfit_audit_report.json')
    promotion = load_json(EVOLUTION / 'factor_promotion_decision.json')
    market_state = load_json(CANONICAL / 'market_state_latest.json')
    market_snapshot = load_json(HEALTH / 'market_snapshot_latest.json')
    industry_state = load_json(CANONICAL / 'industry_state_latest.json')
    morning_brief = load_json(REPORTS / 'market_morning_brief_latest.json')
    heatmap = load_json(REPORTS / 'market_industry_heatmap.json')
    regime_hist = pd.read_parquet(ROOT / 'data' / 'regimes' / 'regime_labels_phase1.parquet')
    regime_hist['trade_date'] = regime_hist['trade_date'].astype(str)

    last_hist_date = str(regime_hist['trade_date'].max())
    recent = build_recent_market_summary(last_hist_date)
    current_summary_basis = 'raw_cache_beyond_regime_history'
    if recent.empty:
        recent = build_recent_market_summary('00000000')
        if recent.empty:
            raise SystemExit('recent market summary is empty')
        current_summary_basis = 'latest_available_raw_cache_same_as_regime_history'
    available_trade_dates = recent['trade_date'].astype(str)
    requested_trade_date = str(args.target_trade_date or '').strip()
    if requested_trade_date:
        available_trade_date_set = set(available_trade_dates.tolist())
        if requested_trade_date not in available_trade_date_set:
            full_summary = build_recent_market_summary('00000000')
            full_trade_dates = full_summary['trade_date'].astype(str) if not full_summary.empty else pd.Series(dtype=str)
            full_trade_date_set = set(full_trade_dates.tolist()) if not full_summary.empty else set()
            if requested_trade_date not in full_trade_date_set:
                max_desc = available_trade_dates.max() if not available_trade_dates.empty else 'n/a'
                min_desc = available_trade_dates.min() if not available_trade_dates.empty else 'n/a'
                raise SystemExit(
                    f'requested target_trade_date {requested_trade_date} not found in available recent market summary range '
                    f'[{min_desc}, {max_desc}] or full raw-cache summary'
                )
            recent = full_summary
            current_summary_basis = 'full_raw_cache_target_trade_date_override'
            available_trade_dates = recent['trade_date'].astype(str)
        current_trade_date = requested_trade_date
        current_summary_basis = f'{current_summary_basis}|target_trade_date_override'
    else:
        current_trade_date = str(available_trade_dates.max())
    current_summary_row = recent[recent['trade_date'] == current_trade_date].iloc[-1].to_dict()
    current_summary_row['trade_date'] = current_trade_date
    latest_weight_plan_selected = weight_map.get('latest_weight_plan', {}).get('selected_factors', []) or []
    current_whitelist = [
        str(item.get('factor_id'))
        for item in latest_weight_plan_selected
        if item.get('selection_source') == 'whitelist' and item.get('factor_id')
    ]
    current_support_focus = [
        str(item.get('factor_id'))
        for item in latest_weight_plan_selected
        if item.get('selection_source') == 'support_pool' and item.get('factor_id')
    ]
    current_fallback_dependency = [
        str(item.get('factor_id'))
        for item in latest_weight_plan_selected
        if str(item.get('selection_source') or '').startswith('fallback') and item.get('factor_id')
    ]

    combined_returns = pd.concat(
        [
            regime_hist[['trade_date', 'market_return_mean']].astype({'trade_date': str}),
            recent[['trade_date', 'market_return_mean']].copy(),
        ],
        ignore_index=True,
    ).drop_duplicates(subset=['trade_date'], keep='last').sort_values('trade_date').reset_index(drop=True)
    combined_returns['proxy_close'] = (1.0 + combined_returns['market_return_mean'].astype(float).fillna(0.0)).cumprod() * 100.0
    proxy_close = combined_returns['proxy_close']
    proxy_ret = combined_returns['market_return_mean'].astype(float).fillna(0.0)

    current_idx = combined_returns.index[combined_returns['trade_date'] == current_trade_date][-1]
    current_close = float(proxy_close.iloc[current_idx])

    def past_return(days: int) -> float | None:
        if current_idx - days < 0:
            return None
        return float(current_close / proxy_close.iloc[current_idx - days] - 1.0)

    def ma_dev(days: int) -> float | None:
        start = max(0, current_idx - days + 1)
        window = proxy_close.iloc[start: current_idx + 1]
        if window.empty:
            return None
        return float(current_close / window.mean() - 1.0)

    def dist_to_extreme(days: int, mode: str) -> float | None:
        start = max(0, current_idx - days + 1)
        window = proxy_close.iloc[start: current_idx + 1]
        if window.empty:
            return None
        ref = window.max() if mode == 'high' else window.min()
        if ref == 0:
            return None
        return float(current_close / ref - 1.0)

    vol20 = float(proxy_ret.iloc[max(0, current_idx - 19): current_idx + 1].std(ddof=0) * np.sqrt(252)) if current_idx >= 1 else None
    vol60 = float(proxy_ret.iloc[max(0, current_idx - 59): current_idx + 1].std(ddof=0) * np.sqrt(252)) if current_idx >= 1 else None
    dd20 = max_drawdown(proxy_close.iloc[max(0, current_idx - 19): current_idx + 1])
    dd60 = max_drawdown(proxy_close.iloc[max(0, current_idx - 59): current_idx + 1])
    down20 = float((proxy_ret.iloc[max(0, current_idx - 19): current_idx + 1] < 0).mean())
    down60 = float((proxy_ret.iloc[max(0, current_idx - 59): current_idx + 1] < 0).mean())
    reversal20 = float(recent['intraday_reversal_freq_proxy'].tail(20).mean())
    reversal60 = float(recent['intraday_reversal_freq_proxy'].tail(60).mean())
    amount_pct = percentile_rank(recent['amount_total'], 80)

    top_sector_snapshots = morning_brief.get('top_sector_snapshots', [])
    top_sector_names = [item.get('industry_name') for item in top_sector_snapshots[:5] if item.get('industry_name')]
    bottom_industries = market_snapshot.get('industry_state', {}).get('bottom_industries', [])
    top_industries = market_snapshot.get('industry_state', {}).get('top_industries', [])
    heatmap_top = heatmap.get('top_sectors_latest', [])[:10]

    unavailable_named_indices = {
        key: {
            'status': 'unavailable_local_cache',
            'missing_reason': '当前本地未保留对应指数历史序列，且外部抓取链路此轮不可用/无 token，暂不能给出 1y/3y/5y 分位和 MA 偏离。',
            'evidence_path': [
                str(CANONICAL / 'market_state_latest.json'),
                str(REPORTS / 'market_morning_brief_latest.json'),
            ],
        }
        for key in ['shanghai_composite', 'csi300', 'chinext', 'csi1000', 'csi_all_share']
    }

    current_market_fingerprint = {
        'schema_version': '2026-04-19.current_market_fingerprint.v1',
        'generated_at': generated_at,
        'trade_date': current_trade_date,
        'data_status': {
            'market_state_target_trade_date': market_state.get('target_trade_date'),
            'market_state_quality_ok': market_state.get('data_preparation', {}).get('quality_ok'),
            'market_state_quality_errors': market_state.get('data_preparation', {}).get('quality_errors', []),
            'market_snapshot_trade_date': market_snapshot.get('trade_date'),
            'morning_brief_trade_date': morning_brief.get('trade_date'),
            'factor_factory_latest_regime_trade_date': weight_map.get('latest_regime', {}).get('trade_date'),
            'recent_raw_proxy_trade_date': current_trade_date,
            'current_summary_basis': current_summary_basis,
        },
        'index_positions': {
            'broad_market_equal_weight_proxy': {
                'status': 'available_proxy',
                'name': 'A股等权市场代理',
                'close_percentile_1y': round_float(percentile_rank(proxy_close, 250), 6),
                'close_percentile_3y': round_float(percentile_rank(proxy_close, 750), 6),
                'close_percentile_5y': round_float(percentile_rank(proxy_close, 1250), 6),
                'ma_deviation_60d': round_float(ma_dev(60), 6),
                'ma_deviation_120d': round_float(ma_dev(120), 6),
                'ma_deviation_250d': round_float(ma_dev(250), 6),
                'distance_to_250d_high': round_float(dist_to_extreme(250, 'high'), 6),
                'distance_to_250d_low': round_float(dist_to_extreme(250, 'low'), 6),
                'note': '本地没有完整指数历史时，先用全市场等权收益代理衡量位置变化。',
            },
            'named_indices': unavailable_named_indices,
            'overnight_external_proxies': {
                'a50_change_pct': morning_brief.get('a50_change_pct'),
                'golden_dragon_change_pct': morning_brief.get('golden_dragon_change_pct'),
                'cnh_change_pct': morning_brief.get('cnh_change_pct'),
            },
        },
        'trend_state': {
            'broad_market_proxy': {
                'return_5d': round_float(past_return(5), 6),
                'return_20d': round_float(past_return(20), 6),
                'return_60d': round_float(past_return(60), 6),
                'return_120d': round_float(past_return(120), 6),
                'breadth_ratio': round_float(current_summary_row.get('breadth_ratio'), 6),
                'is_near_250d_high': bool((dist_to_extreme(250, 'high') or -1) >= -0.03),
                'is_high_volume_state': bool((amount_pct or 0.0) >= 0.8),
                'is_high_level_stagnation': bool((amount_pct or 0.0) >= 0.8 and abs(past_return(5) or 0.0) <= 0.02),
            },
            'morning_brief_regime': morning_brief.get('regime'),
            'morning_brief_risk_score': morning_brief.get('risk_score'),
        },
        'volatility_and_risk': {
            'broad_market_proxy': {
                'volatility_20d': round_float(vol20, 6),
                'volatility_60d': round_float(vol60, 6),
                'max_drawdown_20d': round_float(dd20, 6),
                'max_drawdown_60d': round_float(dd60, 6),
                'intraday_reversal_freq_20d_proxy': round_float(reversal20, 6),
                'intraday_reversal_freq_60d_proxy': round_float(reversal60, 6),
                'down_day_ratio_20d': round_float(down20, 6),
                'down_day_ratio_60d': round_float(down60, 6),
            }
        },
        'turnover_and_liquidity': {
            'broad_market_proxy': {
                'amount_total': round_float(current_summary_row.get('amount_total'), 2),
                'amount_total_percentile_80d': round_float(amount_pct, 6),
                'amount_ratio_median': round_float(current_summary_row.get('amount_ratio_median'), 6),
                'state': 'high_volume' if (amount_pct or 0.0) >= 0.8 else 'normal' if (amount_pct or 0.0) >= 0.4 else 'shrinking',
            }
        },
        'breadth': {
            'up_count': int(current_summary_row.get('up_count', 0)),
            'down_count': int(current_summary_row.get('down_count', 0)),
            'limit_up_count_proxy': int(current_summary_row.get('limit_up_count_proxy', 0)),
            'limit_down_count_proxy': int(current_summary_row.get('limit_down_count_proxy', 0)),
            'new_high_count_proxy_60d': int(current_summary_row.get('new_high_count_proxy_60d', 0)),
            'new_low_count_proxy_60d': int(current_summary_row.get('new_low_count_proxy_60d', 0)),
            'divergence_ratio_proxy': round_float(1.0 - float(current_summary_row.get('breadth_ratio', 0.0)), 6),
            'note': '涨跌停、新高新低使用本地 stock-level 代理口径，不是交易所官方口径。',
        },
        'style_and_sector': {
            'broad_market_proxy': {
                'small_minus_large': round_float(current_summary_row.get('small_minus_large'), 6),
                'regime_id': current_summary_row.get('regime_id'),
                'regime_confidence': round_float(current_summary_row.get('regime_confidence'), 6),
                'regime_components': json.loads(current_summary_row.get('regime_components', '{}')),
            },
            'focus_sectors': morning_brief.get('focus_sectors', []),
            'top_sector_snapshots': top_sector_snapshots[:5],
            'heatmap_top_sectors': heatmap_top,
            'snapshot_top_industries': top_industries,
            'snapshot_bottom_industries': bottom_industries,
            'industry_state_target_strategy': industry_state.get('target_strategy'),
        },
        'valuation_environment': {
            'status': 'unavailable_local_cache',
            'missing_reason': '本地未保留指数/行业估值分位历史，也未接入可用外部估值抓取。',
            'evidence_path': [str(CANONICAL / 'market_state_latest.json')],
        },
        'factor_factory_state': {
            'factor_count': audit.get('factor_count'),
            'whitelist_count': audit.get('whitelist_count'),
            'coldlist_count': audit.get('coldlist_count'),
            'selection_mode': weight_map.get('selection_mode'),
            'support_pool_count': weight_map.get('support_pool_count'),
            'fallback_pool_count': weight_map.get('fallback_pool_count'),
            'risk_count': len(attribution.get('risks', [])),
            'latest_weight_map_regime': weight_map.get('latest_regime', {}).get('regime_id'),
            'latest_weight_map_trade_date': weight_map.get('latest_regime', {}).get('trade_date'),
            'current_whitelist': current_whitelist,
            'current_support_focus': current_support_focus,
            'current_fallback_dependency': current_fallback_dependency,
        },
        'market_regime_summary': {
            'current_proxy_regime': current_summary_row.get('regime_id'),
            'current_proxy_regime_confidence': round_float(current_summary_row.get('regime_confidence'), 6),
            'morning_brief_regime': morning_brief.get('regime'),
            'risk_score': morning_brief.get('risk_score'),
            'key_drivers': morning_brief.get('key_drivers', []),
        },
        'gaps_and_caveats': [
            '上证/沪深300/创业板/中证1000/中证全指的完整历史位置信息本轮不可得，先用本地等权市场代理 + A50/金龙隔夜代理补位。',
            '估值环境字段缺真实指数/行业估值分位数据，当前只能明确标记 unavailable。',
            f"factor_factory 最新 regime 正式产物目前已追到 {weight_map.get('latest_regime', {}).get('trade_date') or '未知'}；本文件同时用本地 raw cache 额外推演到 {current_trade_date} 的当前代理状态。",
        ],
    }

    analog_windows = similarity_windows(regime_hist, current_summary_row, weight_map, audit)
    analog_payload = {
        'schema_version': '2026-04-19.regime_analog_windows.v1',
        'generated_at': generated_at,
        'current_trade_date': current_trade_date,
        'current_feature_vector': {k: round_float(current_summary_row.get(k), 6) for k in FEATURE_COLS + ['regime_id', 'regime_confidence']},
        'methodology': {
            'base_history': str(ROOT / 'data' / 'regimes' / 'regime_labels_phase1.parquet'),
            'similarity_features': FEATURE_COLS,
            'distance_metric': 'euclidean_on_historical_zscores',
            'current_summary_basis': current_summary_basis,
            'caveat': 'analog windows use current raw-cache proxy features mapped onto historical regime_labels; factor references are current regime-map proxies, not reconstructed historical realized factor attribution.',
        },
        'analog_windows': analog_windows,
    }

    hypotheses = build_hypotheses(current_market_fingerprint, morning_brief, weight_map)
    hypotheses_payload = {
        'schema_version': '2026-04-19.candidate_factor_hypotheses.v1',
        'generated_at': generated_at,
        'trade_date': current_trade_date,
        'market_context_summary': {
            'focus_sectors': current_market_fingerprint['style_and_sector']['focus_sectors'],
            'proxy_regime': current_market_fingerprint['market_regime_summary']['current_proxy_regime'],
            'whitelist_count': current_market_fingerprint['factor_factory_state']['whitelist_count'],
            'fallback_pool_count': current_market_fingerprint['factor_factory_state']['fallback_pool_count'],
        },
        'hypotheses': hypotheses,
    }

    (EVOLUTION / 'current_market_fingerprint.json').write_text(json.dumps(current_market_fingerprint, ensure_ascii=False, indent=2, default=json_default), encoding='utf-8')
    (EVOLUTION / 'regime_analog_windows.json').write_text(json.dumps(analog_payload, ensure_ascii=False, indent=2, default=json_default), encoding='utf-8')
    (EVOLUTION / 'candidate_factor_hypotheses.json').write_text(json.dumps(hypotheses_payload, ensure_ascii=False, indent=2, default=json_default), encoding='utf-8')

    daily_digest = render_daily_digest(current_market_fingerprint, analog_payload, hypotheses_payload, factor_validation, portfolio, overfit, promotion)
    weekly_review = render_weekly_review(current_market_fingerprint, factor_validation, overfit)
    skill_draft = render_skill_draft()
    (EVOLUTION / 'daily_factor_evolution_digest.md').write_text(daily_digest, encoding='utf-8')
    (EVOLUTION / 'weekly_factor_evolution_review.md').write_text(weekly_review, encoding='utf-8')
    (EVOLUTION / 'skill_draft_factor_evolution_lab.md').write_text(skill_draft, encoding='utf-8')

    print(json.dumps({
        'status': 'ok',
        'trade_date': current_trade_date,
        'outputs': [
            str(EVOLUTION / 'current_market_fingerprint.json'),
            str(EVOLUTION / 'regime_analog_windows.json'),
            str(EVOLUTION / 'candidate_factor_hypotheses.json'),
            str(EVOLUTION / 'daily_factor_evolution_digest.md'),
            str(EVOLUTION / 'weekly_factor_evolution_review.md'),
            str(EVOLUTION / 'skill_draft_factor_evolution_lab.md'),
        ],
        'top_analog_dates': [item['trade_date'] for item in analog_windows[:3]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
