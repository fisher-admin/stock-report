#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from greenfield_multifactor_lib import (
    add_cross_sectional_zscores,
    build_greenfield_panel,
    dump_json,
    load_config,
    write_parquet_atomic,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build independent greenfield factor pool for A-share multi-factor research')
    parser.add_argument('--config', default='/Users/fisher/.openclaw/workspace/factor_factory/config/greenfield_multifactor_v1.yaml')
    return parser.parse_args()


def build_current_only_quality_snapshot(cfg, latest_date: str) -> tuple[pd.DataFrame, dict]:
    pit_files = sorted(cfg.pit_financial_dir.glob('pit_financial_*.parquet'))
    if not pit_files:
        return pd.DataFrame(columns=['ts_code', 'g_quality_current', 'g_balance_sheet_current']), {'coverage': 0, 'latest_ann_date': None}
    pit = pd.concat([pd.read_parquet(p) for p in pit_files], ignore_index=True)
    pit['ts_code'] = pit['ts_code'].astype(str)
    pit['ann_date'] = pit['ann_date'].astype(str)
    pit = pit.sort_values(['ts_code', 'ann_date']).drop_duplicates(subset=['ts_code'], keep='last').copy()
    for col in ['roe', 'netprofit_yoy', 'debt_to_assets', 'current_ratio', 'grossprofit_margin']:
        if col in pit.columns:
            pit[col] = pd.to_numeric(pit[col], errors='coerce')
    def z(s: pd.Series) -> pd.Series:
        mean = s.mean(skipna=True)
        std = s.std(skipna=True)
        if pd.isna(std) or std == 0:
            return pd.Series(0.0, index=s.index)
        return (s - mean) / std
    pit['g_quality_current'] = 0.45 * z(pit['roe']) + 0.35 * z(pit['netprofit_yoy']) + 0.20 * z(pit['grossprofit_margin'])
    pit['g_balance_sheet_current'] = -0.50 * z(pit['debt_to_assets']) + 0.30 * z(pit['current_ratio']) + 0.20 * z(pit['roe'])
    snapshot = pit[['ts_code', 'ann_date', 'g_quality_current', 'g_balance_sheet_current']].copy()
    meta = {
        'coverage': int(snapshot['ts_code'].nunique()),
        'latest_ann_date': snapshot['ann_date'].max() if not snapshot.empty else None,
        'latest_panel_trade_date': latest_date,
        'note': 'current-only quality factors use the latest PIT snapshot and are not used in long-history walk-forward until broader historical PIT coverage is built.',
    }
    return snapshot, meta


def main() -> int:
    args = parse_args()
    cfg = load_config(Path(args.config))
    panel, regimes = build_greenfield_panel(cfg)
    if panel.empty:
        raise SystemExit('empty greenfield panel')

    factor_cols = [item['factor_id'] for item in cfg.factor_registry['backtestable_factors']]
    panel = add_cross_sectional_zscores(panel, factor_cols)
    cfg.panel_path.parent.mkdir(parents=True, exist_ok=True)
    # 覆盖护栏（20260702 事故复盘）：本脚本按滚动窗设计，但历史上同一路径曾存放
    # 1298 日全历史研究面板，被每日重建静默截断且无备份。若磁盘上现有面板的日期
    # 跨度显著大于本次新面板（>2x），先归档为 .pre_overwrite.bak 再写入，绝不静默丢历史。
    try:
        if cfg.panel_path.exists():
            import pandas as _pd
            _old_dates = _pd.read_parquet(cfg.panel_path, columns=['trade_date'])['trade_date'].astype(str).nunique()
            _new_dates = panel['trade_date'].astype(str).nunique()
            if _old_dates > 2 * max(_new_dates, 1):
                _bak = cfg.panel_path.with_suffix('.pre_overwrite.bak.parquet')
                cfg.panel_path.replace(_bak)
                print(f"[guard] 现有面板 {_old_dates} 日 >2x 新面板 {_new_dates} 日，已归档到 {_bak.name} 再覆盖")
    except Exception as _ge:  # 护栏自身失败不阻断每日构建
        print(f"[guard] overwrite-guard check failed (non-fatal): {_ge}")
    write_parquet_atomic(panel, cfg.panel_path, index=False)

    latest_date = str(panel['trade_date'].max())
    current_only_quality, quality_meta = build_current_only_quality_snapshot(cfg, latest_date)
    latest_rows = panel.loc[panel['trade_date'] == latest_date].copy()
    latest_rows = latest_rows.merge(current_only_quality[['ts_code', 'g_quality_current', 'g_balance_sheet_current']], on='ts_code', how='left')

    factor_stats = []
    current_leaders = {}
    for item in cfg.factor_registry['backtestable_factors']:
        fid = item['factor_id']
        family = item['family']
        series = pd.to_numeric(panel[fid], errors='coerce')
        coverage_ratio = float(series.notna().mean())
        usable_dates = int(panel.loc[series.notna(), 'trade_date'].nunique())
        latest_subset = latest_rows[['ts_code', 'name', 'industry', fid]].dropna(subset=[fid]).nlargest(10, fid)
        current_leaders[fid] = {
            'family': family,
            'top10': latest_subset.to_dict(orient='records'),
        }
        factor_stats.append({
            'factor_id': fid,
            'family': family,
            'coverage_ratio': coverage_ratio,
            'usable_dates': usable_dates,
            'latest_non_null_count': int(latest_rows[fid].notna().sum()),
            'latest_mean': float(pd.to_numeric(latest_rows[fid], errors='coerce').mean(skipna=True)),
            'latest_std': float(pd.to_numeric(latest_rows[fid], errors='coerce').std(skipna=True)),
        })

    if not latest_rows.empty:
        def z(s: pd.Series) -> pd.Series:
            s = pd.to_numeric(s, errors='coerce')
            mean = s.mean(skipna=True)
            std = s.std(skipna=True)
            if pd.isna(std) or std == 0:
                return pd.Series(0.0, index=s.index)
            return (s - mean) / std
        latest_rows['g_quality_current_z'] = z(latest_rows['g_quality_current'])
        latest_rows['g_balance_sheet_current_z'] = z(latest_rows['g_balance_sheet_current'])
    current_only_leaders = {
        'g_quality_current': latest_rows[['ts_code', 'name', 'industry', 'g_quality_current']].dropna(subset=['g_quality_current']).nlargest(10, 'g_quality_current').to_dict(orient='records'),
        'g_balance_sheet_current': latest_rows[['ts_code', 'name', 'industry', 'g_balance_sheet_current']].dropna(subset=['g_balance_sheet_current']).nlargest(10, 'g_balance_sheet_current').to_dict(orient='records'),
    }

    factor_pool_snapshot = {
        'schema_version': '2026-04-19.greenfield_factor_pool.v1',
        'panel_path': str(cfg.panel_path),
        'history_range': {
            'start_date': str(panel['trade_date'].min()),
            'end_date': str(panel['trade_date'].max()),
            'trade_dates': int(panel['trade_date'].nunique()),
            'stocks': int(panel['ts_code'].nunique()),
            'rows': int(len(panel)),
        },
        'regime_coverage': regimes['regime_id'].value_counts().to_dict(),
        'backtestable_factor_count': len(cfg.factor_registry['backtestable_factors']),
        'current_only_factor_count': len(cfg.factor_registry['current_only_factors']),
        'legacy_role': {
            'prebreakout_v41': 'not_loaded_into_greenfield_panel',
            'selection_history': 'not_used',
            'benchmark_fix_work': 'excluded_from_greenfield_mainline',
        },
        'factor_stats': factor_stats,
        'current_only_quality_meta': quality_meta,
        'latest_trade_date_snapshot': {
            'trade_date': latest_date,
            'rows': int(len(latest_rows)),
            'current_only_coverage': {
                'g_quality_current': int(latest_rows['g_quality_current'].notna().sum()),
                'g_balance_sheet_current': int(latest_rows['g_balance_sheet_current'].notna().sum()),
            },
        },
    }
    dump_json(cfg.output_root / 'factor_pool_snapshot.json', factor_pool_snapshot)
    dump_json(cfg.output_root / 'current_factor_leaders.json', {
        'trade_date': latest_date,
        'backtestable_factors': current_leaders,
        'current_only_factors': current_only_leaders,
    })

    summary = {
        'status': 'ok',
        'panel_path': str(cfg.panel_path),
        'snapshot_path': str(cfg.output_root / 'factor_pool_snapshot.json'),
        'leaders_path': str(cfg.output_root / 'current_factor_leaders.json'),
        'latest_trade_date': latest_date,
        'rows': int(len(panel)),
        'trade_dates': int(panel['trade_date'].nunique()),
        'backtestable_factor_count': len(cfg.factor_registry['backtestable_factors']),
        'current_only_factor_count': len(cfg.factor_registry['current_only_factors']),
    }
    print(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
