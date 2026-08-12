#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path(__file__).resolve().parents[3]))
).resolve()
PUBLIC_STOCK_ANALYZER = SCRIPT_DIR.parent / "stock_analyzer"
STOCK_ANALYZER = Path(
    os.environ.get(
        "STOCK_ANALYZER_DIR",
        str(PUBLIC_STOCK_ANALYZER if PUBLIC_STOCK_ANALYZER.exists() else WORKSPACE / "skills/stock-analyzer"),
    )
)
if str(STOCK_ANALYZER) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pipeline as pl
import prebreakout_recipe_engine as eng
from immutable_strategy_registry import (
    PREBREAKOUT_CONTROL_CONFIG_HASH,
    PREBREAKOUT_CONTROL_CONFIG_VERSION,
    PREBREAKOUT_CONTROL_ID,
)


CONTROL_STRATEGY_ID = PREBREAKOUT_CONTROL_ID
TOP15_STRATEGY_ID = "prebreakout_v43_top15"
BALANCED_STRATEGY_ID = "prebreakout_v44_balanced"
CONTROL_CONFIG_HASH = PREBREAKOUT_CONTROL_CONFIG_HASH
CONTROL_CONFIG_VERSION = PREBREAKOUT_CONTROL_CONFIG_VERSION
BALANCED_CATEGORY_NAMES = [
    "volatility_squeeze",
    "macd_early_strength",
    "volume_turnover_stability",
    "relative_strength_neutralized",
    "liquidity_risk_control",
]
TOP15_RULES = {"base_strategy": CONTROL_STRATEGY_ID, "max_names": 15, "industry_cap": 3}
BALANCED_RULES = {
    "max_names": 20,
    "minimum_listing_days": 60,
    "category_weights": {name: 0.2 for name in BALANCED_CATEGORY_NAMES},
    "factor_formula_version": "2026-08-11.1",
}
BANNED_CHIP_COLUMNS = {
    "chip_concentration",
    "chip_support",
    "winner_rate",
    "weight_avg",
    "cost_5pct",
    "cost_15pct",
    "cost_50pct",
    "cost_85pct",
    "cost_95pct",
}
REQUIRED_BALANCED_COLUMNS = {
    "trade_date",
    "ts_code",
    "name",
    "industry_name",
    "sw2021_l1_name",
    "open_qfq",
    "close_qfq",
    "high_qfq",
    "low_qfq",
    "macd_dif",
    "macd_dea",
    "volume_ratio",
    "turnover_rate",
    "ret_5d",
    "ret_20d",
    "amount",
    "circ_mv",
    "total_mv",
    "listing_days",
    "list_status",
    "is_st",
    "is_suspended",
    "universe_flag",
    "used_proxy",
    "completeness",
    "realized_vol_5d",
    "realized_vol_20d",
    "macd_hist_prev",
    "volume_cv_20",
    "turnover_cv_20",
    "amount_ma20",
    "max_abs_return_20",
}
TOP15_CONFIG_HASH = hashlib.sha256(
    json.dumps(TOP15_RULES, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:16]
BALANCED_CONFIG_HASH = hashlib.sha256(
    json.dumps(BALANCED_RULES, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:16]
STRATEGY_REGISTRY = {
    CONTROL_STRATEGY_ID: {
        "strategy_id": CONTROL_STRATEGY_ID,
        "strategy_version": f"4.3+{CONTROL_CONFIG_HASH}",
        "expected_config_hash": CONTROL_CONFIG_HASH,
        "expected_version": CONTROL_CONFIG_VERSION,
        "max_names": 20,
    },
    TOP15_STRATEGY_ID: {
        "strategy_id": TOP15_STRATEGY_ID,
        "strategy_version": f"1.0.0+{TOP15_CONFIG_HASH}",
        "config_hash": TOP15_CONFIG_HASH,
        "max_names": 15,
        "industry_cap": 3,
    },
    BALANCED_STRATEGY_ID: {
        "strategy_id": BALANCED_STRATEGY_ID,
        "strategy_version": f"1.0.0+{BALANCED_CONFIG_HASH}",
        "config_hash": BALANCED_CONFIG_HASH,
        "max_names": 20,
        "category_weights": {name: 0.2 for name in BALANCED_CATEGORY_NAMES},
    },
}


class ShortTrackInputError(RuntimeError):
    pass


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _next_planned_entry(trade_date: str, exchange_trade_dates: list[str]) -> str:
    base = str(trade_date)
    calendar = sorted({str(candidate) for candidate in exchange_trade_dates})
    if base not in calendar:
        raise ShortTrackInputError("signal date is absent from exchange calendar")
    for candidate in calendar:
        if candidate > base:
            return datetime.strptime(candidate, "%Y%m%d").strftime("%Y-%m-%dT09:30:00+08:00")
    raise ShortTrackInputError("no next exchange trade date available")


def _default_contract(
    *,
    strategy_id: str,
    strategy_version: str,
    trade_date: str,
    signal_cutoff: str,
    exchange_trade_dates: list[str],
    rows: list[dict[str, Any]],
    sources: list[str],
    config_hash: str,
) -> dict[str, Any]:
    try:
        cutoff = datetime.fromisoformat(str(signal_cutoff).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShortTrackInputError("signal_cutoff must be ISO-8601") from exc
    if (
        cutoff.tzinfo is None
        or cutoff.utcoffset() != timedelta(hours=8)
        or cutoff.strftime("%Y%m%d") != str(trade_date)
        or (cutoff.hour, cutoff.minute, cutoff.second) != (15, 0, 0)
    ):
        raise ShortTrackInputError("signal_cutoff must be T day 15:00:00+08:00")
    if not rows:
        raise ShortTrackInputError(f"{strategy_id} candidate snapshot is empty")
    planned_entry_time = _next_planned_entry(trade_date, exchange_trade_dates)
    input_hash = _stable_hash(rows)
    enriched_rows = []
    equal_weight = 1.0 / len(rows)
    downstream_fields = {
        "adjusted_action",
        "market_adjusted_action",
        "gate_adjusted_action",
        "final_action",
        "operation_advice",
        "publication_score",
        "strategy_weight",
        "consensus",
        "position_tier",
    }
    for idx, row in enumerate(rows, start=1):
        clean = {
            key: value
            for key, value in row.items()
            if not str(key).startswith("ai_") and key not in downstream_fields
        }
        source_industry = clean.get("source_industry_name") or clean.get("industry_name") or clean.get("industry")
        industry = str(clean.get("sw2021_l1_name") or clean.get("industry") or clean.get("industry_name") or "未知")
        clean["source_industry_name"] = str(source_industry or "")
        clean["industry_name"] = industry
        clean["sw2021_l1_name"] = industry
        source_rank = int(clean.get("rank_no") or clean.get("rank") or idx)
        enriched_rows.append(
            {
                **clean,
                "source_rank": source_rank,
                "rank_no": idx,
                "rank": idx,
                "quant_rank": idx,
                "industry": industry,
                "weight": equal_weight,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "signal_data_cutoff": signal_cutoff,
                "planned_entry_time": planned_entry_time,
                "holding_period_days": 5,
                "diagnostic_holding_period_days": [1, 3],
                "data_sources": sources,
                "used_proxy": False,
                "completeness_status": "complete",
                "settlement_status": "pending_settlement",
                "round_trip_cost": 0.003,
                "stress_round_trip_cost": 0.005,
                "benchmark": "all_a_tradable_equal_weight",
                "return_1d_net": None,
                "return_3d_net": None,
                "return_5d_net": None,
                "return_5d_stress": None,
                "rank_change": 0,
                "publish_mode": "observe_only",
                "ai_evidence_time": None,
                "ai_risk_tags": [],
                "ai_explanation": None,
                "ai_role": "explanation_and_risk_check_only",
            }
        )
    return {
        "artifact_type": "candidate_snapshot",
        "artifact_kind": "candidate_snapshot",
        "trade_date": trade_date,
        "signal_date": trade_date,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "signal_data_cutoff": signal_cutoff,
        "planned_entry_time": planned_entry_time,
        "holding_period_days": 5,
        "diagnostic_holding_period_days": [1, 3],
        "data_sources": sources,
        "used_proxy": False,
        "completeness_status": "complete",
        "round_trip_cost": 0.003,
        "stress_round_trip_cost": 0.005,
        "benchmark": "all_a_tradable_equal_weight",
        "settlement_status": "pending_settlement",
        "input_hash": input_hash,
        "config_hash": config_hash,
        "rank_change": 0,
        "publish_mode": "observe_only",
        "observe_only": True,
        "execution_authority": "observe_only_no_auto_order",
        "ai_policy": {
            "role": "explanation_and_risk_check_only",
            "may_change_rank": False,
            "may_add_candidate": False,
            "may_remove_candidate": False,
        },
        "candidates": enriched_rows,
        "rows": enriched_rows,
    }


def validate_control_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {
        "version": str(pl.PREBREAKOUT_CONFIG.get("version") or ""),
        "config_hash": eng.production_config_fingerprint(),
    }
    if str(config.get("version")) != CONTROL_CONFIG_VERSION:
        raise ShortTrackInputError("prebreakout_v43_control version drift detected")
    if str(config.get("config_hash")) != CONTROL_CONFIG_HASH:
        raise ShortTrackInputError("prebreakout_v43_control config hash drift detected")
    return config


def _health_has_proxy(health_payload: dict[str, Any] | None) -> bool:
    if not health_payload or "cyq_perf_proxy_derived" not in health_payload:
        raise ShortTrackInputError("CYQ health evidence is missing")
    if bool(health_payload.get("cyq_perf_proxy_derived")):
        return True
    serialized = json.dumps(health_payload, ensure_ascii=False, sort_keys=True, default=str).lower()
    if "proxy" in serialized and any(
        marker in serialized
        for marker in ("local_same_day", "proxy_derived\": true", "proxy forbidden", "fallback_proxy")
    ):
        return True
    errs = [str(item).lower() for item in (health_payload.get("quality_errors") or [])]
    return any("cyq proxy forbidden" in item for item in errs)


def _ordered_control_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rows) < 20:
        raise ShortTrackInputError("control strategy requires the complete published Top20")
    enriched = []
    for idx, row in enumerate(rows[:20], start=1):
        clone = dict(row)
        clone.setdefault("rank_no", clone.get("rank") or idx)
        clone.setdefault("rank", clone.get("rank_no"))
        if int(clone.get("rank_no") or 0) != idx:
            raise ShortTrackInputError("control Top20 input order/rank drift detected")
        enriched.append(clone)
    return enriched


def _full_ranked_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fallback_rank, row in enumerate(rows, start=1):
        clone = dict(row)
        code = str(clone.get("ts_code") or "")
        if not code or code in seen:
            raise ShortTrackInputError("ranked candidate pool contains missing or duplicate ts_code")
        seen.add(code)
        rank = int(clone.get("rank_no") or clone.get("rank") or fallback_rank)
        clone["rank_no"] = rank
        clone["rank"] = rank
        ranked.append(clone)
    return sorted(ranked, key=lambda row: (int(row["rank_no"]), str(row["ts_code"])))


def build_control_candidate_snapshot(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    signal_cutoff: str,
    exchange_trade_dates: list[str],
    health_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    validate_control_config()
    if _health_has_proxy(health_payload):
        raise ShortTrackInputError("control strategy refuses proxy CYQ health state")
    ordered = _ordered_control_rows(rows)
    return _default_contract(
        strategy_id=CONTROL_STRATEGY_ID,
        strategy_version=STRATEGY_REGISTRY[CONTROL_STRATEGY_ID]["strategy_version"],
        trade_date=trade_date,
        signal_cutoff=signal_cutoff,
        exchange_trade_dates=exchange_trade_dates,
        rows=ordered,
        sources=["prebreakout_snapshot", "health:data_preparation_run"],
        config_hash=CONTROL_CONFIG_HASH,
    )


def build_top15_candidate_snapshot(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    signal_cutoff: str,
    exchange_trade_dates: list[str],
    health_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_control_config()
    if _health_has_proxy(health_payload):
        raise ShortTrackInputError("top15 strategy refuses proxy CYQ health state")
    ordered = _full_ranked_rows(rows)
    chosen: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for row in ordered:
        industry = str(row.get("sw2021_l1_name") or row.get("industry_name") or "未知")
        if industry_counts.get(industry, 0) >= 3:
            continue
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        chosen.append(dict(row))
        if len(chosen) >= 15:
            break
    return _default_contract(
        strategy_id=TOP15_STRATEGY_ID,
        strategy_version=STRATEGY_REGISTRY[TOP15_STRATEGY_ID]["strategy_version"],
        trade_date=trade_date,
        signal_cutoff=signal_cutoff,
        exchange_trade_dates=exchange_trade_dates,
        rows=chosen,
        sources=["prebreakout_snapshot"],
        config_hash=TOP15_CONFIG_HASH,
    )


def _validated_research_input(frame: pd.DataFrame, *, name: str, required: set[str], trade_date: str) -> pd.DataFrame:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ShortTrackInputError(f"{name} missing columns: {missing}")
    out = frame.copy()
    out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    out = out[out["trade_date"] <= str(trade_date)].copy()
    if out.empty:
        raise ShortTrackInputError(f"{name} has no point-in-time rows through {trade_date}")
    if out["used_proxy"].fillna(True).astype(bool).any():
        raise ShortTrackInputError(f"{name} contains proxy data")
    if not (out["completeness"].astype(str) == "complete").all():
        raise ShortTrackInputError(f"{name} contains incomplete rows")
    if out.duplicated(["trade_date", "ts_code"]).any():
        raise ShortTrackInputError(f"{name} contains duplicate date/security rows")
    return out


def build_balanced_feature_frame(
    *,
    price_history: pd.DataFrame,
    daily_basic_history: pd.DataFrame,
    pit_universe: pd.DataFrame,
    trade_date: str,
) -> pd.DataFrame:
    """Build v4.4 features from at least 21 PIT/qfq observations per security.

    No CYQ/chip columns are consumed or emitted.  Rows with incomplete history or
    incomplete industry/listing state are omitted rather than proxy-filled.
    """
    date = str(trade_date)
    if len(date) != 8 or not date.isdigit():
        raise ShortTrackInputError("trade_date must be YYYYMMDD")
    prices = _validated_research_input(
        price_history,
        name="qfq price history",
        trade_date=date,
        required={
            "trade_date",
            "ts_code",
            "open_qfq",
            "high_qfq",
            "low_qfq",
            "close_qfq",
            "macd_dif",
            "macd_dea",
            "vol",
            "amount",
            "used_proxy",
            "completeness",
        },
    )
    basics = _validated_research_input(
        daily_basic_history,
        name="daily_basic history",
        trade_date=date,
        required={
            "trade_date",
            "ts_code",
            "turnover_rate",
            "circ_mv",
            "total_mv",
            "used_proxy",
            "completeness",
        },
    )
    universe = _validated_research_input(
        pit_universe,
        name="PIT universe",
        trade_date=date,
        required={
            "trade_date",
            "ts_code",
            "name",
            "industry_name",
            "sw2021_l1_name",
            "listing_days",
            "list_status_at_date",
            "is_st",
            "is_suspended",
            "universe_flag",
            "used_proxy",
            "completeness",
        },
    )
    universe = universe[universe["trade_date"] == date].copy()
    if universe.empty:
        raise ShortTrackInputError("PIT universe has no exact signal-date snapshot")
    price_groups = {code: group.sort_values("trade_date") for code, group in prices.groupby("ts_code", sort=True)}
    basic_groups = {code: group.sort_values("trade_date") for code, group in basics.groupby("ts_code", sort=True)}
    rows: list[dict[str, Any]] = []
    for _, state in universe.sort_values("ts_code").iterrows():
        code = str(state["ts_code"])
        price = price_groups.get(code)
        basic = basic_groups.get(code)
        if price is None or basic is None:
            continue
        price = price[price["trade_date"] <= date].tail(21).copy()
        basic = basic[basic["trade_date"] <= date].tail(21).copy()
        if len(price) < 21 or len(basic) < 21:
            continue
        if str(price.iloc[-1]["trade_date"]) != date or str(basic.iloc[-1]["trade_date"]) != date:
            continue
        numeric_price = ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "macd_dif", "macd_dea", "vol", "amount"]
        numeric_basic = ["turnover_rate", "circ_mv", "total_mv"]
        for column in numeric_price:
            price[column] = pd.to_numeric(price[column], errors="coerce")
        for column in numeric_basic:
            basic[column] = pd.to_numeric(basic[column], errors="coerce")
        if price[numeric_price].isna().any().any() or basic[numeric_basic].isna().any().any():
            continue
        close = price["close_qfq"].to_numpy(dtype=float)
        volume = price["vol"].to_numpy(dtype=float)
        amount = price["amount"].to_numpy(dtype=float)
        turnover = basic["turnover_rate"].to_numpy(dtype=float)
        if (
            not np.isfinite(close).all()
            or not np.isfinite(volume).all()
            or not np.isfinite(amount).all()
            or not np.isfinite(turnover).all()
            or (close <= 0).any()
            or np.mean(volume[-20:]) <= 0
            or np.mean(amount[-20:]) <= 0
            or np.mean(turnover[-20:]) <= 0
        ):
            continue
        returns = close[1:] / close[:-1] - 1.0
        current = price.iloc[-1]
        current_basic = basic.iloc[-1]
        industry = state.get("industry_name")
        sw_industry = state.get("sw2021_l1_name")
        if pd.isna(industry) or pd.isna(sw_industry) or not str(sw_industry).strip():
            continue
        rows.append(
            {
                "trade_date": date,
                "ts_code": code,
                "name": str(state["name"]),
                "industry_name": str(industry),
                "sw2021_l1_name": str(sw_industry),
                "open_qfq": float(current["open_qfq"]),
                "high_qfq": float(current["high_qfq"]),
                "low_qfq": float(current["low_qfq"]),
                "close_qfq": float(current["close_qfq"]),
                "macd_dif": float(current["macd_dif"]),
                "macd_dea": float(current["macd_dea"]),
                "macd_hist_prev": float(price.iloc[-2]["macd_dif"] - price.iloc[-2]["macd_dea"]),
                "volume_ratio": float(volume[-1] / np.mean(volume[-20:])),
                "turnover_rate": float(current_basic["turnover_rate"]),
                "ret_5d": float(close[-1] / close[-6] - 1.0),
                "ret_20d": float(close[-1] / close[-21] - 1.0),
                "realized_vol_5d": float(np.std(returns[-5:], ddof=0)),
                "realized_vol_20d": float(np.std(returns[-20:], ddof=0)),
                "volume_cv_20": float(np.std(volume[-20:], ddof=0) / np.mean(volume[-20:])),
                "turnover_cv_20": float(np.std(turnover[-20:], ddof=0) / np.mean(turnover[-20:])),
                "amount": float(current["amount"]),
                "amount_ma20": float(np.mean(amount[-20:])),
                "max_abs_return_20": float(np.max(np.abs(returns[-20:]))),
                "circ_mv": float(current_basic["circ_mv"]),
                "total_mv": float(current_basic["total_mv"]),
                "listing_days": int(state["listing_days"]),
                "list_status": str(state["list_status_at_date"]),
                "is_st": bool(state["is_st"]),
                "is_suspended": bool(state["is_suspended"]),
                "universe_flag": int(state["universe_flag"]),
                "used_proxy": False,
                "completeness": "complete",
            }
        )
    if not rows:
        raise ShortTrackInputError("no securities have complete 21-day PIT/qfq balanced inputs")
    return pd.DataFrame(rows).sort_values("ts_code").reset_index(drop=True)


def _require_balanced_frame(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    missing = sorted(REQUIRED_BALANCED_COLUMNS - set(frame.columns))
    if missing:
        raise ShortTrackInputError(f"balanced strategy missing required columns: {missing}")
    banned = sorted(
        col
        for col in frame.columns
        if col in BANNED_CHIP_COLUMNS
        or "chip" in col.lower()
        or "cyq" in col.lower()
        or col.startswith("cost_")
    )
    if banned:
        raise ShortTrackInputError(f"balanced strategy forbids chip/cyq fields: {banned}")
    out = frame.copy()
    dates = set(out["trade_date"].astype(str).str.replace("-", "", regex=False))
    if dates != {str(trade_date)}:
        raise ShortTrackInputError(f"balanced frame date mismatch: expected {trade_date}, got {sorted(dates)}")
    if out["used_proxy"].fillna(True).astype(bool).any():
        raise ShortTrackInputError("balanced strategy forbids proxy inputs")
    if not (out["completeness"].astype(str) == "complete").all():
        raise ShortTrackInputError("balanced strategy requires complete PIT inputs")
    if out[["list_status", "listing_days", "is_st", "is_suspended", "industry_name", "sw2021_l1_name"]].isna().any().any():
        raise ShortTrackInputError("balanced strategy requires PIT state columns")
    numeric_columns = sorted(
        (REQUIRED_BALANCED_COLUMNS - {"trade_date", "ts_code", "name", "industry_name", "sw2021_l1_name", "list_status", "completeness"})
        - {"is_st", "is_suspended", "used_proxy"}
    )
    for column in numeric_columns:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[numeric_columns].isna().any().any() or not np.isfinite(out[numeric_columns].to_numpy(dtype=float)).all():
        raise ShortTrackInputError("balanced strategy requires finite numeric PIT/qfq features")
    if (out[["open_qfq", "close_qfq", "high_qfq", "low_qfq"]] <= 0).any().any():
        raise ShortTrackInputError("balanced strategy requires positive qfq prices")
    out = out[
        (out["universe_flag"] > 0)
        & (out["list_status"].astype(str) == "L")
        & (~out["is_st"].astype(bool))
        & (~out["is_suspended"].astype(bool))
        & (out["listing_days"] >= BALANCED_RULES["minimum_listing_days"])
    ].copy()
    if len(out) < int(BALANCED_RULES["max_names"]):
        raise ShortTrackInputError("balanced strategy has fewer than 20 complete tradable candidates")
    return out


def _score_percentile(values: pd.Series, *, ascending: bool, ts_codes: pd.Series) -> pd.Series:
    ordered = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"), "ts_code": ts_codes.astype(str)})
    # ascending=False means a larger signal is better.  Sort from worst to best
    # before assigning 0..100 so factor direction cannot be silently inverted.
    ordered = ordered.sort_values(["value", "ts_code"], ascending=[not ascending, True]).reset_index()
    ordered["score"] = np.linspace(0.0, 100.0, len(ordered), endpoint=True)
    result = ordered.set_index("index")["score"].sort_index()
    return result


def _balanced_component_scores(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    ts_codes = df["ts_code"].astype(str)
    range_ratio = (pd.to_numeric(df["high_qfq"]) - pd.to_numeric(df["low_qfq"])) / pd.to_numeric(df["close_qfq"])
    vol_ratio = pd.to_numeric(df["realized_vol_5d"]) / pd.to_numeric(df["realized_vol_20d"]).clip(lower=1e-12)
    macd_hist = pd.to_numeric(df["macd_dif"]) - pd.to_numeric(df["macd_dea"])
    macd_signal = macd_hist - pd.to_numeric(df["macd_hist_prev"]) - macd_hist.abs() * 0.10
    stability_signal = -(
        pd.to_numeric(df["volume_cv_20"]) + pd.to_numeric(df["turnover_cv_20"])
    )

    rel_strength = (pd.to_numeric(df["ret_5d"]) + pd.to_numeric(df["ret_20d"])) / 2.0
    size_bucket = pd.qcut(pd.to_numeric(df["circ_mv"]).rank(method="first"), q=min(3, len(df)), duplicates="drop").astype(str)
    neutral_group = df["sw2021_l1_name"].astype(str) + "|" + size_bucket
    rel_strength_neutral = rel_strength - rel_strength.groupby(neutral_group).transform("mean")

    liquidity_signal = (
        np.log1p(pd.to_numeric(df["amount_ma20"]))
        + 0.25 * np.log1p(pd.to_numeric(df["circ_mv"]))
        - pd.to_numeric(df["max_abs_return_20"]) * 8.0
        - range_ratio * 5.0
    )

    scores = pd.DataFrame(index=df.index)
    scores["volatility_squeeze"] = _score_percentile(-vol_ratio, ascending=False, ts_codes=ts_codes)
    scores["macd_early_strength"] = _score_percentile(macd_signal, ascending=False, ts_codes=ts_codes)
    scores["volume_turnover_stability"] = _score_percentile(stability_signal, ascending=False, ts_codes=ts_codes)
    scores["relative_strength_neutralized"] = _score_percentile(rel_strength_neutral, ascending=False, ts_codes=ts_codes)
    scores["liquidity_risk_control"] = _score_percentile(liquidity_signal, ascending=False, ts_codes=ts_codes)
    return scores


def build_balanced_candidate_snapshot(
    frame: pd.DataFrame,
    *,
    trade_date: str,
    signal_cutoff: str,
    exchange_trade_dates: list[str],
) -> dict[str, Any]:
    df = _require_balanced_frame(frame, trade_date=trade_date)
    scores = _balanced_component_scores(df)
    for col in BALANCED_CATEGORY_NAMES:
        df[col] = scores[col]
    df["composite_score"] = scores.mean(axis=1)
    df = df.sort_values(["composite_score", "ts_code"], ascending=[False, True]).reset_index(drop=True)
    rows = []
    for _, row in df.head(int(BALANCED_RULES["max_names"])).iterrows():
        rows.append(
            {
                "ts_code": str(row["ts_code"]),
                "stock_code": str(row.get("stock_code") or str(row["ts_code"]).split(".")[0]),
                "name": str(row["name"]),
                "source_industry_name": str(row["industry_name"]),
                "industry_name": str(row["sw2021_l1_name"]),
                "sw2021_l1_name": str(row["sw2021_l1_name"]),
                "score": round(float(row["composite_score"]), 4),
                "factor_scores": {name: round(float(row[name]), 4) for name in BALANCED_CATEGORY_NAMES},
            }
        )
    payload = _default_contract(
        strategy_id=BALANCED_STRATEGY_ID,
        strategy_version=STRATEGY_REGISTRY[BALANCED_STRATEGY_ID]["strategy_version"],
        trade_date=trade_date,
        signal_cutoff=signal_cutoff,
        exchange_trade_dates=exchange_trade_dates,
        rows=rows,
        sources=["pit_market_snapshot"],
        config_hash=BALANCED_CONFIG_HASH,
    )
    payload["category_weights"] = STRATEGY_REGISTRY[BALANCED_STRATEGY_ID]["category_weights"]
    return payload


def build_short_track_candidate_snapshots(
    *,
    control_rows: list[dict[str, Any]],
    balanced_frame: pd.DataFrame,
    trade_date: str,
    signal_cutoff: str,
    exchange_trade_dates: list[str],
    health_payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    return {
        CONTROL_STRATEGY_ID: build_control_candidate_snapshot(
            control_rows,
            trade_date=trade_date,
            signal_cutoff=signal_cutoff,
            exchange_trade_dates=exchange_trade_dates,
            health_payload=health_payload,
        ),
        TOP15_STRATEGY_ID: build_top15_candidate_snapshot(
            control_rows,
            trade_date=trade_date,
            signal_cutoff=signal_cutoff,
            exchange_trade_dates=exchange_trade_dates,
            health_payload=health_payload,
        ),
        BALANCED_STRATEGY_ID: build_balanced_candidate_snapshot(
            balanced_frame,
            trade_date=trade_date,
            signal_cutoff=signal_cutoff,
            exchange_trade_dates=exchange_trade_dates,
        ),
    }


def write_candidate_snapshots(snapshots: dict[str, dict[str, Any]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for strategy_id, payload in snapshots.items():
        trade_date = str(payload.get("signal_date") or payload.get("trade_date") or "")
        if len(trade_date) != 8 or not trade_date.isdigit():
            raise ShortTrackInputError(f"{strategy_id} snapshot has invalid signal date")
        path = output_dir / f"{strategy_id}_{trade_date}_candidate_snapshot.json"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(output_dir))
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
        written.append(path)
    return written
