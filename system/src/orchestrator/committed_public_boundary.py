#!/usr/bin/env python3
"""Audit what the v2 publisher is about to commit to the public repository.

20261009: the public boundary (config/public-result-allowlist.txt) was enforced
only by the Pages build and CI after the push, never before the commit, so a
tracked-but-ignored per-stock history file (review_state_unified.json) kept
reaching the public repo.  This check runs after ``git add`` and before
``git commit``: every data file in the index must be allowlisted, small, in a
public format and free of private absolute paths.  Unlike
enforce_public_boundary.py --prepare it never deletes local working files.

Exit 0 pass, 1 violations (the publisher unstages and aborts), 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ALLOWLIST = Path("config/public-result-allowlist.txt")
FORBIDDEN_SUFFIXES = {".csv", ".db", ".sqlite", ".sqlite3", ".parquet"}
MAX_PUBLIC_DATA_BYTES = 1_000_000
PRIVATE_ABSOLUTE_PATH = re.compile(r"/(?:" + "Users" + r"|home)/[^/\s\"']+/")
LOCAL_ONLY_JSON_FIELDS = {"stock_rows", "db_path"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def read_allowlist(repo: Path) -> set[str]:
    path = repo / ALLOWLIST
    if not path.is_file():
        raise FileNotFoundError(f"public result allowlist missing: {path}")
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def _local_only_fields(value: Any, location: str, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in LOCAL_ONLY_JSON_FIELDS:
                out.append(f"{location}.{key}")
            _local_only_fields(item, f"{location}.{key}", out)
    elif isinstance(value, list):
        for index, item in enumerate(value[:50]):
            _local_only_fields(item, f"{location}[{index}]", out)


def audit_index(repo: Path) -> dict[str, Any]:
    repo = Path(repo).resolve()
    allowlist = read_allowlist(repo)
    staged = [line for line in _git(repo, "ls-files", "--cached", "--", "data").splitlines() if line]
    violations: list[str] = []
    for rel in staged:
        if rel not in allowlist:
            violations.append(f"{rel}: not in {ALLOWLIST}")
        if Path(rel).suffix.lower() in FORBIDDEN_SUFFIXES:
            violations.append(f"{rel}: raw tabular/database format is local-only")
        blob = _git(repo, "show", f":{rel}")  # exactly what will be committed
        if len(blob.encode("utf-8")) > MAX_PUBLIC_DATA_BYTES:
            violations.append(f"{rel}: exceeds {MAX_PUBLIC_DATA_BYTES} bytes")
        match = PRIVATE_ABSOLUTE_PATH.search(blob)
        if match:
            violations.append(f"{rel}: private absolute path ({match.group(0)})")
        if rel.endswith(".json"):
            try:
                payload = json.loads(blob)
            except json.JSONDecodeError as exc:
                violations.append(f"{rel}: invalid JSON ({exc})")
            else:
                fields: list[str] = []
                _local_only_fields(payload, "$", fields)
                violations.extend(f"{rel}: local-only field {field}" for field in fields[:5])
    return {"ok": not violations, "checked": len(staged), "violations": sorted(set(violations))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = audit_index(args.repo)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
