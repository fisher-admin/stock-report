#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import strategy_publication_layer as spl  # noqa: E402


class StrategyPublicationLayerRegressionTests(unittest.TestCase):
    def test_parse_float_keeps_numeric_string_support(self):
        self.assertEqual(spl.parse_float("12.5"), 12.5)

    def test_apply_adjustments_uses_numeric_string_scores(self):
        rows, _ = spl.apply_adjustments(
            [
                {
                    "code": "000001",
                    "ai_score": "12.5",
                    "rank": "2",
                    "ai_advice": "主攻",
                }
            ],
            strategy_id=spl.PREBREAKOUT_ID,
            trade_date="20260810",
            source_date="20260810",
            market_context={"risk_score": 25, "sentiment": "偏积极", "market_cycle": "震荡/中性"},
            weight=1.0,
            overlap=set(),
        )
        self.assertAlmostEqual(rows[0]["publication_score"], 12.4, places=6)

    def test_materialize_contract_uses_next_trading_day_not_natural_day(self):
        fields = spl.materialize_recommendation_contract_fields(
            {"source_path": "/tmp/strategy_backtests.json"},
            strategy_id=spl.PREBREAKOUT_ID,
            trade_date="20260807",
            source_date="20260807",
            trade_dates=["20260807", "20260810", "20260811"],
        )
        self.assertEqual(fields["planned_entry_time"], "2026-08-10T09:30:00+08:00")
        self.assertEqual(fields["strategy_version"], spl.PREBREAKOUT_CONTROL_STRATEGY_VERSION)

    def test_materialize_contract_does_not_fabricate_when_next_trade_day_missing(self):
        fields = spl.materialize_recommendation_contract_fields(
            {"source_path": "/tmp/strategy_backtests.json"},
            strategy_id=spl.PREBREAKOUT_ID,
            trade_date="20260811",
            source_date="20260811",
            trade_dates=["20260811"],
        )
        self.assertIsNone(fields["planned_entry_time"])

    def test_materialize_contract_uses_persistent_calendar_when_cache_has_no_future_day(self):
        with mock.patch.object(spl, "load_open_trade_dates", return_value=["20260808", "20260811", "20260812"]):
            fields = spl.materialize_recommendation_contract_fields(
                {"source_path": "/tmp/strategy_backtests.json"},
                strategy_id=spl.PREBREAKOUT_ID,
                trade_date="20260810",
                source_date="20260810",
                trade_dates=None,
            )
        self.assertEqual(fields["planned_entry_time"], "2026-08-11T09:30:00+08:00")


if __name__ == "__main__":
    unittest.main()
