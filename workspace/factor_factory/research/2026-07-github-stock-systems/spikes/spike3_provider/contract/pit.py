"""PIT as-of single entry point.

Acceptance (red line 2): replay any historical as_of — records with
available_at > as_of must never be returned.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Sequence, TypeVar

from .core import StrictRow

R = TypeVar("R", bound=StrictRow)


def pit_filter(rows: Sequence[R], as_of: datetime) -> List[R]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return [r for r in rows if r.available_at <= as_of]


def assert_pit_clean(rows: Sequence[R], as_of: datetime) -> int:
    """Returns violation count of an already-filtered set; must be 0."""
    filtered = pit_filter(rows, as_of)
    violations = [r for r in filtered if r.available_at > as_of]
    assert len(violations) == 0, f"PIT breach: {len(violations)} rows visible past as_of"
    return len(violations)
