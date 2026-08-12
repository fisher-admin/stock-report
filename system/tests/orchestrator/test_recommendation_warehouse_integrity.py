#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import recommendation_warehouse as rw  # noqa: E402


class RecommendationWarehouseIntegrityTests(unittest.TestCase):
    def make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        rw.init_db(conn)
        rw._migrate_forward_return_columns(conn)
        rw._migrate_recommendation_contract_columns(conn)
        return conn

    def make_item(self, **overrides):
        item = {
            "strategy_id": rw.O2C_STRATEGY_ID,
            "strategy_source": "o2c_factor",
            "strategy_name": rw.O2C_STRATEGY_NAME,
            "recommend_date": "20260811",
            "stock_code": "000001",
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "rank_no": 1,
            "recommend_price": 10.0,
            "source_kind": "greenfield_o2c_snapshot",
            "source_path": "/tmp/o2c.json",
            "raw": {
                "signal_data_cutoff": "2026-08-11T15:00:00+08:00",
                "planned_entry_time": "2026-08-12T09:30:00+08:00",
                "holding_period_days": 1,
                "data_sources": ["greenfield_multifactor_panel.parquet"],
                "used_proxy": False,
                "round_trip_cost": 0.003,
                "benchmark": "all_a_tradable_equal_weight",
            },
        }
        item.update(overrides)
        return item

    def test_warehouse_sync_keeps_raw_exports_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            published = root / "stock-report"
            analytics = published / "data/recommendation_analytics"
            published.mkdir()
            summary = root / "summary.json"
            detail = root / "detail.json"
            csv_path = root / "detail.csv"
            summary.write_text("{}", encoding="utf-8")
            detail.write_text('{"rows": [{"stock_code": "000001"}]}', encoding="utf-8")
            csv_path.write_text("stock_code\n000001\n", encoding="utf-8")

            with mock.patch.object(rw, "PUBLISHED_REPO", published):
                result = rw.sync_exports_to_published_repo(
                    "20260811", summary, detail, csv_path
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["publication_mode"], "local_only")
            self.assertFalse(result["raw_data_published"])
            self.assertFalse(analytics.exists())

    def test_prepare_recommendation_fact_record_enforces_new_contract(self):
        prepared = rw.prepare_recommendation_fact_record(self.make_item(), strict=True)
        self.assertEqual(prepared["strategy_version"], rw.O2C_STRATEGY_ID)
        self.assertEqual(prepared["signal_data_cutoff"], "2026-08-11T15:00:00+08:00")
        self.assertEqual(prepared["planned_entry_time"], "2026-08-12T09:30:00+08:00")
        self.assertEqual(prepared["holding_period_days"], 1)
        self.assertEqual(prepared["data_sources"], ["greenfield_multifactor_panel.parquet"])
        self.assertFalse(prepared["used_proxy"])
        self.assertEqual(prepared["benchmark"], "all_a_tradable_equal_weight")
        self.assertEqual(prepared["round_trip_cost"], 0.003)
        self.assertEqual(prepared["rank_change"], 0)

        conn = self.make_conn()
        rw.upsert_recommendations(conn, [prepared])
        row = conn.execute(
            """
            SELECT strategy_version, signal_data_cutoff, planned_entry_time, holding_period_days,
                   data_sources_json, used_proxy, completeness_status, round_trip_cost, benchmark,
                   settlement_status, rank_change
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertEqual(row["strategy_version"], rw.O2C_STRATEGY_ID)
        self.assertEqual(row["signal_data_cutoff"], "2026-08-11T15:00:00+08:00")
        self.assertEqual(row["planned_entry_time"], "2026-08-12T09:30:00+08:00")
        self.assertEqual(row["holding_period_days"], 1)
        self.assertEqual(row["data_sources_json"], '["greenfield_multifactor_panel.parquet"]')
        self.assertEqual(row["used_proxy"], 0)
        self.assertEqual(row["round_trip_cost"], 0.003)
        self.assertEqual(row["benchmark"], "all_a_tradable_equal_weight")
        self.assertEqual(row["settlement_status"], "pending_settlement")
        self.assertEqual(row["rank_change"], 0)

    def test_prepare_recommendation_fact_record_rejects_missing_required_contract_fields(self):
        bad = self.make_item(source_kind="manual_new_record", raw={"planned_entry_time": "2026-08-12T09:30:00+08:00"})
        with self.assertRaises(ValueError):
            rw.prepare_recommendation_fact_record(bad, strict=True)

    def test_prepare_recommendation_fact_record_uses_next_trading_day_not_natural_day(self):
        item = self.make_item(
            strategy_id=rw.TARGET_STRATEGY,
            strategy_source="traditional",
            strategy_name=rw.TARGET_STRATEGY_NAME,
            recommend_date="20260807",
            source_kind="current_strategy_snapshot",
            raw={
                "signal_data_cutoff": "2026-08-07T15:00:00+08:00",
                "holding_period_days": 5,
                "round_trip_cost": 0.003,
                "benchmark": "all_a_tradable_equal_weight",
            },
        )
        prepared = rw.prepare_recommendation_fact_record(
            item,
            strict=True,
            trade_dates=["20260807", "20260810", "20260811"],
        )
        self.assertEqual(prepared["planned_entry_time"], "2026-08-10T09:30:00+08:00")
        self.assertEqual(prepared["strategy_version"], rw.TARGET_STRATEGY_VERSION)
        self.assertEqual(prepared["holding_period_days"], 5)

    def test_prepare_recommendation_fact_record_does_not_fabricate_when_next_trade_day_missing(self):
        item = self.make_item(
            recommend_date="20260811",
            raw={
                "signal_data_cutoff": "2026-08-11T15:00:00+08:00",
                "holding_period_days": 1,
                "data_sources": ["greenfield_multifactor_panel.parquet"],
                "round_trip_cost": 0.003,
                "benchmark": "all_a_tradable_equal_weight",
            },
        )
        with self.assertRaises(ValueError):
            rw.prepare_recommendation_fact_record(item, strict=True, trade_dates=["20260811"])

    def test_current_snapshot_can_migrate_with_honest_defaults(self):
        item = {
            "strategy_id": rw.TARGET_STRATEGY,
            "strategy_source": "traditional",
            "recommend_date": "20260810",
            "stock_code": "600000",
            "ts_code": "600000.SH",
            "stock_name": "浦发银行",
            "rank_no": 1,
            "recommend_price": 12.0,
            "source_kind": "current_strategy_snapshot",
            "source_path": "/tmp/strategy_backtests.json",
            "raw": {
                "signal_data_cutoff": "2026-08-10T15:00:00+08:00",
            },
        }
        prepared = rw.prepare_recommendation_fact_record(item, strict=True, trade_dates=["20260810", "20260811"])
        self.assertEqual(prepared["strategy_name"], rw.TARGET_STRATEGY_NAME)
        self.assertEqual(prepared["data_sources"], ["/tmp/strategy_backtests.json"])
        self.assertEqual(prepared["planned_entry_time"], "2026-08-11T09:30:00+08:00")
        self.assertEqual(prepared["benchmark"], "all_a_tradable_equal_weight")

    def test_current_snapshot_uses_persistent_exchange_calendar_even_when_price_cache_has_no_future_day(self):
        item = {
            "strategy_id": rw.TARGET_STRATEGY,
            "strategy_source": "traditional",
            "recommend_date": "20260810",
            "stock_code": "600000",
            "ts_code": "600000.SH",
            "stock_name": "浦发银行",
            "rank_no": 1,
            "recommend_price": 12.0,
            "source_kind": "current_strategy_snapshot",
            "source_path": "/tmp/strategy_backtests.json",
            "raw": {
                "signal_data_cutoff": "2026-08-10T15:00:00+08:00",
            },
        }
        with mock.patch.object(rw, "load_open_trade_dates", return_value=["20260808", "20260811", "20260812"]):
            prepared = rw.prepare_recommendation_fact_record(
                item,
                strict=True,
                trade_dates=["20260808", "20260810"],
                exchange_trade_dates=["20260808", "20260811", "20260812"],
            )
        self.assertEqual(prepared["planned_entry_time"], "2026-08-11T09:30:00+08:00")

    def test_recompute_metrics_marks_missing_exit_prices_as_pending_or_missing(self):
        conn = self.make_conn()
        prepared = rw.prepare_recommendation_fact_record(self.make_item(), strict=True)
        rw.upsert_recommendations(conn, [prepared])
        conn.execute(
            """
            INSERT INTO price_daily_cache (
                stock_code, ts_code, trade_date, open, high, low, close, pct_chg, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("000001", "000001.SZ", "20260811", 10.0, 10.0, 10.0, 10.0, 0.0, "test.parquet", rw.now_str()),
        )
        conn.commit()

        rw.recompute_metrics(conn, ["20260811"])
        row = conn.execute(
            """
            SELECT next_day_return_pct, cumulative_return_pct, completeness_status, settlement_status
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertIsNone(row["next_day_return_pct"])
        self.assertIsNone(row["cumulative_return_pct"])
        self.assertEqual(row["completeness_status"], "pending_settlement")
        self.assertEqual(row["settlement_status"], "pending_settlement")

        rw.recompute_metrics(conn, ["20260811", "20260812"])
        row = conn.execute(
            """
            SELECT next_day_return_pct, cumulative_return_pct, completeness_status, settlement_status
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertIsNone(row["next_day_return_pct"])
        self.assertIsNone(row["cumulative_return_pct"])
        self.assertEqual(row["completeness_status"], "data_missing")
        self.assertEqual(row["settlement_status"], "data_missing")

    def test_repair_recommendation_metrics_treats_zero_exit_as_missing_data(self):
        conn = self.make_conn()
        prepared = rw.prepare_recommendation_fact_record(self.make_item(), strict=True)
        rw.upsert_recommendations(conn, [prepared])
        conn.executemany(
            """
            INSERT INTO price_daily_cache (
                stock_code, ts_code, trade_date, open, high, low, close, pct_chg,
                open_qfq, close_qfq, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", "000001.SZ", "20260811", 10.0, 10.0, 10.0, 10.0, 0.0, 10.0, 10.0, "test.parquet", rw.now_str()),
                ("000001", "000001.SZ", "20260812", 10.0, 10.0, 0.0, 0.0, -100.0, 10.0, 0.0, "test.parquet", rw.now_str()),
            ],
        )
        conn.commit()

        report = rw.repair_recommendation_metrics(
            conn=conn,
            trade_dates=["20260811", "20260812"],
            hydrate_price_cache=False,
        )
        row = conn.execute(
            """
            SELECT next_day_return_pct, cumulative_return_pct, completeness_status, settlement_status
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertEqual(report["rows_rebuilt"], 1)
        self.assertIsNone(row["next_day_return_pct"])
        self.assertIsNone(row["cumulative_return_pct"])
        self.assertEqual(row["completeness_status"], "data_missing")
        self.assertEqual(row["settlement_status"], "data_missing")

    def test_recompute_metrics_keeps_primary_return_when_only_diagnostic_horizon_is_missing(self):
        conn = self.make_conn()
        prepared = rw.prepare_recommendation_fact_record(
            self.make_item(
                recommend_date="20260807",
                raw={
                    **self.make_item()["raw"],
                    "planned_entry_time": "2026-08-10T09:30:00+08:00",
                    "holding_period_days": 5,
                },
            ),
            strict=True,
            trade_dates=["20260807", "20260810", "20260811", "20260812", "20260813", "20260814"],
        )
        rw.upsert_recommendations(conn, [prepared])
        conn.executemany(
            """
            INSERT INTO price_daily_cache (
                stock_code, ts_code, trade_date, open, high, low, close, pct_chg,
                open_qfq, close_qfq, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", "000001.SZ", "20260810", 10, 10, 10, 10, 0, 10, 10, "t", rw.now_str()),
                ("000001", "000001.SZ", "20260811", 10, 10, 10, 10, 0, 10, 10, "t", rw.now_str()),
                ("000001", "000001.SZ", "20260812", 10, 10, 0, 0, -100, 10, 0, "t", rw.now_str()),
                ("000001", "000001.SZ", "20260813", 10, 10, 10, 10.5, 5, 10, 10.5, "t", rw.now_str()),
                ("000001", "000001.SZ", "20260814", 10, 11, 10, 11, 4.76, 10, 11, "t", rw.now_str()),
            ],
        )
        conn.commit()

        rw.recompute_metrics(
            conn,
            ["20260807", "20260810", "20260811", "20260812", "20260813", "20260814"],
        )
        row = conn.execute(
            """
            SELECT forward_return_1d, forward_return_3d, forward_return_5d,
                   cumulative_return_pct, completeness_status, settlement_status
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertAlmostEqual(row["forward_return_1d"], -0.3, places=4)
        self.assertIsNone(row["forward_return_3d"])
        self.assertAlmostEqual(row["forward_return_5d"], 9.7, places=4)
        self.assertAlmostEqual(row["cumulative_return_pct"], 9.7, places=4)
        self.assertEqual(row["completeness_status"], "complete")
        self.assertEqual(row["settlement_status"], "settled")

    def test_repair_recommendation_metrics_uses_qfq_entry_and_5d_primary_holding(self):
        conn = self.make_conn()
        prepared = rw.prepare_recommendation_fact_record(
            self.make_item(
                strategy_id=rw.TARGET_STRATEGY,
                strategy_source="traditional",
                strategy_name=rw.TARGET_STRATEGY_NAME,
                recommend_date="20260807",
                raw={
                    "signal_data_cutoff": "2026-08-07T15:00:00+08:00",
                    "planned_entry_time": "2026-08-10T09:30:00+08:00",
                    "holding_period_days": 5,
                    "data_sources": ["strategy_backtests.json"],
                    "used_proxy": False,
                    "round_trip_cost": 0.003,
                    "benchmark": "all_a_tradable_equal_weight",
                },
            ),
            strict=True,
            trade_dates=["20260807", "20260810", "20260811", "20260812", "20260813", "20260814"],
        )
        rw.upsert_recommendations(conn, [prepared])
        conn.executemany(
            """
            INSERT INTO price_daily_cache (
                stock_code, ts_code, trade_date, open, high, low, close, pct_chg,
                open_qfq, close_qfq, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", "000001.SZ", "20260807", 9.8, 10.1, 9.7, 10.0, 2.0, 9.8, 10.0, "t.parquet", rw.now_str()),
                ("000001", "000001.SZ", "20260810", 10.1, 10.5, 10.0, 10.3, 3.0, 10.0, 10.2, "t.parquet", rw.now_str()),
                ("000001", "000001.SZ", "20260811", 10.2, 10.8, 10.1, 10.6, 2.9, 10.1, 10.4, "t.parquet", rw.now_str()),
                ("000001", "000001.SZ", "20260812", 10.3, 10.9, 10.2, 10.7, 1.0, 10.2, 10.5, "t.parquet", rw.now_str()),
                ("000001", "000001.SZ", "20260813", 10.4, 11.0, 10.3, 10.8, 0.9, 10.3, 10.7, "t.parquet", rw.now_str()),
                ("000001", "000001.SZ", "20260814", 10.5, 11.2, 10.4, 11.0, 1.8, 10.4, 10.8, "t.parquet", rw.now_str()),
            ],
        )
        conn.commit()

        rw.repair_recommendation_metrics(
            conn=conn,
            trade_dates=["20260807", "20260810", "20260811", "20260812", "20260813", "20260814"],
            hydrate_price_cache=False,
        )
        row = conn.execute(
            """
            SELECT next_trade_date, recommend_price, next_day_return_pct, cumulative_return_pct,
                   forward_return_1d, forward_return_3d, forward_return_5d, completeness_status, settlement_status
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertEqual(row["next_trade_date"], "20260810")
        self.assertEqual(row["recommend_price"], 10.0)
        self.assertAlmostEqual(row["next_day_return_pct"], 1.7, places=4)
        self.assertAlmostEqual(row["forward_return_1d"], 1.7, places=4)
        self.assertAlmostEqual(row["forward_return_3d"], 4.7, places=4)
        self.assertAlmostEqual(row["forward_return_5d"], 7.7, places=4)
        self.assertAlmostEqual(row["cumulative_return_pct"], 7.7, places=4)
        self.assertEqual(row["completeness_status"], "complete")
        self.assertEqual(row["settlement_status"], "settled")

    def test_ensure_price_cache_backfills_missing_qfq_per_stock_not_per_day(self):
        conn = self.make_conn()
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            trade_date = "20260811"
            pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000002.SZ"],
                    "open": [10.0, 20.0],
                    "high": [10.5, 20.5],
                    "low": [9.8, 19.8],
                    "close": [10.2, 20.2],
                    "pct_chg": [2.0, 1.0],
                }
            ).to_parquet(cache_dir / f"daily_{trade_date}.parquet")
            pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "open_qfq": [10.0],
                    "high_qfq": [10.5],
                    "low_qfq": [9.8],
                    "close_qfq": [10.2],
                    "pre_close_qfq": [9.9],
                }
            ).to_parquet(cache_dir / f"stk_factor_{trade_date}.parquet")

            with mock.patch.object(rw, "BACKTEST_CACHE_DIR", cache_dir):
                rw.ensure_price_cache(conn, [trade_date])
                row = conn.execute(
                    "SELECT close_qfq FROM price_daily_cache WHERE stock_code = ? AND trade_date = ?",
                    ("000002", trade_date),
                ).fetchone()
                self.assertIsNone(row["close_qfq"])

                pd.DataFrame(
                    {
                        "ts_code": ["000001.SZ", "000002.SZ"],
                        "open_qfq": [10.0, 20.0],
                        "high_qfq": [10.5, 20.5],
                        "low_qfq": [9.8, 19.8],
                        "close_qfq": [10.2, 20.2],
                        "pre_close_qfq": [9.9, 19.9],
                    }
                ).to_parquet(cache_dir / f"stk_factor_{trade_date}.parquet")
                rw.ensure_price_cache(conn, [trade_date])

            row = conn.execute(
                "SELECT close_qfq FROM price_daily_cache WHERE stock_code = ? AND trade_date = ?",
                ("000002", trade_date),
            ).fetchone()
            self.assertEqual(row["close_qfq"], 20.2)

    def test_repair_recommendation_metrics_hydrates_qfq_cache_before_rebuild(self):
        conn = self.make_conn()
        prepared = rw.prepare_recommendation_fact_record(
            self.make_item(
                recommend_date="20260810",
                raw={
                    **self.make_item()["raw"],
                    "signal_data_cutoff": "2026-08-10T15:00:00+08:00",
                    "planned_entry_time": "2026-08-11T09:30:00+08:00",
                },
            ),
            strict=True,
            trade_dates=["20260810", "20260811"],
        )
        rw.upsert_recommendations(conn, [prepared])

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "open": [10.0],
                    "high": [10.6],
                    "low": [9.9],
                    "close": [10.5],
                    "pct_chg": [5.0],
                }
            ).to_parquet(cache_dir / "daily_20260811.parquet")
            pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "open_qfq": [10.0],
                    "high_qfq": [10.6],
                    "low_qfq": [9.9],
                    "close_qfq": [10.5],
                    "pre_close_qfq": [10.0],
                }
            ).to_parquet(cache_dir / "stk_factor_20260811.parquet")
            with mock.patch.object(rw, "BACKTEST_CACHE_DIR", cache_dir):
                report = rw.repair_recommendation_metrics(
                    conn=conn,
                    trade_dates=["20260810", "20260811"],
                )

        row = conn.execute(
            "SELECT cumulative_return_pct, settlement_status FROM recommendation_fact"
        ).fetchone()
        self.assertEqual(report["price_cache"]["dates_with_qfq"], 1)
        self.assertEqual(report["price_cache"]["rows_upserted"], 1)
        self.assertAlmostEqual(row["cumulative_return_pct"], 4.7, places=4)
        self.assertEqual(row["settlement_status"], "settled")

    def test_upsert_recommendations_does_not_delete_other_history_dates(self):
        conn = self.make_conn()
        base_items = [
            rw.prepare_recommendation_fact_record(
                self.make_item(recommend_date="20260810", raw={**self.make_item()["raw"], "data_sources": ["a.json"]}),
                strict=True,
            ),
            rw.prepare_recommendation_fact_record(
                self.make_item(recommend_date="20260811", raw={**self.make_item()["raw"], "data_sources": ["b.json"]}),
                strict=True,
            ),
        ]
        rw.upsert_recommendations(conn, base_items)
        rw.upsert_recommendations(
            conn,
            [
                rw.prepare_recommendation_fact_record(
                    self.make_item(
                        recommend_date="20260811",
                        stock_code="000002",
                        ts_code="000002.SZ",
                        stock_name="万科A",
                        raw={**self.make_item()["raw"], "data_sources": ["b.json"]},
                    ),
                    strict=True,
                )
            ],
        )
        dates = [
            row["recommend_date"]
            for row in conn.execute(
                "SELECT recommend_date FROM recommendation_fact ORDER BY recommend_date, stock_code"
            ).fetchall()
        ]
        self.assertEqual(dates, ["20260810", "20260811", "20260811"])

    def test_ai_loader_permanently_excludes_future_backfill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir)
            (ai_dir / "20260810.json").write_text(
                json.dumps(
                    [
                        {
                            "code": "000001.SZ",
                            "strategy_source": "traditional",
                            "ai_source_date": "20260810",
                            "ai_analysis_date": "20260810",
                            "generated_at": "2026-08-11 09:00:00",
                            "analysis_summary": "未来回填，不能作为历史证据",
                        },
                        {
                            "code": "000002.SZ",
                            "strategy_source": "traditional",
                            "ai_source_date": "20260810",
                            "ai_analysis_date": "20260810",
                            "generated_at": "2026-08-10 18:00:00",
                            "analysis_summary": "同日生成，可作为历史证据",
                        },
                        {
                            "code": "000003.SZ",
                            "strategy_source": "traditional",
                            "analysis_summary": "没有证据时间，不能猜测为同日",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(rw, "AI_ANALYSIS_DIR", ai_dir):
                same_day, latest = rw.load_same_day_ai_maps()

        self.assertNotIn("traditional:000001", same_day.get("20260810", {}))
        self.assertIn("traditional:000002", same_day.get("20260810", {}))
        self.assertNotIn("traditional:000003", same_day.get("20260810", {}))
        self.assertNotIn("traditional:000001", latest)

    def test_enrich_item_never_uses_stale_ai_or_undated_embedded_ai(self):
        item = self.make_item(
            strategy_id=rw.TARGET_STRATEGY,
            strategy_source="traditional",
            raw={"ai_summary": "无证据时间的内嵌分析", "ai_score": 99},
        )
        stale = {
            "traditional:000001": {
                "ai_summary": "推荐日之后生成的分析",
                "ai_score": 88,
                "ai_source_date": "20260812",
            }
        }
        enriched = rw.enrich_item(item, {}, {}, stale)
        self.assertIsNone(enriched.get("ai_summary"))
        self.assertIsNone(enriched.get("ai_score"))
        self.assertIsNone(enriched.get("ai_source_date"))
        self.assertEqual(enriched.get("ai_source_stale"), 0)

    def test_rank_change_is_fixed_at_zero(self):
        item = self.make_item(rank_change=5)
        with self.assertRaises(ValueError):
            rw.prepare_recommendation_fact_record(item, strict=True)

    def test_ai_evidence_contract_is_persisted_and_same_day_only(self):
        conn = self.make_conn()
        item = self.make_item(
            ai_summary="只解释量化结论",
            ai_source_date="20260811",
            ai_evidence_time="2026-08-11T18:30:00+08:00",
            ai_risk_tags=["公告口径冲突", "流动性"],
        )
        prepared = rw.prepare_recommendation_fact_record(item, strict=True)
        self.assertTrue(prepared["ai_effectiveness_eligible"])
        self.assertEqual(prepared["ai_explanation"], "只解释量化结论")
        rw.upsert_recommendations(conn, [prepared])
        row = conn.execute(
            """
            SELECT ai_evidence_time, ai_risk_tags_json, ai_explanation,
                   ai_effectiveness_eligible, ai_exclusion_reason
            FROM recommendation_fact
            """
        ).fetchone()
        self.assertEqual(row["ai_evidence_time"], "2026-08-11T18:30:00+08:00")
        self.assertEqual(json.loads(row["ai_risk_tags_json"]), ["公告口径冲突", "流动性"])
        self.assertEqual(row["ai_explanation"], "只解释量化结论")
        self.assertEqual(row["ai_effectiveness_eligible"], 1)
        self.assertIsNone(row["ai_exclusion_reason"])

    def test_repair_ai_effectiveness_flags_excludes_future_and_undated_history(self):
        conn = self.make_conn()
        items = [
            self.make_item(
                stock_code="000001",
                ts_code="000001.SZ",
                ai_summary="同日证据",
                ai_source_date="20260811",
                ai_evidence_time="2026-08-11T18:00:00+08:00",
            ),
            self.make_item(
                stock_code="000002",
                ts_code="000002.SZ",
                ai_summary="未来回填",
                ai_source_date="20260812",
                ai_evidence_time="2026-08-12T09:00:00+08:00",
            ),
            self.make_item(
                stock_code="000003",
                ts_code="000003.SZ",
                ai_summary="无证据时间",
                ai_source_date="20260811",
            ),
        ]
        prepared = [
            rw.prepare_recommendation_fact_record(item, strict=True) for item in items
        ]
        rw.upsert_recommendations(conn, prepared)
        conn.execute(
            "UPDATE recommendation_fact SET ai_effectiveness_eligible=1, ai_exclusion_reason=NULL"
        )
        conn.commit()

        report = rw.repair_ai_effectiveness_flags(conn)
        rows = {
            row["stock_code"]: row
            for row in conn.execute(
                "SELECT stock_code, ai_effectiveness_eligible, ai_exclusion_reason "
                "FROM recommendation_fact"
            ).fetchall()
        }

        self.assertEqual(report["eligible_rows"], 1)
        self.assertEqual(report["future_backfill_rows"], 1)
        self.assertEqual(rows["000001"]["ai_effectiveness_eligible"], 1)
        self.assertEqual(rows["000002"]["ai_effectiveness_eligible"], 0)
        self.assertEqual(rows["000002"]["ai_exclusion_reason"], "future_backfill")
        self.assertEqual(rows["000003"]["ai_effectiveness_eligible"], 0)
        self.assertEqual(rows["000003"]["ai_exclusion_reason"], "missing_evidence_time")


if __name__ == "__main__":
    unittest.main()
