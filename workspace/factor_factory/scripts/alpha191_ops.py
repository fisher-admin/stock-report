"""
Alpha191 Operations Engine
===========================
Time-series and cross-sectional operations for computing Alpha191 factors.

All functions operate on pandas DataFrames/Series. Data must be sorted by
(ts_code, trade_date) before calling these functions.

Typical DataFrame columns: ts_code, trade_date, open, high, low, close,
volume, amount, vwap, returns, turn, cap, etc.
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 分组辅助（2026-06 根治跨股票时序泄漏 + 截面 rank 错误）
# ---------------------------------------------------------------------------
# 设计：当传入序列带 (ts_code, trade_date) 多级索引时，
#   - 时序算子（delay/delta/mean/std/...）按 ts_code 分组，窗口不跨股票；
#   - 截面算子（rank/scale）按 trade_date 分组，同日横截面排名/归一。
# 当传入普通序列（无多级索引）时退回原平铺行为，保持 self-test 与单股调用兼容。
_TS_LEVEL = "ts_code"
_XS_LEVEL = "trade_date"


def _has_level(series: pd.Series, level: str) -> bool:
    return isinstance(series.index, pd.MultiIndex) and level in (series.index.names or [])


def _ts_apply(series: pd.Series, fn):
    """时序算子：有 ts_code 级则按股分组逐组算，否则平铺；结果按原索引顺序还原。
    用手动 concat 而非 groupby.apply，避免单分组时被 pandas 误塑成 DataFrame。"""
    if _has_level(series, _TS_LEVEL):
        pieces = [fn(g) for _, g in series.groupby(level=_TS_LEVEL, sort=False)]
        return pd.concat(pieces).reindex(series.index)
    return fn(series)


def _ts_apply2(x: pd.Series, y: pd.Series, fn):
    """双序列时序算子（corr/cov/reg_beta）：按 ts_code 分组配对计算。"""
    if _has_level(x, _TS_LEVEL):
        frame = pd.DataFrame({"x": x, "y": y})
        pieces = [fn(g["x"], g["y"]) for _, g in frame.groupby(level=_TS_LEVEL, sort=False)]
        return pd.concat(pieces).reindex(x.index)
    return fn(x, y)


def _xs_apply(series: pd.Series, fn):
    """截面算子：有 trade_date 级则按日分组逐日算，否则平铺。"""
    if _has_level(series, _XS_LEVEL):
        pieces = [fn(g) for _, g in series.groupby(level=_XS_LEVEL, sort=False)]
        return pd.concat(pieces).reindex(series.index)
    return fn(series)


# ---------------------------------------------------------------------------
# Delay / Delta
# ---------------------------------------------------------------------------

def delay(series: pd.Series, d: int) -> pd.Series:
    """Shift *series* forward by *d* periods (past values).

    Equivalent to ``series.shift(d)``.  The first *d* values will be NaN.

    >>> s = pd.Series([1, 2, 3, 4, 5])
    >>> delay(s, 2).tolist()
    [nan, nan, 1.0, 2.0, 3.0]
    """
    return _ts_apply(series, lambda s: s.shift(d))


def delta(series: pd.Series, d: int) -> pd.Series:
    """Change over *d* periods: ``series - delay(series, d)``.

    >>> s = pd.Series([10, 12, 15, 18])
    >>> delta(s, 1).tolist()
    [nan, 2.0, 3.0, 3.0]
    """
    return series - delay(series, d)


# ---------------------------------------------------------------------------
# Cross-sectional rank
# ---------------------------------------------------------------------------

def rank(series: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank (0-1).

    When applied to a single column of a long-format DataFrame this is
    typically used via ``df.groupby('trade_date')['close'].apply(rank)``
    or after pivoting.  For a plain Series it ranks within the series.
    """
    return _xs_apply(series, lambda s: s.rank(pct=True))


# ---------------------------------------------------------------------------
# Time-series rolling helpers
# ---------------------------------------------------------------------------

def ts_rank(series: pd.Series, d: int) -> pd.Series:
    """Time-series rank: percentile rank of current value within the last
    *d* periods (inclusive).

    >>> s = pd.Series([3, 1, 4, 1, 5, 9])
    >>> ts_rank(s, 3).iloc[2]  # rank of 4 among [3,1,4]
    1.0
    """
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
    ))


def corr(x: pd.Series, y: pd.Series, d: int) -> pd.Series:
    """Rolling Pearson correlation over *d* periods."""
    return _ts_apply2(x, y, lambda a, b: a.rolling(window=d, min_periods=max(d // 2, 2)).corr(b))


def cov(x: pd.Series, y: pd.Series, d: int) -> pd.Series:
    """Rolling covariance over *d* periods."""
    return _ts_apply2(x, y, lambda a, b: a.rolling(window=d, min_periods=max(d // 2, 2)).cov(b))


def mean(series: pd.Series, d: int) -> pd.Series:
    """Rolling mean over *d* periods."""
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).mean())


def std(series: pd.Series, d: int) -> pd.Series:
    """Rolling standard deviation (population) over *d* periods."""
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).std(ddof=0))


def sum(series: pd.Series, d: int) -> pd.Series:
    """Rolling sum over *d* periods."""
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).sum())


def ts_min(series: pd.Series, d: int) -> pd.Series:
    """Rolling minimum over *d* periods."""
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).min())


def ts_max(series: pd.Series, d: int) -> pd.Series:
    """Rolling maximum over *d* periods."""
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).max())


def product(series: pd.Series, d: int) -> pd.Series:
    """Rolling product over *d* periods."""
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).apply(np.prod, raw=True))


# ---------------------------------------------------------------------------
# Argmin / Argmax position within window
# ---------------------------------------------------------------------------

def ts_argmax(series: pd.Series, d: int) -> pd.Series:
    """Position (0-indexed from window start) of the maximum within the
    last *d* periods.  Returns 0 if all NaN in window.
    """
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).apply(
        lambda x: int(np.nanargmax(x)), raw=True
    ))


def ts_argmin(series: pd.Series, d: int) -> pd.Series:
    """Position (0-indexed from window start) of the minimum within the
    last *d* periods.  Returns 0 if all NaN in window.
    """
    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).apply(
        lambda x: int(np.nanargmin(x)), raw=True
    ))


# ---------------------------------------------------------------------------
# Decay / SMA / Power / Scale
# ---------------------------------------------------------------------------

def decay_linear(series: pd.Series, d: int) -> pd.Series:
    """Linearly weighted moving average with weights [1, 2, ..., d].

    Most recent observation gets weight *d*.

    >>> s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    >>> decay_linear(s, 3).iloc[2]  # (1*1 + 2*2 + 3*3) / (1+2+3) = 14/6
    2.3333333333333335
    """
    weights = np.arange(1, d + 1, dtype=float)
    w_sum = weights.sum()

    def _wma(x):
        if np.isnan(x).all():
            return np.nan
        return np.nansum(x * weights[-len(x):]) / weights[-len(x):].sum()

    return _ts_apply(series, lambda s: s.rolling(window=d, min_periods=1).apply(_wma, raw=True))


def sma_cn(series: pd.Series, n: int, m: float) -> pd.Series:
    """Chinese-style SMA: ``SMA_t = (m * X_t + (n - m) * SMA_{t-1}) / n``.

    This is the recursive moving average used in Chinese technical analysis
    (e.g. TongDaXin / 通达信).  Initial value is ``series.iloc[0]``.

    Parameters
    ----------
    series : pd.Series
    n : int  – smoothing period
    m : float – weight for current value (typically 1 or 2)
    """
    def _sma_flat(s: pd.Series) -> pd.Series:
        arr = s.values.astype(float)
        out = np.empty_like(arr)
        if len(arr) == 0:
            return pd.Series(out, index=s.index)
        out[0] = arr[0] if not np.isnan(arr[0]) else 0.0
        for i in range(1, len(arr)):
            v = arr[i]
            out[i] = out[i - 1] if np.isnan(v) else (m * v + (n - m) * out[i - 1]) / n
        return pd.Series(out, index=s.index)

    return _ts_apply(series, _sma_flat)


def signed_power(series: pd.Series, e: float) -> pd.Series:
    """``sign(series) * abs(series) ** e``"""
    return np.sign(series) * np.abs(series) ** e


def scale(series: pd.Series, d: int = 1) -> pd.Series:
    """Scale so that ``sum(abs(series)) == d`` within each cross-section.

    If *series* is already a single stock's time-series, it rescales the
    whole series.  For cross-sectional use apply via groupby.
    """
    def _scale_flat(s: pd.Series) -> pd.Series:
        denom = s.abs().sum()
        return s * 0.0 if denom == 0 else s * d / denom

    return _xs_apply(series, _scale_flat)


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------

def reg_beta(y: pd.Series, x: pd.Series, d: int) -> pd.Series:
    """Rolling OLS regression slope of *y* on *x* over *d* periods.

    ``beta_t = Cov(y, x, d) / Var(x, d)``
    """
    def _beta(yy: pd.Series, xx: pd.Series) -> pd.Series:
        cov_xy = yy.rolling(window=d, min_periods=max(d // 2, 2)).cov(xx)
        var_x = xx.rolling(window=d, min_periods=max(d // 2, 2)).var(ddof=1)
        return cov_xy / var_x

    return _ts_apply2(y, x, _beta)


# ---------------------------------------------------------------------------
# Safe division
# ---------------------------------------------------------------------------

def safe_div(a: pd.Series, b: pd.Series, fill: float = 0.0) -> pd.Series:
    """Element-wise ``a / b`` replacing infinities and 0-denominator with
    *fill*.

    >>> a = pd.Series([1.0, 2.0, 3.0])
    >>> b = pd.Series([1.0, 0.0, np.nan])
    >>> safe_div(a, b, fill=0.0).tolist()
    [1.0, 0.0, 0.0]
    """
    result = a / b
    result = result.replace([np.inf, -np.inf], np.nan)
    result = result.fillna(fill)
    # Also handle case where b is zero (produces inf then replaced above)
    return result


# ---------------------------------------------------------------------------
# Industry neutralization
# ---------------------------------------------------------------------------

def indneutralize(series: pd.Series, industry: pd.Series) -> pd.Series:
    """Demean within each industry group.

    Parameters
    ----------
    series : pd.Series – factor values
    industry : pd.Series – industry labels (same index)

    Returns
    -------
    pd.Series – ``series - group_mean``
    """
    return series.groupby(industry).transform(lambda x: x - x.mean())


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("Running alpha191_ops self-tests …")
    passed = 0
    failed = 0

    def check(name, got, expected, tol=1e-8):
        global passed, failed
        if isinstance(expected, list):
            ok = len(got) == len(expected)
            if ok:
                for g, e in zip(got, expected):
                    if e is None or (isinstance(e, float) and np.isnan(e)):
                        ok = ok and (g is None or (isinstance(g, float) and np.isnan(g)))
                    else:
                        ok = ok and abs(float(g) - float(e)) < tol
        else:
            ok = abs(float(got) - float(expected)) < tol
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL {name}: got {got}, expected {expected}")

    # delay / delta
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    r = delay(s, 2)
    check("delay", r.iloc[2], 1.0)
    assert np.isnan(r.iloc[0]) and np.isnan(r.iloc[1])

    r = delta(s, 1)
    check("delta", r.iloc[1], 1.0)
    assert np.isnan(r.iloc[0])

    # rank (cross-sectional on a plain series)
    s = pd.Series([30.0, 10.0, 20.0])
    r = rank(s)
    check("rank", r.iloc[1], 1 / 3)

    # mean / std / sum / ts_min / ts_max
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    check("mean(3)@4", mean(s, 3).iloc[3], 3.0)      # (2+3+4)/3
    check("sum(3)@4", sum(s, 3).iloc[4], 12.0)        # 3+4+5
    check("ts_min(3)@3", ts_min(s, 3).iloc[3], 2.0)   # min(2,3,4)
    check("ts_max(3)@3", ts_max(s, 3).iloc[3], 4.0)   # max(2,3,4)

    # std with ddof=0
    r = std(s, 3)
    expected_std = np.std([2.0, 3.0, 4.0], ddof=0)
    check("std(3)@3", r.iloc[3], expected_std)

    # product
    s = pd.Series([2.0, 3.0, 4.0])
    check("product(3)@2", product(s, 3).iloc[2], 24.0)

    # ts_rank
    s = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0, 9.0])
    r = ts_rank(s, 3)
    check("ts_rank(3)@2", r.iloc[2], 1.0)  # 4 is max of [3,1,4]

    # decay_linear
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    r = decay_linear(s, 3)
    check("decay_linear(3)@2", r.iloc[2], (1*1 + 2*2 + 3*3) / 6)

    # sma_cn
    s = pd.Series([2.0, 4.0, 6.0, 8.0])
    r = sma_cn(s, 3, 1)
    # t0: 2, t1: (1*4 + 2*2)/3 = 8/3, t2: (1*6 + 2*8/3)/3 = (6+16/3)/3 = 34/9
    check("sma_cn(3,1)@1", r.iloc[1], 8.0 / 3)
    check("sma_cn(3,1)@2", r.iloc[2], 34.0 / 9)

    # signed_power
    s = pd.Series([-2.0, 3.0])
    r = signed_power(s, 2)
    check("signed_power -2^2", r.iloc[0], -4.0)
    check("signed_power 3^2", r.iloc[1], 9.0)

    # scale
    s = pd.Series([1.0, 2.0, 3.0])
    r = scale(s, 1)
    check("scale", r.abs().sum(), 1.0)

    # reg_beta
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    y = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0])  # y = 2x
    r = reg_beta(y, x, 5)
    check("reg_beta", r.iloc[4], 2.0)

    # safe_div
    a = pd.Series([1.0, 2.0, 3.0])
    b = pd.Series([1.0, 0.0, np.nan])
    r = safe_div(a, b, fill=0.0)
    assert r.iloc[0] == 1.0
    assert r.iloc[1] == 0.0  # 2/0 -> inf -> 0
    assert r.iloc[2] == 0.0  # 3/nan -> nan -> 0
    passed += 1

    # indneutralize
    s = pd.Series([10.0, 20.0, 30.0, 40.0])
    ind = pd.Series(["A", "A", "B", "B"])
    r = indneutralize(s, ind)
    check("indneutralize A@0", r.iloc[0], -5.0)   # 10 - 15
    check("indneutralize B@2", r.iloc[2], -5.0)   # 30 - 35

    # ts_argmax / ts_argmin
    s = pd.Series([5.0, 3.0, 8.0, 1.0, 4.0])
    check("ts_argmax(3)@2", ts_argmax(s, 3).iloc[2], 2.0)  # max of [5,3,8] at pos 2
    check("ts_argmin(3)@2", ts_argmin(s, 3).iloc[2], 1.0)  # min of [5,3,8] at pos 1

    # corr / cov
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    y = pd.Series([2.0, 4.0, 5.0, 4.0, 5.0])
    r = corr(x, y, 5)
    assert not np.isnan(r.iloc[4])
    passed += 1
    r = cov(x, y, 5)
    assert not np.isnan(r.iloc[4])
    passed += 1

    print(f"\nResults: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("All tests passed ✓")
