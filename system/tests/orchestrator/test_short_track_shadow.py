#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import short_track_shadow as sts  # noqa: E402
import shadow_portfolio_evaluator as spe  # noqa: E402


def make_control_rows():
    rows = []
    industries = ["银行", "电子", "汽车", "医药", "软件"]
    for rank in range(1, 21):
        rows.append(
            {
                "ts_code": f"{rank:06d}.SZ",
                "stock_code": f"{rank:06d}",
                "name": f"样本{rank}",
                "industry_name": industries[(rank - 1) % len(industries)],
                "score": 100 - rank,
                "rank": rank,
                "rank_no": rank,
                "open_qfq": 10.0 + rank / 100,
                "close_qfq": 10.2 + rank / 100,
                "high_qfq": 10.3 + rank / 100,
                "low_qfq": 9.9 + rank / 100,
                "macd_dif": 0.05,
                "macd_dea": 0.01,
                "volume_ratio": 1.2,
                "turnover_rate": 1.0,
                "ret_5d": 2.0,
                "ret_20d": 5.0,
                "amount": 1000000.0 + rank,
                "circ_mv": 1000.0 + rank,
                "total_mv": 1200.0 + rank,
                "listing_days": 500,
                "list_status": "L",
                "is_st": False,
                "is_suspended": False,
                "sw2021_l1_name": industries[(rank - 1) % len(industries)],
            }
        )
    return rows


def make_balanced_frame():
    rows = []
    for idx in range(1, 22):
        rows.append(
            {
                "ts_code": f"{idx:06d}.SZ",
                "stock_code": f"{idx:06d}",
                "name": f"平衡{idx}",
                "industry_name": "电子" if idx <= 10 else "医药",
                "sw2021_l1_name": "电子" if idx <= 10 else "医药",
                "open_qfq": 10.0 + idx / 10,
                "close_qfq": 10.2 + idx / 10,
                "high_qfq": 10.4 + idx / 10,
                "low_qfq": 9.9 + idx / 10,
                "macd_dif": 0.01 * idx,
                "macd_dea": 0.008 * idx,
                "volume_ratio": 1.0 + idx / 50,
                "turnover_rate": 0.8 + idx / 100,
                "ret_5d": idx / 10,
                "ret_20d": idx / 5,
                "amount": 1000000.0 + idx * 1000,
                "circ_mv": 500.0 + idx * 10,
                "total_mv": 600.0 + idx * 12,
                "listing_days": 400,
                "list_status": "L",
                "is_st": False,
                "is_suspended": False,
                "universe_flag": 1,
                "used_proxy": False,
                "completeness": "complete",
                "trade_date": "20260811",
                "realized_vol_5d": 0.01 + idx / 10000,
                "realized_vol_20d": 0.03 + idx / 10000,
                "macd_hist_prev": 0.001 * idx,
                "volume_cv_20": 0.2 + idx / 1000,
                "turnover_cv_20": 0.15 + idx / 1000,
                "amount_ma20": 900000.0 + idx * 1000,
                "max_abs_return_20": 0.05 + idx / 10000,
            }
        )
    return pd.DataFrame(rows)


def make_balanced_history_inputs():
    dates = [date.strftime("%Y%m%d") for date in pd.bdate_range(end="2026-08-11", periods=21)]
    price_rows = []
    basic_rows = []
    pit_rows = []
    for stock_index in range(1, 26):
        code = f"{stock_index:06d}.SZ"
        for day_index, trade_date in enumerate(dates):
            close = 10.0 + stock_index / 10 + day_index / 100
            price_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "open_qfq": close * 0.995,
                    "high_qfq": close * 1.01,
                    "low_qfq": close * 0.99,
                    "close_qfq": close,
                    "macd_dif": 0.01 * stock_index + day_index / 10000,
                    "macd_dea": 0.008 * stock_index,
                    "vol": 100000.0 + stock_index * 100 + day_index * 10,
                    "amount": 1000000.0 + stock_index * 1000 + day_index * 100,
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
            basic_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": code,
                    "turnover_rate": 0.8 + stock_index / 100 + day_index / 1000,
                    "circ_mv": 500.0 + stock_index * 10,
                    "total_mv": 600.0 + stock_index * 12,
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
        pit_rows.append(
            {
                "trade_date": "20260811",
                "ts_code": code,
                "name": f"历史{stock_index}",
                "industry_name": "电子" if stock_index <= 13 else "医药",
                "sw2021_l1_name": "电子" if stock_index <= 13 else "医药",
                "listing_days": 500,
                "list_status_at_date": "L",
                "is_st": False,
                "is_suspended": False,
                "universe_flag": 1,
                "used_proxy": False,
                "completeness": "complete",
            }
        )
    return pd.DataFrame(price_rows), pd.DataFrame(basic_rows), pd.DataFrame(pit_rows)


class ShortTrackShadowTests(unittest.TestCase):
    def test_registry_exposes_exact_ids(self):
        self.assertEqual(
            list(sts.STRATEGY_REGISTRY.keys()),
            [
                "prebreakout_v43_control",
                "prebreakout_v43_top15",
                "prebreakout_v44_balanced",
            ],
        )

    def test_control_snapshot_preserves_order_and_contract(self):
        snapshot = sts.build_control_candidate_snapshot(
            make_control_rows(),
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
            health_payload={"cyq_perf_proxy_derived": False},
        )
        self.assertEqual(snapshot["strategy_id"], "prebreakout_v43_control")
        self.assertEqual([row["rank_no"] for row in snapshot["rows"][:5]], [1, 2, 3, 4, 5])
        self.assertEqual(snapshot["artifact_type"], "candidate_snapshot")
        self.assertTrue(snapshot["observe_only"])
        self.assertEqual(snapshot["planned_entry_time"], "2026-08-12T09:30:00+08:00")
        self.assertEqual(snapshot["holding_period_days"], 5)
        self.assertEqual(snapshot["diagnostic_holding_period_days"], [1, 3])
        self.assertEqual(snapshot["signal_date"], "20260811")
        self.assertEqual(snapshot["publish_mode"], "observe_only")
        self.assertEqual(snapshot["rank_change"], 0)
        self.assertEqual(snapshot["round_trip_cost"], 0.003)
        self.assertEqual(snapshot["stress_round_trip_cost"], 0.005)
        self.assertEqual(len(snapshot["candidates"]), 20)
        self.assertAlmostEqual(sum(row["weight"] for row in snapshot["candidates"]), 1.0)

    def test_control_rejects_proxy_and_config_drift(self):
        with self.assertRaises(sts.ShortTrackInputError):
            sts.build_control_candidate_snapshot(
                make_control_rows(),
                trade_date="20260811",
                signal_cutoff="2026-08-11T15:00:00+08:00",
                exchange_trade_dates=["20260811", "20260812"],
                health_payload={"cyq_perf_proxy_derived": True},
            )
        with self.assertRaises(sts.ShortTrackInputError):
            sts.validate_control_config({"version": "4.3", "config_hash": "deadbeef"})

    def test_top15_enforces_industry_cap(self):
        rows = make_control_rows()
        for idx, row in enumerate(rows, start=1):
            row["industry_name"] = "银行" if idx <= 10 else "电子"
            row["sw2021_l1_name"] = row["industry_name"]
        snapshot = sts.build_top15_candidate_snapshot(
            rows,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
            health_payload={"cyq_perf_proxy_derived": False},
        )
        industries = [row["industry_name"] for row in snapshot["rows"]]
        self.assertLessEqual(industries.count("银行"), 3)
        self.assertLessEqual(industries.count("电子"), 3)
        self.assertLessEqual(len(snapshot["rows"]), 15)

    def test_top15_fills_from_full_ranked_pool_beyond_control_top20(self):
        rows = make_control_rows()
        for row in rows:
            row["industry_name"] = "银行"
            row["sw2021_l1_name"] = "银行"
        for rank in range(21, 41):
            row = dict(rows[-1])
            row.update(
                {
                    "ts_code": f"{rank:06d}.SZ",
                    "stock_code": f"{rank:06d}",
                    "name": f"样本{rank}",
                    "rank": rank,
                    "rank_no": rank,
                    "industry_name": f"行业{(rank - 21) // 3}",
                    "sw2021_l1_name": f"行业{(rank - 21) // 3}",
                }
            )
            rows.append(row)
        snapshot = sts.build_top15_candidate_snapshot(
            rows,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
            health_payload={"cyq_perf_proxy_derived": False},
        )
        self.assertEqual(len(snapshot["candidates"]), 15)
        self.assertTrue(any(int(row["source_rank"]) > 20 for row in snapshot["candidates"]))

    def test_target_date_sw_industry_is_authoritative_for_caps_and_contract(self):
        rows = make_control_rows()
        for rank in range(21, 41):
            row = dict(rows[-1])
            row.update(
                {
                    "ts_code": f"{rank:06d}.SZ",
                    "stock_code": f"{rank:06d}",
                    "name": f"样本{rank}",
                    "rank": rank,
                    "rank_no": rank,
                }
            )
            rows.append(row)
        for index, row in enumerate(rows):
            row["industry_name"] = "旧行业"
            row["sw2021_l1_name"] = f"SW{index // 3}"
        top15 = sts.build_top15_candidate_snapshot(
            rows,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
            health_payload={"cyq_perf_proxy_derived": False},
        )
        self.assertEqual(len(top15["candidates"]), 15)
        self.assertTrue(all(row["industry"].startswith("SW") for row in top15["candidates"]))
        self.assertTrue(all(row["industry_name"].startswith("SW") for row in top15["candidates"]))
        self.assertTrue(all(row["source_industry_name"] == "旧行业" for row in top15["candidates"]))

        balanced_frame = make_balanced_frame()
        balanced_frame["industry_name"] = "旧行业"
        balanced_frame["sw2021_l1_name"] = [
            "电子" if index < 10 else "医药" for index in range(len(balanced_frame))
        ]
        balanced = sts.build_balanced_candidate_snapshot(
            balanced_frame,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
        )
        self.assertIn(balanced["candidates"][0]["industry"], {"电子", "医药"})
        self.assertNotEqual(balanced["candidates"][0]["industry"], "旧行业")

    def test_balanced_requires_pit_and_qfq_and_equal_category_weights(self):
        frame = make_balanced_frame()
        snapshot = sts.build_balanced_candidate_snapshot(
            frame,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
        )
        self.assertEqual(snapshot["category_weights"], {name: 0.2 for name in sts.BALANCED_CATEGORY_NAMES})
        self.assertEqual(snapshot["strategy_id"], "prebreakout_v44_balanced")
        self.assertTrue(snapshot["observe_only"])
        self.assertEqual(snapshot["rows"][0]["rank_change"], 0)
        self.assertEqual(len(snapshot["candidates"]), 20)
        self.assertAlmostEqual(sum(row["weight"] for row in snapshot["candidates"]), 1.0)

        broken = frame.drop(columns=["open_qfq"])
        with self.assertRaises(sts.ShortTrackInputError):
            sts.build_balanced_candidate_snapshot(
                broken,
                trade_date="20260811",
                signal_cutoff="2026-08-11T15:00:00+08:00",
                exchange_trade_dates=["20260811", "20260812"],
            )

    def test_balanced_rejects_chip_fields_and_is_deterministic(self):
        frame = make_balanced_frame()
        chipy = frame.copy()
        chipy["winner_rate"] = 50.0
        with self.assertRaises(sts.ShortTrackInputError):
            sts.build_balanced_candidate_snapshot(
                chipy,
                trade_date="20260811",
                signal_cutoff="2026-08-11T15:00:00+08:00",
                exchange_trade_dates=["20260811", "20260812"],
            )

        tied = frame.copy()
        tied["ret_5d"] = 1.0
        tied["ret_20d"] = 2.0
        first = sts.build_balanced_candidate_snapshot(
            tied,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
        )
        second = sts.build_balanced_candidate_snapshot(
            tied.sample(frac=1.0, random_state=42),
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
        )
        self.assertEqual(
            [row["ts_code"] for row in first["rows"]],
            [row["ts_code"] for row in second["rows"]],
        )

    def test_percentile_direction_rewards_stronger_signal(self):
        scores = sts._score_percentile(
            pd.Series([1.0, 2.0]),
            ascending=False,
            ts_codes=pd.Series(["000001.SZ", "000002.SZ"]),
        )
        self.assertGreater(scores.iloc[1], scores.iloc[0])

    def test_balanced_filters_nontradable_rows_and_rejects_proxy_metadata(self):
        frame = make_balanced_frame()
        frame.loc[0, "universe_flag"] = 0
        frame.loc[0, "is_st"] = True
        snapshot = sts.build_balanced_candidate_snapshot(
            frame,
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812"],
        )
        self.assertNotIn(frame.loc[0, "ts_code"], [row["ts_code"] for row in snapshot["candidates"]])

        proxy = make_balanced_frame()
        proxy.loc[0, "used_proxy"] = True
        with self.assertRaises(sts.ShortTrackInputError):
            sts.build_balanced_candidate_snapshot(
                proxy,
                trade_date="20260811",
                signal_cutoff="2026-08-11T15:00:00+08:00",
                exchange_trade_dates=["20260811", "20260812"],
            )

    def test_candidate_contract_enters_forward_ledger_without_translation(self):
        snapshots = sts.build_short_track_candidate_snapshots(
            control_rows=make_control_rows(),
            balanced_frame=make_balanced_frame(),
            trade_date="20260811",
            signal_cutoff="2026-08-11T15:00:00+08:00",
            exchange_trade_dates=["20260811", "20260812", "20260813", "20260814", "20260817", "20260818"],
            health_payload={"cyq_perf_proxy_derived": False},
        )
        for strategy_id, snapshot in snapshots.items():
            ledger = spe.pending_rows_from_snapshot(
                snapshot,
                existing=pd.DataFrame(),
                open_trade_dates=["20260811", "20260812", "20260813", "20260814", "20260817", "20260818"],
            )
            self.assertEqual(set(ledger["strategy_id"]), {strategy_id})
            self.assertEqual(set(ledger["publish_mode"]), {"observe_only"})
            self.assertTrue(ledger["return_5d_net"].isna().all())

    def test_balanced_feature_builder_uses_only_qfq_history_and_pit_state(self):
        prices, daily_basic, pit = make_balanced_history_inputs()
        features = sts.build_balanced_feature_frame(
            price_history=prices,
            daily_basic_history=daily_basic,
            pit_universe=pit,
            trade_date="20260811",
        )
        self.assertEqual(len(features), 25)
        self.assertTrue(sts.REQUIRED_BALANCED_COLUMNS.issubset(features.columns))
        self.assertFalse(any("chip" in col.lower() or "cyq" in col.lower() for col in features.columns))
        self.assertTrue((features["trade_date"] == "20260811").all())
        self.assertTrue((features["realized_vol_20d"] >= features["realized_vol_5d"] * 0).all())

        future = prices.iloc[[0]].copy()
        future["trade_date"] = "20260812"
        future["close_qfq"] = 9999.0
        with_future = sts.build_balanced_feature_frame(
            price_history=pd.concat([prices, future], ignore_index=True),
            daily_basic_history=daily_basic,
            pit_universe=pit,
            trade_date="20260811",
        )
        pd.testing.assert_frame_equal(features, with_future)

        proxy = daily_basic.copy()
        proxy.loc[0, "used_proxy"] = True
        with self.assertRaises(sts.ShortTrackInputError):
            sts.build_balanced_feature_frame(
                price_history=prices,
                daily_basic_history=proxy,
                pit_universe=pit,
                trade_date="20260811",
            )


if __name__ == "__main__":
    unittest.main()
