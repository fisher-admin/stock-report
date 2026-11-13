#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 rule-based regime labeling for factor_factory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run rule-based regime labeling')
    parser.add_argument('--root', default='/Users/fisher/.openclaw/workspace/factor_factory', help='factor_factory root')
    return parser.parse_args()


def choose_primary_regime(mean_ret: float, amount_ratio: float, hl_mean: float, breadth: float) -> tuple[str, float]:
    if abs(mean_ret) < 0.002 and hl_mean >= 0.05:
        return 'high_vol_chop', min(0.95, 0.55 + (hl_mean - 0.05) * 4)
    if amount_ratio >= 1.05 and mean_ret >= 0.002:
        return 'volume_uptrend', min(0.95, 0.6 + (amount_ratio - 1.05) * 0.8 + max(0.0, mean_ret) * 12)
    if amount_ratio >= 1.05 and mean_ret <= -0.002:
        return 'volume_downtrend', min(0.95, 0.6 + (amount_ratio - 1.05) * 0.8 + abs(mean_ret) * 12)
    if amount_ratio < 1.0 and mean_ret >= 0:
        return 'shrink_uptrend', min(0.85, 0.5 + max(0.0, mean_ret) * 10 + max(0.0, breadth - 0.5) * 0.4)
    if amount_ratio < 1.0 and mean_ret < 0:
        return 'shrink_downtrend', min(0.85, 0.5 + abs(mean_ret) * 10 + max(0.0, 0.5 - breadth) * 0.4)
    if mean_ret >= 0:
        return 'shrink_uptrend', 0.45
    return 'shrink_downtrend', 0.45


def choose_style_regime(small_minus_large: float) -> tuple[str, float]:
    if small_minus_large >= 0.004:
        return 'small_cap_rotation', min(0.9, 0.55 + small_minus_large * 25)
    if small_minus_large <= -0.004:
        return 'large_cap_rotation', min(0.9, 0.55 + abs(small_minus_large) * 25)
    target = 'small_cap_rotation' if small_minus_large >= 0 else 'large_cap_rotation'
    return target, 0.35 + min(0.15, abs(small_minus_large) * 10)


def choose_sector_regime(top5_share: float) -> tuple[str, float]:
    if top5_share >= 0.42:
        return 'sector_concentration', min(0.95, 0.55 + (top5_share - 0.42) * 2)
    if top5_share <= 0.25:
        return 'sector_dispersion', min(0.95, 0.55 + (0.25 - top5_share) * 2)
    if top5_share >= 0.335:
        return 'sector_concentration', 0.45 + (top5_share - 0.335) * 1.2
    return 'sector_dispersion', 0.45 + (0.335 - top5_share) * 1.2


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()

    phase1 = load_yaml(root / 'config' / 'phase1.yaml')
    regime_schema = load_yaml(root / 'config' / 'regime_schema.yaml')

    panel_path = root / 'data' / 'factors' / 'panel_phase1.parquet'
    out_data = root / 'data' / 'regimes' / 'regime_labels_phase1.parquet'
    out_json = root / 'outputs' / 'regime_maps' / 'regime_label_phase1.json'
    out_data.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    panel = pd.read_parquet(panel_path, columns=[
        'trade_date', 'ts_code', 'avg_amount_lookback', 'ret_1d', 'amount', 'amount_ma_20', 'hl_range', 'base_factor_selected', 'base_factor_score_norm'
    ])
    panel['trade_date'] = panel['trade_date'].astype(str)
    panel['ret_1d'] = pd.to_numeric(panel['ret_1d'], errors='coerce').fillna(0.0)
    panel['amount'] = pd.to_numeric(panel['amount'], errors='coerce').fillna(0.0)
    panel['amount_ma_20'] = pd.to_numeric(panel['amount_ma_20'], errors='coerce').replace(0, np.nan)
    panel['avg_amount_lookback'] = pd.to_numeric(panel['avg_amount_lookback'], errors='coerce').fillna(0.0)
    panel['hl_range'] = pd.to_numeric(panel['hl_range'], errors='coerce').fillna(0.0)

    rows: list[dict[str, Any]] = []
    for trade_date, day in panel.groupby('trade_date', sort=True):
        mean_ret = float(day['ret_1d'].mean())
        median_ret = float(day['ret_1d'].median())
        breadth = float((day['ret_1d'] > 0).mean())
        amount_ratio = float((day['amount'] / day['amount_ma_20']).replace([np.inf, -np.inf], np.nan).median(skipna=True) or 0.0)
        hl_mean = float(day['hl_range'].mean())

        q_low = day['avg_amount_lookback'].quantile(0.25)
        q_high = day['avg_amount_lookback'].quantile(0.75)
        small_bucket = day[day['avg_amount_lookback'] <= q_low]
        large_bucket = day[day['avg_amount_lookback'] >= q_high]
        small_minus_large = float(small_bucket['ret_1d'].mean() - large_bucket['ret_1d'].mean()) if not small_bucket.empty and not large_bucket.empty else 0.0

        contribution = (day['amount'].abs() * day['ret_1d'].abs()).sort_values(ascending=False)
        top5_share = float(contribution.head(5).sum() / contribution.sum()) if contribution.sum() > 0 else 0.0

        primary_id, primary_conf = choose_primary_regime(mean_ret, amount_ratio, hl_mean, breadth)
        style_id, style_conf = choose_style_regime(small_minus_large)
        sector_id, sector_conf = choose_sector_regime(top5_share)

        overall_conf = float(np.clip(np.mean([primary_conf, style_conf, sector_conf]), 0.0, 1.0))
        rows.append({
            'trade_date': trade_date,
            'regime_id': primary_id,
            'regime_confidence': overall_conf,
            'market_return_mean': mean_ret,
            'market_return_median': median_ret,
            'breadth_ratio': breadth,
            'amount_ratio_median': amount_ratio,
            'hl_range_mean': hl_mean,
            'small_minus_large': small_minus_large,
            'top5_contribution_share': top5_share,
            'regime_components': json.dumps({
                'primary': {'id': primary_id, 'confidence': primary_conf},
                'style': {'id': style_id, 'confidence': style_conf},
                'sector': {'id': sector_id, 'confidence': sector_conf},
            }, ensure_ascii=False),
        })

    regime_df = pd.DataFrame(rows).sort_values('trade_date').reset_index(drop=True)
    regime_df.to_parquet(out_data, index=False)

    counts = regime_df['regime_id'].value_counts().to_dict() if not regime_df.empty else {}
    latest = regime_df.iloc[-1].to_dict() if not regime_df.empty else None
    summary = {
        'status': 'ok',
        'engine_mode': regime_schema.get('phase1_engine', {}).get('mode', 'rule_based'),
        'regime_count': int(len(regime_df)),
        'regime_ids': [item.get('id') for item in regime_schema.get('regimes', [])],
        'counts': counts,
        'latest': latest,
        'recent': regime_df.tail(20).to_dict(orient='records'),
        'data_path': str(out_data),
    }
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'ok', 'data_path': str(out_data), 'summary_path': str(out_json), 'regime_count': len(regime_df)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
