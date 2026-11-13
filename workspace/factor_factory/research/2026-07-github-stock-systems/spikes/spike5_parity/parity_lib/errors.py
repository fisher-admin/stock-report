"""Spike-5 错误类型: 全部 hard-fail, 反 qlib min_periods=1 静默降级."""


class LookbackUnderflow(Exception):
    """面板历史不足以覆盖 ext_start(= start 前移 required_lookback 交易日)。

    qlib 的反面: qlib 用 max(0, start-etd)+min_periods=1, 数据不够时静默给短窗值
    (qlib/data/data.py#L854 + ops.py#L748) —— 这正是 #52 事故形态。我方必须 raise。
    """


class UnregisteredFactorError(Exception):
    """因子未注册(或注册元数据不全), 禁止进入装载器/闯关机。注册即门。"""


class RegistryError(Exception):
    """注册表元数据不合法: required_lookback 缺失、横截面因子未声明 ties 等。"""


class AdjustmentModeError(Exception):
    """复权口径违规: mode 非法 / qfq 不带 asof / asof 不在面板日历内。

    学 Lean DataNormalizationMode: 口径必须显式声明, 无默认(lean.md §5.2)。
    """


class LookbackUnderReported(Exception):
    """低报审计失败: lookback+1 与 lookback 两种扩窗下因子值不一致
    => required_lookback 声明低于真实需求, CI 必须拒绝。"""


class PanelHoleError(Exception):
    """面板日历完整性破坏: 覆盖区间内存在整日无任何行的开市日(数据洞)。

    spike 实测发现生产 backtest_cache 日线在覆盖区间内有 31 个开市日缺文件 ——
    对滚动窗因子是 #52 同型风险(整日 NaN 会静默传播为窗口 NaN)。全市场面板
    建议开启 require_full_sessions 把洞变成硬失败。"""


class MissingFieldError(Exception):
    """面板缺少因子所需字段(如 adj_factor)。缺列必须 hard fail,
    反 qlib extra_quote 缺列默认 False 仅 warning 的 fail-open(qlib.md §5.1)。"""
