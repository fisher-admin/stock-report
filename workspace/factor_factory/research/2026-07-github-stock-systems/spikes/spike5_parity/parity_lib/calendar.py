"""交易日历: 前移/后移按交易日, 越界即 LookbackUnderflow(不许静默截到 0)."""
from __future__ import annotations

import bisect

from .errors import LookbackUnderflow


class TradingCalendar:
    """sessions: 升序 yyyymmdd 字符串列表(仅开市日)。"""

    def __init__(self, sessions: list[str]):
        if not sessions or list(sessions) != sorted(set(sessions)):
            raise ValueError("sessions 必须为升序去重的 yyyymmdd 列表")
        self.sessions = list(sessions)
        self._idx = {d: i for i, d in enumerate(self.sessions)}

    @classmethod
    def from_trade_cal(cls, cal_df) -> "TradingCalendar":
        s = cal_df[cal_df["is_open"] == 1]["cal_date"].astype(str).sort_values()
        return cls(s.tolist())

    def index_of(self, date: str) -> int:
        if date not in self._idx:
            raise KeyError(f"{date} 不是交易日(或不在日历覆盖范围内)")
        return self._idx[date]

    def shift(self, date: str, n: int) -> str:
        """按交易日前移(n<0)/后移(n>0)。越过日历边界 => LookbackUnderflow。

        对照 qlib data.py#L851-854 的 max(0, start_index - lft_etd):
        qlib 截到 0 后静默继续, 我方越界直接炸。
        """
        j = self.index_of(date) + n
        if j < 0 or j >= len(self.sessions):
            raise LookbackUnderflow(
                f"calendar exhausted: {date} shift {n} 越过日历边界 "
                f"[{self.sessions[0]}, {self.sessions[-1]}]"
            )
        return self.sessions[j]

    def range(self, start: str, end: str) -> list[str]:
        i = bisect.bisect_left(self.sessions, start)
        j = bisect.bisect_right(self.sessions, end)
        return self.sessions[i:j]
