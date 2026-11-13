"""测试共用: 路径、日历、数据源、常量."""
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE_DIR = os.path.dirname(HERE)
DATA_DIR = os.path.join(SPIKE_DIR, "data")
sys.path.insert(0, SPIKE_DIR)

from parity_lib.calendar import TradingCalendar          # noqa: E402
from parity_lib.sources import (CACHE_DIR, CacheSource,  # noqa: E402
                                ClippedSource, DropRowsSource, SampleSource)
import parity_lib.factors as F                           # noqa: E402

T_END = "20260710"          # 样本终点交易日 T
REAL_SUSP_CODE = "688981.SH"
REAL_SUSP_DATES = ["20250901", "20250902", "20250903", "20250904", "20250905", "20250908"]

_cal = None
_src = None


def calendar() -> TradingCalendar:
    global _cal
    if _cal is None:
        cal_df = pd.read_parquet(os.path.join(DATA_DIR, "trade_cal.parquet"))
        _cal = TradingCalendar.from_trade_cal(cal_df)
    return _cal


def sample_source() -> SampleSource:
    global _src
    if _src is None:
        _src = SampleSource(DATA_DIR)
    return _src


def cache_available() -> bool:
    return os.path.isdir(CACHE_DIR)


def windows_250_5():
    """规格窗: [T-250, T] 与 [T-5, T]。"""
    cal = calendar()
    return (cal.shift(T_END, -250), T_END), (cal.shift(T_END, -5), T_END)
