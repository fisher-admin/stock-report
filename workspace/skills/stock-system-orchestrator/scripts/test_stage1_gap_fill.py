#!/usr/bin/env python3
"""2026-10-23: after a multi-day outage Stage 1 only fetched the target day, so
10-20/10-21 caches stayed missing and downstream evaluators failed for days."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from test_trading_calendar_stage1 import load_stage1_module


class Stage1GapFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_stage1_module()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cache = root / "cache"
        self.health = root / "health"
        self.cache.mkdir()
        self.health.mkdir()
        for name in ("CACHE", "HEALTH_DIR", "WORKSPACE"):
            patcher = mock.patch.object(self.module, name, {"CACHE": self.cache, "HEALTH_DIR": self.health,
                                                             "WORKSPACE": root}[name])
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _touch(self, day: str, types=("stk_factor", "daily", "cyq_perf")) -> None:
        for data_type in types:
            (self.cache / f"{data_type}_{day}.parquet").write_bytes(b"x")

    def test_missed_days_are_open_days_before_target_with_incomplete_caches(self):
        cached = {"stk_factor": {"20261019", "20261020"}, "daily": {"20261019", "20261020"},
                  "cyq_perf": {"20261019"}}
        days = self.module.missed_trade_days(
            "20261022", ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023"], cached,
            max_days=3)
        self.assertEqual(days, ["20261020", "20261021"])

    def test_window_is_bounded(self):
        days = self.module.missed_trade_days("20261022", ["20261019", "20261020", "20261021"], {}, max_days=1)
        self.assertEqual(days, ["20261021"])
        self.assertEqual(self.module.missed_trade_days("20261022", ["20261021"], {}, max_days=0), [])

    def test_fill_fetches_missing_days_and_never_touches_latest_pointers(self):
        self._touch("20261019")
        fetched = []

        def fake_get_cached(pro, day, data_type):
            fetched.append((day, data_type))
            frame = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(1200)],
                                  "source_provider": "rds"})
            if data_type == "cyq_perf":
                frame = frame.assign(cost_15pct=range(1200), cost_50pct=range(1200), cost_85pct=range(1200),
                                     weight_avg=range(1200), winner_rate=range(1200))
            frame.to_parquet(self.cache / f"{data_type}_{day}.parquet")
            return frame

        with mock.patch.object(self.module.bt, "get_cached", fake_get_cached, create=True), \
                mock.patch.object(self.module, "PointInTimeStore") as store:
            result = self.module.fill_missed_trade_days(
                None, "20261022", {"open_dates": ["20261019", "20261020", "20261021", "20261022"]})

        self.assertEqual(result["filled"], ["20261020", "20261021"])
        self.assertEqual({day for day, _ in fetched}, {"20261020", "20261021"})
        self.assertEqual(store.return_value.put.call_count, 6)
        evidence = json.loads((self.health / "data_ready_20261020.json").read_text())
        self.assertEqual(evidence["mode"], "gap_fill")
        self.assertTrue(evidence["ok"])
        self.assertFalse((self.health / "data_preparation_run.json").exists())
        self.assertFalse(list(self.health.rglob("*_latest.json")))

    def test_provider_failure_is_recorded_not_raised(self):
        def boom(pro, day, data_type):
            raise RuntimeError("rds HTTP 503")

        with mock.patch.object(self.module.bt, "get_cached", boom, create=True), \
                mock.patch.object(self.module, "PointInTimeStore"):
            result = self.module.fill_missed_trade_days(None, "20261022", {"open_dates": ["20261021"]})
        self.assertEqual(result["filled"], [])
        self.assertIn("rds HTTP 503", " ".join(result["failed"]["20261021"]))

    def test_exhausted_budget_skips_remaining_days(self):
        ticks = iter([0.0, 0.0, 100.0, 100.0])
        with mock.patch.object(self.module.bt, "get_cached", lambda *a: None, create=True), \
                mock.patch.object(self.module, "PointInTimeStore"):
            result = self.module.fill_missed_trade_days(
                None, "20261022", {"open_dates": ["20261020", "20261021"]},
                budget_seconds=50, clock=lambda: next(ticks))
        self.assertEqual(result["skipped_budget"], ["20261021"])


    def test_pit_snapshot_frozen_once_and_never_refetched(self):
        calls = []

        def collect(pro, day):
            calls.append(day)
            frame = pd.DataFrame({"trade_date": [day] * 2, "ts_code": ["000001.SZ", "000002.SZ"]})
            return {"trade_date": day, "universe": frame, "daily_basic": frame.assign(pb=[1.0, 2.0])}

        store = Path(self.tmp.name) / "pit_v2"
        first = self.module.freeze_pit_market_snapshot(None, "20261030", store=store, collect=collect)
        self.assertEqual(first["status"], "frozen")
        self.assertEqual(sorted(first["written"]), ["daily_basic", "universe"])
        second = self.module.freeze_pit_market_snapshot(None, "20261030", store=store, collect=collect)
        self.assertEqual(second["status"], "already_frozen")
        self.assertEqual(calls, ["20261030"])
        self.assertEqual(pd.read_parquet(store / "daily_basic_20261030.parquet")["pb"].tolist(), [1.0, 2.0])

    def test_pit_snapshot_date_mismatch_is_rejected(self):
        frame = pd.DataFrame({"trade_date": ["20261029"], "ts_code": ["000001.SZ"]})
        with self.assertRaises(ValueError):
            self.module.freeze_pit_market_snapshot(
                None, "20261030", store=Path(self.tmp.name) / "pit_v2",
                collect=lambda pro, day: {"trade_date": "20261029", "universe": frame, "daily_basic": frame})


    def test_truncated_stk_factor_fails_completeness(self):
        refreshed = {"daily": {"row_count": 5561}, "stk_factor": {"row_count": 4139}, "cyq_perf": {"row_count": 5561}}
        issues = self.module.completeness_issues("20261012", refreshed)
        self.assertEqual(len(issues), 1)
        self.assertIn("stk_factor incomplete_vs_daily", issues[0])
        healthy = {"daily": {"row_count": 5561}, "stk_factor": {"row_count": 5558}, "cyq_perf": {"row_count": 5352}}
        self.assertEqual(self.module.completeness_issues("20261012", healthy), [])

    def test_partial_cached_day_is_quarantined_and_refetched(self):
        self._touch("20261009")
        pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(5561)]}).to_parquet(self.cache / "daily_20261012.parquet")
        pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(4139)]}).to_parquet(self.cache / "stk_factor_20261012.parquet")
        pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(5561)]}).to_parquet(self.cache / "cyq_perf_20261012.parquet")
        refetched = []

        def fake_get_cached(pro, day, data_type):
            path = self.cache / f"{data_type}_{day}.parquet"
            if path.exists():
                return pd.read_parquet(path)
            refetched.append((day, data_type))
            frame = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(5560)], "source_provider": "rds"})
            frame.to_parquet(path)
            return frame

        with mock.patch.object(self.module.bt, "get_cached", fake_get_cached, create=True), \
                mock.patch.object(self.module, "PointInTimeStore"), \
                mock.patch.object(self.module, "inspect_cache_quality",
                                  side_effect=lambda day, refreshed: (not self.module.completeness_issues(day, refreshed),
                                                                      self.module.completeness_issues(day, refreshed))):
            result = self.module.fill_missed_trade_days(
                None, "20261013", {"open_dates": ["20261009", "20261012", "20261013"]})
        self.assertEqual(refetched, [("20261012", "stk_factor")])
        self.assertEqual(result["filled"], ["20261012"])
        self.assertEqual(len(result["quarantined"]), 1)
        self.assertEqual(len(pd.read_parquet(self.cache / "stk_factor_20261012.parquet")), 5560)
        self.assertTrue(list(self.cache.glob("stk_factor_20261012.parquet.incomplete-*")))


if __name__ == "__main__":
    unittest.main()
