from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "test_factor_combinations.py"
SPEC = importlib.util.spec_from_file_location("legacy_factor_combinations", MODULE_PATH)
COMBINATIONS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COMBINATIONS)


def test_missing_selected_factor_is_excluded_before_walk_forward() -> None:
    frame = pd.DataFrame({"factor_a": [1.0], "label_exec_return": [0.01]})
    assert COMBINATIONS.available_selected_factors(frame, ["factor_a", "factor_missing"]) == [
        "factor_a"
    ]


def test_negative_icir_never_receives_portfolio_weight() -> None:
    rows = []
    for date_index in range(6):
        values = np.arange(30, dtype=float)
        labels = values + np.random.default_rng(date_index).normal(
            loc=0.0,
            scale=1.0 + date_index,
            size=len(values),
        )
        for value, label in zip(values, labels):
            rows.append(
                {
                    "trade_date": f"202601{date_index + 1:02d}",
                    "positive": value,
                    "negative": -value,
                    "label_exec_return": label,
                }
            )
    frame = pd.DataFrame(rows)

    weights = COMBINATIONS.icir_weighted_portfolio(
        frame,
        ["positive", "negative"],
        lookback=6,
    )

    assert weights["positive"] == 1.0
    assert weights["negative"] == 0.0


def test_best_combination_must_have_positive_absolute_result() -> None:
    results = {
        5: {"hit_rate": 0.60, "avg_return": -0.001, "total_return": -0.02},
        10: {"hit_rate": 0.50, "avg_return": 0.001, "total_return": 0.03},
    }

    best = COMBINATIONS.choose_best_result(results)

    assert best[0] == 10
