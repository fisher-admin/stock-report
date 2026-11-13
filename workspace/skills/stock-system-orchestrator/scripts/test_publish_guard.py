#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_guard


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class PublishGuardLifecycleTests(unittest.TestCase):
    def _workspace(self, root: Path, recommendation: dict) -> tuple[Path, Path]:
        latest = root / "data" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        _write(
            latest / "decision_state.json",
            {
                "trade_date": "20260810",
                "gates": {
                    "freshness_gate": {},
                    "market_gate": {},
                    "strategy_gate": {},
                    "candidate_gate": {},
                },
            },
        )
        _write(latest / "recommendation_state.json", recommendation)
        app_js = root / "assets" / "scripts" / "v2" / "app.js"
        app_js.parent.mkdir(parents=True, exist_ok=True)
        app_js.write_text("function mountAiToggleHandlers() {}", encoding="utf-8")
        return latest, app_js

    def _run(self, latest: Path, app_js: Path) -> tuple[int, dict]:
        with mock.patch.object(publish_guard, "LATEST", latest), mock.patch.object(
            publish_guard,
            "APP_JS",
            app_js,
        ), mock.patch.object(publish_guard, "_git", return_value="abc123"):
            result = publish_guard.main()
        payload = json.loads((latest / "publish_guard_state.json").read_text("utf-8"))
        return result, payload

    def test_archived_strategies_do_not_need_active_strategy_gates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest, app_js = self._workspace(
                Path(tmpdir),
                {
                    "active_strategy_ids": ["prebreakout_v41"],
                    "strategies": {
                        "prebreakout_v41": {"strategy_gate": {"status": "warn"}}
                    },
                    "archived_strategies": {
                        "greenfield_o2c_v1": {},
                        "t1_factor_v1": {},
                    },
                },
            )
            result, payload = self._run(latest, app_js)

        self.assertEqual(result, 0, payload)
        self.assertTrue(payload["ok"])

    def test_declared_active_strategy_without_gate_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest, app_js = self._workspace(
                Path(tmpdir),
                {
                    "active_strategy_ids": ["prebreakout_v41", "greenfield_o2c_v1"],
                    "strategies": {
                        "prebreakout_v41": {"strategy_gate": {"status": "warn"}},
                        "greenfield_o2c_v1": {},
                    },
                    "archived_strategies": {"t1_factor_v1": {}},
                },
            )
            result, payload = self._run(latest, app_js)

        self.assertEqual(result, 1)
        self.assertTrue(any("greenfield_o2c_v1" in failure for failure in payload["failures"]))

    def test_active_and_archived_overlap_fails_lifecycle_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest, app_js = self._workspace(
                Path(tmpdir),
                {
                    "active_strategy_ids": ["prebreakout_v41"],
                    "strategies": {
                        "prebreakout_v41": {"strategy_gate": {"status": "warn"}}
                    },
                    "archived_strategies": {"prebreakout_v41": {}},
                },
            )
            result, payload = self._run(latest, app_js)

        self.assertEqual(result, 1)
        self.assertTrue(
            any("strategy_lifecycle_scope" in failure for failure in payload["failures"])
        )


if __name__ == "__main__":
    unittest.main()
