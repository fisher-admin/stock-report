"""因子注册表: required_lookback/required_forward/lookback_basis/ties/price_mode 声明式元数据.

对应 qlib.md §2.2 注册表字段 + lean.md §5.2 口径强制声明。注册即门:
元数据不全的因子 register() 直接拒绝; 未注册因子 get_spec()/gauntlet_admit() 拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import RegistryError, UnregisteredFactorError

VALID_TIES = ("average", "min", "max", "first", "dense")
VALID_PRICE_MODES = ("raw", "hfq")  # qfq_asof 因子须另携 asof 策略, 本 spike 明确拒绝


@dataclass(frozen=True)
class FactorSpec:
    name: str
    # 计算当日值所需最少交易日根数(不含当日); expanding/EWM 类填保守截断值并在 basis 注明
    required_lookback: Optional[int]
    # 后验依赖天数(label/复盘用, 对应 qlib rght_etd); 信号因子恒 0
    required_forward: Optional[int]
    # 推导依据一句话, 供审计
    lookback_basis: str
    # compute(panel: Panel) -> DataFrame [sessions x codes], 只准依赖 [t-required_lookback, t] 根
    compute: Callable
    # 横截面 rank 因子必须声明并列值语义
    ties: Optional[str] = None
    # 复权口径, 必须显式声明(学 Lean DataNormalizationMode 无默认)
    price_mode: str = "raw"
    is_cross_sectional: bool = False
    # 所需面板字段
    fields: tuple = ("close",)


_REGISTRY: dict[str, FactorSpec] = {}


def validate_spec(spec: FactorSpec) -> None:
    if not isinstance(spec.required_lookback, int) or spec.required_lookback < 0:
        raise RegistryError(
            f"{spec.name}: required_lookback 必须为非负 int(实际={spec.required_lookback!r})"
            " —— 注册即门, 无 required_lookback 禁止进闯关机"
        )
    if not isinstance(spec.required_forward, int) or spec.required_forward < 0:
        raise RegistryError(f"{spec.name}: required_forward 必须为非负 int")
    if not spec.lookback_basis or not spec.lookback_basis.strip():
        raise RegistryError(f"{spec.name}: lookback_basis 必填(审计依据)")
    if spec.is_cross_sectional and spec.ties not in VALID_TIES:
        raise RegistryError(
            f"{spec.name}: 横截面 rank 因子必须声明 ties ∈ {VALID_TIES}(实际={spec.ties!r})"
        )
    if spec.price_mode not in VALID_PRICE_MODES:
        raise RegistryError(
            f"{spec.name}: price_mode 必须显式 ∈ {VALID_PRICE_MODES}; "
            "qfq 类因子必须携带 asof 策略, 本注册表拒收"
        )
    if not callable(spec.compute):
        raise RegistryError(f"{spec.name}: compute 必须可调用")


def register(spec: FactorSpec) -> FactorSpec:
    validate_spec(spec)
    if spec.name in _REGISTRY:
        raise RegistryError(f"{spec.name}: 重复注册")
    _REGISTRY[spec.name] = spec
    return spec


def get_spec(name: str) -> FactorSpec:
    if name not in _REGISTRY:
        raise UnregisteredFactorError(
            f"因子 {name!r} 未注册 required_lookback 元数据, 禁止装载/进闯关机"
        )
    return _REGISTRY[name]


def gauntlet_admit(name: str) -> FactorSpec:
    """闯关机入场闸: 未注册或元数据不全一律拒绝(验收7)。"""
    spec = get_spec(name)
    validate_spec(spec)  # 防注册后被绕过/篡改的双保险
    return spec


def registered_names() -> list[str]:
    return sorted(_REGISTRY)
