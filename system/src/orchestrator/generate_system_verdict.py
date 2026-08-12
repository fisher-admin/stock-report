#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator_common import PUBLISHED_REPO, WORKSPACE, extract_yyyymmdd, resolve_effective_trade_date

LATEST_DIR = PUBLISHED_REPO / "data" / "latest"
HEALTH_DIR = WORKSPACE / "stock_data" / "03-working" / "health"

SYSTEM_VERDICT_OUT = LATEST_DIR / "system_verdict.json"
MARKET_STATE_JSON = LATEST_DIR / "market_state.json"
STRATEGY_STATE_JSON = LATEST_DIR / "strategy_state.json"
CANDIDATE_STATE_JSON = LATEST_DIR / "candidate_state.json"
REVIEW_STATE_JSON = LATEST_DIR / "review_state.json"
RESEARCH_STATE_JSON = LATEST_DIR / "research_state.json"

VALIDATION_JSON = HEALTH_DIR / "validation_report.json"
AI_PUBLISH_JSON = HEALTH_DIR / "ai_publish_readiness.json"
RECOMMENDATION_DB_JSON = HEALTH_DIR / "recommendation_db_sync.json"
ORCHESTRATOR_JSON = HEALTH_DIR / "orchestrator_run.json"


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_json_value(item) for key, item in value.items()}
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def first_strategy(strategy_state: dict[str, Any]) -> dict[str, Any]:
    rows = strategy_state.get("strategies") or []
    return rows[0] if rows else {}


def dominant_trade_date(values: list[Any]) -> str:
    dates = [extract_yyyymmdd(v) for v in values]
    dates = [d for d in dates if d]
    if not dates:
        return ""
    counts = Counter(dates)
    return sorted(counts.keys(), key=lambda d: (-counts[d], -int(d)))[0]


def collect_dates(
    market_state: dict[str, Any],
    strategy_state: dict[str, Any],
    candidate_state: dict[str, Any],
    review_state: dict[str, Any],
    validation: dict[str, Any],
    recommendation_db: dict[str, Any],
) -> dict[str, str]:
    candidates = candidate_state.get("candidates") or []
    price_trade_date = dominant_trade_date([row.get("current_price_trade_date") for row in candidates])

    validation_trade_date = extract_yyyymmdd(
        ((validation.get("run") or {}).get("trade_date"))
        or ((validation.get("checks") or {}).get("strategy_latest_trade_date"))
    )
    decision_trade_date = dominant_trade_date([
        strategy_state.get("latest_trade_date"),
        candidate_state.get("latest_trade_date"),
        review_state.get("latest_recommend_date"),
        recommendation_db.get("latest_recommend_date"),
        market_state.get("latest_trade_date"),
        (market_state.get("morning") or {}).get("trade_date"),
        validation_trade_date,
        price_trade_date,
    ]) or resolve_effective_trade_date(
        strategy_state.get("latest_trade_date"),
        candidate_state.get("latest_trade_date"),
        recommendation_db.get("latest_recommend_date"),
        review_state.get("latest_recommend_date"),
        market_state.get("latest_trade_date"),
        (market_state.get("morning") or {}).get("trade_date"),
        validation_trade_date,
        price_trade_date,
    )
    market_data_trade_date = dominant_trade_date([
        market_state.get("latest_trade_date"),
        (market_state.get("morning") or {}).get("market_data_trade_date"),
        decision_trade_date,
    ]) or resolve_effective_trade_date(
        market_state.get("latest_trade_date"),
        (market_state.get("morning") or {}).get("market_data_trade_date"),
        decision_trade_date,
    )

    return {
        "decision_trade_date": decision_trade_date,
        "market_data_trade_date": market_data_trade_date,
        "price_trade_date": price_trade_date,
        "generated_at": now_iso(),
        "strategy_trade_date": extract_yyyymmdd(strategy_state.get("latest_trade_date")),
        "candidate_trade_date": extract_yyyymmdd(candidate_state.get("latest_trade_date")),
        "review_trade_date": extract_yyyymmdd(
            review_state.get("latest_recommend_date") or recommendation_db.get("latest_recommend_date")
        ),
        "validation_trade_date": validation_trade_date,
    }


def blocker(code: str, field: str, expected: str, actual: str, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "field": field,
        "expected": expected,
        "actual": actual,
        "message": message,
    }


def build_date_contract(dates: dict[str, str]) -> dict[str, Any]:
    decision = dates["decision_trade_date"]
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    required_fields = [
        "decision_trade_date",
        "market_data_trade_date",
        "strategy_trade_date",
        "candidate_trade_date",
        "price_trade_date",
    ]
    for field in required_fields:
        if not dates.get(field):
            blockers.append(blocker(f"MISSING_{field.upper()}", field, decision, "", f"{field} 缺失"))

    if decision:
        for field in ["market_data_trade_date", "strategy_trade_date", "candidate_trade_date", "price_trade_date"]:
            actual = dates.get(field, "")
            if actual and actual != decision:
                blockers.append(blocker(
                    f"{field.upper()}_MISMATCH",
                    field,
                    decision,
                    actual,
                    f"{field} 与 decision_trade_date 不一致",
                ))

        validation_trade_date = dates.get("validation_trade_date", "")
        if validation_trade_date and validation_trade_date != decision:
            blockers.append(blocker(
                "VALIDATION_STALE",
                "validation_trade_date",
                decision,
                validation_trade_date,
                "validation_report 仍停留在旧交易日",
            ))

        review_trade_date = dates.get("review_trade_date", "")
        if review_trade_date and review_trade_date != decision:
            warnings.append(blocker(
                "REVIEW_STALE",
                "review_trade_date",
                decision,
                review_trade_date,
                "复盘层不在当前决策日，仅作为研究参考",
            ))

    return {
        "status": "pass" if not blockers else "block",
        "hard_blocking": bool(blockers),
        "passed": not blockers,
        "summary": "日期合同通过" if not blockers else "关键日期口径未闭环",
        "checked_fields": dates,
        "blockers": blockers,
        "warnings": warnings,
    }


def build_market_gate(market_state: dict[str, Any]) -> dict[str, Any]:
    morning = market_state.get("morning") or {}
    summary = market_state.get("market_summary") or {}
    regime = morning.get("regime") or summary.get("market_regime")
    risk_score = safe_int(morning.get("risk_score") or summary.get("risk_score"))
    midday_source = market_state.get("midday_source") or "missing"

    if not regime:
        status, passed, hard = "block", False, True
        text = "市场状态缺失，不能继续执行判断。"
    elif regime == "高风险" or (risk_score is not None and risk_score >= 75):
        status, passed, hard = "block", False, True
        text = "市场高风险，直接阻断执行。"
    elif regime == "谨慎" or (risk_score is not None and risk_score >= 55) or midday_source == "missing":
        status, passed, hard = "warn", True, False
        text = "市场可继续看，但只适合谨慎模式。"
    else:
        status, passed, hard = "pass", True, False
        text = "市场环境允许继续评估策略与候选。"

    return {
        "status": status,
        "hard_blocking": hard,
        "passed": passed,
        "downstream_allowed": passed,
        "summary": text,
        "evidence": {
            "regime": regime,
            "risk_score": risk_score,
            "midday_available": midday_source != "missing",
            "midday_source": midday_source,
            "focus_sectors": (morning.get("focus_sectors") or [])[:5],
        },
        "blockers": [] if status != "block" else [{"code": "MARKET_BLOCKED", "message": text}],
    }


def build_strategy_gate(strategy_state: dict[str, Any]) -> dict[str, Any]:
    row = first_strategy(strategy_state)
    activation = str(row.get("activation") or "")
    if not row:
        status, passed, hard = "block", False, True
        text = "公开策略卡缺失。"
    elif activation == "active":
        status, passed, hard = "pass", True, False
        text = "启动前夕已激活。"
    elif activation == "watch":
        status, passed, hard = "warn", True, False
        text = "启动前夕仍处于观察态。"
    else:
        status, passed, hard = "block", False, True
        text = "启动前夕未成立，策略层阻断执行。"

    return {
        "status": status,
        "hard_blocking": hard,
        "passed": passed,
        "downstream_allowed": passed,
        "summary": text,
        "evidence": {
            "strategy_id": row.get("strategy_id"),
            "strategy_name": row.get("strategy_name"),
            "activation": activation or "missing",
            "market_overlap_count": row.get("market_overlap_count"),
            "top20_count": row.get("top20_count"),
            "avg_quant_score": row.get("avg_quant_score"),
            "avg_ai_score": row.get("avg_ai_score"),
        },
        "blockers": [] if status != "block" else [{"code": "STRATEGY_BLOCKED", "message": text}],
    }


def derive_final_candidate_action(
    raw_role: str,
    freshness_status: str,
    market_status: str,
    strategy_status: str,
    regime: str = "",
    quant_score: float | None = None,
) -> str:
    role = raw_role or "watch"
    if freshness_status == "block":
        return "avoid" if role == "avoid" else "watch"
    if market_status == "block":
        return "avoid"
    if strategy_status == "block":
        return "avoid"
    if strategy_status == "warn" and role == "main":
        if regime == "中性" and quant_score is not None and quant_score > 85:
            return "conditional_long"
        return "watch"
    return role


def build_candidate_gate(candidate_state: dict[str, Any], gates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_rows = candidate_state.get("candidates") or []
    market_evidence = gates.get("market_gate", {}).get("evidence", {})
    regime = str(market_evidence.get("regime") or "")
    final_rows = []
    for row in raw_rows:
        quant_score = safe_float(row.get("score"))
        final_role = derive_final_candidate_action(
            str(row.get("role_type") or "watch"),
            gates["freshness_gate"]["status"],
            gates["market_gate"]["status"],
            gates["strategy_gate"]["status"],
            regime=regime,
            quant_score=quant_score,
        )
        final_rows.append({
            "code": row.get("code"),
            "name": row.get("name"),
            "raw_role_type": row.get("role_type"),
            "final_candidate_action": final_role,
            "ai_score": row.get("ai_score"),
            "quant_score": quant_score,
        })

    counts = Counter(row["final_candidate_action"] for row in final_rows)
    eligible_count = len(final_rows)

    if eligible_count == 0:
        status, passed, hard = "block", False, True
        text = "候选池为空，执行层阻断。"
    elif counts.get("main", 0) > 0:
        status, passed, hard = "pass", True, False
        text = "候选层已形成主攻名单。"
    elif counts.get("conditional_long", 0) > 0:
        status, passed, hard = "warn", True, False
        text = f"候选层有 {counts['conditional_long']} 只有条件主攻（quant_score>85，regime=中性），未形成无条件主攻。"
    else:
        status, passed, hard = "warn", True, False
        text = "候选层只有观察名单，没有形成主攻名单。"

    examples = sorted(
        final_rows,
        key=lambda row: (
            {"main": 0, "conditional_long": 1, "watch": 2, "avoid": 3}.get(row["final_candidate_action"], 9),
            -(safe_int(row.get("ai_score"), -999) or -999),
        ),
    )[:3]

    return (
        {
            "status": status,
            "hard_blocking": hard,
            "passed": passed,
            "downstream_allowed": passed,
            "summary": text,
            "evidence": {
                "candidate_count": eligible_count,
                "final_candidate_action_counts": {
                    "main": counts.get("main", 0),
                    "conditional_long": counts.get("conditional_long", 0),
                    "watch": counts.get("watch", 0),
                    "avoid": counts.get("avoid", 0),
                },
                "raw_candidate_role_counts": candidate_state.get("role_counts") or {},
                "eligible_count": eligible_count,
            },
            "blockers": [] if status != "block" else [{"code": "CANDIDATE_BLOCKED", "message": text}],
        },
        {
            "policy_version": "candidate_final_action.v2",
            "field_name": "final_candidate_action",
            "allowed_values": ["main", "conditional_long", "watch", "avoid"],
            "derivation_rule": "freshness_gate -> market_gate -> strategy_gate -> candidate_quality; 中性regime + quant_score>85 -> conditional_long",
            "summary": {
                "main": counts.get("main", 0),
                "conditional_long": counts.get("conditional_long", 0),
                "watch": counts.get("watch", 0),
                "avoid": counts.get("avoid", 0),
            },
            "examples": [
                {
                    "code": row.get("code"),
                    "name": row.get("name"),
                    "final_candidate_action": row.get("final_candidate_action"),
                    "reason": "当前闸门状态下的最终候选动作",
                }
                for row in examples
            ],
        },
    )


def derive_final_action(gates: dict[str, Any], candidate_execution: dict[str, Any]) -> dict[str, Any]:
    blocking = [name for name, gate in gates.items() if gate["status"] == "block"]
    warnings = [name for name, gate in gates.items() if gate["status"] == "warn"]
    passed = [name for name, gate in gates.items() if gate["status"] == "pass"]

    if "freshness_gate" in blocking:
        return {
            "action": "halt",
            "label": "暂缓执行",
            "summary": "先修复日期闭环，再谈今天是否执行。",
            "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
            "next_step": {
                "type": "repair_data_contract",
                "owner": "backend_state_pipeline",
                "message": "先补齐同交易日的 strategy/candidate/price 状态，再重新生成 verdict。",
            },
        }

    if "market_gate" in blocking:
        return {
            "action": "halt",
            "label": "暂缓执行",
            "summary": "市场环境本身不支持执行。",
            "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
            "next_step": {
                "type": "wait_market_reset",
                "owner": "trading_operator",
                "message": "等待市场风险回落或主线重建后再评估。",
            },
        }

    if "strategy_gate" in blocking or "candidate_gate" in blocking:
        return {
            "action": "observe_only",
            "label": "只观察",
            "summary": "市场未完全否决，但策略或候选层未形成可执行状态。",
            "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
            "next_step": {
                "type": "observe_only",
                "owner": "frontend_execution_layer",
                "message": "保留观察，不给主攻执行结论。",
            },
        }

    if "market_gate" in warnings:
        return {
            "action": "cautious_execute",
            "label": "谨慎执行",
            "summary": "市场可做，但只适合保守执行。",
            "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
            "next_step": {
                "type": "reduced_risk_execution",
                "owner": "frontend_execution_layer",
                "message": "缩仓、先看主线确认，再决定是否扩大执行。",
            },
        }

    if "strategy_gate" in warnings or "candidate_gate" in warnings:
        conditional_long_count = (candidate_execution.get("summary") or {}).get("conditional_long", 0)
        if conditional_long_count > 0:
            return {
                "action": "conditional_execute",
                "label": "有条件主攻",
                "summary": f"策略层未完全确认，但有 {conditional_long_count} 只高分候选（quant_score>85）在中性市场下可有条件执行。",
                "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
                "next_step": {
                    "type": "conditional_execute",
                    "owner": "frontend_execution_layer",
                    "message": "按有条件主攻名单（Conditional Long）缩仓执行，严格止损。",
                },
            }
        return {
            "action": "observe_only",
            "label": "只观察",
            "summary": "启动前夕还没形成足够强的执行确认。",
            "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
            "next_step": {
                "type": "observe_only",
                "owner": "frontend_execution_layer",
                "message": "先看观察名单，不给主攻。",
            },
        }

    if (candidate_execution.get("summary") or {}).get("main", 0) > 0:
        return {
            "action": "execute",
            "label": "可执行",
            "summary": "四大闸门通过，且已形成主攻名单。",
            "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
            "next_step": {
                "type": "execute_main_candidates",
                "owner": "frontend_execution_layer",
                "message": "按主攻名单进入执行层。",
            },
        }

    return {
        "action": "observe_only",
        "label": "只观察",
        "summary": "没有形成主攻名单，保持观察。",
        "derived_from": {"hard_blocking_gates": blocking, "warning_gates": warnings, "passed_gates": passed},
        "next_step": {
            "type": "observe_only",
            "owner": "frontend_execution_layer",
            "message": "等待候选层出现更强确认。",
        },
    }


def build_system_verdict(
    market_state: dict[str, Any] | None = None,
    strategy_state: dict[str, Any] | None = None,
    candidate_state: dict[str, Any] | None = None,
    review_state: dict[str, Any] | None = None,
    research_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_state = market_state or load_json(MARKET_STATE_JSON)
    strategy_state = strategy_state or load_json(STRATEGY_STATE_JSON)
    candidate_state = candidate_state or load_json(CANDIDATE_STATE_JSON)
    review_state = review_state or load_json(REVIEW_STATE_JSON)
    research_state = research_state or load_json(RESEARCH_STATE_JSON)

    validation = load_json(VALIDATION_JSON)
    ai_publish = load_json(AI_PUBLISH_JSON)
    recommendation_db = load_json(RECOMMENDATION_DB_JSON)
    orchestrator = load_json(ORCHESTRATOR_JSON)

    strategy = first_strategy(strategy_state)
    dates = collect_dates(market_state, strategy_state, candidate_state, review_state, validation, recommendation_db)
    date_contract = build_date_contract(dates)

    gates = {
        "freshness_gate": {
            "status": date_contract["status"],
            "hard_blocking": date_contract["hard_blocking"],
            "passed": date_contract["passed"],
            "downstream_allowed": date_contract["passed"],
            "summary": date_contract["summary"],
            "checked_fields": date_contract["checked_fields"],
            "blockers": date_contract["blockers"],
        },
        "market_gate": build_market_gate(market_state),
        "strategy_gate": build_strategy_gate(strategy_state),
    }
    candidate_gate, candidate_execution = build_candidate_gate(candidate_state, gates)
    gates["candidate_gate"] = candidate_gate

    final_action = derive_final_action(gates, candidate_execution)

    return {
        "schema_version": "system_verdict.v1",
        "generated_at": dates["generated_at"],
        "run": {
            "run_id": orchestrator.get("run_id") or ((validation.get("run") or {}).get("run_id")) or "manual",
            "started_at": orchestrator.get("started_at") or ((validation.get("run") or {}).get("started_at")),
            "producer": "generate_system_verdict.py",
        },
        "scope": {
            "mode": "single_public_strategy",
            "strategy_id": strategy.get("strategy_id") or strategy.get("id") or "prebreakout_v41",
            "strategy_name": strategy.get("strategy_name") or strategy.get("name") or "启动前夕 v4.3 对照",
        },
        "dates": {
            "decision_trade_date": dates["decision_trade_date"],
            "market_data_trade_date": dates["market_data_trade_date"],
            "price_trade_date": dates["price_trade_date"],
            "generated_at": dates["generated_at"],
        },
        "date_contract": date_contract,
        "gates": gates,
        "final_action": final_action,
        "candidate_execution": candidate_execution,
        "source_lineage": {
            "market_state": {
                "source_file": str(MARKET_STATE_JSON),
                "latest_trade_date": market_state.get("latest_trade_date"),
                "generated_at": market_state.get("generated_at"),
            },
            "strategy_state": {
                "source_file": str(STRATEGY_STATE_JSON),
                "latest_trade_date": strategy_state.get("latest_trade_date"),
                "generated_at": strategy_state.get("generated_at"),
            },
            "candidate_state": {
                "source_file": str(CANDIDATE_STATE_JSON),
                "latest_trade_date": candidate_state.get("latest_trade_date"),
                "generated_at": candidate_state.get("generated_at"),
            },
            "review_state": {
                "source_file": str(REVIEW_STATE_JSON),
                "latest_trade_date": review_state.get("latest_recommend_date"),
                "generated_at": review_state.get("generated_at"),
            },
            "research_state": {
                "source_file": str(RESEARCH_STATE_JSON),
                "generated_at": research_state.get("generated_at"),
            },
            "validation_report": {
                "source_file": str(VALIDATION_JSON),
                "ok": validation.get("ok"),
                "trade_date": dates["validation_trade_date"],
            },
            "ai_publish_readiness": {
                "source_file": str(AI_PUBLISH_JSON),
                "ok": ai_publish.get("ok"),
                "published": ai_publish.get("published"),
            },
            "recommendation_db_sync": {
                "source_file": str(RECOMMENDATION_DB_JSON),
                "ok": recommendation_db.get("ok"),
                "latest_recommend_date": recommendation_db.get("latest_recommend_date"),
                "latest_price_date": recommendation_db.get("latest_price_date"),
            },
        },
        "artifacts": {
            "market_state": "data/latest/market_state.json",
            "strategy_state": "data/latest/strategy_state.json",
            "candidate_state": "data/latest/candidate_state.json",
            "review_state": "data/latest/review_state.json",
            "research_state": "data/latest/research_state.json",
        },
    }


def write_system_verdict(
    market_state: dict[str, Any] | None = None,
    strategy_state: dict[str, Any] | None = None,
    candidate_state: dict[str, Any] | None = None,
    review_state: dict[str, Any] | None = None,
    research_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_system_verdict(
        market_state=market_state,
        strategy_state=strategy_state,
        candidate_state=candidate_state,
        review_state=review_state,
        research_state=research_state,
    )
    write_json(SYSTEM_VERDICT_OUT, payload)
    return payload


def main() -> int:
    verdict = write_system_verdict()
    print(json.dumps({
        "ok": True,
        "path": str(SYSTEM_VERDICT_OUT),
        "decision_trade_date": (verdict.get("dates") or {}).get("decision_trade_date"),
        "final_action": ((verdict.get("final_action") or {}).get("action")),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
