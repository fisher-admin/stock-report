#!/usr/bin/env python3
"""Settled closing values for the three domestic indices.

20261009 incident: the provider had no 399006.SZ index_daily row for the session,
so the evening refresh silently kept the noon Sina spot quote (-2.61%) while the
real ChiNext close was +0.22%; the strict publication contract then blocked the
run.  After the close a domestic index is either a settled close for the session
or explicitly ``unavailable`` -- never an intraday/spot quote.

Resolution order after the close:
1. configured index_daily client (tushare/rds/promax), retried;
2. Sina daily bar (akshare ``stock_zh_index_daily``) dated the session itself;
3. ``unavailable`` with the collected errors.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")
# Continuous trading ends 15:00; leave a margin before trusting a daily bar as settled.
SETTLED_AFTER = (15, 30)

# key -> (index_daily ts_code, sina symbol, label)
DOMESTIC_INDEX: dict[str, tuple[str, str, str]] = {
    "shanghai": ("000001.SH", "sh000001", "上证指数"),
    "shenzhen": ("399001.SZ", "sz399001", "深证成指"),
    "chinext": ("399006.SZ", "sz399006", "创业板指"),
}

SETTLED_KIND = "exact_close"
UNAVAILABLE_KIND = "unavailable"


def _date8(value: Any) -> str:
    return str(value or "").replace("-", "").strip()[:8]


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def is_post_close(trade_date: str, now: datetime | None = None) -> bool:
    """True once the session for ``trade_date`` has settled (Asia/Shanghai)."""
    trade_date = _date8(trade_date)
    if len(trade_date) != 8 or not trade_date.isdigit():
        return False
    now = now.astimezone(SH_TZ) if now and now.tzinfo else (now.replace(tzinfo=SH_TZ) if now else datetime.now(SH_TZ))
    today = now.strftime("%Y%m%d")
    if trade_date < today:
        return True
    if trade_date > today:
        return False
    return (now.hour, now.minute) >= SETTLED_AFTER


def _snapshot(key: str, *, source_kind: str, provider: str, close: float | None, prev_close: float | None,
              as_of: str | None, raw_change_pct: float | None = None, source_error: str | None = None) -> dict[str, Any]:
    _, symbol, label = DOMESTIC_INDEX[key]
    change_pct = None
    if close is not None and prev_close not in (None, 0):
        change_pct = round((close / prev_close - 1.0) * 100.0, 4)
    elif raw_change_pct is not None:
        change_pct = round(raw_change_pct, 4)
    return {
        "key": key,
        "symbol": symbol,
        "label": label,
        "source_kind": source_kind,
        "provider": provider,
        "close": close,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "as_of": as_of,
        "bar_count": 1 if change_pct is not None else 0,
        "source_error": None if change_pct is not None else source_error,
    }


def fetch_provider_close(
    pro: Any,
    ts_code: str,
    trade_date: str,
    *,
    attempts: int = 3,
    delay_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any] | None, str | None]:
    """index_daily row for exactly ``trade_date``; retried on empty result or error."""
    if not pro:
        return None, "index_daily client unavailable"
    error = None
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(delay_seconds)
        try:
            frame = pro.index_daily(ts_code=ts_code, trade_date=trade_date)
        except Exception as exc:  # provider/network failure: retry, then report
            error = f"index_daily {ts_code}@{trade_date}: {type(exc).__name__}: {exc}"
            continue
        if frame is None or len(frame) == 0:
            error = f"index_daily empty for {ts_code}@{trade_date}"
            continue
        row = frame.iloc[0]
        close = _num(row.get("close"))
        if close is None or _date8(row.get("trade_date") or trade_date) != trade_date:
            error = f"index_daily row unusable for {ts_code}@{trade_date}"
            continue
        provider = str(row.get("source_provider") or getattr(pro, "provider_name", "") or "tushare")
        return {
            "close": close,
            "prev_close": _num(row.get("pre_close")),
            "pct_chg": _num(row.get("pct_chg")),
            "provider": provider,
        }, None
    return None, error


def fetch_sina_daily_close(ak_module: Any, sina_symbol: str, trade_date: str) -> tuple[dict[str, Any] | None, str | None]:
    """Sina daily bar dated ``trade_date``; only meaningful once the session settled."""
    if ak_module is None:
        return None, "akshare unavailable"
    try:
        frame = ak_module.stock_zh_index_daily(symbol=sina_symbol)
    except Exception as exc:
        return None, f"stock_zh_index_daily {sina_symbol}: {type(exc).__name__}: {exc}"
    if frame is None or len(frame) == 0 or "date" not in frame or "close" not in frame:
        return None, f"stock_zh_index_daily empty for {sina_symbol}"
    dates = [_date8(value) for value in frame["date"]]
    if trade_date not in dates:
        return None, f"stock_zh_index_daily has no {trade_date} bar for {sina_symbol}"
    pos = dates.index(trade_date)
    close = _num(frame["close"].iloc[pos])
    prev_close = _num(frame["close"].iloc[pos - 1]) if pos > 0 else None
    if close is None or prev_close is None:
        return None, f"stock_zh_index_daily incomplete {trade_date} bar for {sina_symbol}"
    return {"close": close, "prev_close": prev_close, "pct_chg": None, "provider": "akshare_sina_daily"}, None


def _default_akshare() -> Any:
    try:
        import akshare  # noqa: PLC0415 - optional heavy dependency
    except Exception:
        return None
    return akshare


def resolve_settled_close(
    key: str,
    trade_date: str,
    *,
    pro: Any,
    ak_module: Any = "default",
    now: datetime | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Settled close snapshot for ``key`` or an explicit ``unavailable`` snapshot.

    Must only be used once the session settled; before that there is no close.
    """
    trade_date = _date8(trade_date)
    if not is_post_close(trade_date, now):
        raise ValueError(f"session {trade_date} has not settled; no closing value exists yet")
    ts_code, sina_symbol, _ = DOMESTIC_INDEX[key]
    errors: list[str] = []

    row, error = fetch_provider_close(pro, ts_code, trade_date, attempts=attempts, sleep=sleep)
    if row is None:
        errors.append(error or "index_daily failed")
        if ak_module == "default":
            ak_module = _default_akshare()
        row, error = fetch_sina_daily_close(ak_module, sina_symbol, trade_date)
        if row is None:
            errors.append(error or "sina daily failed")

    if row is None:
        return _snapshot(key, source_kind=UNAVAILABLE_KIND, provider="unavailable", close=None, prev_close=None,
                         as_of=trade_date, source_error="; ".join(errors))
    return _snapshot(key, source_kind=SETTLED_KIND, provider=row["provider"], close=row["close"],
                     prev_close=row["prev_close"], as_of=trade_date, raw_change_pct=row["pct_chg"])


def is_settled_for(snapshot: dict[str, Any] | None, trade_date: str) -> bool:
    snapshot = snapshot or {}
    return (
        str(snapshot.get("source_kind") or "") == SETTLED_KIND
        and _date8(snapshot.get("as_of")) == _date8(trade_date)
        and _num(snapshot.get("change_pct")) is not None
    )
