#!/usr/bin/env python3
"""Validation suite for Research Panel v2 (plan Phase 0 acceptance tests).

Checks (each returns a dict with status PASS / FAIL / WARN and evidence):
  structure        unique keys, no forward columns, revisable fields only lagged, calendar coverage
  pit_alignment    daily_basic close == daily close on the same row (exact-date join)
  dv_lag           dv_ttm_lag1(T) == raw dv_ttm(previous official session)
  lookahead_shift  |IC(f_T)| vs |IC(f_T-1)| for base fields, with a planted-leak positive control
  prod_crosscheck  panel values == production caches (extension rows; lab rows on overlap dates)
  universe         rule compliance, counts/churn, daily_basic coverage, PIT-source agreement
  survivorship     delisted securities present before delisting / estimate of excluded rows

Read-only. Writes the report next to the panel.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_panel_v2 as rp  # noqa: E402

ST_RE = re.compile(r"ST|退")
LEAK_GAP = 0.03  # |IC_now| - |IC_lag| above this on a slow base field => suspected leak


def _status(ok: bool, warn: bool = False) -> str:
    return "PASS" if ok and not warn else ("WARN" if ok else "FAIL")


def check_structure(panel: pd.DataFrame, sessions: list[str]) -> dict[str, Any]:
    dup = int(panel.duplicated(["ts_code", "trade_date"]).sum())
    forbidden = [c for c in panel.columns if any(t in c.lower() for t in rp.FORBIDDEN_COLUMN_TOKENS)]
    raw_revisable = [c for c in rp.DB_REVISABLE_COLS if c in panel.columns]
    dates = sorted(panel["trade_date"].unique())
    in_range = [d for d in sessions if dates[0] <= d <= dates[-1]]
    missing = [d for d in in_range if d not in set(dates)]
    off_calendar = [d for d in dates if d not in set(sessions)]
    ok = dup == 0 and not forbidden and not raw_revisable and not off_calendar
    return {"status": _status(ok, warn=bool(missing)), "duplicate_keys": dup, "forbidden_columns": forbidden,
            "raw_revisable_columns_present": raw_revisable, "off_calendar_dates": off_calendar,
            "official_sessions_in_range": len(in_range), "panel_dates": len(dates),
            "missing_official_sessions": missing}


def check_pit_alignment(panel: pd.DataFrame) -> dict[str, Any]:
    both = panel[["close", "db_close", "src_block"]].dropna()
    rel = (both["close"].astype("float64") - both["db_close"]).abs() / both["db_close"].abs().clip(lower=1e-9)
    bad = rel > 1e-3  # float32 storage of close; a one-day misalignment moves ~1% on average
    by_block = {b: {"rows": int(len(g)), "mismatch_share": float(bad[g.index].mean())}
                for b, g in both.groupby("src_block")}
    coverage = float(panel["db_close"].notna().mean())
    share = float(bad.mean()) if len(both) else 1.0
    return {"status": _status(share <= 0.005), "rows_compared": int(len(both)), "mismatch_share": share,
            "by_block": by_block, "daily_basic_row_coverage": coverage}


def check_dv_lag(panel: pd.DataFrame, sessions: list[str], sample_dates: int = 20) -> dict[str, Any]:
    dates = sorted(panel["trade_date"].unique())
    rng = np.random.default_rng(7)
    picks = sorted(rng.choice(dates[1:], size=min(sample_dates, len(dates) - 1), replace=False))
    pos = {d: i for i, d in enumerate(sessions)}
    worst = 0.0
    checked = 0
    for d in picks:
        prev = sessions[pos[d] - 1]
        raw = pd.read_parquet(rp.DAILY_BASIC_DIR / f"daily_basic_{prev}.parquet", columns=["ts_code", "dv_ttm"])
        day = panel.loc[panel["trade_date"] == d, ["ts_code", "dv_ttm_lag1"]].merge(raw, on="ts_code", how="left")
        a = pd.to_numeric(day["dv_ttm_lag1"], errors="coerce")
        b = pd.to_numeric(day["dv_ttm"], errors="coerce")
        both = a.notna() & b.notna()
        nan_mismatch = int((a.isna() != b.isna()).sum())
        diff = float((a[both] - b[both]).abs().max()) if both.any() else 0.0
        worst = max(worst, diff, float(nan_mismatch))
        checked += int(len(day))
    return {"status": _status(worst <= 1e-4), "sample_dates": [str(x) for x in picks], "rows_checked": checked,
            "max_abs_diff_or_nan_mismatch": worst}


def _daily_spearman(x: pd.Series, y: pd.Series, by: pd.Series, min_n: int = 100) -> float:
    df = pd.DataFrame({"x": x, "y": y, "d": by}).dropna()
    ics = []
    for _, g in df.groupby("d"):
        if len(g) >= min_n:
            ics.append(g["x"].rank().corr(g["y"].rank()))
    return float(np.nanmean(ics)) if ics else float("nan")


def check_lookahead_shift(panel: pd.DataFrame, sessions: list[str], every: int = 5) -> dict[str, Any]:
    pos = {d: i for i, d in enumerate(sessions)}
    p = panel[["ts_code", "trade_date", "pct_chg", "pb", "turnover_rate_f", "circ_mv", "winner_rate", "rsi_6"]].copy()
    p["_pos"] = p["trade_date"].map(pos)
    p = p.sort_values(["ts_code", "_pos"])
    g = p.groupby("ts_code")
    r = p["pct_chg"].astype("float64") / 100.0
    # Forward 5-session return T+1..T+5 from the panel's own rows; require 5 consecutive official sessions.
    fwd = np.ones(len(p))
    valid = np.ones(len(p), dtype=bool)
    for k in range(1, 6):
        nxt_pos = g["_pos"].shift(-k)
        valid &= (nxt_pos - p["_pos"]).to_numpy() == k
        fwd *= (1 + g["pct_chg"].shift(-k).astype("float64") / 100.0).to_numpy()
    p["fwd5"] = np.where(valid, fwd - 1, np.nan)
    p["leak_control"] = g["pct_chg"].shift(-1).where((g["_pos"].shift(-1) - p["_pos"]) == 1)  # planted leak
    p["log_mv"] = np.log(p["circ_mv"].astype("float64").clip(lower=1))
    p["_lr"] = np.log1p(r)
    p["mom20"] = np.expm1(p.groupby("ts_code")["_lr"].transform(lambda s: s.rolling(20).sum()))
    keep_dates = set(sorted(p["trade_date"].unique())[::every])
    factors = ["pb", "turnover_rate_f", "log_mv", "winner_rate", "rsi_6", "mom20", "leak_control"]
    out = {}
    for f in factors:
        lag = p.groupby("ts_code")[f].shift(1).where((p["_pos"] - g["_pos"].shift(1)) == 1)
        mask = p["trade_date"].isin(keep_dates)
        ic_now = _daily_spearman(p.loc[mask, f], p.loc[mask, "fwd5"], p.loc[mask, "trade_date"])
        ic_lag = _daily_spearman(lag[mask], p.loc[mask, "fwd5"], p.loc[mask, "trade_date"])
        gap = abs(ic_now) - abs(ic_lag)
        out[f] = {"ic_now": round(ic_now, 4), "ic_lag1": round(ic_lag, 4), "abs_gap": round(gap, 4),
                  "suspected_leak": bool(gap > LEAK_GAP)}
    base = {k: v for k, v in out.items() if k != "leak_control"}
    control_fires = out["leak_control"]["suspected_leak"]
    ok = control_fires and not any(v["suspected_leak"] for v in base.values())
    return {"status": _status(ok), "every_nth_date": every, "leak_gap_threshold": LEAK_GAP,
            "positive_control_detected": control_fires, "factors": out}


def check_prod_crosscheck(panel: pd.DataFrame, n_dates: int = 6) -> dict[str, Any]:
    res: dict[str, Any] = {"ext": {}, "lab_overlap": {}}
    ext_dates = sorted(panel.loc[panel["src_block"] == "prod_cache_ext", "trade_date"].unique())
    lab_dates = sorted(d for d in panel.loc[panel["src_block"] == "lab_long_panel", "trade_date"].unique()
                       if (rp.BACKTEST_CACHE / f"daily_{d}.parquet").exists())
    pick = lambda ds: [ds[i] for i in np.linspace(0, len(ds) - 1, min(n_dates, len(ds))).astype(int)] if ds else []
    fails = 0
    for block, dates in (("ext", pick(ext_dates)), ("lab_overlap", pick(lab_dates))):
        for d in dates:
            row = panel[panel["trade_date"] == d]
            entry = {}
            for src, cols in (("daily", rp.PRICE_COLS), ("stk_factor", rp.TECH_COLS), ("cyq_perf", rp.CHIP_COLS)):
                f = rp.BACKTEST_CACHE / f"{src}_{d}.parquet"
                if not f.exists():
                    entry[src] = "cache_missing"
                    continue
                c = pd.read_parquet(f, columns=["ts_code", *cols])
                m = row[["ts_code", *cols]].merge(c, on="ts_code", how="left", suffixes=("", "_c"))
                stats = {}
                for col in cols:
                    a, b = m[col].astype("float64"), m[f"{col}_c"].astype("float64")
                    both = a.notna() & b.notna()
                    rel = ((a - b).abs() / b.abs().clip(lower=1e-6))[both]
                    stats[col] = float((rel > 1e-4).mean()) if both.any() else None
                worst = max((v for v in stats.values() if v is not None), default=0.0)
                entry[src] = {"rows": int(len(m)), "missing_in_cache": int(m[f"{cols[0]}_c"].isna().sum()),
                              "worst_col_mismatch_share": worst,
                              "worst_col": max(stats, key=lambda k: stats[k] or 0) if stats else None}
                # Production-cache rows must match exactly; lab price rows must too (same raw source).
                if (block == "ext" or src == "daily") and worst > 1e-3:
                    fails += 1
            res[block][d] = entry
    res["status"] = _status(fails == 0)
    res["note"] = ("lab stk_factor/cyq_perf rows are expected to differ from today's caches "
                   "(provider recomputes rolling-window indicators; PANEL_FULL_README §5).")
    return res


def check_universe(panel: pd.DataFrame, manifest: dict[str, Any]) -> dict[str, Any]:
    by_date = panel.groupby("trade_date")
    counts = by_date.size()
    codes = by_date["ts_code"].apply(set)
    churn = [len(codes.iloc[i] ^ codes.iloc[i - 1]) / max(1, len(codes.iloc[i - 1])) for i in range(1, len(codes))]
    rules = {
        "listing_days_lt_252": int((pd.to_numeric(panel["listing_days"], errors="coerce") < 252).sum()),
        "close_lt_2": int((panel["close"] < 2).sum()),
        "no_trade_rows": int(((panel["vol"] <= 0) | (panel["amount"] <= 0)).sum()),
        "st_or_delisting_named": int(panel["name"].fillna("").str.contains(ST_RE).sum()),
        "excluded_board_rows_bse": int(panel["ts_code"].str.endswith(".BJ").sum()),
        "excluded_board_rows_star": int(panel["ts_code"].str.startswith(("688", "689")).sum()),
        "rows_before_list_date": int((panel["list_date"].astype(str) > panel["trade_date"]).sum()),
        "rows_on_or_after_delist_date": int((panel["delist_date"].notna()
                                              & (panel["delist_date"].astype(str) <= panel["trade_date"])).sum()),
    }
    db_cov = by_date["db_close"].apply(lambda s: s.notna().mean())
    src_counts = panel.groupby("universe_source")["trade_date"].nunique().to_dict()
    hard = {k: v for k, v in rules.items() if v and not k.startswith("excluded_board")}
    warn = bool(rules["excluded_board_rows_bse"] or rules["excluded_board_rows_star"]) or float(db_cov.min()) < 0.99
    ext = manifest.get("extension", {})
    return {"status": _status(not hard, warn=warn), "rule_violations": rules,
            "count_min": int(counts.min()), "count_median": int(counts.median()), "count_max": int(counts.max()),
            "daily_churn_median": float(np.median(churn)) if churn else None,
            "daily_churn_p99": float(np.quantile(churn, 0.99)) if churn else None,
            "daily_basic_coverage_min": float(db_cov.min()), "daily_basic_coverage_min_date": str(db_cov.idxmin()),
            "daily_basic_coverage_median": float(db_cov.median()),
            "dates_by_universe_source": src_counts,
            "recomputed_missing_pit_dates": ext.get("missing_pit", []),
            "recomputed_suspension_conflicts": {k: v for k, v in (ext.get("suspension_conflicts") or {}).items() if v}}


def check_survivorship(panel: pd.DataFrame, sessions: list[str]) -> dict[str, Any]:
    static_paths = sorted(rp.STOCK_WORKING.glob(rp.STATIC_BUNDLE_GLOB), key=lambda p: p.stat().st_mtime)
    st = pd.read_parquet(static_paths[-1], columns=["ts_code", "name", "list_date", "delist_date",
                                                    "current_list_status", "market_board"])
    first, last = panel["trade_date"].min(), panel["trade_date"].max()
    dl = st[(st["current_list_status"] == "D") & st["delist_date"].notna()
            & (st["delist_date"].astype(str) > first) & (st["delist_date"].astype(str) <= last)]
    dl = dl[~dl["ts_code"].str.endswith(".BJ")]
    in_panel = set(panel["ts_code"])
    present = dl[dl["ts_code"].isin(in_panel)]
    # Estimate rows a survivorship-free universe would have held for delisted names:
    # sample one session per month; liquidity proxy = turnover_rate% * circ_mv (万元),
    # eligible if listed >= 252 days and proxy >= the day's market median (rule: top 50%).
    months = sorted({d[:6]: d for d in sessions if first <= d <= last}.values())
    st_sets = rp.load_stock_st_sets()
    est_rows = 0
    examples: dict[str, int] = {}
    dl_codes = set(dl["ts_code"])
    for d in months:
        db = pd.read_parquet(rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet",
                             columns=["ts_code", "turnover_rate", "circ_mv", "close"])
        db = db[~db["ts_code"].str.endswith(".BJ")]
        db["liq"] = pd.to_numeric(db["turnover_rate"], errors="coerce") * pd.to_numeric(db["circ_mv"], errors="coerce")
        med = db["liq"].median()
        cand = db[db["ts_code"].isin(dl_codes) & (db["liq"] >= med)].merge(dl[["ts_code", "list_date", "name"]],
                                                                            on="ts_code")
        cand = cand[(pd.to_datetime(d) - pd.to_datetime(cand["list_date"].astype(str))).dt.days >= 252]
        cand = cand[(cand["close"] >= 2.0) & ~cand["ts_code"].isin(st_sets.get(d, set()))]
        day_codes = set(panel.loc[panel["trade_date"] == d, "ts_code"])
        miss = cand[~cand["ts_code"].isin(day_codes)]
        est_rows += len(miss)
        for code in miss["ts_code"]:
            examples[code] = examples.get(code, 0) + 1
    per_session = est_rows / max(1, len(months))
    top = sorted(examples.items(), key=lambda kv: -kv[1])[:10]
    names = st.set_index("ts_code")["name"].to_dict()
    share = per_session / panel.groupby("trade_date").size().median()
    ok = (len(present) > 0 or len(dl) == 0) and share <= 0.005
    return {"status": _status(ok), "delisted_in_window_ex_bse": int(len(dl)),
            "delisted_present_in_panel": int(len(present)),
            "est_missing_names_per_session": round(per_session, 1),
            "est_missing_share_of_universe": round(share, 4), "pass_threshold_share": 0.005,
            "sampled_sessions": len(months),
            "top_excluded_examples": [{"ts_code": c, "name": names.get(c), "sampled_sessions_eligible": n}
                                      for c, n in top],
            "note": "Eligibility proxy: listed>=252d, close>=2, not on PIT stock_st, turnover*circ_mv >= day median. "
                    "Proxy ignores the 60-day amount window and trading-ratio rule, so residuals are an upper bound."}


def run(panel_path: Path) -> dict[str, Any]:
    manifest_path = panel_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    panel = pd.read_parquet(panel_path)
    sessions = rp.official_sessions(manifest["as_of"])
    checks = {
        "structure": check_structure(panel, sessions),
        "pit_alignment": check_pit_alignment(panel),
        "dv_lag": check_dv_lag(panel, sessions),
        "lookahead_shift": check_lookahead_shift(panel, sessions),
        "prod_crosscheck": check_prod_crosscheck(panel),
        "universe": check_universe(panel, manifest),
        "survivorship": check_survivorship(panel, sessions),
    }
    report = {"panel": str(panel_path), "panel_sha256": manifest["sha256"], "schema_version": manifest["schema_version"],
              "summary": {k: v["status"] for k, v in checks.items()}, "checks": checks}
    out = panel_path.with_suffix(".validation.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate Research Panel v2")
    ap.add_argument("--panel", default=str(ROOT / "data/research_panel_v2.parquet"))
    args = ap.parse_args(argv)
    report = run(Path(args.panel))
    print(json.dumps(report["summary"], indent=2))
    return 0 if all(v != "FAIL" for v in report["summary"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
