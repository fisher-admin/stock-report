#!/usr/bin/env python3
"""
O2C Research Preview Simulator
===============================
Daily paper-trading simulation for the 6-factor O2C portfolio.

Workflow per day:
  1. Load pre-built panel.parquet (1265 days, 6 O2C factors already computed)
  2. Compute cross-sectional z-scores per day, weighted composite score
  3. Apply liquidity gate, select top-20 by composite score
  4. Record open price as entry, close price as exit → O2C return
  5. Compare vs equal-weight baseline of all liquid stocks

Outputs (per day):
  - outputs/research_preview/daily_log_YYYYMMDD.json
  - outputs/research_preview/slippage_report_YYYYMMDD.json
  - outputs/research_preview/concentration_report_YYYYMMDD.json

Cumulative:
  - outputs/research_preview/cumulative_stats.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FACTOR_FACTORY_ROOT = Path(
    os.environ.get("OPENCLAW_FACTOR_FACTORY_ROOT", str(Path(__file__).resolve().parents[1]))
).resolve()
SCRIPTS_DIR = FACTOR_FACTORY_ROOT / 'scripts'
OUTPUT_ROOT = FACTOR_FACTORY_ROOT / 'outputs' / 'research_preview'
PORTFOLIO_SPEC_PATH = FACTOR_FACTORY_ROOT / 'outputs' / 'greenfield_multifactor' / 'portfolio_spec.json'
PANEL_PATH = FACTOR_FACTORY_ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'

# Add scripts dir to path so we can import the library
sys.path.insert(0, str(SCRIPTS_DIR))
from greenfield_multifactor_lib import (
    dump_json,
    max_drawdown,
    annualized_sharpe,
    annualized_return,
    add_cross_sectional_zscores,
    combine_factor_scores,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TOP_N = 20

# Liquidity gate thresholds
# amount in tushare 千元: 5000万 = 50,000 千元
MIN_AMOUNT_THRESHOLD = 50_000

# 6 O2C factors and their weights (from portfolio_spec.json)
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    'g_intraday_range_expansion': 0.3557,
    'g_intraday_vwap_deviation': 0.3074,
    'g_close_strength_ratio': 0.2606,
    'g_chip_pullback_support': 0.0431,
    'g_long_cost_concentration': 0.0176,
    'g_volume_price_divergence': 0.0156,
}

# Slippage cost warning threshold (30-day rolling mean)
SLIPPAGE_WARNING_THRESHOLD = 0.0015  # 0.15%

# Industry concentration thresholds
INDUSTRY_CONCENTRATION_THRESHOLD = 0.40  # 40% single-industry
CROWDING_LOOKBACK_DAYS = 5  # consecutive days same industry tops
ROUND_TRIP_COST_BASE = 0.003
ROUND_TRIP_COST_STRESS = 0.005
PRICE_BASIS = "qfq"


def load_portfolio_weights() -> dict[str, float]:
    """Load weights from portfolio_spec.json, fall back to hardcoded."""
    if PORTFOLIO_SPEC_PATH.exists():
        try:
            spec = json.loads(PORTFOLIO_SPEC_PATH.read_text(encoding='utf-8'))
            w = spec.get('weights', {})
            if w:
                return {k: float(v) for k, v in w.items()}
        except Exception:
            pass
    return dict(DEFAULT_FACTOR_WEIGHTS)


def load_panel() -> pd.DataFrame:
    """Load the pre-built panel with all factors already computed."""
    print(f"[load] Reading panel from {PANEL_PATH} ...")
    df = pd.read_parquet(PANEL_PATH)
    print(f"[load] Shape: {df.shape}, dates: {df['trade_date'].nunique()}, "
          f"range: {df['trade_date'].min()} → {df['trade_date'].max()}")
    return df


def apply_liquidity_gate(day: pd.DataFrame) -> pd.DataFrame:
    """Filter stocks by liquidity requirements:
    - daily amount > 50M (50,000 千元)
    - avg daily amount lookback > 50M
    - exclude ST stocks (name contains ST)
    - exclude limit-up/down stocks (pct_chg near ±10%)
    """
    df = day.copy()

    # Current day amount threshold
    if 'amount' in df.columns:
        amt = pd.to_numeric(df['amount'], errors='coerce').fillna(0)
        df = df[amt > MIN_AMOUNT_THRESHOLD].copy()
    if df.empty:
        return df

    # Also require avg_amount_lookback > threshold (persistent liquidity)
    if 'avg_amount_lookback' in df.columns:
        avg_amt = pd.to_numeric(df['avg_amount_lookback'], errors='coerce').fillna(0)
        df = df[avg_amt > MIN_AMOUNT_THRESHOLD].copy()
    if df.empty:
        return df

    # Exclude ST
    if 'name' in df.columns:
        mask = ~df['name'].astype(str).str.contains('ST', case=False, na=False)
        df = df[mask].copy()
    if df.empty:
        return df

    # Exclude limit-up / limit-down (pct_chg >= 9.8% or <= -9.8%)
    if 'pct_chg' in df.columns:
        pct = pd.to_numeric(df['pct_chg'], errors='coerce').fillna(0)
        df = df[(pct.abs() < 9.8)].copy()

    return df


def compute_slippage_estimates(day_selections: pd.DataFrame) -> dict[str, Any]:
    """Slippage placeholder for paper-trading mode.

    In paper-trading we use theoretical prices (open/close), not real execution
    prices, so slippage is not measurable.  Return zeros with mode='theoretical'.
    Real slippage will only be available once live trading begins.
    """
    records = []
    for _, row in day_selections.iterrows():
        avg_amt = float(row.get('avg_amount_lookback', 0))
        if avg_amt >= 40_000:
            cap_bucket = 'large_cap'
        elif avg_amt >= 12_000:
            cap_bucket = 'mid_cap'
        else:
            cap_bucket = 'small_cap'
        records.append({
            'code': str(row.get('code', row.get('ts_code', 'unknown'))),
            'cap_bucket': cap_bucket,
        })

    by_bucket: dict[str, Any] = {}
    for bucket in ['large_cap', 'mid_cap', 'small_cap']:
        cnt = sum(1 for r in records if r['cap_bucket'] == bucket)
        by_bucket[bucket] = {'count': cnt, 'avg_slippage_bps': 0.0, 'median_slippage_bps': 0.0}

    return {
        'mode': 'theoretical',
        'note': 'Paper-trading uses open/close prices; real slippage requires live execution data.',
        'per_stock': records,
        'by_cap_bucket': by_bucket,
        'overall_avg_slippage_bps': 0.0,
    }


def compute_concentration_report(day_selections: pd.DataFrame) -> dict[str, Any]:
    """Compute industry concentration for selected stocks."""
    if 'industry' not in day_selections.columns:
        return {
            'industry_distribution': {},
            'top_industry': None,
            'top_industry_share': 0.0,
            'concentration_warning': False,
            'n_stocks': len(day_selections),
        }

    dist = day_selections['industry'].value_counts(normalize=True)
    n = len(day_selections)
    distribution = {
        k: {'count': int(round(v * n)), 'share': round(float(v), 4)}
        for k, v in dist.items()
    }

    top_industry = str(dist.index[0]) if len(dist) > 0 else None
    top_share = float(dist.iloc[0]) if len(dist) > 0 else 0.0

    return {
        'n_stocks': n,
        'industry_distribution': distribution,
        'top_industry': top_industry,
        'top_industry_share': round(top_share, 4),
        'concentration_warning': top_share > INDUSTRY_CONCENTRATION_THRESHOLD,
    }


def _default_signal_cutoff(trade_date: str) -> str:
    return f"{trade_date}T15:00:00+08:00"


def _planned_entry_time_from_execution_date(execution_trade_date: str) -> str:
    dt = datetime.strptime(execution_trade_date, "%Y%m%d")
    return dt.strftime("%Y-%m-%dT09:30:00+08:00")


def _validate_execution_value(value: Any, *, code: str, field: str, trade_date: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{trade_date} {code} invalid {field}: {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{trade_date} {code} invalid {field}: {value!r}")
    return number


def _validate_intraday_return(value: float, *, code: str, trade_date: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{trade_date} {code} non-finite intraday return: {value!r}")
    if value <= -1.0 or value >= 1.0:
        raise ValueError(f"{trade_date} {code} impossible intraday return: {value:.6f}")
    return value


def _require_qfq_value(row: pd.Series, field: str, *, code: str, trade_date: str) -> float:
    column = f"{field}_qfq"
    if column not in row.index:
        raise ValueError(f"{trade_date} {code} missing required {column}")
    return _validate_execution_value(row.get(column), code=code, field=column, trade_date=trade_date)


def validate_cumulative_stats(cumulative_stats: dict[str, Any]) -> None:
    for key, value in list(cumulative_stats.items()):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite cumulative stat: {key}={value!r}")
    return_keys = (
        "o2c_total_return",
        "o2c_total_return_net_base",
        "o2c_total_return_net_stress",
        "baseline_o2c_total_return",
        "baseline_o2c_total_return_net_base",
        "baseline_o2c_total_return_net_stress",
        "excess_total_return",
        "excess_total_return_net_base",
        "excess_total_return_net_stress",
    )
    for key in return_keys:
        value = cumulative_stats.get(key)
        if value is not None and value <= -1.0:
            raise RuntimeError(
                f'invalid_data_suspected: {key}={value} implies impossible <= -100% aggregate return'
            )


def run_simulation(
    weights: dict[str, float],
    start_date: str | None = None,
    end_date: str | None = None,
    max_days: int | None = None,
) -> None:
    """Run the O2C paper-trading simulation."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    panel = load_panel()

    # Filter date range
    all_dates = sorted(panel['trade_date'].unique())
    compute_dates = list(all_dates)  # full range for z-score computation
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    if max_days:
        all_dates = all_dates[:max_days]

    if not all_dates:
        print(f"[error] No trading days in requested range")
        return

    # Subset panel to needed dates + lookback for z-score computation
    # We need ~20 prior dates for rolling z-scores to be meaningful
    # Keep full panel for z-score computation; only all_dates controls which days to simulate
    panel = panel[panel['trade_date'].isin(compute_dates)].copy()

    factor_cols = list(weights.keys())
    missing = [f for f in factor_cols if f not in panel.columns]
    if missing:
        print(f"[error] Missing factor columns in panel: {missing}")
        return

    print(f"[zscore] Computing cross-sectional z-scores for {len(factor_cols)} factors...")
    panel = add_cross_sectional_zscores(panel, factor_cols)
    panel = combine_factor_scores(panel, weights, output_col='composite_score')
    print(f"[zscore] Done. Sample dates: {all_dates[:3]}...")

    # State tracking
    cumulative_returns: list[float] = []
    baseline_returns: list[float] = []
    prev_holdings: set[str] = set()
    top_industry_history: list[str] = []
    slippage_rolling: list[float] = []
    all_daily_logs: list[dict[str, Any]] = []

    if len(all_dates) < 2:
        raise ValueError("simulation requires at least two trade dates for T+1 execution")

    execution_pairs = list(zip(all_dates[:-1], all_dates[1:]))
    print(f"[sim] Running {len(execution_pairs)} signal days: {execution_pairs[0][0]} → {execution_pairs[-1][0]}")

    cumulative_returns_net_base: list[float] = []
    cumulative_returns_net_stress: list[float] = []
    baseline_returns_net_base: list[float] = []
    baseline_returns_net_stress: list[float] = []

    for i, (signal_trade_date, execution_trade_date) in enumerate(execution_pairs):
        signal_data = panel[panel['trade_date'] == signal_trade_date].copy()
        execution_data = panel[panel['trade_date'] == execution_trade_date].copy()
        if signal_data.empty or execution_data.empty:
            continue

        if execution_trade_date <= signal_trade_date:
            raise ValueError(
                f"future-reference guard violated: signal {signal_trade_date} -> execution {execution_trade_date}"
            )

        signal_data = apply_liquidity_gate(signal_data)
        if len(signal_data) < TOP_N:
            continue

        signal_data = signal_data.sort_values('composite_score', ascending=False).reset_index(drop=True)
        selected = signal_data.head(TOP_N).copy()
        execution_quotes = execution_data.set_index('ts_code', drop=False)

        selected_records = []
        for _, row in selected.iterrows():
            code = str(row['ts_code'])
            if code not in execution_quotes.index:
                raise ValueError(f"{execution_trade_date} missing execution quote for {code}")
            quote = execution_quotes.loc[code]
            open_p = _require_qfq_value(quote, 'open', code=code, trade_date=execution_trade_date)
            close_p = _require_qfq_value(quote, 'close', code=code, trade_date=execution_trade_date)
            o2c_ret = _validate_intraday_return(close_p / open_p - 1.0, code=code, trade_date=execution_trade_date)
            rec = {
                'code': code,
                'score': round(float(row['composite_score']), 6),
                'open': round(open_p, 4),
                'close': round(close_p, 4),
                'o2c_return': round(o2c_ret, 6),
                'industry': str(row.get('industry', quote.get('industry', '未知'))),
                'avg_amount_lookback': float(row.get('avg_amount_lookback', 0)),
                'high': float(quote.get('high_qfq', quote.get('high', 0))),
                'low': float(quote.get('low_qfq', quote.get('low', 0))),
                'pct_chg': float(quote.get('pct_chg', 0)),
            }
            # Carry factor raw values for factor_scores reporting
            for fcol in factor_cols:
                rec[fcol] = float(row.get(fcol, 0)) if pd.notna(row.get(fcol)) else 0.0
            selected_records.append(rec)

        if not selected_records:
            continue

        selected_df = pd.DataFrame(selected_records)

        portfolio_o2c = float(selected_df['o2c_return'].mean())
        _validate_intraday_return(portfolio_o2c, code="portfolio", trade_date=execution_trade_date)
        portfolio_o2c_net_base = portfolio_o2c - ROUND_TRIP_COST_BASE
        portfolio_o2c_net_stress = portfolio_o2c - ROUND_TRIP_COST_STRESS

        baseline_exec = execution_data.set_index('ts_code', drop=False)
        baseline_rets = []
        for _, row in signal_data.iterrows():
            code = str(row['ts_code'])
            if code not in baseline_exec.index:
                continue
            quote = baseline_exec.loc[code]
            op = _require_qfq_value(quote, 'open', code=code, trade_date=execution_trade_date)
            cl = _require_qfq_value(quote, 'close', code=code, trade_date=execution_trade_date)
            baseline_rets.append(_validate_intraday_return(cl / op - 1.0, code=code, trade_date=execution_trade_date))
        baseline_o2c = float(np.mean(baseline_rets)) if baseline_rets else 0.0
        baseline_o2c_net_base = baseline_o2c - ROUND_TRIP_COST_BASE
        baseline_o2c_net_stress = baseline_o2c - ROUND_TRIP_COST_STRESS

        excess = portfolio_o2c - baseline_o2c
        excess_net_base = portfolio_o2c_net_base - baseline_o2c_net_base
        excess_net_stress = portfolio_o2c_net_stress - baseline_o2c_net_stress

        # Turnover
        new_holdings = set(selected_df['code'])
        if prev_holdings:
            overlap = len(new_holdings & prev_holdings)
            turnover = 1.0 - overlap / max(len(new_holdings), 1)
        else:
            turnover = 1.0
        turnover = float(np.clip(turnover, 0.0, 1.0))
        prev_holdings = new_holdings

        # Factor cross-sectional stats
        factor_scores: dict[str, Any] = {}
        for fcol in factor_cols:
            vals = pd.to_numeric(selected_df.get(fcol, pd.Series(dtype=float)), errors='coerce').dropna()
            factor_scores[fcol] = {
                'mean': round(float(vals.mean()), 6) if len(vals) > 0 else 0.0,
                'std': round(float(vals.std()), 6) if len(vals) > 1 else 0.0,
            }

        # Slippage
        slippage_info = compute_slippage_estimates(selected_df)
        slippage_rolling.append(slippage_info['overall_avg_slippage_bps'] / 10000.0)
        if len(slippage_rolling) > 30:
            slippage_rolling = slippage_rolling[-30:]

        cost_warning = False
        if len(slippage_rolling) >= 5:
            cost_warning = float(np.mean(slippage_rolling)) > SLIPPAGE_WARNING_THRESHOLD

        # Concentration
        conc_report = compute_concentration_report(selected_df)
        top_ind = conc_report.get('top_industry', '')
        top_industry_history.append(top_ind)
        if len(top_industry_history) > CROWDING_LOOKBACK_DAYS:
            top_industry_history = top_industry_history[-CROWDING_LOOKBACK_DAYS:]
        crowding_alert = (
            len(top_industry_history) >= CROWDING_LOOKBACK_DAYS
            and len(set(top_industry_history)) == 1
        )

        # ---- Write daily log ----
        daily_log: dict[str, Any] = {
            'trade_date': str(execution_trade_date),
            'signal_trade_date': str(signal_trade_date),
            'signal_data_cutoff': _default_signal_cutoff(str(signal_trade_date)),
            'planned_entry_time': _planned_entry_time_from_execution_date(str(execution_trade_date)),
            'price_basis': PRICE_BASIS,
            'round_trip_cost_base': ROUND_TRIP_COST_BASE,
            'round_trip_cost_stress': ROUND_TRIP_COST_STRESS,
            'selected_stocks': [{
                'code': r['code'],
                'score': r['score'],
                'open': r['open'],
                'close': r['close'],
                'o2c_return': r['o2c_return'],
            } for r in selected_records],
            'portfolio_o2c_return': round(portfolio_o2c, 6),
            'portfolio_o2c_return_net_base': round(portfolio_o2c_net_base, 6),
            'portfolio_o2c_return_net_stress': round(portfolio_o2c_net_stress, 6),
            'baseline_o2c_return': round(baseline_o2c, 6),
            'baseline_o2c_return_net_base': round(baseline_o2c_net_base, 6),
            'baseline_o2c_return_net_stress': round(baseline_o2c_net_stress, 6),
            'excess_return': round(excess, 6),
            'excess_return_net_base': round(excess_net_base, 6),
            'excess_return_net_stress': round(excess_net_stress, 6),
            'turnover': round(turnover, 4),
            'slippage_estimate': round(slippage_info['overall_avg_slippage_bps'], 2),
            'factor_scores': factor_scores,
        }
        dump_json(OUTPUT_ROOT / f'daily_log_{execution_trade_date}.json', daily_log)
        all_daily_logs.append(daily_log)

        # ---- Write slippage report ----
        slippage_report: dict[str, Any] = {
            'trade_date': str(execution_trade_date),
            'signal_trade_date': str(signal_trade_date),
            'per_stock': slippage_info['per_stock'],
            'by_cap_bucket': slippage_info['by_cap_bucket'],
            'overall_avg_slippage_bps': slippage_info['overall_avg_slippage_bps'],
            'rolling_30d_avg_slippage': round(float(np.mean(slippage_rolling)) * 10000, 2) if slippage_rolling else 0.0,
            'cost_warning': cost_warning,
        }
        dump_json(OUTPUT_ROOT / f'slippage_report_{execution_trade_date}.json', slippage_report)

        # ---- Write concentration report ----
        conc_report_full: dict[str, Any] = {
            'trade_date': str(execution_trade_date),
            'signal_trade_date': str(signal_trade_date),
            **conc_report,
            'crowding_alert': crowding_alert,
            'recent_top_industries': top_industry_history,
        }
        dump_json(OUTPUT_ROOT / f'concentration_report_{execution_trade_date}.json', conc_report_full)

        cumulative_returns.append(portfolio_o2c)
        baseline_returns.append(baseline_o2c)
        cumulative_returns_net_base.append(portfolio_o2c_net_base)
        cumulative_returns_net_stress.append(portfolio_o2c_net_stress)
        baseline_returns_net_base.append(baseline_o2c_net_base)
        baseline_returns_net_stress.append(baseline_o2c_net_stress)

        if (i + 1) % 10 == 0 or i == len(execution_pairs) - 1:
            cum_ret = float(np.prod([1 + r for r in cumulative_returns]) - 1.0)
            print(f"  [{i+1}/{len(execution_pairs)}] {signal_trade_date}->{execution_trade_date}  "
                  f"day_o2c={portfolio_o2c:+.4%}  excess={excess:+.4%}  "
                  f"cum={cum_ret:+.2%}  turnover={turnover:.0%}  "
                  f"n={len(selected_records)}")

    # ---- Write cumulative stats ----
    if cumulative_returns:
        rets = pd.Series(cumulative_returns)
        base_rets = pd.Series(baseline_returns)
        rets_net_base = pd.Series(cumulative_returns_net_base)
        rets_net_stress = pd.Series(cumulative_returns_net_stress)
        base_rets_net_base = pd.Series(baseline_returns_net_base)
        base_rets_net_stress = pd.Series(baseline_returns_net_stress)
        cum_o2c = float((1 + rets).prod() - 1.0)
        cum_base = float((1 + base_rets).prod() - 1.0)
        cum_o2c_net_base = float((1 + rets_net_base).prod() - 1.0)
        cum_o2c_net_stress = float((1 + rets_net_stress).prod() - 1.0)
        cum_base_net_base = float((1 + base_rets_net_base).prod() - 1.0)
        cum_base_net_stress = float((1 + base_rets_net_stress).prod() - 1.0)

        # Consecutive negative days
        neg_streak = 0
        max_neg_streak = 0
        for r in cumulative_returns:
            if r < 0:
                neg_streak += 1
                max_neg_streak = max(max_neg_streak, neg_streak)
            else:
                neg_streak = 0

        # Pass rate: days with positive excess return
        excess_series = rets - base_rets
        pass_rate = float((excess_series > 0).mean()) if len(excess_series) > 0 else 0.0

        # Average slippage from daily files
        slip_vals = []
        for p in sorted(OUTPUT_ROOT.glob('slippage_report_*.json')):
            try:
                sr = json.loads(p.read_text(encoding='utf-8'))
                slip_vals.append(sr.get('overall_avg_slippage_bps', 0.0))
            except Exception:
                pass

        cumulative_stats: dict[str, Any] = {
            'total_days': len(cumulative_returns),
            'price_basis': PRICE_BASIS,
            'round_trip_cost_base': ROUND_TRIP_COST_BASE,
            'round_trip_cost_stress': ROUND_TRIP_COST_STRESS,
            'o2c_total_return': round(cum_o2c, 6),
            'o2c_total_return_net_base': round(cum_o2c_net_base, 6),
            'o2c_total_return_net_stress': round(cum_o2c_net_stress, 6),
            'o2c_sharpe': round(annualized_sharpe(rets), 4),
            'o2c_max_drawdown': round(max_drawdown(rets), 6),
            'baseline_o2c_total_return': round(cum_base, 6),
            'baseline_o2c_total_return_net_base': round(cum_base_net_base, 6),
            'baseline_o2c_total_return_net_stress': round(cum_base_net_stress, 6),
            'excess_total_return': round(cum_o2c - cum_base, 6),
            'excess_total_return_net_base': round(cum_o2c_net_base - cum_base_net_base, 6),
            'excess_total_return_net_stress': round(cum_o2c_net_stress - cum_base_net_stress, 6),
            'avg_turnover': round(float(np.mean([d['turnover'] for d in all_daily_logs])), 4),
            'avg_slippage_bps': round(float(np.mean(slip_vals)), 2) if slip_vals else 0.0,
            'win_rate': round(float((rets > 0).mean()), 4),
            'consecutive_negative_days': max_neg_streak,
            'pass_rate': round(pass_rate, 4),
            'last_trade_date': all_dates[-1],
            'last_signal_trade_date': execution_pairs[-1][0],
            'updated_at': datetime.now().isoformat(),
        }

        validate_cumulative_stats(cumulative_stats)
        dump_json(OUTPUT_ROOT / 'cumulative_stats.json', cumulative_stats)
        print(f"\n{'='*60}")
        print("[cumulative stats]")
        for k, v in cumulative_stats.items():
            print(f"  {k}: {v}")
        print(f"{'='*60}")
    else:
        print("[warning] No simulation days produced results.")

    print(f"\n[done] Output: {OUTPUT_ROOT}")
    print(f"[done] Daily logs: {len(all_daily_logs)} files")
    print(f"[done] Total output files: {len(list(OUTPUT_ROOT.glob('*.json')))}")


def main():
    parser = argparse.ArgumentParser(description='O2C Research Preview Simulator')
    parser.add_argument('--start-date', type=str, default=None,
                        help='Start date for simulation (YYYYMMDD). Default: all available')
    parser.add_argument('--end-date', type=str, default=None,
                        help='End date for simulation (YYYYMMDD). Default: all available')
    parser.add_argument('--max-days', type=int, default=None,
                        help='Maximum number of trading days to simulate')
    args = parser.parse_args()

    weights = load_portfolio_weights()
    print(f"[init] Factors: {list(weights.keys())}")
    print(f"[init] Weights: {json.dumps(weights, indent=2)}")
    print(f"[init] Panel: {PANEL_PATH}")
    print(f"[init] Output: {OUTPUT_ROOT}")

    run_simulation(
        weights=weights,
        start_date=args.start_date,
        end_date=args.end_date,
        max_days=args.max_days,
    )


if __name__ == '__main__':
    main()
