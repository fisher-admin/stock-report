"""窗口决定论时序算子: 每个输出值只由其自身窗口内的根数决定.

为什么不用 pandas.rolling 聚合: pandas roll_mean/roll_var 是滑动累加实现,
浮点累积状态依赖序列起点 —— 同一窗口在不同装载起点下可能差最后一个 ulp,
无法满足验收1 的 rtol=0 恒等回放。本模块用 sliding_window_view 对每个窗口
独立归约(归约顺序只由窗宽决定), 起点无关 => 恒等回放可精确成立。

NaN 语义: 严格传播 —— 窗口内含任一 NaN 即输出 NaN(等价 min_periods=窗宽,
反 qlib min_periods=1)。停牌日 NaN 因此天然穿透(验收4)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def _prep(df: pd.DataFrame):
    a = df.to_numpy(dtype=np.float64)
    out = np.full(a.shape, np.nan)
    return a, out


def roll_mean(df: pd.DataFrame, w: int) -> pd.DataFrame:
    a, out = _prep(df)
    if a.shape[0] >= w:
        sw = sliding_window_view(a, w, axis=0)  # (T-w+1, N, w)
        out[w - 1:] = sw.mean(axis=2)
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def roll_weighted_sum(df: pd.DataFrame, weights: np.ndarray) -> pd.DataFrame:
    """权重按 oldest->newest 排列; 窗口含 NaN 即 NaN。"""
    w = len(weights)
    a, out = _prep(df)
    if a.shape[0] >= w:
        sw = sliding_window_view(a, w, axis=0)
        out[w - 1:] = (sw * weights).sum(axis=2)
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def roll_corr(dfx: pd.DataFrame, dfy: pd.DataFrame, w: int, chunk: int = 512) -> pd.DataFrame:
    assert dfx.shape == dfy.shape
    ax, out = _prep(dfx)
    ay = dfy.to_numpy(dtype=np.float64)
    T, N = ax.shape
    if T >= w:
        for j0 in range(0, N, chunk):
            j1 = min(j0 + chunk, N)
            sx = sliding_window_view(ax[:, j0:j1], w, axis=0)
            sy = sliding_window_view(ay[:, j0:j1], w, axis=0)
            mx = sx.mean(axis=2, keepdims=True)
            my = sy.mean(axis=2, keepdims=True)
            dx = sx - mx
            dy = sy - my
            cov = (dx * dy).mean(axis=2)
            vx = (dx * dx).mean(axis=2)
            vy = (dy * dy).mean(axis=2)
            with np.errstate(invalid="ignore", divide="ignore"):
                denom = np.sqrt(vx * vy)
                r = cov / denom
                r = np.where(denom > 0, r, np.nan)  # 零方差(如量恒定)=NaN, 确定性
            out[w - 1:, j0:j1] = r
    return pd.DataFrame(out, index=dfx.index, columns=dfx.columns)
