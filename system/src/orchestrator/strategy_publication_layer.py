#!/usr/bin/env python3
"""Materialize strategy research stores and the unified publication payloads."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator_common import (
    ARCHIVED_STRATEGIES,
    PUBLISHED_REPO,
    WORKSPACE,
    extract_yyyymmdd,
    resolve_effective_trade_date,
    write_json,
)
from trading_calendar_store import load_open_trade_dates, next_open_trade_date
from immutable_strategy_registry import (
    PREBREAKOUT_CONTROL_ID,
    PREBREAKOUT_CONTROL_STRATEGY_VERSION,
    PREBREAKOUT_LEGACY_ALIAS,
)


WORKING_DIR = WORKSPACE / "stock_data" / "03-working"
WORKING_REPO = WORKING_DIR / "stock-report-repo"
SOURCE_STORE_DIR = WORKING_DIR / "backtest_cache"
AI_ANALYSIS_DIR = WORKING_DIR / "ai_analysis"
FACTOR_OUTPUTS_DIR = WORKSPACE / "factor_factory" / "outputs"
FACTOR_GREENFIELD_PATH = FACTOR_OUTPUTS_DIR / "greenfield_multifactor" / "current_selection_snapshot.json"
FACTOR_T1_STATE_PATH = FACTOR_OUTPUTS_DIR / "t1_alpha191" / "research_state_t1.json"
LEGACY_WAREHOUSE_DB = WORKING_DIR / "recommendation_warehouse" / "recommendations.db"
LOCAL_WAREHOUSE_EXPORT_DIR = WORKING_DIR / "recommendation_warehouse" / "exports"
STRATEGY_STORE_DIR = WORKING_DIR / "strategy_research"
LATEST_DIR = PUBLISHED_REPO / "data" / "latest"
ANALYTICS_DIR = PUBLISHED_REPO / "data" / "recommendation_analytics"

PREBREAKOUT_ID = PREBREAKOUT_LEGACY_ALIAS
O2C_ID = "greenfield_o2c_v1"
T1_ID = "t1_factor_v1"


STRATEGIES = {
    PREBREAKOUT_ID: {
        "strategy_id": PREBREAKOUT_ID,
        "strategy_source": "traditional",
        "strategy_name": "启动前夕 v4.3 对照",
        "canonical_strategy_id": PREBREAKOUT_CONTROL_ID,
        "legacy_source_alias": PREBREAKOUT_LEGACY_ALIAS,
        "strategy_version": PREBREAKOUT_CONTROL_STRATEGY_VERSION,
        "positioning": "技术面启动前夕选股",
        "db_name": "prebreakout_v41.db",
        "evidence_type": "趋势、均线、筹码、事件、AI技术面分析",
    },
    O2C_ID: {
        "strategy_id": O2C_ID,
        "strategy_source": "o2c_factor",
        "strategy_name": "O2C日内因子",
        "strategy_version": "greenfield_o2c_v1",
        "positioning": "开盘到收盘日内因子选股",
        "db_name": "greenfield_o2c_v1.db",
        "evidence_type": "日内因子、因子权重、因子IC、AI日内分析",
    },
    T1_ID: {
        "strategy_id": T1_ID,
        "strategy_source": "t1_factor",
        "strategy_name": "T+1胜率因子",
        "strategy_version": "t1_factor_v1",
        "positioning": "主板+创业板T+1短线胜率策略",
        "db_name": "t1_factor_v1.db",
        "evidence_type": "胜率因子组合、回测证据、风险过滤、AI T+1分析",
    },
}
ACTIVE_STRATEGY_IDS = [
    strategy_id for strategy_id in STRATEGIES if strategy_id not in ARCHIVED_STRATEGIES
]
ARCHIVED_PUBLICATION_STRATEGIES = {
    strategy_id: {
        **STRATEGIES[strategy_id],
        **ARCHIVED_STRATEGIES[strategy_id],
        "active": False,
        "execution_authority": "historical_control_only",
    }
    for strategy_id in STRATEGIES
    if strategy_id in ARCHIVED_STRATEGIES
}
DEFAULT_HOLDING_PERIOD_DAYS = {
    PREBREAKOUT_ID: 5,
    O2C_ID: 1,
    T1_ID: 1,
}
DEFAULT_ROUND_TRIP_COST = 0.003
DEFAULT_BENCHMARK = "all_a_tradable_equal_weight"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_active_strategy_json(strategy_id: str, path: Path, payload: dict[str, Any]) -> bool:
    if strategy_id in ARCHIVED_STRATEGIES:
        return False
    write_json(path, payload)
    return True


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(".")[0] if "." in text else text


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_float(value: Any) -> float | None:
    if value in (None, "", "None", "—"):
        return None
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def load_available_trade_dates() -> list[str]:
    return load_open_trade_dates()


def next_trade_date_from_calendar(base_date: str, trade_dates: list[str] | None = None) -> str | None:
    dates = trade_dates if trade_dates is not None else load_available_trade_dates()
    return next_open_trade_date(base_date, open_dates=dates)


def default_signal_data_cutoff(trade_date: str) -> str | None:
    trade_date = str(trade_date or "")
    if len(trade_date) == 8 and trade_date.isdigit():
        return f"{trade_date}T15:00:00+08:00"
    return None


def default_planned_entry_time(trade_date: str, trade_dates: list[str] | None = None) -> str | None:
    next_trade_date = next_trade_date_from_calendar(trade_date, trade_dates=trade_dates)
    if next_trade_date:
        return datetime.strptime(next_trade_date, "%Y%m%d").strftime("%Y-%m-%dT09:30:00+08:00")
    return None


def materialize_recommendation_contract_fields(
    item: dict[str, Any],
    *,
    strategy_id: str,
    trade_date: str,
    source_date: str,
    trade_dates: list[str] | None = None,
) -> dict[str, Any]:
    data_sources = item.get("data_sources")
    if isinstance(data_sources, str):
        data_sources = [data_sources]
    elif not isinstance(data_sources, list):
        data_sources = [source for source in (item.get("source_path"), item.get("strategy_source"), strategy_id) if source]
    return {
        "strategy_name": STRATEGIES[strategy_id]["strategy_name"],
        "strategy_version": item.get("strategy_version") or STRATEGIES[strategy_id]["strategy_version"],
        "signal_data_cutoff": item.get("signal_data_cutoff") or default_signal_data_cutoff(source_date or trade_date),
        "planned_entry_time": item.get("planned_entry_time") or default_planned_entry_time(source_date or trade_date, trade_dates=trade_dates),
        "holding_period_days": item.get("holding_period_days") or DEFAULT_HOLDING_PERIOD_DAYS[strategy_id],
        "data_sources": [str(source) for source in data_sources if str(source)],
        "used_proxy": bool(item.get("used_proxy")),
        "completeness_status": item.get("completeness_status") or "pending_settlement",
        "round_trip_cost": parse_float(item.get("round_trip_cost")) or DEFAULT_ROUND_TRIP_COST,
        "benchmark": DEFAULT_BENCHMARK if str(item.get("benchmark") or "").strip() in {"", "hs300"} else item.get("benchmark"),
        "settlement_status": item.get("settlement_status") or "pending_settlement",
        "rank_change": int(item.get("rank_change") or 0),
    }
def ensure_strategy_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_run (
            strategy_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            source_database_ref TEXT,
            source_data_date TEXT,
            market_context_json TEXT,
            run_metrics_json TEXT,
            notes_json TEXT,
            PRIMARY KEY (strategy_id, trade_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_snapshot (
            strategy_id TEXT NOT NULL,
            recommend_date TEXT NOT NULL,
            rank_no INTEGER,
            stock_code TEXT NOT NULL,
            ts_code TEXT,
            stock_name TEXT,
            sector_name TEXT,
            raw_action TEXT,
            adjusted_action TEXT,
            adjustment_reason TEXT,
            ai_view TEXT,
            ai_score REAL,
            ai_summary TEXT,
            evidence_json TEXT,
            review_json TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, recommend_date, stock_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_snapshot (
            strategy_id TEXT NOT NULL,
            recommend_date TEXT NOT NULL,
            next_trade_date TEXT,
            sample_count INTEGER,
            hit_rate_pct REAL,
            avg_next_day_return_pct REAL,
            avg_cumulative_return_pct REAL,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, recommend_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adjustment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            recommend_date TEXT,
            stock_code TEXT,
            before_action TEXT,
            after_action TEXT,
            reason TEXT NOT NULL,
            market_context_json TEXT,
            review_context_json TEXT,
            affects_publication INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reflection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            recommend_date TEXT,
            issue TEXT NOT NULL,
            evidence_json TEXT,
            suggestion TEXT,
            accepted INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_context_snapshot (
            trade_date TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    return conn


def upsert_strategy_run(
    conn: sqlite3.Connection,
    strategy: dict[str, Any],
    trade_date: str,
    source_date: str,
    market_context: dict[str, Any],
    metrics: dict[str, Any],
    notes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO strategy_run (
            strategy_id, trade_date, generated_at, source_database_ref, source_data_date,
            market_context_json, run_metrics_json, notes_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, trade_date) DO UPDATE SET
            generated_at=excluded.generated_at,
            source_database_ref=excluded.source_database_ref,
            source_data_date=excluded.source_data_date,
            market_context_json=excluded.market_context_json,
            run_metrics_json=excluded.run_metrics_json,
            notes_json=excluded.notes_json
        """,
        (
            strategy["strategy_id"],
            trade_date,
            now_str(),
            str(SOURCE_STORE_DIR),
            source_date,
            as_json(market_context),
            as_json(metrics),
            as_json(notes),
        ),
    )
    conn.execute(
        """
        INSERT INTO market_context_snapshot (trade_date, generated_at, payload_json)
        VALUES (?, ?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET
            generated_at=excluded.generated_at,
            payload_json=excluded.payload_json
        """,
        (trade_date, now_str(), as_json(market_context)),
    )


def upsert_recommendations(
    conn: sqlite3.Connection,
    strategy_id: str,
    trade_date: str,
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        code = normalize_code(row.get("code") or row.get("stock_code") or row.get("ts_code"))
        if not code:
            continue
        conn.execute(
            """
            INSERT INTO recommendation_snapshot (
                strategy_id, recommend_date, rank_no, stock_code, ts_code, stock_name, sector_name,
                raw_action, adjusted_action, adjustment_reason, ai_view, ai_score, ai_summary,
                evidence_json, review_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, recommend_date, stock_code) DO UPDATE SET
                rank_no=excluded.rank_no,
                ts_code=excluded.ts_code,
                stock_name=excluded.stock_name,
                sector_name=excluded.sector_name,
                raw_action=excluded.raw_action,
                adjusted_action=excluded.adjusted_action,
                adjustment_reason=excluded.adjustment_reason,
                ai_view=excluded.ai_view,
                ai_score=excluded.ai_score,
                ai_summary=excluded.ai_summary,
                evidence_json=excluded.evidence_json,
                review_json=excluded.review_json,
                created_at=excluded.created_at
            """,
            (
                strategy_id,
                trade_date,
                row.get("rank") or row.get("rank_no") or row.get("displayRank"),
                code,
                row.get("ts_code") or row.get("code"),
                row.get("name") or row.get("stock_name"),
                row.get("industry_name") or row.get("industry") or row.get("sector_name"),
                row.get("raw_action") or row.get("role_type") or row.get("action"),
                row.get("adjusted_action"),
                "; ".join(row.get("adjustment_reasons") or []),
                row.get("ai_advice") or row.get("ai_view") or row.get("operation_advice"),
                parse_float(row.get("ai_score")),
                row.get("ai_summary") or row.get("ai_conclusion") or row.get("analysis_summary"),
                as_json(row.get("evidence") or row),
                as_json(row.get("review") or {}),
                now_str(),
            ),
        )


def upsert_reviews(conn: sqlite3.Connection, strategy_id: str, review_doc: dict[str, Any]) -> None:
    for row in review_doc.get("date_stats") or []:
        recommend_date = str(row.get("recommend_date") or "")
        if not recommend_date:
            continue
        conn.execute(
            """
            INSERT INTO review_snapshot (
                strategy_id, recommend_date, next_trade_date, sample_count, hit_rate_pct,
                avg_next_day_return_pct, avg_cumulative_return_pct, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, recommend_date) DO UPDATE SET
                next_trade_date=excluded.next_trade_date,
                sample_count=excluded.sample_count,
                hit_rate_pct=excluded.hit_rate_pct,
                avg_next_day_return_pct=excluded.avg_next_day_return_pct,
                avg_cumulative_return_pct=excluded.avg_cumulative_return_pct,
                payload_json=excluded.payload_json,
                created_at=excluded.created_at
            """,
            (
                strategy_id,
                recommend_date,
                row.get("next_trade_date"),
                int(row.get("sample_count") or 0),
                parse_float(row.get("next_day_hit_rate_pct")),
                parse_float(row.get("avg_next_day_return_pct")),
                parse_float(row.get("avg_cumulative_return_pct")),
                as_json(row),
                now_str(),
            ),
        )


def record_adjustments(
    conn: sqlite3.Connection,
    strategy_id: str,
    rows: list[dict[str, Any]],
    market_context: dict[str, Any],
    review_context: dict[str, Any],
) -> None:
    conn.execute("DELETE FROM adjustment_log WHERE strategy_id = ?", (strategy_id,))
    for row in rows:
        reasons = row.get("adjustment_reasons") or []
        before = row.get("raw_action")
        after = row.get("adjusted_action")
        if not reasons and before == after:
            continue
        conn.execute(
            """
            INSERT INTO adjustment_log (
                generated_at, strategy_id, recommend_date, stock_code, before_action, after_action,
                reason, market_context_json, review_context_json, affects_publication
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                now_str(),
                strategy_id,
                row.get("recommend_date"),
                normalize_code(row.get("code") or row.get("stock_code") or row.get("ts_code")),
                before,
                after,
                "; ".join(reasons) or "保持原动作",
                as_json(market_context),
                as_json(review_context),
            ),
        )


def record_reflection(
    conn: sqlite3.Connection,
    strategy_id: str,
    recommend_date: str,
    review_doc: dict[str, Any],
    market_context: dict[str, Any],
) -> None:
    conn.execute("DELETE FROM reflection_log WHERE strategy_id = ?", (strategy_id,))
    perf = review_doc.get("performance") or {}
    hit = parse_float(perf.get("next_day_hit_rate_pct"))
    avg = parse_float(perf.get("avg_next_day_return_pct"))
    if hit is None:
        issue = "复盘样本不足"
        suggestion = "保持观察权重，不自动增强。"
    elif hit < 50 or (avg is not None and avg < 0):
        issue = "近期复盘表现偏弱"
        suggestion = "降低策略权重，优先保留共识股与高AI一致性样本。"
    else:
        issue = "近期复盘表现可用"
        suggestion = "允许在市场环境配合时参与排序增强。"
    conn.execute(
        """
        INSERT INTO reflection_log (
            generated_at, strategy_id, recommend_date, issue, evidence_json, suggestion, accepted
        ) VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (now_str(), strategy_id, recommend_date, issue, as_json({"review": review_doc, "market": market_context}), suggestion),
    )


def action_from_advice(value: Any, fallback: str = "watch") -> str:
    text = str(value or "")
    if "买入" in text or "主攻" in text:
        return "main"
    if "回避" in text or "剔除" in text:
        return "avoid"
    if "观察" in text or "观望" in text or "持有" in text:
        return "watch"
    return fallback


def effective_risk_score(market_context: dict[str, Any], conservative_default: float = 100.0) -> float:
    """风险分缺失（None）时返回保守高值：触发全部「高风险降级」、不触发任何「低风险上调」，
    避免 None 参与数值比较崩溃，且绝不把缺失伪装成「中性可交易(50)」。risk_score=0 等合法低值原样返回。"""
    rs = market_context.get("risk_score")
    return conservative_default if rs is None else rs


def _market_gate_status(mc: dict[str, Any]) -> str:
    """市场闸门状态：风险分缺失/高位→warn/fail，否则按 risk_score 分档。供 decision_state.gates.market。"""
    if not mc.get("risk_data_available"):
        return "warn"
    rs = mc.get("risk_score")
    if rs is None:
        return "warn"
    return "fail" if rs >= 65 else ("warn" if rs >= 50 else "pass")


# 策略硬门槛阈值（#7 诚实化 2026-06）：任一【可评估且不达标】项即把该策略压成「研究观察」、不进可交易区。
GATE_AI_COVERAGE_MIN = 0.6      # top20 真实 AI 覆盖率下限
GATE_OOS_ICIR_MIN = 0.30        # 样本外 ICIR 下限（仅对已计算 signal_gate 的策略，如 T1）
GATE_REVIEW_SAMPLES_MIN = 3     # 复盘样本（交易日）下限——无足够 track record 不进可交易区


def _gate_value(v: Any) -> bool:
    return v not in (None, "", [], {})


def _row_ai_counted(r: dict[str, Any]) -> bool:
    """该股是否计入真实 AI 覆盖。
    传统/启动前夕：以 ai_score 非空为准。
    O2C 结构化 AI：ai_score 故意为 null（不造假分），改以「有真实 o2c_ai_analysis 文本」为准——
    需同时满足 ai_source_kind==o2c_ai_analysis 且 ai_summary 非空，模板/未覆盖/失败均不计。
    """
    if _gate_value(r.get("ai_score")):
        return True
    if str(r.get("ai_source_kind") or "") == "o2c_ai_analysis" and _gate_value(r.get("ai_summary")):
        return True
    return False


def compute_strategy_gate(
    strategy_id: str,
    rows: list[dict[str, Any]],
    review_doc: dict[str, Any],
    source_date: str,
    trade_date: str,
    signal_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一硬门槛（#7）：对每套策略评估 ① AI 覆盖率 ② 数据新鲜度 ③ 复盘样本数 ④ 样本外 ICIR ⑤ 净超额。
    任一【可评估(status!=n/a)且 fail】项即 research_only=True（只显示研究观察、不发可交易建议）。
    某策略未计算的项（如 prebreakout/O2C 暂无样本外 ICIR）记 n/a、不阻断但如实公示缺口。"""
    gates: dict[str, dict[str, Any]] = {}

    n = len(rows)
    have_ai = sum(1 for r in rows if _row_ai_counted(r))
    cov = (have_ai / n) if n else 0.0
    gates["ai_coverage"] = {
        "value": round(cov, 3), "have": have_ai, "total": n, "threshold": GATE_AI_COVERAGE_MIN,
        "status": ("pass" if cov >= GATE_AI_COVERAGE_MIN else "fail") if n else "n/a",
    }

    fresh = bool(source_date) and str(source_date) == str(trade_date)
    gates["data_freshness"] = {
        "source_date": str(source_date), "trade_date": str(trade_date),
        "status": "pass" if fresh else "fail",
    }

    samples = len(review_doc.get("date_stats") or [])
    gates["review_samples"] = {
        "value": samples, "threshold": GATE_REVIEW_SAMPLES_MIN,
        "status": "pass" if samples >= GATE_REVIEW_SAMPLES_MIN else "fail",
    }

    icir = (signal_gate or {}).get("oos_icir")
    net = (signal_gate or {}).get("top20_net_excess_mean_daily")
    if icir is not None:
        gates["oos_icir"] = {
            "value": icir, "threshold": GATE_OOS_ICIR_MIN,
            "status": "pass" if icir > GATE_OOS_ICIR_MIN else "fail",
        }
        gates["net_excess"] = {
            "value": net, "status": "pass" if (net is not None and net > 0) else "fail",
        }
    else:
        gates["oos_icir"] = {"status": "n/a", "note": "该策略暂未计算样本外 ICIR（缺真·样本外验证）"}
        gates["net_excess"] = {"status": "n/a"}

    # ⑥ REVIEW_GATE（升级门槛）：由 generate_o2c_review 预算入 review_state 的 review_gate
    # （valid_review_days>=3 且 平均超额>0 且 命中率>=50 且 最近3有效日不连续为负）。只有提供该对象的
    # 策略（当前为 O2C）参与；缺失则 n/a、不影响 prebreakout/T1。状态 fail 即压研究观察。
    _rg = review_doc.get("review_gate")
    if isinstance(_rg, dict) and _rg.get("status") in ("pass", "fail"):
        gates["review_gate"] = _rg
    else:
        gates["review_gate"] = {"status": "n/a", "note": "无复盘升级门槛数据（未跑 generate_o2c_review 或样本为 0）"}

    failed = [k for k, v in gates.items() if v.get("status") == "fail"]
    research_only = bool(failed)
    return {
        "strategy_id": strategy_id,
        "research_only": research_only,
        "verdict": "research_only" if research_only else "tradeable",
        "failed_gates": failed,
        "gates": gates,
        "note": (
            "硬门槛未全部达标→仅研究观察、不构成可交易建议：" + "、".join(failed)
            if research_only else "硬门槛全部达标（可评估项）"
        ),
    }


def strategy_contract_fields(gate: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """合同 v2：从硬门槛结果 + 行集产出每策略统一字段（recommendation_state / strategy_run_state 共用）。
    ai_analyzed_count 用 ai_coverage_counted（与 gate.ai_coverage.have 同口径；T1 模板 ai_score=None 不计）。"""
    g = gate.get("gates") or {}
    return {
        "research_only": gate["research_only"],
        "research_mode": gate["research_only"],  # 兼容旧名
        "strategy_gate": {
            "status": "pass" if not gate["research_only"] else "warn",
            "verdict": gate["verdict"],
            "failed_gates": gate["failed_gates"],
            "gates": g,
        },
        "ai_coverage": g.get("ai_coverage", {}),
        "data_freshness": g.get("data_freshness", {}),
        "review_gate": _review_gate_public(g.get("review_gate") or {}),
        "item_count": len(rows),
        "ai_required_count": len(rows),
        "ai_analyzed_count": sum(1 for r in rows if r.get("ai_coverage_counted")),
    }


def o2c_allow_ai_pending_flag(o2c_fields: dict[str, Any]) -> bool:
    """O2C AI 未满 20/20 时必须显式 allow_o2c_ai_pending，否则 pre-push 合同 v2 会拒绝整仓推送。

    历史事故(20260715): 启动前夕 AI 已满、O2C 回填被 SIGTERM 杀掉 → O2C 覆盖 0/20 且无
    allow_pending → git push 连拒 → 线上 Pages/S3 名单停更。底座与 S3 不得被 O2C 单策略 AI
    缺口整仓锁死；合同校验器已支持该显式 pending 放行，此处由发布层自动写入、诚实公示。
    """
    cov = o2c_fields.get("ai_coverage") or {}
    try:
        have = int(cov.get("have") or 0)
        total = int(cov.get("total") or o2c_fields.get("item_count") or 0)
    except (TypeError, ValueError):
        have, total = 0, 0
    if total <= 0:
        return True
    return have < total


def _review_gate_public(rg: dict[str, Any]) -> dict[str, Any]:
    """对外发布的 review_gate 子集（用户合同 schema）。无数据时给 n/a 占位，前端可显示「样本不足」。"""
    status = rg.get("status") or "n/a"
    return {
        "valid_review_days": rg.get("valid_review_days", 0),
        "required_review_days": rg.get("required_review_days", 3),
        "hit_rate_pct": rg.get("hit_rate_pct"),
        "avg_excess_return_pct": rg.get("avg_excess_return_pct"),
        "information_ratio": rg.get("information_ratio"),
        "ir_min": rg.get("ir_min"),
        "horizon_label": rg.get("horizon_label"),
        "avg_next_day_return_pct": rg.get("avg_next_day_return_pct"),
        "recent_consecutive_negative": rg.get("recent_consecutive_negative"),
        "status": status,
        "summary": rg.get("summary") or "尚无复盘升级门槛数据",
        "benchmark_note": rg.get("benchmark_note"),
    }


def load_close_day_actuals(trade_date: str) -> dict[str, Any] | None:
    """读当日 daily 缓存算【收盘实况】(涨跌面/均涨幅/涨跌停数)。缓存缺失或当日无数据返回 None。
    20260703事故修复: 收盘定调必须由当日实况驱动,不得继承早晨盘前的隔夜外盘评估。"""
    p = WORKING_DIR / "backtest_cache" / f"daily_{trade_date}.parquet"
    if not p.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(p, columns=["ts_code", "pct_chg", "close"])
        df = df[pd.to_numeric(df["close"], errors="coerce") > 0]
        pc = pd.to_numeric(df["pct_chg"], errors="coerce").dropna()
        if len(pc) < 1000:
            return None
        return {
            "n": int(len(pc)),
            "breadth": round(float((pc > 0).mean()), 4),
            "avg_pct": round(float(pc.mean()), 3),
            "limit_up": int((pc >= 9.5).sum()),
            "limit_down": int((pc <= -9.5).sum()),
        }
    except Exception:
        return None


def close_gate_from_actuals(a: dict[str, Any]) -> tuple[str, float, str, str, str, str]:
    """由收盘实况映射闸门(阈值为未经调参的透明先验,写死留痕):
    risk_off: 涨跌面<30% 或 均涨幅<-2% 或 跌停>=120; cautious: <42%/-0.8%/>=60;
    risk_on: 涨跌面>=62%且均涨幅>=+0.5%; 其余 neutral。返回(gate,score,cycle,high_low,policy,limit)。"""
    b, m, ld = a["breadth"], a["avg_pct"], a["limit_down"]
    if b < 0.30 or m < -2.0 or ld >= 120:
        return ("risk_off", 75.0, "退潮/高风险", "高风险区", "买入整体降级为观察，日内冲动信号禁止执行。", "空仓或极轻仓")
    if b < 0.42 or m < -0.8 or ld >= 60:
        return ("cautious", 60.0, "谨慎/偏弱", "中高风险区", "买入信号降级，日内冲动信号不得放大。", "轻仓")
    if b >= 0.62 and m >= 0.5:
        return ("risk_on", 25.0, "修复/偏积极", "低风险区", "允许共识股和高一致性信号增强。", "满仓可用")
    return ("neutral", 45.0, "震荡/中性", "中位震荡区", "维持中性权重，以共识和AI一致性排序。", "标准仓")


def build_market_context(
    run_manifest: dict[str, Any],
    market_state: dict[str, Any],
    system_verdict: dict[str, Any],
) -> dict[str, Any]:
    market_gate = (system_verdict.get("gates") or {}).get("market_gate") or {}
    evidence = market_gate.get("evidence") or {}
    raw_risk_score = (
        market_state.get("market_summary", {}).get("risk_score")
        or market_state.get("morning", {}).get("risk_score")
        or evidence.get("risk_score")
        or run_manifest.get("risk_score")
    )
    # 市场风险分缺失时不再硬填 50（伪装成「中性·标准仓」的真实评估）；标记数据缺失并走保守闸门。
    try:
        risk_score = float(raw_risk_score) if raw_risk_score is not None else None
    except (TypeError, ValueError):
        risk_score = None
    risk_data_available = risk_score is not None
    regime = (
        market_state.get("market_summary", {}).get("market_regime")
        or market_state.get("morning", {}).get("regime")
        or evidence.get("regime")
        or run_manifest.get("market_regime")
        or "中性"
    )

    # Market gate v2(20260703事故修复): 收盘后定调必须由【当日收盘实况】驱动。
    # 事故: 7/3实际普涨(上证+0.37%,69%上涨),但晚间发布继承早晨盘前A50代理评估(70分)判risk_off/空仓。
    # 修复: daily缓存有当日实况→按实况定闸门,盘前评估降级为preopen参考字段;实况缺失才回退旧链路。
    close_actuals = load_close_day_actuals(str(run_manifest.get("trade_date") or ""))
    preopen_risk_score = risk_score  # 早晨盘前链路的评估,留痕
    verdict_basis = "preopen_external_fallback"
    if close_actuals is not None:
        gate_signal, risk_score, cycle, high_low, policy, position_limit = close_gate_from_actuals(close_actuals)
        risk_data_available = True
        verdict_basis = "close_actuals"
        regime = {"risk_on": "偏积极", "neutral": "中性", "cautious": "谨慎", "risk_off": "防御"}[gate_signal]
    # Market gate v1: standardized risk assessment (仅在收盘实况缺失时走)
    elif not risk_data_available:
        # 风险分缺失：保守失败（fail-safe），明确标注数据缺失，不当中性可执行处理。
        gate_signal = "unknown"
        cycle = "数据缺失/保守"
        high_low = "风险未知（市场数据缺失）"
        policy = "市场风险数据缺失，保守处理：买入信号统一降级为观察，日内冲动信号不得放大。"
        position_limit = "轻仓（数据缺失保守）"
    elif risk_score <= 25:
        gate_signal = "risk_on"
        cycle = "修复/偏积极"
        high_low = "低风险区"
        policy = "允许共识股和高一致性信号增强。"
        position_limit = "满仓可用"
    elif risk_score <= 45:
        gate_signal = "neutral"
        cycle = "震荡/中性"
        high_low = "中位震荡区"
        policy = "维持中性权重，以共识和AI一致性排序。"
        position_limit = "标准仓"
    elif risk_score <= 65:
        gate_signal = "cautious"
        cycle = "谨慎/偏弱"
        high_low = "中高风险区"
        policy = "买入信号降级，日内冲动信号不得放大。"
        position_limit = "轻仓"
    else:
        gate_signal = "risk_off"
        cycle = "退潮/高风险"
        high_low = "高风险区"
        policy = "买入整体降级为观察，日内冲动信号禁止执行。"
        position_limit = "空仓或极轻仓"

    # External factors (A50, US markets, etc.)
    morning = market_state.get("morning", {})
    a50_change = parse_float(morning.get("a50_change_pct"))
    golden_dragon = parse_float(morning.get("golden_dragon_change_pct"))

    external_summary = []
    if a50_change is not None:
        external_summary.append(f"A50隔夜{'+' if a50_change >= 0 else ''}{a50_change:.2f}%(盘前参考,美股ETF代理或存偏差)")
    if golden_dragon is not None:
        external_summary.append(f"金龙指数{'+' if golden_dragon >= 0 else ''}{golden_dragon:.2f}%(盘前参考)")

    return {
        "generated_at": now_str(),
        "trade_date": run_manifest.get("trade_date"),
        "gate_signal": gate_signal,
        "risk_score": risk_score,
        "risk_data_available": risk_data_available,
        "verdict_basis": verdict_basis,
        "close_actuals": close_actuals,
        "preopen_risk_score": preopen_risk_score,
        "regime": regime,
        "market_position": high_low,
        "market_cycle": cycle,
        "position_limit": position_limit,
        "sentiment": (
            "未知（市场数据缺失）"
            if not risk_data_available
            else ("偏积极" if risk_score <= 35 else ("偏谨慎" if risk_score >= 65 else "中性"))
        ),
        "external_factors": {
            "status": "connected" if external_summary else "not_connected",
            "summary": "；".join(external_summary) if external_summary else "场外因素暂未形成结构化数据，自动调整不会因此放大信号。",
            "a50_change_pct": a50_change,
            "golden_dragon_change_pct": golden_dragon,
        },
        "policy": policy,
        "source": {
            "run_manifest": "data/latest/run_manifest.json",
            "market_state": "data/latest/market_state.json",
            "system_verdict": "data/latest/system_verdict.json",
            "source_database_ref": str(SOURCE_STORE_DIR),
        },
    }


def latest_review_context(review_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "latest_recommend_date": review_doc.get("latest_recommend_date"),
        "latest_raw_recommend_date": review_doc.get("latest_raw_recommend_date"),
        "latest_date_row_count": review_doc.get("latest_date_row_count"),
        "performance": review_doc.get("performance") or {},
        "date_stats_count": len(review_doc.get("date_stats") or []),
    }


def strategy_weights(
    trade_date: str,
    pre_review: dict[str, Any],
    o2c_review: dict[str, Any],
    t1_review: dict[str, Any],
    greenfield: dict[str, Any],
    t1_state: dict[str, Any],
    market_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        PREBREAKOUT_ID: 1.0,
        O2C_ID: 0.0,
        T1_ID: 0.0,
        "reasons": [
            {"strategy_id": O2C_ID, "reason": "策略已归档，只保留历史对照，不参与当前排序。"},
            {"strategy_id": T1_ID, "reason": "策略已归档，只保留历史对照，不参与当前排序。"},
        ],
    }

def _is_gem(code: str) -> bool:
    """Return True if stock_code is a ChiNext (创业板) ticker (300xxx / 301xxx)."""
    return bool(code) and (code.startswith("300") or code.startswith("301"))


def _record_adjustment_reason(
    reasons: list[str], adjustment_log: list[dict[str, Any]],
    strategy_id: str, code: str, trade_date: str,
    before: str, after: str, reason: str,
) -> None:
    """Append reason and keep an in-memory audit trail."""
    reasons.append(reason)
    if before != after:
        adjustment_log.append({
            "strategy_id": strategy_id,
            "stock_code": code,
            "recommend_date": trade_date,
            "before_action": before,
            "after_action": after,
            "reason": reason,
        })


def apply_adjustments(
    rows: list[dict[str, Any]],
    *,
    strategy_id: str,
    trade_date: str,
    source_date: str,
    market_context: dict[str, Any],
    weight: float,
    overlap: set[str],
    multi_overlap: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply market / staleness / GEM / consensus adjustments.

    Returns ``(adjusted_rows, adjustment_audit)``.
    """
    adjusted_rows: list[dict[str, Any]] = []
    adjustment_audit: list[dict[str, Any]] = []
    stale = bool(trade_date and source_date and source_date != trade_date)
    risk_score = effective_risk_score(market_context)  # 缺失按最高风险保守处理，不再默认 50
    sentiment = market_context.get("sentiment", "中性")
    market_cycle = market_context.get("market_cycle", "震荡/中性")

    for idx, item in enumerate(rows, start=1):
        code = normalize_code(item.get("code") or item.get("stock_code") or item.get("ts_code"))
        raw_action = (
            item.get("role_type")
            or item.get("raw_action")
            or item.get("action")
            or action_from_advice(item.get("ai_advice") or item.get("ai_view") or item.get("operation_advice"))
        )
        raw_action = "main" if raw_action in ("buy", "主攻", "买入") else ("avoid" if raw_action in ("回避", "avoid") else ("watch" if raw_action in ("observe", "观望", "观察") else raw_action))
        if raw_action not in {"main", "watch", "avoid"}:
            raw_action = action_from_advice(item.get("ai_advice") or item.get("ai_view") or item.get("operation_advice"), "watch")
        adjusted = raw_action
        reasons: list[str] = []
        advice = str(item.get("ai_advice") or item.get("ai_view") or item.get("operation_advice") or "")
        position_tier = 1  # 1=strongest, 3=weakest

        # --- Rule 1: Data stale ---
        if stale:
            if adjusted == "main":
                adjusted = "watch"
            reasons.append("策略源数据日期与决策日不一致，只作参考。")

        # --- Rule 2: Market high risk + sentiment overheated → downgrade chase-high ---
        if risk_score >= 65 and sentiment in ("偏谨慎", "过热"):
            if adjusted == "main":
                adjusted = "watch"
            reasons.append("市场高位+情绪过热，追高信号降级。")

        # --- Rule 3: Market declining → all buy signals downgraded ---
        if market_cycle in ("退潮/高风险", "持续下跌") and adjusted == "main":
            adjusted = "watch"
            reasons.append("市场持续下跌，买入信号全面降级。")

        # --- Rule 4: Market low + sentiment improving → upgrade consensus stocks ---
        if risk_score <= 35 and sentiment == "偏积极" and code in overlap:
            position_tier = max(1, position_tier - 1)
            reasons.append("市场低位+情绪改善，共识股获得优先级提升。")

        # --- Rule 5: Generic high-risk downgrade (existing rule) ---
        if risk_score >= 65 and adjusted == "main" and "市场处于高风险区" not in "; ".join(reasons):
            adjusted = "watch"
            reasons.append("市场处于高风险区，买入信号降级。")

        # --- Rule 6: AI advice inconsistency ---
        if adjusted == "main" and advice and not ("买入" in advice or "主攻" in advice):
            adjusted = "watch"
            reasons.append("AI建议与买入动作不一致，降为观察。")

        # --- Rule 7: GEM (创业板) stocks auto-downgrade position tier ---
        if _is_gem(code):
            position_tier = min(3, position_tier + 1)
            reasons.append("创业板股票，仓位层级自动下调一级。")

        # --- Consensus / multi-overlap ---
        if multi_overlap and code in multi_overlap:
            position_tier = max(1, position_tier - 1)
            reasons.append("同时进入三套策略名单，获得最高共识优先级。")
        elif code in overlap:
            reasons.append("同时进入多套策略名单，获得共识优先级。")

        if not reasons:
            reasons.append("保持原策略动作。")

        score = parse_float(item.get("ai_score")) or parse_float(item.get("score")) or 0
        rank = int(item.get("rank") or item.get("rank_no") or idx)

        # Derive execution fields — ALL stocks must have execution fields
        raw_buy = item.get("buy_zone") or item.get("trigger_price") or item.get("entry_range") or ""
        raw_inv = item.get("invalidation") or item.get("stop_loss") or item.get("loss_stop") or ""
        raw_ndh = item.get("next_day_handling") or item.get("next_day_plan") or ""

        current_price = parse_float(item.get("current_price") or item.get("price") or item.get("close"))
        low_price = parse_float(item.get("low") or item.get("signal_day_low"))
        ma5 = parse_float(item.get("ma5") or item.get("ma_5"))

        # Buy zone: all stocks must have buy zone
        if raw_buy:
            buy_zone = raw_buy
        elif adjusted == "main" and current_price:
            buy_zone = f"次日开盘涨幅不超过3%买入，参考价{current_price:.2f}附近；涨停封死不追"
        elif adjusted == "main":
            buy_zone = "次日开盘涨幅不超过3%，且未封死涨停"
        elif adjusted == "watch" and current_price:
            buy_zone = f"观察为主，若突破关键位{current_price:.2f}可考虑轻仓试探"
        elif adjusted == "watch":
            buy_zone = "观察为主，等待明确突破信号再动作"
        else:
            buy_zone = "回避，不参与"

        # Invalidation: all stocks must have invalidation
        if raw_inv:
            invalidation = raw_inv
        elif adjusted == "main" and low_price:
            invalidation = f"跌破信号日低点{low_price:.2f}"
        elif adjusted == "main" and ma5:
            invalidation = f"跌破5日均线{ma5:.2f}"
        elif adjusted == "main":
            invalidation = "跌破信号日低点，或跌破5日均线，或高开后快速回落"
        elif adjusted == "watch" and low_price:
            invalidation = f"若跌破{low_price:.2f}则彻底放弃"
        elif adjusted == "watch":
            invalidation = "跌破关键支撑位则放弃跟踪"
        else:
            invalidation = "已回避，无需止损"

        # Next day handling: all stocks must have next day handling
        if raw_ndh:
            next_day_handling = raw_ndh
        elif adjusted == "main":
            next_day_handling = "T+1买入后，T+2根据开盘强弱和失效条件处理；开盘弱势或触及止损则退出"
        elif adjusted == "watch":
            next_day_handling = "观察为主，等待确认信号再动作；若放量突破可轻仓试探"
        else:
            next_day_handling = "回避，不参与"

        adjusted_rows.append({
            **item,
            **materialize_recommendation_contract_fields(
                item,
                strategy_id=strategy_id,
                trade_date=trade_date,
                source_date=source_date,
            ),
            "strategy_id": strategy_id,
            "recommend_date": trade_date,
            "source_date": source_date,
            "rank_no": rank,
            "stock_code": code,
            "raw_action": raw_action,
            "adjusted_action": adjusted,
            # 动作三段式（合同 v2）：raw（原始）→ market_adjusted（市场/staleness/共识规则后）
            # → gate_adjusted（策略硬门槛后）→ final（最终对外）。初始三者相等，gate 阶段可能改写。
            "market_adjusted_action": adjusted,
            "gate_adjusted_action": adjusted,
            "final_action": adjusted,
            "adjustment_reasons": reasons,
            "strategy_weight": weight,
            "consensus": code in overlap or (multi_overlap is not None and code in multi_overlap),
            "position_tier": position_tier,
            "buy_zone": buy_zone,
            "invalidation": invalidation,
            "next_day_handling": next_day_handling,
            "publication_score": round(score * weight + (8 if code in overlap or (multi_overlap is not None and code in multi_overlap) else 0) - rank * 0.05, 4),
        })

    return adjusted_rows, adjustment_audit


def detail_rows_by_latest(path: Path, latest_date: str | None = None) -> list[dict[str, Any]]:
    doc = load_json(path, {})
    rows = doc.get("rows") or []
    if not isinstance(rows, list):
        return []
    target = latest_date or doc.get("latest_recommend_date")
    if target:
        return [row for row in rows if str(row.get("recommend_date") or "") == str(target)]
    return rows[:20]


def factor_ai_entry_to_fields(entry: dict[str, Any], strategy_source: str, trade_date: str) -> dict[str, Any]:
    dashboard = entry.get("dashboard") or {}
    core = dashboard.get("core_conclusion") or {}
    intelligence = dashboard.get("intelligence") or {}
    battle = dashboard.get("battle_plan") or {}
    sniper = battle.get("sniper_points") or {}
    chip = (dashboard.get("data_perspective") or {}).get("chip_structure") or {}
    summary = core.get("one_sentence") or entry.get("analysis_summary") or entry.get("ai_summary") or ""
    ai_points = "\n".join(
        [
            f"板块定位：{entry.get('sector_position') or entry.get('company_highlights') or '未提供板块相对强弱'}",
            "筹码判断："
            f"{chip.get('chip_health') or '未提供完整筹码分布'}；"
            f"均价参考 {chip.get('avg_cost') or entry.get('current_price') or '待补充'}；"
            f"获利盘参考 {chip.get('profit_ratio') or '待补充'}",
            f"事件催化：{intelligence.get('latest_news') or entry.get('news_summary') or '待补充'}",
            f"触发条件：{sniper.get('ideal_buy') or entry.get('buy_reason') or entry.get('operation_advice') or '等待确认信号'}",
            f"失效条件：{sniper.get('stop_loss') or entry.get('risk_warning') or '按策略风控执行'}",
        ]
    )
    decision = core.get("signal_type") or entry.get("decision_type") or entry.get("ai_decision") or ""
    score = core.get("sentiment_score")
    if score is None:
        score = entry.get("sentiment_score") or entry.get("ai_score")
    # O2C 结构化 AI 校验要求 ai_score 为 null（不造假分），以文本质量为准。
    if strategy_source == "o2c_factor":
        score = None
    return {
        **entry,
        "code": normalize_code(entry.get("code") or entry.get("stock_code") or entry.get("ts_code")),
        "strategy_source": strategy_source,
        "strategy_name": "O2C日内因子" if strategy_source == "o2c_factor" else ("T+1胜率因子" if strategy_source == "t1_factor" else entry.get("strategy_name")),
        "ai_score": score,
        "ai_advice": entry.get("operation_advice") or entry.get("ai_advice"),
        "operation_advice": entry.get("operation_advice") or entry.get("ai_advice"),
        "ai_decision": decision,
        "ai_confidence": entry.get("confidence_level") or entry.get("ai_confidence"),
        "ai_summary": summary,
        "ai_conclusion": entry.get("analysis_summary") or summary,
        "ai_points": entry.get("ai_points") or ai_points,
        "ai_trend": entry.get("trend_analysis"),
        "ai_ma": entry.get("ma_analysis"),
        "ai_volume": entry.get("volume_analysis"),
        "ai_fundamental": entry.get("fundamental_analysis"),
        "ai_risk_warning": entry.get("risk_warning"),
        "ai_risks": entry.get("ai_risks"),
        "ai_news": entry.get("news_summary"),
        # O2C 结构化生成器写入的真实标识需原样保留；其它（daily_stock_analysis 深度分析）走 *_deep_analysis 默认。
        # 桥接脚本产出的 o2c_factor_llm_analysis 映射为校验期望的 o2c_ai_analysis。
        "ai_source_kind": (entry.get("ai_source_kind") or f"{strategy_source}_deep_analysis").replace("o2c_factor_llm_analysis", "o2c_ai_analysis"),
        "ai_source_name": entry.get("source") or strategy_source,
        "ai_source_date": str(entry.get("trade_date") or entry.get("ai_analysis_date") or trade_date),
        "ai_analysis_date": str(entry.get("trade_date") or entry.get("ai_analysis_date") or trade_date),
    }


def ai_lookup_for_date(trade_date: str, strategy_source: str) -> dict[str, dict[str, Any]]:
    candidate_paths = [AI_ANALYSIS_DIR / f"{trade_date}.json"]
    if strategy_source == "o2c_factor":
        candidate_paths.extend([
            AI_ANALYSIS_DIR / f"{trade_date}_o2c_greenfield.json",
            AI_ANALYSIS_DIR / f"{trade_date}_factor_combined.json",
        ])
    elif strategy_source == "t1_factor":
        candidate_paths.extend([
            AI_ANALYSIS_DIR / f"{trade_date}_t1_alpha191.json",
            AI_ANALYSIS_DIR / f"{trade_date}_factor_combined.json",
        ])

    lookup: dict[str, dict[str, Any]] = {}
    for path in candidate_paths:
        rows = load_json(path, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_source = row.get("strategy_source")
            legacy_source = row.get("source")
            normalized_source = row_source or ({
                "o2c_greenfield": "o2c_factor",
                "t1_alpha191": "t1_factor",
            }.get(str(legacy_source), "traditional"))
            if strategy_source == "traditional":
                if normalized_source in ("o2c_factor", "t1_factor"):
                    continue
            elif normalized_source != strategy_source:
                continue
            code = normalize_code(row.get("code") or row.get("stock_code") or row.get("ts_code"))
            if not code:
                continue
            lookup[code] = factor_ai_entry_to_fields(row, normalized_source, trade_date)
    return lookup


AI_ERROR_MARKERS = (
    "分析过程出错",
    "All LLM models failed",
    "Traceback (most recent call last)",
    "No module named",
    "LLM returned empty response",
)
AI_TEXT_FIELDS = ("ai_summary", "ai_conclusion", "ai_points", "ai_advice", "ai_trend",
                  "ai_volume", "ai_ma", "ai_chip", "ai_event", "ai_sector", "ai_risk_warning")
AI_FALLBACK_TEXT = "AI分析暂缺（上次分析失败，待补齐）"


def sanitize_ai_text_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Never publish raw LLM error traces to public JSON (honesty fix 2026-06-12)."""
    for key in AI_TEXT_FIELDS:
        value = row.get(key)
        if isinstance(value, str) and any(marker in value for marker in AI_ERROR_MARKERS):
            row[key] = AI_FALLBACK_TEXT
    return row


def rows_all_zero_scores(rows: list[dict[str, Any]]) -> bool:
    """True when a non-empty list carries only zero/None scores (invalid research output)."""
    if not rows:
        return False
    try:
        return all(abs(float((r or {}).get("score", 0) or 0)) < 1e-9 for r in rows)
    except (TypeError, ValueError):
        return False


def merge_strategy_ai(items: list[dict[str, Any]], trade_date: str, strategy_source: str) -> list[dict[str, Any]]:
    if not trade_date:
        return items
    lookup = ai_lookup_for_date(trade_date, strategy_source)
    if not lookup:
        return items
    ai_fields = {
        "ai_advice",
        "ai_confidence",
        "ai_conclusion",
        "ai_score",
        "ai_source_kind",
        "ai_source_name",
        "ai_summary",
        "ai_points",
        "ai_trend",
        "ai_volume",
        "ai_ma",
        "ai_chip",
        "ai_event",
        "ai_sector",
        "ai_risk_warning",
        "ai_risks",
        "ai_o2c_top_factors",
        "ai_o2c_factor_score",
        "ai_o2c_driver_note",
        "ai_o2c_risk_note",
        "operation_advice",
        "strategy_source",
        "strategy_name",
    }
    merged: list[dict[str, Any]] = []
    for item in items:
        code = normalize_code(item.get("code") or item.get("stock_code") or item.get("ts_code"))
        ai_row = lookup.get(code)
        if not ai_row:
            merged.append(item)
            continue
        enriched = dict(item)
        for key in ai_fields:
            if key in ai_row and ai_row.get(key) not in (None, ""):
                enriched[key] = ai_row[key]
        if not enriched.get("name") and ai_row.get("name"):
            enriched["name"] = ai_row["name"]
        if not enriched.get("stock_name") and ai_row.get("name"):
            enriched["stock_name"] = ai_row["name"]
        merged.append(sanitize_ai_text_fields(enriched))
    return merged


def source_date(payload: dict[str, Any]) -> str:
    return str(payload.get("latest_trade_date") or payload.get("trade_date") or payload.get("recommend_date") or "")


def load_best_payload(primary: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    primary_doc = load_json(primary, {}) if primary.exists() else {}
    if not isinstance(primary_doc, dict):
        primary_doc = {}
    primary_date = source_date(primary_doc)
    fallback_date = source_date(fallback) if isinstance(fallback, dict) else ""
    if primary_date and (not fallback_date or primary_date >= fallback_date):
        return primary_doc
    return fallback if isinstance(fallback, dict) else primary_doc


def strategy_doc_trade_date(doc: dict[str, Any]) -> str:
    date = extract_yyyymmdd(doc.get("latest_trade_date") or doc.get("trade_date"))
    if date:
        return date
    strategies = doc.get("strategies") or []
    dates = []
    if isinstance(strategies, list):
        for strategy in strategies:
            if not isinstance(strategy, dict):
                continue
            for key in ("latest_trade_date", "trade_date", "source_date"):
                value = extract_yyyymmdd(strategy.get(key))
                if value:
                    dates.append(value)
            for row in (strategy.get("top20") or [])[:20]:
                if isinstance(row, dict):
                    value = extract_yyyymmdd(row.get("recommend_date") or row.get("trade_date") or row.get("ai_analysis_date") or row.get("ai_source_date"))
                    if value:
                        dates.append(value)
    return max(dates) if dates else ""


def canonical_top20(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows[:20], start=1):
        code = normalize_code(row.get("code") or row.get("stock_code") or row.get("ts_code"))
        if not code:
            continue
        enriched = dict(row)
        enriched.setdefault("rank", row.get("rank_no") or idx)
        enriched.setdefault("rank_no", row.get("rank") or idx)
        enriched["code"] = code
        enriched.setdefault("stock_code", code)
        enriched.setdefault("name", row.get("stock_name") or row.get("name") or "")
        enriched.setdefault("stock_name", enriched.get("name"))
        if not enriched.get("ts_code"):
            suffix = "SZ" if code.startswith(("0", "3")) else "SH" if code.startswith("6") else "BJ"
            enriched["ts_code"] = f"{code}.{suffix}"
        out.append(enriched)
    return out


def write_ai_alias_files(trade_date: str, strategy_id: str, rows: list[dict[str, Any]], aliases: list[str]) -> None:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    stocks = canonical_top20(rows)
    payload = {
        "source": strategy_id,
        "trade_date": trade_date,
        "analyzed_at": now_str(),
        "count": len(stocks),
        "stocks": [
            {
                "ts_code": row.get("ts_code"),
                "code": row.get("code"),
                "name": row.get("name") or row.get("stock_name"),
                "score": row.get("score") or row.get("publication_score"),
                "ai_score": row.get("ai_score"),
                "ai_summary": row.get("ai_summary") or row.get("analysis_summary"),
                "ai_decision": row.get("ai_decision") or row.get("decision_type") or row.get("adjusted_action"),
                "ai_advice": row.get("ai_advice") or row.get("operation_advice"),
                "ai_analysis_date": row.get("ai_analysis_date") or row.get("ai_source_date") or trade_date,
                "strategy_id": strategy_id,
            }
            for row in stocks
        ],
    }
    for name in aliases:
        write_json(ANALYTICS_DIR / name, payload)


def write_canonical_strategy_backtests(
    trade_date: str,
    pre_rows: list[dict[str, Any]],
    o2c_rows: list[dict[str, Any]],
    t1_rows: list[dict[str, Any]],
    weights: dict[str, Any],
    source_dates: dict[str, str],
) -> dict[str, Any]:
    def strategy_doc(strategy_id: str, rows: list[dict[str, Any]], tier: str, source_date: str) -> dict[str, Any]:
        meta = STRATEGIES[strategy_id]
        _stale = bool(trade_date and source_date and source_date != trade_date)
        return {
            "artifact_kind": "candidate_snapshot",
            "id": strategy_id,
            "canonical_id": meta.get("canonical_strategy_id") or strategy_id,
            "legacy_source_alias": meta.get("legacy_source_alias"),
            "strategy_version": meta["strategy_version"],
            "name": meta["strategy_name"],
            "tier": tier,
            "strategy_source": meta["strategy_source"],
            "source_date": source_date,
            "source_stale": _stale,
            # 任务D：发布新鲜度硬字段——数据源交易日 != 本次管线目标交易日时必须显式标记，禁止前端按“最新”展示旧快照。
            "data_stale": _stale,
            "stale_source_date": source_date if _stale else None,
            "weights": {"publication_weight": float(weights.get(strategy_id) or 0)},
            "top20": canonical_top20(rows),
            "summary": {
                "top20_count": len(rows[:20]),
                "ai_analyzed_count": sum(1 for row in rows[:20] if row.get("ai_summary") or row.get("ai_score")),
                "source_date": source_date,
            },
        }

    payload = {
        "generated_at": now_str(),
        "window": {"to": trade_date},
        "latest_trade_date": trade_date,
        "strategies": [
            strategy_doc(PREBREAKOUT_ID, pre_rows, "primary", source_dates.get(PREBREAKOUT_ID) or trade_date),
        ],
        "archived_strategy_ids": [O2C_ID, T1_ID],
    }
    for path in [WORKING_REPO / "data" / "strategy_backtests.json", PUBLISHED_REPO / "data" / "strategy_backtests.json"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, payload)
    return payload


def build_publication_layer() -> dict[str, Any]:
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    run_manifest = load_json(LATEST_DIR / "run_manifest.json", {})
    market_state = load_json(LATEST_DIR / "market_state.json", {})
    system_verdict = load_json(LATEST_DIR / "system_verdict.json", {})
    candidate_state = load_json(LATEST_DIR / "candidate_state.json", {})
    working_strategy_doc = load_json(WORKING_REPO / "data" / "strategy_backtests.json", {})
    working_data_json = load_json(WORKING_REPO / "data.json", {})
    published_data_json = load_json(PUBLISHED_REPO / "data.json", {})
    greenfield = load_best_payload(FACTOR_GREENFIELD_PATH, load_json(LATEST_DIR / "greenfield_top20.json", {}))
    pre_review = load_json(LATEST_DIR / "review_state.json", {})
    o2c_review = load_json(LATEST_DIR / "review_state_o2c.json", {})
    # T+1 research state (research_preview mode — may be empty)
    t1_state = load_best_payload(FACTOR_T1_STATE_PATH, load_json(LATEST_DIR / "research_state_t1.json", {}))
    t1_review: dict[str, Any] = {}  # T+1 has no review history yet

    # Prefer the current pipeline target and freshly generated strategy/data dates.
    # The published run_manifest may still be stale when this script is called from
    # stage4 before generate_latest_states has refreshed latest/*.json.
    trade_date = resolve_effective_trade_date(
        os.environ.get("OPENCLAW_TARGET_TRADE_DATE"),
        strategy_doc_trade_date(working_strategy_doc),
        working_data_json.get("latest_trade_date") or working_data_json.get("trade_date"),
        published_data_json.get("latest_trade_date") or published_data_json.get("trade_date"),
        candidate_state.get("latest_trade_date"),
        run_manifest.get("trade_date"),
    )
    market_context = build_market_context({**run_manifest, "trade_date": trade_date}, market_state, system_verdict)
    o2c_source_date = str(greenfield.get("latest_trade_date") or greenfield.get("trade_date") or "")
    t1_source_date = str(t1_state.get("latest_trade_date") or "")
    working_pre_strategy = next(
        (strategy for strategy in (working_strategy_doc.get("strategies") or []) if strategy.get("id") == PREBREAKOUT_ID),
        {},
    )
    pre_source_items = working_pre_strategy.get("top20") or detail_rows_by_latest(
        LOCAL_WAREHOUSE_EXPORT_DIR / "prebreakout_recommendations.json", trade_date
    )
    pre_items = merge_strategy_ai(
        pre_source_items,
        trade_date,
        "traditional",
    )
    archived_o2c_count = len(greenfield.get("top20") or [])
    o2c_items: list[dict[str, Any]] = []
    # T+1 items — load from t1_factor research state, fallback to research_state.json
    t1_research = load_json(STRATEGY_STORE_DIR / T1_ID / "research_state.json", {})
    t1_items_raw = t1_state.get("top20") or t1_research.get("top20") or []
    # fail-closed（诚实化整改 2026-06-12）：全零分名单是无效研究输出，禁止发布
    if rows_all_zero_scores(t1_items_raw):
        print("⚠️ T1 输出 top20 全为 0 分（无有效因子），fail-closed 拦截，不进入发布")
        t1_items_raw = []
    archived_t1_count = len(t1_items_raw)
    t1_items: list[dict[str, Any]] = []

    pre_codes = {normalize_code(item.get("code") or item.get("stock_code") or item.get("ts_code")) for item in pre_items}
    o2c_codes = {normalize_code(item.get("code") or item.get("stock_code") or item.get("ts_code")) for item in o2c_items}
    t1_codes = {normalize_code(item.get("code") or item.get("stock_code") or item.get("ts_code")) for item in t1_items}

    # 2-way and 3-way overlap
    pairwise_overlap = {code for code in pre_codes & o2c_codes if code}
    all_three = pre_codes & o2c_codes & t1_codes
    any_overlap = {code for code in (pre_codes | o2c_codes | t1_codes) if sum([code in pre_codes, code in o2c_codes, code in t1_codes]) >= 2}

    weights = strategy_weights(trade_date, pre_review, o2c_review, t1_review, greenfield, t1_state, market_context)
    pre_weight = float(weights.get(PREBREAKOUT_ID) or 0.4)
    o2c_weight = float(weights.get(O2C_ID) or 0.4)
    t1_weight = float(weights.get(T1_ID) or 0.2)

    pre_adjusted, pre_audit = apply_adjustments(
        pre_items,
        strategy_id=PREBREAKOUT_ID,
        trade_date=trade_date,
        source_date=trade_date,
        market_context=market_context,
        weight=pre_weight,
        overlap=pairwise_overlap,
        multi_overlap=all_three,
    )
    o2c_adjusted, o2c_audit = apply_adjustments(
        o2c_items,
        strategy_id=O2C_ID,
        trade_date=trade_date,
        source_date=o2c_source_date,
        market_context=market_context,
        weight=o2c_weight,
        overlap=pairwise_overlap,
        multi_overlap=all_three,
    )
    t1_adjusted, t1_audit = apply_adjustments(
        t1_items,
        strategy_id=T1_ID,
        trade_date=trade_date,
        source_date=t1_source_date,
        market_context=market_context,
        weight=t1_weight,
        overlap=pairwise_overlap,
        multi_overlap=all_three,
    )

    # 统一硬门槛（#7 诚实化）：逐策略评估 AI覆盖率/数据新鲜度/复盘样本/样本外ICIR/净超额，
    # 任一【可评估且不达标】→ 该策略压成「研究观察」、其 main/买入 一律降级为 watch，不进可交易区。
    strategy_gates = {
        PREBREAKOUT_ID: compute_strategy_gate(PREBREAKOUT_ID, pre_adjusted, pre_review, trade_date, trade_date),
        O2C_ID: compute_strategy_gate(O2C_ID, o2c_adjusted, o2c_review, o2c_source_date, trade_date),
        T1_ID: compute_strategy_gate(
            T1_ID, t1_adjusted, t1_review, t1_source_date, trade_date,
            signal_gate=(t1_state.get("signal_gate") or {}),
        ),
    }
    active_strategy_gates = {
        strategy_id: gate
        for strategy_id, gate in strategy_gates.items()
        if strategy_id in ACTIVE_STRATEGY_IDS
    }
    _rows_by_sid = {PREBREAKOUT_ID: pre_adjusted, O2C_ID: o2c_adjusted, T1_ID: t1_adjusted}
    for _sid, _gate in strategy_gates.items():
        _research = _gate["research_only"]
        for _r in _rows_by_sid[_sid]:
            # 每行都打标（含 False）；ai_coverage_counted 与 compute_strategy_gate 的 have_ai 同口径
            # （ai_score 非空才计入真实 AI 覆盖；T1 模板 ai_score=None → 不计）。
            _r["strategy_research_only"] = _research
            _r["ai_coverage_counted"] = _row_ai_counted(_r)
            # 合同 v2.1：每股链接/锚点/面板 + AI 分析类型，三类策略统一可展开结构。
            _code = normalize_code(_r.get("code") or _r.get("stock_code") or _r.get("ts_code"))
            _ts = _r.get("ts_code") or (str(_r.get("code")) if "." in str(_r.get("code") or "") else "") or _code
            _r["code"] = _r.get("code") or _code
            _r["display_code"] = str(_ts) if "." in str(_ts) else _code
            # 名称兜底（诚实化）：上游行情对个别停牌/特殊状态代码返回空名时，
            # 用代码本身占位（绝不编造中文名），并打 name_resolved=false 标记保持透明。
            # 伪名护栏：name 被塞成代码串（display_code/ts_code/6位码，如次新股字典缺失时的
            #   001312.SZ）也视为未解析，否则 name_resolved=true 与「名==代码」自相矛盾。
            _name = str(_r.get("name") or _r.get("stock_name") or "").strip()
            _code_forms = {
                str(_r.get("display_code") or ""),
                str(_ts or ""),
                str(_r.get("ts_code") or ""),
                str(_code or ""),
            }
            _code_forms.discard("")
            if not _name or _name.lower() == "nan" or _name in _code_forms:
                _r["name"] = _r["display_code"]
                _r["name_resolved"] = False
            else:
                _r["name"] = _name
                _r.setdefault("name_resolved", True)
            _anchor = f"stock-{_code}-{_sid}"
            _r["analysis_anchor_id"] = _anchor
            _r["analysis_panel_id"] = f"{_anchor}-analysis"
            _r["analysis_link_href"] = f"#{_anchor}"
            _has_summary = bool(_gate_value(_r.get("ai_summary")))
            if _sid == T1_ID:
                # T1 研究观察：模板研究解读可展示但不计真实 AI 覆盖（ai_score 仍 None）；真 LLM 解读则 t1_research_ai。
                _kind = str(_r.get("ai_source_kind") or "")
                _atype = ("t1_research_ai" if "research_ai" in _kind else "t1_template_note") if _has_summary else "none"
            elif _sid == O2C_ID:
                _atype = "o2c_ai" if (_has_summary and _r["ai_coverage_counted"]) else "none"
            else:
                _atype = "trading_ai" if (_has_summary and _r["ai_coverage_counted"]) else "none"
            _r["ai_analysis_type"] = _atype
            _r["has_ai_analysis"] = _atype != "none"
            # prebreakout 无条件关闭 main 通道（不依赖 research_only 标志）：
            # 冻结证据=去幸存者偏差复盘 IR-7.71，无任何已证实边际。与 generate_latest_states
            # 的 role 通道关闭、validate_publication_contract 第16节合同兜底构成三层一致防线。
            _force_no_main = _sid == PREBREAKOUT_ID
            if (_research or _force_no_main) and _r.get("market_adjusted_action") == "main":
                _r["gate_adjusted_action"] = "watch"
                _r["final_action"] = "watch"
                _r["adjusted_action"] = "watch"  # 保持旧字段兼容（前端/DB 仍读它）
                _reasons = _r.get("adjustment_reasons")
                if isinstance(_reasons, list):
                    if _research:
                        _reasons.append("策略硬门槛未达标(" + "、".join(_gate["failed_gates"]) + ")，降级为研究观察")
                    else:
                        _reasons.append("启动前夕复盘IR-7.71无已证实边际，main通道永久关闭，降级为观察")

    db_paths: dict[str, str] = {}
    for strategy_id, rows, review_doc, source_date, weight in [
        (PREBREAKOUT_ID, pre_adjusted, pre_review, trade_date, pre_weight),
        (O2C_ID, o2c_adjusted, o2c_review, o2c_source_date, o2c_weight),
        (T1_ID, t1_adjusted, t1_review, t1_source_date, t1_weight),
    ]:
        strategy = STRATEGIES[strategy_id]
        db_path = STRATEGY_STORE_DIR / strategy_id / strategy["db_name"]
        db_paths[strategy_id] = str(db_path)
        if strategy_id in ARCHIVED_STRATEGIES:
            continue
        conn = ensure_strategy_db(db_path)
        try:
            upsert_strategy_run(
                conn,
                strategy,
                trade_date,
                source_date,
                market_context,
                {"strategy_weight": weight, "review": latest_review_context(review_doc)},
                {
                    "source_database_policy": "策略仅读取共享源数据；策略产物写入各自策略数据库",
                    "legacy_warehouse_db_used_for_migration": str(LEGACY_WAREHOUSE_DB),
                    "research_mode": strategy_gates[strategy_id]["research_only"],
                    "strategy_gate": strategy_gates[strategy_id],
                },
            )
            upsert_recommendations(conn, strategy_id, trade_date, rows)
            upsert_reviews(conn, strategy_id, review_doc)
            record_adjustments(conn, strategy_id, rows, market_context, latest_review_context(review_doc))
            record_reflection(conn, strategy_id, trade_date, review_doc, market_context)
            conn.commit()
        finally:
            conn.close()

    final_rows = sorted(pre_adjusted, key=lambda row: row.get("publication_score") or 0, reverse=True)
    final_counts = Counter(row.get("adjusted_action") or "watch" for row in final_rows)
    consensus_rows = [row for row in final_rows if row.get("consensus")]
    divergence_rows = [row for row in final_rows if not row.get("consensus")]
    # 合同 v2：每条含动作三段式 + changed + 有信息原因；只收真正变动的行（剔 raw==final 噪声）；
    # 不再把策略级 {strategy_id,reason} 混进逐股数组（旧 schema bug）。
    adjustment_rows = [
        {
            "strategy_id": row.get("strategy_id"),
            "recommend_date": row.get("recommend_date"),
            "stock_code": row.get("stock_code"),
            "stock_name": row.get("name") or row.get("stock_name"),
            "raw_action": row.get("raw_action"),
            "gate_adjusted_action": row.get("gate_adjusted_action"),
            "final_action": row.get("final_action"),
            # 旧字段兼容
            "before_action": row.get("raw_action"),
            "after_action": row.get("final_action"),
            "changed": row.get("raw_action") != row.get("final_action"),
            "reasons": [r for r in (row.get("adjustment_reasons") or []) if r != "保持原策略动作。"],
            "market_context_used": {"risk_score": market_context.get("risk_score"), "regime": market_context.get("regime")},
            "review_context_used": {"latest_evaluable_recommend_date": (
                pre_review.get("latest_evaluable_recommend_date")
                or o2c_review.get("latest_evaluable_recommend_date")
            )},
            "affects_publication": True,
        }
        for row in final_rows
        if row.get("raw_action") != row.get("final_action")
    ]

    # --- Strategy consensus state: which stocks appear in multiple strategies ---
    consensus_detail: list[dict[str, Any]] = []
    for code in sorted(any_overlap):
        in_pre = code in pre_codes
        in_o2c = code in o2c_codes
        in_t1 = code in t1_codes
        count = sum([in_pre, in_o2c, in_t1])
        # Resolve stock name from whichever row we have
        name = ""
        for row in pre_adjusted + o2c_adjusted + t1_adjusted:
            if normalize_code(row.get("stock_code")) == code:
                name = row.get("name") or row.get("stock_name") or ""
                break
        consensus_detail.append({
            "stock_code": code,
            "stock_name": name,
            "strategy_count": count,
            "in_prebreakout": in_pre,
            "in_o2c": in_o2c,
            "in_t1": in_t1,
            "three_way": code in all_three,
        })
    strategy_consensus_state = {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "total_consensus_stocks": len(consensus_detail),
        "three_way_count": len(all_three),
        "stocks": consensus_detail,
    }

    # --- Execution state: today's actionable execution list ---
    execution_rows = []
    for row in final_rows:
        if row.get("adjusted_action") == "avoid":
            continue
        execution_rows.append({
            "stock_code": row.get("stock_code"),
            "stock_name": row.get("name") or row.get("stock_name"),
            "strategy_source": row.get("strategy_id"),
            "raw_action": row.get("raw_action"),
            "adjusted_action": row.get("adjusted_action"),
            "adjustment_reason": "; ".join(row.get("adjustment_reasons") or []),
            "position_tier": row.get("position_tier", 2),
            "buy_zone": row.get("buy_zone", ""),
            "invalidation": row.get("invalidation", ""),
            "next_day_handling": row.get("next_day_handling", ""),
        })
    execution_state = {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "total_execution_count": len(execution_rows),
        "main_count": sum(1 for r in execution_rows if r["adjusted_action"] == "main"),
        "watch_count": sum(1 for r in execution_rows if r["adjusted_action"] == "watch"),
        "avoid_count": sum(1 for r in execution_rows if r["adjusted_action"] == "avoid"),
        "consensus_in_execution": sum(1 for r in execution_rows if r["stock_code"] in any_overlap),
        "three_way_consensus": [code for code in sorted(all_three)],
        "strategy_counts": {
            PREBREAKOUT_ID: sum(1 for r in execution_rows if r["strategy_source"] == PREBREAKOUT_ID),
        },
        "active_strategy_ids": ACTIVE_STRATEGY_IDS,
        "archived_strategies": ARCHIVED_PUBLICATION_STRATEGIES,
        "divergence_stocks": [row.get("stock_code") for row in divergence_rows if row.get("adjusted_action") != "avoid"],
        "executions": execution_rows,
    }

    # --- T+1 research state snapshot ---
    t1_top20 = t1_state.get("top20") or t1_research.get("top20") or []
    if rows_all_zero_scores(t1_top20):
        t1_top20 = []
        t1_state = {**t1_state, "status": "no_valid_output"}
    # 诚实门禁（2026-06）：未经样本外(T+2)验证的回测数字一律不对外当业绩展示
    # （现状多为样本内/负夏普，会误导）。仅在 has_t2_validation 为真时透出真实回测。
    t1_validated = bool(t1_state.get("has_t2_validation"))
    t1_backtest_raw = t1_state.get("backtest_summary") or t1_research.get("backtest_summary") or {}
    t1_backtest = t1_backtest_raw if t1_validated else {
        "validated": False,
        "note": "未经样本外验证，暂不展示业绩数字（避免用样本内/自评结果误导）",
    }
    # 净 IC 闸门（2026-06）：build_t1_portfolio 用真·walk-forward 实测的样本外信号结论。
    # 「可交易」需同时满足 has_t2_validation 且 signal_gate.tradeable；否则一律研究观察、不发买卖建议。
    t1_signal_gate = t1_state.get("signal_gate") or {}
    t1_tradeable = bool(t1_validated and t1_signal_gate.get("tradeable"))
    if t1_tradeable:
        t1_notes = "T+1策略已通过 T+2 样本外验证且净 IC 闸门达标。"
    elif t1_signal_gate.get("signal_status") == "no_credible_signal":
        _icir = t1_signal_gate.get("oos_icir")
        _net = t1_signal_gate.get("top20_net_excess_mean_daily")
        t1_notes = (
            "T+1 多因子策略：真·样本外(walk-forward)检验显示无可信信号"
            f"（OOS ICIR={_icir}，扣费后 top20 净超额={_net}/日，未达可交易门槛），"
            "仅作因子研究观察展示，不构成任何买卖建议、请勿据此交易。"
        )
    else:
        t1_notes = "T+1 多因子策略处于研究阶段，尚未通过样本外验证，仅作研究信号展示，请勿据此交易。"
    t1_factor_research_state = {
        "generated_at": now_str(),
        "trade_date": t1_source_date or trade_date,
        "status": t1_state.get("status", "research_preview"),
        "has_t2_validation": t1_validated,
        "tradeable": t1_tradeable,
        "signal_gate": t1_signal_gate,
        "strategy_id": T1_ID,
        "strategy_name": STRATEGIES[T1_ID]["strategy_name"],
        "weight": t1_weight,
        "source_date": t1_source_date,
        "data_stale": bool(t1_source_date and t1_source_date != trade_date),
        "stale_source_date": t1_source_date if (t1_source_date and t1_source_date != trade_date) else None,
        "top20_count": len(t1_top20),
        "top20": t1_top20,
        "backtest_summary": t1_backtest,
        "db_path": db_paths.get(T1_ID, ""),
        "notes": t1_notes,
        "lifecycle_status": "archived_historical_only",
        "execution_authority": "historical_control_only",
    }

    registry = {
        "generated_at": now_str(),
        "source_database": {
            "kind": "shared_clean_source_store",
            "path": str(SOURCE_STORE_DIR),
            "write_policy": "策略不得写入源数据；策略产物写入各自策略数据库。",
            "legacy_recommendation_warehouse": str(LEGACY_WAREHOUSE_DB),
        },
        "strategies": [STRATEGIES[strategy_id] for strategy_id in ACTIVE_STRATEGY_IDS],
        "active_strategy_ids": ACTIVE_STRATEGY_IDS,
        "archived_strategies": ARCHIVED_PUBLICATION_STRATEGIES,
    }
    strategy_run_state = {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "source_database": registry["source_database"],
        "strategy_databases": db_paths,
        "strategy_weights": {
            PREBREAKOUT_ID: pre_weight,
        },
        "active_strategy_ids": ACTIVE_STRATEGY_IDS,
        "archived_strategies": ARCHIVED_PUBLICATION_STRATEGIES,
        # 合同 v2：系统解码页主读 strategies[]（runs 保留为旧兼容）。
        "strategies": [
            {
                "strategy_id": sid,
                "strategy_name": STRATEGIES[sid]["strategy_name"],
                "run_status": "ok" if rows else "empty",
                "source_date": src,
                "database_path": db_paths[sid],
                "item_count": len(rows),
                "weight": {PREBREAKOUT_ID: pre_weight, O2C_ID: o2c_weight, T1_ID: t1_weight}[sid],
                # 任务D：发布新鲜度硬字段。
                "data_stale": bool(trade_date and src and src != trade_date),
                "stale_source_date": src if (trade_date and src and src != trade_date) else None,
                "ai_coverage": (strategy_gates[sid]["gates"] or {}).get("ai_coverage", {}),
                "gate_status": "pass" if not strategy_gates[sid]["research_only"] else "warn",
                "research_only": strategy_gates[sid]["research_only"],
                "summary": strategy_gates[sid]["note"],
                "review": latest_review_context(rev),
            }
            for sid, rows, src, rev in [
                (PREBREAKOUT_ID, pre_adjusted, trade_date, pre_review),
                (O2C_ID, o2c_adjusted, o2c_source_date, o2c_review),
                (T1_ID, t1_adjusted, t1_source_date, t1_review),
            ]
            if sid in ACTIVE_STRATEGY_IDS
        ],
        "runs": [
            {
                **STRATEGIES[PREBREAKOUT_ID],
                "trade_date": trade_date,
                "source_date": trade_date,
                "database": db_paths[PREBREAKOUT_ID],
                "top20_count": len(pre_adjusted),
                "review": latest_review_context(pre_review),
            },
            {
                **STRATEGIES[O2C_ID],
                "trade_date": trade_date,
                "source_date": o2c_source_date,
                "database": db_paths[O2C_ID],
                "top20_count": len(o2c_adjusted),
                "review": latest_review_context(o2c_review),
                "source_stale": bool(o2c_source_date and o2c_source_date != trade_date),
                "data_stale": bool(o2c_source_date and o2c_source_date != trade_date),
                "stale_source_date": o2c_source_date if (o2c_source_date and o2c_source_date != trade_date) else None,
            },
            {
                **STRATEGIES[T1_ID],
                "trade_date": trade_date,
                "source_date": t1_source_date,
                "database": db_paths[T1_ID],
                "top20_count": len(t1_adjusted),
                "review": latest_review_context(t1_review),
                "source_stale": bool(t1_source_date and t1_source_date != trade_date),
                "data_stale": bool(t1_source_date and t1_source_date != trade_date),
                "stale_source_date": t1_source_date if (t1_source_date and t1_source_date != trade_date) else None,
                "research_mode": True,
            },
        ],
    }
    strategy_run_state["runs"] = [
        run
        for run in strategy_run_state["runs"]
        if run.get("strategy_id") in ACTIVE_STRATEGY_IDS
    ]
    pre_contract = strategy_contract_fields(strategy_gates[PREBREAKOUT_ID], pre_adjusted)
    o2c_contract = strategy_contract_fields(strategy_gates[O2C_ID], o2c_adjusted)
    t1_contract = strategy_contract_fields(strategy_gates[T1_ID], t1_adjusted)
    # O2C AI 未满时必须显式 pending，否则 pre-push 合同会拒绝整仓（含 S3 名单）推送。
    o2c_allow_pending = o2c_allow_ai_pending_flag(o2c_contract)
    recommendation_state = {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "market_context": market_context,
        "strategy_weights": strategy_run_state["strategy_weights"],
        "counts": {
            "main": final_counts.get("main", 0),
            "watch": final_counts.get("watch", 0),
            "avoid": final_counts.get("avoid", 0),
            "total": len(final_rows),
            "consensus": len(consensus_rows),
            "three_way_consensus": len(all_three),
        },
        "consensus_stocks": consensus_rows,
        "divergence_stocks": divergence_rows,
        "strategies": {
            PREBREAKOUT_ID: {
                **STRATEGIES[PREBREAKOUT_ID],
                "source_date": trade_date,
                "weight": pre_weight,
                # 任务D：发布新鲜度硬字段（启动前夕源恒等于决策日，故恒 false）。
                "data_stale": False,
                "stale_source_date": None,
                **pre_contract,
                "items": pre_adjusted,
            },
            O2C_ID: {
                **STRATEGIES[O2C_ID],
                "source_date": o2c_source_date,
                "weight": o2c_weight,
                "source_stale": bool(o2c_source_date and o2c_source_date != trade_date),
                "data_stale": bool(o2c_source_date and o2c_source_date != trade_date),
                "stale_source_date": o2c_source_date if (o2c_source_date and o2c_source_date != trade_date) else None,
                **o2c_contract,
                "allow_o2c_ai_pending": o2c_allow_pending,
                "items": o2c_adjusted,
            },
            T1_ID: {
                **STRATEGIES[T1_ID],
                "source_date": t1_source_date,
                "weight": t1_weight,
                "source_stale": bool(t1_source_date and t1_source_date != trade_date),
                "data_stale": bool(t1_source_date and t1_source_date != trade_date),
                "stale_source_date": t1_source_date if (t1_source_date and t1_source_date != trade_date) else None,
                **t1_contract,
                "items": t1_adjusted,
            },
        },
        "final_recommendations": final_rows[:40],
    }
    recommendation_state["strategies"] = {
        strategy_id: payload
        for strategy_id, payload in recommendation_state["strategies"].items()
        if strategy_id in ACTIVE_STRATEGY_IDS
    }
    recommendation_state["active_strategy_ids"] = ACTIVE_STRATEGY_IDS
    recommendation_state["archived_strategies"] = ARCHIVED_PUBLICATION_STRATEGIES
    canonical_strategy_payload = write_canonical_strategy_backtests(
        trade_date,
        pre_adjusted,
        o2c_adjusted,
        t1_adjusted,
        strategy_run_state["strategy_weights"],
        {
            PREBREAKOUT_ID: trade_date,
            O2C_ID: o2c_source_date or trade_date,
            T1_ID: t1_source_date or trade_date,
        },
    )
    # Archived strategies keep their existing historical files untouched.

    # Keep strategy-specific page/analytics feeds on the same source date as the
    # canonical strategy payload. These files are consumed directly by strategy
    # tabs; leaving them to stale warehouse exports can make O2C/T1 show an old
    # trade date even when strategy_backtests.json is fresh.
    write_active_strategy_json(
        O2C_ID,
        LATEST_DIR / "greenfield_top20.json",
        {
            "generated_at": now_str(),
            "trade_date": o2c_source_date or trade_date,
            "source_date": o2c_source_date or trade_date,
            "strategy_id": O2C_ID,
            "strategy_name": STRATEGIES[O2C_ID]["strategy_name"],
            "count": len(o2c_adjusted[:20]),
            "data_stale": bool(o2c_source_date and o2c_source_date != trade_date),
            "stale_source_date": o2c_source_date if (o2c_source_date and o2c_source_date != trade_date) else None,
            "top20": o2c_adjusted[:20],
        },
    )
    if T1_ID not in ARCHIVED_STRATEGIES and isinstance(t1_state, dict) and t1_state:
        fresh_t1_state = dict(t1_state)
        fresh_t1_state["latest_trade_date"] = t1_source_date or trade_date
        fresh_t1_state.setdefault("generated_at", now_str())
        write_json(LATEST_DIR / "research_state_t1.json", fresh_t1_state)
    write_active_strategy_json(
        O2C_ID,
        ANALYTICS_DIR / "o2c_factor_recommendations.json",
        {
            "generated_at": now_str(),
            "trade_date": o2c_source_date or trade_date,
            "source_date": o2c_source_date or trade_date,
            "strategy_id": O2C_ID,
            "strategy_name": STRATEGIES[O2C_ID]["strategy_name"],
            "row_count": len(o2c_adjusted[:20]),
            "data_stale": bool(o2c_source_date and o2c_source_date != trade_date),
            "stale_source_date": o2c_source_date if (o2c_source_date and o2c_source_date != trade_date) else None,
            "rows": o2c_adjusted[:20],
        },
    )
    write_active_strategy_json(
        O2C_ID,
        ANALYTICS_DIR / "o2c_factor_summary.json",
        {
            "generated_at": now_str(),
            "trade_date": o2c_source_date or trade_date,
            "source_date": o2c_source_date or trade_date,
            "strategy_id": O2C_ID,
            "strategy_name": STRATEGIES[O2C_ID]["strategy_name"],
            "row_count": len(o2c_adjusted[:20]),
            "main_count": sum(1 for row in o2c_adjusted[:20] if row.get("adjusted_action") == "main"),
            "watch_count": sum(1 for row in o2c_adjusted[:20] if row.get("adjusted_action") == "watch"),
            "avoid_count": sum(1 for row in o2c_adjusted[:20] if row.get("adjusted_action") == "avoid"),
            "data_stale": bool(o2c_source_date and o2c_source_date != trade_date),
            "stale_source_date": o2c_source_date if (o2c_source_date and o2c_source_date != trade_date) else None,
        },
    )
    write_active_strategy_json(
        T1_ID,
        ANALYTICS_DIR / "t1_factor_recommendations.json",
        {
            "generated_at": now_str(),
            "trade_date": t1_source_date or trade_date,
            "source_date": t1_source_date or trade_date,
            "strategy_id": T1_ID,
            "strategy_name": STRATEGIES[T1_ID]["strategy_name"],
            "row_count": len(t1_adjusted[:20]),
            "data_stale": bool(t1_source_date and t1_source_date != trade_date),
            "stale_source_date": t1_source_date if (t1_source_date and t1_source_date != trade_date) else None,
            "rows": t1_adjusted[:20],
        },
    )

    o2c_source_stale = bool(o2c_source_date and o2c_source_date != trade_date)
    t1_source_stale = bool(t1_source_date and t1_source_date != trade_date)
    decision_state = {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "final_verdict": "只观察" if final_counts.get("main", 0) == 0 else ("谨慎执行" if effective_risk_score(market_context) >= 50 else "可执行"),
        "market_regime": market_context["regime"],
        "market_cycle": market_context["market_cycle"],
        "risk_score": market_context["risk_score"],
        "strategy_weights": strategy_run_state["strategy_weights"],
        "active_strategy_ids": ACTIVE_STRATEGY_IDS,
        "archived_strategies": ARCHIVED_PUBLICATION_STRATEGIES,
        "counts": recommendation_state["counts"],
        # 合同 v2 统一四闸（freshness/market/strategy/candidate_gate）：前端首页/工作流只读这里的标准字段判状态。
        "gates": {
            "freshness_gate": {
                "status": "pass",
                "summary": "数据新鲜度",
                "reasons": [],
            },
            "market_gate": {
                "status": _market_gate_status(market_context),
                "summary": str(market_context.get("gate_signal") or "市场闸门"),
                "reasons": (
                    ["市场风险数据缺失，按保守闸门处理"] if not market_context.get("risk_data_available")
                    else [f"风险分 {market_context.get('risk_score')}（{market_context.get('regime')}）"]
                ),
            },
            "strategy_gate": {
                "status": "pass" if all(not g["research_only"] for g in active_strategy_gates.values()) else "warn",
                "summary": "策略硬门槛",
                "reasons": [
                    f"{sid}: {'、'.join(g['failed_gates'])}"
                    for sid, g in active_strategy_gates.items() if g["research_only"]
                ],
                "per_strategy": {sid: g["verdict"] for sid, g in active_strategy_gates.items()},
            },
            "candidate_gate": {
                "status": "pass" if final_counts.get("main", 0) > 0 else "warn",
                "summary": "可交易候选",
                "reasons": ([] if final_counts.get("main", 0) > 0 else ["无策略达可交易门槛，全部候选仅研究观察"]),
            },
        },
        "data_status": {
            "source_database_clean": True,
            "prebreakout_database": db_paths[PREBREAKOUT_ID],
            "historical_control_databases": {
                O2C_ID: db_paths[O2C_ID],
                T1_ID: db_paths[T1_ID],
            },
        },
        "primary_links": {
            "recommendations": "decision-candidates.html",
            "review": "recommendation-review.html",
            "market": "market-overview.html",
            "system": "research-lab.html",
        },
    }
    # 合同 v2：默认复盘日选「最近一个有次日后验数据的交易日」，不选还没有后验数据的当天。
    _all_review_dates = sorted(
        {
            str(r.get("recommend_date"))
            for r in (pre_review.get("date_stats") or [])
            if r.get("recommend_date")
        },
        reverse=True,
    )
    _default_review_date = (
        pre_review.get("latest_evaluable_recommend_date")
        or (_all_review_dates[0] if _all_review_dates else None)
    )
    review_unified = {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "latest_date": _all_review_dates[0] if _all_review_dates else trade_date,
        "default_review_date": _default_review_date,
        "available_dates": _all_review_dates,
        "available_strategies": ACTIVE_STRATEGY_IDS,
        "archived_strategies": ARCHIVED_PUBLICATION_STRATEGIES,
        # 禁止 null 策略：每套始终带 strategy_id/strategy_name 骨架 + has_review_data 标记（无数据如实显示，不造假对比）。
        "strategies": {
            PREBREAKOUT_ID: {"strategy_id": PREBREAKOUT_ID, "strategy_name": "启动前夕",
                             "has_review_data": bool(pre_review.get("date_stats")), **pre_review},
        },
        "historical_controls": {
            O2C_ID: {
                **ARCHIVED_PUBLICATION_STRATEGIES[O2C_ID],
                "has_review_data": bool(o2c_review.get("date_stats")),
                "review": o2c_review,
            },
            T1_ID: {
                **ARCHIVED_PUBLICATION_STRATEGIES[T1_ID],
                "has_review_data": bool(t1_review.get("date_stats")),
                "review": t1_review,
            },
        },
        "daily_comparison": [
            *[{**row, "strategy_id": PREBREAKOUT_ID, "strategy_name": "启动前夕"} for row in pre_review.get("date_stats") or []],
        ],
        "stock_rows": [
            *[
                {**row, "strategy_id": PREBREAKOUT_ID, "strategy_name": "启动前夕"}
                for row in load_json(
                    LOCAL_WAREHOUSE_EXPORT_DIR / "prebreakout_recommendations.json", {}
                ).get("rows") or []
            ],
        ],
    }
    system_health = {
        "generated_at": now_str(),
        "ok": True,
        "source_database": registry["source_database"],
        "strategy_databases": db_paths,
        "publication_files": [
            "decision_state.json",
            "market_context.json",
            "strategy_registry.json",
            "strategy_run_state.json",
            "recommendation_state.json",
            "review_state_unified.json",
            "adjustment_log.json",
            "system_health.json",
            "execution_state.json",
            "strategy_consensus_state.json",
            "t1_factor_research_state.json",
        ],
        "checks": {
            "shared_source_database_exists": SOURCE_STORE_DIR.exists(),
            "prebreakout_strategy_db_exists": Path(db_paths[PREBREAKOUT_ID]).exists(),
            "active_strategy_count": len(ACTIVE_STRATEGY_IDS),
            "active_strategy_has_candidates": bool(pre_adjusted),
            "archived_strategy_count": len(ARCHIVED_PUBLICATION_STRATEGIES),
            "canonical_strategy_count": len(canonical_strategy_payload.get("strategies") or []),
        },
    }

    outputs = {
        "decision_state.json": decision_state,
        "market_context.json": market_context,
        "strategy_registry.json": registry,
        "strategy_run_state.json": strategy_run_state,
        "recommendation_state.json": recommendation_state,
        "review_state_unified.json": review_unified,
        "adjustment_log.json": {"generated_at": now_str(), "rows": adjustment_rows},
        "system_health.json": system_health,
        "execution_state.json": execution_state,
        "strategy_consensus_state.json": strategy_consensus_state,
        "t1_factor_research_state.json": t1_factor_research_state,
    }
    for name, payload in outputs.items():
        write_json(LATEST_DIR / name, payload)
    return {
        "generated_at": now_str(),
        "ok": True,
        "trade_date": trade_date,
        "strategy_databases": db_paths,
        "published_files": sorted(outputs),
        "counts": recommendation_state["counts"],
    }


def main() -> int:
    result = build_publication_layer()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
