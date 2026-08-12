#!/usr/bin/env python3
"""Remove local machine paths and reject credential fields before public push."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from orchestrator_common import PUBLISHED_REPO, WORKSPACE


class PublicReportSafetyError(RuntimeError):
    pass


SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "passwd",
    "client_secret",
    "secret",
}
LOCAL_ONLY_FIELDS = {"stock_rows", "latest_sample", "db_path"}
PRIVATE_PATH_PATTERN = re.compile(
    r"(?:/" + r"Users/[^/\s]+|/" + r"home/[^/\s]+)/(?:[^\s\"']+)"
)


def default_private_roots(repo: Path) -> list[tuple[str, str]]:
    roots = [
        (str(repo.resolve()), "report://"),
        (str(WORKSPACE.resolve()), "workspace://"),
        (str(WORKSPACE.resolve().parent), "openclaw://"),
        (str(Path.home().resolve()), "home://"),
        (str(Path(tempfile.gettempdir()).resolve()), "temp://"),
        ("/private/tmp", "temp://"),
        ("/tmp", "temp://"),
    ]
    deduplicated: dict[str, str] = {}
    for root, replacement in roots:
        if root and root != "/":
            deduplicated[root.rstrip("/")] = replacement
    return sorted(deduplicated.items(), key=lambda item: len(item[0]), reverse=True)


def replace_private_paths(text: str, roots: Iterable[tuple[str, str]]) -> tuple[str, int]:
    updated = text
    replacements = 0
    for root, replacement in roots:
        normalized_root = str(root).rstrip("/")
        if not normalized_root:
            continue
        with_separator = f"{normalized_root}/"
        count = updated.count(with_separator)
        if count:
            updated = updated.replace(with_separator, replacement)
            replacements += count
        exact_count = updated.count(normalized_root)
        if exact_count:
            updated = updated.replace(normalized_root, replacement)
            replacements += exact_count
    return updated, replacements


def _walk(value: Any, *, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in SENSITIVE_KEYS and item not in (None, "", "[redacted]"):
                raise PublicReportSafetyError(
                    f"credential-like field must not be published: {location}.{key}"
                )
            _walk(item, location=f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, location=f"{location}[{index}]")
        return
    if isinstance(value, str) and PRIVATE_PATH_PATTERN.search(value):
        raise PublicReportSafetyError(
            f"unsanitized private home path remains at {location}"
        )


def strip_local_only_fields(value: Any) -> tuple[Any, int]:
    """Remove historical row detail and machine-only database metadata."""
    if isinstance(value, dict):
        clean: dict[Any, Any] = {}
        removed = 0
        for key, item in value.items():
            if str(key).strip().lower() in LOCAL_ONLY_FIELDS:
                removed += 1
                continue
            clean_item, child_removed = strip_local_only_fields(item)
            clean[key] = clean_item
            removed += child_removed
        return clean, removed
    if isinstance(value, list):
        clean_list = []
        removed = 0
        for item in value:
            clean_item, child_removed = strip_local_only_fields(item)
            clean_list.append(clean_item)
            removed += child_removed
        return clean_list, removed
    return value, 0


def _atomic_text(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def sanitize_public_tree(
    repo: Path,
    *,
    private_roots: Iterable[tuple[str, str]] | None = None,
) -> dict[str, int]:
    repo = Path(repo).resolve()
    data_dir = repo / "data"
    if not data_dir.is_dir():
        raise PublicReportSafetyError(f"public data directory is missing: {data_dir}")
    roots = list(
        default_private_roots(repo) if private_roots is None else private_roots
    )
    files = sorted(data_dir.rglob("*.json"))
    if not files:
        raise PublicReportSafetyError(f"public data directory has no JSON files: {data_dir}")
    changed = 0
    replacement_count = 0
    local_only_fields_removed = 0
    for path in files:
        original = path.read_text(encoding="utf-8")
        sanitized, replacements = replace_private_paths(original, roots)
        try:
            payload = json.loads(sanitized)
        except json.JSONDecodeError as exc:
            raise PublicReportSafetyError(f"invalid public JSON: {path}") from exc
        payload, removed = strip_local_only_fields(payload)
        if removed:
            sanitized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _walk(payload)
        if sanitized != original:
            _atomic_text(path, sanitized)
            changed += 1
            replacement_count += replacements
            local_only_fields_removed += removed
    return {
        "files_scanned": len(files),
        "files_changed": changed,
        "private_paths_replaced": replacement_count,
        "local_only_fields_removed": local_only_fields_removed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sanitize public report JSON before push")
    parser.add_argument("--repo", default=str(PUBLISHED_REPO))
    args = parser.parse_args(argv)
    result = sanitize_public_tree(Path(args.repo))
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
