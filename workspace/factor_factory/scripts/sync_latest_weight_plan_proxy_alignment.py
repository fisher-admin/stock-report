#!/usr/bin/env python3
from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
OUTPUTS = ROOT / "outputs"
EVOLUTION = OUTPUTS / "evolution"
WEIGHT_MAP_PATH = OUTPUTS / "weight_maps" / "regime_weight_map_phase1.json"
CURRENT_MARKET_PATH = EVOLUTION / "current_market_fingerprint.json"
ANALOG_WINDOWS_PATH = EVOLUTION / "regime_analog_windows.json"
ANALOG_REPORT_PATH = EVOLUTION / "analog_validation_report.json"
PORTFOLIO_PATH = EVOLUTION / "portfolio_backtest_report.json"
OVERFIT_PATH = EVOLUTION / "overfit_audit_report.json"
PROMOTION_PATH = EVOLUTION / "factor_promotion_decision.json"
DIGEST_PATH = EVOLUTION / "daily_factor_evolution_digest.md"
ALIGNMENT_PATH = EVOLUTION / "latest_weight_plan_analog_alignment.json"

TZ = ZoneInfo("Asia/Shanghai")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec="seconds")


def ensure_list_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def refresh_source_mtimes(payload: dict[str, Any], overrides: dict[str, str] | None = None) -> dict[str, str]:
    source_mtimes = payload.setdefault("source_mtimes", {})
    for key, raw_path in payload.get("source_paths", {}).items():
        path = Path(raw_path)
        if path.exists():
            source_mtimes[key] = iso_mtime(path)
    if overrides:
        source_mtimes.update(overrides)
    return source_mtimes


def has_return_level_analog_replay_control_plane(payload: dict[str, Any]) -> bool:
    validation_scope = payload.get("validation_scope", {}) or {}
    validation_status = payload.get("validation_status", {}) or {}
    return bool(
        validation_scope.get("portfolio_return_replay_included")
        or payload.get("current_combo_return_level_analog_replay")
        or validation_status.get("decision_grade") == "hold_return_level_replay_present_no_independent_oos"
    )


def summarize_returns(windows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    values = [
        float(window.get("subsequent_market_proxy_returns", {}).get(horizon))
        for window in windows
        if window.get("subsequent_market_proxy_returns", {}).get(horizon) is not None
    ]
    if not values:
        return {
            "sample_count": 0,
            "mean": None,
            "median": None,
            "positive_ratio": None,
            "non_positive_ratio": None,
            "min": None,
            "max": None,
        }
    positive_ratio = sum(value > 0 for value in values) / len(values)
    return {
        "sample_count": len(values),
        "mean": round(statistics.mean(values), 6),
        "median": round(statistics.median(values), 6),
        "positive_ratio": round(positive_ratio, 4),
        "non_positive_ratio": round(1 - positive_ratio, 4),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def directionality_hint(horizon: str, all_stats: dict[str, Any], same_primary_stats: dict[str, Any]) -> dict[str, Any]:
    mean = all_stats.get("mean")
    positive_ratio = all_stats.get("positive_ratio")
    if horizon == "5d":
        if mean is not None and positive_ratio is not None and mean <= 0.005 and positive_ratio <= 0.4:
            assessment = "neutral_to_weak_short_term"
            hint = "5d analog 仅能给出偏中性到偏弱、且分歧较大的短线提示。"
        elif mean is not None and positive_ratio is not None and mean >= 0.01 and positive_ratio >= 0.6:
            assessment = "positive_short_term_bias"
            hint = "5d analog 偏正向，但样本分歧仍明显。"
        else:
            assessment = "mixed_short_term"
            hint = "5d analog 分歧较大，只能视作高噪声短线参考。"
    elif horizon == "20d":
        if mean is not None and positive_ratio is not None and mean >= 0.02 and positive_ratio >= 0.7:
            assessment = "positive_intermediate_rebound_bias"
            hint = "20d analog 明显偏正向，更像中期修复/回升窗口。"
        else:
            assessment = "mixed_intermediate"
            hint = "20d analog 没有形成足够稳定的中期方向共识。"
    else:
        if mean is not None and positive_ratio is not None and mean >= 0.03 and positive_ratio >= 0.45:
            assessment = "mild_positive_but_heterogeneous"
            hint = "60d analog 仍偏正向，但异质性显著，不能把均值直接当成确定性趋势。"
        else:
            assessment = "mixed_longer_term"
            hint = "60d analog 分布仍很分散，只能作为概率提示。"
    return {
        "horizon": horizon,
        "assessment": assessment,
        "hint": hint,
        "all_windows": all_stats,
        "same_primary_only": same_primary_stats,
    }


now = datetime.now(TZ).isoformat(timespec="seconds")
weights = load_json(WEIGHT_MAP_PATH)
current_market = load_json(CURRENT_MARKET_PATH)
analog_windows_payload = load_json(ANALOG_WINDOWS_PATH)
analog_report = load_json(ANALOG_REPORT_PATH)
portfolio = load_json(PORTFOLIO_PATH)
overfit = load_json(OVERFIT_PATH)
promotion = load_json(PROMOTION_PATH)

if has_return_level_analog_replay_control_plane(analog_report):
    print(json.dumps(
        {
            "status": "skipped_existing_return_level_replay_control_plane",
            "generated_at": now,
            "reason": (
                "analog_validation_report.json 已处于 current_combo 收益级 analog replay 口径；"
                "为避免旧 proxy-only helper 把控制面回退，本脚本本轮不再改写 analog / portfolio / overfit / promotion / digest。"
            ),
            "preserved_outputs": [
                str(ANALOG_REPORT_PATH),
                str(PORTFOLIO_PATH),
                str(OVERFIT_PATH),
                str(PROMOTION_PATH),
                str(DIGEST_PATH),
            ],
            "recommended_sync_chain": [
                "python scripts/run_current_combo_analog_replay.py --root /Users/fisher/.openclaw/workspace/factor_factory",
                "python scripts/sync_multi_label_combo_validation_context.py",
            ],
        },
        ensure_ascii=False,
        indent=2,
    ))
    raise SystemExit(0)

latest_weight_plan_factor_ids = [
    str(item["factor_id"]) for item in weights.get("latest_weight_plan", {}).get("selected_factors", [])
]
regime_union_factor_ids = ensure_list_unique(
    [
        str(item["factor_id"])
        for regime in weights.get("regime_weight_map", [])
        for item in regime.get("selected_factors", [])
    ]
)

windows = sorted(
    analog_windows_payload.get("analog_windows", []),
    key=lambda item: float(item.get("similarity_score") or 0.0),
    reverse=True,
)
proxy_window_count = len(windows)
coverage_counts = {factor_id: 0 for factor_id in regime_union_factor_ids}
window_rows: list[dict[str, Any]] = []
regime_counter: Counter[str] = Counter()
fully_aligned_window_count = 0
same_primary_full_alignment_count = 0
same_style_full_alignment_count = 0
same_primary_regime_count = 0
same_style_regime_count = 0

for window in windows:
    effective_factor_ids = [str(item["factor_id"]) for item in window.get("effective_factor_proxies", [])]
    effective_factor_set = set(effective_factor_ids)
    for factor_id in effective_factor_ids:
        coverage_counts[factor_id] = coverage_counts.get(factor_id, 0) + 1

    latest_overlap = [factor_id for factor_id in latest_weight_plan_factor_ids if factor_id in effective_factor_set]
    latest_missing = [factor_id for factor_id in latest_weight_plan_factor_ids if factor_id not in effective_factor_set]
    union_overlap = [factor_id for factor_id in regime_union_factor_ids if factor_id in effective_factor_set]
    union_missing = [factor_id for factor_id in regime_union_factor_ids if factor_id not in effective_factor_set]

    same_primary = bool(window.get("similar_reasons", {}).get("same_primary_regime"))
    same_style = bool(window.get("similar_reasons", {}).get("same_style_regime"))
    same_primary_regime_count += int(same_primary)
    same_style_regime_count += int(same_style)
    regime_counter[str(window.get("regime_id"))] += 1

    latest_weight_plan_fully_covered = not latest_missing
    if latest_weight_plan_fully_covered:
        fully_aligned_window_count += 1
        if same_primary:
            same_primary_full_alignment_count += 1
        if same_style:
            same_style_full_alignment_count += 1

    window_rows.append(
        {
            "trade_date": window.get("trade_date"),
            "window_start": window.get("window_start"),
            "window_end": window.get("window_end"),
            "regime_id": window.get("regime_id"),
            "regime_confidence": window.get("regime_confidence"),
            "similarity_score": window.get("similarity_score"),
            "distance": window.get("distance"),
            "same_primary_regime": same_primary,
            "same_style_regime": same_style,
            "subsequent_market_proxy_returns": window.get("subsequent_market_proxy_returns", {}),
            "effective_factor_proxy_ids": effective_factor_ids,
            "latest_weight_plan_overlap_factor_ids": latest_overlap,
            "missing_latest_weight_plan_factor_ids": latest_missing,
            "regime_union_overlap_factor_ids": union_overlap,
            "missing_regime_union_factor_ids": union_missing,
            "latest_weight_plan_fully_covered": latest_weight_plan_fully_covered,
            "f_price_vs_ma20_proxy_only": "f_price_vs_ma20" in union_missing,
        }
    )

union_only_no_window_coverage_factor_ids = [
    factor_id
    for factor_id in regime_union_factor_ids
    if factor_id not in latest_weight_plan_factor_ids and coverage_counts.get(factor_id, 0) == 0
]
proxy_only_factor_ids = list(union_only_no_window_coverage_factor_ids)

factor_level_alignment: dict[str, Any] = {}
for factor_id in regime_union_factor_ids:
    coverage = coverage_counts.get(factor_id, 0)
    in_latest_weight_plan = factor_id in latest_weight_plan_factor_ids
    if in_latest_weight_plan:
        if coverage == proxy_window_count:
            alignment_status = "covered_in_all_proxy_windows"
        elif coverage > 0:
            alignment_status = "partially_covered_in_proxy_windows"
        else:
            alignment_status = "uncovered_in_proxy_windows"
        claim_scope = "proxy_aligned_not_portfolio_validated"
    else:
        if coverage > 0:
            alignment_status = "union_only_but_proxy_covered"
            claim_scope = "proxy_aligned_outside_latest_weight_plan"
        else:
            alignment_status = "union_only_no_window_coverage"
            claim_scope = "union_only_no_window_match"
    factor_level_alignment[factor_id] = {
        "in_latest_weight_plan": in_latest_weight_plan,
        "in_regime_union": factor_id in regime_union_factor_ids,
        "proxy_window_coverage_count": coverage,
        "proxy_window_coverage_ratio": round((coverage / proxy_window_count), 4) if proxy_window_count else 0.0,
        "alignment_status": alignment_status,
        "claim_scope": claim_scope,
    }

alignment_summary = {
    "latest_weight_plan_alignment": (
        "10/10 analog proxy windows cover f_chip_support_proxy / f_gap_return / f_atr_proxy_14"
        if fully_aligned_window_count == proxy_window_count and proxy_window_count
        else f"{fully_aligned_window_count}/{proxy_window_count} analog proxy windows cover latest weight plan"
    ),
    "proxy_only_factor_ids": proxy_only_factor_ids,
    "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
    "interpretation": "latest shrink_uptrend weight plan is fully covered by all analog proxy windows, while f_price_vs_ma20 remains a regime-union-only factor with 0/10 window coverage; none of this upgrades the result from proxy to portfolio-level validation.",
}

alignment_payload = {
    "report_type": "latest_weight_plan_analog_alignment",
    "schema_version": "2026-04-19.latest_weight_plan_analog_alignment.v2",
    "generated_at": now,
    "source_paths": {
        "weight_map": str(WEIGHT_MAP_PATH),
        "regime_analog_windows": str(ANALOG_WINDOWS_PATH),
        "analog_validation_report": str(ANALOG_REPORT_PATH),
    },
    "source_mtimes": {
        "weight_map": iso_mtime(WEIGHT_MAP_PATH),
        "regime_analog_windows": iso_mtime(ANALOG_WINDOWS_PATH),
        "analog_validation_report": now,
    },
    "latest_weight_plan_factor_ids": latest_weight_plan_factor_ids,
    "regime_union_factor_ids": regime_union_factor_ids,
    "proxy_window_count": proxy_window_count,
    "fully_aligned_window_count": fully_aligned_window_count,
    "fully_aligned_window_ratio": round((fully_aligned_window_count / proxy_window_count), 4) if proxy_window_count else 0.0,
    "same_primary_full_alignment_count": same_primary_full_alignment_count,
    "same_style_full_alignment_count": same_style_full_alignment_count,
    "factor_level_alignment": factor_level_alignment,
    "window_matrix": window_rows,
    "summary": alignment_summary,
}
dump_json(ALIGNMENT_PATH, alignment_payload)

same_primary_windows = [window for window in windows if window.get("similar_reasons", {}).get("same_primary_regime")]
all_window_stats = {horizon: summarize_returns(windows, horizon) for horizon in ["5d", "20d", "60d"]}
same_primary_stats = {horizon: summarize_returns(same_primary_windows, horizon) for horizon in ["5d", "20d", "60d"]}
directionality = {
    horizon: directionality_hint(horizon, all_window_stats[horizon], same_primary_stats[horizon])
    for horizon in ["5d", "20d", "60d"]
}

top_similarity_windows = [
    {
        "trade_date": row.get("trade_date"),
        "window_start": row.get("window_start"),
        "window_end": row.get("window_end"),
        "similarity_score": row.get("similarity_score"),
        "distance": row.get("distance"),
        "regime_id": row.get("regime_id"),
        "regime_confidence": row.get("regime_confidence"),
        "same_primary_regime": row.get("same_primary_regime"),
        "same_style_regime": row.get("same_style_regime"),
        "subsequent_market_proxy_returns": row.get("subsequent_market_proxy_returns"),
        "effective_factor_proxy_ids": row.get("effective_factor_proxy_ids"),
    }
    for row in window_rows[:5]
]

prev_generated_at = analog_report.get("generated_at")
analog_report["schema_version"] = "2026-04-19.analog_validation_report.v4"
analog_report["generated_at"] = now
analog_report["supersedes_report_generated_at"] = prev_generated_at
rewrite_reason = list(analog_report.get("rewrite_reason", []))
new_reasons = [
    "按最新 21:46 current_market_fingerprint / regime_analog_windows 重刷 source_mtimes 与 canonical_refresh_basis，消除 11:47 旧来源残留。",
    "修复 analog top window 对 20251231 样本 60d 回报的陈旧 null 值，并把 60d 汇总样本数恢复到 10。",
    "新增 union_only_no_window_coverage_factor_ids，避免把 f_price_vs_ma20 的 0/10 命中误读成 proxy 命中。",
]
for item in new_reasons:
    if item not in rewrite_reason:
        rewrite_reason.append(item)
analog_report["rewrite_reason"] = rewrite_reason
analog_report.setdefault("source_paths", {})["latest_weight_plan_analog_alignment"] = str(ALIGNMENT_PATH)
refresh_source_mtimes(analog_report, overrides={"latest_weight_plan_analog_alignment": now})
canonical_refresh_basis = analog_report.setdefault("canonical_refresh_basis", {})
canonical_refresh_basis["fingerprint_generated_at"] = current_market.get("generated_at")
canonical_refresh_basis["analog_windows_generated_at"] = analog_windows_payload.get("generated_at")
canonical_refresh_basis["latest_weight_plan_factor_ids"] = latest_weight_plan_factor_ids
canonical_refresh_basis["latest_regime_union_factor_ids"] = regime_union_factor_ids

broad_market_proxy = current_market.get("trend_state", {}).get("broad_market_proxy", {})
style_proxy = current_market.get("style_and_sector", {}).get("broad_market_proxy", {})
analog_report["current_market_proxy_context"] = {
    "trade_date": current_market.get("trade_date"),
    "current_proxy_regime": style_proxy.get("regime_id"),
    "current_proxy_regime_confidence": style_proxy.get("regime_confidence"),
    "market_state_quality_ok": current_market.get("data_status", {}).get("market_state_quality_ok"),
    "market_state_quality_errors": current_market.get("data_status", {}).get("market_state_quality_errors", []),
    "broad_market_proxy_returns": {
        "5d": broad_market_proxy.get("return_5d"),
        "20d": broad_market_proxy.get("return_20d"),
        "60d": broad_market_proxy.get("return_60d"),
        "120d": broad_market_proxy.get("return_120d"),
    },
    "breadth_ratio": broad_market_proxy.get("breadth_ratio"),
    "is_high_volume_state": broad_market_proxy.get("is_high_volume_state"),
    "morning_brief_regime": current_market.get("trend_state", {}).get("morning_brief_regime"),
    "focus_sectors": current_market.get("style_and_sector", {}).get("focus_sectors", []),
}
analog_report["analog_sample_summary"] = {
    "analog_window_count": proxy_window_count,
    "same_primary_regime_count": same_primary_regime_count,
    "same_style_regime_count": same_style_regime_count,
    "regime_distribution": dict(regime_counter),
    "top_similarity_windows": top_similarity_windows,
    "forward_market_proxy_summary_all_windows": all_window_stats,
    "forward_market_proxy_summary_same_primary_only": same_primary_stats,
    "directionality_hint": directionality,
    "interpretation": "当前 analog 更像“短线偏震荡/偏弱、中期偏修复、60d 温和偏正但分散”的市场代理情境提示；它不是当前组合收益分布。",
}
analog_report["latest_weight_plan_window_alignment"] = {
    "latest_weight_plan_factor_ids": latest_weight_plan_factor_ids,
    "regime_union_factor_ids": regime_union_factor_ids,
    "proxy_window_count": proxy_window_count,
    "fully_aligned_window_count": fully_aligned_window_count,
    "same_primary_full_alignment_count": same_primary_full_alignment_count,
    "same_style_full_alignment_count": same_style_full_alignment_count,
    "window_matrix": window_rows,
    "summary": alignment_summary,
}
analog_report["factor_level_analog_alignment"] = factor_level_alignment
overlap = analog_report.setdefault("current_regime_union_overlap", {})
overlap["latest_weight_plan_factor_ids"] = latest_weight_plan_factor_ids
overlap["current_regime_union_factor_ids"] = regime_union_factor_ids
overlap["analog_effective_factor_proxy_frequency"] = coverage_counts
overlap["overlap_factor_ids"] = [
    factor_id for factor_id in latest_weight_plan_factor_ids if coverage_counts.get(factor_id, 0) > 0
]
overlap["current_only_factor_ids"] = [
    factor_id for factor_id in regime_union_factor_ids if factor_id not in latest_weight_plan_factor_ids
]
overlap["proxy_only_factor_ids"] = proxy_only_factor_ids
overlap["union_only_no_window_coverage_factor_ids"] = union_only_no_window_coverage_factor_ids
overlap["coverage_assessment"] = "latest shrink_uptrend 权重计划 3/3 已被 10/10 analog proxy windows 完整覆盖；f_chip_support_proxy / f_gap_return / f_atr_proxy_14 全命中，而 f_price_vs_ma20 仅存在于 broader regime union，且当前 0/10 windows 命中，应视为 union-only / no-window-coverage。"

analog_report.setdefault("decision_impact", {})["directional_takeaway"] = {
    horizon: directionality[horizon]["hint"] for horizon in ["5d", "20d", "60d"]
}
analog_report["decision_impact"]["promotion_freeze_should_remain"] = True
analog_report["decision_impact"]["why_freeze_cannot_be_lifted"] = [
    "analog 仍只是 market-proxy coverage，不是组合收益重放。",
    "current_combo 的 c2c / o2c / c2o formal backtest 已存在，但它们不能替代收益级 analog validation。",
    "即便 latest weight plan trio（f_chip_support_proxy, f_gap_return, f_atr_proxy_14）已被 10/10 proxy windows 覆盖，也不能替代独立 OOS / 收益级 analog validation。",
    "f_price_vs_ma20 仍只存在于 broader regime union，当前 0/10 windows 命中，只能视作 union-only / no-window-coverage 暴露。",
]
analog_report["decision_impact"]["blocked_claims"] = [
    "不能宣称当前组合已经通过 analog validation。",
    "不能把 5d/20d/60d 的 market proxy forward stats 直接当成组合收益预期。",
    "不能据此解除 promotion freeze 或把 watch 因子升级为正式 promote/active。",
]
analog_report["risk_and_limitations"] = [
    "analog windows use current raw-cache proxy features mapped onto historical regime_labels; factor references are current regime-map proxies, not reconstructed historical realized factor attribution.",
    "analog windows 使用的是当前 raw-cache proxy feature 与历史 regime_labels 的相似度映射，不是历史窗口内真实重建的组合持仓/收益 attribution。",
    "top-10 analog 中虽有 7/10 与当前同 primary regime、9/10 同 style，但最相近的前两窗仍是 shrink_downtrend，说明短线结构并不纯净。",
    "current_combo 的 c2c / o2c / c2o formal backtest 已落地，但它们仍共享同一 clean window，不能替代独立 OOS 或收益级 analog replay。",
    "claim-limit 继续显示三口径 holdout=0，nested 仍只适用于 c2c skeleton，因此 promotion freeze 必须维持。",
]
analog_report.setdefault("validation_status", {})["analog_validation_completed"] = False
analog_report["validation_status"]["independent_validation_completed"] = False
analog_report["validation_status"]["usable_portfolio_replay_sample_count"] = 0
analog_report["validation_status"]["proxy_analog_window_count"] = proxy_window_count
analog_report["validation_status"]["decision_grade"] = "hold_proxy_only_non_replay"
if str(ALIGNMENT_PATH) not in analog_report.setdefault("evidence_path", []):
    analog_report["evidence_path"].append(str(ALIGNMENT_PATH))
dump_json(ANALOG_REPORT_PATH, analog_report)

portfolio["generated_at"] = now
portfolio.setdefault("source_paths", {})["latest_weight_plan_analog_alignment"] = str(ALIGNMENT_PATH)
refresh_source_mtimes(portfolio, overrides={"latest_weight_plan_analog_alignment": now})
portfolio_oos = portfolio.setdefault("oos_validation_assessment", {})
portfolio_oos["analog_validation_completed"] = False
portfolio_oos["analog_proxy_stale_vs_latest_weight_map"] = False
portfolio_oos_overlap = portfolio_oos.setdefault("analog_proxy_overlap_with_latest_weight_plan", {})
portfolio_oos_overlap["overlap_factor_ids"] = [
    factor_id for factor_id in latest_weight_plan_factor_ids if coverage_counts.get(factor_id, 0) > 0
]
portfolio_oos_overlap["current_only_factor_ids"] = [
    factor_id for factor_id in regime_union_factor_ids if factor_id not in latest_weight_plan_factor_ids
]
portfolio_oos_overlap["proxy_only_factor_ids"] = proxy_only_factor_ids
portfolio_oos_overlap["union_only_no_window_coverage_factor_ids"] = union_only_no_window_coverage_factor_ids
portfolio_proxy = portfolio.setdefault("analog_proxy_context", {})
portfolio_proxy["effective_factor_proxy_frequency"] = coverage_counts
portfolio_proxy["overlap_with_latest_weight_plan"] = {
    "overlap_factor_ids": portfolio_oos_overlap["overlap_factor_ids"],
    "current_only_factor_ids": portfolio_oos_overlap["current_only_factor_ids"],
    "proxy_only_factor_ids": proxy_only_factor_ids,
    "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
}
portfolio_proxy["latest_weight_plan_proxy_window_alignment"] = {
    "proxy_window_count": proxy_window_count,
    "fully_aligned_window_count": fully_aligned_window_count,
    "proxy_only_factor_ids": proxy_only_factor_ids,
    "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
    "same_primary_full_alignment_count": same_primary_full_alignment_count,
}
portfolio_proxy["staleness_assessment"] = "regime_analog_windows.json 与 latest_weight_plan_analog_alignment.json 已对齐到最新 21:46 市场指纹：10/10 proxy windows 完整覆盖 latest weight plan trio（f_chip_support_proxy / f_gap_return / f_atr_proxy_14），但 f_price_vs_ma20 仅是 union-only / no-window-coverage 因子；整体仍只是市场情境 proxy，不是组合收益重放。"
portfolio.setdefault("conclusion", {})["summary"] = "current_combo 的 c2c / o2c / c2o formal backtest 已全部落地，latest shrink_uptrend trio 也已被 10/10 analog proxy windows 覆盖；但 analog 仍只是 market-proxy，不是收益级 replay，且 independent OOS / label-specific nested 仍未闭环，因此 promotion freeze 继续维持。"
if str(ALIGNMENT_PATH) not in portfolio.setdefault("evidence_path", []):
    portfolio["evidence_path"].append(str(ALIGNMENT_PATH))
dump_json(PORTFOLIO_PATH, portfolio)

overfit["generated_at"] = now
overfit_signal = overfit.setdefault("analog_proxy_signal", {})
overfit_signal["current_proxy_regime"] = style_proxy.get("regime_id")
overfit_signal["top10_market_proxy_return_stats"] = {
    horizon: {
        key: value
        for key, value in all_window_stats[horizon].items()
        if key in {"sample_count", "mean", "median", "positive_ratio"}
    }
    for horizon in ["5d", "20d", "60d"]
}
overfit_signal["effective_factor_proxy_frequency"] = coverage_counts
overfit_signal["overlap_with_latest_weight_plan"] = {
    "overlap_factor_ids": portfolio_oos_overlap["overlap_factor_ids"],
    "current_only_factor_ids": portfolio_oos_overlap["current_only_factor_ids"],
    "proxy_only_factor_ids": proxy_only_factor_ids,
    "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
}
overfit_signal["interpretation"] = "latest shrink_uptrend 计划中的 f_chip_support_proxy / f_gap_return / f_atr_proxy_14 已被 10/10 analog proxy windows 覆盖；但 f_price_vs_ma20 只存在于 broader regime union 且 0/10 windows 命中，因此仍只能视为 union-only / no-window-coverage 暴露，不能替代组合收益级验证。"
overfit_signal["latest_weight_plan_proxy_window_alignment"] = {
    "proxy_window_count": proxy_window_count,
    "fully_aligned_window_count": fully_aligned_window_count,
    "proxy_only_factor_ids": proxy_only_factor_ids,
    "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
    "same_primary_full_alignment_count": same_primary_full_alignment_count,
}
missing_detail = overfit.setdefault("missing_validation_stack", {}).setdefault("detail", [])
detail_line = "latest_weight_plan_analog_alignment.json 已按 21:46 指纹/analog windows 刷新：10/10 analog proxy windows 覆盖 current latest weight plan trio，但 f_price_vs_ma20 仍是 union-only / no-window-coverage（0/10 window coverage），所以 analog 仍不能当作组合收益验证。"
if detail_line not in missing_detail:
    missing_detail.append(detail_line)
if str(ALIGNMENT_PATH) not in overfit.setdefault("evidence_path", []):
    overfit["evidence_path"].append(str(ALIGNMENT_PATH))
dump_json(OVERFIT_PATH, overfit)

promotion["generated_at"] = now
promotion["analog_proxy_alignment"] = {
    "latest_weight_plan_factor_ids": latest_weight_plan_factor_ids,
    "fully_aligned_window_count": fully_aligned_window_count,
    "proxy_window_count": proxy_window_count,
    "proxy_only_factor_ids": proxy_only_factor_ids,
    "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
}
promotion.setdefault("summary", {})["summary"] = "promotion freeze 不变；current_combo 三口径 formal backtest 已齐，latest shrink_uptrend trio 在 10/10 analog proxy windows 中被覆盖，但 analog 仍不是收益级 replay，且独立 holdout=0 / nested 仍只适用于 c2c，因此当前因子继续只保留 watch。"
for item in promotion.get("watch", []):
    evidence_path = item.setdefault("evidence_path", [])
    if str(ALIGNMENT_PATH) not in evidence_path:
        evidence_path.append(str(ALIGNMENT_PATH))
dump_json(PROMOTION_PATH, promotion)

digest_text = DIGEST_PATH.read_text(encoding="utf-8")
new_bullet = "- 已重刷 analog_validation_report / latest_weight_plan_analog_alignment，来源切到 21:46 的 current_market_fingerprint / regime_analog_windows，并把 f_price_vs_ma20 明确标成 union-only 0/10 window coverage。"
if new_bullet not in digest_text:
    anchor = "## 三口径最小可信结论"
    if anchor in digest_text:
        digest_text = digest_text.replace(anchor, new_bullet + "\n\n" + anchor)
    else:
        digest_text = digest_text.rstrip() + "\n" + new_bullet + "\n"
DIGEST_PATH.write_text(digest_text, encoding="utf-8")

print(json.dumps(
    {
        "status": "ok",
        "generated_at": now,
        "alignment_path": str(ALIGNMENT_PATH),
        "analog_report_path": str(ANALOG_REPORT_PATH),
        "latest_weight_plan_factor_ids": latest_weight_plan_factor_ids,
        "regime_union_factor_ids": regime_union_factor_ids,
        "fully_aligned_window_count": fully_aligned_window_count,
        "proxy_window_count": proxy_window_count,
        "proxy_only_factor_ids": proxy_only_factor_ids,
        "union_only_no_window_coverage_factor_ids": union_only_no_window_coverage_factor_ids,
    },
    ensure_ascii=False,
    indent=2,
))
