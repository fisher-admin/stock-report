#!/usr/bin/env python3
"""Run a minimal local Qlib sidecar factor arena experiment.

This script verifies that Qlib is importable in the local sidecar environment,
then runs conservative baseline models on factor_factory's exported panel.
It uses mature non-null labels only and writes a JSON report for synchronization
back into factor_factory.
"""

from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

try:
    from lightgbm import LGBMRegressor
except Exception:  # pragma: no cover - fallback path is runtime dependent
    LGBMRegressor = None


ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
DEFAULT_PANEL = ROOT / "qlib_lab/exports/panel_for_qlib.parquet"
DEFAULT_CONFIG = ROOT / "qlib_lab/exports/qlib_dataset_config.json"
DEFAULT_OUTPUT = ROOT / "qlib_lab/experiments/qlib_factor_arena_report.json"
TZ = ZoneInfo("Asia/Shanghai")


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def rank_ic_by_date(frame: pd.DataFrame, pred_col: str, label_col: str) -> dict:
    values = []
    for _, group in frame.groupby("trade_date"):
        if group[pred_col].nunique(dropna=True) < 2 or group[label_col].nunique(dropna=True) < 2:
            continue
        corr = spearmanr(group[pred_col], group[label_col], nan_policy="omit").correlation
        if corr is not None and np.isfinite(corr):
            values.append(float(corr))
    if not values:
        return {"mean": None, "std": None, "count": 0}
    return {
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "count": len(values),
    }


def top_bottom_spread(frame: pd.DataFrame, pred_col: str, label_col: str, q: float = 0.2) -> dict:
    spreads = []
    for _, group in frame.groupby("trade_date"):
        group = group.sort_values(pred_col)
        n = len(group)
        k = max(1, int(n * q))
        if n < 2 * k:
            continue
        top = group.tail(k)[label_col].mean()
        bottom = group.head(k)[label_col].mean()
        if np.isfinite(top) and np.isfinite(bottom):
            spreads.append(float(top - bottom))
    if not spreads:
        return {"mean": None, "count": 0}
    return {"mean": float(np.mean(spreads)), "count": len(spreads)}


def build_models() -> dict:
    models = {
        "ridge": make_pipeline(StandardScaler(with_mean=True), Ridge(alpha=1.0)),
        "hist_gradient_boosting": HistGradientBoostingRegressor(
            max_iter=100,
            max_leaf_nodes=15,
            learning_rate=0.05,
            random_state=42,
        ),
    }
    if LGBMRegressor is not None:
        models["lightgbm"] = LGBMRegressor(
            n_estimators=100,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=20,
            subsample=0.9,
            colsample_bytree=0.9,
            random_state=42,
            verbosity=-1,
        )
    return models


def main() -> int:
    args = parse_args()
    if not args.panel.exists():
        raise FileNotFoundError(args.panel)
    if not args.config.exists():
        raise FileNotFoundError(args.config)

    qlib = importlib.import_module("qlib")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    panel = pd.read_parquet(args.panel)
    panel["trade_date"] = panel["trade_date"].astype(str)

    label_col = config["label_column"]
    features = config["feature_columns"]
    usable = panel[["trade_date", "ts_code", label_col] + features].dropna(subset=[label_col]).copy()
    usable[features] = usable[features].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    train_mask = (usable["trade_date"] >= config["train"]["start"]) & (
        usable["trade_date"] <= config["train"]["end"]
    )
    test_mask = (usable["trade_date"] >= config["test"]["start"]) & (
        usable["trade_date"] <= config["test"]["end"]
    )
    train = usable[train_mask].copy()
    test = usable[test_mask].copy()
    if train.empty or test.empty:
        raise ValueError("empty train or test set after split")

    X_train = train[features]
    y_train = train[label_col]
    X_test = test[features]
    y_test = test[label_col]

    model_reports = []
    for name, model in build_models().items():
        model.fit(X_train, y_train)
        pred = model.predict(X_test)
        eval_frame = test[["trade_date", "ts_code", label_col]].copy()
        eval_frame["prediction"] = pred
        mse = float(mean_squared_error(y_test, pred))
        rank_ic = rank_ic_by_date(eval_frame, "prediction", label_col)
        spread = top_bottom_spread(eval_frame, "prediction", label_col)
        model_reports.append(
            {
                "model": name,
                "test_rows": int(len(test)),
                "mse": mse,
                "rank_ic": rank_ic,
                "top_bottom_spread": spread,
            }
        )

    report = {
        "generated_at": iso_now(),
        "qlib_import_ok": True,
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "source_panel": str(args.panel),
        "dataset_config": str(args.config),
        "label_column": label_col,
        "feature_count": len(features),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_window": config["train"],
        "test_window": config["test"],
        "label_maturity": config.get("label_maturity", {}),
        "models": model_reports,
        "decision": {
            "sidecar_only": True,
            "may_promote_factor": False,
            "reason": "Qlib sidecar validates candidate signals only; factor promotion remains controlled by factor_factory gates.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
