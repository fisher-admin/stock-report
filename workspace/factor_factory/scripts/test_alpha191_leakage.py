#!/usr/bin/env python3
"""单元测试：验证 Alpha191 因子引擎已根治跨股票时序泄漏 + 截面 rank 正确。

回归保护（2026-06 修复）：此前所有滚动算子未按 ts_code 分组，每只股票窗口混入上一只股票的
尾部数据（每股上市首日 100% 有因子值，本应 NaN）；ops.rank 在整张长表上排名而非按 trade_date 截面。
本测试用 2 股合成数据断言：① 时序算子按股分组（每股首行 NaN、按股独立）；② rank 按日截面。

运行：python3 test_alpha191_leakage.py
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import alpha191_ops as ops          # noqa: E402
import expand_alpha191_factors as ex  # noqa: E402


def _synthetic() -> pd.DataFrame:
    rows = []
    for code, base in [("A.SZ", 10.0), ("B.SZ", 50.0)]:
        for d in range(10):
            rows.append(dict(
                ts_code=code, trade_date=f"202601{d:02d}",
                open=base + d, high=base + d + 1, low=base + d - 1, close=base + d + 0.5,
                vol=1000.0 + d * 10, amount=(base + d) * 1000.0, pct_chg=d * 0.1,
            ))
    df = pd.DataFrame(rows).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return ex.compute_derived(df)


def main() -> int:
    df = _synthetic()
    a0 = df.index[df.ts_code == "A.SZ"][0]
    b0 = df.index[df.ts_code == "B.SZ"][0]

    # ① 泄漏根治：需要 1 期历史的因子，每股首行应为 NaN（泄漏时 B 首行会用 A 的尾部数据）。
    f12 = ex.compute_factor(df, "alpha012")  # sign(delta(v,1))*(-delta(c,1))
    assert np.isnan(f12.iloc[a0]) and np.isnan(f12.iloc[b0]), "跨股票泄漏未根治：每股首行应为 NaN"

    # ② 不含截面 rank 的因子：合并计算应与逐股单独计算逐元素一致。
    for fid in ("alpha012", "alpha006", "alpha009"):
        full = ex.compute_factor(df, fid).to_numpy()
        indep = np.concatenate([
            ex.compute_factor(df[df.ts_code == c].copy(), fid).to_numpy() for c in ("A.SZ", "B.SZ")
        ])
        assert np.allclose(np.nan_to_num(full), np.nan_to_num(indep), atol=1e-9), f"{fid} 未按股分组"

    # ③ rank 按 trade_date 截面：同一交易日两股 rank 应为 {0.5, 1.0}。
    close_mi = pd.Series(
        df["close"].to_numpy(),
        index=pd.MultiIndex.from_arrays([df["ts_code"], df["trade_date"]], names=["ts_code", "trade_date"]),
    )
    day_rank = ops.rank(close_mi).xs("20260103", level="trade_date")
    assert sorted(day_rank.round(3).tolist()) == [0.5, 1.0], "rank 未按日截面排名"

    # ④ 含 rank + 时序的复合因子可算且每股首行 NaN（cov 需历史）。
    f13 = ex.compute_factor(df, "alpha013")
    assert np.isnan(f13.iloc[b0]), "复合因子 B 股首行应为 NaN"

    # ⑤ ops 平铺序列（无多级索引）保持原行为（向后兼容单股调用）。
    flat = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert ops.delay(flat, 1).tolist()[1:] == [1.0, 2.0, 3.0]

    print("alpha191 因子引擎泄漏/截面 rank 单测全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
