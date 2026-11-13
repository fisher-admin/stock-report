#!/usr/bin/env python3
from __future__ import annotations

import copy
import unittest

import pandas as pd

import event_quality_drift_v1 as eqd


def make_inputs(target_date: str = "20250103"):
    event_rows = []
    quality_rows = []
    valuation_rows = []
    universe_rows = []
    industries = ["电子", "医药", "银行", "机械", "消费", "公用事业"]
    for index in range(1, 31):
        symbol = f"{index:06d}"
        code = f"{symbol}.SZ"
        previous_growth = 5.0 + index
        previous_forecast = 100.0 + index
        for field, value in (("np_growth_pct", previous_growth), ("np_forecast", previous_forecast)):
            event_rows.append(
                {
                    "symbol": symbol,
                    "field": field,
                    "period": 20240930,
                    "value": value,
                    "announce_date": 20241020,
                    "available_at": "2024-10-20T00:00:00+08:00",
                    "revision_seq": 0,
                    "source": "pit_yjyg",
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
        current_growth = previous_growth + 10.0 + index / 10
        current_forecast = previous_forecast + 30.0
        for field, value in (("np_growth_pct", current_growth), ("np_forecast", current_forecast)):
            event_rows.append(
                {
                    "symbol": symbol,
                    "field": field,
                    "period": 20241231,
                    "value": value,
                    "announce_date": int(target_date),
                    "available_at": f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}T00:00:00+08:00",
                    "revision_seq": 0,
                    "source": "pit_yjyg",
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
        quality_rows.extend(
            [
                {
                    "ts_code": code,
                    "ann_date": "20241031",
                    "end_date": "20240930",
                    "roe": 5.0 + index / 5,
                    "grossprofit_margin": 20.0 + index / 10,
                    "debt_to_assets": 60.0 - index / 5,
                    "used_proxy": False,
                    "completeness": "complete",
                },
                {
                    "ts_code": code,
                    "ann_date": "20250110",
                    "end_date": "20241231",
                    "roe": 99.0,
                    "grossprofit_margin": 99.0,
                    "debt_to_assets": 1.0,
                    "used_proxy": False,
                    "completeness": "complete",
                },
            ]
        )
        valuation_rows.append(
            {
                "ts_code": code,
                "trade_date": "20250102",
                "pe_ttm": 8.0 + index / 10,
                "pb": 0.8 + index / 100,
                "circ_mv": 1000.0 + index * 50,
                "used_proxy": False,
                "completeness": "complete",
            }
        )
        universe_rows.append(
            {
                "ts_code": code,
                "trade_date": "20250103",
                "name": f"事件{index}",
                "sw2021_l1_name": industries[(index - 1) % len(industries)],
                "universe_flag": 1,
                "tradable": 1,
                "is_st": False,
                "is_suspended": False,
                "listing_days": 500,
                "used_proxy": False,
                "completeness": "complete",
            }
        )
    # Old event in the 120-day window: it is context only and must not become a
    # target-date candidate.
    event_rows.append(
        {
            "symbol": "999999",
            "field": "np_growth_pct",
            "period": 20240930,
            "value": 500.0,
            "announce_date": 20241201,
            "available_at": "2024-12-01T00:00:00+08:00",
            "revision_seq": 0,
            "source": "pit_yjyg",
            "used_proxy": False,
            "completeness": "complete",
        }
    )
    return (
        pd.DataFrame(event_rows),
        pd.DataFrame(quality_rows),
        pd.DataFrame(valuation_rows),
        pd.DataFrame(universe_rows),
    )


OPEN_DATES = ["20250102", "20250103", "20250106", "20250107", "20250108"]


class EventQualityDriftTests(unittest.TestCase):
    def test_builds_only_new_announcement_candidates_with_fixed_contract(self):
        events, quality, valuation, universe = make_inputs()
        snapshot = eqd.build_event_quality_drift_snapshot(
            pit_events=events,
            quality_history=quality,
            valuation_history=valuation,
            pit_universe=universe,
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=True,
        )

        self.assertEqual(snapshot["strategy_id"], "event_quality_drift_v1")
        self.assertEqual(snapshot["factor_weights"], {
            "forecast_or_revision": 0.35,
            "profit_acceleration": 0.25,
            "quality": 0.20,
            "valuation_protection": 0.20,
        })
        self.assertEqual(snapshot["planned_entry_time"], "2025-01-06T09:30:00+08:00")
        self.assertEqual(snapshot["holding_period_days"], 20)
        self.assertEqual(snapshot["auxiliary_holding_period_days"], [40])
        self.assertEqual(snapshot["publish_mode"], "observe_only")
        self.assertEqual(snapshot["rank_change"], 0)
        self.assertFalse(snapshot["ai_policy"]["may_change_rank"])
        self.assertNotIn("999999", {row["ts_code"].split(".")[0] for row in snapshot["ranked_events"]})
        self.assertLessEqual(len(snapshot["candidates"]), 20)
        self.assertLessEqual(max(row["weight"] for row in snapshot["candidates"]), 0.075)
        industry_weights = {}
        for row in snapshot["candidates"]:
            industry_weights[row["industry"]] = industry_weights.get(row["industry"], 0.0) + row["weight"]
        self.assertLessEqual(max(industry_weights.values()), 0.25 + 1e-12)

    def test_future_quality_and_future_events_cannot_change_target_snapshot(self):
        events, quality, valuation, universe = make_inputs()
        first = eqd.build_event_quality_drift_snapshot(
            pit_events=events,
            quality_history=quality,
            valuation_history=valuation,
            pit_universe=universe,
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=True,
        )
        future_event = events.iloc[[0]].copy()
        future_event["symbol"] = "000001"
        future_event["period"] = 20250331
        future_event["announce_date"] = 20250401
        future_event["available_at"] = "2025-04-01T00:00:00+08:00"
        future_event["value"] = 999999.0
        second = eqd.build_event_quality_drift_snapshot(
            pit_events=pd.concat([events, future_event], ignore_index=True),
            quality_history=quality,
            valuation_history=valuation,
            pit_universe=universe,
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=True,
        )
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(first["candidates"], second["candidates"])

    def test_active_book_caps_new_entries_across_announcement_dates(self):
        events, quality, valuation, universe = make_inputs()
        active_positions = [
            {
                "ts_code": f"9{index:05d}.SZ",
                "industry": f"存量行业{index % 6}",
                "weight": 0.05,
            }
            for index in range(18)
        ]
        snapshot = eqd.build_event_quality_drift_snapshot(
            pit_events=events,
            quality_history=quality,
            valuation_history=valuation,
            pit_universe=universe,
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=True,
            active_positions=active_positions,
        )

        self.assertEqual(snapshot["active_position_count"], 18)
        self.assertEqual(len(snapshot["candidates"]), 2)
        self.assertEqual(snapshot["portfolio_position_count"], 20)
        self.assertAlmostEqual(snapshot["active_invested_weight"], 0.90)
        self.assertAlmostEqual(snapshot["new_invested_weight"], 0.10)
        self.assertAlmostEqual(snapshot["portfolio_invested_weight"], 1.0)
        self.assertAlmostEqual(snapshot["cash_weight"], 0.0)

        full_book = active_positions + [
            {"ts_code": "980001.SZ", "industry": "存量行业A", "weight": 0.05},
            {"ts_code": "980002.SZ", "industry": "存量行业B", "weight": 0.05},
        ]
        blocked = eqd.build_event_quality_drift_snapshot(
            pit_events=events,
            quality_history=quality,
            valuation_history=valuation,
            pit_universe=universe,
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=True,
            active_positions=full_book,
        )
        self.assertEqual(blocked["candidates"], [])
        self.assertEqual(blocked["portfolio_position_count"], 20)
        self.assertAlmostEqual(blocked["cash_weight"], 0.0)

    def test_ai_can_only_attach_timed_explanations_without_changing_selection(self):
        args = make_inputs()
        snapshot = eqd.build_event_quality_drift_snapshot(
            pit_events=args[0],
            quality_history=args[1],
            valuation_history=args[2],
            pit_universe=args[3],
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=True,
        )
        before = [(row["ts_code"], row["rank"], row["score"]) for row in snapshot["candidates"]]
        first_code = snapshot["candidates"][0]["ts_code"]
        attached = eqd.attach_ai_explanations(
            snapshot,
            {
                first_code: {
                    "ai_evidence_time": "2025-01-06T08:00:00+08:00",
                    "ai_risk_tags": ["盈利修订风险"],
                    "ai_explanation": "公告证据与量化因子方向一致，但需核对修订链。",
                }
            },
        )
        after = [(row["ts_code"], row["rank"], row["score"]) for row in attached["candidates"]]
        self.assertEqual(before, after)
        self.assertEqual(attached["rank_change"], 0)
        self.assertEqual(attached["candidates"][0]["rank_change"], 0)
        self.assertEqual(attached["candidates"][0]["ai_risk_tags"], ["盈利修订风险"])

        with self.assertRaises(eqd.EventDataIntegrityError):
            eqd.attach_ai_explanations(
                snapshot,
                {
                    "999999.SZ": {
                        "ai_evidence_time": "2025-01-06T08:00:00+08:00",
                        "ai_risk_tags": [],
                        "ai_explanation": "不得添加候选。",
                    }
                },
            )
        with self.assertRaises(eqd.EventDataIntegrityError):
            eqd.attach_ai_explanations(
                snapshot,
                {
                    first_code: {
                        "ai_evidence_time": "2025-01-06T10:00:00+08:00",
                        "ai_risk_tags": [],
                        "ai_explanation": "成交后生成的证据不可回填。",
                    }
                },
            )

    def test_incomplete_revision_chain_is_auxiliary_only_and_proxy_fails_closed(self):
        events, quality, valuation, universe = make_inputs()
        snapshot = eqd.build_event_quality_drift_snapshot(
            pit_events=events,
            quality_history=quality,
            valuation_history=valuation,
            pit_universe=universe,
            announce_date="20250103",
            exchange_trade_dates=OPEN_DATES,
            revision_chain_complete=False,
        )
        self.assertEqual(snapshot["evidence_scope"], "auxiliary_only")
        self.assertFalse(snapshot["promotion_evidence_eligible"])

        proxy = valuation.copy()
        proxy.loc[0, "used_proxy"] = True
        with self.assertRaises(eqd.EventDataIntegrityError):
            eqd.build_event_quality_drift_snapshot(
                pit_events=events,
                quality_history=quality,
                valuation_history=proxy,
                pit_universe=universe,
                announce_date="20250103",
                exchange_trade_dates=OPEN_DATES,
                revision_chain_complete=True,
            )

    def test_same_inputs_are_deterministic_and_version_is_immutable(self):
        args = make_inputs()
        first = eqd.build_event_quality_drift_snapshot(
            pit_events=args[0], quality_history=args[1], valuation_history=args[2], pit_universe=args[3],
            announce_date="20250103", exchange_trade_dates=OPEN_DATES, revision_chain_complete=True,
        )
        second = eqd.build_event_quality_drift_snapshot(
            pit_events=args[0].sample(frac=1.0, random_state=42),
            quality_history=args[1].sample(frac=1.0, random_state=43),
            valuation_history=args[2].sample(frac=1.0, random_state=44),
            pit_universe=args[3].sample(frac=1.0, random_state=45),
            announce_date="20250103", exchange_trade_dates=list(reversed(OPEN_DATES)), revision_chain_complete=True,
        )
        self.assertEqual(first["strategy_version"], second["strategy_version"])
        self.assertEqual(first["config_hash"], second["config_hash"])
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(first["candidates"], second["candidates"])


if __name__ == "__main__":
    unittest.main()
