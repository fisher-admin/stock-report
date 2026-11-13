"""T+1/T+3/T+5 verification of selection_history snapshots.

For each snapshot date D and each selected stock:
    forward_return(N) = close(D + N trading days) / close(D) - 1   (percent)
i.e. plain close-to-close from the snapshot date — this is the *signal
quality* view (no execution frictions), complementing backtest_v2 which
models honest execution.

Market baseline for the same window: compound of the all-stock equal-weight
mean pct_chg over trading days (D, D+N]; close-to-close, exactly comparable.

Defensive about snapshot format drift: stock entries may carry ts_code or
bare code, rank or rank_no or neither, and extra fields are ignored.
"""

from __future__ import annotations

import bisect
import glob
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import numpy as np

from quant.datahub import (PriceBook, next_trade_date, normalize_ts_code)

SCHEMA_VERSION = "verify_v2.1"
DEFAULT_HORIZONS = (1, 3, 5)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Snapshot loading (defensive)
# --------------------------------------------------------------------------

def load_selection_snapshots(dir_path: str) -> List[dict]:
    """Load selection_history/*.json -> [{trade_date, stocks, source_file}].

    Only files named YYYYMMDD.json are considered (quarantine subdir and
    suffixed files are excluded by construction). Older/newer snapshot
    layouts are normalized; malformed entries are skipped, not fatal.
    """
    out: List[dict] = []
    for path in sorted(glob.glob(os.path.join(dir_path, "*.json"))):
        base = os.path.basename(path)[:-len(".json")]
        if not (len(base) == 8 and base.isdigit()):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            out.append({"trade_date": base, "stocks": [],
                        "source_file": path, "load_error": True})
            continue
        stocks: List[dict] = []
        for i, s in enumerate(raw.get("stocks") or []):
            if not isinstance(s, dict):
                continue
            ts = s.get("ts_code") or s.get("code")
            if not ts:
                continue
            rank = s.get("rank") or s.get("rank_no") or (i + 1)
            try:
                rank = int(rank)
            except (TypeError, ValueError):
                rank = i + 1
            stocks.append({
                "ts_code": normalize_ts_code(ts),
                "name": s.get("name") or s.get("stock_name"),
                "rank": rank,
                "score": s.get("score"),
                "snap_price": s.get("price"),
            })
        stocks.sort(key=lambda x: x["rank"])
        out.append({
            "trade_date": str(raw.get("trade_date") or base),
            "stocks": stocks,
            "source_file": path,
        })
    out.sort(key=lambda x: x["trade_date"])
    return out


# --------------------------------------------------------------------------
# Core verification math
# --------------------------------------------------------------------------

def baseline_window_pct(base_map: Dict[str, float], calendar: List[str],
                        date: str, n: int) -> Optional[float]:
    """Market compounded pct over trading days (date, date+n]; None if pending."""
    end = next_trade_date(calendar, date, n)
    if end is None:
        return None
    i = bisect.bisect_right(calendar, str(date))
    j = bisect.bisect_left(calendar, end)
    eq = 1.0
    for d in calendar[i:j + 1]:
        eq *= (1.0 + base_map.get(d, 0.0) / 100.0)
    return (eq - 1.0) * 100.0


def verify_snapshot(snapshot: dict, book: PriceBook, calendar: List[str],
                    base_map: Dict[str, float],
                    horizons: Sequence[int] = DEFAULT_HORIZONS) -> dict:
    """Realized forward returns + hit rates for one snapshot date."""
    horizons = sorted(set(int(h) for h in horizons))
    date = str(snapshot["trade_date"])
    rows: List[dict] = []
    for s in snapshot.get("stocks", []):
        code = s["ts_code"]
        base_row = book.row_on(code, date)
        row = {"ts_code": code, "name": s.get("name"), "rank": s.get("rank"),
               "score": s.get("score"),
               "base_close": None if base_row is None else base_row[1],
               "flags": []}
        if base_row is None:
            row["flags"].append("no_base_close")
        for h in horizons:
            key = f"t{h}_pct"
            row[key] = None
            if base_row is None:
                continue
            target = next_trade_date(calendar, date, h)
            if target is None:
                continue  # horizon beyond available calendar -> pending
            hit = book.row_on(code, target)
            if hit is not None:
                close_n, carried = hit[1], False
            else:
                cb = book.close_on_or_before(code, target)
                if cb is None or cb[0] < date:
                    continue  # nothing tradable in window -> pending
                close_n, carried = cb[1], True
            row[key] = (close_n / base_row[1] - 1.0) * 100.0
            if carried and f"t{h}_carry_forward" not in row["flags"]:
                row["flags"].append(f"t{h}_carry_forward")
        rows.append(row)

    stats: Dict[str, dict] = {}
    for h in horizons:
        key = f"t{h}_pct"
        vals = [(r[key], r) for r in rows if r[key] is not None]
        if vals:
            arr = np.array([v for v, _ in vals], dtype=float)
            best = max(vals, key=lambda x: x[0])[1]
            worst = min(vals, key=lambda x: x[0])[1]
            baseline = baseline_window_pct(base_map, calendar, date, h)
            stat = {
                "n": int(len(arr)),
                "avg_pct": float(arr.mean()),
                "median_pct": float(np.median(arr)),
                "win_rate_pct": float((arr > 0).mean() * 100.0),
                "baseline_pct": baseline,
                "excess_pct": None if baseline is None else float(arr.mean()) - baseline,
                "best": {"ts_code": best["ts_code"], "name": best["name"],
                         "pct": round(best[key], 4)},
                "worst": {"ts_code": worst["ts_code"], "name": worst["name"],
                          "pct": round(worst[key], 4)},
            }
        else:
            stat = {"n": 0, "avg_pct": None, "median_pct": None,
                    "win_rate_pct": None, "baseline_pct": None,
                    "excess_pct": None, "best": None, "worst": None}
        stats[f"t{h}"] = stat

    return {
        "schema_version": SCHEMA_VERSION,
        "trade_date": date,
        "generated_at": _now_iso(),
        "horizons": horizons,
        "n_stocks": len(rows),
        "n_no_base_close": sum(1 for r in rows if "no_base_close" in r["flags"]),
        "stats": stats,
        "stocks": rows,
        "source_file": snapshot.get("source_file"),
    }


# --------------------------------------------------------------------------
# Rolling summary across dates
# --------------------------------------------------------------------------

def summarize_results(results: List[dict],
                      horizons: Sequence[int] = DEFAULT_HORIZONS,
                      top_n: int = 5) -> dict:
    """Cross-date rolling summary of verify_snapshot outputs."""
    horizons = sorted(set(int(h) for h in horizons))
    per_date: List[dict] = []
    pooled: Dict[int, List[float]] = {h: [] for h in horizons}
    pooled_excess: Dict[int, List[float]] = {h: [] for h in horizons}
    all_obs: List[dict] = []

    for res in sorted(results, key=lambda r: r["trade_date"]):
        row = {"trade_date": res["trade_date"], "n_stocks": res["n_stocks"]}
        for h in horizons:
            st = res["stats"].get(f"t{h}") or {}
            row[f"t{h}_avg_pct"] = st.get("avg_pct")
            row[f"t{h}_win_rate_pct"] = st.get("win_rate_pct")
            row[f"t{h}_baseline_pct"] = st.get("baseline_pct")
            row[f"t{h}_excess_pct"] = st.get("excess_pct")
            if st.get("avg_pct") is not None:
                pooled[h].append(st["avg_pct"])
            if st.get("excess_pct") is not None:
                pooled_excess[h].append(st["excess_pct"])
        st1 = res["stats"].get(f"t{horizons[0]}") or {}
        row["best"], row["worst"] = st1.get("best"), st1.get("worst")
        per_date.append(row)
        for s in res.get("stocks", []):
            for h in horizons:
                if s.get(f"t{h}_pct") is not None:
                    all_obs.append({"trade_date": res["trade_date"],
                                    "ts_code": s["ts_code"], "name": s["name"],
                                    "horizon": h, "pct": s[f"t{h}_pct"]})

    overall: Dict[str, dict] = {}
    for h in horizons:
        obs = [o["pct"] for o in all_obs if o["horizon"] == h]
        arr = np.array(obs, dtype=float) if obs else np.array([])
        overall[f"t{h}"] = {
            "n_dates": len(pooled[h]),
            "n_observations": int(arr.size),
            "avg_pct": float(arr.mean()) if arr.size else None,
            "win_rate_pct": float((arr > 0).mean() * 100.0) if arr.size else None,
            "avg_daily_avg_pct": float(np.mean(pooled[h])) if pooled[h] else None,
            "avg_excess_vs_market_pct":
                float(np.mean(pooled_excess[h])) if pooled_excess[h] else None,
            "share_dates_beating_market_pct":
                float(np.mean([1.0 if e > 0 else 0.0 for e in pooled_excess[h]]) * 100.0)
                if pooled_excess[h] else None,
        }

    # Top/bottom single-stock observations on the longest horizon.
    h_star = horizons[-1]
    obs_star = sorted((o for o in all_obs if o["horizon"] == h_star),
                      key=lambda o: o["pct"])
    fmt = lambda o: {"trade_date": o["trade_date"], "ts_code": o["ts_code"],
                     "name": o["name"], "pct": round(o["pct"], 4)}
    return {
        "horizons": horizons,
        "n_dates": len(per_date),
        "date_range": [per_date[0]["trade_date"], per_date[-1]["trade_date"]]
            if per_date else None,
        "overall": overall,
        f"top_performers_t{h_star}": [fmt(o) for o in obs_star[-top_n:][::-1]],
        f"bottom_performers_t{h_star}": [fmt(o) for o in obs_star[:top_n]],
        "per_date": per_date,
    }
