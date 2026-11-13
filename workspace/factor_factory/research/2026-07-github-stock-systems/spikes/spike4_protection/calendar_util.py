"""TradingCalendar: 交易日历工具 (spike4, 隔离实现).

数据源: 注入的已排序交易日列表 (YYYYMMDD 字符串).
- 影子回放: 从 recommendations.db price_daily_cache 的 distinct trade_date 派生 (只读).
- 单测: 直接注入合成日列表.

无全局态, 构造注入 (修 freqtrade 类属性单例反模式的同族纪律).
"""
from __future__ import annotations

import bisect


class CalendarError(ValueError):
    pass


class TradingCalendar:
    def __init__(self, trading_days: list[str]):
        if not trading_days:
            raise CalendarError("empty trading calendar")
        days = sorted(set(trading_days))
        for d in days:
            if len(d) != 8 or not d.isdigit():
                raise CalendarError(f"bad day format: {d!r}")
        self._days = days
        self._set = set(days)

    def is_trading_day(self, day: str) -> bool:
        return day in self._set

    def next_trading_day(self, day: str) -> str:
        """严格大于 day 的第一个交易日."""
        i = bisect.bisect_right(self._days, day)
        if i >= len(self._days):
            raise CalendarError(f"calendar exhausted after {day}")
        return self._days[i]

    def roll_forward(self, day: str) -> str:
        """day 若为交易日返回自身, 否则顺延至下一交易日 (S2/S5 日历版)."""
        if day in self._set:
            return day
        return self.next_trading_day(day)

    def add_trading_days(self, anchor: str, n: int) -> str:
        """自 anchor 起加 n 个交易日. anchor 非交易日先顺延.

        n 必须 >=1 (锁至少跨到下一交易日); n<=0 抛错 (fail-fast).
        """
        if n < 1:
            raise CalendarError(f"add_trading_days requires n>=1, got {n}")
        cur = anchor
        if cur not in self._set:
            # 锚在非交易日: 顺延后的首个交易日计为第 1 天
            cur = self.next_trading_day(cur)
            n -= 1
        for _ in range(n):
            cur = self.next_trading_day(cur)
        return cur

    def window_days(self, as_of: str, lookback: int) -> list[str]:
        """lookback 窗左开右闭 (S3): (as_of - lookback 个交易日, as_of].

        返回窗内交易日列表 (含 as_of, 不含左边界日).
        """
        if lookback < 1:
            raise CalendarError(f"lookback must be >=1, got {lookback}")
        i = bisect.bisect_right(self._days, as_of)
        if i == 0:
            return []
        lo = max(0, i - lookback)
        return self._days[lo:i]

    def days_between(self, start: str, end: str) -> list[str]:
        """[start, end] 闭区间内交易日."""
        i = bisect.bisect_left(self._days, start)
        j = bisect.bisect_right(self._days, end)
        return self._days[i:j]

    @property
    def all_days(self) -> list[str]:
        return list(self._days)
