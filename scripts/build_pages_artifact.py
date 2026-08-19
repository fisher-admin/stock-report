#!/usr/bin/env python3
"""Build the minimal, sanitized artifact deployed to GitHub Pages.

The repository contains source code, tests, and local publishing machinery in
addition to the public site.  Pages should receive only the static frontend and
the result contracts named in ``config/public-result-allowlist.txt``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from enforce_public_boundary import audit_public_tree  # noqa: E402
from sanitize_published_data import sanitize_value  # noqa: E402


PUBLIC_ROOT_FILES = (
    "404.html",
    "decision-candidates.html",
    "index.html",
    "industry-compare.html",
    "industry-heatmap.html",
    "market-industry-heatmap.html",
    "market-overview.html",
    "prebreakout-shadow.html",
    "recommendation-review.html",
    "research-lab.html",
    "robots.txt",
    "s3-watch.html",
    "sentiment.html",
    "strategy-vs-market.html",
)
REQUIRED_SITE_FILES = (
    "index.html",
    "assets/scripts/v2/app.js",
    "data/latest/run_manifest.json",
    "data/latest/system_verdict.json",
)


class ArtifactBuildError(RuntimeError):
    """Raised when a safe Pages artifact cannot be produced."""


def _read_allowlist(source: Path) -> set[str]:
    path = source / "config/public-result-allowlist.txt"
    if not path.is_file():
        raise ArtifactBuildError(f"missing public result allowlist: {path}")
    entries = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    invalid = sorted(entry for entry in entries if not entry.startswith("data/"))
    if invalid:
        raise ArtifactBuildError(f"allowlist contains non-data paths: {invalid}")
    return entries


def _prepare_output(source: Path, output: Path) -> None:
    if output == source or output in source.parents:
        raise ArtifactBuildError("output must not be the source directory or its parent")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def _copy_json(source_path: Path, output_path: Path) -> None:
    try:
        payload: Any = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactBuildError(f"invalid public JSON {source_path}: {exc}") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_value(payload)
    output_path.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_site(source: Path, output: Path) -> dict[str, Any]:
    """Create and audit a deployable Pages tree without modifying ``source``."""

    source = source.resolve()
    output = output.resolve()
    _prepare_output(source, output)
    allowlist = _read_allowlist(source)

    copied_root_files: list[str] = []
    for relative in PUBLIC_ROOT_FILES:
        source_path = source / relative
        if not source_path.is_file():
            raise ArtifactBuildError(f"missing public frontend file: {relative}")
        shutil.copy2(source_path, output / relative)
        copied_root_files.append(relative)

    assets = source / "assets"
    if not assets.is_dir():
        raise ArtifactBuildError("missing public assets directory")
    shutil.copytree(assets, output / "assets")

    copied_data_files: list[str] = []
    missing_optional_data: list[str] = []
    for relative in sorted(allowlist):
        source_path = source / relative
        if not source_path.is_file():
            missing_optional_data.append(relative)
            continue
        output_path = output / relative
        if source_path.suffix.lower() == ".json":
            _copy_json(source_path, output_path)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, output_path)
        copied_data_files.append(relative)

    missing_required = [
        relative for relative in REQUIRED_SITE_FILES if not (output / relative).is_file()
    ]
    if missing_required:
        raise ArtifactBuildError(f"artifact is missing required files: {missing_required}")

    audit = audit_public_tree(output, allowed_data_paths=allowlist)
    if not audit["ok"]:
        raise ArtifactBuildError(
            "public artifact audit failed:\n- " + "\n- ".join(audit["violations"])
        )

    return {
        "ok": True,
        "output": str(output),
        "root_file_count": len(copied_root_files),
        "data_file_count": len(copied_data_files),
        "missing_optional_data": missing_optional_data,
        "audit": audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "_site")
    args = parser.parse_args()
    try:
        report = build_site(args.source, args.output)
    except ArtifactBuildError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
