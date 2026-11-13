#!/usr/bin/env python3
"""Research Panel v2 — the single base panel for greenfield factor research.

Plan: docs/superpowers/plans/2026-10-10-greenfield-factor-mining-plan.md, Phase 0 / D3.

Scope (deliberately a *base* layer):
- raw daily OHLCV, stk_factor technicals, cyq_perf chips, universe fields;
- daily_basic valuation / size / free-float turnover from data/daily_basic_hist;
- static classification columns (documented as non-PIT);
- no derived g_* factors and no forward labels. Labels are D4; factors are
  mined later through the gauntlet and must be computed from this panel.

Sources:
- 20210129..20260702: the lab long panel (data/factors/greenfield_multifactor_panel_full.parquet),
  raw-column subset only. It is already universe-filtered (dynamic universe).
- 20260703..as-of: production caches (stock_data/03-working/backtest_cache daily/stk_factor/cyq_perf),
  restricted to universe members. Universe = data/universe snapshots where they exist,
  else recomputed with the identical build_universe.py rules over exact-date PIT snapshots.

Read-only on every production path. Writes only under factor_factory/data/research_panel_v2*.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
STOCK_WORKING = WORKSPACE / "stock_data/03-working"
sys.path.insert(0, str(ROOT / "scripts"))

import build_universe as bu  # noqa: E402

SCHEMA_VERSION = "research_panel_v2.2"
LONG_PANEL = ROOT / "data/factors/greenfield_multifactor_panel_full.parquet"
DAILY_BASIC_DIR = ROOT / "data/daily_basic_hist"
UNIVERSE_DIR = ROOT / "data/universe"
BACKTEST_CACHE = STOCK_WORKING / "backtest_cache"
STATIC_BUNDLE_GLOB = "factor_attribution/cache/cfg_*/pit_fetch/pit_static_bundle.parquet"
SW_MEMBER_GLOB = "factor_attribution/cache/cfg_*/pit_fetch/sw2021_member_all.parquet"
# Exact-date PIT universe snapshots, in priority order (first hit wins per date).
PIT_UNIVERSE_DIRS = (
    STOCK_WORKING / "fundamental_cache/pit_market",
    STOCK_WORKING / "strategy_research/short_track/materialized/pit_universe",
)
PIT_UNIVERSE_GLOB = "factor_attribution/cache/cfg_*/pit_fetch"

LONG_PANEL_LAST = "20260702"

PRICE_COLS = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
TECH_COLS = ["macd_dif", "macd_dea", "macd", "kdj_k", "kdj_d", "kdj_j", "rsi_6", "rsi_12", "rsi_24",
             "boll_upper", "boll_mid", "boll_lower", "cci"]
CHIP_COLS = ["cost_5pct", "cost_15pct", "cost_50pct", "cost_85pct", "cost_95pct", "weight_avg", "winner_rate"]
UNIVERSE_COLS = ["avg_amount_lookback", "trading_ratio_lookback", "listing_days", "liquidity_threshold",
                 "universe_flag"]
LEGACY_CLASS_COLS = ["industry", "market"]
# daily_basic fields carried as exact-date PIT values (verified identical to real
# PIT captures on 29 overlap days; QUALITY_REPORT.md).
DB_PIT_COLS = ["turnover_rate", "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
               "total_share", "float_share", "free_share", "total_mv", "circ_mv"]
# Dividend fields are vendor-revised after the fact (8/29 overlap days differ).
# Rule: never expose the same-day value; expose only the previous official
# session's value, and flag the column as revisable in the manifest.
DB_REVISABLE_COLS = ["dv_ratio", "dv_ttm"]
DV_LAG_SESSIONS = 1
FORBIDDEN_COLUMN_TOKENS = ("next_", "fwd", "forward", "label", "future", "target")

COLUMN_NOTES = {
    "dv_ttm_lag1": "dv_ttm of the previous official session. Vendor revises TTM dividends after the fact "
                   "(<=0.08% of names on 8/29 overlap days); not strictly PIT. Do not use for ex-dividend event timing.",
    "dv_ratio_lag1": "Same rule as dv_ttm_lag1.",
    "industry": "Legacy tushare industry from a 2026-03-13 static map; current classification applied to history (non-PIT).",
    "sw2021_l1": "Current SW2021 L1 membership with in_date<=trade_date; reclassification history unavailable (non-PIT).",
    "market": "Board label from the legacy static map (non-PIT).",
    "db_close": "daily_basic close, kept only for the exact-date alignment check against daily close.",
    "adj_factor": "Only present for production-cache rows (20260703+); NaN for lab rows.",
    "src_block": "lab_long_panel | prod_cache_gapfill | prod_cache_ext | delisted_backfill",
    "universe_source": "lab_long_panel | ff_universe_snapshot | recomputed_build_universe_rules | "
                       "delisted_rule_lab_threshold (build_universe rules, lab survivor-set threshold, "
                       "PIT name from namechange)",
}


@dataclass(frozen=True)
class BuildConfig:
    as_of: str
    out_path: Path
    manifest_path: Path


def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def official_sessions(as_of: str) -> list[str]:
    """Official sessions = the RDS trade_cal-driven daily_basic backfill dates."""
    dates = sorted(p.stem.split("_")[-1] for p in DAILY_BASIC_DIR.glob("daily_basic_*.parquet"))
    return [d for d in dates if d <= as_of]


def lag_by_session(frame: pd.DataFrame, cols: list[str], sessions: list[str], lag: int) -> pd.DataFrame:
    """Per-stock value from `lag` official sessions earlier (NaN if absent that session).

    Lags on the official calendar, not on the stock's own rows, so a suspension
    gap never pulls a value from further back than `lag` sessions.
    """
    pos = {d: i for i, d in enumerate(sessions)}
    src = frame[["ts_code", "trade_date", *cols]].copy()
    src["_pos"] = src["trade_date"].map(pos)
    src = src.dropna(subset=["_pos"])
    src["_pos"] = src["_pos"].astype(int) + lag
    src = src[src["_pos"] < len(sessions)]
    src["trade_date"] = np.asarray(sessions, dtype=object)[src["_pos"].to_numpy()]
    return src.drop(columns="_pos").rename(columns={c: f"{c}_lag{lag}" for c in cols})


def load_daily_basic(sessions: list[str]) -> pd.DataFrame:
    frames = []
    cols = ["ts_code", "trade_date", "close", *DB_PIT_COLS, *DB_REVISABLE_COLS]
    for d in sessions:
        f = pd.read_parquet(DAILY_BASIC_DIR / f"daily_basic_{d}.parquet", columns=cols)
        f["trade_date"] = f["trade_date"].astype(str)
        if set(f["trade_date"]) != {d}:
            raise RuntimeError(f"daily_basic payload date mismatch for {d}")
        frames.append(f)
    out = pd.concat(frames, ignore_index=True)
    if out.duplicated(["ts_code", "trade_date"]).any():
        raise RuntimeError("daily_basic duplicate keys")
    for c in [*DB_PIT_COLS, *DB_REVISABLE_COLS, "close"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    return out.rename(columns={"close": "db_close"})


def load_lab_block() -> pd.DataFrame:
    cols = ["ts_code", "trade_date", "name", *PRICE_COLS, *TECH_COLS, *CHIP_COLS, *UNIVERSE_COLS, *LEGACY_CLASS_COLS]
    df = pd.read_parquet(LONG_PANEL, columns=cols)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df[df["trade_date"] <= LONG_PANEL_LAST]
    df["src_block"] = "lab_long_panel"
    df["universe_source"] = "lab_long_panel"
    df["adj_factor"] = np.nan
    return df


def _pit_universe_dirs() -> list[Path]:
    return [*PIT_UNIVERSE_DIRS, *sorted(STOCK_WORKING.glob(PIT_UNIVERSE_GLOB))]


def load_pit_snapshot(trade_date: str) -> tuple[pd.DataFrame | None, str | None]:
    required = {"trade_date", "ts_code", "name", "list_status_at_date", "is_st", "is_suspended", "listing_days",
                "universe_flag", "tradable", "used_proxy", "completeness"}
    for d in _pit_universe_dirs():
        p = d / f"universe_{trade_date}.parquet"
        if not p.exists():
            continue
        f = pd.read_parquet(p)
        if "tradable" not in f.columns and "universe_flag" in f.columns and d.name == "pit_fetch":
            # factor_attribution_engine sets tradable = universe_flag but drops the column on write;
            # identity verified on all 170,865 pit_market rows and 4 overlap dates (2026-10-14).
            f["tradable"] = f["universe_flag"]
        if not required.issubset(f.columns):
            continue
        if set(f["trade_date"].astype(str).str.replace("-", "", regex=False)) != {trade_date}:
            continue
        if f["used_proxy"].fillna(True).astype(bool).any() or not (f["completeness"].astype(str) == "complete").all():
            continue
        if f["ts_code"].astype(str).duplicated().any():
            continue
        f = f[list(required)].copy()
        f["trade_date"] = trade_date
        f["ts_code"] = f["ts_code"].astype(str)
        return f, str(p)
    return None, None


def recompute_universe(target_dates: list[str], daily_dates: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Identical rules to build_universe.compute_universe_snapshots, over exact-date PIT snapshots."""
    cfg = bu.load_runtime_config(ROOT)
    pairs = [(d, BACKTEST_CACHE / f"daily_{d}.parquet") for d in daily_dates]
    daily = bu.load_all_daily_panel(pairs)
    daily["trade_date"] = daily["trade_date"].dt.strftime("%Y%m%d")
    g = daily.groupby("ts_code", group_keys=False)
    lb = cfg.avg_amount_lookback_days
    daily["is_trading_day"] = ((daily["amount"] > 0) & (daily["vol"] > 0)).astype(int)
    daily["avg_amount_lookback"] = g["amount"].transform(lambda s: s.rolling(lb, min_periods=min(20, lb)).mean())
    daily["trading_ratio_lookback"] = g["is_trading_day"].transform(
        lambda s: s.rolling(lb, min_periods=min(20, lb)).mean())
    out, meta = [], {"dates": {}, "missing_pit": [], "suspension_conflicts": {}}
    for d in target_dates:
        snap, src = load_pit_snapshot(d)
        if snap is None:
            meta["missing_pit"].append(d)
            continue
        day = daily[daily["trade_date"] == d].merge(snap, on=["trade_date", "ts_code"], how="inner",
                                                    validate="one_to_one")
        reported = day["is_suspended"].fillna(True).astype(bool)
        no_trade = (day["amount"] <= 0) | (day["vol"] <= 0)
        meta["suspension_conflicts"][d] = int((reported != no_trade).sum())
        listed = (day["list_status_at_date"].astype(str) == "L") & (
            pd.to_numeric(day["listing_days"], errors="coerce").fillna(-1) >= cfg.minimum_listing_days)
        pre = day[listed & (day["close"].fillna(0) >= cfg.min_price)
                  & (pd.to_numeric(day["universe_flag"], errors="coerce") > 0)
                  & (pd.to_numeric(day["tradable"], errors="coerce") > 0)
                  & (~day["is_st"].astype(bool)) & (~bu.mark_st_flags(day, cfg.st_name_patterns)) & (~reported)
                  & (day["trading_ratio_lookback"].fillna(0) >= cfg.min_daily_trading_days_ratio)].copy()
        if pre.empty:
            meta["missing_pit"].append(d)
            continue
        thr = float(pre["avg_amount_lookback"].quantile(cfg.avg_amount_percentile_threshold))
        sel = pre[pre["avg_amount_lookback"] >= thr].copy()
        sel["liquidity_threshold"] = thr
        sel["universe_flag"] = 1
        meta["dates"][d] = {"source": src, "selected": int(len(sel)), "prefilter": int(len(pre))}
        out.append(sel[["trade_date", "ts_code", "name", "avg_amount_lookback", "trading_ratio_lookback",
                        "listing_days", "liquidity_threshold", "universe_flag"]])
    uni = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    uni["universe_source"] = "recomputed_build_universe_rules"
    return uni, meta


def load_ff_universe(dates: list[str]) -> pd.DataFrame:
    frames = []
    for d in dates:
        p = UNIVERSE_DIR / f"universe_{d}.parquet"
        if p.exists():
            f = pd.read_parquet(p)
            f["trade_date"] = d
            frames.append(f[["trade_date", "ts_code", "name", "avg_amount_lookback", "trading_ratio_lookback",
                             "listing_days", "liquidity_threshold", "universe_flag"]])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out["universe_source"] = "ff_universe_snapshot"
    return out


def load_ext_block(ext_dates: list[str], sessions: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    ff = load_ff_universe(ext_dates)
    need = [d for d in ext_dates if d not in set(ff["trade_date"])]
    first_idx = sessions.index(need[0]) if need else 0
    daily_dates = [d for d in sessions[max(0, first_idx - 80):] if d <= ext_dates[-1]
                   and (BACKTEST_CACHE / f"daily_{d}.parquet").exists()]
    rec, meta = recompute_universe(need, daily_dates) if need else (pd.DataFrame(), {"dates": {}, "missing_pit": []})
    uni = pd.concat([ff, rec], ignore_index=True)
    rows = []
    for d in ext_dates:
        u = uni[uni["trade_date"] == d]
        if u.empty:
            continue
        daily = pd.read_parquet(BACKTEST_CACHE / f"daily_{d}.parquet", columns=["ts_code", "trade_date", *PRICE_COLS])
        stk = pd.read_parquet(BACKTEST_CACHE / f"stk_factor_{d}.parquet", columns=["ts_code", "adj_factor", *TECH_COLS])
        cyq = pd.read_parquet(BACKTEST_CACHE / f"cyq_perf_{d}.parquet", columns=["ts_code", *CHIP_COLS])
        daily["trade_date"] = daily["trade_date"].astype(str)
        if set(daily["trade_date"]) != {d}:
            raise RuntimeError(f"daily cache payload date mismatch for {d}")
        day = (u.merge(daily, on=["trade_date", "ts_code"], how="inner", validate="one_to_one")
                .merge(stk, on="ts_code", how="left", validate="one_to_one")
                .merge(cyq, on="ts_code", how="left", validate="one_to_one"))
        rows.append(day)
    ext = pd.concat(rows, ignore_index=True)
    ext["src_block"] = "prod_cache_ext"
    meta["ext_dates_built"] = sorted(ext["trade_date"].unique().tolist())
    meta["ext_dates_without_universe"] = [d for d in ext_dates if d not in set(ext["trade_date"])]
    return ext, meta


DELISTED_DIR = ROOT / "data/delisted_hist"


def names_at_dates(namechange: pd.DataFrame, dates: pd.Series) -> pd.Series:
    """PIT security name on each date from namechange intervals (latest start_date <= date wins)."""
    nc = namechange[["name", "start_date", "end_date"]].dropna(subset=["start_date"]).copy()
    nc["start_date"] = nc["start_date"].astype(str)
    nc = nc.sort_values("start_date")
    starts = nc["start_date"].to_numpy()
    idx = np.searchsorted(starts, dates.astype(str).to_numpy(), side="right") - 1
    names = np.where(idx >= 0, nc["name"].to_numpy()[np.clip(idx, 0, None)], None)
    ends = np.where(idx >= 0, nc["end_date"].fillna("").astype(str).to_numpy()[np.clip(idx, 0, None)], "")
    expired = (ends != "") & (ends < dates.astype(str).to_numpy())
    return pd.Series(np.where(expired, None, names), index=dates.index)


def load_stock_st_sets() -> dict[str, set[str]]:
    """PIT ST/*ST list per trade date (stock_st backfill), the primary ST source for delisted names."""
    out = {}
    for p in (ROOT / "data/price_hist/stock_st").glob("stock_st_*.parquet"):
        f = pd.read_parquet(p, columns=["ts_code"]) if p.stat().st_size else pd.DataFrame(columns=["ts_code"])
        out[p.stem.split("_")[-1]] = set(f["ts_code"].astype(str))
    return out


def delisted_rule_rows(code_daily: pd.DataFrame, names: pd.Series, list_date: str, delist_date: str,
                       thresholds: dict[str, float], cfg, st_sets: dict[str, set[str]] | None = None,
                       code: str = "") -> pd.DataFrame:
    """build_universe.py rules for one security, using the lab's per-date liquidity threshold.

    Rolling windows run over the security's own rows, exactly as build_universe does.
    """
    d = code_daily.sort_values("trade_date").reset_index(drop=True).copy()
    d["trade_date"] = d["trade_date"].astype(str)
    for c in ("amount", "vol", "close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    lb = cfg.avg_amount_lookback_days
    d["is_trading_day"] = ((d["amount"] > 0) & (d["vol"] > 0)).astype(int)
    d["avg_amount_lookback"] = d["amount"].fillna(0.0).rolling(lb, min_periods=min(20, lb)).mean()
    d["trading_ratio_lookback"] = d["is_trading_day"].rolling(lb, min_periods=min(20, lb)).mean()
    d["listing_days"] = (pd.to_datetime(d["trade_date"]) - pd.to_datetime(str(list_date))).dt.days
    d["name"] = names.to_numpy()  # PIT names aligned with code_daily sorted by trade_date
    d["liquidity_threshold"] = d["trade_date"].map(thresholds)
    st = bu.mark_st_flags(d.assign(name=d["name"].fillna("")), cfg.st_name_patterns)
    st_sets = st_sets or {}
    on_list = d["trade_date"].map(lambda x: code in st_sets[x] if x in st_sets else None)
    # ST is known if stock_st covers the date or a PIT name exists; unknown -> excluded.
    known = on_list.notna() | d["name"].notna()
    st = st | on_list.fillna(False).astype(bool)
    ok = (d["liquidity_threshold"].notna() & (d["trade_date"] < str(delist_date))
          & (d["is_trading_day"] == 1) & (d["listing_days"] >= cfg.minimum_listing_days)
          & (d["close"] >= cfg.min_price) & known & ~st
          & (d["trading_ratio_lookback"].fillna(0) >= cfg.min_daily_trading_days_ratio)
          & (d["avg_amount_lookback"] >= d["liquidity_threshold"]))
    out = d.loc[ok].copy()
    out["universe_flag"] = 1
    return out


def load_delisted_block(lab: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    cfg = bu.load_runtime_config(ROOT)
    thresholds = lab.groupby("trade_date")["liquidity_threshold"].first().astype("float64").to_dict()
    static_paths = sorted(STOCK_WORKING.glob(STATIC_BUNDLE_GLOB), key=lambda p: p.stat().st_mtime)
    st = pd.read_parquet(static_paths[-1], columns=["ts_code", "list_date", "delist_date"]).set_index("ts_code")
    st_sets = load_stock_st_sets()
    rows, meta = [], {"codes": 0, "codes_with_rows": 0, "missing": {}, "near_threshold_rows": 0,
                      "stock_st_dates": len(st_sets)}
    for path in sorted((DELISTED_DIR / "daily").glob("*.parquet")):
        code = path.stem
        meta["codes"] += 1
        nc_path = DELISTED_DIR / "namechange" / f"{code}.parquet"
        if code not in st.index:
            meta["missing"][code] = "static_missing"
            continue
        daily = pd.read_parquet(path)
        if daily.empty:
            continue
        daily = daily.sort_values("trade_date").reset_index(drop=True)
        if nc_path.exists():
            names = names_at_dates(pd.read_parquet(nc_path), daily["trade_date"].astype(str))
        else:
            meta["missing"][code] = "namechange_missing_stock_st_only"
            names = pd.Series([None] * len(daily), index=daily.index)
        sel = delisted_rule_rows(daily, names, st.at[code, "list_date"], st.at[code, "delist_date"], thresholds,
                                 cfg, st_sets=st_sets, code=code)
        if sel.empty:
            continue
        rel = sel["avg_amount_lookback"] / sel["liquidity_threshold"]
        meta["near_threshold_rows"] += int((rel < 1.05).sum())
        for api, cols in (("stk_factor", ["adj_factor", *TECH_COLS]), ("cyq_perf", CHIP_COLS)):
            ap = DELISTED_DIR / api / f"{code}.parquet"
            if ap.exists():
                x = pd.read_parquet(ap)
                if not x.empty:
                    x["trade_date"] = x["trade_date"].astype(str)
                    keep = [c for c in cols if c in x.columns]
                    sel = sel.merge(x[["trade_date", *keep]].drop_duplicates("trade_date"), on="trade_date", how="left")
        meta["codes_with_rows"] += 1
        rows.append(sel)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    keep = ["ts_code", "trade_date", "name", *PRICE_COLS, *[c for c in TECH_COLS + CHIP_COLS + ["adj_factor"]
                                                          if c in out.columns], *UNIVERSE_COLS]
    out = out[keep].copy()
    out["src_block"] = "delisted_backfill"
    out["universe_source"] = "delisted_rule_lab_threshold"
    meta["rows"] = int(len(out))
    return out, meta


def attach_static(panel: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    static_paths = sorted(STOCK_WORKING.glob(STATIC_BUNDLE_GLOB), key=lambda p: p.stat().st_mtime)
    sw_paths = sorted(STOCK_WORKING.glob(SW_MEMBER_GLOB), key=lambda p: p.stat().st_mtime)
    meta: dict[str, Any] = {}
    if static_paths:
        st = pd.read_parquet(static_paths[-1], columns=["ts_code", "list_date", "delist_date", "market_board"])
        panel = panel.merge(st, on="ts_code", how="left", validate="many_to_one")
        meta["static_bundle"] = str(static_paths[-1])
    if sw_paths:
        m = pd.read_parquet(sw_paths[-1], columns=["ts_code", "l1_name", "in_date", "out_date"])
        m = m.drop_duplicates("ts_code", keep="last")
        panel = panel.merge(m.rename(columns={"l1_name": "_sw_l1", "in_date": "_sw_in", "out_date": "_sw_out"}),
                            on="ts_code", how="left")
        ok = panel["_sw_in"].fillna("99999999").astype(str) <= panel["trade_date"]
        ok &= ~(panel["_sw_out"].notna() & (panel["_sw_out"].astype(str) < panel["trade_date"]))
        panel["sw2021_l1"] = panel["_sw_l1"].where(ok)
        panel = panel.drop(columns=["_sw_l1", "_sw_in", "_sw_out"])
        meta["sw2021_member_all"] = str(sw_paths[-1])
    return panel, meta


def git_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(WORKSPACE), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


def build(cfg: BuildConfig) -> dict[str, Any]:
    started = datetime.now(ZoneInfo("Asia/Shanghai"))
    sessions = official_sessions(cfg.as_of)
    lab = load_lab_block()
    lab_dates = set(lab["trade_date"].unique())
    gap_dates = [d for d in sessions if min(lab_dates) <= d <= LONG_PANEL_LAST and d not in lab_dates]
    ext_dates = gap_dates + [d for d in sessions if LONG_PANEL_LAST < d <= cfg.as_of]
    ext, ext_meta = load_ext_block(ext_dates, sessions)
    ext.loc[ext["trade_date"] <= LONG_PANEL_LAST, "src_block"] = "prod_cache_gapfill"
    ext_meta["gap_dates_filled"] = sorted(set(gap_dates) & set(ext["trade_date"]))
    delisted, delisted_meta = load_delisted_block(lab)
    delisted = delisted[~delisted["trade_date"].isin(set(ext["trade_date"]))]  # ext/gap days are full-market already
    # Fill legacy class columns for extension rows from the lab mapping (same static map, keyed by code).
    legacy = lab.drop_duplicates("ts_code", keep="last").set_index("ts_code")[LEGACY_CLASS_COLS]
    for c in LEGACY_CLASS_COLS:
        ext[c] = ext["ts_code"].map(legacy[c])
        delisted[c] = delisted["ts_code"].map(legacy[c])
    panel = pd.concat([lab, ext, delisted], ignore_index=True, sort=False)
    panel["trade_date"] = panel["trade_date"].astype(str)
    panel["ts_code"] = panel["ts_code"].astype(str)
    # universe_rules.yaml include_boards: SH_main, SZ_main, ChiNext (STAR/BSE excluded).
    # build_universe.py never applied this; enforce it uniformly here and record what was dropped.
    excluded_board = panel["ts_code"].str.endswith(".BJ") | panel["ts_code"].str.startswith(("688", "689"))
    board_drop = panel.loc[excluded_board].groupby("universe_source").size().to_dict()
    panel = panel.loc[~excluded_board].reset_index(drop=True)
    if panel.duplicated(["ts_code", "trade_date"]).any():
        raise RuntimeError("duplicate (ts_code, trade_date) after splice")

    db = load_daily_basic(sessions)  # full calendar, so the first panel day still has a lagged value
    lagged = lag_by_session(db, DB_REVISABLE_COLS, sessions, DV_LAG_SESSIONS)
    db_pit = db.drop(columns=DB_REVISABLE_COLS)
    panel = panel.merge(db_pit, on=["ts_code", "trade_date"], how="left", validate="one_to_one")
    panel = panel.merge(lagged, on=["ts_code", "trade_date"], how="left", validate="one_to_one")
    panel, static_meta = attach_static(panel)

    bad = [c for c in panel.columns if any(t in c.lower() for t in FORBIDDEN_COLUMN_TOKENS)]
    if bad:
        raise RuntimeError(f"forward-looking columns in base panel: {bad}")
    for c in panel.columns:
        if panel[c].dtype == "float64" and c not in ("vol", "amount", "total_mv", "circ_mv", "total_share",
                                                      "float_share", "free_share", "avg_amount_lookback",
                                                      "liquidity_threshold"):
            panel[c] = panel[c].astype("float32")
    panel = panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    cfg.out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.out_path.with_suffix(".parquet.tmp")
    panel.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(cfg.out_path)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "built_at": started.isoformat(),
        "finished_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "as_of": cfg.as_of,
        "path": str(cfg.out_path),
        "sha256": sha256_file(cfg.out_path),
        "rows": int(len(panel)),
        "columns": list(panel.columns),
        "dates": int(panel["trade_date"].nunique()),
        "first": str(panel["trade_date"].min()),
        "last": str(panel["trade_date"].max()),
        "symbols": int(panel["ts_code"].nunique()),
        "code_commit": git_commit(),
        "inputs": {
            "long_panel": {"path": str(LONG_PANEL), "sha256": sha256_file(LONG_PANEL), "last": LONG_PANEL_LAST},
            "daily_basic_hist": str(DAILY_BASIC_DIR),
            "backtest_cache": str(BACKTEST_CACHE),
            **static_meta,
        },
        "rules": {
            "dv_lag_sessions": DV_LAG_SESSIONS,
            "revisable_columns_exposed_only_lagged": DB_REVISABLE_COLS,
            "forbidden_column_tokens": list(FORBIDDEN_COLUMN_TOKENS),
            "universe_rules": "factor_factory/config/universe_rules.yaml via build_universe.py",
        },
        "extension": ext_meta,
        "delisted_backfill": delisted_meta,
        "board_filter_dropped_rows": {k: int(v) for k, v in board_drop.items()},
        "column_notes": COLUMN_NOTES,
    }
    cfg.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--as-of", default="20261009")
    ap.add_argument("--out", default=str(ROOT / "data/research_panel_v2.parquet"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    m = build(BuildConfig(as_of=args.as_of, out_path=out, manifest_path=out.with_suffix(".manifest.json")))
    print(json.dumps({k: m[k] for k in ("rows", "dates", "first", "last", "symbols", "sha256")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
