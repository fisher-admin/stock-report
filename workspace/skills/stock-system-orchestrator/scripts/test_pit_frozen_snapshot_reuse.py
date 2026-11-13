#!/usr/bin/env python3
"""2026-10-30: consumers reuse the evening-frozen PIT day instead of refetching."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pit_market_snapshot as pms  # noqa: E402


class FrozenSnapshotReuseTests(unittest.TestCase):
    def test_missing_day_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pms.load_frozen_pit_market_snapshot(Path(tmp), "20261030"))

    def test_half_frozen_day_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            pd.DataFrame({"trade_date": ["20261030"], "ts_code": ["000001.SZ"]}).to_parquet(
                Path(tmp) / "universe_20261030.parquet")
            self.assertIsNone(pms.load_frozen_pit_market_snapshot(Path(tmp), "20261030"))

    def test_frozen_day_is_loaded_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = pd.DataFrame({"trade_date": ["20261030"] * 2, "ts_code": ["000002.SZ", "000001.SZ"]})
            frame.to_parquet(Path(tmp) / "universe_20261030.parquet")
            frame.assign(pb=[2.0, 1.0]).to_parquet(Path(tmp) / "daily_basic_20261030.parquet")
            snap = pms.load_frozen_pit_market_snapshot(Path(tmp), "20261030")
        self.assertEqual(snap["trade_date"], "20261030")
        self.assertEqual(snap["universe"]["ts_code"].tolist(), ["000001.SZ", "000002.SZ"])
        self.assertEqual(snap["daily_basic"]["pb"].tolist(), [1.0, 2.0])

    def test_wrong_date_inside_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = pd.DataFrame({"trade_date": ["20261029"], "ts_code": ["000001.SZ"]})
            frame.to_parquet(Path(tmp) / "universe_20261030.parquet")
            frame.to_parquet(Path(tmp) / "daily_basic_20261030.parquet")
            with self.assertRaises(pms.PitSnapshotIncomplete):
                pms.load_frozen_pit_market_snapshot(Path(tmp), "20261030")


if __name__ == "__main__":
    unittest.main()
