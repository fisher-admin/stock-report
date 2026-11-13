#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Promotion Evaluator — 晋升评估脚本（含降级规则）

Reads `outputs/research_preview/cumulative_stats.json`, checks 8 promotion
conditions, and writes `outputs/research_preview/promotion_evaluation.json`.

Suspension / demotion rules are evaluated first; if any trigger fires the
overall status is set to `suspended` with the corresponding reason, and a
`suspension_notice.json` is written alongside the evaluation.

Expected schema of cumulative_stats.json:

{
  "evaluation_date": "2026-05-05",
  "total_trading_days": 30,
  "daily_returns": [
    {
      "date": "2026-04-01",
      "o2c_return": 0.0012,
      "c2c_return": 0.0008,
      "benchmark_return": 0.0005
    }, ...
  ],
  "factor_ic_history": {
    "g_intraday_vwap_deviation":    [{"date": "2026-04-01", "ic": 0.03}, ...],
    "g_volume_price_divergence":    [...],
    "g_chip_pullback_support":      [...],
    "g_long_cost_concentration":    [...],
    "g_close_strength_ratio":       [...],
    "g_intraday_range_expansion":   [...]
  },
  "slippage_history": [
    {"date": "2026-04-01", "actual_slippage_pct": 0.0012}, ...
  ],
  "industry_concentration": [
    {"date": "2026-04-01", "max_industry_pct": 0.35}, ...
  ],
  "max_drawdown": -0.15,
  "o2c_sharpe_rolling_30d": 1.8,
  "o2c_return_vs_baseline_pct": 0.85
}

Usage:
    python scripts/promotion_evaluator.py [--stats PATH] [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
DEFAULT_STATS = ROOT / "outputs/research_preview/cumulative_stats.json"
DEFAULT_OUTPUT_DIR = ROOT / "outputs/research_preview"

# 6 因子门控 IC 值 — 与 phaseC4 gate 保持一致
FACTOR_IC_GATE: dict[str, float] = {
    "g_intraday_vwap_deviation": 0.025,
    "g_volume_price_divergence": 0.018,
    "g_chip_pullback_support": 0.008,
    "g_long_cost_concentration": 0.008,
    "g_close_strength_ratio": 0.018,
    "g_intraday_range_expansion": 0.025,
}

# Promotion-condition thresholds
THRESH_O2C_NET_POSITIVE_DAYS_MIN = 20          # out of 30
THRESH_O2C_NET_POSITIVE_WINDOW = 30
THRESH_O2C_SHARPE_ROLLING = 1.5
THRESH_O2C_RETURN_VS_BASELINE = 0.80           # 80 %

THRESH_SLIPPAGE_MAX = 0.0015                    # 0.15 %
THRESH_FACTOR_IC_WINDOW = 30                    # days — no direction flip
THRESH_MAX_DRAWDOWN = -0.25                     # MaxDD < 25 %
THRESH_INDUSTRY_CONCENTRATION_MAX = 0.40        # 40 %
THRESH_FACTOR_IC_POSITIVE_MIN = 5               # out of 6

# Suspension triggers
THRESH_SUSPEND_FACTOR_REVERSALS = 3             # 3+ factors flip simultaneously
THRESH_SUSPEND_CONSECUTIVE_LOSSES = 5           # 5 consecutive negative O2C days
THRESH_SUSPEND_MAX_DRAWDOWN = -0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning an empty dict on missing / parse errors."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_load_error": str(exc), "_path": str(path)}


def safe_float(val: Any, default: float | None = None) -> float | None:
    """Coerce *val* to float, returning *default* on failure."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Suspension / demotion checks  (evaluated BEFORE the 8 promotion gates)
# ---------------------------------------------------------------------------

def check_suspension_triggers(stats: dict[str, Any]) -> dict[str, Any] | None:
    """Return a suspension payload if any trigger fires, else ``None``."""

    # 1) 3+ 因子 IC 同时翻转
    factor_ic: dict[str, Any] = stats.get("factor_ic_history") or {}
    reversed_factors: list[str] = []
    for fname, gate_val in FACTOR_IC_GATE.items():
        history = factor_ic.get(fname) or []
        # Look at last 30 entries; a flip means the mean IC in the window
        # has the opposite sign of the gate value.
        window = history[-THRESH_FACTOR_IC_WINDOW:]
        if not window:
            continue
        ics = [safe_float(e.get("ic")) for e in window]
        ics = [v for v in ics if v is not None]
        if not ics:
            continue
        mean_ic = sum(ics) / len(ics)
        # gate_val is positive (absolute IC); actual gate sign depends on
        # whether the factor was originally reversed.  We store the signed
        # gate in the phaseC4 decision.  Here we detect *direction change*
        # by checking if recent IC mean has opposite sign vs the gate value
        # recorded in the stats under `factor_gate_sign` or just check if
        # mean IC crossed zero.
        gate_sign = safe_float(
            (stats.get("factor_gate_sign") or {}).get(fname)
        )
        if gate_sign is not None:
            # flip = sign(mean_ic) != sign(gate_sign)
            if (mean_ic > 0) != (gate_sign > 0):
                reversed_factors.append(fname)
        else:
            # Fallback: if gate is positive but mean is negative (or vice-versa
            # for reversed factors), treat as flip.
            # For the 6 known factors, 4 are positive-signal, 2 reversed.
            # We simply check whether |mean_ic| is < 10% of gate → degraded.
            # Full reversal = mean IC has opposite sign to what we'd expect.
            # Without explicit sign, treat mean_ic <= 0 as reversal for all
            # (conservative).
            if mean_ic < 0:
                reversed_factors.append(fname)

    if len(reversed_factors) >= THRESH_SUSPEND_FACTOR_REVERSALS:
        return {
            "status": "suspended",
            "reason": "multiple_factor_reversals",
            "detail": (
                f"{len(reversed_factors)} factor(s) IC direction flipped "
                f"within {THRESH_FACTOR_IC_WINDOW}-day window: "
                + ", ".join(reversed_factors)
            ),
            "reversed_factors": reversed_factors,
        }

    # 2) 连续 5 天 O2C 收益为负
    daily_returns: list[dict[str, Any]] = stats.get("daily_returns") or []
    if len(daily_returns) >= THRESH_SUSPEND_CONSECUTIVE_LOSSES:
        last_n = daily_returns[-THRESH_SUSPEND_CONSECUTIVE_LOSSES:]
        all_negative = all(
            safe_float(d.get("o2c_return"), 0) < 0 for d in last_n
        )
        if all_negative:
            dates = [d.get("date") for d in last_n]
            return {
                "status": "suspended",
                "reason": "consecutive_losses",
                "detail": (
                    f"O2C return negative for {THRESH_SUSPEND_CONSECUTIVE_LOSSES} "
                    f"consecutive days: {dates}"
                ),
                "consecutive_negative_days": dates,
            }

    # 3) MaxDD > 25 %
    max_dd = safe_float(stats.get("max_drawdown"))
    if max_dd is not None and max_dd < THRESH_SUSPEND_MAX_DRAWDOWN:
        return {
            "status": "suspended",
            "reason": "max_drawdown_exceeded",
            "detail": (
                f"Max drawdown {max_dd:.2%} exceeds threshold "
                f"{THRESH_SUSPEND_MAX_DRAWDOWN:.2%}"
            ),
            "max_drawdown": max_dd,
        }

    return None


# ---------------------------------------------------------------------------
# Promotion-condition evaluators
# ---------------------------------------------------------------------------

def _cond_o2c_net_positive_days(
    stats: dict[str, Any],
) -> dict[str, Any]:
    daily = stats.get("daily_returns") or []
    window = daily[-THRESH_O2C_NET_POSITIVE_WINDOW:]
    positive_days = sum(
        1 for d in window if safe_float(d.get("o2c_return"), 0) > 0
    )
    return {
        "passed": positive_days >= THRESH_O2C_NET_POSITIVE_DAYS_MIN,
        "value": positive_days,
        "threshold": f">= {THRESH_O2C_NET_POSITIVE_DAYS_MIN}/{THRESH_O2C_NET_POSITIVE_WINDOW} days",
        "description": "O2C 净收益 > 0 的天数（近30天）",
    }


def _cond_o2c_sharpe(stats: dict[str, Any]) -> dict[str, Any]:
    sharpe = safe_float(stats.get("o2c_sharpe_rolling_30d"))
    passed = sharpe is not None and sharpe > THRESH_O2C_SHARPE_ROLLING
    return {
        "passed": passed,
        "value": sharpe,
        "threshold": f"> {THRESH_O2C_SHARPE_ROLLING}",
        "description": "O2C Sharpe（30天滚动）",
    }


def _cond_o2c_return_vs_baseline(stats: dict[str, Any]) -> dict[str, Any]:
    ratio = safe_float(stats.get("o2c_return_vs_baseline_pct"))
    passed = ratio is not None and ratio > THRESH_O2C_RETURN_VS_BASELINE
    return {
        "passed": passed,
        "value": ratio,
        "threshold": f"> {THRESH_O2C_RETURN_VS_BASELINE:.0%}",
        "description": "O2C 收益 > 基线百分比",
    }


def _cond_slippage(stats: dict[str, Any]) -> dict[str, Any]:
    slip_hist = stats.get("slippage_history") or []
    if not slip_hist:
        return {
            "passed": False,
            "value": None,
            "threshold": f"< {THRESH_SLIPPAGE_MAX:.2%}",
            "description": "实际滑点 < 0.15%",
        }
    vals = [safe_float(e.get("actual_slippage_pct")) for e in slip_hist]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {
            "passed": False,
            "value": None,
            "threshold": f"< {THRESH_SLIPPAGE_MAX:.2%}",
            "description": "实际滑点 < 0.15%",
        }
    avg_slip = sum(vals) / len(vals)
    return {
        "passed": avg_slip < THRESH_SLIPPAGE_MAX,
        "value": round(avg_slip, 6),
        "threshold": f"< {THRESH_SLIPPAGE_MAX:.2%}",
        "description": "实际平均滑点",
    }


def _cond_no_ic_flip(stats: dict[str, Any]) -> dict[str, Any]:
    factor_ic: dict[str, Any] = stats.get("factor_ic_history") or {}
    flipped: list[str] = []
    for fname in FACTOR_IC_GATE:
        history = factor_ic.get(fname) or []
        window = history[-THRESH_FACTOR_IC_WINDOW:]
        if not window:
            continue
        ics = [safe_float(e.get("ic")) for e in window]
        ics = [v for v in ics if v is not None]
        if not ics:
            continue
        # Check if sign of IC changed within window (any consecutive pair)
        for i in range(1, len(ics)):
            if (ics[i] > 0) != (ics[i - 1] > 0):
                flipped.append(fname)
                break
    passed = len(flipped) == 0
    return {
        "passed": passed,
        "value": flipped if flipped else "none",
        "threshold": "0 factors with IC sign reversal in 30d window",
        "description": "无因子 IC 方向翻转（30天内）",
        "flipped_factors": flipped,
    }


def _cond_max_drawdown(stats: dict[str, Any]) -> dict[str, Any]:
    max_dd = safe_float(stats.get("max_drawdown"))
    # max_drawdown is expected to be negative (e.g. -0.1646)
    passed = max_dd is not None and max_dd > THRESH_MAX_DRAWDOWN
    return {
        "passed": passed,
        "value": max_dd,
        "threshold": f"> {THRESH_MAX_DRAWDOWN:.0%} (i.e. drawdown < 25%)",
        "description": "最大回撤 < 25%",
    }


def _cond_industry_concentration(stats: dict[str, Any]) -> dict[str, Any]:
    conc_hist = stats.get("industry_concentration") or []
    if not conc_hist:
        return {
            "passed": False,
            "value": None,
            "threshold": f"< {THRESH_INDUSTRY_CONCENTRATION_MAX:.0%}",
            "description": "单行业集中度 < 40%",
        }
    vals = [safe_float(e.get("max_industry_pct")) for e in conc_hist]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {
            "passed": False,
            "value": None,
            "threshold": f"< {THRESH_INDUSTRY_CONCENTRATION_MAX:.0%}",
            "description": "单行业集中度 < 40%",
        }
    max_conc = max(vals)
    return {
        "passed": max_conc < THRESH_INDUSTRY_CONCENTRATION_MAX,
        "value": round(max_conc, 4),
        "threshold": f"< {THRESH_INDUSTRY_CONCENTRATION_MAX:.0%}",
        "description": "单行业最大集中度",
    }


def _cond_factor_ic_positive(stats: dict[str, Any]) -> dict[str, Any]:
    factor_ic: dict[str, Any] = stats.get("factor_ic_history") or {}
    positive_count = 0
    per_factor: dict[str, Any] = {}
    for fname in FACTOR_IC_GATE:
        history = factor_ic.get(fname) or []
        if not history:
            per_factor[fname] = {"mean_ic": None, "positive": False}
            continue
        ics = [safe_float(e.get("ic")) for e in history]
        ics = [v for v in ics if v is not None]
        if not ics:
            per_factor[fname] = {"mean_ic": None, "positive": False}
            continue
        mean_ic = sum(ics) / len(ics)
        is_positive = mean_ic > 0
        if is_positive:
            positive_count += 1
        per_factor[fname] = {
            "mean_ic": round(mean_ic, 6),
            "positive": is_positive,
        }
    return {
        "passed": positive_count >= THRESH_FACTOR_IC_POSITIVE_MIN,
        "value": positive_count,
        "threshold": f">= {THRESH_FACTOR_IC_POSITIVE_MIN}/{len(FACTOR_IC_GATE)}",
        "description": "6 因子中仍保持 IC > 0 的数量",
        "per_factor": per_factor,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

CONDITION_CHECKS = [
    ("c1_o2c_net_positive_days", _cond_o2c_net_positive_days),
    ("c2_o2c_sharpe_rolling", _cond_o2c_sharpe),
    ("c3_o2c_return_vs_baseline", _cond_o2c_return_vs_baseline),
    ("c4_actual_slippage", _cond_slippage),
    ("c5_no_ic_direction_flip", _cond_no_ic_flip),
    ("c6_max_drawdown", _cond_max_drawdown),
    ("c7_industry_concentration", _cond_industry_concentration),
    ("c8_factor_ic_positive_count", _cond_factor_ic_positive),
]


def evaluate(stats: dict[str, Any]) -> dict[str, Any]:
    """Run all 8 conditions and return the evaluation payload."""
    conditions: dict[str, Any] = {}
    for key, fn in CONDITION_CHECKS:
        conditions[key] = fn(stats)

    all_passed = all(c.get("passed") for c in conditions.values())
    total_days = stats.get("total_trading_days") or len(
        stats.get("daily_returns") or []
    )

    if all_passed:
        recommendation = "PROMOTE — all 8 conditions satisfied"
    else:
        failed = [k for k, v in conditions.items() if not v.get("passed")]
        recommendation = (
            f"HOLD — {len(failed)} condition(s) not met: "
            + ", ".join(failed)
        )

    return {
        "schema_version": "2026-05-05.promotion_evaluation.v1",
        "evaluation_date": stats.get("evaluation_date") or now_iso(),
        "generated_at": now_iso(),
        "total_trading_days": total_days,
        "conditions": conditions,
        "all_passed": all_passed,
        "recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Promotion evaluator with suspension rules")
    ap.add_argument(
        "--stats",
        default=str(DEFAULT_STATS),
        help="Path to cumulative_stats.json",
    )
    ap.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for output JSON files",
    )
    ap.add_argument("--json", action="store_true", help="Print result as JSON to stdout")
    args = ap.parse_args()

    stats_path = Path(args.stats)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load cumulative stats
    if not stats_path.exists():
        msg = {
            "status": "error",
            "detail": f"cumulative_stats.json not found at {stats_path}",
            "hint": (
                "Run the research_preview monitoring pipeline first to "
                "generate cumulative_stats.json"
            ),
        }
        if args.json:
            print(json.dumps(msg, ensure_ascii=False, indent=2))
        else:
            print(f"[ERROR] {msg['detail']}")
            print(f"  hint: {msg['hint']}")
        return 1

    stats = load_json(stats_path)
    if "_load_error" in stats:
        msg = {"status": "error", "detail": stats["_load_error"]}
        if args.json:
            print(json.dumps(msg, ensure_ascii=False, indent=2))
        else:
            print(f"[ERROR] {msg['detail']}")
        return 1

    # ── Suspension check (runs first) ──────────────────────────────────────
    suspension = check_suspension_triggers(stats)
    if suspension is not None:
        # Write suspension notice
        notice = {
            "schema_version": "2026-05-05.suspension_notice.v1",
            "generated_at": now_iso(),
            "evaluation_date": stats.get("evaluation_date") or now_iso(),
            **suspension,
            "action": "Suspend paper-trading; manual review required before re-activation",
        }
        notice_path = output_dir / "suspension_notice.json"
        notice_path.write_text(
            json.dumps(notice, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Still run the 8 conditions for audit trail, but mark overall as suspended
        eval_result = evaluate(stats)
        eval_result["status"] = "suspended"
        eval_result["suspension"] = suspension
        eval_result["recommendation"] = (
            f"SUSPENDED — {suspension['reason']}: {suspension['detail']}"
        )

        eval_path = output_dir / "promotion_evaluation.json"
        eval_path.write_text(
            json.dumps(eval_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = {
            "status": "suspended",
            "reason": suspension["reason"],
            "detail": suspension["detail"],
            "evaluation_path": str(eval_path),
            "suspension_notice_path": str(notice_path),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[SUSPENDED] {suspension['reason']}")
            print(f"  detail: {suspension['detail']}")
            print(f"  notice: {notice_path}")
            print(f"  evaluation: {eval_path}")
        return 0

    # ── Normal promotion evaluation ────────────────────────────────────────
    eval_result = evaluate(stats)

    eval_path = output_dir / "promotion_evaluation.json"
    eval_path.write_text(
        json.dumps(eval_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = {
        "status": "ok",
        "all_passed": eval_result["all_passed"],
        "recommendation": eval_result["recommendation"],
        "evaluation_path": str(eval_path),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status_icon = "PASS" if eval_result["all_passed"] else "HOLD"
        print(f"[{status_icon}] {eval_result['recommendation']}")
        print(f"  evaluation: {eval_path}")
        for key, cond in eval_result["conditions"].items():
            icon = "✓" if cond["passed"] else "✗"
            print(f"    {icon} {key}: value={cond['value']}  threshold={cond['threshold']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
