#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

ROOT_DEFAULT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
CONFIG_DEFAULT = ROOT_DEFAULT / 'config' / 'greenfield_multifactor_v1.yaml'


@dataclass
class GreenfieldConfig:
    root: Path
    workspace: Path
    stock_data_root: Path
    output_root: Path
    panel_path: Path
    industry_map_path: Path
    pit_financial_dir: Path
    current_market_fingerprint_path: Path
    regime_analog_windows_path: Path
    market_state_path: Path
    market_snapshot_path: Path
    market_morning_brief_path: Path
    start_date: str
    end_date: str | None
    top_n: int
    round_trip_cost: float
    min_listing_days: int
    train_days: int
    validation_days: int
    test_days: int
    step_days: int
    min_train_days: int
    analog_max_windows: int
    analog_lookback_days: int
    analog_min_lookback_days: int
    family_budgets: dict[str, dict[str, float]]
    factor_registry: dict[str, Any]
    scoring_controls: dict[str, Any]
    selection_controls: dict[str, Any]
    materialization_controls: dict[str, Any]
    regime_controls: dict[str, Any]
    scenario_meta: dict[str, Any]
    hypotheses_path: Path | None = None
    rebalance_interval: int = 1  # 默认每日调仓，兼容老测试/构造调用


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def write_text_atomic(path: Path, content: str, encoding: str = 'utf-8') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode='w',
        encoding=encoding,
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_parquet_atomic(df: pd.DataFrame, path: Path, **kwargs: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode='wb',
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
        delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        df.to_parquet(tmp_path, **kwargs)
        with tmp_path.open('rb') as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def read_parquet_with_retries(path: Path, attempts: int = 24, delay_seconds: float = 5.0, **kwargs: Any) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return pd.read_parquet(path, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def load_config(config_path: Path | str = CONFIG_DEFAULT) -> GreenfieldConfig:
    cfg = load_yaml(Path(config_path).expanduser().resolve())
    paths = cfg['paths']
    history = cfg['history']
    portfolio = cfg['portfolio']
    walkforward = cfg['walkforward']
    analog = cfg['analog_validation']
    return GreenfieldConfig(
        root=Path(paths['factor_factory_root']).expanduser().resolve(),
        workspace=Path(paths['workspace']).expanduser().resolve(),
        stock_data_root=Path(paths['stock_data_root']).expanduser().resolve(),
        output_root=Path(paths['output_root']).expanduser().resolve(),
        panel_path=Path(paths['panel_path']).expanduser().resolve(),
        industry_map_path=Path(paths['industry_map_path']).expanduser().resolve(),
        pit_financial_dir=Path(paths['pit_financial_dir']).expanduser().resolve(),
        current_market_fingerprint_path=Path(paths['current_market_fingerprint_path']).expanduser().resolve(),
        regime_analog_windows_path=Path(paths['regime_analog_windows_path']).expanduser().resolve(),
        market_state_path=Path(paths['market_state_path']).expanduser().resolve(),
        market_snapshot_path=Path(paths['market_snapshot_path']).expanduser().resolve(),
        market_morning_brief_path=Path(paths['market_morning_brief_path']).expanduser().resolve(),
        hypotheses_path=Path(paths['hypotheses_path']).expanduser().resolve() if paths.get('hypotheses_path') else None,
        start_date=str(history['start_date']).replace('-', ''),
        end_date=str(history['end_date']).replace('-', '') if history.get('end_date') else None,
        top_n=int(portfolio['top_n']),
        round_trip_cost=float(portfolio['round_trip_cost']),
        min_listing_days=int(portfolio['min_listing_days']),
        rebalance_interval=int(portfolio.get('rebalance_interval', 1)),  # 默认每天调仓
        train_days=int(walkforward['train_days']),
        validation_days=int(walkforward['validation_days']),
        test_days=int(walkforward['test_days']),
        step_days=int(walkforward['step_days']),
        min_train_days=int(walkforward['min_train_days']),
        analog_max_windows=int(analog['max_windows']),
        analog_lookback_days=int(analog['lookback_days']),
        analog_min_lookback_days=int(analog['min_lookback_days']),
        family_budgets=cfg['family_budgets'],
        factor_registry=cfg['factor_registry'],
        scoring_controls=cfg.get('scoring_controls') or {},
        selection_controls=cfg.get('selection_controls') or {},
        materialization_controls=cfg.get('materialization_controls') or {},
        regime_controls=cfg.get('regime_controls') or {},
        scenario_meta=cfg.get('scenario_meta') or {},
    )


def _cache_dir(cfg: GreenfieldConfig) -> Path:
    return cfg.stock_data_root / '03-working' / 'backtest_cache'


def _universe_dir(cfg: GreenfieldConfig) -> Path:
    return cfg.root / 'data' / 'universe'


RAW_DAILY_COLUMNS = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'vol', 'amount']
RAW_STK_COLUMNS = ['ts_code', 'trade_date', 'macd_dif', 'macd_dea', 'macd', 'kdj_k', 'kdj_d', 'kdj_j', 'rsi_6', 'rsi_12', 'rsi_24', 'boll_upper', 'boll_mid', 'boll_lower', 'cci']
RAW_CYQ_COLUMNS = ['ts_code', 'trade_date', 'cost_5pct', 'cost_15pct', 'cost_50pct', 'cost_85pct', 'cost_95pct', 'weight_avg', 'winner_rate']
UNIVERSE_COLUMNS = ['trade_date', 'ts_code', 'name', 'avg_amount_lookback', 'trading_ratio_lookback', 'listing_days', 'liquidity_threshold', 'universe_flag']


def available_common_dates(cfg: GreenfieldConfig) -> list[str]:
    cache_dir = _cache_dir(cfg)
    universe_dir = _universe_dir(cfg)
    daily_dates = {p.stem.split('_')[-1] for p in cache_dir.glob('daily_*.parquet')}
    stk_dates = {p.stem.split('_')[-1] for p in cache_dir.glob('stk_factor_*.parquet')}
    cyq_dates = {p.stem.split('_')[-1] for p in cache_dir.glob('cyq_perf_*.parquet')}
    universe_dates = {p.stem.split('_')[-1] for p in universe_dir.glob('universe_*.parquet')}
    dates = sorted(daily_dates & stk_dates & cyq_dates & universe_dates)
    return [d for d in dates if d >= cfg.start_date and (cfg.end_date is None or d <= cfg.end_date)]


def load_cache_frame(path: Path, columns: list[str], trade_date_hint: str | None = None) -> pd.DataFrame:
    df = pd.read_parquet(path)
    keep = [c for c in columns if c in df.columns]
    df = df[keep].copy()
    if 'trade_date' not in df.columns and trade_date_hint is not None:
        df['trade_date'] = str(trade_date_hint)
    if 'ts_code' in df.columns:
        df['ts_code'] = df['ts_code'].astype(str)
    if 'trade_date' in df.columns:
        df['trade_date'] = df['trade_date'].astype(str)
    return df


def load_raw_history_panel(cfg: GreenfieldConfig, dates: list[str] | None = None) -> pd.DataFrame:
    dates = dates or available_common_dates(cfg)
    cache_dir = _cache_dir(cfg)
    universe_dir = _universe_dir(cfg)
    frames: list[pd.DataFrame] = []
    for d in dates:
        universe = load_cache_frame(universe_dir / f'universe_{d}.parquet', UNIVERSE_COLUMNS, trade_date_hint=d)
        daily = load_cache_frame(cache_dir / f'daily_{d}.parquet', RAW_DAILY_COLUMNS, trade_date_hint=d)
        stk = load_cache_frame(cache_dir / f'stk_factor_{d}.parquet', RAW_STK_COLUMNS, trade_date_hint=d)
        cyq = load_cache_frame(cache_dir / f'cyq_perf_{d}.parquet', RAW_CYQ_COLUMNS, trade_date_hint=d)
        merged = universe.merge(daily, on=['trade_date', 'ts_code'], how='left')
        merged = merged.merge(stk, on=['trade_date', 'ts_code'], how='left')
        merged = merged.merge(cyq, on=['trade_date', 'ts_code'], how='left')
        frames.append(merged)
    panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if panel.empty:
        return panel
    panel = panel.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    panel['trade_date'] = panel['trade_date'].astype(str)
    return panel


def rolling_group(df: pd.DataFrame, col: str, win: int, func: str, min_periods: int | None = None) -> pd.Series:
    min_p = min_periods or min(win, 5)
    grp = df.groupby('ts_code', group_keys=False)[col]
    if func == 'mean':
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).mean())
    if func == 'std':
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).std())
    if func == 'min':
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).min())
    if func == 'max':
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).max())
    raise ValueError(func)


def shift_group(df: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return df.groupby('ts_code', group_keys=False)[col].shift(periods)


def pct_change_group(df: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return df.groupby('ts_code', group_keys=False)[col].pct_change(periods)


def rolling_max_drawdown(close: pd.Series, win: int) -> pd.Series:
    rolling_peak = close.rolling(win, min_periods=min(win, 5)).max()
    drawdown = close / rolling_peak.replace(0, np.nan) - 1.0
    return drawdown.rolling(win, min_periods=min(win, 5)).min()


def attach_core_features(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.copy()
    numeric_cols = [
        'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'vol', 'amount',
        'macd_dif', 'macd_dea', 'macd', 'kdj_k', 'kdj_d', 'kdj_j', 'rsi_6', 'rsi_12', 'rsi_24',
        'boll_upper', 'boll_mid', 'boll_lower', 'cci', 'cost_5pct', 'cost_15pct', 'cost_50pct',
        'cost_85pct', 'cost_95pct', 'weight_avg', 'winner_rate', 'avg_amount_lookback',
        'trading_ratio_lookback', 'listing_days',
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce')
    out['ret_1d'] = out['pct_chg'] / 100.0
    out['ret_5d'] = pct_change_group(out, 'close', 5)
    out['ret_10d'] = pct_change_group(out, 'close', 10)
    out['ret_20d'] = pct_change_group(out, 'close', 20)
    out['ma_5'] = rolling_group(out, 'close', 5, 'mean')
    out['ma_10'] = rolling_group(out, 'close', 10, 'mean')
    out['ma_20'] = rolling_group(out, 'close', 20, 'mean')
    out['ma_60'] = rolling_group(out, 'close', 60, 'mean', min_periods=20)
    out['vol_ma_5'] = rolling_group(out, 'vol', 5, 'mean')
    out['vol_ma_20'] = rolling_group(out, 'vol', 20, 'mean')
    out['amount_ma_20'] = rolling_group(out, 'amount', 20, 'mean')
    out['amount_std_20'] = rolling_group(out, 'amount', 20, 'std')
    out['vol_std_20'] = rolling_group(out, 'vol', 20, 'std')
    out['ret_std_10'] = rolling_group(out, 'ret_1d', 10, 'std')
    out['ret_std_20'] = rolling_group(out, 'ret_1d', 20, 'std')
    out['close_min_20'] = rolling_group(out, 'close', 20, 'min')
    out['close_max_20'] = rolling_group(out, 'close', 20, 'max')
    out['hl_range'] = (out['high'] - out['low']) / out['close'].replace(0, np.nan)
    out['atr_proxy_14'] = rolling_group(out, 'hl_range', 14, 'mean')
    out['boll_width'] = (out['boll_upper'] - out['boll_lower']) / out['boll_mid'].replace(0, np.nan)
    out['f_price_vs_ma20'] = out['close'] / out['ma_20'].replace(0, np.nan) - 1.0
    out['f_price_vs_ma60'] = out['close'] / out['ma_60'].replace(0, np.nan) - 1.0
    out['f_close_pos_20'] = (out['close'] - out['close_min_20']) / (out['close_max_20'] - out['close_min_20'] + 1e-9)
    out['f_up_day_ratio_10'] = out.groupby('ts_code', group_keys=False)['ret_1d'].transform(lambda s: s.gt(0).rolling(10, min_periods=5).mean())
    out['f_gap_return'] = out['open'] / out['pre_close'].replace(0, np.nan) - 1.0
    out['f_intraday_reversal'] = out['close'] / out['open'].replace(0, np.nan) - 1.0
    out['f_amount_ratio_20'] = out['amount'] / out['amount_ma_20'].replace(0, np.nan)
    out['f_volume_ratio_5'] = out['vol'] / out['vol_ma_5'].replace(0, np.nan)
    out['f_volume_ratio_20'] = out['vol'] / out['vol_ma_20'].replace(0, np.nan)
    out['f_amount_cv_20'] = out['amount_std_20'] / out['amount_ma_20'].replace(0, np.nan)
    out['f_volume_cv_20'] = out['vol_std_20'] / out['vol_ma_20'].replace(0, np.nan)
    out['f_amihud_20'] = out.groupby('ts_code', group_keys=False).apply(
        lambda g: (g['ret_1d'].abs() / g['amount'].replace(0, np.nan)).rolling(20, min_periods=5).mean()
    ).reset_index(level=0, drop=True)
    out['f_amount_zscore_20'] = (out['amount'] - out['amount_ma_20']) / (out['amount_std_20'] + 1e-9)
    out['f_winner_rate_level'] = out['winner_rate'] / 100.0
    winner_mean_20 = rolling_group(out, 'f_winner_rate_level', 20, 'mean')
    winner_std_20 = rolling_group(out, 'f_winner_rate_level', 20, 'std')
    out['f_winner_rate_z_20'] = (out['f_winner_rate_level'] - winner_mean_20) / (winner_std_20 + 1e-9)
    out['f_chip_band_width'] = (out['cost_85pct'] - out['cost_15pct']) / out['close'].replace(0, np.nan)
    out['f_weight_avg_gap'] = out['close'] / out['weight_avg'].replace(0, np.nan) - 1.0
    out['f_weight_avg_gap_delta_5'] = out['f_weight_avg_gap'] - shift_group(out, 'f_weight_avg_gap', 5)
    out['f_chip_support_proxy'] = -(out['close'] - out['weight_avg']).abs() / out['close'].replace(0, np.nan)
    out['rolling_drawdown_20'] = out.groupby('ts_code', group_keys=False)['close'].transform(lambda s: rolling_max_drawdown(s, 20))
    return out


def load_industry_map(cfg: GreenfieldConfig) -> pd.DataFrame:
    industry_map = pd.read_parquet(cfg.industry_map_path)
    cols = [c for c in ['ts_code', 'industry', 'market', 'list_date'] if c in industry_map.columns]
    industry_map = industry_map[cols].copy()
    industry_map['ts_code'] = industry_map['ts_code'].astype(str)
    if 'industry' not in industry_map.columns:
        industry_map['industry'] = '未知'
    industry_map['industry'] = industry_map['industry'].fillna('未知').astype(str)
    return industry_map.drop_duplicates(subset=['ts_code'])


def attach_industry_features(panel: pd.DataFrame, industry_map: pd.DataFrame) -> pd.DataFrame:
    out = panel.merge(industry_map[['ts_code', 'industry', 'market']], on='ts_code', how='left')
    out['industry'] = out['industry'].fillna('未知')
    out['market'] = out['market'].fillna('未知')
    grp = out.groupby(['trade_date', 'industry'], sort=False)
    industry_daily = grp.agg(
        industry_ret_5=('ret_5d', 'mean'),
        industry_ret_20=('ret_20d', 'mean'),
        industry_price_vs_ma20=('f_price_vs_ma20', 'mean'),
        industry_amount_ratio_20=('f_amount_ratio_20', 'median'),
        industry_breadth=('ret_1d', lambda s: float((s > 0).mean())),
    ).reset_index()
    industry_daily = industry_daily.sort_values(['industry', 'trade_date'])
    industry_daily['industry_ret_5_ema3'] = industry_daily.groupby('industry', group_keys=False)['industry_ret_5'].transform(lambda s: s.ewm(span=3, adjust=False).mean())
    industry_daily['industry_heat_accel'] = industry_daily['industry_ret_5_ema3'] - industry_daily.groupby('industry', group_keys=False)['industry_ret_5_ema3'].shift(3)
    out = out.merge(industry_daily, on=['trade_date', 'industry'], how='left')
    return out


def choose_primary_regime(mean_ret: float, amount_ratio: float, hl_mean: float, breadth: float) -> tuple[str, float]:
    amount_ratio = 0.0 if pd.isna(amount_ratio) else float(amount_ratio)
    if abs(mean_ret) < 0.002 and hl_mean >= 0.05:
        return 'high_vol_chop', min(0.95, 0.55 + (hl_mean - 0.05) * 4)
    if amount_ratio >= 1.05 and mean_ret >= 0.002:
        return 'volume_uptrend', min(0.95, 0.6 + (amount_ratio - 1.05) * 0.8 + max(0.0, mean_ret) * 12)
    if amount_ratio >= 1.05 and mean_ret <= -0.002:
        return 'volume_downtrend', min(0.95, 0.6 + (amount_ratio - 1.05) * 0.8 + abs(mean_ret) * 12)
    if amount_ratio < 1.0 and mean_ret >= 0:
        return 'shrink_uptrend', min(0.85, 0.5 + max(0.0, mean_ret) * 10 + max(0.0, breadth - 0.5) * 0.4)
    if amount_ratio < 1.0 and mean_ret < 0:
        return 'shrink_downtrend', min(0.85, 0.5 + abs(mean_ret) * 10 + max(0.0, 0.5 - breadth) * 0.4)
    if mean_ret >= 0:
        return 'shrink_uptrend', 0.45
    return 'shrink_downtrend', 0.45


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


def build_daily_regime_features(panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for trade_date, day in panel.groupby('trade_date', sort=True):
        mean_ret = float(day['ret_1d'].mean(skipna=True))
        median_ret = float(day['ret_1d'].median(skipna=True))
        breadth = float((day['ret_1d'] > 0).mean())
        amount_ratio = float(day['f_amount_ratio_20'].replace([np.inf, -np.inf], np.nan).median(skipna=True))
        hl_mean = float(day['hl_range'].mean(skipna=True))
        size_proxy = np.log1p(day['avg_amount_lookback'].clip(lower=0))
        q_low = size_proxy.quantile(0.25)
        q_high = size_proxy.quantile(0.75)
        small_bucket = day[size_proxy <= q_low]
        large_bucket = day[size_proxy >= q_high]
        small_minus_large = float(small_bucket['ret_1d'].mean(skipna=True) - large_bucket['ret_1d'].mean(skipna=True)) if not small_bucket.empty and not large_bucket.empty else 0.0
        contribution = (day['amount'].abs() * day['ret_1d'].abs()).sort_values(ascending=False)
        top5_share = float(contribution.head(5).sum() / contribution.sum()) if contribution.sum() > 0 else 0.0
        primary_id, primary_conf = choose_primary_regime(mean_ret, amount_ratio, hl_mean, breadth)
        style_id, style_conf = choose_style_regime(small_minus_large)
        sector_id, sector_conf = choose_sector_regime(top5_share)
        overall_conf = float(np.clip(np.mean([primary_conf, style_conf, sector_conf]), 0.0, 1.0))
        rows.append({
            'trade_date': str(trade_date),
            'regime_id': primary_id,
            'regime_confidence': overall_conf,
            'style_regime_id': style_id,
            'style_regime_confidence': style_conf,
            'sector_regime_id': sector_id,
            'sector_regime_confidence': sector_conf,
            'market_return_mean': mean_ret,
            'market_return_median': median_ret,
            'breadth_ratio': breadth,
            'amount_ratio_median': amount_ratio,
            'hl_range_mean': hl_mean,
            'small_minus_large': small_minus_large,
            'top5_contribution_share': top5_share,
        })
    return pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)


def add_greenfield_factors(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel  # in-place to avoid OOM on 16GB Mac
    size_proxy = np.log1p(out['avg_amount_lookback'].clip(lower=0))
    out['g_trend_quality_20'] = (
        0.35 * out['ret_20d']
        + 0.20 * out['f_price_vs_ma20']
        + 0.15 * out['f_price_vs_ma60']
        + 0.15 * out['f_close_pos_20']
        + 0.15 * out['f_up_day_ratio_10']
        - 0.10 * out['f_gap_return'].abs()
    )
    out['g_momentum_stability_10_20'] = (
        0.45 * out['ret_10d']
        + 0.20 * out['ret_20d']
        - 0.20 * out['ret_std_20']
        - 0.15 * (out['f_gap_return'].abs() + out['f_intraday_reversal'].abs())
    )
    out['g_liquid_leadership'] = (
        0.35 * size_proxy
        + 0.25 * out['ret_20d']
        + 0.20 * out['f_amount_ratio_20']
        + 0.20 * out['f_price_vs_ma20']
    )
    out['g_liquidity_followthrough'] = (
        0.35 * out['f_amount_ratio_20']
        + 0.25 * out['f_volume_ratio_5']
        + 0.20 * out['ret_5d']
        - 0.20 * out['f_amount_cv_20']
    )
    out['g_turnover_efficiency'] = (
        0.45 * out['f_amount_zscore_20']
        - 0.30 * out['f_amihud_20']
        - 0.15 * out['f_volume_cv_20']
        + 0.10 * out['ret_10d']
    )
    out['g_sector_heat_alignment'] = (
        0.40 * out['industry_ret_5']
        + 0.20 * out['industry_heat_accel']
        + 0.20 * out['industry_amount_ratio_20']
        + 0.20 * out['industry_breadth']
    )
    out['g_industry_relative_strength'] = (
        0.55 * (out['ret_20d'] - out['industry_ret_20'])
        + 0.25 * (out['f_price_vs_ma20'] - out['industry_price_vs_ma20'])
        + 0.20 * (out['ret_5d'] - out['industry_ret_5'])
    )
    out['g_drawdown_resilience'] = (
        -0.45 * out['rolling_drawdown_20'].abs()
        - 0.25 * out['ret_std_20']
        + 0.20 * out['f_chip_support_proxy']
        + 0.10 * out['f_price_vs_ma20']
    )
    out['g_low_vol_leader'] = (
        0.40 * out['ret_20d']
        - 0.30 * out['ret_std_20']
        - 0.20 * out['boll_width']
        + 0.10 * out['f_price_vs_ma20']
    )
    out['g_chip_pullback_support'] = (
        0.40 * out['f_chip_support_proxy']
        - 0.25 * out['f_chip_band_width']
        - 0.20 * out['f_winner_rate_z_20'].abs()
        - 0.15 * out['f_weight_avg_gap_delta_5'].abs()
    )
    out['g_gap_risk_filter'] = -(
        0.55 * out['f_gap_return'].abs()
        + 0.25 * out['f_intraday_reversal'].abs()
        + 0.20 * out['ret_std_10']
    )

    # P1 long-only guardrail repair factors.
    # The previous greenfield pool was dominated by negative/short-side IC:
    # high raw momentum, liquidity expansion, turnover heat, and relative strength
    # usually needed mechanical inversion to work.  These companion factors are
    # defined so a *positive* score directly represents the observed long alpha:
    # controlled overnight gap risk, anti-crowding / pullback from crowded heat,
    # and low-volatility resilience.  They are fully historical/backtestable and
    # intentionally do not use selection_history or legacy prebreakout inputs.
    out['g_long_gap_safety'] = (
        0.55 * (-out['f_gap_return'].abs())
        + 0.25 * (-out['f_intraday_reversal'].abs())
        + 0.20 * (-out['ret_std_10'])
    )
    out['g_long_anti_crowding_reversal'] = (
        -0.35 * out['ret_20d']
        -0.20 * out['f_price_vs_ma20']
        -0.15 * out['f_amount_zscore_20']
        +0.15 * out['f_chip_support_proxy']
        -0.15 * out['f_gap_return'].abs()
    )
    out['g_long_low_vol_resilience'] = (
        0.40 * (-out['ret_std_20'])
        + 0.25 * (-out['boll_width'])
        + 0.20 * out['f_chip_support_proxy']
        + 0.15 * (-out['rolling_drawdown_20'].abs())
    )
    out['g_long_liquidity_cooldown'] = (
        -0.35 * out['f_volume_ratio_20']
        -0.25 * out['f_amount_ratio_20']
        -0.20 * out['f_volume_cv_20']
        +0.20 * (-out['f_amihud_20'])
    )
    out['g_long_relative_pullback'] = (
        -0.45 * (out['ret_20d'] - out['industry_ret_20'])
        -0.25 * (out['f_price_vs_ma20'] - out['industry_price_vs_ma20'])
        -0.15 * (out['ret_5d'] - out['industry_ret_5'])
        +0.15 * out['f_chip_support_proxy']
    )

    # P1 v7 close-to-close companions.  These are not mechanical inversions of
    # short-side factors: they encode historically observable long-side conditions
    # that survived the v6 skipped early-fold diagnosis -- low gap/low realized
    # volatility, constructive intraday close, and mild rebound confirmation.
    # The goal is to broaden independent holdout-confirmed family supply before
    # selection, while keeping positive score == desired long alpha.
    out['g_long_c2c_calm_rebound'] = (
        -0.25 * out['f_gap_return'].abs()
        -0.20 * out['ret_std_10']
        -0.15 * out['ret_std_20']
        +0.20 * out['f_close_pos_20']
        -0.10 * out['rolling_drawdown_20'].abs()
        -0.10 * out['f_amount_zscore_20'].abs()
    )
    out['g_long_c2c_intraday_followthrough'] = (
        +0.30 * out['f_intraday_reversal']
        +0.20 * out['f_close_pos_20']
        -0.25 * out['f_gap_return'].abs()
        -0.15 * out['ret_std_10']
        -0.10 * out['f_amount_zscore_20'].abs()
    )
    out['g_long_c2c_defensive_momentum'] = (
        +0.25 * out['ret_5d']
        +0.20 * out['ret_10d']
        +0.15 * out['f_price_vs_ma20']
        -0.20 * out['ret_std_20']
        -0.10 * out['f_gap_return'].abs()
        +0.10 * out['f_chip_support_proxy']
    )

    # P1 v9 baseline-beating long-side companions.  These add independent
    # close-to-close supply from historically backtestable primitives that showed
    # positive long-side C2C evidence in the v8 supply audit.  They are not
    # mechanical short inversions: positive score means calm liquidity / low
    # realized volatility / supported pullback conditions intended for long alpha.
    out['g_long_c2c_low_vol_carry'] = (
        -0.45 * out['ret_std_20']
        -0.25 * out['boll_width']
        +0.20 * out['ret_5d']
        -0.10 * out['f_gap_return'].abs()
    )
    out['g_long_c2c_calm_liquidity'] = (
        -0.35 * out['f_volume_cv_20']
        -0.25 * out['f_amount_zscore_20'].abs()
        -0.25 * out['ret_std_10']
        +0.15 * out['f_close_pos_20']
    )
    out['g_long_c2c_supported_pullback'] = (
        -0.35 * out['ret_20d']
        +0.30 * out['f_chip_support_proxy']
        -0.20 * out['f_gap_return'].abs()
        -0.15 * out['ret_std_20']
    )

    # P1 v10 long-side C2C supply expansion.  These are genuine long-only
    # companion factors built from historical, pre-trade observables.  Positive
    # score means supported value reversion / industry-lag rebound in calm risk
    # conditions; they are not mechanical inversions of a short factor.
    out['g_long_c2c_value_reversion'] = (
        -0.55 * out['ret_20d']
        -0.25 * out['f_price_vs_ma20']
        +0.20 * out['f_close_pos_20']
        -0.15 * out['ret_std_20']
    )
    out['g_long_c2c_industry_lag_rebound'] = (
        -0.45 * (out['ret_20d'] - out['industry_ret_20'])
        +0.25 * out['industry_ret_5']
        +0.20 * out['f_chip_support_proxy']
        -0.15 * out['f_gap_return'].abs()
    )

    # P1 v12 fold/analog support companions.  A 2026-05-03 lightweight
    # probe targeted the previously skipped/weak folds 1/2/3/7 and equal-
    # weight C2C excess.  These formulas add genuinely long-side, historical
    # companion supply: industry-calm pullback, early oversold-with-chip-support,
    # and defensive rebound in low-volatility / normalized-liquidity contexts.
    # Positive score remains the intended long alpha; no mechanical short-side
    # inversion or promotion waiver is used.
    out['g_long_c2c_industry_calm_pullback'] = (
        -0.35 * (out['ret_20d'] - out['industry_ret_20'])
        -0.20 * out['ret_std_10']
        -0.20 * out['f_gap_return'].abs()
        +0.15 * out['industry_breadth']
        +0.10 * out['f_chip_support_proxy']
    )
    out['g_long_c2c_early_oversold_support'] = (
        -0.40 * out['ret_10d']
        -0.20 * out['ret_20d']
        +0.25 * out['f_chip_support_proxy']
        -0.15 * out['ret_std_20']
    )
    out['g_long_c2c_analog_defensive_rebound'] = (
        -0.30 * out['rolling_drawdown_20'].abs()
        -0.25 * out['ret_std_20']
        -0.20 * out['f_amount_zscore_20'].abs()
        +0.15 * out['ret_5d']
        +0.10 * out['f_close_pos_20']
    )

    # Phase2 new defense factors: ATR stability, RSI oversold bounce, Boll squeeze
    # These are genuine long-only factors using primitives not yet covered.
    # Positive score represents favorable long-side conditions.
    out['g_long_atr_stability'] = (
        -0.45 * out['atr_proxy_14']
        -0.25 * out['ret_std_20']
        +0.15 * out['f_chip_support_proxy']
        +0.15 * out['f_close_pos_20']
    )
    out['g_long_rsi_oversold_bounce'] = (
        -0.35 * out['rsi_6']
        -0.25 * out['rsi_12']
        +0.25 * out['f_chip_support_proxy']
        -0.15 * out['ret_std_20']
    )
    out['g_long_boll_squeeze'] = (
        -0.40 * out['boll_width']
        -0.20 * out['ret_std_20']
        +0.20 * out['f_close_pos_20']
        +0.20 * out['f_chip_support_proxy']
    )

    # Phase3 new independent dimension factors
    # These use primitives not yet covered by existing factors:
    # MACD, KDJ, CCI, cost distribution, market breadth, concentration

    # MACD momentum confirmation: positive = MACD bullish + low vol + chip support
    out['g_long_macd_momentum'] = (
        +0.35 * out['macd']
        +0.25 * (out['macd_dif'] - out['macd_dea'])
        -0.20 * out['ret_std_20']
        +0.20 * out['f_chip_support_proxy']
    )

    # KDJ oversold bounce: positive = KDJ low + chip support + low vol
    out['g_long_kdj_oversold'] = (
        -0.35 * out['kdj_k']
        -0.25 * out['kdj_d']
        +0.25 * out['f_chip_support_proxy']
        -0.15 * out['ret_std_20']
    )

    # CCI mean reversion: positive = CCI low + calm + chip support
    out['g_long_cci_reversion'] = (
        -0.40 * out['cci']
        -0.20 * out['ret_std_10']
        +0.20 * out['f_chip_support_proxy']
        +0.20 * out['f_close_pos_20']
    )

    # Cost distribution tightness: positive = narrow cost band + low vol + support
    out['g_long_cost_concentration'] = (
        -0.35 * out['f_chip_band_width']
        -0.25 * out['ret_std_20']
        +0.25 * out['f_chip_support_proxy']
        -0.15 * out['f_winner_rate_z_20'].abs()
    )

    # Market breadth sentiment: positive = breadth aligned + calm vol + chip support
    out['g_long_breadth_alignment'] = (
        +0.40 * out['industry_breadth']
        +0.20 * out['f_up_day_ratio_10']
        -0.25 * out['ret_std_20']
        +0.15 * out['f_chip_support_proxy']
    )

    # Winner rate resilience: positive = high winner rate + pullback + low vol
    out['g_long_winner_resilience'] = (
        +0.30 * out['f_winner_rate_level']
        -0.25 * out['ret_10d']
        -0.25 * out['ret_std_20']
        +0.20 * out['f_chip_support_proxy']
    )

    # ========== Phase B: 5 O2C-specific factors ==========
    # These factors are designed to predict next-day open-to-close returns
    # rather than close-to-close. They capture intraday dynamics that
    # traditional C2C factors miss.

    # 1) Opening gap reversal: negative gap → intraday bounce, positive gap → fade
    # High positive = likely intraday reversal opportunity
    out['g_o2c_gap_reversal'] = (
        -0.40 * out['f_gap_return']
        + 0.25 * out['f_intraday_reversal']
        - 0.20 * out['ret_std_10']
        + 0.15 * out['f_chip_support_proxy']
    )

    # 2) Intraday strength proxy: close near high → bullish continuation
    # Measures buying pressure into close → next-day open-to-close follow-through
    out['g_o2c_intraday_strength'] = (
        +0.35 * out['f_close_pos_20']
        - 0.25 * out['f_gap_return'].abs()
        + 0.20 * out['f_up_day_ratio_10']
        - 0.20 * out['ret_std_20']
    )

    # 3) Volatility-compressed breakout: low ATR + tight range → expansion ahead
    # Compressed volatility precedes O2C breakout moves
    out['g_o2c_vol_compression'] = (
        -0.35 * out['atr_proxy_14']
        - 0.30 * out['hl_range']
        + 0.20 * out['f_chip_support_proxy']
        + 0.15 * out['f_close_pos_20']
    )

    # 4) Volume-price divergence: volume spike without price move → accumulation
    # Elevated volume with muted price → next-day directional move
    out['g_o2c_volume_price_div'] = (
        +0.30 * out['f_amount_zscore_20']
        - 0.25 * out['hl_range']
        - 0.25 * out['ret_std_10']
        + 0.20 * out['f_chip_support_proxy']
    )

    # 5) Tail pressure: close near high + volume → bullish O2C continuation
    # Strong close with volume → next-day intraday momentum
    out['g_o2c_tail_pressure'] = (
        +0.35 * out['f_close_pos_20']
        + 0.25 * out['f_amount_ratio_20']
        - 0.20 * out['ret_std_10']
        - 0.20 * out['f_gap_return'].abs()
    )

    # ========== Phase B-revised: 5 O2C-specific factors (economic rationale) ==========
    # Designed from first principles for next-day open-to-close returns.
    # Each factor has a clear economic mechanism, not data-mined.

    # 1) Opening gap reversal: large gap down → intraday mean reversion
    # Economic rationale: Retail-dominated A-share market sees panic selling at open;
    # institutional buyers absorb during the day, causing bounce.
    gap = out['f_gap_return']
    hl_spread = out['high'] - out['low']
    # Continuous: tanh compresses to [-1, 1], all non-suspended stocks get non-zero value
    out['g_opening_gap_reversal'] = np.where(
        hl_spread > 0,  # exclude suspended (high=low)
        np.tanh(-gap / 0.03),  # scale=3%, tanh compress to [-1, 1]
        0.0
    )

    # 2) Intraday VWAP deviation: close below VWAP → mean reversion upward
    # Economic rationale: Market makers and algo traders buy below VWAP to push
    # price back toward the volume-weighted average.
    vwap_proxy = (out['high'] + out['low'] + out['close']) / 3.0
    vwap_dev = (out['close'] - vwap_proxy) / vwap_proxy.replace(0, np.nan)
    out['g_intraday_vwap_deviation'] = -vwap_dev  # below VWAP = high factor

    # 3) Close strength ratio (REVERSED): weak close → next-day intraday bounce
    # Economic rationale: Weak close (close near day's low) signals retail panic selling
    # into the close. In A-shares, retail-dominated late-day pricing is less efficient,
    # and this panic tends to be corrected next day. IC = -0.018 (NW t = -3.24) over
    # 500 days confirms this contrarian effect.
    out['g_close_strength_ratio'] = np.where(
        hl_spread > 0,
        (out['high'] - out['close']) / hl_spread,  # reversed: weak close = high value
        0.5
    )

    # 4) Intraday range expansion (REVERSED): range contraction + bearish close → exhaustion bounce
    # Economic rationale: Range contraction combined with bearish close signals selling
    # exhaustion. Low volatility + negative close indicates sellers have exhausted their
    # ammunition, setting up a mean-reversion bounce. IC = -0.025 (NW t = -4.13) over
    # 500 days strongly confirms this contrarian signal.
    out['_daily_range'] = hl_spread / out['close'].replace(0, np.nan)
    avg_range_20 = out.groupby('ts_code', group_keys=False)['_daily_range'].transform(
        lambda s: s.rolling(20, min_periods=10).mean()
    )
    range_ratio = np.where(avg_range_20 > 0, out['_daily_range'] / avg_range_20, 1.0)
    direction = np.where(hl_spread > 0, (out['close'] - out['open']) / hl_spread, 0.0)
    out['g_intraday_range_expansion'] = np.tanh(-range_ratio * direction)  # reversed: contraction+bearish = high
    out.drop(columns=['_daily_range'], inplace=True)

    # 5) Volume-price divergence: price drop + volume shrinkage = selling exhaustion → bounce
    # Economic rationale: When price falls but volume contracts, it signals that sellers
    # are exhausted. The lack of selling pressure sets up an intraday rebound.
    # Fix: continuous function, no hard filter
    price_change = out['f_gap_return'] + out['f_intraday_reversal']  # approx daily return
    vol_ma20 = out.groupby('ts_code', group_keys=False)['amount'].transform(
        lambda s: s.rolling(20, min_periods=10).mean()
    )
    volume_change = (out['amount'] - vol_ma20) / vol_ma20.replace(0, np.nan)
    out['g_volume_price_divergence'] = np.where(
        price_change < 0,
        np.tanh(-volume_change),  # volume shrinkage → high value, continuous
        -np.tanh(volume_change.abs()) * 0.3  # price up: mild negative signal
    )

    # ========== Phase C: 5 Overnight-return factors ==========
    # These factors directly target next-day overnight returns
    # (close_to_open). They address the C2C overnight gap drag that
    # makes the current long-only pool fail C2C guardrails despite
    # strong O2C performance. Positive score = expected positive
    # overnight drift.

    # 1) Overnight carry: sustained positive overnight drift + low reversal risk
    # High positive = historically favorable overnight holding pattern
    out['g_overnight_carry'] = (
        +0.35 * out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
            lambda s: s.rolling(10, min_periods=5).mean()
        )
        + 0.25 * out['f_close_pos_20']
        - 0.20 * out.groupby('ts_code', group_keys=False)['f_intraday_reversal'].transform(
            lambda s: s.abs().rolling(10, min_periods=5).mean()
        )
        - 0.20 * out['ret_std_10']
    )

    # 2) Gap mean reversion: recent negative gap → overnight bounce expected
    # High positive = oversold overnight setup with support
    out['g_overnight_gap_reversion'] = (
        -0.35 * out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
            lambda s: s.rolling(5, min_periods=3).mean()
        )
        + 0.25 * out['f_chip_support_proxy']
        - 0.20 * out['f_amount_zscore_20'].abs()
        + 0.20 * out['f_close_pos_20']
    )

    # 3) Overnight momentum: consecutive positive overnight returns
    # High positive = persistent overnight bid
    out['g_overnight_momentum'] = (
        +0.35 * out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
            lambda s: s.rolling(5, min_periods=3).mean()
        )
        + 0.25 * out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
            lambda s: s.rolling(20, min_periods=10).mean()
        )
        - 0.20 * out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
            lambda s: s.abs().rolling(10, min_periods=5).std()
        )
        - 0.20 * out['ret_std_20']
    )

    # 4) Calm close overnight: low intraday vol → positive overnight drift
    # High positive = quiet intraday → favorable overnight hold
    out['g_overnight_calm_close'] = (
        -0.35 * out.groupby('ts_code', group_keys=False)['f_intraday_reversal'].transform(
            lambda s: s.abs().rolling(5, min_periods=3).mean()
        )
        - 0.25 * out['ret_std_10']
        + 0.20 * out['f_chip_support_proxy']
        + 0.20 * out['f_close_pos_20']
    )

    # 5) Overnight-volatility ratio: overnight moves dominate intraday → carry opportunity
    # High positive = stock where overnight gap > intraday move (carry potential)
    gap_abs_10 = out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
        lambda s: s.abs().rolling(10, min_periods=5).mean()
    )
    intraday_abs_10 = out.groupby('ts_code', group_keys=False)['f_intraday_reversal'].transform(
        lambda s: s.abs().rolling(10, min_periods=5).mean()
    )
    out['g_overnight_vol_ratio'] = (
        +0.40 * (gap_abs_10 - intraday_abs_10)
        + 0.25 * out.groupby('ts_code', group_keys=False)['f_gap_return'].transform(
            lambda s: s.rolling(10, min_periods=5).mean()
        )
        - 0.20 * out['ret_std_20']
        + 0.15 * out['f_chip_support_proxy']
    )

    # ---- External-borrowing batch (2026-08-25) ----
    # Three factor hypotheses distilled from Dzy-HW-XD/a-share-quant-selector
    # (bowl-rebound strategy), reformulated as continuous backtestable factors.
    # See research/20260825_external_borrowing_hypotheses.md for rationale.

    # H1: g_max_vol_down_day -- sign of return on the max-volume day within the
    # lookback window.  Max-volume day closing up = accumulation; closing down =
    # distribution.  Continuous version of the source project's binary filter.
    def _max_vol_day_ret(g: pd.DataFrame) -> pd.Series:
        vol_w = g['vol'].rolling(10, min_periods=5)
        # idxmax position of vol in window -> align ret_1d
        def _pick(sub: pd.DataFrame) -> pd.Series:
            # sub: window rows; use numpy sliding argmax
            v = sub['vol'].to_numpy()
            r = sub['ret_1d'].to_numpy()
            n = len(v)
            res = np.full(n, np.nan)
            w = 10
            for i in range(w - 1, n):
                win_v = v[i - w + 1:i + 1]
                win_r = r[i - w + 1:i + 1]
                if np.isnan(win_v).all():
                    continue
                j = int(np.nanargmax(win_v))
                res[i] = win_r[j]
            return pd.Series(res, index=sub.index)
        return _pick(g)

    out['g_max_vol_down_day'] = (
        out.groupby('ts_code', group_keys=False, sort=False)
        .apply(_max_vol_day_ret)
        .reset_index(level=0, drop=True)
        .reindex(out.index)
    )

    # H2: g_surge_yang_oversold -- volume-surge yang-day strength x KDJ-J depth.
    # Uses existing kdj_j primitive if present; otherwise computes proxy.
    if 'kdj_j' in out.columns:
        j_depth = -out['kdj_j']
    else:
        j_depth = -out['f_close_pos_20'] * 100.0  # fallback proxy
    surge_yang = out['f_volume_ratio_5'].where(out['f_intraday_reversal'] > 0, 0.0)
    surge_yang = out.groupby('ts_code', group_keys=False)['f_volume_ratio_5'].transform(
        lambda s: s.rolling(5, min_periods=3).max()
    ).where(out['f_intraday_reversal'] > 0, 0.0)
    # cross-sectional z via rank to bound outliers
    surge_rank = surge_yang.groupby(out['trade_date']).rank(pct=True) - 0.5
    j_rank = j_depth.groupby(out['trade_date']).rank(pct=True) - 0.5
    out['g_surge_yang_oversold'] = surge_rank * j_rank

    # H3: g_pattern_dtw_sim -- negative self-similarity DTW distance between the
    # last 20-day normalized price curve and the previous 20-day curve.
    # Small DTW distance = regular consolidation pattern -> positive score.
    def _dtw_dist(a: np.ndarray, b: np.ndarray) -> float:
        n, m = len(a), len(b)
        D = np.full((n + 1, m + 1), np.inf)
        D[0, 0] = 0.0
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                cost = abs(a[i - 1] - b[j - 1])
                D[i, j] = cost + min(D[i - 1, j], D[i, j - 1], D[i - 1, j - 1])
        return D[n, m] / max(n, m)

    def _pattern_sim(g: pd.DataFrame) -> pd.Series:
        close = g['close'].to_numpy()
        n = len(close)
        res = np.full(n, np.nan)
        w = 20
        for i in range(2 * w - 1, n):
            cur = close[i - w + 1:i + 1]
            prev = close[i - 2 * w + 1:i - w + 1]
            if np.isnan(cur).any() or np.isnan(prev).any() or prev[0] == 0 or cur[0] == 0:
                continue
            cur_n = cur / cur[0] - 1.0
            prev_n = prev / prev[0] - 1.0
            res[i] = -_dtw_dist(cur_n, prev_n)
        return pd.Series(res, index=g.index)

    out['g_pattern_dtw_sim'] = (
        out.groupby('ts_code', group_keys=False, sort=False)
        .apply(_pattern_sim)
        .reset_index(level=0, drop=True)
        .reindex(out.index)
    )

    return out


def add_forward_labels(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)  # no .copy() to avoid OOM
    next_close = out.groupby('ts_code', group_keys=False)['close'].shift(-1)
    next_open = out.groupby('ts_code', group_keys=False)['open'].shift(-1)
    curr_close = out['close'].replace(0, np.nan)
    out['next_return_close_to_close_1d'] = next_close / curr_close - 1.0
    out['next_return_open_to_close_1d'] = next_close / next_open.replace(0, np.nan) - 1.0
    out['next_return_close_to_open_1d'] = next_open / curr_close - 1.0
    return out


def build_greenfield_panel(cfg: GreenfieldConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    panel = load_raw_history_panel(cfg)
    if panel.empty:
        return panel, pd.DataFrame()
    panel = attach_core_features(panel)
    panel = panel[panel['listing_days'].fillna(0) >= cfg.min_listing_days]
    industry_map = load_industry_map(cfg)
    panel = attach_industry_features(panel, industry_map)
    panel = add_greenfield_factors(panel)
    panel = add_forward_labels(panel)
    regimes = build_daily_regime_features(panel)
    panel = panel.merge(regimes, on='trade_date', how='left')
    panel = panel.sort_values(['trade_date', 'ts_code']).reset_index(drop=True)
    return panel, regimes


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    def _z(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors='coerce')
        mean = s.mean(skipna=True)
        std = s.std(skipna=True)
        if pd.isna(std) or std == 0:
            out = pd.Series(np.nan, index=s.index, dtype=float)
            out.loc[s.notna()] = 0.0
            return out
        return (s - mean) / std
    return df.groupby('trade_date', group_keys=False)[col].apply(_z)


def add_cross_sectional_zscores(df: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    # Memory-optimized: operate in-place to avoid OOM (df.copy() doubles 4.8GB→9.6GB on 16GB Mac)
    for col in factor_cols:
        df[f'{col}__z'] = zscore_by_date(df, col)
    return df


def max_drawdown(returns: pd.Series) -> float:
    x = returns.dropna()
    if x.empty:
        return 0.0
    nav = (1.0 + x).cumprod()
    peak = nav.cummax()
    dd = nav / peak - 1.0
    return float(dd.min())


def annualized_return(returns: pd.Series) -> float:
    x = returns.dropna()
    if x.empty:
        return 0.0
    total = float((1.0 + x).prod() - 1.0)
    years = max(len(x) / 252.0, 1 / 252.0)
    return float((1.0 + total) ** (1.0 / years) - 1.0)


def annualized_sharpe(returns: pd.Series) -> float:
    x = returns.dropna()
    if len(x) < 2:
        return 0.0
    std = float(x.std())
    if std == 0 or np.isnan(std):
        return 0.0
    return float(x.mean() / std * np.sqrt(252))


def calmar_ratio(returns: pd.Series) -> float:
    mdd = abs(max_drawdown(returns))
    if mdd == 0:
        return 0.0
    return annualized_return(returns) / mdd


@dataclass(frozen=True)
class MaterializationSelectionOverlay:
    source_path: str | None = None
    candidate_id: str | None = None
    contract_version: str | None = None
    top_n: int | None = None
    max_names_per_industry: int | None = None
    min_listing_days: int | None = None
    industry_blacklist: tuple[str, ...] = ()
    gap_risk_filter_factor_id: str | None = None
    gap_risk_filter_operator: str | None = None
    gap_risk_filter_min_zscore: float | None = None


def effective_top_n(top_n: int, selection_overlay: MaterializationSelectionOverlay | None = None) -> int:
    if selection_overlay is not None and selection_overlay.top_n is not None:
        return int(selection_overlay.top_n)
    return int(top_n)


def selection_overlay_summary(
    selection_overlay: MaterializationSelectionOverlay | None,
    default_top_n: int | None = None,
) -> dict[str, Any] | None:
    if selection_overlay is None:
        return None
    gap_risk_filter = None
    if selection_overlay.gap_risk_filter_factor_id:
        gap_risk_filter = {
            'factor_id': selection_overlay.gap_risk_filter_factor_id,
            'operator': selection_overlay.gap_risk_filter_operator,
            'min_zscore': selection_overlay.gap_risk_filter_min_zscore,
        }
    return {
        'source_path': selection_overlay.source_path,
        'candidate_id': selection_overlay.candidate_id,
        'contract_version': selection_overlay.contract_version,
        'top_n': selection_overlay.top_n if selection_overlay.top_n is not None else default_top_n,
        'max_names_per_industry': selection_overlay.max_names_per_industry,
        'min_listing_days': selection_overlay.min_listing_days,
        'industry_blacklist': list(selection_overlay.industry_blacklist),
        'gap_risk_filter': gap_risk_filter,
    }


def _overlay_operator_mask(series: pd.Series, operator: str | None, threshold: float) -> pd.Series:
    op = str(operator or '>=').strip() or '>='
    if op == '>=':
        return series >= threshold
    if op == '>':
        return series > threshold
    if op == '<=':
        return series <= threshold
    if op == '<':
        return series < threshold
    if op == '==':
        return series == threshold
    raise ValueError(f'unsupported overlay operator: {op}')


def select_ranked_holdings(
    day: pd.DataFrame,
    score_col: str,
    top_n: int,
    return_col: str | None = None,
    selection_overlay: MaterializationSelectionOverlay | None = None,
) -> pd.DataFrame:
    required_cols = [score_col]
    if return_col:
        required_cols.append(return_col)
    subset = day.dropna(subset=required_cols).copy()
    if subset.empty:
        return subset

    if selection_overlay is not None:
        if selection_overlay.min_listing_days is not None and 'listing_days' in subset.columns:
            listing_days = pd.to_numeric(subset['listing_days'], errors='coerce').fillna(-1)
            subset = subset.loc[listing_days >= selection_overlay.min_listing_days].copy()
        if subset.empty:
            return subset

        if selection_overlay.industry_blacklist and 'industry' in subset.columns:
            blocked = set(selection_overlay.industry_blacklist)
            subset = subset.loc[~subset['industry'].astype(str).isin(blocked)].copy()
        if subset.empty:
            return subset

        if selection_overlay.gap_risk_filter_factor_id:
            factor_id = selection_overlay.gap_risk_filter_factor_id
            if factor_id not in subset.columns:
                return subset.iloc[0:0].copy()
            values = pd.to_numeric(subset[factor_id], errors='coerce')
            threshold = float(selection_overlay.gap_risk_filter_min_zscore or 0.0)
            mask = values.notna() & _overlay_operator_mask(values, selection_overlay.gap_risk_filter_operator, threshold)
            subset = subset.loc[mask].copy()
        if subset.empty:
            return subset

    ranking_cols = [score_col]
    ranking_ascending = [False]
    if 'ts_code' in subset.columns:
        ranking_cols.append('ts_code')
        ranking_ascending.append(True)
    subset = subset.sort_values(ranking_cols, ascending=ranking_ascending, kind='mergesort').reset_index(drop=True)

    allow_partial_selection = selection_overlay is not None
    max_names_per_industry = selection_overlay.max_names_per_industry if selection_overlay is not None else None
    if max_names_per_industry is None:
        if not allow_partial_selection and len(subset) < top_n:
            return subset.iloc[0:0].copy()
        return subset.head(top_n).copy().reset_index(drop=True)

    if 'industry' not in subset.columns:
        subset['industry'] = '未分类'

    industry_counts: dict[str, int] = {}
    chosen_rows: list[dict[str, Any]] = []
    for row in subset.to_dict(orient='records'):
        industry = str(row.get('industry') or '未分类')
        if industry_counts.get(industry, 0) >= max_names_per_industry:
            continue
        chosen_rows.append(row)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(chosen_rows) >= top_n:
            break

    if not chosen_rows:
        return subset.iloc[0:0].copy()
    return pd.DataFrame(chosen_rows)


def evaluate_portfolio(
    df: pd.DataFrame,
    score_col: str,
    return_col: str,
    top_n: int,
    round_trip_cost: float,
    min_positions: int = 10,
    selection_overlay: MaterializationSelectionOverlay | None = None,
    rebalance_interval: int = 1,  # 新增：调仓间隔（交易日数），1=每天，5=每周，20=每月
) -> dict[str, Any]:
    prev_holdings: set[str] = set()
    rows: list[dict[str, Any]] = []
    resolved_top_n = effective_top_n(top_n, selection_overlay)
    day_counter = 0
    current_holdings: set[str] = set()
    for trade_date, day in df.groupby('trade_date', sort=True):
        day_counter += 1
        # 只在调仓日重新选择持仓
        if day_counter == 1 or day_counter % rebalance_interval == 1:
            chosen = select_ranked_holdings(
                day,
                score_col=score_col,
                return_col=return_col,
                top_n=resolved_top_n,
                selection_overlay=selection_overlay,
            )
            required_positions = max(1, min(min_positions, resolved_top_n)) if selection_overlay is not None else max(1, min_positions)
            if len(chosen) < required_positions:
                continue
            new_holdings = set(chosen['ts_code'].astype(str))
            turnover = 1.0 if not prev_holdings else 1.0 - len(new_holdings.intersection(prev_holdings)) / max(1, len(new_holdings))
            turnover = min(max(float(turnover), 0.0), 1.0)
            prev_holdings = new_holdings
            current_holdings = new_holdings
        else:
            # 非调仓日：使用现有持仓，过滤出当天有数据的股票
            available = set(day['ts_code'].astype(str))
            active_holdings = current_holdings.intersection(available)
            if not active_holdings:
                continue
            chosen = day[day['ts_code'].astype(str).isin(active_holdings)]
            turnover = 0.0  # 非调仓日无换手
        gross = float(chosen[return_col].mean())
        cost_drag = round_trip_cost * turnover
        net = gross - cost_drag
        rows.append({
            'trade_date': str(trade_date),
            'gross_return': gross,
            'net_return': net,
            'cost_drag': cost_drag,
            'turnover': turnover,
            'holding_count': int(len(chosen)),
        })
    daily = pd.DataFrame(rows)
    if daily.empty:
        return {
            'daily': daily,
            'metrics': {
                'days': 0,
                'total_return': 0.0,
                'gross_return': 0.0,
                'net_return': 0.0,
                'annualized_return': 0.0,
                'gross_annualized_return': 0.0,
                'net_annualized_return': 0.0,
                'total_cost_drag': 0.0,
                'sharpe': 0.0,
                'max_drawdown': 0.0,
                'calmar': 0.0,
                'win_rate': 0.0,
                'avg_turnover': 0.0,
            },
        }
    gross = daily['gross_return']
    net = daily['net_return']
    gross_total = float((1.0 + gross).prod() - 1.0)
    net_total = float((1.0 + net).prod() - 1.0)
    metrics = {
        'days': int(len(daily)),
        'total_return': net_total,
        'gross_return': gross_total,
        'net_return': net_total,
        'annualized_return': annualized_return(net),
        'gross_annualized_return': annualized_return(gross),
        'net_annualized_return': annualized_return(net),
        'total_cost_drag': float(daily.get('cost_drag', pd.Series(dtype=float)).sum()),
        'sharpe': annualized_sharpe(net),
        'max_drawdown': max_drawdown(net),
        'calmar': calmar_ratio(net),
        'win_rate': float((net > 0).mean()),
        'avg_turnover': float(daily['turnover'].mean()),
    }
    return {'daily': daily, 'metrics': metrics}


def spearman_corr(x: pd.Series, y: pd.Series) -> float:
    pair = pd.DataFrame({'x': pd.to_numeric(x, errors='coerce'), 'y': pd.to_numeric(y, errors='coerce')}).dropna()
    if len(pair) < 10:
        return float('nan')
    xr = pair['x'].rank(method='average')
    yr = pair['y'].rank(method='average')
    corr = xr.corr(yr, method='pearson')
    return float(corr) if pd.notna(corr) else float('nan')


def compute_factor_diagnostics(
    df: pd.DataFrame,
    factor_col: str,
    return_col: str,
    top_n: int,
    round_trip_cost: float,
    selection_overlay: MaterializationSelectionOverlay | None = None,
) -> dict[str, Any]:
    rank_ic_rows = []
    for trade_date, day in df.groupby('trade_date', sort=True):
        subset = day[[factor_col, return_col]].dropna()
        if len(subset) < max(20, top_n):
            continue
        ic = spearman_corr(subset[factor_col], subset[return_col])
        if pd.notna(ic):
            rank_ic_rows.append(ic)
    rank_ic_mean = float(np.mean(rank_ic_rows)) if rank_ic_rows else 0.0
    portfolio = evaluate_portfolio(
        df,
        factor_col,
        return_col,
        top_n=top_n,
        round_trip_cost=round_trip_cost,
        selection_overlay=selection_overlay,
    )
    metrics = portfolio['metrics']
    return {
        'factor_id': factor_col,
        'rank_ic_mean': rank_ic_mean,
        'rank_ic_abs': abs(rank_ic_mean),
        'portfolio_metrics': metrics,
        'days': metrics['days'],
    }


def normalize_weights(raw_weights: dict[str, float]) -> dict[str, float]:
    positive = {k: float(v) for k, v in raw_weights.items() if float(v) > 0}
    total = sum(positive.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in positive.items()}


def combine_factor_scores(df: pd.DataFrame, weights: dict[str, float], output_col: str = 'composite_score') -> pd.DataFrame:
    out = df  # in-place to avoid OOM on 16GB Mac
    score = pd.Series(0.0, index=out.index, dtype=float)
    for factor_id, weight in weights.items():
        z_col = f'{factor_id}__z'
        if z_col not in out.columns:
            raise KeyError(f'missing zscore column: {z_col}')
        score = score.add(out[z_col].fillna(0.0) * float(weight), fill_value=0.0)
    out[output_col] = score
    return out


def build_walkforward_slices(trade_dates: list[str], train_days: int, validation_days: int, test_days: int, step_days: int, min_train_days: int) -> list[dict[str, list[str]]]:
    folds: list[dict[str, list[str]]] = []
    n = len(trade_dates)
    train_end = max(train_days, min_train_days)
    while True:
        val_end = train_end + validation_days
        test_end = val_end + test_days
        if test_end > n:
            break
        train_slice = trade_dates[max(0, train_end - train_days):train_end]
        if len(train_slice) < min_train_days:
            train_end += step_days
            continue
        folds.append({
            'train_dates': train_slice,
            'validation_dates': trade_dates[train_end:val_end],
            'test_dates': trade_dates[val_end:test_end],
        })
        train_end += step_days
    return folds


def current_state_from_panel(regimes: pd.DataFrame) -> dict[str, Any]:
    latest = regimes.iloc[-1].to_dict() if not regimes.empty else {}
    return latest
