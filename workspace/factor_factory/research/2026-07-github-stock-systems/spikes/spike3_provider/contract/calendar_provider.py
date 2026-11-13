"""TradingCalendarV1 (minimal slice for this spike).

Primary: akshare tool_trade_date_hist_sina (aligned with workspace practice —
Tushare-calendar SPOF was removed in production). Fallback: tushare proxy
trade_cal. Horizon-tail guard: horizon_end - today < 90 days => hard warn.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional, Set, Tuple

logger = logging.getLogger("spike3.calendar")

HORIZON_GUARD_DAYS = 90


def load_open_days(sink=None) -> Tuple[Set[date], List[str]]:
    """Returns (open_days, notes). Records lineage attempts when sink given."""
    notes: List[str] = []
    open_days: Optional[Set[date]] = None

    from .lineage import timed_attempt

    if sink is not None:
        ctx = timed_attempt(sink, "calendar", "akshare_sina", "tool_trade_date_hist_sina")
    else:
        import contextlib

        ctx = contextlib.nullcontext(type("N", (), {"record_count": None})())
    try:
        with ctx as att:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            days = {d for d in df["trade_date"].tolist()}
            att.record_count = len(days)
            open_days = days
    except Exception as exc:
        notes.append(f"akshare calendar failed: {type(exc).__name__}: {exc}")

    if open_days is None:
        if sink is not None:
            ctx2 = timed_attempt(sink, "calendar", "tushare_proxy", "trade_cal")
        else:
            import contextlib

            ctx2 = contextlib.nullcontext(type("N", (), {"record_count": None})())
        with ctx2 as att:
            from .tushare_client import get_pro

            pro = get_pro()
            tcal = pro.trade_cal(exchange="SSE", start_date="19901219", end_date="20271231")
            open_rows = tcal[tcal["is_open"] == 1]
            open_days = {
                date(int(s[:4]), int(s[4:6]), int(s[6:8])) for s in open_rows["cal_date"].astype(str)
            }
            att.record_count = len(open_days)
        notes.append("calendar source = tushare_proxy fallback")

    horizon_end = max(open_days)
    tail = (horizon_end - date.today()).days
    if tail < HORIZON_GUARD_DAYS:
        msg = f"CALENDAR_TAIL_GUARD: horizon_end={horizon_end} only {tail}d ahead (<{HORIZON_GUARD_DAYS}d)"
        logger.warning(msg)
        notes.append(msg)
    return open_days, notes


def last_n_open_days(open_days: Set[date], n: int, before: Optional[date] = None) -> List[date]:
    ref = before or date.today()
    past = sorted(d for d in open_days if d <= ref)
    return past[-n:]
