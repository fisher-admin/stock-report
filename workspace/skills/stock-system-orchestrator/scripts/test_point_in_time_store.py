import tempfile
import unittest
from pathlib import Path

from point_in_time_store import PointInTimeStore, SnapshotImmutableError


class PointInTimeStoreTests(unittest.TestCase):
    def test_put_and_get_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PointInTimeStore(Path(tmp_dir))
            p = store.put("20260820", "daily_bars", b"bar_data_123")
            self.assertTrue(p.exists())
            self.assertEqual(store.get("20260820", "daily_bars"), p)
            self.assertTrue(store.has("20260820", "daily_bars"))

    def test_vendor_drift_preserves_initial_snapshot_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PointInTimeStore(Path(tmp_dir))
            p1 = store.put("20260820", "daily_bars", b"version_1")
            self.assertEqual(p1.read_bytes(), b"version_1")

            # Subsequent put with different content returns the frozen original version
            p2 = store.put("20260820", "daily_bars", b"version_2_vendor_added_row")
            self.assertEqual(p2.read_bytes(), b"version_1")

            # Audit log was written
            audit_log = Path(tmp_dir) / "20260820" / "drift_audit.jsonl"
            self.assertTrue(audit_log.exists())
            self.assertIn("preserved_initial_snapshot", audit_log.read_text())
            self.assertIn("incoming_sha256", audit_log.read_text())

    def test_strict_mode_raises_on_mutation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PointInTimeStore(Path(tmp_dir))
            store.put("20260820", "daily_bars", b"version_1")
            with self.assertRaises(SnapshotImmutableError):
                store.put("20260820", "daily_bars", b"version_2", strict=True)


if __name__ == "__main__":
    unittest.main()
