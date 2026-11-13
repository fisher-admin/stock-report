#!/usr/bin/env python3
import pandas as pd, numpy as np, sys
sys.path.insert(0, '.')
from greenfield_multifactor_lib import load_config, compute_factor_diagnostics
from run_greenfield_multifactor_backtest import score_factor_candidate
from pathlib import Path

cfg = load_config(Path('/Users/fisher/.openclaw/workspace/factor_factory/config/greenfield_multifactor_v1_o2c_rebuilt.yaml'))
panel = pd.read_parquet('/Users/fisher/.openclaw/workspace/factor_factory/data/factors/greenfield_multifactor_panel.parquet')

# Use actual walkforward fold 1 train window
train_df = panel.loc[(panel['trade_date'] >= '20221128') & (panel['trade_date'] <= '20231208')].copy()
print(f'Fold 1 train rows: {len(train_df)}')

factors = ['g_opening_gap_reversal__z', 'g_intraday_vwap_deviation__z', 'g_close_strength_ratio__z',
           'g_intraday_range_expansion__z', 'g_volume_price_divergence__z', 'g_chip_pullback_support__z', 'g_long_cost_concentration__z']
for col in factors:
    fo = compute_factor_diagnostics(train_df, col, 'next_return_open_to_close_1d', 20, 0.002)
    fc = compute_factor_diagnostics(train_df, col, 'next_return_close_to_close_1d', 20, 0.002)
    diags = dict(full_close_to_close=fc, full_open_to_close=fo, regime_close_to_close=fc, regime_open_to_close=fo,
                 holdout_confirmation=dict(available=False, reason='disabled'), holdout_pass=True, holdout_failures=[])
    score, pen, meta = score_factor_candidate(diags, cfg)
    fid = col.replace('__z','')
    print(f'{fid}: score={score:.4f}  O2C_sharpe={fo["portfolio_metrics"]["sharpe"]:.3f}  O2C_ic={fo["rank_ic_mean"]:.4f}')
