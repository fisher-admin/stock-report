#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evening_chain_retry as retry  # noqa: E402


class EveningChainRetryDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / "evening_retry_20261023.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_successful_evening_is_left_alone(self):
        manifest = {"target_trade_date": "20261023", "status": "succeeded"}
        self.assertEqual(retry.decide("20261023", manifest, locked=False, marker=self.marker), "already_ok")

    def test_yesterdays_success_does_not_count_for_today(self):
        manifest = {"target_trade_date": "20261022", "status": "succeeded"}
        self.assertEqual(retry.decide("20261023", manifest, locked=False, marker=self.marker), "retry")

    def test_failed_evening_is_retried_once(self):
        manifest = {"target_trade_date": "20261023", "status": "failed"}
        self.assertEqual(retry.decide("20261023", manifest, locked=False, marker=self.marker), "retry")
        self.marker.write_text("{}")
        self.assertEqual(retry.decide("20261023", manifest, locked=False, marker=self.marker), "already_retried")

    def test_running_pipeline_is_not_disturbed(self):
        manifest = {"target_trade_date": "20261023", "status": "running"}
        self.assertEqual(retry.decide("20261023", manifest, locked=True, marker=self.marker), "running")

    def test_market_gate_halt_is_not_retried(self):
        manifest = {"target_trade_date": "20261026", "status": "failed", "failure_category": "market_halt"}
        self.assertEqual(retry.decide("20261026", manifest, locked=False, marker=self.marker), "market_halt")

    def test_lock_with_dead_owner_is_not_held(self):
        lockdir = Path(self.tmp.name) / "lockdir"
        lockdir.mkdir()
        (lockdir / "owner.txt").write_text("999999|x|t\n")
        self.assertFalse(retry.lock_held(lockdir))

    def test_missing_lock_is_not_held(self):
        self.assertFalse(retry.lock_held(Path(self.tmp.name) / "absent"))


if __name__ == "__main__":
    unittest.main()
