#!/usr/bin/env python3
from __future__ import annotations

import copy
import unittest

import pandas as pd

import event_quality_drift_evaluator as eqde
import event_quality_drift_v1 as strategy


OPEN_DATES_2025 = [date.strftime("%Y%m%d") for date in pd.bdate_range("2025-01-02", periods=70)]


def _snapshot(signal_date: str = "20250103") -> dict:
    snapshot = {
        "strategy_id": strategy.STRATEGY_ID,
        "strategy_version": strategy.STRATEGY_VERSION,
        "signal_date": signal_date,
        "signal_data_cutoff": f"{signal_date[:4]}-{signal_date[4:6]}-{signal_date[6:]}T23:59:59+08:00",
        "planned_entry_time": "2025-01-06T09:30:00+08:00",
        "holding_period_days": 20,
        "auxiliary_holding_period_days": [40],
        "data_sources": ["pit_yjyg", "pit_financial", "daily_basic", "pit_sw2021_universe"],
        "used_proxy": False,
        "completeness_status": "complete",
        "revision_chain_complete": True,
        "evidence_scope": "promotion_evidence",
        "promotion_evidence_eligible": True,
        "round_trip_cost": 0.003,
        "stress_round_trip_cost": 0.005,
        "benchmark": "all_a_tradable_equal_weight",
        "settlement_status": "pending_settlement",
        "rank_change": 0,
        "publish_mode": "observe_only",
        "execution_authority": "observe_only_no_auto_order",
        "ai_policy": {
            "role": "announcement_evidence_explanation_and_risk_check_only",
            "may_change_rank": False,
            "may_add_candidate": False,
            "may_remove_candidate": False,
        },
        "candidates": [
            {
                "ts_code": "000001.SZ",
                "rank": 1,
                "industry": "银行",
                "weight": 0.05,
                "ai_evidence_time": None,
                "ai_risk_tags": [],
                "ai_explanation": None,
                "risk_gate_passed": True,
                "deterministic_risk_checks": {"complete_non_proxy_inputs": True},
            },
            {
                "ts_code": "600000.SH",
                "rank": 2,
                "industry": "电子",
                "weight": 0.05,
                "ai_evidence_time": None,
                "ai_risk_tags": [],
                "ai_explanation": None,
                "risk_gate_passed": True,
                "deterministic_risk_checks": {"complete_non_proxy_inputs": True},
            },
        ],
    }
    snapshot["active_positions"] = []
    snapshot["active_invested_weight"] = 0.0
    snapshot["new_invested_weight"] = 0.10
    snapshot["portfolio_invested_weight"] = 0.10
    snapshot["cash_weight"] = 0.90
    snapshot["ranked_events"] = [
        {
            "ts_code": row["ts_code"],
            "quant_rank": row["rank"],
            "industry": row["industry"],
            "rank_change": 0,
            "risk_gate_passed": True,
            "deterministic_risk_checks": {"complete_non_proxy_inputs": True},
        }
        for row in snapshot["candidates"]
    ]
    return snapshot


def _price_panel() -> pd.DataFrame:
    entry_date = OPEN_DATES_2025[2]
    exit_20 = OPEN_DATES_2025[21]
    exit_40 = OPEN_DATES_2025[41]
    rows = []
    series = {
        "000001.SZ": (10.0, 12.0, 13.0),
        "600000.SH": (10.0, 12.0, 13.0),
        "000002.SZ": (20.0, 20.0, 20.0),
        "600001.SH": (20.0, 20.0, 20.0),
    }
    for code, (entry_open, close_20, close_40) in series.items():
        rows.extend(
            [
                {
                    "trade_date": entry_date,
                    "ts_code": code,
                    "open_qfq": entry_open,
                    "close_qfq": entry_open * 1.01,
                },
                {
                    "trade_date": exit_20,
                    "ts_code": code,
                    "open_qfq": entry_open,
                    "close_qfq": close_20,
                },
                {
                    "trade_date": exit_40,
                    "ts_code": code,
                    "open_qfq": entry_open,
                    "close_qfq": close_40,
                },
            ]
        )
    return pd.DataFrame(rows)


def _pit_universe() -> pd.DataFrame:
    entry_date = OPEN_DATES_2025[2]
    return pd.DataFrame(
        {
            "trade_date": [entry_date] * 4,
            "ts_code": ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"],
            "universe_flag": [1, 1, 1, 1],
            "tradable": [1, 1, 1, 1],
        }
    )


def _daily_mark_to_market_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict] = []
    universe_rows: list[dict] = []
    holding_dates = OPEN_DATES_2025[2:22]
    codes = ["000001.SZ", "600000.SH", "000002.SZ", "600001.SH"]
    for day_index, trade_date in enumerate(holding_dates):
        selected_close = 10.0 + (2.0 * day_index / (len(holding_dates) - 1))
        for code in codes:
            selected = code in {"000001.SZ", "600000.SH"}
            rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "open_qfq": 10.0 if day_index == 0 else (selected_close if selected else 20.0),
                    "close_qfq": selected_close if selected else 20.0,
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
                    "is_suspended": False,
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(universe_rows)


def _promotion_rows(
    *,
    concentrated: bool = False,
    final_2026_negative: bool = False,
    strategy_version: str | None = None,
    revision_chain_complete: bool = True,
    promotion_evidence_eligible: bool = True,
    include_research_2024: bool = False,
) -> pd.DataFrame:
    rows: list[dict] = []
    version = strategy_version or strategy.STRATEGY_VERSION
    industries = [f"行业{index}" for index in range(1, 11)]
    evidence_scope = "promotion_evidence" if promotion_evidence_eligible else "auxiliary_only"
    if include_research_2024:
        for date in pd.bdate_range("2024-06-03", periods=10):
            rows.append(
                {
                    "strategy_id": strategy.STRATEGY_ID,
                    "strategy_version": version,
                    "signal_date": date.strftime("%Y%m%d"),
                    "ts_code": "000001.SZ",
                    "industry": "银行",
                    "weight": 0.05,
                    "is_selected": True,
                    "quant_rank": 1,
                    "settlement_status": "settled",
                    "revision_chain_complete": revision_chain_complete,
                    "promotion_evidence_eligible": promotion_evidence_eligible,
                    "evidence_scope": evidence_scope,
                    "return_20d_net": 0.01,
                    "benchmark_return_20d": 0.002,
                    "active_invested_weight": 0.0,
                    "new_invested_weight": 0.05,
                    "portfolio_invested_weight": 0.05,
                    "cash_weight": 0.95,
                }
            )
    for date in list(pd.bdate_range("2025-01-06", periods=60)) + list(pd.bdate_range("2026-01-05", periods=60)):
        signal_date = date.strftime("%Y%m%d")
        if concentrated:
            selected_count = 1
            pool_count = 10
        else:
            selected_count = 12
            pool_count = 24
        if signal_date.startswith("2025"):
            selected_return = 0.012
        else:
            selected_return = -0.006 if final_2026_negative else 0.011
        for pool_index in range(pool_count):
            is_selected = pool_index < selected_count
            code = "000001.SZ" if concentrated and pool_index == 0 else f"{pool_index + 1:06d}.SZ"
            industry = "银行" if concentrated and pool_index == 0 else industries[pool_index % len(industries)]
            rows.append(
                {
                    "strategy_id": strategy.STRATEGY_ID,
                    "strategy_version": version,
                    "signal_date": signal_date,
                    "ts_code": code,
                    "industry": industry,
                    "weight": 0.05 if is_selected else 0.0,
                    "is_selected": is_selected,
                    "quant_rank": pool_index + 1,
                    "settlement_status": "settled",
                    "revision_chain_complete": revision_chain_complete,
                    "promotion_evidence_eligible": promotion_evidence_eligible,
                    "evidence_scope": evidence_scope,
                    "return_20d_net": selected_return + pool_index * 0.00001 if is_selected else -0.004,
                    "benchmark_return_20d": 0.003,
                    "active_invested_weight": 0.0,
                    "new_invested_weight": round(selected_count * 0.05, 12),
                    "portfolio_invested_weight": round(selected_count * 0.05, 12),
                    "cash_weight": round(1.0 - selected_count * 0.05, 12),
                }
            )
    return pd.DataFrame(rows)


def _portfolio_daily(ledger: pd.DataFrame, *, final_2026_negative: bool = False) -> pd.DataFrame:
    dates = sorted(
        set(
            ledger.loc[
                ledger["signal_date"].astype(str).between("20250101", "20261231"),
                "signal_date",
            ].astype(str)
        )
    )
    rows = []
    strategy_nav = 1.0
    benchmark_nav = 1.0
    ledger_hash = eqde.promotion_ledger_hash(ledger)
    for date in dates:
        strategy_return = -0.004 if final_2026_negative and date.startswith("2026") else 0.004
        benchmark_return = 0.001
        strategy_nav *= 1.0 + strategy_return
        benchmark_nav *= 1.0 + benchmark_return
        rows.append(
            {
                "trade_date": date,
                "strategy_id": strategy.STRATEGY_ID,
                "strategy_version": strategy.STRATEGY_VERSION,
                "source_ledger_hash": ledger_hash,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "strategy_nav": strategy_nav,
                "benchmark_nav": benchmark_nav,
                "active_position_count": 10,
                "cash_weight": 0.5,
                "used_proxy": False,
                "completeness_status": "complete",
                "promotion_evidence_clean": True,
            }
        )
    return pd.DataFrame(rows)


class EventQualityDriftEvaluatorTests(unittest.TestCase):
    def test_pending_rows_enforce_snapshot_contract_and_are_idempotent(self):
        rows = eqde.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows["settlement_status"]), {"pending"})
        self.assertEqual(set(rows["main_holding_period_days"]), {20})
        self.assertEqual(set(rows["auxiliary_holding_period_days"]), {"[40]"})
        self.assertEqual(set(rows["publish_mode"]), {"observe_only"})
        self.assertEqual(set(rows["rank_change"]), {0})
        self.assertTrue(rows["revision_chain_complete"].all())
        self.assertEqual(set(rows["evidence_scope"]), {"promotion_evidence"})
        self.assertEqual(set(rows["benchmark"]), {"all_a_tradable_equal_weight"})
        self.assertTrue(rows["return_20d_net"].isna().all())
        self.assertTrue(rows["return_40d_net"].isna().all())
        self.assertEqual(set(rows["exit_20d_trade_date"]), {OPEN_DATES_2025[21]})
        self.assertEqual(set(rows["exit_40d_trade_date"]), {OPEN_DATES_2025[41]})

        again = eqde.pending_rows_from_snapshot(_snapshot(), existing=rows, open_trade_dates=OPEN_DATES_2025)
        self.assertEqual(len(again), 2)

        mutated = _snapshot()
        mutated["candidates"][0]["weight"] = 0.06
        mutated["candidates"][1]["weight"] = 0.04
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.pending_rows_from_snapshot(mutated, existing=rows, open_trade_dates=OPEN_DATES_2025)

    def test_snapshot_contract_rejects_proxy_rank_changes_and_ai_candidate_override(self):
        proxy = _snapshot()
        proxy["used_proxy"] = True
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.pending_rows_from_snapshot(proxy, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

        changed = _snapshot()
        changed["rank_change"] = 1
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.pending_rows_from_snapshot(changed, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

        wrong_id = _snapshot()
        wrong_id["strategy_id"] = "other_strategy"
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.pending_rows_from_snapshot(wrong_id, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

        same_day = _snapshot()
        same_day["planned_entry_time"] = "2025-01-03T09:30:00+08:00"
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.pending_rows_from_snapshot(same_day, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

        ai_override = copy.deepcopy(_snapshot())
        ai_override["ai_policy"]["may_change_rank"] = True
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.pending_rows_from_snapshot(ai_override, existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

    def test_weekend_announcement_and_cash_weight_are_valid(self):
        weekend = _snapshot("20250104")
        weekend["planned_entry_time"] = "2025-01-06T09:30:00+08:00"
        weekend["candidates"][0]["weight"] = 0.05
        weekend["candidates"][1]["weight"] = 0.05
        weekend["new_invested_weight"] = 0.10
        weekend["portfolio_invested_weight"] = 0.10
        weekend["cash_weight"] = 0.90
        rows = eqde.pending_rows_from_snapshot(
            weekend,
            existing=pd.DataFrame(),
            open_trade_dates=OPEN_DATES_2025,
        )
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows["weight"].sum(), 0.10)
        self.assertEqual(set(rows["entry_trade_date"]), {"20250106"})

    def test_settlement_uses_t_plus_one_open_and_20_40_trade_day_closes(self):
        ledger = eqde.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)

        settled = eqde.settle_ledger(
            ledger,
            prices=_price_panel(),
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[41],
        )

        self.assertEqual(set(settled["settlement_status"]), {"settled"})
        self.assertTrue((settled["entry_trade_date"] == OPEN_DATES_2025[2]).all())
        self.assertTrue((settled["exit_20d_trade_date"] == OPEN_DATES_2025[21]).all())
        self.assertTrue((settled["exit_40d_trade_date"] == OPEN_DATES_2025[41]).all())
        self.assertTrue((settled["return_20d_net"] - 0.197).abs().max() < 1e-12)
        self.assertTrue((settled["return_20d_stress"] - 0.195).abs().max() < 1e-12)
        self.assertTrue((settled["benchmark_return_20d"] - 0.10).abs().max() < 1e-12)
        self.assertTrue((settled["excess_return_20d"] - 0.097).abs().max() < 1e-12)
        self.assertTrue((settled["return_40d_net"] - 0.297).abs().max() < 1e-12)
        self.assertTrue((settled["benchmark_return_40d"] - 0.15).abs().max() < 1e-12)
        self.assertTrue((settled["excess_return_40d"] - 0.147).abs().max() < 1e-12)

    def test_missing_primary_price_stays_pending_then_becomes_data_missing_with_nulls(self):
        ledger = eqde.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)
        prices = _price_panel()
        missing_20 = prices[
            ~(
                (prices["trade_date"] == OPEN_DATES_2025[21])
                & (prices["ts_code"].isin(["000001.SZ", "600000.SH"]))
            )
        ].copy()

        pending = eqde.settle_ledger(
            ledger,
            prices=missing_20,
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[20],
        )
        self.assertEqual(set(pending["settlement_status"]), {"pending"})
        self.assertTrue(pending["return_40d_net"].isna().all())

        missing = eqde.settle_ledger(
            ledger,
            prices=missing_20,
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[21],
        )
        self.assertEqual(set(missing["settlement_status"]), {"data_missing"})
        self.assertTrue(missing["return_20d_net"].isna().all())
        self.assertTrue(missing["return_40d_net"].isna().all())
        self.assertFalse(((missing["return_20d_net"] == -1.0) | (missing["return_40d_net"] == -1.0)).any())

    def test_primary_20d_settles_before_auxiliary_40d_and_auxiliary_backfills_later(self):
        ledger = eqde.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)
        through_20 = _price_panel()[lambda frame: frame["trade_date"] <= OPEN_DATES_2025[21]].copy()
        primary = eqde.settle_ledger(
            ledger,
            prices=through_20,
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[21],
        )
        self.assertEqual(set(primary["settlement_status"]), {"settled"})
        self.assertTrue(primary["return_20d_net"].notna().all())
        self.assertTrue(primary["return_40d_net"].isna().all())

        auxiliary = eqde.settle_ledger(
            primary,
            prices=_price_panel(),
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[41],
        )
        self.assertTrue(auxiliary["return_40d_net"].notna().all())

    def test_missing_auxiliary_price_does_not_erase_a_valid_primary_result(self):
        ledger = eqde.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)
        primary = eqde.settle_ledger(
            ledger,
            prices=_price_panel(),
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[21],
        )
        missing_auxiliary = _price_panel()[
            _price_panel()["trade_date"] != OPEN_DATES_2025[41]
        ].copy()
        later = eqde.settle_ledger(
            primary,
            prices=missing_auxiliary,
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[41],
        )
        self.assertEqual(set(later["settlement_status"]), {"settled"})
        self.assertTrue(later["return_20d_net"].notna().all())
        self.assertTrue(later["return_40d_net"].isna().all())
        self.assertEqual(
            set(later["data_missing_reason"]),
            {"auxiliary_40d_qfq_price_or_pit_benchmark_missing"},
        )

    def test_benchmark_fails_closed_when_any_eligible_security_price_is_missing(self):
        ledger = eqde.pending_rows_from_snapshot(_snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025)
        prices = _price_panel()
        incomplete_benchmark = prices[
            ~(
                (prices["trade_date"] == OPEN_DATES_2025[21])
                & (prices["ts_code"] == "000002.SZ")
            )
        ].copy()
        result = eqde.settle_ledger(
            ledger,
            prices=incomplete_benchmark,
            pit_universe=_pit_universe(),
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[21],
        )
        self.assertEqual(set(result["settlement_status"]), {"data_missing"})
        self.assertTrue(result["benchmark_return_20d"].isna().all())

    def test_persistent_portfolio_daily_evidence_marks_overlapping_book_and_costs(self):
        ledger = eqde.pending_rows_from_snapshot(
            _snapshot(), existing=pd.DataFrame(), open_trade_dates=OPEN_DATES_2025
        )
        prices, universe = _daily_mark_to_market_inputs()

        daily = eqde.build_persistent_portfolio_daily_evidence(
            ledger,
            prices=prices,
            pit_universe=universe,
            open_trade_dates=OPEN_DATES_2025,
            as_of_date=OPEN_DATES_2025[21],
        )

        self.assertEqual(len(daily), 20)
        self.assertEqual(daily.iloc[0]["active_position_count"], 2)
        self.assertEqual(daily.iloc[-1]["active_position_count"], 0)
        self.assertAlmostEqual(float(daily.iloc[-1]["strategy_nav"]), 1.0197, places=10)
        self.assertEqual(
            set(daily["source_ledger_hash"]), {eqde.promotion_ledger_hash(ledger)}
        )
        self.assertTrue(daily["promotion_evidence_clean"].all())

        missing = prices[
            ~(
                (prices["trade_date"] == OPEN_DATES_2025[10])
                & (prices["ts_code"] == "000001.SZ")
            )
        ]
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.build_persistent_portfolio_daily_evidence(
                ledger,
                prices=missing,
                pit_universe=universe,
                open_trade_dates=OPEN_DATES_2025,
                as_of_date=OPEN_DATES_2025[21],
            )

    def test_promotion_requires_two_positive_periods_span_sample_size_and_randomized_edge(self):
        ledger = _promotion_rows(include_research_2024=True)

        verdict = eqde.evaluate_event_quality_drift_promotion(
            ledger,
            portfolio_daily=_portfolio_daily(ledger),
        )

        self.assertTrue(verdict["all_gates_pass"])
        self.assertEqual(verdict["decision"], "promotion_gate_passed_observe_only")
        self.assertEqual(verdict["execution_authority"], "observe_only_no_auto_order")
        self.assertGreaterEqual(verdict["valid_announcement_events"], 100)
        self.assertGreaterEqual(verdict["sample_months"], 12)
        self.assertEqual(verdict["research_period_rows_ignored"], 10)
        self.assertLess(verdict["random_ranking_test"]["p_value_absolute"], 0.05)
        self.assertLess(verdict["random_ranking_test"]["p_value_excess"], 0.05)
        self.assertGreater(verdict["random_ranking_test"]["eligible_cross_sections"], 0)
        self.assertGreater(verdict["segments"]["frozen_2025"]["net_absolute_return"], 0.0)
        self.assertGreater(verdict["segments"]["frozen_2025"]["net_excess_return"], 0.0)
        self.assertLessEqual(verdict["segments"]["frozen_2025"]["maximum_drawdown"], 0.12)
        self.assertGreater(verdict["segments"]["final_2026"]["net_absolute_return"], 0.0)
        self.assertGreater(verdict["segments"]["final_2026"]["net_excess_return"], 0.0)
        self.assertLessEqual(verdict["segments"]["final_2026"]["maximum_drawdown"], 0.12)

    def test_promotion_fails_for_negative_or_concentrated_results_and_rejects_version_mix(self):
        bad = _promotion_rows(concentrated=True, final_2026_negative=True)
        verdict = eqde.evaluate_event_quality_drift_promotion(
            bad,
            portfolio_daily=_portfolio_daily(bad, final_2026_negative=True),
        )
        self.assertFalse(verdict["all_gates_pass"])
        self.assertIn("final_2026_positive_net_absolute_return", verdict["failed_gates"])
        self.assertIn("final_2026_positive_net_excess_return", verdict["failed_gates"])
        self.assertIn("industry_concentration", verdict["failed_gates"])
        self.assertIn("stock_concentration", verdict["failed_gates"])

        mixed = pd.concat(
            [
                _promotion_rows(),
                _promotion_rows(strategy_version="9.9.9", revision_chain_complete=True),
            ],
            ignore_index=True,
        )
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.evaluate_event_quality_drift_promotion(
                mixed,
                portfolio_daily=_portfolio_daily(mixed),
            )

        auxiliary_only = _promotion_rows(revision_chain_complete=False, promotion_evidence_eligible=False)
        blocked = eqde.evaluate_event_quality_drift_promotion(
            auxiliary_only,
            portfolio_daily=_portfolio_daily(auxiliary_only),
        )
        self.assertFalse(blocked["all_gates_pass"])
        self.assertIn("minimum_100_valid_announcement_events", blocked["failed_gates"])

    def test_random_ranking_gate_rejects_a_pool_with_no_ranking_edge(self):
        no_edge = _promotion_rows()
        no_edge["return_20d_net"] = 0.01
        verdict = eqde.evaluate_event_quality_drift_promotion(
            no_edge,
            permutation_trials=256,
            portfolio_daily=_portfolio_daily(no_edge),
        )
        self.assertFalse(verdict["all_gates_pass"])
        self.assertEqual(verdict["random_ranking_test"]["p_value_absolute"], 1.0)
        self.assertEqual(verdict["random_ranking_test"]["p_value_excess"], 1.0)
        self.assertIn(
            "random_ranking_absolute_better_than_random",
            verdict["failed_gates"],
        )

    def test_promotion_cannot_pass_without_matching_persistent_book_mark_to_market(self):
        ledger = _promotion_rows()
        blocked = eqde.evaluate_event_quality_drift_promotion(
            ledger,
            permutation_trials=64,
        )
        self.assertFalse(blocked["all_gates_pass"])
        self.assertIn("persistent_portfolio_mark_to_market_complete", blocked["failed_gates"])

        mismatched = _portfolio_daily(ledger)
        mismatched["source_ledger_hash"] = "wrong-ledger"
        with self.assertRaises(eqde.EvaluationContractError):
            eqde.evaluate_event_quality_drift_promotion(
                ledger,
                portfolio_daily=mismatched,
                permutation_trials=64,
            )


if __name__ == "__main__":
    unittest.main()
