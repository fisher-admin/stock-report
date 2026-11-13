"""2026-10-03 regression tests for frozen-input revision handling.

Covers the real 2026-10-02 failure (``N力勤资源`` -> ``力勤资源``), risk-marker
changes hidden in the name field, meaningful numeric revisions, and row-set
changes.  Parquet I/O is replaced by in-memory frames so the tests also run
where no parquet engine is installed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import frozen_input_revision as fir  # noqa: E402

UNIVERSE = pd.DataFrame({
    "trade_date": ["20260930"] * 3,
    "ts_code": ["000001.SZ", "603327.SH", "600512.SH"],
    "name": ["平安银行", "N力勤资源", "腾达建设"],
    "pe_ttm": [5.1, 30.2, 12.0],
})


def variant(changes):
    frame = UNIVERSE.copy()
    for (row, column), value in changes.items():
        frame.loc[row, column] = value
    return frame


class ClassifyRevisionTests(unittest.TestCase):
    def test_new_listing_prefix_drop_is_presentational(self):
        result = fir.classify_revision(UNIVERSE, variant({(1, "name"): "力勤资源"}))
        self.assertTrue(result["presentational_only"], result)
        self.assertEqual(result["differences"]["name"]["count"], 1)
        self.assertEqual(result["reasons"], ["listing_marker_name_change_only"])

    def test_risk_marker_changes_in_name_are_meaningful(self):
        for old, new in (("平安银行", "ST平安"), ("平安银行", "*ST平安"), ("ST美丽", "*ST美丽"),
                         ("*ST美丽", "ST美丽"), ("新都", "新都退"), ("N力勤资源", "ST力勤资源")):
            with self.subTest(old=old, new=new):
                base = variant({(0, "name"): old})
                result = fir.classify_revision(base, variant({(0, "name"): new}))
                self.assertFalse(result["presentational_only"], result)

    def test_other_name_changes_are_meaningful(self):
        result = fir.classify_revision(UNIVERSE, variant({(2, "name"): "XD腾达建"}))
        self.assertFalse(result["presentational_only"])

    def test_numeric_revision_is_meaningful_without_tolerance(self):
        result = fir.classify_revision(UNIVERSE, variant({(0, "pe_ttm"): 5.1000001}))
        self.assertFalse(result["presentational_only"])
        self.assertIn("pe_ttm", result["differences"])
        self.assertEqual(result["reasons"], ["value_revision"])

    def test_name_prefix_plus_numeric_revision_is_meaningful(self):
        changed = variant({(1, "name"): "力勤资源", (1, "pe_ttm"): 31.0})
        self.assertFalse(fir.classify_revision(UNIVERSE, changed)["presentational_only"])

    def test_row_set_and_schema_changes_are_meaningful(self):
        self.assertIn("row_set_changed", fir.classify_revision(UNIVERSE, UNIVERSE.iloc[:2])["reasons"])
        self.assertIn("schema_changed", fir.classify_revision(UNIVERSE, UNIVERSE.drop(columns=["pe_ttm"]))["reasons"])

    def test_dtype_only_difference_is_equal(self):
        other = UNIVERSE.copy()
        other["pe_ttm"] = other["pe_ttm"].astype("Float64")
        self.assertTrue(fir.classify_revision(UNIVERSE, other)["presentational_only"])


class ShortTrackFrozenCompareTests(unittest.TestCase):
    """Drive the real load_frozen_run_state comparison with in-memory frames."""

    @classmethod
    def setUpClass(cls):
        import short_track_shadow_runner as runner  # noqa: PLC0415

        cls.runner = runner

    def run_compare(self, common_universe: pd.DataFrame):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.runner.build_paths(Path(tmp))
            for directory in (paths.shadow_universe_dir, paths.shadow_daily_basic_dir, paths.common_pit_market_dir,
                              paths.short_track_daily_dir):
                directory.mkdir(parents=True, exist_ok=True)
            daily_basic = pd.DataFrame({"trade_date": ["20260930"], "ts_code": ["000001.SZ"], "volume_ratio": [0.7]})
            frames = {
                paths.shadow_universe_dir / "universe_20260930.parquet": UNIVERSE,
                paths.common_pit_market_dir / "universe_20260930.parquet": common_universe,
                paths.shadow_daily_basic_dir / "daily_basic_20260930.parquet": daily_basic,
                paths.common_pit_market_dir / "daily_basic_20260930.parquet": daily_basic,
            }
            for path, frame in frames.items():
                path.write_bytes(frame.to_json().encode("utf-8"))  # distinct bytes for checksums

            def fake_read(path, columns=None, **_kwargs):
                frame = frames[Path(path)]
                return frame[list(columns)].copy() if columns else frame.copy()

            with mock.patch.object(self.runner.pd, "read_parquet", side_effect=fake_read):
                try:
                    state = self.runner.load_frozen_run_state(paths, "20260930")
                    error = None
                except self.runner.RunnerInputError as exc:
                    state, error = None, exc
            revisions = sorted((paths.short_track_daily_dir / "input_revisions").glob("*.json"))
            records = [json.loads(p.read_text(encoding="utf-8")) for p in revisions]
            frozen_bytes = (paths.shadow_universe_dir / "universe_20260930.parquet").read_bytes()
            return state, error, records, frozen_bytes

    def test_n_prefix_case_passes_compare_and_is_recorded(self):
        state, error, records, frozen_bytes = self.run_compare(variant({(1, "name"): "力勤资源"}))
        self.assertIsNone(error)
        self.assertIsNone(state)  # comparison passed; no candidate snapshots in this fixture
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["classification"]["presentational_only"])
        self.assertTrue(records[0]["frozen_input"]["used"])
        self.assertIsNotNone(records[0]["frozen_input"]["sha256"])
        self.assertIsNotNone(records[0]["supplier_copy"]["sha256"])
        self.assertEqual(frozen_bytes, UNIVERSE.to_json().encode("utf-8"))  # original untouched

    def test_numeric_revision_still_fails_closed_with_record(self):
        state, error, records, _ = self.run_compare(variant({(0, "pe_ttm"): 5.2}))
        self.assertIsNotNone(error)
        self.assertIn("value_revision", str(error))
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["classification"]["presentational_only"])

    def test_st_change_still_fails_closed(self):
        _, error, _, _ = self.run_compare(variant({(0, "name"): "ST平安银行"}))
        self.assertIsNotNone(error)

    def test_identical_copy_writes_no_record(self):
        _, error, records, _ = self.run_compare(UNIVERSE.copy())
        self.assertIsNone(error)
        self.assertEqual(records, [])


class EventRunnerRevisionTests(unittest.TestCase):
    def test_immutable_refetch_with_drift_is_stored_as_new_version(self):
        import event_quality_drift_runner as runner  # noqa: PLC0415

        first = pd.DataFrame({"trade_date": ["20260817"], "ts_code": ["000001.SZ"], "dv_ratio": [2.4409]})
        drifted = pd.DataFrame({"trade_date": ["20260817"], "ts_code": ["000001.SZ"], "dv_ratio": [2.4435]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_basic_20260817.parquet"
            path.write_bytes(b"frozen-original")
            written = {}

            def fake_to_parquet(self_frame, target, index=False):
                Path(target).write_bytes(self_frame.to_json().encode())
                written[str(target)] = self_frame.copy()

            with mock.patch.object(runner.pd, "read_parquet", return_value=first), \
                 mock.patch.object(pd.DataFrame, "to_parquet", fake_to_parquet):
                runner._atomic_parquet(path, drifted, immutable=True)
                runner._atomic_parquet(path, first.copy(), immutable=True)  # equal -> no new record
            self.assertEqual(path.read_bytes(), b"frozen-original")
            records = list((Path(tmp) / "revisions").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["classification"]["reasons"], ["value_revision"])
            self.assertTrue(Path(record["supplier_copy"]["stored_version"]).is_file())


if __name__ == "__main__":
    unittest.main()
