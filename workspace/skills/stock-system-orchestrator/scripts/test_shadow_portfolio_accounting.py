#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import shadow_portfolio_evaluator as spe


OPEN_DATES = ["20260811", "20260812", "20260813", "20260814", "20260817", "20260818"]


def _snapshot(signal_date: str = "20260811") -> dict:
    return {
        "strategy_id": "prebreakout_v44_balanced",
        "strategy_version": "1.0.0",
        "signal_date": signal_date,
        "signal_data_cutoff": f"{signal_date[:4]}-{signal_date[4:6]}-{signal_date[6:]}T15:00:00+08:00",
        "planned_entry_time": "2026-08-12T09:30:00+08:00",
        "holding_period_days": 5,
        "diagnostic_holding_period_days": [1, 3],
        "used_proxy": False,
        "completeness_status": "complete",
        "round_trip_cost": 0.003,
        "stress_round_trip_cost": 0.005,
        "benchmark": "all_a_tradable_equal_weight",
        "settlement_status": "pending_settlement",
        "rank_change": 0,
        "publish_mode": "observe_only",
        "candidates": [
            {"ts_code": "000001.SZ", "rank": 1, "industry": "银行", "weight": 0.5},
            {"ts_code": "600000.SH", "rank": 2, "industry": "银行", "weight": 0.5},
        ],
    }


def _portfolio_daily(ledger: pd.DataFrame, daily_return: float = 0.004) -> pd.DataFrame:
    dates = sorted(set(ledger["signal_date"].astype(str)))
    strategy_nav = 1.0
    benchmark_nav = 1.0
    rows = []
    source_hash = spe.promotion_ledger_hash(ledger)
    for trade_date in dates:
        benchmark_return = 0.001
        strategy_nav *= 1.0 + daily_return
        benchmark_nav *= 1.0 + benchmark_return
        rows.append(
            {
                "trade_date": trade_date,
                "strategy_id": str(ledger["strategy_id"].iloc[0]),
                "strategy_version": str(ledger["strategy_version"].iloc[0]),
                "source_ledger_hash": source_hash,
                "strategy_return": daily_return,
                "benchmark_return": benchmark_return,
                "strategy_nav": strategy_nav,
                "benchmark_nav": benchmark_nav,
                "active_cohort_count": 5,
                "active_position_count": 20,
                "cash_weight": 0.0,
                "used_proxy": False,
                "completeness_status": "complete",
            }
        )
    return pd.DataFrame(rows)


class ShadowPortfolioAccountingTests(unittest.TestCase):
    def test_snapshot_becomes_pending_immutable_ledger_rows(self):
        rows = spe.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)

        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows["settlement_status"]), {"pending_settlement"})
        self.assertEqual(set(rows["main_holding_period_days"]), {5})
        self.assertTrue(rows["return_5d_net"].isna().all())
        self.assertTrue(rows["is_post_freeze_sample"].all())
        self.assertEqual(set(rows["publish_mode"]), {"observe_only"})
        self.assertEqual(set(rows["rank_change"]), {0})

        again = spe.pending_rows_from_snapshot(_snapshot(), existing=rows, open_trade_dates=OPEN_DATES)
        self.assertEqual(len(again), 2)

        mutated = _snapshot()
        mutated["candidates"][0]["weight"] = 0.9
        mutated["candidates"][1]["weight"] = 0.1
        with self.assertRaises(spe.EvaluationContractError):
            spe.pending_rows_from_snapshot(mutated, existing=rows, open_trade_dates=OPEN_DATES)

    def test_snapshot_contract_rejects_proxy_and_rank_changes(self):
        proxy = _snapshot()
        proxy["used_proxy"] = True
        with self.assertRaises(spe.EvaluationContractError):
            spe.pending_rows_from_snapshot(proxy, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)

        changed = _snapshot()
        changed["rank_change"] = 1
        with self.assertRaises(spe.EvaluationContractError):
            spe.pending_rows_from_snapshot(changed, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)

        same_day = _snapshot()
        same_day["planned_entry_time"] = "2026-08-11T15:00:00+08:00"
        with self.assertRaises(spe.EvaluationContractError):
            spe.pending_rows_from_snapshot(same_day, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)

        wrong_time = _snapshot()
        wrong_time["planned_entry_time"] = "2026-08-12T10:00:00+08:00"
        with self.assertRaises(spe.EvaluationContractError):
            spe.pending_rows_from_snapshot(wrong_time, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)

    def test_settlement_uses_t_plus_one_open_and_t1_t3_t5_qfq_closes(self):
        ledger = spe.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)
        calendar = OPEN_DATES
        records = []
        for code in ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"]:
            entry = 10.0 if code in {"000001.SZ", "600000.SH"} else 20.0
            for date, close_multiple in [
                ("20260812", 1.05),
                ("20260813", 1.07),
                ("20260814", 1.10),
                ("20260817", 1.15),
                ("20260818", 1.20),
            ]:
                records.append(
                    {
                        "trade_date": date,
                        "ts_code": code,
                        "open_qfq": entry,
                        "close_qfq": entry * close_multiple,
                    }
                )
        prices = pd.DataFrame(records)
        universe = pd.DataFrame(
            {
                "trade_date": ["20260812"] * 4,
                "ts_code": ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"],
                "universe_flag": [1, 1, 1, 1],
                "tradable": [1, 1, 1, 1],
            }
        )

        settled = spe.settle_ledger(
            ledger,
            prices=prices,
            pit_universe=universe,
            open_trade_dates=calendar,
            as_of_date="20260818",
        )

        self.assertEqual(set(settled["settlement_status"]), {"settled"})
        self.assertTrue((settled["entry_trade_date"] == "20260812").all())
        self.assertTrue((settled["exit_5d_trade_date"] == "20260818").all())
        self.assertTrue((settled["return_1d_net"] - 0.047).abs().max() < 1e-12)
        self.assertTrue((settled["return_3d_net"] - 0.097).abs().max() < 1e-12)
        self.assertTrue((settled["return_5d_net"] - 0.197).abs().max() < 1e-12)
        self.assertTrue((settled["return_5d_stress"] - 0.195).abs().max() < 1e-12)
        self.assertTrue((settled["benchmark_return_5d"] - 0.20).abs().max() < 1e-12)
        self.assertTrue((settled["excess_return_5d"] + 0.003).abs().max() < 1e-12)

    def test_missing_future_exit_stays_pending_but_past_missing_price_is_data_missing(self):
        ledger = spe.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES)
        calendar = OPEN_DATES
        prices = pd.DataFrame(
            {
                "trade_date": ["20260812", "20260812"],
                "ts_code": ["000001.SZ", "600000.SH"],
                "open_qfq": [10.0, 10.0],
                "close_qfq": [10.1, 10.1],
            }
        )
        universe = pd.DataFrame(
            {
                "trade_date": ["20260812", "20260812"],
                "ts_code": ["000001.SZ", "600000.SH"],
                "universe_flag": [1, 1],
                "tradable": [1, 1],
            }
        )

        pending = spe.settle_ledger(
            ledger,
            prices=prices,
            pit_universe=universe,
            open_trade_dates=calendar,
            as_of_date="20260814",
        )
        self.assertEqual(set(pending["settlement_status"]), {"pending_settlement"})
        self.assertTrue(pending["return_5d_net"].isna().all())

        missing = spe.settle_ledger(
            ledger,
            prices=prices,
            pit_universe=universe,
            open_trade_dates=calendar,
            as_of_date="20260818",
        )
        self.assertEqual(set(missing["settlement_status"]), {"data_missing"})
        self.assertTrue(missing["return_5d_net"].isna().all())

    def test_benchmark_fails_closed_when_one_eligible_constituent_price_is_missing(self):
        ledger = spe.pending_rows_from_snapshot(
            _snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES
        )
        records = []
        for code in ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"]:
            for trade_date in OPEN_DATES[1:]:
                records.append(
                    {
                        "trade_date": trade_date,
                        "ts_code": code,
                        "open_qfq": 10.0,
                        "close_qfq": 11.0,
                    }
                )
        prices = pd.DataFrame(records)
        incomplete = prices[
            ~(
                (prices["trade_date"] == OPEN_DATES[-1])
                & (prices["ts_code"] == "000002.SZ")
            )
        ]
        universe = pd.DataFrame(
            {
                "trade_date": [OPEN_DATES[1]] * 4,
                "ts_code": ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"],
                "universe_flag": [1, 1, 1, 1],
                "tradable": [1, 1, 1, 1],
            }
        )

        result = spe.settle_ledger(
            ledger,
            prices=incomplete,
            pit_universe=universe,
            open_trade_dates=OPEN_DATES,
            as_of_date=OPEN_DATES[-1],
        )

        self.assertEqual(set(result["settlement_status"]), {"data_missing"})
        self.assertTrue(result["benchmark_return_5d"].isna().all())

    def test_promotion_requires_60_new_days_and_all_positive_direction_gates(self):
        rows = []
        codes = [f"{i:06d}.SZ" for i in range(1, 31)]
        industries = [f"行业{i}" for i in range(1, 11)]
        for day_index, date in enumerate(pd.bdate_range("2026-08-11", periods=60)):
            for stock_index, code in enumerate(codes):
                rows.append(
                    {
                        "strategy_id": "prebreakout_v44_balanced",
                        "strategy_version": "1.0.0",
                        "signal_date": date.strftime("%Y%m%d"),
                        "ts_code": code,
                        "industry": industries[stock_index % len(industries)],
                        "weight": 1.0 / len(codes),
                        "settlement_status": "settled",
                        "is_post_freeze_sample": True,
                        "return_5d_net": 0.012 + (stock_index % 3) * 0.0001,
                        "benchmark_return_5d": 0.004,
                    }
                )
        ledger = pd.DataFrame(rows)

        expected_dates = sorted(ledger["signal_date"].unique())
        verdict = spe.evaluate_short_track_promotion(
            ledger,
            expected_signal_dates=expected_dates,
            portfolio_daily=_portfolio_daily(ledger),
        )

        self.assertTrue(verdict["all_gates_pass"])
        self.assertEqual(verdict["decision"], "eligible_for_shadow_promotion")
        self.assertEqual(verdict["execution_authority"], "observe_only_no_auto_order")
        self.assertEqual(verdict["sample_trade_days"], 60)
        self.assertEqual(verdict["positive_20d_windows"], 3)
        self.assertGreater(verdict["net_absolute_return"], 0)
        self.assertGreater(verdict["net_excess_return"], 0)

        too_short = spe.evaluate_short_track_promotion(
            ledger[ledger["signal_date"] != ledger["signal_date"].max()],
            expected_signal_dates=expected_dates[:-1],
            portfolio_daily=_portfolio_daily(
                ledger[ledger["signal_date"] != ledger["signal_date"].max()]
            ),
        )
        self.assertFalse(too_short["all_gates_pass"])
        self.assertIn("minimum_60_new_trade_days", too_short["failed_gates"])

    def test_negative_direction_and_concentration_never_pass(self):
        rows = []
        for date in pd.bdate_range("2026-08-11", periods=60):
            rows.append(
                {
                    "strategy_id": "prebreakout_v43_top15",
                    "strategy_version": "1.0.0",
                    "signal_date": date.strftime("%Y%m%d"),
                    "ts_code": "000001.SZ",
                    "industry": "银行",
                    "weight": 1.0,
                    "settlement_status": "settled",
                    "is_post_freeze_sample": True,
                    "return_5d_net": -0.01,
                    "benchmark_return_5d": -0.005,
                }
            )
        dates = sorted({row["signal_date"] for row in rows})
        ledger = pd.DataFrame(rows)
        verdict = spe.evaluate_short_track_promotion(
            ledger,
            expected_signal_dates=dates,
            portfolio_daily=_portfolio_daily(ledger, daily_return=-0.004),
        )

        self.assertFalse(verdict["all_gates_pass"])
        self.assertIn("positive_net_absolute_return", verdict["failed_gates"])
        self.assertIn("positive_net_excess_return", verdict["failed_gates"])
        self.assertIn("industry_concentration", verdict["failed_gates"])
        self.assertIn("stock_concentration", verdict["failed_gates"])

    def test_promotion_uses_stored_portfolio_weights(self):
        rows = []
        for date in pd.bdate_range("2026-08-11", periods=60):
            signal_date = date.strftime("%Y%m%d")
            rows.append(
                {
                    "strategy_id": "prebreakout_v44_balanced",
                    "strategy_version": "1.0.0",
                    "signal_date": signal_date,
                    "ts_code": "000001.SZ",
                    "industry": "银行",
                    "weight": 0.90,
                    "settlement_status": "settled",
                    "is_post_freeze_sample": True,
                    "return_5d_net": -0.02,
                    "benchmark_return_5d": 0.0,
                }
            )
            for index in range(2, 31):
                rows.append(
                    {
                        "strategy_id": "prebreakout_v44_balanced",
                        "strategy_version": "1.0.0",
                        "signal_date": signal_date,
                        "ts_code": f"{index:06d}.SZ",
                        "industry": f"行业{index % 10}",
                        "weight": 0.10 / 29.0,
                        "settlement_status": "settled",
                        "is_post_freeze_sample": True,
                        "return_5d_net": 0.02,
                        "benchmark_return_5d": 0.0,
                    }
                )

        dates = sorted({row["signal_date"] for row in rows})
        ledger = pd.DataFrame(rows)
        verdict = spe.evaluate_short_track_promotion(
            ledger,
            expected_signal_dates=dates,
            portfolio_daily=_portfolio_daily(ledger, daily_return=-0.004),
        )

        self.assertFalse(verdict["all_gates_pass"])
        self.assertLess(verdict["net_absolute_return"], 0)
        self.assertIn("positive_net_absolute_return", verdict["failed_gates"])
        self.assertIn("industry_concentration", verdict["failed_gates"])

    def test_post_freeze_missing_or_pending_days_block_promotion(self):
        rows = []
        dates = [date.strftime("%Y%m%d") for date in pd.bdate_range("2026-08-11", periods=65)]
        for index, signal_date in enumerate(dates):
            rows.append(
                {
                    "strategy_id": "prebreakout_v44_balanced",
                    "strategy_version": "1.0.0",
                    "signal_date": signal_date,
                    "ts_code": "000001.SZ",
                    "industry": "银行" if index % 2 else "电子",
                    "weight": 1.0,
                    "settlement_status": "settled" if index < 60 else "data_missing",
                    "is_post_freeze_sample": True,
                    "return_5d_net": 0.01 if index < 60 else None,
                    "benchmark_return_5d": 0.0 if index < 60 else None,
                }
            )

        ledger = pd.DataFrame(rows)
        verdict = spe.evaluate_short_track_promotion(
            ledger,
            expected_signal_dates=dates,
            portfolio_daily=_portfolio_daily(ledger, daily_return=0.004),
        )

        self.assertFalse(verdict["all_gates_pass"])
        self.assertEqual(verdict["expected_trade_days"], 65)
        self.assertEqual(verdict["incomplete_trade_days"], 5)
        self.assertIn("complete_expected_post_freeze_samples", verdict["failed_gates"])

    def test_promotion_cannot_pass_without_daily_overlapping_portfolio_evidence(self):
        rows = []
        for date in pd.bdate_range("2026-08-11", periods=60):
            rows.append(
                {
                    "strategy_id": "prebreakout_v44_balanced",
                    "strategy_version": "1.0.0",
                    "signal_date": date.strftime("%Y%m%d"),
                    "ts_code": "000001.SZ",
                    "industry": "银行",
                    "weight": 1.0,
                    "settlement_status": "settled",
                    "is_post_freeze_sample": True,
                    "return_5d_net": 0.01,
                    "benchmark_return_5d": 0.0,
                }
            )
        ledger = pd.DataFrame(rows)
        dates = sorted(set(ledger["signal_date"]))

        verdict = spe.evaluate_short_track_promotion(
            ledger,
            expected_signal_dates=dates,
        )

        self.assertFalse(verdict["all_gates_pass"])
        self.assertIn(
            "staggered_portfolio_mark_to_market_complete", verdict["failed_gates"]
        )

    def test_staggered_portfolio_marks_overlapping_cohorts_once_per_day(self):
        calendar = [
            "20260811",
            "20260812",
            "20260813",
            "20260814",
            "20260817",
            "20260818",
            "20260819",
        ]
        first = _snapshot("20260811")
        second = _snapshot("20260812")
        second["planned_entry_time"] = "2026-08-13T09:30:00+08:00"
        ledger = spe.pending_rows_from_snapshot(
            first, existing=pd.DataFrame(), open_trade_dates=calendar
        )
        ledger = spe.pending_rows_from_snapshot(
            second, existing=ledger, open_trade_dates=calendar
        )
        price_rows = []
        universe_rows = []
        for trade_date in calendar[1:]:
            for code in ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"]:
                price_rows.append(
                    {
                        "trade_date": trade_date,
                        "ts_code": code,
                        "open_qfq": 10.0,
                        "close_qfq": 10.0,
                        "used_proxy": False,
                        "completeness": "complete",
                    }
                )
                universe_rows.append(
                    {
                        "trade_date": trade_date,
                        "ts_code": code,
                        "universe_flag": 1,
                        "tradable": 1,
                        "used_proxy": False,
                        "completeness": "complete",
                    }
                )

        daily = spe.build_staggered_portfolio_daily_evidence(
            ledger,
            prices=pd.DataFrame(price_rows),
            pit_universe=pd.DataFrame(universe_rows),
            open_trade_dates=calendar,
            as_of_date="20260819",
        )

        self.assertEqual(list(daily["trade_date"]), calendar[1:])
        self.assertEqual(list(daily["active_cohort_count"][:2]), [1, 2])
        self.assertEqual(list(daily["active_position_count"][:2]), [2, 4])
        self.assertAlmostEqual(float(daily.iloc[-1]["strategy_nav"]), 0.9988)
        self.assertAlmostEqual(float(daily.iloc[-1]["benchmark_nav"]), 1.0)
        self.assertEqual(
            set(daily["source_ledger_hash"]), {spe.promotion_ledger_hash(ledger)}
        )

    def test_operational_and_effectiveness_status_are_separate_and_serializable(self):
        payload = spe.build_tracking_report(
            strategy_id="prebreakout_v43_control",
            strategy_version="1.0.0",
            operational_ok=True,
            trade_date="20260811",
            operational_evidence={"candidate_snapshot_written": True},
            promotion_verdict={"all_gates_pass": False, "decision": "observe_only"},
        )
        self.assertEqual(payload["operational_status"], "healthy")
        self.assertEqual(payload["effectiveness_status"], "not_validated")
        self.assertNotIn("curve", payload)
        self.assertEqual(payload["artifact_kind"], "candidate_tracking_report")
        json.dumps(payload, allow_nan=False)

    def test_atomic_output_uses_candidate_tracking_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = spe.tracking_report_path(Path(tmpdir), "prebreakout_v43_control", "20260811")
            self.assertEqual(path.name, "prebreakout_v43_control_20260811_candidate_tracking.json")
            spe.write_json_atomic(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})


if __name__ == "__main__":
    unittest.main()
