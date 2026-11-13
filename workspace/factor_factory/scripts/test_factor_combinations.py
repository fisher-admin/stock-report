#!/usr/bin/env python3
"""Test different factor combinations to find optimal set."""

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


def load_selected_factors() -> list[str]:
    """Load selected factors."""
    with open(OUTPUT_DIR / "factor_selection_report.json") as f:
        return json.load(f)["corr_selected"]


def available_selected_factors(df: pd.DataFrame, factors: list[str]) -> list[str]:
    """Keep the frozen selection order but never fabricate missing factor columns."""
    return [factor for factor in factors if factor in df.columns]


def choose_best_result(results: dict[int, dict]) -> tuple[int, dict] | None:
    """Choose only among positive research results; this is never a promotion gate."""
    eligible = [
        item
        for item in results.items()
        if float(item[1].get("avg_return") or 0.0) > 0.0
        and float(item[1].get("total_return") or 0.0) > 0.0
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[1]["hit_rate"], item[1]["total_return"]))


def icir_weighted_portfolio(df: pd.DataFrame, factors: list[str], lookback: int = 60) -> dict[str, float]:
    """Compute ICIR-weighted portfolio weights."""
    dates = sorted(df["trade_date"].unique())
    recent_dates = dates[-lookback:]
    recent_df = df[df["trade_date"].isin(recent_dates)]
    
    icir_values = {}
    for f in factors:
        def _ic(group):
            subset = group[[f, "label_exec_return"]].dropna()
            if len(subset) < 30:
                return np.nan
            return subset[f].corr(subset["label_exec_return"], method="spearman")
        
        daily_ic = recent_df.groupby("trade_date").apply(_ic, include_groups=False)
        valid_ic = daily_ic.dropna()
        
        if len(valid_ic) >= 5:
            ic_mean = valid_ic.mean()
            ic_std = valid_ic.std()
            signed_icir = ic_mean / ic_std if ic_std > 0 else 0
            icir_values[f] = max(float(signed_icir), 0.0)
        else:
            icir_values[f] = 0
    
    total = sum(icir_values.values())
    if total > 0:
        icir_values = {k: v / total for k, v in icir_values.items()}
    
    return icir_values


def walk_forward_validation(df: pd.DataFrame, factors: list[str], 
                          train_window: int = 60, test_window: int = 20) -> dict:
    """Walk-forward validation."""
    dates = sorted(df["trade_date"].unique())
    
    all_results = []
    
    for i in range(train_window, len(dates) - test_window, test_window):
        train_dates = dates[i - train_window:i]
        test_dates = dates[i:i + test_window]
        
        train_df = df[df["trade_date"].isin(train_dates)]
        
        # Compute ICIR weights on training data
        weights = icir_weighted_portfolio(train_df, factors, lookback=train_window)
        
        # Evaluate on test data
        test_df = df[df["trade_date"].isin(test_dates)]
        
        daily_returns = []
        for date in test_dates:
            day_df = test_df[test_df["trade_date"] == date].copy()
            if len(day_df) < 20:
                continue
            
            # Compute weighted score
            day_df["score"] = 0
            for f in factors:
                if f in day_df.columns:
                    day_df["score"] += day_df[f].fillna(0) * weights.get(f, 0)
            
            # Select top 20
            top = day_df.nlargest(20, "score")
            avg_return = top["label_exec_return"].mean()
            
            if not np.isnan(avg_return):
                daily_returns.append({"date": date, "return": avg_return})
        
        if daily_returns:
            returns = [r["return"] for r in daily_returns]
            avg_return = np.mean(returns)
            hit_rate = sum(1 for r in returns if r > 0) / len(returns)
            total_return = np.prod([1 + r for r in returns]) - 1
            
            all_results.append({
                "fold": len(all_results),
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "days": len(daily_returns),
                "avg_return": avg_return,
                "hit_rate": hit_rate,
                "total_return": total_return
            })
    
    if not all_results:
        return {}
    
    avg_hit_rate = np.mean([r["hit_rate"] for r in all_results])
    avg_return = np.mean([r["avg_return"] for r in all_results])
    avg_total_return = np.mean([r["total_return"] for r in all_results])
    std_total_return = np.std([r["total_return"] for r in all_results])
    
    return {
        "folds": len(all_results),
        "avg_hit_rate": avg_hit_rate,
        "avg_return": avg_return,
        "avg_total_return": avg_total_return,
        "std_total_return": std_total_return,
        "fold_details": all_results
    }


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} rows, {df['ts_code'].nunique()} stocks")
    
    frozen_factors = load_selected_factors()
    all_factors = available_selected_factors(df, frozen_factors)
    print(f"Available selected factors: {len(all_factors)}/{len(frozen_factors)}")
    if not all_factors:
        raise RuntimeError("none of the frozen selected factors exist in the factor panel")
    
    # Test different numbers of factors
    results = {}
    
    for n in [3, 5, 7, 10, 15, 20]:
        factors = all_factors[:n]
        print(f"\nTesting {n} factors...")
        
        wf_result = walk_forward_validation(df, factors, train_window=60, test_window=20)
        
        if wf_result:
            results[n] = {
                "factors": factors,
                "hit_rate": wf_result["avg_hit_rate"],
                "avg_return": wf_result["avg_return"],
                "total_return": wf_result["avg_total_return"],
                "std_total_return": wf_result["std_total_return"]
            }
            print(f"  Hit rate: {wf_result['avg_hit_rate']:.1%}")
            print(f"  Avg return: {wf_result['avg_return']:.4f}")
            print(f"  Total return: {wf_result['avg_total_return']:.2%} ± {wf_result['std_total_return']:.2%}")
    
    # Find best
    if results:
        best_n = choose_best_result(results)
        if best_n is None:
            print("\nNo combination has positive absolute research returns; no result saved.")
            return 1
        print(f"\n=== Best: {best_n[0]} factors ===")
        print(f"  Hit rate: {best_n[1]['hit_rate']:.1%}")
        print(f"  Avg return: {best_n[1]['avg_return']:.4f}")
        print(f"  Total return: {best_n[1]['total_return']:.2%} ± {best_n[1]['std_total_return']:.2%}")
        print(f"  Factors: {best_n[1]['factors']}")
        
        # Save best
        with open(OUTPUT_DIR / "optimal_factor_count.json", "w") as f:
            json.dump({
                "optimal_count": best_n[0],
                "results": results,
                "research_only": True,
                "promotion_eligible": False,
                "promotion_note": "Market excess and frozen validation are not evaluated by this archived script.",
            }, f, indent=2)
        
        print(f"\nSaved optimal factor count to {OUTPUT_DIR}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
