"""Thresholds of the forward_shadow_v1 input gate (2026-11-17 pre-mortem)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import forward_shadow_preflight as pf  # noqa: E402

REF = {"daily": 5571, "stk_factor": 5571, "daily_basic": 5571}


class MissingTest(unittest.TestCase):
    def full(self, **kw):
        c = {"ref": REF, "daily": 5572, "stk_factor": 5572, "daily_basic": 5572, "pit_universe": 5923}
        c.update(kw)
        return c

    def test_complete_day_passes(self):
        self.assertEqual(pf.missing(self.full()), [])

    def test_partial_daily_like_20261106_is_flagged(self):
        self.assertEqual(pf.missing(self.full(daily=4904)), ["daily"])

    def test_absent_inputs_are_flagged(self):
        self.assertEqual(pf.missing(self.full(daily_basic=0, pit_universe=0)), ["daily_basic", "pit_universe"])

    def test_absolute_floor_without_reference(self):
        self.assertEqual(pf.missing(self.full(ref={}, daily=3999)), ["daily"])


if __name__ == "__main__":
    unittest.main()
