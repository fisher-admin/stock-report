#!/usr/bin/env python3
"""cohort_monitor (forward_shadow_cohort_v1): frozen-spec guard, book construction, sleeve tiling, forward-only start.

Pure tests use synthetic frames; the smoke test reads the real 20261014 scores into a temporary ledger and never
touches the official cohort_ledger.jsonl.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cohort_monitor as cm  # noqa: E402


class SpecTests(unittest.TestCase):
    def test_frozen_spec_hash_verifies_and_constants_match_text(self):
        spec = cm.load_spec()
        self.assertEqual(spec["spec_id"], "forward_shadow_cohort_v1")
        books = spec["cohort"]["books"]
        self.assertIn("前 20 名，单行业最多 4 只", books["cohort_top20"])
        self.assertIn("前 100 名，单行业最多 15 只", books["cohort_top100"])
        self.assertIn("后 30%", books["cohort_avoid70"])
        self.assertEqual(spec["cost"]["round_trip"], cm.ROUND_TRIP_COST)
        self.assertIn("滞后 4", spec["evaluation"]["primary"])
        self.assertIn(">= 120", spec["evaluation"]["when"])
        self.assertIn("60 组", spec["evaluation"]["when"])
        self.assertIn("t >= 2.0", spec["evaluation"]["success"])
        self.assertEqual((cm.TOP20, cm.TOP100, cm.DROP, cm.NW_LAG), ((20, 4), (100, 15), 0.30, 4))

    def test_edited_spec_is_refused(self):
        raw = json.loads(cm.SPEC_PATH.read_text())
        raw["cost"]["round_trip"] = 0.001
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "spec.json"
            p.write_text(json.dumps(raw, ensure_ascii=False))
            with self.assertRaises(SystemExit):
                cm.load_spec(p)


class PureTests(unittest.TestCase):
    def test_pick_top_caps_industry_and_backfills(self):
        s = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(10)],
                          "industry": ["银行"] * 6 + ["医药"] * 4,
                          "comp_rank_ew6": [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.5, 0.4, 0.3, None]})
        top = cm.pick_top(s, 6, 4)
        self.assertEqual([m["code"] for m in top][:4], [f"{i:06d}.SZ" for i in range(4)])
        self.assertEqual(sum(m["industry"] == "银行" for m in top), 4)
        self.assertEqual([m["code"] for m in top][4:], ["000006.SZ", "000007.SZ"])
        self.assertEqual(top[4]["rank"], 7)

    def test_sleeves_tile_and_gap_leaves_cash(self):
        sessions = [f"2026110{i}" for i in range(10)]
        si = {d: i for i, d in enumerate(sessions)}
        cohorts = [{"signal_date": sessions[i], "exit": sessions[i] + "x",
                    "ret": {cm.PRIMARY: 0.10, "bench_ew": 0.0}} for i in (0, 1, 2, 3, 5)]  # session 4 is a gap
        curve = cm.sleeve_curve(cohorts, si)
        # sleeves 0..3 grew 10% once each; sleeve 4 idle; cohort 5 compounds sleeve 0 again
        self.assertAlmostEqual(curve[-1]["sleeve_nav"], (1.1 * 1.1 + 1.1 * 3 + 1.0) / 5, places=6)
        self.assertAlmostEqual(curve[-1]["bench_nav"], 1.0, places=6)

    def test_verdict_stages(self):
        self.assertEqual(cm.verdict({"cohorts": 10, "mean_excess": 0.01, "nw_t_excess": 5})["status"],
                         "insufficient_sample")
        self.assertEqual(cm.verdict({"cohorts": 60, "mean_excess": 0.01, "nw_t_excess": 5})["status"],
                         "interim_snapshot")
        self.assertEqual(cm.verdict({"cohorts": 120, "mean_excess": 0.01, "nw_t_excess": 2.1})["status"], "pass")
        self.assertEqual(cm.verdict({"cohorts": 120, "mean_excess": 0.01, "nw_t_excess": 1.9})["status"], "fail")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "cohort_ledger.jsonl"
        self.p = mock.patch.object(cm, "LEDGER", self.ledger)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_record_refuses_signals_recorded_before_freezing(self):
        self.assertEqual(cm.record("20261019"), "before_start")
        self.assertFalse(self.ledger.exists())

    def test_smoke_record_and_settle_on_real_scores(self):
        spec = {**cm.load_spec(), "frozen_at": "2026-10-01T00:00:00+08:00"}  # pretend frozen before 20261014
        before = datetime.fromisoformat("2026-10-14T23:40:00+08:00")
        self.assertEqual(cm.record("20261014", now=before, spec=spec), "recorded")
        self.assertEqual(cm.record("20261014", now=before, spec=spec), "exists")
        row = json.loads(self.ledger.read_text().splitlines()[0])
        self.assertEqual(len(row["cohort_top20"]), 20)
        self.assertEqual(len(row["cohort_top100"]), 100)
        self.assertEqual(len(row["universe"]), 2258)
        late = datetime.fromisoformat("2026-10-17T10:00:00+08:00")
        self.assertEqual(cm.record("20261016", now=late, spec=spec), "recorded")  # deadline 10-19 09:30
        res = cm.settle(spec=spec)
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["matured"]["h1"], 2)
        self.assertIsNotNone(res["stats"]["h1"]["cohort_top100"]["mean_net"])


if __name__ == "__main__":
    unittest.main()
