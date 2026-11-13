#!/usr/bin/env python3
"""Export factor_factory panel data into the local Qlib sidecar staging area.

This adapter deliberately keeps factor_factory as the source of truth.  It
does not fetch Qlib sample data and does not change production artifacts.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
DEFAULT_PANEL = ROOT / "data/factors/panel_phase1.parquet"
DEFAULT_EXPORT = ROOT / "qlib_lab/exports/panel_for_qlib.parquet"
DEFAULT_META = ROOT / "qlib_lab/exports/panel_for_qlib.meta.json"
TZ = ZoneInfo("Asia/Shanghai")

REQUIRED_COLUMNS = {
    "trade_date",
    "ts_code",
    "benchmark_eligible",
    "next_return_close_to_close_1d",
}


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--benchmark-only", action="store_true", default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.panel.exists():
        raise FileNotFoundError(args.panel)

    panel = pd.read_parquet(args.panel)
    missing = sorted(REQUIRED_COLUMNS - set(panel.columns))
    if missing:
        raise ValueError(f"panel missing required columns: {missing}")

    panel["trade_date"] = panel["trade_date"].astype(str)
    panel["ts_code"] = panel["ts_code"].astype(str)
    if args.benchmark_only:
        panel = panel[panel["benchmark_eligible"].astype(bool)].copy()

    feature_cols = [
        c
        for c in panel.columns
        if c.startswith("f_") and pd.api.types.is_numeric_dtype(panel[c])
    ]
    label_cols = [
        c
        for c in [
            "next_return_close_to_close_1d",
            "next_return_open_to_close_1d",
            "next_return_close_to_open_1d",
        ]
        if c in panel.columns
    ]
    keep_cols = ["trade_date", "ts_code", "benchmark_eligible"] + feature_cols + label_cols
    export = panel[keep_cols].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    export.to_parquet(args.output, index=False)

    labeled_dates = sorted(
        export.loc[export["next_return_close_to_close_1d"].notna(), "trade_date"].unique().tolist()
    )
    meta = {
        "generated_at": iso_now(),
        "source_panel": str(args.panel),
        "export_path": str(args.output),
        "benchmark_only": bool(args.benchmark_only),
        "rows": int(len(export)),
        "feature_count": len(feature_cols),
        "label_columns": label_cols,
        "trade_date_min": export["trade_date"].min() if len(export) else None,
        "trade_date_max": export["trade_date"].max() if len(export) else None,
        "latest_labeled_trade_date": labeled_dates[-1] if labeled_dates else None,
        "latest_exported_trade_date": export["trade_date"].max() if len(export) else None,
    }
    args.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
