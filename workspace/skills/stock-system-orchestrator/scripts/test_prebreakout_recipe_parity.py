#!/usr/bin/env python3
"""冒烟：搜索 evaluate 与工厂 confirm 规则一致性（raw confirm）。

不跑全市场回测，只验证：
1) rescore + pick 在 raw confirm 下行为
2) 乘子改变分数但不改变 confirm hits（相对 raw）
3) resolve_trade_date 读 env
4) holdout split 密封元数据
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import prebreakout_recipe_engine as eng  # noqa: E402


def test_confirm_ignores_multiplier() -> None:
    df = pd.DataFrame(
        {
            "ts_code": ["AAA", "BBB", "CCC"],
            "macd_early_signal": [80.0, 40.0, 70.0],
            "volume_warmup": [80.0, 40.0, 70.0],
            "turnover_stability": [80.0, 40.0, 70.0],
            "chip_support": [80.0, 40.0, 70.0],
            "momentum": [50.0, 90.0, 60.0],
        }
    )
    factors = list(df.columns)
    factors.remove("ts_code")
    weights = {f: 1.0 / len(factors) for f in factors}
    mult = {f: 1.0 for f in factors}
    mult["momentum"] = 3.0  # 抬升 BBB 综合分

    scores_raw = eng.rescore_with_mult(df, weights, None, factors)
    scores_m = eng.rescore_with_mult(df, weights, mult, factors)
    # BBB 在乘子后应更高分
    assert float(scores_m.iloc[1]) > float(scores_raw.iloc[1])

    keys = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"]
    hits = eng.confirm_hits_arr_raw(df, keys, 60.0)
    assert int(hits[0]) == 4
    assert int(hits[1]) == 0  # 原始 confirm 不过
    assert int(hits[2]) == 4

    picks = eng.pick_top_codes(
        df,
        scores_m,
        {},
        top_n=2,
        max_per_industry=3,
        score_pct=0,
        min_score=0,
        min_confirm=2,
        confirm_keys=keys,
        confirm_thr=60.0,
    )
    # BBB 即使综合分高，confirm 失败也进不了
    assert "BBB" not in picks
    assert "AAA" in picks


def test_resolve_trade_date_env() -> None:
    os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260101"
    try:
        assert eng.resolve_trade_date(None) == "20260101"
        assert eng.resolve_trade_date("20260202") == "20260202"
    finally:
        os.environ.pop("OPENCLAW_TARGET_TRADE_DATE", None)


def test_holdout_split_sealed() -> None:
    pairs = [(f"d{i}", f"d{i+1}") for i in range(60)]
    train, test, hold, meta = eng.split_pairs_true_holdout(pairs, holdout_days=20)
    assert meta.get("holdout_sealed") is True
    assert len(hold) == 20
    # hold 与 train/test 无交集
    hold_set = set(hold)
    assert not hold_set.intersection(train)
    assert not hold_set.intersection(test)
    # 目标函数不得引用 hold（本测试只检查 split 合同）
    assert len(train) + len(test) + len(hold) == 60


def test_fingerprint_stable() -> None:
    a = eng.production_config_fingerprint()
    b = eng.production_config_fingerprint()
    assert a == b and len(a) == 16


if __name__ == "__main__":
    test_confirm_ignores_multiplier()
    test_resolve_trade_date_env()
    test_holdout_split_sealed()
    test_fingerprint_stable()
    print("OK all parity smokes passed")
