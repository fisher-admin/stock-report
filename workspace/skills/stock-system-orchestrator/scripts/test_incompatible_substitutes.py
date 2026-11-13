"""2026-10-03: reachable-but-incompatible fallbacks must not satisfy readiness."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class IncompatibleSubstituteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import stage1_data_prepare as module  # noqa: PLC0415

        cls.m = module

    def guard(self, **providers):
        refreshed = {k: {"provider": v} for k, v in providers.items()}
        return self.m.apply_quality_guards(quality_ok=True, quality_errors=[], refreshed=refreshed, calendar_ok=True)

    def test_primary_providers_pass(self):
        ok, errors = self.guard(stk_factor="rds", daily="rds", cyq_perf="promax")
        self.assertTrue(ok, errors)

    def test_local_derived_stk_factor_is_rejected(self):
        ok, errors = self.guard(stk_factor="local_history_derived_technical", daily="rds", cyq_perf="rds")
        self.assertFalse(ok)
        self.assertTrue(any("unadjusted" in e for e in errors))

    def test_akshare_cyq_band_mapping_is_rejected(self):
        ok, errors = self.guard(stk_factor="rds", daily="rds", cyq_perf="akshare_stock_cyq_em")
        self.assertFalse(ok)
        self.assertTrue(any("cost_15pct" in e for e in errors))

    def test_same_day_market_snapshot_daily_is_not_rejected_by_this_guard(self):
        ok, errors = self.guard(stk_factor="rds", daily="efinance_realtime_snapshot", cyq_perf="rds")
        self.assertTrue(ok, errors)

    def test_proxy_cyq_still_rejected(self):
        ok, errors = self.guard(stk_factor="rds", daily="rds", cyq_perf="local_proxy_cyq")
        self.assertIn("cyq proxy forbidden", errors)


if __name__ == "__main__":
    unittest.main()
