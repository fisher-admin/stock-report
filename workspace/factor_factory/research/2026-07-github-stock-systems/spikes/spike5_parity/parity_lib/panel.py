"""Panel: 日历对齐的宽表容器. 停牌日=NaN 行, 结构上禁 ffill(qlib.md §5.1 停牌契约)."""
from __future__ import annotations

import pandas as pd

from .errors import MissingFieldError

# 长表 -> 宽表的字段
PRICE_FIELDS = ("open", "high", "low", "close", "vol", "amount")


class Panel:
    """fields: {name: DataFrame[sessions x codes]}; req_start/req_end 为调用方查询窗,
    ext_start 为扩窗后的实际装载起点。因子计算在全扩窗上进行, 输出由 run_factor 截回。
    """

    def __init__(self, fields: dict, sessions: list, ext_start: str, req_start: str, req_end: str):
        self._fields = fields
        self.sessions = list(sessions)
        self.ext_start = ext_start
        self.req_start = req_start
        self.req_end = req_end

    def field(self, name: str) -> pd.DataFrame:
        if name not in self._fields:
            raise MissingFieldError(
                f"面板缺少字段 {name!r}(缺列必须 hard fail, 反 qlib extra_quote fail-open)"
            )
        return self._fields[name]

    @property
    def field_names(self):
        return sorted(self._fields)

    @property
    def first_date(self) -> str:
        return self.sessions[0]


def build_panel(daily_long: pd.DataFrame, sessions: list, ext_start: str,
                req_start: str, req_end: str, adj_long: pd.DataFrame | None = None) -> Panel:
    """长表 -> 日历对齐宽表。

    停牌契约: 缺行的 (code, session) 位置保持 NaN —— 本函数无任何 ffill/bfill/fillna 调用,
    这是结构保证而非纪律(验收4 的实现依据)。
    """
    fields = {}
    codes = sorted(daily_long["ts_code"].unique())
    for f in PRICE_FIELDS:
        if f in daily_long.columns:
            wide = daily_long.pivot(index="trade_date", columns="ts_code", values=f)
            wide = wide.reindex(index=sessions, columns=codes)  # 停牌行=NaN, 禁 ffill
            wide.index.name = "trade_date"
            fields[f] = wide
    if adj_long is not None and len(adj_long):
        aw = adj_long.pivot(index="trade_date", columns="ts_code", values="adj_factor")
        aw = aw.reindex(index=sessions, columns=codes)  # 同样不填充
        aw.index.name = "trade_date"
        fields["adj_factor"] = aw
    return Panel(fields, sessions, ext_start, req_start, req_end)
