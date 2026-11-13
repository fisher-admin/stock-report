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

import short_track_incremental_verify as verify  # noqa: E402


def frame(date, close=10.0, codes=("000001.SZ", "000002.SZ")):
    return pd.DataFrame({"ts_code": list(codes), "trade_date": date, "open_qfq": close, "high_qfq": close,
                         "low_qfq": close, "close_qfq": close, "macd_dif": 0.1, "macd_dea": 0.05})


class IncrementalVerifyTests(unittest.TestCase):
    def test_window_is_the_latest_incremental_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            for date, target in [("20261028", "20261029"), ("20261029", "20261029"), ("20261027", None)]:
                f = frame(date)
                f.attrs["query"] = {"trade_date": date, **({"incremental_rebase_to": target} if target else {})}
                f.to_parquet(cache / f"stk_factor_{date}.parquet")
            with mock.patch.object(verify, "INPUT_CACHE", cache):
                self.assertEqual(verify.incremental_window(), ("20261029", ["20261028", "20261029"]))

    def test_rounding_level_differences_agree(self):
        used = {"d": frame("d", 10.0)}
        full = {"d": frame("d", 10.0001)}
        result = verify.compare_cells(used, full)
        self.assertEqual(result["beyond_tolerance"], {})
        self.assertEqual(result["code_set_differences"], {})

    def test_real_differences_are_reported(self):
        used = {"d": frame("d", 10.0, codes=("000001.SZ",))}
        full = {"d": frame("d", 10.5)}
        result = verify.compare_cells(used, full)
        self.assertEqual(result["code_set_differences"], {"d": ["000002.SZ"]})
        self.assertEqual(result["beyond_tolerance"]["close_qfq"], 1)

    def test_finished_check_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "summary.json").write_text(json.dumps({"ok_days": ["a"] * 5, "done": True}))
            with mock.patch.object(verify, "STATE_DIR", state), \
                    mock.patch.object(verify, "incremental_window") as window:
                self.assertEqual(verify.main(), 0)
            window.assert_not_called()


if __name__ == "__main__":
    unittest.main()
