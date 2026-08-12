#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
import re

import numpy as np
import pandas as pd
from orchestrator_common import HEALTH_DIR, PUBLISHED_REPO, WORKSPACE, resolve_effective_trade_date
from generate_system_verdict import SYSTEM_VERDICT_OUT, write_system_verdict

ANALYTICS_DIR = PUBLISHED_REPO / "data" / "recommendation_analytics"
LATEST_DIR = PUBLISHED_REPO / "data" / "latest"
WORKING_ANALYTICS_DIR = WORKSPACE / "stock_data" / "03-working" / "recommendation_analytics"
WORKING_HEALTH_DIR = WORKSPACE / "stock_data" / "03-working" / "health"
LOCAL_WAREHOUSE_EXPORT_DIR = (
    WORKSPACE / "stock_data" / "03-working" / "recommendation_warehouse" / "exports"
)

DATA_JSON = PUBLISHED_REPO / "data.json"
STRATEGY_JSON = PUBLISHED_REPO / "data" / "strategy_backtests.json"
MORNING_JSON = ANALYTICS_DIR / "market_morning_brief_latest.json"
MIDDAY_PUBLISHED_JSON = ANALYTICS_DIR / "midday_analysis_latest.json"
MIDDAY_WORKING_JSON = WORKING_ANALYTICS_DIR / "midday_analysis_latest.json"
MARKET_JSON = ANALYTICS_DIR / "market_industry_heatmap.json"
STRATEGY_HEAT_JSON = ANALYTICS_DIR / "industry_heatmap.json"
REVIEW_JSON = LOCAL_WAREHOUSE_EXPORT_DIR / "prebreakout_summary.json"
DETAIL_JSON = LOCAL_WAREHOUSE_EXPORT_DIR / "prebreakout_recommendations.json"
O2C_REVIEW_JSON = LOCAL_WAREHOUSE_EXPORT_DIR / "o2c_factor_summary.json"
O2C_DETAIL_JSON = LOCAL_WAREHOUSE_EXPORT_DIR / "o2c_factor_recommendations.json"
GREENFIELD_JSON_CANDIDATES = [
    PUBLISHED_REPO / "data/latest/greenfield_top20.json",
    WORKSPACE / "stock_data/03-working/stock-report-repo/data/latest/greenfield_top20.json",
]
AI_ANALYSIS_DIR = WORKSPACE / "stock_data/03-working/ai_analysis"
RESEARCH_JSON = ANALYTICS_DIR / "research_lab_latest.json"

VALIDATION_JSON = WORKING_HEALTH_DIR / "validation_report.json"
AI_PUBLISH_JSON = WORKING_HEALTH_DIR / "ai_publish_readiness.json"
ORCHESTRATOR_JSON = WORKING_HEALTH_DIR / "orchestrator_run.json"
RECOMMENDATION_DB_JSON = WORKING_HEALTH_DIR / "recommendation_db_sync.json"

RUN_MANIFEST_OUT = LATEST_DIR / "run_manifest.json"
MARKET_STATE_OUT = LATEST_DIR / "market_state.json"
STRATEGY_STATE_OUT = LATEST_DIR / "strategy_state.json"
CANDIDATE_STATE_OUT = LATEST_DIR / "candidate_state.json"
REVIEW_STATE_OUT = LATEST_DIR / "review_state.json"
REVIEW_STATE_O2C_OUT = LATEST_DIR / "review_state_o2c.json"
RESEARCH_STATE_OUT = LATEST_DIR / "research_state.json"
PUBLIC_STRATEGY_IDS = {"prebreakout_v41"}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path, default: Any = None):
    if not path.exists():
        return {} if default is None else default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except Exception:
        return {} if default is None else default


def normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(".")[0] if "." in text else text


def load_o2c_ai_rows(trade_date: str) -> dict[str, dict[str, Any]]:
    path = AI_ANALYSIS_DIR / f"{trade_date}.json"
    payload = load_json(path, [])
    if not isinstance(payload, list):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict) or row.get("strategy_source") != "o2c_factor":
            continue
        code = normalize_stock_code(row.get("code") or row.get("ts_code"))
        if code:
            rows[code] = row
    return rows


def build_o2c_review_fallback() -> dict[str, Any]:
    source_path = next((path for path in GREENFIELD_JSON_CANDIDATES if path.exists()), None)
    if source_path is None:
        return {
            "generated_at": now_str(),
            "strategy_id": "greenfield_o2c_v1",
            "strategy_source": "o2c_factor",
            "strategy_name": "O2C日内因子",
            "performance": {},
            "ai_view_stats": [],
            "top_repeat_recommendations": [],
            "latest_sample": [],
        }
    payload = load_json(source_path, {})
    trade_date = str(payload.get("latest_trade_date") or payload.get("trade_date") or "")
    stocks = payload.get("top20") or payload.get("stocks") or []
    if not isinstance(stocks, list):
        stocks = []
    ai_rows = load_o2c_ai_rows(trade_date)
    latest_sample = []
    view_counts: Counter[str] = Counter()
    repeat_rows = []
    for idx, stock in enumerate(stocks[:20], start=1):
        if not isinstance(stock, dict):
            continue
        code = normalize_stock_code(stock.get("code") or stock.get("ts_code"))
        ai = ai_rows.get(code, {})
        ai_view = ai.get("ai_advice") or ai.get("operation_advice") or "待分析"
        view_counts[str(ai_view)] += 1
        row = {
            "recommend_date": trade_date,
            "rank_no": stock.get("rank") or idx,
            "stock_code": code,
            "ts_code": stock.get("ts_code") or stock.get("code"),
            "stock_name": stock.get("name") or stock.get("stock_name"),
            "sector_name": stock.get("industry_name") or stock.get("industry") or "未标注",
            "ai_view": ai_view,
            "ai_score": ai.get("ai_score"),
            "recommend_price": stock.get("price") or stock.get("close"),
            "next_day_return_pct": None,
            "cumulative_return_pct": None,
            "cumulative_recommend_count": 1,
        }
        latest_sample.append(row)
        repeat_rows.append(
            {
                "stock_code": code,
                "stock_name": row["stock_name"],
                "recommend_count": 1,
                "avg_cumulative_return_pct": None,
            }
        )
    return {
        "generated_at": now_str(),
        "strategy_id": "greenfield_o2c_v1",
        "strategy_source": "o2c_factor",
        "strategy_name": "O2C日内因子",
        "source_path": str(source_path),
        "latest_recommend_date": trade_date,
        "latest_date_row_count": len(latest_sample),
        "performance": {
            "total_recommendations": len(latest_sample),
            "next_day_hit_rate_pct": None,
            "avg_next_day_return_pct": None,
            "avg_cumulative_return_pct": None,
            "max_drawdown_pct": None,
        },
        "ai_view_stats": [
            {
                "ai_view": view,
                "recommendation_count": count,
                "avg_next_day_return_pct": None,
                "avg_cumulative_return_pct": None,
                "avg_ai_score": None,
            }
            for view, count in view_counts.most_common()
        ],
        "top_repeat_recommendations": repeat_rows[:20],
        "latest_sample": latest_sample,
    }


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


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(".")[0] if "." in text else text


CYQ_CACHE_DIR = WORKSPACE / "stock_data" / "03-working" / "backtest_cache"


def load_chip_factors(trade_date: str) -> dict[str, dict[str, float]]:
    """加载 CYQ 数据并计算筹码因子，返回 {normalized_code: {chip_conc, winner_rate, ...}} 字典。"""
    health = load_json(WORKING_HEALTH_DIR / f"data_ready_{trade_date}.json", {})
    provider = str(health.get("cyq_perf_provider") or "").lower()
    if (
        str(health.get("target_trade_date") or "") != trade_date
        or not bool(health.get("ok"))
        or bool(health.get("cyq_perf_proxy_derived"))
        or not provider
        or "proxy" in provider
    ):
        return {}
    cyq_file = CYQ_CACHE_DIR / f"cyq_perf_{trade_date}.parquet"
    if not cyq_file.exists():
        return {}
    try:
        cyq_df = pd.read_parquet(cyq_file)
    except Exception:
        return {}
    for column in ("source_provider", "fallback_source", "source", "provenance"):
        if column in cyq_df.columns and cyq_df[column].astype(str).str.contains(
            "proxy", case=False, na=False
        ).any():
            return {}
    if "used_proxy" in cyq_df.columns and cyq_df["used_proxy"].fillna(True).astype(bool).any():
        return {}

    # 计算筹码因子
    cyq_df["chip_concentration"] = (cyq_df["cost_85pct"] - cyq_df["cost_15pct"]) / cyq_df["cost_50pct"].replace(0, np.nan)
    cyq_df["cost_deviation"] = (cyq_df["cost_50pct"] - cyq_df["weight_avg"]) / cyq_df["weight_avg"].replace(0, np.nan)
    cyq_df["chip_support"] = cyq_df["cost_15pct"] / cyq_df["cost_50pct"].replace(0, np.nan)
    cyq_df["chip_resistance"] = cyq_df["cost_85pct"] / cyq_df["cost_50pct"].replace(0, np.nan)

    result: dict[str, dict[str, float]] = {}
    for _, row in cyq_df.iterrows():
        code = normalize_code(row.get("ts_code"))
        if not code:
            continue
        result[code] = {
            "chip_conc": round(float(row["chip_concentration"]), 4) if pd.notna(row["chip_concentration"]) else None,
            "winner_rate": round(float(row["winner_rate"]), 2) if pd.notna(row["winner_rate"]) else None,
            "cost_deviation": round(float(row["cost_deviation"]), 4) if pd.notna(row["cost_deviation"]) else None,
            "chip_support": round(float(row["chip_support"]), 4) if pd.notna(row["chip_support"]) else None,
            "chip_resistance": round(float(row["chip_resistance"]), 4) if pd.notna(row["chip_resistance"]) else None,
        }
    return result


def choose_midday() -> tuple[dict[str, Any], str]:
    published = load_json(MIDDAY_PUBLISHED_JSON)
    working = load_json(MIDDAY_WORKING_JSON)

    published_trade_date = extract_yyyymmdd((published or {}).get("trade_date"))
    working_trade_date = extract_yyyymmdd((working or {}).get("trade_date"))

    if working_trade_date and (not published_trade_date or working_trade_date > published_trade_date):
        return working, "working_preferred"
    if published_trade_date:
        return published, "published"
    if working_trade_date:
        return working, "working_fallback"
    return {}, "missing"


def top_market_rows(market: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    latest_trade_date = str(market.get("latest_trade_date") or "")
    rows = [row for row in (market.get("rows") or []) if str(row.get("trade_date") or "") == latest_trade_date]
    rows = sorted(rows, key=lambda row: safe_float(row.get("market_heat_ema_5"), -999.0) or -999.0, reverse=True)
    top = []
    for row in rows[:limit]:
        top.append({
            "industry": row.get("industry_name"),
            "trade_date": row.get("trade_date"),
            "market_rank": row.get("market_heat_rank"),
            "market_heat": row.get("market_heat_ema_5"),
            "trend_signal": row.get("trend_signal"),
            "avg_pct_chg": row.get("avg_pct_chg"),
            "stock_count": row.get("stock_count"),
            "up_ratio": row.get("up_ratio"),
            "strong_ratio": row.get("strong_ratio"),
            "hot_stocks": row.get("hot_stocks") or [],
        })
    return top


def top_strategy_rows(strategy_heat: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    latest_date = str(strategy_heat.get("latest_recommend_date") or "")
    rows = [row for row in (strategy_heat.get("rows") or []) if str(row.get("recommend_date") or "") == latest_date]
    rows = sorted(rows, key=lambda row: safe_float(row.get("heat_ema_5"), -999.0) or -999.0, reverse=True)
    top = []
    for row in rows[:limit]:
        top.append({
            "industry": row.get("sector_name"),
            "recommend_date": row.get("recommend_date"),
            "strategy_rank": row.get("heat_rank"),
            "strategy_heat": row.get("heat_ema_5"),
            "trend_signal": row.get("trend_signal"),
            "avg_ai_score": row.get("avg_ai_score"),
            "avg_next_day_return_pct": row.get("avg_next_day_return_pct"),
            "avg_cumulative_return_pct": row.get("avg_cumulative_return_pct"),
            "represent_stock_name": row.get("represent_stock_name"),
            "represent_stock_code": row.get("represent_stock_code"),
            "represent_ai_view": row.get("represent_ai_view"),
            "represent_ai_score": row.get("represent_ai_score"),
        })
    return top


def detail_rows_by_latest_date(detail: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rows = detail.get("rows") or []
    dates = sorted({str(row.get("recommend_date") or "") for row in rows if str(row.get("recommend_date") or "")})
    latest_date = dates[-1] if dates else ""
    filtered = [row for row in rows if str(row.get("recommend_date") or "") == latest_date]
    return latest_date, filtered


def build_strategy_candidates_by_industry(detail_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        industry = str(row.get("sector_name") or row.get("industry_name") or "未知").strip()
        grouped[industry].append({
            "stock_code": row.get("stock_code"),
            "ts_code": row.get("ts_code"),
            "stock_name": row.get("stock_name"),
            "ai_view": row.get("ai_view"),
            "ai_score": row.get("ai_score"),
            "ai_confidence": row.get("ai_confidence"),
            "recommend_price": row.get("recommend_price"),
            "next_day_return_pct": row.get("next_day_return_pct"),
            "cumulative_return_pct": row.get("cumulative_return_pct"),
        })
    for industry, rows in grouped.items():
        grouped[industry] = sorted(
            rows,
            key=lambda row: (
                -(safe_float(row.get("ai_score"), -999.0) or -999.0),
                normalize_code(row.get("stock_code") or row.get("ts_code")),
            ),
        )[:5]
    return grouped


def build_industry_actions(market_top: list[dict[str, Any]], strategy_top: list[dict[str, Any]], detail_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    market_map = {str(row.get("industry") or ""): row for row in market_top if row.get("industry")}
    strategy_map = {str(row.get("industry") or ""): row for row in strategy_top if row.get("industry")}
    candidates_map = build_strategy_candidates_by_industry(detail_rows)
    industries = sorted(set(market_map) | set(strategy_map))
    actions: list[dict[str, Any]] = []
    for industry in industries:
        market_row = market_map.get(industry)
        strategy_row = strategy_map.get(industry)
        if market_row and strategy_row:
            kind = "overlap"
            action = "观察"
            reason = "市场主线与策略候选同时命中，优先看持续性与分化强度。"
        elif market_row and not strategy_row:
            kind = "market_only"
            action = "增配"
            reason = "市场已经形成主线，但策略侧覆盖不足，应补抓代表股。"
        else:
            kind = "strategy_only"
            action = "回避"
            reason = "策略候选出现，但市场主线未确认，暂不作为重点方向。"
        actions.append({
            "industry": industry,
            "kind": kind,
            "action": action,
            "reason": reason,
            "market_rank": market_row.get("market_rank") if market_row else None,
            "strategy_rank": strategy_row.get("strategy_rank") if strategy_row else None,
            "market_heat": market_row.get("market_heat") if market_row else None,
            "strategy_heat": strategy_row.get("strategy_heat") if strategy_row else None,
            "trend_signal": (market_row or strategy_row or {}).get("trend_signal"),
            "market_hot_stocks": (market_row or {}).get("hot_stocks") or [],
            "strategy_candidates": candidates_map.get(industry) or [],
            "action_summary": (
                "市场主线已形成，策略侧需要补抓龙头/扩散股" if action == "增配" else
                "市场与策略一致，继续跟踪确认度更高的标的" if action == "观察" else
                "策略存在但市场未确认，只做跟踪不做主配"
            ),
        })
    rank_map = {"增配": 0, "观察": 1, "回避": 2}
    return sorted(
        actions,
        key=lambda row: (
            rank_map.get(str(row.get("action") or ""), 9),
            safe_float(row.get("market_rank"), 999.0) or 999.0,
            safe_float(row.get("strategy_rank"), 999.0) or 999.0,
            str(row.get("industry") or ""),
        ),
    )


def classify_candidate_role(item: dict[str, Any]) -> str:
    advice = str(item.get("ai_advice") or item.get("ai_decision") or "").strip().lower()
    # safe_float returns None (not the 0.0 fallback) when ai_score is null/empty,
    # so we can tell "AI rated it 0" apart from "AI never ran".
    ai_score_raw = safe_float(item.get("ai_score"), None)
    has_ai = bool(advice) or ai_score_raw is not None
    # AI 缺失 ≠ 建议回避。没有 AI 数据时返回中性的 watch（策略分层=观察），
    # 而不是把 ai_score 默认成 0 后落入 avoid 分支。AI 是否存在由前端的
    # AI 状态徽章（ai-none）单独呈现，动作分层不应代为编造回避结论。
    if not has_ai:
        return "watch"
    ai_score = ai_score_raw or 0.0
    # PUBLIC_STRATEGY_IDS 目前只含 prebreakout_v41（本函数只服务它），复盘 IR-7.71，
    # 未见任何已证实边际。ai_score/买入话术升级为 main 的通道在此关闭：最高只到 watch。
    if advice in {"sell", "reduce"} or any(word in advice for word in ["卖出", "减仓", "回避"]):
        return "avoid"
    if ai_score < 45:
        return "avoid"
    return "watch"


def extract_yyyymmdd(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    m = re.search(r'(\d{8})', text)
    if m:
        return m.group(1)
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        return ''.join(m.groups())
    return ''


def build_candidate_state(strategy_doc: dict[str, Any], detail_rows: list[dict[str, Any]], market_actions: list[dict[str, Any]]) -> dict[str, Any]:
    strategies = [s for s in (strategy_doc.get("strategies") or []) if s.get("id") in PUBLIC_STRATEGY_IDS]
    detail_map = {normalize_code(row.get("stock_code") or row.get("ts_code")): row for row in detail_rows}
    industry_action_map = {str(row.get("industry") or ""): row for row in market_actions}
    candidates: list[dict[str, Any]] = []
    by_strategy_summary: list[dict[str, Any]] = []

    # 加载筹码因子
    trade_date = strategy_doc.get("latest_trade_date") or ""
    chip_map = load_chip_factors(trade_date) if trade_date else {}

    for strategy in strategies:
        strategy_id = strategy.get("id")
        strategy_name = strategy.get("name")
        tier = strategy.get("tier")
        items = strategy.get("top20") or []
        role_counter = Counter()
        for item in items:
            code = normalize_code(item.get("code"))
            detail_row = detail_map.get(code, {})
            industry = detail_row.get("sector_name") or item.get("industry_name") or "未标注行业"
            role_type = classify_candidate_role(item)
            role_counter[role_type] += 1
            action = industry_action_map.get(str(industry) or "", {})
            current_price = item.get("close")
            if current_price in (None, ""):
                current_price = detail_row.get("latest_price")
            current_change_pct = item.get("change")
            current_price_date = detail_row.get("latest_price_date") or strategy_doc.get("latest_trade_date")
            candidates.append({
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "strategy_tier": tier,
                "rank": item.get("rank"),
                "code": item.get("code"),
                "normalized_code": code,
                "name": item.get("name"),
                "industry_name": industry,
                "score": item.get("score"),
                "ai_score": item.get("ai_score"),
                "ai_confidence": item.get("ai_confidence"),
                "ai_advice": item.get("ai_advice") or item.get("ai_decision"),
                "ai_conclusion": item.get("ai_conclusion") or item.get("ai_summary"),
                "ai_summary": item.get("ai_summary"),
                "ai_points": item.get("ai_points"),
                "ai_trend": item.get("ai_trend"),
                "ai_ma": item.get("ai_ma"),
                "ai_volume": item.get("ai_volume"),
                "ai_fundamental": item.get("ai_fundamental"),
                "ai_risk_warning": item.get("ai_risk_warning"),
                "ai_risks": item.get("ai_risks") or [],
                "ai_catalysts": item.get("ai_catalysts") or [],
                "close": item.get("close"),
                "change": item.get("change"),
                "price": current_price,
                "change_pct": current_change_pct,
                "current_price": current_price,
                "current_change_pct": current_change_pct,
                "current_price_trade_date": current_price_date,
                "volume_ratio": item.get("volume_ratio"),
                "chip_conc": chip_map.get(code, {}).get("chip_conc"),
                "winner_rate": chip_map.get(code, {}).get("winner_rate"),
                "cost_deviation": chip_map.get(code, {}).get("cost_deviation"),
                "chip_support": chip_map.get(code, {}).get("chip_support"),
                "chip_resistance": chip_map.get(code, {}).get("chip_resistance"),
                "review_recommend_date": detail_row.get("recommend_date"),
                "review_ai_view": detail_row.get("ai_view"),
                "review_next_day_return_pct": detail_row.get("next_day_return_pct"),
                "review_cumulative_return_pct": detail_row.get("cumulative_return_pct"),
                "market_action": action.get("action"),
                "market_action_kind": action.get("kind"),
                "market_action_summary": action.get("action_summary"),
                "role_type": role_type,
                # 合同 v2 字段补齐（阶段5）：candidate_state 也带统一字段，减少前端兜底/未来歧义。
                # 全部从已带合同字段的 strategy_doc.top20 item 透传；研究观察策略个股不出现买入。
                "raw_action": item.get("raw_action"),
                "gate_adjusted_action": item.get("gate_adjusted_action") or item.get("adjusted_action"),
                "final_action": item.get("final_action") or item.get("adjusted_action"),
                "research_only": bool(item.get("strategy_research_only")),
                "strategy_gate_status": "warn" if item.get("strategy_research_only") else "pass",
                "ai_coverage_counted": bool(item.get("ai_coverage_counted")) if "ai_coverage_counted" in item
                    else (item.get("ai_score") not in (None, "", [], {})),
                "adjustment_reasons": item.get("adjustment_reasons") or [],
            })
        industries = [str(item.get("industry_name") or "").strip() for item in items if str(item.get("industry_name") or "").strip()]
        by_strategy_summary.append({
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "strategy_tier": tier,
            "top20_count": len(items),
            "main_count": role_counter.get("main", 0),
            "watch_count": role_counter.get("watch", 0),
            "avoid_count": role_counter.get("avoid", 0),
            "top_industries": [name for name, _ in Counter(industries).most_common(5)],
        })

    candidates = sorted(
        candidates,
        key=lambda item: (
            {"main": 0, "watch": 1, "avoid": 2}.get(item.get("role_type"), 9),
            -(safe_float(item.get("ai_score"), -999.0) or -999.0),
            safe_int(item.get("rank"), 999),
            str(item.get("normalized_code") or ""),
        ),
    )

    return {
        "generated_at": now_str(),
        "latest_trade_date": strategy_doc.get("latest_trade_date"),
        "latest_strategy_count": len(strategies),
        "role_counts": {
            "main": sum(1 for item in candidates if item.get("role_type") == "main"),
            "watch": sum(1 for item in candidates if item.get("role_type") == "watch"),
            "avoid": sum(1 for item in candidates if item.get("role_type") == "avoid"),
        },
        "strategy_summaries": by_strategy_summary,
        "candidates": candidates,
    }


def build_strategy_state(strategy_doc: dict[str, Any], strategy_heat: dict[str, Any], market_top: list[dict[str, Any]], candidate_state: dict[str, Any]) -> dict[str, Any]:
    market_industries = {str(row.get("industry") or "") for row in market_top if row.get("industry")}
    strategy_heat_rows = top_strategy_rows(strategy_heat, limit=12)
    strategy_heat_industries = [str(row.get("industry") or '').strip() for row in strategy_heat_rows if str(row.get("industry") or '').strip()]
    strategy_cards = []
    for strategy in [s for s in (strategy_doc.get("strategies") or []) if s.get("id") in PUBLIC_STRATEGY_IDS]:
        items = strategy.get("top20") or []
        ai_scores = [safe_float(item.get("ai_score")) for item in items if safe_float(item.get("ai_score")) is not None]
        quant_scores = [safe_float(item.get("score")) for item in items if safe_float(item.get("score")) is not None]
        changes = [safe_float(item.get("change")) for item in items if safe_float(item.get("change")) is not None]
        industries = [str(item.get("industry_name") or "").strip() for item in items if str(item.get("industry_name") or "").strip()]
        raw_top_industries = [name for name, _ in Counter(industries).most_common(5)]
        top_industries = strategy_heat_industries[:5] or raw_top_industries
        overlap_count = sum(1 for name in strategy_heat_industries if name in market_industries)
        if overlap_count >= 2:
            activation = "active"
        elif overlap_count >= 1 or str(strategy.get("tier") or "") == "主策略":
            activation = "watch"
        else:
            activation = "deprioritized"
        public_candidates = [
            item for item in (candidate_state.get("candidates") or []) if item.get("strategy_id") == strategy.get("id")
        ]
        strategy_cards.append({
            "strategy_id": strategy.get("id"),
            "strategy_name": strategy.get("name"),
            "tier": strategy.get("tier"),
            "activation": activation,
            "latest_trade_date": strategy_doc.get("latest_trade_date"),
            "top20_count": len(items),
            "market_overlap_count": overlap_count,
            "top_industries": top_industries,
            "summary": strategy.get("summary") or {},
            "avg_ai_score": round(sum(ai_scores) / len(ai_scores), 2) if ai_scores else None,
            "avg_quant_score": round(sum(quant_scores) / len(quant_scores), 2) if quant_scores else None,
            "avg_change_pct": round(sum(changes) / len(changes), 2) if changes else None,
            "sample_candidates": [
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "industry_name": item.get("industry_name"),
                    "ai_score": item.get("ai_score"),
                    "ai_advice": item.get("ai_advice"),
                    "change": item.get("change"),
                    "current_price": item.get("current_price") if item.get("current_price") not in (None, "") else item.get("close"),
                    "current_change_pct": item.get("current_change_pct") if item.get("current_change_pct") not in (None, "") else item.get("change"),
                }
                for item in public_candidates[:5]
            ],
        })

    return {
        "generated_at": now_str(),
        "latest_trade_date": strategy_doc.get("latest_trade_date"),
        "strategy_count": len(strategy_cards),
        "active_count": sum(1 for card in strategy_cards if card.get("activation") == "active"),
        "watch_count": sum(1 for card in strategy_cards if card.get("activation") == "watch"),
        "deprioritized_count": sum(1 for card in strategy_cards if card.get("activation") == "deprioritized"),
        "strategies": strategy_cards,
        "candidate_role_counts": candidate_state.get("role_counts") or {},
    }


_LS_TS_PRO = None
_LS_DOMESTIC_TS = {"shanghai": "000001.SH", "shenzhen": "399001.SZ", "chinext": "399006.SZ"}


def _ls_tushare_pro():
    global _LS_TS_PRO
    if _LS_TS_PRO is not None:
        return _LS_TS_PRO
    try:
        import sys as _sys
        _sys.path.insert(0, str(WORKSPACE / "skills" / "stock-analyzer"))
        from credentials import get_tushare_token, get_tushare_http_url  # type: ignore
        import tushare as _ts
        tok = get_tushare_token(); url = get_tushare_http_url()
        pro = _ts.pro_api(tok); pro._DataApi__token = tok; pro._DataApi__http_url = url
        _LS_TS_PRO = pro
    except Exception:
        _LS_TS_PRO = False
    return _LS_TS_PRO


def refresh_domestic_close(session_snapshot: dict[str, Any], trade_date: str) -> dict[str, Any]:
    """收盘发布用 tushare 结算收盘覆盖三大指数(午盘搬运来的现价快照会虚高)。20260703事故修复。
    tushare 无该日结算(如盘中/非交易日)或失败→保留原值不动(fail-safe)。"""
    if not trade_date or len(str(trade_date)) != 8:
        return session_snapshot
    pro = _ls_tushare_pro()
    if not pro:
        return session_snapshot
    for key, ts_code in _LS_DOMESTIC_TS.items():
        try:
            d = pro.index_daily(ts_code=ts_code, trade_date=str(trade_date))
            if d is None or len(d) == 0:
                continue
            r = d.iloc[0]
            close = safe_float(r.get("close"), None)
            prev = safe_float(r.get("pre_close"), None)
            if close is None:
                continue
            snap = dict(session_snapshot.get(key) or {})
            snap.update({
                "close": close, "prev_close": prev,
                "change_pct": round((close / prev - 1.0) * 100.0, 4) if prev else safe_float(r.get("pct_chg"), None),
                "source_kind": "exact_close", "provider": "tushare",
                "as_of": str(trade_date), "bar_count": 1, "source_error": None,
            })
            session_snapshot[key] = snap
        except Exception:
            continue
    return session_snapshot


def build_market_state(
    morning: dict[str, Any],
    midday: dict[str, Any],
    midday_source: str,
    market: dict[str, Any],
    strategy_heat: dict[str, Any],
    market_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_trade_date = str(market.get("latest_trade_date") or "")
    rows = [row for row in (market.get("rows") or []) if str(row.get("trade_date") or "") == latest_trade_date]
    total_sectors = len(rows)
    positive_count = sum(1 for row in rows if (safe_float(row.get("avg_pct_chg"), 0.0) or 0.0) > 0)
    strong_count = sum(1 for row in rows if str(row.get("trend_signal") or "") in {"升温", "拐点", "强势"})
    avg_change = None
    if rows:
        vals = [safe_float(row.get("avg_pct_chg"), 0.0) or 0.0 for row in rows]
        avg_change = round(sum(vals) / len(vals), 4)

    market_top = top_market_rows(market, limit=8)
    strategy_top = top_strategy_rows(strategy_heat, limit=8)
    bottom_rows = sorted(rows, key=lambda row: safe_float(row.get("market_heat_ema_5"), 999.0) or 999.0)[:5]
    bottom_sectors = [{
        "industry": row.get("industry_name"),
        "market_rank": row.get("market_heat_rank"),
        "market_heat": row.get("market_heat_ema_5"),
        "trend_signal": row.get("trend_signal"),
        "avg_pct_chg": row.get("avg_pct_chg"),
    } for row in bottom_rows]

    return {
        "generated_at": now_str(),
        "latest_trade_date": latest_trade_date,
        "midday_source": midday_source,
        "morning": morning,
        "midday": midday,
        "market_summary": {
            "latest_trade_date": latest_trade_date,
            "sector_count": total_sectors,
            "positive_sector_count": positive_count,
            "positive_sector_ratio": round(positive_count / total_sectors, 4) if total_sectors else None,
            "strong_signal_sector_count": strong_count,
            "average_sector_change_pct": avg_change,
            "market_regime": morning.get("regime") or (midday.get("market_view_midday") or {}).get("regime"),
            "risk_score": morning.get("risk_score") or (midday.get("market_view_midday") or {}).get("risk_score"),
        },
        "top_market_sectors": market_top,
        "bottom_market_sectors": bottom_sectors,
        "top_strategy_sectors": strategy_top,
        "industry_actions": market_actions,
        "session_snapshot": refresh_domestic_close(midday.get("session_snapshot") or {}, latest_trade_date),
        "midday_recommended_stocks": midday.get("recommended_stocks_topN") or [],
        "midday_watchlist": midday.get("prebreakout_watchlist_topN") or [],
    }


def build_run_manifest(
    validation: dict[str, Any],
    ai_publish: dict[str, Any],
    orchestrator: dict[str, Any],
    recommendation_db: dict[str, Any],
    market_state: dict[str, Any],
    strategy_state: dict[str, Any],
    candidate_state: dict[str, Any],
    review_state: dict[str, Any],
    research_state: dict[str, Any],
) -> dict[str, Any]:
    run = validation.get("run") or {}
    checks = validation.get("checks") or {}
    manifest_trade_date = resolve_effective_trade_date(
        run.get("trade_date"),
        strategy_state.get("latest_trade_date"),
        market_state.get("latest_trade_date"),
        (market_state.get("midday") or {}).get("trade_date"),
    )
    return {
        "generated_at": now_str(),
        "trade_date": manifest_trade_date,
        "run_id": run.get("run_id") or ((orchestrator.get("run") or {}).get("run_id")),
        "started_at": run.get("started_at") or ((orchestrator.get("run") or {}).get("started_at")),
        "validation_ok": bool(validation.get("ok")),
        "ai_complete": bool(ai_publish.get("ai_complete")),
        "publish_ready": bool(validation.get("publish_ready")),
        "published": bool(ai_publish.get("published")),
        "publish_mode": ai_publish.get("publish_mode"),
        "retained_strategy_count": strategy_state.get("strategy_count") or checks.get("strategy_count"),
        "top20_ready": {
            key: value for key, value in (checks.get("top20_lengths") or {}).items() if key in PUBLIC_STRATEGY_IDS
        },
        "candidate_role_counts": candidate_state.get("role_counts") or {},
        "market_regime": (market_state.get("morning") or {}).get("regime") or ((market_state.get("midday") or {}).get("market_view_midday") or {}).get("regime"),
        "risk_score": (market_state.get("morning") or {}).get("risk_score") or ((market_state.get("midday") or {}).get("market_view_midday") or {}).get("risk_score"),
        "artifacts": {
            "market_state": str(MARKET_STATE_OUT.relative_to(PUBLISHED_REPO)),
            "strategy_state": str(STRATEGY_STATE_OUT.relative_to(PUBLISHED_REPO)),
            "candidate_state": str(CANDIDATE_STATE_OUT.relative_to(PUBLISHED_REPO)),
            "review_state": str(REVIEW_STATE_OUT.relative_to(PUBLISHED_REPO)),
            "research_state": str(RESEARCH_STATE_OUT.relative_to(PUBLISHED_REPO)),
            "system_verdict": str(SYSTEM_VERDICT_OUT.relative_to(PUBLISHED_REPO)),
        },
        "sources": {
            "validation_report_generated_at": validation.get("generated_at"),
            "ai_publish_generated_at": ai_publish.get("generated_at"),
            "orchestrator_generated_at": orchestrator.get("generated_at"),
            "recommendation_db_generated_at": recommendation_db.get("generated_at"),
            "market_generated_at": (market_state.get("morning") or {}).get("generated_at"),
            "midday_generated_at": (market_state.get("midday") or {}).get("generated_at") or (market_state.get("midday") or {}).get("as_of_time"),
            "review_generated_at": review_state.get("generated_at"),
            "research_generated_at": research_state.get("generated_at"),
        },
    }


def build_research_state(research_doc: dict[str, Any], validation: dict[str, Any], review_doc: dict[str, Any]) -> dict[str, Any]:
    trade_date = extract_yyyymmdd((validation.get('run') or {}).get('trade_date'))
    short_term = dict(research_doc.get('short_term') or {})
    long_term = dict(research_doc.get('long_term') or {})
    short_generated = extract_yyyymmdd(short_term.get('generated_at') or research_doc.get('generated_at'))
    long_generated = extract_yyyymmdd(long_term.get('generated_at') or research_doc.get('generated_at'))
    freshness = {
        'current_trade_date': trade_date,
        'source_generated_at': research_doc.get('generated_at'),
        'short_term_generated_at': short_term.get('generated_at') or research_doc.get('generated_at'),
        'long_term_generated_at': long_term.get('generated_at') or research_doc.get('generated_at'),
        'review_generated_at': review_doc.get('generated_at'),
        'validation_generated_at': validation.get('generated_at'),
        'short_term_stale': bool(short_generated and trade_date and short_generated < trade_date),
        'long_term_stale': bool(long_generated and trade_date and long_generated < trade_date),
    }
    warnings = []
    if freshness['short_term_stale']:
        warnings.append('短线研究结果不是当前交易日新鲜产物，当前仅作为研究参考。')
    if freshness['long_term_stale']:
        warnings.append('长线研究结果不是当前交易日新鲜产物，当前仅作为研究参考。')
    return {
        'generated_at': now_str(),
        'validation': validation,
        'short_term': short_term,
        'long_term': long_term,
        'review': review_doc,
        'freshness': freshness,
        'warnings': warnings,
    }


def main() -> int:
    morning = load_json(MORNING_JSON)
    midday, midday_source = choose_midday()
    market = load_json(MARKET_JSON)
    strategy_heat = load_json(STRATEGY_HEAT_JSON)
    detail = load_json(DETAIL_JSON)
    strategy_doc = load_json(STRATEGY_JSON)
    review_doc = load_json(REVIEW_JSON)
    o2c_review_doc = load_json(O2C_REVIEW_JSON)
    research_doc = load_json(RESEARCH_JSON)
    validation = load_json(VALIDATION_JSON)
    ai_publish = load_json(AI_PUBLISH_JSON)
    orchestrator = load_json(ORCHESTRATOR_JSON)
    recommendation_db = load_json(RECOMMENDATION_DB_JSON)

    latest_detail_date, latest_detail_rows = detail_rows_by_latest_date(detail)
    market_top = top_market_rows(market, limit=10)
    strategy_top = top_strategy_rows(strategy_heat, limit=10)
    industry_actions = build_industry_actions(market_top, strategy_top, latest_detail_rows)

    candidate_state = build_candidate_state(strategy_doc, latest_detail_rows, industry_actions)
    strategy_state = build_strategy_state(strategy_doc, strategy_heat, market_top, candidate_state)
    market_state = build_market_state(morning, midday, midday_source, market, strategy_heat, industry_actions)
    review_state = dict(review_doc) if isinstance(review_doc, dict) else {}
    review_state.setdefault("generated_at", now_str())
    o2c_review_state = dict(o2c_review_doc) if isinstance(o2c_review_doc, dict) else {}
    if not o2c_review_state:
        o2c_review_state = build_o2c_review_fallback()
    o2c_review_state.setdefault("generated_at", now_str())
    o2c_review_state.setdefault("strategy_source", "o2c_factor")
    o2c_review_state.setdefault("strategy_name", "O2C日内因子")
    # 写序竞态修复：generate_o2c_review.py 才是 review_gate/date_stats 的权威来源。
    # 若本次要写的内容缺 review_gate，而磁盘上已有的 review_state_o2c.json 更"富"（已带
    # review_gate），说明本次数据来自 fallback 或半成品，跳过写入，避免用较空的数据覆盖
    # generate_o2c_review 写入的真实门槛结论。注意只看 review_gate：date_stats 为空是
    # "0 有效复盘日"的合法输出（review 引擎明确支持的 n/a 场景），不能据此判为半成品。
    existing_o2c_state = load_json(REVIEW_STATE_O2C_OUT)
    new_has_gate = bool(o2c_review_state.get("review_gate"))
    existing_has_gate = isinstance(existing_o2c_state, dict) and bool(existing_o2c_state.get("review_gate"))
    skip_o2c_write = existing_has_gate and not new_has_gate
    if skip_o2c_write:
        print(f"[skip] review_state_o2c.json 本次数据缺 review_gate/date_stats，磁盘已有更完整版本，跳过覆盖写入")
    research_state = build_research_state(research_doc, validation, review_state)
    run_manifest = build_run_manifest(
        validation,
        ai_publish,
        orchestrator,
        recommendation_db,
        market_state,
        strategy_state,
        candidate_state,
        review_state,
        research_state,
    )
    run_manifest["detail_latest_recommend_date"] = latest_detail_date

    write_json(RUN_MANIFEST_OUT, run_manifest)
    write_json(MARKET_STATE_OUT, market_state)
    write_json(STRATEGY_STATE_OUT, strategy_state)
    write_json(CANDIDATE_STATE_OUT, candidate_state)
    write_json(REVIEW_STATE_OUT, review_state)
    if not skip_o2c_write:
        write_json(REVIEW_STATE_O2C_OUT, o2c_review_state)
    write_json(RESEARCH_STATE_OUT, research_state)
    write_system_verdict(
        market_state=market_state,
        strategy_state=strategy_state,
        candidate_state=candidate_state,
        review_state=review_state,
        research_state=research_state,
    )

    print(json.dumps({
        "ok": True,
        "trade_date": run_manifest.get("trade_date"),
        "run_id": run_manifest.get("run_id"),
        "midday_source": midday_source,
        "o2c_write_skipped": skip_o2c_write,
        "written": [
            str(RUN_MANIFEST_OUT),
            str(MARKET_STATE_OUT),
            str(STRATEGY_STATE_OUT),
            str(CANDIDATE_STATE_OUT),
            str(REVIEW_STATE_OUT),
        ] + ([] if skip_o2c_write else [str(REVIEW_STATE_O2C_OUT)]) + [
            str(RESEARCH_STATE_OUT),
            str(SYSTEM_VERDICT_OUT),
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
