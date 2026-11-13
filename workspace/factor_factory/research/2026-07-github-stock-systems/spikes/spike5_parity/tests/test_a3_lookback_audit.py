"""验收3 低报审计: 每因子 lookback+1 vs lookback 扰动, 值变则 required_lookback 低报, CI 拒绝.

正向: 5 因子全过审计(+1 不改变输出)。
负向对照: 人造低报 spec(ma60_diff 声明 59) => 审计必须抓住并 raise(证明审计有牙齿)。
复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a3_lookback_audit -v
"""
import unittest
from dataclasses import replace

from common import T_END, calendar, sample_source
from parity_lib import LookbackUnderReported, get_spec
from parity_lib.audit import lookback_audit
from parity_lib.factors import ALL_FACTORS


class TestLookbackAudit(unittest.TestCase):

    def _window(self):
        cal = calendar()
        return cal.shift(T_END, -60), T_END  # 61 个输出日的审计窗

    def test_all_factors_pass_audit(self):
        start, end = self._window()
        for name in ALL_FACTORS:
            with self.subTest(factor=name):
                detail = lookback_audit(get_spec(name), start, end,
                                        source=sample_source(), calendar=calendar())
                self.assertTrue(detail["plus1_equal"], detail)
                # 紧致性(信息性): -1 应当改变输出, 否则声明虚胖
                self.assertTrue(detail.get("minus1_changes_output", True),
                                f"{name}: lookback-1 输出不变, 声明可能虚胖: {detail}")

    def test_underreported_spec_is_caught(self):
        """负向对照: ma60_diff 若声明 59(低报1根), 审计必须 raise。"""
        start, end = self._window()
        bad = replace(get_spec("ma60_diff"), name="ma60_diff_underreported",
                      required_lookback=59)
        with self.assertRaises(LookbackUnderReported):
            lookback_audit(bad, start, end, source=sample_source(), calendar=calendar())

    def test_underreported_ewm_full_history_is_caught(self):
        """负向对照2: 若 EWM 不做硬窗口化(用全历史 pandas ewm), +1 扰动必然值变。
        证明审计能抓'隐性无限记忆'这类低报。"""
        import pandas as pd

        from parity_lib.prices import load_prices
        from parity_lib.registry import FactorSpec

        def f_ewm_full_history(panel):
            c = load_prices(panel, mode="raw")
            ret = c / c.shift(1) - 1.0
            return ret.ewm(span=20, adjust=True).mean()  # 起点依赖 => parity 破坏

        bad = FactorSpec(name="ewm_full_history_bad", required_lookback=138,
                         required_forward=0, lookback_basis="(故意低报: 实际无限记忆)",
                         compute=f_ewm_full_history)
        start, end = self._window()
        with self.assertRaises(LookbackUnderReported):
            lookback_audit(bad, start, end, source=sample_source(), calendar=calendar())


if __name__ == "__main__":
    unittest.main(verbosity=2)
