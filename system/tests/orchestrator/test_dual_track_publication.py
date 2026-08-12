from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dual_track_publication as publication  # noqa: E402


class DualTrackPublicationTests(unittest.TestCase):
    def make_workspace(self, root: Path) -> tuple[Path, Path]:
        workspace = root / "workspace"
        published = root / "stock-report"
        daily = workspace / "stock_data/03-working/strategy_research/short_track/daily"
        event_daily = workspace / "stock_data/03-working/strategy_research/event_quality_drift_v1/daily"
        warehouse = workspace / "stock_data/03-working/recommendation_warehouse"
        latest = published / "data/latest"
        daily.mkdir(parents=True)
        event_daily.mkdir(parents=True)
        warehouse.mkdir(parents=True)
        latest.mkdir(parents=True)

        specs = {
            "prebreakout_v43_control": ("4.3+hash", 20),
            "prebreakout_v43_top15": ("1.0.0+top15", 15),
            "prebreakout_v44_balanced": ("1.0.0+balanced", 20),
        }
        for strategy_id, (version, count) in specs.items():
            rows = []
            for rank in range(1, count + 1):
                rows.append(
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": version,
                        "rank": rank,
                        "rank_no": rank,
                        "rank_change": 0,
                        "ts_code": f"{rank:06d}.SZ",
                        "stock_code": f"{rank:06d}",
                        "name": f"股票{rank}",
                        "industry_name": f"行业{(rank - 1) // 3 + 1}",
                        "score": 100 - rank,
                        "settlement_status": "pending_settlement",
                        "planned_entry_time": "2026-08-12T09:30:00+08:00",
                        "signal_data_cutoff": "2026-08-11T15:00:00+08:00",
                        "used_proxy": False,
                        "factor_scores": {"volatility_squeeze": 80.0},
                    }
                )
            snapshot = {
                "artifact_kind": "candidate_snapshot",
                "strategy_id": strategy_id,
                "strategy_version": version,
                "trade_date": "20260811",
                "signal_date": "20260811",
                "holding_period_days": 5,
                "round_trip_cost": 0.003,
                "stress_round_trip_cost": 0.005,
                "benchmark": "all_a_tradable_equal_weight",
                "used_proxy": False,
                "rank_change": 0,
                "execution_authority": "observe_only_no_auto_order",
                "candidates": rows,
            }
            tracking = {
                "artifact_kind": "candidate_tracking_report",
                "generated_at": "2026-08-11T19:35:25+08:00",
                "strategy_id": strategy_id,
                "strategy_version": version,
                "operational_status": "healthy",
                "effectiveness_status": "not_validated",
                "effectiveness_evidence": {
                    "validation_start_date": "20260811",
                    "sample_trade_days": 0,
                    "failed_gates": ["insufficient_matured_trade_days"],
                    "all_gates_pass": False,
                    "decision": "observe_only",
                },
                "execution_authority": "observe_only_no_auto_order",
            }
            (daily / f"{strategy_id}_20260811_candidate_snapshot.json").write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
            )
            (daily / f"{strategy_id}_20260811_candidate_tracking.json").write_text(
                json.dumps(tracking, ensure_ascii=False), encoding="utf-8"
            )

        event = {
            "artifact_kind": "candidate_tracking_report",
            "signal_date": "20260810",
            "strategy_id": "event_quality_drift_v1",
            "strategy_version": "1.0.0+event",
            "operational_status": "healthy_no_eligible_candidates",
            "effectiveness_status": "not_applicable_no_eligible_events",
            "execution_authority": "observe_only_no_auto_order",
            "decision": "observe_only",
            "new_announcement_event_count": 1,
            "eligible_event_count": 0,
            "valid_announcement_events": 0,
            "sample_months": 0,
            "sample_trade_days": 0,
            "revision_chain_complete": False,
            "evidence_scope": "auxiliary_only",
            "rejection_reason": "no eligible PIT security",
            "failed_gates": ["minimum_100_valid_announcement_events"],
        }
        (event_daily / "event_quality_drift_v1_20260810_candidate_tracking.json").write_text(
            json.dumps(event, ensure_ascii=False), encoding="utf-8"
        )

        conn = sqlite3.connect(warehouse / "recommendations.db")
        conn.execute(
            """
            CREATE TABLE recommendation_fact (
                strategy_id TEXT, recommend_date TEXT, stock_code TEXT,
                settlement_status TEXT, completeness_status TEXT,
                next_day_return_pct REAL, cumulative_return_pct REAL,
                forward_return_1d REAL, forward_return_3d REAL, forward_return_5d REAL,
                used_proxy INTEGER, rank_change INTEGER,
                ai_effectiveness_eligible INTEGER, ai_exclusion_reason TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO recommendation_fact VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("prebreakout_v41", "20260810", "000001", "settled", "complete", 1.0, 2.0, 1.0, 1.5, 2.0, 0, 0, 0, "future_backfill"),
                ("prebreakout_v41", "20260811", "000002", "pending_settlement", "pending_settlement", None, None, None, None, None, 0, 0, 0, "no_ai_evidence"),
                ("prebreakout_v41", "20260428", "000003", "data_missing", "data_missing", None, None, None, None, None, 0, 0, 0, "missing_evidence_time"),
            ],
        )
        conn.commit()
        conn.close()

        (latest / "review_track_latest.json").write_text(
            '{"public_contract_version":"public_results_v1","daily_comparison":[]}',
            encoding="utf-8",
        )
        return workspace, published

    def test_builds_public_dual_track_state_and_replaces_misleading_evaluation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            result = publication.build_dual_track_publication(
                workspace=workspace,
                published_repo=published,
                generated_at="2026-08-11T20:30:00+08:00",
            )

            self.assertTrue(result["ok"])
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            self.assertEqual(state["contract_version"], "dual_track_v1")
            self.assertEqual(state["trade_date"], "20260811")
            self.assertEqual(state["flow_status"], "healthy")
            self.assertEqual(state["effectiveness_status"], "not_validated")
            self.assertEqual(state["execution_authority"], "observe_only_no_auto_order")
            self.assertEqual(
                [item["candidate_count"] for item in state["short_track_strategies"]],
                [20, 15, 20],
            )
            self.assertEqual(state["event_track"]["eligible_event_count"], 0)
            self.assertEqual(state["evaluation_integrity"]["fake_or_impossible_return_count"], 0)
            self.assertTrue((published / "data/latest/review_track_latest.json").exists())

            evaluation = json.loads((published / "data/latest/strategy_evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(evaluation["contract_version"], "evaluation_integrity_v2")
            self.assertEqual(evaluation["integrity"]["total_rows"], 3)
            self.assertEqual(evaluation["integrity"]["settlement_counts"]["data_missing"], 1)
            self.assertIsNone(evaluation["strategies"]["prebreakout_v43_control"]["metrics"])
            self.assertEqual(evaluation["strategies"]["prebreakout_v43_control"]["effectiveness_status"], "not_validated")

    def test_rejects_proxy_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            path = workspace / "stock_data/03-working/strategy_research/short_track/daily/prebreakout_v44_balanced_20260811_candidate_snapshot.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["candidates"][0]["used_proxy"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(publication.PublicationContractError):
                publication.build_dual_track_publication(workspace=workspace, published_repo=published)

    def test_rejects_impossible_returns_in_evaluation_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            db = workspace / "stock_data/03-working/recommendation_warehouse/recommendations.db"
            conn = sqlite3.connect(db)
            conn.execute("UPDATE recommendation_fact SET forward_return_3d = -100.3 WHERE stock_code = '000001'")
            conn.commit()
            conn.close()
            with self.assertRaises(publication.PublicationContractError):
                publication.build_dual_track_publication(workspace=workspace, published_repo=published)

    def test_v2_publisher_runs_dual_track_contract_before_sanitizing(self):
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        dual_pos = text.index("dual_track_publication.py")
        sanitize_pos = text.index("sanitize_public_report.py")
        validate_pos = text.index("validate_publication_contract.py")
        self.assertLess(dual_pos, sanitize_pos)
        self.assertLess(dual_pos, validate_pos)

    def test_v2_publisher_enforces_public_boundary_after_contract_validation(self):
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        validate_pos = text.index("validate_publication_contract.py")
        boundary_pos = text.index("enforce_public_boundary.py")
        stage_pos = text.index("git add data/")
        self.assertLess(validate_pos, boundary_pos)
        self.assertLess(boundary_pos, stage_pos)

    def test_v2_publisher_refreshes_review_before_building_public_summaries(self):
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        heatmap_pos = text.index("generate_industry_heatmap.py")
        latest_pos = text.index("generate_latest_states.py")
        refreshed_publication_pos = text.index("strategy_publication_layer.py", latest_pos)
        summaries_pos = text.index("generate_view_summaries.py", refreshed_publication_pos)
        dual_pos = text.index("dual_track_publication.py", summaries_pos)
        self.assertLess(heatmap_pos, latest_pos)
        self.assertLess(latest_pos, refreshed_publication_pos)
        self.assertLess(refreshed_publication_pos, summaries_pos)
        self.assertLess(summaries_pos, dual_pos)

    def test_v2_publisher_uses_portable_stock_system_runtime(self):
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        self.assertIn("STOCK_SYSTEM_PYTHON", text)
        self.assertIn("STOCK_SYSTEM_ROOT", text)
        self.assertNotIn("/" + "Users/fisher", text)

    def test_latest_state_builder_reads_recommendation_detail_from_local_warehouse(self):
        text = (SCRIPT_DIR / "generate_latest_states.py").read_text(encoding="utf-8")
        self.assertIn("LOCAL_WAREHOUSE_EXPORT_DIR", text)
        self.assertIn('LOCAL_WAREHOUSE_EXPORT_DIR / "prebreakout_recommendations.json"', text)
        self.assertNotIn('DETAIL_JSON = ANALYTICS_DIR / "prebreakout_recommendations.json"', text)

    def test_publication_builders_read_full_recommendation_history_locally(self):
        publication_text = (SCRIPT_DIR / "strategy_publication_layer.py").read_text(encoding="utf-8")
        heatmap_text = (SCRIPT_DIR / "generate_industry_heatmap.py").read_text(encoding="utf-8")
        self.assertIn("LOCAL_WAREHOUSE_EXPORT_DIR", publication_text)
        self.assertIn("LOCAL_WAREHOUSE_EXPORT_DIR", heatmap_text)
        self.assertNotIn(
            'detail_rows_by_latest(ANALYTICS_DIR / "prebreakout_recommendations.json"',
            publication_text,
        )
        self.assertNotIn("DETAIL_JSON = ANALYTICS_DIR", heatmap_text)


if __name__ == "__main__":
    unittest.main()
