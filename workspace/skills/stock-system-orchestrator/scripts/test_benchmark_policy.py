import unittest

from benchmark_policy import (
    Coverage,
    PriceReturnResult,
    UnknownPriceGap,
    build_tracking_report,
    evaluate_coverage,
    price_return,
)


class BenchmarkPolicyTests(unittest.TestCase):
    def test_known_suspension_uses_zero_return_with_flag(self):
        result = price_return(previous_close=10.0, current_close=None, suspension=True)
        self.assertEqual(result.return_value, 0.0)
        self.assertEqual(result.provenance, "suspension_carry")

    def test_unknown_missing_price_is_not_imputed(self):
        with self.assertRaises(UnknownPriceGap):
            price_return(previous_close=10.0, current_close=None, suspension=False)

    def test_normal_price_return(self):
        result = price_return(previous_close=10.0, current_close=10.5, suspension=False)
        self.assertAlmostEqual(result.return_value, 0.05)
        self.assertEqual(result.provenance, "normal")

    def test_board_coverage_below_threshold_degrades(self):
        coverage = Coverage(overall=0.995, sh=0.999, sz=0.998, bj=0.91)
        res = evaluate_coverage(coverage)
        self.assertEqual(res.status, "degraded")
        self.assertFalse(res.bj_ok)

    def test_build_tracking_report_honest_status(self):
        rep = build_tracking_report(
            operational_status="degraded_observation",
            effectiveness_status="not_validated",
            expected_trade_days=10,
            sample_trade_days=8,
            failed_gates=["portfolio_eval_incomplete"],
        )
        self.assertEqual(rep["flow_status"], "degraded")
        self.assertEqual(rep["expected_trade_days"], 10)
        self.assertEqual(rep["sample_trade_days"], 8)


if __name__ == "__main__":
    unittest.main()
