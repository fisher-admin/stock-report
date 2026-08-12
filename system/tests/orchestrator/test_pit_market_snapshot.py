#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pit_market_snapshot as pms  # noqa: E402


class FakeClient:
    def __init__(self):
        self.frames = {
            ("stock_basic", "L"): pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000002.SZ"],
                    "symbol": ["000001", "000002"],
                    "name": ["平安银行", "ST测试"],
                    "industry": ["银行", "银行"],
                    "market": ["主板", "主板"],
                    "list_date": ["20200101", "20200101"],
                    "delist_date": ["", ""],
                    "list_status": ["L", "L"],
                }
            ),
            ("stock_basic", "D"): pd.DataFrame(
                {
                    "ts_code": ["000003.SZ"],
                    "symbol": ["000003"],
                    "name": ["退市样本"],
                    "industry": ["银行"],
                    "market": ["主板"],
                    "list_date": ["20180101"],
                    "delist_date": ["20260801"],
                    "list_status": ["D"],
                }
            ),
            ("stock_basic", "P"): pd.DataFrame(
                {
                    "ts_code": ["000004.SZ"],
                    "symbol": ["000004"],
                    "name": ["停牌样本"],
                    "industry": ["电子"],
                    "market": ["主板"],
                    "list_date": ["20190101"],
                    "delist_date": [""],
                    "list_status": ["P"],
                }
            ),
            ("stock_st", "20260811"): pd.DataFrame({"ts_code": ["000002.SZ"]}),
            ("suspend_d", "20260811"): pd.DataFrame({"ts_code": ["000004.SZ"]}),
            ("daily_basic", "20260811"): pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
                    "trade_date": ["20260811"] * 4,
                    "close": [10.2, 9.8, 4.0, 8.5],
                    "turnover_rate": [1.2, 0.8, 0.2, 0.0],
                    "turnover_rate_f": [1.0, 0.7, 0.1, 0.0],
                    "volume_ratio": [1.1, 0.9, 0.5, 0.0],
                    "pe": [8.0, 9.0, 5.0, 7.0],
                    "pe_ttm": [8.5, 9.5, 5.5, 7.5],
                    "pb": [0.8, 0.9, 0.6, 0.7],
                    "ps": [1.1, 1.2, 0.7, 0.8],
                    "ps_ttm": [1.0, 1.1, 0.6, 0.7],
                    "dv_ratio": [2.0, 1.5, 0.0, 0.0],
                    "dv_ttm": [2.1, 1.6, 0.0, 0.0],
                    "total_share": [100.0, 110.0, 90.0, 95.0],
                    "float_share": [80.0, 85.0, 60.0, 65.0],
                    "free_share": [70.0, 75.0, 55.0, 60.0],
                    "total_mv": [1000.0, 950.0, 300.0, 500.0],
                    "circ_mv": [800.0, 760.0, 250.0, 420.0],
                }
            ),
            ("index_classify", "SW2021"): pd.DataFrame(
                {
                    "index_code": ["801780.SI", "801080.SI"],
                    "industry_name": ["银行", "电子"],
                    "level": ["L1", "L1"],
                    "src": ["SW2021", "SW2021"],
                }
            ),
            ("index_member_all", "801780.SI"): pd.DataFrame(
                {
                    "l1_code": ["801780.SI", "801780.SI"],
                    "l1_name": ["银行", "银行"],
                    "ts_code": ["000001.SZ", "000003.SZ"],
                    "in_date": ["20200101", "20180101"],
                    "out_date": ["", "20260801"],
                    "is_new": ["Y", "N"],
                }
            ),
            ("index_member_all", "801080.SI"): pd.DataFrame(
                {
                    "l1_code": ["801080.SI"],
                    "l1_name": ["电子"],
                    "ts_code": ["000004.SZ"],
                    "in_date": ["20190101"],
                    "out_date": [""],
                    "is_new": ["Y"],
                }
            ),
        }

    def stock_basic(self, *, list_status: str, fields: str | None = None):
        return self.frames[("stock_basic", list_status)].copy()

    def stock_st(self, *, trade_date: str):
        return self.frames[("stock_st", trade_date)].copy()

    def suspend_d(self, *, trade_date: str):
        return self.frames[("suspend_d", trade_date)].copy()

    def daily_basic(self, *, trade_date: str, fields: str | None = None):
        return self.frames[("daily_basic", trade_date)].copy()

    def index_classify(self, *, src: str, level: str):
        return self.frames[("index_classify", src)].copy()

    def index_member_all(self, *, l1_code: str):
        return self.frames[("index_member_all", l1_code)].copy()


class PitMarketSnapshotTests(unittest.TestCase):
    def test_collect_pit_market_snapshot_builds_compatible_frames(self):
        snapshot = pms.collect_pit_market_snapshot(FakeClient(), "20260811")
        universe = snapshot["universe"]
        daily_basic = snapshot["daily_basic"]

        self.assertEqual(list(universe["ts_code"]), ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"])
        self.assertIn("universe_flag", universe.columns)
        self.assertIn("tradable", universe.columns)
        self.assertIn("listing_days", universe.columns)
        self.assertIn("sw2021_l1_name", universe.columns)
        self.assertEqual(universe.loc[universe["ts_code"] == "000001.SZ", "universe_flag"].iloc[0], 1)
        self.assertEqual(universe.loc[universe["ts_code"] == "000002.SZ", "universe_flag"].iloc[0], 0)
        self.assertEqual(universe.loc[universe["ts_code"] == "000003.SZ", "sw2021_l1_name"].iloc[0], None)
        self.assertEqual(universe.loc[universe["ts_code"] == "000003.SZ", "universe_flag"].iloc[0], 0)
        self.assertTrue((universe["used_proxy"] == False).all())  # noqa: E712
        self.assertTrue((daily_basic["used_proxy"] == False).all())  # noqa: E712
        self.assertEqual(snapshot["metadata"]["completeness"], "complete")

    def test_historical_membership_uses_listing_and_delisting_dates_not_current_status(self):
        client = FakeClient()
        historical_date = "20260731"
        client.frames[("stock_st", historical_date)] = pd.DataFrame({"ts_code": []})
        client.frames[("suspend_d", historical_date)] = pd.DataFrame({"ts_code": []})
        daily = client.frames[("daily_basic", "20260811")].copy()
        daily["trade_date"] = historical_date
        client.frames[("daily_basic", historical_date)] = daily

        snapshot = pms.collect_pit_market_snapshot(client, historical_date)
        universe = snapshot["universe"].set_index("ts_code")

        self.assertEqual(universe.loc["000003.SZ", "current_list_status"], "D")
        self.assertEqual(universe.loc["000003.SZ", "list_status_at_date"], "L")
        self.assertEqual(universe.loc["000003.SZ", "universe_flag"], 1)
        self.assertEqual(universe.loc["000004.SZ", "list_status_at_date"], "L")
        self.assertEqual(universe.loc["000004.SZ", "universe_flag"], 1)
        self.assertEqual(universe.loc["000003.SZ", "sw2021_l1_name"], "银行")

    def test_future_listing_and_wrong_date_payload_fail_closed(self):
        client = FakeClient()
        future = client.frames[("stock_basic", "L")].iloc[[0]].copy()
        future["ts_code"] = "000005.SZ"
        future["name"] = "未来上市"
        future["list_date"] = "20260901"
        client.frames[("stock_basic", "L")] = pd.concat(
            [client.frames[("stock_basic", "L")], future], ignore_index=True
        )
        daily = client.frames[("daily_basic", "20260811")].iloc[[0]].copy()
        daily["ts_code"] = "000005.SZ"
        client.frames[("daily_basic", "20260811")] = pd.concat(
            [client.frames[("daily_basic", "20260811")], daily], ignore_index=True
        )
        snapshot = pms.collect_pit_market_snapshot(client, "20260811")
        future_row = snapshot["universe"].set_index("ts_code").loc["000005.SZ"]
        self.assertEqual(future_row["list_status_at_date"], "not_listed")
        self.assertEqual(future_row["universe_flag"], 0)

        client.frames[("daily_basic", "20260811")]["trade_date"] = "20260810"
        with self.assertRaises(pms.PitSnapshotIncomplete):
            pms.collect_pit_market_snapshot(client, "20260811")

    def test_collect_pit_market_snapshot_fails_closed_on_missing_critical_data(self):
        client = FakeClient()
        client.frames[("daily_basic", "20260811")] = pd.DataFrame()
        with self.assertRaises(pms.PitSnapshotIncomplete):
            pms.collect_pit_market_snapshot(client, "20260811")

    def test_collect_pit_market_snapshot_fails_closed_when_sw_membership_is_empty(self):
        client = FakeClient()
        for key in list(client.frames):
            if key[0] == "index_member_all":
                client.frames[key] = client.frames[key].iloc[0:0]
        with self.assertRaises(pms.PitSnapshotIncomplete):
            pms.collect_pit_market_snapshot(client, "20260811")

    def test_collect_pit_market_snapshot_rejects_proxy_provenance(self):
        client = FakeClient()
        client.frames[("daily_basic", "20260811")]["source_provider"] = "local_same_day_proxy"
        with self.assertRaises(pms.PitSnapshotIncomplete):
            pms.collect_pit_market_snapshot(client, "20260811")

    def test_write_snapshot_outputs_compatible_files(self):
        snapshot = pms.collect_pit_market_snapshot(FakeClient(), "20260811")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pms.write_pit_market_snapshot(snapshot, Path(tmpdir))
            self.assertTrue((Path(tmpdir) / "universe_20260811.parquet").exists())
            self.assertTrue((Path(tmpdir) / "daily_basic_20260811.parquet").exists())
            self.assertTrue((Path(tmpdir) / "pit_market_snapshot_20260811.json").exists())
            self.assertEqual(out["trade_date"], "20260811")

    def test_immutable_snapshot_replay_accepts_identical_and_rejects_changes(self):
        snapshot = pms.collect_pit_market_snapshot(FakeClient(), "20260811")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            first = pms.write_pit_market_snapshot(
                snapshot, output_dir, immutable=True
            )
            second = pms.write_pit_market_snapshot(
                snapshot, output_dir, immutable=True
            )
            self.assertEqual(first, second)

            changed = dict(snapshot)
            changed["universe"] = snapshot["universe"].copy()
            changed["universe"].loc[0, "universe_flag"] = 0
            with self.assertRaises(pms.PitSnapshotIncomplete):
                pms.write_pit_market_snapshot(changed, output_dir, immutable=True)


if __name__ == "__main__":
    unittest.main()
