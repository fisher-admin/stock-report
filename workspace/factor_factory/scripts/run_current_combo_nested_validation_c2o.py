#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 current combo 生成 close-to-open 口径的 label-specific micro nested validation 证据。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

from run_current_combo_nested_validation import (
    ROOT_DEFAULT,
    build_candidate_templates,
    build_comparison,
    build_folds,
    build_metrics_from_rows,
    direction_of,
    dump_json,
    evaluate_dates,
    infer_factor_signs,
    read_json,
    same_nonflat_direction,
    zscore_by_date,
)

PRIMARY_LABEL_MODE = "close_to_open"
LABEL_MODE_TO_COLUMN = {
    "close_to_close": "next_return_close_to_close_1d",
    "open_to_close": "next_return_open_to_close_1d",
    "close_to_open": "next_return_close_to_open_1d",
}
LABEL_MODE_TO_RESULT_KEY = {
    "close_to_close": "close_to_close_combo",
    "open_to_close": "open_to_close_combo",
    "close_to_open": "close_to_open_combo",
}
LABEL_MODE_TO_WEIGHT_FILE = {
    "close_to_close": "regime_weight_map_phase1.json",
    "open_to_close": "regime_weight_map_phase1.o2c_full45_33d.json",
    "close_to_open": "regime_weight_map_phase1.c2o_full45_33d.json",
}
LABEL_MODE_TO_AUDIT_FILE = {
    "close_to_close": "factor_audit_phase1.json",
    "open_to_close": "factor_audit_phase1.o2c_full45_33d.json",
    "close_to_open": "factor_audit_phase1.c2o_full45_33d.json",
}
LABEL_MODE_TO_REPLAY_FILE = {
    "close_to_close": "current_combo_replay_report.json",
    "open_to_close": "current_combo_replay_report.o2c_canonical.json",
    "close_to_open": "current_combo_replay_report.c2o_canonical.json",
}
LABEL_MODE_TO_BACKTEST_FILE = {
    "close_to_close": "current_combo_portfolio_backtest.json",
    "open_to_close": "current_combo_portfolio_backtest.o2c_canonical.json",
    "close_to_open": "current_combo_portfolio_backtest.c2o_canonical.json",
}
DEFAULT_OUTPUT_STEM = "current_combo_nested_validation.c2o_canonical"
ALL_LABEL_MODES = ["close_to_close", "open_to_close", "close_to_open"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run close-to-open label-specific current combo nested validation")
    parser.add_argument("--root", default=str(ROOT_DEFAULT))
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    return parser.parse_args()


def label_text(label_mode: str) -> str:
    return label_mode.replace("_", "-")


def load_existing_total_return(payload: dict[str, Any], label_mode: str, fallback_payload: dict[str, Any]) -> float:
    metric_key = LABEL_MODE_TO_RESULT_KEY[label_mode]
    value = (
        payload.get("minimum_required_metrics", {})
        .get(metric_key, {})
        .get("total_return")
    )
    if value is not None:
        return float(value)
    return float(fallback_payload["replay_results"][metric_key]["metrics"]["total_return"])


def write_daily_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "fold_id",
        "date",
        "template_name",
        "c2c_combo_net_return",
        "c2c_base_net_return",
        "o2c_combo_net_return",
        "o2c_base_net_return",
        "c2o_combo_net_return",
        "c2o_base_net_return",
        "c2c_combo_turnover",
        "o2c_combo_turnover",
        "c2o_combo_turnover",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_markdown(report: dict[str, Any]) -> str:
    agg = report["aggregate_oos"]
    coverage = report["sample_coverage"]
    verdict = report["verdict"]
    template_selection = report["template_selection_summary"]
    comparison = report["comparison_vs_existing_replay"]
    primary_mode = report["primary_label_mode"]
    lines = [
        "# Current Combo Nested Validation Report (close-to-open label-specific)",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- primary_label_mode={primary_mode}",
        f"- 验证口径：{report['validation_scope']}",
        f"- applies_to_label_modes={report['applies_to_label_modes']}",
        f"- clean effective days：{coverage['effective_clean_days']}",
        f"- outer OOS days：{coverage['outer_oos_days']} ({coverage['outer_oos_coverage_ratio']:.2%})",
        f"- folds：{coverage['fold_count']} | 配置=train≥{report['fold_config']['min_outer_train_days']} / inner_val={report['fold_config']['inner_validation_days']} / outer_test={report['fold_config']['outer_test_days']}",
        "",
        "## 聚合 OOS 结果",
        (
            f"- c2o primary：combo={agg['close_to_open_combo']['total_return']:.4%}, "
            f"base={agg['close_to_open_base']['total_return']:.4%}, "
            f"excess={agg['close_to_open']['excess_total_return']:.4%}, "
            f"days={agg['close_to_open_combo']['days']}"
        ),
        (
            f"- c2c cross-check：combo={agg['close_to_close_combo']['total_return']:.4%}, "
            f"base={agg['close_to_close_base']['total_return']:.4%}, "
            f"excess={agg['close_to_close']['excess_total_return']:.4%}, "
            f"days={agg['close_to_close_combo']['days']}"
        ),
        (
            f"- o2c cross-check：combo={agg['open_to_close_combo']['total_return']:.4%}, "
            f"base={agg['open_to_close_base']['total_return']:.4%}, "
            f"excess={agg['open_to_close']['excess_total_return']:.4%}, "
            f"days={agg['open_to_close_combo']['days']}"
        ),
        (
            f"- c2o vs c2c 绝对收益方向一致性：{agg['direction_consistency']['combo_absolute_direction_consistent_c2o_vs_c2c']} "
            f"(c2o={agg['direction_consistency']['close_to_open_combo_direction']}, "
            f"c2c={agg['direction_consistency']['close_to_close_combo_direction']})"
        ),
        (
            f"- c2o vs o2c 绝对收益方向一致性：{agg['direction_consistency']['combo_absolute_direction_consistent_c2o_vs_o2c']} "
            f"(c2o={agg['direction_consistency']['close_to_open_combo_direction']}, "
            f"o2c={agg['direction_consistency']['open_to_close_combo_direction']})"
        ),
        "",
        "## 模板选择稳定性",
        f"- 选中过的模板：{', '.join(template_selection['chosen_template_names'])}",
        f"- 模板切换次数：{template_selection['template_switch_count']}",
        f"- 各模板命中次数：{json.dumps(template_selection['template_counts'], ensure_ascii=False)}",
        "",
        "## 相对现有 full replay / formal backtest 的变化",
        (
            f"- c2o total_return: existing={comparison['existing_replay']['close_to_open_combo_total_return']:.4%}, "
            f"nested={comparison['nested_oos']['close_to_open_combo_total_return']:.4%}, "
            f"delta={comparison['delta']['close_to_open_combo_total_return']:.4%}"
        ),
        (
            f"- c2c total_return: existing={comparison['existing_replay']['close_to_close_combo_total_return']:.4%}, "
            f"nested={comparison['nested_oos']['close_to_close_combo_total_return']:.4%}, "
            f"delta={comparison['delta']['close_to_close_combo_total_return']:.4%}"
        ),
        (
            f"- o2c total_return: existing={comparison['existing_replay']['open_to_close_combo_total_return']:.4%}, "
            f"nested={comparison['nested_oos']['open_to_close_combo_total_return']:.4%}, "
            f"delta={comparison['delta']['open_to_close_combo_total_return']:.4%}"
        ),
        "",
        "## 放行结论",
        f"- 是否已形成最小独立 OOS / nested 证据：{verdict['minimal_independent_oos_evidence_present']}",
        f"- 是否足以解除 promotion freeze：{verdict['sufficient_to_lift_promotion_freeze']}",
        f"- 结论：{verdict['summary']}",
        "",
        "## Fold 明细",
    ]
    for fold in report["folds"]:
        lines.extend(
            [
                (
                    f"- fold {fold['fold_id']}: chosen={fold['chosen_template']['name']}; "
                    f"train={fold['outer_train_dates'][0]}~{fold['outer_train_dates'][-1]}; "
                    f"inner_val={fold['inner_validation_dates'][0]}~{fold['inner_validation_dates'][-1]}; "
                    f"test={fold['outer_test_dates'][0]}~{fold['outer_test_dates'][-1]}"
                ),
                (
                    f"  - c2o test combo/base/excess = "
                    f"{fold['test_results']['close_to_open_combo']['metrics']['total_return']:.4%} / "
                    f"{fold['test_results']['close_to_open_base']['metrics']['total_return']:.4%} / "
                    f"{fold['test_results']['close_to_open']['excess_total_return']:.4%}"
                ),
                (
                    f"  - c2c test combo/base/excess = "
                    f"{fold['test_results']['close_to_close_combo']['metrics']['total_return']:.4%} / "
                    f"{fold['test_results']['close_to_close_base']['metrics']['total_return']:.4%} / "
                    f"{fold['test_results']['close_to_close']['excess_total_return']:.4%}"
                ),
                (
                    f"  - o2c test combo/base/excess = "
                    f"{fold['test_results']['open_to_close_combo']['metrics']['total_return']:.4%} / "
                    f"{fold['test_results']['open_to_close_base']['metrics']['total_return']:.4%} / "
                    f"{fold['test_results']['open_to_close']['excess_total_return']:.4%}"
                ),
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    evolution_dir = root / "outputs" / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)

    panel_path = root / "data" / "factors" / "panel_phase1.parquet"
    regime_path = root / "data" / "regimes" / "regime_labels_phase1.parquet"
    weight_path = root / "outputs" / "weight_maps" / LABEL_MODE_TO_WEIGHT_FILE[PRIMARY_LABEL_MODE]
    audit_path = root / "outputs" / "audit_reports" / LABEL_MODE_TO_AUDIT_FILE[PRIMARY_LABEL_MODE]
    replay_path = evolution_dir / LABEL_MODE_TO_REPLAY_FILE[PRIMARY_LABEL_MODE]
    backtest_path = evolution_dir / LABEL_MODE_TO_BACKTEST_FILE[PRIMARY_LABEL_MODE]

    output_json = evolution_dir / f"{args.output_stem}.json"
    output_md = evolution_dir / f"{args.output_stem}.md"
    output_csv = evolution_dir / f"{args.output_stem}_daily_returns.csv"

    weight_report = read_json(weight_path)
    replay_report = read_json(replay_path)
    audit_report = read_json(audit_path)
    replay_by_mode = {
        mode: read_json(evolution_dir / LABEL_MODE_TO_REPLAY_FILE[mode])
        for mode in ALL_LABEL_MODES
    }
    backtest_by_mode = {
        mode: read_json(evolution_dir / LABEL_MODE_TO_BACKTEST_FILE[mode])
        for mode in ALL_LABEL_MODES
    }

    effective_dates = list(replay_report["replay_results"][LABEL_MODE_TO_RESULT_KEY[PRIMARY_LABEL_MODE]]["effective_trade_dates"])
    factor_union = list(replay_report["current_combo_definition"]["regime_union_factor_ids"])
    regime_map = {str(row["regime_id"]): row for row in weight_report.get("regime_weight_map", [])}
    full_sample_signs = {
        str(row.get("factor_id")): 1.0 if float(row.get("rank_ic_mean", 0.0) or 0.0) >= 0 else -1.0
        for row in audit_report.get("reports", [])
    }

    panel_columns = [
        "trade_date",
        "ts_code",
        "base_factor_score_norm",
        "benchmark_eligible",
        *LABEL_MODE_TO_COLUMN.values(),
        *factor_union,
    ]
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
    folds = build_folds(effective_dates)

    aggregate_rows = {
        f"{mode}_combo": []
        for mode in ALL_LABEL_MODES
    }
    aggregate_rows.update({
        f"{mode}_base": []
        for mode in ALL_LABEL_MODES
    })
    daily_csv_rows: list[dict[str, Any]] = []
    fold_reports: list[dict[str, Any]] = []

    primary_return_col = LABEL_MODE_TO_COLUMN[PRIMARY_LABEL_MODE]

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
                primary_return_col,
            )
            validation_result = evaluate_dates(
                nested_df,
                fold["inner_validation_dates"],
                template,
                regime_map,
                factor_signs,
                primary_return_col,
                series_name=f"validation_{PRIMARY_LABEL_MODE}_combo",
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

        mode_results: dict[str, Any] = {}
        combo_rows_by_mode: dict[str, dict[str, Any]] = {}
        base_rows_by_mode: dict[str, dict[str, Any]] = {}
        for mode in ALL_LABEL_MODES:
            return_col = LABEL_MODE_TO_COLUMN[mode]
            combo = evaluate_dates(
                nested_df,
                fold["outer_test_dates"],
                chosen_template,
                regime_map,
                chosen_signs,
                return_col,
                series_name=f"{mode}_combo",
                benchmark_only=False,
            )
            base = evaluate_dates(
                nested_df,
                fold["outer_test_dates"],
                chosen_template,
                regime_map,
                chosen_signs,
                return_col,
                series_name=f"{mode}_base",
                benchmark_only=True,
            )
            aggregate_rows[f"{mode}_combo"].extend(combo["daily_records"])
            aggregate_rows[f"{mode}_base"].extend(base["daily_records"])
            mode_results[f"{mode}_combo"] = combo
            mode_results[f"{mode}_base"] = base
            mode_results[mode] = build_comparison(combo["metrics"], base["metrics"])
            combo_rows_by_mode[mode] = {row["date"]: row for row in combo["daily_records"]}
            base_rows_by_mode[mode] = {row["date"]: row for row in base["daily_records"]}

        for trade_date in fold["outer_test_dates"]:
            daily_csv_rows.append(
                {
                    "fold_id": fold["fold_id"],
                    "date": trade_date,
                    "template_name": chosen_template["name"],
                    "c2c_combo_net_return": combo_rows_by_mode["close_to_close"].get(trade_date, {}).get("net_return"),
                    "c2c_base_net_return": base_rows_by_mode["close_to_close"].get(trade_date, {}).get("net_return"),
                    "o2c_combo_net_return": combo_rows_by_mode["open_to_close"].get(trade_date, {}).get("net_return"),
                    "o2c_base_net_return": base_rows_by_mode["open_to_close"].get(trade_date, {}).get("net_return"),
                    "c2o_combo_net_return": combo_rows_by_mode["close_to_open"].get(trade_date, {}).get("net_return"),
                    "c2o_base_net_return": base_rows_by_mode["close_to_open"].get(trade_date, {}).get("net_return"),
                    "c2c_combo_turnover": combo_rows_by_mode["close_to_close"].get(trade_date, {}).get("turnover"),
                    "o2c_combo_turnover": combo_rows_by_mode["open_to_close"].get(trade_date, {}).get("turnover"),
                    "c2o_combo_turnover": combo_rows_by_mode["close_to_open"].get(trade_date, {}).get("turnover"),
                }
            )

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
                "test_results": mode_results,
            }
        )

    aggregate_oos: dict[str, Any] = {}
    for mode in ALL_LABEL_MODES:
        combo_metrics = build_metrics_from_rows(aggregate_rows[f"{mode}_combo"])
        base_metrics = build_metrics_from_rows(aggregate_rows[f"{mode}_base"])
        aggregate_oos[f"{mode}_combo"] = combo_metrics
        aggregate_oos[f"{mode}_base"] = base_metrics
        aggregate_oos[mode] = build_comparison(combo_metrics, base_metrics)

    aggregate_oos["direction_consistency"] = {
        "close_to_close_combo_direction": direction_of(aggregate_oos["close_to_close_combo"]["total_return"]),
        "open_to_close_combo_direction": direction_of(aggregate_oos["open_to_close_combo"]["total_return"]),
        "close_to_open_combo_direction": direction_of(aggregate_oos["close_to_open_combo"]["total_return"]),
        "close_to_close_excess_direction": direction_of(aggregate_oos["close_to_close"]["excess_total_return"]),
        "open_to_close_excess_direction": direction_of(aggregate_oos["open_to_close"]["excess_total_return"]),
        "close_to_open_excess_direction": direction_of(aggregate_oos["close_to_open"]["excess_total_return"]),
        "combo_absolute_direction_consistent_c2o_vs_c2c": same_nonflat_direction(
            aggregate_oos["close_to_open_combo"]["total_return"],
            aggregate_oos["close_to_close_combo"]["total_return"],
        ),
        "combo_absolute_direction_consistent_c2o_vs_o2c": same_nonflat_direction(
            aggregate_oos["close_to_open_combo"]["total_return"],
            aggregate_oos["open_to_close_combo"]["total_return"],
        ),
        "combo_excess_direction_consistent_c2o_vs_c2c": same_nonflat_direction(
            aggregate_oos["close_to_open"]["excess_total_return"],
            aggregate_oos["close_to_close"]["excess_total_return"],
        ),
        "combo_excess_direction_consistent_c2o_vs_o2c": same_nonflat_direction(
            aggregate_oos["close_to_open"]["excess_total_return"],
            aggregate_oos["open_to_close"]["excess_total_return"],
        ),
    }

    template_counts: dict[str, int] = {}
    chosen_template_names: list[str] = []
    for fold in fold_reports:
        name = fold["chosen_template"]["name"]
        chosen_template_names.append(name)
        template_counts[name] = template_counts.get(name, 0) + 1
    template_switch_count = 0
    for prev, curr in zip(chosen_template_names, chosen_template_names[1:]):
        if prev != curr:
            template_switch_count += 1

    existing_total_return = {
        mode: load_existing_total_return(backtest_by_mode[mode], mode, replay_by_mode[mode])
        for mode in ALL_LABEL_MODES
    }

    primary_agg_combo = aggregate_oos[f"{PRIMARY_LABEL_MODE}_combo"]
    sample_coverage = {
        "effective_clean_days": int(len(effective_dates)),
        "outer_oos_days": int(primary_agg_combo["days"]),
        "outer_oos_coverage_ratio": float(primary_agg_combo["days"] / len(effective_dates)) if effective_dates else 0.0,
        "fold_count": int(len(fold_reports)),
    }
    last_fold_test_days = len(fold_reports[-1].get("outer_test_dates", [])) if fold_reports else 0

    report = {
        "report_type": "current_combo_nested_validation",
        "schema_version": "2026-04-20.current_combo_nested_validation.c2o_label_specific.v1",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(timespec="seconds"),
        "primary_label_mode": PRIMARY_LABEL_MODE,
        "applies_to_label_modes": [PRIMARY_LABEL_MODE],
        "cross_check_label_modes": [mode for mode in ALL_LABEL_MODES if mode != PRIMARY_LABEL_MODE],
        "validation_scope": "micro_nested_validation_on_clean_window_existing_templates_only_close_to_open_primary_with_multi_mode_cross_checks",
        "source_paths": {
            "panel": str(panel_path),
            "regime": str(regime_path),
            "weight_map_primary": str(weight_path),
            "factor_audit_primary": str(audit_path),
            "current_combo_replay_report_close_to_open": str(replay_path),
            "current_combo_portfolio_backtest_close_to_open": str(backtest_path),
            "current_combo_replay_report": str(evolution_dir / LABEL_MODE_TO_REPLAY_FILE["close_to_close"]),
            "current_combo_portfolio_backtest": str(evolution_dir / LABEL_MODE_TO_BACKTEST_FILE["close_to_close"]),
            "current_combo_replay_report_open_to_close": str(evolution_dir / LABEL_MODE_TO_REPLAY_FILE["open_to_close"]),
            "current_combo_portfolio_backtest_open_to_close": str(evolution_dir / LABEL_MODE_TO_BACKTEST_FILE["open_to_close"]),
        },
        "source_mtimes": {
            "panel": pd.Timestamp.fromtimestamp(panel_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if panel_path.exists() else None,
            "regime": pd.Timestamp.fromtimestamp(regime_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if regime_path.exists() else None,
            "weight_map_primary": pd.Timestamp.fromtimestamp(weight_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if weight_path.exists() else None,
            "factor_audit_primary": pd.Timestamp.fromtimestamp(audit_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if audit_path.exists() else None,
            "current_combo_replay_report_close_to_open": pd.Timestamp.fromtimestamp(replay_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if replay_path.exists() else None,
            "current_combo_portfolio_backtest_close_to_open": pd.Timestamp.fromtimestamp(backtest_path.stat().st_mtime, tz="Asia/Shanghai").isoformat() if backtest_path.exists() else None,
            "current_combo_replay_report": pd.Timestamp.fromtimestamp((evolution_dir / LABEL_MODE_TO_REPLAY_FILE['close_to_close']).stat().st_mtime, tz="Asia/Shanghai").isoformat() if (evolution_dir / LABEL_MODE_TO_REPLAY_FILE['close_to_close']).exists() else None,
            "current_combo_portfolio_backtest": pd.Timestamp.fromtimestamp((evolution_dir / LABEL_MODE_TO_BACKTEST_FILE['close_to_close']).stat().st_mtime, tz="Asia/Shanghai").isoformat() if (evolution_dir / LABEL_MODE_TO_BACKTEST_FILE['close_to_close']).exists() else None,
            "current_combo_replay_report_open_to_close": pd.Timestamp.fromtimestamp((evolution_dir / LABEL_MODE_TO_REPLAY_FILE['open_to_close']).stat().st_mtime, tz="Asia/Shanghai").isoformat() if (evolution_dir / LABEL_MODE_TO_REPLAY_FILE['open_to_close']).exists() else None,
            "current_combo_portfolio_backtest_open_to_close": pd.Timestamp.fromtimestamp((evolution_dir / LABEL_MODE_TO_BACKTEST_FILE['open_to_close']).stat().st_mtime, tz="Asia/Shanghai").isoformat() if (evolution_dir / LABEL_MODE_TO_BACKTEST_FILE['open_to_close']).exists() else None,
        },
        "methodology": {
            "purpose": "在不重跑整条 audit/weight 链的前提下，为 close_to_open 口径补一份 label-specific 的 micro nested validation 证据。",
            "candidate_templates": templates,
            "factor_sign_estimation": "每个 outer fold 仅用 inner_train 的 close-to-open 日度 rank IC 均值推导方向；不复用 full-sample audit sign 做 nested 选择。",
            "selection_target": "先在 inner_validation 上按 close-to-open total_return/sharpe/win_rate 选模板，再把同一模板与同一组 sign 扔到 outer_test 同时评估 c2o / c2c / o2c。",
            "independence_scope": "只对 clean window 内的模板选择和方向选择形成部分独立性；上游 factor discovery 与 canonical weight_map 仍来自既有全样本产物，因此这不是完整独立研究闭环。",
            "base_benchmark_note": "base 仍沿用 benchmark_eligible 行构成的 prebreakout_v41 当日持仓；部分日期 benchmark 行数小于 20，这会直接反映在 base OOS 结果里。",
        },
        "fold_config": {
            "min_outer_train_days": 12,
            "inner_validation_days": 4,
            "outer_test_days": 4,
            "min_outer_test_days": 2,
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
            "existing_replay": {
                "close_to_close_combo_total_return": float(existing_total_return["close_to_close"]),
                "open_to_close_combo_total_return": float(existing_total_return["open_to_close"]),
                "close_to_open_combo_total_return": float(existing_total_return["close_to_open"]),
            },
            "nested_oos": {
                "close_to_close_combo_total_return": float(aggregate_oos["close_to_close_combo"]["total_return"]),
                "open_to_close_combo_total_return": float(aggregate_oos["open_to_close_combo"]["total_return"]),
                "close_to_open_combo_total_return": float(aggregate_oos["close_to_open_combo"]["total_return"]),
            },
            "delta": {
                "close_to_close_combo_total_return": float(aggregate_oos["close_to_close_combo"]["total_return"] - existing_total_return["close_to_close"]),
                "open_to_close_combo_total_return": float(aggregate_oos["open_to_close_combo"]["total_return"] - existing_total_return["open_to_close"]),
                "close_to_open_combo_total_return": float(aggregate_oos["close_to_open_combo"]["total_return"] - existing_total_return["close_to_open"]),
            },
        },
        "daily_returns_csv": str(output_csv),
        "verdict": {
            "minimal_independent_oos_evidence_present": True,
            "sufficient_to_lift_promotion_freeze": False,
            "summary": (
                f"现有 {len(effective_dates)} 个 clean effective days 足以落一份 close_to_open label-specific micro nested validation：共形成 {len(fold_reports)} 个 outer folds、覆盖 {primary_agg_combo['days']} 个 OOS 日。"
                f"但聚合后 c2o combo 仅 {aggregate_oos['close_to_open_combo']['total_return']:.4%}，虽然相对 base 仍保留 {aggregate_oos['close_to_open']['excess_total_return']:.4%} 的正超额，"
                f"同时 cross-check 显示 c2c / o2c 分别为 {aggregate_oos['close_to_close_combo']['total_return']:.4%} / {aggregate_oos['open_to_close_combo']['total_return']:.4%}，"
                "说明口径间仍存在结构迁移。该证据能证明 close_to_open 的 label-specific nested 骨架可落地，但远不足以解除 promotion freeze，也不能替代独立 holdout 或收益级 analog validation。"
            ),
            "claim_limitations": [
                "这是 close_to_open label-specific 的微型 nested validation，不是完整独立 research pipeline。",
                "上游 factor discovery / canonical weight_map 仍复用了既有全样本产物。",
                f"outer OOS 共 {primary_agg_combo['days']} 天，且最后一个 fold 只有 {last_fold_test_days} 天 test，统计强度仍然较弱。",
                "这份 c2o 证据尚未接入共享控制面；open_to_close 仍缺独立 label-specific nested。",
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
                "primary_label_mode": PRIMARY_LABEL_MODE,
                "report_json": str(output_json),
                "report_md": str(output_md),
                "report_csv": str(output_csv),
                "fold_count": len(fold_reports),
                "outer_oos_days": primary_agg_combo["days"],
                "close_to_open_combo_total_return": aggregate_oos["close_to_open_combo"]["total_return"],
                "close_to_open_excess_total_return": aggregate_oos["close_to_open"]["excess_total_return"],
                "close_to_close_combo_total_return": aggregate_oos["close_to_close_combo"]["total_return"],
                "open_to_close_combo_total_return": aggregate_oos["open_to_close_combo"]["total_return"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
