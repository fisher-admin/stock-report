#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为当前最新 support_pool + fallback 组合生成最小可信 replay 报告。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROUND_TRIP_COST = 0.002
TOP_N = 20
BACKFILL_DATES = ["20260224", "20260225"]
EPS = 1e-12
DEFAULT_HOLDOUT_RESERVE_FILE = "current_combo_holdout_reserve.json"
LABEL_MODE_TO_COLUMN = {
    "close_to_close": "next_return_close_to_close_1d",
    "open_to_close": "next_return_open_to_close_1d",
    "close_to_open": "next_return_close_to_open_1d",
}
SERIES_ORDER = [
    "close_to_close_combo",
    "close_to_close_base",
    "open_to_close_combo",
    "open_to_close_base",
    "close_to_open_combo",
    "close_to_open_base",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run current combo minimal replay")
    parser.add_argument("--root", default="/Users/fisher/.openclaw/workspace/factor_factory")
    parser.add_argument("--weight-path", default=None)
    parser.add_argument("--audit-path", default=None)
    parser.add_argument("--label-mode", choices=sorted(LABEL_MODE_TO_COLUMN), default=None)
    parser.add_argument("--output-stem", default="current_combo_replay_report")
    parser.add_argument("--reserved-holdout-dates", default=None)
    parser.add_argument("--reserved-holdout-path", default=None)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_trade_dates(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [item.strip() for item in values.split(",") if item.strip()]
    elif isinstance(values, (list, tuple, set)):
        raw_values = [str(item).strip() for item in values if str(item).strip()]
    else:
        raw_values = [str(values).strip()] if str(values).strip() else []
    return sorted(dict.fromkeys(raw_values))


def load_holdout_reserve(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = read_json(path)
    if isinstance(payload, list):
        return {"reserved_holdout_dates": normalize_trade_dates(payload)}
    if not isinstance(payload, dict):
        return {}
    payload = dict(payload)
    payload["reserved_holdout_dates"] = normalize_trade_dates(payload.get("reserved_holdout_dates"))
    return payload


def iso_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return pd.Timestamp.fromtimestamp(path.stat().st_mtime, tz="Asia/Shanghai").isoformat()


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    def _zscore(s: pd.Series) -> pd.Series:
        std = s.std(ddof=0)
        if pd.isna(std) or std == 0:
            return pd.Series(0.0, index=s.index)
        return (s - s.mean()) / std

    return df.groupby("trade_date")[col].transform(_zscore)


def build_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "days": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "turnover": 0.0,
            "start": None,
            "end": None,
        }
    rets = pd.Series({row["date"]: row["net_return"] for row in rows}).sort_index()
    turns = pd.Series({row["date"]: row["turnover"] for row in rows}).sort_index()
    total = float((1.0 + rets).prod() - 1.0)
    annual = float((1.0 + total) ** (252 / max(1, len(rets))) - 1.0)
    nav = (1.0 + rets).cumprod()
    drawdown = nav / nav.cummax() - 1.0
    std = float(rets.std(ddof=0))
    sharpe = float(rets.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    mdd = float(drawdown.min()) if not drawdown.empty else 0.0
    return {
        "days": int(len(rets)),
        "total_return": total,
        "annualized_return": annual,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": float(annual / abs(mdd)) if mdd < 0 else 0.0,
        "win_rate": float((rets > 0).mean()),
        "turnover": float(turns.mean() if not turns.empty else 0.0),
        "start": str(rets.index.min()) if not rets.empty else None,
        "end": str(rets.index.max()) if not rets.empty else None,
    }


def direction_of(value: float) -> str:
    if value > EPS:
        return "positive"
    if value < -EPS:
        return "negative"
    return "flat"


def same_nonflat_direction(lhs: float, rhs: float) -> bool:
    left = direction_of(lhs)
    right = direction_of(rhs)
    return left == right and left != "flat"


def infer_label_mode(weight_report: dict[str, Any], audit_report: dict[str, Any], args_label_mode: str | None) -> str:
    if args_label_mode in LABEL_MODE_TO_COLUMN:
        return str(args_label_mode)
    candidates = [
        weight_report.get("audit_context", {}).get("label_mode"),
        audit_report.get("label_mode"),
    ]
    for candidate in candidates:
        if candidate in LABEL_MODE_TO_COLUMN:
            return str(candidate)
    return "close_to_close"


def collect_weight_map_factor_union(weight_report: dict[str, Any]) -> list[str]:
    factor_ids: list[str] = []
    for plan in weight_report.get("regime_weight_map", []):
        for item in plan.get("selected_factors", []):
            factor_id = str(item["factor_id"])
            if factor_id not in factor_ids:
                factor_ids.append(factor_id)
    return factor_ids


def build_regime_factor_coverage(weight_report: dict[str, Any]) -> dict[str, Any]:
    coverage: dict[str, dict[str, Any]] = {}
    for plan in weight_report.get("regime_weight_map", []):
        regime_id = str(plan["regime_id"])
        for item in plan.get("selected_factors", []):
            factor_id = str(item["factor_id"])
            slot = coverage.setdefault(
                factor_id,
                {
                    "regime_count": 0,
                    "nominal_weight_sum": 0.0,
                    "selection_sources": set(),
                    "regime_ids": [],
                },
            )
            slot["regime_count"] += 1
            slot["nominal_weight_sum"] += float(item.get("weight", 0.0))
            slot["selection_sources"].add(str(item.get("selection_source", "unknown")))
            slot["regime_ids"].append(regime_id)
    return {
        factor_id: {
            "regime_count": item["regime_count"],
            "nominal_weight_sum": round(float(item["nominal_weight_sum"]), 6),
            "selection_sources": sorted(item["selection_sources"]),
            "regime_ids": item["regime_ids"],
        }
        for factor_id, item in coverage.items()
    }


def build_audit_status_lookup(audit_report: dict[str, Any]) -> dict[str, str]:
    return {
        str(row.get("factor_id")): str(row.get("status"))
        for row in audit_report.get("reports", [])
        if row.get("factor_id")
    }


def build_selected_factor_usage_summary(weight_report: dict[str, Any], audit_report: dict[str, Any]) -> dict[str, Any]:
    audit_status = build_audit_status_lookup(audit_report)
    usage: dict[str, dict[str, Any]] = {}
    for plan in weight_report.get("regime_weight_map", []):
        invested_capital = 1.0 - float(plan.get("cash_buffer", 0.0) or 0.0)
        if invested_capital <= 0:
            invested_capital = 1.0
        for item in plan.get("selected_factors", []):
            factor_id = str(item["factor_id"])
            weight = float(item.get("weight", 0.0) or 0.0)
            slot = usage.setdefault(
                factor_id,
                {
                    "regime_count": 0,
                    "nominal_weight_sum": 0.0,
                    "selection_sources": set(),
                    "share_of_invested_capital": [],
                },
            )
            slot["regime_count"] += 1
            slot["nominal_weight_sum"] += weight
            slot["selection_sources"].add(str(item.get("selection_source", "unknown")))
            slot["share_of_invested_capital"].append(weight / invested_capital)
    return {
        factor_id: {
            "regime_count": item["regime_count"],
            "nominal_weight_sum": round(float(item["nominal_weight_sum"]), 6),
            "selection_sources": sorted(item["selection_sources"]),
            "avg_share_of_invested_capital_across_regimes": round(float(np.mean(item["share_of_invested_capital"])), 6),
            "max_share_of_invested_capital": round(float(np.max(item["share_of_invested_capital"])), 6),
            "audit_status": audit_status.get(factor_id),
        }
        for factor_id, item in usage.items()
    }


def build_selection_layers_note(
    label_mode: str,
    audit_path: Path,
    weight_path: Path,
    weight_report: dict[str, Any],
    audit_report: dict[str, Any],
) -> str:
    latest_regime = weight_report.get("latest_weight_plan", {}).get("regime_id") or "unknown_regime"
    latest_plan_ids = [
        str(item.get("factor_id"))
        for item in weight_report.get("latest_weight_plan", {}).get("selected_factors", [])
        if item.get("factor_id")
    ]
    benchmark_days = audit_report.get("benchmark_eligible_trade_days", "NA")
    benchmark_rows = audit_report.get("benchmark_eligible_row_count", "NA")
    factor_text = " / ".join(latest_plan_ids) if latest_plan_ids else "NA"
    return (
        f"{label_mode} canonical catch-up confirmed：{audit_path.name} 已追平到 "
        f"{benchmark_days} 个 benchmark_eligible trade days / {benchmark_rows} 行，"
        f"{weight_path.name} 也已切到同一口径；latest {latest_regime} 正式计划仍为 {factor_text}。"
    )


def run_replay(
    panel: pd.DataFrame,
    regime_map: dict[str, Any],
    factor_signs: dict[str, float],
    label_specs: list[tuple[str, str, str]],
) -> dict[str, Any]:
    records: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in label_specs}
    prev_holdings: dict[str, set[str]] = {}

    for trade_date, day in panel.groupby("trade_date", sort=True):
        regime_id = str(day["regime_id"].iloc[0])
        plan = regime_map.get(regime_id)
        if not plan:
            continue

        day = day.copy()
        day["base_score_only"] = float(plan.get("base_weight", 0.4)) * day["base_factor_score_norm_z"]
        combo_score = day["base_score_only"].copy()
        for item in plan.get("selected_factors", []):
            factor_id = str(item["factor_id"])
            combo_score = combo_score + float(item.get("weight", 0.0)) * factor_signs.get(factor_id, 1.0) * day[f"{factor_id}_z"]
        day["combo_score"] = combo_score

        for label, ret_col, score_col in label_specs:
            subset = day[["ts_code", score_col, ret_col]].dropna()
            if len(subset) < TOP_N:
                continue
            chosen = subset.nlargest(TOP_N, score_col)
            holdings = set(chosen["ts_code"].astype(str))
            turnover = 1.0 if label not in prev_holdings else 1.0 - len(holdings & prev_holdings[label]) / max(1, len(holdings | prev_holdings[label]))
            prev_holdings[label] = holdings
            gross = float(chosen[ret_col].mean())
            net = gross - ROUND_TRIP_COST * turnover
            records[label].append(
                {
                    "date": str(trade_date),
                    "regime_id": regime_id,
                    "gross_return": gross,
                    "turnover": float(turnover),
                    "net_return": float(net),
                    "top_codes": list(chosen["ts_code"].astype(str).head(5)),
                }
            )

    return {
        label: {
            "metrics": build_metrics(rows),
            "effective_trade_dates": [row["date"] for row in rows],
            "daily_records": rows,
        }
        for label, rows in records.items()
    }


def build_comparisons(replay_results: dict[str, Any]) -> dict[str, float]:
    comparisons = {
        "close_to_close_excess_total_return": replay_results["close_to_close_combo"]["metrics"]["total_return"] - replay_results["close_to_close_base"]["metrics"]["total_return"],
        "open_to_close_excess_total_return": replay_results["open_to_close_combo"]["metrics"]["total_return"] - replay_results["open_to_close_base"]["metrics"]["total_return"],
        "close_to_open_excess_total_return": replay_results["close_to_open_combo"]["metrics"]["total_return"] - replay_results["close_to_open_base"]["metrics"]["total_return"],
        "combo_c2c_minus_o2c_total_return": replay_results["close_to_close_combo"]["metrics"]["total_return"] - replay_results["open_to_close_combo"]["metrics"]["total_return"],
        "combo_c2c_minus_c2o_total_return": replay_results["close_to_close_combo"]["metrics"]["total_return"] - replay_results["close_to_open_combo"]["metrics"]["total_return"],
        "combo_o2c_minus_c2o_total_return": replay_results["open_to_close_combo"]["metrics"]["total_return"] - replay_results["close_to_open_combo"]["metrics"]["total_return"],
        "base_c2c_minus_o2c_total_return": replay_results["close_to_close_base"]["metrics"]["total_return"] - replay_results["open_to_close_base"]["metrics"]["total_return"],
        "base_c2c_minus_c2o_total_return": replay_results["close_to_close_base"]["metrics"]["total_return"] - replay_results["close_to_open_base"]["metrics"]["total_return"],
        "base_o2c_minus_c2o_total_return": replay_results["open_to_close_base"]["metrics"]["total_return"] - replay_results["close_to_open_base"]["metrics"]["total_return"],
    }
    return comparisons


def build_direction_consistency(
    comparisons: dict[str, float],
    replay_results: dict[str, Any],
    primary_label_mode: str,
) -> dict[str, Any]:
    combo_returns = {
        mode: replay_results[f"{mode}_combo"]["metrics"]["total_return"]
        for mode in LABEL_MODE_TO_COLUMN
    }
    excess_returns = {
        mode: comparisons[f"{mode}_excess_total_return"]
        for mode in LABEL_MODE_TO_COLUMN
    }
    c2c_combo = combo_returns["close_to_close"]
    o2c_combo = combo_returns["open_to_close"]
    c2o_combo = combo_returns["close_to_open"]
    c2c_excess = excess_returns["close_to_close"]
    o2c_excess = excess_returns["open_to_close"]
    c2o_excess = excess_returns["close_to_open"]
    combo_consistent = same_nonflat_direction(c2c_combo, o2c_combo)
    excess_consistent = same_nonflat_direction(c2c_excess, o2c_excess)
    primary_combo = combo_returns[primary_label_mode]
    primary_excess = excess_returns[primary_label_mode]
    return {
        "primary_label_mode": primary_label_mode,
        "primary_combo_direction": direction_of(primary_combo),
        "primary_excess_direction": direction_of(primary_excess),
        "primary_vs_close_to_close_combo_direction_consistent": same_nonflat_direction(primary_combo, c2c_combo),
        "primary_vs_close_to_close_excess_direction_consistent": same_nonflat_direction(primary_excess, c2c_excess),
        "close_to_close_combo_direction": direction_of(c2c_combo),
        "open_to_close_combo_direction": direction_of(o2c_combo),
        "close_to_open_combo_direction": direction_of(c2o_combo),
        "combo_direction_consistent": combo_consistent,
        "close_to_close_excess_direction": direction_of(c2c_excess),
        "open_to_close_excess_direction": direction_of(o2c_excess),
        "close_to_open_excess_direction": direction_of(c2o_excess),
        "excess_direction_consistent": excess_consistent,
        "summary": "consistent" if combo_consistent and excess_consistent else "inconsistent",
    }


def build_markdown(report: dict[str, Any]) -> str:
    replay = report["replay_results"]
    comparisons = report["comparisons"]
    sensitivity = report["sensitivity"]
    direction = report["direction_consistency"]
    verdict = report["verdict"]
    combo = report["current_combo_definition"]
    lines = [
        "# Current Combo Replay Report",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 组合范围：{combo['selection_mode']} | base={combo['base_factor_id']}({combo['base_weight']:.0%})",
        f"- latest shrink_uptrend 因子：{', '.join(combo['latest_weight_plan_factor_ids'])}",
        f"- regime union 因子：{', '.join(combo['regime_union_factor_ids'])}",
        f"- 当前代理 regime：{combo['current_proxy_regime']} @ {combo['current_proxy_trade_date']}",
        f"- canonical label_mode：{combo['canonical_label_mode']} ({combo['canonical_label_column']})",
        f"- benchmark_eligible 天数：{report['benchmark_eligible_summary']['eligible_trade_days']}",
        f"- 预留 holdout 日期：{', '.join(report['benchmark_eligible_summary'].get('reserved_holdout_dates', [])) or '无'}",
        (
            "- 实际可回放天数（c2c/o2c/c2o）："
            f"{replay['close_to_close_combo']['metrics']['days']} / "
            f"{replay['open_to_close_combo']['metrics']['days']} / "
            f"{replay['close_to_open_combo']['metrics']['days']}"
        ),
        f"- 未能进入有效回放的 eligible 日期：{', '.join(report['benchmark_eligible_summary']['excluded_from_effective_replay']['close_to_close_combo']) or '无'}",
        "",
        "## 组合级 replay（净收益，20bps round-trip cost）",
    ]
    for key in SERIES_ORDER:
        metrics = replay[key]["metrics"]
        lines.append(
            f"- {key}: days={metrics['days']}, total_return={metrics['total_return']:.4%}, sharpe={metrics['sharpe']:.3f}, max_drawdown={metrics['max_drawdown']:.4%}, win_rate={metrics['win_rate']:.2%}, turnover={metrics['turnover']:.3f}"
        )
    lines.extend(
        [
            "",
            "## 方向一致性",
            (
                "- combo 绝对收益方向："
                f"c2c={direction['close_to_close_combo_direction']}, "
                f"o2c={direction['open_to_close_combo_direction']}, "
                f"c2o={direction['close_to_open_combo_direction']}, "
                f"c2c_vs_o2c一致={direction['combo_direction_consistent']}"
            ),
            (
                "- combo 相对 base 超额方向："
                f"c2c={direction['close_to_close_excess_direction']}, "
                f"o2c={direction['open_to_close_excess_direction']}, "
                f"c2o={direction['close_to_open_excess_direction']}, "
                f"c2c_vs_o2c一致={direction['excess_direction_consistent']}"
            ),
            (
                f"- primary({direction['primary_label_mode']}) 相对 c2c 方向一致："
                f"combo={direction['primary_vs_close_to_close_combo_direction_consistent']} / "
                f"excess={direction['primary_vs_close_to_close_excess_direction_consistent']}"
            ),
            f"- close-to-close 超额总收益：{comparisons['close_to_close_excess_total_return']:.4%}",
            f"- open-to-close 超额总收益：{comparisons['open_to_close_excess_total_return']:.4%}",
            f"- close-to-open 超额总收益：{comparisons['close_to_open_excess_total_return']:.4%}",
            f"- combo 口径切换差值（c2c - o2c）：{comparisons['combo_c2c_minus_o2c_total_return']:.4%}",
            f"- combo 口径切换差值（c2c - c2o）：{comparisons['combo_c2c_minus_c2o_total_return']:.4%}",
            "",
            "## 回填日敏感性",
            f"- 剔除 {', '.join(sensitivity['backfill_dates'])} 后，c2c combo 总收益：{sensitivity['without_backfill_dates']['close_to_close_combo']['metrics']['total_return']:.4%}",
            f"- 剔除 {', '.join(sensitivity['backfill_dates'])} 后，o2c combo 总收益：{sensitivity['without_backfill_dates']['open_to_close_combo']['metrics']['total_return']:.4%}",
            f"- 剔除 {', '.join(sensitivity['backfill_dates'])} 后，c2o combo 总收益：{sensitivity['without_backfill_dates']['close_to_open_combo']['metrics']['total_return']:.4%}",
            f"- 剔除回填日后 c2c 超额总收益：{sensitivity['comparisons_without_backfill']['close_to_close_excess_total_return']:.4%}",
            f"- 剔除回填日后 o2c 超额总收益：{sensitivity['comparisons_without_backfill']['open_to_close_excess_total_return']:.4%}",
            f"- 剔除回填日后 c2o 超额总收益：{sensitivity['comparisons_without_backfill']['close_to_open_excess_total_return']:.4%}",
            "",
            "## 证据与放行结论",
            f"- 当前组合是否已有组合级回测证据：{verdict['current_combo_has_portfolio_level_backtest_evidence']}",
            f"- 是否已有独立 end-to-end 回测：{verdict['current_combo_has_independent_end_to_end_backtest']}",
            f"- close-to-close vs open-to-close 是否方向一致：{verdict['close_to_close_vs_open_to_close_direction_consistent']}",
            f"- 是否足以解除 promotion freeze：{verdict['sufficient_to_lift_promotion_freeze']}",
            f"- 最小可信结论：{verdict['minimal_credible_conclusion']}",
            "",
            "## 上游冻结上下文",
            f"- portfolio_backtest_report 原状态：{report['upstream_gap_context']['prior_portfolio_backtest_status']}",
            f"- factor_validation_report release decision：{report['upstream_gap_context']['validation_release_decision']}",
            f"- promotion freeze（上游）= {report['upstream_gap_context']['prior_promotion_freeze']}",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    output_dir = root / "outputs" / "evolution"
    output_dir.mkdir(parents=True, exist_ok=True)
    holdout_reserve_path = (
        Path(args.reserved_holdout_path).expanduser().resolve()
        if args.reserved_holdout_path
        else output_dir / DEFAULT_HOLDOUT_RESERVE_FILE
    )
    holdout_reserve = load_holdout_reserve(holdout_reserve_path)
    reserved_holdout_dates = normalize_trade_dates(
        list(holdout_reserve.get("reserved_holdout_dates", [])) + normalize_trade_dates(args.reserved_holdout_dates)
    )

    panel_path = root / "data" / "factors" / "panel_phase1.parquet"
    regime_path = root / "data" / "regimes" / "regime_labels_phase1.parquet"
    weight_path = Path(args.weight_path).expanduser().resolve() if args.weight_path else root / "outputs" / "weight_maps" / "regime_weight_map_phase1.json"
    audit_path = Path(args.audit_path).expanduser().resolve() if args.audit_path else root / "outputs" / "audit_reports" / "factor_audit_phase1.json"
    analog_path = output_dir / "regime_analog_windows.json"
    current_market_path = output_dir / "current_market_fingerprint.json"
    iteration_pool_path = output_dir / "factor_iteration_pool.json"
    validation_report_path = output_dir / "factor_validation_report.json"
    portfolio_backtest_path = output_dir / "portfolio_backtest_report.json"
    overfit_path = output_dir / "overfit_audit_report.json"
    promotion_path = output_dir / "factor_promotion_decision.json"
    selection_history_dir = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/selection_history")
    recommendation_db = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db")

    weights = read_json(weight_path)
    audit = read_json(audit_path)
    label_mode = infer_label_mode(weights, audit, args.label_mode)
    if label_mode not in LABEL_MODE_TO_COLUMN:
        raise ValueError(f"unsupported label_mode: {label_mode}")
    primary_label_column = LABEL_MODE_TO_COLUMN[label_mode]
    analog = read_json(analog_path) if analog_path.exists() else {"analog_windows": [], "methodology": {}}
    current_market = read_json(current_market_path)
    iteration_pool = read_json(iteration_pool_path)
    validation_report = read_json(validation_report_path)
    portfolio_backtest = read_json(portfolio_backtest_path)
    overfit_report = read_json(overfit_path)
    promotion = read_json(promotion_path)

    regime_map = {row["regime_id"]: row for row in weights["regime_weight_map"]}
    factor_union = collect_weight_map_factor_union(weights)
    latest_weight_plan_factor_ids = [
        str(item["factor_id"])
        for item in weights.get("latest_weight_plan", {}).get("selected_factors", [])
    ]
    current_portfolio_structure = portfolio_backtest.get("current_portfolio_structure", {})
    base_factor_id = current_portfolio_structure.get("base_factor_id", "prebreakout_v41")
    base_weight = float(weights.get("base_weight", current_portfolio_structure.get("base_weight", 0.4)))
    selected_factor_usage_summary = build_selected_factor_usage_summary(weights, audit)
    current_active_factor_ids = [base_factor_id, *factor_union]
    latest_selection_layers_note = build_selection_layers_note(label_mode, audit_path, weight_path, weights, audit)

    selection_history_dates = sorted(p.stem for p in selection_history_dir.glob("*.json"))
    db_only_dates: list[str] = []
    db_dates: set[str] = set()
    if recommendation_db.exists():
        conn = sqlite3.connect(recommendation_db)
        db_dates = {
            str(row[0])
            for row in conn.execute(
                "select distinct recommend_date from recommendation_fact where strategy_id='prebreakout_v41'"
            )
        }
        conn.close()
        db_only_dates = sorted(d for d in db_dates if d not in selection_history_dates)

    panel = pd.read_parquet(
        panel_path,
        columns=[
            "trade_date",
            "ts_code",
            "base_factor_score_norm",
            "next_return_close_to_close_1d",
            "next_return_open_to_close_1d",
            "next_return_close_to_open_1d",
            *factor_union,
        ],
    )
    regime = pd.read_parquet(regime_path, columns=["trade_date", "regime_id"])
    regime_dates = set(regime["trade_date"].astype(str))
    eligible_dates = sorted(set(selection_history_dates) & regime_dates)
    reserved_holdout_dates = [d for d in reserved_holdout_dates if d in eligible_dates]
    consumable_eligible_dates = [d for d in eligible_dates if d not in set(reserved_holdout_dates)]

    replay_panel = panel[panel["trade_date"].isin(consumable_eligible_dates)].copy()
    replay_panel = replay_panel.merge(regime, on="trade_date", how="inner")
    replay_panel["base_factor_score_norm_z"] = zscore_by_date(replay_panel, "base_factor_score_norm")
    for factor_id in factor_union:
        replay_panel[f"{factor_id}_z"] = zscore_by_date(replay_panel, factor_id)

    factor_signs = {
        row["factor_id"]: (1.0 if float(row.get("rank_ic_mean", 0.0) or 0.0) >= 0 else -1.0)
        for row in audit.get("reports", [])
    }
    label_specs = [
        ("close_to_close_combo", "next_return_close_to_close_1d", "combo_score"),
        ("close_to_close_base", "next_return_close_to_close_1d", "base_score_only"),
        ("open_to_close_combo", "next_return_open_to_close_1d", "combo_score"),
        ("open_to_close_base", "next_return_open_to_close_1d", "base_score_only"),
        ("close_to_open_combo", "next_return_close_to_open_1d", "combo_score"),
        ("close_to_open_base", "next_return_close_to_open_1d", "base_score_only"),
    ]
    replay_results = run_replay(replay_panel, regime_map, factor_signs, label_specs)
    comparisons = build_comparisons(replay_results)
    direction_consistency = build_direction_consistency(comparisons, replay_results, label_mode)

    without_backfill_dates = [d for d in consumable_eligible_dates if d not in BACKFILL_DATES]
    sensitivity_panel = replay_panel[replay_panel["trade_date"].isin(without_backfill_dates)].copy()
    sensitivity_results = run_replay(sensitivity_panel, regime_map, factor_signs, label_specs)
    sensitivity_comparisons = build_comparisons(sensitivity_results)

    top5 = analog.get("analog_windows", [])[:5]

    def _mean_or_zero(key: str) -> float:
        values = [
            float(item.get("subsequent_market_proxy_returns", {}).get(key))
            for item in top5
            if item.get("subsequent_market_proxy_returns", {}).get(key) is not None
        ]
        return float(sum(values) / len(values)) if values else 0.0

    effective_dates_by_label = {
        key: replay_results[key]["effective_trade_dates"]
        for key in replay_results
    }
    excluded_from_effective_replay = {
        key: sorted(set(eligible_dates) - set(dates))
        for key, dates in effective_dates_by_label.items()
    }
    current_proxy_regime = current_market.get("market_regime_summary", {}).get(
        "current_proxy_regime",
        current_market.get("style_and_sector", {}).get("broad_market_proxy", {}).get("regime_id"),
    )
    sensitivity_comparisons = build_comparisons(sensitivity_results)
    eligible_trade_days = len(eligible_dates)
    primary_combo_key = f"{label_mode}_combo"
    primary_base_key = f"{label_mode}_base"
    effective_primary_days = replay_results[primary_combo_key]["metrics"]["days"]

    verdict = {
        "current_combo_has_portfolio_level_backtest_evidence": "yes_but_minimal_replay_only_for_current_weight_map",
        "current_combo_has_independent_end_to_end_backtest": False,
        "close_to_close_vs_open_to_close_direction_consistent": direction_consistency["combo_direction_consistent"] and direction_consistency["excess_direction_consistent"],
        "close_to_close_vs_open_to_close_absolute_direction_consistent": direction_consistency["combo_direction_consistent"],
        "close_to_close_vs_open_to_close_excess_direction_consistent": direction_consistency["excess_direction_consistent"],
        "primary_label_mode": label_mode,
        "primary_label_column": primary_label_column,
        "primary_vs_close_to_close_combo_direction_consistent": direction_consistency["primary_vs_close_to_close_combo_direction_consistent"],
        "primary_vs_close_to_close_excess_direction_consistent": direction_consistency["primary_vs_close_to_close_excess_direction_consistent"],
        "sufficient_to_lift_promotion_freeze": False,
        "minimal_credible_conclusion": (
            f"当前 {label_mode} canonical 组合已补出最小可信 replay：primary combo 总收益 {replay_results[primary_combo_key]['metrics']['total_return']:.4%}、"
            f"相对 base 超额 {comparisons[f'{label_mode}_excess_total_return']:.4%}；"
            f"同一 replay 组合在三口径离线对照下为 c2c={replay_results['close_to_close_combo']['metrics']['total_return']:.4%} / "
            f"o2c={replay_results['open_to_close_combo']['metrics']['total_return']:.4%} / "
            f"c2o={replay_results['close_to_open_combo']['metrics']['total_return']:.4%}（该对照不是三套 canonical formal backtest 的横向结论）。"
            f"即便剔除回填日后 primary 超额仍为 {sensitivity_comparisons[f'{label_mode}_excess_total_return']:.4%}，"
            f"它依然只是 {eligible_trade_days} 个 eligible trade days / {effective_primary_days} 个有效回放日上的离线 replay，"
            "不能替代独立 end-to-end 回测或独立 OOS；当前虽已有三口径 nested validation 与收益级 analog replay，但它们仍不是独立 OOS，因此不足以解除 promotion freeze。"
        ),
        "key_reasons": [
            f"已有 {label_mode} canonical 当前权重图对齐的组合级 replay 证据，但仅限 panel 内最小可信重放。",
            "close-to-close / open-to-close / close-to-open 三种收益口径的离线 replay 绝对收益与相对 base 超额仍存在结构差异；这里的 cross-check 不是三套 canonical formal backtest 的横向结论。",
            "当前虽已有三口径 nested validation 与收益级 analog replay，但二者仍不是独立 OOS；promotion_freeze 仍为 true。",
            f"benchmark_eligible 当前为 {eligible_trade_days} 天、{label_mode} 有效回放日为 {effective_primary_days} 天；样本仍短，不能外推为稳定可交易优势。",
        ],
    }

    report = {
        "report_type": "current_combo_replay_report",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "source_paths": {
            "panel": str(panel_path),
            "regime": str(regime_path),
            "weight_map": str(weight_path),
            "factor_audit": str(audit_path),
            "current_combo_holdout_reserve": str(holdout_reserve_path),
            "current_market_fingerprint": str(current_market_path),
            "regime_analog_windows": str(analog_path),
            "factor_iteration_pool": str(iteration_pool_path),
            "factor_validation_report": str(validation_report_path),
            "portfolio_backtest_report": str(portfolio_backtest_path),
            "overfit_audit_report": str(overfit_path),
            "factor_promotion_decision": str(promotion_path),
            "selection_history_dir": str(selection_history_dir),
            "recommendation_db": str(recommendation_db),
        },
        "source_mtimes": {
            "panel": iso_mtime(panel_path),
            "regime": iso_mtime(regime_path),
            "weight_map": iso_mtime(weight_path),
            "factor_audit": iso_mtime(audit_path),
            "current_combo_holdout_reserve": iso_mtime(holdout_reserve_path),
            "current_market_fingerprint": iso_mtime(current_market_path),
            "regime_analog_windows": iso_mtime(analog_path),
            "factor_iteration_pool": iso_mtime(iteration_pool_path),
            "factor_validation_report": iso_mtime(validation_report_path),
            "portfolio_backtest_report": iso_mtime(portfolio_backtest_path),
            "overfit_audit_report": iso_mtime(overfit_path),
            "factor_promotion_decision": iso_mtime(promotion_path),
        },
        "current_combo_definition": {
            "selection_mode": weights.get("selection_mode", current_portfolio_structure.get("selection_mode")),
            "base_factor_id": base_factor_id,
            "base_weight": base_weight,
            "support_pool_count": int(weights.get("support_pool_count", current_portfolio_structure.get("support_pool_count", 0) or 0)),
            "fallback_pool_count": int(weights.get("fallback_pool_count", current_portfolio_structure.get("fallback_pool_count", 0) or 0)),
            "canonical_label_mode": label_mode,
            "canonical_label_column": primary_label_column,
            "latest_weight_map_trade_date": weights.get("latest_regime", {}).get("trade_date"),
            "latest_weight_plan_regime": weights.get("latest_weight_plan", {}).get("regime_id"),
            "latest_weight_plan_factor_ids": latest_weight_plan_factor_ids,
            "regime_union_factor_ids": factor_union,
            "regime_count": len(weights.get("regime_weight_map", [])),
            "regime_factor_coverage": build_regime_factor_coverage(weights),
            "selected_factor_usage_summary": selected_factor_usage_summary,
            "current_active_factor_ids_including_base": current_active_factor_ids,
            "current_proxy_trade_date": current_market.get("trade_date"),
            "current_proxy_regime": current_proxy_regime,
            "current_proxy_regime_confidence": current_market.get("market_regime_summary", {}).get(
                "current_proxy_regime_confidence",
                current_market.get("style_and_sector", {}).get("broad_market_proxy", {}).get("regime_confidence"),
            ),
            "latest_selection_layers_note": latest_selection_layers_note,
        },
        "benchmark_eligible_summary": {
            "selection_history_days": len(selection_history_dates),
            "selection_history_backfill_dates_present": [d for d in BACKFILL_DATES if d in selection_history_dates],
            "recommendation_db_only_days": db_only_dates,
            "recommendation_db_total_distinct_days": len(db_dates),
            "eligible_trade_days": len(eligible_dates),
            "consumable_eligible_trade_days": len(consumable_eligible_dates),
            "eligible_start": eligible_dates[0] if eligible_dates else None,
            "eligible_end": eligible_dates[-1] if eligible_dates else None,
            "eligible_dates": eligible_dates,
            "consumable_eligible_dates": consumable_eligible_dates,
            "reserved_holdout_dates": reserved_holdout_dates,
            "excluded_by_reserved_holdout_dates": reserved_holdout_dates,
            "effective_replay_days": {
                key: replay_results[key]["metrics"]["days"]
                for key in replay_results
            },
            "excluded_from_effective_replay": excluded_from_effective_replay,
        },
        "replay_assumptions": {
            "top_n": TOP_N,
            "round_trip_cost": ROUND_TRIP_COST,
            "factor_source": "latest regime_weight_map union of selected_factors across regimes",
            "score_formula": "base_weight * z(base_factor_score_norm) + Σ regime_weight_i * sign(rank_ic_mean_i) * z(factor_i)",
            "primary_label_mode": label_mode,
            "primary_label_column": primary_label_column,
            "label_columns": LABEL_MODE_TO_COLUMN,
            "reserved_holdout_dates": reserved_holdout_dates,
            "holdout_reserve_note": holdout_reserve.get(
                "reason",
                "reserved_holdout_dates 会在 replay 样本选择阶段先行排除；即便后续 next-day 标签到来，也不应被 current_combo replay 自动消耗。",
            ),
            "cash_buffer_handling": "cash_buffer 不参与个股打分；replay 只比较 invested sleeve 的 top20 组合与 base。",
            "execution_caveat": "该 replay 为 panel 标签离线重放，不含涨跌停、成交量约束、冲击成本、真实撮合与组合容量约束。",
        },

        "factor_signs": {factor_id: float(factor_signs.get(factor_id, 1.0)) for factor_id in factor_union},
        "replay_results": replay_results,
        "comparisons": comparisons,
        "direction_consistency": direction_consistency,
        "sensitivity": {
            "backfill_dates": BACKFILL_DATES,
            "without_backfill_dates": sensitivity_results,
            "comparisons_without_backfill": sensitivity_comparisons,
        },
        "upstream_gap_context": {
            "prior_portfolio_backtest_status": portfolio_backtest.get("conclusion", {}).get("status"),
            "prior_portfolio_backtest_summary": portfolio_backtest.get("conclusion", {}).get("summary"),
            "validation_release_decision": validation_report.get("release_recommendation", {}).get("decision"),
            "validation_release_reason": validation_report.get("release_recommendation", {}).get("reason"),
            "prior_promotion_freeze": promotion.get("global_constraints", {}).get("promotion_freeze"),
            "prior_overfit_verdict": overfit_report.get("overall_verdict", {}).get("summary"),
        },
        "analog_validation": {
            "top3_windows": top5[:3],
            "top5_mean_5d": _mean_or_zero("5d"),
            "top5_mean_20d": _mean_or_zero("20d"),
            "top5_mean_60d": _mean_or_zero("60d"),
            "method_caveat": analog.get("methodology", {}).get("caveat"),
        },
        "verdict": verdict,
    }

    json_path = output_dir / f"{args.output_stem}.json"
    md_path = output_dir / f"{args.output_stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "json": str(json_path),
                "md": str(md_path),
                "factor_union": factor_union,
                "close_to_close_excess_total_return": report["comparisons"]["close_to_close_excess_total_return"],
                "open_to_close_excess_total_return": report["comparisons"]["open_to_close_excess_total_return"],
                "promotion_freeze_lifted": report["verdict"]["sufficient_to_lift_promotion_freeze"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
