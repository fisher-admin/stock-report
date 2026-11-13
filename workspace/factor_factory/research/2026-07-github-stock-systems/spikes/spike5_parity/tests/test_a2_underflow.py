"""验收2 欠窗必炸: 面板起点裁到 ext_start 之后 => 装载器 raise LookbackUnderflow(毒样本).

qlib 反面对照: qlib 在同形态下静默给短窗值(min_periods=1, #52 事故形态), 我方必须 raise。
复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a2_underflow -v
"""
import unittest

from common import T_END, calendar, sample_source
from parity_lib import LookbackUnderflow, load_panel_for
from parity_lib.factors import ALL_FACTORS
from parity_lib.sources import ClippedSource


class TestLookbackUnderflow(unittest.TestCase):

    def test_clipped_panel_raises(self):
        """把源起点裁到 ext_start 后 1 个交易日 => 必炸。"""
        cal, src = calendar(), sample_source()
        start = cal.shift(T_END, -5)
        for name in ALL_FACTORS:
            with self.subTest(factor=name):
                from parity_lib import get_spec
                lb = get_spec(name).required_lookback
                ext_start = cal.shift(start, -lb)
                poison = ClippedSource(src, cal.shift(ext_start, +1))
                with self.assertRaises(LookbackUnderflow):
                    load_panel_for([name], start, T_END, source=poison, calendar=cal)

    def test_clip_exactly_at_ext_start_passes(self):
        """恰好覆盖 ext_start => 不炸(守卫边界不虚炸)。"""
        cal, src = calendar(), sample_source()
        start = cal.shift(T_END, -5)
        from parity_lib import get_spec
        lb = get_spec("ma60_diff").required_lookback
        ext_start = cal.shift(start, -lb)
        panel = load_panel_for(["ma60_diff"], start, T_END,
                               source=ClippedSource(src, ext_start), calendar=cal)
        self.assertEqual(panel.first_date, ext_start)

    def test_calendar_exhausted_raises(self):
        """查询窗过早, 日历本身前移越界 => 同样 LookbackUnderflow。"""
        cal, src = calendar(), sample_source()
        early = cal.sessions[10]  # 日历第11个交易日, 无法前移138根
        with self.assertRaises(LookbackUnderflow):
            load_panel_for(["ewm_ret20"], early, T_END, source=src, calendar=cal)

    def test_real_production_hole_caught(self):
        """真实毒样本(非人造): 生产 backtest_cache 在 20260427/28 等 31 个开市日
        整日缺文件(spike 实测发现)。require_full_sessions=True 时装载器必须把
        中段数据洞变成硬失败, 而非静默整日 NaN。"""
        from common import cache_available
        from parity_lib import PanelHoleError
        from parity_lib.sources import CacheSource
        if not cache_available():
            self.skipTest("backtest_cache 不在本机")
        cal = calendar()
        with self.assertRaises(PanelHoleError):
            load_panel_for(["pv_corr20"], "20260420", "20260508",
                           source=CacheSource(), calendar=cal,
                           require_full_sessions=True)
        # 同窗不开开关: 不炸(个股停牌语义), 但洞日整日 NaN —— 报告注明此残余风险
        panel = load_panel_for(["pv_corr20"], "20260420", "20260508",
                               source=CacheSource(), calendar=cal)
        close = panel.field("close")
        self.assertTrue(close.loc["20260427"].isna().all())
        self.assertTrue(close.loc["20260428"].isna().all())


if __name__ == "__main__":
    unittest.main(verbosity=2)
