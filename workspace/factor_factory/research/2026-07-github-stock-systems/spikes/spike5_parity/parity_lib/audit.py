"""低报审计(验收3): lookback+1 vs lookback 扰动, 值变 => required_lookback 低报, CI 拒绝.

原理: 若因子真实需要的历史多于声明值, 那么"多给1根历史"会改变输出(多出的那根参与
了计算); 若声明足额, 多给1根不改变任何输出值(窗口决定论)。机械可复跑, 无人工判断。
"""
from __future__ import annotations

import numpy as np

from .calendar import TradingCalendar
from .errors import LookbackUnderReported
from .panel import build_panel
from .registry import FactorSpec


def _panel_with_lookback(spec: FactorSpec, lb: int, start: str, end: str, source,
                         calendar: TradingCalendar):
    ext_start = calendar.shift(start, -lb)
    daily, adj = source.read(ext_start, end)
    sessions = calendar.range(ext_start, end)
    return build_panel(daily, sessions, ext_start, start, end, adj_long=adj)


def exact_equal(a, b) -> tuple:
    """rtol=0 逐元素相等(NaN==NaN 视为相等)。返回 (bool, max_abs_diff, n_diff)。"""
    assert list(a.index) == list(b.index) and list(a.columns) == list(b.columns), \
        f"index/columns 不一致: {a.shape} vs {b.shape}"
    av = a.to_numpy(dtype=np.float64)
    bv = b.to_numpy(dtype=np.float64)
    both_nan = np.isnan(av) & np.isnan(bv)
    eq = (av == bv) | both_nan
    if eq.all():
        return True, 0.0, 0
    with np.errstate(invalid="ignore"):
        diff = np.abs(av - bv)
    diff[both_nan] = 0.0
    # NaN vs 非NaN 也算 diff(置 inf 便于报告)
    one_nan = np.isnan(av) ^ np.isnan(bv)
    diff[one_nan] = np.inf
    return False, float(np.nanmax(diff)), int((~eq).sum())


def lookback_audit(spec: FactorSpec, start: str, end: str, *, source,
                   calendar: TradingCalendar) -> dict:
    """返回审计明细; 若 +1 扰动值变, raise LookbackUnderReported(CI 拒绝语义)。"""
    lb = spec.required_lookback
    p0 = _panel_with_lookback(spec, lb, start, end, source, calendar)
    p1 = _panel_with_lookback(spec, lb + 1, start, end, source, calendar)
    out0 = spec.compute(p0).loc[start:end]
    out1 = spec.compute(p1).loc[start:end]
    ok, maxdiff, ndiff = exact_equal(out0, out1)
    detail = {"factor": spec.name, "lookback": lb, "plus1_equal": ok,
              "max_abs_diff": maxdiff, "n_diff": ndiff}

    # 信息性紧致检查(lookback-1 应当改变输出, 证明声明不虚胖; 不作为失败条件)
    if lb >= 1:
        pm1 = _panel_with_lookback(spec, lb - 1, start, end, source, calendar)
        outm1 = spec.compute(pm1).loc[start:end]
        tight_changed, _, _ = exact_equal(out0, outm1)
        detail["minus1_changes_output"] = not tight_changed
    if not ok:
        raise LookbackUnderReported(
            f"{spec.name}: required_lookback={lb} 低报 —— +1 扰动下 {ndiff} 个值改变 "
            f"(max_abs_diff={maxdiff}); CI 拒绝")
    return detail
