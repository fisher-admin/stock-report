#!/usr/bin/env python3
"""Recompute the 2 reversed factors in the existing panel (in-place update)."""
import sys, time
import numpy as np
import pandas as pd
from pathlib import Path

PANEL = Path('/Users/fisher/.openclaw/workspace/factor_factory/data/factors/greenfield_multifactor_panel.parquet')

t0 = time.time()
print(f"Loading panel: {PANEL}")
p = pd.read_parquet(PANEL)
print(f"  Loaded {len(p):,} rows, {p['trade_date'].nunique()} dates in {time.time()-t0:.1f}s")

# ---- Recompute g_close_strength_ratio (REVERSED) ----
# Old: (close - low) / hl_spread  (strong close = high)
# New: (high - close) / hl_spread  (weak close = high)
hl = p['high'] - p['low']
p['g_close_strength_ratio'] = np.where(hl > 0, (p['high'] - p['close']) / hl, 0.5)
print(f"  g_close_strength_ratio reversed (weak close = high)")

# ---- Recompute g_intraday_range_expansion (REVERSED) ----
# Need: daily_range, avg_range_20, direction
daily_range = hl / p['close'].replace(0, np.nan)
avg_range_20 = p.groupby('ts_code', group_keys=False)['close'].transform(
    lambda s: pd.Series(s.index).map(lambda _: np.nan)  # placeholder
)
# Actually compute properly
avg_range_20 = p.groupby('ts_code', group_keys=False).apply(
    lambda g: (g['high'] - g['low']) / g['close'].replace(0, np.nan)
).droplevel(0) if False else None

# Simpler: compute from scratch using groupby on the already-loaded data
daily_range_series = (p['high'] - p['low']) / p['close'].replace(0, np.nan)
avg_range_20 = p.assign(_dr=daily_range_series).groupby('ts_code', group_keys=False)['_dr'].transform(
    lambda s: s.rolling(20, min_periods=10).mean()
)
range_ratio = np.where(avg_range_20 > 0, daily_range_series / avg_range_20, 1.0)
direction = np.where(hl > 0, (p['close'] - p['open']) / hl, 0.0)
# REVERSED: negate direction
p['g_intraday_range_expansion'] = np.tanh(-range_ratio * direction)
print(f"  g_intraday_range_expansion reversed (contraction+bearish = high)")

# ---- Also remove g_opening_gap_reversal if present ----
if 'g_opening_gap_reversal' in p.columns:
    p.drop(columns=['g_opening_gap_reversal'], inplace=True)
    print(f"  g_opening_gap_reversal dropped")

# ---- Rebuild z-scores for the 2 reversed factors ----
print("Rebuilding cross-sectional z-scores for reversed factors...")
from scipy import stats as sp_stats
for col in ['g_close_strength_ratio', 'g_intraday_range_expansion']:
    z_col = f'{col}__z'
    def zscore_group(g):
        vals = g[col].values
        if len(vals) < 30 or np.std(vals) == 0:
            return pd.Series(np.nan, index=g.index)
        ranks = sp_stats.rankdata(vals, method='average')
        normed = sp_stats.norm.ppf(ranks / (len(ranks) + 1))
        return pd.Series(normed, index=g.index)
    p[z_col] = p.groupby('trade_date', group_keys=False).apply(zscore_group)
    print(f"  {z_col} rebuilt")

# ---- Save ----
print(f"Writing panel back to {PANEL}...")
t1 = time.time()
p.to_parquet(PANEL, index=False)
print(f"  Saved in {time.time()-t1:.1f}s")

# ---- Quick IC verification ----
print("\n=== Quick IC verification (last 500 days) ===")
p['fwd_o2c'] = p['close'] / p['open'] - 1
p['fwd_o2c_lead1'] = p.groupby('ts_code')['fwd_o2c'].shift(-1)
eval_dates = sorted(p['trade_date'].unique())[-500:]
for f in ['g_close_strength_ratio', 'g_intraday_range_expansion']:
    ics = []
    for dt in eval_dates:
        d = p[p['trade_date']==dt][[f,'fwd_o2c_lead1']].dropna()
        if len(d) > 50:
            ic, _ = sp_stats.spearmanr(d[f], d['fwd_o2c_lead1'])
            ics.append(ic)
    arr = np.array(ics)
    print(f"  {f}: IC_mean={arr.mean():.4f} (should be POSITIVE)")

print(f"\nDone in {time.time()-t0:.1f}s")
