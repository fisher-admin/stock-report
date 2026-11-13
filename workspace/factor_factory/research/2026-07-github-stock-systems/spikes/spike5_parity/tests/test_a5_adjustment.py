"""验收5 复权: raw vs hfq 在除权日前后显式断言(值域+符号); qfq 无 asof 即 raise.

数据: 真实除权事件(小样本内 adj_factor 变化 165 处, tushare 代理), 非合成。
恒等式(lean.md §5.2): hfq(t)=raw(t)*adj(t); qfq(t,asof)=raw(t)*adj(t)/adj(asof)。
复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a5_adjustment -v
"""
import unittest

import numpy as np

from common import T_END, calendar, sample_source
from parity_lib import (AdjustmentModeError, RegistryError, get_spec,
                        load_panel_for, load_prices, run_factor)
from parity_lib.registry import FactorSpec

MOM_W = 20


class TestAdjustmentSensitivity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cal = calendar()
        cls.cal = cal
        cls.start = cal.shift(T_END, -250)
        cls.panel = load_panel_for(["mom20_hfq"], cls.start, T_END,
                                   source=sample_source(), calendar=cal)
        cls.adj = cls.panel.field("adj_factor")
        cls.raw = load_prices(cls.panel, mode="raw")
        cls.hfq = load_prices(cls.panel, mode="hfq")
        cls.mom_hfq = run_factor("mom20_hfq", cls.panel)
        mom_raw_full = cls.raw / cls.raw.shift(MOM_W) - 1.0
        cls.mom_raw = mom_raw_full.loc[cls.mom_hfq.index]
        # 查询窗内真实除权事件: adj_factor 相邻日变化
        chg = cls.adj.diff().abs() > 1e-9
        ev = chg.loc[cls.mom_hfq.index].stack()
        cls.events = [(d, c) for (d, c) in ev[ev].index]

    def test_real_events_exist(self):
        self.assertGreater(len(self.events), 5,
                           f"查询窗内应有真实除权事件, 实际 {len(self.events)}")

    def test_identity_hfq_vs_raw(self):
        """恒等式: (1+mom_hfq) = (1+mom_raw) * adj(t)/adj(t-20), 全面板逐元素。"""
        ratio = (self.adj / self.adj.shift(MOM_W)).loc[self.mom_hfq.index]
        lhs = (1.0 + self.mom_hfq).to_numpy()
        rhs = ((1.0 + self.mom_raw) * ratio).to_numpy()
        both_nan = np.isnan(lhs) & np.isnan(rhs)
        close_ = np.isclose(lhs, rhs, rtol=1e-12, atol=0, equal_nan=False)
        self.assertTrue((close_ | both_nan).all(), "恒等式在某些元素上破坏")

    def test_explicit_divergence_at_events(self):
        """除权窗内: mom_raw != mom_hfq, 且符号关系 = sign(adj比-1); 无事件窗: 两口径一致。"""
        ratio = (self.adj / self.adj.shift(MOM_W)).loc[self.mom_hfq.index]
        n_checked = 0
        for (d, c) in self.events:
            r = ratio.loc[d, c]
            mh, mr = self.mom_hfq.loc[d, c], self.mom_raw.loc[d, c]
            if np.isnan(r) or np.isnan(mh) or np.isnan(mr):
                continue
            n_checked += 1
            self.assertNotAlmostEqual(mh, mr, places=15,
                                      msg=f"{c}@{d}: 除权窗内 raw 与 hfq 动量必须分离")
            # 符号断言: hfq 因子跳升(分红/送转 => r>1) => mom_hfq > mom_raw
            self.assertEqual(np.sign(mh - mr), np.sign(r - 1.0),
                             f"{c}@{d}: 口径差方向应与 adj 比一致 (r={r})")
            # 值域断言: 差值 = (r-1)*(1+mom_raw)
            self.assertTrue(np.isclose(mh - mr, (r - 1.0) * (1.0 + mr), rtol=1e-9),
                            f"{c}@{d}: 口径差值域不符")
        self.assertGreater(n_checked, 5, "有效事件断言样本过少")
        # 反向: adj 比恒 1 的 (t,c) 上两口径一致(rtol 1e-12, 浮点消去容差)
        flat = np.isclose(ratio.to_numpy(), 1.0, rtol=0, atol=0)
        mh, mr = self.mom_hfq.to_numpy(), self.mom_raw.to_numpy()
        both_nan = np.isnan(mh) & np.isnan(mr)
        agree = np.isclose(mh, mr, rtol=1e-12, atol=1e-15) | both_nan
        self.assertTrue(agree[flat].all(), "无除权窗内两口径应一致")

    def test_qfq_without_asof_raises(self):
        with self.assertRaises(AdjustmentModeError):
            load_prices(self.panel, mode="qfq_asof")           # 无 asof => 拒绝
        with self.assertRaises(AdjustmentModeError):
            load_prices(self.panel, mode="qfq")                # 别名 => 拒绝
        with self.assertRaises(AdjustmentModeError):
            load_prices(self.panel, mode="qfq_asof", asof="20990101")  # 日历外 asof => 拒绝
        with self.assertRaises(TypeError):
            load_prices(self.panel)                            # 不声明口径 => TypeError

    def test_qfq_identity_with_asof(self):
        qfq = load_prices(self.panel, mode="qfq_asof", asof=T_END)
        expect = self.raw * self.adj / self.adj.loc[T_END]
        a, b = qfq.to_numpy(), expect.to_numpy()
        both_nan = np.isnan(a) & np.isnan(b)
        self.assertTrue(((a == b) | both_nan).all(), "qfq 恒等式(同一算式)应精确成立")
        # asof 当日 qfq == raw
        r0, q0 = self.raw.loc[T_END].to_numpy(), qfq.loc[T_END].to_numpy()
        bn = np.isnan(r0) & np.isnan(q0)
        self.assertTrue((np.isclose(q0, r0, rtol=1e-12, atol=0) | bn).all())

    def test_registry_declares_price_mode(self):
        self.assertEqual(get_spec("mom20_hfq").price_mode, "hfq")
        # 注册表拒收 qfq 系 price_mode(qfq 因子必须另携 asof 策略, 本 spike 拒绝)
        from parity_lib.registry import validate_spec
        bad = FactorSpec(name="mom20_qfq_bad", required_lookback=20, required_forward=0,
                         lookback_basis="x", compute=lambda p: None, price_mode="qfq_asof")
        with self.assertRaises(RegistryError):
            validate_spec(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
