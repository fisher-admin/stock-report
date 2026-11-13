#!/usr/bin/env python3
"""Classify and record supplier revisions of frozen point-in-time inputs.

2026-10-03 reliability revision.

Frozen inputs stay immutable and remain the copy that is used.  When a later
supplier copy of the same day differs, the difference is classified and stored
as a *new version* with explicit provenance; nothing is overwritten and no
original failure is rewritten.

Only one kind of difference is treated as presentational: the ``name`` column
changing solely by a new-listing marker prefix (``N``/``C``, e.g.
``N力勤资源`` -> ``力勤资源``) while every risk marker (ST, *ST, PT, 退) is
unchanged.  Everything else -- numeric/valuation/factor revisions, row-set
changes, schema changes, any risk-marker change -- is ``meaningful`` and the
caller must fail closed.  There is no numeric tolerance here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

KEYS = ("trade_date", "ts_code")
LISTING_MARKERS = ("N", "C")
MAX_EXAMPLES = 20


def risk_flags(name: Any) -> frozenset[str]:
    text = str(name or "")
    flags: set[str] = set()
    if "*ST" in text:
        flags.add("*ST")
    elif re.search(r"(^|[^A-Z])ST", text):
        flags.add("ST")
    if text.startswith("PT"):
        flags.add("PT")
    if "退" in text:
        flags.add("退")
    return frozenset(flags)


def strip_listing_marker(name: Any) -> str:
    text = str(name or "")
    if len(text) > 1 and text[0] in LISTING_MARKERS and not text[1].isascii():
        return text[1:]
    return text


def is_presentational_name_change(old: Any, new: Any) -> bool:
    if old == new:
        return False
    return strip_listing_marker(old) == strip_listing_marker(new) and risk_flags(old) == risk_flags(new)


def _same(a: Any, b: Any) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def classify_revision(frozen: pd.DataFrame, candidate: pd.DataFrame, keys: tuple[str, ...] = KEYS) -> dict[str, Any]:
    """Return a structured diff; ``presentational_only`` is True only for N/C name prefixes."""

    result: dict[str, Any] = {"presentational_only": False, "differences": {}, "reasons": []}
    if list(frozen.columns) != list(candidate.columns):
        result["reasons"].append("schema_changed")
        result["frozen_columns"] = list(frozen.columns)
        result["candidate_columns"] = list(candidate.columns)
        return result
    keys = tuple(key for key in keys if key in frozen.columns)
    if "ts_code" not in keys:
        result["reasons"].append("missing_keys:ts_code")
        return result
    left = frozen.copy()
    right = candidate.copy()
    for key in keys:
        left[key] = left[key].astype(str)
        right[key] = right[key].astype(str)
    if left.duplicated(list(keys)).any() or right.duplicated(list(keys)).any():
        result["reasons"].append("duplicate_keys")
        return result
    left_keys = set(map(tuple, left[list(keys)].itertuples(index=False)))
    right_keys = set(map(tuple, right[list(keys)].itertuples(index=False)))
    if left_keys != right_keys:
        result["reasons"].append("row_set_changed")
        result["rows_only_in_frozen"] = sorted(left_keys - right_keys)[:MAX_EXAMPLES]
        result["rows_only_in_candidate"] = sorted(right_keys - left_keys)[:MAX_EXAMPLES]
        return result
    left = left.set_index(list(keys)).sort_index()
    right = right.set_index(list(keys)).sort_index()
    meaningful = False
    for column in left.columns:
        diffs = []
        for index, (a, b) in enumerate(zip(left[column].tolist(), right[column].tolist())):
            if not _same(a, b):
                diffs.append((left.index[index], a, b))
        if not diffs:
            continue
        presentational = column == "name" and all(is_presentational_name_change(a, b) for _, a, b in diffs)
        if not presentational:
            meaningful = True
        result["differences"][column] = {
            "count": len(diffs),
            "presentational": presentational,
            "examples": [
                {"key": list(key) if isinstance(key, tuple) else [key], "frozen": _jsonable(a), "candidate": _jsonable(b)}
                for key, a, b in diffs[:MAX_EXAMPLES]
            ],
        }
    if not result["differences"]:
        result["reasons"].append("dtype_only_or_equal")
        # Values are identical; only representation (dtype) differs.
        result["presentational_only"] = True
        return result
    if meaningful:
        result["reasons"].append("value_revision")
    else:
        result["reasons"].append("listing_marker_name_change_only")
        result["presentational_only"] = True
    return result


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return value if isinstance(value, (int, float, str, bool)) else str(value)


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def record_revision(
    revisions_dir: Path,
    *,
    frozen_path: Path,
    candidate_path: Path | None,
    classification: dict[str, Any],
    context: str,
    candidate_frame: pd.DataFrame | None = None,
) -> Path:
    """Write a new, immutable revision record (and optionally the revised data)."""

    revisions_dir.mkdir(parents=True, exist_ok=True)
    frozen_sha = sha256_file(frozen_path)
    candidate_sha = sha256_file(candidate_path) if candidate_path else None
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    token = hashlib.sha256(f"{frozen_sha}:{candidate_sha}:{context}".encode()).hexdigest()[:12]
    base = f"{Path(frozen_path).stem}.rev-{token}"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "context": context,
        "detected_at": stamp,
        "frozen_input": {"path": str(frozen_path), "sha256": frozen_sha, "used": True},
        "supplier_copy": {"path": str(candidate_path) if candidate_path else None, "sha256": candidate_sha},
        "classification": classification,
        "policy": "frozen input kept and used; supplier copy stored as a separate version; no overwrite",
    }
    if candidate_frame is not None:
        data_path = revisions_dir / f"{base}.parquet"
        if not data_path.exists():
            fd, tmp_name = tempfile.mkstemp(prefix=f".{base}.", suffix=".parquet", dir=str(revisions_dir))
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                candidate_frame.reset_index(drop=True).to_parquet(tmp, index=False)
                os.replace(tmp, data_path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        payload["supplier_copy"]["stored_version"] = str(data_path)
        payload["supplier_copy"]["stored_sha256"] = sha256_file(data_path)
    record_path = revisions_dir / f"{base}.json"
    if not record_path.exists():
        fd, tmp_name = tempfile.mkstemp(prefix=f".{base}.", suffix=".json", dir=str(revisions_dir))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        os.replace(tmp_name, record_path)
    return record_path
