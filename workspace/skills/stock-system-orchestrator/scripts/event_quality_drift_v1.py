#!/usr/bin/env python3
"""Point-in-time announcement strategy for medium-horizon A-share research.

Only announcements first visible on the requested date become candidates.  The
strategy is research/observation only; AI may explain evidence and flag risks but
cannot alter the quantitative ranking or membership.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd


STRATEGY_ID = "event_quality_drift_v1"
FACTOR_WEIGHTS = {
    "forecast_or_revision": 0.35,
    "profit_acceleration": 0.25,
    "quality": 0.20,
    "valuation_protection": 0.20,
}
RULES = {
    "strategy_id": STRATEGY_ID,
    "factor_weights": FACTOR_WEIGHTS,
    "primary_holding_period_days": 20,
    "auxiliary_holding_period_days": [40],
    "max_positions": 20,
    "position_weight": 0.05,
    "max_stock_weight": 0.075,
    "max_industry_weight": 0.25,
    "base_round_trip_cost": 0.003,
    "stress_round_trip_cost": 0.005,
    "benchmark": "all_a_tradable_equal_weight",
    "portfolio_allocation": "persistent_20_position_book_with_5pct_new_entries",
    "formula_version": "2026-08-11.2",
}
CONFIG_HASH = hashlib.sha256(
    json.dumps(RULES, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:16]
STRATEGY_VERSION = f"1.0.0+{CONFIG_HASH}"


class EventDataIntegrityError(RuntimeError):
    pass


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _date8(value: Any, *, field: str) -> str:
    text = str(value or "").replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        raise EventDataIntegrityError(f"{field} must be YYYYMMDD")
    return text


def _ts_code(symbol: Any) -> str:
    digits = "".join(char for char in str(symbol) if char.isdigit())[:6]
    if len(digits) != 6:
        raise EventDataIntegrityError(f"invalid event symbol: {symbol!r}")
    if digits.startswith("6"):
        suffix = "SH"
    elif digits.startswith(("4", "8", "9")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{digits}.{suffix}"


def _reject_proxy_or_incomplete(frame: pd.DataFrame, *, name: str) -> None:
    for column in ("used_proxy", "completeness"):
        if column not in frame.columns:
            raise EventDataIntegrityError(f"{name} missing integrity column {column}")
    if frame["used_proxy"].fillna(True).astype(bool).any():
        raise EventDataIntegrityError(f"{name} contains proxy data")
    if not (frame["completeness"].astype(str) == "complete").all():
        raise EventDataIntegrityError(f"{name} contains incomplete rows")


def _require_columns(frame: pd.DataFrame, required: set[str], *, name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise EventDataIntegrityError(f"{name} is empty")
    missing = sorted(required - set(frame.columns))
    if missing:
        raise EventDataIntegrityError(f"{name} missing columns: {missing}")
    _reject_proxy_or_incomplete(frame, name=name)
    return frame.copy()


def _next_open(date: str, exchange_trade_dates: list[str]) -> str:
    calendar = sorted({_date8(value, field="exchange_trade_date") for value in exchange_trade_dates})
    following = [value for value in calendar if value > date]
    if not following:
        raise EventDataIntegrityError("exchange calendar does not contain next open day")
    value = following[0]
    return f"{value[:4]}-{value[4:6]}-{value[6:]}T09:30:00+08:00"


def _normalize_active_positions(active_positions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    industry_weights: dict[str, float] = {}
    for raw in active_positions or []:
        code = str(raw.get("ts_code") or "").strip()
        industry = str(raw.get("industry") or "UNKNOWN")
        try:
            weight = float(raw.get("weight"))
        except (TypeError, ValueError) as exc:
            raise EventDataIntegrityError("active position weight must be numeric") from exc
        if not code or code in seen_codes:
            raise EventDataIntegrityError("active position codes must be present and unique")
        if not math.isfinite(weight) or weight <= 0.0 or weight > float(RULES["max_stock_weight"]) + 1e-12:
            raise EventDataIntegrityError("active position weight violates the single-stock cap")
        seen_codes.add(code)
        industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
        normalized.append({"ts_code": code, "industry": industry, "weight": weight})
    if len(normalized) > int(RULES["max_positions"]):
        raise EventDataIntegrityError("active position count exceeds the portfolio cap")
    if sum(row["weight"] for row in normalized) > 1.0 + 1e-12:
        raise EventDataIntegrityError("active position weights exceed total capital")
    if any(
        weight > float(RULES["max_industry_weight"]) + 1e-12
        for weight in industry_weights.values()
    ):
        raise EventDataIntegrityError("active positions violate the industry cap")
    return sorted(normalized, key=lambda row: row["ts_code"])


def _visible_event_panel(pit_events: pd.DataFrame, announce_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = _require_columns(
        pit_events,
        {
            "symbol",
            "field",
            "period",
            "value",
            "announce_date",
            "available_at",
            "revision_seq",
            "source",
            "used_proxy",
            "completeness",
        },
        name="PIT announcement store",
    )
    events["announce_date"] = events["announce_date"].map(lambda value: _date8(value, field="announce_date"))
    events["available_at_utc"] = pd.to_datetime(events["available_at"], errors="coerce", utc=True)
    if events["available_at_utc"].isna().any():
        raise EventDataIntegrityError("PIT announcement store contains invalid available_at")
    cutoff = pd.Timestamp(
        f"{announce_date[:4]}-{announce_date[4:6]}-{announce_date[6:]} 23:59:59",
        tz="Asia/Shanghai",
    ).tz_convert("UTC")
    visible = events[events["available_at_utc"] <= cutoff].copy()
    visible["period"] = pd.to_numeric(visible["period"], errors="coerce")
    visible["revision_seq"] = pd.to_numeric(visible["revision_seq"], errors="coerce")
    visible["value"] = pd.to_numeric(visible["value"], errors="coerce")
    visible = visible.dropna(subset=["period", "revision_seq", "value"])
    visible["period"] = visible["period"].astype(int)
    visible["revision_seq"] = visible["revision_seq"].astype(int)
    current = visible[visible["announce_date"] == announce_date].copy()
    if current.empty:
        raise EventDataIntegrityError(f"no new announcement events on {announce_date}")
    return visible, current


def _pivot_events(frame: pd.DataFrame) -> pd.DataFrame:
    indexed = (
        frame.sort_values(["symbol", "period", "announce_date", "revision_seq", "field"])
        .pivot_table(
            index=["symbol", "period", "announce_date", "revision_seq"],
            columns="field",
            values="value",
            aggfunc="last",
        )
        .reset_index()
    )
    indexed.columns.name = None
    return indexed


def _event_features(visible: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    history = _pivot_events(visible)
    today = _pivot_events(current)
    today = today.sort_values(["symbol", "period", "revision_seq"]).drop_duplicates(
        ["symbol", "period"], keep="last"
    )
    rows: list[dict[str, Any]] = []
    for _, event in today.iterrows():
        symbol = str(event["symbol"])
        period = int(event["period"])
        revision_seq = int(event["revision_seq"])
        growth = float(event.get("np_growth_pct")) if pd.notna(event.get("np_growth_pct")) else np.nan
        forecast = float(event.get("np_forecast")) if pd.notna(event.get("np_forecast")) else np.nan
        if not math.isfinite(growth):
            continue
        same_period_prior = history[
            (history["symbol"].astype(str) == symbol)
            & (history["period"] == period)
            & (history["announce_date"] < str(event["announce_date"]))
        ].sort_values(["announce_date", "revision_seq"])
        previous_revision_growth = (
            float(same_period_prior.iloc[-1]["np_growth_pct"])
            if len(same_period_prior) and pd.notna(same_period_prior.iloc[-1].get("np_growth_pct"))
            else np.nan
        )
        prior_periods = history[
            (history["symbol"].astype(str) == symbol)
            & (history["period"] < period)
        ].sort_values(["period", "announce_date", "revision_seq"])
        prior_periods = prior_periods.drop_duplicates("period", keep="last")
        previous_growth = (
            float(prior_periods.iloc[-1]["np_growth_pct"])
            if len(prior_periods) and pd.notna(prior_periods.iloc[-1].get("np_growth_pct"))
            else np.nan
        )
        previous_forecast = (
            float(prior_periods.iloc[-1]["np_forecast"])
            if len(prior_periods) and pd.notna(prior_periods.iloc[-1].get("np_forecast"))
            else np.nan
        )
        if not math.isfinite(previous_growth):
            continue
        surprise = growth - previous_revision_growth if revision_seq > 0 and math.isfinite(previous_revision_growth) else growth
        acceleration = growth - previous_growth
        turnaround = bool(math.isfinite(forecast) and math.isfinite(previous_forecast) and forecast > 0 >= previous_forecast)
        recent_growth = [
            float(value)
            for value in prior_periods.tail(2).get("np_growth_pct", pd.Series(dtype=float)).tolist()
            if pd.notna(value)
        ] + [growth]
        continuous_improvement = len(recent_growth) >= 3 and all(
            later > earlier for earlier, later in zip(recent_growth, recent_growth[1:])
        )
        rows.append(
            {
                "ts_code": _ts_code(symbol),
                "symbol": symbol,
                "period": period,
                "announce_date": str(event["announce_date"]),
                "revision_seq": revision_seq,
                "growth_pct": growth,
                "forecast_value": forecast if math.isfinite(forecast) else None,
                "previous_growth_pct": previous_growth,
                "forecast_or_revision_raw": surprise,
                "profit_acceleration_raw": acceleration + (50.0 if turnaround else 0.0) + (20.0 if continuous_improvement else 0.0),
                "turnaround": turnaround,
                "continuous_improvement": continuous_improvement,
            }
        )
    if not rows:
        raise EventDataIntegrityError("no target-date events have complete point-in-time growth history")
    return pd.DataFrame(rows)


def _latest_asof(frame: pd.DataFrame, *, date_column: str, target_date: str) -> pd.DataFrame:
    out = frame.copy()
    out[date_column] = out[date_column].map(lambda value: _date8(value, field=date_column))
    out = out[out[date_column] <= target_date].copy()
    return out.sort_values(["ts_code", date_column]).drop_duplicates("ts_code", keep="last")


def _neutralized_score(frame: pd.DataFrame, raw_column: str) -> pd.Series:
    values = pd.to_numeric(frame[raw_column], errors="coerce")
    lower, upper = values.quantile(0.01), values.quantile(0.99)
    clipped = values.clip(lower, upper)
    size = np.log1p(pd.to_numeric(frame["circ_mv"], errors="coerce").clip(lower=0.0))
    industries = pd.get_dummies(frame["industry"].astype(str), prefix="industry", drop_first=True, dtype=float)
    design = pd.concat(
        [pd.Series(1.0, index=frame.index, name="intercept"), size.rename("log_circ_mv"), industries],
        axis=1,
    ).astype(float)
    try:
        fitted = design.to_numpy() @ np.linalg.lstsq(design.to_numpy(), clipped.to_numpy(dtype=float), rcond=None)[0]
        residual = clipped.to_numpy(dtype=float) - fitted
    except np.linalg.LinAlgError as exc:
        raise EventDataIntegrityError(f"neutralization failed for {raw_column}") from exc
    ordered = pd.DataFrame(
        {"residual": residual, "ts_code": frame["ts_code"].astype(str), "original_index": frame.index}
    ).sort_values(["residual", "ts_code"], ascending=[True, True])
    ordered["score"] = np.linspace(0.0, 100.0, len(ordered), endpoint=True)
    return ordered.set_index("original_index")["score"].reindex(frame.index)


def build_event_quality_drift_snapshot(
    *,
    pit_events: pd.DataFrame,
    quality_history: pd.DataFrame,
    valuation_history: pd.DataFrame,
    pit_universe: pd.DataFrame,
    announce_date: str,
    exchange_trade_dates: list[str],
    revision_chain_complete: bool,
    active_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target = _date8(announce_date, field="announce_date")
    visible, current = _visible_event_panel(pit_events, target)
    event_features = _event_features(visible, current)
    quality = _require_columns(
        quality_history,
        {"ts_code", "ann_date", "end_date", "roe", "grossprofit_margin", "debt_to_assets", "used_proxy", "completeness"},
        name="PIT quality history",
    )
    valuation = _require_columns(
        valuation_history,
        {"ts_code", "trade_date", "pe_ttm", "pb", "circ_mv", "used_proxy", "completeness"},
        name="PIT valuation history",
    )
    universe = _require_columns(
        pit_universe,
        {
            "ts_code",
            "trade_date",
            "name",
            "sw2021_l1_name",
            "universe_flag",
            "tradable",
            "is_st",
            "is_suspended",
            "listing_days",
            "used_proxy",
            "completeness",
        },
        name="PIT universe history",
    )
    quality = _latest_asof(quality, date_column="ann_date", target_date=target)
    valuation = _latest_asof(valuation, date_column="trade_date", target_date=target)
    universe = _latest_asof(universe, date_column="trade_date", target_date=target)
    universe = universe[
        (pd.to_numeric(universe["universe_flag"], errors="coerce") > 0)
        & (pd.to_numeric(universe["tradable"], errors="coerce") > 0)
        & (~universe["is_st"].astype(bool))
        & (~universe["is_suspended"].astype(bool))
        & (pd.to_numeric(universe["listing_days"], errors="coerce") >= 60)
    ].copy()
    merged = event_features.merge(
        quality[["ts_code", "ann_date", "end_date", "roe", "grossprofit_margin", "debt_to_assets"]],
        on="ts_code",
        how="inner",
    ).merge(
        valuation[["ts_code", "trade_date", "pe_ttm", "pb", "circ_mv"]],
        on="ts_code",
        how="inner",
    ).merge(
        universe[["ts_code", "name", "sw2021_l1_name"]],
        on="ts_code",
        how="inner",
    )
    if merged.empty:
        raise EventDataIntegrityError("new events have no complete point-in-time quality/valuation/universe match")
    numeric = [
        "forecast_or_revision_raw",
        "profit_acceleration_raw",
        "roe",
        "grossprofit_margin",
        "debt_to_assets",
        "pe_ttm",
        "pb",
        "circ_mv",
    ]
    for column in numeric:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
    merged = merged.replace([np.inf, -np.inf], np.nan).dropna(subset=numeric)
    merged = merged[(merged["pb"] > 0) & (merged["circ_mv"] > 0)].copy()
    if merged.empty:
        raise EventDataIntegrityError("new events have no finite scoring inputs")
    merged["industry"] = merged["sw2021_l1_name"].astype(str)
    merged["quality_raw"] = merged["roe"] + 0.5 * merged["grossprofit_margin"] - 0.5 * merged["debt_to_assets"]
    earnings_yield = np.where(merged["pe_ttm"] > 0, 100.0 / merged["pe_ttm"], -100.0)
    merged["valuation_protection_raw"] = earnings_yield - np.log(merged["pb"]) * 5.0
    raw_map = {
        "forecast_or_revision": "forecast_or_revision_raw",
        "profit_acceleration": "profit_acceleration_raw",
        "quality": "quality_raw",
        "valuation_protection": "valuation_protection_raw",
    }
    for factor, raw_column in raw_map.items():
        merged[f"{factor}_score"] = _neutralized_score(merged, raw_column)
    merged["composite_score"] = sum(
        merged[f"{factor}_score"] * weight for factor, weight in FACTOR_WEIGHTS.items()
    )
    merged = merged.sort_values(["composite_score", "ts_code"], ascending=[False, True]).reset_index(drop=True)
    merged["quant_rank"] = np.arange(1, len(merged) + 1)
    ranked_events: list[dict[str, Any]] = []
    for _, row in merged.iterrows():
        ranked_events.append(
            {
                "ts_code": str(row["ts_code"]),
                "name": str(row["name"]),
                "industry": str(row["industry"]),
                "period": int(row["period"]),
                "revision_seq": int(row["revision_seq"]),
                "quant_rank": int(row["quant_rank"]),
                "score": round(float(row["composite_score"]), 8),
                "factor_scores": {
                    factor: round(float(row[f"{factor}_score"]), 8) for factor in FACTOR_WEIGHTS
                },
                "growth_pct": float(row["growth_pct"]),
                "turnaround": bool(row["turnaround"]),
                "continuous_improvement": bool(row["continuous_improvement"]),
                "quality_ann_date": str(row["ann_date"]),
                "valuation_trade_date": str(row["trade_date"]),
                "rank_change": 0,
                "deterministic_risk_checks": {
                    "point_in_time_universe_member": True,
                    "tradable_on_signal_date": True,
                    "not_st_on_signal_date": True,
                    "not_suspended_on_signal_date": True,
                    "listed_at_least_60_days": True,
                    "complete_non_proxy_inputs": True,
                    "finite_positive_pb_and_market_value": True,
                },
                "risk_gate_passed": True,
            }
        )
    active = _normalize_active_positions(active_positions)
    active_codes = {row["ts_code"] for row in active}
    active_industry_weights: dict[str, float] = {}
    for row in active:
        active_industry_weights[row["industry"]] = (
            active_industry_weights.get(row["industry"], 0.0) + float(row["weight"])
        )
    candidates: list[dict[str, Any]] = []
    industry_weights = dict(active_industry_weights)
    available_slots = int(RULES["max_positions"]) - len(active)
    for event in ranked_events:
        if len(candidates) >= available_slots:
            break
        if event["ts_code"] in active_codes:
            continue
        industry = event["industry"]
        position_weight = float(RULES["position_weight"])
        if industry_weights.get(industry, 0.0) + position_weight > float(RULES["max_industry_weight"]) + 1e-12:
            continue
        candidate = dict(event)
        candidate["rank"] = len(candidates) + 1
        candidate["weight"] = position_weight
        candidate["settlement_status"] = "pending_settlement"
        candidate["return_20d_net"] = None
        candidate["return_40d_net"] = None
        candidate["ai_evidence_time"] = None
        candidate["ai_risk_tags"] = []
        candidate["ai_explanation"] = None
        candidates.append(candidate)
        industry_weights[industry] = industry_weights.get(industry, 0.0) + position_weight
    input_hash = _stable_hash(
        {
            "announce_date": target,
            "revision_chain_complete": bool(revision_chain_complete),
            "active_positions": active,
            "ranked_events": ranked_events,
        }
    )
    active_invested_weight = round(sum(float(row["weight"]) for row in active), 12)
    new_invested_weight = round(sum(float(row["weight"]) for row in candidates), 12)
    portfolio_invested_weight = round(active_invested_weight + new_invested_weight, 12)
    portfolio_positions = [
        {**row, "position_origin": "active"} for row in active
    ] + [
        {
            "ts_code": row["ts_code"],
            "industry": row["industry"],
            "weight": row["weight"],
            "position_origin": "new_event",
        }
        for row in candidates
    ]
    return {
        "artifact_kind": "candidate_snapshot",
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "config_hash": CONFIG_HASH,
        "announce_date": target,
        "signal_date": target,
        "signal_data_cutoff": f"{target[:4]}-{target[4:6]}-{target[6:]}T23:59:59+08:00",
        "planned_entry_time": _next_open(target, exchange_trade_dates),
        "holding_period_days": 20,
        "auxiliary_holding_period_days": [40],
        "factor_weights": dict(FACTOR_WEIGHTS),
        "max_positions": 20,
        "max_stock_weight": 0.075,
        "max_industry_weight": 0.25,
        "active_position_count": len(active),
        "new_position_count": len(candidates),
        "portfolio_position_count": len(portfolio_positions),
        "active_invested_weight": float(active_invested_weight),
        "new_invested_weight": float(new_invested_weight),
        "portfolio_invested_weight": float(portfolio_invested_weight),
        "cash_weight": float(round(1.0 - portfolio_invested_weight, 12)),
        "round_trip_cost": 0.003,
        "stress_round_trip_cost": 0.005,
        "benchmark": "all_a_tradable_equal_weight",
        "data_sources": ["pit_yjyg", "pit_financial", "daily_basic", "pit_sw2021_universe"],
        "used_proxy": False,
        "completeness_status": "complete",
        "revision_chain_complete": bool(revision_chain_complete),
        "evidence_scope": "promotion_evidence" if revision_chain_complete else "auxiliary_only",
        "promotion_evidence_eligible": bool(revision_chain_complete),
        "settlement_status": "pending_settlement",
        "rank_change": 0,
        "publish_mode": "observe_only",
        "execution_authority": "observe_only_no_auto_order",
        "ai_policy": {
            "role": "announcement_evidence_explanation_and_risk_check_only",
            "may_change_rank": False,
            "may_add_candidate": False,
            "may_remove_candidate": False,
        },
        "deterministic_risk_policy": {
            "authority": "rules_only",
            "ai_tags_are_non_blocking": True,
            "all_candidates_passed": True,
        },
        "input_hash": input_hash,
        "active_positions": active,
        "portfolio_positions": portfolio_positions,
        "ranked_events": ranked_events,
        "candidates": candidates,
    }


def attach_ai_explanations(
    snapshot: dict[str, Any],
    analyses_by_code: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Attach evidence/risk prose while proving ranking and membership stay unchanged."""
    if str(snapshot.get("strategy_id")) != STRATEGY_ID or str(snapshot.get("strategy_version")) != STRATEGY_VERSION:
        raise EventDataIntegrityError("AI attachment target does not match the immutable strategy version")
    if not isinstance(analyses_by_code, dict):
        raise EventDataIntegrityError("AI analyses must be keyed by candidate ts_code")
    result = copy.deepcopy(snapshot)
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        raise EventDataIntegrityError("AI attachment target has no candidate list")
    before = [
        (str(row.get("ts_code")), int(row.get("rank") or 0), float(row.get("score") or 0.0))
        for row in candidates
    ]
    by_code = {str(row.get("ts_code")): row for row in candidates}
    unknown = sorted(set(map(str, analyses_by_code)) - set(by_code))
    if unknown:
        raise EventDataIntegrityError(f"AI may not add candidates: {unknown}")
    try:
        planned_entry = datetime.fromisoformat(str(result["planned_entry_time"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise EventDataIntegrityError("snapshot planned_entry_time is invalid") from exc
    signal_date = _date8(result.get("signal_date"), field="signal_date")
    attached = 0
    for code, analysis in analyses_by_code.items():
        if not isinstance(analysis, dict):
            raise EventDataIntegrityError(f"AI explanation for {code} must be an object")
        evidence_text = str(analysis.get("ai_evidence_time") or "")
        try:
            evidence_time = datetime.fromisoformat(evidence_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EventDataIntegrityError(f"AI evidence time is invalid for {code}") from exc
        if evidence_time.tzinfo is None or evidence_time.utcoffset() != timedelta(hours=8):
            raise EventDataIntegrityError("AI evidence time must use Asia/Shanghai +08:00")
        if evidence_time.strftime("%Y%m%d") < signal_date or evidence_time > planned_entry:
            raise EventDataIntegrityError("AI evidence must be generated after the signal date and no later than entry")
        risk_tags = analysis.get("ai_risk_tags")
        explanation = str(analysis.get("ai_explanation") or "").strip()
        if not isinstance(risk_tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in risk_tags):
            raise EventDataIntegrityError("AI risk tags must be a list of non-empty strings")
        if not explanation:
            raise EventDataIntegrityError("AI explanation text cannot be empty")
        candidate = by_code[str(code)]
        candidate["ai_evidence_time"] = evidence_time.isoformat()
        candidate["ai_risk_tags"] = list(risk_tags)
        candidate["ai_explanation"] = explanation
        candidate["rank_change"] = 0
        attached += 1
    after = [
        (str(row.get("ts_code")), int(row.get("rank") or 0), float(row.get("score") or 0.0))
        for row in candidates
    ]
    if before != after:
        raise EventDataIntegrityError("AI attachment changed quantitative membership or ranking")
    result["rank_change"] = 0
    result["ai_attachment_status"] = {
        "attached_count": attached,
        "candidate_count": len(candidates),
        "rank_or_membership_changed": False,
    }
    return result


__all__ = [
    "EventDataIntegrityError",
    "build_event_quality_drift_snapshot",
    "attach_ai_explanations",
    "FACTOR_WEIGHTS",
    "STRATEGY_ID",
    "STRATEGY_VERSION",
]
