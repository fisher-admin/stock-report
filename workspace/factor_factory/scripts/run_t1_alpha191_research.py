#!/usr/bin/env python3
"""Run T+1 Alpha191 single factor research."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"
OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "t1_alpha191"


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load factor panel and labels."""
    panel = pd.read_parquet(FACTORS_DIR / "gtja_alpha191_panel.parquet")
    labels = pd.read_parquet(FACTORS_DIR / "t1_labels.parquet")
    
    # Merge on ts_code + trade_date
    merged = panel.merge(labels, on=["ts_code", "trade_date"], how="inner")
    print(f"Merged: {len(merged)} rows, {merged['ts_code'].nunique()} stocks")
    return merged, labels


def compute_rank_ic(df: pd.DataFrame, factor_col: str, label_col: str) -> float:
    """Compute Rank IC for a factor."""
    subset = df[[factor_col, label_col]].dropna()
    if len(subset) < 100:
        return np.nan
    return subset[factor_col].corr(subset[label_col], method="spearman")


def compute_ic_by_date(df: pd.DataFrame, factor_col: str, label_col: str) -> pd.Series:
    """Compute daily Rank IC."""
    def _ic(group):
        subset = group[[factor_col, label_col]].dropna()
        if len(subset) < 30:
            return np.nan
        return subset[factor_col].corr(subset[label_col], method="spearman")
    
    return df.groupby("trade_date").apply(_ic)


def compute_group_returns(df: pd.DataFrame, factor_col: str, label_col: str, 
                          n_groups: int = 5) -> dict:
    """Compute quantile group returns."""
    subset = df[[factor_col, label_col, "trade_date"]].dropna()
    if len(subset) < 100:
        return {}
    
    # Rank within each date
    subset["rank"] = subset.groupby("trade_date")[factor_col].rank(pct=True)
    
    # Assign to groups
    subset["group"] = pd.cut(subset["rank"], bins=n_groups, labels=False) + 1
    
    # Compute group returns
    group_returns = subset.groupby("group")[label_col].mean()
    
    return {
        "top_return": float(group_returns.get(n_groups, np.nan)),
        "bottom_return": float(group_returns.get(1, np.nan)),
        "long_short": float(group_returns.get(n_groups, np.nan) - group_returns.get(1, np.nan))
    }


def compute_hit_rate(df: pd.DataFrame, factor_col: str, label_col: str) -> float:
    """Compute hit rate (top quintile positive return ratio)."""
    subset = df[[factor_col, label_col, "trade_date"]].dropna()
    if len(subset) < 100:
        return np.nan
    
    # Rank within each date
    subset["rank"] = subset.groupby("trade_date")[factor_col].rank(pct=True)
    
    # Top quintile
    top = subset[subset["rank"] >= 0.8]
    if len(top) < 10:
        return np.nan
    
    return float((top[label_col] > 0).mean())


def compute_max_drawdown(df: pd.DataFrame, factor_col: str, label_col: str) -> float:
    """Compute max drawdown of top quintile portfolio."""
    subset = df[[factor_col, label_col, "trade_date"]].dropna()
    if len(subset) < 100:
        return np.nan
    
    # Rank within each date
    subset["rank"] = subset.groupby("trade_date")[factor_col].rank(pct=True)
    
    # Top quintile daily returns
    top = subset[subset["rank"] >= 0.8]
    daily_returns = top.groupby("trade_date")[label_col].mean()
    
    if len(daily_returns) < 10:
        return np.nan
    
    # Compute cumulative returns
    cum_returns = (1 + daily_returns).cumprod()
    
    # Compute drawdown
    peak = cum_returns.expanding().max()
    drawdown = (cum_returns - peak) / peak
    
    return float(drawdown.min())


def compute_coverage(df: pd.DataFrame, factor_col: str) -> float:
    """Compute factor coverage percentage."""
    return float(df[factor_col].notna().mean() * 100)


def analyze_factor(df: pd.DataFrame, factor_col: str, label_col: str) -> dict:
    """Compute all metrics for a single factor."""
    return {
        "coverage_pct": compute_coverage(df, factor_col),
        "rank_ic": compute_rank_ic(df, factor_col, label_col),
        "ic_by_date": compute_ic_by_date(df, factor_col, label_col),
        "group_returns": compute_group_returns(df, factor_col, label_col),
        "hit_rate": compute_hit_rate(df, factor_col, label_col),
        "max_drawdown": compute_max_drawdown(df, factor_col, label_col)
    }


def classify_factor(metrics: dict) -> str:
    """Classify factor into whitelist or coldlist."""
    # Whitelist conditions (adjusted for single factor research)
    if metrics["coverage_pct"] < 70:
        return "coldlist"
    if np.isnan(metrics["rank_ic"]):
        return "coldlist"
    if abs(metrics["rank_ic"]) < 0.01:
        return "coldlist"
    # Hit rate threshold lowered for single factors (45% is realistic)
    if np.isnan(metrics["hit_rate"]):
        return "coldlist"
    if metrics["hit_rate"] < 0.45:
        return "coldlist"
    # Max drawdown threshold (more lenient for research phase)
    if np.isnan(metrics["max_drawdown"]):
        return "coldlist"
    if metrics["max_drawdown"] < -0.5:
        return "coldlist"
    
    return "whitelist"


def main() -> int:
    df, labels = load_data()
    
    factor_cols = [c for c in df.columns if c.startswith("alpha")]
    print(f"Analyzing {len(factor_cols)} factors...")
    
    results = {}
    whitelist = []
    coldlist = []
    
    for col in factor_cols:
        print(f"  {col}...", end=" ", flush=True)
        metrics = analyze_factor(df, col, "label_exec_return")
        results[col] = metrics
        
        # Compute ICIR
        ic_series = metrics["ic_by_date"]
        if len(ic_series.dropna()) > 10:
            metrics["icir"] = float(ic_series.mean() / ic_series.std())
        else:
            metrics["icir"] = np.nan
        
        # Classify
        classification = classify_factor(metrics)
        metrics["classification"] = classification
        
        if classification == "whitelist":
            whitelist.append(col)
            print(f"✓ IC={metrics['rank_ic']:.4f} ICIR={metrics['icir']:.2f} Hit={metrics['hit_rate']:.1%}")
        else:
            coldlist.append(col)
            print(f"✗ IC={metrics['rank_ic']:.4f}")
    
    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Convert ic_by_date to serializable format
    for col in results:
        results[col]["ic_by_date"] = {str(k): v for k, v in results[col]["ic_by_date"].items()}
    
    with open(OUTPUT_DIR / "factor_validation_report.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    with open(OUTPUT_DIR / "factor_whitelist.json", "w") as f:
        json.dump({"factors": whitelist, "count": len(whitelist)}, f, ensure_ascii=False, indent=2)
    
    with open(OUTPUT_DIR / "factor_coldlist.json", "w") as f:
        json.dump({"factors": coldlist, "count": len(coldlist)}, f, ensure_ascii=False, indent=2)
    
    print(f"\nResults saved to {OUTPUT_DIR}")
    print(f"Whitelist: {len(whitelist)} factors")
    print(f"Coldlist: {len(coldlist)} factors")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
