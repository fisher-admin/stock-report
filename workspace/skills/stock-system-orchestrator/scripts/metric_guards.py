#!/usr/bin/env python3
"""Guards for honest ratio/coverage metrics (plan v1.4 Epic A)."""
from __future__ import annotations

from typing import Any


def safe_ratio(have: Any, total: Any, *, default: float | None = 0.0) -> float | None:
    """Return have/total clamped to [0, 1]. Invalid inputs → default."""
    try:
        h = float(have)
        t = float(total)
    except (TypeError, ValueError):
        return default
    if t <= 0:
        return default
    value = h / t
    if value != value:  # NaN
        return default
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def coverage_payload(have: Any, total: Any, *, pass_threshold: float = 0.6) -> dict[str, Any]:
    """Build a self-consistent coverage object with value always in [0, 1]."""
    try:
        h = int(have)
    except (TypeError, ValueError):
        h = 0
    try:
        t = int(total)
    except (TypeError, ValueError):
        t = 0
    if h < 0:
        h = 0
    if t < 0:
        t = 0
    if h > t and t > 0:
        # Do not invent coverage; cap have for display consistency and flag.
        capped_have = t
        value = 1.0
        inconsistent = True
    else:
        capped_have = h
        value = safe_ratio(capped_have, t, default=0.0) or 0.0
        inconsistent = False
    status = "pass" if t > 0 and value >= pass_threshold else "fail"
    if t == 0:
        status = "unknown"
    return {
        "have": capped_have,
        "total": t,
        "value": round(float(value), 4),
        "status": status,
        "raw_have": h,
        "inconsistent_have_gt_total": inconsistent,
    }


def assert_ratio_at_most_one(value: Any, *, field: str) -> list[str]:
    """Return error messages if ratio is illegal (>1 or negative)."""
    errors: list[str] = []
    try:
        if value is None:
            return errors
        v = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} 不是合法数字")
        return errors
    if v < 0:
        errors.append(f"{field}={v} < 0")
    if v > 1.0 + 1e-12:
        errors.append(f"{field}={v} > 1（覆盖率/比率不得超过 100%）")
    return errors
