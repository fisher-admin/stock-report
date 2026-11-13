#!/usr/bin/env python3
import pandas as pd, numpy as np, sys, json
sys.path.insert(0, '.')
from greenfield_multifactor_lib import load_config, compute_factor_diagnostics
from run_greenfield_multifactor_backtest import (
    score_factor_candidate, select_weights, train_factor_scores,
    _holdout_enabled, _holdout_controls, _selection_controls, _truthy
)
from pathlib import Path

cfg = load_config(Path('/Users/fisher/.openclaw/workspace/factor_factory/config/greenfield_multifactor_v1_o2c_rebuilt.yaml'))
panel = pd.read_parquet('/Users/fisher/.openclaw/workspace/factor_factory/data/factors/greenfield_multifactor_panel.parquet')

train_df = panel.loc[(panel['trade_date'] >= '20221128') & (panel['trade_date'] <= '20231208')].copy()

factor_meta = [item for item in cfg.factor_registry['backtestable_factors']]
print('Factor meta: %d factors' % len(factor_meta))
print('Holdout enabled: %s' % _holdout_enabled(cfg))

scores = train_factor_scores(train_df, factor_meta, cfg.top_n, cfg.round_trip_cost, 'shrink_uptrend', cfg)
print('\nFactor scores (%d factors):' % len(scores))
for s in scores:
    fid = s['factor_id']
    sc = s['train_score']
    fam = s['family']
    print('  %s: score=%.4f family=%s' % (fid, sc, fam))

fb = cfg.family_budgets.get('shrink_uptrend', cfg.family_budgets)
print('\nFamily budget:', fb)
weights, meta, ctx = select_weights(train_df, scores, fb, cfg)
print('\nSelected: %d factors' % len(meta))
for m in meta:
    fid = m['factor_id']
    bs = m.get('budget_share', 0)
    sc = m['train_score']
    print('  %s: weight=%.4f score=%.4f' % (fid, bs, sc))
