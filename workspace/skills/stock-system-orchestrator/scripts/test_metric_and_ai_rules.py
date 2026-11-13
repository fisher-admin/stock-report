#!/usr/bin/env python3
from __future__ import annotations

import unittest

import ai_risk_rules as rules
import metric_guards as mg
import v45_diagnostic_report as diag


class MetricGuardTests(unittest.TestCase):
    def test_ratio_clamped(self):
        self.assertEqual(mg.safe_ratio(15, 10), 1.0)
        self.assertEqual(mg.safe_ratio(-1, 10), 0.0)
        self.assertEqual(mg.safe_ratio(5, 10), 0.5)
        self.assertIsNone(mg.safe_ratio(1, 0, default=None))

    def test_coverage_payload_flags_inconsistent(self):
        p = mg.coverage_payload(25, 20)
        self.assertEqual(p["value"], 1.0)
        self.assertTrue(p["inconsistent_have_gt_total"])
        self.assertEqual(p["have"], 20)

    def test_assert_ratio(self):
        self.assertTrue(mg.assert_ratio_at_most_one(1.2, field="x"))
        self.assertFalse(mg.assert_ratio_at_most_one(0.5, field="x"))


class AiRiskRuleTests(unittest.TestCase):
    def test_observe_only_caps_advice(self):
        out = rules.apply_risk_rules(
            {"ai_advice": "主攻", "ai_summary": "短", "ai_points": ["a"]},
            system_observe_only=True,
        )
        self.assertEqual(out["ai_advice"], "观察")
        self.assertEqual(out["rule_action_cap"], "观察")
        self.assertEqual(out["rank_change"], 0)
        self.assertIsNone(out.get("display_score"))

    def test_strip_model_prices(self):
        out = rules.strip_model_estimate_prices(
            {
                "trigger_price": {"value": 10, "source": "model_estimate"},
                "support_price": 12.3,
                "stop_loss_price": {"value": 9, "source": "rule_computed"},
                "display_score": 88,
            }
        )
        self.assertNotIn("trigger_price", out)
        self.assertNotIn("support_price", out)
        self.assertNotIn("display_score", out)
        self.assertIn("stop_loss_price", out)

    def test_all_observe_allowed(self):
        self.assertTrue(rules.all_observe_is_allowed(system_observe_only=True))
        self.assertFalse(rules.all_observe_is_allowed(system_observe_only=False))


class DiagnosticTests(unittest.TestCase):
    def test_jaccard_report(self):
        v44 = {
            "candidates": [{"ts_code": "1"}, {"ts_code": "2"}],
            "input_universe_hash": "abc",
            "confirmed_universe_ts_codes": ["1", "2", "3"],
            "input_universe_size": 3,
        }
        v45 = {
            "signal_date": "20260812",
            "candidates": [{"ts_code": "2"}, {"ts_code": "3"}],
            "input_universe_hash": "abc",
            "confirmed_universe_ts_codes": ["1", "2", "3"],
            "input_universe_size": 3,
            "cs_pipeline": "v45.1",
            "diagnostics": {"mad_clip_counts": {}},
        }
        report = diag.build_report(v44, v45)
        self.assertTrue(report["not_for_promotion"])
        self.assertTrue(report["universe"]["input_universe_hash_match"])
        self.assertGreaterEqual(report["topn_overlap"]["jaccard"], 0.0)


if __name__ == "__main__":
    unittest.main()
