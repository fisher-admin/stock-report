#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pit_market_backfill as backfill
from test_pit_market_snapshot import FakeClient


class PitMarketBackfillTests(unittest.TestCase):
    def test_select_trade_dates_is_exact_and_rejects_reversed_range(self):
        dates = backfill.select_trade_dates(
            ["20260808", "20260811", "20260812", "20260813"],
            start_date="20260811",
            end_date="20260812",
        )
        self.assertEqual(dates, ["20260811", "20260812"])
        with self.assertRaises(backfill.BackfillError):
            backfill.select_trade_dates(
                ["20260811"], start_date="20260812", end_date="20260811"
            )

    def test_backfill_writes_once_then_reuses_verified_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            client = FakeClient()
            with mock.patch.object(
                backfill.pms,
                "collect_pit_market_snapshot",
                wraps=backfill.pms.collect_pit_market_snapshot,
            ) as collect:
                first = backfill.backfill_pit_market_snapshots(
                    client=client,
                    trade_dates=["20260811"],
                    output_dir=output_dir,
                )
                second = backfill.backfill_pit_market_snapshots(
                    client=client,
                    trade_dates=["20260811"],
                    output_dir=output_dir,
                )

            self.assertEqual(collect.call_count, 1)
            self.assertEqual(first["created_dates"], 1)
            self.assertEqual(second["verified_existing_dates"], 1)
            self.assertTrue((output_dir / "universe_20260811.parquet").exists())
            self.assertTrue((output_dir / "daily_basic_20260811.parquet").exists())
            self.assertTrue((output_dir / "pit_market_snapshot_20260811.json").exists())


if __name__ == "__main__":
    unittest.main()
