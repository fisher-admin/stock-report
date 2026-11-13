#!/usr/bin/env python3
"""Advanced T+1 Alpha191 portfolio construction with IC weighting and ensemble methods."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"
OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "t1_alpha191"


def load_data() -> pd.DataFrame:
    """Load factor panel with labels."""
    panel = pd.read_parquet(FACTORS_DIR / "gtja_alpha191_panel.parquet")
    labels = pd.read_parquet(FACTORS_DIR / "t1_labels.parquet")
    return panel.merge(labels, on=["ts_code", "trade_date"], how="inner")


def load_whitelist() -> list[str]:
    """Load whitelist factors."""
    with open(OUTPUT_DIR / "factor_whitelist.json") as f:
        return json.load(f)["factors"]


def compute_daily_ic(df: pd.DataFrame, factor_col: str, label_col: str) -> pd.Series:
    """Compute daily Rank IC for a factor."""
    def _ic(group):
        subset = group[[factor_col, label_col]].dropna()
        if len(subset) < 30:
            return np.nan
        return subset[factor_col].corr(subset[label_col], method="spearman")
    
    return df.groupby("trade_date").apply(_ic, include_groups=False)


def ic_weighted_portfolio(df: pd.DataFrame, factors: list[str], lookback: int = 20) -> dict:
    """Build IC-weighted portfolio using rolling IC as weights."""
    # Compute rolling IC for each factor
    ic_weights = {}
    
    for f in factors:
        daily_ic = compute_daily_ic(df, f, "label_exec_return")
        # Use recent IC as weight
        recent_ic = daily_ic.tail(lookback).mean()
        ic_weights[f] = abs(recent_ic) if not np.isnan(recent_ic) else 0
    
    # Normalize weights
    total = sum(ic_weights.values())
    if total > 0:
        ic_weights = {k: v / total for k, v in ic_weights.items()}
    
    # Cap at 25% per factor
    for f in ic_weights:
        ic_weights[f] = min(0.25, ic_weights[f])
    
    # Re-normalize
    total = sum(ic_weights.values())
    if total > 0:
        ic_weights = {k: v / total for k, v in ic_weights.items()}
    
    return ic_weights


def icir_weighted_portfolio(df: pd.DataFrame, factors: list[str], lookback: int = 60) -> dict:
    """Build ICIR-weighted portfolio using IC stability."""
    icir_weights = {}
    
    for f in factors:
        daily_ic = compute_daily_ic(df, f, "label_exec_return")
        recent_ic = daily_ic.tail(lookback)
        
        if len(recent_ic.dropna()) > 10:
            icir = recent_ic.mean() / recent_ic.std() if recent_ic.std() > 0 else 0
            icir_weights[f] = abs(icir)
        else:
            icir_weights[f] = 0
    
    # Normalize
    total = sum(icir_weights.values())
    if total > 0:
        icir_weights = {k: v / total for k, v in icir_weights.items()}
    
    # Cap at 25%
    for f in icir_weights:
        icir_weights[f] = min(0.25, icir_weights[f])
    
    # Re-normalize
    total = sum(icir_weights.values())
    if total > 0:
        icir_weights = {k: v / total for k, v in icir_weights.items()}
    
    return icir_weights


def ensemble_portfolio(df: pd.DataFrame, factors: list[str], lookback: int = 40) -> dict:
    """Build ensemble portfolio combining IC and ICIR weights."""
    ic_w = ic_weighted_portfolio(df, factors, lookback)
    icir_w = icir_weighted_portfolio(df, factors, lookback)
    
    # Combine: 40% IC + 60% ICIR
    ensemble = {}
    for f in factors:
        ensemble[f] = 0.4 * ic_w.get(f, 0) + 0.6 * icir_w.get(f, 0)
    
    # Normalize
    total = sum(ensemble.values())
    if total > 0:
        ensemble = {k: v / total for k, v in ensemble.items()}
    
    return ensemble


def evaluate_portfolio(df: pd.DataFrame, factors: list[str], weights: dict[str, float], 
                      test_dates: list[str], top_n: int = 20) -> dict:
    """Evaluate portfolio performance on test dates."""
    daily_returns = []
    
    for date in test_dates:
        day_df = df[df["trade_date"] == date].copy()
        if len(day_df) < top_n:
            continue
        
        # Compute weighted score
        day_df["score"] = 0
        for f in factors:
            if f in day_df.columns:
                day_df["score"] += day_df[f].fillna(0) * weights.get(f, 0)
        
        # Select top N
        top = day_df.nlargest(top_n, "score")
        avg_return = top["label_exec_return"].mean()
        
        if not np.isnan(avg_return):
            daily_returns.append({"date": date, "return": avg_return})
    
    if not daily_returns:
        return {}
    
    returns = [r["return"] for r in daily_returns]
    returns = np.array(returns)
    
    return {
        "days": len(returns),
        "hit_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "total_return": float((1 + returns).prod() - 1),
        "max_drawdown": float((pd.Series((1 + returns).cumprod()) / pd.Series((1 + returns).cumprod()).expanding().max() - 1).min()),
        "sharpe": float(returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0
    }


def walk_forward_validation(df: pd.DataFrame, factors: list[str], weights: dict[str, float] = None,
                          train_window: int = 60, test_window: int = 20) -> dict:
    """Walk-forward validation with IC weighting."""
    dates = sorted(df["trade_date"].unique())
    
    all_results = []
    
    for i in range(train_window, len(dates) - test_window, test_window):
        train_dates = dates[i - train_window:i]
        test_dates = dates[i:i + test_window]
        
        # Get training data
        train_df = df[df["trade_date"].isin(train_dates)]
        
        # Use pre-computed weights or compute IC weights on training data
        if weights is None:
            weights = ic_weighted_portfolio(train_df, factors, lookback=train_window)
        
        # Evaluate on test data
        test_result = evaluate_portfolio(df, factors, weights, test_dates)
        
        if test_result:
            test_result["train_end"] = train_dates[-1]
            test_result["test_start"] = test_dates[0]
            test_result["test_end"] = test_dates[-1]
            all_results.append(test_result)
    
    if not all_results:
        return {}
    
    # Aggregate results
    avg_hit_rate = np.mean([r["hit_rate"] for r in all_results])
    avg_return = np.mean([r["avg_return"] for r in all_results])
    total_returns = [r["total_return"] for r in all_results]
    
    return {
        "folds": len(all_results),
        "avg_hit_rate": float(avg_hit_rate),
        "avg_return": float(avg_return),
        "avg_total_return": float(np.mean(total_returns)),
        "std_total_return": float(np.std(total_returns)),
        "min_total_return": float(np.min(total_returns)),
        "max_total_return": float(np.max(total_returns)),
        "details": all_results
    }


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} rows, {df['ts_code'].nunique()} stocks")
    
    whitelist = load_whitelist()
    print(f"Whitelist factors: {len(whitelist)}")
    
    # Test different portfolio methods
    methods = {
        "equal_weight": {f: 1/len(whitelist) for f in whitelist},
        "ic_weighted": ic_weighted_portfolio(df, whitelist),
        "icir_weighted": icir_weighted_portfolio(df, whitelist),
        "ensemble": ensemble_portfolio(df, whitelist)
    }
    
    # Run walk-forward validation for each method
    results = {}
    for method_name, weights in methods.items():
        print(f"\nTesting {method_name}...")
        wf_result = walk_forward_validation(df, whitelist, weights=weights, train_window=60, test_window=20)
        
        if wf_result:
            results[method_name] = wf_result
            print(f"  Hit rate: {wf_result['avg_hit_rate']:.1%}")
            print(f"  Avg return: {wf_result['avg_return']:.4f}")
            print(f"  Total return: {wf_result['avg_total_return']:.2%} ± {wf_result['std_total_return']:.2%}")
    
    # Find best method
    if results:
        best_method = max(results.items(), key=lambda x: x[1]["avg_hit_rate"])
        print(f"\nBest method: {best_method[0]} (hit rate: {best_method[1]['avg_hit_rate']:.1%})")
        
        # Save best portfolio
        best_weights = methods[best_method[0]]
        portfolio = {
            "strategy_id": "t1_factor_v1",
            "factor_source": "gtja_alpha191",
            "selected_factors": whitelist,
            "weights": best_weights,
            "method": best_method[0],
            "validation_metrics": best_method[1],
            "all_methods": {k: {"hit_rate": v["avg_hit_rate"], "avg_return": v["avg_return"]} for k, v in results.items()},
            "label_policy": "label_exec_return",
            "universe_policy": "main+gem",
            "cost_model": {"buy": 0.001, "sell": 0.001},
            "created_at": datetime.now().isoformat()
        }
        
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_DIR / "portfolio_spec.json", "w") as f:
            json.dump(portfolio, f, ensure_ascii=False, indent=2)
        
        print(f"\nSaved portfolio spec to {OUTPUT_DIR / 'portfolio_spec.json'}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
