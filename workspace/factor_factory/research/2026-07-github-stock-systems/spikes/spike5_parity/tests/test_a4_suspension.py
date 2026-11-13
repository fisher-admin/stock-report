"""验收4 停牌: 停牌日行情=NaN 且不被 ffill 污染; 复牌日与无停牌对照的差异可解释.

三层证据:
  A. 真实停牌(小样本): 688981.SH 于 20250901-20250908 连续 6 个交易日无行情行
     (tushare 代理真实数据) => 面板 NaN、因子 NaN、无 ffill。
  B. 合成受控停牌[合成样本, 已注明]: DropRowsSource 删除 600519.SH 5 个交易日行,
     与无停牌对照逐日比对 —— 差异日集合 == 按各因子滞后集推导的"污染带", 带外逐元素相等,
     复牌后首个带外日与对照精确一致(复牌差异可解释、可预测)。
  C. 真实停牌(生产 cache): 300052.SZ 20260415 缺行, 前后日在场 => NaN 不被前值填充。
复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a4_suspension -v
"""
import unittest

import numpy as np

from common import (REAL_SUSP_CODE, REAL_SUSP_DATES, T_END, cache_available,
                    calendar, sample_source)
from parity_lib import exact_equal, load_panel_for, run_factor
from parity_lib.factors import ALL_FACTORS
from parity_lib.sources import CacheSource, DropRowsSource

# 各因子第 t 日输出依赖的滞后集(交易日): 用于推导停牌污染带
LAG_SETS = {
    "ma60_diff": set(range(0, 61)),      # 全窗
    "ewm_ret20": set(range(0, 139)),     # 全窗(硬截断)
    "pv_corr20": set(range(0, 21)),      # 全窗
    "cs_rank_ret5": {0, 5},              # 端点(own 列)
    "mom20_hfq": {0, 20},                # 端点
}

SYN_CODE = "600519.SH"


def affected_days(cal, sessions, susp_dates, lags):
    """污染带: t 使得存在 lag∈lags, shift(t,-lag)∈susp_dates。"""
    susp_idx = {cal.index_of(d) for d in susp_dates}
    out = []
    for t in sessions:
        ti = cal.index_of(t)
        if any((ti - L) in susp_idx for L in lags):
            out.append(t)
    return out


class TestRealSuspensionSample(unittest.TestCase):
    """A: 688981.SH 真实停牌 20250901-20250908。"""

    def setUp(self):
        cal = calendar()
        self.cal = cal
        self.start, self.end = "20250801", "20251231"
        self.panel = load_panel_for(ALL_FACTORS, self.start, self.end,
                                    source=sample_source(), calendar=cal)

    def test_panel_nan_no_ffill(self):
        close = self.panel.field("close")[REAL_SUSP_CODE]
        for d in REAL_SUSP_DATES:
            self.assertTrue(np.isnan(close.loc[d]), f"{d} 停牌日 close 应为 NaN")
        prev = self.cal.shift(REAL_SUSP_DATES[0], -1)
        nxt = self.cal.shift(REAL_SUSP_DATES[-1], +1)
        self.assertFalse(np.isnan(close.loc[prev]), "停牌前一日应有行情")
        self.assertFalse(np.isnan(close.loc[nxt]), "复牌日应有行情")
        # 前一日有值而停牌日 NaN => 结构上未发生 ffill

    def test_factors_nan_during_suspension(self):
        for name in ALL_FACTORS:
            out = run_factor(name, self.panel)
            for d in REAL_SUSP_DATES:
                self.assertTrue(np.isnan(out.loc[d, REAL_SUSP_CODE]),
                                f"{name}@{d} 停牌日输出应为 NaN")

    def test_no_ffill_invariant(self):
        """结构不变式: 面板 close 的 NaN 数 == 日历格点数 - 原始行数(每个缺行都留白)。"""
        daily, _ = sample_source().read(self.panel.ext_start, self.end)
        close = self.panel.field("close")
        expected_nan = close.shape[0] * close.shape[1] - len(daily)
        self.assertEqual(int(close.isna().sum().sum()), expected_nan)


class TestSyntheticSuspensionControlled(unittest.TestCase):
    """B: 合成受控停牌(600519.SH, 5 日) vs 无停牌对照。[合成样本]"""

    @classmethod
    def setUpClass(cls):
        cal = calendar()
        cls.cal = cal
        d0 = cal.index_of("20260310")
        cls.susp = cal.sessions[d0:d0 + 5]
        cls.start, cls.end = "20260105", T_END
        src = sample_source()
        cls.p_ctl = load_panel_for(ALL_FACTORS, cls.start, cls.end,
                                   source=src, calendar=cal)
        cls.p_susp = load_panel_for(ALL_FACTORS, cls.start, cls.end,
                                    source=DropRowsSource(src, SYN_CODE, set(cls.susp)),
                                    calendar=cal)

    def test_own_column_contamination_band_exact(self):
        for name in ALL_FACTORS:
            with self.subTest(factor=name):
                out_c = run_factor(name, self.p_ctl)[SYN_CODE]
                out_s = run_factor(name, self.p_susp)[SYN_CODE]
                band = set(affected_days(self.cal, list(out_c.index), self.susp,
                                         LAG_SETS[name]))
                for t in out_c.index:
                    if t in band:
                        self.assertTrue(np.isnan(out_s.loc[t]),
                                        f"{name}@{t} 在污染带内应为 NaN")
                    else:
                        a, b = out_c.loc[t], out_s.loc[t]
                        self.assertTrue((a == b) or (np.isnan(a) and np.isnan(b)),
                                        f"{name}@{t} 带外应与对照逐元素相等: {a} vs {b}")

    def _shift_or_none(self, date, n):
        try:
            return self.cal.shift(date, n)
        except Exception:
            return None  # 越过日历边界: 边界断言不适用(ewm_ret20 带宽138 超出样本终点)

    def test_resumption_boundary_explainable(self):
        """复牌差异可解释: 带内最后一日仍 NaN, 次一交易日起与对照精确相等。
        (ewm_ret20 污染带 138 日延伸超出样本终点 20260710, 其带外边界断言不适用,
        带内全 NaN 已由 test_own_column_contamination_band_exact 覆盖到样本终点。)"""
        susp_end = self.susp[-1]
        n_boundary_checked = 0
        for name, max_lag in [("ma60_diff", 60), ("ewm_ret20", 138),
                              ("pv_corr20", 20), ("mom20_hfq", 20)]:
            with self.subTest(factor=name):
                out_c = run_factor(name, self.p_ctl)[SYN_CODE]
                out_s = run_factor(name, self.p_susp)[SYN_CODE]
                last_bad = self._shift_or_none(susp_end, +max_lag)
                first_ok = self._shift_or_none(susp_end, +max_lag + 1)
                if last_bad is not None and last_bad in out_s.index:
                    self.assertTrue(np.isnan(out_s.loc[last_bad]),
                                    f"{name}: 复牌后第{max_lag}日窗口仍含停牌 NaN")
                if first_ok is not None and first_ok in out_s.index:
                    self.assertEqual(out_s.loc[first_ok], out_c.loc[first_ok],
                                     f"{name}: 窗口移出停牌段后应与对照精确一致")
                    n_boundary_checked += 1
        self.assertGreaterEqual(n_boundary_checked, 3, "边界断言覆盖不足")

    def test_cross_section_other_columns(self):
        """cs_rank: 其它股票仅在停牌票 ret5 可用性变化的日子受影响(样本池 30->29), 其余日相等。"""
        out_c = run_factor("cs_rank_ret5", self.p_ctl)
        out_s = run_factor("cs_rank_ret5", self.p_susp)
        band = set(affected_days(self.cal, list(out_c.index), self.susp, {0, 5}))
        others = [c for c in out_c.columns if c != SYN_CODE]
        ok, maxdiff, ndiff = exact_equal(
            out_c.loc[[t for t in out_c.index if t not in band], others],
            out_s.loc[[t for t in out_s.index if t not in band], others])
        self.assertTrue(ok, f"带外其它列应逐元素相等: ndiff={ndiff} maxdiff={maxdiff}")


@unittest.skipUnless(cache_available(), "backtest_cache 不在本机")
class TestRealSuspensionCache(unittest.TestCase):
    """C: 生产 cache 真实停牌 300052.SZ@20260415。"""

    def test_cache_suspension_nan_not_ffilled(self):
        cal = calendar()
        panel = load_panel_for(["pv_corr20"], "20260401", "20260430",
                               source=CacheSource(), calendar=cal)
        close = panel.field("close")["300052.SZ"]
        self.assertTrue(np.isnan(close.loc["20260415"]), "停牌日 close 应为 NaN(未被 ffill)")
        self.assertFalse(np.isnan(close.loc["20260414"]))
        self.assertFalse(np.isnan(close.loc["20260416"]))
        out = run_factor("pv_corr20", panel)["300052.SZ"]
        self.assertTrue(np.isnan(out.loc["20260415"]))
        self.assertTrue(np.isnan(out.loc["20260416"]), "窗口含停牌 NaN => 输出 NaN(严格传播)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
