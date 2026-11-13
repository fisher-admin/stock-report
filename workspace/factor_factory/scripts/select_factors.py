#!/usr/bin/env python3
"""Factor redundancy analysis and selection for T+1 Alpha191 strategy."""

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


def load_whitelist() -> list[str]:
    """Load whitelist factors."""
    with open(OUTPUT_DIR / "factor_whitelist.json") as f:
        return json.load(f)["factors"]


def load_validation_report() -> dict:
    """Load factor validation report."""
    with open(OUTPUT_DIR / "factor_validation_report.json") as f:
        return json.load(f)


def compute_correlation_matrix(df: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """Compute correlation matrix between factors."""
    # Use a recent sample for efficiency
    recent_dates = sorted(df["trade_date"].unique())[-60:]
    sample_df = df[df["trade_date"].isin(recent_dates)]
    
    # Compute rank correlation between factors
    factor_data = sample_df[factors].dropna()
    corr_matrix = factor_data.corr(method="spearman")
    
    return corr_matrix


def find_redundant_factors(corr_matrix: pd.DataFrame, threshold: float = 0.85) -> list[tuple[str, str, float]]:
    """Find highly correlated factor pairs."""
    redundant = []
    factors = corr_matrix.columns.tolist()
    
    for i in range(len(factors)):
        for j in range(i + 1, len(factors)):
            corr = abs(corr_matrix.iloc[i, j])
            if corr > threshold:
                redundant.append((factors[i], factors[j], corr))
    
    # Sort by correlation (highest first)
    redundant.sort(key=lambda x: x[2], reverse=True)
    return redundant


def select_factors_by_quality(df: pd.DataFrame, factors: list[str], 
                            validation_report: dict, max_factors: int = 20) -> list[str]:
    """Select top factors by quality metrics."""
    # Score each factor
    scores = {}
    
    for f in factors:
        if f not in validation_report:
            continue
        
        metrics = validation_report[f]
        
        # Skip if no valid IC
        if np.isnan(metrics.get("rank_ic", np.nan)):
            continue
        
        # Quality score: IC * ICIR * hit_rate
        ic = abs(metrics["rank_ic"])
        icir = abs(metrics.get("icir", 0))
        hit_rate = metrics.get("hit_rate", 0.5)
        
        # Penalize low hit rate
        if hit_rate < 0.45:
            continue
        
        scores[f] = ic * icir * hit_rate
    
    # Sort by score
    sorted_factors = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    return [f for f, _ in sorted_factors[:max_factors]]


def greedy_factor_selection_with_corr(df: pd.DataFrame, factors: list[str], 
                                    validation_report: dict, corr_matrix: pd.DataFrame,
                                    max_factors: int = 20, max_corr: float = 0.85) -> list[str]:
    """Greedy factor selection with correlation constraint."""
    # Sort factors by quality
    quality_scores = {}
    for f in factors:
        if f not in validation_report:
            continue
        metrics = validation_report[f]
        if np.isnan(metrics.get("rank_ic", np.nan)):
            continue
        ic = abs(metrics["rank_ic"])
        icir = abs(metrics.get("icir", 0))
        quality_scores[f] = ic * icir
    
    sorted_factors = sorted(quality_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Greedy selection with correlation constraint
    selected = []
    remaining = [f for f, _ in sorted_factors]
    
    for _ in range(max_factors):
        if not remaining:
            break
        
        best_factor = None
        best_score = -999
        
        for f in remaining:
            # Check correlation with already selected factors
            max_corr_with_selected = 0
            for s in selected:
                if f in corr_matrix.columns and s in corr_matrix.columns:
                    corr = abs(corr_matrix.loc[f, s])
                    max_corr_with_selected = max(max_corr_with_selected, corr)
            
            # Skip if too correlated
            if max_corr_with_selected > max_corr:
                continue
            
            # Score
            score = quality_scores.get(f, 0)
            if score > best_score:
                best_score = score
                best_factor = f
        
        if best_factor:
            selected.append(best_factor)
            remaining.remove(best_factor)
        else:
            break
    
    return selected


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} rows, {df['ts_code'].nunique()} stocks")
    
    whitelist = load_whitelist()
    validation_report = load_validation_report()
    print(f"Whitelist factors: {len(whitelist)}")
    
    # Step 1: Compute correlation matrix
    print("\n=== Step 1: Computing correlation matrix ===")
    corr_matrix = compute_correlation_matrix(df, whitelist)
    print(f"Correlation matrix: {corr_matrix.shape}")
    
    # Step 2: Find redundant factors
    print("\n=== Step 2: Finding redundant factors ===")
    redundant = find_redundant_factors(corr_matrix, threshold=0.85)
    print(f"Found {len(redundant)} highly correlated pairs (>0.85):")
    for f1, f2, corr in redundant[:10]:
        print(f"  {f1} - {f2}: {corr:.3f}")
    
    # Step 3: Select factors by quality (no correlation constraint)
    print("\n=== Step 3: Selecting by quality only ===")
    quality_selected = select_factors_by_quality(df, whitelist, validation_report, max_factors=20)
    print(f"Selected {len(quality_selected)} factors by quality:")
    for f in quality_selected:
        ic = validation_report[f]["rank_ic"]
        icir = validation_report[f].get("icir", 0)
        hit = validation_report[f].get("hit_rate", 0)
        print(f"  {f}: IC={ic:.4f} ICIR={icir:.2f} Hit={hit:.1%}")
    
    # Step 4: Select factors with correlation constraint
    print("\n=== Step 4: Selecting with correlation constraint ===")
    corr_selected = greedy_factor_selection_with_corr(
        df, whitelist, validation_report, corr_matrix, 
        max_factors=20, max_corr=0.85
    )
    print(f"Selected {len(corr_selected)} factors with correlation constraint:")
    for f in corr_selected:
        ic = validation_report[f]["rank_ic"]
        icir = validation_report[f].get("icir", 0)
        hit = validation_report[f].get("hit_rate", 0)
        print(f"  {f}: IC={ic:.4f} ICIR={icir:.2f} Hit={hit:.1%}")
    
    # Save results
    results = {
        "total_whitelist": len(whitelist),
        "redundant_pairs": len(redundant),
        "quality_selected": quality_selected,
        "corr_selected": corr_selected,
        "correlation_threshold": 0.85
    }
    
    with open(OUTPUT_DIR / "factor_selection_report.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    # Save correlation matrix
    corr_matrix.to_csv(OUTPUT_DIR / "factor_correlation_matrix.csv")
    
    print(f"\nSaved results to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
