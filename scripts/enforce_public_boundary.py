#!/usr/bin/env python3
"""Keep the public repository limited to source code and publishable results.

The stock database, historical recommendation rows, raw AI output, and other
working data belong to the local runtime.  This module provides both a cleanup
step and a fail-closed audit for the GitHub Pages tree.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Optional, Set


DEFAULT_ALLOWLIST = Path("config/public-result-allowlist.txt")

# Exact local working artifacts that older publishers copied into Pages.
PRIVATE_PATHS = (
    "combined.json",
    "data.json",
    "data/history",
    "data/latest/combined_recommendation.json",
    "data/latest/combined_recommendation.md",
    "data/latest/recommendation_history.csv",
    "data/latest/review_state_unified.json",
    "data/latest/review_state_o2c.json",
    "data/latest/review_state_prebreakout.json",
    "data/latest/review_state_t1.json",
    "data/recommendation_analytics/industry_heatmap.json",
    "data/recommendation_analytics/market_industry_heatmap.json",
    "data/recommendation_analytics/o2c_ai_analysis.json",
    "data/recommendation_analytics/o2c_factor_recommendations.json",
    "data/recommendation_analytics/o2c_greenfield_ai_analysis.json",
    "data/recommendation_analytics/prebreakout_recommendations.csv",
    "data/recommendation_analytics/prebreakout_recommendations.json",
    "data/recommendation_analytics/research_lab_latest.json",
    "data/recommendation_analytics/t1_ai_analysis.json",
    "data/recommendation_analytics/t1_alpha191_ai_analysis.json",
    "data/recommendation_analytics/t1_factor_recommendations.json",
)

FORBIDDEN_DATA_SUFFIXES = {".csv", ".db", ".sqlite", ".sqlite3", ".parquet"}
MAX_PUBLIC_DATA_BYTES = 1_000_000
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
PRIVATE_ABSOLUTE_PATH = re.compile(
    r"(?:/" + r"Users/[^/\s'\"]+|/" + r"home/[^/\s'\"]+)(?:/[^\s'\"]*)?"
)
LITERAL_CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|private[_-]?key|secret|token)"
    r"\s*[:=]\s*['\"][^'\"]{8,}['\"]"
)
TOKEN_SIGNATURE = re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,})")
LOCAL_ONLY_JSON_FIELDS = {"stock_rows", "latest_sample", "db_path"}


def _repo_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_allowlist(root: Path) -> Set[str]:
    path = root / DEFAULT_ALLOWLIST
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _walk_json(value: Any, location: str, violations: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if key in LOCAL_ONLY_JSON_FIELDS and child not in (None, "", [], {}):
                violations.append(f"{child_location}: local-only detail/runtime metadata")
            if isinstance(child, str) and child.startswith("~/"):
                violations.append(f"{child_location}: local runtime path is not publishable")
            _walk_json(child, child_location, violations)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_json(child, f"{location}[{index}]", violations)


def _iter_public_text(root: Path) -> Iterable[Path]:
    for top_level in ("data", "docs", "system", "scripts", "config"):
        base = root / top_level
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def prepare_public_tree(root: Path) -> dict[str, Any]:
    """Remove known local-only artifacts from a repository checkout."""

    root = root.resolve()
    removed: list[str] = []
    for relative in PRIVATE_PATHS:
        target = root / relative
        if target.is_dir():
            for path in sorted(target.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    removed.append(_repo_path(root, path))
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            removed.append(relative)
            target.unlink()
    return {"ok": True, "removed_count": len(removed), "removed": sorted(removed)}


def audit_public_tree(
    root: Path,
    allowed_data_paths: Optional[Set[str]] = None,
) -> dict[str, Any]:
    """Fail when the public tree contains anything outside the result contract."""

    root = root.resolve()
    allowlist = _read_allowlist(root) if allowed_data_paths is None else set(allowed_data_paths)
    violations: list[str] = []
    data_files: list[Path] = []

    data_root = root / "data"
    if data_root.exists():
        data_files = sorted(path for path in data_root.rglob("*") if path.is_file())
        for path in data_files:
            relative = _repo_path(root, path)
            if relative not in allowlist:
                violations.append(f"{relative}: data path is not in the public result allowlist")
            if path.suffix.lower() in FORBIDDEN_DATA_SUFFIXES:
                violations.append(f"{relative}: raw tabular/database format is local-only")
            if path.stat().st_size > MAX_PUBLIC_DATA_BYTES:
                violations.append(f"{relative}: public result exceeds {MAX_PUBLIC_DATA_BYTES} bytes")
            if path.suffix.lower() == ".json":
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    violations.append(f"{relative}: invalid JSON ({exc})")
                else:
                    _walk_json(payload, relative, violations)

    for relative in ("combined.json", "data.json"):
        if (root / relative).exists():
            violations.append(f"{relative}: legacy root data file is local-only")

    for path in _iter_public_text(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = PRIVATE_ABSOLUTE_PATH.search(text)
        if match:
            violations.append(
                f"{_repo_path(root, path)}: private absolute path is not publishable ({match.group(0)})"
            )
        credential = LITERAL_CREDENTIAL.search(text) or TOKEN_SIGNATURE.search(text)
        if credential:
            violations.append(
                f"{_repo_path(root, path)}: literal credential is not publishable"
            )

    unique = sorted(set(violations))
    return {
        "ok": not unique,
        "data_file_count": len(data_files),
        "allowlisted_data_file_count": len(allowlist),
        "violations": unique,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--prepare", action="store_true", help="remove known local-only artifacts first")
    args = parser.parse_args()

    report: dict[str, Any] = {}
    if args.prepare:
        report["prepare"] = prepare_public_tree(args.root)
    report["audit"] = audit_public_tree(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["audit"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
