#!/usr/bin/env python3
"""
factor_health_monitor.py — 因子健康监控器
==========================================
Weekly health check for the 6-factor greenfield portfolio.

Computes rolling 20-day Rank IC for each factor, compares against gate
thresholds, and emits alerts when:
  - Any factor IC direction reverses
  - Any factor IC drops below 30 % of its gate value
  - 3+ factors simultaneously decaying  → regime_shift_detected

Output
------
outputs/research_preview/factor_health_YYYYMMDD.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# project imports
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from greenfield_multifactor_lib import (  # noqa: E402
    GreenfieldConfig,
    add_forward_labels,
    add_greenfield_factors,
    attach_core_features,
    attach_industry_features,
    build_daily_regime_features,
    dump_json,
    load_config,
    load_industry_map,
    load_raw_history_panel,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
WINDOW = 20  # rolling IC window (trading days)

# IC gate values from portfolio_spec / task context
# key: factor_id  →  (gate_ic, expected_direction)
#   expected_direction: +1 means positive IC is normal, -1 means negative IC is normal
FACTOR_GATE: dict[str, dict[str, Any]] = {
    "g_intraday_vwap_deviation":    {"gate_ic": 0.025, "expected_direction": +1},
    "g_volume_price_divergence":    {"gate_ic": 0.018, "expected_direction": +1},
    "g_chip_pullback_support":      {"gate_ic": 0.008, "expected_direction": +1},
    "g_long_cost_concentration":    {"gate_ic": 0.008, "expected_direction": +1},
    "g_close_strength_ratio":       {"gate_ic": 0.018, "expected_direction": +1, "reversed": True},
    "g_intraday_range_expansion":   {"gate_ic": 0.025, "expected_direction": +1, "reversed": True},
}

REVERSED_FACTORS = {
    fid for fid, meta in FACTOR_GATE.items() if meta.get("reversed")
}
ALL_FACTOR_IDS = list(FACTOR_GATE.keys())

# Forward return label for Rank IC
LABEL_COL = "next_return_open_to_close_1d"

OUTPUT_DIR = Path("/Users/fisher/.openclaw/workspace/factor_factory/outputs/research_preview")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _spearman_rank_ic(x: pd.Series, y: pd.Series, min_obs: int = 30) -> float:
    """Daily cross-sectional Rank IC (Spearman via Pearson on ranks)."""
    m = x.notna() & y.notna()
    if m.sum() < min_obs:
        return float("nan")
    xr = x[m].rank(pct=True)
    yr = y[m].rank(pct=True)
    if xr.nunique() < 3 or yr.nunique() < 3:
        return float("nan")
    corr = xr.corr(yr, method="pearson")
    return float(corr) if pd.notna(corr) else float("nan")


def _daily_rank_ic_series(
    panel: pd.DataFrame,
    factor_col: str,
    label_col: str,
    dates: list[str],
) -> pd.Series:
    """Return a Series indexed by trade_date of daily cross-sectional Rank IC."""
    sub = panel.loc[panel["trade_date"].isin(dates), ["trade_date", factor_col, label_col]].copy()
    sub[factor_col] = pd.to_numeric(sub[factor_col], errors="coerce")
    sub[label_col] = pd.to_numeric(sub[label_col], errors="coerce")
    rows: list[dict[str, Any]] = []
    for dt, g in sub.groupby("trade_date", sort=True):
        ic = _spearman_rank_ic(g[factor_col], g[label_col])
        rows.append({"trade_date": dt, "ic": ic})
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows).set_index("trade_date")["ic"]
    return df


# ---------------------------------------------------------------------------
# main logic
# ---------------------------------------------------------------------------
def build_panel(cfg: GreenfieldConfig) -> pd.DataFrame:
    """Load raw history, attach features & factors, forward labels."""
    panel = load_raw_history_panel(cfg)
    if panel.empty:
        return panel
    panel = attach_core_features(panel)
    panel = panel[panel["listing_days"].fillna(0) >= cfg.min_listing_days].copy()
    industry_map = load_industry_map(cfg)
    panel = attach_industry_features(panel, industry_map)
    panel = add_greenfield_factors(panel)
    panel = add_forward_labels(panel)
    # regime columns are not needed for IC but attach for completeness
    _regimes = build_daily_regime_features(panel)
    panel = panel.merge(_regimes, on="trade_date", how="left")
    panel = panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    return panel


def compute_rolling_ic(
    panel: pd.DataFrame,
    window: int = WINDOW,
) -> dict[str, dict[str, Any]]:
    """
    For each factor, compute the rolling *window*-day Rank IC on the last
    *window* trading days of the panel.

    Returns:
        {factor_id: {rolling_ic: float, direction: str, ic_series_tail: list}}
    """
    all_dates = sorted(panel["trade_date"].unique())
    if len(all_dates) < window:
        tail_dates = all_dates
    else:
        tail_dates = all_dates[-window:]

    result: dict[str, dict[str, Any]] = {}
    for fid in ALL_FACTOR_IDS:
        if fid not in panel.columns:
            result[fid] = {
                "rolling_ic": None,
                "direction": "unknown",
                "ic_series_tail": [],
                "note": "factor column not found in panel",
            }
            continue

        ic_series = _daily_rank_ic_series(panel, fid, LABEL_COL, tail_dates)
        ic_vals = ic_series.dropna().tolist()
        if not ic_vals:
            result[fid] = {
                "rolling_ic": None,
                "direction": "unknown",
                "ic_series_tail": [],
                "note": "insufficient data",
            }
            continue

        mean_ic = float(np.mean(ic_vals))
        result[fid] = {
            "rolling_ic": round(mean_ic, 6),
            "direction": "positive" if mean_ic >= 0 else "negative",
            "ic_series_tail": [round(v, 6) for v in ic_vals[-5:]],  # last 5 for quick glance
        }

    return result


def detect_alerts(
    factor_stats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Apply the three alert rules and return a list of alert dicts.
    """
    alerts: list[dict[str, Any]] = []
    decaying_count = 0

    for fid, meta in FACTOR_GATE.items():
        stats = factor_stats.get(fid, {})
        rolling_ic = stats.get("rolling_ic")
        if rolling_ic is None:
            continue

        gate_ic = meta["gate_ic"]
        expected_dir = meta["expected_direction"]

        # --- rule 1: direction reversal ---
        actual_sign = 1 if rolling_ic >= 0 else -1
        if actual_sign != expected_dir:
            alerts.append({
                "type": "factor_direction_reversal",
                "factor": fid,
                "rolling_ic": round(rolling_ic, 6),
                "expected_direction": "positive" if expected_dir == 1 else "negative",
                "actual_direction": "positive" if actual_sign == 1 else "negative",
            })

        # --- rule 2: IC decaying below gate * 0.3 ---
        abs_ic = abs(rolling_ic)
        decay_threshold = gate_ic * 0.3
        if abs_ic < decay_threshold:
            decaying_count += 1
            alerts.append({
                "type": "ic_decaying",
                "factor": fid,
                "rolling_ic": round(rolling_ic, 6),
                "gate_ic": gate_ic,
                "decay_threshold": round(decay_threshold, 6),
            })

    # --- rule 3: 3+ factors decaying → regime shift ---
    if decaying_count >= 3:
        alerts.append({
            "type": "regime_shift_detected",
            "decaying_factor_count": decaying_count,
            "threshold": 3,
        })

    return alerts


def build_reversed_factors_status(
    factor_stats: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Special status block for the two reversed factors (g_close_strength_ratio,
    g_intraday_range_expansion).
    """
    status: dict[str, dict[str, Any]] = {}
    for fid in REVERSED_FACTORS:
        stats = factor_stats.get(fid, {})
        rolling_ic = stats.get("rolling_ic")
        meta = FACTOR_GATE[fid]
        expected = "positive"  # both reversed factors expect positive IC
        if rolling_ic is None:
            current_direction = "unknown"
            factor_status = "no_data"
        else:
            current_direction = "positive" if rolling_ic >= 0 else "negative"
            if current_direction == expected:
                factor_status = "healthy"
            else:
                factor_status = "reversed"
        status[fid] = {
            "current_direction": current_direction,
            "expected": expected,
            "status": factor_status,
            "rolling_ic": round(rolling_ic, 6) if rolling_ic is not None else None,
        }
    return status


def build_factor_health_report(cfg: GreenfieldConfig) -> dict[str, Any]:
    """
    Orchestrate: load panel → compute rolling IC → detect alerts → build JSON.
    """
    today_str = datetime.now().strftime("%Y%m%d")
    print(f"[factor_health_monitor] building panel …")
    panel = build_panel(cfg)
    if panel.empty:
        return {
            "date": today_str,
            "window": WINDOW,
            "factors": {},
            "alerts": [{"type": "data_unavailable", "note": "panel is empty"}],
            "reversed_factors_status": {},
        }

    all_dates = sorted(panel["trade_date"].unique())
    print(f"[factor_health_monitor] panel: {len(panel)} rows, {len(all_dates)} dates, "
          f"range {all_dates[0]} – {all_dates[-1]}")

    # --- compute rolling IC ---
    print(f"[factor_health_monitor] computing rolling {WINDOW}-day Rank IC …")
    factor_stats = compute_rolling_ic(panel, WINDOW)

    # --- enrich with gate comparison ---
    for fid, stats in factor_stats.items():
        meta = FACTOR_GATE.get(fid, {})
        gate_ic = meta.get("gate_ic", 0.0)
        rolling_ic = stats.get("rolling_ic")
        if rolling_ic is not None and gate_ic > 0:
            stats["vs_gate_ic"] = round(rolling_ic / gate_ic, 4)  # ratio: >1 means above gate
        else:
            stats["vs_gate_ic"] = None

    # --- detect alerts ---
    alerts = detect_alerts(factor_stats)

    # --- reversed factors status ---
    reversed_status = build_reversed_factors_status(factor_stats)

    # --- assemble report ---
    report: dict[str, Any] = {
        "date": today_str,
        "window": WINDOW,
        "panel_dates": len(all_dates),
        "panel_date_range": [all_dates[0], all_dates[-1]],
        "factors": {},
        "alerts": alerts,
        "reversed_factors_status": reversed_status,
    }

    for fid in ALL_FACTOR_IDS:
        stats = factor_stats.get(fid, {})
        gate_meta = FACTOR_GATE.get(fid, {})
        report["factors"][fid] = {
            "rolling_ic": stats.get("rolling_ic"),
            "direction": stats.get("direction", "unknown"),
            "vs_gate_ic": stats.get("vs_gate_ic"),
            "gate_ic": gate_meta.get("gate_ic"),
            "ic_series_tail": stats.get("ic_series_tail", []),
        }

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    cfg = load_config()
    report = build_health_report(cfg)
    out_path = OUTPUT_DIR / f"factor_health_{report['date']}.json"
    dump_json(out_path, report)
    print(f"[factor_health_monitor] ✓ written to {out_path}")

    # quick summary
    alert_types = [a["type"] for a in report.get("alerts", [])]
    if alert_types:
        print(f"[factor_health_monitor] ALERTS: {alert_types}")
    else:
        print(f"[factor_health_monitor] No alerts — all factors healthy.")

    for fid, fdata in report["factors"].items():
        ic = fdata.get("rolling_ic")
        vs = fdata.get("vs_gate_ic")
        ic_str = f"{ic:+.4f}" if ic is not None else "N/A"
        vs_str = f"{vs:.2f}x" if vs is not None else "N/A"
        print(f"  {fid:40s}  IC={ic_str}  gate={vs_str}")


def build_health_report(cfg: GreenfieldConfig) -> dict[str, Any]:
    """Public API — same as build_factor_health_report."""
    return build_factor_health_report(cfg)


if __name__ == "__main__":
    main()
