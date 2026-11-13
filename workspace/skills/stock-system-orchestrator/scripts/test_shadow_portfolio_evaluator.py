#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import shadow_portfolio_evaluator as spe  # noqa: E402


class ShadowPortfolioEvaluatorTests(unittest.TestCase):
    def test_normalized_prices_builds_direct_lookup(self):
        prices = pd.DataFrame(
            {
                "trade_date": ["2026-08-11", "20260812"],
                "ts_code": ["000001.SZ", "000001.SZ"],
                "open_qfq": [10.0, 10.2],
                "close_qfq": [10.1, 10.3],
                "used_proxy": [False, False],
                "completeness": ["complete", "complete"],
            }
        )

        lookup = spe._normalized_prices(prices)

        self.assertIsInstance(lookup, dict)
        self.assertEqual(lookup[("20260811", "000001.SZ")], (10.0, 10.1))
        self.assertEqual(
            spe._price_value(lookup, "20260812", "000001.SZ", "close_qfq"),
            10.3,
        )

    def test_settlement_caches_identical_benchmark_window(self):
        trade_dates = ["20260811", "20260812", "20260813", "20260814", "20260817"]
        prices = pd.DataFrame(
            [
                {
                    "trade_date": date,
                    "ts_code": code,
                    "open_qfq": 10.0 + offset,
                    "close_qfq": 10.1 + offset,
                }
                for offset, date in enumerate(trade_dates)
                for code in ("000001.SZ", "000002.SZ")
            ]
        )
        universe = pd.DataFrame(
            {
                "trade_date": ["20260811", "20260811"],
                "ts_code": ["000001.SZ", "000002.SZ"],
                "universe_flag": [1, 1],
                "tradable": [1, 1],
            }
        )
        ledger = pd.DataFrame(
            {
                "signal_date": ["20260810", "20260810"],
                "ts_code": ["000001.SZ", "000002.SZ"],
                "settlement_status": ["pending_settlement", "pending_settlement"],
                "round_trip_cost": [0.003, 0.003],
                "stress_round_trip_cost": [0.005, 0.005],
            }
        )

        with mock.patch.object(
            spe,
            "_benchmark_return",
            wraps=spe._benchmark_return,
        ) as benchmark_return:
            settled = spe.settle_ledger(
                ledger,
                prices=prices,
                pit_universe=universe,
                open_trade_dates=trade_dates,
                as_of_date="20260817",
            )

        self.assertEqual(benchmark_return.call_count, 1)
        self.assertEqual(set(settled["settlement_status"]), {"settled"})

    def test_operational_health_is_separate_from_promotion_effectiveness(self):
        payload = spe.build_tracking_report(
            strategy_id="prebreakout_v43_control",
            strategy_version="1.0.0",
            trade_date="20260824",
            operational_ok=True,
            operational_evidence={"candidate_snapshot_written": True},
            promotion_verdict={
                "all_gates_pass": False,
                "failed_gates": ["insufficient_matured_trade_days"],
                "decision": "observe_only",
            },
        )
        self.assertEqual(payload["trade_date"], "20260824")
        self.assertEqual(payload["operational_status"], "healthy")
        self.assertEqual(payload["effectiveness_status"], "not_validated")
        self.assertEqual(payload["execution_authority"], "observe_only_no_auto_order")

    def test_benchmark_membership_falls_back_to_last_available_universe_day(self):
        universe = pd.DataFrame(
            {
                "trade_date": ["20260818", "20260818", "20260820"],
                "ts_code": ["000001.SZ", "000002.SZ", "000001.SZ"],
                "universe_flag": [1, 1, 1],
                "tradable": [1, 1, 1],
            }
        )
        self.assertEqual(
            spe._resolve_benchmark_membership_date(universe, "20260819"),
            "20260818",
        )

    def test_daily_benchmark_uses_fallback_membership_when_hole_exists(self):
        price_index = pd.DataFrame(
            {
                "open_qfq": [10.0, 20.0, 10.3, 20.6, 10.7, 21.4],
                "close_qfq": [10.2, 20.4, 10.3, 20.6, 10.7, 21.4],
            },
            index=pd.MultiIndex.from_tuples(
                [
                    ("20260818", "000001.SZ"),
                    ("20260818", "000002.SZ"),
                    ("20260819", "000001.SZ"),
                    ("20260819", "000002.SZ"),
                    ("20260820", "000001.SZ"),
                    ("20260820", "000002.SZ"),
                ],
                names=["trade_date", "ts_code"],
            ),
        )
        universe = pd.DataFrame(
            {
                "trade_date": ["20260818", "20260818"],
                "ts_code": ["000001.SZ", "000002.SZ"],
                "universe_flag": [1, 1],
                "tradable": [1, 1],
            }
        )
        value = spe._daily_equal_weight_benchmark_return(
            price_index,
            universe,
            trade_date="20260820",
            previous_trade_date="20260819",
        )
        expected = ((10.7 / 10.3 - 1.0) + (21.4 / 20.6 - 1.0)) / 2.0
        self.assertAlmostEqual(value, expected)


if __name__ == "__main__":
    unittest.main()
