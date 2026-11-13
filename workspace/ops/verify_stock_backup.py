#!/usr/bin/env python3
"""Verify integrity of stock database backups."""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def verify_database_file(db_path: Path) -> dict:
    db_path = Path(db_path)
    if not db_path.is_file():
        return {"ok": False, "file": str(db_path), "error": "file_not_found"}

    temp_db = None
    target_to_check = db_path

    try:
        if db_path.suffix == ".gz":
            fd, temp_db = tempfile.mkstemp(prefix="verify_db_", suffix=".db")
            os.close(fd)
            with gzip.open(db_path, "rb") as f_in, open(temp_db, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            target_to_check = Path(temp_db)

        conn = sqlite3.connect(f"file:{target_to_check}?mode=ro", uri=True, timeout=10)
        try:
            cursor = conn.cursor()
            rows = cursor.execute("PRAGMA integrity_check;").fetchall()
            integrity_ok = bool(rows and rows[0][0] == "ok")
            return {
                "ok": integrity_ok,
                "file": str(db_path),
                "integrity_result": rows[0][0] if rows else "no_result",
            }
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "file": str(db_path), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if temp_db and os.path.exists(temp_db):
            try:
                os.unlink(temp_db)
            except OSError:
                pass


def verify_backup_directory(backup_dir: Path) -> dict:
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return {"ok": False, "error": "directory_not_found", "verified": []}

    results = []
    has_error = False

    for gz_file in sorted(backup_dir.glob("*.db.gz")):
        res = verify_database_file(gz_file)
        results.append(res)
        if not res["ok"]:
            has_error = True

    return {
        "ok": not has_error and len(results) > 0,
        "backup_count": len(results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify stock database backups")
    parser.add_argument("--backup-dir", default=str(Path.home() / ".openclaw/workspace/stock_data/03-working/backups"))
    args = parser.parse_args()

    summary = verify_backup_directory(Path(args.backup_dir))
    if summary["ok"]:
        print(f"✅ All {summary['backup_count']} database backups verified ok.")
        return 0
    else:
        print(f"❌ Backup verification failed (count={summary.get('backup_count', 0)}):")
        for r in summary.get("results", []):
            if not r.get("ok"):
                print(f"  - {r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
