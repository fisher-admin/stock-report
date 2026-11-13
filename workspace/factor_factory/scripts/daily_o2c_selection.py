#!/usr/bin/env python3
"""
daily_o2c_selection.py — Factor Factory 每日 O2C 选股脚本

每日开盘前运行，计算当日 6 因子截面 z-score，选出 top-20，
输出 current_selection_snapshot.json。

Usage:
    python daily_o2c_selection.py [--panel PATH] [--spec PATH] [--output PATH]
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FACTOR_FACTORY_ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
DEFAULT_PANEL = FACTOR_FACTORY_ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'
DEFAULT_SPEC = FACTOR_FACTORY_ROOT / 'outputs' / 'greenfield_multifactor' / 'portfolio_spec.json'
DEFAULT_OUTPUT = FACTOR_FACTORY_ROOT / 'outputs' / 'greenfield_multifactor' / 'current_selection_snapshot.json'

# ---------------------------------------------------------------------------
# The 6 selected O2C factors and their default weights (from portfolio_spec)
# ---------------------------------------------------------------------------
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    'g_intraday_range_expansion': 0.35565363430095814,
    'g_intraday_vwap_deviation':  0.30743943509455496,
    'g_close_strength_ratio':     0.2605795075039091,
    'g_chip_pullback_support':    0.04313339800625389,
    'g_long_cost_concentration':  0.01758386685788636,
    'g_volume_price_divergence':  0.015610158236437513,
}

FACTOR_IDS = list(DEFAULT_FACTOR_WEIGHTS.keys())

TOP_N = 20

# Liquidity thresholds
MIN_AVG_AMOUNT = 50_000             # 日均成交额 > 5000万 (amount 单位: 千元, 5000万=50000千元)
PCT_CHG_LIMIT_UP   = 9.8           # 接近涨停
PCT_CHG_LIMIT_DOWN = -9.8          # 接近跌停

# Timezone
TZ_SHANGHAI = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding='utf-8'))


def zscore_cross_sectional(s: pd.Series) -> pd.Series:
    """Compute cross-sectional z-score for a single date's data."""
    s = pd.to_numeric(s, errors='coerce')
    mu = s.mean(skipna=True)
    sigma = s.std(skipna=True)
    if pd.isna(sigma) or sigma == 0:
        out = pd.Series(np.nan, index=s.index, dtype=float)
        out.loc[s.notna()] = 0.0
        return out
    return (s - mu) / sigma


def safe_float(v, default: float = 0.0) -> float:
    """Convert a value to float, returning default for NaN/None."""
    if v is None:
        return default
    f = float(v)
    if np.isnan(f) or np.isinf(f):
        return default
    return f


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(
    panel_path: Path = DEFAULT_PANEL,
    spec_path: Path = DEFAULT_SPEC,
    output_path: Path = DEFAULT_OUTPUT,
    top_n: int = TOP_N,
) -> dict:
    """Run the daily O2C selection and write the snapshot JSON."""

    # 1) Load panel
    print(f"[daily_o2c] Loading panel: {panel_path}")
    panel = pd.read_parquet(panel_path)
    panel['ts_code'] = panel['ts_code'].astype(str)
    panel['trade_date'] = panel['trade_date'].astype(str)

    # 2) Load portfolio spec (weights + IC)
    print(f"[daily_o2c] Loading portfolio spec: {spec_path}")
    spec = load_json(spec_path)

    factor_weights: dict[str, float] = {}
    for fid in FACTOR_IDS:
        if fid in spec.get('weights', {}):
            factor_weights[fid] = float(spec['weights'][fid])
        elif fid in DEFAULT_FACTOR_WEIGHTS:
            factor_weights[fid] = DEFAULT_FACTOR_WEIGHTS[fid]
        else:
            print(f"[daily_o2c] WARNING: factor {fid} not in spec weights, using default")
            factor_weights[fid] = DEFAULT_FACTOR_WEIGHTS[fid]

    # IC values from spec (if present)
    factor_ic_values: dict[str, float | None] = {}
    for sf in spec.get('selected_factors', []):
        factor_ic_values[sf['factor_id']] = sf.get('train_score')

    # 3) Get latest trade date
    all_dates = sorted(panel['trade_date'].unique())
    latest_date = all_dates[-1]
    print(f"[daily_o2c] Latest trade_date in panel: {latest_date}  (total dates: {len(all_dates)})")

    day = panel[panel['trade_date'] == latest_date].copy()
    print(f"[daily_o2c] Stocks on latest date: {len(day)}")

    # 4) Check that all 6 factor columns exist
    missing = [f for f in FACTOR_IDS if f not in day.columns]
    if missing:
        raise KeyError(f"Missing factor columns in panel: {missing}")

    # 5) Liquidity filtering BEFORE z-score (z-score on full universe for
    #    proper cross-sectional normalization, but selection only from filtered)
    total_scanned = len(day)

    # 5a) Exclude ST stocks (name contains ST)
    is_st = day['name'].astype(str).str.contains(r'ST|st|\*ST', case=False, na=False)
    # 5b) Exclude limit-up / limit-down (涨跌停)
    pct = pd.to_numeric(day['pct_chg'], errors='coerce').fillna(0)
    is_limit = (pct >= PCT_CHG_LIMIT_UP) | (pct <= PCT_CHG_LIMIT_DOWN)
    # 5c) Average amount filter (amount in 千元, threshold 5000万 = 50000千元)
    avg_amt = pd.to_numeric(day['avg_amount_lookback'], errors='coerce').fillna(0)
    low_liquidity = avg_amt < MIN_AVG_AMOUNT

    keep_mask = ~is_st & ~is_limit & ~low_liquidity
    passed_liquidity = int(keep_mask.sum())
    print(f"[daily_o2c] Liquidity filter: {total_scanned} scanned, "
          f"{passed_liquidity} passed (ST={is_st.sum()}, "
          f"limit={is_limit.sum()}, low_liq={low_liquidity.sum()})")

    # 6) Compute cross-sectional z-scores on FULL universe (proper normalization)
    for fid in FACTOR_IDS:
        day[f'{fid}__z'] = zscore_cross_sectional(day[fid])

    # 7) Compute composite score
    day['composite_score'] = 0.0
    for fid, w in factor_weights.items():
        z_col = f'{fid}__z'
        day['composite_score'] = day['composite_score'].add(
            day[z_col].fillna(0.0) * w, fill_value=0.0
        )

    # 8) Filter to liquidity-passed stocks, sort, select top-N
    candidates = day[keep_mask].copy()
    candidates = candidates.sort_values('composite_score', ascending=False)
    selected_count = min(top_n, len(candidates))
    top = candidates.head(selected_count)

    print(f"[daily_o2c] Top-{top_n} selected: {selected_count} stocks")
    print(f"[daily_o2c] Best: {top.iloc[0]['ts_code']} {top.iloc[0].get('name','')} "
          f"score={top.iloc[0]['composite_score']:.4f}")
    print(f"[daily_o2c] Worst in top-N: {top.iloc[-1]['ts_code']} "
          f"score={top.iloc[-1]['composite_score']:.4f}")

    # 9) Build top20 entries
    top20_list: list[dict] = []
    for _, row in top.iterrows():
        entry: dict = {
            'ts_code':          str(row['ts_code']),
            'name':             str(row.get('name', '')),
            'industry':         str(row.get('industry', '未知')),
            'composite_score':  safe_float(row['composite_score']),
        }
        # Raw factor values
        factor_values: dict[str, float | None] = {}
        factor_zscores: dict[str, float | None] = {}
        for fid in FACTOR_IDS:
            raw = row.get(fid)
            z = row.get(f'{fid}__z')
            factor_values[fid] = None if pd.isna(raw) else round(safe_float(raw), 6)
            factor_zscores[fid] = None if pd.isna(z) else round(safe_float(z), 6)
        entry['factor_values'] = factor_values
        entry['factor_zscores'] = factor_zscores

        # Market data
        entry['open']   = safe_float(row.get('open'))
        entry['close']  = safe_float(row.get('close'))
        entry['amount'] = safe_float(row.get('amount'))
        entry['pct_chg'] = safe_float(row.get('pct_chg'))

        # Market cap proxy (log of avg amount)
        avg_amt_val = pd.to_numeric(row.get('avg_amount_lookback', 0), errors='coerce')
        entry['market_cap_proxy'] = round(float(np.log1p(max(0, safe_float(avg_amt_val)))), 4)

        top20_list.append(entry)

    # 10) Build output
    now_sh = datetime.now(TZ_SHANGHAI)
    snapshot = {
        'schema_version': '2026-05-05.daily_o2c_selection.v1',
        'trade_date': latest_date,
        'generated_at': now_sh.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'factor_weights': {k: round(v, 10) for k, v in factor_weights.items()},
        'factor_ic_values': {k: (None if v is None else round(float(v), 6))
                             for k, v in factor_ic_values.items()},
        'selection_stats': {
            'total_scanned': total_scanned,
            'passed_liquidity': passed_liquidity,
            'selected': selected_count,
        },
        'top20': top20_list,
    }

    # 11) Write output (atomic via temp file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    import tempfile
    import os

    content = json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n'
    tmp = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8',
        dir=output_path.parent,
        prefix=f'.{output_path.name}.',
        suffix='.tmp',
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    print(f"[daily_o2c] Snapshot written to: {output_path}")
    print(f"[daily_o2c] Done. trade_date={latest_date}, "
          f"selected={selected_count}, generated_at={snapshot['generated_at']}")
    return snapshot


def main():
    parser = argparse.ArgumentParser(description='Daily O2C stock selection')
    parser.add_argument('--panel', type=Path, default=DEFAULT_PANEL,
                        help='Path to panel.parquet')
    parser.add_argument('--spec', type=Path, default=DEFAULT_SPEC,
                        help='Path to portfolio_spec.json')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help='Output snapshot JSON path')
    parser.add_argument('--top-n', type=int, default=TOP_N,
                        help='Number of top stocks to select')
    args = parser.parse_args()

    t0 = time.time()
    run(
        panel_path=args.panel,
        spec_path=args.spec,
        output_path=args.output,
        top_n=args.top_n,
    )
    elapsed = time.time() - t0
    print(f"[daily_o2c] Total time: {elapsed:.1f}s")


if __name__ == '__main__':
    main()
