#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from orchestrator_common import HEALTH_DIR, sanitize_json_value


DEFAULT_CALENDAR_PATH = HEALTH_DIR / "trading_calendar.json"
DEFAULT_HISTORY_DAYS = 400
DEFAULT_FUTURE_DAYS = 60


def calendar_path(path: Path | None = None) -> Path:
    return path or DEFAULT_CALENDAR_PATH


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            json.dump(sanitize_json_value(payload), tmp, ensure_ascii=False, indent=2, allow_nan=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def build_trading_calendar_payload(
    calendar_frame: pd.DataFrame,
    *,
    source: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if calendar_frame is None or calendar_frame.empty:
        raise ValueError("trade_cal returned empty result")
    required = {"cal_date", "is_open"}
    missing = required - set(calendar_frame.columns)
    if missing:
        raise ValueError(f"trade_cal missing required columns: {sorted(missing)}")
    frame = calendar_frame.copy()
    frame["cal_date"] = frame["cal_date"].astype(str).str.replace("-", "", regex=False)
    if not frame["cal_date"].str.fullmatch(r"\d{8}").all():
        raise ValueError("trade_cal contains invalid cal_date values")
    frame["is_open"] = pd.to_numeric(frame["is_open"], errors="coerce")
    if frame["is_open"].isna().any():
        raise ValueError("trade_cal contains invalid is_open values")
    open_dates = sorted(frame.loc[frame["is_open"] == 1, "cal_date"].drop_duplicates().tolist())
    if not open_dates:
        raise ValueError("trade_cal produced no open dates")
    return {
        "source": source,
        "generated_at": generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from": str(frame["cal_date"].min()),
        "to": str(frame["cal_date"].max()),
        "open_dates": open_dates,
    }


def fetch_trading_calendar_payload(
    pro: Any,
    *,
    as_of_date: str | None = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    future_days: int = DEFAULT_FUTURE_DAYS,
    generated_at: str | None = None,
) -> dict[str, Any]:
    base_dt = datetime.strptime(as_of_date, "%Y%m%d") if as_of_date else datetime.now()
    start_date = (base_dt - timedelta(days=max(history_days, 1))).strftime("%Y%m%d")
    end_date = (base_dt + timedelta(days=max(future_days, 45))).strftime("%Y%m%d")
    calendar_frame = pro.trade_cal(
        exchange="SSE",
        start_date=start_date,
        end_date=end_date,
        fields="cal_date,is_open,pretrade_date",
    )
    payload = build_trading_calendar_payload(
        calendar_frame,
        source="tushare_trade_cal",
        generated_at=generated_at,
    )
    payload["requested_as_of"] = as_of_date or base_dt.strftime("%Y%m%d")
    payload["future_days"] = max(future_days, 45)
    payload["history_days"] = max(history_days, 1)
    if payload["to"] < (base_dt + timedelta(days=45)).strftime("%Y%m%d"):
        raise ValueError("trade_cal horizon shorter than required future 45 days")
    return payload


def persist_trading_calendar(
    pro: Any,
    *,
    path: Path | None = None,
    as_of_date: str | None = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    future_days: int = DEFAULT_FUTURE_DAYS,
    generated_at: str | None = None,
) -> dict[str, Any]:
    payload = fetch_trading_calendar_payload(
        pro,
        as_of_date=as_of_date,
        history_days=history_days,
        future_days=future_days,
        generated_at=generated_at,
    )
    write_json_atomic(calendar_path(path), payload)
    return payload


def load_trading_calendar(path: Path | None = None) -> dict[str, Any]:
    target = calendar_path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    open_dates = payload.get("open_dates")
    if not isinstance(open_dates, list):
        return {}
    normalized = [str(item) for item in open_dates if isinstance(item, (str, int))]
    if not normalized or not all(len(item) == 8 and item.isdigit() for item in normalized):
        return {}
    payload["open_dates"] = sorted(dict.fromkeys(normalized))
    return payload


def load_open_trade_dates(path: Path | None = None) -> list[str]:
    payload = load_trading_calendar(path)
    return list(payload.get("open_dates") or [])


def next_open_trade_date(base_date: str, *, open_dates: list[str] | None = None, path: Path | None = None) -> str | None:
    dates = open_dates if open_dates is not None else load_open_trade_dates(path)
    base = str(base_date or "")
    if not (len(base) == 8 and base.isdigit()):
        return None
    for trade_date in dates:
        if trade_date > base:
            return trade_date
    return None
