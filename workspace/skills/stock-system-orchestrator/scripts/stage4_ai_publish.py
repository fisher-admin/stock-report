#!/usr/bin/env python3
"""Stage 4: publish base outputs first, then incrementally backfill AI fields."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from python_env_bootstrap import bootstrap_openclaw_venv

ENV_BOOTSTRAP = bootstrap_openclaw_venv()

from orchestrator_common import (
    EXTENDED_FIELDS,
    HEALTH_DIR,
    PUBLISHED_DATA_JSON,
    PUBLISHED_REPO,
    PUBLISHED_STRATEGY_JSON,
    REQUIRED_AI_FIELDS,
    RETAINED_STRATEGIES,
    TARGET_STRATEGY,
    WORKING_DATA_JSON,
    WORKING_STRATEGY_JSON,
    WORKING_REPO,
    WORKSPACE,
    CACHE_DIR,
    ai_field_counts,
    data_json_trade_date,
    find_strategy,
    git_status,
    has_value,
    has_explicit_labels,
    latest_common_trade_date,
    load_json,
    merge_ai_map,
    normalize_code,
    now_str,
    top_industries,
    write_canonical_object,
    write_health_snapshot,
    write_json,
)


FACTOR_LABELS = {
    "macd_boll_resonance": "MACD+BOLL共振",
    "chip_concentration": "筹码集中度",
    "volume_price": "量价关系",
    "rsi_volume": "RSI放量验证",
    "chip_support": "筹码支撑",
    "momentum": "20日动量",
    "liquidity": "流动性",
    "volatility_health": "波动健康度",
    "kdj_position": "KDJ位置",
    "volatility_squeeze": "波动收敛",
    "macd_early_signal": "MACD早期信号",
    "volume_warmup": "温和放量",
}
O2C_FACTOR_LABELS = {
    "g_intraday_vwap_deviation": "VWAP偏离度",
    "g_volume_price_divergence": "量价背离度",
    "g_chip_pullback_support": "筹码回调支撑",
    "g_long_cost_concentration": "成本集中度",
    "g_close_strength_ratio": "收盘强度比",
    "g_intraday_range_expansion": "日内振幅扩张",
}
AI_ANALYSIS_DIR = WORKSPACE / "stock_data/03-working/ai_analysis"
GREENFIELD_TOP20_CANDIDATES = [
    WORKSPACE / "factor_factory/outputs/greenfield_multifactor/current_selection_snapshot.json",
    WORKING_REPO / "data/latest/greenfield_top20.json",
    PUBLISHED_REPO / "data/latest/greenfield_top20.json",
]
AI_STATUS_PRIORITY = {"pending": 0, "partial": 1, "ready": 2}
INDEX_NAME_MARKERS = ["指数", "沪深", "中证", "创业板", "科创", "A50", "金龙"]
FAILED_AI_RESET_FIELDS = sorted(
    set(
        EXTENDED_FIELDS
        + REQUIRED_AI_FIELDS
        + [
            "ai_analysis_date",
            "ai_evidence_time",
            "ai_missing_fields",
            "ai_provenance",
            "ai_temporal_class",
            "ai_coverage_counted",
            "ai_source_date",
            "ai_source_kind",
            "ai_source_name",
            "ai_status",
        ]
    )
)
from orchestrator_common import PREFERRED_PYTHON
from ai_temporal_contract import classify_ai_temporal_evidence, is_same_day_ai_evidence
# Order matters: strategy_publication_layer materializes canonical strategy/latest
# payloads and must run before unified/latest state generators. Running it last can
# overwrite fresh same-day strategy data with stale run_manifest-derived dates.
ANALYTICS_REFRESH_SCRIPTS = [
    Path(__file__).resolve().parent / "strategy_publication_layer.py",
    Path(__file__).resolve().parent / "generate_market_industry_heatmap.py",
    Path(__file__).resolve().parent / "generate_unified_decision_payload.py",
    Path(__file__).resolve().parent / "generate_latest_states.py",
]
ANALYTICS_TRACKED_FILES = [
    "data/recommendation_analytics/market_industry_heatmap.json",
    "data/recommendation_analytics/unified_decision_payload.json",
    "data/recommendation_analytics/midday_analysis_latest.json",
    "data/recommendation_analytics/o2c_ai_analysis.json",
    "data/recommendation_analytics/o2c_greenfield_ai_analysis.json",
    "data/recommendation_analytics/t1_ai_analysis.json",
    "data/recommendation_analytics/t1_alpha191_ai_analysis.json",
    "data/recommendation_analytics/t1_factor_recommendations.json",
    "data/latest/run_manifest.json",
    "data/latest/market_state.json",
    "data/latest/strategy_state.json",
    "data/latest/candidate_state.json",
    "data/latest/review_state.json",
    "data/latest/review_state_o2c.json",
    "data/latest/research_state.json",
    "data/latest/research_state_t1.json",
    "data/latest/system_verdict.json",
    "data/latest/greenfield_top20.json",
    "data/latest/combined_recommendation.json",
    "data/latest/combined_recommendation.md",
    "data/latest/decision_state.json",
    "data/latest/execution_state.json",
    "data/latest/market_context.json",
    "data/latest/strategy_registry.json",
    "data/latest/strategy_run_state.json",
    "data/latest/recommendation_state.json",
    "data/latest/review_state_unified.json",
    "data/latest/adjustment_log.json",
    "data/latest/system_health.json",
    "decision-candidates.html",
]


def compose_text(parts: list[str]) -> str:
    return "\n".join([part.strip() for part in parts if isinstance(part, str) and part.strip()])


def current_data_source_label(base: dict[str, Any]) -> str:
    """Prefer the provider recorded by Stage 1 over legacy Tushare text."""
    existing = str(base.get("data_source") or "").strip()
    if existing and existing.lower() not in {"tushare", "tushare pro", "tushare pro (试用接口)"}:
        return existing
    prep_path = HEALTH_DIR / "data_preparation_run.json"
    prep = load_json(prep_path) if prep_path.exists() else {}
    providers = {
        str((info or {}).get("provider") or "").strip().lower()
        for info in (prep.get("refreshed") or {}).values()
        if isinstance(info, dict)
    }
    providers.discard("")
    if "rds" in providers and "promax" in providers:
        return "RDS主源 / Promax备用"
    if "rds" in providers:
        return "RDS主源"
    if "promax" in providers:
        return "Promax主源"
    return existing or "数据源状态未知"


def build_historical_ai_card(entry: dict) -> tuple[str, str]:
    dashboard = entry.get("dashboard") or {}
    intelligence = dashboard.get("intelligence") or {}
    battle = dashboard.get("battle_plan") or {}
    sniper = battle.get("sniper_points") or {}
    chip = (dashboard.get("data_perspective") or {}).get("chip_structure") or {}

    ai_summary = compose_text(
        [
            (dashboard.get("core_conclusion") or {}).get("one_sentence"),
            entry.get("analysis_summary"),
            entry.get("fundamental_analysis"),
        ]
    )
    ai_points = compose_text(
        [
            f"板块定位：{entry.get('sector_position') or entry.get('company_highlights') or '历史AI未提供板块相对强弱'}",
            (
                "筹码判断："
                f"{chip.get('chip_health') or '历史AI未提供完整筹码分布'}；"
                f"均价参考 {chip.get('avg_cost') or entry.get('current_price') or '待补充'}；"
                f"获利盘参考 {chip.get('profit_ratio') or '待补充'}"
            ),
            f"事件催化：{compose_text([intelligence.get('latest_news'), entry.get('news_summary')]) or '待补充'}",
            f"触发条件：{sniper.get('ideal_buy') or entry.get('buy_reason') or '等待当日AI补齐'}",
            f"失效条件：{sniper.get('stop_loss') or entry.get('risk_warning') or '待补充'}",
        ]
    )
    return ai_summary, ai_points


def looks_like_index_entry(entry: dict) -> bool:
    name = str(entry.get("name") or entry.get("stock_name") or "").strip()
    code = str(entry.get("code") or "").strip()
    if any(marker in name for marker in INDEX_NAME_MARKERS):
        return True
    return code.startswith("sh") or code.startswith("sz")


def _normalized_ai_date(value: Any) -> str | None:
    text = str(value or "").strip().replace("-", "")
    date = text[:8]
    return date if len(date) == 8 and date.isdigit() else None


def ai_entry_has_same_day_evidence(entry: dict, target_trade_date: str | None) -> bool:
    """A filename/source label cannot disguise analysis generated after recommendation day."""
    return is_same_day_ai_evidence(entry, target_trade_date)


def historical_ai_entry_to_fields(entry: dict, source_date: str) -> dict:
    legacy_summary, legacy_points = build_historical_ai_card(entry)
    ai_summary = entry.get("ai_summary") if has_value(entry.get("ai_summary")) else legacy_summary
    ai_points = entry.get("ai_points") if has_value(entry.get("ai_points")) else legacy_points
    # 历史 AI 缺真实情绪分时不再硬填 50（伪装成中性真实分）；置 None，
    # 下游合并全程用 has_value 守卫，会自动跳过、不计入已分析、如实显示缺失。
    score = entry.get("ai_score") if has_value(entry.get("ai_score")) else entry.get("sentiment_score")
    effective_source_date = str(entry.get("ai_source_date") or entry.get("ai_analysis_date") or source_date)

    return {
        "code": normalize_code(entry.get("code")),
        "ai_score": score,
        "ai_advice": entry.get("ai_advice") or entry.get("operation_advice"),
        "ai_decision": entry.get("ai_decision") or entry.get("decision_type"),
        "ai_confidence": entry.get("ai_confidence") or entry.get("confidence_level"),
        "ai_summary": ai_summary,
        "ai_conclusion": entry.get("ai_conclusion") or entry.get("analysis_summary") or ai_summary,
        "ai_points": ai_points,
        "ai_risks": entry.get("ai_risks"),
        "ai_trend": entry.get("trend_analysis") or entry.get("technical_analysis"),
        "ai_ma": entry.get("ma_analysis"),
        "ai_volume": entry.get("volume_analysis"),
        "ai_fundamental": entry.get("fundamental_analysis"),
        "ai_risk_warning": entry.get("risk_warning"),
        "ai_news": entry.get("news_summary"),
        "industry_name": entry.get("hot_topics"),
        "news_sentiment_score": entry.get("sentiment_score"),
        "ai_source_kind": entry.get("ai_source_kind") or "historical_ai_analysis",
        "ai_source_name": entry.get("ai_source_name") or entry.get("ai_source_kind") or "historical_ai_analysis",
        "ai_source_date": effective_source_date,
        "ai_source_stale": effective_source_date != source_date,
        "ai_evidence_time": entry.get("ai_evidence_time") or entry.get("generated_at") or entry.get("created_at"),
        "ai_temporal_class": classify_ai_temporal_evidence(entry, source_date),
        "ai_coverage_counted": ai_entry_has_same_day_evidence(entry, source_date),
        "ai_risk_tags": entry.get("ai_risk_tags") or entry.get("ai_risks") or [],
        "rank_change": 0,
    }


def load_historical_ai_map_for_trade_date(trade_date: str | None) -> tuple[dict[str, dict], dict]:
    merged_map: dict[str, dict] = {}
    summary = {
        "latest_date": trade_date,
        "item_count": 0,
        "eligible_item_count": 0,
        "excluded_future_or_undated": 0,
        "path": None,
        "exists": False,
    }

    if not AI_ANALYSIS_DIR.exists() or not trade_date:
        return merged_map, summary

    path = AI_ANALYSIS_DIR / f"{trade_date}.json"
    summary["path"] = str(path)
    if not path.exists():
        return merged_map, summary

    summary["exists"] = True
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return merged_map, summary
    if not isinstance(entries, list):
        return merged_map, summary

    summary["item_count"] = len(entries)
    for entry in entries:
        if not isinstance(entry, dict) or not ai_entry_has_same_day_evidence(entry, trade_date):
            summary["excluded_future_or_undated"] += 1
            continue
        if looks_like_index_entry(entry):
            continue
        code = normalize_code(entry.get("code"))
        if not code:
            continue
        merged_map[code] = historical_ai_entry_to_fields(entry, trade_date)
        summary["eligible_item_count"] += 1

    return merged_map, summary


def load_ai_temporal_map_for_trade_date(trade_date: str | None) -> dict[str, str]:
    """Keep excluded raw records visible as delayed/unknown terminal evidence."""
    if not trade_date:
        return {}
    path = AI_ANALYSIS_DIR / f"{trade_date}.json"
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(entries, list):
        return {}
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = normalize_code(entry.get("code"))
        if not code:
            continue
        temporal = classify_ai_temporal_evidence(entry, trade_date)
        if temporal != "fresh_same_day":
            result[code] = temporal
    return result


def filter_same_day_ai_items(items: list[dict], target_trade_date: str | None) -> list[dict]:
    if not target_trade_date:
        return list(items or [])
    filtered: list[dict] = []
    for item in items or []:
        if ai_entry_has_same_day_evidence(item, target_trade_date):
            filtered.append(item)
    return filtered


def load_candidate_sources(target_trade_date: str | None) -> tuple[dict[str, dict], list[dict]]:
    merged_map: dict[str, dict] = {}
    sources: list[dict] = []

    def add_source(name: str, items: list[dict], trade_date: str | None, *, merge_enabled: bool) -> None:
        valid = bool(items) and trade_date == target_trade_date
        tagged_items = []
        for item in items:
            row = dict(item)
            row.setdefault("ai_source_name", name)
            row.setdefault("ai_source_date", trade_date or "")
            row.setdefault("ai_source_kind", name)
            tagged_items.append(row)
        sources.append(
            {
                "name": name,
                "trade_date": trade_date,
                "matched_target_trade_date": valid,
                "item_count": len(items),
                "merge_enabled": merge_enabled,
            }
        )
        if valid and merge_enabled:
            merge_ai_map(merged_map, tagged_items)

    historical_map, historical_summary = load_historical_ai_map_for_trade_date(target_trade_date)
    historical_valid = bool(historical_summary.get("exists")) and historical_summary.get("latest_date") == target_trade_date
    if historical_map:
        sources.append(
            {
                "name": "historical_ai_analysis",
                "trade_date": historical_summary["latest_date"],
                "matched_target_trade_date": historical_valid,
                "item_count": historical_summary["item_count"],
                "stale_allowed": False,
                "merge_enabled": historical_valid,
            }
        )
        if historical_valid:
            for code, item in historical_map.items():
                merged_map[code] = {
                    key: value
                    for key, value in item.items()
                    if key == "code" or key in EXTENDED_FIELDS or key.startswith("ai_source_")
                }

    if WORKING_DATA_JSON.exists():
        working_data = load_json(WORKING_DATA_JSON)
        working_items = filter_mergeable_ai_items(
            filter_same_day_ai_items(working_data.get("top20", []), target_trade_date)
        )
        add_source("working_data_json", working_items, data_json_trade_date(working_data), merge_enabled=True)

    if PUBLISHED_STRATEGY_JSON.exists():
        published_strategy = load_json(PUBLISHED_STRATEGY_JSON)
        published_target = find_strategy(published_strategy, TARGET_STRATEGY) or {}
        add_source(
            "published_strategy_backtests",
            published_target.get("top20", []),
            str(published_strategy.get("latest_trade_date") or ""),
            merge_enabled=False,
        )

    if PUBLISHED_DATA_JSON.exists():
        published_data = load_json(PUBLISHED_DATA_JSON)
        add_source("published_data_json", published_data.get("top20", []), data_json_trade_date(published_data), merge_enabled=False)

    return merged_map, sources


def load_o2c_ai_map(target_trade_date: str | None) -> dict[str, dict]:
    if not target_trade_date or not AI_ANALYSIS_DIR.exists():
        return {}
    paths = [
        AI_ANALYSIS_DIR / f"{target_trade_date}.json",
        AI_ANALYSIS_DIR / f"{target_trade_date}_o2c_greenfield.json",
        AI_ANALYSIS_DIR / f"{target_trade_date}_factor_combined.json",
    ]

    mapped: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not ai_entry_has_same_day_evidence(entry, target_trade_date):
                continue
            normalized_source = entry.get("strategy_source") or ({"o2c_greenfield": "o2c_factor"}.get(str(entry.get("source")), "traditional"))
            if normalized_source != "o2c_factor":
                continue
            code = normalize_code(entry.get("code") or entry.get("ts_code"))
            if not code:
                continue
            row = {
                key: value
                for key, value in entry.items()
                if key in EXTENDED_FIELDS or key.startswith("ai_")
            }
            dashboard = entry.get("dashboard") or {}
            core = dashboard.get("core_conclusion") or {}
            if has_value(core.get("sentiment_score")) and not has_value(row.get("ai_score")):
                row["ai_score"] = core.get("sentiment_score")
            if has_value(entry.get("sentiment_score")) and not has_value(row.get("ai_score")):
                row["ai_score"] = entry.get("sentiment_score")
            if has_value(entry.get("operation_advice")) and not has_value(row.get("ai_advice")):
                row["ai_advice"] = entry.get("operation_advice")
            if has_value(core.get("one_sentence")) and not has_value(row.get("ai_summary")):
                row["ai_summary"] = core.get("one_sentence")
            if has_value(entry.get("analysis_summary")) and not has_value(row.get("ai_summary")):
                row["ai_summary"] = entry.get("analysis_summary")
            if has_value(entry.get("risk_warning")) and not has_value(row.get("ai_risk_warning")):
                row["ai_risk_warning"] = entry.get("risk_warning")
            if has_value(entry.get("trend_analysis")) and not has_value(row.get("ai_trend")):
                row["ai_trend"] = entry.get("trend_analysis")
            if has_value(entry.get("ma_analysis")) and not has_value(row.get("ai_ma")):
                row["ai_ma"] = entry.get("ma_analysis")
            if has_value(entry.get("volume_analysis")) and not has_value(row.get("ai_volume")):
                row["ai_volume"] = entry.get("volume_analysis")
            row["code"] = code
            row["strategy_source"] = "o2c_factor"
            row["ai_source_kind"] = entry.get("ai_source_kind") or row.get("ai_source_kind") or "o2c_factor_deep_analysis"
            row["ai_source_name"] = entry.get("ai_source_name") or entry.get("source") or row.get("ai_source_name") or row["ai_source_kind"]
            row["ai_source_date"] = str(entry.get("ai_source_date") or entry.get("ai_analysis_date") or entry.get("trade_date") or target_trade_date)
            row["ai_source_stale"] = row["ai_source_date"] != target_trade_date
            row["ai_evidence_time"] = entry.get("ai_evidence_time") or entry.get("generated_at") or entry.get("created_at")
            row["ai_risk_tags"] = entry.get("ai_risk_tags") or entry.get("ai_risks") or []
            row["rank_change"] = 0
            mapped[code] = row
    return mapped


def first_existing_greenfield_path() -> Path | None:
    for path in GREENFIELD_TOP20_CANDIDATES:
        if path.exists():
            return path
    return None


def o2c_top_factor_labels(stock: dict, limit: int = 3) -> list[str]:
    details = stock.get("factor_details") or {}
    ranked: list[tuple[float, str]] = []
    if not isinstance(details, dict):
        return []
    for key, value in details.items():
        detail = value if isinstance(value, dict) else {"value": value}
        weight = detail.get("weight")
        if weight is None:
            weight = detail.get("weight_pct")
        try:
            weight_num = abs(float(weight or 0))
        except (TypeError, ValueError):
            weight_num = 0.0
        label = O2C_FACTOR_LABELS.get(str(key), str(key))
        ranked.append((weight_num, label))
    return [label for _, label in sorted(ranked, reverse=True)[:limit]]


def load_greenfield_o2c(target_trade_date: str | None) -> tuple[dict | None, list[dict], dict]:
    path = first_existing_greenfield_path()
    source = {
        "name": "greenfield_o2c",
        "path": str(path) if path else None,
        "trade_date": None,
        "matched_target_trade_date": False,
        "item_count": 0,
        "ai_analyzed_count": 0,
        "merge_enabled": True,
    }
    if path is None:
        return None, [], source
    try:
        data = load_json(path)
    except Exception as exc:
        source["error"] = str(exc)
        return None, [], source

    trade_date = str(data.get("latest_trade_date") or data.get("trade_date") or data.get("recommend_date") or "")
    items = data.get("top20") or data.get("stocks") or []
    if not isinstance(items, list):
        items = []
    matched = bool(trade_date and target_trade_date and trade_date == target_trade_date)
    if target_trade_date and not matched:
        source.update(
            {
                "trade_date": trade_date,
                "matched_target_trade_date": False,
                "skipped_reason": "trade_date_mismatch",
            }
        )
        return None, [], source
    ai_map = load_o2c_ai_map(target_trade_date if matched else trade_date)
    enriched: list[dict] = []
    for idx, item in enumerate(items[:20], start=1):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        code = normalize_code(row.get("code") or row.get("ts_code"))
        if not code:
            continue
        row["code"] = code
        row.setdefault("rank", row.get("rank_no") or idx)
        row.setdefault("ts_code", item.get("ts_code") or item.get("code"))
        row.setdefault("name", item.get("name") or item.get("stock_name"))
        row.setdefault("industry_name", item.get("industry_name") or item.get("industry") or item.get("sector_name"))
        row["strategy_source"] = "o2c_factor"
        row["strategy_name"] = data.get("strategy_name") or "O2C日内因子"
        row["score"] = item.get("score") if has_value(item.get("score")) else item.get("composite_score")
        row["ai_o2c_factor_score"] = row.get("score")
        row["ai_o2c_top_factors"] = o2c_top_factor_labels(row)
        row["ai_o2c_risk_note"] = "反转因子、流动性和行业集中度需要盘中确认。"
        row["greenfield_source_date"] = trade_date
        row["greenfield_source_stale"] = bool(target_trade_date and trade_date != target_trade_date)

        ai_row = ai_map.get(code)
        if ai_row:
            for key, value in ai_row.items():
                if key in EXTENDED_FIELDS or key.startswith("ai_") or key.startswith("strategy_"):
                    if has_value(value):
                        row[key] = value
            row["ai_status"] = item_ai_status(row)
        else:
            row.setdefault("ai_status", "pending")
            row.setdefault("ai_source_kind", "o2c_factor_pending")
            row.setdefault("ai_source_name", "o2c_factor_pending")
            row.setdefault("ai_source_date", trade_date)
        enriched.append(row)

    source.update(
        {
            "trade_date": trade_date,
            "matched_target_trade_date": matched,
            "item_count": len(enriched),
            "ai_analyzed_count": sum(1 for row in enriched if row.get("ai_summary") or row.get("ai_score")),
        }
    )
    return data, enriched, source


def strategy_factor_weights(strategy: dict) -> dict[str, str]:
    weights = strategy.get("weights") or {}
    return {
        FACTOR_LABELS.get(name, name): f"{float(weight) * 100:.0f}%"
        for name, weight in weights.items()
    }


def build_backtest(strategy: dict, strategy_doc: dict) -> dict:
    summary = strategy.get("summary") or {}
    window = strategy_doc.get("window") or {}
    period = f"{window.get('trade_days', '-') }天 ({window.get('from', '-') } ~ {window.get('to', '-') })"

    # 诚实化整改 2026-06-12：发布层 summary 不含收益指标时，此前会格式化出
    # "+0.00%/胜率0.0%" 的伪数字误导读者；现在改为显式的不可用状态。
    def _metric(key):
        try:
            return float(summary.get(key))
        except (TypeError, ValueError):
            return None

    metrics = {key: _metric(key) for key in ("total_return", "sharpe", "win_rate", "max_drawdown")}
    if all(value is None or abs(value) < 1e-9 for value in metrics.values()):
        return {
            "period": period,
            "status": "unavailable",
            "note": "回测指标暂不展示：旧版硬编码数字已下线，回测引擎v2重建中",
        }
    return {
        "period": period,
        "total_return": f"{metrics['total_return'] or 0:+.2f}%",
        "sharpe": metrics["sharpe"] or 0.0,
        "win_rate": f"{metrics['win_rate'] or 0:.1f}%",
        "max_drawdown": f"{metrics['max_drawdown'] or 0:.2f}%",
        "methodology_note": "简化口径：T+1收盘等权、未计费用滑点与成交可达性，结果偏乐观，仅供横向对比",
    }


def enrich_top20(items: list[dict]) -> list[dict]:
    enriched = []
    for item in items:
        row = dict(item)
        if "change" in row and "change_pct" not in row:
            row["change_pct"] = row.get("change")
        if "close" in row and "price" not in row:
            row["price"] = row.get("close")
        if "code" in row and "ts_code" not in row:
            code = str(row.get("code") or "")
            if code:
                suffix = "SZ" if code.startswith(("0", "3")) else "SH" if code.startswith("6") else "BJ"
                row["ts_code"] = f"{code}.{suffix}"
        enriched.append(row)
    return enriched


def load_history_base(trade_date: str) -> dict:
    candidates = [
        WORKING_DATA_JSON.parent / "data" / "history" / f"{trade_date}.json",
        WORKING_DATA_JSON.parent / "history" / f"{trade_date}.json",
        PUBLISHED_DATA_JSON.parent / "data" / "history" / f"{trade_date}.json",
        PUBLISHED_DATA_JSON.parent / "history" / f"{trade_date}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                return load_json(path)
            except Exception:
                continue
    return {}


def same_day_universe_count(trade_date: str) -> tuple[int, str]:
    """Return the scanned-universe size from the same-day cache.

    A generated history snapshot is optional, but the daily cache is the
    source used to produce the candidate ranking.  When the history snapshot
    is absent, falling back to zero makes a healthy run look empty to the
    public report.  Read parquet metadata only; do not load the full frame or
    manufacture a count from the 20 published candidates.
    """
    if not isinstance(trade_date, str) or len(trade_date) != 8 or not trade_date.isdigit():
        return 0, "unknown"
    for filename in (f"daily_{trade_date}.parquet", f"stk_factor_{trade_date}.parquet"):
        path = CACHE_DIR / filename
        if not path.exists():
            continue
        try:
            import pyarrow.parquet as parquet

            count = int(parquet.ParquetFile(path).metadata.num_rows)
            if count > 0:
                return count, f"same_day_cache:{filename}"
        except Exception:
            # A cache format/runtime mismatch should not make publication
            # crash.  Try the next cache file and fail closed if neither can
            # be measured.
            continue
    return 0, "unknown"


def resolve_total_scanned(base: dict, trade_date: str) -> tuple[int, str]:
    """Resolve the universe count without treating top20 as the universe."""
    for source_name, container in (
        ("history_report", base.get("report") or {}),
        ("history_statistics", base.get("statistics") or {}),
    ):
        raw = container.get("total_scanned")
        try:
            count = int(raw)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            return count, source_name
    return same_day_universe_count(trade_date)


def build_compatible_data_json(
    strategy_doc: dict,
    strategy: dict,
    enriched_items: list[dict],
    latest_trade_date: str,
    greenfield_data: dict | None = None,
    greenfield_items: list[dict] | None = None,
) -> dict:
    base = load_history_base(latest_trade_date)
    generated_at = now_str()
    display_strategy = strategy.get("name") or TARGET_STRATEGY
    trade_date_human = f"{latest_trade_date[:4]}-{latest_trade_date[4:6]}-{latest_trade_date[6:8]}" if len(latest_trade_date) == 8 else latest_trade_date
    report = dict(base.get("report") or {})
    total_scanned, total_scanned_source = resolve_total_scanned(base, latest_trade_date)
    eligible_ai_count = sum(1 for item in enriched_items if item.get("ai_coverage_counted") is True)
    report.update(
        {
            "title": report.get("title") or "A股量化选股报告",
            "subtitle": f"策略: {display_strategy}",
            "date": trade_date_human,
            "update_time": generated_at,
            "version": str((base.get("report") or {}).get("version") or "3.0"),
            "total_scanned": total_scanned,
            "total_scanned_source": total_scanned_source,
            "top_n": len(enriched_items),
        }
    )

    compatible = {
        "generated_at": generated_at,
        "latest_trade_date": latest_trade_date,
        "update_time": generated_at,
        "total_stocks": report["total_scanned"],
        "data_source": current_data_source_label(base),
        "strategy": display_strategy,
        "factor_weights": strategy_factor_weights(strategy),
        "backtest": build_backtest(strategy, strategy_doc),
        "has_ai_analysis": eligible_ai_count > 0,
        "ai_analyzed_count": eligible_ai_count,
        "count": len(enriched_items),
        "top20": enriched_items,
        "report": report,
        "statistics": dict(base.get("statistics") or {}),
        "risk_warning": base.get("risk_warning") or "⚠️ 仅供参考，不构成投资建议",
        "recommendations": enrich_top20(enriched_items),
        "ai_progress": summarize_ai_progress(enriched_items),
    }
    compatible["total_scanned_source"] = total_scanned_source
    compatible["statistics"]["total_scanned"] = report["total_scanned"]
    compatible["statistics"]["total_scanned_source"] = total_scanned_source
    compatible["statistics"]["top_n"] = len(enriched_items)
    if greenfield_data is not None or greenfield_items:
        o2c_items = greenfield_items or []
        factor_pool = (greenfield_data or {}).get("factor_pool") or {}
        compatible["greenfield_o2c"] = {
            "strategy": (greenfield_data or {}).get("strategy") or "greenfield_o2c_v1",
            "strategy_name": (greenfield_data or {}).get("strategy_name") or "O2C日内因子",
            "latest_trade_date": (greenfield_data or {}).get("latest_trade_date") or (greenfield_data or {}).get("trade_date"),
            "factor_pool": factor_pool,
            "has_ai_analysis": any(item.get("ai_summary") or item.get("ai_score") for item in o2c_items),
            "ai_analyzed_count": sum(1 for item in o2c_items if item.get("ai_summary") or item.get("ai_score")),
            "ai_progress": summarize_ai_progress(o2c_items),
            "top20": o2c_items,
        }
    return compatible


def ai_content_failed(item: dict) -> bool:
    text_parts = []
    for key in ("ai_summary", "ai_conclusion", "ai_points", "ai_advice"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            text_parts.append(value.strip())
    text = "\n".join(text_parts)
    failure_markers = [
        "分析过程出错",
        "AI分析暂缺",
        "上次分析失败",
        "All LLM models failed",
        "APIConnectionError",
        "GeminiException",
        "模型调用失败",
        "LLM models failed",
    ]
    return any(marker in text for marker in failure_markers)


def item_ai_status(item: dict) -> str:
    if ai_content_failed(item):
        return "failed"
    filled = sum(1 for field in REQUIRED_AI_FIELDS if item.get(field) not in (None, "", [], {}))
    if filled == len(REQUIRED_AI_FIELDS) and has_explicit_labels(item):
        return "ready"
    if filled > 0:
        return "partial"
    return "pending"


def apply_ai_policy_fields(item: dict, *, system_observe_only: bool = True) -> dict:
    """Epic D: strip model prices, apply rule caps, stamp ai_depth. Never changes rank."""
    try:
        from ai_risk_rules import apply_risk_rules
    except Exception:
        cleaned = dict(item)
        cleaned["rank_change"] = 0
        cleaned.pop("display_score", None)
        cleaned["ai_depth"] = cleaned.get("ai_depth") or "shallow"
        return cleaned
    out = apply_risk_rules(
        item,
        system_observe_only=system_observe_only,
        used_proxy=bool(item.get("used_proxy")),
        chip_mode=str(item.get("chip_mode") or "unknown"),
    )
    out["rank_change"] = 0
    out["ai_status"] = item_ai_status(out)
    return out


def summarize_ai_progress(items: list[dict]) -> dict:
    summary = {"ready": 0, "partial": 0, "pending": 0, "failed": 0, "ready_ratio": 0.0}
    for item in items:
        key = item.get("ai_status", "pending")
        if key not in summary:
            key = "pending"
        summary[key] += 1
    total = len(items) or 1
    try:
        from metric_guards import safe_ratio
        summary["ready_ratio"] = round(float(safe_ratio(summary["ready"], total) or 0.0), 4)
    except Exception:
        summary["ready_ratio"] = round(min(1.0, max(0.0, summary["ready"] / total)), 4)
    summary["completed"] = summary["ready"]
    summary["total"] = len(items)
    return summary


def scrub_failed_ai_fields(item: dict) -> dict:
    cleaned = dict(item)
    if item_ai_status(cleaned) != "failed":
        return cleaned
    for key in FAILED_AI_RESET_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def filter_mergeable_ai_items(items: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for item in items or []:
        cleaned = scrub_failed_ai_fields(item)
        if any(has_value(cleaned.get(field)) for field in EXTENDED_FIELDS):
            filtered.append(cleaned)
    return filtered


def classify_ai_provenance(original_item: dict, merged_item: dict, target_trade_date: str) -> str:
    temporal = classify_ai_temporal_evidence(merged_item, target_trade_date)
    if temporal == "fresh_same_day":
        return "fresh_same_day"
    if temporal in {"delayed_analysis", "stale_historical", "unknown"}:
        return temporal
    if item_ai_status(merged_item) == "failed":
        return "failed"
    return "pending"


def sanitize_temporal_ai_item(item: dict, target_trade_date: str) -> dict:
    """Strip non-eligible AI fields before a public artifact can be materialized."""
    cleaned = dict(item)
    temporal = classify_ai_temporal_evidence(cleaned, target_trade_date)
    cleaned["ai_temporal_class"] = temporal
    cleaned["ai_coverage_counted"] = bool(
        temporal == "fresh_same_day" and item_ai_status(cleaned) == "ready"
    )
    if temporal != "fresh_same_day" and any(has_value(cleaned.get(field)) for field in EXTENDED_FIELDS):
        for key in FAILED_AI_RESET_FIELDS:
            cleaned.pop(key, None)
        cleaned["ai_temporal_class"] = temporal
        cleaned["ai_coverage_counted"] = False
        cleaned["ai_status"] = "pending"
        cleaned["ai_provenance"] = temporal
    return cleaned


def merge_candidate_ai(original_item: dict, source_item: dict, target_trade_date: str) -> dict:
    """Prefer a complete same-day model result over nonempty generated placeholders."""

    merged = dict(original_item)
    source_date = str(source_item.get("ai_source_date") or source_item.get("ai_analysis_date") or "")
    source_kind = str(source_item.get("ai_source_kind") or "")
    authoritative_same_day = bool(
        source_date == target_trade_date
        and source_kind == "traditional_deep_analysis"
        and item_ai_status(source_item) == "ready"
    )
    for key, value in source_item.items():
        if key.startswith("ai_source_") or key in {
            "ai_analysis_date",
            "ai_evidence_time",
            "ai_temporal_class",
            "ai_coverage_counted",
            "generated_at",
        }:
            if has_value(value):
                merged[key] = value
            continue
        if key not in EXTENDED_FIELDS or not has_value(value):
            continue
        if authoritative_same_day or not has_value(merged.get(key)):
            merged[key] = value
    return merged


def summarize_provenance(items: list[dict]) -> dict[str, int]:
    counters = {
        "fresh_same_day": 0,
        "same_day_reused": 0,
        "delayed_analysis": 0,
        "stale_historical": 0,
        "unknown": 0,
        "generated_fallback": 0,
        "failed": 0,
        "pending": 0,
    }
    for item in items:
        key = item.get("ai_provenance", "pending")
        if key not in counters:
            key = "unknown"
        counters[key] += 1
    return counters


def candidate_snapshot(item: dict) -> dict:
    keep = [
        "code",
        "name",
        "industry_name",
        "ai_status",
        "ai_provenance",
        "ai_source_name",
        "ai_source_date",
        "ai_evidence_time",
        "ai_temporal_class",
        "ai_coverage_counted",
        "ai_score",
        "ai_confidence",
        "ai_advice",
        "ai_summary",
        "ai_risk_warning",
    ]
    return {key: item.get(key) for key in keep if key in item}


def refresh_published_analytics(publish: dict) -> bool:
    python_exec = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    steps: list[dict] = []
    for script in ANALYTICS_REFRESH_SCRIPTS:
        proc = subprocess.run([python_exec, str(script)], cwd=WORKSPACE, capture_output=True, text=True)
        step = {
            "script": str(script),
            "python_exec": python_exec,
            "returncode": proc.returncode,
            "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-20:]),
            "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-20:]),
        }
        steps.append(step)
        if proc.returncode != 0:
            publish["analytics_refresh"] = {"ok": False, "steps": steps}
            publish["error"] = f"analytics refresh failed: {script.name}"
            return False
    publish["analytics_refresh"] = {"ok": True, "steps": steps}
    return True


def build_public_strategy_doc(strategy_doc: dict) -> dict:
    public_doc = dict(strategy_doc)
    retained = set(RETAINED_STRATEGIES)
    public_doc["strategies"] = [
        item for item in (strategy_doc.get("strategies") or []) if item.get("id") in retained
    ]
    return public_doc


def publish_outputs(latest_trade_date: str, payload: dict) -> bool:
    publish = payload.setdefault("publish", {})
    publish["published_repo"] = str(PUBLISHED_REPO)
    publish["working_repo"] = str(WORKING_REPO)

    try:
        if not PUBLISHED_REPO.exists():
            publish["error"] = f"published repo missing: {PUBLISHED_REPO}"
            return False

        repo_state = git_status(PUBLISHED_REPO)
        publish["pre_publish_repo_state"] = repo_state
        publish["pre_publish_repo_dirty"] = not bool(repo_state.get("clean"))
        publish["pre_publish_repo_dirty_unrelated"] = publish["pre_publish_repo_dirty"] and not bool(repo_state.get("allowed_output_changes_only"))
        # Stage4 only materializes local clone files. Remote push is exclusive to
        # publish_stock_report_v2.sh (pre-push contract v2). Bare git commit/push here
        # previously created incomplete-contract commits that GitHub Pages rejected.
        publish["remote_push"] = "deferred_to_v2_publisher"

        PUBLISHED_STRATEGY_JSON.parent.mkdir(parents=True, exist_ok=True)
        write_json(PUBLISHED_DATA_JSON, load_json(WORKING_DATA_JSON))
        working_strategy_doc = load_json(WORKING_STRATEGY_JSON)
        public_strategy_doc = build_public_strategy_doc(working_strategy_doc)
        write_json(PUBLISHED_STRATEGY_JSON, public_strategy_doc)

        history_src = WORKING_REPO / "data" / "history" / f"{latest_trade_date}.json"
        history_dst = PUBLISHED_REPO / "data" / "history" / f"{latest_trade_date}.json"
        if history_src.exists():
            history_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(history_src, history_dst)

        midday_src = WORKING_REPO.parent.parent / "recommendation_analytics" / "midday_analysis_latest.json"
        midday_dst = PUBLISHED_REPO / "data" / "recommendation_analytics" / "midday_analysis_latest.json"
        if midday_src.exists():
            publish["midday_analysis_seen_in_working"] = True
            midday_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(midday_src, midday_dst)
            publish["midday_analysis_copied_to_published"] = True
        else:
            publish["midday_analysis_seen_in_working"] = False
            publish["midday_analysis_copied_to_published"] = False

        if not refresh_published_analytics(publish):
            return False

        publish["local_clone_materialized"] = True
        publish["ok"] = True
        return True
    except Exception as exc:
        publish["error"] = str(exc)
        return False


def main() -> int:
    payload = {
        "stage": "aiEnrichmentAndPublish",
        "generated_at": now_str(),
        "target_strategy": TARGET_STRATEGY,
        "target_top_n": 20,
        "checks": {},
        "sources": [],
        "ok": False,
    }

    if not WORKING_STRATEGY_JSON.exists():
        payload["checks"]["strategy_backtests_exists"] = False
        write_json(HEALTH_DIR / "ai_publish_readiness.json", payload)
        print(f"[stage4] missing strategy file: {WORKING_STRATEGY_JSON}")
        return 1

    payload["checks"]["strategy_backtests_exists"] = True
    strategy_doc = load_json(WORKING_STRATEGY_JSON)
    latest_trade_date = str(strategy_doc.get("latest_trade_date") or "")
    payload["checks"]["strategy_latest_trade_date"] = latest_trade_date
    latest_common, latest_by_type = latest_common_trade_date()
    payload["checks"]["latest_common_trade_date"] = latest_common
    payload["checks"]["latest_cache_by_type"] = latest_by_type

    target = find_strategy(strategy_doc, TARGET_STRATEGY)
    if not target:
        payload["checks"]["prebreakout_exists"] = False
        write_json(HEALTH_DIR / "ai_publish_readiness.json", payload)
        print(f"[stage4] missing strategy: {TARGET_STRATEGY}")
        return 1

    payload["checks"]["prebreakout_exists"] = True
    original_items = [scrub_failed_ai_fields(item) for item in target.get("top20", [])]
    payload["checks"]["prebreakout_top20_len"] = len(original_items)

    merged_map, sources = load_candidate_sources(latest_trade_date)
    greenfield_data = {
        "strategy": "greenfield_o2c_v1",
        "strategy_name": "O2C日内因子",
        "lifecycle_status": "archived_historical_only",
    }
    greenfield_items: list[dict] = []
    greenfield_source = {
        "name": "greenfield_o2c",
        "trade_date": None,
        "matched_target_trade_date": False,
        "item_count": 0,
        "ai_analyzed_count": 0,
        "skipped_reason": "strategy_archived",
    }
    sources.append(greenfield_source)
    payload["sources"] = sources

    enriched_items: list[dict] = []
    source_hits = 0
    raw_temporal_by_code = load_ai_temporal_map_for_trade_date(latest_trade_date)
    for item in original_items:
        code = normalize_code(item.get("code"))
        merged = dict(item)
        if code in merged_map:
            merged = merge_candidate_ai(item, merged_map[code], latest_trade_date)
            source_hits += 1
        merged["ai_provenance"] = classify_ai_provenance(item, merged, latest_trade_date)
        sanitized = sanitize_temporal_ai_item(merged, latest_trade_date)
        if code in raw_temporal_by_code and sanitized.get("ai_temporal_class") != "fresh_same_day":
            sanitized["ai_temporal_class"] = raw_temporal_by_code[code]
            sanitized["ai_coverage_counted"] = False
            sanitized["ai_provenance"] = raw_temporal_by_code[code]
        enriched_items.append(sanitized)

    # System is observe-only until short-track promotion; all-observe advice is allowed.
    system_observe_only = True
    enriched_items = [
        apply_ai_policy_fields(item, system_observe_only=system_observe_only)
        for item in enriched_items
    ]
    target["top20"] = enriched_items
    for item in enriched_items:
        item["ai_status"] = item_ai_status(item)
        item["ai_missing_fields"] = [
            field for field in REQUIRED_AI_FIELDS if item.get(field) in (None, "", [], {})
        ]
        item["rank_change"] = 0
    advices = [str(item.get("ai_advice") or "") for item in enriched_items]
    all_same_observe = bool(advices) and all(a in {"观察", "观望", ""} for a in advices)
    payload["checks"]["source_hits"] = source_hits
    payload["checks"]["missing_source_codes"] = [
        normalize_code(item.get("code"))
        for item in enriched_items
        if normalize_code(item.get("code")) not in merged_map
    ]
    payload["checks"]["required_ai_field_counts"] = ai_field_counts(enriched_items, REQUIRED_AI_FIELDS)
    payload["checks"]["explicit_label_coverage"] = sum(1 for item in enriched_items if has_explicit_labels(item))
    payload["checks"]["ai_status_counts"] = summarize_ai_progress(enriched_items)
    payload["checks"]["all_observe_advice"] = all_same_observe
    payload["checks"]["all_observe_allowed"] = bool(system_observe_only)
    payload["checks"]["ai_depth_counts"] = {}
    for item in enriched_items:
        depth = str(item.get("ai_depth") or "shallow")
        payload["checks"]["ai_depth_counts"][depth] = payload["checks"]["ai_depth_counts"].get(depth, 0) + 1
    # Plan: do NOT fail publish solely because all advice is 观察 under observe_only.
    payload["checks"]["forced_opinion_diversity_required"] = False
    payload["checks"]["ai_provenance_counts"] = summarize_provenance(enriched_items)
    payload["checks"]["ai_temporal_eligible_count"] = sum(
        1 for item in enriched_items if item.get("ai_coverage_counted") is True
    )
    payload["checks"]["ai_temporal_classes"] = {
        key: sum(1 for item in enriched_items if item.get("ai_temporal_class") == key)
        for key in ("fresh_same_day", "delayed_analysis", "stale_historical", "unknown")
    }
    payload["checks"]["greenfield_o2c_top20_len"] = len(greenfield_items)
    payload["checks"]["greenfield_o2c_source_date"] = greenfield_source.get("trade_date")
    payload["checks"]["greenfield_o2c_matched_trade_date"] = greenfield_source.get("matched_target_trade_date")
    payload["checks"]["greenfield_o2c_ai_analyzed_count"] = greenfield_source.get("ai_analyzed_count")
    payload["checks"]["greenfield_o2c_ai_status_counts"] = summarize_ai_progress(greenfield_items)
    payload["checks"]["merge_policy"] = {
        "working_data_json": True,
        "historical_ai_analysis_same_day_only": True,
        "published_strategy_backtests": False,
        "published_data_json": False,
        "greenfield_o2c_ai_analysis_same_day_only": True,
    }
    payload["checks"]["unexpected_ai_provenance"] = [
        normalize_code(item.get("code"))
        for item in enriched_items
        if item.get("ai_provenance") in {"stale_historical", "failed"}
    ]

    strategy_doc["generated_at"] = now_str()
    write_json(WORKING_STRATEGY_JSON, strategy_doc)

    publish_payload = build_compatible_data_json(
        strategy_doc,
        target,
        enriched_items,
        latest_trade_date,
        greenfield_data=greenfield_data,
        greenfield_items=greenfield_items,
    )
    write_json(WORKING_DATA_JSON, publish_payload)
    payload["checks"]["data_json_exists"] = WORKING_DATA_JSON.exists()
    payload["checks"]["data_json_trade_date"] = latest_trade_date
    payload["checks"]["compatibility_fields"] = {
        key: key in publish_payload
        for key in [
            "update_time",
            "total_stocks",
            "data_source",
            "strategy",
            "factor_weights",
            "backtest",
            "top20",
            "recommendations",
            "statistics",
            "report",
            "has_ai_analysis",
            "ai_analyzed_count",
            "greenfield_o2c",
        ]
    }

    counts = payload["checks"]["required_ai_field_counts"]
    base_publish_blockers = []
    if len(enriched_items) != 20:
        base_publish_blockers.append("top20_incomplete")
    if latest_common != latest_trade_date:
        base_publish_blockers.append("latest_common_trade_date_mismatch")
    payload["checks"]["base_publish_blockers"] = base_publish_blockers
    payload["base_ok"] = not base_publish_blockers
    payload["ai_complete"] = (
        all(value == 20 for value in counts.values())
        and payload["checks"]["explicit_label_coverage"] == 20
        and payload["checks"]["ai_temporal_eligible_count"] == 20
    )
    payload["ok"] = payload["base_ok"]
    if payload["ai_complete"]:
        payload["publish_mode"] = "full"
    elif payload["checks"]["ai_status_counts"]["partial"] or payload["checks"]["ai_status_counts"]["ready"]:
        payload["publish_mode"] = "base_with_partial_ai"
    else:
        payload["publish_mode"] = "base_only"

    payload["subphases"] = {
        "ai_enrichment": {
            "ok": len(enriched_items) == 20,
            "source_hits": source_hits,
            "ai_status_counts": payload["checks"]["ai_status_counts"],
            "ai_provenance_counts": payload["checks"]["ai_provenance_counts"],
            "explicit_label_coverage": payload["checks"]["explicit_label_coverage"],
        }
    }

    if payload["base_ok"]:
        payload["local_materialized"] = publish_outputs(latest_trade_date, payload)
    else:
        payload["local_materialized"] = False
    payload["published"] = False

    payload["subphases"]["publish"] = {
        "attempted": bool(payload["base_ok"]),
        "local_materialized": bool(payload["local_materialized"]),
        "published": False,
        "git": payload.get("publish"),
    }

    canonical_paths = {}
    canonical_paths["decision_candidates"] = str(
        write_canonical_object(
            "decision_candidates",
            latest_trade_date,
            {
                "target_strategy": TARGET_STRATEGY,
                "candidate_count": len(enriched_items),
                "ai_status_counts": payload["checks"]["ai_status_counts"],
                "ai_provenance_counts": payload["checks"]["ai_provenance_counts"],
                "candidates": [candidate_snapshot(item) for item in enriched_items],
            },
        )
    )
    canonical_paths["industry_state"] = str(
        write_canonical_object(
            "industry_state",
            latest_trade_date,
            {
                "target_strategy": TARGET_STRATEGY,
                "candidate_count": len(enriched_items),
                "top_industries": top_industries(enriched_items, limit=10),
            },
        )
    )
    payload["canonical_objects"] = canonical_paths

    out = HEALTH_DIR / "ai_publish_readiness.json"
    write_health_snapshot(out, payload, trade_date=latest_trade_date)
    print(f"[stage4] wrote readiness file: {out}")
    # Return 0 if base data is valid (20 items, correct date).
    # Publish failure or incomplete AI is a warning, not a hard pipeline error.
    if not payload["base_ok"]:
        print("[stage4] base data not ok - returning error")
        return 1
    if not payload.get("local_materialized"):
        print("[stage4] WARNING: local materialization failed but base data is ok - pipeline continues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
