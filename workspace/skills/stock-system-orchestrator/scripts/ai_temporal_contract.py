"""Fail-closed timing contract for analysis provenance.

The stock date and the analysis generation date are different facts.  This
module keeps that distinction in one place so every materialization path uses
the same eligibility decision.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo


EXCHANGE_TIMEZONE = ZoneInfo("Asia/Shanghai")
SOURCE_DATE_KEYS = ("ai_source_date", "ai_analysis_date", "trade_date", "recommend_date", "source_date")
EVIDENCE_TIME_KEYS = ("ai_evidence_time", "generated_at", "created_at", "analyzed_at")


def normalized_ai_date(value: Any) -> str | None:
    """Return YYYYMMDD, converting timezone-aware timestamps to exchange time."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value.strftime("%Y%m%d")
    else:
        text = str(value).strip()
        compact = text.replace("-", "").replace("/", "")
        if len(compact) >= 8 and compact[:8].isdigit() and (len(compact) == 8 or not compact[8].isdigit()):
            return compact[:8]
        parsed = None
        iso = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(EXCHANGE_TIMEZONE)
    return parsed.strftime("%Y%m%d")


def _distinct_dates(entry: dict[str, Any], keys: Iterable[str]) -> set[str]:
    dates: set[str] = set()
    for key in keys:
        value = entry.get(key)
        if value in (None, ""):
            continue
        normalized = normalized_ai_date(value)
        if normalized is not None:
            dates.add(normalized)
        else:
            dates.add("")
    return dates


def classify_ai_temporal_evidence(entry: dict[str, Any], target_trade_date: str | None) -> str:
    """Classify timing without trusting producer-supplied provenance labels.

    Results are ``fresh_same_day``, ``delayed_analysis``, ``stale_historical`` or
    ``unknown``.  Conflicting or missing evidence is always ``unknown``.
    """
    target = normalized_ai_date(target_trade_date)
    source_dates = _distinct_dates(entry, SOURCE_DATE_KEYS)
    evidence_dates = _distinct_dates(entry, EVIDENCE_TIME_KEYS)
    if not target or "" in source_dates or "" in evidence_dates:
        return "unknown"
    if len(source_dates) != 1:
        return "unknown"
    source_date = next(iter(source_dates))
    if source_date != target:
        return "stale_historical"
    if len(evidence_dates) != 1:
        return "unknown"
    evidence_date = next(iter(evidence_dates))
    if evidence_date == target:
        return "fresh_same_day"
    if evidence_date > target:
        return "delayed_analysis"
    return "unknown"


def is_same_day_ai_evidence(entry: dict[str, Any], target_trade_date: str | None) -> bool:
    return classify_ai_temporal_evidence(entry, target_trade_date) == "fresh_same_day"
