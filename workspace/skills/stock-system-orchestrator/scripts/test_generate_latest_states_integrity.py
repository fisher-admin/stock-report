#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_latest_states as states  # noqa: E402


class GenerateLatestStatesIntegrityTests(unittest.TestCase):
    def _write_cyq(self, cache_dir: Path, trade_date: str) -> None:
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "cost_15pct": [9.0],
                "cost_50pct": [10.0],
                "cost_85pct": [11.0],
                "weight_avg": [10.0],
                "winner_rate": [55.0],
            }
        ).to_parquet(cache_dir / f"cyq_perf_{trade_date}.parquet")

    def test_chip_factors_require_same_day_non_proxy_health_evidence(self):
        trade_date = "20260811"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            health_dir = root / "health"
            cache_dir.mkdir()
            health_dir.mkdir()
            self._write_cyq(cache_dir, trade_date)

            health_path = health_dir / f"data_ready_{trade_date}.json"
            health_path.write_text(
                json.dumps(
                    {
                        "target_trade_date": trade_date,
                        "ok": True,
                        "cyq_perf_proxy_derived": True,
                        "cyq_perf_provider": "local_same_day_proxy",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(states, "CYQ_CACHE_DIR", cache_dir),
                mock.patch.object(states, "WORKING_HEALTH_DIR", health_dir),
            ):
                self.assertEqual(states.load_chip_factors(trade_date), {})

                health_path.write_text(
                    json.dumps(
                        {
                            "target_trade_date": trade_date,
                            "ok": True,
                            "cyq_perf_proxy_derived": False,
                            "cyq_perf_provider": "tushare_cached_fetch",
                        }
                    ),
                    encoding="utf-8",
                )
                factors = states.load_chip_factors(trade_date)

            self.assertIn("000001", factors)
            self.assertEqual(factors["000001"]["winner_rate"], 55.0)


if __name__ == "__main__":
    unittest.main()
