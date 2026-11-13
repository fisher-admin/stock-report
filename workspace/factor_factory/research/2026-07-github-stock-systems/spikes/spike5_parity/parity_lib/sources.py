"""数据源: 只读. SampleSource=spike 本地小样本(tushare 代理拉取的真实数据);
CacheSource=生产 backtest_cache 日线 parquet(只读); 两个包装源用于毒样本构造."""
from __future__ import annotations

import glob
import os

import pandas as pd

CACHE_DIR = "/Users/fisher/.openclaw/workspace/stock_data/03-working/backtest_cache"


class SampleSource:
    """spike5_parity/data/ 下的小样本真实数据(30票, 20240102-20260710, tushare 代理)。"""

    def __init__(self, data_dir: str):
        self._daily = pd.read_parquet(os.path.join(data_dir, "sample_daily.parquet"))
        self._adj = pd.read_parquet(os.path.join(data_dir, "sample_adj_factor.parquet"))
        self.has_adj = True

    @property
    def first_available(self) -> str:
        return str(self._daily["trade_date"].min())

    def read(self, start: str, end: str):
        d = self._daily
        a = self._adj
        daily = d[(d["trade_date"] >= start) & (d["trade_date"] <= end)].copy()
        adj = a[(a["trade_date"] >= start) & (a["trade_date"] <= end)].copy()
        return daily, adj


class CacheSource:
    """生产 backtest_cache daily_*.parquet, 严格只读; 无复权因子(has_adj=False)。"""

    def __init__(self, cache_dir: str = CACHE_DIR):
        self._files = {}
        for p in sorted(glob.glob(os.path.join(cache_dir, "daily_*.parquet"))):
            date = os.path.basename(p)[len("daily_"):-len(".parquet")]
            self._files[date] = p
        if not self._files:
            raise FileNotFoundError(f"no daily_*.parquet under {cache_dir}")
        self.has_adj = False

    @property
    def first_available(self) -> str:
        return min(self._files)

    @property
    def dates(self) -> list:
        return sorted(self._files)

    def read(self, start: str, end: str):
        parts = [pd.read_parquet(p) for d, p in sorted(self._files.items()) if start <= d <= end]
        daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
            columns=["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"])
        return daily, None


class ClippedSource:
    """毒样本包装: 把底层源的可用起点裁到 clip_start 之后(模拟'面板起点晚于 ext_start')。
    验收2 欠窗必炸的构造工具。"""

    def __init__(self, inner, clip_start: str):
        self._inner = inner
        self._clip = clip_start
        self.has_adj = inner.has_adj

    @property
    def first_available(self) -> str:
        return max(self._inner.first_available, self._clip)

    def read(self, start: str, end: str):
        daily, adj = self._inner.read(max(start, self._clip), end)
        return daily, adj


class DropRowsSource:
    """合成停牌包装: 删除指定 (ts_code, trade_date) 行 —— 模拟停牌日无行情行。
    [合成样本, 报告须注明] 验收4 的受控对照构造工具(真实停牌样本另测)。"""

    def __init__(self, inner, code: str, dates: set):
        self._inner = inner
        self._code = code
        self._dates = set(dates)
        self.has_adj = inner.has_adj

    @property
    def first_available(self) -> str:
        return self._inner.first_available

    def read(self, start: str, end: str):
        daily, adj = self._inner.read(start, end)
        mask = (daily["ts_code"] == self._code) & (daily["trade_date"].isin(self._dates))
        return daily[~mask].copy(), adj
