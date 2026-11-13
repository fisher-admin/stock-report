#!/usr/bin/env python3
"""Build a local dataset config for the Qlib sidecar arena.

The config is based on exported factor_factory data and the current label
maturity/readiness report.  It blocks unresolved or intraday labels before
they can enter model validation.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
DEFAULT_EXPORT = ROOT / "qlib_lab/exports/panel_for_qlib.parquet"
DEFAULT_READINESS = ROOT / "outputs/evolution/validation_readiness_and_label_gap_report.json"
DEFAULT_OUTPUT = ROOT / "qlib_lab/exports/qlib_dataset_config.json"
TZ = ZoneInfo("Asia/Shanghai")


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--label-column", default="next_return_close_to_close_1d")
    parser.add_argument("--test-date-fraction", type=float, default=0.2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.panel.exists():
        raise FileNotFoundError(args.panel)
    if not args.readiness.exists():
        raise FileNotFoundError(args.readiness)

    panel = pd.read_parquet(args.panel)
    panel["trade_date"] = panel["trade_date"].astype(str)
    if args.label_column not in panel.columns:
        raise ValueError(f"missing label column: {args.label_column}")

    readiness = json.loads(args.readiness.read_text(encoding="utf-8"))
    label_gap = readiness.get("label_gap", {})
    validation_readiness = readiness.get("validation_readiness", {})

    labeled = panel[panel[args.label_column].notna()].copy()
    dates = sorted(labeled["trade_date"].unique().tolist())
    if len(dates) < 10:
        raise ValueError(f"not enough labeled dates for Qlib sidecar: {len(dates)}")

    split_idx = max(1, int(len(dates) * (1 - args.test_date_fraction)))
    if split_idx >= len(dates):
        split_idx = len(dates) - 1
    train_dates = dates[:split_idx]
    test_dates = dates[split_idx:]

    feature_cols = [
        c for c in panel.columns if c.startswith("f_") and pd.api.types.is_numeric_dtype(panel[c])
    ]
    maturity = "eod_stable"
    if validation_readiness.get("audit_weight_blocked"):
        maturity = "eod_prelim"
    if label_gap.get("next_daily_status") == "pending_eod_label":
        maturity = "intraday_pending"

    config = {
        "generated_at": iso_now(),
        "source_panel": str(args.panel),
        "readiness_report": str(args.readiness),
        "label_column": args.label_column,
        "feature_columns": feature_cols,
        "feature_count": len(feature_cols),
        "all_labeled_date_count": len(dates),
        "train": {
            "start": train_dates[0],
            "end": train_dates[-1],
            "date_count": len(train_dates),
        },
        "test": {
            "start": test_dates[0],
            "end": test_dates[-1],
            "date_count": len(test_dates),
        },
        "label_maturity": {
            "status": maturity,
            "audit_weight_ready": validation_readiness.get("audit_weight_ready"),
            "audit_weight_blocked": validation_readiness.get("audit_weight_blocked"),
            "latest_missing_label_counts": label_gap.get("latest_missing_label_counts"),
            "latest_rows_with_any_missing_label": label_gap.get("latest_rows_with_any_missing_label"),
            "policy": "Qlib sidecar may validate only rows with mature, non-null labels; intraday labels are excluded.",
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
