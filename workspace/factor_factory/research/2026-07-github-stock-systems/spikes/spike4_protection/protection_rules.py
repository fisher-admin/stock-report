"""IProtectionRule + 首批 4 保护器 (spike4, 检测层只读台账不碰锁).

规格: deep/freqtrade_protections.md §3.3 接口形状 + §3.4 首批保护器 + §5 单测清单.
† 标注 = 对 freqtrade 语义的显式反转 (单测清单要求带对照注释):
  - †T1/T2 反转: 触发阈值一律含等号 (≥/≤). freqtrade MaxDrawdown 为严格 >
    (max_drawdown_protection.py#L96@2bd60670f4, 回撤恰等于阈值不触发),
    LowProfitPairs 为严格 < —— 我方护栏保守默认取含等.
  - †falsy-zero 修复: freqtrade stoploss_guard.py#L64-65 / low_profit_pairs.py#L63-65
    用 truthiness 过滤 close_profit, 0.0 被静默排除. 我方一律显式 `is not None`,
    0.0 参与计数/求和; None(缺失) 排除且必须产生 INSUFFICIENT_DATA 事件 (修 T3 fail-open).
  - 双口径修复: DrawdownBrake 只有 equity 口径, 不实现 ratios, 不设口径开关 (T4/T5).

台账视图 (LedgerView) 由调用方 (影子重放器/单测) 构造, 保证 as-of 过滤在检测器之外完成.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from calendar_util import TradingCalendar
from lock_store import LockConfigError, UnlockSpec

# --- 事件 (可观测性: INSUFFICIENT_DATA 与 NO_DRAWDOWN 必须可区分, 修 T3) ---
EVENT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
EVENT_NO_DRAWDOWN = "NO_DRAWDOWN"
EVENT_PROTECTION_TRIGGER = "PROTECTION_TRIGGER"


@dataclass(frozen=True)
class ProtectionEvent:
    """类型化事件, Display 对齐 nautilus deny 格式 CATEGORY: k=v (nautilus.md §2.2)."""
    category: str
    rule: str
    as_of_day: str
    kv: tuple = ()   # ((key, value), ...) 有序, 保确定性

    def display(self) -> str:
        kvs = ",".join(f"{k}={v}" for k, v in self.kv)
        return f"{self.category}: rule={self.rule},as_of={self.as_of_day}" + ("," + kvs if kvs else "")


@dataclass(frozen=True)
class ProtectionVerdict:
    scope: str            # strategy / symbol / global
    scope_key: str
    side: str             # entry / exit_blocked / *
    reason_code: str
    detail: str
    unlock_spec: UnlockSpec


@dataclass
class RuleResult:
    verdicts: list = field(default_factory=list)
    events: list = field(default_factory=list)


@dataclass
class LedgerView:
    """检测器输入 (由重放器按 as-of 截断后传入).

    rec_marks: 每条推荐的复盘标记列表, dict:
      {strategy_id, stock_code, recommend_date, eval_day, value}
      - ConsecutiveLossGuard 用: value = 持有窗内最差 mark-to-market 相对收益
        (mtm 口径, 不问是否成交 —— 修 freqtrade "止损成交计数" A股失真, §4.4);
        eval_day = 该标记可知日 (突破止损线之日 / 持有窗结束日).
      - value=None = 数据缺失 (须触发 INSUFFICIENT_DATA, 不静默).
    strategy_daily: 逐策略逐日收益 {strategy_id, day, day_return} (equity 曲线原料).
    rec_excess: LowEdgePause 用: {strategy_id, eval_day, excess} 已实现超额 (None=缺失).
    incidents: PostFailureCooldown 用: {day, scope, scope_key, incident_id, fixed_confirmed}.
    """
    rec_marks: list = field(default_factory=list)
    strategy_daily: list = field(default_factory=list)
    rec_excess: list = field(default_factory=list)
    incidents: list = field(default_factory=list)


class IProtectionRule(Protocol):
    name: str
    scope_flags: dict     # {'global': bool, 'per_strategy': bool, 'per_symbol': bool}

    def evaluate(self, as_of_day: str, ledger: LedgerView, cal: TradingCalendar) -> RuleResult: ...


def _in_window(day: str, window_days: list[str]) -> bool:
    """S3 lookback 窗左开右闭: window_days 即 (as_of-N, as_of] 的交易日集合."""
    return day in window_days


class ConsecutiveLossGuard:
    """对应 freqtrade StoplossGuard (stoploss_guard.py@2bd60670f4), 三处改动:
    ① 计数口径 = mark-to-market 跌破止损线 (A股跌停不成交, 成交计数失真, §4.4);
    ② †阈值含等号: value <= loss_line 计入 (freqtrade close_profit < profit 严格);
    ③ †falsy-zero 修复: 显式 is not None; 0.0 在 loss_line>=0 场景照计
      (freqtrade L64-65 truthiness 把保本 0.0 静默排除).
    """
    name = "ConsecutiveLossGuard"
    scope_flags = {"global": False, "per_strategy": True, "per_symbol": False}

    def __init__(self, lookback_days: int, loss_line: float, trade_limit: int,
                 stop_duration_days: int):
        if lookback_days < 1 or trade_limit < 1 or stop_duration_days < 1:
            raise LockConfigError("ConsecutiveLossGuard: positive int params required")
        self.lookback_days = lookback_days
        self.loss_line = loss_line
        self.trade_limit = trade_limit
        self.stop_duration_days = stop_duration_days

    def evaluate(self, as_of_day: str, ledger: LedgerView, cal: TradingCalendar) -> RuleResult:
        res = RuleResult()
        window = cal.window_days(as_of_day, self.lookback_days)
        by_strategy: dict[str, list] = {}
        for m in ledger.rec_marks:
            if m["eval_day"] > as_of_day:
                continue  # as-of 防前视兜底 (重放器应已截断)
            if not _in_window(m["eval_day"], window):
                continue
            by_strategy.setdefault(m["strategy_id"], []).append(m)
        for sid in sorted(by_strategy):
            marks = by_strategy[sid]
            missing = [m for m in marks if m["value"] is None]
            valued = [m for m in marks if m["value"] is not None]  # †显式 is not None, 非 truthiness
            if missing:
                res.events.append(ProtectionEvent(
                    EVENT_INSUFFICIENT_DATA, self.name, as_of_day,
                    (("strategy", sid), ("missing", len(missing)))))
            # †含等号 <=: 保本 0.0 在 loss_line>=0 场景计入 (freqtrade 严格< 且 truthiness 排除0.0)
            breaches = [m for m in valued if m["value"] <= self.loss_line]
            if len(breaches) >= self.trade_limit:  # †计数达 limit 即触发 (≥)
                anchor = max(m["eval_day"] for m in breaches)  # S4: 锚定触发事件日
                res.verdicts.append(ProtectionVerdict(
                    scope="strategy", scope_key=sid, side="entry",
                    reason_code="CONSECUTIVE_LOSS",
                    detail=f"breaches={len(breaches)},limit={self.trade_limit},"
                           f"loss_line={self.loss_line},window={self.lookback_days}d,anchor={anchor}",
                    unlock_spec=UnlockSpec(type="trading_days", anchor_day=anchor,
                                           after_days=self.stop_duration_days)))
        return res


class StrategyDrawdownBrake:
    """对应 freqtrade MaxDrawdownProtection, 只实现 equity 口径 (T4/T5 双口径反例:
    ratios 默认为既证反模式, 我方不实现、不设开关 —— 口径唯一性).
    †T1 反转: 回撤恰等于阈值 → 触发 (freqtrade max_drawdown_protection.py#L96 严格> 不触发).
    修 T3: 样本不足 → INSUFFICIENT_DATA 事件; 无回撤 → NO_DRAWDOWN 事件, 两者可区分,
    不复现 freqtrade 吞 ValueError 静默放行.
    阈值合法性: ∉(0,1] 启动抛异常 (兑现 freqtrade 未兑现的 TODO, 清单#15).
    """
    name = "StrategyDrawdownBrake"
    scope_flags = {"global": True, "per_strategy": False, "per_symbol": False}

    def __init__(self, lookback_days: int, max_drawdown: float, trade_limit: int):
        if not (0.0 < max_drawdown <= 1.0):
            raise LockConfigError(
                f"StrategyDrawdownBrake: max_drawdown must be in (0,1], got {max_drawdown}")
        if lookback_days < 2 or trade_limit < 1:
            raise LockConfigError("StrategyDrawdownBrake: lookback>=2, trade_limit>=1 required")
        self.lookback_days = lookback_days
        self.max_drawdown = max_drawdown
        self.trade_limit = trade_limit
        # 口径唯一性: 不存在 ratios 开关 (单测#13 断言 hasattr(rule,'drawdown_calculation')==False)

    @staticmethod
    def equity_drawdown(day_returns: list[float]) -> float:
        """equity 口径: 权益曲线 cumprod(1+r), 相对峰值最大回撤. 纯函数, 单测#13 手工对照点."""
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in day_returns:
            equity *= (1.0 + r)
            if equity > peak:
                peak = equity
            dd = 1.0 - equity / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def evaluate(self, as_of_day: str, ledger: LedgerView, cal: TradingCalendar) -> RuleResult:
        res = RuleResult()
        window = set(cal.window_days(as_of_day, self.lookback_days))
        rows = [r for r in ledger.strategy_daily
                if r["day"] in window and r["day"] <= as_of_day]
        # 组合口径: 同日多策略取均值 (equal weight)
        by_day: dict[str, list[float]] = {}
        n_missing = 0
        for r in rows:
            if r["day_return"] is None:   # †显式 None 判别
                n_missing += 1
                continue
            by_day.setdefault(r["day"], []).append(r["day_return"])
        if n_missing:
            res.events.append(ProtectionEvent(
                EVENT_INSUFFICIENT_DATA, self.name, as_of_day, (("missing_days", n_missing),)))
        days = sorted(by_day)
        if len(days) < self.trade_limit:
            res.events.append(ProtectionEvent(
                EVENT_INSUFFICIENT_DATA, self.name, as_of_day,
                (("samples", len(days)), ("required", self.trade_limit))))
            return res  # 数据不足 ≠ 未触发: 事件已留痕, 不静默 (修 T3)
        returns = [sum(by_day[d]) / len(by_day[d]) for d in days]
        dd = self.equity_drawdown(returns)
        if dd == 0.0:
            res.events.append(ProtectionEvent(
                EVENT_NO_DRAWDOWN, self.name, as_of_day, (("samples", len(days)),)))
            return res  # NO_DRAWDOWN 与 INSUFFICIENT_DATA 分立可观测 (清单#16)
        if dd >= self.max_drawdown:  # †≥ 含等 (freqtrade 严格>, T1 反转, 清单#14)
            res.verdicts.append(ProtectionVerdict(
                scope="global", scope_key="*", side="entry",
                reason_code="DRAWDOWN_BRAKE",
                detail=f"equity_dd={dd:.6f},threshold={self.max_drawdown},window={self.lookback_days}d",
                unlock_spec=UnlockSpec(type="at_phase", anchor_day=as_of_day,
                                       at_phase="after_review")))
        return res


class LowEdgePause:
    """对应 freqtrade LowProfitPairs, 域改为 strategy (滚动 M 日超额和).
    †T2 反转: 超额和 <= 阈值触发 (freqtrade 严格<).
    †falsy-zero 修复 (清单#12): 0.0 参与求和; None 排除且产生 INSUFFICIENT_DATA 事件,
    两者不混用一个 truthiness (freqtrade low_profit_pairs.py#L63-65 反例).
    freqtrade 暗坑修复: 计数门槛与求和同口径 (§1 判决表 low_profit_pairs 行:
    上游 trade_limit 计数在 side 过滤之前、求和在之后 —— 我方计数=求和样本数).
    """
    name = "LowEdgePause"
    scope_flags = {"global": False, "per_strategy": True, "per_symbol": False}

    def __init__(self, lookback_days: int, required_excess: float, trade_limit: int,
                 stop_duration_days: int):
        if lookback_days < 1 or trade_limit < 1 or stop_duration_days < 1:
            raise LockConfigError("LowEdgePause: positive int params required")
        self.lookback_days = lookback_days
        self.required_excess = required_excess
        self.trade_limit = trade_limit
        self.stop_duration_days = stop_duration_days

    def evaluate(self, as_of_day: str, ledger: LedgerView, cal: TradingCalendar) -> RuleResult:
        res = RuleResult()
        window = cal.window_days(as_of_day, self.lookback_days)
        by_strategy: dict[str, list] = {}
        for r in ledger.rec_excess:
            if r["eval_day"] > as_of_day or not _in_window(r["eval_day"], window):
                continue
            by_strategy.setdefault(r["strategy_id"], []).append(r)
        for sid in sorted(by_strategy):
            rows = by_strategy[sid]
            missing = [r for r in rows if r["excess"] is None]      # 显式 None 判别
            valued = [r["excess"] for r in rows if r["excess"] is not None]
            if missing:
                res.events.append(ProtectionEvent(
                    EVENT_INSUFFICIENT_DATA, self.name, as_of_day,
                    (("strategy", sid), ("missing", len(missing)))))
            if len(valued) < self.trade_limit:   # 计数与求和同口径 (修上游暗坑)
                res.events.append(ProtectionEvent(
                    EVENT_INSUFFICIENT_DATA, self.name, as_of_day,
                    (("strategy", sid), ("samples", len(valued)), ("required", self.trade_limit))))
                continue
            total = sum(valued)   # 0.0 参与求和
            if total <= self.required_excess:   # †<= 含等 (freqtrade 严格<)
                res.verdicts.append(ProtectionVerdict(
                    scope="strategy", scope_key=sid, side="entry",
                    reason_code="LOW_EDGE",
                    detail=f"excess_sum={total:.6f},threshold={self.required_excess},"
                           f"samples={len(valued)},window={self.lookback_days}d",
                    unlock_spec=UnlockSpec(type="trading_days", anchor_day=as_of_day,
                                           after_days=self.stop_duration_days)))
        return res


class PostFailureCooldown:
    """对应 freqtrade CooldownPeriod 的形 (锚定事件时刻算锁 end, 不看盈亏),
    语义改为: 管线级事故 (0710 类) 修复确认后的短冷却.
    事故台账由调用方注入 (incidents), 检测器只读.
    """
    name = "PostFailureCooldown"
    scope_flags = {"global": True, "per_strategy": True, "per_symbol": False}

    def __init__(self, cooldown_days: int):
        if cooldown_days < 1:
            raise LockConfigError("PostFailureCooldown: cooldown_days>=1 required")
        self.cooldown_days = cooldown_days

    def evaluate(self, as_of_day: str, ledger: LedgerView, cal: TradingCalendar) -> RuleResult:
        res = RuleResult()
        for inc in sorted(ledger.incidents, key=lambda x: (x["day"], x["scope"], x["scope_key"])):
            if inc["day"] > as_of_day:
                continue
            if not inc.get("fixed_confirmed", False):
                continue  # 修复未确认: 不进入冷却计时 (事故处置流程外部把关)
            # 锁 end 锚定事故日 (S4); 过期即过期, 重评估不产生仍活跃新锁
            scope = inc["scope"]
            res.verdicts.append(ProtectionVerdict(
                scope=scope, scope_key=inc["scope_key"],
                side="entry", reason_code="POST_FAILURE_COOLDOWN",
                detail=f"incident={inc['incident_id']},day={inc['day']}",
                unlock_spec=UnlockSpec(type="trading_days", anchor_day=inc["day"],
                                       after_days=self.cooldown_days)))
        return res
