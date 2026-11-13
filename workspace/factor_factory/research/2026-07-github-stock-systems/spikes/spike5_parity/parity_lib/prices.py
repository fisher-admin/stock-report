"""口径 API: load_prices(panel, *, mode, asof) —— 学 Lean DataNormalizationMode 强制声明.

lean.md §5.2:
  load_prices(..., mode: Literal["raw","qfq_asof","hfq"], asof)  # mode 必填无默认
  恒等式: qfq(t, asof) = close_raw(t) * adj(t) / adj(asof);  hfq(t) = close_raw(t) * adj(t)
tushare adj_factor 天然是 Lean 式累计因子(PriceScaleFactor 语义)。
"""
from __future__ import annotations

import pandas as pd

from .errors import AdjustmentModeError
from .panel import Panel

VALID_MODES = ("raw", "hfq", "qfq_asof")


def load_prices(panel: Panel, *, mode: str, asof: str | None = None) -> pd.DataFrame:
    """mode 为 keyword-only 且无默认: 调用方不显式声明口径 => TypeError。

    - mode='qfq_asof' 不带 asof => AdjustmentModeError(验收5: qfq 无 asof 即 raise)。
    - mode 传 'qfq' 等任何别名 => AdjustmentModeError(只有显式 qfq_asof 存在)。
    """
    if mode not in VALID_MODES:
        raise AdjustmentModeError(
            f"非法复权口径 {mode!r}; 合法值={VALID_MODES}。"
            "qfq 必须写作 mode='qfq_asof' 并显式给 asof, 保证任何产出可复现可对账"
        )
    close = panel.field("close")
    if mode == "raw":
        return close
    adj = panel.field("adj_factor")  # 缺列 => MissingFieldError hard fail
    if mode == "hfq":
        return close * adj
    # qfq_asof
    if asof is None:
        raise AdjustmentModeError("mode='qfq_asof' 必须显式给 asof(无默认、不取当天)")
    if asof not in adj.index:
        raise AdjustmentModeError(f"asof={asof} 不在面板日历内, 拒绝隐式就近取值")
    return close * adj / adj.loc[asof]
