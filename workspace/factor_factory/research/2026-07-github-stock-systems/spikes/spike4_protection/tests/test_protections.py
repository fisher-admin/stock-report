"""spike4 保护单测 — 覆盖 freqtrade_protections.md §5 单测清单指定条目.

映射 (清单# -> 测试):
  S系:   #1 S1 左闭右开; #2 S2/S5 非交易日顺延; #3 S3 窗左开右闭;
         #4 S4 过期锁重评估不复活; #5 O1 去重; #6 O2+S6 吸收/取最长.
  falsy-zero: #10† 保本0.0计入; #11 None排除+INSUFFICIENT_DATA; #12 LowEdge求和口径.
  双口径: #13 equity手工对照+无ratios开关; #14† 回撤恰等阈值触发; #15 阈值合法性; #16 NO_DRAWDOWN分立.
  另附: #7 G1/O3 global 覆盖 (O3 吸收路径依赖); #21 配置 fail-fast; #24 断言卫生自查.
† = 对 freqtrade 语义的显式反转, 注释给出上游对照行为.

断言卫生 (§2.6): 保护判定断言一律落在返回 bool 的 is_locked 上, 用 assertTrue/assertFalse/
assertEqual, 禁 "对象 != 布尔" 式空转断言.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calendar_util import TradingCalendar
from lock_store import (Lock, LockConfigError, MemLockStore, UnlockSpec,
                        materialize_unlock)
from protection_engine import ProtectionEngine
from protection_rules import (EVENT_INSUFFICIENT_DATA, EVENT_NO_DRAWDOWN,
                              ConsecutiveLossGuard, LedgerView, LowEdgePause,
                              PostFailureCooldown, StrategyDrawdownBrake)

# 合成日历: 20260302(一)~20260320(五), 周末非交易日; 另设 20260316(一) 为假日 (清单#2 用)
DAYS = ["20260302", "20260303", "20260304", "20260305", "20260306",
        "20260309", "20260310", "20260311", "20260312", "20260313",
        # 20260316 假日 (非交易日)
        "20260317", "20260318", "20260319", "20260320",
        "20260323", "20260324", "20260325", "20260326", "20260327"]
NOW = "20260309T20:45:00+08:00"


def mk_store():
    return MemLockStore(TradingCalendar(DAYS))


class TestS1LockBoundary(unittest.TestCase):
    """#1 (S1): 锁到 D 日 next_open: D-1 任意相位 locked; D 日 open 及以后 unlocked."""

    def test_left_closed_right_open(self):
        store = mk_store()
        # 锚 20260309 + 2 交易日 -> lock_end_date=20260311, next_open
        store.lock("strategy", "stratA", "entry", "MANUAL", "t",
                   UnlockSpec(type="trading_days", anchor_day="20260309", after_days=2),
                   NOW, "test")
        # D-1 = 20260310 任意相位: locked
        for phase in ("open", "close", "after_review"):
            self.assertTrue(store.is_locked("stratA", "20260310", phase, "entry"))
        # D = 20260311 open 起: unlocked (左闭右开, 解锁日该相位起即可用)
        for phase in ("open", "close", "after_review"):
            self.assertFalse(store.is_locked("stratA", "20260311", phase, "entry"))

    def test_unlock_at_close_phase(self):
        """end 日 next_close: open 相位仍 locked, close 起 unlocked (相位序判定)."""
        store = mk_store()
        store.lock("strategy", "stratA", "entry", "MANUAL", "t",
                   UnlockSpec(type="trading_days", anchor_day="20260309", after_days=1,
                              at_phase="next_close"),
                   NOW, "test")
        self.assertTrue(store.is_locked("stratA", "20260310", "open", "entry"))
        self.assertFalse(store.is_locked("stratA", "20260310", "close", "entry"))
        self.assertFalse(store.is_locked("stratA", "20260310", "after_review", "entry"))


class TestS2S5CalendarRoll(unittest.TestCase):
    """#2 (S2/S5 日历版): 解锁日落非交易日 -> 顺延至下一可交易日."""

    def test_weekend_roll(self):
        cal = TradingCalendar(DAYS)
        # 锚 20260312(四) + 2 交易日 = 20260313(五)+1 -> 下一交易日跳过周末与 0316 假日 = 20260317
        end, phase = materialize_unlock(
            UnlockSpec(type="trading_days", anchor_day="20260312", after_days=2), cal)
        self.assertEqual(end, "20260317")
        # 对照: 若 0316 是交易日, end 应为 0316 —— 顺延生效
        cal2 = TradingCalendar(DAYS + ["20260316"])
        end2, _ = materialize_unlock(
            UnlockSpec(type="trading_days", anchor_day="20260312", after_days=2), cal2)
        self.assertEqual(end2, "20260316")

    def test_nontrading_anchor_rolls(self):
        cal = TradingCalendar(DAYS)
        # 锚在周六 20260307: 顺延后首个交易日 20260309 计第 1 天
        end, _ = materialize_unlock(
            UnlockSpec(type="trading_days", anchor_day="20260307", after_days=1), cal)
        self.assertEqual(end, "20260309")


class TestS3LookbackWindow(unittest.TestCase):
    """#3 (S3): lookback=N 窗左开右闭: 恰在窗左边界日的复盘不计数, 窗内最后一日计数."""

    def test_window_boundaries(self):
        cal = TradingCalendar(DAYS)
        guard = ConsecutiveLossGuard(lookback_days=3, loss_line=-0.05,
                                     trade_limit=1, stop_duration_days=2)
        # as_of=20260311, lookback=3 -> 窗=(20260306, 20260311] = {0309,0310,0311}
        window = cal.window_days("20260311", 3)
        self.assertEqual(window, ["20260309", "20260310", "20260311"])
        # 左边界日 20260306 的标记: 不计数 -> 不触发
        ledger = LedgerView(rec_marks=[
            {"strategy_id": "s", "stock_code": "000001", "recommend_date": "20260305",
             "eval_day": "20260306", "value": -0.10}])
        res = guard.evaluate("20260311", ledger, cal)
        self.assertEqual(len(res.verdicts), 0)
        # 窗内最后一日 20260311 的标记: 计数 -> 触发
        ledger2 = LedgerView(rec_marks=[
            {"strategy_id": "s", "stock_code": "000001", "recommend_date": "20260310",
             "eval_day": "20260311", "value": -0.10}])
        res2 = guard.evaluate("20260311", ledger2, cal)
        self.assertEqual(len(res2.verdicts), 1)


class TestS4ExpiredLockNoResurrection(unittest.TestCase):
    """#4 (S4): 锁过期后重评估 (台账仍在窗内): end 锚定事件日落在过去, is_locked=False."""

    def test_refire_lands_in_past(self):
        cal = TradingCalendar(DAYS)
        store = MemLockStore(cal)
        guard = ConsecutiveLossGuard(lookback_days=8, loss_line=-0.05,
                                     trade_limit=1, stop_duration_days=1)
        engine = ProtectionEngine([guard], store, cal)
        ledger = LedgerView(rec_marks=[
            {"strategy_id": "s", "stock_code": "000001", "recommend_date": "20260302",
             "eval_day": "20260303", "value": -0.10}])
        # 事件日 20260303 触发, end = 0303+1td = 20260304 next_open
        engine.run("20260303", ledger, NOW)
        self.assertTrue(store.is_locked("s", "20260303", "after_review", "entry"))
        self.assertFalse(store.is_locked("s", "20260304", "open", "entry"))
        # 20260310 重评估: 标记仍在 8 日窗内, 规则 re-fire, 但 end 锚定 0303 落在过去
        rec = engine.run("20260310", ledger, NOW)
        # 不产生"仍活跃"的新锁 (锚定语义, 非评估时刻续期)
        self.assertFalse(store.is_locked("s", "20260310", "open", "entry"))
        self.assertFalse(store.is_locked("s", "20260311", "open", "entry"))


class TestO1Dedup(unittest.TestCase):
    """#5 (O1): 同因同 end 同侧重复触发: 表内仅一条记录."""

    def test_dedup(self):
        store = mk_store()
        spec = UnlockSpec(type="trading_days", anchor_day="20260309", after_days=2)
        a = store.lock("strategy", "s", "entry", "CONSECUTIVE_LOSS", "x", spec, NOW, "t")
        b = store.lock("strategy", "s", "entry", "CONSECUTIVE_LOSS", "x", spec, NOW, "t")
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(store.active_locks("20260310")), 1)


class TestO2S6AbsorbAndLongest(unittest.TestCase):
    """#6 (O2+S6): 先长后短吸收无新记录; 先短后长两条并存, longest_lock 取长."""

    def test_long_then_short_absorbed(self):
        store = mk_store()
        long_lk = store.lock("strategy", "s", "entry", "LOW_EDGE", "long",
                             UnlockSpec(type="trading_days", anchor_day="20260309", after_days=5),
                             NOW, "t")
        short_lk = store.lock("strategy", "s", "entry", "CONSECUTIVE_LOSS", "short",
                              UnlockSpec(type="trading_days", anchor_day="20260309", after_days=2),
                              NOW, "t")
        self.assertEqual(short_lk.id, long_lk.id)          # 吸收: 返回已有锁
        self.assertEqual(len(store.active_locks("20260310")), 1)

    def test_short_then_long_coexist_longest_wins(self):
        store = mk_store()
        short_lk = store.lock("strategy", "s", "entry", "CONSECUTIVE_LOSS", "short",
                              UnlockSpec(type="trading_days", anchor_day="20260309", after_days=2),
                              NOW, "t")
        long_lk = store.lock("strategy", "s", "entry", "LOW_EDGE", "long",
                             UnlockSpec(type="trading_days", anchor_day="20260309", after_days=5),
                             NOW, "t")
        self.assertNotEqual(short_lk.id, long_lk.id)       # 两条并存 (锁只增不减)
        self.assertEqual(len(store.active_locks("20260310")), 2)
        longest = store.longest_lock("s", "20260310")
        self.assertEqual(longest.id, long_lk.id)
        # 短锁到期后仍锁 (取最长): 短锁 end=20260311, 长锁 end=20260317(0316假日顺延)
        self.assertTrue(store.is_locked("s", "20260312", "open", "entry"))


class TestG1O3Global(unittest.TestCase):
    """#7 (G1/O3): global 锁活动时, 任意 strategy/symbol is_locked=True; global 吸收短的下级锁."""

    def test_global_covers_all(self):
        store = mk_store()
        store.lock("global", "*", "entry", "DRAWDOWN_BRAKE", "g",
                   UnlockSpec(type="trading_days", anchor_day="20260309", after_days=3),
                   NOW, "t")
        self.assertTrue(store.is_locked("any_strategy", "20260310", "open", "entry"))
        self.assertTrue(store.is_locked("600000", "20260310", "open", "entry"))
        # O3: 提议一条更短的 strategy 锁 -> 被 global 吸收, 无新记录
        lk = store.lock("strategy", "s", "entry", "CONSECUTIVE_LOSS", "x",
                        UnlockSpec(type="trading_days", anchor_day="20260309", after_days=1),
                        NOW, "t")
        self.assertEqual(lk.scope, "global")
        self.assertEqual(len(store.active_locks("20260310")), 1)


class TestFalsyZero(unittest.TestCase):
    """清单 #10† #11 #12: falsy-zero 专防."""

    def test_10_breakeven_zero_counts(self):
        """#10†: 保本推荐 (值恰=0.0) 在阈值>0 场景计入计数.
        freqtrade 对照: stoploss_guard.py#L64-65@2bd60670f4 以 truthiness 连接
        close_profit 判断, 0.0 被静默排除 —— 我方显式 is not None, 0.0 照计."""
        cal = TradingCalendar(DAYS)
        guard = ConsecutiveLossGuard(lookback_days=5, loss_line=0.01,  # 阈值>0 场景
                                     trade_limit=1, stop_duration_days=1)
        ledger = LedgerView(rec_marks=[
            {"strategy_id": "s", "stock_code": "000001", "recommend_date": "20260309",
             "eval_day": "20260310", "value": 0.0}])   # 精确保本
        res = guard.evaluate("20260310", ledger, cal)
        self.assertEqual(len(res.verdicts), 1)   # 0.0 <= 0.01 计入并触发
        self.assertIn("breaches=1", res.verdicts[0].detail)

    def test_11_none_excluded_with_event(self):
        """#11: None(缺失) 从计数排除 且 产生 INSUFFICIENT_DATA 事件, 不静默."""
        cal = TradingCalendar(DAYS)
        guard = ConsecutiveLossGuard(lookback_days=5, loss_line=-0.05,
                                     trade_limit=1, stop_duration_days=1)
        ledger = LedgerView(rec_marks=[
            {"strategy_id": "s", "stock_code": "000001", "recommend_date": "20260309",
             "eval_day": "20260310", "value": None}])
        res = guard.evaluate("20260310", ledger, cal)
        self.assertEqual(len(res.verdicts), 0)   # 排除 -> 不触发
        cats = [e.category for e in res.events]
        self.assertIn(EVENT_INSUFFICIENT_DATA, cats)   # 但必须留痕

    def test_12_lowedge_sum_zero_in_none_out(self):
        """#12: LowEdgePause 求和: 0.0 参与求和, None 排除+事件, 不混用一个 truthiness.
        freqtrade 对照: low_profit_pairs.py#L63-65@2bd60670f4 truthiness 求和排除 0.0."""
        cal = TradingCalendar(DAYS)
        rule = LowEdgePause(lookback_days=5, required_excess=0.0,
                            trade_limit=2, stop_duration_days=1)
        ledger = LedgerView(rec_excess=[
            {"strategy_id": "s", "eval_day": "20260310", "excess": 0.0},
            {"strategy_id": "s", "eval_day": "20260310", "excess": 0.0},
            {"strategy_id": "s", "eval_day": "20260310", "excess": None}])
        res = rule.evaluate("20260310", ledger, cal)
        # 0.0+0.0=0.0 <= 0.0 且样本 2>=limit 2 -> 触发 (0.0 参与了求和与计数)
        self.assertEqual(len(res.verdicts), 1)
        self.assertIn("samples=2", res.verdicts[0].detail)
        # None 排除且留事件
        cats = [e.category for e in res.events]
        self.assertIn(EVENT_INSUFFICIENT_DATA, cats)


class TestDualBasis(unittest.TestCase):
    """清单 #13-#16: 双口径专防 (StrategyDrawdownBrake, equity 唯一口径)."""

    def _daily(self, sid, pairs):
        return [{"strategy_id": sid, "day": d, "day_return": r} for d, r in pairs]

    def test_13_equity_manual_match_no_ratios_switch(self):
        """#13: 移植 T4 反例思想 — 手工计算 equity 相对回撤 == 规则值;
        断言系统内不存在 ratios 模式开关 (口径唯一性)."""
        rets = [0.10, -0.05, -0.08, 0.02]
        # 手工: equity=1.10, 1.045, 0.9614, 0.980628; peak=1.10; max_dd=1-0.9614/1.10
        equity, peak, max_dd = 1.0, 1.0, 0.0
        for r in rets:
            equity *= 1 + r
            peak = max(peak, equity)
            max_dd = max(max_dd, 1 - equity / peak)
        got = StrategyDrawdownBrake.equity_drawdown(rets)
        self.assertAlmostEqual(got, max_dd, places=12)
        self.assertAlmostEqual(got, 1 - (1.10 * 0.95 * 0.92) / 1.10, places=12)
        # 口径唯一性: 无 ratios 开关 (freqtrade drawdown_calculation 参数不存在)
        rule = StrategyDrawdownBrake(lookback_days=5, max_drawdown=0.1, trade_limit=2)
        self.assertFalse(hasattr(rule, "drawdown_calculation"))
        self.assertFalse(hasattr(rule, "ratios"))

    def test_14_drawdown_equal_threshold_triggers(self):
        """#14†: 回撤恰等于阈值 -> 触发.
        freqtrade 对照: max_drawdown_protection.py#L96@2bd60670f4 为严格 >,
        恰等于阈值不触发 (T1) —— 我方护栏保守默认反转为 ≥."""
        cal = TradingCalendar(DAYS)
        # 构造回撤恰=0.5 (二进制可精确表示, 排除浮点表示噪声): 1.0 -> 0.5, dd=0.5
        rule = StrategyDrawdownBrake(lookback_days=5, max_drawdown=0.5, trade_limit=2)
        ledger = LedgerView(strategy_daily=self._daily(
            "s", [("20260309", 0.0), ("20260310", -0.5)]))
        dd = StrategyDrawdownBrake.equity_drawdown([0.0, -0.5])
        self.assertEqual(dd, 0.5)   # 回撤恰等于阈值 (精确相等)
        res = rule.evaluate("20260310", ledger, cal)
        self.assertEqual(len(res.verdicts), 1)         # †触发 (freqtrade 此处不触发)
        self.assertEqual(res.verdicts[0].reason_code, "DRAWDOWN_BRAKE")

    def test_15_threshold_validation(self):
        """#15: 回撤阈值 ∉(0,1] -> 启动抛异常 (兑现 freqtrade TODO L26)."""
        for bad in (0.0, -0.1, 1.5, 100.0):
            with self.assertRaises(LockConfigError):
                StrategyDrawdownBrake(lookback_days=5, max_drawdown=bad, trade_limit=2)
        # 合法边界 1.0 可构造
        StrategyDrawdownBrake(lookback_days=5, max_drawdown=1.0, trade_limit=2)

    def test_16_no_drawdown_event_distinct(self):
        """#16: 无亏损样本: 不触发 且 留 NO_DRAWDOWN 事件, 区别于 INSUFFICIENT_DATA
        (修 T3: freqtrade 吞 ValueError 静默放行)."""
        cal = TradingCalendar(DAYS)
        rule = StrategyDrawdownBrake(lookback_days=5, max_drawdown=0.1, trade_limit=2)
        # 全正收益 -> 无回撤
        ledger = LedgerView(strategy_daily=self._daily(
            "s", [("20260309", 0.01), ("20260310", 0.02)]))
        res = rule.evaluate("20260310", ledger, cal)
        self.assertEqual(len(res.verdicts), 0)
        cats = [e.category for e in res.events]
        self.assertIn(EVENT_NO_DRAWDOWN, cats)
        self.assertNotIn(EVENT_INSUFFICIENT_DATA, cats)
        # 对照: 样本不足场景产生 INSUFFICIENT_DATA 而非 NO_DRAWDOWN
        ledger2 = LedgerView(strategy_daily=self._daily("s", [("20260310", 0.01)]))
        res2 = rule.evaluate("20260310", ledger2, cal)
        cats2 = [e.category for e in res2.events]
        self.assertIn(EVENT_INSUFFICIENT_DATA, cats2)
        self.assertNotIn(EVENT_NO_DRAWDOWN, cats2)


class TestConfigFailFast(unittest.TestCase):
    """#21 (T6 同族): 恢复条件互斥参数/非法相位 -> 启动异常."""

    def test_invalid_specs(self):
        cal = TradingCalendar(DAYS)
        with self.assertRaises(LockConfigError):
            materialize_unlock(UnlockSpec(type="trading_days", anchor_day="20260309"), cal)
        with self.assertRaises(LockConfigError):
            materialize_unlock(UnlockSpec(type="trading_days", anchor_day="20260309",
                                          after_days=2, condition="x"), cal)
        with self.assertRaises(LockConfigError):
            materialize_unlock(UnlockSpec(type="at_phase", anchor_day="20260309",
                                          after_days=3), cal)
        with self.assertRaises(LockConfigError):
            materialize_unlock(UnlockSpec(type="bogus", anchor_day="20260309"), cal)
        with self.assertRaises(LockConfigError):
            materialize_unlock(UnlockSpec(type="trading_days", anchor_day="20260309",
                                          after_days=1, at_phase="noon"), cal)
        with self.assertRaises(LockConfigError):
            PostFailureCooldown(cooldown_days=0)


class TestAssertionHygiene(unittest.TestCase):
    """#24 (§2.6): 本测试文件自查 — 禁 '对象/None != 布尔' 空转断言."""

    def test_no_object_bool_inequality(self):
        import re
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            src = f.read()
        # 禁"不等号后接布尔字面量"与"双等号后接空值字面量"的断言写法
        # (模式用拼接构造, 源文件中不出现可自匹配的字面片段)
        pat_bool = "!=" + r"\s+" + "(True|False)" + r"\b"
        pat_none = "==" + r"\s+" + "None" + r"\b"
        self.assertEqual(re.findall(pat_bool, src), [])
        self.assertEqual(re.findall(pat_none, src), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
