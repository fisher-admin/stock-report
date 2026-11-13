#!/usr/bin/env python3
"""Regression checks for explicit historical stock-pipeline backfills."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import generate_prebreakout_ai as pre_ai  # noqa: E402
import generate_market_industry_heatmap as market_heatmap  # noqa: E402
import orchestrator_common as common  # noqa: E402
import stage4_ai_backfill_loop as backfill  # noqa: E402
import stage4_ai_publish as publish  # noqa: E402
import validate_run  # noqa: E402


class BackfillTargetingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._target = os.environ.get("OPENCLAW_TARGET_TRADE_DATE")

    def tearDown(self) -> None:
        if self._target is None:
            os.environ.pop("OPENCLAW_TARGET_TRADE_DATE", None)
        else:
            os.environ["OPENCLAW_TARGET_TRADE_DATE"] = self._target

    def test_explicit_target_overrides_newer_complete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cache = common.CACHE_DIR
            common.CACHE_DIR = Path(tmp)
            try:
                for day in ("20260728", "20260807"):
                    for kind in ("daily", "stk_factor", "cyq_perf"):
                        (Path(tmp) / f"{kind}_{day}.parquet").touch()
                os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260728"
                latest, latest_by_type = common.latest_common_trade_date()
                self.assertEqual(latest, "20260728")
                self.assertEqual(latest_by_type["daily"], "20260807")
            finally:
                common.CACHE_DIR = old_cache

    def test_normal_run_still_uses_newest_complete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cache = common.CACHE_DIR
            common.CACHE_DIR = Path(tmp)
            try:
                for day in ("20260728", "20260807"):
                    for kind in ("daily", "stk_factor", "cyq_perf"):
                        (Path(tmp) / f"{kind}_{day}.parquet").touch()
                os.environ.pop("OPENCLAW_TARGET_TRADE_DATE", None)
                latest, _ = common.latest_common_trade_date()
                self.assertEqual(latest, "20260807")
            finally:
                common.CACHE_DIR = old_cache

    def test_market_heatmap_window_stops_at_explicit_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            for day in ("20260727", "20260728", "20260807"):
                (cache / f"daily_{day}.parquet").touch()

            def fake_read(path: Path) -> pd.DataFrame:
                day = path.stem.rsplit("_", 1)[-1]
                return pd.DataFrame(
                    [{"ts_code": "000001.SZ", "trade_date": day, "close": 10, "pct_chg": 1, "amount": 2, "vol": 3}]
                )

            old_cache = market_heatmap.CACHE_DIR
            market_heatmap.CACHE_DIR = cache
            os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260728"
            try:
                with mock.patch.object(market_heatmap.pd, "read_parquet", side_effect=fake_read):
                    rows = market_heatmap.load_market_rows(limit_dates=30)
            finally:
                market_heatmap.CACHE_DIR = old_cache
            self.assertEqual(sorted(rows["trade_date"].unique().tolist()), ["20260727", "20260728"])

    def test_trade_date_contract_prefers_explicit_backfill_date(self) -> None:
        os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260728"
        contract = validate_run.build_trade_date_contract(
            strategy_doc={"latest_trade_date": "20260728"},
            data_doc={"latest_trade_date": "20260728"},
            recommendation_sync={"latest_recommend_date": "20260728", "latest_price_date": "20260807"},
            morning_doc={"trade_date": "20260727", "market_data_trade_date": "20260727"},
            market_doc={"latest_trade_date": "20260807"},
        )
        self.assertEqual(contract["decision_trade_date"], "20260728")
        codes = {item["code"] for item in contract["blockers"]}
        self.assertEqual(codes, {"MARKET_TRADE_DATE_MISMATCH"})

    def test_stale_greenfield_is_not_merged_or_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "current_selection_snapshot.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "latest_trade_date": "20260807",
                        "top20": [{"code": "000001", "name": "测试"}],
                    }
                ),
                encoding="utf-8",
            )
            old_candidates = publish.GREENFIELD_TOP20_CANDIDATES
            publish.GREENFIELD_TOP20_CANDIDATES = [snapshot]
            try:
                data, items, source = publish.load_greenfield_o2c("20260728")
                self.assertIsNone(data)
                self.assertEqual(items, [])
                self.assertFalse(source["matched_target_trade_date"])

                # O2C archived (plan v1.4 §2.2): pending_o2c_codes is a stub that never merges greenfield.
                trade_date, codes, stocks, meta = backfill.pending_o2c_codes("20260728")
                self.assertEqual(trade_date, "20260728")  # echoes request date when archived
                self.assertEqual(codes, [])
                self.assertEqual(stocks, [])
                self.assertEqual(meta.get("skipped_reason"), "strategy_archived")
            finally:
                publish.GREENFIELD_TOP20_CANDIDATES = old_candidates

    def test_failed_placeholder_is_not_partial_ai(self) -> None:
        self.assertTrue(publish.ai_content_failed({"ai_summary": "AI分析暂缺（上次分析失败，待补齐）"}))

    def test_direct_generator_fields_survive_historical_loader(self) -> None:
        points = [
            "技术形态/启动条件：测试",
            "核心因子贡献：测试",
            "量价与筹码：测试",
            "风险点：测试",
            "执行参考：测试",
        ]
        fields = publish.historical_ai_entry_to_fields(
            {
                "code": "000001",
                "ai_summary": "新格式摘要",
                "analysis_summary": "新格式摘要",
                "ai_points": points,
                "ai_risks": ["风险一", "风险二"],
                "ai_score": 72,
                "ai_advice": "观察",
                "ai_source_kind": "traditional_deep_analysis",
                "ai_source_date": "20260728",
            },
            "20260728",
        )
        self.assertEqual(fields["ai_summary"], "新格式摘要")
        self.assertEqual(fields["ai_points"], points)
        self.assertEqual(fields["ai_score"], 72)
        self.assertEqual(fields["ai_advice"], "观察")
        self.assertEqual(fields["ai_source_kind"], "traditional_deep_analysis")
        self.assertTrue(common.has_explicit_labels(fields))

    def test_fresh_same_day_ai_replaces_nonempty_generated_fallback(self) -> None:
        fallback = {
            "code": "000001",
            "ai_score": 50,
            "ai_advice": "观察",
            "ai_points": ["简略占位"],
            "ai_summary": "旧的简略占位解读",
            "ai_provenance": "generated_fallback",
        }
        fresh = {
            "code": "000001",
            "ai_score": 78,
            "ai_advice": "观察",
            "ai_points": [
                "技术形态：新",
                "核心因子贡献：新",
                "量价与筹码：新",
                "风险点：新",
                "执行参考：新",
            ],
            "ai_summary": "同日完整 AI 解读",
            "ai_source_kind": "traditional_deep_analysis",
            "ai_source_date": "20260902",
        }

        merged = publish.merge_candidate_ai(fallback, fresh, "20260902")

        self.assertEqual(merged["ai_summary"], "同日完整 AI 解读")
        self.assertEqual(merged["ai_score"], 78)
        self.assertEqual(publish.item_ai_status(merged), "ready")
        self.assertEqual(
            publish.classify_ai_provenance(fallback, merged, "20260902"),
            "fresh_same_day",
        )

    def test_stale_ai_cannot_replace_nonempty_current_fields(self) -> None:
        current = {"code": "000001", "ai_summary": "当前解读", "ai_score": 60}
        stale = {
            "code": "000001",
            "ai_summary": "旧日解读",
            "ai_score": 88,
            "ai_source_kind": "traditional_deep_analysis",
            "ai_source_date": "20260901",
        }

        merged = publish.merge_candidate_ai(current, stale, "20260902")

        self.assertEqual(merged["ai_summary"], "当前解读")
        self.assertEqual(merged["ai_score"], 60)

    def test_prebreakout_loader_uses_latest_same_day_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pre.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE recommendation_snapshot (
                    strategy_id TEXT,
                    recommend_date TEXT,
                    rank_no INTEGER,
                    stock_code TEXT,
                    ts_code TEXT,
                    stock_name TEXT,
                    sector_name TEXT,
                    adjusted_action TEXT,
                    evidence_json TEXT,
                    created_at TEXT
                )
                """
            )
            rows = [
                ("prebreakout_v41", "20260728", 1, "000001", "000001.SZ", "旧一", "旧", "watch", "{}", "2026-08-09 14:00:00"),
                ("prebreakout_v41", "20260728", 2, "000002", "000002.SZ", "旧二", "旧", "watch", "{}", "2026-08-09 14:00:00"),
                ("prebreakout_v41", "20260728", 1, "600001", "600001.SH", "新一", "新", "watch", "{}", "2026-08-09 15:00:00"),
                ("prebreakout_v41", "20260728", 2, "600002", "600002.SH", "新二", "新", "watch", "{}", "2026-08-09 15:00:00"),
            ]
            conn.executemany(
                "INSERT INTO recommendation_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            conn.commit()
            conn.close()

            old_db = pre_ai.PRE_DB
            pre_ai.PRE_DB = db
            try:
                picks = pre_ai.load_picks("20260728", 20)
            finally:
                pre_ai.PRE_DB = old_db
            self.assertEqual([row["code"] for row in picks], ["600001", "600002"])

    def test_reset_giveup_removes_only_requested_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "giveup.json"
            path.write_text(
                json.dumps({"20260728": {"000001": 3}, "20260729": {"000002": 2}}),
                encoding="utf-8",
            )
            old_path = backfill.GIVEUP_PATH
            backfill.GIVEUP_PATH = path
            try:
                self.assertTrue(backfill.reset_giveup("20260728"))
            finally:
                backfill.GIVEUP_PATH = old_path
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"20260729": {"000002": 2}})

    def test_ai_backfill_completion_does_not_require_remote_publish(self) -> None:
        readiness = {
            "base_ok": True,
            "local_materialized": True,
            "published": False,
            "ai_complete": True,
        }
        self.assertTrue(backfill.readiness_complete(readiness, [], []))
        self.assertFalse(
            backfill.readiness_complete(
                {**readiness, "local_materialized": False},
                [],
                [],
            )
        )
        self.assertFalse(backfill.readiness_complete(readiness, ["000001"], []))

    def test_prebreakout_retry_preserves_completed_ai_instead_of_forcing_full_rebuild(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(backfill.subprocess, "run", return_value=completed) as run, mock.patch.object(backfill, 'PREBREAKOUT_AI_PATH', Path(__file__)):
            result = backfill.run_fisher_bridge(["000001"], target_trade_date="20260901")

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertNotIn(
            "--force",
            command,
            "retries must keep prior successes so partial coverage converges",
        )

    def test_validation_publish_readiness_uses_local_predeployment_state(self) -> None:
        readiness = {
            "base_ok": True,
            "local_materialized": True,
            "published": False,
            "ai_complete": True,
        }
        self.assertTrue(validate_run.publication_ready(True, readiness, True))
        self.assertFalse(
            validate_run.publication_ready(
                True,
                {**readiness, "local_materialized": False},
                True,
            )
        )
        self.assertFalse(validate_run.publication_ready(True, readiness, False))


if __name__ == "__main__":
    unittest.main()
