"""验收7 未注册禁入: 无 required_lookback 的因子进闯关机被拒(注册即门).

复跑: /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a7_registry_gate -v
"""
import unittest

from common import T_END, calendar, sample_source
from parity_lib import (RegistryError, UnregisteredFactorError, gauntlet_admit,
                        load_panel_for, register)
from parity_lib.factors import ALL_FACTORS
from parity_lib.registry import FactorSpec


class TestRegistryGate(unittest.TestCase):

    def test_unregistered_rejected_by_gauntlet(self):
        with self.assertRaises(UnregisteredFactorError):
            gauntlet_admit("rogue_momentum_v9")

    def test_unregistered_rejected_by_loader(self):
        with self.assertRaises(UnregisteredFactorError):
            load_panel_for(["rogue_momentum_v9"], "20260601", T_END,
                           source=sample_source(), calendar=calendar())

    def test_register_without_lookback_rejected(self):
        bad = FactorSpec(name="no_lookback", required_lookback=None, required_forward=0,
                         lookback_basis="x", compute=lambda p: None)
        with self.assertRaises(RegistryError):
            register(bad)
        with self.assertRaises(UnregisteredFactorError):
            gauntlet_admit("no_lookback")  # 注册失败 => 依然不在表内

    def test_register_without_basis_rejected(self):
        bad = FactorSpec(name="no_basis", required_lookback=10, required_forward=0,
                         lookback_basis="  ", compute=lambda p: None)
        with self.assertRaises(RegistryError):
            register(bad)

    def test_cross_sectional_without_ties_rejected(self):
        bad = FactorSpec(name="cs_no_ties", required_lookback=5, required_forward=0,
                         lookback_basis="x", compute=lambda p: None,
                         is_cross_sectional=True, ties=None)
        with self.assertRaises(RegistryError):
            register(bad)

    def test_negative_forward_rejected(self):
        bad = FactorSpec(name="neg_fwd", required_lookback=5, required_forward=-1,
                         lookback_basis="x", compute=lambda p: None)
        with self.assertRaises(RegistryError):
            register(bad)

    def test_all_five_factors_admitted(self):
        for name in ALL_FACTORS:
            spec = gauntlet_admit(name)
            self.assertIsInstance(spec.required_lookback, int)
            self.assertEqual(spec.required_forward, 0)
            self.assertTrue(spec.lookback_basis.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
