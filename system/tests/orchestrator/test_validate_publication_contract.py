#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import validate_publication_contract as contract


def _write(path: Path, name: str, payload: dict) -> None:
    (path / name).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _base_latest(path: Path) -> dict:
    gate = {"status": "pass", "summary": "ok", "reasons": []}
    strategy = {
        "strategy_id": contract.PREBREAKOUT_ID,
        "source_date": "20260810",
        "ai_coverage": {"have": 0, "total": 0, "status": "warn"},
        "research_only": True,
        "strategy_gate": {"status": "warn", "verdict": "research_only"},
        "data_freshness": {"status": "pass"},
        "items": [],
    }
    recommendation = {
        "trade_date": "20260810",
        "active_strategy_ids": [contract.PREBREAKOUT_ID],
        "archived_strategies": {
            contract.O2C_ID: {"lifecycle_status": "archived_historical_only"},
            contract.T1_ID: {"lifecycle_status": "archived_historical_only"},
        },
        "strategies": {contract.PREBREAKOUT_ID: strategy},
    }
    _write(
        path,
        "decision_state.json",
        {
            "trade_date": "20260810",
            "gates": {
                "freshness_gate": gate,
                "market_gate": gate,
                "strategy_gate": gate,
                "candidate_gate": gate,
            },
        },
    )
    _write(path, "recommendation_state.json", recommendation)
    _write(
        path,
        "strategy_run_state.json",
        {
            "trade_date": "20260810",
            "strategies": [{"strategy_id": contract.PREBREAKOUT_ID}],
            "runs": [{"strategy_id": contract.PREBREAKOUT_ID}],
        },
    )
    _write(
        path,
        "review_state_unified.json",
        {
            "strategies": {
                contract.PREBREAKOUT_ID: {
                    "strategy_id": contract.PREBREAKOUT_ID,
                    "strategy_name": "启动前夕",
                }
            },
            "available_dates": [],
            "default_review_date": None,
        },
    )
    _write(path, "adjustment_log.json", {"rows": []})
    _write(path, "system_health.json", {})
    dual_strategies = [
        {
            "strategy_id": strategy_id,
            "candidate_count": count,
            "effectiveness_status": "not_validated",
            "execution_authority": "observe_only_no_auto_order",
            "candidates": [
                {
                    "rank": rank,
                    "ts_code": f"{rank:06d}.SZ",
                    "used_proxy": False,
                    "rank_change": 0,
                }
                for rank in range(1, count + 1)
            ],
        }
        for strategy_id, count in (
            ("prebreakout_v43_control", 20),
            ("prebreakout_v43_top15", 15),
            ("prebreakout_v44_balanced", 20),
        )
    ]
    _write(
        path,
        "prebreakout_shadow_watch.json",
        {
            "contract_version": "dual_track_v1",
            "trade_date": "20260810",
            "flow_status": "healthy",
            "effectiveness_status": "not_validated",
            "execution_authority": "observe_only_no_auto_order",
            "short_track_strategies": dual_strategies,
            "event_track": {
                "strategy_id": "event_quality_drift_v1",
                "execution_authority": "observe_only_no_auto_order",
                "effectiveness_status": "not_validated",
            },
        },
    )
    _write(
        path,
        "strategy_evaluation.json",
        {
            "contract_version": "evaluation_integrity_v2",
            "integrity": {
                "fake_or_impossible_return_count": 0,
                "proxy_rows": 0,
                "rank_changed_rows": 0,
            },
        },
    )
    return recommendation


class PublicationContractLifecycleTests(unittest.TestCase):
    def _run(self, latest: Path) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(contract, "LATEST", latest), mock.patch.object(
            sys,
            "argv",
            ["validate_publication_contract.py", "--strict"],
        ), contextlib.redirect_stdout(output):
            result = contract.main()
        return result, output.getvalue()

    def test_archived_strategies_are_not_required_in_the_active_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)

            result, output = self._run(latest)

        self.assertEqual(result, 0, output)
        self.assertIn("contract v2: 0 fail", output)
        self.assertNotIn("greenfield_o2c_v1 缺", output)
        self.assertNotIn("t1_factor_v1 缺", output)

    def test_public_result_summary_can_replace_local_unified_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            unified = json.loads((latest / "review_state_unified.json").read_text(encoding="utf-8"))
            _write(latest, "review_track_latest.json", unified)
            (latest / "review_state_unified.json").unlink()

            result, output = self._run(latest)

        self.assertEqual(result, 0, output)
        self.assertIn("contract v2: 0 fail", output)

    def test_declared_active_strategy_still_receives_full_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            recommendation = _base_latest(latest)
            recommendation["active_strategy_ids"].append(contract.O2C_ID)
            recommendation["strategies"][contract.O2C_ID] = {
                "strategy_id": contract.O2C_ID,
                "items": [],
            }
            _write(latest, "recommendation_state.json", recommendation)

            result, output = self._run(latest)

        self.assertEqual(result, 1)
        self.assertIn(
            "recommendation_state.strategies.greenfield_o2c_v1 缺 ai_coverage",
            output,
        )

    def test_active_and_archived_strategy_sets_may_not_overlap(self):
        errors: list[str] = []
        active = contract._active_strategy_scope(
            {
                "active_strategy_ids": [contract.PREBREAKOUT_ID],
                "strategies": {contract.PREBREAKOUT_ID: {}},
                "archived_strategies": {contract.PREBREAKOUT_ID: {}},
            },
            errors,
        )

        self.assertEqual(active, [contract.PREBREAKOUT_ID])
        self.assertTrue(any("活跃与归档策略重叠" in error for error in errors))

    def test_dual_track_candidate_counts_are_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            state = json.loads((latest / "prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
            state["short_track_strategies"][1]["candidate_count"] = 14
            state["short_track_strategies"][1]["candidates"] = state["short_track_strategies"][1]["candidates"][:14]
            _write(latest, "prebreakout_shadow_watch.json", state)

            result, output = self._run(latest)

        self.assertEqual(result, 1)
        self.assertIn("prebreakout_v43_top15 候选数必须为 15", output)

    def test_evaluation_integrity_rejects_impossible_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            evaluation = json.loads((latest / "strategy_evaluation.json").read_text(encoding="utf-8"))
            evaluation["integrity"]["fake_or_impossible_return_count"] = 1
            _write(latest, "strategy_evaluation.json", evaluation)

            result, output = self._run(latest)

        self.assertEqual(result, 1)
        self.assertIn("评价库仍含伪造或不可能收益", output)

    def test_public_review_json_rejects_impossible_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            review = json.loads((latest / "review_state_unified.json").read_text(encoding="utf-8"))
            review["strategies"][contract.PREBREAKOUT_ID]["date_stats"] = [
                {"recommend_date": "20260429", "avg_cumulative_return_pct": -100.3}
            ]
            _write(latest, "review_state_unified.json", review)

            result, output = self._run(latest)

        self.assertEqual(result, 1)
        self.assertIn("公开发布文件仍含不可能收益", output)

    def test_public_history_csv_rejects_impossible_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            (latest / "recommendation_history.csv").write_text(
                "推荐日,次日收益%,累计收益%\n20260429,-0.3,-100.3\n",
                encoding="utf-8",
            )

            result, output = self._run(latest)

        self.assertEqual(result, 1)
        self.assertIn("公开发布文件仍含不可能收益", output)

    def test_public_industry_heatmap_rejects_impossible_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            latest = root / "data/latest"
            latest.mkdir(parents=True)
            _base_latest(latest)
            analytics = root / "data/recommendation_analytics"
            analytics.mkdir(parents=True)
            _write(
                analytics,
                "industry_heatmap.json",
                {"rows": [{"avg_cumulative_return_pct": -100.3}]},
            )

            result, output = self._run(latest)

        self.assertEqual(result, 1)
        self.assertIn("公开发布文件仍含不可能收益", output)


if __name__ == "__main__":
    unittest.main()
