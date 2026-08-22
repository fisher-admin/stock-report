#!/usr/bin/env python3
"""Evaluation and promotion gates for event_quality_drift_v1 snapshots."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd

import event_quality_drift_v1 as strategy


PROMOTION_START_DATE = "20250101"
FINAL_HISTORY_END_DATE = "20261231"
PRIMARY_HOLDING_PERIOD_DAYS = 20
AUXILIARY_HOLDING_PERIOD_DAYS = (40,)
BASE_ROUND_TRIP_COST = 0.003
STRESS_ROUND_TRIP_COST = 0.005
BENCHMARK_ID = "all_a_tradable_equal_weight"
MAX_DRAWDOWN = 0.12
MIN_VALID_ANNOUNCEMENT_EVENTS = 100
MIN_SAMPLE_MONTHS = 12
MAX_INDUSTRY_CONTRIBUTION_SHARE = 0.50
MAX_TOP5_STOCK_CONTRIBUTION_SHARE = 0.50
PERMUTATION_SEED = 20260811
PERMUTATION_TRIALS = 4096
# Independent 2026 completeness evidence (not derived from candidate signal rows).
COMPLETENESS_CONTEXT_KEYS = (
    "expected_2026_trade_dates",
    "announcement_coverage_ok_dates",
    "evaluation_as_of_date",
    "open_trade_dates",
)


class EvaluationContractError(ValueError):
    """Raised when a candidate snapshot or ledger violates the contract."""


LEDGER_COLUMNS = [
    "record_id",
    "strategy_id",
    "strategy_version",
    "signal_date",
    "signal_data_cutoff",
    "planned_entry_time",
    "entry_trade_date",
    "main_holding_period_days",
    "auxiliary_holding_period_days",
    "ts_code",
    "rank",
    "industry",
    "weight",
    "active_invested_weight",
    "new_invested_weight",
    "portfolio_invested_weight",
    "cash_weight",
    "is_selected",
    "quant_rank",
    "round_trip_cost",
    "stress_round_trip_cost",
    "benchmark",
    "data_sources",
    "used_proxy",
    "completeness_status",
    "revision_chain_complete",
    "evidence_scope",
    "promotion_evidence_eligible",
    "settlement_status",
    "data_missing_reason",
    "publish_mode",
    "rank_change",
    "execution_authority",
    "ai_evidence_time",
    "ai_risk_tags",
    "ai_explanation",
    "risk_gate_passed",
    "deterministic_risk_checks",
    "exit_20d_trade_date",
    "exit_40d_trade_date",
    "entry_open_qfq",
    "return_20d_net",
    "return_20d_stress",
    "return_40d_net",
    "return_40d_stress",
    "benchmark_return_20d",
    "benchmark_return_40d",
    "excess_return_20d",
    "excess_return_40d",
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
    "active_position_count",
    "cash_weight",
    "used_proxy",
    "completeness_status",
    "promotion_evidence_clean",
]

PROMOTION_LEDGER_HASH_FIELDS = [
    "record_id",
    "strategy_id",
    "strategy_version",
    "signal_date",
    "entry_trade_date",
    "exit_20d_trade_date",
    "ts_code",
    "industry",
    "weight",
    "active_invested_weight",
    "new_invested_weight",
    "portfolio_invested_weight",
    "cash_weight",
    "is_selected",
    "quant_rank",
    "round_trip_cost",
    "settlement_status",
    "revision_chain_complete",
    "promotion_evidence_eligible",
    "evidence_scope",
    "return_20d_net",
    "benchmark_return_20d",
]

IMMUTABLE_LEDGER_FIELDS = [
    "strategy_id",
    "strategy_version",
    "signal_date",
    "signal_data_cutoff",
    "planned_entry_time",
    "entry_trade_date",
    "main_holding_period_days",
    "auxiliary_holding_period_days",
    "ts_code",
    "rank",
    "industry",
    "weight",
    "active_invested_weight",
    "new_invested_weight",
    "portfolio_invested_weight",
    "cash_weight",
    "is_selected",
    "quant_rank",
    "round_trip_cost",
    "stress_round_trip_cost",
    "benchmark",
    "data_sources",
    "used_proxy",
    "revision_chain_complete",
    "evidence_scope",
    "promotion_evidence_eligible",
    "publish_mode",
    "rank_change",
    "execution_authority",
    "ai_evidence_time",
    "ai_risk_tags",
    "ai_explanation",
    "risk_gate_passed",
    "deterministic_risk_checks",
    "exit_20d_trade_date",
    "exit_40d_trade_date",
]


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number
    return str(value)


def promotion_ledger_hash(ledger: pd.DataFrame) -> str:
    """Bind portfolio mark-to-market evidence to the exact immutable ledger."""
    if not isinstance(ledger, pd.DataFrame):
        raise EvaluationContractError("promotion ledger must be a DataFrame")
    work = ledger.copy()
    for column in PROMOTION_LEDGER_HASH_FIELDS:
        if column not in work.columns:
            work[column] = None
    sort_columns = [
        column
        for column in ("signal_date", "ts_code", "quant_rank", "record_id")
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


def _record_id(strategy_id: str, strategy_version: str, signal_date: str, ts_code: str) -> str:
    raw = "|".join((strategy_id, strategy_version, signal_date, ts_code))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _planned_entry_date(value: Any) -> str:
    parsed = _contract_timestamp(value, field="planned_entry_time")
    if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) != (9, 30, 0, 0):
        raise EvaluationContractError("planned_entry_time must be the next open at 09:30:00+08:00")
    return parsed.strftime("%Y%m%d")


def _validate_snapshot(snapshot: dict[str, Any], *, open_trade_dates: Iterable[str]) -> None:
    required = {
        "strategy_id",
        "strategy_version",
        "signal_date",
        "signal_data_cutoff",
        "planned_entry_time",
        "holding_period_days",
        "auxiliary_holding_period_days",
        "data_sources",
        "used_proxy",
        "completeness_status",
        "revision_chain_complete",
        "evidence_scope",
        "promotion_evidence_eligible",
        "round_trip_cost",
        "stress_round_trip_cost",
        "benchmark",
        "rank_change",
        "publish_mode",
        "execution_authority",
        "ai_policy",
        "active_positions",
        "active_invested_weight",
        "new_invested_weight",
        "portfolio_invested_weight",
        "cash_weight",
        "ranked_events",
        "candidates",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        raise EvaluationContractError(f"snapshot missing fields: {missing}")
    if str(snapshot.get("strategy_id")) != strategy.STRATEGY_ID:
        raise EvaluationContractError("snapshot strategy_id does not match event_quality_drift_v1")
    if str(snapshot.get("strategy_version")) != strategy.STRATEGY_VERSION:
        raise EvaluationContractError("snapshot strategy_version does not match the immutable strategy contract")
    if bool(snapshot.get("used_proxy")):
        raise EvaluationContractError("proxy data is forbidden in event_quality_drift evaluation")
    if str(snapshot.get("completeness_status")) != "complete":
        raise EvaluationContractError("incomplete snapshots cannot enter the evaluation ledger")
    if int(snapshot.get("rank_change") or 0) != 0:
        raise EvaluationContractError("AI or downstream rank changes are forbidden")
    if str(snapshot.get("publish_mode")) != "observe_only":
        raise EvaluationContractError("event_quality_drift candidates must remain observe_only")
    if str(snapshot.get("execution_authority")) != "observe_only_no_auto_order":
        raise EvaluationContractError("execution authority must remain observe_only_no_auto_order")
    if int(snapshot.get("holding_period_days") or 0) != PRIMARY_HOLDING_PERIOD_DAYS:
        raise EvaluationContractError("primary holding period must be 20 trading days")
    auxiliary = tuple(int(value) for value in snapshot.get("auxiliary_holding_period_days") or ())
    if auxiliary != AUXILIARY_HOLDING_PERIOD_DAYS:
        raise EvaluationContractError("auxiliary holding periods must be [40]")
    if str(snapshot.get("benchmark")) != BENCHMARK_ID:
        raise EvaluationContractError("benchmark must be all-A tradable equal weight")
    if abs(_finite_float(snapshot.get("round_trip_cost"), field="round_trip_cost") - BASE_ROUND_TRIP_COST) > 1e-12:
        raise EvaluationContractError("base round-trip cost must be 0.30%")
    if abs(_finite_float(snapshot.get("stress_round_trip_cost"), field="stress_round_trip_cost") - STRESS_ROUND_TRIP_COST) > 1e-12:
        raise EvaluationContractError("stress round-trip cost must be 0.50%")
    signal_date = _date8(snapshot.get("signal_date"), field="signal_date")
    cutoff = _contract_timestamp(snapshot.get("signal_data_cutoff"), field="signal_data_cutoff")
    if cutoff.strftime("%Y%m%d") != signal_date or (cutoff.hour, cutoff.minute, cutoff.second) != (23, 59, 59):
        raise EvaluationContractError("signal_data_cutoff must be T day 23:59:59+08:00")
    calendar = sorted({_date8(value, field="open_trade_date") for value in open_trade_dates})
    future_dates = [date for date in calendar if date > signal_date]
    if not future_dates:
        raise EvaluationContractError("exchange calendar does not contain the next open day")
    if _planned_entry_date(snapshot.get("planned_entry_time")) != future_dates[0]:
        raise EvaluationContractError("planned_entry_time is not the next exchange open after signal_date")
    ai_policy = snapshot.get("ai_policy")
    if not isinstance(ai_policy, dict):
        raise EvaluationContractError("ai_policy must be a dict")
    if bool(ai_policy.get("may_change_rank")) or bool(ai_policy.get("may_add_candidate")) or bool(ai_policy.get("may_remove_candidate")):
        raise EvaluationContractError("AI policy may not alter candidate ranking or membership")
    revision_chain_complete = bool(snapshot.get("revision_chain_complete"))
    promotion_evidence_eligible = bool(snapshot.get("promotion_evidence_eligible"))
    expected_scope = "promotion_evidence" if revision_chain_complete and promotion_evidence_eligible else "auxiliary_only"
    if str(snapshot.get("evidence_scope")) != expected_scope:
        raise EvaluationContractError("evidence_scope does not match revision-chain promotion eligibility")
    if not isinstance(snapshot.get("data_sources"), list) or not snapshot["data_sources"]:
        raise EvaluationContractError("data_sources must be a non-empty list")
    if not isinstance(snapshot.get("candidates"), list):
        raise EvaluationContractError("candidates must be a list")
    if not isinstance(snapshot.get("ranked_events"), list) or not snapshot["ranked_events"]:
        raise EvaluationContractError("ranked_events must be a non-empty list")
    active_invested_weight = _finite_float(
        snapshot.get("active_invested_weight"), field="active_invested_weight"
    )
    new_invested_weight = _finite_float(
        snapshot.get("new_invested_weight"), field="new_invested_weight"
    )
    portfolio_invested_weight = _finite_float(
        snapshot.get("portfolio_invested_weight"), field="portfolio_invested_weight"
    )
    cash_weight = _finite_float(snapshot.get("cash_weight"), field="cash_weight")
    if min(active_invested_weight, new_invested_weight, portfolio_invested_weight, cash_weight) < -1e-8:
        raise EvaluationContractError("portfolio sleeve weights cannot be negative")
    if portfolio_invested_weight > 1.0 or cash_weight > 1.0:
        raise EvaluationContractError("portfolio and cash weights cannot exceed 1.0")
    if not math.isclose(
        active_invested_weight + new_invested_weight,
        portfolio_invested_weight,
        rel_tol=0.0,
        abs_tol=1e-8,
    ):
        raise EvaluationContractError("active plus new weight must equal portfolio invested weight")
    if not math.isclose(portfolio_invested_weight + cash_weight, 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise EvaluationContractError("portfolio invested weight and cash weight must sum to 1.0")
    active_positions = snapshot.get("active_positions")
    if not isinstance(active_positions, list):
        raise EvaluationContractError("active_positions must be a list")
    active_weights = [_finite_float(item.get("weight"), field="active position weight") for item in active_positions]
    if any(
        weight <= 0.0 or weight > float(strategy.RULES["max_stock_weight"]) + 1e-12
        for weight in active_weights
    ):
        raise EvaluationContractError("active position weight violates the single-stock cap")
    if not math.isclose(sum(active_weights), active_invested_weight, rel_tol=0.0, abs_tol=1e-8):
        raise EvaluationContractError("active position weights must sum to active_invested_weight")
    weights = [_finite_float(item.get("weight"), field="candidate weight") for item in snapshot["candidates"]]
    if any(weight <= 0 for weight in weights):
        raise EvaluationContractError("candidate weights must be strictly positive")
    if not math.isclose(sum(weights), new_invested_weight, rel_tol=0.0, abs_tol=1e-8):
        raise EvaluationContractError("candidate weights must sum to new_invested_weight")
    if len(snapshot["candidates"]) + len(active_positions) > int(strategy.RULES["max_positions"]):
        raise EvaluationContractError("active and new positions exceed the 20-position cap")
    if any(weight > float(strategy.RULES["max_stock_weight"]) + 1e-12 for weight in weights):
        raise EvaluationContractError("candidate weight exceeds the single-stock cap")
    ranked_codes: set[str] = set()
    ranked_quant_ranks: set[int] = set()
    for event in snapshot["ranked_events"]:
        code = str(event.get("ts_code") or "").strip()
        quant_rank = int(event.get("quant_rank") or 0)
        if not code or code in ranked_codes:
            raise EvaluationContractError("ranked event ts_code must be present and unique")
        if quant_rank <= 0 or quant_rank in ranked_quant_ranks:
            raise EvaluationContractError("ranked event quant_rank must be positive and unique")
        if int(event.get("rank_change") or 0) != 0:
            raise EvaluationContractError("ranked events may not be changed by AI")
        ranked_codes.add(code)
        ranked_quant_ranks.add(quant_rank)
    candidate_codes: set[str] = set()
    active_codes: set[str] = set()
    industry_weights: dict[str, float] = {}
    for position in active_positions:
        code = str(position.get("ts_code") or "").strip()
        if not code or code in active_codes:
            raise EvaluationContractError("active position codes must be present and unique")
        active_codes.add(code)
        industry = str(position.get("industry") or "UNKNOWN")
        industry_weights[industry] = industry_weights.get(industry, 0.0) + _finite_float(
            position.get("weight"), field="active position weight"
        )
    for candidate in snapshot["candidates"]:
        code = str(candidate.get("ts_code") or "").strip()
        if not code or code in candidate_codes or code in active_codes or code not in ranked_codes:
            raise EvaluationContractError("candidate codes must be unique members of ranked_events")
        if int(candidate.get("rank_change") or 0) != 0:
            raise EvaluationContractError("candidate ranks may not be changed by AI")
        if not bool(candidate.get("risk_gate_passed")):
            raise EvaluationContractError("candidate failed deterministic risk rules")
        candidate_codes.add(code)
        industry = str(candidate.get("industry") or "UNKNOWN")
        industry_weights[industry] = industry_weights.get(industry, 0.0) + _finite_float(
            candidate.get("weight"), field="candidate weight"
        )
    if any(
        weight > float(strategy.RULES["max_industry_weight"]) + 1e-12
        for weight in industry_weights.values()
    ):
        raise EvaluationContractError("candidate industry weight exceeds the industry cap")


def pending_rows_from_snapshot(
    snapshot: dict[str, Any],
    *,
    existing: pd.DataFrame | None = None,
    open_trade_dates: Iterable[str],
) -> pd.DataFrame:
    """Create append-only pending ledger rows from an immutable strategy snapshot."""
    calendar = list(open_trade_dates)
    _validate_snapshot(snapshot, open_trade_dates=calendar)
    signal_date = _date8(snapshot["signal_date"], field="signal_date")
    entry_date = _planned_entry_date(snapshot["planned_entry_time"])
    calendar_target = _calendar_targets(signal_date, calendar)
    if calendar_target is None or calendar_target[0] != entry_date:
        raise EvaluationContractError("snapshot entry date does not match the exchange calendar")
    _, exit_targets = calendar_target
    strategy_id = str(snapshot["strategy_id"])
    strategy_version = str(snapshot["strategy_version"])
    data_sources = json.dumps(snapshot["data_sources"], ensure_ascii=False)
    new_rows: list[dict[str, Any]] = []
    candidates_by_code = {
        str(candidate["ts_code"]): candidate for candidate in snapshot["candidates"]
    }
    ranked_events = sorted(
        snapshot["ranked_events"], key=lambda event: int(event["quant_rank"])
    )
    for event in ranked_events:
        ts_code = str(event["ts_code"])
        candidate = candidates_by_code.get(ts_code)
        is_selected = candidate is not None
        source = candidate if candidate is not None else event
        row = {
            "record_id": _record_id(strategy_id, strategy_version, signal_date, ts_code),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "signal_date": signal_date,
            "signal_data_cutoff": str(snapshot["signal_data_cutoff"]),
            "planned_entry_time": str(snapshot["planned_entry_time"]),
            "entry_trade_date": entry_date,
            "main_holding_period_days": PRIMARY_HOLDING_PERIOD_DAYS,
            "auxiliary_holding_period_days": json.dumps(list(AUXILIARY_HOLDING_PERIOD_DAYS), ensure_ascii=False),
            "ts_code": ts_code,
            "rank": int(source.get("rank") or event.get("quant_rank")),
            "industry": str(source.get("industry") or event.get("industry") or "UNKNOWN"),
            "weight": (
                _finite_float(candidate.get("weight"), field="candidate weight")
                if candidate is not None
                else 0.0
            ),
            "active_invested_weight": _finite_float(
                snapshot["active_invested_weight"], field="active_invested_weight"
            ),
            "new_invested_weight": _finite_float(
                snapshot["new_invested_weight"], field="new_invested_weight"
            ),
            "portfolio_invested_weight": _finite_float(
                snapshot["portfolio_invested_weight"], field="portfolio_invested_weight"
            ),
            "cash_weight": _finite_float(snapshot["cash_weight"], field="cash_weight"),
            "is_selected": bool(is_selected),
            "quant_rank": int(event["quant_rank"]),
            "round_trip_cost": BASE_ROUND_TRIP_COST,
            "stress_round_trip_cost": STRESS_ROUND_TRIP_COST,
            "benchmark": BENCHMARK_ID,
            "data_sources": data_sources,
            "used_proxy": False,
            "completeness_status": "complete",
            "revision_chain_complete": bool(snapshot["revision_chain_complete"]),
            "evidence_scope": str(snapshot["evidence_scope"]),
            "promotion_evidence_eligible": bool(snapshot["promotion_evidence_eligible"]),
            "settlement_status": "pending",
            "data_missing_reason": None,
            "publish_mode": "observe_only",
            "rank_change": 0,
            "execution_authority": "observe_only_no_auto_order",
            "ai_evidence_time": source.get("ai_evidence_time") if is_selected else None,
            "ai_risk_tags": json.dumps(
                source.get("ai_risk_tags") or [], ensure_ascii=False
            ) if is_selected else "[]",
            "ai_explanation": source.get("ai_explanation") if is_selected else None,
            "risk_gate_passed": bool(source.get("risk_gate_passed")),
            "deterministic_risk_checks": json.dumps(
                source.get("deterministic_risk_checks") or {}, ensure_ascii=False, sort_keys=True
            ),
            "exit_20d_trade_date": exit_targets.get(20),
            "exit_40d_trade_date": exit_targets.get(40),
            "entry_open_qfq": np.nan,
            "return_20d_net": np.nan,
            "return_20d_stress": np.nan,
            "return_40d_net": np.nan,
            "return_40d_stress": np.nan,
            "benchmark_return_20d": np.nan,
            "benchmark_return_40d": np.nan,
            "excess_return_20d": np.nan,
            "excess_return_40d": np.nan,
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
    for horizon in (20, 40):
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
    """Settle matured rows from T+1 open to the 20th/40th trading-day closes."""
    result = ledger.copy()
    if result.empty:
        return result
    price_index = _normalized_prices(prices)
    universe = _normalized_universe(pit_universe)
    as_of = _date8(as_of_date, field="as_of_date")
    for index, row in result.iterrows():
        signal_date = _date8(row.get("signal_date"), field="signal_date")
        calendar_target = _calendar_targets(signal_date, open_trade_dates)
        if calendar_target is None:
            continue
        entry_date, targets = calendar_target
        result.at[index, "entry_trade_date"] = entry_date
        result.at[index, "exit_20d_trade_date"] = targets.get(20)
        result.at[index, "exit_40d_trade_date"] = targets.get(40)
        main_exit = targets.get(20)
        if main_exit is None or as_of < main_exit:
            if str(row.get("settlement_status")) != "settled":
                result.at[index, "settlement_status"] = "pending"
            continue
        code = str(row.get("ts_code"))
        prior_entry = pd.to_numeric(pd.Series([row.get("entry_open_qfq")]), errors="coerce").iloc[0]
        prior_return_20 = pd.to_numeric(pd.Series([row.get("return_20d_net")]), errors="coerce").iloc[0]
        prior_benchmark_20 = pd.to_numeric(
            pd.Series([row.get("benchmark_return_20d")]), errors="coerce"
        ).iloc[0]
        primary_already_settled = (
            str(row.get("settlement_status")) == "settled"
            and pd.notna(prior_entry)
            and pd.notna(prior_return_20)
            and pd.notna(prior_benchmark_20)
        )
        entry = float(prior_entry) if primary_already_settled else _price_value(
            price_index, entry_date, code, "open_qfq"
        )
        if not primary_already_settled:
            exit_20 = _price_value(price_index, main_exit, code, "close_qfq")
            benchmark_20 = _benchmark_return(price_index, universe, entry_date, main_exit)
            if entry is None or exit_20 is None or benchmark_20 is None:
                result.at[index, "settlement_status"] = "data_missing"
                result.at[index, "completeness_status"] = "data_missing"
                result.at[index, "data_missing_reason"] = "primary_20d_qfq_price_or_pit_benchmark_missing"
                for field in (
                    "entry_open_qfq",
                    "return_20d_net",
                    "return_20d_stress",
                    "return_40d_net",
                    "return_40d_stress",
                    "benchmark_return_20d",
                    "benchmark_return_40d",
                    "excess_return_20d",
                    "excess_return_40d",
                ):
                    result.at[index, field] = np.nan
                continue
            gross_20 = float(exit_20 / entry - 1.0)
            result.at[index, "entry_open_qfq"] = entry
            result.at[index, "return_20d_net"] = gross_20 - BASE_ROUND_TRIP_COST
            result.at[index, "return_20d_stress"] = gross_20 - STRESS_ROUND_TRIP_COST
            result.at[index, "benchmark_return_20d"] = benchmark_20
            result.at[index, "excess_return_20d"] = (
                gross_20 - BASE_ROUND_TRIP_COST - benchmark_20
            )
        result.at[index, "settlement_status"] = "settled"
        result.at[index, "completeness_status"] = "complete"
        result.at[index, "data_missing_reason"] = None

        aux_exit = targets.get(40)
        if aux_exit is None or as_of < aux_exit:
            continue
        prior_return_40 = pd.to_numeric(
            pd.Series([row.get("return_40d_net")]), errors="coerce"
        ).iloc[0]
        if pd.notna(prior_return_40):
            continue
        exit_40 = _price_value(price_index, aux_exit, code, "close_qfq")
        benchmark_40 = _benchmark_return(price_index, universe, entry_date, aux_exit)
        if entry is None or exit_40 is None or benchmark_40 is None:
            result.at[index, "data_missing_reason"] = "auxiliary_40d_qfq_price_or_pit_benchmark_missing"
            for field in (
                "return_40d_net",
                "return_40d_stress",
                "benchmark_return_40d",
                "excess_return_40d",
            ):
                result.at[index, field] = np.nan
            continue
        gross_40 = float(exit_40 / entry - 1.0)
        result.at[index, "return_40d_net"] = gross_40 - BASE_ROUND_TRIP_COST
        result.at[index, "return_40d_stress"] = gross_40 - STRESS_ROUND_TRIP_COST
        result.at[index, "benchmark_return_40d"] = benchmark_40
        result.at[index, "excess_return_40d"] = (
            gross_40 - BASE_ROUND_TRIP_COST - benchmark_40
        )
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
    returns: list[float] = []
    current_universe = universe[universe["trade_date"] == trade_date]
    current_by_code = current_universe.set_index("ts_code", drop=False)
    for code in codes:
        if previous_trade_date is None:
            start_price = _price_value(price_index, trade_date, code, "open_qfq")
        else:
            start_price = _price_value(price_index, previous_trade_date, code, "close_qfq")
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
            # Left the tape (delist / rename / dropped from daily). Carry last print.
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


def build_persistent_portfolio_daily_evidence(
    ledger: pd.DataFrame,
    *,
    prices: pd.DataFrame,
    pit_universe: pd.DataFrame,
    open_trade_dates: Iterable[str],
    as_of_date: str,
) -> pd.DataFrame:
    """Mark the real overlapping 20-day position book to market once per open day."""
    required = {
        "strategy_id",
        "strategy_version",
        "signal_date",
        "ts_code",
        "industry",
        "weight",
        "is_selected",
        "round_trip_cost",
        "revision_chain_complete",
        "promotion_evidence_eligible",
        "evidence_scope",
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
    work = work[
        (work["signal_date"] >= PROMOTION_START_DATE)
        & (work["signal_date"] <= FINAL_HISTORY_END_DATE)
        & work["is_selected"].fillna(False).astype(bool)
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=PORTFOLIO_DAILY_COLUMNS)
    strategy_ids = sorted(set(work["strategy_id"].astype(str)))
    strategy_versions = sorted(set(work["strategy_version"].astype(str)))
    if len(strategy_ids) != 1 or len(strategy_versions) != 1:
        raise EvaluationContractError(
            "portfolio evidence must contain one immutable strategy version"
        )
    calendar = sorted({_date8(value, field="open_trade_date") for value in open_trade_dates})
    as_of = _date8(as_of_date, field="as_of_date")
    prepared: list[dict[str, Any]] = []
    for row_index, row in work.iterrows():
        signal_date = str(row["signal_date"])
        targets = _calendar_targets(signal_date, calendar)
        if targets is None:
            raise EvaluationContractError(
                f"exchange calendar has no entry date for {signal_date}"
            )
        entry_date, exits = targets
        exit_date = exits.get(PRIMARY_HOLDING_PERIOD_DAYS)
        if exit_date is None:
            raise EvaluationContractError(
                f"exchange calendar has no 20-day exit for {signal_date}"
            )
        stored_entry = str(row.get("entry_trade_date") or "").replace("-", "")[:8]
        stored_exit = str(row.get("exit_20d_trade_date") or "").replace("-", "")[:8]
        if stored_entry and stored_entry != entry_date:
            raise EvaluationContractError("ledger entry date conflicts with the exchange calendar")
        if stored_exit and stored_exit != exit_date:
            raise EvaluationContractError("ledger 20-day exit conflicts with the exchange calendar")
        weight = _finite_float(row.get("weight"), field="portfolio position weight")
        cost = _finite_float(row.get("round_trip_cost"), field="round_trip_cost")
        if weight <= 0.0 or weight > float(strategy.RULES["max_stock_weight"]) + 1e-12:
            raise EvaluationContractError("portfolio position violates the single-stock cap")
        if abs(cost - BASE_ROUND_TRIP_COST) > 1e-12:
            raise EvaluationContractError("portfolio evidence must use the 0.30% base cost")
        position_id = str(row.get("record_id") or f"{signal_date}|{row.get('ts_code')}|{row_index}")
        prepared.append(
            {
                "position_id": position_id,
                "ts_code": str(row.get("ts_code") or ""),
                "industry": str(row.get("industry") or "UNKNOWN"),
                "weight": weight,
                "round_trip_cost": cost,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "promotion_evidence_eligible": bool(row.get("revision_chain_complete"))
                and bool(row.get("promotion_evidence_eligible"))
                and str(row.get("evidence_scope")) == "promotion_evidence",
            }
        )
    position_ids = [row["position_id"] for row in prepared]
    if len(position_ids) != len(set(position_ids)):
        raise EvaluationContractError("portfolio evidence contains duplicate position ids")
    first_entry = min(row["entry_date"] for row in prepared)
    last_exit = min(as_of, max(row["exit_date"] for row in prepared))
    trade_dates = [date for date in calendar if first_entry <= date <= last_exit]
    if not trade_dates:
        return pd.DataFrame(columns=PORTFOLIO_DAILY_COLUMNS)
    price_index = _normalized_prices(prices)
    universe = _normalized_universe(pit_universe)
    entries: dict[str, list[dict[str, Any]]] = {}
    for row in prepared:
        if row["entry_date"] <= as_of:
            entries.setdefault(row["entry_date"], []).append(row)
    active: dict[str, dict[str, Any]] = {}
    cash = 1.0
    prior_nav = 1.0
    benchmark_nav = 1.0
    previous_trade_date: str | None = None
    source_hash = promotion_ledger_hash(ledger)
    evidence_clean = all(row["promotion_evidence_eligible"] for row in prepared)
    daily_rows: list[dict[str, Any]] = []
    for trade_date in trade_dates:
        entering = sorted(
            entries.get(trade_date, []), key=lambda row: (row["ts_code"], row["position_id"])
        )
        active_codes = {position["ts_code"] for position in active.values()}
        industry_weights: dict[str, float] = {}
        for position in active.values():
            industry_weights[position["industry"]] = (
                industry_weights.get(position["industry"], 0.0) + position["weight"]
            )
        total_allocation = sum(row["weight"] * prior_nav for row in entering)
        if total_allocation > cash + 1e-10:
            raise EvaluationContractError(
                f"persistent portfolio has insufficient cash for entries on {trade_date}"
            )
        for row in entering:
            code = row["ts_code"]
            if not code or code in active_codes:
                raise EvaluationContractError(
                    f"persistent portfolio has an overlapping duplicate position for {code}"
                )
            if len(active) + 1 > int(strategy.RULES["max_positions"]):
                raise EvaluationContractError("persistent portfolio exceeds the 20-position cap")
            next_industry_weight = industry_weights.get(row["industry"], 0.0) + row["weight"]
            if next_industry_weight > float(strategy.RULES["max_industry_weight"]) + 1e-12:
                raise EvaluationContractError("persistent portfolio exceeds the industry cap")
            entry_open = _price_value(price_index, trade_date, code, "open_qfq")
            if entry_open is None:
                raise EvaluationContractError(
                    f"entry qfq open is missing for {code} on {trade_date}"
                )
            allocation = row["weight"] * prior_nav
            active[row["position_id"]] = {
                **row,
                "allocation": allocation,
                "units": allocation / entry_open,
            }
            cash -= allocation
            active_codes.add(code)
            industry_weights[row["industry"]] = next_industry_weight
        close_values: dict[str, float] = {}
        for position_id, position in active.items():
            close_price = _price_value(
                price_index, trade_date, position["ts_code"], "close_qfq"
            )
            if close_price is None:
                raise EvaluationContractError(
                    f"active position qfq close is missing for {position['ts_code']} on {trade_date}"
                )
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
            raise EvaluationContractError("persistent portfolio NAV became non-positive or invalid")
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
                "active_position_count": int(len(active)),
                "cash_weight": float(cash / nav),
                "used_proxy": False,
                "completeness_status": "complete",
                "promotion_evidence_clean": bool(evidence_clean),
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


def _months_between(start_date: str, end_date: str) -> int:
    start = pd.Timestamp(f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}")
    end = pd.Timestamp(f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}")
    return int((end.year - start.year) * 12 + (end.month - start.month) + 1)


def _contribution_shares(rows: pd.DataFrame) -> tuple[float, float]:
    work = rows.copy()
    work["abs_contribution"] = (
        pd.to_numeric(work["return_20d_net"], errors="coerce")
        * pd.to_numeric(work["weight"], errors="coerce")
    ).abs()
    total = float(work["abs_contribution"].sum())
    if total <= 1e-15:
        return 1.0, 1.0
    industry = work.groupby("industry", dropna=False)["abs_contribution"].sum()
    stock = work.groupby("ts_code", dropna=False)["abs_contribution"].sum().sort_values(ascending=False)
    return float(industry.max() / total), float(stock.head(5).sum() / total)


def _random_ranking_test(
    pool: pd.DataFrame,
    *,
    seed: int,
    trials: int,
) -> dict[str, float | int]:
    """Re-rank each day's real event pool and rebuild constrained portfolios."""
    if trials <= 0:
        raise EvaluationContractError("random-ranking trials must be positive")
    cross_sections: list[dict[str, Any]] = []
    for signal_date, group in pool.groupby("signal_date", sort=True):
        ordered = group.sort_values(["quant_rank", "ts_code"]).reset_index(drop=True)
        selected = ordered[ordered["is_selected"]].sort_values(["quant_rank", "ts_code"])
        if selected.empty or len(ordered) <= len(selected):
            continue
        cross_sections.append(
            {
                "signal_date": str(signal_date),
                "returns": ordered["return_20d_net"].to_numpy(dtype=float),
                "benchmarks": ordered["benchmark_return_20d"].to_numpy(dtype=float),
                "industries": ordered["industry"].astype(str).to_numpy(),
                "weights": selected["weight"].to_numpy(dtype=float),
                "observed_return": float(
                    np.dot(
                        selected["return_20d_net"].to_numpy(dtype=float),
                        selected["weight"].to_numpy(dtype=float),
                    )
                ),
                "observed_benchmark": float(
                    np.dot(
                        selected["benchmark_return_20d"].to_numpy(dtype=float),
                        selected["weight"].to_numpy(dtype=float),
                    )
                ),
            }
        )
    if not cross_sections:
        return {
            "seed": seed,
            "trials": trials,
            "eligible_cross_sections": 0,
            "observed_absolute_return": 0.0,
            "observed_excess_return": 0.0,
            "p_value_absolute": 1.0,
            "percentile_absolute": 0.0,
            "p_value_excess": 1.0,
            "percentile_excess": 0.0,
        }
    observed_returns = np.asarray(
        [section["observed_return"] for section in cross_sections], dtype=float
    )
    observed_benchmarks = np.asarray(
        [section["observed_benchmark"] for section in cross_sections], dtype=float
    )
    observed_absolute = _compound(observed_returns)
    observed_excess = _compound(observed_returns - observed_benchmarks)
    simulated_absolute = np.empty(trials, dtype=float)
    simulated_excess = np.empty(trials, dtype=float)
    rng = np.random.default_rng(seed)
    industry_cap = float(strategy.RULES["max_industry_weight"])
    for trial in range(trials):
        trial_returns: list[float] = []
        trial_benchmarks: list[float] = []
        for section in cross_sections:
            permutation = rng.permutation(len(section["returns"]))
            selected_indexes: list[int] = []
            industry_weights: dict[str, float] = {}
            for pool_index in permutation:
                weight_position = len(selected_indexes)
                if weight_position >= len(section["weights"]):
                    break
                weight = float(section["weights"][weight_position])
                industry = str(section["industries"][pool_index])
                if industry_weights.get(industry, 0.0) + weight > industry_cap + 1e-12:
                    continue
                selected_indexes.append(int(pool_index))
                industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
            if len(selected_indexes) != len(section["weights"]):
                raise EvaluationContractError(
                    "random ranking could not rebuild the observed portfolio under industry caps"
                )
            weights = np.asarray(section["weights"], dtype=float)
            indexes = np.asarray(selected_indexes, dtype=int)
            trial_returns.append(float(np.dot(section["returns"][indexes], weights)))
            trial_benchmarks.append(float(np.dot(section["benchmarks"][indexes], weights)))
        trial_return_array = np.asarray(trial_returns, dtype=float)
        simulated_absolute[trial] = _compound(trial_return_array)
        simulated_excess[trial] = _compound(
            trial_return_array - np.asarray(trial_benchmarks, dtype=float)
        )
    p_value_absolute = float(
        (np.sum(simulated_absolute >= observed_absolute) + 1) / (trials + 1)
    )
    p_value_excess = float(
        (np.sum(simulated_excess >= observed_excess) + 1) / (trials + 1)
    )
    return {
        "seed": seed,
        "trials": trials,
        "eligible_cross_sections": int(len(cross_sections)),
        "observed_absolute_return": float(observed_absolute),
        "observed_excess_return": float(observed_excess),
        "p_value_absolute": p_value_absolute,
        "percentile_absolute": float(
            (np.sum(simulated_absolute <= observed_absolute) + 1) / (trials + 1)
        ),
        "p_value_excess": p_value_excess,
        "percentile_excess": float(
            (np.sum(simulated_excess <= observed_excess) + 1) / (trials + 1)
        ),
    }


def _segment_metrics(daily: pd.DataFrame) -> dict[str, float | int]:
    if daily.empty:
        return {
            "trade_days": 0,
            "net_absolute_return": 0.0,
            "net_excess_return": 0.0,
            "maximum_drawdown": 1.0,
        }
    strategy_values = daily["strategy_return"].to_numpy(dtype=float)
    excess_values = daily["excess_return"].to_numpy(dtype=float)
    return {
        "trade_days": int(len(daily)),
        "net_absolute_return": float(_compound(strategy_values)),
        "net_excess_return": float(_compound(excess_values)),
        "maximum_drawdown": float(_max_drawdown(strategy_values)),
    }


def _validated_portfolio_daily(
    ledger: pd.DataFrame,
    portfolio_daily: pd.DataFrame | None,
    *,
    strategy_id: str | None,
    strategy_version: str | None,
) -> tuple[pd.DataFrame, bool, bool]:
    if portfolio_daily is None or portfolio_daily.empty:
        return (
            pd.DataFrame(columns=[*PORTFOLIO_DAILY_COLUMNS, "excess_return"]),
            False,
            False,
        )
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
        "active_position_count",
        "cash_weight",
    ]
    for column in numeric_columns:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
    if daily[numeric_columns].replace([np.inf, -np.inf], np.nan).isna().any().any():
        raise EvaluationContractError("portfolio daily evidence contains invalid numeric values")
    if (daily[["strategy_nav", "benchmark_nav"]] <= 0.0).any().any():
        raise EvaluationContractError("portfolio daily NAV must remain positive")
    if (daily["active_position_count"] < 0).any() or (
        daily["active_position_count"] > int(strategy.RULES["max_positions"])
    ).any():
        raise EvaluationContractError("portfolio daily position count violates the cap")
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
    evidence_clean = bool(
        daily["promotion_evidence_clean"].map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
        ).all()
    )
    return daily, True, evidence_clean


def evaluate_final_2026_completeness(
    promotion_window: pd.DataFrame,
    *,
    completeness_context: dict[str, Any] | None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Return 2026 completeness gates using calendar + independent coverage (fail-closed).

    Signal-window completeness must NOT be inferred from the last candidate signal in the ledger.
    Settlement completeness includes every selected row with a complete revision chain in the
    promotion window for 2026, regardless of evidence_scope / promotion_evidence_eligible.
    """
    failed_closed = {
        "final_2026_signal_window_complete": False,
        "final_2026_settlement_complete": False,
        "final_2026_not_before_calendar_close": False,
    }
    details: dict[str, Any] = {
        "completeness_context_present": completeness_context is not None,
        "signal_window_basis": "exchange_calendar_and_announcement_coverage",
        "settlement_bypass_via_auxiliary_scope": False,
    }
    if not isinstance(completeness_context, dict):
        details["error"] = "completeness_context_required"
        return failed_closed, details
    missing_keys = [key for key in COMPLETENESS_CONTEXT_KEYS if key not in completeness_context]
    if missing_keys:
        details["error"] = f"completeness_context missing keys: {missing_keys}"
        return failed_closed, details

    try:
        calendar = sorted(
            {
                _date8(value, field="open_trade_date")
                for value in (completeness_context.get("open_trade_dates") or [])
            }
        )
        expected = sorted(
            {
                _date8(value, field="expected_2026_trade_date")
                for value in (completeness_context.get("expected_2026_trade_dates") or [])
            }
        )
        covered = {
            _date8(value, field="announcement_coverage_ok_date")
            for value in (completeness_context.get("announcement_coverage_ok_dates") or [])
        }
        as_of = _date8(
            completeness_context.get("evaluation_as_of_date"),
            field="evaluation_as_of_date",
        )
    except EvaluationContractError as exc:
        details["error"] = str(exc)
        return failed_closed, details

    calendar_2026 = [
        date for date in calendar if date.startswith("2026") and date <= FINAL_HISTORY_END_DATE
    ]
    expected_2026 = [
        date for date in expected if date.startswith("2026") and date <= FINAL_HISTORY_END_DATE
    ]
    details.update(
        {
            "expected_2026_trade_date_count": len(expected_2026),
            "calendar_2026_trade_date_count": len(calendar_2026),
            "coverage_ok_count": len(covered),
            "evaluation_as_of_date": as_of,
            "final_history_end_date": FINAL_HISTORY_END_DATE,
            "last_2026_trade_date": calendar_2026[-1] if calendar_2026 else None,
            "last_expected_2026_trade_date": expected_2026[-1] if expected_2026 else None,
        }
    )

    signal_window_complete = bool(calendar_2026) and expected_2026 == calendar_2026 and set(
        expected_2026
    ).issubset(covered)
    details["missing_coverage_dates"] = sorted(set(expected_2026) - covered)[:20]
    details["expected_vs_calendar_mismatch"] = expected_2026 != calendar_2026

    work = promotion_window.copy()
    if not work.empty:
        work["signal_date"] = work["signal_date"].map(
            lambda value: _date8(value, field="signal_date")
        )
        if "is_selected" in work.columns:
            work["is_selected"] = work["is_selected"].map(
                lambda value: bool(value) if isinstance(value, (bool, np.bool_)) or value in (0, 1) else False
            )
        else:
            work["is_selected"] = False
        if "revision_chain_complete" in work.columns:
            work["revision_chain_complete"] = work["revision_chain_complete"].fillna(False).astype(bool)
        else:
            work["revision_chain_complete"] = False
    # Must-settle set ignores evidence_scope so re-labeling unsettled rows as auxiliary cannot bypass.
    must_settle = work[
        work["signal_date"].astype(str).str.startswith("2026")
        & (work["signal_date"].astype(str) <= FINAL_HISTORY_END_DATE)
        & work["is_selected"].astype(bool)
        & work["revision_chain_complete"].astype(bool)
    ].copy() if not work.empty else work
    unsettled_mask = pd.Series(dtype=bool)
    if not must_settle.empty:
        returns = pd.to_numeric(must_settle.get("return_20d_net"), errors="coerce")
        unsettled_mask = (
            must_settle["settlement_status"].astype(str) != "settled"
        ) | returns.isna() | ~np.isfinite(returns.to_numpy(dtype=float))
    settlement_complete = bool(must_settle.empty) or (not bool(unsettled_mask.any()))
    details["must_settle_selected_revision_complete_2026"] = int(len(must_settle))
    details["unsettled_must_settle_rows"] = (
        int(unsettled_mask.sum()) if len(unsettled_mask) else 0
    )
    if not must_settle.empty and "evidence_scope" in must_settle.columns:
        details["settlement_bypass_via_auxiliary_scope"] = bool(
            (
                unsettled_mask
                & (must_settle["evidence_scope"].astype(str) != "promotion_evidence")
            ).any()
        )

    earliest_conclusion_date = None
    last_trade = calendar_2026[-1] if calendar_2026 else None
    if last_trade is not None:
        targets = _calendar_targets(last_trade, calendar)
        if targets is not None:
            _entry, exit_map = targets
            earliest_conclusion_date = exit_map.get(PRIMARY_HOLDING_PERIOD_DAYS)
    details["earliest_promotion_conclusion_date"] = earliest_conclusion_date
    calendar_close_ok = bool(
        earliest_conclusion_date is not None and as_of >= earliest_conclusion_date
    )

    gates = {
        "final_2026_signal_window_complete": bool(signal_window_complete),
        "final_2026_settlement_complete": bool(settlement_complete),
        "final_2026_not_before_calendar_close": bool(calendar_close_ok),
    }
    return gates, details


def evaluate_event_quality_drift_promotion(
    ledger: pd.DataFrame,
    *,
    portfolio_daily: pd.DataFrame | None = None,
    permutation_seed: int = PERMUTATION_SEED,
    permutation_trials: int = PERMUTATION_TRIALS,
    completeness_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate frozen 2025/final 2026 evidence against true same-day random rankings."""
    required = {
        "strategy_id",
        "strategy_version",
        "signal_date",
        "ts_code",
        "industry",
        "weight",
        "is_selected",
        "quant_rank",
        "settlement_status",
        "revision_chain_complete",
        "promotion_evidence_eligible",
        "evidence_scope",
        "return_20d_net",
        "benchmark_return_20d",
    }
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise EvaluationContractError(f"ledger missing promotion fields: {missing}")
    work = ledger.copy()
    work["signal_date"] = work["signal_date"].map(lambda value: _date8(value, field="signal_date"))
    research_period_rows_ignored = int((work["signal_date"] < PROMOTION_START_DATE).sum())
    promotion_window = work[
        (work["signal_date"] >= PROMOTION_START_DATE)
        & (work["signal_date"] <= FINAL_HISTORY_END_DATE)
    ].copy()
    promotion_strategy_ids = (
        sorted(set(promotion_window["strategy_id"].astype(str)))
        if not promotion_window.empty
        else []
    )
    promotion_strategy_versions = (
        sorted(set(promotion_window["strategy_version"].astype(str)))
        if not promotion_window.empty
        else []
    )
    if len(promotion_strategy_ids) > 1 or len(promotion_strategy_versions) > 1:
        raise EvaluationContractError("promotion must be evaluated one immutable strategy version at a time")
    eligible_scope = promotion_window[
        promotion_window["revision_chain_complete"].fillna(False).astype(bool)
        & promotion_window["promotion_evidence_eligible"].fillna(False).astype(bool)
        & (promotion_window["evidence_scope"].astype(str) == "promotion_evidence")
    ].copy()
    strategy_ids = sorted(set(eligible_scope["strategy_id"].astype(str))) if not eligible_scope.empty else []
    strategy_versions = sorted(set(eligible_scope["strategy_version"].astype(str))) if not eligible_scope.empty else []
    if len(strategy_ids) > 1 or len(strategy_versions) > 1:
        raise EvaluationContractError("promotion must be evaluated one immutable strategy version at a time")
    if eligible_scope.duplicated(["signal_date", "ts_code"]).any():
        raise EvaluationContractError("promotion ledger contains duplicate date/security events")
    bool_values = eligible_scope["is_selected"]
    if not bool_values.map(lambda value: isinstance(value, (bool, np.bool_)) or value in (0, 1)).all():
        raise EvaluationContractError("is_selected must contain only boolean values")
    eligible_scope["is_selected"] = bool_values.astype(bool)
    for column in ("return_20d_net", "benchmark_return_20d", "weight", "quant_rank"):
        eligible_scope[column] = pd.to_numeric(eligible_scope[column], errors="coerce")
    eligible_scope = eligible_scope.replace([np.inf, -np.inf], np.nan)
    invalid_row = (
        (eligible_scope["settlement_status"].astype(str) != "settled")
        | eligible_scope[["return_20d_net", "benchmark_return_20d", "weight", "quant_rank"]]
        .isna()
        .any(axis=1)
    )
    invalid_dates = set(eligible_scope.loc[invalid_row, "signal_date"].astype(str))
    eligible = eligible_scope[~eligible_scope["signal_date"].isin(invalid_dates)].copy()
    if not eligible.empty:
        if eligible.duplicated(["signal_date", "quant_rank"]).any():
            raise EvaluationContractError("quant_rank must be unique within each announcement date")
        selected_weight_invalid = eligible["is_selected"] & (eligible["weight"] <= 0.0)
        unselected_weight_invalid = (~eligible["is_selected"]) & (eligible["weight"].abs() > 1e-12)
        if selected_weight_invalid.any() or unselected_weight_invalid.any():
            raise EvaluationContractError("selected events need positive weight and unselected events need zero weight")
        if (eligible.loc[eligible["is_selected"], "weight"] > float(strategy.RULES["max_stock_weight"]) + 1e-12).any():
            raise EvaluationContractError("promotion ledger exceeds the single-stock weight cap")
        selected_counts = eligible[eligible["is_selected"]].groupby("signal_date").size()
        if (selected_counts > int(strategy.RULES["max_positions"])).any():
            raise EvaluationContractError("promotion ledger exceeds the 20-position cap")
        weight_sums = eligible[eligible["is_selected"]].groupby("signal_date")["weight"].sum()
        if (weight_sums > 1.0 + 1e-8).any() or (weight_sums <= 0.0).any():
            raise EvaluationContractError("selected portfolio weights must be in (0, 1]")
        industry_weights = (
            eligible[eligible["is_selected"]]
            .groupby(["signal_date", "industry"], dropna=False)["weight"]
            .sum()
        )
        if (industry_weights > float(strategy.RULES["max_industry_weight"]) + 1e-12).any():
            raise EvaluationContractError("promotion ledger exceeds the industry weight cap")
    selected = eligible[eligible["is_selected"]].copy()
    daily, portfolio_daily_complete, portfolio_evidence_clean = _validated_portfolio_daily(
        ledger,
        portfolio_daily,
        strategy_id=(promotion_strategy_ids[0] if promotion_strategy_ids else None),
        strategy_version=(
            promotion_strategy_versions[0] if promotion_strategy_versions else None
        ),
    )
    segments = {
        "frozen_2025": _segment_metrics(daily[daily["trade_date"].str.startswith("2025")]),
        "final_2026": _segment_metrics(daily[daily["trade_date"].str.startswith("2026")]),
    }
    sample_months = _months_between(daily["trade_date"].min(), daily["trade_date"].max()) if not daily.empty else 0
    industry_share, stock_share = _contribution_shares(selected) if not selected.empty else (1.0, 1.0)
    random_ranking = _random_ranking_test(
        eligible,
        seed=permutation_seed,
        trials=permutation_trials,
    )
    completeness_gates, completeness_details = evaluate_final_2026_completeness(
        promotion_window,
        completeness_context=completeness_context,
    )
    gates = {
        "persistent_portfolio_mark_to_market_complete": portfolio_daily_complete,
        "portfolio_evidence_not_contaminated_by_auxiliary_events": portfolio_evidence_clean,
        "minimum_100_valid_announcement_events": int(len(selected)) >= MIN_VALID_ANNOUNCEMENT_EVENTS,
        "minimum_12_month_span": sample_months >= MIN_SAMPLE_MONTHS,
        "frozen_2025_positive_net_absolute_return": segments["frozen_2025"]["net_absolute_return"] > 0.0,
        "frozen_2025_positive_net_excess_return": segments["frozen_2025"]["net_excess_return"] > 0.0,
        "frozen_2025_maximum_drawdown_at_most_12pct": segments["frozen_2025"]["maximum_drawdown"] <= MAX_DRAWDOWN,
        "final_2026_positive_net_absolute_return": segments["final_2026"]["net_absolute_return"] > 0.0,
        "final_2026_positive_net_excess_return": segments["final_2026"]["net_excess_return"] > 0.0,
        "final_2026_maximum_drawdown_at_most_12pct": segments["final_2026"]["maximum_drawdown"] <= MAX_DRAWDOWN,
        "random_ranking_absolute_better_than_random": random_ranking["p_value_absolute"] < 0.05,
        "random_ranking_excess_better_than_random": random_ranking["p_value_excess"] < 0.05,
        "industry_concentration": industry_share <= MAX_INDUSTRY_CONTRIBUTION_SHARE,
        "stock_concentration": stock_share <= MAX_TOP5_STOCK_CONTRIBUTION_SHARE,
        **completeness_gates,
    }
    failed_gates = [name for name, passed in gates.items() if not passed]
    all_gates_pass = not failed_gates
    return {
        "strategy_id": strategy_ids[0] if strategy_ids else None,
        "strategy_version": strategy_versions[0] if strategy_versions else None,
        "promotion_start_date": PROMOTION_START_DATE,
        "final_history_end_date": FINAL_HISTORY_END_DATE,
        "valid_announcement_events": int(len(selected)),
        "valid_random_ranking_pool_events": int(len(eligible)),
        "dropped_incomplete_cross_sections": int(len(invalid_dates)),
        "sample_trade_days": int(len(daily)),
        "sample_months": int(sample_months),
        "research_period_rows_ignored": research_period_rows_ignored,
        "portfolio_daily_source_ledger_hash": (
            promotion_ledger_hash(ledger) if portfolio_daily_complete else None
        ),
        "segments": segments,
        "random_ranking_test": random_ranking,
        "max_industry_contribution_share": float(industry_share),
        "top5_stock_contribution_share": float(stock_share),
        "final_2026_completeness": completeness_details,
        "gates": gates,
        "failed_gates": failed_gates,
        "all_gates_pass": all_gates_pass,
        "decision": "promotion_gate_passed_observe_only" if all_gates_pass else "observe_only",
        "execution_authority": "observe_only_no_auto_order",
    }


__all__ = [
    "EvaluationContractError",
    "PORTFOLIO_DAILY_COLUMNS",
    "COMPLETENESS_CONTEXT_KEYS",
    "promotion_ledger_hash",
    "pending_rows_from_snapshot",
    "settle_ledger",
    "build_persistent_portfolio_daily_evidence",
    "evaluate_final_2026_completeness",
    "evaluate_event_quality_drift_promotion",
]
