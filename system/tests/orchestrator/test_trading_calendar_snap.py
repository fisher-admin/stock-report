#!/usr/bin/env python3
"""非交易日（周末 / 国庆长假）运行时，目标交易日必须收敛到最近开市日。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import orchestrator_common
import publish_guard
import trading_calendar_store as tcs

# 2026 国庆：10-01 ~ 10-07 休市，09-30 为节前最后一个交易日，10-08 复市。
GOLDEN_WEEK_OPEN_DATES = [
    "20260925", "20260928", "20260929", "20260930",
    "20261008", "20261009", "20261012", "20261013",
]
GOLDEN_WEEK_CALENDAR = {"from": "20260920", "to": "20261031", "open_dates": GOLDEN_WEEK_OPEN_DATES}


class _CalendarFileMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "trading_calendar.json"
        path.write_text(json.dumps(GOLDEN_WEEK_CALENDAR), encoding="utf-8")
        patcher = mock.patch.object(tcs, "DEFAULT_CALENDAR_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class SnapToOpenTradeDateTests(unittest.TestCase):
    def test_golden_week_holidays_snap_to_last_pre_holiday_session(self):
        for holiday in ("20261001", "20261002", "20261004", "20261007"):
            with self.subTest(holiday=holiday):
                self.assertEqual(tcs.snap_to_open_trade_date(holiday, calendar=GOLDEN_WEEK_CALENDAR), "20260930")

    def test_open_days_are_unchanged(self):
        for open_day in ("20260930", "20261008", "20261009"):
            with self.subTest(open_day=open_day):
                self.assertEqual(tcs.snap_to_open_trade_date(open_day, calendar=GOLDEN_WEEK_CALENDAR), open_day)

    def test_weekend_inside_calendar_snaps_to_friday(self):
        self.assertEqual(tcs.snap_to_open_trade_date("20261010", calendar=GOLDEN_WEEK_CALENDAR), "20261009")
        self.assertEqual(tcs.snap_to_open_trade_date("20261011", calendar=GOLDEN_WEEK_CALENDAR), "20261009")

    def test_without_calendar_weekends_still_snap_to_friday(self):
        self.assertEqual(tcs.snap_to_open_trade_date("20261010", calendar={}), "20261009")
        self.assertEqual(tcs.snap_to_open_trade_date("20261011", calendar={}), "20261009")
        self.assertEqual(tcs.snap_to_open_trade_date("20261014", calendar={}), "20261014")

    def test_date_beyond_calendar_coverage_falls_back_to_weekend_rule(self):
        calendar = {"to": "20261013", "open_dates": GOLDEN_WEEK_OPEN_DATES}
        self.assertEqual(tcs.snap_to_open_trade_date("20261120", calendar=calendar), "20261120")
        self.assertEqual(tcs.snap_to_open_trade_date("20261121", calendar=calendar), "20261120")

    def test_accepts_dashed_dates_and_rejects_garbage(self):
        self.assertEqual(tcs.snap_to_open_trade_date("2026-10-02", calendar=GOLDEN_WEEK_CALENDAR), "20260930")
        self.assertEqual(tcs.snap_to_open_trade_date("", calendar=GOLDEN_WEEK_CALENDAR), "")
        self.assertEqual(tcs.snap_to_open_trade_date("20261399", calendar=GOLDEN_WEEK_CALENDAR), "")


class LatestCompletedTradeDateTests(unittest.TestCase):
    def test_first_session_after_holiday_before_close_uses_pre_holiday_session(self):
        now = datetime(2026, 10, 8, 8, 0)
        self.assertEqual(tcs.latest_completed_trade_date(now, calendar=GOLDEN_WEEK_CALENDAR), "20260930")

    def test_after_close_uses_today(self):
        now = datetime(2026, 10, 8, 19, 30)
        self.assertEqual(tcs.latest_completed_trade_date(now, calendar=GOLDEN_WEEK_CALENDAR), "20261008")

    def test_holiday_evening_uses_pre_holiday_session(self):
        now = datetime(2026, 10, 2, 19, 30)
        self.assertEqual(tcs.latest_completed_trade_date(now, calendar=GOLDEN_WEEK_CALENDAR), "20260930")

    def test_monday_morning_uses_previous_friday(self):
        now = datetime(2026, 10, 12, 8, 0)
        self.assertEqual(tcs.latest_completed_trade_date(now, calendar=GOLDEN_WEEK_CALENDAR), "20261009")


class RequestedTargetTradeDateTests(_CalendarFileMixin, unittest.TestCase):
    def test_holiday_env_target_is_snapped(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20261002"}):
            self.assertEqual(tcs.requested_target_trade_date(), "20260930")

    def test_unset_or_invalid_env_target_is_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tcs.requested_target_trade_date(), "")
        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "today"}):
            self.assertEqual(tcs.requested_target_trade_date(), "")

    def test_secondary_keys_are_honoured_when_requested(self):
        keys = ("OPENCLAW_TARGET_TRADE_DATE", "TARGET_TRADE_DATE")
        with mock.patch.dict(os.environ, {"TARGET_TRADE_DATE": "20261004"}, clear=True):
            self.assertEqual(tcs.requested_target_trade_date(keys), "20260930")


class OrchestratorCommonTradeDateTests(_CalendarFileMixin, unittest.TestCase):
    def test_resolve_effective_trade_date_snaps_holiday_candidate(self):
        self.assertEqual(orchestrator_common.resolve_effective_trade_date("", None, "20261002", "20261008"), "20260930")

    def test_resolve_effective_trade_date_keeps_open_day(self):
        self.assertEqual(orchestrator_common.resolve_effective_trade_date("20261008"), "20261008")

    def test_fallback_now_uses_latest_completed_trade_date(self):
        real = tcs.latest_completed_trade_date
        with mock.patch.object(tcs, "latest_completed_trade_date", lambda: real(datetime(2026, 10, 2, 19, 30))):
            self.assertEqual(orchestrator_common.resolve_effective_trade_date(None, "", fallback_now=True), "20260930")

    def test_attach_run_metadata_snaps_holiday_env_target(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20261002"}):
            payload = orchestrator_common.attach_run_metadata({})
        self.assertEqual(payload["run"]["trade_date"], "20260930")

    def test_attach_run_metadata_explicit_trade_date_wins(self):
        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20261002"}):
            payload = orchestrator_common.attach_run_metadata({}, trade_date="20261008")
        self.assertEqual(payload["run"]["trade_date"], "20261008")


class RunnerTradeDateTests(_CalendarFileMixin, unittest.TestCase):
    def test_short_track_runner_snaps_holiday_env_target(self):
        import short_track_shadow_runner

        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20261002"}):
            self.assertEqual(short_track_shadow_runner.resolve_trade_date(Path(self._tmp.name)), "20260930")

    def test_recipe_engine_snaps_holiday_env_target(self):
        import prebreakout_recipe_engine

        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20261005"}):
            self.assertEqual(prebreakout_recipe_engine.resolve_trade_date(), "20260930")


class PublishGuardHolidayFreshnessTests(_CalendarFileMixin, unittest.TestCase):
    def _run_guard(self, decision_trade_date: str, now: datetime, factory: dict | None = None) -> dict:
        root = Path(self._tmp.name)
        latest = root / "data" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        gates = {k: {} for k in ("freshness_gate", "market_gate", "strategy_gate", "candidate_gate")}
        (latest / "decision_state.json").write_text(
            json.dumps({"trade_date": decision_trade_date, "gates": gates}), encoding="utf-8"
        )
        (latest / "recommendation_state.json").write_text(
            json.dumps({
                "active_strategy_ids": ["prebreakout_v41"],
                "strategies": {"prebreakout_v41": {"strategy_gate": {"status": "warn"}}},
                "archived_strategies": {},
            }),
            encoding="utf-8",
        )
        if factory is not None:
            (latest / "prebreakout_factory_watch.json").write_text(json.dumps(factory), encoding="utf-8")
        app_js = root / "app.js"
        app_js.write_text("function mountAiToggleHandlers() {}", encoding="utf-8")
        real = tcs.latest_completed_trade_date
        with mock.patch.object(publish_guard, "LATEST", latest), \
                mock.patch.object(publish_guard, "APP_JS", app_js), \
                mock.patch.object(publish_guard, "_git", return_value="abc123"), \
                mock.patch.object(publish_guard, "latest_completed_trade_date", lambda *_: real(now)):
            publish_guard.main()
        return json.loads((latest / "publish_guard_state.json").read_text("utf-8"))

    def _check(self, payload: dict, name: str) -> dict:
        return next(item for item in payload["checks"] if item["name"] == name)

    def test_pre_holiday_data_is_fresh_on_first_morning_after_golden_week(self):
        payload = self._run_guard("20260930", datetime(2026, 10, 8, 8, 0))
        self.assertTrue(self._check(payload, "data_freshness")["ok"], payload)

    def test_data_two_sessions_behind_warns(self):
        payload = self._run_guard("20260930", datetime(2026, 10, 9, 19, 30))
        check = self._check(payload, "data_freshness")
        self.assertFalse(check["ok"])
        self.assertIn("落后 2 个交易日", check["detail"])

    def test_stale_factory_watch_warns(self):
        payload = self._run_guard(
            "20261008",
            datetime(2026, 10, 9, 12, 0),
            factory={"trade_date": "20260930", "generated_at": "2026-10-02 23:15:22"},
        )
        check = self._check(payload, "factory_watch_freshness")
        self.assertFalse(check["ok"])
        self.assertTrue(payload["ok"], "factory watch staleness must stay a warning")
        self.assertTrue(any("factory_watch_freshness" in w for w in payload["warnings"]))

    def test_current_factory_watch_passes(self):
        payload = self._run_guard(
            "20261008",
            datetime(2026, 10, 9, 12, 0),
            factory={"trade_date": "20261008", "generated_at": "2026-10-08 23:15:00"},
        )
        self.assertTrue(self._check(payload, "factory_watch_freshness")["ok"])


if __name__ == "__main__":
    unittest.main()
