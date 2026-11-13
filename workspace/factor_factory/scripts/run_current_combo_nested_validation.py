#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为当前组合生成最小独立 OOS / micro nested validation 证据。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from greenfield_multifactor_lib import build_walkforward_slices


ROOT_DEFAULT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
TOP_N = 20
ROUND_TRIP_COST = 0.002
MIN_OUTER_TRAIN_DAYS = 12
INNER_VALIDATION_DAYS = 4
OUTER_TEST_DAYS = 4
MIN_OUTER_TEST_DAYS = 2
BASE_WEIGHT = 0.4
EPS = 1e-12
DEFAULT_OUTPUT_STEM = "current_combo_nested_validation"
DEFAULT_WALKFORWARD_OUTPUT_STEM = "current_combo_walkforward_validation"
LABEL_MODE_TO_COLUMN = {
    "close_to_close": "next_return_close_to_close_1d",
    "open_to_close": "next_return_open_to_close_1d",
    "close_to_open": "next_return_close_to_open_1d",
}
LABEL_MODE_TO_ALIAS = {
    "close_to_close": "c2c",
    "open_to_close": "o2c",
    "close_to_open": "c2o",
}
LABEL_MODE_TO_CANONICAL_SUFFIX = {
    "close_to_close": "",
    "open_to_close": ".o2c_full45_33d",
    "close_to_open": ".c2o_full45_33d",
}
LABEL_MODE_TO_OUTPUT_SUFFIX = {
    "close_to_close": "",
    "open_to_close": ".o2c_canonical",
    "close_to_open": ".c2o_canonical",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run current combo micro nested validation")
    parser.add_argument("--root", default=str(ROOT_DEFAULT))
    parser.add_argument("--weight-path", default=None)
    parser.add_argument("--audit-path", default=None)
    parser.add_argument("--replay-path", default=None)
    parser.add_argument("--portfolio-path", default=None)
    parser.add_argument("--label-mode", choices=sorted(LABEL_MODE_TO_COLUMN), default=None)
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    parser.add_argument("--fold-scheme", choices=["micro_nested", "walkforward"], default="micro_nested")
    return parser.parse_args()


def infer_label_mode(weight_report: dict[str, Any], audit_report: dict[str, Any], args_label_mode: str | None) -> str:
    if args_label_mode in LABEL_MODE_TO_COLUMN:
        return str(args_label_mode)
    for candidate in (
        weight_report.get("audit_context", {}).get("label_mode"),
        audit_report.get("label_mode"),
    ):
        if candidate in LABEL_MODE_TO_COLUMN:
            return str(candidate)
    return "close_to_close"


def canonical_paths(root: Path, label_mode: str) -> dict[str, Path]:
    canonical_suffix = LABEL_MODE_TO_CANONICAL_SUFFIX[label_mode]
    output_suffix = LABEL_MODE_TO_OUTPUT_SUFFIX[label_mode]
    return {
        "weight_path": root / "outputs" / "weight_maps" / f"regime_weight_map_phase1{canonical_suffix}.json",
        "audit_path": root / "outputs" / "audit_reports" / f"factor_audit_phase1{canonical_suffix}.json",
        "replay_path": root / "outputs" / "evolution" / f"current_combo_replay_report{output_suffix}.json",
        "portfolio_path": root / "outputs" / "evolution" / f"current_combo_portfolio_backtest{output_suffix}.json",
    }


def combo_key(label_mode: str) -> str:
    return f"{label_mode}_combo"


def base_key(label_mode: str) -> str:
    return f"{label_mode}_base"


def preferred_output_stem(label_mode: str, output_stem: str, fold_scheme: str) -> str:
    base_stem = output_stem
    if fold_scheme == "walkforward" and output_stem == DEFAULT_OUTPUT_STEM:
        base_stem = DEFAULT_WALKFORWARD_OUTPUT_STEM
    if label_mode == "close_to_close":
        return base_stem
    suffix = LABEL_MODE_TO_OUTPUT_SUFFIX[label_mode]
    if base_stem.endswith(suffix):
        return base_stem
    return f"{base_stem}{suffix}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def all_same_nonflat_direction(values: list[float]) -> bool:
    directions = {direction_of(value) for value in values}
    return len(directions) == 1 and "flat" not in directions


def build_direction_consistency(aggregate_oos: dict[str, Any], primary_label_mode: str) -> dict[str, Any]:
    combo_totals = {
        label_mode: float(aggregate_oos[combo_key(label_mode)]["total_return"])
        for label_mode in LABEL_MODE_TO_COLUMN
    }
    excess_totals = {
        label_mode: float(aggregate_oos[label_mode]["excess_total_return"])
        for label_mode in LABEL_MODE_TO_COLUMN
    }
    return {
        "primary_label_mode": primary_label_mode,
        "close_to_close_combo_direction": direction_of(combo_totals["close_to_close"]),
        "open_to_close_combo_direction": direction_of(combo_totals["open_to_close"]),
        "close_to_open_combo_direction": direction_of(combo_totals["close_to_open"]),
        "combo_absolute_direction_consistent": all_same_nonflat_direction(list(combo_totals.values())),
        "close_to_close_excess_direction": direction_of(excess_totals["close_to_close"]),
        "open_to_close_excess_direction": direction_of(excess_totals["open_to_close"]),
        "close_to_open_excess_direction": direction_of(excess_totals["close_to_open"]),
        "combo_excess_direction_consistent": all_same_nonflat_direction(list(excess_totals.values())),
        "primary_vs_close_to_close_combo_direction_consistent": same_nonflat_direction(
            combo_totals[primary_label_mode],
            combo_totals["close_to_close"],
        ),
        "primary_vs_close_to_close_excess_direction_consistent": same_nonflat_direction(
            excess_totals[primary_label_mode],
            excess_totals["close_to_close"],
        ),
        "by_label_mode": {
            label_mode: {
                "combo_direction": direction_of(combo_totals[label_mode]),
                "excess_direction": direction_of(excess_totals[label_mode]),
            }
            for label_mode in LABEL_MODE_TO_COLUMN
        },
    }


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    def _zscore(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        std = float(s.std(ddof=0))
        if np.isnan(std) or std == 0:
            return pd.Series(0.0, index=s.index, dtype=float)
        return (s - float(s.mean())) / std

    return df.groupby("trade_date", group_keys=False)[col].transform(_zscore)


def annualized_sharpe(returns: pd.Series) -> float:
    x = returns.dropna()
    if len(x) < 2:
        return 0.0
    std = float(x.std(ddof=0))
    if std == 0 or np.isnan(std):
        return 0.0
    return float(x.mean() / std * np.sqrt(252))


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    wealth = (1.0 + returns.fillna(0.0)).cumprod()
    peak = wealth.cummax()
    drawdown = wealth / peak - 1.0
    return float(drawdown.min()) if not drawdown.empty else 0.0


def calmar_ratio(returns: pd.Series) -> float:
    x = returns.dropna()
    if x.empty:
        return 0.0
    annual = float((1.0 + x.mean()) ** 252 - 1.0)
    mdd = abs(max_drawdown(x))
    if mdd == 0:
        return 0.0
    return float(annual / mdd)


def build_metrics_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
    rets = pd.Series({row["date"]: float(row["net_return"]) for row in rows}).sort_index()
    turns = pd.Series({row["date"]: float(row["turnover"]) for row in rows}).sort_index()
    total = float((1.0 + rets).prod() - 1.0)
    annual = float((1.0 + total) ** (252 / max(1, len(rets))) - 1.0)
    return {
        "days": int(len(rets)),
        "total_return": total,
        "annualized_return": annual,
        "sharpe": annualized_sharpe(rets),
        "max_drawdown": max_drawdown(rets),
        "calmar": calmar_ratio(rets),
        "win_rate": float((rets > 0).mean()),
        "turnover": float(turns.mean() if not turns.empty else 0.0),
        "start": str(rets.index.min()) if not rets.empty else None,
        "end": str(rets.index.max()) if not rets.empty else None,
    }


def safe_daily_rank_ic(day: pd.DataFrame, factor_col: str, return_col: str) -> float:
    subset = day[[factor_col, return_col]].dropna()
    if len(subset) < 20:
        return float("nan")
    xr = subset[factor_col].rank(method="average")
    yr = subset[return_col].rank(method="average")
    xr_std = float(xr.std())
    yr_std = float(yr.std())
    if xr_std == 0 or yr_std == 0 or np.isnan(xr_std) or np.isnan(yr_std):
        return float("nan")
    corr = xr.corr(yr, method="pearson")
    return float(corr) if pd.notna(corr) else float("nan")


def infer_factor_signs(train_df: pd.DataFrame, factor_ids: list[str], return_col: str) -> tuple[dict[str, float], dict[str, Any]]:
    signs: dict[str, float] = {}
    diagnostics: dict[str, Any] = {}
    for factor_id in factor_ids:
        daily_ics: list[float] = []
        for _, day in train_df.groupby("trade_date", sort=True):
            ic = safe_daily_rank_ic(day, factor_id, return_col)
            if pd.notna(ic):
                daily_ics.append(float(ic))
        rank_ic_mean = float(np.mean(daily_ics)) if daily_ics else 0.0
        sign = 1.0 if rank_ic_mean >= 0 else -1.0
        signs[factor_id] = sign
        diagnostics[factor_id] = {
            "rank_ic_mean": rank_ic_mean,
            "rank_ic_days": int(len(daily_ics)),
            "sign": sign,
            "direction": "long_high_factor" if sign >= 0 else "long_low_factor",
        }
    return signs, diagnostics


def normalize_weights(weights: dict[str, float], target_sum: float | None = None) -> dict[str, float]:
    out = {str(k): float(v) for k, v in weights.items() if float(v) != 0.0}
    if target_sum is None:
        return out
    current = float(sum(out.values()))
    if current == 0:
        return out
    scale = float(target_sum) / current
    return {k: float(v * scale) for k, v in out.items()}


def build_candidate_templates(weight_report: dict[str, Any], factor_union: list[str]) -> list[dict[str, Any]]:
    latest_plan = weight_report.get("latest_weight_plan", {})
    latest_items = latest_plan.get("selected_factors", []) or []
    latest_aux_budget = float(latest_plan.get("aux_weight_budget", 0.45) or 0.45)
    latest_official = {
        str(item["factor_id"]): float(item.get("weight", 0.0))
        for item in latest_items
    }
    latest_equal = {
        str(item["factor_id"]): float(latest_aux_budget / len(latest_items))
        for item in latest_items
    } if latest_items else {}

    templates = [
        {
            "name": "dynamic_regime_official_nominal",
            "kind": "dynamic",
            "description": "按最新 regime_weight_map 的原始权重做逐日 regime 切换。",
            "candidate_factor_ids": factor_union,
        },
        {
            "name": "dynamic_regime_equal_aux",
            "kind": "dynamic_equal",
            "description": "沿用最新 regime_weight_map 的因子集合，但每个 regime 内把 aux budget 平均分配。",
            "candidate_factor_ids": factor_union,
        },
        {
            "name": "static_latest_plan_official_nominal",
            "kind": "static",
            "description": "固定使用 latest_weight_plan 的 3 因子与原始权重。",
            "weights": latest_official,
            "candidate_factor_ids": list(latest_official.keys()),
        },
        {
            "name": "static_latest_plan_equal_aux",
            "kind": "static",
            "description": "固定使用 latest_weight_plan 的 3 因子，并把 aux budget 均分。",
            "weights": latest_equal,
            "candidate_factor_ids": list(latest_equal.keys()),
        },
    ]
    return templates


def build_dynamic_weights(plan: dict[str, Any], equal_weight: bool) -> dict[str, float]:
    items = plan.get("selected_factors", []) or []
    if not items:
        return {}
    if equal_weight:
        aux_budget = float(plan.get("aux_weight_budget", 0.45) or 0.45)
        return {str(item["factor_id"]): float(aux_budget / len(items)) for item in items}
    weights = {str(item["factor_id"]): float(item.get("weight", 0.0)) for item in items}
    return normalize_weights(weights, None)


def score_day(
    day: pd.DataFrame,
    template: dict[str, Any],
    regime_map: dict[str, Any],
    factor_signs: dict[str, float],
) -> pd.Series:
    score = BASE_WEIGHT * day["base_factor_score_norm_z"].fillna(0.0)
    if template["kind"] == "dynamic":
        regime_id = str(day["regime_id"].iloc[0])
        weights = build_dynamic_weights(regime_map.get(regime_id, {}), equal_weight=False)
    elif template["kind"] == "dynamic_equal":
        regime_id = str(day["regime_id"].iloc[0])
        weights = build_dynamic_weights(regime_map.get(regime_id, {}), equal_weight=True)
    else:
        weights = normalize_weights(template.get("weights", {}), None)
    for factor_id, weight in weights.items():
        score = score + float(weight) * float(factor_signs.get(factor_id, 1.0)) * day[f"{factor_id}_z"].fillna(0.0)
    return score


def evaluate_dates(
    df: pd.DataFrame,
    date_sequence: list[str],
    template: dict[str, Any],
    regime_map: dict[str, Any],
    factor_signs: dict[str, float],
    return_col: str,
    series_name: str,
    benchmark_only: bool = False,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    prev_holdings: set[str] = set()
    for trade_date in date_sequence:
        day = df.loc[df["trade_date"] == trade_date].copy()
        if day.empty:
            continue
        if benchmark_only:
            subset = day.loc[
                day["benchmark_eligible"].fillna(0).astype(int) == 1,
                ["ts_code", return_col],
            ].dropna(subset=[return_col])
            if subset.empty:
                continue
            holdings_frame = subset.copy()
        else:
            day["nested_score"] = score_day(day, template, regime_map, factor_signs)
            subset = day[["ts_code", "nested_score", return_col]].dropna(subset=["nested_score", return_col])
            if len(subset) < TOP_N:
                continue
            holdings_frame = subset.nlargest(TOP_N, "nested_score").copy()
        holdings = set(holdings_frame["ts_code"].astype(str))
        turnover = 1.0 if not rows else 1.0 - len(holdings & prev_holdings) / max(1, len(holdings | prev_holdings))
        prev_holdings = holdings
        gross_return = float(holdings_frame[return_col].mean())
        net_return = gross_return - ROUND_TRIP_COST * turnover
        row = {
            "date": str(trade_date),
            "gross_return": gross_return,
            "net_return": float(net_return),
            "turnover": float(turnover),
            "holding_count": int(len(holdings_frame)),
            "series": series_name,
        }
        if not benchmark_only:
            row["top_codes"] = list(holdings_frame["ts_code"].astype(str).head(5))
        rows.append(row)
    return {
        "metrics": build_metrics_from_rows(rows),
        "daily_records": rows,
        "effective_trade_dates": [row["date"] for row in rows],
    }


def build_micro_nested_folds(effective_dates: list[str]) -> list[dict[str, Any]]:
    folds: list[dict[str, Any]] = []
    outer_train_end = MIN_OUTER_TRAIN_DAYS
    fold_id = 1
    total = len(effective_dates)
    while outer_train_end < total:
        outer_test_dates = effective_dates[outer_train_end: outer_train_end + OUTER_TEST_DAYS]
        if len(outer_test_dates) < MIN_OUTER_TEST_DAYS:
            break
        outer_train_dates = effective_dates[:outer_train_end]
        if len(outer_train_dates) <= INNER_VALIDATION_DAYS:
            break
        inner_train_dates = outer_train_dates[:-INNER_VALIDATION_DAYS]
        inner_validation_dates = outer_train_dates[-INNER_VALIDATION_DAYS:]
        folds.append(
            {
                "fold_id": fold_id,
                "outer_train_dates": outer_train_dates,
                "inner_train_dates": inner_train_dates,
                "inner_validation_dates": inner_validation_dates,
                "outer_test_dates": outer_test_dates,
            }
        )
        fold_id += 1
        outer_train_end += OUTER_TEST_DAYS
    return folds


def build_walkforward_folds(effective_dates: list[str]) -> list[dict[str, Any]]:
    raw_folds = build_walkforward_slices(
        effective_dates,
        train_days=MIN_OUTER_TRAIN_DAYS,
        validation_days=INNER_VALIDATION_DAYS,
        test_days=OUTER_TEST_DAYS,
        step_days=OUTER_TEST_DAYS,
        min_train_days=MIN_OUTER_TRAIN_DAYS,
    )
    folds: list[dict[str, Any]] = []
    for fold_id, fold in enumerate(raw_folds, start=1):
        train_dates = list(fold.get("train_dates", []))
        validation_dates = list(fold.get("validation_dates", []))
        test_dates = list(fold.get("test_dates", []))
        if len(test_dates) < MIN_OUTER_TEST_DAYS:
            continue
        folds.append(
            {
                "fold_id": fold_id,
                "outer_train_dates": train_dates,
                "inner_train_dates": train_dates,
                "inner_validation_dates": validation_dates,
                "outer_test_dates": test_dates,
            }
        )
    return folds


def build_folds(effective_dates: list[str], fold_scheme: str) -> list[dict[str, Any]]:
    if fold_scheme == "walkforward":
        return build_walkforward_folds(effective_dates)
    return build_micro_nested_folds(effective_dates)


def build_comparison(combo_metrics: dict[str, Any], base_metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "combo_total_return": float(combo_metrics["total_return"]),
        "base_total_return": float(base_metrics["total_return"]),
        "excess_total_return": float(combo_metrics["total_return"] - base_metrics["total_return"]),
        "combo_direction": direction_of(float(combo_metrics["total_return"])),
        "base_direction": direction_of(float(base_metrics["total_return"])),
    }


def write_daily_csv(path: Path, fold_rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    net_fields: list[str] = []
    turnover_fields: list[str] = []
    for label_mode, alias in LABEL_MODE_TO_ALIAS.items():
        net_fields.extend([f"{alias}_combo_net_return", f"{alias}_base_net_return"])
        turnover_fields.append(f"{alias}_combo_turnover")
    fieldnames = ["fold_id", "date", "template_name", *net_fields, *turnover_fields]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in fold_rows:
            writer.writerow(row)


def build_markdown(report: dict[str, Any]) -> str:
    agg = report["aggregate_oos"]
    coverage = report["sample_coverage"]
    verdict = report["verdict"]
    template_selection = report["template_selection_summary"]
    comparison = report["comparison_vs_existing_replay"]
    primary_label_mode = report["primary_label_mode"]
    primary_label_column = report["primary_label_column"]
    direction = agg["direction_consistency"]
    report_title = report.get("report_title") or "Current Combo Nested Validation Report"
    evidence_label = report.get("evidence_label") or "nested"

    lines = [
        f"# {report_title}",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 主验证口径：{primary_label_mode} ({primary_label_column})",
        f"- 验证口径：{report['validation_scope']}",
        f"- clean effective days：{coverage['effective_clean_days']}",
        f"- outer OOS days：{coverage['outer_oos_days']} ({coverage['outer_oos_coverage_ratio']:.2%})",
        f"- folds：{coverage['fold_count']} | 配置=train≥{report['fold_config']['min_outer_train_days']} / inner_val={report['fold_config']['inner_validation_days']} / outer_test={report['fold_config']['outer_test_days']}",
        "",
        "## 聚合 OOS 结果",
    ]
    for label_mode, alias in LABEL_MODE_TO_ALIAS.items():
        lines.append(
            f"- {alias}/{label_mode}：combo={agg[combo_key(label_mode)]['total_return']:.4%}, "
            f"base={agg[base_key(label_mode)]['total_return']:.4%}, "
            f"excess={agg[label_mode]['excess_total_return']:.4%}, "
            f"days={agg[combo_key(label_mode)]['days']}"
        )
    lines.extend(
        [
            (
                f"- 三口径绝对收益方向一致性：{direction['combo_absolute_direction_consistent']} "
                f"(c2c={direction['close_to_close_combo_direction']}, "
                f"o2c={direction['open_to_close_combo_direction']}, "
                f"c2o={direction['close_to_open_combo_direction']})"
            ),
            (
                f"- 三口径超额方向一致性：{direction['combo_excess_direction_consistent']} "
                f"(c2c={direction['close_to_close_excess_direction']}, "
                f"o2c={direction['open_to_close_excess_direction']}, "
                f"c2o={direction['close_to_open_excess_direction']})"
            ),
            (
                f"- primary({primary_label_mode}) 相对 c2c 方向一致性："
                f"combo={direction['primary_vs_close_to_close_combo_direction_consistent']} / "
                f"excess={direction['primary_vs_close_to_close_excess_direction_consistent']}"
            ),
            "",
            "## 模板选择稳定性",
            f"- 选中过的模板：{', '.join(template_selection['chosen_template_names'])}",
            f"- 模板切换次数：{template_selection['template_switch_count']}",
            f"- 各模板命中次数：{json.dumps(template_selection['template_counts'], ensure_ascii=False)}",
            "",
            "## 相对现有 full replay 的变化",
        ]
    )
    for label_mode, alias in LABEL_MODE_TO_ALIAS.items():
        key = f"{label_mode}_combo_total_return"
        lines.append(
            f"- {alias} total_return: existing={comparison['existing_replay'][key]:.4%}, "
            f"nested={comparison['nested_oos'][key]:.4%}, "
            f"delta={comparison['delta'][key]:.4%}"
        )
    lines.extend(
        [
            "",
            "## 放行结论",
            f"- 是否已形成最小独立 OOS / {evidence_label} 证据：{verdict['minimal_independent_oos_evidence_present']}",
            f"- 是否足以解除 promotion freeze：{verdict['sufficient_to_lift_promotion_freeze']}",
            f"- 结论：{verdict['summary']}",
        ]
    )

    lines.append("")
    lines.append("## Fold 明细")
    for fold in report["folds"]:
        lines.append(
            f"- fold {fold['fold_id']}: chosen={fold['chosen_template']['name']}; "
            f"train={fold['outer_train_dates'][0]}~{fold['outer_train_dates'][-1]}; "
            f"inner_val={fold['inner_validation_dates'][0]}~{fold['inner_validation_dates'][-1]}; "
            f"test={fold['outer_test_dates'][0]}~{fold['outer_test_dates'][-1]}"
        )
        for label_mode, alias in LABEL_MODE_TO_ALIAS.items():
            lines.append(
                f"  - {alias} test combo/base/excess = "
                f"{fold['test_results'][combo_key(label_mode)]['metrics']['total_return']:.4%} / "
                f"{fold['test_results'][base_key(label_mode)]['metrics']['total_return']:.4%} / "
                f"{fold['test_results'][label_mode]['excess_total_return']:.4%}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    evolution_dir = root / "outputs" / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    fold_scheme = str(args.fold_scheme)
    is_walkforward = fold_scheme == "walkforward"

    panel_path = root / "data" / "factors" / "panel_phase1.parquet"
    regime_path = root / "data" / "regimes" / "regime_labels_phase1.parquet"

    seed_paths = canonical_paths(root, "close_to_close")
    seed_weight_path = Path(args.weight_path).expanduser().resolve() if args.weight_path else seed_paths["weight_path"]
    seed_audit_path = Path(args.audit_path).expanduser().resolve() if args.audit_path else seed_paths["audit_path"]
    seed_weight_report = read_json(seed_weight_path)
    seed_audit_report = read_json(seed_audit_path)
    label_mode = infer_label_mode(seed_weight_report, seed_audit_report, args.label_mode)
    if label_mode not in LABEL_MODE_TO_COLUMN:
        raise ValueError(f"unsupported label_mode: {label_mode}")
    primary_label_column = LABEL_MODE_TO_COLUMN[label_mode]

    paths = canonical_paths(root, label_mode)
    weight_path = Path(args.weight_path).expanduser().resolve() if args.weight_path else paths["weight_path"]
    audit_path = Path(args.audit_path).expanduser().resolve() if args.audit_path else paths["audit_path"]
    replay_path = Path(args.replay_path).expanduser().resolve() if args.replay_path else paths["replay_path"]
    portfolio_path = Path(args.portfolio_path).expanduser().resolve() if args.portfolio_path else paths["portfolio_path"]

    output_stem = preferred_output_stem(label_mode, args.output_stem, fold_scheme)
    output_json = evolution_dir / f"{output_stem}.json"
    output_md = evolution_dir / f"{output_stem}.md"
    output_csv = evolution_dir / f"{output_stem}_daily_returns.csv"

    weight_report = read_json(weight_path)
    replay_report = read_json(replay_path)
    audit_report = read_json(audit_path)
    portfolio_report = read_json(portfolio_path) if portfolio_path.exists() else {}

    effective_dates = list(replay_report["replay_results"][combo_key(label_mode)]["effective_trade_dates"])
    factor_union = list(replay_report["current_combo_definition"]["regime_union_factor_ids"])
    regime_map = {str(row["regime_id"]): row for row in weight_report.get("regime_weight_map", [])}
    full_sample_signs = {
        str(row.get("factor_id")): 1.0 if float(row.get("rank_ic_mean", 0.0) or 0.0) >= 0 else -1.0
        for row in audit_report.get("reports", [])
    }

    panel_columns = list(
        dict.fromkeys(
            [
                "trade_date",
                "ts_code",
                "base_factor_score_norm",
                "benchmark_eligible",
                *LABEL_MODE_TO_COLUMN.values(),
                *factor_union,
            ]
        )
    )
    panel = pd.read_parquet(panel_path, columns=panel_columns)
    panel["trade_date"] = panel["trade_date"].astype(str)
    regime = pd.read_parquet(regime_path, columns=["trade_date", "regime_id"])
    regime["trade_date"] = regime["trade_date"].astype(str)

    nested_df = panel.loc[panel["trade_date"].isin(effective_dates)].merge(regime, on="trade_date", how="inner")
    nested_df = nested_df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    nested_df["base_factor_score_norm_z"] = zscore_by_date(nested_df, "base_factor_score_norm")
    for factor_id in factor_union:
        nested_df[f"{factor_id}_z"] = zscore_by_date(nested_df, factor_id)

    templates = build_candidate_templates(weight_report, factor_union)
    folds = build_folds(effective_dates, fold_scheme)

    aggregate_rows = {
        key: []
        for mode_key in LABEL_MODE_TO_COLUMN
        for key in (combo_key(mode_key), base_key(mode_key))
    }
    daily_csv_rows: list[dict[str, Any]] = []
    fold_reports: list[dict[str, Any]] = []

    for fold in folds:
        inner_train_df = nested_df.loc[nested_df["trade_date"].isin(fold["inner_train_dates"])].copy()
        candidate_rankings: list[dict[str, Any]] = []
        chosen_template: dict[str, Any] | None = None
        chosen_signs: dict[str, float] = {}
        chosen_sign_diag: dict[str, Any] = {}

        for template in templates:
            factor_ids = list(template.get("candidate_factor_ids", []))
            factor_signs, sign_diagnostics = infer_factor_signs(
                inner_train_df,
                factor_ids,
                primary_label_column,
            )
            validation_result = evaluate_dates(
                nested_df,
                fold["inner_validation_dates"],
                template,
                regime_map,
                factor_signs,
                primary_label_column,
                series_name=f"validation_{combo_key(label_mode)}",
                benchmark_only=False,
            )
            candidate_rankings.append(
                {
                    "template_name": template["name"],
                    "description": template["description"],
                    "validation_metrics": validation_result["metrics"],
                    "factor_signs": factor_signs,
                    "factor_sign_diagnostics": sign_diagnostics,
                }
            )

        candidate_rankings.sort(
            key=lambda item: (
                float(item["validation_metrics"]["total_return"]),
                float(item["validation_metrics"]["sharpe"]),
                float(item["validation_metrics"]["win_rate"]),
            ),
            reverse=True,
        )
        if candidate_rankings:
            best = candidate_rankings[0]
            chosen_template = next(item for item in templates if item["name"] == best["template_name"])
            chosen_signs = dict(best["factor_signs"])
            chosen_sign_diag = dict(best["factor_sign_diagnostics"])
        else:
            chosen_template = templates[0]
            chosen_signs = {factor_id: full_sample_signs.get(factor_id, 1.0) for factor_id in factor_union}

        test_results: dict[str, Any] = {}
        combo_records_by_mode: dict[str, dict[str, Any]] = {}
        base_records_by_mode: dict[str, dict[str, Any]] = {}
        for mode_key, return_col in LABEL_MODE_TO_COLUMN.items():
            combo_result = evaluate_dates(
                nested_df,
                fold["outer_test_dates"],
                chosen_template,
                regime_map,
                chosen_signs,
                return_col,
                series_name=combo_key(mode_key),
                benchmark_only=False,
            )
            base_result = evaluate_dates(
                nested_df,
                fold["outer_test_dates"],
                chosen_template,
                regime_map,
                chosen_signs,
                return_col,
                series_name=base_key(mode_key),
                benchmark_only=True,
            )
            aggregate_rows[combo_key(mode_key)].extend(combo_result["daily_records"])
            aggregate_rows[base_key(mode_key)].extend(base_result["daily_records"])
            combo_records_by_mode[mode_key] = {row["date"]: row for row in combo_result["daily_records"]}
            base_records_by_mode[mode_key] = {row["date"]: row for row in base_result["daily_records"]}
            test_results[combo_key(mode_key)] = combo_result
            test_results[base_key(mode_key)] = base_result
            test_results[mode_key] = build_comparison(combo_result["metrics"], base_result["metrics"])

        for trade_date in fold["outer_test_dates"]:
            csv_row: dict[str, Any] = {
                "fold_id": fold["fold_id"],
                "date": trade_date,
                "template_name": chosen_template["name"],
            }
            for mode_key, alias in LABEL_MODE_TO_ALIAS.items():
                csv_row[f"{alias}_combo_net_return"] = combo_records_by_mode[mode_key].get(trade_date, {}).get("net_return")
                csv_row[f"{alias}_base_net_return"] = base_records_by_mode[mode_key].get(trade_date, {}).get("net_return")
                csv_row[f"{alias}_combo_turnover"] = combo_records_by_mode[mode_key].get(trade_date, {}).get("turnover")
            daily_csv_rows.append(csv_row)

        fold_reports.append(
            {
                "fold_id": fold["fold_id"],
                "outer_train_dates": fold["outer_train_dates"],
                "inner_train_dates": fold["inner_train_dates"],
                "inner_validation_dates": fold["inner_validation_dates"],
                "outer_test_dates": fold["outer_test_dates"],
                "candidate_rankings": candidate_rankings,
                "chosen_template": {
                    "name": chosen_template["name"],
                    "description": chosen_template["description"],
                    "factor_signs": chosen_signs,
                    "factor_sign_diagnostics": chosen_sign_diag,
                },
                "test_results": test_results,
            }
        )

    aggregate_oos: dict[str, Any] = {}
    for mode_key in LABEL_MODE_TO_COLUMN:
        agg_combo = build_metrics_from_rows(aggregate_rows[combo_key(mode_key)])
        agg_base = build_metrics_from_rows(aggregate_rows[base_key(mode_key)])
        aggregate_oos[combo_key(mode_key)] = agg_combo
        aggregate_oos[base_key(mode_key)] = agg_base
        aggregate_oos[mode_key] = build_comparison(agg_combo, agg_base)
    aggregate_oos["direction_consistency"] = build_direction_consistency(aggregate_oos, label_mode)

    template_counts: dict[str, int] = {}
    chosen_template_names: list[str] = []
    for fold in fold_reports:
        template_name = fold["chosen_template"]["name"]
        chosen_template_names.append(template_name)
        template_counts[template_name] = template_counts.get(template_name, 0) + 1
    template_switch_count = 0
    for prev, curr in zip(chosen_template_names, chosen_template_names[1:]):
        if prev != curr:
            template_switch_count += 1

    existing_replay: dict[str, float] = {}
    nested_oos: dict[str, float] = {}
    delta: dict[str, float] = {}
    for mode_key in LABEL_MODE_TO_COLUMN:
        series_key = combo_key(mode_key)
        existing_total = portfolio_report.get("minimum_required_metrics", {}).get(series_key, {}).get("total_return")
        if existing_total is None:
            existing_total = replay_report["replay_results"][series_key]["metrics"]["total_return"]
        nested_total = aggregate_oos[series_key]["total_return"]
        existing_replay[f"{mode_key}_combo_total_return"] = float(existing_total)
        nested_oos[f"{mode_key}_combo_total_return"] = float(nested_total)
        delta[f"{mode_key}_combo_total_return"] = float(nested_total - float(existing_total))

    effective_clean_days = len(effective_dates)
    outer_oos_days = int(aggregate_oos[combo_key(label_mode)]["days"])
    last_fold_test_days = len(fold_reports[-1].get("outer_test_dates", [])) if fold_reports else 0
    sample_coverage = {
        "effective_clean_days": int(effective_clean_days),
        "outer_oos_days": outer_oos_days,
        "outer_oos_coverage_ratio": float(outer_oos_days / effective_clean_days) if effective_clean_days else 0.0,
        "fold_count": int(len(fold_reports)),
    }

    primary_combo_total = float(aggregate_oos[combo_key(label_mode)]["total_return"])
    primary_excess_total = float(aggregate_oos[label_mode]["excess_total_return"])
    cross_check_summary = " / ".join(
        f"{LABEL_MODE_TO_ALIAS[mode_key]}={aggregate_oos[combo_key(mode_key)]['total_return']:.4%}"
        for mode_key in LABEL_MODE_TO_COLUMN
    )
    validation_scope = (
        f"walkforward_validation_on_clean_window_existing_templates_only_primary_label_mode={label_mode}"
        if is_walkforward
        else f"micro_nested_validation_on_clean_window_existing_templates_only_primary_label_mode={label_mode}"
    )
    report_title = "Current Combo Walkforward Validation Report" if is_walkforward else "Current Combo Nested Validation Report"
    evidence_label = "walkforward" if is_walkforward else "nested"
    report_type = "current_combo_walkforward_validation" if is_walkforward else "current_combo_nested_validation"
    schema_version = "2026-04-20.current_combo_walkforward_validation.v1" if is_walkforward else "2026-04-20.current_combo_nested_validation.v2"
    methodology_purpose = (
        "在不重跑整条 audit/weight 链的前提下，验证现有 clean window 是否至少能支撑一个最小组合级 walkforward / rolling OOS 证据。"
        if is_walkforward
        else "在不重跑整条 audit/weight 链的前提下，验证现有 clean window 是否至少能支撑一个最小独立 OOS / nested validation 证据。"
    )
    selection_target = (
        f"先在上一段 validation 窗口上按 {label_mode} total_return/sharpe/win_rate 选模板，再把同一模板与同一组 sign 扔到下一段 walkforward test 同时评估 c2c / o2c / c2o。"
        if is_walkforward
        else f"先在 inner_validation 上按 {label_mode} total_return/sharpe/win_rate 选模板，再把同一模板与同一组 sign 扔到 outer_test 同时评估 c2c / o2c / c2o。"
    )
    verdict_summary = (
        f"现有 {effective_clean_days} 个 clean effective days 足以在 {label_mode} canonical 上落一个 walkforward validation："
        f"共形成 {len(fold_reports)} 个 walkforward folds、覆盖 {outer_oos_days} 个 {label_mode} OOS 日。"
        f"聚合后 primary combo {primary_combo_total:.4%}、primary excess {primary_excess_total:.4%}；"
        f"cross-check 为 {cross_check_summary}。"
        "模板也在不同 fold 间切换，说明信号仍不稳。"
        f"这份证据现在只对 {label_mode} 提供最小 label-specific walkforward 骨架，仍不足以解除 promotion freeze。"
        if is_walkforward
        else f"现有 {effective_clean_days} 个 clean effective days 足以在 {label_mode} canonical 上落一个 micro nested validation："
        f"共形成 {len(fold_reports)} 个 outer folds、覆盖 {outer_oos_days} 个 {label_mode} OOS 日。"
        f"聚合后 primary combo {primary_combo_total:.4%}、primary excess {primary_excess_total:.4%}；"
        f"cross-check 为 {cross_check_summary}。"
        "模板也在不同 fold 间切换，说明信号仍不稳。"
        f"这份证据现在只对 {label_mode} 提供最小 label-specific nested 骨架，仍不足以解除 promotion freeze。"
    )

    report = {
        "report_type": report_type,
        "report_title": report_title,
        "schema_version": schema_version,
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(timespec="seconds"),
        "primary_label_mode": label_mode,
        "primary_label_column": primary_label_column,
        "fold_scheme": fold_scheme,
        "evidence_label": evidence_label,
        "applies_to_label_modes": [label_mode],
        "cross_check_label_modes": [mode_key for mode_key in LABEL_MODE_TO_COLUMN if mode_key != label_mode],
        "validation_scope": validation_scope,
        "source_paths": {
            "panel": str(panel_path),
            "regime": str(regime_path),
            "weight_map": str(weight_path),
            "factor_audit": str(audit_path),
            "current_combo_replay_report": str(replay_path),
            "current_combo_portfolio_backtest": str(portfolio_path),
        },
        "source_mtimes": {
            "panel": pd.Timestamp.fromtimestamp(panel_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if panel_path.exists() else None,
            "regime": pd.Timestamp.fromtimestamp(regime_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if regime_path.exists() else None,
            "weight_map": pd.Timestamp.fromtimestamp(weight_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if weight_path.exists() else None,
            "factor_audit": pd.Timestamp.fromtimestamp(audit_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if audit_path.exists() else None,
            "current_combo_replay_report": pd.Timestamp.fromtimestamp(replay_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if replay_path.exists() else None,
            "current_combo_portfolio_backtest": pd.Timestamp.fromtimestamp(portfolio_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if portfolio_path.exists() else None,
        },
        "methodology": {
            "purpose": methodology_purpose,
            "candidate_templates": templates,
            "factor_sign_estimation": f"每个 outer fold 仅用 inner_train 的 {label_mode} 日度 rank IC 均值推导方向；不复用 full-sample audit sign 做 nested 选择。",
            "selection_target": selection_target,
            "independence_scope": "只对 clean window 内的模板选择和方向选择形成部分独立性；上游 factor discovery 与 canonical weight_map 仍来自既有全样本产物，因此这不是完整独立研究闭环。",
            "base_benchmark_note": "base 仍沿用 benchmark_eligible 行构成的 prebreakout_v41 当日持仓；部分日期 benchmark 行数小于 20，这会直接反映在 base OOS 结果里。",
        },
        "fold_config": {
            "fold_scheme": fold_scheme,
            "min_outer_train_days": MIN_OUTER_TRAIN_DAYS,
            "inner_validation_days": INNER_VALIDATION_DAYS,
            "outer_test_days": OUTER_TEST_DAYS,
            "min_outer_test_days": MIN_OUTER_TEST_DAYS,
        },
        "sample_coverage": sample_coverage,
        "effective_clean_dates": effective_dates,
        "folds": fold_reports,
        "aggregate_oos": aggregate_oos,
        "template_selection_summary": {
            "chosen_template_names": chosen_template_names,
            "template_counts": template_counts,
            "template_switch_count": int(template_switch_count),
        },
        "comparison_vs_existing_replay": {
            "primary_label_mode": label_mode,
            "existing_replay": existing_replay,
            "nested_oos": nested_oos,
            "delta": delta,
        },
        "daily_returns_csv": str(output_csv),
        "verdict": {
            "minimal_independent_oos_evidence_present": True,
            "sufficient_to_lift_promotion_freeze": False,
            "summary": verdict_summary,
            "claim_limitations": [
                (
                    "这是 clean window 上的 walkforward validation，不是完整独立 research pipeline。"
                    if is_walkforward
                    else "这是 clean window 上的微型 nested validation，不是完整独立 research pipeline。"
                ),
                "上游 factor discovery / canonical weight_map 仍复用了既有全样本产物。",
                f"outer OOS 共 {outer_oos_days} 天，且最后一个 fold 只有 {last_fold_test_days} 天 test，统计强度仍然较弱。",
                (
                    f"当前这份 walkforward 仅对 {label_mode} 形成 label-specific 放行骨架；其他 label_mode 仍需单独重跑。"
                    if is_walkforward
                    else f"当前这份 nested 仅对 {label_mode} 形成 label-specific 放行骨架；其他 label_mode 仍需单独重跑。"
                ),
            ],
        },
    }

    dump_json(output_json, report)
    output_md.write_text(build_markdown(report), encoding="utf-8")
    write_daily_csv(output_csv, daily_csv_rows)

    print(
        json.dumps(
            {
                "status": "ok",
                "primary_label_mode": label_mode,
                "fold_scheme": fold_scheme,
                "report_json": str(output_json),
                "report_md": str(output_md),
                "report_csv": str(output_csv),
                "fold_count": len(fold_reports),
                "outer_oos_days": outer_oos_days,
                "primary_combo_total_return": primary_combo_total,
                "primary_excess_total_return": primary_excess_total,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
