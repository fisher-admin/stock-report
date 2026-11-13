"""#52 滚动窗事故回归测试 —— 永久资产 (PERMANENT ASSET).

不随 spike 回滚删除(PHASE2_TIERA_REPORT §Spike-5 回滚方式)。
#52 事故形态: 管线只拉 N 天, 长窗因子在不足历史上静默算错(qlib min_periods=1 同型)。
本回归钉死三道闸:
  R1 恒等回放: 查询窗长短不得改变重叠日因子值(rtol=0);
  R2 欠窗必炸: 历史不足 => LookbackUnderflow, 永不静默短窗;
  R3 注册即门: 未声明 required_lookback 的因子不得进入装载路径。
复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_52_regression -v
"""
import unittest

from common import T_END, calendar, sample_source, windows_250_5
from parity_lib import (LookbackUnderflow, UnregisteredFactorError, exact_equal,
                        get_spec, load_panel_for, run_factor)
from parity_lib.factors import ALL_FACTORS
from parity_lib.sources import ClippedSource


class Test52Regression(unittest.TestCase):

    def test_R1_identity_replay(self):
        (a_start, a_end), (b_start, b_end) = windows_250_5()
        src, cal = sample_source(), calendar()
        for name in ALL_FACTORS:
            with self.subTest(factor=name):
                out_a = run_factor(name, load_panel_for([name], a_start, a_end,
                                                        source=src, calendar=cal))
                out_b = run_factor(name, load_panel_for([name], b_start, b_end,
                                                        source=src, calendar=cal))
                ok, maxdiff, ndiff = exact_equal(out_a.loc[out_b.index], out_b)
                self.assertTrue(ok, f"{name}: parity 破坏 ndiff={ndiff} maxdiff={maxdiff}")
                self.assertGreater(int(out_b.notna().sum().sum()), 0)

    def test_R2_underflow_hard_fail(self):
        src, cal = sample_source(), calendar()
        start = cal.shift(T_END, -5)
        for name in ALL_FACTORS:
            with self.subTest(factor=name):
                lb = get_spec(name).required_lookback
                poison = ClippedSource(src, cal.shift(cal.shift(start, -lb), +1))
                with self.assertRaises(LookbackUnderflow):
                    load_panel_for([name], start, T_END, source=poison, calendar=cal)

    def test_R3_registration_gate(self):
        with self.assertRaises(UnregisteredFactorError):
            load_panel_for(["factor_without_metadata"], "20260601", T_END,
                           source=sample_source(), calendar=calendar())


if __name__ == "__main__":
    unittest.main(verbosity=2)
