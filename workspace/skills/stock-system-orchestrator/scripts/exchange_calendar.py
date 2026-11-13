#!/usr/bin/env python3
"""Shared exchange-calendar check for stock business entry points.

Standard library only, so the Hermes wrappers can call it before any heavy
import.  The cached calendar is written by Stage 1 from the configured data
provider (``trading_calendar.json``).

Rules (2026-10-03 reliability revision):
- open      -> the date is an SSE trading session in a valid, current calendar.
- closed    -> the date lies inside the calendar range and is NOT an open date.
- error     -> the calendar is missing, unreadable, malformed, stale, or does
               not cover the date.  An unusable calendar is never evidence that
               the market is closed.

CLI (used by the Hermes wrappers)::

    python exchange_calendar.py gate --job morning_brief
      exit 0  -> open: continue with business work
      exit 3  -> closed: durable ``skipped_market_closed`` outcome written
      exit 2  -> calendar error: durable ``calendar_error`` outcome written,
                 message printed so the scheduler reports the failure
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo

    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    SHANGHAI = None

DATE_RE = re.compile(r"^\d{8}$")
JOB_RE = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_CALENDAR_AGE_DAYS = 45

EXIT_OPEN = 0
EXIT_ERROR = 2
EXIT_CLOSED = 3


def _workspace() -> Path:
    return Path(
        os.environ.get("STOCK_SYSTEM_WORKSPACE")
        or os.environ.get("STOCK_WORKSPACE")
        or str(Path.home() / ".openclaw" / "workspace")
    )


def default_health_dir() -> Path:
    return _workspace() / "stock_data" / "03-working" / "health"


def default_calendar_path() -> Path:
    return default_health_dir() / "trading_calendar.json"


def now_shanghai() -> datetime:
    if SHANGHAI is not None:
        return datetime.now(SHANGHAI)
    return datetime.now()


def today_shanghai() -> str:
    return now_shanghai().strftime("%Y%m%d")


class CalendarError(RuntimeError):
    """The calendar cannot be trusted for an open/closed decision."""


def _parse_generated_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def load_calendar(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else default_calendar_path()
    if not target.is_file():
        raise CalendarError(f"trading calendar missing: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CalendarError(f"trading calendar unreadable: {target}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise CalendarError("trading calendar is not an object")
    source = str(payload.get("source") or "").strip()
    if not source:
        raise CalendarError("trading calendar has no source")
    date_from = str(payload.get("from") or "")
    date_to = str(payload.get("to") or "")
    if not (DATE_RE.fullmatch(date_from) and DATE_RE.fullmatch(date_to)) or date_from > date_to:
        raise CalendarError("trading calendar range invalid")
    raw = payload.get("open_dates")
    if not isinstance(raw, list) or not raw:
        raise CalendarError("trading calendar has no open_dates")
    open_dates: list[str] = []
    for item in raw:
        text = str(item)
        if not DATE_RE.fullmatch(text):
            raise CalendarError(f"trading calendar contains invalid date: {text!r}")
        if not (date_from <= text <= date_to):
            raise CalendarError(f"trading calendar open date outside range: {text}")
        open_dates.append(text)
    generated = _parse_generated_at(payload.get("generated_at"))
    if generated is None:
        raise CalendarError("trading calendar generated_at missing or invalid")
    return {
        "path": str(target),
        "source": source,
        "generated_at": str(payload.get("generated_at")),
        "generated_dt": generated,
        "from": date_from,
        "to": date_to,
        "open_dates": sorted(set(open_dates)),
    }


def classify_date(
    date: str,
    *,
    calendar_path: Path | None = None,
    now: datetime | None = None,
    max_age_days: int = MAX_CALENDAR_AGE_DAYS,
) -> dict[str, Any]:
    """Return {'status': 'open'|'closed'|'error', ...} for an exchange date."""

    result: dict[str, Any] = {"date": str(date or ""), "status": "error"}
    if not DATE_RE.fullmatch(str(date or "")):
        result["reason"] = f"invalid date: {date!r}"
        return result
    try:
        cal = load_calendar(calendar_path)
    except CalendarError as exc:
        result["reason"] = str(exc)
        return result
    result["calendar"] = {
        "path": cal["path"],
        "source": cal["source"],
        "generated_at": cal["generated_at"],
        "from": cal["from"],
        "to": cal["to"],
    }
    current = now or now_shanghai()
    current_naive = current.replace(tzinfo=None)
    if current_naive - cal["generated_dt"] > timedelta(days=max_age_days):
        result["reason"] = (
            f"trading calendar stale: generated_at={cal['generated_at']} "
            f"older than {max_age_days} days"
        )
        return result
    if cal["generated_dt"] - current_naive > timedelta(days=1):
        result["reason"] = f"trading calendar generated_at is in the future: {cal['generated_at']}"
        return result
    if not (cal["from"] <= date <= cal["to"]):
        result["reason"] = f"trading calendar does not cover {date} (range {cal['from']}..{cal['to']})"
        return result
    result["status"] = "open" if date in cal["open_dates"] else "closed"
    prior = [value for value in cal["open_dates"] if value < date]
    later = [value for value in cal["open_dates"] if value > date]
    result["previous_open_date"] = prior[-1] if prior else None
    result["next_open_date"] = later[0] if later else None
    return result


def require_open_session(date: str, *, calendar_path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Raise unless ``date`` is a confirmed open session.

    Used by session-analysis generators so a wall-clock date can never be
    turned into a fake trading session on a holiday.
    """

    decision = classify_date(date, calendar_path=calendar_path, now=now)
    if decision["status"] == "open":
        return decision
    if decision["status"] == "closed":
        raise CalendarError(f"SESSION_NOT_OPEN: {date} is a closed exchange day")
    raise CalendarError(f"CALENDAR_UNAVAILABLE: {decision.get('reason')}")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def record_outcome(
    *,
    job: str,
    date: str,
    outcome: str,
    detail: dict[str, Any],
    health_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    if not JOB_RE.fullmatch(job):
        raise ValueError(f"invalid job name: {job!r}")
    base = Path(health_dir) if health_dir else default_health_dir()
    path = base / "business_outcomes" / date / f"{job}.json"
    payload = {
        "schema_version": 1,
        "job": job,
        "session_date": date,
        "outcome": outcome,
        "recorded_at": (now or now_shanghai()).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        **detail,
    }
    _write_json_atomic(path, payload)
    return path


def gate(job: str, *, date: str | None = None, calendar_path: Path | None = None,
         health_dir: Path | None = None, now: datetime | None = None) -> int:
    current = now or now_shanghai()
    session = date or current.strftime("%Y%m%d")
    decision = classify_date(session, calendar_path=calendar_path, now=current)
    status = decision["status"]
    if status == "open":
        return EXIT_OPEN
    if status == "closed":
        record_outcome(
            job=job,
            date=session,
            outcome="skipped_market_closed",
            detail={
                "calendar": decision.get("calendar"),
                "previous_open_date": decision.get("previous_open_date"),
                "next_open_date": decision.get("next_open_date"),
            },
            health_dir=health_dir,
            now=current,
        )
        return EXIT_CLOSED
    try:
        record_outcome(
            job=job,
            date=session,
            outcome="calendar_error",
            detail={"reason": decision.get("reason"), "calendar": decision.get("calendar")},
            health_dir=health_dir,
            now=current,
        )
    except Exception as exc:  # noqa: BLE001 - the error must still be reported
        print(f"(failed to record calendar_error outcome: {type(exc).__name__})")
    print(f"🚨 【交易日历不可用】{job} 未执行业务步骤：{decision.get('reason')}")
    print("日历无效不等于休市；请检查 trading_calendar.json（由 Stage1 刷新）。")
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exchange calendar gate for stock jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    gate_parser = sub.add_parser("gate")
    gate_parser.add_argument("--job", required=True)
    gate_parser.add_argument("--date", default="")
    gate_parser.add_argument("--calendar", default="")
    gate_parser.add_argument("--health-dir", default="")
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--date", default="")
    check_parser.add_argument("--calendar", default="")
    args = parser.parse_args(argv)
    calendar_path = Path(args.calendar) if args.calendar else None
    if args.command == "gate":
        if not JOB_RE.fullmatch(args.job):
            print(f"invalid job name: {args.job!r}")
            return EXIT_ERROR
        return gate(
            args.job,
            date=args.date or None,
            calendar_path=calendar_path,
            health_dir=Path(args.health_dir) if args.health_dir else None,
        )
    decision = classify_date(args.date or today_shanghai(), calendar_path=calendar_path)
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return {"open": EXIT_OPEN, "closed": EXIT_CLOSED}.get(decision["status"], EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
