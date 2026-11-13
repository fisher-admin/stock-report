import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from verify_stock_backup import verify_backup_directory, verify_database_file


class VerifyStockBackupTests(unittest.TestCase):
    def test_verify_valid_sqlite_backup_gz(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO foo VALUES (1, 'hello');")
            conn.commit()
            conn.close()

            gz_path = Path(tmp_dir) / "test.db.gz"
            with open(db_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                f_out.write(f_in.read())

            res = verify_database_file(gz_path)
            self.assertTrue(res["ok"])
            self.assertEqual(res["integrity_result"], "ok")

    def test_verify_corrupted_backup_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            gz_path = Path(tmp_dir) / "corrupt.db.gz"
            with gzip.open(gz_path, "wb") as f_out:
                f_out.write(b"not a valid sqlite database content header")

            res = verify_database_file(gz_path)
            self.assertFalse(res["ok"])



    def test_main_cli_verify_backup(self):
        import gzip
        import verify_stock_backup as vsb
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Empty dir returns 1 (no backups)
            with mock.patch("sys.argv", ["verify_stock_backup.py", "--backup-dir", tmp_dir]):
                ret = vsb.main()
                self.assertEqual(ret, 1)

            # Valid db in dir
            db_path = Path(tmp_dir) / "test.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE foo (id INT);")
            conn.commit()
            conn.close()
            with open(db_path, "rb") as f_in, gzip.open(Path(tmp_dir) / "test.db.gz", "wb") as f_out:
                f_out.write(f_in.read())

            with mock.patch("sys.argv", ["verify_stock_backup.py", "--backup-dir", tmp_dir]):
                ret = vsb.main()
                self.assertEqual(ret, 0)

if __name__ == "__main__":
    unittest.main()
