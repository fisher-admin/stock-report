#!/usr/bin/env python3
"""
o2c_weekly_evolution.py — O2C Weekly Evolution Analyzer
=======================================================
Runs weekly (Friday after market close) to analyze O2C strategy performance
and generate evolution recommendations.

Functionality:
  1. Read O2C strategy database for recent performance (last 5 trading days)
  2. Calculate: hit rate, avg return, max drawdown, factor contributions
  3. Generate shadow combination from factor_factory outputs
  4. Compare shadow vs official performance
  5. Evolution recommendation (upgrade if shadow outperforms for 1 week)

Outputs:
  - outputs/o2c_weekly_evolution/evolution_YYYYMMDD.json (local)
  - stock-report/data/latest/factor_evolution_state.json (publication)

Usage:
    python o2c_weekly_evolution.py [--trade-date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FACTOR_FACTORY_ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
SCRIPTS_DIR = FACTOR_FACTORY_ROOT / 'scripts'
PANEL_PATH = FACTOR_FACTORY_ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'
PORTFOLIO_SPEC_PATH = FACTOR_FACTORY_ROOT / 'outputs' / 'greenfield_multifactor' / 'portfolio_spec.json'
FACTOR_HEALTH_DIR = FACTOR_FACTORY_ROOT / 'outputs' / 'research_preview'

# Strategy database
STRATEGY_DB = Path('/Users/fisher/.openclaw/workspace/stock_data/03-working/strategy_research/greenfield_o2c_v1/greenfield_o2c_v1.db')
RECOMMENDATION_DB = Path('/Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db')

# Output paths
LOCAL_OUTPUT_DIR = FACTOR_FACTORY_ROOT / 'outputs' / 'o2c_weekly_evolution'
PUBLICATION_PATH = Path('/Users/fisher/.openclaw/workspace/stock-report/data/latest/factor_evolution_state.json')

# Timezone
TZ_SHANGHAI = timezone(timedelta(hours=8))

# 6 O2C factors
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    'g_intraday_range_expansion': 0.35565363430095814,
    'g_intraday_vwap_deviation': 0.30743943509455496,
    'g_close_strength_ratio': 0.2605795075039091,
    'g_chip_pullback_support': 0.04313339800625389,
    'g_long_cost_concentration': 0.01758386685788636,
    'g_volume_price_divergence': 0.015610158236437513,
}

FACTOR_IDS = list(DEFAULT_FACTOR_WEIGHTS.keys())

# Upgrade criteria
UPGRADE_WIN_RATE_DELTA = 3.0  # +3% win rate
UPGRADE_DD_TOLERANCE = 0.0    # max DD not wider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict:
    """Load JSON file."""
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    """Write JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    tmp = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8',
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


def safe_float(v, default: float = 0.0) -> float:
    """Convert to float, return default for NaN/None."""
    if v is None:
        return default
    f = float(v)
    if np.isnan(f) or np.isinf(f):
        return default
    return f


def max_drawdown(returns: pd.Series) -> float:
    """Calculate maximum drawdown from a return series."""
    cumulative = (1 + returns / 100).cumprod()
    peak = cumulative.cummax()
    drawdown = (cumulative - peak) / peak
    return float(drawdown.min() * 100) if len(drawdown) > 0 else 0.0


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def load_review_snapshots(db_path: Path, strategy_id: str = 'greenfield_o2c_v1') -> pd.DataFrame:
    """Load review_snapshot data from strategy database."""
    if not db_path.exists():
        return pd.DataFrame()
    
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            "SELECT * FROM review_snapshot WHERE strategy_id = ? ORDER BY recommend_date DESC",
            conn,
            params=(strategy_id,)
        )
    except Exception as e:
        print(f"[weekly_evolution] Warning: Could not read review_snapshot: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


def load_recommendation_performance(db_path: Path, strategy_id: str = 'greenfield_o2c_v1') -> pd.DataFrame:
    """Load recommendation_fact data from recommendation warehouse."""
    if not db_path.exists():
        return pd.DataFrame()
    
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT strategy_id, recommend_date, stock_code, ts_code, stock_name, 
                   sector_name, ai_view, ai_score, next_day_return_pct, 
                   cumulative_return_pct, next_trade_date, latest_price_date
            FROM recommendation_fact 
            WHERE strategy_id = ?
            ORDER BY recommend_date DESC, rank_no
            """,
            conn,
            params=(strategy_id,)
        )
    except Exception as e:
        print(f"[weekly_evolution] Warning: Could not read recommendation_fact: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


def load_factor_health(trade_date: str) -> dict:
    """Load latest factor health report."""
    health_path = FACTOR_HEALTH_DIR / f'factor_health_{trade_date}.json'
    if health_path.exists():
        return load_json(health_path)
    
    # Try to find the most recent one
    health_files = sorted(FACTOR_HEALTH_DIR.glob('factor_health_*.json'), reverse=True)
    if health_files:
        return load_json(health_files[0])
    return {}


def load_latest_selection_snapshot() -> dict:
    """Load the latest daily O2C selection snapshot."""
    snapshot_path = FACTOR_FACTORY_ROOT / 'outputs' / 'greenfield_multifactor' / 'current_selection_snapshot.json'
    if snapshot_path.exists():
        return load_json(snapshot_path)
    return {}


# ---------------------------------------------------------------------------
# Performance Analysis
# ---------------------------------------------------------------------------
def calculate_weekly_performance(reviews: pd.DataFrame) -> dict[str, Any]:
    """Calculate this week's O2C performance metrics."""
    if reviews.empty:
        return {
            'hit_rate_pct': None,
            'avg_return_pct': None,
            'max_drawdown_pct': None,
            'sample_count': 0,
            'dates': [],
        }
    
    # Parse payload_json for detailed metrics
    all_returns = []
    hit_count = 0
    total_count = 0
    dates = []
    
    for _, row in reviews.iterrows():
        recommend_date = str(row.get('recommend_date', ''))
        dates.append(recommend_date)
        
        # Parse payload for per-stock returns
        try:
            payload = json.loads(row.get('payload_json', '{}'))
            next_day_returns = payload.get('next_day_returns', [])
            if next_day_returns:
                all_returns.extend(next_day_returns)
                hit_count += sum(1 for r in next_day_returns if r > 0)
                total_count += len(next_day_returns)
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        
        # Fall back to aggregate metrics from review_snapshot columns
        avg_ret = safe_float(row.get('avg_next_day_return_pct'))
        hit_rate = safe_float(row.get('hit_rate_pct'))
        sample_count = int(row.get('sample_count', 0))
        
        if sample_count > 0:
            # Use aggregate metrics directly
            all_returns.extend([avg_ret] * sample_count)
            hit_count += int(hit_rate * sample_count / 100)
            total_count += sample_count
    
    if total_count == 0:
        return {
            'hit_rate_pct': None,
            'avg_return_pct': None,
            'max_drawdown_pct': None,
            'sample_count': 0,
            'dates': dates,
        }
    
    returns_series = pd.Series(all_returns)
    hit_rate = (hit_count / total_count * 100) if total_count > 0 else 0.0
    
    return {
        'hit_rate_pct': round(hit_rate, 2),
        'avg_return_pct': round(float(returns_series.mean()), 4),
        'max_drawdown_pct': round(max_drawdown(returns_series), 4),
        'sample_count': total_count,
        'dates': dates,
    }


def analyze_factor_contributions(
    factor_health: dict,
    portfolio_spec: dict,
) -> list[dict[str, Any]]:
    """Analyze factor contribution changes."""
    contributions = []
    
    factors_data = factor_health.get('factors', {})
    spec_factors = {sf['factor_id']: sf for sf in portfolio_spec.get('selected_factors', [])}
    
    for fid in FACTOR_IDS:
        factor_info = factors_data.get(fid, {})
        spec_info = spec_factors.get(fid, {})
        
        current_ic = safe_float(factor_info.get('rolling_ic'))
        gate_ic = safe_float(factor_info.get('gate_ic'))
        ic_series = factor_info.get('ic_series_tail', [])
        
        # Determine trend from IC series
        if len(ic_series) >= 2:
            recent = ic_series[-1] if ic_series else 0
            previous = ic_series[-2] if len(ic_series) >= 2 else 0
            if recent > previous * 1.1:
                trend = 'up'
            elif recent < previous * 0.9:
                trend = 'down'
            else:
                trend = 'stable'
        else:
            trend = 'unknown'
        
        contributions.append({
            'factor_name': fid,
            'this_week_ic': round(current_ic, 6) if current_ic else None,
            'last_week_ic': round(ic_series[-2], 6) if len(ic_series) >= 2 else None,
            'trend': trend,
            'vs_gate': round(current_ic / gate_ic, 4) if gate_ic and current_ic else None,
            'weight': safe_float(spec_info.get('actual_weight')),
        })
    
    return contributions


def identify_failed_samples(recommendations: pd.DataFrame) -> list[dict[str, Any]]:
    """Identify failed samples (stocks with negative returns)."""
    if recommendations.empty:
        return []
    
    failed = []
    for _, row in recommendations.iterrows():
        next_day_ret = safe_float(row.get('next_day_return_pct'))
        cum_ret = safe_float(row.get('cumulative_return_pct'))
        
        if next_day_ret < -2.0:  # Significant loss threshold
            failed.append({
                'stock_code': str(row.get('stock_code', '')),
                'ts_code': str(row.get('ts_code', '')),
                'stock_name': str(row.get('stock_name', '')),
                'sector': str(row.get('sector_name', '')),
                'recommend_date': str(row.get('recommend_date', '')),
                'next_day_return_pct': round(next_day_ret, 4),
                'cumulative_return_pct': round(cum_ret, 4),
                'ai_view': str(row.get('ai_view', '')),
            })
    
    return failed


# ---------------------------------------------------------------------------
# Shadow Combination
# ---------------------------------------------------------------------------
def generate_shadow_weights(
    factor_health: dict,
    current_weights: dict[str, float],
) -> dict[str, float]:
    """Generate shadow weight set based on factor health."""
    factors_data = factor_health.get('factors', {})
    
    # Start with current weights
    shadow_weights = dict(current_weights)
    
    # Adjust weights based on IC performance vs gate
    for fid in FACTOR_IDS:
        factor_info = factors_data.get(fid, {})
        current_ic = safe_float(factor_info.get('rolling_ic'))
        gate_ic = safe_float(factor_info.get('gate_ic'))
        
        if gate_ic and current_ic:
            ic_ratio = current_ic / gate_ic
            
            if ic_ratio > 1.5:
                # Factor performing well above gate - increase weight
                shadow_weights[fid] = shadow_weights.get(fid, 0) * 1.2
            elif ic_ratio < 0.5:
                # Factor performing poorly - decrease weight
                shadow_weights[fid] = shadow_weights.get(fid, 0) * 0.8
    
    # Normalize weights to sum to 1
    total = sum(shadow_weights.values())
    if total > 0:
        shadow_weights = {k: v / total for k, v in shadow_weights.items()}
    
    return shadow_weights


def simulate_shadow_performance(
    panel: pd.DataFrame,
    shadow_weights: dict[str, float],
    trade_dates: list[str],
) -> dict[str, Any]:
    """Simulate shadow combination performance."""
    if panel.empty or not trade_dates:
        return {
            'hit_rate_pct': None,
            'avg_return_pct': None,
            'max_drawdown_pct': None,
            'sample_count': 0,
        }
    
    # Filter panel to recent dates
    recent_panel = panel[panel['trade_date'].isin(trade_dates)].copy()
    if recent_panel.empty:
        return {
            'hit_rate_pct': None,
            'avg_return_pct': None,
            'max_drawdown_pct': None,
            'sample_count': 0,
        }
    
    # Compute z-scores and composite scores
    all_returns = []
    hit_count = 0
    total_count = 0
    
    for date in trade_dates:
        day_data = recent_panel[recent_panel['trade_date'] == date].copy()
        if day_data.empty:
            continue
        
        # Z-score each factor
        for fid in FACTOR_IDS:
            if fid not in day_data.columns:
                continue
            col = pd.to_numeric(day_data[fid], errors='coerce')
            mu = col.mean()
            sigma = col.std()
            if sigma and sigma > 0:
                day_data[f'{fid}__z'] = (col - mu) / sigma
            else:
                day_data[f'{fid}__z'] = 0.0
        
        # Compute composite score with shadow weights
        day_data['shadow_score'] = 0.0
        for fid, w in shadow_weights.items():
            z_col = f'{fid}__z'
            if z_col in day_data.columns:
                day_data['shadow_score'] = day_data['shadow_score'].add(
                    day_data[z_col].fillna(0.0) * w, fill_value=0.0
                )
        
        # Select top 20
        top20 = day_data.nlargest(20, 'shadow_score')
        
        # Calculate returns (open-to-close)
        if 'open' in top20.columns and 'close' in top20.columns:
            opens = pd.to_numeric(top20['open'], errors='coerce')
            closes = pd.to_numeric(top20['close'], errors='coerce')
            valid_mask = (opens > 0) & closes.notna()
            if valid_mask.any():
                returns = ((closes[valid_mask] - opens[valid_mask]) / opens[valid_mask] * 100).tolist()
                all_returns.extend(returns)
                hit_count += sum(1 for r in returns if r > 0)
                total_count += len(returns)
    
    if total_count == 0:
        return {
            'hit_rate_pct': None,
            'avg_return_pct': None,
            'max_drawdown_pct': None,
            'sample_count': 0,
        }
    
    returns_series = pd.Series(all_returns)
    hit_rate = (hit_count / total_count * 100) if total_count > 0 else 0.0
    
    return {
        'hit_rate_pct': round(hit_rate, 2),
        'avg_return_pct': round(float(returns_series.mean()), 4),
        'max_drawdown_pct': round(max_drawdown(returns_series), 4),
        'sample_count': total_count,
    }


# ---------------------------------------------------------------------------
# Evolution Recommendation
# ---------------------------------------------------------------------------
def evaluate_upgrade(
    official: dict[str, Any],
    shadow: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate whether to recommend upgrade to shadow weights."""
    blocking_factors = []
    should_upgrade = False
    
    # Check if we have enough data
    if not official.get('hit_rate_pct') or not shadow.get('hit_rate_pct'):
        return {
            'should_upgrade': False,
            'reason': 'insufficient_data',
            'blocking_factors': ['Not enough performance data to compare'],
        }
    
    # Check win rate improvement
    win_rate_delta = shadow['hit_rate_pct'] - official['hit_rate_pct']
    if win_rate_delta < UPGRADE_WIN_RATE_DELTA:
        blocking_factors.append(f'Win rate delta {win_rate_delta:+.2f}% < {UPGRADE_WIN_RATE_DELTA}% threshold')
    
    # Check cost-adjusted return
    if shadow['avg_return_pct'] is not None and official['avg_return_pct'] is not None:
        if shadow['avg_return_pct'] < official['avg_return_pct'] * 0.9:  # 10% tolerance
            blocking_factors.append('Cost-adjusted return decreased >10%')
    
    # Check max drawdown
    if shadow['max_drawdown_pct'] is not None and official['max_drawdown_pct'] is not None:
        if shadow['max_drawdown_pct'] < official['max_drawdown_pct'] - UPGRADE_DD_TOLERANCE:
            blocking_factors.append('Max drawdown widened')
    
    # Check for single-week anomaly (need at least 3 days of data)
    if shadow.get('sample_count', 0) < 60:  # 3 days * 20 stocks
        blocking_factors.append('Insufficient sample size for reliable comparison')
    
    # Decision
    if not blocking_factors:
        should_upgrade = True
        reason = 'Shadow outperformed official on all criteria'
    else:
        reason = f'{len(blocking_factors)} blocking criteria not met'
    
    return {
        'should_upgrade': should_upgrade,
        'reason': reason,
        'blocking_factors': blocking_factors,
        'metrics_comparison': {
            'win_rate_delta': round(win_rate_delta, 2) if shadow.get('hit_rate_pct') and official.get('hit_rate_pct') else None,
            'return_delta': round(
                (shadow.get('avg_return_pct', 0) or 0) - (official.get('avg_return_pct', 0) or 0), 4
            ),
            'dd_delta': round(
                (shadow.get('max_drawdown_pct', 0) or 0) - (official.get('max_drawdown_pct', 0) or 0), 4
            ),
        },
    }


# ---------------------------------------------------------------------------
# Main Workflow
# ---------------------------------------------------------------------------
def run(trade_date: str | None = None) -> dict[str, Any]:
    """Run the weekly O2C evolution analysis."""
    now = datetime.now(TZ_SHANGHAI)
    
    if trade_date is None:
        trade_date = now.strftime('%Y%m%d')
    
    print(f"[weekly_evolution] Starting O2C weekly evolution analysis for {trade_date}")
    
    # 1. Load data sources
    print("[weekly_evolution] Loading data sources...")
    reviews = load_review_snapshots(STRATEGY_DB)
    recommendations = load_recommendation_performance(RECOMMENDATION_DB)
    factor_health = load_factor_health(trade_date)
    selection_snapshot = load_latest_selection_snapshot()
    
    # Load portfolio spec
    portfolio_spec = {}
    if PORTFOLIO_SPEC_PATH.exists():
        portfolio_spec = load_json(PORTFOLIO_SPEC_PATH)
    
    # Get current weights
    current_weights = portfolio_spec.get('weights', DEFAULT_FACTOR_WEIGHTS)
    
    # 2. Calculate official performance
    print("[weekly_evolution] Calculating official performance...")
    official_perf = calculate_weekly_performance(reviews)
    
    # 3. Analyze factor contributions
    print("[weekly_evolution] Analyzing factor contributions...")
    factor_contributions = analyze_factor_contributions(factor_health, portfolio_spec)
    
    # 4. Identify failed samples
    print("[weekly_evolution] Identifying failed samples...")
    failed_samples = identify_failed_samples(recommendations)
    
    # 5. Generate shadow combination
    print("[weekly_evolution] Generating shadow combination...")
    shadow_weights = generate_shadow_weights(factor_health, current_weights)
    
    # Load panel for shadow simulation
    panel = pd.DataFrame()
    if PANEL_PATH.exists():
        try:
            panel = pd.read_parquet(PANEL_PATH)
            panel['ts_code'] = panel['ts_code'].astype(str)
            panel['trade_date'] = panel['trade_date'].astype(str)
        except Exception as e:
            print(f"[weekly_evolution] Warning: Could not load panel: {e}")
    
    # Get recent trade dates for shadow simulation
    if not panel.empty:
        all_dates = sorted(panel['trade_date'].unique())
        recent_dates = all_dates[-5:] if len(all_dates) >= 5 else all_dates
    else:
        recent_dates = []
    
    # Simulate shadow performance
    shadow_perf = simulate_shadow_performance(panel, shadow_weights, recent_dates)
    
    # 6. Evaluate upgrade recommendation
    print("[weekly_evolution] Evaluating upgrade recommendation...")
    evolution_rec = evaluate_upgrade(official_perf, shadow_perf)
    
    # 7. Build output
    output = {
        'generated_at': now.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'trade_date': trade_date,
        'o2c_official': {
            'hit_rate_pct': official_perf.get('hit_rate_pct'),
            'avg_return_pct': official_perf.get('avg_return_pct'),
            'max_drawdown_pct': official_perf.get('max_drawdown_pct'),
            'weight_map': current_weights,
            'sample_count': official_perf.get('sample_count', 0),
        },
        'o2c_shadow': {
            'hit_rate_pct': shadow_perf.get('hit_rate_pct'),
            'avg_return_pct': shadow_perf.get('avg_return_pct'),
            'max_drawdown_pct': shadow_perf.get('max_drawdown_pct'),
            'weight_map': shadow_weights,
            'sample_count': shadow_perf.get('sample_count', 0),
        },
        'factor_contribution': factor_contributions,
        'failed_samples': failed_samples[:10],  # Top 10 failed samples
        'evolution_recommendation': evolution_rec,
    }
    
    # Check if we have any real data
    has_data = (
        official_perf.get('sample_count', 0) > 0 or
        shadow_perf.get('sample_count', 0) > 0 or
        len(factor_contributions) > 0
    )
    
    if not has_data:
        output['status'] = 'no_data'
        output['note'] = 'No O2C performance data available for analysis'
    
    # 8. Write outputs
    # Local output
    local_output_path = LOCAL_OUTPUT_DIR / f'evolution_{trade_date}.json'
    dump_json(local_output_path, output)
    print(f"[weekly_evolution] Local output: {local_output_path}")
    
    # Publication output
    dump_json(PUBLICATION_PATH, output)
    print(f"[weekly_evolution] Publication output: {PUBLICATION_PATH}")
    
    # 9. Summary
    print(f"\n[weekly_evolution] === Weekly Evolution Summary ===")
    print(f"  Trade date: {trade_date}")
    print(f"  Official hit rate: {official_perf.get('hit_rate_pct', 'N/A')}%")
    print(f"  Shadow hit rate: {shadow_perf.get('hit_rate_pct', 'N/A')}%")
    print(f"  Upgrade recommended: {evolution_rec.get('should_upgrade', False)}")
    if evolution_rec.get('blocking_factors'):
        print(f"  Blocking factors: {', '.join(evolution_rec['blocking_factors'])}")
    print(f"[weekly_evolution] Done.")
    
    return output


def main():
    parser = argparse.ArgumentParser(description='O2C Weekly Evolution Analysis')
    parser.add_argument('--trade-date', type=str, default=None,
                        help='Trade date (YYYYMMDD). Default: today')
    args = parser.parse_args()
    
    run(trade_date=args.trade_date)


if __name__ == '__main__':
    main()
