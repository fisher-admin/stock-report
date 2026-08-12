#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np


UNIVERSE_COMPAT_COLUMNS = [
    "trade_date",
    "ts_code",
    "name",
    "avg_amount_lookback",
    "trading_ratio_lookback",
    "listing_days",
    "liquidity_threshold",
    "universe_flag",
]

DAILY_BASIC_COMPAT_COLUMNS = [
    "trade_date",
    "ts_code",
    "close",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]

META_COLUMNS = ["source", "provenance", "used_proxy", "completeness"]


class PitSnapshotIncomplete(RuntimeError):
    pass


def _require_trade_date(trade_date: str) -> str:
    text = str(trade_date or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise PitSnapshotIncomplete(f"invalid trade_date: {trade_date!r}")
    return text


def _empty_or_frame(frame: pd.DataFrame | None, name: str) -> pd.DataFrame:
    if frame is None:
        raise PitSnapshotIncomplete(f"missing critical PIT dataset: {name}")
    if not isinstance(frame, pd.DataFrame):
        raise PitSnapshotIncomplete(f"invalid PIT dataset type for {name}: {type(frame).__name__}")
    return frame.copy()


def _reject_proxy(frame: pd.DataFrame, name: str) -> None:
    for col in ("source_provider", "fallback_source", "source"):
        if col not in frame.columns:
            continue
        vals = [str(v).lower() for v in frame[col].dropna().tolist()]
        if any("proxy" in v for v in vals):
            raise PitSnapshotIncomplete(f"{name} used proxy provenance")


def _normalize_stock_basic(frame: pd.DataFrame, list_status: str) -> pd.DataFrame:
    required = {"ts_code", "name", "list_date"}
    missing = required - set(frame.columns)
    if missing:
        raise PitSnapshotIncomplete(f"stock_basic[{list_status}] missing columns: {sorted(missing)}")
    out = frame.copy()
    out["ts_code"] = out["ts_code"].astype(str)
    out["name"] = out["name"].astype(str)
    out["list_date"] = out["list_date"].astype(str)
    out["delist_date"] = out.get("delist_date", "").astype(str) if "delist_date" in out.columns else ""
    out["current_list_status"] = out.get("list_status", list_status)
    out["industry_name"] = out.get("industry", "")
    out["market_board"] = out.get("market", "")
    return out[
        [
            "ts_code",
            "name",
            "list_date",
            "delist_date",
            "current_list_status",
            "industry_name",
            "market_board",
        ]
    ]


def _active_sw2021_membership(index_classify: pd.DataFrame, index_member_all: pd.DataFrame, trade_date: str) -> dict[str, str | None]:
    cls = index_classify.copy()
    mem = index_member_all.copy()
    if "level" in cls.columns:
        cls = cls[cls["level"].astype(str) == "L1"].copy()
    if "src" in cls.columns:
        cls = cls[cls["src"].astype(str).str.upper() == "SW2021"].copy()
    code_to_name = {
        str(row["index_code"]): str(row.get("industry_name") or "")
        for _, row in cls.iterrows()
        if str(row.get("index_code") or "")
    }
    security_column = "ts_code" if "ts_code" in mem.columns else "con_code"
    industry_code_column = "l1_code" if "l1_code" in mem.columns else "index_code"
    missing = {security_column, industry_code_column, "in_date", "out_date"} - set(mem.columns)
    if missing:
        raise PitSnapshotIncomplete(f"index_member_all missing columns: {sorted(missing)}")
    active: dict[str, str | None] = {}
    for _, row in mem.iterrows():
        ts_code = str(row.get(security_column) or "")
        if not ts_code:
            continue
        in_date = str(row.get("in_date") or "")
        out_date = str(row.get("out_date") or "")
        if in_date and in_date > trade_date:
            continue
        if out_date and out_date < trade_date:
            continue
        industry_name = str(row.get("l1_name") or "").strip()
        if not industry_name:
            industry_name = code_to_name.get(str(row.get(industry_code_column) or "")) or ""
        if industry_name:
            active[ts_code] = industry_name
    return active


def _fetch_sw2021_membership(client: Any, index_classify: pd.DataFrame) -> pd.DataFrame:
    """Fetch every SW2021 L1 bucket; the API's unfiltered form is capped at 3,000 rows."""
    if "index_code" not in index_classify.columns:
        raise PitSnapshotIncomplete("index_classify missing index_code")
    index_codes = sorted(
        {
            str(value)
            for value in index_classify["index_code"].dropna().tolist()
            if str(value)
        }
    )
    if not index_codes:
        raise PitSnapshotIncomplete("index_classify returned no SW2021 L1 industries")
    frames: list[pd.DataFrame] = []
    for index_code in index_codes:
        frame = _empty_or_frame(
            client.index_member_all(l1_code=index_code),
            f"index_member_all[{index_code}]",
        )
        _reject_proxy(frame, f"index_member_all[{index_code}]")
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise PitSnapshotIncomplete("index_member_all returned no SW2021 membership rows")
    combined = pd.concat(frames, ignore_index=True)
    security_column = "ts_code" if "ts_code" in combined.columns else "con_code"
    if security_column not in combined.columns:
        raise PitSnapshotIncomplete("index_member_all missing security code column")
    if combined[security_column].astype(str).str.strip().eq("").all():
        raise PitSnapshotIncomplete("index_member_all returned no valid security codes")
    return combined


def collect_pit_market_snapshot(client: Any, trade_date: str) -> dict[str, Any]:
    trade_date = _require_trade_date(trade_date)
    stock_basic_frames = {
        status: _normalize_stock_basic(
            _empty_or_frame(client.stock_basic(list_status=status, fields=None), f"stock_basic[{status}]"),
            status,
        )
        for status in ("L", "D", "P")
    }
    stock_st = _empty_or_frame(client.stock_st(trade_date=trade_date), "stock_st")
    suspend_d = _empty_or_frame(client.suspend_d(trade_date=trade_date), "suspend_d")
    daily_basic = _empty_or_frame(client.daily_basic(trade_date=trade_date, fields=None), "daily_basic")
    index_classify = _empty_or_frame(client.index_classify(src="SW2021", level="L1"), "index_classify")
    index_member_all = _fetch_sw2021_membership(client, index_classify)

    for name, frame in {
        "stock_st": stock_st,
        "suspend_d": suspend_d,
        "daily_basic": daily_basic,
        "index_classify": index_classify,
        "index_member_all": index_member_all,
    }.items():
        _reject_proxy(frame, name)
    for frame in stock_basic_frames.values():
        _reject_proxy(frame, "stock_basic")

    stock_dim = pd.concat(stock_basic_frames.values(), ignore_index=True)
    if stock_dim.empty:
        raise PitSnapshotIncomplete("stock_basic(L/D/P) returned no securities")
    stock_dim = stock_dim.sort_values(["ts_code", "current_list_status"]).drop_duplicates("ts_code", keep="first").reset_index(drop=True)
    stock_dim["trade_date"] = trade_date
    stock_dim["listing_days"] = (
        pd.to_datetime(trade_date) - pd.to_datetime(stock_dim["list_date"], format="%Y%m%d", errors="coerce")
    ).dt.days
    st_codes = set(stock_st.get("ts_code", pd.Series(dtype=str)).astype(str))
    suspend_codes = set(suspend_d.get("ts_code", pd.Series(dtype=str)).astype(str))
    list_date = stock_dim["list_date"].fillna("").astype(str)
    delist_date = stock_dim["delist_date"].fillna("").astype(str)
    listed_by_date = (list_date.str.fullmatch(r"\d{8}")) & (list_date <= trade_date)
    delisted_by_date = delist_date.str.fullmatch(r"\d{8}") & (delist_date <= trade_date)
    stock_dim["list_status_at_date"] = np.where(
        ~listed_by_date,
        "not_listed",
        np.where(delisted_by_date, "D", "L"),
    )
    stock_dim["list_status"] = stock_dim["list_status_at_date"]
    stock_dim["is_st"] = stock_dim["ts_code"].isin(st_codes)
    # Current stock_basic status must never be projected backwards. Exact-date
    # suspend_d is the authoritative suspension state for the target day.
    stock_dim["is_suspended"] = stock_dim["ts_code"].isin(suspend_codes)
    stock_dim["sw2021_l1_name"] = stock_dim["ts_code"].map(
        _active_sw2021_membership(index_classify, index_member_all, trade_date)
    )
    stock_dim["sw2021_l1_name"] = stock_dim["sw2021_l1_name"].astype(object).where(
        stock_dim["sw2021_l1_name"].notna(), None
    )
    stock_dim["avg_amount_lookback"] = None
    stock_dim["trading_ratio_lookback"] = None
    stock_dim["liquidity_threshold"] = None
    if daily_basic.empty:
        raise PitSnapshotIncomplete("daily_basic returned empty snapshot")
    if "trade_date" not in daily_basic.columns:
        raise PitSnapshotIncomplete("daily_basic missing trade_date")
    payload_dates = set(daily_basic["trade_date"].astype(str).str.replace("-", "", regex=False))
    if payload_dates != {trade_date}:
        raise PitSnapshotIncomplete(
            f"daily_basic payload date mismatch: expected {trade_date}, got {sorted(payload_dates)}"
        )
    if daily_basic["ts_code"].astype(str).duplicated().any():
        raise PitSnapshotIncomplete("daily_basic contains duplicate securities")
    daily_codes = set(daily_basic["ts_code"].astype(str))
    stock_dim["has_daily_basic"] = stock_dim["ts_code"].isin(daily_codes)
    stock_dim["universe_flag"] = (
        (stock_dim["list_status_at_date"].astype(str) == "L")
        & (~stock_dim["is_st"])
        & (~stock_dim["is_suspended"])
        & (stock_dim["listing_days"].fillna(-1) >= 0)
        & stock_dim["has_daily_basic"]
    ).astype(int)
    stock_dim["tradable"] = stock_dim["universe_flag"]

    universe = stock_dim[
        UNIVERSE_COMPAT_COLUMNS
        + [
            "list_status",
            "list_status_at_date",
            "current_list_status",
            "list_date",
            "delist_date",
            "is_st",
            "is_suspended",
            "has_daily_basic",
            "tradable",
            "sw2021_l1_name",
            "industry_name",
            "market_board",
        ]
    ].copy()
    for col in META_COLUMNS:
        universe[col] = False if col == "used_proxy" else ("complete" if col == "completeness" else "tushare_pit")
    universe["provenance"] = "stock_basic(L/D/P)+stock_st+suspend_d+SW2021"

    missing_daily = set(DAILY_BASIC_COMPAT_COLUMNS) - set(daily_basic.columns)
    if missing_daily:
        raise PitSnapshotIncomplete(f"daily_basic missing columns: {sorted(missing_daily)}")
    daily_basic_frame = daily_basic[DAILY_BASIC_COMPAT_COLUMNS].copy()
    daily_basic_frame["trade_date"] = trade_date
    daily_basic_frame["sw2021_l1_name"] = daily_basic_frame["ts_code"].astype(str).map(
        _active_sw2021_membership(index_classify, index_member_all, trade_date)
    )
    daily_basic_frame["sw2021_l1_name"] = daily_basic_frame["sw2021_l1_name"].astype(object).where(
        daily_basic_frame["sw2021_l1_name"].notna(),
        None,
    )
    for col in META_COLUMNS:
        daily_basic_frame[col] = False if col == "used_proxy" else ("complete" if col == "completeness" else "tushare_pit")
    daily_basic_frame["provenance"] = "daily_basic+SW2021"

    metadata = {
        "trade_date": trade_date,
        "source": "tushare_pit",
        "provenance": {
            "stock_basic_statuses": ["L", "D", "P"],
            "used_sw2021_l1": True,
            "historical_listing_state": "reconstructed_from_list_date_and_delist_date",
            "suspension_state": "exact_trade_date_suspend_d",
            "st_state": "exact_trade_date_stock_st",
        },
        "used_proxy": False,
        "completeness": "complete",
    }
    return {
        "trade_date": trade_date,
        "universe": universe.sort_values("ts_code").reset_index(drop=True),
        "daily_basic": daily_basic_frame.sort_values("ts_code").reset_index(drop=True),
        "metadata": metadata,
    }


def _write_parquet_atomic(
    frame: pd.DataFrame,
    path: Path,
    *,
    immutable: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.reset_index(drop=True)
    if immutable and path.exists():
        existing = pd.read_parquet(path).reset_index(drop=True)
        logical_existing = existing.copy()
        logical_normalized = normalized.copy()
        for logical in (logical_existing, logical_normalized):
            for column in logical.columns:
                values = logical[column].astype(object)
                values[pd.isna(values)] = None
                logical[column] = values
        try:
            pd.testing.assert_frame_equal(
                logical_existing,
                logical_normalized,
                check_dtype=False,
                check_like=False,
            )
        except AssertionError as exc:
            raise PitSnapshotIncomplete(f"immutable PIT snapshot mismatch: {path}") from exc
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(temporary)
    try:
        normalized.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_json_atomic(
    payload: dict[str, Any],
    path: Path,
    *,
    immutable: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if immutable and path.exists():
        try:
            existing = json.dumps(
                json.loads(path.read_text(encoding="utf-8")),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PitSnapshotIncomplete(f"invalid immutable PIT metadata: {path}") from exc
        if existing != canonical:
            raise PitSnapshotIncomplete(f"immutable PIT metadata mismatch: {path}")
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def write_pit_market_snapshot(
    snapshot: dict[str, Any],
    output_dir: Path,
    *,
    daily_basic_output_dir: Path | None = None,
    immutable: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    daily_dir = Path(daily_basic_output_dir) if daily_basic_output_dir is not None else output_dir
    daily_dir.mkdir(parents=True, exist_ok=True)
    trade_date = _require_trade_date(snapshot["trade_date"])
    universe_path = output_dir / f"universe_{trade_date}.parquet"
    daily_basic_path = daily_dir / f"daily_basic_{trade_date}.parquet"
    meta_path = output_dir / f"pit_market_snapshot_{trade_date}.json"
    _write_parquet_atomic(snapshot["universe"], universe_path, immutable=immutable)
    _write_parquet_atomic(snapshot["daily_basic"], daily_basic_path, immutable=immutable)
    _write_json_atomic(snapshot["metadata"], meta_path, immutable=immutable)
    return {
        "trade_date": trade_date,
        "universe_path": str(universe_path),
        "daily_basic_path": str(daily_basic_path),
        "metadata_path": str(meta_path),
    }
