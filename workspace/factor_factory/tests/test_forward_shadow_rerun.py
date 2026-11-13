#!/usr/bin/env python3
"""forward_shadow.run idempotency: rerunning a recorded date after the next open must not log a gap.

Uses a temporary ledger; the official forward_shadow/ ledger is never touched. Needs the real trading
calendar (stock_data/03-working/health/trading_calendar.json) to contain 20261013-20261015.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import forward_shadow as fs  # noqa: E402

AFTER_OPEN = datetime(2026, 10, 15, 10, 0, tzinfo=fs.TZ)  # past the 20261014 entry deadline (10-15 09:30)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AFTER_OPEN


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for sub in ("scores", "runs"):
            (base / sub).mkdir()
        self.P = {"base": base, "books": base / "books.jsonl", "settled": base / "settled.jsonl",
                  "gaps": base / "gaps.jsonl", "state": base / "state.json"}
        self.patches = [mock.patch.object(fs, "paths", lambda test: self.P),
                        mock.patch.object(fs, "datetime", _Clock),
                        mock.patch.object(fs, "committed_and_clean", lambda p: True),
                        mock.patch.object(fs, "settle", lambda P, upto, cal: {"matured_periods": 0})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def gaps(self) -> list[dict]:
        return fs.read_jsonl(self.P["gaps"])

    def test_recorded_date_rerun_after_open_writes_no_gap(self):
        fs.append(self.P["books"], {"signal_date": "20261014", "official": True, "is_rebalance": True})
        before = self.P["books"].read_text()
        for _ in range(2):
            out = fs.run("20261014", test=False)
            self.assertEqual(out["status"], "already_recorded")
        self.assertEqual(self.gaps(), [])
        self.assertEqual(self.P["books"].read_text(), before)

    def test_unrecorded_date_after_open_still_logs_gap(self):
        out = fs.run("20261014", test=False)
        self.assertEqual(out["status"], "gap_logged")
        self.assertEqual([g["signal_date"] for g in self.gaps()], ["20261014"])


if __name__ == "__main__":
    unittest.main()
