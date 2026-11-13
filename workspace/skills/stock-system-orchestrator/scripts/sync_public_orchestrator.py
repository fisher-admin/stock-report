#!/usr/bin/env python3
"""Sync production orchestrator scripts to public stock-report mirror with parity checking.

2026-10-09: by default the mirror is generated from a committed revision of the
workspace repo (``--rev HEAD``), never from the working tree, so uncommitted or
unverified edits (including other sessions' work in progress) cannot leak into
the public repository.  ``--worktree`` keeps the old behaviour explicitly.
``--public-root`` / ``--manifest`` let the mirror be written into a separate
stock-report worktree branch for review before it is pushed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_ROOT = WORKSPACE / "skills" / "stock-system-orchestrator" / "scripts"
DEFAULT_PUBLIC_ROOT = WORKSPACE / "stock-report" / "system" / "src" / "orchestrator"
DEFAULT_MANIFEST_PATH = WORKSPACE / "stock-report" / "system" / "orchestrator-manifest.json"

DEFAULT_REV = "HEAD"

# Every file published under system/src/orchestrator.  2026-10-09: covers the
# whole existing mirror (17 files had been copied outside this tool and were
# never parity-checked) plus the post-close index resolver and maintenance mode.
ALLOWLIST_FILES = [
    "committed_public_boundary.py",
    "domestic_index_close.py",
    "dual_track_publication.py",
    "event_quality_drift_evaluator.py",
    "event_quality_drift_runner.py",
    "event_quality_drift_v1.py",
    "generate_industry_heatmap.py",
    "generate_latest_states.py",
    "generate_system_verdict.py",
    "immutable_strategy_registry.py",
    "maintenance_mode.py",
    "orchestrator_common.py",
    "pit_market_backfill.py",
    "pit_market_snapshot.py",
    "prebreakout_recipe_engine.py",
    "publish_guard.py",
    "publish_stock_report_v2.sh",
    "qfq_price_fallback.py",
    "recommendation_warehouse.py",
    "sanitize_public_report.py",
    "shadow_portfolio_evaluator.py",
    "short_track_shadow.py",
    "short_track_shadow_runner.py",
    "stage3_strategy_suite.py",
    "stock_report_pre_push_hook.sh",
    "strategy_publication_layer.py",
    "trading_calendar_store.py",
    "validate_publication_contract.py",
]


def sanitize_for_public(content: str) -> str:
    # These paths occur inside string literals. Keep them as path values,
    # rather than inserting executable expressions and nested quotation marks.
    sanitized = content.replace("/Users/fisher/.openclaw/workspace", "./workspace")
    return sanitized.replace("/Users/fisher/.openclaw", "./stock-system")


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_rev(source_root: Path, rev: str) -> str:
    proc = subprocess.run(["git", "-C", str(source_root), "rev-parse", "--verify", f"{rev}^{{commit}}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ValueError(f"cannot resolve revision {rev!r} in {source_root}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def read_source(source_root: Path, filename: str, rev: str | None) -> str | None:
    """File content from the working tree (rev=None) or from a committed revision."""
    if rev is None:
        src_file = source_root / filename
        return src_file.read_text(encoding="utf-8") if src_file.exists() else None
    proc = subprocess.run(["git", "-C", str(source_root), "show", f"{rev}:./{filename}"],
                          capture_output=True)
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def sync_public(
    source_root: Path,
    public_root: Path,
    manifest_path: Path | None = None,
    check_only: bool = False,
    rev: str | None = None,
    only: list[str] | None = None,
) -> dict[str, Any]:
    source_root = Path(source_root)
    public_root = Path(public_root)
    diffs: list[dict[str, str]] = []
    synced: list[str] = []
    resolved = resolve_rev(source_root, rev) if rev is not None else None
    unknown = sorted(set(only or []) - set(ALLOWLIST_FILES))
    if unknown:
        raise ValueError(f"not in ALLOWLIST_FILES: {unknown}")
    selected = [name for name in ALLOWLIST_FILES if not only or name in only]

    for filename in selected:
        dst_file = public_root / filename
        src_content = read_source(source_root, filename, resolved)
        if src_content is None:
            diffs.append({"file": filename, "reason": "source_missing"})
            continue

        expected_content = sanitize_for_public(src_content)
        expected_bytes = expected_content.encode("utf-8")

        if dst_file.exists():
            dst_bytes = dst_file.read_bytes()
            if dst_bytes == expected_bytes:
                synced.append(filename)
                continue
            else:
                diffs.append({
                    "file": filename,
                    "reason": "content_drift",
                    "src_sha": compute_sha256(expected_bytes),
                    "dst_sha": compute_sha256(dst_bytes),
                })
        else:
            diffs.append({"file": filename, "reason": "destination_missing"})

        if not check_only:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            dst_file.write_bytes(expected_bytes)
            synced.append(filename)

    manifest_payload = {
        "source_root": "workspace://skills/stock-system-orchestrator/scripts",
        "public_root": "repository://system/src/orchestrator",
        "source_rev": resolved or "working_tree",
        "files": ALLOWLIST_FILES,
        "synced": synced,
        "diff_count": len(diffs),
    }

    if manifest_path and not check_only:
        manifest_path = Path(manifest_path)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": len(diffs) == 0 or (not check_only and len(synced) == len(selected)),
        "diffs": diffs,
        "synced": synced,
        "source_rev": resolved or "working_tree",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync production orchestrator to public mirror")
    parser.add_argument("--check", action="store_true", help="Check parity without writing")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--rev", default=None, help=f"committed revision to mirror (default {DEFAULT_REV})")
    source.add_argument("--worktree", action="store_true", help="mirror the working tree (uncommitted edits included)")
    parser.add_argument("--public-root", type=Path, default=None, help="mirror destination (e.g. a stock-report worktree)")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest path for --public-root")
    parser.add_argument("--only", nargs="+", default=None, metavar="FILE",
                        help="sync/check only these allowlisted files (e.g. while hand-edited mirror files are reconciled)")
    args = parser.parse_args()

    rev = None if args.worktree else (args.rev or DEFAULT_REV)
    try:
        res = sync_public(
            source_root=DEFAULT_SOURCE_ROOT,
            public_root=args.public_root or DEFAULT_PUBLIC_ROOT,
            manifest_path=args.manifest or (None if args.public_root else DEFAULT_MANIFEST_PATH),
            check_only=args.check,
            rev=rev,
            only=args.only,
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        return 2
    print(f"source: {res['source_rev']}")
    if args.check:
        if res["ok"]:
            print("✅ Public mirror parity check passed: 100% matched.")
            return 0
        else:
            print(f"❌ Public mirror parity check failed ({len(res["diffs"])} diffs):")
            for d in res["diffs"]:
                print(f"  - {d}")
            return 1
    else:
        print(f"✅ Synchronized {len(res["synced"])} files to public mirror.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
