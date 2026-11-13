"""ProtectionEngine (spike4, 执行层): 按配置顺序评估规则 -> LockStore 落锁 (O1/O2/O3 在 store 内) -> 事件.

规格: deep/freqtrade_protections.md §3.3. 影子模式下事件写入内存 sink (不发 Discord, 不建 denials 表).
G2 能力旗标硬边界: verdict.scope 必须被规则 scope_flags 允许, 违规即 LockConfigError (fail-fast).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from calendar_util import TradingCalendar
from lock_store import LockConfigError, LockStore
from protection_rules import (EVENT_PROTECTION_TRIGGER, LedgerView,
                              ProtectionEvent, RuleResult)

_SCOPE_TO_FLAG = {"global": "global", "strategy": "per_strategy", "symbol": "per_symbol"}


@dataclass
class EngineRunRecord:
    as_of_day: str
    new_locks: list = field(default_factory=list)      # 本轮真正落库的锁
    absorbed: list = field(default_factory=list)       # 被 O1/O2/O3 吸收的 verdict
    events: list = field(default_factory=list)         # ProtectionEvent 列表


class ProtectionEngine:
    def __init__(self, rules: list, lock_store: LockStore, calendar: TradingCalendar):
        if not rules:
            raise LockConfigError("ProtectionEngine requires >=1 rule")
        names = [r.name for r in rules]
        if len(set(names)) != len(names):
            raise LockConfigError(f"duplicate rule names: {names}")
        self._rules = list(rules)      # 配置顺序即评估顺序
        self._store = lock_store       # 构造注入 (无全局态)
        self._cal = calendar

    def run(self, as_of_day: str, ledger: LedgerView, now: str) -> EngineRunRecord:
        rec = EngineRunRecord(as_of_day=as_of_day)
        for rule in self._rules:
            result: RuleResult = rule.evaluate(as_of_day, ledger, self._cal)
            rec.events.extend(result.events)
            for v in result.verdicts:
                flag = _SCOPE_TO_FLAG.get(v.scope)
                if flag is None or not rule.scope_flags.get(flag, False):
                    raise LockConfigError(
                        f"rule {rule.name} emitted scope={v.scope} outside its scope_flags (G2)")
                before_ids = {lk.id for lk in self._store.active_locks("00000101")}
                lk = self._store.lock(scope=v.scope, scope_key=v.scope_key, side=v.side,
                                      reason_code=v.reason_code, detail=v.detail,
                                      unlock_spec=v.unlock_spec, now=now,
                                      created_by=f"protection:{rule.name}")
                if lk.id in before_ids:
                    rec.absorbed.append((rule.name, v))
                else:
                    rec.new_locks.append(lk)
                    # 触发即发类型化消息 (E1+告警同源; 影子模式写 sink)
                    rec.events.append(ProtectionEvent(
                        EVENT_PROTECTION_TRIGGER, rule.name, as_of_day,
                        (("scope", v.scope), ("key", v.scope_key),
                         ("reason", v.reason_code), ("end", lk.lock_end_date),
                         ("end_phase", lk.unlock_at_phase))))
        return rec
