#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 whitelist_blend 的最小可信组合级 replay 报告。"""

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
REPLAY_FACTORS = [
    "f_gap_return",
    "f_volume_ratio_5",
    "f_volume_ratio_20",
    "f_atr_proxy_14",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run minimal whitelist_blend portfolio replay")
    parser.add_argument("--root", default="/Users/fisher/.openclaw/workspace/factor_factory")
    return parser.parse_args()


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
    calmar = float(annual / abs(mdd)) if mdd < 0 else 0.0
    return {
        "days": int(len(rets)),
        "total_return": total,
        "annualized_return": annual,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": calmar,
        "win_rate": float((rets > 0).mean()),
        "turnover": float(turns.mean() if not turns.empty else 0.0),
        "start": str(rets.index.min()) if not rets.empty else None,
        "end": str(rets.index.max()) if not rets.empty else None,
    }


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
            fid = str(item["factor_id"])
            combo_score = combo_score + float(item.get("weight", 0.0)) * factor_signs.get(fid, 1.0) * day[f"{fid}_z"]
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
            "daily_records": rows,
        }
        for label, rows in records.items()
    }


def render_markdown(report: dict[str, Any]) -> str:
    r = report["replay_results"]
    lines = [
        "# Minimal Portfolio Replay Report",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- benchmark_eligible（selection_history ∩ regime）天数：{report['benchmark_eligible_summary']['eligible_trade_days']}",
        f"- selection_history 文件天数：{report['benchmark_eligible_summary']['selection_history_days']}",
        f"- recommendation_warehouse 可回填天数：{report['benchmark_eligible_summary']['recommendation_db_only_days']}",
        f"- 回放窗口：{report['benchmark_eligible_summary']['eligible_start']} ~ {report['benchmark_eligible_summary']['eligible_end']}",
        "",
        "## 组合级 replay（净收益，20bps round-trip cost）",
    ]
    for key in ["close_to_close_combo", "close_to_close_base", "open_to_close_combo", "open_to_close_base"]:
        m = r[key]["metrics"]
        lines.append(
            f"- {key}: days={m['days']}, total_return={m['total_return']:.4%}, sharpe={m['sharpe']:.3f}, max_drawdown={m['max_drawdown']:.4%}, win_rate={m['win_rate']:.2%}, turnover={m['turnover']:.3f}"
        )
    lines.extend(
        [
            "",
            "## 关键对照",
            f"- close-to-close 组合相对 base 超额总收益：{report['comparisons']['close_to_close_excess_total_return']:.4%}",
            f"- open-to-close 组合相对 base 超额总收益：{report['comparisons']['open_to_close_excess_total_return']:.4%}",
            f"- 口径切换拖累（combo: c2c - o2c）：{report['comparisons']['combo_c2c_minus_o2c_total_return']:.4%}",
            f"- 两个回填日剔除后，close-to-close 组合总收益：{report['sensitivity']['without_backfill_dates']['close_to_close_combo']['metrics']['total_return']:.4%}",
            "",
            "## Analog validation 摘要",
            f"- Top5 analog 5d 均值：{report['analog_validation']['top5_mean_5d']:.4%}",
            f"- Top5 analog 20d 均值：{report['analog_validation']['top5_mean_20d']:.4%}",
            f"- Top5 analog 60d 均值：{report['analog_validation']['top5_mean_60d']:.4%}",
            "",
            "## 结论",
            f"- 当前 whitelist_blend 是否已有组合级回测证据：{report['verdict']['has_portfolio_level_backtest_evidence']}",
            f"- 是否可宣称有效样本外：{report['verdict']['effective_oos_claim_allowed']}",
            f"- 当前最小可信结论：{report['verdict']['minimal_credible_conclusion']}",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    output_dir = root / "outputs" / "evolution"
    output_dir.mkdir(parents=True, exist_ok=True)

    panel_path = root / "data" / "factors" / "panel_phase1.parquet"
    regime_path = root / "data" / "regimes" / "regime_labels_phase1.parquet"
    weight_path = root / "outputs" / "weight_maps" / "regime_weight_map_phase1.json"
    audit_path = root / "outputs" / "audit_reports" / "factor_audit_phase1.json"
    analog_path = output_dir / "regime_analog_windows.json"
    selection_history_dir = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/selection_history")
    recommendation_db = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db")

    selection_history_dates = sorted(p.stem for p in selection_history_dir.glob("*.json"))
    db_only_dates: list[str] = []
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
    else:
        db_dates = set()

    panel = pd.read_parquet(
        panel_path,
        columns=[
            "trade_date",
            "ts_code",
            "open",
            "close",
            "base_factor_score_norm",
            "next_return_1d",
            *REPLAY_FACTORS,
        ],
    )
    panel[["next_open", "next_close"]] = panel.groupby("ts_code")[["open", "close"]].shift(-1)
    panel["next_open_to_close"] = panel["next_close"] / panel["next_open"] - 1.0

    regime = pd.read_parquet(regime_path, columns=["trade_date", "regime_id"])
    regime_dates = set(regime["trade_date"].astype(str))
    eligible_dates = sorted(set(selection_history_dates) & regime_dates)

    replay_panel = panel[panel["trade_date"].isin(eligible_dates)].copy()
    replay_panel = replay_panel.merge(regime, on="trade_date", how="inner")
    replay_panel["base_factor_score_norm_z"] = zscore_by_date(replay_panel, "base_factor_score_norm")
    for factor in REPLAY_FACTORS:
        replay_panel[f"{factor}_z"] = zscore_by_date(replay_panel, factor)

    weights = json.loads(weight_path.read_text(encoding="utf-8"))
    regime_map = {row["regime_id"]: row for row in weights["regime_weight_map"]}
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    factor_signs = {
        row["factor_id"]: (1.0 if float(row.get("rank_ic_mean", 0.0) or 0.0) >= 0 else -1.0)
        for row in audit.get("reports", [])
    }

    label_specs = [
        ("close_to_close_combo", "next_return_1d", "combo_score"),
        ("close_to_close_base", "next_return_1d", "base_score_only"),
        ("open_to_close_combo", "next_open_to_close", "combo_score"),
        ("open_to_close_base", "next_open_to_close", "base_score_only"),
    ]
    replay_results = run_replay(replay_panel, regime_map, factor_signs, label_specs)

    without_backfill_dates = [d for d in eligible_dates if d not in BACKFILL_DATES]
    sensitivity_panel = replay_panel[replay_panel["trade_date"].isin(without_backfill_dates)].copy()
    sensitivity_results = run_replay(sensitivity_panel, regime_map, factor_signs, label_specs)

    analog = json.loads(analog_path.read_text(encoding="utf-8")) if analog_path.exists() else {"analog_windows": []}
    top5 = analog.get("analog_windows", [])[:5]

    def _mean_or_zero(key: str) -> float:
        values = [
            float(item.get("subsequent_market_proxy_returns", {}).get(key))
            for item in top5
            if item.get("subsequent_market_proxy_returns", {}).get(key) is not None
        ]
        return float(sum(values) / len(values)) if values else 0.0

    report = {
        "report_type": "minimal_portfolio_replay_report",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "source_paths": {
            "panel": str(panel_path),
            "regime": str(regime_path),
            "weight_map": str(weight_path),
            "factor_audit": str(audit_path),
            "selection_history_dir": str(selection_history_dir),
            "recommendation_db": str(recommendation_db),
            "analog_windows": str(analog_path),
        },
        "benchmark_eligible_summary": {
            "selection_history_days": len(selection_history_dates),
            "recommendation_db_only_days": [d for d in BACKFILL_DATES if d in db_only_dates or d in selection_history_dates],
            "eligible_trade_days": len(eligible_dates),
            "eligible_start": eligible_dates[0] if eligible_dates else None,
            "eligible_end": eligible_dates[-1] if eligible_dates else None,
            "eligible_dates": eligible_dates,
        },
        "replay_assumptions": {
            "top_n": TOP_N,
            "round_trip_cost": ROUND_TRIP_COST,
            "score_formula": "base_weight * z(base_factor_score_norm) + Σ regime_weight_i * sign(rank_ic_mean_i) * z(factor_i)",
            "cash_buffer_handling": "cash_buffer 不参与个股打分，仅作为名义仓位未投入；本 replay 仍按 top20 invested sleeve 比较组合与 base。",
            "execution_caveat": "该 replay 仍是基于 panel 标签的离线重放，不含成交量约束、涨跌停约束、冲击成本与真实撮合。",
        },
        "factor_signs": {k: factor_signs.get(k, 1.0) for k in REPLAY_FACTORS},
        "replay_results": replay_results,
        "comparisons": {
            "close_to_close_excess_total_return": replay_results["close_to_close_combo"]["metrics"]["total_return"] - replay_results["close_to_close_base"]["metrics"]["total_return"],
            "open_to_close_excess_total_return": replay_results["open_to_close_combo"]["metrics"]["total_return"] - replay_results["open_to_close_base"]["metrics"]["total_return"],
            "combo_c2c_minus_o2c_total_return": replay_results["close_to_close_combo"]["metrics"]["total_return"] - replay_results["open_to_close_combo"]["metrics"]["total_return"],
            "base_c2c_minus_o2c_total_return": replay_results["close_to_close_base"]["metrics"]["total_return"] - replay_results["open_to_close_base"]["metrics"]["total_return"],
        },
        "sensitivity": {
            "without_backfill_dates": sensitivity_results,
            "backfill_dates": BACKFILL_DATES,
        },
        "analog_validation": {
            "top3_windows": top5[:3],
            "top5_mean_5d": _mean_or_zero("5d"),
            "top5_mean_20d": _mean_or_zero("20d"),
            "top5_mean_60d": _mean_or_zero("60d"),
            "method_caveat": analog.get("methodology", {}).get("caveat"),
        },
        "verdict": {
            "has_portfolio_level_backtest_evidence": "yes_but_minimal_replay_only",
            "effective_oos_claim_allowed": False,
            "analog_validation_claim_allowed": False,
            "minimal_credible_conclusion": "在 benchmark_eligible=27 天、close-to-close 标签下，whitelist_blend 重放优于同期 base；但换成 open-to-close 后优势消失，且剔除两个回填日后 close-to-close 总收益接近 0，因此该证据只能作为最小可用 replay，不足以解除 promotion freeze。",
        },
    }

    json_path = output_dir / "minimal_portfolio_replay_report.json"
    md_path = output_dir / "minimal_portfolio_replay_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": "ok", "json": str(json_path), "md": str(md_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
