#!/usr/bin/env python3
"""离线重建全历史 greenfield multifactor 研究面板 (2026-07-02)。

背景：管线滚动窗口把 greenfield_multifactor_panel.parquet 从 1298 交易日
(20210129~20260626) 压缩成 64 交易日 (20260105~20260702)，原始滚动缓存
(daily_*/stk_factor_*/cyq_perf_*/universe_*) 早期文件已被删除，无法用管线脚本
直接重跑全历史。本脚本从"半成品"数据离线重算：

  1. panel_phase1.parquet (20210129~20260428, 原始层齐全但无 g_*/industry_* 列)
     取原始层列子集。
  2. greenfield_multifactor_panel.parquet (现存 64 日 20260105~20260702) 取
     原始层列子集，补上 phase1 没有覆盖的尾部 (20260429~20260702)。
  3. 两者按 (ts_code, trade_date) 拼接，重叠期(20260105~20260428)优先用 phase1。
  4. 用 greenfield_multifactor_lib.py 的官方函数(不重写公式)重新计算全部
     衍生列：attach_core_features -> min_listing_days过滤 -> attach_industry_features
     -> add_greenfield_factors -> add_forward_labels -> build_daily_regime_features。
  5. 对 factor_registry.backtestable_factors 计算 __z 列。

严格只读 panel_phase1.parquet / greenfield_multifactor_panel.parquet，不写入或
覆盖它们。唯一输出：greenfield_multifactor_panel_full.parquet。

内存策略：raw 层只保留必要列(float32 for numeric, 除 trade_date/ts_code/name 等
标识列)，一次性 concat 后按 ts_code 排序即可整体喂给 attach_core_features
(pandas groupby.transform 天然支持整表一次性处理，不需要手工分批 — 分批只在写出
阶段的 parquet compression 上没有必要，因为 2,662,608 行 x ~190 列 float32
估算内存约 2,662,608*190*4 bytes ≈ 2.0GB，加上 pandas 对象开销，峰值预计
4-6GB，在 16GB 机器上可承受，遂采用整表处理，不做人为的按股票分批。)
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
sys.path.insert(0, str(ROOT / 'scripts'))

import greenfield_multifactor_lib as lib  # noqa: E402

PHASE1_PATH = ROOT / 'data' / 'factors' / 'panel_phase1.parquet'
CURRENT_PATH = ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel.parquet'
OUTPUT_PATH = ROOT / 'data' / 'factors' / 'greenfield_multifactor_panel_full.parquet'
CONFIG_PATH = ROOT / 'config' / 'greenfield_multifactor_v1.yaml'
INDUSTRY_PARQUET_FALLBACK = Path(
    '/Users/fisher/.openclaw/workspace/stock_data/03-working/cache/industry_map.json'
)

RAW_COLUMNS = [
    'ts_code', 'trade_date', 'name',
    'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'vol', 'amount',
    'macd_dif', 'macd_dea', 'macd', 'kdj_k', 'kdj_d', 'kdj_j',
    'rsi_6', 'rsi_12', 'rsi_24', 'boll_upper', 'boll_mid', 'boll_lower', 'cci',
    'cost_5pct', 'cost_15pct', 'cost_50pct', 'cost_85pct', 'cost_95pct',
    'weight_avg', 'winner_rate',
    'avg_amount_lookback', 'trading_ratio_lookback', 'listing_days',
    'liquidity_threshold', 'universe_flag',
]

IDENTIFIER_COLS = {'ts_code', 'trade_date', 'name'}

FLOAT32_SAFE = [c for c in RAW_COLUMNS if c not in IDENTIFIER_COLS and c != 'universe_flag']


def log(msg: str) -> None:
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def load_raw_subset(path: Path, label: str) -> pd.DataFrame:
    import pyarrow.parquet as pq
    names = pq.ParquetFile(path).schema.names
    cols = [c for c in RAW_COLUMNS if c in names]
    missing = [c for c in RAW_COLUMNS if c not in names]
    if missing:
        log(f'{label}: 缺少原始层列(将不存在于该来源中): {missing}')
    df = pd.read_parquet(path, columns=cols, engine='pyarrow')
    df['ts_code'] = df['ts_code'].astype(str)
    df['trade_date'] = df['trade_date'].astype(str)
    for c in cols:
        if c in FLOAT32_SAFE:
            df[c] = pd.to_numeric(df[c], errors='coerce').astype('float32')
    log(f'{label}: 读取完成 shape={df.shape} 日期范围={df["trade_date"].min()}~{df["trade_date"].max()}')
    return df


def build_raw_history() -> pd.DataFrame:
    log('读取 panel_phase1 原始层子集...')
    p1 = load_raw_subset(PHASE1_PATH, 'phase1')

    log('读取现面板原始层子集...')
    cur = load_raw_subset(CURRENT_PATH, 'current')

    # 重叠期(phase1覆盖的日期)优先用phase1；现面板只贡献phase1未覆盖的尾部日期。
    phase1_max_date = p1['trade_date'].max()
    tail = cur[cur['trade_date'] > phase1_max_date].copy()
    log(f'phase1 最大日期={phase1_max_date}; 现面板尾部(> phase1_max_date)贡献 {tail.shape[0]:,} 行, '
        f'日期范围={tail["trade_date"].min() if not tail.empty else None}~{tail["trade_date"].max() if not tail.empty else None}')

    overlap_check = cur[(cur['trade_date'] >= '20260105') & (cur['trade_date'] <= phase1_max_date)]
    log(f'重叠期(20260105~{phase1_max_date}) 现面板有 {overlap_check.shape[0]:,} 行，全部丢弃，优先用 phase1 的口径')

    del cur
    gc.collect()

    combined = pd.concat([p1, tail], ignore_index=True)
    del p1, tail
    gc.collect()

    # (ts_code, trade_date) 理论上此时已唯一(phase1和tail日期区间不重叠)，但做个保险去重
    before = len(combined)
    combined = combined.drop_duplicates(subset=['ts_code', 'trade_date'], keep='first')
    after = len(combined)
    if before != after:
        log(f'去重: {before:,} -> {after:,} 行 (丢弃 {before - after:,} 条重复 ts_code+trade_date)')

    combined = combined.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    log(f'原始层拼接完成: shape={combined.shape} 日期范围={combined["trade_date"].min()}~{combined["trade_date"].max()} '
        f'股票数={combined["ts_code"].nunique()}')
    return combined


def load_industry_map_with_fallback(cfg) -> pd.DataFrame:
    if cfg.industry_map_path.exists():
        log(f'使用配置指定的 industry_map_path: {cfg.industry_map_path}')
        return lib.load_industry_map(cfg)
    log(f'配置指定的 industry_map_path 不存在({cfg.industry_map_path})，回退到 {INDUSTRY_PARQUET_FALLBACK}')
    import json
    d = json.loads(INDUSTRY_PARQUET_FALLBACK.read_text(encoding='utf-8'))
    industry_map = pd.DataFrame({'ts_code': list(d.keys()), 'industry': list(d.values())})
    industry_map['market'] = '未知'
    industry_map['industry'] = industry_map['industry'].fillna('未知').astype(str)
    return industry_map.drop_duplicates(subset=['ts_code'])


def main() -> None:
    t0 = time.time()
    cfg = lib.load_config(CONFIG_PATH)
    log(f'配置加载完成: min_listing_days={cfg.min_listing_days} start_date={cfg.start_date}')

    raw = build_raw_history()

    log('调用 attach_core_features (逐股票连续时间序列滚动特征)...')
    panel = lib.attach_core_features(raw)
    del raw
    gc.collect()
    log(f'attach_core_features 完成: shape={panel.shape}')

    before_n = len(panel)
    panel = panel[panel['listing_days'].fillna(0) >= cfg.min_listing_days].reset_index(drop=True)
    log(f'min_listing_days>={cfg.min_listing_days} 过滤: {before_n:,} -> {len(panel):,} 行')

    industry_map = load_industry_map_with_fallback(cfg)
    log(f'industry_map: {len(industry_map):,} 条记录, 匹配率检查中...')
    matched = panel['ts_code'].isin(set(industry_map['ts_code'])).mean()
    log(f'  面板 ts_code 命中 industry_map 比例: {matched:.4f}')

    log('调用 attach_industry_features...')
    panel = lib.attach_industry_features(panel, industry_map)
    gc.collect()
    log(f'attach_industry_features 完成: shape={panel.shape}')

    log('调用 add_greenfield_factors (55个g_*列，含f_amihud_20风格的groupby.apply，预计较慢)...')
    t_g = time.time()
    panel = lib.add_greenfield_factors(panel)
    gc.collect()
    log(f'add_greenfield_factors 完成: shape={panel.shape} 耗时={time.time()-t_g:.1f}s')

    log('调用 add_forward_labels...')
    panel = lib.add_forward_labels(panel)
    gc.collect()
    log(f'add_forward_labels 完成: shape={panel.shape}')

    log('调用 build_daily_regime_features...')
    regimes = lib.build_daily_regime_features(panel)
    panel = panel.merge(regimes, on='trade_date', how='left')
    panel = panel.sort_values(['trade_date', 'ts_code']).reset_index(drop=True)
    gc.collect()
    log(f'build_daily_regime_features + merge 完成: shape={panel.shape}')

    backtestable_factors = [f['factor_id'] for f in cfg.factor_registry['backtestable_factors']]
    log(f'对 {len(backtestable_factors)} 个 backtestable_factors 计算 __z 列: {backtestable_factors}')
    missing_factors = [f for f in backtestable_factors if f not in panel.columns]
    if missing_factors:
        log(f'!! 警告: 以下 factor_id 在面板中缺失，无法计算 __z: {missing_factors}')
        backtestable_factors = [f for f in backtestable_factors if f in panel.columns]
    panel = lib.add_cross_sectional_zscores(panel, backtestable_factors)
    gc.collect()
    log(f'add_cross_sectional_zscores 完成: shape={panel.shape}')

    # --- 缺失/大范围NaN检查 ---
    log('=== g_* 列缺失/NaN 检查 ===')
    g_cols = [c for c in panel.columns if c.startswith('g_') and not c.endswith('__z')]
    nan_report = []
    for c in g_cols:
        nan_frac = panel[c].isna().mean()
        nan_report.append((c, nan_frac))
        if nan_frac > 0.5:
            log(f'  !! {c}: NaN比例={nan_frac:.4f} (超过50%，需要在报告中说明)')
    log(f'共检查 {len(g_cols)} 个 g_* 列')

    # --- dtype 优化: 非标签列转 float32 以省内存/磁盘 ---
    log('转换数值列为 float32 (标签/regime相关保留float64以防精度问题影响后续统计)...')
    keep_float64 = {
        'next_return_close_to_close_1d', 'next_return_open_to_close_1d', 'next_return_close_to_open_1d',
        'regime_confidence', 'style_regime_confidence', 'sector_regime_confidence',
        'market_return_mean', 'market_return_median', 'breadth_ratio', 'amount_ratio_median',
        'hl_range_mean', 'small_minus_large', 'top5_contribution_share',
    }
    for c in panel.columns:
        if c in keep_float64 or c in IDENTIFIER_COLS or c in ('industry', 'market', 'regime_id', 'style_regime_id', 'sector_regime_id'):
            continue
        if panel[c].dtype == np.float64:
            panel[c] = panel[c].astype('float32')

    log(f'最终面板: shape={panel.shape} 列数={panel.shape[1]} 日期范围={panel["trade_date"].min()}~{panel["trade_date"].max()} '
        f'交易日数={panel["trade_date"].nunique()} 股票数={panel["ts_code"].nunique()}')

    log(f'写出 {OUTPUT_PATH} ...')
    panel.to_parquet(OUTPUT_PATH, engine='pyarrow', compression='snappy', index=False)
    log(f'写出完成. 耗时总计 {time.time()-t0:.1f}s')

    # 供后续对照脚本/报告脚本使用的元信息落盘到脚本可复用的小文件
    meta_path = ROOT / 'data' / 'factors' / '.panel_full_build_meta.txt'
    with meta_path.open('w', encoding='utf-8') as f:
        f.write(f'rows={panel.shape[0]}\n')
        f.write(f'cols={panel.shape[1]}\n')
        f.write(f'date_min={panel["trade_date"].min()}\n')
        f.write(f'date_max={panel["trade_date"].max()}\n')
        f.write(f'n_dates={panel["trade_date"].nunique()}\n')
        f.write(f'n_codes={panel["ts_code"].nunique()}\n')
        f.write(f'backtestable_factors={",".join(backtestable_factors)}\n')
        f.write(f'g_cols={",".join(g_cols)}\n')
        for c, frac in nan_report:
            f.write(f'nan_frac__{c}={frac:.6f}\n')
    log(f'元信息写出到 {meta_path}')


if __name__ == '__main__':
    main()
