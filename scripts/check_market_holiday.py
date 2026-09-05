#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-flight check: skip the deployment pipeline on A-share market holidays.

The cron schedule only skips weekends, but the Shanghai/Shenzhen markets are
also closed on official holidays (Spring Festival, National Day, ...). This
script loads ``data/market_holidays.json`` and reports whether today (in the
market timezone, Asia/Shanghai) is a market holiday.

It always exits with 0 so that a skipped run still completes cleanly; whether
to skip is communicated through the ``is_holiday`` step output and the log
line ``Skipping: YYYY-MM-DD is a market holiday``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOLIDAY_FILE = REPO_ROOT / "data" / "market_holidays.json"
MARKET_TIMEZONE = "Asia/Shanghai"


def load_holidays(path: Path = DEFAULT_HOLIDAY_FILE) -> set[str]:
    """Return the market closure dates as a set of 'YYYY-MM-DD' strings."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = payload.get("holidays", []) if isinstance(payload, dict) else payload
    return {str(entry).strip() for entry in entries}


def is_market_holiday(date: str, holidays: set[str]) -> bool:
    """Return True when *date* is a closed A-share market day."""
    return date in holidays


def today_in_market_timezone(now: datetime | None = None) -> str:
    """Return the current market date ('YYYY-MM-DD') in Asia/Shanghai."""
    if now is not None:
        return now.strftime("%Y-%m-%d")
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(MARKET_TIMEZONE))
    except Exception:
        # The 01:00 UTC cron (09:00 Beijing) never crosses a date boundary
        # between the two zones, so UTC is a safe fallback.
        now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def emit_step_output(key: str, value: str) -> None:
    """Append a GitHub Actions step output when running inside a workflow."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skip the deployment pipeline on A-share market holidays."
    )
    parser.add_argument(
        "--date",
        help="Date to check as YYYY-MM-DD (defaults to today in Asia/Shanghai).",
    )
    parser.add_argument(
        "--holidays-file",
        default=str(DEFAULT_HOLIDAY_FILE),
        help="Path to market_holidays.json.",
    )
    args = parser.parse_args(argv)

    date = args.date or today_in_market_timezone()
    try:
        holidays = load_holidays(Path(args.holidays_file))
    except (OSError, ValueError) as exc:
        # A broken calendar must not block deployments; log and continue.
        print(f"WARNING: could not load market holiday calendar ({exc}); continuing pipeline.", file=sys.stderr)
        emit_step_output("is_holiday", "false")
        emit_step_output("check_date", date)
        return 0

    holiday = is_market_holiday(date, holidays)
    emit_step_output("is_holiday", "true" if holiday else "false")
    emit_step_output("check_date", date)
    if holiday:
        print(f"Skipping: {date} is a market holiday")
    else:
        print(f"{date} is not a market holiday, continuing pipeline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
