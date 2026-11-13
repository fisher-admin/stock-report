#!/usr/bin/env python3
"""一次性：用修复后引擎计算真实 alpha001-101（WorldQuant-101）研究面板，供扩因子池海选。
alpha102-191 经确认为参数变体/重复，不纳入。输出 data/factors/research_panel_101.parquet。"""
import time
import pandas as pd
import build_gtja_alpha191_panel as b
import expand_alpha191_factors as ex

t0 = time.time()
df = b.load_daily_data()
df = b.filter_universe(df)
df = b.compute_derived(df)
print(f"universe: {len(df)} rows {df['ts_code'].nunique()} stocks", flush=True)

panel = df[["ts_code", "trade_date"]].copy()
for i in range(1, 102):
    fid = f"alpha{i:03d}"
    try:
        panel[fid] = ex.compute_factor(df, fid)
    except Exception as e:
        print(f"  {fid} FAIL {e}", flush=True)
    if i % 20 == 0:
        print(f"  ...{fid} done ({time.time()-t0:.0f}s)", flush=True)

out = b.OUTPUT_DIR / "research_panel_101.parquet"   # OUTPUT_DIR 已是 data/factors
out.parent.mkdir(parents=True, exist_ok=True)
panel.to_parquet(out, index=False)
print(f"saved {out} shape={panel.shape} elapsed={time.time()-t0:.0f}s", flush=True)
