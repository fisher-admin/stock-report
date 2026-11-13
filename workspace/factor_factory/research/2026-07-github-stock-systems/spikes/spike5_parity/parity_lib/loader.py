"""load_panel_for: 统一装载器 —— 扩窗查询再截断 + 欠窗硬失败(qlib.md §2.2 形状).

剥夺各脚本决定拉多少历史的权力: 因子只能经注册表进装载器; 扩窗量=声明元数据 max;
数据不足 raise LookbackUnderflow(qlib 是 max(0,...)+min_periods=1 静默降级 —— 反向执行)。
"""
from __future__ import annotations

from .calendar import TradingCalendar
from .errors import LookbackUnderflow, PanelHoleError
from .panel import Panel, build_panel
from .registry import get_spec


def load_panel_for(factors: list, start: str, end: str, *,
                   source, calendar: TradingCalendar,
                   require_full_sessions: bool = False) -> Panel:
    specs = [get_spec(f) for f in factors]  # 未注册 => UnregisteredFactorError(验收7)
    lb = max(s.required_lookback for s in specs)
    fwd = max(s.required_forward for s in specs)

    # 交易日历上前移, 非自然日; 日历越界本身即 LookbackUnderflow
    ext_start = calendar.shift(start, -lb)
    ext_end = calendar.shift(end, +fwd) if fwd else end

    daily, adj = source.read(ext_start, ext_end)
    if len(daily) == 0:
        raise LookbackUnderflow(f"need data from {ext_start}, source returned empty")
    first_date = str(daily["trade_date"].min())
    if first_date > ext_start:
        # 守卫: 窗口不足即 hard fail —— #52 事故的结构性堵点
        raise LookbackUnderflow(f"need {ext_start}, panel starts {first_date}")

    sessions = calendar.range(ext_start, ext_end)
    if require_full_sessions:
        # 全市场面板契约: 每个开市日必须至少有一行(整日缺行=数据洞, 非个股停牌)。
        # 个股/小样本面板不适用(停牌可合法整日无行), 故为显式开关而非默认。
        present = set(daily["trade_date"].astype(str).unique())
        holes = [s for s in sessions if s not in present]
        if holes:
            raise PanelHoleError(f"面板覆盖区间内 {len(holes)} 个开市日整日无行: {holes[:10]}...")
    return build_panel(daily, sessions, ext_start, start, end, adj_long=adj)


def run_factor(name: str, panel: Panel):
    """计算因子并把输出截回查询窗 [req_start, req_end](扩窗对调用方不可见)。"""
    spec = get_spec(name)
    out = spec.compute(panel)
    return out.loc[panel.req_start:panel.req_end]
