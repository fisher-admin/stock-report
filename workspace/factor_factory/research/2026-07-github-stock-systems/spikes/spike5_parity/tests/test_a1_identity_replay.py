"""验收1 恒等回放: 每因子在 [T-250,T] 与 [T-5,T] 两查询窗下重叠日值逐元素相等(rtol=0).

主测: 小样本真实数据(30票, tushare 代理, 609 交易日) —— 满足规格窗深度。
辅测: 生产 backtest_cache 全截面(~5400票, 153 交易日, 窗深不足以跑 [T-250,T],
     以 [T-30,T] vs [T-5,T] 复核 lookback<=60 的三个因子; 窗口缩减在报告注明)。
复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a1_identity_replay -v
"""
import unittest

from common import (T_END, cache_available, calendar, sample_source, windows_250_5)
from parity_lib import exact_equal, load_panel_for, run_factor
from parity_lib.factors import ALL_FACTORS
from parity_lib.sources import CacheSource


class TestIdentityReplaySample(unittest.TestCase):
    """[T-250,T] vs [T-5,T], 5 因子, 真实小样本。"""

    def test_identity_replay_all_factors(self):
        (a_start, a_end), (b_start, b_end) = windows_250_5()
        src, cal = sample_source(), calendar()
        for name in ALL_FACTORS:
            with self.subTest(factor=name):
                pa = load_panel_for([name], a_start, a_end, source=src, calendar=cal)
                pb = load_panel_for([name], b_start, b_end, source=src, calendar=cal)
                out_a = run_factor(name, pa)
                out_b = run_factor(name, pb)
                overlap = out_b.index  # [T-5, T]
                ok, maxdiff, ndiff = exact_equal(out_a.loc[overlap], out_b)
                self.assertTrue(
                    ok, f"{name}: 重叠窗 {ndiff} 个值不等, max_abs_diff={maxdiff}")
                # 重叠窗内须有真实数值(防"全 NaN 恒等"假通过)
                self.assertGreater(int(out_b.notna().sum().sum()), 0,
                                   f"{name}: 重叠窗全 NaN, 恒等无意义")


@unittest.skipUnless(cache_available(), "backtest_cache 不在本机")
class TestIdentityReplayCache(unittest.TestCase):
    """生产 cache 全截面辅测(~5400票真实日线)。

    窗深与窗位受限并在报告注明: cache 覆盖区间内实测存在 31 个开市日数据洞
    (20251015-20251118 块 + 20260408-10/0417/0427-28), 连续清洁段为
    20251119..20260407(91 个交易日), 故辅测用 T=20260407, [T-30,T] vs [T-5,T],
    只跑 lookback<=60 的三个因子。
    universe 说明: 装载 universe=窗口内出现过的代码并集, 长窗 A ⊇ 短窗 B;
    比较在 B 的代码集上进行(截面 rank 只消费当日有效值, 与并集大小无关)。
    """

    T_CACHE = "20260407"

    def test_identity_replay_cache(self):
        cal = calendar()
        src = CacheSource()
        a_start = cal.shift(self.T_CACHE, -30)
        b_start = cal.shift(self.T_CACHE, -5)
        for name in ["ma60_diff", "pv_corr20", "cs_rank_ret5"]:
            with self.subTest(factor=name):
                pa = load_panel_for([name], a_start, self.T_CACHE, source=src, calendar=cal)
                pb = load_panel_for([name], b_start, self.T_CACHE, source=src, calendar=cal)
                out_a = run_factor(name, pa)
                out_b = run_factor(name, pb)
                self.assertTrue(set(out_b.columns) <= set(out_a.columns))
                ok, maxdiff, ndiff = exact_equal(
                    out_a.loc[out_b.index, out_b.columns], out_b)
                self.assertTrue(
                    ok, f"{name}@cache: {ndiff} 个值不等, max_abs_diff={maxdiff}")
                self.assertGreater(int(out_b.notna().sum().sum()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
