#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为当前最新辅助层生成 close-to-close vs open-to-close 标签敏感性对照产物。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


BACKFILL_DATES = ["20260224", "20260225"]
DEFAULT_ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
DEFAULT_OUTPUT_JSON = DEFAULT_ROOT / "outputs" / "audit_reports" / "current_combo_label_sensitivity.json"
DEFAULT_OUTPUT_MD = DEFAULT_ROOT / "outputs" / "audit_reports" / "current_combo_label_sensitivity.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build current combo label sensitivity artifact")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="factor_factory root")
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON), help="output json path")
    parser.add_argument("--output-md", default=str(DEFAULT_OUTPUT_MD), help="output markdown path")
    return parser.parse_args()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    def _zscore(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        valid = s.notna()
        if not valid.any():
            return pd.Series(np.nan, index=s.index, dtype=float)
        mean = float(s[valid].mean())
        std = float(s[valid].std())
        if std == 0 or np.isnan(std):
            out = pd.Series(np.nan, index=s.index, dtype=float)
            out.loc[valid] = 0.0
            return out
        return (s - mean) / std

    return df.groupby("trade_date", group_keys=False)[col].apply(_zscore)


def safe_spearman_corr(x: pd.Series, y: pd.Series) -> float:
    pair = pd.DataFrame({
        "x": pd.to_numeric(x, errors="coerce"),
        "y": pd.to_numeric(y, errors="coerce"),
    }).dropna()
    if len(pair) < 2:
        return float("nan")
    xr = pair["x"].rank(method="average")
    yr = pair["y"].rank(method="average")
    x_std = float(xr.std())
    y_std = float(yr.std())
    if x_std == 0 or y_std == 0 or np.isnan(x_std) or np.isnan(y_std):
        return float("nan")
    corr = xr.corr(yr, method="pearson")
    return float(corr) if pd.notna(corr) else float("nan")


def compute_daily_rank_ic(df: pd.DataFrame, score_col: str, return_col: str) -> pd.Series:
    rows: list[tuple[pd.Timestamp, float]] = []
    for trade_date, day in df.groupby("trade_date", sort=True):
        subset = day[[score_col, return_col]].dropna()
        if len(subset) < 20:
            continue
        ic = safe_spearman_corr(subset[score_col], subset[return_col])
        if pd.notna(ic):
            rows.append((trade_date, float(ic)))
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series({trade_date: value for trade_date, value in rows}).sort_index()


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    wealth = (1.0 + returns.fillna(0.0)).cumprod()
    peak = wealth.cummax()
    drawdown = wealth / peak - 1.0
    return float(drawdown.min())


def annualized_sharpe(returns: pd.Series) -> float:
    x = returns.dropna()
    if len(x) < 2:
        return 0.0
    std = float(x.std())
    if std == 0 or np.isnan(std):
        return 0.0
    return float(x.mean() / std * np.sqrt(252))


def calmar_ratio(returns: pd.Series) -> float:
    x = returns.dropna()
    if x.empty:
        return 0.0
    annual = float((1.0 + x.mean()) ** 252 - 1.0)
    mdd = abs(max_drawdown(x))
    if mdd == 0:
        return 0.0
    return annual / mdd


def build_metrics_from_series(returns: pd.Series, turnover: pd.Series | None = None) -> dict[str, Any]:
    series = returns.dropna().sort_index()
    if series.empty:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "turnover": 0.0,
            "days": 0,
        }
    total = float((1.0 + series).prod() - 1.0)
    annual = float((1.0 + total) ** (252 / max(1, len(series))) - 1.0)
    turnover_value = 0.0
    if turnover is not None and not turnover.empty:
        turnover_value = float(turnover.reindex(series.index).dropna().mean())
    return {
        "total_return": total,
        "annualized_return": annual,
        "sharpe": annualized_sharpe(series),
        "max_drawdown": max_drawdown(series),
        "calmar": calmar_ratio(series),
        "win_rate": float((series > 0).mean()),
        "turnover": turnover_value,
        "days": int(len(series)),
    }


def evaluate_long_only_and_spread(
    df: pd.DataFrame,
    score_col: str,
    return_col: str,
    top_n: int,
    round_trip_cost: float,
) -> dict[str, Any]:
    daily_returns: list[tuple[pd.Timestamp, float]] = []
    daily_turnover: list[tuple[pd.Timestamp, float]] = []
    daily_spread: list[tuple[pd.Timestamp, float]] = []
    prev_holdings: set[str] = set()

    for trade_date, day in df.groupby("trade_date", sort=True):
        subset = day[["ts_code", score_col, return_col]].dropna()
        if len(subset) < top_n:
            continue
        chosen = subset.nlargest(top_n, score_col).copy()
        bottom = subset.nsmallest(top_n, score_col).copy()
        holdings = set(chosen["ts_code"].astype(str))
        turnover = 1.0 if not prev_holdings else 1.0 - len(holdings & prev_holdings) / max(1, len(holdings | prev_holdings))
        prev_holdings = holdings
        gross_return = float(chosen[return_col].mean())
        net_return = gross_return - round_trip_cost * turnover
        spread = float(chosen[return_col].mean() - bottom[return_col].mean())
        daily_returns.append((trade_date, net_return))
        daily_turnover.append((trade_date, float(turnover)))
        daily_spread.append((trade_date, spread))

    returns = pd.Series({d: v for d, v in daily_returns}).sort_index() if daily_returns else pd.Series(dtype=float)
    turnover = pd.Series({d: v for d, v in daily_turnover}).sort_index() if daily_turnover else pd.Series(dtype=float)
    spread = pd.Series({d: v for d, v in daily_spread}).sort_index() if daily_spread else pd.Series(dtype=float)
    return {
        "daily_returns": returns,
        "daily_turnover": turnover,
        "daily_spread": spread,
        "portfolio_metrics": build_metrics_from_series(returns, turnover),
        "long_short_spread_mean": float(spread.mean()) if not spread.empty else 0.0,
        "long_short_positive_ratio": float((spread > 0).mean()) if not spread.empty else 0.0,
    }


def format_direction(sign: float, score_name: str = "factor") -> str:
    return f"long_high_{score_name}" if sign >= 0 else f"long_low_{score_name}"


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = float(sum(weights.values()))
    if total == 0:
        return {k: 0.0 for k in weights}
    return {k: float(v / total) for k, v in weights.items()}


def find_report(audit: dict[str, Any], factor_id: str) -> dict[str, Any]:
    for row in audit.get("reports", []):
        if row.get("factor_id") == factor_id:
            return row
    return {}


def infer_official_close_sign(row: dict[str, Any]) -> float:
    split_reports = row.get("split_reports") or []
    if split_reports:
        try:
            return float(split_reports[0].get("sign", 1.0))
        except Exception:
            pass
    return 1.0 if float(row.get("rank_ic_mean", 0.0) or 0.0) >= 0 else -1.0


def factor_label_summary(
    clean_df: pd.DataFrame,
    factor_id: str,
    return_col: str,
    top_n: int,
    round_trip_cost: float,
) -> dict[str, Any]:
    daily_ic = compute_daily_rank_ic(clean_df, factor_id, return_col)
    rank_ic_mean = float(daily_ic.mean()) if not daily_ic.empty else 0.0
    direction_sign = 1.0 if rank_ic_mean >= 0 else -1.0
    oriented_col = f"{factor_id}__oriented__{return_col}"
    oriented_df = clean_df[["trade_date", "ts_code", factor_id, return_col]].copy()
    oriented_df[oriented_col] = zscore_by_date(oriented_df, factor_id) * direction_sign
    eval_result = evaluate_long_only_and_spread(oriented_df, oriented_col, return_col, top_n, round_trip_cost)
    aligned_positive_ratio = 0.0
    if not daily_ic.empty:
        aligned_positive_ratio = float(((daily_ic * direction_sign) > 0).mean())
    return {
        "rank_ic_mean": rank_ic_mean,
        "rank_ic_abs": abs(rank_ic_mean),
        "rank_ic_days": int(len(daily_ic)),
        "native_direction": format_direction(direction_sign),
        "native_direction_sign": float(direction_sign),
        "aligned_daily_ic_positive_ratio": aligned_positive_ratio,
        "portfolio_metrics_net": eval_result["portfolio_metrics"],
        "long_short_spread_mean": eval_result["long_short_spread_mean"],
        "long_short_positive_ratio": eval_result["long_short_positive_ratio"],
    }


def combo_label_summary(
    clean_df: pd.DataFrame,
    score_col: str,
    return_col: str,
    top_n: int,
    round_trip_cost: float,
    direction_description: str,
) -> dict[str, Any]:
    daily_ic = compute_daily_rank_ic(clean_df, score_col, return_col)
    eval_result = evaluate_long_only_and_spread(clean_df, score_col, return_col, top_n, round_trip_cost)
    return {
        "rank_ic_mean": float(daily_ic.mean()) if not daily_ic.empty else 0.0,
        "rank_ic_abs": abs(float(daily_ic.mean())) if not daily_ic.empty else 0.0,
        "rank_ic_days": int(len(daily_ic)),
        "aligned_daily_ic_positive_ratio": float((daily_ic > 0).mean()) if not daily_ic.empty else 0.0,
        "direction": direction_description,
        "portfolio_metrics_net": eval_result["portfolio_metrics"],
        "long_short_spread_mean": eval_result["long_short_spread_mean"],
        "long_short_positive_ratio": eval_result["long_short_positive_ratio"],
    }


def build_combo_score(df: pd.DataFrame, weights: dict[str, float], signs: dict[str, float], out_col: str) -> pd.Series:
    score = pd.Series(0.0, index=df.index, dtype=float)
    for factor_id, weight in weights.items():
        score = score + float(weight) * float(signs.get(factor_id, 1.0)) * df[f"{factor_id}_z"].fillna(0.0)
    df[out_col] = score
    return df[out_col]


def build_findings(report: dict[str, Any]) -> list[str]:
    factor_level = report["factor_level"]
    combos = report["combo_diagnostics"]
    latest_combo = combos["latest_weight_plan_aux_official_close_signs"]
    union_combo = combos["regime_union_equal_weight_aux_official_close_signs"]

    latest_c = latest_combo["close_to_close"]
    latest_o = latest_combo["open_to_close"]
    latest_o_native = latest_combo["open_to_close_native_sign_reference"]
    union_c = union_combo["close_to_close"]
    union_o = union_combo["open_to_close"]
    union_o_native = union_combo["open_to_close_native_sign_reference"]

    mismatched = [
        row["factor_id"]
        for row in factor_level
        if row["delta"]["official_close_sign_mismatch_vs_clean_close"]
    ]
    flipped = [row["factor_id"] for row in factor_level if row["delta"]["direction_flipped_between_labels"]]
    largest_ic_drop = max(
        factor_level,
        key=lambda row: abs(float(row["delta"]["rank_ic_mean_open_minus_close"])),
    )

    return [
        (
            "最新 3 因子辅助层（official close signs）在 clean window 上，"
            f"close-to-close rank_ic={latest_c['rank_ic_mean']:.4f}、净总收益={latest_c['portfolio_metrics_net']['total_return']:.2%}；"
            f"open-to-close rank_ic={latest_o['rank_ic_mean']:.4f}、净总收益={latest_o['portfolio_metrics_net']['total_return']:.2%}，"
            f"收益口径切换带来 {latest_combo['delta_open_minus_close']['portfolio_total_return']:.2%} 的净总收益差。"
        ),
        (
            "即便把 3 因子组合按 open-to-close 重新定方向，"
            f"open-to-close 净总收益也只恢复到 {latest_o_native['portfolio_metrics_net']['total_return']:.2%}，"
            "说明损失不只是 sign mismatch，还包含口径本身的信息衰减。"
        ),
        (
            f"当前 union 中 clean-window 方向与 official close sign 不一致的因子有 {mismatched or ['无']}；"
            f"标签间发生方向翻转的因子有 {flipped or ['无']}。"
        ),
        (
            f"因子层面 rank_ic 变化最大的是 {largest_ic_drop['factor_id']}，"
            f"open-close 差值={largest_ic_drop['delta']['rank_ic_mean_open_minus_close']:.4f}。"
        ),
        (
            "4 因子 regime union 等权诊断组合在 official close signs 下，"
            f"close-to-close 净总收益={union_c['portfolio_metrics_net']['total_return']:.2%}，"
            f"open-to-close 净总收益={union_o['portfolio_metrics_net']['total_return']:.2%}；"
            f"若改用 open-native signs，则 open-to-close 可回到 {union_o_native['portfolio_metrics_net']['total_return']:.2%}，"
            "说明 union 口径敏感性很大一部分来自方向切换。"
        ),
    ]


def render_markdown(report: dict[str, Any]) -> str:
    clean = report["clean_window"]
    ctx = report["current_context"]
    combos = report["combo_diagnostics"]
    latest = combos["latest_weight_plan_aux_official_close_signs"]
    union = combos["regime_union_equal_weight_aux_official_close_signs"]
    blend = combos["latest_weight_plan_plus_masked_base_official_close_signs"]

    lines = [
        "# Current Combo Label Sensitivity",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 当前代理 regime：{ctx['current_proxy_regime']}（trade_date={ctx['current_proxy_trade_date']}）",
        f"- 最新权重计划 regime：{ctx['latest_weight_plan_regime']}",
        f"- clean window 过滤：{clean['filter']}",
        f"- clean window trade days：{clean['benchmark_eligible_trade_days']}（{clean['clean_window_start']} ~ {clean['clean_window_end']}）",
        f"- clean window 全量样本行数：{clean['clean_window_total_rows']}",
        f"- benchmark_eligible 行数：{clean['benchmark_eligible_row_count']}",
        f"- backfill dates：{', '.join(clean['backfill_dates'])}",
        "",
        "## 当前因子集合",
        f"- latest_weight_plan：{', '.join(ctx['latest_weight_plan_factor_ids'])}",
        f"- regime_union：{', '.join(ctx['regime_union_factor_ids'])}",
        f"- official close signs：{json.dumps(ctx['official_close_signs'], ensure_ascii=False)}",
        "",
        "## 3 因子最新辅助层（official close signs）",
        (
            f"- close-to-close：rank_ic={latest['close_to_close']['rank_ic_mean']:.4f}，"
            f"净总收益={latest['close_to_close']['portfolio_metrics_net']['total_return']:.2%}，"
            f"Sharpe={latest['close_to_close']['portfolio_metrics_net']['sharpe']:.3f}，"
            f"long-short 均值={latest['close_to_close']['long_short_spread_mean']:.4%}"
        ),
        (
            f"- open-to-close（保持 official close signs）：rank_ic={latest['open_to_close']['rank_ic_mean']:.4f}，"
            f"净总收益={latest['open_to_close']['portfolio_metrics_net']['total_return']:.2%}，"
            f"Sharpe={latest['open_to_close']['portfolio_metrics_net']['sharpe']:.3f}，"
            f"long-short 均值={latest['open_to_close']['long_short_spread_mean']:.4%}"
        ),
        (
            f"- open-to-close（改用 open-native signs 仅作诊断）：rank_ic={latest['open_to_close_native_sign_reference']['rank_ic_mean']:.4f}，"
            f"净总收益={latest['open_to_close_native_sign_reference']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        (
            f"- 去除 backfill 后：close-to-close={latest['without_backfill_dates']['close_to_close']['portfolio_metrics_net']['total_return']:.2%}，"
            f"open-to-close={latest['without_backfill_dates']['open_to_close']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        "",
        "## 4 因子 regime union 等权诊断",
        (
            f"- close-to-close：rank_ic={union['close_to_close']['rank_ic_mean']:.4f}，"
            f"净总收益={union['close_to_close']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        (
            f"- open-to-close（保持 official close signs）：rank_ic={union['open_to_close']['rank_ic_mean']:.4f}，"
            f"净总收益={union['open_to_close']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        (
            f"- open-to-close（改用 open-native signs）：rank_ic={union['open_to_close_native_sign_reference']['rank_ic_mean']:.4f}，"
            f"净总收益={union['open_to_close_native_sign_reference']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        "",
        "## base + 辅助层参考（masked base）",
        (
            f"- close-to-close：rank_ic={blend['close_to_close']['rank_ic_mean']:.4f}，"
            f"净总收益={blend['close_to_close']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        (
            f"- open-to-close：rank_ic={blend['open_to_close']['rank_ic_mean']:.4f}，"
            f"净总收益={blend['open_to_close']['portfolio_metrics_net']['total_return']:.2%}"
        ),
        "",
        "## 关键结论",
    ]
    lines.extend([f"- {item}" for item in report["primary_findings"]])
    lines.extend([
        "",
        "## 风险 / 局限",
        *[f"- {item}" for item in report["limitations"]],
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    output_json = Path(args.output_json).expanduser().resolve()
    output_md = Path(args.output_md).expanduser().resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)

    config_phase = load_yaml(root / "config" / "phase1.yaml")
    config_costs = load_yaml(root / "config" / "backtest_costs.yaml")
    top_n = int(config_costs.get("portfolio_rules", {}).get("max_position_count", 20))
    round_trip_cost = float(config_costs.get("friction", {}).get("round_trip_cost", 0.002))

    panel_path = root / "data" / "factors" / "panel_phase1.parquet"
    weight_path = root / "outputs" / "weight_maps" / "regime_weight_map_phase1.json"
    audit_path = root / "outputs" / "audit_reports" / "factor_audit_phase1.json"
    pool_path = root / "outputs" / "evolution" / "factor_iteration_pool.json"
    portfolio_report_path = root / "outputs" / "evolution" / "portfolio_backtest_report.json"
    fingerprint_path = root / "outputs" / "evolution" / "current_market_fingerprint.json"

    weight_map = json.loads(weight_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    portfolio_report = json.loads(portfolio_report_path.read_text(encoding="utf-8"))
    fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))

    latest_plan = weight_map.get("latest_weight_plan", {})
    latest_items = latest_plan.get("selected_factors", [])
    latest_weights = {str(item["factor_id"]): float(item.get("weight", 0.0)) for item in latest_items}
    latest_factor_ids = list(latest_weights)
    regime_union_factor_ids = list(
        pool.get("pool_summary", {})
        .get("selection_layers", {})
        .get("latest_weight_plan_factor_union", latest_factor_ids)
    )
    aux_budget = float(latest_plan.get("aux_weight_budget", sum(latest_weights.values()) or 0.45))
    base_weight = float(latest_plan.get("base_weight", config_phase.get("base_factor", {}).get("fixed_weight", 0.4)))
    union_equal_weights = {factor_id: float(aux_budget / max(1, len(regime_union_factor_ids))) for factor_id in regime_union_factor_ids}

    official_close_signs: dict[str, float] = {}
    official_audit_status: dict[str, str] = {}
    for factor_id in regime_union_factor_ids:
        row = find_report(audit, factor_id)
        official_close_signs[factor_id] = infer_official_close_sign(row)
        official_audit_status[factor_id] = str(row.get("status", "unknown"))

    needed_columns = [
        "trade_date",
        "ts_code",
        "base_factor_score_norm",
        "benchmark_eligible",
        "benchmark_date_eligible",
        "next_return_close_to_close_1d",
        "next_return_open_to_close_1d",
        *sorted(set(regime_union_factor_ids)),
    ]
    panel = pd.read_parquet(panel_path, columns=needed_columns)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    panel["benchmark_eligible"] = panel["benchmark_eligible"].fillna(0).astype(int)
    panel["benchmark_date_eligible"] = panel["benchmark_date_eligible"].fillna(0).astype(int)

    clean_df = panel.loc[panel["benchmark_date_eligible"] == 1].copy()
    clean_df = clean_df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    clean_df["base_score_masked"] = clean_df["base_factor_score_norm"].where(clean_df["benchmark_eligible"] == 1)
    clean_df["base_z"] = zscore_by_date(clean_df, "base_score_masked")
    for factor_id in regime_union_factor_ids:
        clean_df[f"{factor_id}_z"] = zscore_by_date(clean_df, factor_id)

    factor_level = []
    clean_close_signs: dict[str, float] = {}
    clean_open_signs: dict[str, float] = {}
    for factor_id in regime_union_factor_ids:
        close_summary = factor_label_summary(clean_df, factor_id, "next_return_close_to_close_1d", top_n, round_trip_cost)
        open_summary = factor_label_summary(clean_df, factor_id, "next_return_open_to_close_1d", top_n, round_trip_cost)
        clean_close_signs[factor_id] = float(close_summary["native_direction_sign"])
        clean_open_signs[factor_id] = float(open_summary["native_direction_sign"])
        factor_level.append(
            {
                "factor_id": factor_id,
                "official_audit_status": official_audit_status.get(factor_id, "unknown"),
                "in_latest_weight_plan": factor_id in latest_factor_ids,
                "in_regime_union": factor_id in regime_union_factor_ids,
                "official_close_sign": official_close_signs[factor_id],
                "clean_close_sign": clean_close_signs[factor_id],
                "clean_open_sign": clean_open_signs[factor_id],
                "close_to_close": close_summary,
                "open_to_close": open_summary,
                "delta": {
                    "rank_ic_mean_open_minus_close": float(open_summary["rank_ic_mean"] - close_summary["rank_ic_mean"]),
                    "long_short_spread_mean_open_minus_close": float(open_summary["long_short_spread_mean"] - close_summary["long_short_spread_mean"]),
                    "direction_flipped_between_labels": bool(clean_close_signs[factor_id] != clean_open_signs[factor_id]),
                    "official_close_sign_mismatch_vs_clean_close": bool(official_close_signs[factor_id] != clean_close_signs[factor_id]),
                    "official_close_sign_mismatch_vs_open": bool(official_close_signs[factor_id] != clean_open_signs[factor_id]),
                },
            }
        )

    build_combo_score(clean_df, latest_weights, official_close_signs, "score_latest_aux_official")
    build_combo_score(clean_df, union_equal_weights, official_close_signs, "score_union_aux_official")
    clean_df["score_latest_plus_base_official"] = base_weight * clean_df["base_z"].fillna(0.0) + clean_df["score_latest_aux_official"]
    build_combo_score(clean_df, latest_weights, clean_open_signs, "score_latest_aux_open_native")
    build_combo_score(clean_df, union_equal_weights, clean_open_signs, "score_union_aux_open_native")

    no_backfill_df = clean_df.loc[~clean_df["trade_date"].dt.strftime("%Y%m%d").isin(BACKFILL_DATES)].copy()

    latest_close = combo_label_summary(
        clean_df,
        "score_latest_aux_official",
        "next_return_close_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    latest_open = combo_label_summary(
        clean_df,
        "score_latest_aux_official",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    latest_open_native = combo_label_summary(
        clean_df,
        "score_latest_aux_open_native",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_open_native_signs",
    )

    union_close = combo_label_summary(
        clean_df,
        "score_union_aux_official",
        "next_return_close_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    union_open = combo_label_summary(
        clean_df,
        "score_union_aux_official",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    union_open_native = combo_label_summary(
        clean_df,
        "score_union_aux_open_native",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_open_native_signs",
    )

    blend_close = combo_label_summary(
        clean_df,
        "score_latest_plus_base_official",
        "next_return_close_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_masked_base_plus_official_close_signs",
    )
    blend_open = combo_label_summary(
        clean_df,
        "score_latest_plus_base_official",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_masked_base_plus_official_close_signs",
    )

    latest_no_backfill_close = combo_label_summary(
        no_backfill_df,
        "score_latest_aux_official",
        "next_return_close_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    latest_no_backfill_open = combo_label_summary(
        no_backfill_df,
        "score_latest_aux_official",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    union_no_backfill_close = combo_label_summary(
        no_backfill_df,
        "score_union_aux_official",
        "next_return_close_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )
    union_no_backfill_open = combo_label_summary(
        no_backfill_df,
        "score_union_aux_official",
        "next_return_open_to_close_1d",
        top_n,
        round_trip_cost,
        "long_high_official_close_signs",
    )

    report = {
        "report_type": "current_combo_label_sensitivity",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "source_paths": {
            "panel": str(panel_path),
            "weight_map": str(weight_path),
            "factor_audit": str(audit_path),
            "factor_iteration_pool": str(pool_path),
            "portfolio_backtest_report": str(portfolio_report_path),
            "current_market_fingerprint": str(fingerprint_path),
            "phase1_config": str(root / "config" / "phase1.yaml"),
            "backtest_costs": str(root / "config" / "backtest_costs.yaml"),
        },
        "clean_window": {
            "filter": "benchmark_date_eligible == 1",
            "benchmark_eligible_trade_days": int(clean_df["trade_date"].nunique()),
            "benchmark_eligible_row_count": int(clean_df["benchmark_eligible"].sum()),
            "clean_window_total_rows": int(len(clean_df)),
            "clean_window_start": clean_df["trade_date"].min().strftime("%Y%m%d") if not clean_df.empty else None,
            "clean_window_end": clean_df["trade_date"].max().strftime("%Y%m%d") if not clean_df.empty else None,
            "backfill_dates": BACKFILL_DATES,
            "without_backfill_trade_days": int(no_backfill_df["trade_date"].nunique()),
        },
        "current_context": {
            "current_proxy_trade_date": str(fingerprint.get("trade_date")),
            "current_proxy_regime": fingerprint.get("style_and_sector", {}).get("broad_market_proxy", {}).get("regime_id"),
            "latest_weight_plan_regime": latest_plan.get("regime_id"),
            "latest_weight_plan_factor_ids": latest_factor_ids,
            "regime_union_factor_ids": regime_union_factor_ids,
            "latest_weight_plan_weights_raw": latest_weights,
            "latest_weight_plan_weights_normalized": normalize_weights(latest_weights),
            "regime_union_equal_weights_raw": union_equal_weights,
            "regime_union_equal_weights_normalized": normalize_weights(union_equal_weights),
            "official_close_signs": official_close_signs,
            "clean_close_signs": clean_close_signs,
            "clean_open_signs": clean_open_signs,
            "base_weight": base_weight,
            "aux_weight_budget": aux_budget,
            "top_n": top_n,
            "round_trip_cost": round_trip_cost,
            "selection_mode": portfolio_report.get("current_portfolio_structure", {}).get("selection_mode"),
        },
        "factor_level": factor_level,
        "combo_diagnostics": {
            "latest_weight_plan_aux_official_close_signs": {
                "description": "当前 shrink_uptrend 最新 3 因子辅助层；权重固定为 latest_weight_plan，方向固定为 canonical close-to-close 官方方向。",
                "weights_raw": latest_weights,
                "weights_normalized": normalize_weights(latest_weights),
                "signs": {factor_id: official_close_signs[factor_id] for factor_id in latest_factor_ids},
                "close_to_close": latest_close,
                "open_to_close": latest_open,
                "delta_open_minus_close": {
                    "rank_ic_mean": float(latest_open["rank_ic_mean"] - latest_close["rank_ic_mean"]),
                    "portfolio_total_return": float(latest_open["portfolio_metrics_net"]["total_return"] - latest_close["portfolio_metrics_net"]["total_return"]),
                    "portfolio_sharpe": float(latest_open["portfolio_metrics_net"]["sharpe"] - latest_close["portfolio_metrics_net"]["sharpe"]),
                    "long_short_spread_mean": float(latest_open["long_short_spread_mean"] - latest_close["long_short_spread_mean"]),
                },
                "open_to_close_native_sign_reference": {
                    "weights_raw": latest_weights,
                    "weights_normalized": normalize_weights(latest_weights),
                    "signs": {factor_id: clean_open_signs[factor_id] for factor_id in latest_factor_ids},
                    **latest_open_native,
                },
                "without_backfill_dates": {
                    "close_to_close": latest_no_backfill_close,
                    "open_to_close": latest_no_backfill_open,
                },
            },
            "regime_union_equal_weight_aux_official_close_signs": {
                "description": "4 因子 regime union 等权诊断组合；用于覆盖当前 union，不代表 canonical 实盘权重。",
                "weights_raw": union_equal_weights,
                "weights_normalized": normalize_weights(union_equal_weights),
                "signs": official_close_signs,
                "close_to_close": union_close,
                "open_to_close": union_open,
                "delta_open_minus_close": {
                    "rank_ic_mean": float(union_open["rank_ic_mean"] - union_close["rank_ic_mean"]),
                    "portfolio_total_return": float(union_open["portfolio_metrics_net"]["total_return"] - union_close["portfolio_metrics_net"]["total_return"]),
                    "portfolio_sharpe": float(union_open["portfolio_metrics_net"]["sharpe"] - union_close["portfolio_metrics_net"]["sharpe"]),
                    "long_short_spread_mean": float(union_open["long_short_spread_mean"] - union_close["long_short_spread_mean"]),
                },
                "open_to_close_native_sign_reference": {
                    "weights_raw": union_equal_weights,
                    "weights_normalized": normalize_weights(union_equal_weights),
                    "signs": clean_open_signs,
                    **union_open_native,
                },
                "without_backfill_dates": {
                    "close_to_close": union_no_backfill_close,
                    "open_to_close": union_no_backfill_open,
                },
            },
            "latest_weight_plan_plus_masked_base_official_close_signs": {
                "description": "参考项：masked base_factor_score_norm + 当前 3 因子辅助层 official close signs。base 仅在 benchmark_eligible 行上参与 zscore。",
                "base_weight": base_weight,
                "aux_weights_raw": latest_weights,
                "aux_weights_normalized": normalize_weights(latest_weights),
                "signs": {factor_id: official_close_signs[factor_id] for factor_id in latest_factor_ids},
                "close_to_close": blend_close,
                "open_to_close": blend_open,
                "delta_open_minus_close": {
                    "rank_ic_mean": float(blend_open["rank_ic_mean"] - blend_close["rank_ic_mean"]),
                    "portfolio_total_return": float(blend_open["portfolio_metrics_net"]["total_return"] - blend_close["portfolio_metrics_net"]["total_return"]),
                    "portfolio_sharpe": float(blend_open["portfolio_metrics_net"]["sharpe"] - blend_close["portfolio_metrics_net"]["sharpe"]),
                    "long_short_spread_mean": float(blend_open["long_short_spread_mean"] - blend_close["long_short_spread_mean"]),
                },
            },
        },
        "limitations": [
            f"全套结果只使用 benchmark_date_eligible == 1 的 clean window（{int(clean_df['trade_date'].nunique())} 个 trade days），不是 664 天污染基准；样本仍然很短，不能单独用于 promotion 放行。",
            "factor 方向在 current_combo_label_sensitivity 中按 clean window 重新估计只用于诊断；official combo 评估仍固定使用 canonical close-to-close 方向，以反映当前实际辅助层口径敏感性。",
            "portfolio 指标是基于 panel 标签的离线 top20 重放，包含 20bps round-trip cost，但不含涨跌停、成交量约束、冲击成本与真实撮合。",
            "latest_weight_plan_plus_masked_base_official_close_signs 只是参考项；由于 base 分数只在 benchmark_eligible 行上参与 clean zscore，它不能替代真正的 end-to-end 组合回测。",
        ],
    }
    report["primary_findings"] = build_findings(report)

    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "json": str(output_json),
        "md": str(output_md),
        "benchmark_eligible_trade_days": report["clean_window"]["benchmark_eligible_trade_days"],
        "latest_weight_plan_factor_ids": latest_factor_ids,
        "regime_union_factor_ids": regime_union_factor_ids,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
