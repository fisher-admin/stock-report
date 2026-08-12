#!/usr/bin/env python3
"""Publish the audited short/event observation tracks to the GitHub Pages data contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


class PublicationContractError(RuntimeError):
    pass


SHORT_TRACK_SPECS: tuple[tuple[str, str, int], ...] = (
    ("prebreakout_v43_control", "v4.3 对照组", 20),
    ("prebreakout_v43_top15", "v4.3 Top15 行业约束组", 15),
    ("prebreakout_v44_balanced", "v4.4 五类等权组", 20),
)
EVENT_STRATEGY_ID = "event_quality_drift_v1"
RETURN_COLUMNS = (
    "next_day_return_pct",
    "cumulative_return_pct",
    "forward_return_1d",
    "forward_return_3d",
    "forward_return_5d",
)
EXPECTED_EXECUTION_AUTHORITY = "observe_only_no_auto_order"
DATE_RE = re.compile(r"_(\d{8})_candidate_snapshot\.json$")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationContractError(f"invalid JSON artifact: {path.name}") from exc
    if not isinstance(payload, dict):
        raise PublicationContractError(f"JSON artifact must be an object: {path.name}")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _snapshot_dates(daily_dir: Path, strategy_id: str) -> set[str]:
    dates: set[str] = set()
    for path in daily_dir.glob(f"{strategy_id}_*_candidate_snapshot.json"):
        match = DATE_RE.search(path.name)
        if match:
            dates.add(match.group(1))
    return dates


def _latest_common_short_track_date(daily_dir: Path) -> str:
    date_sets = [_snapshot_dates(daily_dir, strategy_id) for strategy_id, _, _ in SHORT_TRACK_SPECS]
    if any(not dates for dates in date_sets):
        missing = [
            strategy_id
            for (strategy_id, _, _), dates in zip(SHORT_TRACK_SPECS, date_sets)
            if not dates
        ]
        raise PublicationContractError(f"missing short-track snapshots: {', '.join(missing)}")
    common = set.intersection(*date_sets)
    if not common:
        raise PublicationContractError("short-track strategies do not share a common signal date")
    return max(common)


def _public_candidate(row: dict[str, Any]) -> dict[str, Any]:
    factor_scores = row.get("factor_scores") if isinstance(row.get("factor_scores"), dict) else {}
    risk_tags = row.get("ai_risk_tags") if isinstance(row.get("ai_risk_tags"), list) else []
    return {
        "rank": int(row.get("rank") or row.get("rank_no") or 0),
        "source_rank": row.get("source_rank"),
        "ts_code": str(row.get("ts_code") or ""),
        "stock_code": str(row.get("stock_code") or row.get("code") or ""),
        "name": str(row.get("name") or row.get("stock_name") or ""),
        "industry_name": str(row.get("industry_name") or row.get("industry") or "未知"),
        "score": row.get("score"),
        "close": row.get("close"),
        "change_pct": row.get("change_pct", row.get("change")),
        "weight": row.get("weight"),
        "factor_scores": factor_scores,
        "signal_data_cutoff": row.get("signal_data_cutoff"),
        "planned_entry_time": row.get("planned_entry_time"),
        "settlement_status": row.get("settlement_status"),
        "completeness_status": row.get("completeness_status"),
        "return_1d_net": row.get("return_1d_net"),
        "return_3d_net": row.get("return_3d_net"),
        "return_5d_net": row.get("return_5d_net"),
        "return_5d_stress": row.get("return_5d_stress"),
        "used_proxy": False,
        "rank_change": 0,
        "ai_role": row.get("ai_role") or "explanation_and_risk_check_only",
        "ai_risk_tags": [str(item) for item in risk_tags],
        "ai_explanation": row.get("ai_explanation"),
    }


def _validate_short_snapshot(
    snapshot: dict[str, Any],
    *,
    strategy_id: str,
    expected_count: int,
    trade_date: str,
) -> list[dict[str, Any]]:
    if snapshot.get("strategy_id") != strategy_id:
        raise PublicationContractError(f"strategy identity mismatch for {strategy_id}")
    actual_date = str(snapshot.get("trade_date") or snapshot.get("signal_date") or "")
    if actual_date != trade_date:
        raise PublicationContractError(f"signal date mismatch for {strategy_id}")
    if not str(snapshot.get("strategy_version") or ""):
        raise PublicationContractError(f"missing immutable version for {strategy_id}")
    if snapshot.get("used_proxy") is not False:
        raise PublicationContractError(f"proxy data is forbidden for {strategy_id}")
    if int(snapshot.get("rank_change") or 0) != 0:
        raise PublicationContractError(f"rank_change must be zero for {strategy_id}")
    if snapshot.get("execution_authority") != EXPECTED_EXECUTION_AUTHORITY:
        raise PublicationContractError(f"execution authority drift for {strategy_id}")
    rows = snapshot.get("candidates") or snapshot.get("rows") or []
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise PublicationContractError(
            f"{strategy_id} expected {expected_count} candidates, got {len(rows) if isinstance(rows, list) else 'invalid'}"
        )
    codes: list[str] = []
    industries: Counter[str] = Counter()
    for expected_rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise PublicationContractError(f"invalid candidate row for {strategy_id}")
        if row.get("used_proxy") is not False:
            raise PublicationContractError(f"proxy candidate entered {strategy_id}")
        if int(row.get("rank_change") or 0) != 0:
            raise PublicationContractError(f"candidate rank_change drift in {strategy_id}")
        rank = int(row.get("rank") or row.get("rank_no") or 0)
        if rank != expected_rank:
            raise PublicationContractError(f"non-deterministic ranks in {strategy_id}")
        code = str(row.get("ts_code") or row.get("stock_code") or row.get("code") or "")
        if not code:
            raise PublicationContractError(f"missing security code in {strategy_id}")
        codes.append(code)
        industries[str(row.get("industry_name") or row.get("industry") or "未知")] += 1
    if len(set(codes)) != len(codes):
        raise PublicationContractError(f"duplicate security in {strategy_id}")
    if strategy_id == "prebreakout_v43_top15" and industries and max(industries.values()) > 3:
        raise PublicationContractError("prebreakout_v43_top15 exceeds three stocks per industry")
    return rows


def _short_track_public_state(daily_dir: Path, trade_date: str) -> list[dict[str, Any]]:
    strategies: list[dict[str, Any]] = []
    for strategy_id, display_name, expected_count in SHORT_TRACK_SPECS:
        snapshot_path = daily_dir / f"{strategy_id}_{trade_date}_candidate_snapshot.json"
        tracking_path = daily_dir / f"{strategy_id}_{trade_date}_candidate_tracking.json"
        snapshot = _read_json(snapshot_path)
        tracking = _read_json(tracking_path)
        rows = _validate_short_snapshot(
            snapshot,
            strategy_id=strategy_id,
            expected_count=expected_count,
            trade_date=trade_date,
        )
        if tracking.get("strategy_id") != strategy_id:
            raise PublicationContractError(f"tracking strategy mismatch for {strategy_id}")
        if tracking.get("execution_authority") != EXPECTED_EXECUTION_AUTHORITY:
            raise PublicationContractError(f"tracking execution authority drift for {strategy_id}")
        if not str(tracking.get("operational_status") or "").startswith("healthy"):
            raise PublicationContractError(f"short-track operation is not healthy for {strategy_id}")
        evidence = tracking.get("effectiveness_evidence") or {}
        strategies.append(
            {
                "strategy_id": strategy_id,
                "display_name": display_name,
                "strategy_version": str(snapshot.get("strategy_version")),
                "role": "control" if strategy_id.endswith("control") else "shadow",
                "candidate_count": len(rows),
                "holding_period_days": int(snapshot.get("holding_period_days") or 5),
                "diagnostic_holding_period_days": snapshot.get("diagnostic_holding_period_days") or [1, 3],
                "round_trip_cost": snapshot.get("round_trip_cost"),
                "stress_round_trip_cost": snapshot.get("stress_round_trip_cost"),
                "benchmark": snapshot.get("benchmark"),
                "operational_status": tracking.get("operational_status"),
                "effectiveness_status": tracking.get("effectiveness_status") or "not_validated",
                "effectiveness_evidence": {
                    "validation_start_date": evidence.get("validation_start_date"),
                    "validation_through_date": evidence.get("validation_through_date"),
                    "sample_trade_days": int(evidence.get("sample_trade_days") or 0),
                    "expected_trade_days": int(evidence.get("expected_trade_days") or 0),
                    "settled_security_rows": int(evidence.get("settled_security_rows") or 0),
                    "failed_gates": [str(item) for item in (evidence.get("failed_gates") or [])],
                    "all_gates_pass": bool(evidence.get("all_gates_pass")),
                    "decision": evidence.get("decision") or "observe_only",
                },
                "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
                "candidates": [_public_candidate(row) for row in rows],
            }
        )
    return strategies


def _latest_event_state(event_daily: Path) -> dict[str, Any]:
    paths = sorted(event_daily.glob(f"{EVENT_STRATEGY_ID}_*_candidate_tracking.json"))
    if not paths:
        return {
            "strategy_id": EVENT_STRATEGY_ID,
            "operational_status": "not_run",
            "effectiveness_status": "not_validated",
            "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
            "candidate_count": 0,
        }
    payloads = [_read_json(path) for path in paths]
    payload = max(payloads, key=lambda item: str(item.get("signal_date") or ""))
    if payload.get("strategy_id") != EVENT_STRATEGY_ID:
        raise PublicationContractError("event strategy identity mismatch")
    if payload.get("execution_authority") != EXPECTED_EXECUTION_AUTHORITY:
        raise PublicationContractError("event strategy execution authority drift")
    if not str(payload.get("operational_status") or "").startswith("healthy"):
        raise PublicationContractError("event strategy operation is not healthy")
    return {
        "strategy_id": EVENT_STRATEGY_ID,
        "display_name": "公告事件质量漂移",
        "strategy_version": payload.get("strategy_version"),
        "signal_date": payload.get("signal_date"),
        "operational_status": payload.get("operational_status"),
        "effectiveness_status": payload.get("effectiveness_status") or "not_validated",
        "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
        "decision": payload.get("decision") or "observe_only",
        "new_announcement_event_count": int(payload.get("new_announcement_event_count") or 0),
        "eligible_event_count": int(payload.get("eligible_event_count") or 0),
        "candidate_count": int(payload.get("eligible_event_count") or 0),
        "valid_announcement_events": int(payload.get("valid_announcement_events") or 0),
        "sample_months": int(payload.get("sample_months") or 0),
        "sample_trade_days": int(payload.get("sample_trade_days") or 0),
        "revision_chain_complete": bool(payload.get("revision_chain_complete")),
        "evidence_scope": payload.get("evidence_scope") or "auxiliary_only",
        "rejection_reason": payload.get("rejection_reason"),
        "failed_gates": [str(item) for item in (payload.get("failed_gates") or [])],
    }


def _database_integrity(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise PublicationContractError("recommendation warehouse is missing")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise PublicationContractError(f"recommendation database quick_check failed: {quick_check}")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(recommendation_fact)").fetchall()
        }
        required = {"settlement_status", "used_proxy", "rank_change", "ai_effectiveness_eligible"}
        if not required.issubset(columns):
            raise PublicationContractError("recommendation warehouse contract columns are incomplete")
        total_rows = int(conn.execute("SELECT COUNT(*) FROM recommendation_fact").fetchone()[0])
        settlement_counts = {
            str(row[0] or "unknown"): int(row[1])
            for row in conn.execute(
                "SELECT settlement_status, COUNT(*) FROM recommendation_fact GROUP BY settlement_status"
            ).fetchall()
        }
        impossible = 0
        per_column: dict[str, int] = {}
        for column in RETURN_COLUMNS:
            if column not in columns:
                continue
            count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM recommendation_fact WHERE {column} IS NOT NULL AND {column} <= -99.0"
                ).fetchone()[0]
            )
            per_column[column] = count
            impossible += count
        proxy_count = int(conn.execute("SELECT COUNT(*) FROM recommendation_fact WHERE COALESCE(used_proxy, 0) != 0").fetchone()[0])
        rank_changed = int(conn.execute("SELECT COUNT(*) FROM recommendation_fact WHERE COALESCE(rank_change, 0) != 0").fetchone()[0])
        ai_eligible = int(conn.execute("SELECT COUNT(*) FROM recommendation_fact WHERE COALESCE(ai_effectiveness_eligible, 0) = 1").fetchone()[0])
        exclusions: dict[str, int] = {}
        if "ai_exclusion_reason" in columns:
            exclusions = {
                str(row[0] or "unspecified"): int(row[1])
                for row in conn.execute(
                    "SELECT ai_exclusion_reason, COUNT(*) FROM recommendation_fact WHERE COALESCE(ai_effectiveness_eligible, 0) = 0 GROUP BY ai_exclusion_reason"
                ).fetchall()
            }
    finally:
        conn.close()
    if impossible:
        raise PublicationContractError(f"impossible historical returns remain: {per_column}")
    if proxy_count:
        raise PublicationContractError(f"proxy recommendations remain in evaluation database: {proxy_count}")
    if rank_changed:
        raise PublicationContractError(f"AI/downstream rank changes remain: {rank_changed}")
    return {
        "database_check": "ok",
        "total_rows": total_rows,
        "settlement_counts": {
            "settled": settlement_counts.get("settled", 0),
            "pending_settlement": settlement_counts.get("pending_settlement", 0),
            "data_missing": settlement_counts.get("data_missing", 0),
        },
        "fake_or_impossible_return_count": impossible,
        "impossible_return_counts_by_field": per_column,
        "proxy_rows": proxy_count,
        "rank_changed_rows": rank_changed,
        "ai_effectiveness_eligible_rows": ai_eligible,
        "ai_exclusion_counts": exclusions,
    }


def _evaluation_document(
    generated_at: str,
    trade_date: str,
    strategies: list[dict[str, Any]],
    event_track: dict[str, Any],
    integrity: dict[str, Any],
) -> dict[str, Any]:
    strategy_entries: dict[str, Any] = {}
    for item in strategies:
        evidence = item.get("effectiveness_evidence") or {}
        status = item.get("effectiveness_status") or "not_validated"
        strategy_entries[item["strategy_id"]] = {
            "strategy_name": item["display_name"],
            "strategy_version": item["strategy_version"],
            "flow_status": item["operational_status"],
            "effectiveness_status": status,
            "sample_trade_days": int(evidence.get("sample_trade_days") or 0),
            "required_trade_days": 60,
            "metrics": None if status != "validated" else evidence.get("metrics"),
            "failed_gates": evidence.get("failed_gates") or [],
            "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
        }
    strategy_entries[EVENT_STRATEGY_ID] = {
        "strategy_name": event_track.get("display_name"),
        "strategy_version": event_track.get("strategy_version"),
        "flow_status": event_track.get("operational_status"),
        "effectiveness_status": event_track.get("effectiveness_status"),
        "sample_months": event_track.get("sample_months", 0),
        "valid_announcement_events": event_track.get("valid_announcement_events", 0),
        "required_months": 12,
        "required_announcement_events": 100,
        "metrics": None,
        "failed_gates": event_track.get("failed_gates") or [],
        "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
    }
    return {
        "contract_version": "evaluation_integrity_v2",
        "generated_at": generated_at,
        "trade_date": trade_date,
        "methodology": {
            "signal_timing": "T close after signal; T+1 open_qfq entry",
            "primary_holding_period_days": 5,
            "diagnostic_holding_period_days": [1, 3],
            "round_trip_cost": 0.003,
            "stress_round_trip_cost": 0.005,
            "benchmark": "all_a_tradable_equal_weight",
            "missing_price_policy": "null_with_pending_or_data_missing_status",
            "proxy_policy": "forbidden",
            "ai_policy": "same_day_evidence_only; rank_change=0",
        },
        "integrity": integrity,
        "historical_audit": {
            "status": "historical_control_only",
            "legacy_mixed_version_history_used_for_promotion": False,
            "note": "旧记录只保留审计与历史对照；三组新策略从 2026-08-11 起重新积累前瞻样本。",
        },
        "strategies": strategy_entries,
    }


def _enrich_registry(latest_dir: Path, generated_at: str, strategies: list[dict[str, Any]], event_track: dict[str, Any]) -> None:
    path = latest_dir / "strategy_registry.json"
    if not path.exists():
        return
    registry = _read_json(path)
    registry["generated_at"] = generated_at
    registry["legacy_source_aliases"] = {"prebreakout_v41": "prebreakout_v43_control"}
    registry["observation_strategy_ids"] = [item["strategy_id"] for item in strategies] + [EVENT_STRATEGY_ID]
    registry["observation_strategies"] = [
        {
            "strategy_id": item["strategy_id"],
            "strategy_name": item["display_name"],
            "strategy_version": item["strategy_version"],
            "status": item["effectiveness_status"],
            "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
        }
        for item in strategies
    ] + [
        {
            "strategy_id": EVENT_STRATEGY_ID,
            "strategy_name": event_track.get("display_name"),
            "strategy_version": event_track.get("strategy_version"),
            "status": event_track.get("effectiveness_status"),
            "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
        }
    ]
    _write_json_atomic(path, registry)


def build_dual_track_publication(
    *,
    workspace: Path,
    published_repo: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    published_repo = Path(published_repo).resolve()
    generated_at = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    short_daily = workspace / "stock_data/03-working/strategy_research/short_track/daily"
    event_daily = workspace / "stock_data/03-working/strategy_research/event_quality_drift_v1/daily"
    db_path = workspace / "stock_data/03-working/recommendation_warehouse/recommendations.db"
    latest_dir = published_repo / "data/latest"

    trade_date = _latest_common_short_track_date(short_daily)
    strategies = _short_track_public_state(short_daily, trade_date)
    event_track = _latest_event_state(event_daily)
    integrity = _database_integrity(db_path)
    flow_healthy = all(str(item.get("operational_status") or "").startswith("healthy") for item in strategies)
    flow_healthy = flow_healthy and str(event_track.get("operational_status") or "").startswith(("healthy", "not_run"))

    state = {
        "contract_version": "dual_track_v1",
        "generated_at": generated_at,
        "trade_date": trade_date,
        "title": "双轨策略观察与验证",
        "flow_status": "healthy" if flow_healthy else "failed",
        "effectiveness_status": "not_validated",
        "decision": "observe_only",
        "execution_authority": EXPECTED_EXECUTION_AUTHORITY,
        "honesty_banner": "流程运行正常不代表策略有效。三组短线与公告事件策略均处于前瞻观察，未接自动下单。",
        "ai_policy": {
            "role": "explanation_and_risk_check_only",
            "can_change_rank": False,
            "can_add_or_remove_candidates": False,
            "rank_change": 0,
            "historical_effect_evidence": "same_day_only",
        },
        "short_track_strategies": strategies,
        "event_track": event_track,
        "evaluation_integrity": integrity,
        "retired_strategies": [
            "greenfield_o2c_v1",
            "t1_factor_v1",
            "s3_intraday",
            "wts",
            "auction_chase",
            "simple_pead",
            "same_day_intraday_reversal",
        ],
        "promotion_rules": {
            "short_track": "至少 60 个新成熟交易日；扣费后绝对与超额收益均为正，风险调整收益≥0.5，最大回撤≤8%，连续三个20日窗口至少两个为正。",
            "event_track": "至少 12 个月与 100 个有效公告；2025、2026 两段绝对与超额收益均为正，最大回撤≤12%，并优于随机排序。",
            "concentration": "主要收益若由单一行业或少数股票贡献则不通过。",
        },
    }
    evaluation = _evaluation_document(generated_at, trade_date, strategies, event_track, integrity)

    _write_json_atomic(latest_dir / "prebreakout_shadow_watch.json", state)
    _write_json_atomic(latest_dir / "dual_track_state.json", state)
    _write_json_atomic(latest_dir / "strategy_evaluation.json", evaluation)
    # review_track_latest.json is a separately generated public aggregate.  Do not
    # delete it here: the boundary gate rejects raw rows while preserving the
    # already repaired combination-level review result.
    _enrich_registry(latest_dir, generated_at, strategies, event_track)
    return {
        "ok": True,
        "trade_date": trade_date,
        "flow_status": state["flow_status"],
        "effectiveness_status": state["effectiveness_status"],
        "strategy_counts": {item["strategy_id"]: item["candidate_count"] for item in strategies},
        "event_candidate_count": event_track.get("candidate_count", 0),
        "published_files": [
            "data/latest/prebreakout_shadow_watch.json",
            "data/latest/dual_track_state.json",
            "data/latest/strategy_evaluation.json",
        ],
    }


def main() -> int:
    stock_root = Path(os.environ.get("STOCK_SYSTEM_ROOT", str(Path.home() / ".openclaw")))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=os.environ.get("OPENCLAW_WORKSPACE_DIR", str(stock_root / "workspace")),
    )
    parser.add_argument(
        "--published-repo",
        default=os.environ.get("OPENCLAW_PUBLISHED_REPO", str(stock_root / "workspace/stock-report")),
    )
    args = parser.parse_args()
    result = build_dual_track_publication(
        workspace=Path(args.workspace),
        published_repo=Path(args.published_repo),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
