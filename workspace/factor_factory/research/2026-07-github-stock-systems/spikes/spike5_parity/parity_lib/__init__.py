"""Spike-5 表达式 parity 库(additive, 隔离于生产代码).

组件: registry(声明式元数据) + loader(扩窗再截断+欠窗硬失败) + prices(口径 API)
     + tswin(窗口决定论算子) + factors(5 因子) + audit(低报审计)
"""
from .audit import exact_equal, lookback_audit
from .calendar import TradingCalendar
from .errors import (AdjustmentModeError, LookbackUnderflow, LookbackUnderReported,
                     MissingFieldError, PanelHoleError, RegistryError,
                     UnregisteredFactorError)
from .loader import load_panel_for, run_factor
from .panel import Panel, build_panel
from .prices import load_prices
from .registry import FactorSpec, gauntlet_admit, get_spec, register, registered_names
