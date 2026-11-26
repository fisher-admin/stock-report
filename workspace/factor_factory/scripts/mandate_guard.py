#!/usr/bin/env python3
"""Strict mandate board guard (zero-paid forward-shadow, 2026-11-26).

Allowed boards ONLY: Shanghai main board (60xxxx.SH), Shenzhen main board
(00xxxx.SZ) and ChiNext (30xxxx.SZ).  Everything else -- STAR (688/689), BSE
(4/8/9xxxxx.BJ), B-shares, funds, wrong-exchange suffixes, lowercase or
malformed strings, trailing newlines, non-ASCII digits, non-strings -- is
rejected.  Fail closed.
"""

from __future__ import annotations

import re
from typing import Iterable

import numpy as np

STRICT_MANDATE_RE = re.compile(r"^(60[0-9]{4}\.SH|00[0-9]{4}\.SZ|30[0-9]{4}\.SZ)\Z")


def is_allowed_mandate_code(code) -> bool:
    """True only for a str fullmatching STRICT_MANDATE_RE (ASCII digits only, no stripping, no trailing newline)."""
    return isinstance(code, str) and STRICT_MANDATE_RE.fullmatch(code) is not None


def is_allowed_mandate_board(codes: Iterable) -> np.ndarray:
    """Vectorised allow-mask (numpy bool array, same length/order as ``codes``)."""
    return np.fromiter((is_allowed_mandate_code(c) for c in codes), dtype=bool)


def assert_strict_mandate(codes: Iterable) -> None:
    """Raise AssertionError listing every offending code; passes silently when all are allowed."""
    items = list(codes)
    offenders = [c for c in items if not is_allowed_mandate_code(c)]
    if offenders:
        shown = ", ".join(repr(c) for c in offenders[:20])
        more = f" (+{len(offenders) - 20} more)" if len(offenders) > 20 else ""
        raise AssertionError(
            f"strict mandate violated by {len(offenders)}/{len(items)} codes: {shown}{more}"
        )
