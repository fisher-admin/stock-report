#!/usr/bin/env python3
"""Hard validation for the retained stock-strategy pipeline."""

from __future__ import annotations

import os

from orchestrator_common import (
    ALLOWED_REPO_OUTPUTS,
    EXPLICIT_LABELS,
    HEALTH_DIR,
    PUBLISHED_REPO,
    REQUIRED_AI_FIELDS,
    RETAINED_STRATEGIES,
    TARGET_STRATEGY,
    WORKING_DATA_JSON,
    WORKING_REPO,
    WORKING_STRATEGY_JSON,
    ai_field_counts,
    current_run_id,
    data_json_trade_date,
    find_strategy,
    git_status,
    has_explicit_labels,
    in_range,
    latest_common_trade_date,
    load_json,
    normalize_code,
    now_str,
    resolve_effective_trade_date,
    write_canonical_object,
    write_health_snapshot,
    write_json,
)
from ai_temporal_contract import is_same_day_ai_evidence

RECOMMENDATION_SYNC_PATH = HEALTH_DIR / "recommendation_db_sync.json"
DATA_PREPARATION_PATH = HEALTH_DIR / "data_preparation_run.json"
ANALYTICS_FILES = {
    "prebreakout_summary": "data/recommendation_analytics/prebreakout_summary.json",
    "prebreakout_recommendations": "data/recommendation_analytics/prebreakout_recommendations.json",
    "industry_heatmap": "data/recommendation_analytics/industry_heatmap.json",
    "market_industry_heatmap": "data/recommendation_analytics/market_industry_heatmap.json",
    "unified_decision_payload": "data/recommendation_analytics/unified_decision_payload.json",
    "latest": "data/recommendation_analytics/latest.json",
    "market_morning_brief": "data/recommendation_analytics/market_morning_brief_latest.json",
}
CORE_ANALYTICS_REQUIRED = {
    "prebreakout_summary",
    "prebreakout_recommendations",
    "industry_heatmap",
    "market_industry_heatmap",
    "latest",
}


def ai_temporal_eligible_count(items: list[dict], target_trade_date: str | None) -> int:
    """Count complete AI records only when their raw timing evidence is same-day."""
    return sum(
        1
        for item in items
        if all(item.get(field) not in (None, "", [], {}) for field in REQUIRED_AI_FIELDS)
        and is_same_day_ai_evidence(item, target_trade_date)
    )


def publication_ready(
    validation_ok: bool,
    ai_publish: dict,
    ai_complete: bool,
) -> bool:
    """Return pre-deployment readiness without waiting for Stage 6's receipt."""
    return bool(
        validation_ok
        and ai_publish.get("base_ok")
        and ai_publish.get("local_materialized")
        and ai_complete
    )


def check_numeric_ranges(items: list[dict]) -> dict[str, int]:
    counters = {
        "ai_score_0_100": 0,
        "industry_heat_score_0_100": 0,
        "news_sentiment_score_0_100": 0,
        "event_impact_score_-1_1": 0,
        "winner_rate_0_100": 0,
        "volume_ratio_non_negative": 0,
    }
    for item in items:
        if in_range(item.get("ai_score"), 0, 100):
            counters["ai_score_0_100"] += 1
        if in_range(item.get("industry_heat_score"), 0, 100):
            counters["industry_heat_score_0_100"] += 1
        if in_range(item.get("news_sentiment_score"), 0, 100):
            counters["news_sentiment_score_0_100"] += 1
        if in_range(item.get("event_impact_score"), -1, 1):
            counters["event_impact_score_-1_1"] += 1
        if in_range(item.get("winner_rate"), 0, 100):
            counters["winner_rate_0_100"] += 1
        if in_range(item.get("volume_ratio"), 0, 999):
            counters["volume_ratio_non_negative"] += 1
    return counters


def analytics_dates_check(repo_dir, expected_dates: dict[str, str]) -> dict:
    result = {
        "expected_dates": expected_dates,
        "files": {},
        "all_present": True,
        "all_match": True,
        "core_required": sorted(CORE_ANALYTICS_REQUIRED),
        "core_all_present": True,
        "core_all_match": True,
        "context_all_present": True,
        "context_all_match": True,
    }
    for name, rel in ANALYTICS_FILES.items():
        path = repo_dir / rel
        required = name in CORE_ANALYTICS_REQUIRED
        entry = {"path": str(path), "exists": path.exists(), "required_for_validation": required}
        if not path.exists():
            result["all_present"] = False
            result["all_match"] = False
            if required:
                result["core_all_present"] = False
                result["core_all_match"] = False
            else:
                result["context_all_present"] = False
                result["context_all_match"] = False
            result["files"][name] = entry
            continue
        try:
            doc = load_json(path)
        except Exception as exc:
            entry["error"] = str(exc)
            result["all_match"] = False
            if required:
                result["core_all_match"] = False
            else:
                result["context_all_match"] = False
            result["files"][name] = entry
            continue
        observed = None
        expected = expected_dates.get(name, "")
        if name in {"prebreakout_summary", "latest", "industry_heatmap"}:
            observed = str(doc.get("latest_recommend_date") or "")
        elif name == "market_industry_heatmap":
            observed = str(doc.get("latest_trade_date") or "")
        elif name == "market_morning_brief":
            observed = str(doc.get("trade_date") or "")
        elif name == "unified_decision_payload":
            observed = str(((doc.get("morning_brief") or {}).get("trade_date")) or "")
        elif name == "prebreakout_recommendations":
            observed = str(doc.get("generated_at") or "")
        entry["expected_trade_date"] = expected
        entry["observed_trade_date"] = observed
        if name == "prebreakout_recommendations":
            entry["matched"] = True
        else:
            entry["matched"] = observed == expected
            if not entry["matched"]:
                result["all_match"] = False
                if required:
                    result["core_all_match"] = False
                else:
                    result["context_all_match"] = False
        result["files"][name] = entry
    return result


def build_trade_date_contract(
    strategy_doc: dict[str, Any],
    data_doc: dict[str, Any],
    recommendation_sync: dict[str, Any],
    morning_doc: dict[str, Any],
    market_doc: dict[str, Any],
) -> dict[str, Any]:
    strategy_trade_date = str(strategy_doc.get("latest_trade_date") or "")
    market_latest_trade_date = str(market_doc.get("latest_trade_date") or "")
    morning_trade_date = str(morning_doc.get("trade_date") or "")
    morning_market_data_trade_date = str(morning_doc.get("market_data_trade_date") or "")
    data_trade_date = str(data_json_trade_date(data_doc) or "")
    latest_recommend_date = str(recommendation_sync.get("latest_recommend_date") or "")
    latest_price_date = str(recommendation_sync.get("latest_price_date") or "")
    requested_trade_date = str(os.environ.get("OPENCLAW_TARGET_TRADE_DATE") or "").strip()
    if len(requested_trade_date) != 8 or not requested_trade_date.isdigit():
        requested_trade_date = ""

    decision_trade_date = resolve_effective_trade_date(
        requested_trade_date,
        market_latest_trade_date,
        strategy_trade_date,
        data_trade_date,
        latest_recommend_date,
        latest_price_date,
        morning_trade_date,
        morning_market_data_trade_date,
    )

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add_block(code: str, field: str, expected: str, actual: str, message: str) -> None:
        blockers.append(
            {
                "code": code,
                "field": field,
                "expected": expected,
                "actual": actual,
                "message": message,
            }
        )

    def add_warning(code: str, field: str, expected: str, actual: str, message: str) -> None:
        warnings.append(
            {
                "code": code,
                "field": field,
                "expected": expected,
                "actual": actual,
                "message": message,
            }
        )

    if not decision_trade_date:
        add_block("MISSING_DECISION_TRADE_DATE", "decision_trade_date", "", "", "无法解析 decision_trade_date")
    if not market_latest_trade_date:
        add_block("MISSING_MARKET_TRADE_DATE", "market_latest_trade_date", decision_trade_date, "", "市场热力图缺少 latest_trade_date")
    if not strategy_trade_date:
        add_block("MISSING_STRATEGY_TRADE_DATE", "strategy_trade_date", decision_trade_date, "", "策略层缺少 latest_trade_date")
    if not data_trade_date:
        add_block("MISSING_DATA_JSON_TRADE_DATE", "data_json_trade_date", decision_trade_date, "", "data.json 缺少 trade_date")
    if not latest_recommend_date:
        add_block("MISSING_RECOMMEND_DATE", "latest_recommend_date", decision_trade_date, "", "推荐仓缺少 latest_recommend_date")
    if not latest_price_date:
        add_block("MISSING_PRICE_DATE", "latest_price_date", decision_trade_date, "", "推荐仓缺少 latest_price_date")

    if decision_trade_date:
        if market_latest_trade_date and market_latest_trade_date != decision_trade_date:
            add_block("MARKET_TRADE_DATE_MISMATCH", "market_latest_trade_date", decision_trade_date, market_latest_trade_date, "市场热力图日期与决策日不一致")
        if morning_trade_date and morning_trade_date != decision_trade_date:
            add_warning("MORNING_TRADE_DATE_STALE", "morning_trade_date", decision_trade_date, morning_trade_date, "晨判 trade_date 未跟随最新决策日，仅作上下文参考")
        if morning_market_data_trade_date and morning_market_data_trade_date != decision_trade_date:
            add_warning("MORNING_MARKET_DATA_STALE", "morning_market_data_trade_date", decision_trade_date, morning_market_data_trade_date, "晨判 market_data_trade_date 未跟随最新决策日，仅作上下文参考")
        if strategy_trade_date and strategy_trade_date != decision_trade_date:
            add_block("STRATEGY_TRADE_DATE_MISMATCH", "strategy_trade_date", decision_trade_date, strategy_trade_date, "策略层日期与决策日不一致")
        if data_trade_date and data_trade_date != decision_trade_date:
            add_block("DATA_JSON_TRADE_DATE_MISMATCH", "data_json_trade_date", decision_trade_date, data_trade_date, "data.json 日期与决策日不一致")
        if latest_recommend_date and latest_recommend_date < decision_trade_date:
            add_block("RECOMMENDATION_DATE_STALE", "latest_recommend_date", decision_trade_date, latest_recommend_date, "推荐仓仍停留在旧交易日")
        if latest_price_date and latest_price_date < decision_trade_date:
            add_block("PRICE_DATE_STALE", "latest_price_date", decision_trade_date, latest_price_date, "价格日期仍停留在旧交易日")

    return {
        "status": "pass" if not blockers else "block",
        "hard_blocking": bool(blockers),
        "passed": not blockers,
        "decision_trade_date": decision_trade_date,
        "checked_fields": {
            "requested_trade_date": requested_trade_date,
            "morning_trade_date": morning_trade_date,
            "morning_market_data_trade_date": morning_market_data_trade_date,
            "market_latest_trade_date": market_latest_trade_date,
            "strategy_trade_date": strategy_trade_date,
            "data_json_trade_date": data_trade_date,
            "latest_recommend_date": latest_recommend_date,
            "latest_price_date": latest_price_date,
        },
        "blockers": blockers,
        "warnings": warnings,
    }


def main() -> int:
    payload = {
        "generated_at": now_str(),
        "retained_strategies": RETAINED_STRATEGIES,
        "required_ai_fields": REQUIRED_AI_FIELDS,
        "explicit_labels": EXPLICIT_LABELS,
        "checks": {},
        "ok": False,
        "ai_complete": False,
        "publish_ready": False,
    }

    latest_common, latest_by_type = latest_common_trade_date()
    payload["checks"]["latest_common_trade_date"] = latest_common
    payload["checks"]["latest_cache_by_type"] = latest_by_type
    payload["checks"]["strategy_backtests_exists"] = WORKING_STRATEGY_JSON.exists()
    payload["checks"]["data_json_exists"] = WORKING_DATA_JSON.exists()

    if not WORKING_STRATEGY_JSON.exists() or not WORKING_DATA_JSON.exists():
        out = HEALTH_DIR / "validation_report.json"
        write_json(out, payload)
        print(f"[validate] wrote validation report: {out}")
        return 1

    strategy_doc = load_json(WORKING_STRATEGY_JSON)
    data_doc = load_json(WORKING_DATA_JSON)
    strategies = strategy_doc.get("strategies", [])
    strategy_ids = [item.get("id") for item in strategies]
    payload["checks"]["strategy_ids"] = strategy_ids
    payload["checks"]["retained_only"] = sorted(strategy_ids) == sorted(RETAINED_STRATEGIES)
    payload["checks"]["strategy_count"] = len(strategies)
    payload["checks"]["strategy_latest_trade_date"] = strategy_doc.get("latest_trade_date")
    payload["checks"]["data_json_trade_date"] = data_json_trade_date(data_doc)
    payload["checks"]["latest_trade_date_match"] = (
        latest_common is not None
        and str(strategy_doc.get("latest_trade_date") or "") == latest_common
        and data_json_trade_date(data_doc) == latest_common
    )

    top20_lengths = {item.get("id"): len(item.get("top20", [])) for item in strategies}
    payload["checks"]["top20_lengths"] = top20_lengths

    prebreakout = find_strategy(strategy_doc, TARGET_STRATEGY) or {}
    strategy_items = prebreakout.get("top20", [])
    data_items = data_doc.get("top20", [])
    payload["checks"]["prebreakout_top20_len"] = len(strategy_items)
    payload["checks"]["data_json_top20_len"] = len(data_items)
    payload["checks"]["codes_match_data_json"] = [
        normalize_code(item.get("code")) for item in strategy_items
    ] == [normalize_code(item.get("code")) for item in data_items]

    ai_counts = ai_field_counts(strategy_items, REQUIRED_AI_FIELDS)
    payload["checks"]["prebreakout_ai_field_counts"] = ai_counts
    payload["checks"]["explicit_label_coverage"] = sum(1 for item in strategy_items if has_explicit_labels(item))
    payload["checks"]["ai_temporal_eligible_count"] = ai_temporal_eligible_count(
        strategy_items,
        str(strategy_doc.get("latest_trade_date") or ""),
    )
    payload["checks"]["numeric_range_coverage"] = check_numeric_ranges(strategy_items)

    git = git_status(WORKING_REPO)
    payload["checks"]["git"] = git
    payload["checks"]["published_git"] = git_status(PUBLISHED_REPO) if PUBLISHED_REPO.exists() else None
    payload["checks"]["git_allowed_output_paths"] = sorted(ALLOWED_REPO_OUTPUTS)

    ai_publish_path = HEALTH_DIR / "ai_publish_readiness.json"
    ai_publish = load_json(ai_publish_path) if ai_publish_path.exists() else {}
    payload["checks"]["ai_publish"] = {
        "published": bool(ai_publish.get("published")),
        "local_materialized": bool(ai_publish.get("local_materialized")),
        "publish_mode": ai_publish.get("publish_mode"),
        "ai_complete": bool(ai_publish.get("ai_complete")),
        "base_ok": bool(ai_publish.get("base_ok")),
        "ai_status_counts": (ai_publish.get("checks") or {}).get("ai_status_counts"),
        "run_id": ((ai_publish.get("run") or {}).get("run_id")),
    }

    data_preparation = load_json(DATA_PREPARATION_PATH) if DATA_PREPARATION_PATH.exists() else {}
    payload["checks"]["data_preparation"] = {
        "exists": DATA_PREPARATION_PATH.exists(),
        "target_trade_date": data_preparation.get("target_trade_date"),
        "latest_common_trade_date": data_preparation.get("latest_common_trade_date"),
        "same_day_ready": data_preparation.get("same_day_ready"),
        "ok": data_preparation.get("ok"),
        "degraded": bool(data_preparation.get("degraded")),
        "cyq_perf_fallback": data_preparation.get("cyq_perf_fallback"),
        "trade_date_resolution": data_preparation.get("trade_date_resolution"),
        "run_id": ((data_preparation.get("run") or {}).get("run_id")),
    }

    recommendation_sync = load_json(RECOMMENDATION_SYNC_PATH) if RECOMMENDATION_SYNC_PATH.exists() else {}
    payload["checks"]["recommendation_db"] = {
        "exists": RECOMMENDATION_SYNC_PATH.exists(),
        "ok": bool(recommendation_sync.get("ok")),
        "latest_recommend_date": recommendation_sync.get("latest_recommend_date"),
        "latest_date_row_count": recommendation_sync.get("latest_date_row_count"),
        "total_rows": recommendation_sync.get("total_rows"),
        "unique_stock_count": recommendation_sync.get("unique_stock_count"),
        "db_path": recommendation_sync.get("db_path"),
        "run_id": ((recommendation_sync.get("run") or {}).get("run_id")),
    }
    expected_analytics_dates = {
        "prebreakout_summary": str(recommendation_sync.get("latest_recommend_date") or ""),
        "prebreakout_recommendations": str(recommendation_sync.get("latest_recommend_date") or ""),
        "industry_heatmap": str(recommendation_sync.get("latest_recommend_date") or ""),
        "market_industry_heatmap": str(strategy_doc.get("latest_trade_date") or ""),
        "unified_decision_payload": str((load_json(PUBLISHED_REPO / "data/recommendation_analytics/market_morning_brief_latest.json").get("trade_date") if (PUBLISHED_REPO / "data/recommendation_analytics/market_morning_brief_latest.json").exists() else "") or ""),
        "latest": str(recommendation_sync.get("latest_recommend_date") or ""),
        "market_morning_brief": str((load_json(PUBLISHED_REPO / "data/recommendation_analytics/market_morning_brief_latest.json").get("trade_date") if (PUBLISHED_REPO / "data/recommendation_analytics/market_morning_brief_latest.json").exists() else "") or ""),
    }
    payload["checks"]["analytics_dates"] = analytics_dates_check(PUBLISHED_REPO, expected_analytics_dates)
    morning_doc = load_json(PUBLISHED_REPO / "data/recommendation_analytics/market_morning_brief_latest.json") if (PUBLISHED_REPO / "data/recommendation_analytics/market_morning_brief_latest.json").exists() else {}
    market_doc = load_json(PUBLISHED_REPO / "data/recommendation_analytics/market_industry_heatmap.json") if (PUBLISHED_REPO / "data/recommendation_analytics/market_industry_heatmap.json").exists() else {}
    payload["checks"]["trade_date_contract"] = build_trade_date_contract(
        strategy_doc=strategy_doc,
        data_doc=data_doc,
        recommendation_sync=recommendation_sync,
        morning_doc=morning_doc,
        market_doc=market_doc,
    )

    required_fields_full = all(count == 20 for count in ai_counts.values())
    numeric_coverage = payload["checks"]["numeric_range_coverage"]
    payload["checks"]["hard_numeric_range_coverage"] = {
        "winner_rate_0_100": numeric_coverage["winner_rate_0_100"],
        "volume_ratio_non_negative": numeric_coverage["volume_ratio_non_negative"],
    }
    payload["checks"]["optional_numeric_range_coverage"] = {
        "ai_score_0_100": numeric_coverage["ai_score_0_100"],
        "industry_heat_score_0_100": numeric_coverage["industry_heat_score_0_100"],
        "news_sentiment_score_0_100": numeric_coverage["news_sentiment_score_0_100"],
        "event_impact_score_-1_1": numeric_coverage["event_impact_score_-1_1"],
    }
    numeric_full = all(count == 20 for count in payload["checks"]["hard_numeric_range_coverage"].values())
    # 按策略隔离（2026-06 加固，根治「一个策略产 0 条就毙掉整条当日发布」）：
    #   主线策略 prebreakout 的 top20 满 20 + 公开 data.json 20 条 = 主线就位，当日即可发布；
    #   附属策略（O2C / T1）缺失只记 incomplete_strategies 诚实公示，不再整体一票否决。
    per_strategy_ready = {sid: (top20_lengths.get(sid) == 20) for sid in RETAINED_STRATEGIES}
    payload["checks"]["per_strategy_ready"] = per_strategy_ready
    payload["checks"]["incomplete_strategies"] = [sid for sid, ready in per_strategy_ready.items() if not ready]
    target_strategy_ready = per_strategy_ready.get(TARGET_STRATEGY, False) and len(data_items) == 20
    # 全策略完整：仅供 ai_complete / publish 参考，不再用于 ok 门禁。
    top20_full = all(per_strategy_ready.values()) and len(data_items) == 20

    recommendation_db = payload["checks"]["recommendation_db"]
    strategy_trade_date = str(strategy_doc.get("latest_trade_date") or "")
    db_latest_recommend_date = str(recommendation_db.get("latest_recommend_date") or "")
    db_date_compatible = bool(db_latest_recommend_date) and db_latest_recommend_date >= strategy_trade_date
    payload["checks"]["recommendation_db"]["date_compatible_with_strategy"] = db_date_compatible

    data_preparation_ok = (
        payload["checks"]["data_preparation"]["exists"]
        and bool(payload["checks"]["data_preparation"]["ok"])
        and bool(payload["checks"]["data_preparation"]["same_day_ready"])
        and str(payload["checks"]["data_preparation"]["target_trade_date"] or "") == strategy_trade_date
    )
    payload["checks"]["data_preparation"]["date_aligned_with_strategy"] = data_preparation_ok

    trade_date = str(strategy_doc.get("latest_trade_date") or "")
    validation_run_id = current_run_id()
    orchestrator_run = load_json(HEALTH_DIR / "orchestrator_run.json") if (HEALTH_DIR / "orchestrator_run.json").exists() else {}
    orchestrator_run_id = str(os.environ.get("OPENCLAW_RUN_ID") or orchestrator_run.get("run_id") or "")
    data_preparation_run_id = str(payload["checks"]["data_preparation"]["run_id"] or "")
    ai_publish_run_id = str(payload["checks"]["ai_publish"]["run_id"] or "")
    recommendation_db_run_id = str(payload["checks"]["recommendation_db"]["run_id"] or "")
    payload["checks"]["run_lineage"] = {
        "validation_run_id": validation_run_id,
        "orchestrator_run_id": orchestrator_run_id,
        "data_preparation_run_id": data_preparation_run_id,
        "ai_publish_run_id": ai_publish_run_id,
        "recommendation_db_run_id": recommendation_db_run_id,
    }
    upstream_run_ids = {rid for rid in [orchestrator_run_id, data_preparation_run_id, recommendation_db_run_id] if rid}
    payload["checks"]["run_lineage"]["core_aligned"] = bool(orchestrator_run_id) and len(upstream_run_ids) == 1
    payload["checks"]["run_lineage"]["ai_publish_same_run"] = (not ai_publish_run_id) or ai_publish_run_id == orchestrator_run_id

    analytics_dates = payload["checks"]["analytics_dates"]
    if not payload["checks"]["trade_date_contract"]["passed"]:
        payload["failure_code"] = "TRADE_DATE_CONTRACT_BROKEN"
    payload["ok"] = (
        payload["checks"]["retained_only"]
        and payload["checks"]["latest_trade_date_match"]
        and target_strategy_ready
        and payload["checks"]["codes_match_data_json"]
        and numeric_full
        and data_preparation_ok
        and recommendation_db["ok"]
        and db_date_compatible
        and recommendation_db["latest_date_row_count"] == 20
        and analytics_dates["core_all_present"]
        and analytics_dates["core_all_match"]
        and payload["checks"]["trade_date_contract"]["passed"]
        and payload["checks"]["run_lineage"]["core_aligned"]
    )
    payload["ai_complete"] = (
        required_fields_full
        and payload["checks"]["explicit_label_coverage"] == 20
        and payload["checks"]["ai_temporal_eligible_count"] == 20
    )
    payload["publish_ready"] = publication_ready(
        payload["ok"],
        payload["checks"]["ai_publish"],
        payload["ai_complete"],
    )

    canonical_paths = {}
    if trade_date:
        canonical_paths["validation_summary"] = str(
            write_canonical_object(
                "validation_summary",
                trade_date,
                {
                    "ok": payload["ok"],
                    "ai_complete": payload["ai_complete"],
                    "publish_ready": payload["publish_ready"],
                    "retained_only": payload["checks"]["retained_only"],
                    "top20_lengths": payload["checks"]["top20_lengths"],
                    "ai_status_counts": payload["checks"]["ai_publish"]["ai_status_counts"],
                    "analytics_dates": payload["checks"]["analytics_dates"],
                },
            )
        )
    payload["canonical_objects"] = canonical_paths

    out = HEALTH_DIR / "validation_report.json"
    write_health_snapshot(out, payload, trade_date=trade_date or None)
    print(f"[validate] wrote validation report: {out}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
