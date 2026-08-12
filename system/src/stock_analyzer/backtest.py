#!/usr/bin/env python3
from __future__ import annotations

"""
A股量化选股回测框架 (快速版)
直接使用Tushare批量数据，不逐只读parquet

用法:
    python3 backtest.py --days 60 --top 20
    python3 backtest.py --days 30 --top 10
"""

import tushare as ts
import pandas as pd
import numpy as np
import json
import os
import sys
import time
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as pl

CACHE_DIR = pl.WORKING_DIR / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STOCK_LIST_FILE = pl.ROOT_DIR / "stock_list.csv"
STOCK_SYSTEM_ROOT = Path(os.environ.get("STOCK_SYSTEM_ROOT", str(Path(__file__).resolve().parents[3])))
PIT_UNIVERSE_DIR = Path(os.environ.get("OPENCLAW_PIT_UNIVERSE_DIR", str(STOCK_SYSTEM_ROOT / "factor_factory" / "data" / "universe")))
CYQ_TS_CODE_CHUNK_SIZE = int(os.environ.get("OPENCLAW_CYQ_TS_CODE_CHUNK_SIZE", "1000"))
CYQ_BATCH_SLEEP_SECONDS = float(os.environ.get("OPENCLAW_CYQ_BATCH_SLEEP_SECONDS", "0.05"))
ENABLE_AKSHARE_CYQ_FALLBACK = os.environ.get("OPENCLAW_ENABLE_AKSHARE_CYQ_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
ENABLE_LOCAL_CYQ_PROXY_FALLBACK = os.environ.get("OPENCLAW_ENABLE_LOCAL_CYQ_PROXY_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
AKSHARE_CYQ_LIMIT = int(os.environ.get("OPENCLAW_AKSHARE_CYQ_LIMIT", "0"))
AKSHARE_CYQ_MIN_ROWS = max(int(os.environ.get("OPENCLAW_AKSHARE_CYQ_MIN_ROWS", os.environ.get("OPENCLAW_STAGE1_MIN_ROW_COUNT", "1000"))), 0)
AKSHARE_CYQ_MAX_RUNTIME_SECONDS = max(int(os.environ.get("OPENCLAW_AKSHARE_CYQ_MAX_RUNTIME_SECONDS", "120")), 0)
AKSHARE_CYQ_RETRIES = int(os.environ.get("OPENCLAW_AKSHARE_CYQ_RETRIES", "2"))
AKSHARE_CYQ_RETRY_SLEEP_SECONDS = float(os.environ.get("OPENCLAW_AKSHARE_CYQ_RETRY_SLEEP_SECONDS", "0.5"))
AKSHARE_CYQ_REQUEST_SLEEP_SECONDS = float(os.environ.get("OPENCLAW_AKSHARE_CYQ_REQUEST_SLEEP_SECONDS", "0.1"))
AKSHARE_CYQ_TIMEOUT_SECONDS = int(os.environ.get("OPENCLAW_AKSHARE_CYQ_TIMEOUT_SECONDS", "180"))
EASTMONEY_PATCH_MIN_SLEEP_SECONDS = float(os.environ.get("OPENCLAW_EASTMONEY_PATCH_MIN_SLEEP", "0"))
EASTMONEY_PATCH_MAX_SLEEP_SECONDS = float(os.environ.get("OPENCLAW_EASTMONEY_PATCH_MAX_SLEEP", "0.1"))
EASTMONEY_PATCH_TIMEOUT_SECONDS = float(os.environ.get("OPENCLAW_EASTMONEY_PATCH_TIMEOUT", "15"))
AKSHARE_CYQ_PYTHON_OVERRIDE = str(os.environ.get("OPENCLAW_AKSHARE_CYQ_PYTHON", "")).strip()
AKSHARE_CYQ_HELPER = Path(os.environ.get("OPENCLAW_AKSHARE_CYQ_HELPER", str(Path(__file__).resolve().parent / "fetch_akshare_cyq_fallback.py")))
ENABLE_MARKET_SNAPSHOT_FALLBACK = os.environ.get("OPENCLAW_ENABLE_MARKET_SNAPSHOT_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
MARKET_SNAPSHOT_PYTHON_OVERRIDE = str(os.environ.get("OPENCLAW_MARKET_SNAPSHOT_PYTHON", "")).strip()
MARKET_SNAPSHOT_HELPER = Path(os.environ.get("OPENCLAW_MARKET_SNAPSHOT_HELPER", str(Path(__file__).resolve().parent / "fetch_market_snapshot_fallback.py")))
MARKET_SNAPSHOT_PROVIDERS = str(os.environ.get("OPENCLAW_MARKET_SNAPSHOT_PROVIDERS", "akshare_sina,efinance,akshare_em")).strip()
ENABLE_LOCAL_STK_FACTOR_FALLBACK = os.environ.get("OPENCLAW_ENABLE_LOCAL_STK_FACTOR_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
STK_FACTOR_DAILY_LOOKBACK = max(int(os.environ.get("OPENCLAW_STK_FACTOR_DAILY_LOOKBACK", "80")), 35)
LOCAL_CYQ_PROXY_TRAIN_DATES = max(int(os.environ.get("OPENCLAW_LOCAL_CYQ_PROXY_TRAIN_DATES", "40")), 10)
LOCAL_CYQ_PROXY_MIN_TRAIN_ROWS = max(int(os.environ.get("OPENCLAW_LOCAL_CYQ_PROXY_MIN_TRAIN_ROWS", "5000")), 500)
LOCAL_CYQ_PROXY_FEATURE_COLUMNS = (
    'intercept',
    'close',
    'vwap',
    'boll_mid',
    'intraday',
    'trend_gap',
    'pct_chg',
    'rsi_6',
    'macd_dif',
)
LOCAL_CYQ_PROXY_TARGET_COLUMNS = (
    'weight_avg',
    'cost_15pct',
    'cost_50pct',
    'cost_85pct',
    'winner_rate',
)
LOCAL_CYQ_PROXY_DEFAULT_COEFFICIENTS = {
    'weight_avg': np.array([0.7536, -0.0678, 0.5254, 0.5655, -0.1077, 0.2110, -0.0108, -0.0176, -0.3210], dtype=float),
    'cost_15pct': np.array([0.8671, -0.0546, 0.4503, 0.5617, -0.0991, -0.0327, -0.0095, -0.0173, 0.0720], dtype=float),
    'cost_50pct': np.array([0.7342, -0.0975, 0.5318, 0.5673, 0.0731, 0.2929, -0.0237, -0.0151, -0.1121], dtype=float),
    'cost_85pct': np.array([0.2843, -0.1495, 0.5981, 0.6362, 0.0912, 0.4208, -0.0162, -0.0132, -0.7211], dtype=float),
    'winner_rate': np.array([-26.8360, 1.5598, -1.8077, 0.2436, -0.0172, -0.0411, 0.3735, 1.4029, 0.5541], dtype=float),
}
_LOCAL_CYQ_PROXY_MODEL_CACHE: dict[str, dict | None] = {}
LAST_UNIVERSE_STATUS: dict[str, object] = {}


class ProxyDataForbidden(RuntimeError):
    """Raised when proxy-generated CYQ data is about to enter scoring."""


def _resolve_external_data_python(*overrides: str) -> Path | None:
    candidates: list[Path] = []
    for override in overrides:
        if override:
            candidates.append(Path(override))
    candidates.extend([
        STOCK_SYSTEM_ROOT / "external/daily_stock_analysis/venv/bin/python3",
        Path.home() / "daily_stock_analysis/venv/bin/python",
    ])
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return None


def _resolve_akshare_cyq_python() -> Path | None:
    return _resolve_external_data_python(AKSHARE_CYQ_PYTHON_OVERRIDE)


def get_trade_dates(pro, n_days):
    """获取最近N个交易日"""
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=n_days * 2)).strftime('%Y%m%d')
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
    cal = cal[cal['is_open'] == 1].sort_values('cal_date')
    dates = cal['cal_date'].tolist()
    return dates[-n_days:] if len(dates) >= n_days else dates


def _load_pit_universe_frame(trade_date):
    global LAST_UNIVERSE_STATUS
    path = PIT_UNIVERSE_DIR / f'universe_{trade_date}.parquet'
    if not path.exists():
        LAST_UNIVERSE_STATUS = {'status': 'missing', 'trade_date': str(trade_date), 'path': str(path)}
        return None
    try:
        universe = pd.read_parquet(path).copy()
    except Exception as e:
        LAST_UNIVERSE_STATUS = {'status': 'error', 'trade_date': str(trade_date), 'path': str(path), 'error': str(e)}
        return None
    if 'ts_code' not in universe.columns:
        LAST_UNIVERSE_STATUS = {'status': 'invalid', 'trade_date': str(trade_date), 'path': str(path), 'error': 'missing ts_code'}
        return None
    universe['ts_code'] = universe['ts_code'].astype(str).str.strip()
    if 'universe_flag' in universe.columns:
        universe = universe[universe['universe_flag'].fillna(0).astype(float) > 0].copy()
    LAST_UNIVERSE_STATUS = {'status': 'ok', 'trade_date': str(trade_date), 'path': str(path), 'count': int(len(universe))}
    return universe


def _load_pit_universe_codes(trade_date) -> list[str]:
    universe = _load_pit_universe_frame(trade_date)
    if universe is None or universe.empty:
        return []
    return universe['ts_code'].dropna().astype(str).drop_duplicates().tolist()


def _fetch_stk_factor_by_ts_code_batches(pro, trade_date):
    """Fetch stk_factor in ts_code batches when bulk fetch returns 0 rows."""
    codes = _load_pit_universe_codes(trade_date)
    if not codes:
        print(f"   ⚠️ stk_factor {trade_date}: PIT universe missing/unavailable; skip ts_code current-list fallback")
        return None

    frames = []
    for offset in range(0, len(codes), CYQ_TS_CODE_CHUNK_SIZE):
        chunk_codes = codes[offset:offset + CYQ_TS_CODE_CHUNK_SIZE]
        if not chunk_codes:
            continue
        try:
            chunk = ','.join(chunk_codes)
            df = pro.stk_factor(ts_code=chunk, trade_date=trade_date)
            if df is not None and len(df) > 0:
                frames.append(df)
        except Exception as e:
            print(f"   ⚠️ stk_factor ts_code_batch {trade_date} chunk@{offset}: {e}")
        time.sleep(CYQ_BATCH_SLEEP_SECONDS)

    if not frames:
        return None

    merged = pd.concat(frames, ignore_index=True)
    if 'ts_code' in merged.columns and 'trade_date' in merged.columns:
        merged = merged.drop_duplicates(subset=['ts_code', 'trade_date'], keep='last')
    merged['source_provider'] = 'tushare_stk_factor_ts_code_batch'
    return merged


def _fetch_cyq_perf_by_ts_code_batches(pro, trade_date):
    codes = _load_pit_universe_codes(trade_date)
    if not codes:
        print(f"   ⚠️ cyq_perf {trade_date}: PIT universe missing/unavailable; skip ts_code current-list fallback")
        return None

    frames = []
    for offset in range(0, len(codes), CYQ_TS_CODE_CHUNK_SIZE):
        chunk_codes = codes[offset:offset + CYQ_TS_CODE_CHUNK_SIZE]
        if not chunk_codes:
            continue
        try:
            chunk = ','.join(chunk_codes)
            df = pro.cyq_perf(ts_code=chunk, trade_date=trade_date)
            if df is not None and len(df) > 0:
                frames.append(df)
        except Exception as e:
            print(f"   ⚠️ cyq_perf ts_code_batch {trade_date} chunk@{offset}: {e}")
        time.sleep(CYQ_BATCH_SLEEP_SECONDS)

    if not frames:
        return None

    merged = pd.concat(frames, ignore_index=True)
    if 'ts_code' in merged.columns and 'trade_date' in merged.columns:
        merged = merged.drop_duplicates(subset=['ts_code', 'trade_date'], keep='last')
    merged['source_provider'] = 'tushare_cyq_ts_code_batch'
    return merged


def _read_helper_output(output_path: Path):
    csv_path = output_path.with_suffix('.csv')
    if output_path.exists():
        frame = pd.read_parquet(output_path)
    elif csv_path.exists():
        frame = pd.read_csv(csv_path)
    else:
        return None

    for col in ['ts_code', 'trade_date', 'source_provider', 'source_timestamp']:
        if col in frame.columns:
            frame[col] = frame[col].astype(str)
    return frame


def _fetch_daily_from_market_snapshot(trade_date, output_path: Path):
    python_exec = _resolve_external_data_python(MARKET_SNAPSHOT_PYTHON_OVERRIDE, AKSHARE_CYQ_PYTHON_OVERRIDE)
    if python_exec is None:
        print("   ⚠️ market snapshot python missing: no usable interpreter found")
        return None
    if not MARKET_SNAPSHOT_HELPER.exists():
        print(f"   ⚠️ market snapshot helper missing: {MARKET_SNAPSHOT_HELPER}")
        return None

    command = [
        str(python_exec),
        str(MARKET_SNAPSHOT_HELPER),
        '--trade-date',
        str(trade_date),
        '--output',
        str(output_path),
        '--providers',
        MARKET_SNAPSHOT_PROVIDERS,
    ]
    if STOCK_LIST_FILE.exists():
        command.extend(['--stock-list', str(STOCK_LIST_FILE)])

    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = '\n'.join((proc.stdout or '').splitlines()[-10:] + (proc.stderr or '').splitlines()[-10:])
        print(f"   ⚠️ market snapshot helper {trade_date} failed rc={proc.returncode}\n{tail}")
        return None
    return _read_helper_output(output_path)


def _round_or_nan(value, digits=3):
    try:
        if pd.isna(value):
            return np.nan
        return round(float(value), digits)
    except Exception:
        return np.nan


def _compute_rsi_series(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100)
    rsi = rsi.where(avg_gain != 0, 0)
    return rsi.fillna(50)


def _compute_kdj_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 9):
    low_n = low.rolling(period, min_periods=period).min()
    high_n = high.rolling(period, min_periods=period).max()
    denominator = (high_n - low_n).replace(0, np.nan)
    rsv = ((close - low_n) / denominator * 100).fillna(50)

    k_values: list[float] = []
    d_values: list[float] = []
    prev_k = 50.0
    prev_d = 50.0
    for item in rsv.tolist():
        value = 50.0 if pd.isna(item) else float(item)
        prev_k = prev_k * 2 / 3 + value / 3
        prev_d = prev_d * 2 / 3 + prev_k / 3
        k_values.append(prev_k)
        d_values.append(prev_d)
    k = pd.Series(k_values, index=close.index, dtype=float)
    d = pd.Series(d_values, index=close.index, dtype=float)
    j = 3 * k - 2 * d
    return k, d, j


def _compute_cci_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    typical_price = (high + low + close) / 3.0
    moving_average = typical_price.rolling(period, min_periods=period).mean()
    mean_deviation = typical_price.rolling(period, min_periods=period).apply(
        lambda values: float(np.mean(np.abs(values - np.mean(values)))), raw=True
    )
    denominator = (0.015 * mean_deviation).replace(0, np.nan)
    return (typical_price - moving_average) / denominator


def _build_stk_factor_from_daily_cache(trade_date, daily_frame: pd.DataFrame | None = None):
    available_dates = sorted(
        path.stem.replace('daily_', '')
        for path in CACHE_DIR.glob('daily_*.parquet')
        if path.stem.replace('daily_', '').isdigit() and path.stem.replace('daily_', '') <= str(trade_date)
    )
    if daily_frame is not None and not daily_frame.empty and str(trade_date) not in available_dates:
        available_dates.append(str(trade_date))
        available_dates = sorted(set(available_dates))

    if str(trade_date) not in available_dates:
        return None

    selected_dates = available_dates[-STK_FACTOR_DAILY_LOOKBACK:]
    if str(trade_date) not in selected_dates:
        selected_dates.append(str(trade_date))
        selected_dates = sorted(selected_dates)

    frames = []
    for current_date in selected_dates:
        if daily_frame is not None and str(current_date) == str(trade_date):
            current = daily_frame.copy()
        else:
            current_path = CACHE_DIR / f'daily_{current_date}.parquet'
            if not current_path.exists():
                continue
            current = pd.read_parquet(current_path)
        if current is None or len(current) == 0:
            continue
        frames.append(current)
    if not frames:
        return None

    panel = pd.concat(frames, ignore_index=True)
    required_columns = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount']
    missing_columns = [col for col in required_columns if col not in panel.columns]
    if missing_columns:
        print(f"   ⚠️ local stk_factor fallback missing daily columns: {missing_columns}")
        return None

    universe = _load_pit_universe_frame(trade_date)
    if universe is None:
        print(f"   ⚠️ stk_factor local rebuild {trade_date}: PIT universe missing; skip degraded same-day rebuild")
        return None
    if not universe.empty:
        panel = panel.merge(universe[['ts_code']], on='ts_code', how='inner')

    panel = panel[required_columns].copy()
    for col in ['open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount']:
        panel[col] = pd.to_numeric(panel[col], errors='coerce')
    panel['trade_date'] = panel['trade_date'].astype(str)
    panel = panel.dropna(subset=['ts_code', 'trade_date', 'close'])
    panel = panel.sort_values(['ts_code', 'trade_date']).drop_duplicates(subset=['ts_code', 'trade_date'], keep='last')

    rows = []
    for ts_code, group in panel.groupby('ts_code', sort=False):
        group = group.sort_values('trade_date').reset_index(drop=True)
        latest = group.iloc[-1]
        if str(latest['trade_date']) != str(trade_date):
            continue

        # 快照抓取失败会把 close 写成 0（不是 NaN，dropna 拦不住），直接喂进 rolling 会把
        # 20 日均值/布林带稀释到真实价格的几分之一，进而让 prebreakout_hard_filter 的
        # max_ma20_bias 对全市场误杀（20260702 实测 4703/4703 全部命中）。
        # 修复：非正 close 视为缺口，先按最近有效值前向填充再参与滚动计算。
        close_raw = pd.to_numeric(group['close'], errors='coerce')
        close = close_raw.mask(close_raw <= 0).ffill()
        high = pd.to_numeric(group['high'], errors='coerce').mask(close_raw <= 0).ffill()
        low = pd.to_numeric(group['low'], errors='coerce').mask(close_raw <= 0).ffill()

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_dif = ema12 - ema26
        macd_dea = macd_dif.ewm(span=9, adjust=False).mean()
        macd = (macd_dif - macd_dea) * 2

        kdj_k, kdj_d, kdj_j = _compute_kdj_series(high, low, close)
        rsi_6 = _compute_rsi_series(close, 6)
        rsi_12 = _compute_rsi_series(close, 12)
        rsi_24 = _compute_rsi_series(close, 24)
        boll_mid = close.rolling(20, min_periods=20).mean()
        boll_std = close.rolling(20, min_periods=20).std(ddof=0)
        boll_upper = boll_mid + 2 * boll_std
        boll_lower = boll_mid - 2 * boll_std
        cci = _compute_cci_series(high, low, close)

        rows.append({
            'ts_code': ts_code,
            'trade_date': str(trade_date),
            'close': _round_or_nan(latest.get('close'), 3),
            'open': _round_or_nan(latest.get('open'), 3),
            'high': _round_or_nan(latest.get('high'), 3),
            'low': _round_or_nan(latest.get('low'), 3),
            'pre_close': _round_or_nan(latest.get('pre_close'), 3),
            'change': _round_or_nan(latest.get('change'), 3),
            'pct_change': _round_or_nan(latest.get('pct_chg'), 4),
            'vol': _round_or_nan(latest.get('vol'), 3),
            'amount': _round_or_nan(latest.get('amount'), 3),
            'adj_factor': np.nan,
            'open_hfq': np.nan,
            'open_qfq': _round_or_nan(latest.get('open'), 3),
            'close_hfq': np.nan,
            'close_qfq': _round_or_nan(latest.get('close'), 3),
            'high_hfq': np.nan,
            'high_qfq': _round_or_nan(latest.get('high'), 3),
            'low_hfq': np.nan,
            'low_qfq': _round_or_nan(latest.get('low'), 3),
            'pre_close_hfq': np.nan,
            'pre_close_qfq': _round_or_nan(latest.get('pre_close'), 3),
            'macd_dif': _round_or_nan(macd_dif.iloc[-1], 3),
            'macd_dea': _round_or_nan(macd_dea.iloc[-1], 3),
            'macd': _round_or_nan(macd.iloc[-1], 3),
            'kdj_k': _round_or_nan(kdj_k.iloc[-1], 3),
            'kdj_d': _round_or_nan(kdj_d.iloc[-1], 3),
            'kdj_j': _round_or_nan(kdj_j.iloc[-1], 3),
            'rsi_6': _round_or_nan(rsi_6.iloc[-1], 3),
            'rsi_12': _round_or_nan(rsi_12.iloc[-1], 3),
            'rsi_24': _round_or_nan(rsi_24.iloc[-1], 3),
            'boll_upper': _round_or_nan(boll_upper.iloc[-1], 3),
            'boll_mid': _round_or_nan(boll_mid.iloc[-1], 3),
            'boll_lower': _round_or_nan(boll_lower.iloc[-1], 3),
            'cci': _round_or_nan(cci.iloc[-1], 3),
            'source_provider': 'local_history_derived_technical',
        })

    if not rows:
        return None

    return pd.DataFrame(rows).sort_values('ts_code').reset_index(drop=True)


def _collect_cache_dates_by_type(data_type: str) -> set[str]:
    prefix = f'{data_type}_'
    dates: set[str] = set()
    for path in CACHE_DIR.glob(f'{data_type}_*.parquet'):
        date_str = path.stem.replace(prefix, '')
        if date_str.isdigit():
            dates.add(date_str)
    return dates


def _prepare_local_cyq_feature_panel(trade_date, daily_frame: pd.DataFrame | None, stk_frame: pd.DataFrame | None):
    if daily_frame is None or len(daily_frame) == 0:
        return None, None

    panel = daily_frame.copy()
    if stk_frame is not None and len(stk_frame) > 0:
        keep_cols = [col for col in ['ts_code', 'boll_mid', 'rsi_6', 'macd_dif'] if col in stk_frame.columns]
        if keep_cols:
            panel = panel.merge(
                stk_frame[keep_cols].drop_duplicates(subset=['ts_code'], keep='last'),
                on='ts_code',
                how='left',
            )
    for col in ['boll_mid', 'rsi_6', 'macd_dif']:
        if col not in panel.columns:
            panel[col] = np.nan

    required_columns = ['ts_code', 'close', 'high', 'low', 'vol', 'amount']
    if any(col not in panel.columns for col in required_columns):
        return None, None

    close = pd.to_numeric(panel['close'], errors='coerce')
    high = pd.to_numeric(panel['high'], errors='coerce')
    low = pd.to_numeric(panel['low'], errors='coerce')
    vol = pd.to_numeric(panel['vol'], errors='coerce')
    amount = pd.to_numeric(panel['amount'], errors='coerce')
    vwap = (amount / vol.replace(0, np.nan)) * 10.0
    boll_mid = pd.to_numeric(panel['boll_mid'], errors='coerce').fillna(close)
    intraday = (high - low).abs()
    trend_gap = (close - boll_mid).abs()

    features = pd.DataFrame(
        {
            'intercept': 1.0,
            'close': close,
            'vwap': vwap.fillna(close),
            'boll_mid': boll_mid,
            'intraday': intraday.fillna(0.0),
            'trend_gap': trend_gap.fillna(0.0),
            'pct_chg': pd.to_numeric(panel.get('pct_chg'), errors='coerce').fillna(0.0),
            'rsi_6': pd.to_numeric(panel.get('rsi_6'), errors='coerce').fillna(50.0),
            'macd_dif': pd.to_numeric(panel.get('macd_dif'), errors='coerce').fillna(0.0),
        }
    ).replace([np.inf, -np.inf], np.nan)

    base = pd.DataFrame(
        {
            'ts_code': panel['ts_code'].astype(str).str.strip(),
            'trade_date': str(trade_date),
        }
    )
    valid = base['ts_code'].ne('') & features[list(LOCAL_CYQ_PROXY_FEATURE_COLUMNS)].notna().all(axis=1)
    if not bool(valid.any()):
        return None, None
    return base.loc[valid].reset_index(drop=True), features.loc[valid].reset_index(drop=True)


def _load_local_cyq_training_frame(train_date: str):
    daily_path = CACHE_DIR / f'daily_{train_date}.parquet'
    stk_path = CACHE_DIR / f'stk_factor_{train_date}.parquet'
    cyq_path = CACHE_DIR / f'cyq_perf_{train_date}.parquet'
    if not (daily_path.exists() and stk_path.exists() and cyq_path.exists()):
        return None

    try:
        daily_frame = pd.read_parquet(daily_path)
        stk_frame = pd.read_parquet(stk_path)
        cyq_frame = pd.read_parquet(cyq_path)
    except Exception:
        return None

    base_frame, feature_frame = _prepare_local_cyq_feature_panel(train_date, daily_frame, stk_frame)
    if base_frame is None or feature_frame is None:
        return None

    target_cols = ['ts_code', *LOCAL_CYQ_PROXY_TARGET_COLUMNS]
    if any(col not in cyq_frame.columns for col in target_cols):
        return None

    targets = cyq_frame[target_cols].copy()
    targets['ts_code'] = targets['ts_code'].astype(str).str.strip()
    dataset = pd.concat([base_frame[['ts_code']], feature_frame], axis=1)
    dataset = dataset.merge(targets, on='ts_code', how='inner')
    dataset = dataset.replace([np.inf, -np.inf], np.nan).dropna(
        subset=[*LOCAL_CYQ_PROXY_FEATURE_COLUMNS, *LOCAL_CYQ_PROXY_TARGET_COLUMNS]
    )
    return dataset.reset_index(drop=True) if len(dataset) > 0 else None


def _fit_local_cyq_proxy_model(target_trade_date: str):
    cache_key = f'{target_trade_date}:{LOCAL_CYQ_PROXY_TRAIN_DATES}'
    if cache_key in _LOCAL_CYQ_PROXY_MODEL_CACHE:
        return _LOCAL_CYQ_PROXY_MODEL_CACHE[cache_key]

    common_dates = sorted(
        _collect_cache_dates_by_type('daily')
        & _collect_cache_dates_by_type('stk_factor')
        & _collect_cache_dates_by_type('cyq_perf')
    )
    train_dates = [date for date in common_dates if date < str(target_trade_date)][-LOCAL_CYQ_PROXY_TRAIN_DATES:]
    if not train_dates:
        _LOCAL_CYQ_PROXY_MODEL_CACHE[cache_key] = None
        return None

    frames = []
    for train_date in train_dates:
        dataset = _load_local_cyq_training_frame(train_date)
        if dataset is not None and len(dataset) > 0:
            frames.append(dataset)

    if not frames:
        _LOCAL_CYQ_PROXY_MODEL_CACHE[cache_key] = None
        return None

    train_frame = pd.concat(frames, ignore_index=True)
    if len(train_frame) < LOCAL_CYQ_PROXY_MIN_TRAIN_ROWS:
        _LOCAL_CYQ_PROXY_MODEL_CACHE[cache_key] = None
        return None

    feature_matrix = train_frame[list(LOCAL_CYQ_PROXY_FEATURE_COLUMNS)].to_numpy(dtype=float)
    coeffs: dict[str, np.ndarray] = {}
    for target in LOCAL_CYQ_PROXY_TARGET_COLUMNS:
        target_vector = train_frame[target].to_numpy(dtype=float)
        coeffs[target] = np.linalg.lstsq(feature_matrix, target_vector, rcond=None)[0]

    model = {
        'coeffs': coeffs,
        'train_dates': train_dates,
        'row_count': int(len(train_frame)),
    }
    _LOCAL_CYQ_PROXY_MODEL_CACHE[cache_key] = model
    return model


def _predict_local_cyq_proxy(feature_frame: pd.DataFrame, coeffs: dict[str, np.ndarray]):
    feature_matrix = feature_frame[list(LOCAL_CYQ_PROXY_FEATURE_COLUMNS)].to_numpy(dtype=float)
    predicted = {}
    for target in LOCAL_CYQ_PROXY_TARGET_COLUMNS:
        beta = coeffs.get(target)
        if beta is None:
            continue
        predicted[target] = feature_matrix @ beta
    return pd.DataFrame(predicted)


def _finalize_local_cyq_proxy(base_frame: pd.DataFrame, feature_frame: pd.DataFrame, predicted: pd.DataFrame, trade_date: str, *, model: dict | None):
    frame = predicted.copy()
    for target in LOCAL_CYQ_PROXY_TARGET_COLUMNS:
        if target not in frame.columns:
            frame[target] = np.nan

    fallback_center = 0.55 * feature_frame['vwap'] + 0.45 * feature_frame['boll_mid']
    min_band = np.maximum(feature_frame['intraday'] * 0.35 + feature_frame['close'] * 0.02, feature_frame['close'] * 0.015)

    frame['weight_avg'] = pd.to_numeric(frame['weight_avg'], errors='coerce').fillna(fallback_center)
    frame['cost_50pct'] = pd.to_numeric(frame['cost_50pct'], errors='coerce').fillna(frame['weight_avg'])
    frame['cost_15pct'] = pd.to_numeric(frame['cost_15pct'], errors='coerce').fillna(frame['weight_avg'] - min_band)
    frame['cost_85pct'] = pd.to_numeric(frame['cost_85pct'], errors='coerce').fillna(frame['weight_avg'] + min_band)

    lower = pd.concat([frame['cost_15pct'], frame['cost_50pct'], frame['cost_85pct']], axis=1).min(axis=1)
    upper = pd.concat([frame['cost_15pct'], frame['cost_50pct'], frame['cost_85pct']], axis=1).max(axis=1)
    too_tight = (upper - lower).abs() < min_band
    lower.loc[too_tight] = frame.loc[too_tight, 'weight_avg'] - min_band.loc[too_tight]
    upper.loc[too_tight] = frame.loc[too_tight, 'weight_avg'] + min_band.loc[too_tight]

    frame['cost_15pct'] = lower.clip(lower=0.01)
    frame['cost_50pct'] = frame['cost_50pct'].clip(lower=frame['cost_15pct'], upper=upper)
    frame['cost_85pct'] = upper.clip(lower=frame['cost_50pct'])
    frame['weight_avg'] = frame['weight_avg'].clip(lower=frame['cost_15pct'], upper=frame['cost_85pct'])

    cost_band = (frame['cost_85pct'] - frame['cost_15pct']).clip(lower=min_band)
    frame['cost_5pct'] = (frame['cost_15pct'] - 0.25 * cost_band).clip(lower=0.01)
    frame['cost_95pct'] = frame['cost_85pct'] + 0.25 * cost_band
    frame['his_low'] = frame['cost_5pct']
    frame['his_high'] = frame['cost_95pct']
    frame['winner_rate'] = pd.to_numeric(frame['winner_rate'], errors='coerce').fillna(50.0).clip(0.5, 99.5)

    output = pd.concat(
        [
            base_frame[['ts_code']].reset_index(drop=True),
            pd.Series(str(trade_date), index=base_frame.index, name='trade_date'),
            frame[['his_low', 'his_high', 'cost_5pct', 'cost_15pct', 'cost_50pct', 'cost_85pct', 'cost_95pct', 'weight_avg', 'winner_rate']].reset_index(drop=True),
        ],
        axis=1,
    )
    output['source_provider'] = 'local_same_day_cyq_proxy_regression'
    output['fallback_source'] = 'local_same_day_cyq_proxy'
    output['fallback_schema'] = 'daily_stkfactor_calibrated_regression'
    output['source_timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if model is not None:
        output['proxy_training_rows'] = int(model.get('row_count', 0) or 0)
        output['proxy_training_window'] = f"{model['train_dates'][0]}:{model['train_dates'][-1]}"
    return output.sort_values('ts_code').reset_index(drop=True)


def _build_cyq_perf_local_proxy(trade_date, pro=None, daily_frame: pd.DataFrame | None = None, stk_frame: pd.DataFrame | None = None):
    if daily_frame is None or len(daily_frame) == 0:
        daily_frame = get_cached(pro, trade_date, 'daily') if pro is not None else None
    if stk_frame is None or len(stk_frame) == 0:
        stk_frame = get_cached(pro, trade_date, 'stk_factor') if pro is not None else None
    if daily_frame is None or len(daily_frame) == 0 or stk_frame is None or len(stk_frame) == 0:
        return None

    base_frame, feature_frame = _prepare_local_cyq_feature_panel(trade_date, daily_frame, stk_frame)
    if base_frame is None or feature_frame is None or len(base_frame) == 0:
        return None

    model = _fit_local_cyq_proxy_model(str(trade_date))
    if model is not None:
        print(
            f"   ⚠️ cyq_perf using local same-day proxy for {trade_date}; "
            f"calibrated on {model['row_count']} rows across {len(model['train_dates'])} cached dates"
        )
        coeffs = model['coeffs']
    else:
        print(f"   ⚠️ cyq_perf using bundled local same-day proxy coefficients for {trade_date}")
        coeffs = LOCAL_CYQ_PROXY_DEFAULT_COEFFICIENTS

    predicted = _predict_local_cyq_proxy(feature_frame, coeffs)
    if predicted is None or len(predicted) == 0:
        return None
    return _finalize_local_cyq_proxy(base_frame, feature_frame, predicted, str(trade_date), model=model)


def _fetch_cyq_perf_from_akshare(trade_date, output_path: Path):
    python_exec = _resolve_akshare_cyq_python()
    if python_exec is None:
        print("   ⚠️ akshare cyq python missing: no usable interpreter found")
        return None
    if not AKSHARE_CYQ_HELPER.exists():
        print(f"   ⚠️ akshare cyq helper missing: {AKSHARE_CYQ_HELPER}")
        return None

    command = [
        str(python_exec),
        str(AKSHARE_CYQ_HELPER),
        '--trade-date',
        str(trade_date),
        '--output',
        str(output_path),
        '--stock-list',
        str(STOCK_LIST_FILE),
        '--retry',
        str(AKSHARE_CYQ_RETRIES),
        '--sleep',
        str(AKSHARE_CYQ_REQUEST_SLEEP_SECONDS),
        '--min-rows',
        str(AKSHARE_CYQ_MIN_ROWS),
        '--max-runtime',
        str(AKSHARE_CYQ_MAX_RUNTIME_SECONDS),
    ]
    if AKSHARE_CYQ_LIMIT > 0:
        command.extend(['--limit', str(AKSHARE_CYQ_LIMIT)])

    env = os.environ.copy()
    env['OPENCLAW_EASTMONEY_PATCH_MIN_SLEEP'] = str(EASTMONEY_PATCH_MIN_SLEEP_SECONDS)
    env['OPENCLAW_EASTMONEY_PATCH_MAX_SLEEP'] = str(EASTMONEY_PATCH_MAX_SLEEP_SECONDS)
    env['OPENCLAW_EASTMONEY_PATCH_TIMEOUT'] = str(EASTMONEY_PATCH_TIMEOUT_SECONDS)
    env['OPENCLAW_AKSHARE_CYQ_NO_PROXY'] = str(env.get('OPENCLAW_AKSHARE_CYQ_NO_PROXY') or '*')
    # Fix: point STOCK_SYSTEM_DAILY_ANALYSIS to the real path so the subprocess
    # can find patch/eastmoney_patch.py for AkShare cyq fallback.
    if 'STOCK_SYSTEM_DAILY_ANALYSIS' not in env:
        real_daily_analysis = Path.home() / 'daily_stock_analysis'
        if real_daily_analysis.exists():
            env['STOCK_SYSTEM_DAILY_ANALYSIS'] = str(real_daily_analysis)
    proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=AKSHARE_CYQ_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        tail = '\n'.join((proc.stdout or '').splitlines()[-10:] + (proc.stderr or '').splitlines()[-10:])
        print(f"   ⚠️ akshare cyq helper {trade_date} failed rc={proc.returncode}\n{tail}")
        return None
    csv_path = output_path.with_suffix('.csv')
    if output_path.exists():
        return pd.read_parquet(output_path)
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return None


def get_cached(pro, trade_date, data_type):
    """拉取并缓存数据"""
    cache_file = CACHE_DIR / f"{data_type}_{trade_date}.parquet"
    if cache_file.exists():
        return pd.read_parquet(cache_file)

    df = None
    try:
        if data_type == 'stk_factor':
            try:
                df = pro.stk_factor(trade_date=trade_date)
            except Exception as e:
                print(f"   ⚠️ stk_factor trade_date bulk {trade_date}: {e}")
                df = None
            if df is None or len(df) == 0:
                print(f"   ⚠️ stk_factor bulk returned 0 rows for {trade_date}; trying ts_code batches")
                df = _fetch_stk_factor_by_ts_code_batches(pro, trade_date)
            if (df is None or len(df) == 0) and ENABLE_LOCAL_STK_FACTOR_FALLBACK:
                daily_frame = get_cached(pro, trade_date, 'daily')
                if daily_frame is not None and len(daily_frame) > 0:
                    print(f"   ⚠️ stk_factor tushare unavailable for {trade_date}; deriving technical factors from local daily cache")
                    df = _build_stk_factor_from_daily_cache(trade_date, daily_frame=daily_frame)
        elif data_type == 'cyq_perf':
            try:
                df = pro.cyq_perf(trade_date=trade_date)
            except Exception as e:
                print(f"   ⚠️ cyq_perf trade_date bulk {trade_date}: {e}")
                df = None
            if df is None or len(df) == 0:
                df = _fetch_cyq_perf_by_ts_code_batches(pro, trade_date)
            if (df is None or len(df) == 0) and ENABLE_AKSHARE_CYQ_FALLBACK:
                print(f"   ⚠️ cyq_perf tushare unavailable for {trade_date}; trying AkShare degraded fallback")
                df = _fetch_cyq_perf_from_akshare(trade_date, cache_file)
            if (df is None or len(df) == 0) and ENABLE_LOCAL_CYQ_PROXY_FALLBACK:
                print(f"   ⚠️ cyq_perf degraded providers unavailable for {trade_date}; building local same-day proxy")
                daily_frame = get_cached(pro, trade_date, 'daily')
                stk_frame = get_cached(pro, trade_date, 'stk_factor')
                df = _build_cyq_perf_local_proxy(trade_date, pro=pro, daily_frame=daily_frame, stk_frame=stk_frame)
        elif data_type == 'daily':
            try:
                df = pro.daily(trade_date=trade_date)
            except Exception as e:
                print(f"   ⚠️ daily trade_date bulk {trade_date}: {e}")
                df = None
            if (df is None or len(df) == 0) and ENABLE_MARKET_SNAPSHOT_FALLBACK:
                print(f"   ⚠️ daily tushare unavailable for {trade_date}; trying same-day market snapshot fallback")
                df = _fetch_daily_from_market_snapshot(trade_date, cache_file)
        else:
            return None
        if df is not None and len(df) > 0:
            df.to_parquet(cache_file)
            return df
        time.sleep(0.5)
    except Exception as e:
        print(f"   ⚠️ {data_type} {trade_date}: {e}")
    return None


# ---------------- Historical bulk features (factor hotfix 2026-06-12) ----------------
# Cross-section scoring previously hardcoded momentum=daily pct_change,
# volume_ratio=1.0, volatility=2.0 and left turnover_stability at its neutral
# default, degrading the published strategy into a low-volatility screener
# (13/20 bank stocks on 20260611). These helpers restore real trailing-window
# features from the local daily parquet cache. Kill switch:
# OPENCLAW_DISABLE_HIST_FEATURES=1 reverts to the previous behaviour.

FEATURES_DISABLED = os.environ.get("OPENCLAW_DISABLE_HIST_FEATURES", "").lower() in {"1", "true", "yes", "on"}
FEATURE_LOOKBACK_DAYS = 26  # >=21 rows needed for 20d momentum/volatility

_DAILY_FRAME_CACHE = {}
_FEATURE_PANEL = None          # computed rolling panel (DataFrame)
_FEATURE_PANEL_RANGE = None    # (start_date, end_date) covered by panel
_BULK_FEATURES_CACHE = {}      # trade_date -> {ts_code: {feature: value}}
LAST_FEATURE_COVERAGE = {}     # diagnostics for the most recent score_from_bulk call


def _list_cached_daily_dates():
    dates = []
    for path in CACHE_DIR.glob("daily_*.parquet"):
        d = path.stem.replace("daily_", "")
        if len(d) == 8 and d.isdigit():
            dates.append(d)
    return sorted(dates)


def _load_daily_frame_for_features(trade_date):
    if trade_date in _DAILY_FRAME_CACHE:
        return _DAILY_FRAME_CACHE[trade_date]
    path = CACHE_DIR / f"daily_{trade_date}.parquet"
    frame = None
    if path.exists():
        try:
            frame = pd.read_parquet(path)
            if "pct_chg" not in frame.columns and "pct_change" in frame.columns:
                frame = frame.rename(columns={"pct_change": "pct_chg"})
            keep = [c for c in ("ts_code", "close", "pct_chg", "vol") if c in frame.columns]
            frame = frame[keep].copy()
            frame["trade_date"] = trade_date
        except Exception as e:
            print(f"   ⚠️ 历史特征: 读取 daily_{trade_date} 失败: {e}")
            frame = None
    _DAILY_FRAME_CACHE[trade_date] = frame
    return frame


def _ensure_feature_panel(start_date, end_date):
    """Build (or reuse) the rolling-feature panel covering [start_date, end_date]."""
    global _FEATURE_PANEL, _FEATURE_PANEL_RANGE
    if (
        _FEATURE_PANEL is not None
        and _FEATURE_PANEL_RANGE is not None
        and _FEATURE_PANEL_RANGE[0] <= start_date
        and _FEATURE_PANEL_RANGE[1] >= end_date
    ):
        return _FEATURE_PANEL

    all_dates = _list_cached_daily_dates()
    in_range = [d for d in all_dates if d <= end_date]
    lead = [d for d in in_range if d < start_date][-FEATURE_LOOKBACK_DAYS:]
    window = lead + [d for d in in_range if d >= start_date]
    frames = [f for f in (_load_daily_frame_for_features(d) for d in window) if f is not None and len(f) > 0]
    if not frames:
        return None

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["ts_code", "close"])
    panel = panel.sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = panel.groupby("ts_code", sort=False)
    if "vol" in panel.columns:
        panel["prev_vol_ma5"] = grouped["vol"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
        panel["log_vol"] = np.log1p(panel["vol"].clip(lower=0))
        panel["turnover_stab"] = -panel.groupby("ts_code", sort=False)["log_vol"].transform(
            lambda s: s.rolling(5, min_periods=5).std()
        )
    if "pct_chg" in panel.columns:
        panel["volatility_20d"] = grouped["pct_chg"].transform(lambda s: s.rolling(20, min_periods=10).std())
    panel["close_m5"] = grouped["close"].transform(lambda s: s.shift(5))
    panel["close_m20"] = grouped["close"].transform(lambda s: s.shift(20))

    _FEATURE_PANEL = panel
    _FEATURE_PANEL_RANGE = (window[0] if window else start_date, end_date)
    return panel


def compute_bulk_features(trade_date, panel_start=None):
    """Return {ts_code: features} for a trade date, computed from local cache.

    Features: momentum_5d / momentum_20d (pct), volume_ratio (vol vs prev 5d avg),
    volatility (20d std of pct_chg), turnover_stability (-std(log1p(vol), 5d)).
    """
    trade_date = str(trade_date)
    if trade_date in _BULK_FEATURES_CACHE:
        return _BULK_FEATURES_CACHE[trade_date]

    panel = _ensure_feature_panel(panel_start or trade_date, trade_date)
    feats = {}
    if panel is not None:
        day = panel.loc[panel["trade_date"] == trade_date]
        for row in day.itertuples(index=False):
            entry = {}
            close = getattr(row, "close", None)
            c5 = getattr(row, "close_m5", None)
            c20 = getattr(row, "close_m20", None)
            if close and c5 and pd.notna(c5) and c5 > 0:
                entry["momentum_5d"] = (close / c5 - 1) * 100
            if close and c20 and pd.notna(c20) and c20 > 0:
                entry["momentum_20d"] = (close / c20 - 1) * 100
            vol = getattr(row, "vol", None)
            pv = getattr(row, "prev_vol_ma5", None)
            if vol is not None and pv is not None and pd.notna(pv) and pv > 0:
                entry["volume_ratio"] = float(vol) / float(pv)
            vol20 = getattr(row, "volatility_20d", None)
            if vol20 is not None and pd.notna(vol20):
                entry["volatility"] = float(vol20)
            tstab = getattr(row, "turnover_stab", None)
            if tstab is not None and pd.notna(tstab):
                entry["turnover_stability"] = float(tstab)
            if entry:
                feats[row.ts_code] = entry
    if not feats:
        print(f"   ⚠️ 历史特征: {trade_date} 无可用特征（缓存缺口），打分退回保守默认值")
    _BULK_FEATURES_CACHE[trade_date] = feats
    return feats


def score_from_bulk(stk_df, cyq_df, mode='trend', trade_date=None, features=None):
    """直接从批量数据计算全市场得分
    mode: 'trend'=趋势跟随(v4.0), 'prebreakout'=启动前夕(v4.1)
    trade_date: 传入后自动从本地缓存计算真实历史窗口特征（动量/量比/波动/量稳定性）
    features: 预计算特征 {ts_code: {...}}，传入则跳过自动计算
    """
    if stk_df is None or len(stk_df) == 0:
        return None

    global LAST_FEATURE_COVERAGE
    if cyq_df is not None and len(cyq_df) > 0:
        proxy_evidence = None
        for col in ('source_provider', 'fallback_source'):
            if col in cyq_df.columns:
                sample = next(
                    (
                        str(value)
                        for value in cyq_df[col].dropna().astype(str).tolist()
                        if 'proxy' in value.lower()
                    ),
                    None,
                )
                if sample:
                    proxy_evidence = f"{col}={sample}"
                    break
        if proxy_evidence is None:
            proxy_cols = [col for col in cyq_df.columns if col.startswith('proxy_')]
            if proxy_cols:
                proxy_evidence = ",".join(sorted(proxy_cols))
        if proxy_evidence is not None:
            LAST_FEATURE_COVERAGE = {
                'trade_date': str(trade_date or ''),
                'mode': mode,
                'scored': 0,
                'with_hist_features': 0,
                'skipped_feature_gap': 0,
                'coverage_pct': 0.0,
                'features_disabled': FEATURES_DISABLED,
                'blocked_reason': 'cyq_proxy_forbidden',
                'proxy_evidence': proxy_evidence,
            }
            print(f"   🚫 {trade_date or 'unknown'} 禁止使用筹码代理字段进入评分: {proxy_evidence}")
            raise ProxyDataForbidden(proxy_evidence)
    if features is None and trade_date and not FEATURES_DISABLED:
        try:
            features = compute_bulk_features(trade_date)
        except Exception as e:
            print(f"   ⚠️ 历史特征计算失败，打分退回保守默认值: {e}")
            features = {}
    features = features or {}
    covered = 0
    skipped_gap = 0  # 历史特征已算出但个股缺口、为避免造假而排除的只数

    # 过滤ST和科创板
    stk = stk_df.copy()
    stk = stk[~stk['ts_code'].str.startswith('68')]

    results = []
    cyq_idx = cyq_df.set_index('ts_code') if cyq_df is not None and len(cyq_df) > 0 else None

    for _, row in stk.iterrows():
        try:
            tc = row['ts_code']
            close = row.get('close', 0)
            if not close or close <= 0:
                continue

            pre_close = row.get('pre_close', close)
            pct_change = float(row.get('pct_change', 0) or 0)

            factors = {'price': float(close)}

            # 技术面因子 (来自stk_factor)
            factors['macd_dif_ts'] = row.get('macd_dif', 0)
            factors['macd_dea_ts'] = row.get('macd_dea', 0)
            factors['rsi_6'] = row.get('rsi_6', 50)
            factors['boll_upper'] = row.get('boll_upper', 0)
            factors['boll_mid'] = row.get('boll_mid', 0)
            factors['boll_lower'] = row.get('boll_lower', 0)
            factors['kdj_k'] = row.get('kdj_k', 50)
            factors['kdj_d'] = row.get('kdj_d', 50)

            # 动量与涨跌幅（热修: 使用真实历史窗口特征，缺失时退回当日值）
            ft = features.get(tc) or {}
            if ft:
                covered += 1
            elif features and not FEATURES_DISABLED:
                # 历史特征已成功计算（features 非空）但该股有缺口：不再用 pct_change/1.0/2.0 中性默认
                # 冒充真实动量/量比/波动因子（会把缺口股伪装成低波动中性股、退化成低波动银行股筛选器）。
                # 「缺口置空不进打分」——直接排除，让覆盖率如实下降而非造假补齐。
                # 守卫：features 整体为空（特征计算全失败）或 FEATURES_DISABLED 时不在此排除，避免全跳断当日。
                skipped_gap += 1
                continue
            mom5 = ft.get('momentum_5d')
            mom20 = ft.get('momentum_20d')
            if mode == 'prebreakout':
                # 启动前夕: 动量口径=5日涨幅（与硬过滤 max_5d_change 一致）
                factors['momentum'] = mom5 if mom5 is not None else pct_change
            else:
                # 趋势模式: 动量口径=20日动量（与 FACTOR_CONFIG desc 一致）
                factors['momentum'] = mom20 if mom20 is not None else pct_change
            factors['momentum_raw'] = mom5 if mom5 is not None else pct_change
            factors['change_pct'] = pct_change

            # 量比与流动性（热修: 真实量比 = 当日量/前5日均量）
            vol = float(row.get('vol', 0) or 0)
            amt = float(row.get('amount', 0) or 0)
            factors['volume_ratio'] = float(ft.get('volume_ratio', 1.0) or 1.0)
            factors['liquidity'] = amt / 10000  # 万元
            factors['volatility'] = float(ft.get('volatility', 2.0) or 2.0)
            if ft.get('turnover_stability') is not None:
                factors['turnover_stability'] = float(ft['turnover_stability'])

            # 筹码面
            if cyq_idx is not None and tc in cyq_idx.index:
                cr = cyq_idx.loc[tc]
                cost_85 = float(cr.get('cost_85pct', 0) or 0)
                cost_15 = float(cr.get('cost_15pct', 0) or 0)
                factors['chip_concentration_raw'] = (cost_85 - cost_15) / close if close > 0 else 0
                factors['winner_rate_raw'] = float(cr.get('winner_rate', 50) or 50)
                factors['weight_avg'] = float(cr.get('weight_avg', 0) or 0)
            else:
                factors['chip_concentration_raw'] = 0
                factors['winner_rate_raw'] = 50
                factors['weight_avg'] = 0

            score, sub_scores = pl.score_stock(factors) if mode == 'trend' else pl.score_stock_prebreakout(factors)

            # 启动前夕模式: 硬过滤
            if mode == 'prebreakout' and not pl.prebreakout_hard_filter(factors):
                continue

            results.append({
                'ts_code': tc,
                'score': score,
                'sub_scores': sub_scores,
                'price': close,
                'change_pct': float(row.get('pct_change', 0) or 0),
                # 透传关键原始因子，避免展示层回退到旧AI缓存值
                'chip_conc': float(factors.get('chip_concentration_raw', 0) or 0),
                'winner_rate': float(factors.get('winner_rate_raw', 50) or 50),
                'weight_avg': float(factors.get('weight_avg', 0) or 0),
                'volume_ratio': float(factors.get('volume_ratio', 1.0) or 1.0),
                'rsi_6': float(factors.get('rsi_6', 50) or 50),
                'macd_dif': float(factors.get('macd_dif_ts', 0) or 0),
                'boll_mid': float(factors.get('boll_mid', 0) or 0),
            })
        except Exception:
            continue

    results.sort(key=lambda x: x['score'], reverse=True)
    LAST_FEATURE_COVERAGE = {
        'trade_date': str(trade_date or ''),
        'mode': mode,
        'scored': len(results),
        'with_hist_features': covered,
        'skipped_feature_gap': skipped_gap,  # 因特征缺口被诚实排除（不造假补齐）的只数
        'coverage_pct': round(covered / max(len(results), 1) * 100, 1),
        'features_disabled': FEATURES_DISABLED,
    }
    if trade_date:
        print(
            f"   📐 特征覆盖 {trade_date}: {covered}/{len(results)} "
            f"({LAST_FEATURE_COVERAGE['coverage_pct']}%)"
        )
    if trade_date and len(stk) > 0 and len(results) == 0:
        # 全市场清零属异常态：正常市况下 hard_filter 不应把候选池打成 0。
        # 明确告警而非静默返回空列表，避免"当日无信号"被误当正常结果消费。
        print(
            f"   🚨 {trade_date} {mode} 模式全市场 0 只通过（候选池 {len(stk)} 只），"
            f"疑似 hard_filter/特征缓存异常，本轮不产出选股结果，请人工核查"
        )
        LAST_FEATURE_COVERAGE['anomaly_zero_results'] = True
    return results


INDUSTRY_MAP_PATH = pl.WORKING_DIR / "cache" / "industry_map.json"


def load_industry_map(max_age_days=30):
    """code->industry 映射：缓存优先，过期则经 tushare stock_basic 刷新。

    失败时返回过期缓存或空 dict（调用方应将行业上限降级为跳过并记录）。
    """
    cached = None
    try:
        if INDUSTRY_MAP_PATH.exists():
            cached = json.loads(INDUSTRY_MAP_PATH.read_text(encoding="utf-8"))
            age_days = (time.time() - INDUSTRY_MAP_PATH.stat().st_mtime) / 86400
            if cached and age_days < max_age_days:
                return cached
    except Exception:
        cached = None
    try:
        pro = pl.init_tushare()
        df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,industry')
        mapping = {
            str(r['ts_code']): str(r['industry'] or '')
            for _, r in df.iterrows()
            if r.get('ts_code')
        }
        if mapping:
            INDUSTRY_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            INDUSTRY_MAP_PATH.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
            print(f"   🏭 行业映射已刷新: {len(mapping)} 只")
            return mapping
    except Exception as e:
        print(f"   ⚠️ 行业映射刷新失败（行业上限将使用旧缓存或跳过）: {e}")
    return cached or {}


def calc_ic(all_ic_data):
    """单因子IC分析"""
    factor_names = list(pl.FACTOR_CONFIG['factors'].keys())
    ic_results = {f: [] for f in factor_names}

    for day_scores, day_returns in all_ic_data:
        if len(day_scores) < 30:
            continue
        score_df = pd.DataFrame(day_scores).set_index('ts_code')
        ret_df = day_returns.set_index('ts_code') if 'ts_code' in day_returns.columns else day_returns

        merged = score_df.join(ret_df[['pct_chg']], how='inner')
        if len(merged) < 30:
            # 尝试pct_change
            if 'pct_change' in ret_df.columns:
                merged = score_df.join(ret_df[['pct_change']].rename(columns={'pct_change': 'pct_chg'}), how='inner')
            if len(merged) < 30:
                continue

        for f in factor_names:
            try:
                vals = merged['sub_scores'].apply(lambda x: x.get(f, 0))
                ret = merged['pct_chg'].astype(float)
                if vals.std() > 0:
                    ic = vals.corr(ret)
                    if not np.isnan(ic):
                        ic_results[f].append(ic)
            except:
                continue

    print("\n📊 单因子IC分析:")
    print(f"  {'因子':<22} {'平均IC':>8} {'IC_IR':>8} {'有效性':>8}")
    print("  " + "-" * 52)
    for f in factor_names:
        ics = ic_results[f]
        desc = pl.FACTOR_CONFIG['factors'][f]['desc']
        if ics:
            avg_ic = np.mean(ics)
            ic_std = np.std(ics) if len(ics) > 1 else 1
            ic_ir = avg_ic / ic_std if ic_std > 0 else 0
            if abs(avg_ic) > 0.05:
                level = "★★★"
            elif abs(avg_ic) > 0.03:
                level = "★★"
            elif abs(avg_ic) > 0.01:
                level = "★"
            else:
                level = "—"
            print(f"  {desc:<22} {avg_ic:>8.4f} {ic_ir:>8.4f} {level:>8}")
        else:
            print(f"  {desc:<22} {'N/A':>8} {'N/A':>8} {'无数据':>8}")

    return ic_results


def run_backtest(n_days=60, top_n=20):
    """运行完整回测"""
    print("=" * 60)
    print(f"📊 A股量化选股回测 (快速版)")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📅 回测天数: {n_days} | TOP: {top_n}")
    print("=" * 60)

    pro = pl.init_tushare()
    dates = get_trade_dates(pro, n_days + 5)
    if len(dates) < 3:
        print("❌ 交易日不足")
        return
    print(f"   交易日: {dates[0]} ~ {dates[-1]} ({len(dates)}天)")

    daily_returns = []
    all_ic_data = []

    for i in range(len(dates) - 1):
        td = dates[i]
        nd = dates[i + 1]
        print(f"\n📅 [{i+1}/{len(dates)-1}] {td} → {nd}", end=" ", flush=True)

        stk = get_cached(pro, td, 'stk_factor')
        time.sleep(0.3)
        cyq = get_cached(pro, td, 'cyq_perf')
        time.sleep(0.3)
        next_daily = get_cached(pro, nd, 'daily')
        time.sleep(0.3)

        if stk is None or next_daily is None:
            print("⚠️ 数据缺失")
            continue

        results = score_from_bulk(stk, cyq, trade_date=td)
        if not results:
            print("⚠️ 无结果")
            continue

        top = results[:top_n]
        top_codes = {s['ts_code'] for s in top}

        # 计算TOP N次日收益
        nd_idx = next_daily.set_index('ts_code')
        returns = []
        for s in top:
            tc = s['ts_code']
            if tc in nd_idx.index:
                r = nd_idx.loc[tc]
                pct = r.get('pct_chg', r.get('pct_change', 0))
                if pct is not None:
                    returns.append(float(pct))

        avg_ret = np.mean(returns) if returns else 0
        daily_returns.append({'date': td, 'next_date': nd, 'avg_return': avg_ret, 'n': len(returns)})
        all_ic_data.append((results, next_daily))
        print(f"→ {avg_ret:+.2f}% ({len(returns)}只)")

    if not daily_returns:
        print("\n❌ 无有效数据")
        return

    # 汇总
    df = pd.DataFrame(daily_returns)
    cum = (1 + df['avg_return'] / 100).cumprod()
    total_ret = (cum.iloc[-1] - 1) * 100
    ann_ret = total_ret / len(df) * 252
    max_dd = ((cum / cum.cummax()) - 1).min() * 100
    sharpe = (df['avg_return'].mean() / df['avg_return'].std() * np.sqrt(252)) if df['avg_return'].std() > 0 else 0
    win_rate = (df['avg_return'] > 0).sum() / len(df) * 100

    print("\n" + "=" * 60)
    print("📈 回测结果")
    print("=" * 60)
    print(f"  区间: {df['date'].iloc[0]} ~ {df['next_date'].iloc[-1]}")
    print(f"  天数: {len(df)}")
    print(f"  累计: {total_ret:+.2f}%")
    print(f"  年化: {ann_ret:+.2f}%")
    print(f"  回撤: {max_dd:.2f}%")
    print(f"  夏普: {sharpe:.2f}")
    print(f"  胜率: {win_rate:.1f}%")
    print(f"  日均: {df['avg_return'].mean():+.3f}%")

    # IC分析
    calc_ic(all_ic_data)

    # 保存
    result = {
        'strategy': pl.FACTOR_CONFIG['name'],
        'factors': {k: v['weight'] for k, v in pl.FACTOR_CONFIG['factors'].items()},
        'summary': {
            'total_return': round(total_ret, 2),
            'ann_return': round(ann_ret, 2),
            'max_drawdown': round(max_dd, 2),
            'sharpe': round(sharpe, 2),
            'win_rate': round(win_rate, 1),
            'avg_daily': round(float(df['avg_return'].mean()), 4),
            'days': len(df),
        },
        'daily': daily_returns,
    }
    out = pl.WORKING_DIR / "backtest_result.json"
    with open(out, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存: {out}")
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=60)
    parser.add_argument('--top', type=int, default=20)
    args = parser.parse_args()
    run_backtest(n_days=args.days, top_n=args.top)
