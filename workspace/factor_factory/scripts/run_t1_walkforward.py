#!/usr/bin/env python3
"""Walk-forward backtest for T+1 Alpha191 strategy."""

from __future__ import annotations

import json
import sys
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


def load_portfolio() -> dict:
    """Load portfolio specification."""
    with open(OUTPUT_DIR / "portfolio_spec.json") as f:
        return json.load(f)


def compute_daily_returns(df: pd.DataFrame, factors: list[str], weights: dict[str, float], 
                         date: str, top_n: int = 20) -> dict:
    """Compute portfolio return for a single day."""
    day_df = df[df["trade_date"] == date].copy()
    if len(day_df) < top_n:
        return None
    
    # Compute weighted score
    day_df["score"] = 0
    for f in factors:
        if f in day_df.columns:
            day_df["score"] += day_df[f].fillna(0) * weights.get(f, 0)
    
    # Select top N
    top = day_df.nlargest(top_n, "score")
    
    # Compute returns (using label_exec_return which is T+1 open -> T+2 open)
    avg_return = top["label_exec_return"].mean()
    
    return {
        "date": date,
        "return": float(avg_return) if not np.isnan(avg_return) else 0,
        "stocks": len(top),
        "valid_stocks": int(top["label_exec_return"].notna().sum())
    }


def walk_forward_backtest(df: pd.DataFrame, portfolio: dict, 
                         train_window: int = 60, test_window: int = 20) -> list[dict]:
    """Run walk-forward backtest."""
    factors = portfolio["selected_factors"]
    weights = portfolio["weights"]
    
    # Get all dates
    dates = sorted(df["trade_date"].unique())
    print(f"Total dates: {len(dates)}")
    
    results = []
    
    # Walk forward
    for i in range(train_window, len(dates) - test_window, test_window):
        train_dates = dates[i:i + test_window]
        
        # For each test day
        for date in train_dates:
            result = compute_daily_returns(df, factors, weights, date)
            if result:
                results.append(result)
    
    return results


def compute_metrics(results: list[dict]) -> dict:
    """Compute backtest metrics."""
    returns = [r["return"] for r in results]
    
    if not returns:
        return {}
    
    returns = np.array(returns)
    
    # Basic metrics
    total_days = len(returns)
    positive_days = (returns > 0).sum()
    hit_rate = positive_days / total_days
    
    # Returns
    avg_return = returns.mean()
    cumulative = (1 + returns).cumprod()
    total_return = cumulative[-1] - 1
    
    # Risk metrics
    std = returns.std()
    sharpe = avg_return / std * np.sqrt(252) if std > 0 else 0
    
    # Drawdown
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak
    max_drawdown = drawdown.min()
    
    # Calmar ratio
    calmar = total_return / abs(max_drawdown) if max_drawdown != 0 else 0
    
    # Consecutive losses
    max_consecutive_losses = 0
    current_losses = 0
    for r in returns:
        if r < 0:
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0
    
    return {
        "sample_days": total_days,
        "positive_days": int(positive_days),
        "hit_rate": float(hit_rate),
        "avg_return": float(avg_return),
        "avg_return_pct": float(avg_return * 100),
        "total_return": float(total_return),
        "total_return_pct": float(total_return * 100),
        "std": float(std),
        "sharpe": float(sharpe),
        "max_drawdown": float(max_drawdown),
        "max_drawdown_pct": float(max_drawdown * 100),
        "calmar": float(calmar),
        "max_consecutive_losses": int(max_consecutive_losses)
    }


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} rows, {df['ts_code'].nunique()} stocks")
    
    portfolio = load_portfolio()
    print(f"Portfolio: {len(portfolio['selected_factors'])} factors")
    
    # Run walk-forward backtest
    results = walk_forward_backtest(df, portfolio)
    print(f"Backtest completed: {len(results)} days")
    
    # Compute metrics
    metrics = compute_metrics(results)
    
    print(f"\nWalk-forward backtest results:")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")
    
    # Save report
    report = {
        "backtest_type": "walk_forward",
        "train_window": 120,
        "test_window": 20,
        "portfolio": portfolio,
        "metrics": metrics,
        "daily_results": results
    }
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "walkforward_report.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"\nSaved to {OUTPUT_DIR / 'walkforward_report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
