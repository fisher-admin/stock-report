#!/usr/bin/env python3
"""Factor-level IC attribution diagnostics (diagnostic_only; never promotion).

Plan task: docs/factor-attribution-task-2026-08-12.md
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

import short_track_shadow as sts

SCHEMA_VERSION = "factor_attribution_ic.v2"
PUBLIC_STATE_SCHEMA = "factor_attribution_public_state.v1"
EVIDENCE_SCOPE = "diagnostic_only"
# Reverse / Phase-2 / Q1 conclusions require this many IC days.
MIN_INFERENCE_DAYS = 30
REPORT_ARCHIVE_KEEP = 30
ARCHIVE_NAME_RE = re.compile(r"^factor_ic_report_(\d{8})\.md$")
V43_FULL_DOMAIN_MIN_NAMES = 100
HORIZONS = (1, 3, 5, 10, 20)
FACTOR_NAMES = list(sts.BALANCED_CATEGORY_NAMES)
V45_FACTOR_NAMES = [f"v45_{name}" for name in FACTOR_NAMES]
_PANEL_CONFIG = {
    "schema": SCHEMA_VERSION,
    "factors": list(sts.BALANCED_CATEGORY_NAMES),
    "horizons": list(HORIZONS),
    "returns": "close_qfq_T_to_TplusN_no_cost",
    "ic": "spearman_rank_vs_excess",
    "nw_lag": "N-1",
    "universe": "full_balanced_domain",
    "calendar": "official_sse_trade_cal",
    "aggregation": "always_full_history",
    "v43": "artifact_only_full_domain",
    "v45": "processed_scores_from_first_snapshot",
    "deciles": "all_ten_mean_excess",
    "factor_formula_version": sts.BALANCED_RULES.get("factor_formula_version"),
}
# Day-panel cache key excludes the inference threshold so raising it does not rebuild PIT.
PANEL_CONFIG_HASH = hashlib.sha256(json.dumps(_PANEL_CONFIG, sort_keys=True).encode()).hexdigest()[:16]
CONFIG_HASH = hashlib.sha256(
    json.dumps({**_PANEL_CONFIG, "min_inference_days": MIN_INFERENCE_DAYS}, sort_keys=True).encode()
).hexdigest()[:16]


class AttributionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AttributionPaths:
    root: Path
    cache: Path
    latest_json: Path
    latest_md: Path
    public_state: Path


def build_paths(workspace: Path) -> AttributionPaths:
    root = workspace / "stock_data/03-working/factor_attribution"
    return AttributionPaths(
        root=root,
        cache=root / "cache" / f"cfg_{PANEL_CONFIG_HASH}",
        latest_json=root / "factor_ic_latest.json",
        latest_md=root / "factor_ic_report_latest.md",
        public_state=root / "factor_attribution_state.json",
    )


def build_public_factor_attribution_state(result: dict[str, Any]) -> dict[str, Any]:
    """Slim diagnostic-only payload for public latest/. Never includes local paths."""
    coverage = result.get("coverage") or {}
    homology = result.get("homology") or {}
    reverse_test = result.get("reverse_test") or {}
    repro = result.get("repro_metadata") or {}
    per_factor = result.get("per_factor") or {}
    extremes = pick_signed_ic_extremes(per_factor)
    positive = extremes.get("positive")
    monotonicity = None
    if positive:
        monotonicity = ((per_factor.get(positive[0]) or {}).get("5") or {}).get("decile_monotonicity_mean")
    try:
        monotonicity_warning = monotonicity is not None and float(monotonicity) < 0.5
    except (TypeError, ValueError):
        monotonicity_warning = False
    strongest_positive = None
    if positive:
        strongest_positive = {
            "factor": positive[0],
            "mean_ic": finite_or_none(positive[1]),
            "icir": finite_or_none(positive[2]),
            "t_nw": finite_or_none(positive[3]),
            "n_days": int(positive[4]),
            "decile_monotonicity_mean": finite_or_none(monotonicity),
        }
    return json_safe(
        {
            "schema_version": PUBLIC_STATE_SCHEMA,
            "evidence_scope": "diagnostic_only",
            "promotion_eligible": False,
            "not_for_promotion": True,
            "as_of": coverage.get("as_of"),
            "aggregation_scope": coverage.get("aggregation_scope") or "full_history",
            "min_inference_days": MIN_INFERENCE_DAYS,
            "signal_days_ok": coverage.get("signal_days_ok"),
            "homology_flag_same_factor_family": bool(homology.get("flag_same_factor_family")),
            "reverse_phase2_trigger": bool(reverse_test.get("meets_phase2_trigger")),
            "reverse_phase2_auto_start": False,
            "q1_decile_monotonicity_warning": bool(monotonicity_warning),
            "strongest_positive_ic_t5": strongest_positive,
            "generated_at": repro.get("generated_at"),
            "config_hash": repro.get("config_hash"),
        }
    )


def write_public_factor_attribution_state(
    result: dict[str, Any],
    *,
    workspace: Path,
    paths: AttributionPaths,
) -> dict[str, str]:
    payload = json.dumps(
        build_public_factor_attribution_state(result),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    write_text_atomic(paths.public_state, payload)
    written = {"working": str(paths.public_state.name)}
    published_dir = Path(workspace) / "stock-report" / "data" / "latest"
    if published_dir.exists():
        dest = published_dir / "factor_attribution_state.json"
        write_text_atomic(dest, payload)
        written["published"] = dest.name
    return written


def finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def json_safe(value: Any) -> Any:
    """Convert numpy / NaN / Inf so json.dumps(..., allow_nan=False) succeeds."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return finite_or_none(value)
    return value


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def fmt4(value: Any) -> str:
    """Four-decimal display for report prose; None/NaN → n/a."""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(number):
        return "n/a"
    return f"{number:.4f}"


def series_inference_status(n_days: int) -> str:
    if n_days <= 0:
        return "pending"
    if n_days < MIN_INFERENCE_DAYS:
        return "insufficient_sample"
    return "ok"


def usable_ic_cell(ic: dict[str, Any] | None) -> bool:
    if not isinstance(ic, dict):
        return False
    if ic.get("status") != "ok":
        return False
    if ic.get("mean_ic") is None:
        return False
    try:
        return int(ic.get("n_days") or 0) >= MIN_INFERENCE_DAYS
    except (TypeError, ValueError):
        return False


def pick_signed_ic_extremes(
    per_factor: dict[str, Any],
    *,
    horizon: str = "5",
    names: list[str] | None = None,
) -> dict[str, tuple[str, float, Any, Any, int] | None]:
    """Strongest positive / strongest negative mean IC among inference-ok cells."""
    rows: list[tuple[str, float, Any, Any, int]] = []
    for name in names or FACTOR_NAMES:
        ic = ((per_factor.get(name) or {}).get(str(horizon)) or {}).get("ic") or {}
        if not usable_ic_cell(ic):
            continue
        rows.append(
            (
                name,
                float(ic["mean_ic"]),
                ic.get("icir"),
                ic.get("t_nw"),
                int(ic.get("n_days") or 0),
            )
        )
    positive = [row for row in rows if row[1] > 0]
    negative = [row for row in rows if row[1] < 0]
    return {
        "positive": max(positive, key=lambda row: row[1]) if positive else None,
        "negative": min(negative, key=lambda row: row[1]) if negative else None,
    }


def describe_ic_half_life(per_factor: dict[str, Any], names: list[str] | None = None) -> str:
    """One-sentence IC-vs-horizon summary for inference-ok cells."""
    growing: list[str] = []
    decaying: list[str] = []
    stable: list[str] = []
    for name in names or FACTOR_NAMES:
        points: list[tuple[int, float]] = []
        for horizon in HORIZONS:
            ic = ((per_factor.get(name) or {}).get(str(horizon)) or {}).get("ic") or {}
            if usable_ic_cell(ic):
                points.append((int(horizon), float(ic["mean_ic"])))
        if len(points) < 2:
            continue
        delta = points[-1][1] - points[0][1]
        if delta > 0.02:
            growing.append(f"{name}({fmt4(points[0][1])}→{fmt4(points[-1][1])})")
        elif delta < -0.02:
            decaying.append(f"{name}({fmt4(points[0][1])}→{fmt4(points[-1][1])})")
        else:
            stable.append(name)
    bits: list[str] = []
    if growing:
        bits.append("随持有期增强：" + "、".join(growing))
    if decaying:
        bits.append("随持有期减弱或更负：" + "、".join(decaying))
    if stable:
        bits.append("短长窗变化不大：" + "、".join(stable))
    if not bits:
        return "可用持有期不足以概括半衰期。"
    return "；".join(bits) + "。诊断口径，非可执行收益。"


def panel_missing_v45_columns(panel: pd.DataFrame | None) -> bool:
    if panel is None:
        return True
    required = V45_FACTOR_NAMES + ["v45_composite"]
    return any(column not in panel.columns for column in required)


def seed_cache_from_previous_config(paths: AttributionPaths) -> None:
    """Copy day panels from an older cfg_* cache so a conclusion-only hash bump does not rebuild PIT."""
    day_dir = paths.cache / "day_panels"
    day_dir.mkdir(parents=True, exist_ok=True)
    if any(day_dir.glob("panel_*.parquet")):
        return
    cache_root = paths.root / "cache"
    if not cache_root.exists():
        return
    best: Path | None = None
    best_mtime = -1.0
    for candidate in cache_root.glob("cfg_*/day_panels"):
        if candidate.resolve() == day_dir.resolve():
            continue
        count = len(list(candidate.glob("panel_*.parquet")))
        if count == 0:
            continue
        mtime = candidate.stat().st_mtime
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    if best is None:
        return
    for src in list(best.glob("panel_*.parquet")) + list(best.glob("skip_*.json")):
        dest = day_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
    pit = paths.cache / "pit_fetch"
    old_pit = best.parent / "pit_fetch"
    if old_pit.exists() and not any(pit.glob("*")):
        shutil.copytree(old_pit, pit, dirs_exist_ok=True)


def archive_previous_markdown_report(paths: AttributionPaths, *, incoming_as_of: str) -> Path | None:
    """Copy latest.md to factor_ic_report_{previous_as_of}.md before overwrite; keep 30 archives."""
    if not paths.latest_md.exists():
        return None
    prev_as_of = incoming_as_of
    if paths.latest_json.exists():
        try:
            previous = json.loads(paths.latest_json.read_text(encoding="utf-8"))
            prev_as_of = str((previous.get("coverage") or {}).get("as_of") or incoming_as_of)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            prev_as_of = incoming_as_of
    if not re.fullmatch(r"\d{8}", prev_as_of):
        prev_as_of = incoming_as_of
    dest = paths.root / f"factor_ic_report_{prev_as_of}.md"
    shutil.copy2(paths.latest_md, dest)
    prune_report_archives(paths.root)
    return dest


def prune_report_archives(root: Path, keep: int = REPORT_ARCHIVE_KEEP) -> None:
    dated: list[tuple[str, Path]] = []
    for path in root.glob("factor_ic_report_*.md"):
        match = ARCHIVE_NAME_RE.match(path.name)
        if match:
            dated.append((match.group(1), path))
    dated.sort(key=lambda item: item[0], reverse=True)
    for _, path in dated[keep:]:
        path.unlink(missing_ok=True)


def spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    rx = pd.Series(x).rank(method="average").to_numpy(dtype=float)
    ry = pd.Series(y).rank(method="average").to_numpy(dtype=float)
    if np.std(rx) <= 0 or np.std(ry) <= 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def newey_west_t(series: Iterable[float], *, lag: int) -> float:
    """t-stat of mean with Newey–West HAC; lag should be N-1 for N-day overlapping returns."""
    x = np.asarray(list(series), dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return float("nan")
    mean = float(np.mean(x))
    demean = x - mean
    gamma0 = float(np.dot(demean, demean) / n)
    var = gamma0
    L = max(0, int(lag))
    for j in range(1, L + 1):
        w = 1.0 - j / (L + 1.0)
        cov = float(np.dot(demean[j:], demean[:-j]) / n)
        var += 2.0 * w * cov
    se = math.sqrt(max(var, 0.0) / n)
    if se <= 0:
        return float("nan")
    return mean / se


def assign_deciles(values: pd.Series, ts_codes: pd.Series, *, n: int = 10) -> pd.Series:
    """Stable decile labels 1..n; ties broken by ts_code."""
    # Use positional construction so a named index (e.g. ts_code) never collides
    # with the ts_code column during sort_values.
    values = values.reset_index(drop=True)
    codes = ts_codes.reset_index(drop=True).astype(str)
    frame = pd.DataFrame(
        {
            "value": pd.to_numeric(values, errors="coerce"),
            "ts_code": codes,
            "_orig": np.arange(len(values)),
        }
    )
    valid = frame.dropna(subset=["value"]).sort_values(["value", "ts_code"], ascending=[True, True])
    m = len(valid)
    labels_arr = np.full(len(frame), np.nan)
    if m < n:
        order = np.arange(m)
        assigned = np.minimum(n, 1 + (order * n // max(m, 1)))
        labels_arr[valid["_orig"].to_numpy()] = assigned
    else:
        base = m // n
        rem = m % n
        sizes = [base + (1 if i < rem else 0) for i in range(n)]
        labels = []
        for i, size in enumerate(sizes, start=1):
            labels.extend([i] * size)
        labels_arr[valid["_orig"].to_numpy()] = labels
    # Caller reindexes via original factor index alignment in decile_stats_for_day
    return pd.Series(labels_arr, index=range(len(frame)), dtype=float)


def load_price_panel(cache_dir: Path, trade_dates: list[str]) -> pd.DataFrame:
    frames = []
    for day in trade_dates:
        path = cache_dir / f"stk_factor_{day}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["ts_code", "trade_date", "close_qfq", "open_qfq", "high_qfq", "low_qfq", "vol", "amount", "macd_dif", "macd_dea"])
        df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
        frames.append(df)
    if not frames:
        raise AttributionError(f"no stk_factor panels under {cache_dir}")
    out = pd.concat(frames, ignore_index=True)
    out["ts_code"] = out["ts_code"].astype(str)
    for col in ("close_qfq", "open_qfq", "high_qfq", "low_qfq", "vol", "amount", "macd_dif", "macd_dea"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def slice_prices_for_signal(
    prices: pd.DataFrame,
    *,
    trade_date: str,
    calendar: list[str],
    lookback_sessions: int = 20,
) -> pd.DataFrame:
    """Keep only official sessions in [T-lookback, T]. Never include T+1 or later."""
    if trade_date not in calendar:
        return prices.iloc[0:0].copy()
    idx = calendar.index(trade_date)
    start = max(0, idx - int(lookback_sessions))
    allowed = set(calendar[start : idx + 1])
    out = prices[prices["trade_date"].isin(allowed)].copy()
    leaked = out[out["trade_date"].astype(str) > str(trade_date)]
    if not leaked.empty:
        raise AttributionError(f"price slice leaked {sorted(leaked['trade_date'].unique().tolist())} after {trade_date}")
    return out


def official_end_date(calendar: list[str], signal_date: str, horizon: int) -> str | None:
    if signal_date not in calendar:
        return None
    end_pos = calendar.index(signal_date) + int(horizon)
    if end_pos >= len(calendar):
        return None
    return calendar[end_pos]


def forward_close_returns(
    prices: pd.DataFrame,
    *,
    signal_date: str,
    horizon: int,
    calendar: list[str],
) -> pd.Series:
    """close(T)->close(T+N) on the *official* calendar.

    Missing end-session price → NaN. Must not substitute the next cached file date.
    """
    end_date = official_end_date(calendar, signal_date, horizon)
    if end_date is None:
        return pd.Series(dtype=float)
    base = prices[prices["trade_date"] == signal_date][["ts_code", "close_qfq"]].rename(columns={"close_qfq": "p0"})
    end = prices[prices["trade_date"] == end_date][["ts_code", "close_qfq"]].rename(columns={"close_qfq": "p1"})
    if base.empty:
        return pd.Series(dtype=float)
    merged = base.merge(end, on="ts_code", how="left")
    ret = merged["p1"] / merged["p0"] - 1.0
    ret.index = merged["ts_code"].astype(str)
    ret[~np.isfinite(ret.to_numpy(dtype=float))] = np.nan
    bad = (merged["p0"] <= 0) | (merged["p1"] <= 0)
    ret[bad.to_numpy()] = np.nan
    return ret


def daily_factor_panel_from_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Given a validated balanced feature frame, attach component scores + composite."""
    scores = sts._balanced_component_scores(frame)
    out = frame[["ts_code"]].copy()
    out["ts_code"] = out["ts_code"].astype(str)
    for name in FACTOR_NAMES:
        out[name] = scores[name].to_numpy(dtype=float)
    out["composite"] = scores.mean(axis=1).to_numpy(dtype=float)
    out["composite_reversed"] = -out["composite"]
    return out.reset_index(drop=True)


def daily_v45_panel_from_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Full-domain v45 processed scores (not Top20). Same formulas as production snapshot."""
    raw = sts._cross_sectional_raw_signals(frame)
    processed = pd.DataFrame(index=frame.index)
    k = float(sts.CROSS_SECTIONAL_RULES["mad_winsor_k"])
    for name in FACTOR_NAMES:
        winsorized = sts._mad_winsor(raw[name], k=k)
        if name == "relative_strength_neutralized":
            signal = sts._joint_industry_size_residual(winsorized, frame)
        else:
            signal = winsorized
        z = sts._zscore(signal)
        ordered = pd.DataFrame({"z": z, "ts_code": frame["ts_code"].astype(str)}).sort_values(
            ["z", "ts_code"], ascending=[True, True]
        )
        ordered["score"] = np.linspace(0.0, 100.0, len(ordered), endpoint=True)
        processed[name] = ordered["score"].reindex(frame.index)
    out = frame[["ts_code"]].copy()
    out["ts_code"] = out["ts_code"].astype(str)
    for name in FACTOR_NAMES:
        out[f"v45_{name}"] = processed[name].to_numpy(dtype=float)
    out["v45_composite"] = processed.mean(axis=1).to_numpy(dtype=float)
    out["v45_composite_reversed"] = -out["v45_composite"]
    return out.reset_index(drop=True)


def first_v45_production_date(workspace: Path) -> str | None:
    daily = workspace / "stock_data/03-working/strategy_research/short_track/daily"
    dates: list[str] = []
    if daily.exists():
        for path in daily.glob("prebreakout_v45_cross_sectional_*_candidate_snapshot.json"):
            stem = path.stem  # prebreakout_v45_cross_sectional_YYYYMMDD_candidate_snapshot
            parts = stem.split("_")
            for part in parts:
                if len(part) == 8 and part.isdigit():
                    dates.append(part)
                    break
    return min(dates) if dates else None


def load_v43_composite_artifact(workspace: Path, trade_date: str) -> dict[str, Any]:
    """Load v43 composite only from existing ranking artifacts.

    Full-domain artifacts enter IC. Top20-only snapshots are range_restricted
    and excluded from IC (optional within-Top20 reference only).
    """
    candidates = [
        workspace
        / f"stock_data/03-working/strategy_research/short_track/daily/prebreakout_v43_control_{trade_date}_candidate_snapshot.json",
        workspace / f"stock_data/03-working/selection_history/{trade_date}.json",
    ]
    payload = None
    source = None
    rows: list[dict[str, Any]] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("candidates"), list):
            rows = list(payload["candidates"])
        elif isinstance(payload.get("stocks"), list):
            rows = list(payload["stocks"])
        else:
            continue
        source = str(path)
        break
    if not rows:
        return {"status": "missing", "range_restricted": None, "excluded_from_ic": True, "panel": None}
    codes: list[str] = []
    scores: list[float] = []
    for row in rows:
        ts_code = str(row.get("ts_code") or row.get("display_code") or "").strip()
        if not ts_code and row.get("stock_code"):
            raw = str(row["stock_code"])
            ts_code = raw if "." in raw else ""
        score = row.get("score")
        if not ts_code or score is None:
            continue
        try:
            scores.append(float(score))
        except (TypeError, ValueError):
            continue
        codes.append(ts_code)
    if len(codes) < 3:
        return {"status": "missing", "range_restricted": None, "excluded_from_ic": True, "panel": None}
    restricted = len(codes) < V43_FULL_DOMAIN_MIN_NAMES
    panel = pd.DataFrame(
        {
            "ts_code": codes,
            "v43_composite": scores,
            "v43_composite_reversed": [-s for s in scores],
        }
    )
    return {
        "status": "ok",
        "range_restricted": restricted,
        "excluded_from_ic": restricted,
        "n_names": len(codes),
        "source": source,
        "panel": panel,
    }


def ic_for_day(factor: pd.Series, excess: pd.Series) -> float:
    mask = factor.notna() & excess.notna()
    if mask.sum() < 30:
        return float("nan")
    return spearman_corr(factor[mask].to_numpy(dtype=float), excess[mask].to_numpy(dtype=float))


def decile_stats_for_day(factor: pd.Series, excess: pd.Series, ts_codes: pd.Series) -> dict[str, Any]:
    mask = (factor.notna() & excess.notna()).to_numpy()
    f = pd.Series(factor.to_numpy(dtype=float)[mask])
    e = pd.Series(excess.to_numpy(dtype=float)[mask])
    codes = pd.Series(ts_codes.astype(str).to_numpy()[mask])
    if len(f) < 30:
        return {"decile_means": None, "top_minus_bottom": float("nan"), "monotonicity": float("nan")}
    labels = assign_deciles(f, codes, n=10)
    means = []
    for d in range(1, 11):
        part = e[labels.to_numpy() == d]
        means.append(float(part.mean()) if len(part) else float("nan"))
    top = means[9]
    bottom = means[0]
    mono = spearman_corr(np.arange(1, 11, dtype=float), np.asarray(means, dtype=float))
    return {
        "decile_means": means,
        "top_minus_bottom": top - bottom if np.isfinite(top) and np.isfinite(bottom) else float("nan"),
        "bottom_minus_top": bottom - top if np.isfinite(top) and np.isfinite(bottom) else float("nan"),
        "monotonicity": mono,
    }


def summarize_ic_series(ics: list[float], *, horizon: int) -> dict[str, Any]:
    arr = np.asarray(ics, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {
            "n_days": 0,
            "mean_ic": None,
            "icir": None,
            "t_nw": None,
            "t_naive": None,
            "status": "pending",
        }
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else float("nan")
    icir = mean / std if std and np.isfinite(std) and std > 0 else None
    t_naive = mean / (std / math.sqrt(len(arr))) if std and std > 0 else None
    t_nw = newey_west_t(arr, lag=max(horizon - 1, 0))
    n_days = int(len(arr))
    return {
        "n_days": n_days,
        "mean_ic": finite_or_none(mean),
        "icir": finite_or_none(icir),
        "t_nw": finite_or_none(t_nw),
        "t_naive": finite_or_none(t_naive),
        "status": series_inference_status(n_days),
    }


def summarize_spread_series(spreads: list[float], *, horizon: int) -> dict[str, Any]:
    arr = np.asarray(spreads, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {"n_days": 0, "mean": None, "t_nw": None, "status": "pending"}
    mean = float(np.mean(arr))
    n_days = int(len(arr))
    return {
        "n_days": n_days,
        "mean": finite_or_none(mean),
        "t_nw": finite_or_none(newey_west_t(arr, lag=max(horizon - 1, 0))),
        "status": series_inference_status(n_days),
    }


def summarize_decile_means(daily_means: list[list[float]]) -> dict[str, Any]:
    """Average each of the 10 decile buckets across days."""
    if not daily_means:
        return {"n_days": 0, "mean_excess": [None] * 10, "status": "pending"}
    mat = np.asarray(daily_means, dtype=float)
    if mat.ndim != 2 or mat.shape[1] != 10:
        return {"n_days": 0, "mean_excess": [None] * 10, "status": "pending"}
    means = []
    for j in range(10):
        col = mat[:, j]
        col = col[np.isfinite(col)]
        means.append(finite_or_none(np.mean(col)) if len(col) else None)
    n_days = int(len(mat))
    return {"n_days": n_days, "mean_excess": means, "status": series_inference_status(n_days)}


def significant_nw_t(*, t_nw: Any, n_days: Any, threshold: float = 2.0) -> bool:
    try:
        n = int(n_days or 0)
        t = float(t_nw)
    except (TypeError, ValueError):
        return False
    return n >= MIN_INFERENCE_DAYS and np.isfinite(t) and t > threshold


def pairwise_spearman_matrix(day_df: pd.DataFrame, cols: list[str]) -> np.ndarray | None:
    valid = day_df[cols].dropna()
    if len(valid) < 30:
        return None
    k = len(cols)
    mat = np.eye(k)
    for i in range(k):
        for j in range(i + 1, k):
            c = spearman_corr(valid.iloc[:, i].to_numpy(dtype=float), valid.iloc[:, j].to_numpy(dtype=float))
            mat[i, j] = mat[j, i] = c
    return mat


def repro_metadata(*, as_of: str, n_signal_days: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "evidence_scope": EVIDENCE_SCOPE,
        "config_hash": CONFIG_HASH,
        "as_of": as_of,
        "n_signal_days": n_signal_days,
        "aggregation_scope": "full_history",
        "min_inference_days": MIN_INFERENCE_DAYS,
        "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "producer": "factor_attribution.py",
        "not_for_promotion": True,
    }
    if extra:
        meta.update(extra)
    return meta
