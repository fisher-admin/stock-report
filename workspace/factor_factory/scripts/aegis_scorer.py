#!/usr/bin/env python3
"""Aegis-Alpha v1 daily cross-sectional scorer (ported 2026-11-18 by opus-5-5 from the Antigravity
aegis_prod_kit/aegis_live_scorer.py, sha256 85bd3f9a…; audit: research/offense_v0/notes/aegis_v1_redteam_audit.md).

Patches vs the source (everything else byte-identical in logic):
  P1 write guard: writes only under factor_factory/data/forward_shadow/multi_v1/ (source refused the workspace).
  P2 universe: for dates after the research panel, the official universe is the row set of
     data/forward_shadow/scores/scores_<D>.parquet (forward_shadow_v1); proxy / ffill / static fallbacks are
     still available for the continuous track but flagged, and any of them makes the day non-official.
  P3 no un-preregistered pillar swap: when cyq_perf is out > 5 sessions the cgo pillar is simply missing
     (min_valid 4/5 and 5/7 decide); neg_roc_20 is never substituted.
  P4 ann_store freshness is reported (x_jump_ms_v1 ann_store manifest) so the caller can gate Core7P.

Mandate & Universe Boundary (HARD INVARIANT):
  - Strictly `SH Main (60*) + SZ Main (00*) + ChiNext (300*/301*)` ONLY.
  - STAR Market (`68*`) and BSE (`*.BJ`, `8*`, `4*`) are UNCONDITIONALLY EXCLUDED
    (hard account/mandate boundary — never configurable via soft toggle).

Computes:
  1. Arm-1 (`aegis_micro_5p`): 1/5 EW of size + PIT-SW1 neutralized
     `[neg_roc_60, rpo_clip_60, cgo, vpd_20, ivol_60]` (`min_valid=4`).
  2. Arm-2 (`aegis_core_7p`): 1/7 EW adding strict `side='right'` (`eff_ann < signal_date`)
     PIT fundamentals `[qarp_pit, acc_pit]` (`min_valid=5`).
  3. Legacy Reference (`comp_rank_ew6`): 6-factor Phase-3 composite (`min_valid=4`)
     for paired forward-shadow tracking.
  4. Actionable Top-20 Buffered Basket (`Aegis_Core7P_Top20_Hold60_Cap3`:
     `enter_top_k=20, hold_top_k=60, sw1_ind_cap=3`):
     - Uses clean `T+1 09:30 Open` execution as standard on `Core_7P`.
     - Optional `E4a_Cond_rocT` (`OPEN_EXEC` vs `CLOSE_DEFER_CANDIDATE`) router supported
       strictly as an opt-in diagnostic / `Micro_5P` execution option.

Operational Resilience:
  - Causal `ffill(limit=5)` on `cyq_perf.weight_avg` + automatic `min_valid` (`4/5`, `5/7`) degradation.
  - Automatic `No-CGO` mode (`degraded_mode="no_cgo"`, substituting `neg_roc_20` for `cgo`) when
    `cyq_perf` has been absent or degenerate for >5 consecutive sessions — zero nightly hard crashes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
import warnings
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
LOCAL_LOCKED_WORKSPACE = Path("/Users/fisher/.openclaw/workspace").resolve()
DEFAULT_VPS_WORKSPACE = Path("/srv/hermes-stock/workspace")
RESEARCH_PANEL_END = "20261009"
EARLY_CUT = "20210129"
FLOOR_MV, FLOOR_NA = 0.0025, 0.0050
MICRO_5P_STD_KEYS = ["neg_roc_60", "rpo_clip_60", "cgo", "vpd_20", "ivol_60"]
MICRO_5P_NOCGO_KEYS = ["neg_roc_60", "neg_roc_20", "rpo_clip_60", "vpd_20", "ivol_60"]
CORE_7P_STD_KEYS = ["neg_roc_60", "rpo_clip_60", "cgo", "vpd_20", "ivol_60", "qarp_pit", "acc_pit"]
CORE_7P_NOCGO_KEYS = ["neg_roc_60", "neg_roc_20", "rpo_clip_60", "vpd_20", "ivol_60", "qarp_pit", "acc_pit"]
EW6_FACTORS = [
    ("ivol_60_ew6", -1),
    ("turn_20_ew6", -1),
    ("turn_abn_5_60_ew6", -1),
    ("val_comp_indneu_ew6", 1),
    ("vpd_10_ew6", 1),
    ("turn_cv_20_ew6", -1),
]


WRITE_ROOT_REL = Path("factor_factory/data/forward_shadow/multi_v1")


def assert_safe_write_path(path: Path) -> Path:
    """P1: writes are allowed only under <workspace>/factor_factory/data/forward_shadow/multi_v1/."""
    resolved = path.expanduser().resolve()
    for ws in {LOCAL_LOCKED_WORKSPACE, DEFAULT_VPS_WORKSPACE.resolve() if DEFAULT_VPS_WORKSPACE.exists() else LOCAL_LOCKED_WORKSPACE,
               Path(os.environ.get("AEGIS_WORKSPACE", LOCAL_LOCKED_WORKSPACE)).expanduser().resolve()}:
        root = (ws / WRITE_ROOT_REL).resolve()
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError(f"write outside {WRITE_ROOT_REL}: {resolved}")


def default_workspace() -> Path:
    env_ws = os.environ.get("AEGIS_WORKSPACE", "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    if DEFAULT_VPS_WORKSPACE.exists():
        return DEFAULT_VPS_WORKSPACE.resolve()
    return LOCAL_LOCKED_WORKSPACE


@dataclass
class ScorerConfig:
    workspace: Path = field(default_factory=default_workspace)
    window_sessions: int = 110
    cyq_ffill_limit: int = 5
    enter_top_k: int = 20
    hold_top_k: int = 60
    sw1_ind_cap: int = 3
    basket_signal: str = "aegis_core_7p"
    enable_e4a_router: bool = False  # Clean T+1 09:30 Open is standard for Core_7P; E4a optional for Micro_5P only
    cyq_cache_dir: Path | None = None
    allow_panel_cyq_fallback: bool = True
    daily_cache_dir: Path | None = None
    pit_market_dirs: tuple[Path, ...] | None = None
    ann_store_dirs: tuple[Path, ...] | None = None
    prefer_panel_exact_inputs: bool = False

    @property
    def ff_root(self) -> Path:
        return self.workspace / "factor_factory"

    @property
    def stock_working(self) -> Path:
        return self.workspace / "stock_data/03-working"

    @property
    def backtest_cache(self) -> Path:
        return self.daily_cache_dir or (self.stock_working / "backtest_cache")

    @property
    def effective_cyq_dir(self) -> Path:
        return self.cyq_cache_dir or self.backtest_cache

    @property
    def panel_path(self) -> Path:
        return self.ff_root / "data/research_panel_v2.parquet"

    @property
    def daily_basic_hist_dir(self) -> Path:
        return self.ff_root / "data/daily_basic_hist"

    @property
    def forward_db_dirs(self) -> tuple[Path, ...]:
        return (
            self.stock_working / "fundamental_cache/pit_market/v2",
            self.stock_working / "strategy_research/short_track/materialized/daily_basic",
            self.stock_working / "strategy_research/short_track/input_cache",
            self.stock_working / "fundamental_cache/pit_market",
        )

    @property
    def effective_pit_universe_dirs(self) -> tuple[Path, ...]:
        if self.pit_market_dirs is not None:
            return self.pit_market_dirs
        extra = tuple(sorted(self.stock_working.glob("factor_attribution/cache/cfg_*/pit_fetch")))
        return (
            self.stock_working / "fundamental_cache/pit_market",
            self.stock_working / "strategy_research/short_track/materialized/pit_universe",
            *extra,
        )

    @property
    def effective_ann_store_dirs(self) -> tuple[Path, ...]:
        if self.ann_store_dirs is not None:
            return self.ann_store_dirs
        cand = self.ff_root / "data/forward_shadow/x_jump_ms_v1/ann_store"
        return (cand,) if cand.exists() else ()


# ==================== HARD MANDATE BOARD & LIMIT-RATIO RULES ====================


def is_allowed_mandate_board(codes: pd.Index | pd.Series | list[str]) -> np.ndarray:
    """Hard account/mandate filter: strictly `SH Main (60*) + SZ Main (00*) + ChiNext (30*)` ONLY.
    Unconditionally excludes STAR (`68*`) and BSE (`*.BJ`, `8*`, `4*`, `92*`)."""
    idx = pd.Index([str(c) for c in codes])
    allowed_prefix = idx.str.startswith(("00", "30", "60")) & idx.str.endswith((".SZ", ".SH"))
    forbidden = idx.str.endswith(".BJ") | idx.str.startswith(("68", "8", "4", "9"))
    return np.asarray(allowed_prefix & (~forbidden), dtype=bool)


def assert_no_forbidden_boards(codes: pd.Index | pd.Series | list[str]) -> None:
    """Fail-closed invariant check: guarantee zero STAR (`68*`) or BSE (`*.BJ`, `8*`, `4*`) leakage."""
    idx = pd.Index([str(c) for c in codes])
    bad = idx[~is_allowed_mandate_board(idx)].tolist()
    if bad:
        raise AssertionError(f"MANDATE VIOLATION: Forbidden STAR/BSE codes detected in universe: {bad[:10]}")


def classify_board(ts_code: str) -> str:
    c = str(ts_code)
    if c.startswith("30") and c.endswith(".SZ"):
        return "CHINEXT"
    if c.startswith(("00", "60")) and not c.startswith("68") and c.endswith((".SZ", ".SH")):
        return "MAIN"
    raise ValueError(f"Forbidden board code outside SH/SZ Main + ChiNext mandate: {c}")


def limit_ratio_for_codes(
    codes: pd.Index | list[str],
    st_mask: np.ndarray | pd.Series | None = None,
    trade_date: str = "99999999",
) -> np.ndarray:
    """Exact A-share daily price-limit ratio on allowed boards (`SH Main + SZ Main + ChiNext`):
      - ChiNext (`30*`): 20% for trade_date >= 20200824 (10% prior)
      - Main Board (`00*/60*`): 5% if ST else 10%
    """
    idx = pd.Index([str(c) for c in codes])
    assert_no_forbidden_boards(idx)
    is_gem = idx.str.startswith("30") & (str(trade_date) >= "20200824")
    lim = np.where(is_gem, 0.20, 0.10).astype(np.float64)
    if st_mask is not None:
        st_arr = np.asarray(st_mask, dtype=bool)
        lim = np.where((~is_gem) & st_arr, 0.05, lim)
    return lim


# ==================== CALENDAR & DATA LOADING ====================


def get_official_calendar(cfg: ScorerConfig, upto: str = "99999999") -> list[str]:
    """Build full official trading session list from 2017-01-03 through `upto`."""
    early_dates: list[str] = []
    idx_p = cfg.ff_root / "outputs/offense_v0/raw/etf/index_daily.parquet"
    if idx_p.exists():
        idx_df = pd.read_parquet(idx_p, columns=["ts_code", "trade_date"])
        early_dates = sorted(idx_df.loc[idx_df["ts_code"] == "000300.SH", "trade_date"].astype(str).unique())

    dbh_dates = sorted(
        p.stem.split("_")[-1]
        for p in cfg.daily_basic_hist_dir.glob("daily_basic_*.parquet")
        if re.fullmatch(r"\d{8}", p.stem.split("_")[-1]) and not p.name.startswith("._")
    )
    cal_file = cfg.stock_working / "health/trading_calendar.json"
    cal_open: list[str] = []
    if cal_file.exists():
        cal_json = json.loads(cal_file.read_text(encoding="utf-8"))
        cal_open = [str(x) for x in cal_json.get("open_dates", []) if re.fullmatch(r"\d{8}", str(x))]

    bc_dates = sorted(
        p.stem.split("_")[-1]
        for p in cfg.backtest_cache.glob("daily_*.parquet")
        if re.fullmatch(r"\d{8}", p.stem.split("_")[-1]) and not p.name.startswith("._")
    )

    if dbh_dates:
        pre = [d for d in early_dates if d < dbh_dates[0]]
        last_hist = dbh_dates[-1]
        fwd_pool = sorted(set(cal_open) | set(bc_dates))
        fwd = [d for d in fwd_pool if d > last_hist]
        merged = pre + dbh_dates + fwd
    else:
        merged = sorted(set(early_dates) | set(cal_open) | set(bc_dates))
    return [d for d in merged if d <= upto]


def latest_available_session(cfg: ScorerConfig) -> str:
    cal = get_official_calendar(cfg, "99999999")
    avail = [d for d in cal if (cfg.backtest_cache / f"daily_{d}.parquet").exists()]
    if avail:
        return avail[-1]
    dbh = [d for d in cal if (cfg.daily_basic_hist_dir / f"daily_basic_{d}.parquet").exists()]
    if dbh:
        return dbh[-1]
    raise RuntimeError("No cached daily or daily_basic sessions found.")


def load_daily_basic_one(cfg: ScorerConfig, d: str) -> pd.DataFrame | None:
    cols = ["ts_code", "trade_date", "turnover_rate_f", "pe_ttm", "pb", "dv_ttm", "circ_mv", "total_mv"]
    cands = [cfg.daily_basic_hist_dir / f"daily_basic_{d}.parquet"] + [
        p / f"daily_basic_{d}.parquet" for p in cfg.forward_db_dirs
    ]
    for p in cands:
        if not p.exists():
            continue
        f = pd.read_parquet(p)
        f["trade_date"] = f["trade_date"].astype(str).str.replace("-", "", regex=False)
        if set(f["trade_date"]) != {d}:
            continue
        present = [c for c in cols if c in f.columns]
        if not {"ts_code", "circ_mv", "pb"}.issubset(present):
            continue
        out = f[present].copy()
        out["ts_code"] = out["ts_code"].astype(str)
        out = out[is_allowed_mandate_board(out["ts_code"])]
        for col in cols:
            if col not in out.columns:
                out[col] = np.nan
        return out.drop_duplicates("ts_code", keep="last")
    return None


def _load_pit_snapshot_flexible(
    cfg: ScorerConfig, d: str, allow_proxy: bool = False
) -> tuple[pd.DataFrame | None, str | None, bool]:
    req = {"trade_date", "ts_code", "name", "list_status_at_date", "is_st", "is_suspended", "listing_days"}
    for udir in cfg.effective_pit_universe_dirs:
        p = udir / f"universe_{d}.parquet"
        if not p.exists():
            continue
        f = pd.read_parquet(p)
        if "tradable" not in f.columns and "universe_flag" in f.columns:
            f["tradable"] = f["universe_flag"]
        if not req.issubset(f.columns):
            continue
        td_vals = set(f["trade_date"].astype(str).str.replace("-", "", regex=False))
        if td_vals != {d}:
            continue
        has_proxy = bool(f["used_proxy"].fillna(False).astype(bool).any()) if "used_proxy" in f.columns else False
        complete_ok = (
            bool((f["completeness"].astype(str) == "complete").all()) if "completeness" in f.columns else True
        )
        if (has_proxy or not complete_ok) and not allow_proxy:
            continue
        f = f.copy()
        f["trade_date"] = d
        f["ts_code"] = f["ts_code"].astype(str)
        f = f[is_allowed_mandate_board(f["ts_code"])]
        f = f.drop_duplicates("ts_code", keep="last")
        return f, str(p), has_proxy
    return None, None, False


def _load_static_and_st_fallback(cfg: ScorerConfig) -> tuple[pd.DataFrame, dict[str, set[str]], dict[str, str]]:
    static_paths = sorted(
        cfg.stock_working.glob("factor_attribution/cache/cfg_*/pit_fetch/pit_static_bundle.parquet"),
        key=lambda p: p.stat().st_mtime,
    )
    if static_paths:
        st_df = pd.read_parquet(static_paths[-1], columns=["ts_code", "list_date", "delist_date"]).drop_duplicates(
            "ts_code", keep="last"
        )
    else:
        eb = cfg.ff_root / "outputs/offense_v0/raw/early/stock_basic.parquet"
        st_df = (
            pd.read_parquet(eb, columns=["ts_code", "list_date", "delist_date"]).drop_duplicates("ts_code", keep="last")
            if eb.exists()
            else pd.DataFrame(columns=["ts_code", "list_date", "delist_date"])
        )
    st_df["ts_code"] = st_df["ts_code"].astype(str)

    st_sets: dict[str, set[str]] = {}
    st_dir = cfg.ff_root / "data/price_hist/stock_st"
    if st_dir.exists():
        for p in st_dir.glob("stock_st_*.parquet"):
            if p.name.startswith("._"):
                continue
            if p.stat().st_size > 0:
                x = pd.read_parquet(p, columns=["ts_code"])
                st_sets[p.stem.split("_")[-1]] = set(x["ts_code"].astype(str))

    name_map: dict[str, str] = {}
    slist = cfg.workspace / "stock_data/01-root/stock_list.csv"
    if slist.exists():
        sdf = pd.read_csv(slist)
        for r in sdf.itertuples():
            tc = str(getattr(r, "ts_code", ""))
            nm = str(getattr(r, "name", ""))
            if tc and nm:
                name_map[tc] = nm
    return st_df.set_index("ts_code"), st_sets, name_map


def _load_canonical_daily_one(cfg: ScorerConfig, d: str) -> pd.DataFrame | None:
    """Load daily OHLCV for date `d` from backtest_cache, preferring canonical `stk_factor` OHLCV where available."""
    p_d = cfg.backtest_cache / f"daily_{d}.parquet"
    p_s = cfg.backtest_cache / f"stk_factor_{d}.parquet"
    px_cols = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
    df_d = None
    if p_d.exists():
        raw_d = pd.read_parquet(p_d)
        keep = [c for c in px_cols if c in raw_d.columns]
        df_d = raw_d[keep].copy()
        df_d["ts_code"] = df_d["ts_code"].astype(str)
        df_d = df_d[is_allowed_mandate_board(df_d["ts_code"])]
        df_d["trade_date"] = d

    if p_s.exists():
        sf_cols = ["ts_code", "open", "high", "low", "close", "pre_close", "pct_change", "vol", "amount"]
        raw_s = pd.read_parquet(p_s)
        keep_s = [c for c in sf_cols if c in raw_s.columns]
        df_s = raw_s[keep_s].rename(columns={"pct_change": "pct_chg"}).copy()
        df_s["ts_code"] = df_s["ts_code"].astype(str)
        df_s = df_s[is_allowed_mandate_board(df_s["ts_code"])]
        df_s["trade_date"] = d
        if df_d is None:
            return df_s.drop_duplicates("ts_code", keep="last")
        merged = pd.concat([df_s, df_d], ignore_index=True).drop_duplicates("ts_code", keep="first")
        return merged

    return df_d.drop_duplicates("ts_code", keep="last") if df_d is not None else None


def load_window_prices_and_universe(
    cfg: ScorerConfig, sessions: list[str], cal_all: list[str]
) -> tuple[dict[str, np.ndarray], list[str], dict[str, str], dict[str, Any]]:
    """Build aligned `(T_win, N_codes)` price & dynamic universe matrices over `sessions`
    strictly restricted to `SH Main + SZ Main + ChiNext`."""
    d_target = sessions[-1]
    s_first = sessions[0]
    uni_prov: dict[str, Any] = {"mode": "sh_sz_main_chinext_only", "universe_degraded_mode": "none"}

    panel_uni_df = pd.DataFrame(columns=["ts_code", "trade_date", "name"])
    if cfg.panel_path.exists():
        panel_uni_df = pd.read_parquet(
            cfg.panel_path,
            columns=["ts_code", "trade_date", "name"],
            filters=[("trade_date", ">=", s_first), ("trade_date", "<=", d_target)],
        )
        panel_uni_df["trade_date"] = panel_uni_df["trade_date"].astype(str)
        panel_uni_df["ts_code"] = panel_uni_df["ts_code"].astype(str)
        panel_uni_df = panel_uni_df[is_allowed_mandate_board(panel_uni_df["ts_code"])]
        covered = set(panel_uni_df["trade_date"].unique())
        need_live_uni_dates = [d for d in sessions if d not in covered]
    else:
        need_live_uni_dates = list(sessions)

    ff_uni_frames: list[pd.DataFrame] = []
    if need_live_uni_dates:
        still_need = []
        for d in need_live_uni_dates:
            up = cfg.ff_root / f"data/forward_shadow/scores/scores_{d}.parquet"   # P2: official greenfield universe
            if not up.exists():
                up = cfg.ff_root / f"data/universe/universe_{d}.parquet"
            if up.exists():
                uf = pd.read_parquet(up)
                if "name" not in uf.columns:
                    uf["name"] = uf["ts_code"]
                uf = uf[["ts_code", "name"]]
                uf["ts_code"] = uf["ts_code"].astype(str)
                uf = uf[is_allowed_mandate_board(uf["ts_code"])].copy()
                uf["trade_date"] = d
                ff_uni_frames.append(uf[["ts_code", "trade_date", "name"]])
            else:
                still_need.append(d)
                if d == d_target:
                    uni_prov["universe_degraded_mode"] = "no_official_universe"
        need_live_uni_dates = still_need

    if need_live_uni_dates:
        first_need_idx = cal_all.index(need_live_uni_dates[0])
        px_sessions = cal_all[max(0, first_need_idx - 65) : cal_all.index(d_target) + 1]
    else:
        px_sessions = list(sessions)

    bc_frames: list[pd.DataFrame] = []
    missing_bc_dates: list[str] = []
    if cfg.prefer_panel_exact_inputs and d_target <= RESEARCH_PANEL_END:
        missing_bc_dates = list(px_sessions)
    else:
        for d in px_sessions:
            one = _load_canonical_daily_one(cfg, d)
            if one is not None and not one.empty:
                bc_frames.append(one)
            else:
                missing_bc_dates.append(d)

    hist_frames: list[pd.DataFrame] = []
    seen_stems: set[str] = set()
    if missing_bc_dates:
        lo_m, hi_m = min(missing_bc_dates), max(missing_bc_dates)
        miss_set = set(missing_bc_dates)
        price_dirs = (
            [cfg.ff_root / "data/price_hist/daily"]
            if cfg.prefer_panel_exact_inputs
            else [cfg.ff_root / "data/price_hist/daily", cfg.ff_root / "data/delisted_hist/daily"]
        )
        all_files: list[Path] = []
        for pdir in price_dirs:
            if not pdir.exists():
                continue
            for fp in sorted(pdir.glob("*.parquet")):
                if fp.name.startswith("._"):
                    continue
                if fp.stem in seen_stems:
                    continue
                if not is_allowed_mandate_board([fp.stem])[0]:
                    continue
                seen_stems.add(fp.stem)
                all_files.append(fp)

        def _read_one_hist(fp: Path) -> pd.DataFrame | None:
            df = pd.read_parquet(
                fp,
                columns=["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"],
                filters=[("trade_date", ">=", lo_m), ("trade_date", "<=", hi_m)],
            )
            if df.empty:
                return None
            df["trade_date"] = df["trade_date"].astype(str)
            df = df[df["trade_date"].isin(miss_set)]
            if df.empty:
                return None
            df["ts_code"] = fp.stem
            return df

        with ThreadPoolExecutor(max_workers=8) as ex:
            for item in ex.map(_read_one_hist, all_files):
                if item is not None:
                    hist_frames.append(item)

        if cfg.panel_path.exists() and not cfg.prefer_panel_exact_inputs:
            pan_px = pd.read_parquet(
                cfg.panel_path,
                columns=["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"],
                filters=[("trade_date", ">=", lo_m), ("trade_date", "<=", hi_m)],
            )
            pan_px["trade_date"] = pan_px["trade_date"].astype(str)
            pan_px = pan_px[pan_px["trade_date"].isin(miss_set)]
            pan_px = pan_px[is_allowed_mandate_board(pan_px["ts_code"])]
            if not pan_px.empty:
                hist_frames.append(pan_px)

    all_px = pd.concat(bc_frames + hist_frames, ignore_index=True)
    all_px = all_px[is_allowed_mandate_board(all_px["ts_code"])]
    all_px = all_px.drop_duplicates(["ts_code", "trade_date"], keep="first")

    recomp_uni_frames: list[pd.DataFrame] = []
    if need_live_uni_dates:
        px_s = all_px.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        px_s["amount_f"] = pd.to_numeric(px_s["amount"], errors="coerce").fillna(0.0)
        px_s["vol_f"] = pd.to_numeric(px_s["vol"], errors="coerce").fillna(0.0)
        px_s["close_f"] = pd.to_numeric(px_s["close"], errors="coerce")
        px_s["is_trd"] = ((px_s["amount_f"] > 0) & (px_s["vol_f"] > 0)).astype(float)
        grp = px_s.groupby("ts_code", group_keys=False)
        px_s["avg_amt_60"] = grp["amount_f"].transform(lambda s: s.rolling(60, min_periods=20).mean())
        px_s["trd_ratio_60"] = grp["is_trd"].transform(lambda s: s.rolling(60, min_periods=20).mean())
        by_day_px = {d: df_d for d, df_d in px_s[px_s["trade_date"].isin(set(need_live_uni_dates))].groupby("trade_date")}

        static_idx, st_sets, static_names = _load_static_and_st_fallback(cfg)
        st_pat_re = re.compile(r"ST|\*ST|退", flags=re.IGNORECASE)

        for d in need_live_uni_dates:
            day_px = by_day_px.get(d)
            if day_px is None or day_px.empty:
                continue
            snap, snap_src, _ = _load_pit_snapshot_flexible(cfg, d, allow_proxy=False)
            snap_lag = 0
            if snap is None:
                snap, snap_src, _ = _load_pit_snapshot_flexible(cfg, d, allow_proxy=True)
                if snap is not None and d == d_target:
                    uni_prov["universe_degraded_mode"] = "pit_proxy_accepted"
            if snap is None:
                d_idx_cal = cal_all.index(d)
                for back in range(1, 6):
                    if d_idx_cal - back < 0:
                        break
                    prev_d = cal_all[d_idx_cal - back]
                    snap_prev, snap_src, _ = _load_pit_snapshot_flexible(cfg, prev_d, allow_proxy=True)
                    if snap_prev is not None:
                        delta_cal_days = (pd.to_datetime(d) - pd.to_datetime(prev_d)).days
                        snap = snap_prev.copy()
                        snap["trade_date"] = d
                        snap["listing_days"] = pd.to_numeric(snap["listing_days"], errors="coerce") + delta_cal_days
                        snap["is_suspended"] = False
                        snap_lag = back
                        if d == d_target:
                            uni_prov["universe_degraded_mode"] = f"pit_ffill_{back}d"
                        break

            if snap is not None:
                m = day_px.merge(snap, on=["trade_date", "ts_code"], how="inner")
                reported_susp = m["is_suspended"].fillna(False).astype(bool) if snap_lag == 0 else (m["is_trd"] <= 0)
                st_name = m["name"].fillna("").astype(str).map(lambda x: bool(st_pat_re.search(x)))
                u_flag_ok = (
                    (pd.to_numeric(m["universe_flag"], errors="coerce").fillna(1) > 0)
                    if "universe_flag" in m.columns
                    else True
                )
                trad_ok = (
                    (pd.to_numeric(m["tradable"], errors="coerce").fillna(1) > 0)
                    if "tradable" in m.columns
                    else True
                )
                cond = (
                    (m["list_status_at_date"].astype(str) == "L")
                    & (pd.to_numeric(m["listing_days"], errors="coerce").fillna(-1) >= 252)
                    & (m["close_f"].fillna(0.0) >= 2.0)
                    & u_flag_ok
                    & trad_ok
                    & (~m["is_st"].fillna(False).astype(bool))
                    & (~st_name)
                    & (~reported_susp)
                    & (m["is_trd"] > 0)
                    & (m["trd_ratio_60"].fillna(0.0) >= 0.90)
                )
                pre = m.loc[cond].copy()
                if not pre.empty:
                    thr = float(pre["avg_amt_60"].quantile(0.50))
                    sel = pre.loc[pre["avg_amt_60"] >= thr, ["ts_code", "trade_date", "name"]].copy()
                    recomp_uni_frames.append(sel)
                    if d == d_target:
                        uni_prov["source"] = snap_src
            else:
                m = day_px.copy()
                m["list_date"] = m["ts_code"].map(static_idx["list_date"]) if "list_date" in static_idx.columns else None
                m["delist_date"] = (
                    m["ts_code"].map(static_idx["delist_date"]) if "delist_date" in static_idx.columns else None
                )
                m["listing_days"] = (pd.to_datetime(d) - pd.to_datetime(m["list_date"], errors="coerce")).dt.days
                m["name"] = m["ts_code"].map(static_names).fillna(m["ts_code"])
                st_set_d = st_sets.get(d, set())
                is_st = m["ts_code"].isin(st_set_d) | m["name"].map(lambda x: bool(st_pat_re.search(str(x))))
                not_delisted = m["delist_date"].isna() | (m["delist_date"].astype(str) == "") | (
                    m["delist_date"].astype(str) == "None"
                ) | (m["delist_date"].astype(str) > d)
                cond = (
                    not_delisted
                    & (m["listing_days"].fillna(-1) >= 252)
                    & (m["close_f"].fillna(0.0) >= 2.0)
                    & (~is_st)
                    & (m["is_trd"] > 0)
                    & (m["trd_ratio_60"].fillna(0.0) >= 0.90)
                )
                pre = m.loc[cond].copy()
                if not pre.empty:
                    thr = float(pre["avg_amt_60"].quantile(0.50))
                    sel = pre.loc[pre["avg_amt_60"] >= thr, ["ts_code", "trade_date", "name"]].copy()
                    recomp_uni_frames.append(sel)
                    if d == d_target:
                        uni_prov["source"] = "pit_static_bundle_plus_stock_st_rules"
                        if uni_prov["universe_degraded_mode"] == "none" and d > RESEARCH_PANEL_END:
                            uni_prov["universe_degraded_mode"] = "static_st_fallback"

    uni_all = pd.concat([panel_uni_df, *ff_uni_frames, *recomp_uni_frames], ignore_index=True)
    uni_all = uni_all.drop_duplicates(["ts_code", "trade_date"], keep="first")
    uni_all = uni_all[uni_all["trade_date"].isin(set(sessions))]
    if cfg.prefer_panel_exact_inputs and seen_stems:
        uni_all = uni_all[uni_all["ts_code"].isin(seen_stems)]
    uni_all = uni_all[is_allowed_mandate_board(uni_all["ts_code"])]
    assert_no_forbidden_boards(uni_all["ts_code"])

    if (uni_all["trade_date"] == d_target).sum() == 0:
        raise RuntimeError(f"Universe on signal_date={d_target} is empty.")

    px_win = all_px[all_px["trade_date"].isin(set(sessions))].copy()
    codes = sorted(set(uni_all["ts_code"].unique()) | set(px_win["ts_code"].unique()))
    codes = [c for c in codes if is_allowed_mandate_board([c])[0]]
    assert_no_forbidden_boards(codes)
    cal_idx = pd.Index(sessions)

    def _piv(df: pd.DataFrame, col: str) -> np.ndarray:
        if df.empty or col not in df.columns:
            return np.full((len(sessions), len(codes)), np.nan, dtype=np.float64)
        return (
            df.pivot(index="trade_date", columns="ts_code", values=col)
            .reindex(index=cal_idx, columns=codes)
            .to_numpy(dtype=np.float64)
        )

    O = _piv(px_win, "open")
    Hi = _piv(px_win, "high")
    Lo = _piv(px_win, "low")
    C = _piv(px_win, "close")
    PC = _piv(px_win, "pre_close")
    V = _piv(px_win, "vol")
    amt = _piv(px_win, "amount")

    U = (
        uni_all.assign(_u=True)
        .pivot(index="trade_date", columns="ts_code", values="_u")
        .reindex(index=cal_idx, columns=codes)
        .fillna(False)
        .to_numpy(dtype=bool)
    )

    name_lookup: dict[str, str] = {}
    day_u = uni_all[uni_all["trade_date"] == d_target]
    for r in day_u.itertuples():
        if pd.notna(r.name) and str(r.name):
            name_lookup[str(r.ts_code)] = str(r.name)

    traded = np.isfinite(C) & (C > 0) & np.isfinite(V) & (V > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(traded & np.isfinite(PC) & (PC > 0), C / PC - 1.0, np.nan)
    r_df = pd.DataFrame(r, index=cal_idx, columns=codes)
    adj = (1.0 + r_df.fillna(0.0)).cumprod()
    aC = adj.where(pd.DataFrame(traded, index=cal_idx, columns=codes)).ffill().to_numpy(dtype=np.float64)

    mats = {"O": O, "Hi": Hi, "Lo": Lo, "C": C, "PC": PC, "V": V, "amt": amt, "U": U, "traded": traded, "r": r, "aC": aC}
    uni_prov["universe_size"] = int(U[-1].sum())
    return mats, codes, name_lookup, uni_prov


def load_pit_sw1_industries(cfg: ScorerConfig, d_target: str, codes: list[str]) -> tuple[np.ndarray, list[str]]:
    """Build exact PIT Shenwan L1 industry vector on `d_target` matching `accum_v1_early_frame.ind_matrix`."""
    sw_er = cfg.ff_root / "outputs/offense_v0/raw/early/sw_member.parquet"
    sw_fa = sorted(cfg.stock_working.glob("factor_attribution/cache/cfg_*/pit_fetch/sw2021_member_all.parquet"))

    frames = []
    er_codes: set[str] = set()
    if sw_er.exists():
        f0 = pd.read_parquet(sw_er, columns=["ts_code", "l1_name", "in_date", "out_date"])
        f0["ts_code"] = f0["ts_code"].astype(str)
        er_codes = set(f0["ts_code"].unique())
        frames.append(f0)
    if sw_fa and not cfg.prefer_panel_exact_inputs:
        f1 = pd.read_parquet(sw_fa[-1], columns=["ts_code", "l1_name", "in_date", "out_date"])
        f1["ts_code"] = f1["ts_code"].astype(str)
        f1 = f1[~f1["ts_code"].isin(er_codes)]
        if not f1.empty:
            frames.append(f1)

    if not frames:
        return np.full(len(codes), -1, dtype=np.int16), []

    sw = pd.concat(frames, ignore_index=True).astype({"in_date": "string", "out_date": "string"}).sort_values("in_date")
    names = sorted(sw["l1_name"].dropna().astype(str).unique())
    nid = {k: i for i, k in enumerate(names)}
    ci = {c: i for i, c in enumerate(codes)}
    ind_vec = np.full(len(codes), -1, dtype=np.int16)

    for r in sw.drop_duplicates("ts_code", keep="first").itertuples():
        j = ci.get(str(r.ts_code))
        if j is not None and pd.notna(r.l1_name):
            ind_vec[j] = nid[str(r.l1_name)]

    for r in sw.itertuples():
        j = ci.get(str(r.ts_code))
        if j is None or pd.isna(r.l1_name):
            continue
        in_ok = pd.isna(r.in_date) or str(r.in_date) <= d_target
        out_ok = pd.isna(r.out_date) or str(r.out_date) == "" or str(r.out_date) == "<NA>" or d_target < str(r.out_date)
        if in_ok and out_ok:
            ind_vec[j] = nid[str(r.l1_name)]

    return ind_vec, names


def load_valuation_and_cyq(
    cfg: ScorerConfig, sessions: list[str], codes: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load trailing turnover_rate_f, day-D valuation (`circ_mv, total_mv, pb, pe_ttm, dv_lag1`),
    and fault-tolerant `cyq_perf.weight_avg` with causal `ffill(limit=5)` + outage diagnostics."""
    d_target = sessions[-1]
    d_prev = sessions[-2] if len(sessions) >= 2 else None
    cidx = pd.Index(codes)

    turn_sessions = sessions[-65:]
    db_map: dict[str, pd.DataFrame] = {}
    for d in turn_sessions:
        f = load_daily_basic_one(cfg, d)
        if f is not None:
            db_map[d] = f.set_index("ts_code")

    if d_target not in db_map:
        raise RuntimeError(f"daily_basic for signal_date={d_target} is unavailable.")

    turn_mat = np.full((len(sessions), len(codes)), np.nan, dtype=np.float64)
    offset = len(sessions) - len(turn_sessions)
    for idx_rel, d in enumerate(turn_sessions):
        if d in db_map and "turnover_rate_f" in db_map[d].columns:
            turn_mat[offset + idx_rel] = db_map[d]["turnover_rate_f"].reindex(cidx).to_numpy(dtype=np.float64)

    db_d = db_map[d_target]
    circ_mv_d = db_d["circ_mv"].reindex(cidx).to_numpy(dtype=np.float64)
    total_mv_d = db_d["total_mv"].reindex(cidx).to_numpy(dtype=np.float64)
    pb_d = db_d["pb"].reindex(cidx).to_numpy(dtype=np.float64)
    pe_d = db_d["pe_ttm"].reindex(cidx).to_numpy(dtype=np.float64)
    if d_prev and d_prev in db_map:
        dv_lag1_d = db_map[d_prev]["dv_ttm"].reindex(cidx).to_numpy(dtype=np.float64)
    else:
        dv_lag1_d = np.full(len(codes), np.nan, dtype=np.float64)

    if cfg.prefer_panel_exact_inputs and d_target <= RESEARCH_PANEL_END and cfg.panel_path.exists():
        pan_d = pd.read_parquet(
            cfg.panel_path,
            columns=["ts_code", "pb", "pe_ttm", "dv_ttm_lag1", "circ_mv", "total_mv"],
            filters=[("trade_date", "==", d_target)],
        ).set_index("ts_code")
        if not pan_d.empty:
            pb_d = pan_d["pb"].reindex(cidx).to_numpy(dtype=np.float64)
            pe_d = pan_d["pe_ttm"].reindex(cidx).to_numpy(dtype=np.float64)
            dv_lag1_d = pan_d["dv_ttm_lag1"].reindex(cidx).to_numpy(dtype=np.float64)
            circ_mv_d = pan_d["circ_mv"].reindex(cidx).to_numpy(dtype=np.float64)
            total_mv_d = pan_d["total_mv"].reindex(cidx).to_numpy(dtype=np.float64)

    probe_n = min(len(sessions), max(cfg.cyq_ffill_limit + 1, 12))
    probe_sessions = sessions[-probe_n:]
    wavg_probe = np.full((probe_n, len(codes)), np.nan, dtype=np.float64)
    valid_day_flags: list[bool] = []

    panel_cyq_cache: dict[str, pd.Series] = {}
    use_panel_cyq = (
        cfg.allow_panel_cyq_fallback
        and cfg.cyq_cache_dir is None
        and cfg.panel_path.exists()
        and any(d < "20260105" or cfg.prefer_panel_exact_inputs for d in probe_sessions)
    )
    if use_panel_cyq:
        p_q = pd.read_parquet(
            cfg.panel_path,
            columns=["ts_code", "trade_date", "weight_avg"],
            filters=[("trade_date", ">=", probe_sessions[0]), ("trade_date", "<=", probe_sessions[-1])],
        )
        p_q["trade_date"] = p_q["trade_date"].astype(str)
        for d_k, g_k in p_q.groupby("trade_date"):
            panel_cyq_cache[str(d_k)] = g_k.drop_duplicates("ts_code").set_index("ts_code")["weight_avg"]

    for idx_q, d in enumerate(probe_sessions):
        q_series: pd.Series | None = None
        if cfg.prefer_panel_exact_inputs and d in panel_cyq_cache:
            s_cand = panel_cyq_cache[d]
            if s_cand.nunique(dropna=True) > 50:
                q_series = s_cand
        if q_series is None:
            qp = cfg.effective_cyq_dir / f"cyq_perf_{d}.parquet"
            if qp.exists():
                try:
                    qf = pd.read_parquet(qp, columns=["ts_code", "weight_avg"])
                    qf["ts_code"] = qf["ts_code"].astype(str)
                    qf = qf[is_allowed_mandate_board(qf["ts_code"])]
                    s_cand = qf.drop_duplicates("ts_code", keep="last").set_index("ts_code")["weight_avg"]
                    s_cand = pd.to_numeric(s_cand, errors="coerce")
                    if s_cand.nunique(dropna=True) > 50:
                        q_series = s_cand
                except Exception:
                    q_series = None
        if q_series is None and d < "20260105" and d in panel_cyq_cache:
            s_cand = panel_cyq_cache[d]
            if s_cand.nunique(dropna=True) > 50:
                q_series = s_cand

        if q_series is not None:
            wavg_probe[idx_q] = q_series.reindex(cidx).to_numpy(dtype=np.float64)
            valid_day_flags.append(True)
        else:
            valid_day_flags.append(False)

    cyq_absent_days = 0
    for ok_flag in reversed(valid_day_flags):
        if not ok_flag:
            cyq_absent_days += 1
        else:
            break

    wavg_ff5 = pd.DataFrame(wavg_probe, index=probe_sessions, columns=codes).ffill(limit=cfg.cyq_ffill_limit)
    wavg_d = wavg_ff5.iloc[-1].to_numpy(dtype=np.float64)

    val_data = {
        "turn_mat": turn_mat,
        "circ_mv": circ_mv_d,
        "total_mv": total_mv_d,
        "pb": pb_d,
        "pe_ttm": pe_d,
        "dv_lag1": dv_lag1_d,
        "wavg_ff5": wavg_d,
    }
    cyq_meta = {
        "cyq_absent_days": int(cyq_absent_days),
        "cyq_probe_sessions": probe_sessions,
        "cyq_fresh_on_signal_date": bool(valid_day_flags[-1]) if valid_day_flags else False,
    }
    return val_data, cyq_meta


# ==================== IRONCLAD PIT FUNDAMENTALS ENGINE ====================


class PITFundamentalStore:
    """In-memory cache of raw periodic filings & historical daily_basic MV/PB so multi-date
    scoring filters strictly by `eff_ann < signal_date` (`side='right'`) in <0.4s per date."""

    def __init__(self, cfg: ScorerConfig):
        self.cfg = cfg
        cat_dir = cfg.ff_root / "outputs/offense_v0/raw/cat"
        ann_dirs = cfg.effective_ann_store_dirs

        def _load_api(api: str, cols: list[str]) -> pd.DataFrame:
            files = [f for f in sorted((cat_dir / api).glob("*.parquet")) if not f.name.startswith("._")] if (cat_dir / api).exists() else []
            for ad in ann_dirs:
                if (ad / api).exists():
                    files.extend(f for f in sorted((ad / api).glob("seen_*.parquet")) if not f.name.startswith("._"))
            frames = []
            for fp in files:
                raw = pd.read_parquet(fp)
                keep = [c for c in cols if c in raw.columns]
                if {"ts_code", "end_date", "ann_date"}.issubset(keep):
                    frames.append(raw[keep].copy())
            if not frames:
                return pd.DataFrame(columns=cols)
            out = pd.concat(frames, ignore_index=True)
            for col in cols:
                if col not in out.columns:
                    out[col] = np.nan
            return out

        self.inc_raw = _load_api(
            "income_vip",
            ["ts_code", "ann_date", "f_ann_date", "end_date", "total_revenue", "n_income_attr_p", "update_flag"],
        )
        self.fi_raw = _load_api(
            "fina_indicator_vip", ["ts_code", "ann_date", "end_date", "profit_dedt", "update_flag"]
        )
        self.fc_raw = _load_api(
            "forecast_vip", ["ts_code", "ann_date", "end_date", "net_profit_min", "net_profit_max", "update_flag"]
        )
        self.ex_raw = _load_api("express", ["ts_code", "ann_date", "end_date", "revenue", "n_income", "update_flag"])

        b_frames: list[pd.DataFrame] = []
        er_db = cfg.ff_root / "outputs/offense_v0/raw/early/daily_basic"
        if er_db.exists():
            b0 = pd.concat(
                [
                    pd.read_parquet(fp, columns=["ts_code", "trade_date", "total_mv", "pb"])
                    for fp in sorted(er_db.glob("*.parquet")) if not fp.name.startswith("._")
                ],
                ignore_index=True,
            )
            b0["trade_date"] = b0["trade_date"].astype(str)
            b0 = b0[b0["trade_date"] < EARLY_CUT]
            b_frames.append(b0)

        if cfg.panel_path.exists():
            bp = pd.read_parquet(cfg.panel_path, columns=["ts_code", "trade_date", "total_mv", "pb"])
            bp["trade_date"] = bp["trade_date"].astype(str)
            bp = bp[bp["trade_date"] >= EARLY_CUT]
            b_frames.append(bp)
        else:
            for fp in sorted(cfg.daily_basic_hist_dir.glob("daily_basic_*.parquet")):
                if fp.name.startswith("._"):
                    continue
                f = pd.read_parquet(fp, columns=["ts_code", "trade_date", "total_mv", "pb"])
                f["trade_date"] = f["trade_date"].astype(str)
                b_frames.append(f)

        self.base_mv_pb = (
            pd.concat(b_frames, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
            if b_frames
            else pd.DataFrame(columns=["ts_code", "trade_date", "total_mv", "pb"])
        )
        self.base_mv_pb["ts_code"] = self.base_mv_pb["ts_code"].astype(str)
        self._ext_loaded_dates: set[str] = (
            set(self.base_mv_pb["trade_date"].unique()) if not self.base_mv_pb.empty else set()
        )

    def ensure_mv_pb_up_to(self, cal_pit: np.ndarray) -> pd.DataFrame:
        need = [str(d) for d in cal_pit if str(d) > RESEARCH_PANEL_END and str(d) not in self._ext_loaded_dates]
        if need:
            ext = []
            for d in need:
                one = load_daily_basic_one(self.cfg, d)
                if one is not None:
                    ext.append(one[["ts_code", "trade_date", "total_mv", "pb"]])
                    self._ext_loaded_dates.add(d)
            if ext:
                self.base_mv_pb = pd.concat([self.base_mv_pb, *ext], ignore_index=True).drop_duplicates(
                    ["ts_code", "trade_date"]
                )
        max_d = str(cal_pit[-1])
        return self.base_mv_pb[self.base_mv_pb["trade_date"] <= max_d]


def _map_avail_date(eff_ann: np.ndarray, cal_all: np.ndarray) -> np.ndarray:
    """First trading session STRICTLY AFTER `eff_ann` (`side='right'`)."""
    eff = np.asarray(eff_ann, dtype="U8")
    out = np.full(len(eff), "99999999", dtype="U8")
    pre = eff < cal_all[0]
    out[pre] = eff[pre]
    in_range = (~pre) & (eff < cal_all[-1])
    pos = np.searchsorted(cal_all, eff[in_range], side="right")
    valid = pos < len(cal_all)
    idx_in = np.where(in_range)[0]
    out[idx_in[valid]] = cal_all[pos[valid]]
    return out


def compute_pit_fundamentals_at_date(
    store: PITFundamentalStore,
    cal_pit: np.ndarray,
    d_target: str,
    codes: list[str],
    total_mv_d: np.ndarray,
    pb_d: np.ndarray,
) -> dict[str, np.ndarray]:
    """Compute strict PIT `ep_dedt, roe_dedt, jump, rev_jump, sue, sue_mv, ev_age` at `d_target`.
    Strictly enforces `eff_ann < d_target` (`avail <= d_target` via `side='right'`) and non-STAR/BSE mandate."""
    assert str(cal_pit[-1]) == d_target

    inc = store.inc_raw.copy()
    for c in ("ts_code", "ann_date", "f_ann_date", "end_date", "update_flag"):
        inc[c] = inc[c].fillna("").astype(str)
    a_ok = inc["ann_date"].str.len() == 8
    f_ok = inc["f_ann_date"].str.len() == 8
    inc["eff_ann"] = np.where(
        a_ok & f_ok,
        np.maximum(inc["ann_date"].to_numpy(), inc["f_ann_date"].to_numpy()),
        np.where(f_ok, inc["f_ann_date"].to_numpy(), np.where(a_ok, inc["ann_date"].to_numpy(), "99999999")),
    )
    inc = inc[(inc["eff_ann"] < d_target) & (inc["end_date"].str.len() == 8) & (inc["end_date"] <= d_target)]
    inc = inc.sort_values(["ts_code", "end_date", "update_flag", "eff_ann"]).drop_duplicates(
        ["ts_code", "end_date"], keep="first"
    ).copy()
    inc["avail"] = _map_avail_date(inc["eff_ann"].to_numpy(), cal_pit)
    inc = inc[inc["avail"] <= d_target].copy()

    fi = store.fi_raw.dropna(subset=["profit_dedt"]).copy()
    for c in ("ts_code", "ann_date", "end_date", "update_flag"):
        fi[c] = fi[c].fillna("").astype(str)
    fi = fi[(fi["ann_date"].str.len() == 8) & (fi["ann_date"] < d_target) & (fi["end_date"].str.len() == 8) & (fi["end_date"] <= d_target)]
    fi = fi.sort_values(["ts_code", "end_date", "update_flag", "ann_date"]).drop_duplicates(
        ["ts_code", "end_date"], keep="first"
    ).copy()
    fi["avail_dt"] = _map_avail_date(fi["ann_date"].to_numpy(), cal_pit)
    fi = fi[fi["avail_dt"] <= d_target].copy()

    R = inc.merge(fi[["ts_code", "end_date", "profit_dedt", "avail_dt"]], on=["ts_code", "end_date"], how="left")
    R["profit_dedt"] = pd.to_numeric(R["profit_dedt"], errors="coerce")
    R["n_income_attr_p"] = pd.to_numeric(R["n_income_attr_p"], errors="coerce")
    R["total_revenue"] = pd.to_numeric(R["total_revenue"], errors="coerce")
    R["dedt_cum"] = np.where(R["avail_dt"].notna() & (R["avail_dt"] <= R["avail"]), R["profit_dedt"], np.nan)
    R["core_cum"] = np.where(np.isfinite(R["dedt_cum"]), R["dedt_cum"], R["n_income_attr_p"])
    R["y"] = R["end_date"].str[:4].astype(int)
    R["q"] = R["end_date"].str[4:6].astype(int) // 3

    KR = R.set_index(["ts_code", "y", "q"], drop=False)
    KR.index.names = ["_c", "_y", "_q"]
    idx = KR.index
    cap = KR["avail"].to_numpy()

    def look_R(dy: int, q_fixed: int | None, col: str) -> np.ndarray:
        q_arr_l = idx.get_level_values(2) if q_fixed is None else np.full(len(idx), q_fixed)
        key = pd.MultiIndex.from_arrays([idx.get_level_values(0), idx.get_level_values(1) + dy, q_arr_l])
        v = KR[col].reindex(key).to_numpy(dtype=np.float64)
        av = KR["avail"].reindex(key).fillna("99999999").to_numpy()
        return np.where(av <= cap, v, np.nan)

    q_arr = KR["q"].to_numpy()
    for src_col, prefix in [("core_cum", "dedt"), ("n_income_attr_p", "np"), ("total_revenue", "rev")]:
        c_val = KR[src_col].to_numpy(dtype=np.float64)
        c_fy1 = look_R(-1, 4, src_col)
        c_ly = look_R(-1, None, src_col)
        c_fy2 = look_R(-2, 4, src_col)
        c_ly2 = look_R(-2, None, src_col)
        KR[f"{prefix}_ttm"] = np.where(q_arr == 4, c_val, c_val + c_fy1 - c_ly)
        KR[f"{prefix}_ttm_ly"] = np.where(q_arr == 4, c_fy1, c_ly + c_fy2 - c_ly2)

    K_reg = KR.reset_index(drop=True)
    lag_days = (pd.to_datetime(K_reg["eff_ann"]) - pd.to_datetime(K_reg["end_date"])).dt.days
    K_reg = K_reg[(lag_days >= 0) & (lag_days <= 180) & (K_reg["avail"] >= "20170103")].copy()
    K_reg = K_reg.sort_values(["ts_code", "avail", "end_date"]).drop_duplicates(["ts_code", "avail"], keep="last")
    K_reg["_e"] = K_reg["end_date"].astype(int)
    K_reg = K_reg[K_reg["_e"] >= K_reg.groupby("ts_code")["_e"].cummax()].drop(columns=["_e"]).reset_index(drop=True)

    fc = store.fc_raw.dropna(subset=["net_profit_min", "net_profit_max", "ann_date"]).copy()
    for c in ("ts_code", "ann_date", "end_date", "update_flag"):
        fc[c] = fc[c].fillna("").astype(str)
    fc = fc[(fc["ann_date"].str.len() == 8) & (fc["ann_date"] < d_target) & (fc["ann_date"] >= "20170101") & (fc["end_date"].str.len() == 8)]
    fc = fc.sort_values(["ts_code", "end_date", "ann_date", "update_flag"]).drop_duplicates(
        ["ts_code", "end_date", "ann_date"], keep="first"
    )
    fc["avail"] = _map_avail_date(fc["ann_date"].to_numpy(), cal_pit)
    fc = fc[fc["avail"] <= d_target].copy()
    fc["np_cum"] = (pd.to_numeric(fc["net_profit_min"], errors="coerce") + pd.to_numeric(fc["net_profit_max"], errors="coerce")) * 0.5 * 1e4
    fc["rev_cum"] = np.nan
    fc["src"] = "F"
    fc["eff_ann"] = fc["ann_date"]

    ex = store.ex_raw.dropna(subset=["n_income", "ann_date"]).copy()
    for c in ("ts_code", "ann_date", "end_date", "update_flag"):
        ex[c] = ex[c].fillna("").astype(str)
    ex = ex[(ex["ann_date"].str.len() == 8) & (ex["ann_date"] < d_target) & (ex["ann_date"] >= "20170101") & (ex["end_date"].str.len() == 8)]
    ex = ex.sort_values(["ts_code", "end_date", "ann_date", "update_flag"]).drop_duplicates(
        ["ts_code", "end_date", "ann_date"], keep="first"
    )
    ex["avail"] = _map_avail_date(ex["ann_date"].to_numpy(), cal_pit)
    ex = ex[ex["avail"] <= d_target].copy()
    ex["np_cum"] = pd.to_numeric(ex["n_income"], errors="coerce")
    ex["rev_cum"] = pd.to_numeric(ex["revenue"], errors="coerce")
    ex["src"] = "X"
    ex["eff_ann"] = ex["ann_date"]

    r_ev = R[R["eff_ann"] >= "20170101"][
        ["ts_code", "end_date", "eff_ann", "avail", "core_cum", "n_income_attr_p", "total_revenue", "dedt_cum"]
    ].copy()
    r_ev["np_cum"] = r_ev["n_income_attr_p"]
    r_ev["rev_cum"] = r_ev["total_revenue"]
    r_ev["src"] = "R"

    ev = pd.concat(
        [
            r_ev[["ts_code", "end_date", "src", "eff_ann", "avail", "np_cum", "rev_cum", "dedt_cum"]],
            ex[["ts_code", "end_date", "src", "eff_ann", "avail", "np_cum", "rev_cum"]].assign(dedt_cum=np.nan),
            fc[["ts_code", "end_date", "src", "eff_ann", "avail", "np_cum", "rev_cum"]].assign(dedt_cum=np.nan),
        ],
        ignore_index=True,
    )
    # Hard unconditional exclusion of STAR (68*) and BSE (.BJ, 8*, 4*)
    ev = ev[ev["ts_code"].str[:1].isin(list("036")) & ~ev["ts_code"].str.startswith("68") & ~ev["ts_code"].str.endswith(".BJ")].copy()

    lag_ev = (pd.to_datetime(ev["eff_ann"]) - pd.to_datetime(ev["end_date"])).dt.days
    ev = ev[(ev["eff_ann"] >= ev["end_date"]) & (lag_ev <= 150) & (ev["avail"] >= "20170103") & (ev["avail"] <= d_target)].copy()
    ev["pri"] = ev["src"].map({"R": 0, "X": 1, "F": 2})
    ev = ev.sort_values(["ts_code", "end_date", "avail", "pri"]).drop_duplicates(
        ["ts_code", "end_date", "avail"], keep="first"
    ).reset_index(drop=True)

    avail_idx = np.searchsorted(cal_pit, ev["avail"].to_numpy(), side="left")
    tm1_idx = np.maximum(avail_idx - 1, 0)
    ev["Tm1"] = cal_pit[tm1_idx]
    B_mv = store.ensure_mv_pb_up_to(cal_pit).rename(columns={"trade_date": "Tm1"})
    ev = ev.merge(B_mv, on=["ts_code", "Tm1"], how="left")
    ev["mv"] = pd.to_numeric(ev["total_mv"], errors="coerce") * 1e4
    ev["pb_f"] = pd.to_numeric(ev["pb"], errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        ev["be"] = np.where(ev["pb_f"] > 0, ev["mv"] / ev["pb_f"], np.nan)
    fl_arr = np.where(ev["mv"].notna(), np.fmax(ev["mv"].to_numpy() * FLOOR_MV, np.nan_to_num(ev["be"].to_numpy() * FLOOR_NA)), np.nan)

    ev["y"] = ev["end_date"].str[:4].astype(int)
    ev["q"] = ev["end_date"].str[4:6].astype(int) // 3
    use_dt = (ev["src"] == "R") & ev["dedt_cum"].notna()
    ev_cap = ev["avail"].to_numpy()

    def q_back(y_arr: np.ndarray, q_arr_in: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        tot = y_arr * 4 + (q_arr_in - 1) - k
        return tot // 4, (tot % 4) + 1

    def get_cum_at(y_t: np.ndarray, q_t: np.ndarray, col_name: str) -> np.ndarray:
        key = pd.MultiIndex.from_arrays([ev["ts_code"].to_numpy(), y_t, q_t])
        v = KR[col_name].reindex(key).to_numpy(dtype=np.float64)
        av = KR["avail"].reindex(key).fillna("99999999").to_numpy()
        return np.where(av <= ev_cap, v, np.nan)

    def get_sq_hist(k: int, kind: str) -> np.ndarray:
        yk, qk = q_back(ev["y"].to_numpy(), ev["q"].to_numpy(), k)
        if k == 0:
            c0 = (
                ev["dedt_cum"].to_numpy(dtype=np.float64)
                if kind == "dt"
                else (ev["np_cum"].to_numpy(dtype=np.float64) if kind == "np" else ev["rev_cum"].to_numpy(dtype=np.float64))
            )
        else:
            col = "dedt_cum" if kind == "dt" else ("n_income_attr_p" if kind == "np" else "total_revenue")
            c0 = get_cum_at(yk, qk, col)
        yk_prev, qk_prev = q_back(yk, qk, 1)
        col_prev = "dedt_cum" if kind == "dt" else ("n_income_attr_p" if kind == "np" else "total_revenue")
        c_prev = get_cum_at(yk_prev, qk_prev, col_prev)
        return np.where(qk == 1, c0, c0 - c_prev)

    S = []
    for k in range(17):
        s_dt = get_sq_hist(k, "dt")
        s_np = get_sq_hist(k, "np")
        S.append(np.where(use_dt, np.where(np.isfinite(s_dt), s_dt, s_np), s_np))

    def den_vec(v: np.ndarray) -> np.ndarray:
        d = np.fmax(np.abs(v), np.where(np.isfinite(fl_arr), fl_arr, 0.0))
        return np.where(np.isfinite(v) & (d > 0), d, np.nan)

    def yoy_vec(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(np.isfinite(a) & np.isfinite(b), (a - b) / den_vec(b), np.nan)

    yoy0 = yoy_vec(S[0], S[4])
    yoy1 = yoy_vec(S[1], S[5])
    jump = yoy0 - yoy1

    ttm = lambda k: S[k] + S[k + 1] + S[k + 2] + S[k + 3]  # noqa: E731
    y_fy1 = yoy_vec(ttm(1), ttm(5))
    is_q1 = ev["q"].to_numpy() == 1
    jump = np.where(is_q1, yoy0 - y_fy1, jump)

    is_q4 = ev["q"].to_numpy() == 4
    fy0, fy0_l = ttm(0), ttm(4)
    n9, n9_l = S[1] + S[2] + S[3], S[5] + S[6] + S[7]
    y_fy0 = yoy_vec(fy0, fy0_l)
    y_9m = yoy_vec(n9, n9_l)
    jump_final = np.where(is_q4, y_fy0 - y_9m, jump)

    diffs = np.vstack([S[k] - S[k + 4] for k in range(1, 9)])
    n_valid = np.sum(np.isfinite(diffs), axis=0)
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        std_sq = np.nanstd(diffs, axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sue_sq = np.where((n_valid >= 6) & (std_sq > 0) & np.isfinite(S[0] - S[4]), (S[0] - S[4]) / std_sq, np.nan)

    diffs_ttm = np.vstack([ttm(k) - ttm(k + 4) for k in range(1, 9)])
    n_valid_t = np.sum(np.isfinite(diffs_ttm), axis=0)
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        std_ttm = np.nanstd(diffs_ttm, axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sue_fy = np.where((n_valid_t >= 6) & (std_ttm > 0) & np.isfinite(fy0 - fy0_l), (fy0 - fy0_l) / std_ttm, np.nan)
        sue_final = np.where(is_q4, sue_fy, sue_sq)
        sue_mv = np.where(np.isfinite(S[0] - S[4]) & (ev["mv"].to_numpy() > 0), (S[0] - S[4]) / ev["mv"].to_numpy(), np.nan)

    RV = [get_sq_hist(k, "rev") for k in range(8)]
    ry0 = yoy_vec(RV[0], RV[4])
    ry1 = yoy_vec(RV[1], RV[5])
    rjump = ry0 - ry1
    rfy0, rfy0_l = RV[0] + RV[1] + RV[2] + RV[3], RV[4] + RV[5] + RV[6] + RV[7]
    rn9, rn9_l = RV[1] + RV[2] + RV[3], RV[5] + RV[6] + RV[7]
    rev_jump_final = np.where(is_q4, yoy_vec(rfy0, rfy0_l) - yoy_vec(rn9, rn9_l), rjump)

    ev["jump"] = jump_final
    ev["sue"] = sue_final
    ev["sue_mv"] = sue_mv
    ev["rev_jump"] = rev_jump_final

    K_ev = ev.sort_values(["ts_code", "avail", "end_date"]).drop_duplicates(["ts_code", "avail"], keep="last").copy()
    K_ev["_e"] = K_ev["end_date"].astype(int)
    K_ev = K_ev[K_ev["_e"] >= K_ev.groupby("ts_code")["_e"].cummax()].drop(columns=["_e"]).reset_index(drop=True)

    t_pos = len(cal_pit) - 1
    cidx = pd.Index(codes)

    kr = K_reg[K_reg["ts_code"].isin(cidx)].copy()
    kr["apos"] = np.searchsorted(cal_pit, kr["avail"].to_numpy(), side="left").astype(np.int64)
    kr = kr[(kr["apos"] <= t_pos) & (t_pos - kr["apos"] <= 130)].sort_values("apos").drop_duplicates("ts_code", keep="last")
    kr_s = kr.set_index("ts_code")
    dedt_ttm = kr_s["dedt_ttm"].reindex(cidx).to_numpy(dtype=np.float64)

    mv_yuan = total_mv_d.astype(np.float64) * 1e4
    with np.errstate(divide="ignore", invalid="ignore"):
        be_yuan = np.where(pb_d > 0, mv_yuan / pb_d, np.nan)
        ep_dedt = np.where(mv_yuan > 0, dedt_ttm / mv_yuan, np.nan)
        roe_dedt = np.where(be_yuan > 0, dedt_ttm / be_yuan, np.nan)

    ke = K_ev[K_ev["ts_code"].isin(cidx)].copy()
    ke["apos"] = np.searchsorted(cal_pit, ke["avail"].to_numpy(), side="left").astype(np.int64)
    ke_ok = ke[(ke["apos"] <= t_pos) & (t_pos - ke["apos"] <= 130)].sort_values("apos")
    ke_last = ke_ok.drop_duplicates("ts_code", keep="last").set_index("ts_code")

    jump_d = ke_last["jump"].reindex(cidx).to_numpy(dtype=np.float64)
    sue_d = ke_last["sue"].reindex(cidx).to_numpy(dtype=np.float64)
    sue_mv_d = ke_last["sue_mv"].reindex(cidx).to_numpy(dtype=np.float64)
    ev_age_d = (t_pos - ke_last["apos"].reindex(cidx)).to_numpy(dtype=np.float64)

    ke_rx = ke_ok[ke_ok["src"].isin(["R", "X"]) & ke_ok["rev_jump"].notna()].drop_duplicates("ts_code", keep="last").set_index("ts_code")
    rev_jump_d = ke_rx["rev_jump"].reindex(cidx).to_numpy(dtype=np.float64)

    return {
        "ep_dedt": ep_dedt,
        "roe_dedt": roe_dedt,
        "jump": jump_d,
        "rev_jump": rev_jump_d,
        "sue": sue_d,
        "sue_mv": sue_mv_d,
        "ev_age": ev_age_d,
    }


# ==================== CROSS-SECTIONAL NEUTRALIZATION & COMPOSITES ====================


def cs_pct_rank_1d(vec: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return pd.Series(np.where(mask, vec, np.nan)).rank(pct=True).to_numpy(dtype=np.float64)


def ind_pct_rank_1d(vec: np.ndarray, ind_vec: np.ndarray, mask: np.ndarray, min_group: int = 5) -> np.ndarray:
    out = np.full(len(vec), np.nan, dtype=np.float64)
    if len(vec) == 0 or ind_vec.max() < 0:
        return cs_pct_rank_1d(vec, mask)
    n_inds = int(ind_vec.max()) + 1
    for g in range(n_inds):
        gm = mask & (ind_vec == g) & np.isfinite(vec)
        if int(gm.sum()) >= min_group:
            out[gm] = pd.Series(vec[gm]).rank(pct=True).to_numpy(dtype=np.float64)
    miss = mask & np.isfinite(vec) & ~np.isfinite(out)
    if miss.any():
        out = np.where(miss, cs_pct_rank_1d(vec, mask), out)
    return out


def size_ind_neutralize_1d(vec: np.ndarray, lnmv: np.ndarray, ind_vec: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Exact 1D FWL industry + standardized ln(circ_mv) residualization matching `aegis_alpha_v1_confirm.py`."""
    rk = cs_pct_rank_1d(vec, mask)
    out = np.full(len(vec), np.nan, dtype=np.float64)
    ok = mask & np.isfinite(rk) & np.isfinite(lnmv) & (ind_vec >= 0)
    if int(ok.sum()) < 100:
        return cs_pct_rank_1d(vec, mask)
    n_inds = int(ind_vec.max()) + 1
    y = rk[ok]
    lv = lnmv[ok]
    sz = (lv - lv.mean()) / (lv.std() + 1e-12)
    inds = ind_vec[ok]
    cnt = np.bincount(inds, minlength=n_inds)
    inv_cnt = np.where(cnt > 0, 1.0 / np.maximum(cnt, 1), 0.0)
    y_dm = y - (np.bincount(inds, weights=y, minlength=n_inds) * inv_cnt)[inds]
    sz_dm = sz - (np.bincount(inds, weights=sz, minlength=n_inds) * inv_cnt)[inds]
    denom = float(sz_dm @ sz_dm)
    beta = float(sz_dm @ y_dm) / denom if denom > 1e-12 else 0.0
    out[ok] = y_dm - beta * sz_dm
    return cs_pct_rank_1d(out, mask)


def ew_combine_1d(R: dict[str, np.ndarray], keys: list[str], min_valid: int) -> np.ndarray:
    n_valid = sum(np.isfinite(R[k]).astype(int) for k in keys)
    s_sum = sum(np.nan_to_num(R[k], nan=0.0) for k in keys)
    return np.where(n_valid >= min_valid, s_sum / np.maximum(n_valid, 1), np.nan)


# ==================== ACTIONABLE TOP-20 BASKET & EXECUTION PROTOCOL ====================


def select_top20_buffered_basket(
    scores_df: pd.DataFrame,
    prev_holdings: set[str] | list[str] | None = None,
    signal_col: str = "aegis_core_7p",
    enter_top_k: int = 20,
    hold_top_k: int = 60,
    sw1_ind_cap: int = 3,
    enable_e4a_router: bool = False,
) -> dict[str, Any]:
    """Build Actionable Top-20 Buffered Basket (`Aegis_Core7P_Top20_Hold60_Cap3`:
    `enter_top_k=20, hold_top_k=60, sw1_ind_cap=3`).
    - Standard execution for `Core_7P`: clean `T+1 09:30 Open` (`T1_OPEN_0930`).
    - Optional `E4a_Cond_rocT` (`OPEN_EXEC` vs `CLOSE_DEFER_CANDIDATE`) active only when
      `enable_e4a_router=True` and `signal_col == 'aegis_micro_5p'` (always attached as diagnostic `e4a_diagnostic_tag`)."""
    assert_no_forbidden_boards(scores_df["ts_code"])
    prev_set = {str(x) for x in (prev_holdings or ())}
    df = scores_df.copy()
    valid = df[df[signal_col].notna()].sort_values(
        [signal_col, "aegis_micro_5p", "ts_code"], ascending=[False, False, True]
    ).reset_index(drop=True)
    valid["cs_rank"] = np.arange(1, len(valid) + 1, dtype=int)

    roc_med_u = float(np.nanmedian(df["r_oc_t"].to_numpy(dtype=np.float64))) if len(df) else 0.0
    use_e4a_primary = bool(enable_e4a_router and signal_col == "aegis_micro_5p")

    keepers_pool = valid[valid["ts_code"].isin(prev_set) & (valid["cs_rank"] <= hold_top_k)]
    entrants_pool = valid[(~valid["ts_code"].isin(prev_set)) & (valid["cs_rank"] <= enter_top_k)]
    cap_fill_pool = valid[(~valid["ts_code"].isin(prev_set)) & (valid["cs_rank"] > enter_top_k) & (valid["cs_rank"] <= hold_top_k)]

    chosen_rows: list[dict[str, Any]] = []
    chosen_codes: set[str] = set()
    ind_counts: dict[str, int] = {}
    ind_capped_skips: list[str] = []

    for action_tag, pool in (
        ("HOLD", keepers_pool),
        ("BUY_ENTER", entrants_pool),
        ("BUY_IND_CAP_FILL", cap_fill_pool),
    ):
        for r in pool.itertuples():
            if len(chosen_rows) >= enter_top_k:
                break
            code = str(r.ts_code)
            if code in chosen_codes:
                continue
            ind = str(r.ind_sw1_name or "UNKNOWN")
            if ind_counts.get(ind, 0) >= sw1_ind_cap:
                ind_capped_skips.append(code)
                continue
            roc_v = float(r.r_oc_t) if np.isfinite(r.r_oc_t) else np.nan
            e4a_tag = (
                "HOLD_NO_TRADE"
                if action_tag == "HOLD"
                else ("CLOSE_DEFER_CANDIDATE" if (np.isfinite(roc_v) and roc_v > roc_med_u) else "OPEN_EXEC")
            )
            exec_tag = e4a_tag if use_e4a_primary else ("HOLD_NO_TRADE" if action_tag == "HOLD" else "OPEN_EXEC")

            chosen_codes.add(code)
            ind_counts[ind] = ind_counts.get(ind, 0) + 1
            chosen_rows.append(
                {
                    "basket_rank": len(chosen_rows) + 1,
                    "ts_code": code,
                    "name": str(r.name),
                    "board": str(r.board),
                    "sw1_industry": ind,
                    "action": action_tag,
                    "cs_rank": int(r.cs_rank),
                    "score_core_7p": round(float(r.aegis_core_7p), 6) if np.isfinite(r.aegis_core_7p) else None,
                    "score_micro_5p": round(float(r.aegis_micro_5p), 6) if np.isfinite(r.aegis_micro_5p) else None,
                    "pct_core_7p": round(float(r.aegis_core_7p_pct), 4) if np.isfinite(r.aegis_core_7p_pct) else None,
                    "close": round(float(r.close), 2) if np.isfinite(r.close) else None,
                    "r_oc_t": round(float(roc_v), 5) if np.isfinite(roc_v) else None,
                    "limit_ratio": float(r.limit_ratio),
                    "target_weight": round(1.0 / enter_top_k, 6),
                    "exec_route_tag": exec_tag,
                    "e4a_diagnostic_tag": e4a_tag,
                }
            )

    rank_lookup = valid.set_index("ts_code")["cs_rank"].to_dict()
    all_lookup = df.set_index("ts_code")
    exits: list[dict[str, Any]] = []
    for old_c in sorted(prev_set - chosen_codes):
        roc_v = float(all_lookup.at[old_c, "r_oc_t"]) if old_c in all_lookup.index and np.isfinite(all_lookup.at[old_c, "r_oc_t"]) else np.nan
        e4a_sell = "OPEN_EXEC" if (np.isfinite(roc_v) and roc_v > roc_med_u) else "CLOSE_DEFER_CANDIDATE"
        sell_tag = e4a_sell if use_e4a_primary else "OPEN_EXEC"
        exits.append(
            {
                "ts_code": old_c,
                "cs_rank": int(rank_lookup[old_c]) if old_c in rank_lookup else None,
                "r_oc_t": round(float(roc_v), 5) if np.isfinite(roc_v) else None,
                "exec_route_tag": sell_tag,
                "e4a_diagnostic_tag": e4a_sell,
            }
        )

    n_buy_defer = sum(1 for x in chosen_rows if x["action"] != "HOLD" and x["e4a_diagnostic_tag"] == "CLOSE_DEFER_CANDIDATE")
    n_sell_defer = sum(1 for x in exits if x["e4a_diagnostic_tag"] == "CLOSE_DEFER_CANDIDATE")
    sell_defer_scale = round(min(1.0, n_buy_defer / n_sell_defer), 4) if n_sell_defer > 0 else 1.0

    return {
        "basket_id": "Aegis_Core7P_Top20_Hold60_Cap3" if signal_col == "aegis_core_7p" else "Aegis_Micro5P_Top20_Hold60_Cap3",
        "signal_col": signal_col,
        "execution_protocol": "E4a_Cond_rocT" if use_e4a_primary else "T1_OPEN_0930",
        "enter_top_k": enter_top_k,
        "hold_top_k": hold_top_k,
        "sw1_ind_cap": sw1_ind_cap,
        "roc_t_universe_median": round(roc_med_u, 6),
        "n_selected": len(chosen_rows),
        "n_kept": sum(1 for x in chosen_rows if x["action"] == "HOLD"),
        "n_entered": sum(1 for x in chosen_rows if x["action"] != "HOLD"),
        "n_exited": len(exits),
        "e4a_buy_defer_candidates": n_buy_defer,
        "e4a_sell_defer_candidates": n_sell_defer,
        "e4a_self_financing_sell_defer_scale": sell_defer_scale,
        "industry_counts": ind_counts,
        "ind_cap_skipped_codes": ind_capped_skips[:20],
        "holdings": chosen_rows,
        "exits": exits,
    }


# ==================== MAIN SCORING ENTRYPOINT ====================


def ann_store_status(cfg: ScorerConfig, signal_date: str) -> dict[str, Any]:
    """P4: newest announcement date per table in the x_jump_ms_v1 ann_store (+ research seed)."""
    out: dict[str, Any] = {}
    for api in ("income_vip", "fina_indicator_vip", "forecast_vip", "express"):
        mx = ""
        for base in (cfg.ff_root / "outputs/offense_v0/raw/cat", *cfg.effective_ann_store_dirs):
            for fp in [f for f in sorted((base / api).glob("*.parquet")) if not f.name.startswith("._")] if (base / api).exists() else []:
                try:
                    a = pd.read_parquet(fp, columns=["ann_date"])["ann_date"].astype(str)
                    a = a[a < signal_date]
                    if len(a):
                        mx = max(mx, a.max())
                except Exception:
                    continue
        out[api] = mx or None
    return out


def score_cross_section(
    signal_date: str | None = None,
    cfg: ScorerConfig | None = None,
    pit_store: PITFundamentalStore | None = None,
    prev_top20_holdings: set[str] | list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Score full `SH Main + SZ Main + ChiNext` universe cross-section for `signal_date`
    (strictly causal `<= signal_date`, hard non-STAR / non-BSE mandate enforced)."""
    cfg = cfg or ScorerConfig()
    cal_all = get_official_calendar(cfg, "99999999")
    if signal_date is None:
        signal_date = latest_available_session(cfg)
    signal_date = str(signal_date).replace("-", "")
    if signal_date not in cal_all:
        raise ValueError(f"{signal_date} is not an official trading session.")

    cal_upto = [d for d in cal_all if d <= signal_date]
    sessions = cal_upto[-cfg.window_sessions :]
    cal_pit = np.array(cal_upto, dtype="U8")

    mats, codes, name_lookup, uni_prov = load_window_prices_and_universe(cfg, sessions, cal_upto)
    ind_vec, ind_names = load_pit_sw1_industries(cfg, signal_date, codes)
    val_data, cyq_meta = load_valuation_and_cyq(cfg, sessions, codes)

    store = pit_store or PITFundamentalStore(cfg)
    pit_f = compute_pit_fundamentals_at_date(
        store=store,
        cal_pit=cal_pit,
        d_target=signal_date,
        codes=codes,
        total_mv_d=val_data["total_mv"],
        pb_d=val_data["pb"],
    )

    cal_idx = pd.Index(sessions)
    O, C, PC, V, traded, U = mats["O"], mats["C"], mats["PC"], mats["V"], mats["traded"], mats["U"]
    ok_px = traded & (O > 0) & (C > 0) & (PC > 0)

    with np.errstate(divide="ignore", invalid="ignore"):
        r_arr = np.where(traded, mats["r"], np.nan)
        r_oc_arr = np.where(ok_px, C / O - 1.0, np.nan)
        r_po_arr = np.where(ok_px, O / PC - 1.0, np.nan)
        vol_arr = np.where(traded & (V > 0), V, np.nan)
        ac_arr = np.where(traded, mats["aC"], np.nan)

    r_df = pd.DataFrame(r_arr, index=cal_idx, columns=codes)
    roc_df = pd.DataFrame(r_oc_arr, index=cal_idx, columns=codes)
    rpo_clip_df = pd.DataFrame(np.clip(r_po_arr, -0.02, 0.02), index=cal_idx, columns=codes)
    v_df = pd.DataFrame(vol_arr, index=cal_idx, columns=codes)
    ac_df = pd.DataFrame(ac_arr, index=cal_idx, columns=codes)

    m_ret = r_df.where(pd.DataFrame(U, index=cal_idx, columns=codes)).mean(axis=1)
    beta = r_df.rolling(60, min_periods=45).cov(m_ret).div(m_ret.rolling(60, min_periods=45).var(), axis=0)

    C_d = C[-1]
    wavg_d = val_data["wavg_ff5"]
    with np.errstate(divide="ignore", invalid="ignore"):
        cgo_raw = np.where((C_d > 0) & (wavg_d > 0), C_d / wavg_d - 1.0, np.nan)

    raw_p = {
        "neg_roc_60": -roc_df.rolling(60, min_periods=45).mean().iloc[-1].to_numpy(dtype=np.float64),
        "neg_roc_20": -roc_df.rolling(20, min_periods=15).mean().iloc[-1].to_numpy(dtype=np.float64),
        "rpo_clip_60": rpo_clip_df.rolling(60, min_periods=45).mean().iloc[-1].to_numpy(dtype=np.float64),
        "cgo": cgo_raw,
        "vpd_20": -ac_df.rolling(20, min_periods=15).corr(v_df).iloc[-1].to_numpy(dtype=np.float64),
        "ivol_60": -(r_df - beta.mul(m_ret, axis=0)).rolling(20, min_periods=15).std().iloc[-1].to_numpy(dtype=np.float64),
    }

    rank_mask = U[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        lnmv = np.log(np.where(val_data["circ_mv"] > 0, val_data["circ_mv"], np.nan))
        R_si = {k: size_ind_neutralize_1d(raw_p[k], lnmv, ind_vec, rank_mask) for k in raw_p}

        pb_d = val_data["pb"]
        bp = ind_pct_rank_1d(np.where(pb_d > 0, 1.0 / pb_d, np.nan), ind_vec, rank_mask)
        dy = ind_pct_rank_1d(np.where(pb_d > 0, np.nan_to_num(val_data["dv_lag1"], nan=0.0), np.nan), ind_vec, rank_mask)
    ep_d = ind_pct_rank_1d(pit_f["ep_dedt"], ind_vec, rank_mask)
    roe_d = ind_pct_rank_1d(pit_f["roe_dedt"], ind_vec, rank_mask)
    qarp = ew_combine_1d({"ep": ep_d, "bp": bp, "dy": dy, "roe": roe_d}, ["ep", "bp", "dy", "roe"], min_valid=2)
    R_si["qarp_pit"] = size_ind_neutralize_1d(qarp, lnmv, ind_vec, rank_mask)

    age = np.clip(pit_f["ev_age"], 0.0, 130.0)
    dec = np.where(np.isfinite(age), np.exp(-age / 60.0), 0.0)
    acc_parts = {}
    for col in ("jump", "rev_jump", "sue", "sue_mv"):
        rk = ind_pct_rank_1d(pit_f[col], ind_vec, rank_mask)
        acc_parts[col] = np.where(np.isfinite(rk), 0.5 + (rk - 0.5) * dec, np.nan)
    acc_raw = ew_combine_1d(acc_parts, ["jump", "rev_jump", "sue", "sue_mv"], min_valid=1)
    R_si["acc_pit"] = size_ind_neutralize_1d(acc_raw, lnmv, ind_vec, rank_mask)

    cgo_cov_u = float(np.isfinite(cgo_raw[rank_mask]).mean()) if rank_mask.any() else 0.0
    cyq_absent = cyq_meta["cyq_absent_days"]
    if cyq_absent > cfg.cyq_ffill_limit or cgo_cov_u < 0.50:
        degraded_mode = "no_cgo"           # P3: cgo pillar missing, no substitute
        R_si["cgo"] = np.full_like(R_si["cgo"], np.nan)
        micro_keys = MICRO_5P_STD_KEYS
        core_keys = CORE_7P_STD_KEYS
    elif cyq_absent > 0:
        degraded_mode = f"cyq_ffill_{cyq_absent}d"
        micro_keys = MICRO_5P_STD_KEYS
        core_keys = CORE_7P_STD_KEYS
    else:
        degraded_mode = "none"
        micro_keys = MICRO_5P_STD_KEYS
        core_keys = CORE_7P_STD_KEYS

    arm1_score = ew_combine_1d(R_si, micro_keys, min_valid=4)
    arm2_score = ew_combine_1d(R_si, core_keys, min_valid=5)

    # Legacy `comp_rank_ew6` on `rank_mask` for `ew6_retain_15_30` reference book
    m_all = r_df.mean(axis=1, skipna=True)
    beta_ew6 = r_df.rolling(60, min_periods=45).cov(m_all).div(m_all.rolling(60, min_periods=45).var(), axis=0)
    ivol_ew6 = (r_df - beta_ew6.mul(m_all, axis=0)).rolling(20, min_periods=15).std().iloc[-1].to_numpy(dtype=np.float64)
    t_df = pd.DataFrame(val_data["turn_mat"], index=cal_idx, columns=codes)
    t_mean20 = t_df.rolling(20, min_periods=15).mean()
    turn_20_ew6 = t_mean20.iloc[-1].to_numpy(dtype=np.float64)
    turn_abn_ew6 = (t_df.rolling(5, min_periods=4).mean() / t_df.rolling(60, min_periods=45).mean()).iloc[-1].to_numpy(dtype=np.float64)
    vpd_10_ew6 = (-ac_df.rolling(10, min_periods=7).corr(v_df)).iloc[-1].to_numpy(dtype=np.float64)
    turn_cv_ew6 = (t_df.rolling(20, min_periods=15).std() / t_mean20).iloc[-1].to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        pe_d = val_data["pe_ttm"]
        ep_ew6 = np.where(pe_d > 0, 1.0 / pe_d, np.where(np.isnan(pe_d) & (pb_d > 0), -1e-6, np.nan))
        bp_ew6 = np.where(pb_d > 0, 1.0 / pb_d, np.nan)
    val_ew6 = (ind_pct_rank_1d(ep_ew6, ind_vec, rank_mask, min_group=1) + ind_pct_rank_1d(bp_ew6, ind_vec, rank_mask, min_group=1)) / 2.0
    ew6_raw = {
        "ivol_60_ew6": ivol_ew6,
        "turn_20_ew6": turn_20_ew6,
        "turn_abn_5_60_ew6": turn_abn_ew6,
        "val_comp_indneu_ew6": val_ew6,
        "vpd_10_ew6": vpd_10_ew6,
        "turn_cv_20_ew6": turn_cv_ew6,
    }
    ew6_ranks = {k: cs_pct_rank_1d(sgn * ew6_raw[k], rank_mask) for k, sgn in EW6_FACTORS}
    comp_ew6 = ew_combine_1d(ew6_ranks, [k for k, _ in EW6_FACTORS], min_valid=4)

    u_idx = np.where(rank_mask)[0]
    u_codes = [codes[j] for j in u_idx]
    assert_no_forbidden_boards(u_codes)

    roc_t_u = r_oc_arr[-1, u_idx]
    roc_med_u = float(np.nanmedian(roc_t_u)) if np.isfinite(roc_t_u).any() else 0.0
    e4a_tags = np.where(np.isfinite(roc_t_u) & (roc_t_u > roc_med_u), "CLOSE_DEFER_CANDIDATE", "OPEN_EXEC")
    ind_label_arr = [ind_names[ind_vec[j]] if ind_vec[j] >= 0 else "UNKNOWN" for j in u_idx]
    lim_u = limit_ratio_for_codes(u_codes, st_mask=None, trade_date=signal_date)

    scores_df = pd.DataFrame(
        {
            "ts_code": u_codes,
            "trade_date": signal_date,
            "name": [name_lookup.get(c, c) for c in u_codes],
            "board": [classify_board(c) for c in u_codes],
            "ind_sw1_id": ind_vec[u_idx],
            "ind_sw1_name": ind_label_arr,
            "open": O[-1, u_idx],
            "close": C[-1, u_idx],
            "pre_close": PC[-1, u_idx],
            "r_oc_t": roc_t_u,
            "circ_mv": val_data["circ_mv"][u_idx],
            "limit_ratio": lim_u,
            "neg_roc_60_si": R_si["neg_roc_60"][u_idx],
            "neg_roc_20_si": R_si["neg_roc_20"][u_idx],
            "rpo_clip_60_si": R_si["rpo_clip_60"][u_idx],
            "cgo_si": R_si["cgo"][u_idx],
            "vpd_20_si": R_si["vpd_20"][u_idx],
            "ivol_60_si": R_si["ivol_60"][u_idx],
            "qarp_pit_si": R_si["qarp_pit"][u_idx],
            "acc_pit_si": R_si["acc_pit"][u_idx],
            "aegis_micro_5p": arm1_score[u_idx],
            "aegis_core_7p": arm2_score[u_idx],
            "comp_rank_ew6": comp_ew6[u_idx],
            "exec_route_tag": np.where(cfg.enable_e4a_router & (cfg.basket_signal == "aegis_micro_5p"), e4a_tags, "OPEN_EXEC"),
            "micro5p_e4a_tag": e4a_tags,
            "degraded_mode": degraded_mode,
        }
    )
    scores_df["aegis_micro_5p_pct"] = scores_df["aegis_micro_5p"].rank(pct=True)
    scores_df["aegis_core_7p_pct"] = scores_df["aegis_core_7p"].rank(pct=True)
    scores_df["comp_rank_ew6_pct"] = scores_df["comp_rank_ew6"].rank(pct=True)
    scores_df = scores_df.sort_values(["aegis_core_7p", "ts_code"], ascending=[False, True]).reset_index(drop=True)

    basket = select_top20_buffered_basket(
        scores_df=scores_df,
        prev_holdings=prev_top20_holdings,
        signal_col=cfg.basket_signal,
        enter_top_k=cfg.enter_top_k,
        hold_top_k=cfg.hold_top_k,
        sw1_ind_cap=cfg.sw1_ind_cap,
        enable_e4a_router=cfg.enable_e4a_router,
    )

    provenance = {
        "signal_date": signal_date,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "mandate_boards": "SH_MAIN_SZ_MAIN_CHINEXT_ONLY_NO_STAR_NO_BSE",
        "degraded_mode": degraded_mode,
        "universe_degraded_mode": uni_prov["universe_degraded_mode"],
        "cyq_absent_days": int(cyq_absent),
        "cyq_fresh_on_signal_date": cyq_meta["cyq_fresh_on_signal_date"],
        "cgo_coverage_universe": round(cgo_cov_u, 4),
        "micro_5p_pillars_used": micro_keys,
        "ann_store": ann_store_status(cfg, signal_date),
        "core_7p_pillars_used": core_keys,
        "universe_size": int(len(scores_df)),
        "micro_5p_coverage": round(float(scores_df["aegis_micro_5p"].notna().mean()), 4),
        "core_7p_coverage": round(float(scores_df["aegis_core_7p"].notna().mean()), 4),
        "ew6_coverage": round(float(scores_df["comp_rank_ew6"].notna().mean()), 4),
        "roc_t_universe_median": round(roc_med_u, 6),
        "scores_sha256_16": hashlib.sha256(
            pd.util.hash_pandas_object(scores_df[["ts_code", "aegis_micro_5p", "aegis_core_7p"]], index=False).to_numpy().tobytes()
        ).hexdigest()[:16],
    }
    return scores_df, basket, provenance


def write_scorer_outputs(
    out_dir: Path, scores_df: pd.DataFrame, basket: dict[str, Any], provenance: dict[str, Any]
) -> dict[str, str]:
    assert_no_forbidden_boards(scores_df["ts_code"])
    safe_dir = assert_safe_write_path(out_dir)
    safe_dir.mkdir(parents=True, exist_ok=True)
    d = provenance["signal_date"]
    p_scores = safe_dir / f"aegis_scores_{d}.parquet"
    p_basket = safe_dir / f"aegis_top20_basket_{d}.json"
    p_prov = safe_dir / f"aegis_provenance_{d}.json"

    scores_df.to_parquet(p_scores, index=False, compression="zstd")
    p_basket.write_text(json.dumps({**provenance, "basket": basket}, ensure_ascii=False, indent=2), encoding="utf-8")
    p_prov.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"scores_parquet": str(p_scores), "basket_json": str(p_basket), "provenance_json": str(p_prov)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Aegis-Alpha v1 Fault-Tolerant Daily Cross-Sectional Live Scorer")
    ap.add_argument("--date", help="Signal date YYYYMMDD (default: latest session with daily cache)")
    ap.add_argument("--workspace", default=str(default_workspace()), help="Workspace root")
    ap.add_argument(
        "--out-dir",
        default=str(default_workspace() / "factor_factory/data/forward_shadow/multi_v1/scores"),
        help="Output directory for rankings parquet and Top-20 basket JSON",
    )
    ap.add_argument("--basket-signal", choices=["aegis_core_7p", "aegis_micro_5p"], default="aegis_core_7p")
    ap.add_argument("--enable-e4a-router", action="store_true", help="Optional E4a router (for Micro_5P only; Core_7P uses T+1 09:30 Open)")
    ap.add_argument("--cyq-cache-dir", help="Optional custom cyq_perf cache directory")
    ap.add_argument("--no-panel-cyq-fallback", action="store_true", help="Disable historical panel cyq fallback")
    ap.add_argument("--prefer-panel-exact-inputs", action="store_true", help="Use exact panel float32 valuation/cyq on <=20261009")
    ap.add_argument("--prev-basket-json", help="Optional previous Top-20 basket JSON for buffer continuation")
    args = ap.parse_args(argv)

    prev_codes: set[str] = set()
    if args.prev_basket_json and Path(args.prev_basket_json).exists():
        b_prev = json.loads(Path(args.prev_basket_json).read_text(encoding="utf-8"))
        prev_codes = {str(x["ts_code"]) for x in b_prev.get("basket", b_prev).get("holdings", [])}

    cfg = ScorerConfig(
        workspace=Path(args.workspace).expanduser().resolve(),
        basket_signal=args.basket_signal,
        enable_e4a_router=bool(args.enable_e4a_router),
        cyq_cache_dir=Path(args.cyq_cache_dir).expanduser().resolve() if args.cyq_cache_dir else None,
        allow_panel_cyq_fallback=not args.no_panel_cyq_fallback,
        prefer_panel_exact_inputs=bool(args.prefer_panel_exact_inputs),
    )
    scores_df, basket, prov = score_cross_section(
        signal_date=args.date, cfg=cfg, prev_top20_holdings=prev_codes
    )
    paths = write_scorer_outputs(Path(args.out_dir), scores_df, basket, prov)
    print(json.dumps({**prov, "outputs": paths, "top5_core7p": [x["ts_code"] for x in basket["holdings"][:5]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
