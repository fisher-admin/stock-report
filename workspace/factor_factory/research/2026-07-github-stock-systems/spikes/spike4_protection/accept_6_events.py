"""验收6: INSUFFICIENT_DATA 与 NO_DRAWDOWN 分别可观测 (修 freqtrade T3 fail-open).

freqtrade 对照: max_drawdown_protection.py#L93-94@2bd60670f4 无回撤时 ValueError 被吞为 None,
"数据不足"与"未触发"不可区分. 我方两类事件必须经引擎事件流可观测且类别互斥.

两个合成场景 + 真实重放事件流统计:
  场景A: 样本数 < trade_limit           -> 必须出 INSUFFICIENT_DATA, 不出 NO_DRAWDOWN
  场景B: 样本充足且全正收益 (无回撤)     -> 必须出 NO_DRAWDOWN, 不出 INSUFFICIENT_DATA
  真实台账: 重放事件流按类别计数, 两类事件计数可从 CSV 直接审计.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from calendar_util import TradingCalendar
from lock_store import MemLockStore
from protection_engine import ProtectionEngine
from protection_rules import (EVENT_INSUFFICIENT_DATA, EVENT_NO_DRAWDOWN,
                              LedgerView, StrategyDrawdownBrake)

DAYS = ["20260302", "20260303", "20260304", "20260305", "20260306", "20260309", "20260310"]


def run_case(strategy_daily):
    cal = TradingCalendar(DAYS)
    engine = ProtectionEngine(
        [StrategyDrawdownBrake(lookback_days=5, max_drawdown=0.1, trade_limit=3)],
        MemLockStore(cal), cal)
    rec = engine.run("20260305", LedgerView(strategy_daily=strategy_daily),
                     now="20260305T20:45:00+08:00")
    return [e.category for e in rec.events], [e.display() for e in rec.events]


def main():
    ok = True
    # 场景A: 样本 1 < limit 3
    cats_a, disp_a = run_case([{"strategy_id": "s", "day": "20260305", "day_return": 0.01}])
    print("场景A (样本不足):", disp_a)
    ok &= EVENT_INSUFFICIENT_DATA in cats_a and EVENT_NO_DRAWDOWN not in cats_a
    # 场景B: 样本 4 >= 3, 全正 -> 无回撤
    cats_b, disp_b = run_case([
        {"strategy_id": "s", "day": d, "day_return": 0.01}
        for d in ("20260302", "20260303", "20260304", "20260305")])
    print("场景B (无回撤):", disp_b)
    ok &= EVENT_NO_DRAWDOWN in cats_b and EVENT_INSUFFICIENT_DATA not in cats_b

    # 真实重放事件流审计
    ev_csv = os.path.join(HERE, "out", "run1", "protection_events.csv")
    if os.path.exists(ev_csv):
        counts = {}
        with open(ev_csv) as f:
            next(f)
            for line in f:
                cat = line.split(",", 1)[1].split(":")[0]
                counts[cat] = counts.get(cat, 0) + 1
        print(f"真实重放事件流类别计数: {dict(sorted(counts.items()))}")
        ok &= counts.get(EVENT_INSUFFICIENT_DATA, 0) > 0   # 台账早期窗口不足, 必然留痕
        # NO_DRAWDOWN 在真实亏损台账上不出现属预期 (回撤恒>0), 可观测性由场景B证明
    print("ACCEPT-6:", "PASS (两类事件分立可观测)" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
