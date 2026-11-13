#!/usr/bin/env python3
"""Create T+1 labels for Alpha191 factor research."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

DAILY_DIR = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/backtest_cache")
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "factors"


def load_daily_data() -> pd.DataFrame:
    """Load recent daily data for label construction."""
    files = sorted(DAILY_DIR.glob("daily_*.parquet"))
    # Need extra days for T+1 and T+2 labels
    files = files[-110:]  # 100 days + 10 extra for labels
    print(f"Loading {len(files)} daily files...")
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def create_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Create T+1 labels."""
    df = df.copy()
    
    # For each stock, shift to get T+1 and T+2 prices
    grouped = df.groupby("ts_code")
    
    # T+1 close price (for signal quality label)
    df["next_close"] = grouped["close"].shift(-1)
    
    # T+1 open price (for executable trade label - buy price)
    df["next_open"] = grouped["open"].shift(-1)
    
    # T+2 open price (for executable trade label - sell price)
    df["next2_open"] = grouped["open"].shift(-2)
    
    # T+2 close price (fallback if T+2 open missing)
    df["next2_close"] = grouped["close"].shift(-2)
    
    # Signal quality label: T+1 close return relative to T close（分母<=0 置 NaN，不产生 inf）
    base_close = df["close"].where(df["close"] > 0)
    df["label_next_close_return"] = (df["next_close"] - base_close) / base_close

    # Executable trade label: buy at T+1 open, sell at T+2 open（缺 T+2 open 退 T+2 close；买价<=0 置 NaN）
    buy_price = df["next_open"].where(df["next_open"] > 0)
    sell_price = df["next2_open"].fillna(df["next2_close"])
    df["label_exec_return"] = (sell_price - buy_price) / buy_price

    # 标签清洗（2026-06 修复 IC/回测被 inf/-1 污染）：去 inf；剔除 |ret|>21%（除权/停牌/一字板爆值，
    # A股次日单边收益不应超此）与 ret<=-0.99（退市/归零异常）。被剔除处置 NaN，不进入 IC/回测/打分。
    for col in ("label_next_close_return", "label_exec_return"):
        s = df[col].replace([np.inf, -np.inf], np.nan)
        s = s.mask(s.abs() > 0.21)
        s = s.mask(s <= -0.99)
        df[col] = s

    # Mark samples where we don't have complete T+2 data
    df["has_complete_t2"] = df["next2_open"].notna() | df["next2_close"].notna()

    return df


def main() -> int:
    df = load_daily_data()
    print(f"Loaded {len(df)} rows, {df['ts_code'].nunique()} stocks")
    
    df = create_labels(df)
    
    # Extract only the label columns
    labels = df[["ts_code", "trade_date", "label_next_close_return", "label_exec_return", "has_complete_t2"]].copy()
    
    # Save labels
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "t1_labels.parquet"
    labels.to_parquet(out, index=False)
    print(f"Saved: {out}")
    
    # Print stats
    valid_signal = labels["label_next_close_return"].notna().sum()
    valid_exec = labels["label_exec_return"].notna().sum()
    complete_t2 = labels["has_complete_t2"].sum()
    
    print(f"Signal quality label: {valid_signal} valid ({valid_signal/len(labels)*100:.1f}%)")
    print(f"Executable trade label: {valid_exec} valid ({valid_exec/len(labels)*100:.1f}%)")
    print(f"Complete T+2 data: {complete_t2} ({complete_t2/len(labels)*100:.1f}%)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
