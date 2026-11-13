"""5 因子实现, 覆盖五类边界(PHASE2_TIERA_REPORT §Spike-5 因子选择):

  ① ma60_diff     长窗滚动类(MA60+diff)          -> warm-up
  ② ewm_ret20     EWM 类(保守截断+硬窗口化)       -> warm-up 边界形态二
  ③ pv_corr20     含缺失值处理的量价因子           -> 缺失(NaN 严格传播)
  ④ cs_rank_ret5  横截面 rank 因子                -> ties 声明+锁定
  ⑤ mom20_hfq     复权敏感动量因子(hfq 口径)       -> 复权

契约: 每个 compute 的第 t 日输出只依赖 [t-required_lookback, t] 根(窗口决定论),
由验收3 的 lookback+1 扰动审计机械验证。停牌 NaN 对全部 5 因子统一压测(验收4)。
"""
from __future__ import annotations

import math

import numpy as np

from .panel import Panel
from .prices import load_prices
from .registry import FactorSpec, register
from .tswin import roll_corr, roll_mean, roll_weighted_sum

# ---------------------------------------------------------------- ① MA60+diff
MA_W = 60


def f_ma60_diff(panel: Panel):
    c = load_prices(panel, mode="raw")
    ma = roll_mean(c, MA_W)
    return ma - ma.shift(1)


register(FactorSpec(
    name="ma60_diff",
    required_lookback=60,
    required_forward=0,
    lookback_basis="MA60 回看59根 + diff1 需 MA(t-1) 再回看1根 → 60 (min_periods=窗宽, 欠窗=NaN 而非短窗值)",
    compute=f_ma60_diff,
))

# ---------------------------------------------------------------- ② EWM 截断
EWM_SPAN = 20
EWM_ALPHA = 2.0 / (EWM_SPAN + 1)
# qlib ops.py#L760-761 截断规则: int(log(1e-6)/log(1-alpha)) = 138
EWM_K = int(math.log(1e-6) / math.log(1.0 - EWM_ALPHA))
assert EWM_K == 138
_EWM_W = (1.0 - EWM_ALPHA) ** np.arange(EWM_K - 1, -1, -1)  # oldest -> newest
_EWM_W = _EWM_W / _EWM_W.sum()


def f_ewm_ret20(panel: Panel):
    c = load_prices(panel, mode="raw")
    ret = c / c.shift(1) - 1.0
    return roll_weighted_sum(ret, _EWM_W)


register(FactorSpec(
    name="ewm_ret20",
    required_lookback=138,
    required_forward=0,
    lookback_basis=("EWM(span=20, α=2/21) 无限记忆按 qlib 规则保守截断 K=int(ln 1e-6/ln(1-α))=138 根收益, "
                    "并硬窗口化(只用最近138根, 保证窗口决定论/parity); 收益首根需再回看1根收盘 → 138"),
    compute=f_ewm_ret20,
))

# ---------------------------------------------------------------- ③ 量价相关
PV_W = 20


def f_pv_corr20(panel: Panel):
    c = load_prices(panel, mode="raw")
    v = panel.field("vol")
    ret = c / c.shift(1) - 1.0
    dv = v / v.shift(1) - 1.0
    return roll_corr(ret, dv, PV_W)


register(FactorSpec(
    name="pv_corr20",
    required_lookback=20,
    required_forward=0,
    lookback_basis="corr 窗20(回看19) + 收益/量变化各需1根前值 → 20; 窗口含 NaN(停牌)即 NaN, 不填充不缩窗",
    compute=f_pv_corr20,
    fields=("close", "vol"),
))

# ---------------------------------------------------------------- ④ 截面 rank
CS_RANK_TIES = "average"  # 注册声明并由验收6 金值单测锁定
RET_W = 5


def f_cs_rank_ret5(panel: Panel):
    c = load_prices(panel, mode="raw")
    ret5 = c / c.shift(RET_W) - 1.0
    # 逐日截面, 无跨日状态(qlib.md §3.1: 无状态截面算子天然与查询窗无关)
    return ret5.rank(axis=1, method=CS_RANK_TIES, pct=True)


register(FactorSpec(
    name="cs_rank_ret5",
    required_lookback=5,
    required_forward=0,
    lookback_basis="ret5 回看5根; 截面 rank 逐日无状态, 不追加窗口",
    compute=f_cs_rank_ret5,
    ties=CS_RANK_TIES,
    is_cross_sectional=True,
))

# ---------------------------------------------------------------- ⑤ hfq 动量
MOM_W = 20


def f_mom20_hfq(panel: Panel):
    hfq = load_prices(panel, mode="hfq")  # 口径显式声明; qfq 无 asof 会被口径 API 拒绝
    return hfq / hfq.shift(MOM_W) - 1.0


register(FactorSpec(
    name="mom20_hfq",
    required_lookback=20,
    required_forward=0,
    lookback_basis="动量20 回看20根; 复权敏感因子, price_mode=hfq 显式声明(raw 会在除权日产生假动量)",
    compute=f_mom20_hfq,
    price_mode="hfq",
    fields=("close", "adj_factor"),
))

ALL_FACTORS = ["ma60_diff", "ewm_ret20", "pv_corr20", "cs_rank_ret5", "mom20_hfq"]
