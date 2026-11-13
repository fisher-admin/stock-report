#!/usr/bin/env python3
"""
Memory-optimized O2C Research Preview Simulator.
Loads only the 17 needed columns (vs 158 full) and uses float32.
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FACTOR_FACTORY_ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
SCRIPTS_DIR = FACTOR_FACTORY_ROOT / 'scripts'
OUTPUT_ROOT = FACTOR_FACTORY_ROOT / 'outputs' / 'research_preview'
PORTFOLIO_SPEC_PATH = FACTOR_FACTORY_ROOT / 'outputs' / 'greenfield_multifactor' / 'portfolio_spec.json'
PANEL_PATH = FACTOR_FACTORY_ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'

sys.path.insert(0, str(SCRIPTS_DIR))
from greenfield_multifactor_lib import (
    dump_json,
    max_drawdown,
    annualized_sharpe,
    annualized_return,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TOP_N = 20
MIN_AMOUNT_THRESHOLD = 50_000

DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    'g_intraday_range_expansion': 0.3557,
    'g_intraday_vwap_deviation': 0.3074,
    'g_close_strength_ratio': 0.2606,
    'g_chip_pullback_support': 0.0431,
    'g_long_cost_concentration': 0.0176,
    'g_volume_price_divergence': 0.0156,
}

SLIPPAGE_WARNING_THRESHOLD = 0.0015
INDUSTRY_CONCENTRATION_THRESHOLD = 0.40
CROWDING_LOOKBACK_DAYS = 5

# Only load columns we actually use
NEEDED_COLUMNS = [
    'trade_date', 'ts_code', 'name', 'open', 'close', 'high', 'low',
    'pct_chg', 'amount', 'avg_amount_lookback', 'industry',
    'g_intraday_range_expansion', 'g_intraday_vwap_deviation',
    'g_close_strength_ratio', 'g_chip_pullback_support',
    'g_long_cost_concentration', 'g_volume_price_divergence',
]


def load_portfolio_weights() -> dict[str, float]:
    if PORTFOLIO_SPEC_PATH.exists():
        try:
            spec = json.loads(PORTFOLIO_SPEC_PATH.read_text(encoding='utf-8'))
            w = spec.get('weights', {})
            if w:
                return {k: float(v) for k, v in w.items()}
        except Exception:
            pass
    return dict(DEFAULT_FACTOR_WEIGHTS)


def load_panel_light() -> pd.DataFrame:
    """Load panel with only needed columns, downcast to float32."""
    print(f"[load] Reading {PANEL_PATH} (select columns only) ...")
    
    # Read parquet schema to check available columns
    import pyarrow.parquet as pq
    schema = pq.read_schema(str(PANEL_PATH))
    available = set(schema.names)
    
    cols_to_load = [c for c in NEEDED_COLUMNS if c in available]
    missing = [c for c in NEEDED_COLUMNS if c not in available]
    if missing:
        print(f"[warn] Missing columns in parquet: {missing}")
    
    df = pd.read_parquet(PANEL_PATH, columns=cols_to_load)
    
    # Downcast numeric columns to float32
    float_cols = df.select_dtypes(include=[np.float64]).columns
    for c in float_cols:
        df[c] = df[c].astype(np.float32)
    
    print(f"[load] Shape: {df.shape}, dates: {df['trade_date'].nunique()}, "
          f"mem={df.memory_usage(deep=True).sum()/1e6:.0f}MB")
    return df


def zscore_by_date_inplace(df: pd.DataFrame, col: str) -> pd.Series:
    """In-place z-score by trade_date, uses float32 throughout."""
    def _z(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors='coerce')
        mean = s.mean(skipna=True)
        std = s.std(skipna=True)
        if pd.isna(std) or std == 0:
            out = pd.Series(np.nan, index=s.index, dtype=np.float32)
            out.loc[s.notna()] = 0.0
            return out
        return ((s - mean) / std).astype(np.float32)
    return df.groupby('trade_date', group_keys=False)[col].apply(_z)


def add_zscores_inplace(df: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    """Add z-score columns in-place (no copy of entire frame)."""
    for col in factor_cols:
        df[f'{col}__z'] = zscore_by_date_inplace(df, col)
        gc.collect()
    return df


def combine_factor_scores_inplace(df: pd.DataFrame, weights: dict[str, float], output_col: str = 'composite_score') -> pd.DataFrame:
    """Combine z-score columns into composite score in-place (no copy)."""
    score = pd.Series(0.0, index=df.index, dtype=np.float32)
    for factor_id, weight in weights.items():
        z_col = f'{factor_id}__z'
        if z_col not in df.columns:
            raise KeyError(f'missing zscore column: {z_col}')
        score = score.add(df[z_col].fillna(0.0).astype(np.float32) * np.float32(weight), fill_value=0.0)
    df[output_col] = score
    return df


def apply_liquidity_gate(day: pd.DataFrame) -> pd.DataFrame:
    df = day.copy()
    if 'amount' in df.columns:
        amt = pd.to_numeric(df['amount'], errors='coerce').fillna(0)
        df = df[amt > MIN_AMOUNT_THRESHOLD].copy()
    if df.empty:
        return df
    if 'avg_amount_lookback' in df.columns:
        avg_amt = pd.to_numeric(df['avg_amount_lookback'], errors='coerce').fillna(0)
        df = df[avg_amt > MIN_AMOUNT_THRESHOLD].copy()
    if df.empty:
        return df
    if 'name' in df.columns:
        mask = ~df['name'].astype(str).str.contains('ST', case=False, na=False)
        df = df[mask].copy()
    if df.empty:
        return df
    if 'pct_chg' in df.columns:
        pct = pd.to_numeric(df['pct_chg'], errors='coerce').fillna(0)
        df = df[(pct.abs() < 9.8)].copy()
    return df


def compute_slippage_estimates(day_selections: pd.DataFrame) -> dict[str, Any]:
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
    if 'industry' not in day_selections.columns:
        return {
            'industry_distribution': {}, 'top_industry': None,
            'top_industry_share': 0.0, 'concentration_warning': False,
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
        'n_stocks': n, 'industry_distribution': distribution,
        'top_industry': top_industry,
        'top_industry_share': round(top_share, 4),
        'concentration_warning': top_share > INDUSTRY_CONCENTRATION_THRESHOLD,
    }


def run_simulation(
    weights: dict[str, float],
    start_date: str | None = None,
    end_date: str | None = None,
    max_days: int | None = None,
) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    
    panel = load_panel_light()
    gc.collect()
    
    all_dates = sorted(panel['trade_date'].unique())
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    if max_days:
        all_dates = all_dates[:max_days]
    if not all_dates:
        print("[error] No trading days in requested range")
        return
    
    factor_cols = list(weights.keys())
    missing = [f for f in factor_cols if f not in panel.columns]
    if missing:
        print(f"[error] Missing factor columns: {missing}")
        return
    
    print(f"[zscore] Computing cross-sectional z-scores for {len(factor_cols)} factors...")
    panel = add_zscores_inplace(panel, factor_cols)
    gc.collect()
    
    print(f"[zscore] Combining factor scores...")
    panel = combine_factor_scores_inplace(panel, weights, output_col='composite_score')
    gc.collect()
    
    print(f"[sim] Running {len(all_dates)} trading days: {all_dates[0]} → {all_dates[-1]}")
    
    cumulative_returns: list[float] = []
    baseline_returns: list[float] = []
    prev_holdings: set[str] = set()
    top_industry_history: list[str] = []
    slippage_rolling: list[float] = []
    all_daily_logs: list[dict[str, Any]] = []
    
    for i, trade_date in enumerate(all_dates):
        day_data = panel[panel['trade_date'] == trade_date].copy()
        if day_data.empty:
            continue
        day_data = apply_liquidity_gate(day_data)
        if len(day_data) < TOP_N:
            continue
        day_data = day_data.sort_values('composite_score', ascending=False).reset_index(drop=True)
        selected = day_data.head(TOP_N).copy()
        
        selected_records = []
        for _, row in selected.iterrows():
            open_p = float(row.get('open', 0))
            close_p = float(row.get('close', 0))
            if open_p <= 0 or close_p <= 0:
                continue
            o2c_ret = close_p / open_p - 1.0
            rec = {
                'code': str(row['ts_code']), 'score': round(float(row['composite_score']), 6),
                'open': round(open_p, 4), 'close': round(close_p, 4),
                'o2c_return': round(o2c_ret, 6),
                'industry': str(row.get('industry', '未知')),
                'avg_amount_lookback': float(row.get('avg_amount_lookback', 0)),
                'high': float(row.get('high', 0)), 'low': float(row.get('low', 0)),
                'pct_chg': float(row.get('pct_chg', 0)),
            }
            for fcol in factor_cols:
                rec[fcol] = float(row.get(fcol, 0)) if pd.notna(row.get(fcol)) else 0.0
            selected_records.append(rec)
        
        if not selected_records:
            continue
        
        selected_df = pd.DataFrame(selected_records)
        portfolio_o2c = float(selected_df['o2c_return'].mean())
        
        baseline_rets = []
        for _, row in day_data.iterrows():
            op = float(row.get('open', 0))
            cl = float(row.get('close', 0))
            if op > 0 and cl > 0:
                baseline_rets.append(cl / op - 1.0)
        baseline_o2c = float(np.mean(baseline_rets)) if baseline_rets else 0.0
        excess = portfolio_o2c - baseline_o2c
        
        new_holdings = set(selected_df['code'])
        if prev_holdings:
            overlap = len(new_holdings & prev_holdings)
            turnover = 1.0 - overlap / max(len(new_holdings), 1)
        else:
            turnover = 1.0
        turnover = float(np.clip(turnover, 0.0, 1.0))
        prev_holdings = new_holdings
        
        factor_scores: dict[str, Any] = {}
        for fcol in factor_cols:
            vals = pd.to_numeric(selected_df.get(fcol, pd.Series(dtype=float)), errors='coerce').dropna()
            factor_scores[fcol] = {
                'mean': round(float(vals.mean()), 6) if len(vals) > 0 else 0.0,
                'std': round(float(vals.std()), 6) if len(vals) > 1 else 0.0,
            }
        
        slippage_info = compute_slippage_estimates(selected_df)
        slippage_rolling.append(slippage_info['overall_avg_slippage_bps'] / 10000.0)
        if len(slippage_rolling) > 30:
            slippage_rolling = slippage_rolling[-30:]
        cost_warning = False
        if len(slippage_rolling) >= 5:
            cost_warning = float(np.mean(slippage_rolling)) > SLIPPAGE_WARNING_THRESHOLD
        
        conc_report = compute_concentration_report(selected_df)
        top_ind = conc_report.get('top_industry', '')
        top_industry_history.append(top_ind)
        if len(top_industry_history) > CROWDING_LOOKBACK_DAYS:
            top_industry_history = top_industry_history[-CROWDING_LOOKBACK_DAYS:]
        crowding_alert = (
            len(top_industry_history) >= CROWDING_LOOKBACK_DAYS
            and len(set(top_industry_history)) == 1
        )
        
        daily_log: dict[str, Any] = {
            'trade_date': str(trade_date),
            'selected_stocks': [{
                'code': r['code'], 'score': r['score'],
                'open': r['open'], 'close': r['close'],
                'o2c_return': r['o2c_return'],
            } for r in selected_records],
            'portfolio_o2c_return': round(portfolio_o2c, 6),
            'baseline_o2c_return': round(baseline_o2c, 6),
            'excess_return': round(excess, 6),
            'turnover': round(turnover, 4),
            'slippage_estimate': round(slippage_info['overall_avg_slippage_bps'], 2),
            'factor_scores': factor_scores,
        }
        dump_json(OUTPUT_ROOT / f'daily_log_{trade_date}.json', daily_log)
        all_daily_logs.append(daily_log)
        
        slippage_report: dict[str, Any] = {
            'trade_date': str(trade_date),
            'per_stock': slippage_info['per_stock'],
            'by_cap_bucket': slippage_info['by_cap_bucket'],
            'overall_avg_slippage_bps': slippage_info['overall_avg_slippage_bps'],
            'rolling_30d_avg_slippage': round(float(np.mean(slippage_rolling)) * 10000, 2) if slippage_rolling else 0.0,
            'cost_warning': cost_warning,
        }
        dump_json(OUTPUT_ROOT / f'slippage_report_{trade_date}.json', slippage_report)
        
        conc_report_full: dict[str, Any] = {
            'trade_date': str(trade_date), **conc_report,
            'crowding_alert': crowding_alert,
            'recent_top_industries': top_industry_history,
        }
        dump_json(OUTPUT_ROOT / f'concentration_report_{trade_date}.json', conc_report_full)
        
        cumulative_returns.append(portfolio_o2c)
        baseline_returns.append(baseline_o2c)
        
        if (i + 1) % 10 == 0 or i == len(all_dates) - 1:
            cum_ret = float(np.prod([1 + r for r in cumulative_returns]) - 1.0)
            print(f"  [{i+1}/{len(all_dates)}] {trade_date}  "
                  f"day_o2c={portfolio_o2c:+.4%}  excess={excess:+.4%}  "
                  f"cum={cum_ret:+.2%}  turnover={turnover:.0%}  "
                  f"n={len(selected_records)}")
    
    if cumulative_returns:
        rets = pd.Series(cumulative_returns)
        base_rets = pd.Series(baseline_returns)
        cum_o2c = float((1 + rets).prod() - 1.0)
        cum_base = float((1 + base_rets).prod() - 1.0)
        
        neg_streak = 0
        max_neg_streak = 0
        for r in cumulative_returns:
            if r < 0:
                neg_streak += 1
                max_neg_streak = max(max_neg_streak, neg_streak)
            else:
                neg_streak = 0
        
        excess_series = rets - base_rets
        pass_rate = float((excess_series > 0).mean()) if len(excess_series) > 0 else 0.0
        
        slip_vals = []
        for p in sorted(OUTPUT_ROOT.glob('slippage_report_*.json')):
            try:
                sr = json.loads(p.read_text(encoding='utf-8'))
                slip_vals.append(sr.get('overall_avg_slippage_bps', 0.0))
            except Exception:
                pass
        
        cumulative_stats: dict[str, Any] = {
            'total_days': len(cumulative_returns),
            'o2c_total_return': round(cum_o2c, 6),
            'o2c_sharpe': round(annualized_sharpe(rets), 4),
            'o2c_max_drawdown': round(max_drawdown(rets), 6),
            'baseline_o2c_total_return': round(cum_base, 6),
            'excess_total_return': round(cum_o2c - cum_base, 6),
            'avg_turnover': round(float(np.mean([d['turnover'] for d in all_daily_logs])), 4),
            'avg_slippage_bps': round(float(np.mean(slip_vals)), 2) if slip_vals else 0.0,
            'win_rate': round(float((rets > 0).mean()), 4),
            'consecutive_negative_days': max_neg_streak,
            'pass_rate': round(pass_rate, 4),
            'last_trade_date': all_dates[-1],
            'updated_at': datetime.now().isoformat(),
        }
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
    parser = argparse.ArgumentParser(description='O2C Research Preview Simulator (Memory-Optimized)')
    parser.add_argument('--start-date', type=str, default=None)
    parser.add_argument('--end-date', type=str, default=None)
    parser.add_argument('--max-days', type=int, default=None)
    args = parser.parse_args()
    
    weights = load_portfolio_weights()
    print(f"[init] Factors: {list(weights.keys())}")
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
