#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("backup_databases.sh")


class BackupDatabasesTests(unittest.TestCase):
    def test_first_of_month_creates_daily_and_compact_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            workspace = home / ".openclaw/workspace"
            sources = [
                workspace / "daily_stock_analysis/data/stock_analysis.db",
                workspace / "data/stock_analysis.db",
                workspace / "stock_data/03-working/recommendation_warehouse/recommendations.db",
            ]
            for source in sources:
                source.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(source)
                conn.execute("create table sample(value integer)")
                conn.execute("insert into sample values (1)")
                conn.commit()
                conn.close()

            fake_bin = home / "bin"
            fake_bin.mkdir()
            fake_date = fake_bin / "date"
            fake_date.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  +%d) echo 01 ;;\n"
                "  +%Y%m%d) echo 20260801 ;;\n"
                "  +%Y%m) echo 202608 ;;\n"
                "  *) /bin/date \"$@\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_date.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake_bin}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

            proc = subprocess.run(
                ["bash", str(SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            backup_dir = workspace / "stock_data/03-working/backups"
            self.assertTrue((backup_dir / "recommendations.20260801.db.gz").exists())
            self.assertTrue((backup_dir / "recommendations.compact.202608.db.gz").exists())


if __name__ == "__main__":
    unittest.main()
