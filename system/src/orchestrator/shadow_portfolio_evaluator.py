#!/usr/bin/env python3
"""Forward-only accounting and promotion gates for short-track shadow books.

This module never changes production rankings and never grants execution authority.
Signals are generated after T close, entered at the next open, and diagnosed at the
T+1/T+3/T+5 adjusted closes.  Only signals on or after the frozen validation epoch
can count toward promotion evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


VALIDATION_START_DATE = "20260811"
PRIMARY_HOLDING_PERIOD_DAYS = 5
DIAGNOSTIC_HOLDING_PERIOD_DAYS = (1, 3)
BASE_ROUND_TRIP_COST = 0.003
STRESS_ROUND_TRIP_COST = 0.005
BENCHMARK_ID = "all_a_tradable_equal_weight"
MIN_NEW_TRADE_DAYS = 60
MIN_RISK_ADJUSTED_RETURN = 0.5
MAX_DRAWDOWN = 0.08
MIN_POSITIVE_20D_WINDOWS = 2
MAX_INDUSTRY_CONTRIBUTION_SHARE = 0.50
MAX_TOP5_STOCK_CONTRIBUTION_SHARE = 0.50
COHORT_CAPITAL_FRACTION = 1.0 / PRIMARY_HOLDING_PERIOD_DAYS


class EvaluationContractError(ValueError):
    """Raised when a candidate snapshot cannot be evaluated honestly."""


LEDGER_COLUMNS = [
    "record_id",
    "strategy_id",
    "strategy_version",
    "signal_date",
    "signal_data_cutoff",
    "planned_entry_time",
    "entry_trade_date",
    "main_holding_period_days",
    "diagnostic_holding_period_days",
    "ts_code",
    "rank",
    "industry",
    "weight",
    "round_trip_cost",
    "stress_round_trip_cost",
    "benchmark",
    "used_proxy",
    "completeness_status",
    "settlement_status",
    "data_missing_reason",
    "publish_mode",
    "rank_change",
    "is_post_freeze_sample",
    "exit_1d_trade_date",
    "exit_3d_trade_date",
    "exit_5d_trade_date",
    "entry_open_qfq",
    "return_1d_net",
    "return_3d_net",
    "return_5d_net",
    "return_5d_stress",
    "benchmark_return_5d",
    "excess_return_5d",
]

IMMUTABLE_LEDGER_FIELDS = [
    "strategy_id",
    "strategy_version",
    "signal_date",
    "signal_data_cutoff",
    "planned_entry_time",
    "entry_trade_date",
    "main_holding_period_days",
    "diagnostic_holding_period_days",
    "ts_code",
    "rank",
    "industry",
    "weight",
    "round_trip_cost",
    "stress_round_trip_cost",
    "benchmark",
    "used_proxy",
    "publish_mode",
    "rank_change",
    "is_post_freeze_sample",
]

PORTFOLIO_DAILY_COLUMNS = [
    "trade_date",
    "strategy_id",
    "strategy_version",
    "source_ledger_hash",
    "strategy_return",
    "benchmark_return",
    "strategy_nav",
    "benchmark_nav",
    "active_cohort_count",
    "active_position_count",
    "cash_weight",
    "used_proxy",
    "completeness_status",
]

PROMOTION_LEDGER_HASH_FIELDS = [
    "record_id",
    "strategy_id",
    "strategy_version",
    "signal_date",
    "entry_trade_date",
    "exit_5d_trade_date",
    "ts_code",
    "industry",
    "rank",
    "weight",
    "round_trip_cost",
    "settlement_status",
    "is_post_freeze_sample",
    "return_5d_net",
    "benchmark_return_5d",
]


def _json_scalar(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    return str(value)


def promotion_ledger_hash(ledger: pd.DataFrame) -> str:
    """Bind daily portfolio evidence to the exact immutable signal ledger."""
    if not isinstance(ledger, pd.DataFrame):
        raise EvaluationContractError("promotion ledger must be a DataFrame")
    work = ledger.copy()
    for column in PROMOTION_LEDGER_HASH_FIELDS:
        if column not in work.columns:
            work[column] = None
    sort_columns = [
        column
        for column in ("signal_date", "rank", "ts_code", "record_id")
        if column in work.columns
    ]
    if sort_columns:
        work = work.sort_values(sort_columns, kind="mergesort", na_position="last")
    rows = [
        {column: _json_scalar(row.get(column)) for column in PROMOTION_LEDGER_HASH_FIELDS}
        for _, row in work.iterrows()
    ]
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _date8(value: Any, *, field: str) -> str:
    normalized = str(value or "").replace("-", "")[:8]
    if len(normalized) != 8 or not normalized.isdigit():
        raise EvaluationContractError(f"{field} must be YYYYMMDD")
    return normalized


def _finite_float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationContractError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise EvaluationContractError(f"{field} must be finite")
    return number


def _contract_timestamp(value: Any, *, field: str) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationContractError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
        raise EvaluationContractError(f"{field} must use the Asia/Shanghai +08:00 offset")
    return parsed


def _planned_entry_date(value: Any) -> str:
    parsed = _contract_timestamp(value, field="planned_entry_time")
    if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) != (9, 30, 0, 0):
        raise EvaluationContractError("planned_entry_time must be the next open at 09:30:00+08:00")
    return parsed.strftime("%Y%m%d")


def _record_id(strategy_id: str, strategy_version: str, signal_date: str, ts_code: str) -> str:
    raw = "|".join((strategy_id, strategy_version, signal_date, ts_code))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _validate_snapshot(snapshot: dict[str, Any], *, open_trade_dates: Iterable[str]) -> None:
    required = {
        "strategy_id",
        "strategy_version",
        "signal_date",
        "signal_data_cutoff",
        "planned_entry_time",
        "holding_period_days",
        "used_proxy",
        "completeness_status",
        "benchmark",
        "rank_change",
        "publish_mode",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        raise EvaluationContractError(f"snapshot missing fields: {missing}")
    if bool(snapshot.get("used_proxy")):
        raise EvaluationContractError("proxy data is forbidden in shadow evaluation")
    if str(snapshot.get("completeness_status")) != "complete":
        raise EvaluationContractError("incomplete candidate snapshots cannot enter the ledger")
    if int(snapshot.get("rank_change") or 0) != 0:
        raise EvaluationContractError("AI or downstream rank changes are forbidden")
    if str(snapshot.get("publish_mode")) != "observe_only":
        raise EvaluationContractError("short-track candidates must remain observe_only")
    if int(snapshot.get("holding_period_days") or 0) != PRIMARY_HOLDING_PERIOD_DAYS:
        raise EvaluationContractError("primary holding period must be 5 trading days")
    diagnostics = tuple(int(x) for x in snapshot.get("diagnostic_holding_period_days") or ())
    if diagnostics != DIAGNOSTIC_HOLDING_PERIOD_DAYS:
        raise EvaluationContractError("diagnostic holding periods must be [1, 3]")
    if str(snapshot.get("benchmark")) != BENCHMARK_ID:
        raise EvaluationContractError("benchmark must be all-A tradable equal weight")
    if abs(_finite_float(snapshot.get("round_trip_cost"), field="round_trip_cost") - BASE_ROUND_TRIP_COST) > 1e-12:
        raise EvaluationContractError("base round-trip cost must be 0.30%")
    if abs(_finite_float(snapshot.get("stress_round_trip_cost"), field="stress_round_trip_cost") - STRESS_ROUND_TRIP_COST) > 1e-12:
        raise EvaluationContractError("stress round-trip cost must be 0.50%")
    signal_date = _date8(snapshot.get("signal_date"), field="signal_date")
    cutoff = _contract_timestamp(snapshot.get("signal_data_cutoff"), field="signal_data_cutoff")
    if cutoff.strftime("%Y%m%d") != signal_date or (cutoff.hour, cutoff.minute, cutoff.second) != (15, 0, 0):
        raise EvaluationContractError("signal_data_cutoff must be T day 15:00:00+08:00")
    planned_date = _planned_entry_date(snapshot.get("planned_entry_time"))
    calendar = sorted({_date8(value, field="open_trade_date") for value in open_trade_dates})
    if signal_date not in calendar:
        raise EvaluationContractError("signal_date is absent from the persisted exchange calendar")
    future_dates = [date for date in calendar if date > signal_date]
    if not future_dates:
        raise EvaluationContractError("exchange calendar does not contain the next open day")
    if planned_date != future_dates[0]:
        raise EvaluationContractError("planned_entry_time is not the next exchange open after signal_date")
    if not isinstance(snapshot.get("candidates"), list):
        raise EvaluationContractError("candidates must be a list")
    candidates = snapshot["candidates"]
    if not candidates:
        raise EvaluationContractError("candidate snapshot cannot enter the ledger empty")
    weights = [_finite_float(item.get("weight"), field="candidate weight") for item in candidates]
    if any(weight <= 0 for weight in weights):
        raise EvaluationContractError("candidate weights must be strictly positive")
    if not math.isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise EvaluationContractError("candidate weights must sum to 1.0")


def pending_rows_from_snapshot(
    snapshot: dict[str, Any],
    *,
    existing: pd.DataFrame | None = None,
    open_trade_dates: Iterable[str],
) -> pd.DataFrame:
    """Add a candidate snapshot to an append-only logical ledger.

    Re-reading the same immutable strategy/date/security tuple is idempotent.
    """
    calendar = list(open_trade_dates)
    _validate_snapshot(snapshot, open_trade_dates=calendar)
    signal_date = _date8(snapshot["signal_date"], field="signal_date")
    entry_date = _planned_entry_date(snapshot["planned_entry_time"])
    strategy_id = str(snapshot["strategy_id"])
    strategy_version = str(snapshot["strategy_version"])
    base_cost = float(snapshot["round_trip_cost"])
    stress_cost = float(snapshot["stress_round_trip_cost"])
    new_rows: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for item in snapshot["candidates"]:
        ts_code = str(item.get("ts_code") or "").strip()
        if not ts_code or ts_code in seen_codes:
            raise EvaluationContractError("candidate ts_code must be present and unique")
        seen_codes.add(ts_code)
        row = {
            "record_id": _record_id(strategy_id, strategy_version, signal_date, ts_code),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "signal_date": signal_date,
            "signal_data_cutoff": str(snapshot["signal_data_cutoff"]),
            "planned_entry_time": str(snapshot["planned_entry_time"]),
            "entry_trade_date": entry_date,
            "main_holding_period_days": PRIMARY_HOLDING_PERIOD_DAYS,
            "diagnostic_holding_period_days": json.dumps(DIAGNOSTIC_HOLDING_PERIOD_DAYS),
            "ts_code": ts_code,
            "rank": int(item.get("rank") or len(new_rows) + 1),
            "industry": str(item.get("industry") or "UNKNOWN"),
            "weight": _finite_float(item.get("weight", 0.0), field="candidate weight"),
            "round_trip_cost": base_cost,
            "stress_round_trip_cost": stress_cost,
            "benchmark": BENCHMARK_ID,
            "used_proxy": False,
            "completeness_status": "complete",
            "settlement_status": "pending_settlement",
            "data_missing_reason": None,
            "publish_mode": "observe_only",
            "rank_change": 0,
            # Per-strategy epoch (v45 uses own first success day); default remains global freeze.
            "is_post_freeze_sample": signal_date
            >= str(snapshot.get("validation_start_date") or VALIDATION_START_DATE),
            "exit_1d_trade_date": None,
            "exit_3d_trade_date": None,
            "exit_5d_trade_date": None,
            "entry_open_qfq": np.nan,
            "return_1d_net": np.nan,
            "return_3d_net": np.nan,
            "return_5d_net": np.nan,
            "return_5d_stress": np.nan,
            "benchmark_return_5d": np.nan,
            "excess_return_5d": np.nan,
        }
        new_rows.append(row)
    current = existing.copy() if existing is not None else pd.DataFrame(columns=LEDGER_COLUMNS)
    if current.empty:
        return pd.DataFrame(new_rows, columns=LEDGER_COLUMNS)
    if "record_id" not in current.columns:
        raise EvaluationContractError("existing ledger has no record_id")
    if current["record_id"].astype(str).duplicated().any():
        raise EvaluationContractError("existing ledger contains duplicate record_id values")
    existing_ids = set(current["record_id"].astype(str))
    existing_by_id = current.set_index(current["record_id"].astype(str), drop=False)
    for row in new_rows:
        if row["record_id"] not in existing_ids:
            continue
        prior = existing_by_id.loc[row["record_id"]]
        for field in IMMUTABLE_LEDGER_FIELDS:
            old_value = prior.get(field)
            new_value = row.get(field)
            try:
                both_missing = bool(pd.isna(old_value)) and bool(pd.isna(new_value))
            except (TypeError, ValueError):
                both_missing = False
            if both_missing:
                continue
            if isinstance(old_value, (float, np.floating)) or isinstance(new_value, (float, np.floating)):
                try:
                    if math.isclose(float(old_value), float(new_value), rel_tol=0.0, abs_tol=1e-12):
                        continue
                except (TypeError, ValueError):
                    pass
            elif old_value == new_value:
                continue
            raise EvaluationContractError(
                f"immutable ledger replay conflict for {row['record_id']} field {field}: "
                f"{old_value!r} != {new_value!r}"
            )
    additions = [row for row in new_rows if row["record_id"] not in existing_ids]
    if not additions:
        return current.reset_index(drop=True)
    return pd.concat([current, pd.DataFrame(additions, columns=LEDGER_COLUMNS)], ignore_index=True)


def _normalized_prices(prices: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "ts_code", "open_qfq", "close_qfq"}
    missing = sorted(required - set(prices.columns))
    if missing:
        raise EvaluationContractError(f"qfq price panel missing columns: {missing}")
    if "used_proxy" in prices.columns and prices["used_proxy"].fillna(True).astype(bool).any():
        raise EvaluationContractError("proxy qfq prices are forbidden")
    if "completeness" in prices.columns and not (
        prices["completeness"].astype(str) == "complete"
    ).all():
        raise EvaluationContractError("incomplete qfq prices are forbidden")
    out = prices[sorted(required)].copy()
    out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    out["ts_code"] = out["ts_code"].astype(str)
    out["open_qfq"] = pd.to_numeric(out["open_qfq"], errors="coerce")
    out["close_qfq"] = pd.to_numeric(out["close_qfq"], errors="coerce")
    if out.duplicated(["trade_date", "ts_code"]).any():
        raise EvaluationContractError("qfq price panel contains duplicate date/security rows")
    return out.set_index(["trade_date", "ts_code"]).sort_index()


def _normalized_universe(pit_universe: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "ts_code", "universe_flag", "tradable"}
    missing = sorted(required - set(pit_universe.columns))
    if missing:
        raise EvaluationContractError(f"PIT universe missing columns: {missing}")
    if "used_proxy" in pit_universe.columns and pit_universe["used_proxy"].fillna(True).astype(bool).any():
        raise EvaluationContractError("proxy PIT universe rows are forbidden")
    if "completeness" in pit_universe.columns and not (
        pit_universe["completeness"].astype(str) == "complete"
    ).all():
        raise EvaluationContractError("incomplete PIT universe rows are forbidden")
    columns = sorted(required | ({"is_suspended"} & set(pit_universe.columns)))
    out = pit_universe[columns].copy()
    out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    out["ts_code"] = out["ts_code"].astype(str)
    if out.duplicated(["trade_date", "ts_code"]).any():
        raise EvaluationContractError("PIT universe contains duplicate date/security rows")
    return out


def _calendar_targets(signal_date: str, open_trade_dates: Iterable[str]) -> tuple[str, dict[int, str]] | None:
    calendar = sorted({_date8(value, field="open_trade_date") for value in open_trade_dates})
    following = [date for date in calendar if date > signal_date]
    if not following:
        return None
    entry = following[0]
    entry_pos = calendar.index(entry)
    targets: dict[int, str] = {}
    for horizon in (1, 3, 5):
        pos = entry_pos + horizon - 1
        if pos >= len(calendar):
            return entry, targets
        targets[horizon] = calendar[pos]
    return entry, targets


def _price_value(price_index: pd.DataFrame, date: str, code: str, column: str) -> float | None:
    try:
        value = float(price_index.loc[(date, code), column])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _benchmark_return(
    price_index: pd.DataFrame,
    universe: pd.DataFrame,
    entry_date: str,
    exit_date: str,
) -> float | None:
    eligible = universe[
        (universe["trade_date"] == entry_date)
        & (pd.to_numeric(universe["universe_flag"], errors="coerce") > 0)
        & (pd.to_numeric(universe["tradable"], errors="coerce") > 0)
    ]["ts_code"].astype(str)
    codes = sorted(set(eligible))
    if not codes:
        return None
    returns: list[float] = []
    for code in codes:
        entry = _price_value(price_index, entry_date, code, "open_qfq")
        exit_price = _price_value(price_index, exit_date, code, "close_qfq")
        if entry is None or exit_price is None:
            return None
        returns.append(exit_price / entry - 1.0)
    return float(np.mean(returns))


def settle_ledger(
    ledger: pd.DataFrame,
    *,
    prices: pd.DataFrame,
    pit_universe: pd.DataFrame,
    open_trade_dates: Iterable[str],
    as_of_date: str,
) -> pd.DataFrame:
    """Settle matured rows, preserving nulls for pending or missing data."""
    result = ledger.copy()
    if result.empty:
        return result
    price_index = _normalized_prices(prices)
    universe = _normalized_universe(pit_universe)
    as_of = _date8(as_of_date, field="as_of_date")
    for index, row in result.iterrows():
        if str(row.get("settlement_status")) == "settled":
            continue
        signal_date = _date8(row.get("signal_date"), field="signal_date")
        calendar_target = _calendar_targets(signal_date, open_trade_dates)
        if calendar_target is None:
            continue
        entry_date, targets = calendar_target
        result.at[index, "entry_trade_date"] = entry_date
        for horizon, date in targets.items():
            result.at[index, f"exit_{horizon}d_trade_date"] = date
        main_exit = targets.get(5)
        if main_exit is None or as_of < main_exit:
            result.at[index, "settlement_status"] = "pending_settlement"
            continue
        code = str(row.get("ts_code"))
        entry = _price_value(price_index, entry_date, code, "open_qfq")
        exits = {h: _price_value(price_index, date, code, "close_qfq") for h, date in targets.items()}
        benchmark = _benchmark_return(price_index, universe, entry_date, main_exit)
        if entry is None or any(exits.get(h) is None for h in (1, 3, 5)) or benchmark is None:
            result.at[index, "settlement_status"] = "data_missing"
            result.at[index, "completeness_status"] = "data_missing"
            result.at[index, "data_missing_reason"] = "qfq_price_or_pit_benchmark_missing"
            for field in ("return_1d_net", "return_3d_net", "return_5d_net", "return_5d_stress", "benchmark_return_5d", "excess_return_5d"):
                result.at[index, field] = np.nan
            continue
        base_cost = float(row.get("round_trip_cost", BASE_ROUND_TRIP_COST))
        stress_cost = float(row.get("stress_round_trip_cost", STRESS_ROUND_TRIP_COST))
        gross = {h: float(exits[h] / entry - 1.0) for h in (1, 3, 5)}
        result.at[index, "entry_open_qfq"] = entry
        result.at[index, "return_1d_net"] = gross[1] - base_cost
        result.at[index, "return_3d_net"] = gross[3] - base_cost
        result.at[index, "return_5d_net"] = gross[5] - base_cost
        result.at[index, "return_5d_stress"] = gross[5] - stress_cost
        result.at[index, "benchmark_return_5d"] = benchmark
        result.at[index, "excess_return_5d"] = gross[5] - base_cost - benchmark
        result.at[index, "settlement_status"] = "settled"
        result.at[index, "completeness_status"] = "complete"
        result.at[index, "data_missing_reason"] = None
    return result


def _eligible_pit_codes(universe: pd.DataFrame, membership_date: str) -> list[str]:
    if universe is None or universe.empty or "ts_code" not in universe.columns:
        return []
    eligible = universe[
        (universe["trade_date"].astype(str) == str(membership_date))
        & (pd.to_numeric(universe["universe_flag"], errors="coerce") > 0)
        & (pd.to_numeric(universe["tradable"], errors="coerce") > 0)
    ]
    if eligible.empty:
        return []
    return sorted(set(eligible["ts_code"].astype(str)))


def _resolve_benchmark_membership_date(universe: pd.DataFrame, membership_date: str) -> str:
    if _eligible_pit_codes(universe, membership_date):
        return str(membership_date)
    dates = sorted(
        {str(value) for value in universe.get("trade_date", pd.Series(dtype=str)).astype(str).unique()}
    )
    fallback = [
        date for date in dates if date <= str(membership_date) and _eligible_pit_codes(universe, date)
    ]
    if not fallback:
        raise EvaluationContractError(
            f"all-A benchmark has no eligible PIT constituents on {membership_date}"
        )
    return fallback[-1]


def _daily_equal_weight_benchmark_return(
    price_index: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    trade_date: str,
    previous_trade_date: str | None,
) -> float:
    membership_date = previous_trade_date or trade_date
    membership_date = _resolve_benchmark_membership_date(universe, membership_date)
    codes = _eligible_pit_codes(universe, membership_date)
    if not codes:
        raise EvaluationContractError(
            f"all-A benchmark has no eligible PIT constituents on {membership_date}"
        )
    current = universe[universe["trade_date"] == trade_date]
    current_by_code = current.set_index("ts_code", drop=False)
    returns: list[float] = []
    for code in codes:
        if previous_trade_date is None:
            start_price = _price_value(price_index, trade_date, code, "open_qfq")
        else:
            start_price = _price_value(
                price_index, previous_trade_date, code, "close_qfq"
            )
        end_price = _price_value(price_index, trade_date, code, "close_qfq")
        if end_price is None and previous_trade_date is not None and code in current_by_code.index:
            current_row = current_by_code.loc[code]
            if isinstance(current_row, pd.DataFrame):
                raise EvaluationContractError(
                    f"duplicate PIT universe rows prevent benchmark valuation for {code} on {trade_date}"
                )
            if bool(current_row.get("is_suspended", False)):
                end_price = start_price
        if end_price is None and previous_trade_date is not None and code not in current_by_code.index:
            end_price = start_price
        if start_price is None or end_price is None:
            continue
        returns.append(float(end_price / start_price - 1.0))
    if not returns:
        raise EvaluationContractError(
            f"all-A benchmark has no priced constituents on {trade_date}"
        )
    if len(returns) / len(codes) < 0.90:
        raise EvaluationContractError(
            f"all-A benchmark coverage too low on {trade_date}: {len(returns)}/{len(codes)}"
        )
    return float(np.mean(returns))


def build_staggered_portfolio_daily_evidence(
    ledger: pd.DataFrame,
    *,
    prices: pd.DataFrame,
    pit_universe: pd.DataFrame,
    open_trade_dates: Iterable[str],
    as_of_date: str,
) -> pd.DataFrame:
    """Mark the real five-sleeve, overlapping 5-day short book to market daily."""
    required = {
        "strategy_id",
        "strategy_version",
        "signal_date",
        "ts_code",
        "industry",
        "rank",
        "weight",
        "round_trip_cost",
        "is_post_freeze_sample",
    }
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise EvaluationContractError(f"ledger missing portfolio fields: {missing}")
    if ledger.empty:
        return pd.DataFrame(columns=PORTFOLIO_DAILY_COLUMNS)
    work = ledger.copy()
    work["signal_date"] = work["signal_date"].map(
        lambda value: _date8(value, field="signal_date")
    )
    # is_post_freeze_sample already encodes the strategy epoch (global or per-strategy).
    work = work[work["is_post_freeze_sample"].fillna(False).astype(bool)].copy()
    if work.empty:
        return pd.DataFrame(columns=PORTFOLIO_DAILY_COLUMNS)
    strategy_ids = sorted(set(work["strategy_id"].astype(str)))
    strategy_versions = sorted(set(work["strategy_version"].astype(str)))
    if len(strategy_ids) != 1 or len(strategy_versions) != 1:
        raise EvaluationContractError(
            "portfolio evidence must contain one immutable strategy version"
        )
    if work.duplicated(["signal_date", "ts_code"]).any():
        raise EvaluationContractError("portfolio ledger contains duplicate date/security rows")
    weights = pd.to_numeric(work["weight"], errors="coerce")
    if weights.isna().any() or (weights <= 0.0).any():
        raise EvaluationContractError("portfolio ledger contains invalid candidate weights")
    work["weight"] = weights
    weight_sums = work.groupby("signal_date", sort=True)["weight"].sum()
    bad_weight_dates = weight_sums[
        ~np.isclose(weight_sums.to_numpy(dtype=float), 1.0, rtol=0.0, atol=1e-8)
    ]
    if len(bad_weight_dates):
        raise EvaluationContractError(
            f"portfolio weights do not sum to 1.0 on dates: {list(bad_weight_dates.index[:5])}"
        )
    if (work.groupby("signal_date").size() > 20).any():
        raise EvaluationContractError("short-track cohort exceeds 20 positions")

    calendar = sorted({_date8(value, field="open_trade_date") for value in open_trade_dates})
    as_of = _date8(as_of_date, field="as_of_date")
    prepared: list[dict[str, Any]] = []
    for row_index, row in work.iterrows():
        signal_date = str(row["signal_date"])
        targets = _calendar_targets(signal_date, calendar)
        if targets is None:
            continue
        entry_date, exits = targets
        exit_date = exits.get(PRIMARY_HOLDING_PERIOD_DAYS)
        if exit_date is None:
            continue
        stored_entry = str(row.get("entry_trade_date") or "").replace("-", "")[:8]
        stored_exit = str(row.get("exit_5d_trade_date") or "").replace("-", "")[:8]
        if stored_entry and stored_entry != entry_date:
            raise EvaluationContractError("ledger entry date conflicts with the exchange calendar")
        if stored_exit and stored_exit != exit_date:
            raise EvaluationContractError("ledger 5-day exit conflicts with the exchange calendar")
        cost = _finite_float(row.get("round_trip_cost"), field="round_trip_cost")
        if abs(cost - BASE_ROUND_TRIP_COST) > 1e-12:
            raise EvaluationContractError("portfolio evidence must use the 0.30% base cost")
        record_id = str(
            row.get("record_id")
            or f"{signal_date}|{row.get('ts_code')}|{row_index}"
        )
        prepared.append(
            {
                "position_id": record_id,
                "cohort_id": signal_date,
                "ts_code": str(row.get("ts_code") or ""),
                "weight": float(row["weight"]),
                "round_trip_cost": cost,
                "entry_date": entry_date,
                "exit_date": exit_date,
            }
        )
    if not prepared:
        return pd.DataFrame(columns=PORTFOLIO_DAILY_COLUMNS)
    ids = [row["position_id"] for row in prepared]
    if len(ids) != len(set(ids)):
        raise EvaluationContractError("portfolio evidence contains duplicate position ids")
    first_entry = min(row["entry_date"] for row in prepared)
    last_exit = min(as_of, max(row["exit_date"] for row in prepared))
    trade_dates = [date for date in calendar if first_entry <= date <= last_exit]
    if not trade_dates:
        return pd.DataFrame(columns=PORTFOLIO_DAILY_COLUMNS)

    price_index = _normalized_prices(prices)
    universe = _normalized_universe(pit_universe)
    entries: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in prepared:
        if row["entry_date"] <= as_of:
            entries.setdefault(row["entry_date"], {}).setdefault(
                row["cohort_id"], []
            ).append(row)

    active: dict[str, dict[str, Any]] = {}
    cash = 1.0
    prior_nav = 1.0
    benchmark_nav = 1.0
    previous_trade_date: str | None = None
    source_hash = promotion_ledger_hash(ledger)
    daily_rows: list[dict[str, Any]] = []
    universe_by_date_code = universe.set_index(["trade_date", "ts_code"], drop=False)
    for trade_date in trade_dates:
        for cohort_id, cohort_rows in sorted(entries.get(trade_date, {}).items()):
            if any(position["cohort_id"] == cohort_id for position in active.values()):
                raise EvaluationContractError("portfolio contains a duplicate active cohort")
            cohort_allocation = min(COHORT_CAPITAL_FRACTION * prior_nav, cash)
            if cohort_allocation <= 0.0:
                raise EvaluationContractError(
                    f"staggered portfolio has no cash for cohort {cohort_id}"
                )
            for row in sorted(cohort_rows, key=lambda item: (item["ts_code"], item["position_id"])):
                code = row["ts_code"]
                entry_open = _price_value(price_index, trade_date, code, "open_qfq")
                if not code or entry_open is None:
                    raise EvaluationContractError(
                        f"entry qfq open is missing for {code} on {trade_date}"
                    )
                allocation = cohort_allocation * row["weight"]
                active[row["position_id"]] = {
                    **row,
                    "allocation": allocation,
                    "units": allocation / entry_open,
                    "last_close": entry_open,
                }
                cash -= allocation
            if cash < -1e-10:
                raise EvaluationContractError("staggered portfolio cash became negative")
            cash = max(0.0, cash)

        close_values: dict[str, float] = {}
        for position_id, position in active.items():
            close_price = _price_value(
                price_index, trade_date, position["ts_code"], "close_qfq"
            )
            if close_price is None:
                suspended = False
                key = (trade_date, position["ts_code"])
                if key in universe_by_date_code.index:
                    row = universe_by_date_code.loc[key]
                    if isinstance(row, pd.DataFrame):
                        raise EvaluationContractError(
                            "duplicate PIT universe rows prevent suspended-position valuation"
                        )
                    suspended = bool(row.get("is_suspended", False))
                if suspended:
                    close_price = float(position["last_close"])
                else:
                    raise EvaluationContractError(
                        f"active position qfq close is missing for {position['ts_code']} on {trade_date}"
                    )
            position["last_close"] = close_price
            close_values[position_id] = float(position["units"] * close_price)

        exiting_ids = sorted(
            position_id
            for position_id, position in active.items()
            if position["exit_date"] == trade_date
        )
        for position_id in exiting_ids:
            position = active[position_id]
            cash += close_values[position_id] - (
                position["allocation"] * position["round_trip_cost"]
            )
            del active[position_id]
            del close_values[position_id]
        nav = float(cash + sum(close_values.values()))
        if not math.isfinite(nav) or nav <= 0.0:
            raise EvaluationContractError("staggered portfolio NAV became non-positive or invalid")
        strategy_return = float(nav / prior_nav - 1.0)
        benchmark_return = _daily_equal_weight_benchmark_return(
            price_index,
            universe,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
        )
        benchmark_nav *= 1.0 + benchmark_return
        daily_rows.append(
            {
                "trade_date": trade_date,
                "strategy_id": strategy_ids[0],
                "strategy_version": strategy_versions[0],
                "source_ledger_hash": source_hash,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "strategy_nav": nav,
                "benchmark_nav": float(benchmark_nav),
                "active_cohort_count": int(
                    len({position["cohort_id"] for position in active.values()})
                ),
                "active_position_count": int(len(active)),
                "cash_weight": float(cash / nav),
                "used_proxy": False,
                "completeness_status": "complete",
            }
        )
        prior_nav = nav
        previous_trade_date = trade_date
    return pd.DataFrame(daily_rows, columns=PORTFOLIO_DAILY_COLUMNS)


def _compound(values: Iterable[float]) -> float:
    nav = 1.0
    for value in values:
        nav *= 1.0 + float(value)
    return nav - 1.0


def _max_drawdown(values: Iterable[float]) -> float:
    nav = 1.0
    peak = 1.0
    worst = 0.0
    for value in values:
        nav *= 1.0 + float(value)
        peak = max(peak, nav)
        worst = max(worst, 1.0 - nav / peak)
    return float(worst)


def _risk_adjusted(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    mean = float(np.mean(values))
    if values.size < 2:
        return 0.0
    std = float(np.std(values, ddof=1))
    if std <= 1e-15:
        return 999.0 if mean > 0 else (-999.0 if mean < 0 else 0.0)
    return float(mean / std * math.sqrt(252.0))


def _contribution_shares(rows: pd.DataFrame) -> tuple[float, float]:
    work = rows.copy()
    work["abs_contribution"] = (
        pd.to_numeric(work["return_5d_net"], errors="coerce")
        * pd.to_numeric(work["weight"], errors="coerce")
    ).abs()
    total = float(work["abs_contribution"].sum())
    if total <= 1e-15:
        return 1.0, 1.0
    industry = work.groupby("industry", dropna=False)["abs_contribution"].sum()
    stock = work.groupby("ts_code", dropna=False)["abs_contribution"].sum().sort_values(ascending=False)
    return float(industry.max() / total), float(stock.head(5).sum() / total)


def _validated_portfolio_daily(
    ledger: pd.DataFrame,
    portfolio_daily: pd.DataFrame | None,
    *,
    strategy_id: str | None,
    strategy_version: str | None,
) -> tuple[pd.DataFrame, bool]:
    if portfolio_daily is None or portfolio_daily.empty:
        return pd.DataFrame(columns=[*PORTFOLIO_DAILY_COLUMNS, "excess_return"]), False
    required = set(PORTFOLIO_DAILY_COLUMNS)
    missing = sorted(required - set(portfolio_daily.columns))
    if missing:
        raise EvaluationContractError(f"portfolio daily evidence missing fields: {missing}")
    daily = portfolio_daily.copy()
    daily["trade_date"] = daily["trade_date"].map(
        lambda value: _date8(value, field="portfolio trade_date")
    )
    if daily["trade_date"].duplicated().any():
        raise EvaluationContractError("portfolio daily evidence has duplicate trade dates")
    daily = daily.sort_values("trade_date").reset_index(drop=True)
    if bool(daily["used_proxy"].fillna(True).astype(bool).any()):
        raise EvaluationContractError("portfolio daily evidence used proxy data")
    if not (daily["completeness_status"].astype(str) == "complete").all():
        raise EvaluationContractError("portfolio daily evidence is incomplete")
    ids = sorted(set(daily["strategy_id"].astype(str)))
    versions = sorted(set(daily["strategy_version"].astype(str)))
    if len(ids) != 1 or len(versions) != 1:
        raise EvaluationContractError("portfolio daily evidence mixes strategy identities")
    if strategy_id is not None and ids != [strategy_id]:
        raise EvaluationContractError("portfolio daily strategy_id does not match the ledger")
    if strategy_version is not None and versions != [strategy_version]:
        raise EvaluationContractError("portfolio daily strategy_version does not match the ledger")
    hashes = sorted(set(daily["source_ledger_hash"].astype(str)))
    if hashes != [promotion_ledger_hash(ledger)]:
        raise EvaluationContractError("portfolio daily evidence does not match the source ledger")
    numeric_columns = [
        "strategy_return",
        "benchmark_return",
        "strategy_nav",
        "benchmark_nav",
        "active_cohort_count",
        "active_position_count",
        "cash_weight",
    ]
    for column in numeric_columns:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
    if daily[numeric_columns].replace([np.inf, -np.inf], np.nan).isna().any().any():
        raise EvaluationContractError("portfolio daily evidence contains invalid numeric values")
    if (daily[["strategy_nav", "benchmark_nav"]] <= 0.0).any().any():
        raise EvaluationContractError("portfolio daily NAV must remain positive")
    if (daily["active_cohort_count"] < 0).any() or (
        daily["active_cohort_count"] > PRIMARY_HOLDING_PERIOD_DAYS
    ).any():
        raise EvaluationContractError("portfolio daily cohort count exceeds five sleeves")
    if (daily["active_position_count"] < 0).any() or (
        daily["active_position_count"] > 20 * PRIMARY_HOLDING_PERIOD_DAYS
    ).any():
        raise EvaluationContractError("portfolio daily position count exceeds five Top20 sleeves")
    if (daily["cash_weight"] < -1e-10).any() or (daily["cash_weight"] > 1.0 + 1e-10).any():
        raise EvaluationContractError("portfolio daily cash weight is outside [0, 1]")
    prior_strategy_nav = 1.0
    prior_benchmark_nav = 1.0
    for _, row in daily.iterrows():
        expected_strategy_nav = prior_strategy_nav * (1.0 + float(row["strategy_return"]))
        expected_benchmark_nav = prior_benchmark_nav * (1.0 + float(row["benchmark_return"]))
        if not math.isclose(
            float(row["strategy_nav"]), expected_strategy_nav, rel_tol=0.0, abs_tol=1e-10
        ):
            raise EvaluationContractError("strategy NAV does not reconcile to daily returns")
        if not math.isclose(
            float(row["benchmark_nav"]), expected_benchmark_nav, rel_tol=0.0, abs_tol=1e-10
        ):
            raise EvaluationContractError("benchmark NAV does not reconcile to daily returns")
        prior_strategy_nav = float(row["strategy_nav"])
        prior_benchmark_nav = float(row["benchmark_nav"])
    daily["excess_return"] = daily["strategy_return"] - daily["benchmark_return"]
    return daily, True


def evaluate_short_track_promotion(
    ledger: pd.DataFrame,
    *,
    expected_signal_dates: Iterable[str],
    validation_through_date: str | None = None,
    portfolio_daily: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Evaluate frozen forward samples; a pass still carries no trading authority."""
    required = {
        "strategy_id",
        "strategy_version",
        "signal_date",
        "ts_code",
        "industry",
        "settlement_status",
        "is_post_freeze_sample",
        "return_5d_net",
        "benchmark_return_5d",
        "weight",
    }
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise EvaluationContractError(f"ledger missing promotion fields: {missing}")
    # Prefer strategy-specific epoch from snapshot/ledger flags when present.
    epoch = VALIDATION_START_DATE
    if "is_post_freeze_sample" in ledger.columns and not ledger.empty:
        flagged = ledger[ledger["is_post_freeze_sample"].fillna(False).astype(bool)]
        if not flagged.empty:
            epoch = str(flagged["signal_date"].astype(str).min())
            # Floor at global dual-track freeze for pre-existing strategies; v45 first
            # success is never earlier than VALIDATION_START_DATE in production.
            if epoch < VALIDATION_START_DATE:
                epoch = VALIDATION_START_DATE
    expected = sorted(
        {
            _date8(value, field="expected_signal_date")
            for value in expected_signal_dates
            if _date8(value, field="expected_signal_date") >= epoch
        }
    )
    if not expected:
        raise EvaluationContractError("expected_signal_dates contains no post-freeze trade dates")
    through = _date8(validation_through_date, field="validation_through_date") if validation_through_date else expected[-1]
    expected = [date for date in expected if date <= through]
    if not expected:
        raise EvaluationContractError("validation_through_date precedes the post-freeze validation window")
    post_freeze = ledger[
        ledger["is_post_freeze_sample"].fillna(False).astype(bool)
        & (ledger["signal_date"].astype(str) <= through)
    ].copy()
    target = post_freeze[post_freeze["signal_date"].astype(str).isin(expected)].copy()
    target_strategy_ids = sorted(set(target["strategy_id"].astype(str)))
    target_versions = sorted(set(target["strategy_version"].astype(str)))
    if len(target_strategy_ids) > 1 or len(target_versions) > 1:
        raise EvaluationContractError("promotion must be evaluated one immutable strategy version at a time")
    actual_dates = set(target["signal_date"].astype(str))
    missing_dates = sorted(set(expected) - actual_dates)
    incomplete_dates = set(missing_dates)
    if not target.empty:
        for signal_date, day_rows in target.groupby("signal_date", sort=True):
            if not (day_rows["settlement_status"].astype(str) == "settled").all():
                incomplete_dates.add(str(signal_date))
    complete_dates = sorted(set(expected) - incomplete_dates)
    eligible = target[
        (target["settlement_status"].astype(str) == "settled")
        & target["signal_date"].astype(str).isin(complete_dates)
    ].copy()
    eligible["return_5d_net"] = pd.to_numeric(eligible["return_5d_net"], errors="coerce")
    eligible["benchmark_return_5d"] = pd.to_numeric(eligible["benchmark_return_5d"], errors="coerce")
    eligible["weight"] = pd.to_numeric(eligible["weight"], errors="coerce")
    eligible = eligible.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["return_5d_net", "benchmark_return_5d", "weight"]
    )
    if (eligible["weight"] <= 0).any():
        raise EvaluationContractError("promotion ledger contains non-positive portfolio weights")
    strategy_ids = target_strategy_ids
    versions = target_versions
    if not eligible.empty:
        weight_sums = eligible.groupby("signal_date", sort=True)["weight"].sum()
        bad_weight_dates = weight_sums[~np.isclose(weight_sums.to_numpy(dtype=float), 1.0, rtol=0.0, atol=1e-8)]
        if len(bad_weight_dates):
            raise EvaluationContractError(
                f"portfolio weights do not sum to 1.0 on dates: {list(bad_weight_dates.index[:5])}"
            )
    daily, portfolio_daily_complete = _validated_portfolio_daily(
        ledger,
        portfolio_daily,
        strategy_id=(strategy_ids[0] if strategy_ids else None),
        strategy_version=(versions[0] if versions else None),
    )
    sample_days = int(len(complete_dates))
    strategy_values = daily["strategy_return"].to_numpy(dtype=float)
    net_absolute = float(daily.iloc[-1]["strategy_nav"] - 1.0) if not daily.empty else 0.0
    net_excess = (
        float(daily.iloc[-1]["strategy_nav"] / daily.iloc[-1]["benchmark_nav"] - 1.0)
        if not daily.empty
        else 0.0
    )
    risk_adjusted = _risk_adjusted(strategy_values)
    max_drawdown = _max_drawdown(strategy_values)
    latest_sixty = daily.tail(60)
    windows = [latest_sixty.iloc[start : start + 20] for start in (0, 20, 40)] if len(latest_sixty) >= 60 else []
    window_returns = [_compound(window["strategy_return"].to_numpy(dtype=float)) for window in windows]
    positive_windows = sum(value > 0 for value in window_returns)
    industry_share, top5_share = _contribution_shares(eligible) if not eligible.empty else (1.0, 1.0)
    gates = {
        "staggered_portfolio_mark_to_market_complete": portfolio_daily_complete,
        "minimum_60_new_trade_days": sample_days >= MIN_NEW_TRADE_DAYS,
        "positive_net_absolute_return": net_absolute > 0,
        "positive_net_excess_return": net_excess > 0,
        "risk_adjusted_return_at_least_0_5": risk_adjusted >= MIN_RISK_ADJUSTED_RETURN,
        "maximum_drawdown_at_most_8pct": max_drawdown <= MAX_DRAWDOWN,
        "two_of_three_positive_20d_windows": positive_windows >= MIN_POSITIVE_20D_WINDOWS,
        "industry_concentration": industry_share <= MAX_INDUSTRY_CONTRIBUTION_SHARE,
        "stock_concentration": top5_share <= MAX_TOP5_STOCK_CONTRIBUTION_SHARE,
        "complete_expected_post_freeze_samples": not incomplete_dates,
    }
    failed = [name for name, passed in gates.items() if not passed]
    all_pass = not failed
    return {
        "strategy_id": strategy_ids[0] if strategy_ids else None,
        "strategy_version": versions[0] if versions else None,
        "validation_start_date": epoch,
        "sample_trade_days": sample_days,
        "portfolio_mark_to_market_days": int(len(daily)),
        "expected_trade_days": int(len(expected)),
        "validation_through_date": through,
        "missing_trade_days": int(len(missing_dates)),
        "incomplete_trade_days": int(len(incomplete_dates)),
        "incomplete_trade_date_sample": sorted(incomplete_dates)[:20],
        "settled_security_rows": int(len(eligible)),
        "portfolio_daily_source_ledger_hash": (
            promotion_ledger_hash(ledger) if portfolio_daily_complete else None
        ),
        "net_absolute_return": float(net_absolute),
        "net_excess_return": float(net_excess),
        "risk_adjusted_return": float(risk_adjusted),
        "maximum_drawdown": float(max_drawdown),
        "window_20d_returns": [float(value) for value in window_returns],
        "positive_20d_windows": int(positive_windows),
        "max_industry_contribution_share": float(industry_share),
        "top5_stock_contribution_share": float(top5_share),
        "gates": gates,
        "failed_gates": failed,
        "all_gates_pass": all_pass,
        "decision": "eligible_for_shadow_promotion" if all_pass else "observe_only",
        "execution_authority": "observe_only_no_auto_order",
    }


def build_tracking_report(
    *,
    strategy_id: str,
    strategy_version: str,
    operational_ok: bool,
    operational_evidence: dict[str, Any],
    promotion_verdict: dict[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    effective = bool(promotion_verdict.get("all_gates_pass"))
    failed_gates = promotion_verdict.get("failed_gates", [])
    if not operational_ok:
        op_status = "failed"
    elif failed_gates and not effective:
        op_status = "degraded_observation"
    else:
        op_status = "healthy"

    return {
        "artifact_kind": "candidate_tracking_report",
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "operational_status": op_status,
        "flow_status": "degraded" if op_status == "degraded_observation" else op_status,
        "operational_evidence": operational_evidence,
        "effectiveness_status": "promotion_gates_passed" if effective else "not_validated",
        "effectiveness_evidence": promotion_verdict,
        "execution_authority": "observe_only_no_auto_order",
    }


def tracking_report_path(output_dir: Path, strategy_id: str, signal_date: str) -> Path:
    date = _date8(signal_date, field="signal_date")
    return Path(output_dir) / f"{strategy_id}_{date}_candidate_tracking.json"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


__all__ = [
    "EvaluationContractError",
    "PORTFOLIO_DAILY_COLUMNS",
    "promotion_ledger_hash",
    "pending_rows_from_snapshot",
    "settle_ledger",
    "build_staggered_portfolio_daily_evidence",
    "evaluate_short_track_promotion",
    "build_tracking_report",
    "tracking_report_path",
    "write_json_atomic",
]
