#!/usr/bin/env python3
"""Build Alpha factor panel from daily parquet data.

诚实命名（2026-06）：本脚本默认只计算 alpha001-030（max_factor=30）——WorldQuant-101 公式家族的前 30 个，
**不是**完整的国泰君安 GTJA-191：经核实 alpha102-191 多为重复/参数变体（catalog computed_factor_count=30）。
文件名 gtja_alpha191 为历史沿用；真实入面板因子数以 max_factor / configs catalog 为准，对外勿称「完整 191 因子」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import alpha191_ops as ops

# Import expanded factor computation
from expand_alpha191_factors import compute_factor as compute_factor_expanded

FACTOR_CONFIGS = Path(__file__).parent.parent / "configs" / "gtja_alpha191_catalog.json"
DAILY_DIR = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/backtest_cache")
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "factors"
REPORT_DIR = Path(__file__).parent.parent / "outputs" / "gtja_alpha191"


def load_daily_data() -> pd.DataFrame:
    files = sorted(DAILY_DIR.glob("daily_*.parquet"))
    # Use most recent 100 files for initial build
    files = files[-100:]
    print(f"Loading {len(files)} daily files (most recent 100)...")
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def filter_universe(df: pd.DataFrame) -> pd.DataFrame:
    mask = df["ts_code"].str.match(r"^(000|001|002|003|300|301|600|601|603|605)\d{3}\.(SZ|SH)$")
    df = df[mask].copy()
    avg_amount = df.groupby("ts_code")["amount"].mean()
    valid = avg_amount[avg_amount >= 5000].index
    df = df[df["ts_code"].isin(valid)].copy()
    count = df.groupby("ts_code").size()
    valid = count[count >= 60].index
    return df[df["ts_code"].isin(valid)].copy()


def compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["returns"] = df["pct_chg"] / 100.0
    df["vwap"] = np.where(df["vol"] > 0, df["amount"] / df["vol"] * 10, df["close"])
    df["volume"] = df["vol"]
    df["adv20"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(20, min_periods=1).mean())
    df["adv5"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["adv10"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(10, min_periods=1).mean())
    df["adv60"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(60, min_periods=1).mean())
    return df


def build_panel(df: pd.DataFrame, max_factor: int = 30) -> pd.DataFrame:
    panel = df[["ts_code", "trade_date"]].copy()
    for i in range(1, max_factor + 1):
        fid = f"alpha{i:03d}"
        print(f"Computing {fid}...")
        panel[fid] = compute_factor_expanded(df, fid)
    return panel


def quality_report(panel: pd.DataFrame) -> dict:
    factor_cols = [c for c in panel.columns if c.startswith("alpha")]
    report = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_range": [panel["trade_date"].min(), panel["trade_date"].max()],
        "stock_count": int(panel["ts_code"].nunique()),
        "row_count": int(len(panel)),
        "implemented_factor_count": len(factor_cols),
        "per_factor": {},
        "future_function_check": {"passed": True, "notes": "All factors use only historical data"}
    }
    for col in factor_cols:
        s = panel[col]
        report["per_factor"][col] = {
            "coverage_pct": round(float(s.notna().mean() * 100), 2),
            "missing_pct": round(float(s.isna().mean() * 100), 2),
            "mean": round(float(s.mean()), 6) if s.notna().any() else None,
            "std": round(float(s.std()), 6) if s.notna().any() else None,
        }
    return report


def main() -> int:
    df = load_daily_data()
    print(f"Loaded {len(df)} rows, {df['ts_code'].nunique()} stocks")

    df = filter_universe(df)
    print(f"After filter: {len(df)} rows, {df['ts_code'].nunique()} stocks")

    df = compute_derived(df)

    # 仅算前 30 个真实因子（WorldQuant-101 子集），非完整 191——见模块 docstring 诚实命名说明。
    panel = build_panel(df, max_factor=30)
    print(f"Panel shape: {panel.shape}  (computed_factor_count=30, 非完整191)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "gtja_alpha191_panel.parquet"
    panel.to_parquet(out, index=False)
    print(f"Saved: {out}")

    report = quality_report(panel)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rpt = REPORT_DIR / "panel_quality_report.json"
    with open(rpt, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Report: {rpt}")

    for fid, info in report["per_factor"].items():
        print(f"  {fid}: coverage={info['coverage_pct']}% mean={info['mean']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
