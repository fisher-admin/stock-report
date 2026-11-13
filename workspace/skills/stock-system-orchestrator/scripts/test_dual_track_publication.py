from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPT_DIR = Path(__file__).resolve().parent
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
        (latest / "recommendation_state.json").write_text(
            json.dumps({"trade_date": "20260811", "strategies": {}}, ensure_ascii=False),
            encoding="utf-8",
        )

        # Required strategies only in fixture; v45 optional coverage is tested separately.
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
                "trade_date": "20260811",
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
            json.dumps(
                {
                    "public_contract_version": "public_results_v1",
                    "summarized": True,
                    "sentinel": "keep-me",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return workspace, published

    def test_builds_public_dual_track_state_without_deleting_public_summary(self):
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
                [item["candidate_count"] for item in state["short_track_strategies"] if item.get("required_for_publish", True)],
                [20, 15, 20],
            )
            v45 = next(
                item
                for item in state["short_track_strategies"]
                if item["strategy_id"] == "prebreakout_v45_cross_sectional"
            )
            self.assertEqual(v45["status"], "missing")
            self.assertEqual(v45["candidate_count"], 0)
            self.assertFalse(v45.get("required_for_publish", True))
            self.assertEqual(state["event_track"]["eligible_event_count"], 0)
            self.assertEqual(state["evaluation_integrity"]["fake_or_impossible_return_count"], 0)
            review_summary = json.loads(
                (published / "data/latest/review_track_latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review_summary["public_contract_version"], "public_results_v1")
            self.assertEqual(review_summary["sentinel"], "keep-me")

            evaluation = json.loads((published / "data/latest/strategy_evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(evaluation["contract_version"], "evaluation_integrity_v2")
            self.assertEqual(evaluation["integrity"]["total_rows"], 3)
            self.assertEqual(evaluation["integrity"]["settlement_counts"]["data_missing"], 1)
            self.assertIsNone(evaluation["strategies"]["prebreakout_v43_control"]["metrics"])
            self.assertEqual(evaluation["strategies"]["prebreakout_v43_control"]["effectiveness_status"], "not_validated")

    def test_publish_succeeds_with_v45_failure_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            daily = workspace / "stock_data/03-working/strategy_research/short_track/daily"
            (daily / "prebreakout_v45_cross_sectional_20260811_failure.json").write_text(
                json.dumps(
                    {
                        "strategy_id": "prebreakout_v45_cross_sectional",
                        "trade_date": "20260811",
                        "status": "failed",
                        "error": "forced fail for test",
                        "counts_toward_expected_denominator": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = publication.build_dual_track_publication(
                workspace=workspace,
                published_repo=published,
                generated_at="2026-08-11T20:30:00+08:00",
            )
            self.assertTrue(result["ok"])
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            self.assertEqual(state["flow_status"], "healthy")
            v45 = next(
                item
                for item in state["short_track_strategies"]
                if item["strategy_id"] == "prebreakout_v45_cross_sectional"
            )
            self.assertEqual(v45["status"], "failed")
            self.assertIn("forced fail", v45.get("failure_reason", ""))
            self.assertEqual(
                [item["candidate_count"] for item in state["short_track_strategies"] if item.get("required_for_publish")],
                [20, 15, 20],
            )

    def test_publish_includes_v45_when_snapshot_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            daily = workspace / "stock_data/03-working/strategy_research/short_track/daily"
            # Clone v44 fixture shape into a present v45 snapshot (20 names).
            v44 = json.loads(
                (daily / "prebreakout_v44_balanced_20260811_candidate_snapshot.json").read_text(
                    encoding="utf-8"
                )
            )
            v45 = dict(v44)
            v45["strategy_id"] = "prebreakout_v45_cross_sectional"
            v45["strategy_version"] = "1.0.0+cs"
            v45["cs_pipeline"] = "v45.1"
            (daily / "prebreakout_v45_cross_sectional_20260811_candidate_snapshot.json").write_text(
                json.dumps(v45, ensure_ascii=False), encoding="utf-8"
            )
            tracking = {
                "artifact_kind": "candidate_tracking_report",
                "strategy_id": "prebreakout_v45_cross_sectional",
                "strategy_version": "1.0.0+cs",
                "operational_status": "healthy",
                "effectiveness_status": "not_validated",
                "execution_authority": "observe_only_no_auto_order",
                "effectiveness_evidence": {
                    "sample_trade_days": 0,
                    "expected_trade_days": 0,
                    "all_gates_pass": False,
                    "failed_gates": [],
                    "decision": "observe_only",
                },
            }
            (daily / "prebreakout_v45_cross_sectional_20260811_candidate_tracking.json").write_text(
                json.dumps(tracking, ensure_ascii=False), encoding="utf-8"
            )
            result = publication.build_dual_track_publication(
                workspace=workspace,
                published_repo=published,
                generated_at="2026-08-11T20:30:00+08:00",
            )
            self.assertTrue(result["ok"])
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            v45_row = next(
                item
                for item in state["short_track_strategies"]
                if item["strategy_id"] == "prebreakout_v45_cross_sectional"
            )
            self.assertEqual(v45_row["status"], "present")
            self.assertEqual(v45_row["candidate_count"], 20)
            self.assertEqual(len(v45_row["candidates"]), 20)

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

    def test_skips_malformed_later_event_tracking_and_keeps_last_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            event_daily = workspace / "stock_data/03-working/strategy_research/event_quality_drift_v1/daily"
            (event_daily / "event_quality_drift_v1_20260811_candidate_tracking.json").write_text(
                json.dumps(
                    {
                        "artifact_kind": "candidate_tracking_report",
                        "signal_date": "20260811",
                        "strategy_id": None,
                        "operational_status": "ok",
                        "execution_authority": "observe_only_no_auto_order",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = publication.build_dual_track_publication(
                workspace=workspace,
                published_repo=published,
                generated_at="2026-08-11T20:30:00+08:00",
            )
            self.assertTrue(result["ok"])
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            self.assertEqual(state["flow_status"], "healthy")
            self.assertEqual(state["event_track"]["signal_date"], "20260810")
            self.assertEqual(state["event_track"]["operational_status"], "healthy_no_eligible_candidates")

    def test_required_snapshot_without_tracking_degrades_without_blocking_core_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            tracking = (
                workspace
                / "stock_data/03-working/strategy_research/short_track/daily"
                / "prebreakout_v43_control_20260811_candidate_tracking.json"
            )
            tracking.unlink()
            result = publication.build_dual_track_publication(workspace=workspace, published_repo=published)
            self.assertTrue(result["ok"])
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            self.assertEqual(state["flow_status"], "degraded")
            control = next(
                row for row in state["short_track_strategies"]
                if row["strategy_id"] == "prebreakout_v43_control"
            )
            self.assertEqual(control["status"], "failed")
            self.assertEqual(control["candidate_count"], 0)
            self.assertFalse(control["required_for_publish"])

    def test_locks_to_recommendation_date_not_later_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            daily = workspace / "stock_data/03-working/strategy_research/short_track/daily"
            for path in list(daily.glob("*_20260811_*.json")):
                later = daily / path.name.replace("20260811", "20260812")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    if payload.get("trade_date") == "20260811":
                        payload["trade_date"] = "20260812"
                    if payload.get("signal_date") == "20260811":
                        payload["signal_date"] = "20260812"
                later.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = publication.build_dual_track_publication(
                workspace=workspace,
                published_repo=published,
                generated_at="2026-08-11T20:30:00+08:00",
            )
            self.assertEqual(result["trade_date"], "20260811")
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            self.assertEqual(state["trade_date"], "20260811")

    def test_locked_date_without_shadow_outputs_publishes_explicit_degraded_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, published = self.make_workspace(Path(tmpdir))
            rec = published / "data/latest/recommendation_state.json"
            rec.write_text(json.dumps({"trade_date": "20260819", "strategies": {}}, ensure_ascii=False), encoding="utf-8")
            result = publication.build_dual_track_publication(workspace=workspace, published_repo=published)
            self.assertTrue(result["ok"])
            state = json.loads((published / "data/latest/prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            self.assertEqual(state["trade_date"], "20260819")
            self.assertEqual(state["flow_status"], "degraded")
            intended = [
                row for row in state["short_track_strategies"]
                if row.get("intended_required_for_publish")
            ]
            self.assertEqual(len(intended), 3)
            self.assertTrue(all(row["status"] == "missing" for row in intended))

    def test_morning_brief_publishes_only_via_v2_publisher(self):
        text = (SCRIPT_DIR / "publish_market_overview.py").read_text(encoding="utf-8")
        self.assertIn("publish_stock_report_v2.sh", text)
        self.assertNotIn("push_git_branch", text)
        self.assertNotIn("commit_and_push", text)

    def test_midday_publishes_only_via_v2_publisher(self):
        text = (SCRIPT_DIR / "generate_midday_analysis.py").read_text(encoding="utf-8")
        self.assertIn("publish_stock_report_v2.sh", text)
        self.assertNotIn("push_git_branch", text)

    def test_stage6_deploys_only_via_v2_publisher(self):
        text = (SCRIPT_DIR / "stage6_deploy_and_notify.py").read_text(encoding="utf-8")
        self.assertIn("publish_stock_report_v2.sh", text)
        self.assertNotIn('["git", "add", "."]', text)
        self.assertNotIn("push_git_branch", text)

    def test_stage4_defers_remote_push_to_v2_publisher(self):
        text = (SCRIPT_DIR / "stage4_ai_publish.py").read_text(encoding="utf-8")
        self.assertIn("deferred_to_v2_publisher", text)
        self.assertNotIn("push_git_branch", text)
        self.assertNotIn("sync_git_tracking_branch", text)

    def test_warehouse_defers_remote_push_to_v2_publisher(self):
        text = (SCRIPT_DIR / "recommendation_warehouse.py").read_text(encoding="utf-8")
        self.assertIn("deferred_to_v2_publisher", text)
        self.assertNotIn("push_git_branch", text)
        self.assertNotIn("sync_git_tracking_branch", text)

    def test_gate_incremental_backfill_calls_v2_publisher(self):
        gate = SCRIPT_DIR.parents[2] / "scripts" / "stock_pipeline_gate.py"
        text = gate.read_text(encoding="utf-8")
        self.assertIn("publish_stock_report_v2.sh", text)

    def test_optional_research_writers_do_not_overwrite_dual_track_contract(self):
        factory = (SCRIPT_DIR / "prebreakout_factory.py").read_text(encoding="utf-8")
        legacy_shadow = (SCRIPT_DIR / "prebreakout_shadow_research.py").read_text(encoding="utf-8")
        self.assertIn('prebreakout_factory_watch.json', factory)
        self.assertIn('prebreakout_shadow_research_watch.json', legacy_shadow)
        self.assertNotIn(
            'PUBLISHED_WATCH = WORKSPACE / "stock-report" / "data" / "latest" / "prebreakout_shadow_watch.json"',
            factory,
        )
        self.assertNotIn(
            'PUBLISHED_WATCH = WORKSPACE / "stock-report" / "data" / "latest" / "prebreakout_shadow_watch.json"',
            legacy_shadow,
        )

    def test_v2_publisher_runs_dual_track_contract_before_sanitizing(self):
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        dual_pos = text.index("dual_track_publication.py")
        sanitize_pos = text.index("sanitize_public_report.py")
        validate_pos = text.index("validate_publication_contract.py")
        self.assertLess(dual_pos, sanitize_pos)
        self.assertLess(dual_pos, validate_pos)

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

    def test_v2_publisher_uses_stock_system_python_runtime(self):
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        self.assertIn("STOCK_SYSTEM_PYTHON", text)
        # 20261009: paths are portable ($STOCK_ROOT); the default must still resolve to the stock venv.
        import subprocess
        header = "\n".join(line for line in text.splitlines()
                           if line.startswith(("STOCK_ROOT=", "WS=", "PY=")))
        resolved = subprocess.run(["/bin/bash", "-c", header + '\nprintf "%s" "$PY"'], capture_output=True,
                                  text=True, env={"HOME": "/Users/fisher", "PATH": "/usr/bin:/bin"}).stdout
        self.assertEqual(resolved, "/Users/fisher/.openclaw/venv/bin/python")


if __name__ == "__main__":
    unittest.main()
