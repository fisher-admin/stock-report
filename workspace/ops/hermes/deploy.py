#!/usr/bin/env python3
"""Hermes stock wrappers: the repo is the source of truth (phase 4A, 2026-10-30).

Hermes runs ``~/.hermes/scripts/stock/*.sh``.  Those files used to be edited in
place by several agent sessions and were not under version control, so the
repo copies drifted (scripts/stock/post_publish_research.sh had lost the
calendar gate and alert shaping).  Now ``ops/hermes/stock/`` is canonical:

  deploy.py check    list drift (changed / missing in ~/.hermes); exit 1 on drift
  deploy.py deploy   copy repo -> ~/.hermes, backing up each replaced file as
                     <name>.bak-<YYYYmmdd-HHMMSS>; only changed files are touched

Files in ~/.hermes that are not in the repo (backups, one-off scripts) are
left alone and are not reported.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parent / "stock"
TARGET = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser() / "scripts" / "stock"


def drift(source: Path = SOURCE, target: Path = TARGET) -> list[tuple[str, str]]:
    """(name, 'changed' | 'missing') for every repo wrapper that differs from Hermes."""
    out = []
    for src in sorted(source.glob("*.sh")):
        dst = target / src.name
        if not dst.is_file():
            out.append((src.name, "missing"))
        elif not filecmp.cmp(src, dst, shallow=False):
            out.append((src.name, "changed"))
    return out


def deploy(source: Path = SOURCE, target: Path = TARGET, stamp: str | None = None) -> list[str]:
    stamp = stamp or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    done = []
    for name, _ in drift(source, target):
        dst = target / name
        if dst.is_file():
            shutil.copy2(dst, dst.with_name(f"{name}.bak-{stamp}"))
        tmp = dst.with_name(f".{name}.tmp")
        shutil.copy2(source / name, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dst)
        done.append(name)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "deploy"])
    args = parser.parse_args(argv)
    if args.command == "deploy":
        for name in deploy():
            print(f"deployed {name}")
        return 0
    items = drift()
    for name, kind in items:
        print(f"{kind} {name}")
    return 1 if items else 0


if __name__ == "__main__":
    sys.exit(main())
