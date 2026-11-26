#!/usr/bin/env python3
"""forward_shadow_v2: Core7P-native zero-paid forward shadow (passive, paper only).

Same machinery as forward_shadow_multi_v1 (frozen, only imported read-only): T+1-open D4 execution, 5 staggered
sub-books, official-before-09:30 ledger, deterministic replay.  Differences: the signal is Core7P-native
(``core7p_native_scorer``: aegis Core7P pillar math with zero-paid fundamentals, neg_roc_20 in cgo's place), the
official gate adds a fundamentals-health and ex-dividend condition instead of the x_jump ann-store fetch, and a
descriptive "profitable top-25" product book (dedt_ttm > 0, top 25 / hold <= 75 / SW1 cap 4) feeds the product page.

  --write-spec        freeze contracts/forward_shadow_v2.json (commit before the first official run; forbidden with --test)
  --date YYYYMMDD     signal date; --today-session / --prev-session resolve it from the trading calendar
  --test              write under v2/test/, no deadline guard, no contract check, never official
  --summary           print the evaluation summary (counts only before the first look date)
  --parity [D]        recompute D's scores now and compare with the stored ones (default: last decided date)
  --parity-prev       with --today-session: also run parity for the previous session

Writes ONLY factor_factory/data/forward_shadow/v2/ (v2/test/ for --test).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.dont_write_bytecode = True

import forward_shadow as fs  # noqa: E402  (frozen, read-only: calendar, deadline, jsonl io, committed_and_clean)
import forward_shadow_multi as fsm  # noqa: E402  (frozen, read-only: day_prices, day_quotes, _st, _grow, nw_t, ...)
import kline_quality_gate as kq  # noqa: E402
import mandate_guard as mg  # noqa: E402

TZ = fs.TZ
rp = fs.rp
SPEC_PATH = ROOT / "contracts/forward_shadow_v2.json"
FS_V1 = ROOT / "data/forward_shadow"
OUT = ROOT / "data/forward_shadow/v2"
WS = ROOT.parent
H = 5
SIDES = {"9.6bp": 0.00096, "15.1bp": 0.00151, "20bp": 0.0020}
SKIP = 6
ANN_WAIT = timedelta(minutes=60)
TOPK_KEEP = 400
CAP = 4
FIN_MIN = {"qarp_real_dedt_cov": 0.90, "acc_real_cov": 0.90, "ocf_nonfin_cov": 0.85}
ACCOUNTS = (100000, 300000, 1000000)
ROLLBACK_RULE = "60个交易日相对全池累计超额跌破-9%则退回宽基Core7P前30展示"
PRODUCT_BOOK = "core7p_profitable_top25"
PRIMARY_BOOK = "core7p_native_10_30"
REF_BOOK = "ew6_retain_15_30"
PRODUCT_RULE = ("硬约束扣非TTM净利润>0；按Core7P-native得分排序，前25入选，排名<=75保留，申万一级行业最多4只，等权；"
                "研究性描述组合，不用于统计判断")
BOOKS = {
    # name: signal column, enter, hold, kind, needs fundamentals, judged
    PRIMARY_BOOK: {"col": "core7p_native", "enter": 0.10, "hold": 0.30, "kind": "pct", "needs_fin": True, "judged": True},
    PRODUCT_BOOK: {"col": "core7p_native", "enter": 25, "hold": 75, "kind": "profit_cap4", "needs_fin": True, "judged": False},
    REF_BOOK: {"col": "comp_rank_ew6", "enter": 0.15, "hold": 0.30, "kind": "pct", "needs_fin": False, "judged": False},
}

SPEC = {
    "spec_id": "forward_shadow_v2",
    "relation": "Core7P-native zero-paid shadow next to forward_shadow_multi_v1 (frozen, only read); writes only v2/",
    "universe": "rows of forward_shadow_v1 scores_<D>.parquet minus every code not matching "
                "^(60\\d{4}\\.SH|00\\d{4}\\.SZ|30\\d{4}\\.SZ)$ (scripts/mandate_guard.py), no proxy universe, no switch",
    "signals": {
        "core7p_native": "scripts/core7p_native_scorer.py: aegis Core7P pillar math (same windows, FWL size+SW1 "
                         "neutralisation, qarp ep/bp/dy/roe min_valid 2, acc jump/rev_jump/sue/sue_mv with exp(-age/60) "
                         "decay) with fundamentals from zero_paid_fin_v2; keys CORE_7P_NOCGO_KEYS = [neg_roc_60, "
                         "neg_roc_20, rpo_clip_60, vpd_20, ivol_60, qarp_pit, acc_pit], min_valid 5 of 7; no parameter tuning",
        "core7p_cgo_missing": "diagnostic only, never drives a book: v1 no_cgo behaviour (cgo pillar missing, no "
                              "substitute, STD keys, min_valid 5)",
        "comp_rank_ew6": "forward_shadow_v1 scores_<D> column, not recomputed (reference book only)",
        "rank": "pct rank over universe rows with a finite score",
    },
    "documented_discrepancy": "v1 no_cgo mode (multi_v1: 'no substitute') keeps cgo as a MISSING pillar (6 pillars); this "
                              "plan uses the constant CORE_7P_NOCGO_KEYS (neg_roc_20 replaces cgo). Both are computed every "
                              "night; the Spearman between them and the top-25 overlap are recorded in every decision "
                              "(scorer.variant_diag). Only core7p_native drives books.",
    "books": {k: {"signal": v["col"], "enter": v["enter"], "hold": v["hold"], "kind": v["kind"],
                  "needs_fundamentals": v["needs_fin"], "judged": v["judged"]} for k, v in BOOKS.items()},
    "book_rules": {
        "pct": "kept = held & pct >= 1-hold; size = #(pct >= 1-enter & buyable); fill with the best unheld entry names; "
               "equal weight 1/N on every rebalance",
        "profit_cap4": "descriptive product book (judged=false, no use for statistical judgment): constrained set = names "
                       "with finite dedt_ttm > 0 and a finite score; ranked by core7p_native; size 25, keep held names "
                       "ranked <= 75 within the constrained set under the <= 4 names per SW L1 industry cap, fill by rank "
                       "with <= 4 names per SW L1 industry, equal weight 1/N on rebalance; decision stores the top 400 of "
                       "the constrained ranking",
        "missed_rebalance": "a sub-book whose signal night has no decision keeps its old book and is non-official until "
                            "its next decided rebalance",
        "bench_ew": "equal weight of universe(D-1), daily, suspended = 0",
        "ew6_retain_15_30": "reference book recomputed inside v2 from the stored comp_rank_ew6 (no write to multi_v1)",
    },
    "rhythm": "5 staggered sub-books; sub-book k rebalances when (sessions since anchor) % 5 == k; book return = mean of "
              "the 5 sub-book daily returns",
    "execution": "forward_shadow_v1 D4: T+1 open; buy blocked if not traded or open >= limit-up - 0.001; sell blocked if "
                 "not traded or open <= limit-down + 0.001 (held, sold at the first sellable open, cash until next "
                 "rebalance); prices = stk_factor OHLCV first, daily_<d> fills missing codes",
    "costs_one_way": SIDES,
    "official": "written before T+1 09:30 Asia/Shanghai with the clock re-read AFTER scoring/product build right before the "
                "write; deadline not computable -> false; proxy universe, ex-dividend unavailability, or non-official "
                "forward_shadow_v1 record -> false; K-line gate fail -> no decision; fundamentals healthy "
                "(qarp_real_dedt_cov >= 0.90 AND acc_real_cov >= 0.90 AND ocf_nonfin_cov >= 0.85) is required for the "
                "Core7P books (otherwise non-official, gap reason fin_coverage_low); every decision with a non-official "
                "book gets a gaps.jsonl row (late / exdiv_unavailable / non_official_inputs / fin_coverage_low); no decision "
                "is written while zero_paid_daily / forward_shadow_v1 run, nor (until deadline - 60 min) while "
                "fundamentals are unhealthy",
    "schedule": "Hermes weekdays 23:40 --today-session (+ parity of the previous session) after em_ann_store (22:50); "
                "08:40 --prev-session premarket backup; 09:35 --prev-session cutoff; "
                "ops/hermes/stock/forward_shadow_v2.sh & forward_shadow_v2_cutoff.sh",
    "continuous_track": "all decisions drive the paper portfolio; reported separately; never re-decided",
    "product_file": {
        "path": "factor_factory/data/forward_shadow/v2/product/core7p_top25_<YYYYMMDD>.json (v2/test/product/ for --test)",
        "schema": "core7p_top25_v1",
        "holdings": "daily display target of the profit book: chain over decisions (previous display target kept while "
                    "ranked <= 75 in the constrained set under the SW1 cap 4, refilled to 25 with the SW1 cap 4); "
                    "BUY = not in the previous display target, else HOLD. The paper ledger tracks the 5 staggered sub-books "
                    "of the same rule, so the display is a model target, not one sub-book's executed holdings",
        "status": "degraded when fundamentals coverage is below the official thresholds or a pillar is degraded",
        "rollback": ROLLBACK_RULE + "; compounded 60-session net (9.6bp) excess of the top25 paper book vs bench_ew; "
                    "status only (computed and written into the product file: rollback.triggered = true triggers collapsed "
                    "view + alert banner on #/today), never acted on by code",
        "retail_friction": "100-share lots, 5 RMB minimum commission, commission 2.5bp + transfer 0.1bp + slippage 10bp, "
                           "stamp duty 5bp on sells; per account 100000/300000/1000000 RMB: names buyable at 1/N of the "
                           "account (signal-date close * (1 + 10bp slippage)) and unspent-cash share",
    },
    "evaluation": {
        "primary": "core7p_native_10_30 - ew6_retain_15_30 daily net (9.6bp), Newey-West t lag 5, official days",
        "secondary": "core7p_native_10_30 vs bench_ew; profit book vs bench_ew is descriptive only; 15.1bp and 20bp versions",
        "sequential_looks": {"2027-05-31": 3.46, "2027-11-30": 2.45, "2028-05-31": 2.00, "2028-11-30": 1.73},
        "futility": "at any look from 2027-11-30: t <= -1.0 -> close the book",
        "before_first_look": "status and counts only (no return statistics printed)",
        "warm_up": "the first 6 sessions after the anchor are excluded",
        "honesty": "power is low: sd of the daily-diff series ~8.6%/yr (dev); a true +6%/yr gives expected t ~0.7 after "
                   "1 year, so the looks mainly detect harm; passing a boundary early is unlikely",
    },
    "burn_in_for_product_page": "15 valid official sessions (official, no proxy, K-line gate ok, fundamentals healthy, parity "
                                "Spearman >= 0.999 across full matched universe with top25_overlap == 25) before the product "
                                "page may drop its research label; never removed by code",
    "notes": {
        "reproduction_check_2026-11-26": "Mac, full v1 inputs (cyq + Tushare VIP tables), 20 sessions 2026-03-02..2026-10-09, "
                                         "native scorer with zero_paid_fin_v2 on the seed vs ag.score_cross_section aegis_core_7p: "
                                         "Spearman core7p_cgo_missing vs v1 median 0.944 (min 0.934); core7p_native (neg_roc_20 "
                                         "in cgo's place, primary) vs v1 median 0.871 (min 0.833); top-25 overlap with v1 median "
                                         "10.5/25 (native) and 13.5/25 (cgo_missing): the cgo pillar matters at the top of the "
                                         "ranking. Fundamentals pillars reproduce v1 (qarp Spearman 1.000, acc median 0.999; "
                                         "cgo_missing vs v1 pillars with cgo dropped: median 0.9999, top-25 overlap 24-25).",
        "variant_diag_real_date_2026-11-25": "Spearman core7p_native vs core7p_cgo_missing 0.963, top-25 overlap 19/25 (VPS, no cgo "
                                             "available); recorded every night in decisions.jsonl scorer.variant_diag",
        "expectation": "the v2 books are NOT the v1 Core7P books: without cgo the top of the ranking differs from v1 Core7P "
                       "by about half of the top-25 names; judge them as their own pre-registered signal",
    },
    "writes": "factor_factory/data/forward_shadow/v2/ only",
}


def _canonical_spec_hash(obj: dict) -> str:
    body = {k: v for k, v in obj.items() if k not in ("spec_hash", "frozen_at")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def spec_hash() -> str:
    return _canonical_spec_hash(SPEC)


def _now() -> datetime:
    return datetime.now(TZ)


def _write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.stem}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp_path = fh.name
            fh.write(json.dumps(obj, ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _write_parquet_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp_path = fh.name
        df.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


class _DeadlineCrossed(Exception):
    pass


def _append_jsonl_atomic(path: Path, rec: dict, *, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    payload = existing + line
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp_path = fh.name
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def paths(test: bool) -> dict[str, Path]:
    base = OUT / "test" if test else OUT
    for sub in ("", "scores", "runs", "product"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return {"base": base, "decisions": base / "decisions.jsonl", "gaps": base / "gaps.jsonl",
            "parity": base / "parity.jsonl", "daily": base / "daily.parquet", "state": base / "state.json",
            "scores": base / "scores", "product": base / "product"}


def board_ok(codes) -> np.ndarray:
    """Strict mandate regex (60xxxx.SH / 00xxxx.SZ / 30xxxx.SZ)."""
    return mg.is_allowed_mandate_board(list(codes))


# ---------------------------------------------------------------- inputs

def v1_recorded(d: str) -> dict | None:
    return next((b for b in fs.read_jsonl(FS_V1 / "books.jsonl") if b.get("signal_date") == d), None)


def pipelines_running() -> bool:
    for pat in (r"zero_paid_daily\.py", r"scripts/forward_shadow\.py"):
        try:
            rc = subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode
        except OSError:
            return True
        if rc != 1:
            return True
    return False


def exdiv_ok(d: str) -> bool:
    """Fail closed unless daily_<d>.parquet and crosscheck_<d>.json confirm ex-dividend adjustment is valid."""
    dp = fsm.rp.BACKTEST_CACHE / f"daily_{d}.parquet"
    if not dp.exists():
        return False
    try:
        cols = pd.read_parquet(dp)
    except Exception:
        return False
    if cols.empty:
        return False
    has_tdx = True if "source_provider" not in cols.columns else bool(
        (cols["source_provider"].astype("string").fillna("") == "tdx_api").any()
    )
    if has_tdx and "pre_close_source" not in cols.columns:
        return False
    if "pre_close_source" in cols.columns:
        pcs = cols["pre_close_source"].astype("string").fillna("")
        if has_tdx:
            if "source_provider" in cols.columns:
                tdx_mask = cols["source_provider"].astype("string").fillna("") == "tdx_api"
                if bool((pcs[tdx_mask].str.strip() == "").any()):
                    return False
            elif bool((pcs.str.strip() == "").any()):
                return False
        if bool(pcs.str.contains("unavailable", regex=False).any()):
            ev_p = WS / f"stock_data/03-working/fundamental_cache/ex_dividend/events_{d}_{d}.parquet"
            if not ev_p.exists():
                return False
            try:
                if len(pd.read_parquet(ev_p)) != 0:
                    return False
            except Exception:
                return False
    cc_p = WS / f"stock_data/03-working/health/price_crosscheck/crosscheck_{d}.json"
    if has_tdx:
        if not cc_p.exists():
            return False
        try:
            cc = json.loads(cc_p.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(cc, dict):
            return False
        if cc.get("pre_close_exdiv_ok") is not True:
            return False
        if str(cc.get("pre_close_exdiv_status") or "") != "ok":
            return False
        unavail = cc.get("pre_close_exdiv_unavailable_rows")
        if unavail is not None:
            if (
                isinstance(unavail, bool)
                or not isinstance(unavail, (int, float))
                or not math.isfinite(unavail)
                or unavail > 0
            ):
                return False
    elif cc_p.exists():
        try:
            cc = json.loads(cc_p.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(cc, dict):
            return False
        ex_val = cc.get("pre_close_exdiv_ok")
        ex_status = str(cc.get("pre_close_exdiv_status") or "")
        if ex_val is False or str(ex_val or "").startswith("error:") or ex_status.startswith("error:") or ex_status == "mismatch":
            return False
    return True


def score_day(d: str) -> tuple[pd.DataFrame, dict]:
    import core7p_native_scorer as cn
    from zero_paid_fin_v2 import REAL_DEDT_SOURCES

    v1 = pd.read_parquet(FS_V1 / "scores" / f"scores_{d}.parquet")[["ts_code", "comp_rank_ew6"]]
    v1["ts_code"] = v1["ts_code"].astype(str)
    v1 = v1[board_ok(v1["ts_code"])].copy()
    if not v1["ts_code"].is_unique:
        raise ValueError(f"v1 scores_{d}.parquet has duplicate ts_code after board filter")
    nat, meta = cn.score_date(d, ws=WS)
    if not nat["ts_code"].is_unique:
        raise ValueError(f"native scorer returned duplicate ts_code for {d}")
    df = v1.merge(nat, on="ts_code", how="left")
    df["ind_sw1_name"] = df["ind_sw1_name"].fillna("UNKNOWN")
    df["name"] = df["name"].fillna(df["ts_code"])
    df["dedt_source"] = df["dedt_source"].fillna("missing")
    nonfin = ~df["ind_sw1_name"].isin(("银行", "非银金融"))
    if "ocf_applicable" in df.columns:
        df["ocf_applicable"] = np.where(df["ocf_applicable"].isna(), nonfin, df["ocf_applicable"]).astype(bool)
    else:
        df["ocf_applicable"] = nonfin.to_numpy(dtype=bool)
    df["core7p_native_pct"] = df["core7p_native"].rank(pct=True)
    df = df.sort_values(["core7p_native", "ts_code"], ascending=[False, True], na_position="last").reset_index(drop=True)

    tot = int(len(df))
    if tot == 0:
        cov = {k: None for k in ("qarp_real_dedt_cov", "acc_real_cov", "ocf_nonfin_cov", "rev_ttm_cov", "dedt_ttm_cov")}
        cov["n_mask"] = 0
    else:
        real_src = df["dedt_source"].isin(REAL_DEDT_SOURCES).to_numpy(dtype=bool)
        dedt_fin = np.isfinite(pd.to_numeric(df["dedt_ttm"], errors="coerce").to_numpy(dtype=np.float64))
        if "_cov_qarp_real" in df.columns:
            qarp_ok = df["_cov_qarp_real"].fillna(False).to_numpy(dtype=bool)
        elif "qarp_pit_si" in df.columns:
            qarp_ok = np.isfinite(pd.to_numeric(df["qarp_pit_si"], errors="coerce").to_numpy(dtype=np.float64)) & real_src
        else:
            qarp_ok = dedt_fin & real_src
        if "_cov_acc_real" in df.columns:
            acc_ok = df["_cov_acc_real"].fillna(False).to_numpy(dtype=bool)
        elif "acc_pit_si" in df.columns:
            acc_ok = np.isfinite(pd.to_numeric(df["acc_pit_si"], errors="coerce").to_numpy(dtype=np.float64))
        else:
            acc_ok = np.isfinite(pd.to_numeric(df["core7p_native"], errors="coerce").to_numpy(dtype=np.float64))
        if "_cov_rev_ttm" in df.columns:
            rev_ok = df["_cov_rev_ttm"].fillna(False).to_numpy(dtype=bool)
        else:
            rev_ok = np.isfinite(pd.to_numeric(df["rev_yoy"], errors="coerce").to_numpy(dtype=np.float64))
        app = df["ocf_applicable"].to_numpy(dtype=bool)
        ocf_fin = np.isfinite(pd.to_numeric(df["ocf_ttm"], errors="coerce").to_numpy(dtype=np.float64))
        cov = {
            "n_mask": tot,
            "qarp_real_dedt_cov": float(qarp_ok.sum() / tot),
            "acc_real_cov": float(acc_ok.sum() / tot),
            "ocf_nonfin_cov": float((app & ocf_fin).sum() / app.sum()) if app.sum() else None,
            "rev_ttm_cov": float(rev_ok.sum() / tot),
            "dedt_ttm_cov": float(dedt_fin.sum() / tot),
        }
    df = df.drop(columns=[c for c in ("_cov_qarp_real", "_cov_acc_real", "_cov_rev_ttm") if c in df.columns])

    meta["coverage"] = cov
    meta["universe"]["size"] = tot
    meta["universe"]["rows_v1"] = int(len(v1))
    meta["universe"]["rows_matched"] = int(df["core7p_native"].notna().sum())
    meta["native_coverage"] = round(float(df["core7p_native"].notna().mean()), 4) if tot else 0.0
    meta["cgo_missing_coverage"] = round(float(df["core7p_cgo_missing"].notna().mean()), 4) if tot else 0.0
    if all(f"{k}_si" in df.columns for k in cn.CORE_7P_NOCGO_KEYS):
        pillar_cov = {
            k: round(float(np.isfinite(pd.to_numeric(df[f"{k}_si"], errors="coerce")).mean()), 4) if tot else 0.0
            for k in cn.CORE_7P_NOCGO_KEYS
        }
        meta["pillar_coverage"] = pillar_cov
        meta["degraded_pillars"] = sorted(k for k, v in pillar_cov.items() if v < cn.PILLAR_DEGRADED_BELOW)
    meta["scores_sha256_16"] = hashlib.sha256(pd.util.hash_pandas_object(
        df[["ts_code", "core7p_native", "core7p_cgo_missing"]], index=False).to_numpy().tobytes()).hexdigest()[:16]
    return df, meta


# ---------------------------------------------------------------- gates

def fin_healthy(cov: dict) -> tuple[bool, list[str]]:
    bad = []
    for k, thr in FIN_MIN.items():
        v = (cov or {}).get(k)
        if v is None or not (isinstance(v, (int, float)) and math.isfinite(v)) or v < thr:
            bad.append(k)
    return (not bad), bad


def is_late(deadline, now) -> bool:
    return deadline is None or now >= deadline


def official_base(test: bool, late: bool, universe_mode, v1: dict | None, kline_ok: bool,
                  exdiv_healthy: bool = True) -> bool:
    """Fail-closed: test runs, late runs (incl. deadline not computable), a proxy universe, a non-official
    forward_shadow_v1 record, a failed K-line gate, or unavailable ex-dividend adjustment make the decision non-official."""
    return bool((not test) and (not late) and universe_mode == "none" and v1 is not None
                and v1.get("official") is True and kline_ok and exdiv_healthy)


def official_reason(test: bool, late: bool, base: bool, fin_ok: bool, exdiv_healthy: bool = True) -> str | None:
    if test:
        return "test"
    if late:
        return "late"
    if not exdiv_healthy:
        return "exdiv_unavailable"
    if not base:
        return "non_official_inputs"
    if not fin_ok:
        return "fin_coverage_low"
    return None


def should_wait(test: bool, late: bool, running: bool, fin_ok: bool, deadline, now) -> str | None:
    """Do not write while the input pipelines run, nor (until deadline - 60 min) while fundamentals are unhealthy:
    an early non-official write would lock the sub-book non-official for 5 sessions."""
    if test or late:
        return None
    if running:
        return "waiting_for_pipelines"
    if (not fin_ok) and deadline - now > ANN_WAIT:
        return "waiting_for_fundamentals"
    return None


def parity_ok(
    spearman: dict,
    n: int,
    n_old: int | None = None,
    n_new: int | None = None,
    top25_overlap: int | None = None,
    finite_sets_ok: bool = True,
) -> bool:
    if not finite_sets_ok:
        return False
    if n < 100 or not spearman or not all(v is not None and np.isfinite(v) and v >= 0.999 for v in spearman.values()):
        return False
    if n_old is not None and n != n_old:
        return False
    if n_new is not None and n != n_new:
        return False
    if top25_overlap is not None and top25_overlap != min(25, n):
        return False
    return True


# ---------------------------------------------------------------- books

def _ranked(df: pd.DataFrame, col: str, constrained: bool = False) -> pd.Series:
    s = df.set_index("ts_code")[col].astype(float)
    if constrained:
        dd = pd.to_numeric(df.set_index("ts_code")["dedt_ttm"], errors="coerce")
        s = s[np.isfinite(dd.reindex(s.index)) & (dd.reindex(s.index) > 0)]
    s = s[np.isfinite(s)]
    return s.sort_index().sort_values(ascending=False, kind="mergesort")      # ties -> ts_code ascending


def profit_order(df: pd.DataFrame) -> list[str]:
    """Constrained ranking of the product book: dedt_ttm finite and > 0, then core7p_native desc."""
    return _ranked(df, "core7p_native", constrained=True).index.tolist()


def book_lists(df: pd.DataFrame, book: str) -> dict:
    spec = BOOKS[book]
    col, ent, hld, kind = spec["col"], spec["enter"], spec["hold"], spec["kind"]
    if kind == "pct":
        s = _ranked(df, col)
        pct = s.rank(pct=True)
        order = pct.sort_values(ascending=False, kind="mergesort")
        entry = order[order >= 1 - ent - 1e-12].index.tolist()
        hold = order[order >= 1 - hld - 1e-12].index.tolist()
        return {"entry": entry, "hold": sorted(hold)}
    order = profit_order(df)
    ind = df.set_index("ts_code")["ind_sw1_name"].reindex(order).fillna("UNKNOWN")
    entry = order[:TOPK_KEEP]
    hold = sorted(order[:hld])
    return {"entry": entry, "hold": hold, "size": ent,
            "industry": {c: ind[c] for c in sorted(set(entry) | set(hold))}}


def select_product_target(order: list[str], industry: dict, prev: set | list | None, size: int = 25,
                          hold_rank: int = 75, cap: int = CAP) -> list[str]:
    """Display target of the profit book: keep previous names ranked <= hold_rank under the SW1 cap,
    then fill by rank under the SW1 cap."""
    prev = set(prev or ())
    rank = {c: i for i, c in enumerate(order)}
    cnt: dict[str, int] = {}
    new: list[str] = []
    chosen: set[str] = set()
    for c in order[:hold_rank]:
        if c not in prev:
            continue
        if len(new) >= size:
            break
        g = industry.get(c, "UNKNOWN")
        if cnt.get(g, 0) >= cap:
            continue
        new.append(c)
        chosen.add(c)
        cnt[g] = cnt.get(g, 0) + 1
    for c in order:
        if len(new) >= size:
            break
        g = industry.get(c, "UNKNOWN")
        if c in chosen or cnt.get(g, 0) >= cap:
            continue
        new.append(c)
        chosen.add(c)
        cnt[g] = cnt.get(g, 0) + 1
    return sorted(new, key=lambda c: rank[c])


def retail_friction(
    prices: dict[str, float | None],
    accounts=ACCOUNTS,
    lot: int = 100,
    min_comm: float = 5.0,
    comm_bps: float = 2.5,
    transfer_bps: float = 0.1,
    slippage_bps: float = 10.0,
    target_n: int | None = None,
) -> dict:
    """Equal weight 1/N of the account per name (signal-date close with buy slippage); a name is buyable if at
    least one 100-share lot at p_eff = p * (1 + slippage_bps / 1e4) plus commission (>= 5 RMB) and transfer fee
    fits in its cash slice.  Missing-price names remain in target_n.  Unspent cash share is the cash drag."""
    n = target_n if target_n is not None else len(prices)
    out = {}
    for acct in accounts:
        slice_cash = acct / n if n else 0.0
        bought_eff, fees, nb = 0.0, 0.0, 0
        for p in prices.values():
            if not (p is not None and isinstance(p, (int, float, np.floating)) and np.isfinite(p) and p > 0):
                continue
            p_eff = float(p) * (1.0 + slippage_bps / 1e4)
            lots = int(slice_cash // (p_eff * lot))
            fee = 0.0
            notional_eff = 0.0
            while lots > 0:
                notional_eff = lots * lot * p_eff
                fee = max(min_comm, notional_eff * comm_bps / 1e4) + notional_eff * transfer_bps / 1e4
                if notional_eff + fee <= slice_cash + 1e-9:
                    break
                lots -= 1
            if lots > 0:
                bought_eff += notional_eff
                fees += fee
                nb += 1
        out[str(acct)] = {
            "names_buyable": nb,
            "cash_drag_pct": round(100.0 * max(0.0, 1.0 - (bought_eff + fees) / acct), 2) if acct else 0.0,
            "buy_fees_rmb": round(fees, 2),
            "invested_rmb": round(bought_eff, 2),
        }
    return out


# ---------------------------------------------------------------- settlement (deterministic replay)

def _solve_eq_net(avail: float, flex_w: list[float], sell_exited: float, side: float) -> float:
    """Solve n_flex * eq + side * (sell_exited + sum(|eq - w_c|)) = avail for eq >= 0."""
    n = len(flex_w)
    if n == 0 or avail <= 0.0:
        return 0.0
    eq = avail / n
    if side <= 0.0:
        return eq
    for _ in range(12):
        amt = sell_exited + sum(abs(eq - wc) for wc in flex_w)
        nxt = max(0.0, (avail - side * amt) / n)
        if abs(nxt - eq) <= 1e-15:
            return nxt
        eq = nxt
    return eq


def _targets(book: str, L: dict, held_keep: set, q: dict, pre_blocked: set | None = None) -> list:
    pre = set(pre_blocked or ()) - set(held_keep)
    if BOOKS[book]["kind"] == "pct":
        buyable = [c for c in L["entry"] if c in q and q[c][3]]
        new = list(held_keep)
        slots = max(len(buyable) - len(held_keep) - len(pre), 0)
        for c in buyable:
            if slots <= 0:
                break
            if c not in held_keep and c not in pre:
                new.append(c)
                slots -= 1
        return new
    ind = L["industry"]
    entry_list = list(L["entry"])
    entry_set = set(entry_list)
    ranked_kept = [c for c in entry_list if c in held_keep] + [c for c in sorted(held_keep) if c not in entry_set]
    unsellable_kept = [c for c in ranked_kept if not (c in q and q[c][4])]
    sellable_kept = [c for c in ranked_kept if c in q and q[c][4]]
    new: list[str] = []
    cnt: dict[str, int] = {}
    chosen: set[str] = set()
    for c in sorted(pre):
        g = ind.get(c, "UNKNOWN")
        cnt[g] = cnt.get(g, 0) + 1
    cap_slots = max(L["size"] - len(pre), 0)
    for c in unsellable_kept:
        g = ind.get(c, "UNKNOWN")
        new.append(c)
        chosen.add(c)
        cnt[g] = cnt.get(g, 0) + 1
    for c in sellable_kept:
        if len(new) >= cap_slots:
            break
        g = ind.get(c, "UNKNOWN")
        if cnt.get(g, 0) >= CAP:
            continue
        new.append(c)
        chosen.add(c)
        cnt[g] = cnt.get(g, 0) + 1
    for c in entry_list:
        if len(new) >= cap_slots:
            break
        g = ind.get(c, "UNKNOWN")
        if c in chosen or c in pre or cnt.get(g, 0) >= CAP or not (c in q and q[c][3]):
            continue
        new.append(c)
        chosen.add(c)
        cnt[g] = cnt.get(g, 0) + 1
    return new


def replay(decisions: list[dict], cal: list[str], upto: str) -> pd.DataFrame:
    """Deterministic paper replay of every decision (official or not) with D4 execution, 1/N on rebalance."""
    if not decisions:
        return pd.DataFrame()
    anchor = decisions[0]["anchor"]
    sess = [d for d in cal if anchor <= d <= upto]
    ix = {d: i for i, d in enumerate(sess)}
    Q = {d: fsm.day_quotes(d, fsm._st(d)) for d in sess[1:]}
    uni = {dec["signal_date"]: dec.get("universe", []) for dec in decisions}
    by_exec: dict[int, dict[int, dict]] = {}
    for dec in decisions:
        i = ix.get(dec["signal_date"])
        if i is not None and i + 1 < len(sess):
            by_exec.setdefault(i + 1, {})[dec["subbook"]] = dec
    rows = []
    last_u: list = []
    for i in range(1, len(sess)):
        d, prev = sess[i], sess[i - 1]
        u = uni.get(prev) or last_u
        if uni.get(prev):
            last_u = uni[prev]
        q = Q[d]
        if u and q:
            r = float(np.mean([q[c][2] if c in q else 0.0 for c in u]))
            rows.append({"book": "bench_ew", "date": d, **{f"ret_{t}": r for t in SIDES}, "turnover": 0.0,
                         "n_hold": len(u), "official_day": prev in uni, "degraded_day": False})
    for book in BOOKS:
        acc: dict[str, dict] = {}
        for tag, side in SIDES.items():
            sub = {k: {"w": {}, "cash": 1.0, "pend": set(), "official": False, "degraded": False, "live": False}
                   for k in range(H)}
            ind_known: dict[str, str] = {}
            for i in range(1, len(sess)):
                d = sess[i]
                q = Q[d]
                todays = by_exec.get(i, {})
                for dec_i in todays.values():
                    L_i = (dec_i.get("books") or {}).get(book) or {}
                    if "industry" in L_i:
                        ind_known.update(L_i["industry"] or {})
                due_k = (i - 1) % H
                if not q:
                    for k, sb in sub.items():
                        if k == due_k or k in todays:
                            sb["official"] = False
                    continue
                rets, to, nh, offi, degr = [], 0.0, 0.0, True, False
                for k, sb in sub.items():
                    V0 = sum(sb["w"].values()) + sb["cash"]
                    dec = todays.get(k)
                    if dec is not None and book in dec["books"]:
                        L = dec["books"][book]
                        if "industry" in L:
                            L = {**L, "industry": {**ind_known, **L["industry"]}}
                        hold = set(L["hold"])
                        w = fsm._grow(sb["w"], q, 0)
                        V = sum(w.values()) + sb["cash"]
                        held_keep = {c for c in w if c in hold and c not in sb["pend"]}
                        pre_blocked = {c for c in w if c not in held_keep and not (c in q and q[c][4])}
                        newset = set(_targets(book, L, held_keep, q, pre_blocked=pre_blocked))
                        newset = {c for c in newset if (c in w) or (c in q and q[c][3])}
                        blocked = {c for c in w if c not in newset and not (c in q and q[c][4])}
                        frozen = {c: w[c] for c in blocked}
                        flex_set = set(newset)
                        while flex_set:
                            avail = V - sum(frozen.values())
                            sell_exited = sum(w[c] for c in set(w) - set(frozen) - flex_set)
                            eq_net = _solve_eq_net(avail, [w.get(c, 0.0) for c in flex_set], sell_exited, side)
                            newly_frozen = {}
                            for c in flex_set:
                                if c not in w:
                                    continue
                                wc = w[c]
                                if c not in q:
                                    newly_frozen[c] = wc
                                elif eq_net > wc + 1e-12 and not q[c][3]:
                                    newly_frozen[c] = wc
                                elif eq_net < wc - 1e-12 and not q[c][4]:
                                    newly_frozen[c] = wc
                            if not newly_frozen:
                                break
                            frozen.update(newly_frozen)
                            flex_set -= set(newly_frozen)
                        avail = V - sum(frozen.values())
                        sell_exited = sum(w[c] for c in set(w) - set(frozen) - flex_set)
                        tgt = dict(frozen)
                        if flex_set:
                            eq_net = _solve_eq_net(avail, [w.get(c, 0.0) for c in flex_set], sell_exited, side)
                            for c in flex_set:
                                tgt[c] = eq_net
                        amt = sum(abs(tgt.get(c, 0.0) - w.get(c, 0.0)) for c in set(tgt) | set(w))
                        cost = side * amt
                        sb["cash"] = max(0.0, avail - sum(tgt.get(c, 0.0) for c in flex_set) - cost)
                        sb["w"] = {c: v for c, v in fsm._grow(tgt, q, 1).items() if v > 0}
                        sb["pend"] = blocked & set(sb["w"])
                        sb["official"] = bool(dec["official_books"].get(book, False))
                        sb["degraded"] = bool(dec.get("degraded", False))
                        sb["live"] = True
                        to += (amt / 2) / V if V > 0 else 0.0
                    elif sb["live"]:
                        if k == due_k:
                            sb["official"] = False
                        ex = {c for c in sb["pend"] if c in q and q[c][4]}
                        if ex:
                            w = fsm._grow(sb["w"], q, 0)
                            v_open = sum(w.values()) + sb["cash"]
                            proceeds = sum(w[c] for c in ex)
                            sb["cash"] += proceeds * (1 - side)
                            to += (proceeds / 2) / v_open if v_open > 0 else 0.0
                            for c in ex:
                                del w[c]
                            sb["pend"] = sb["pend"] - ex
                            sb["w"] = {c: v for c, v in fsm._grow(w, q, 1).items() if v > 0}
                            sb["pend"] &= set(sb["w"])
                        else:
                            sb["w"] = {c: v for c, v in fsm._grow(sb["w"], q, 2).items() if v > 0}
                            sb["pend"] &= set(sb["w"])
                    if sb["live"]:
                        rets.append((sum(sb["w"].values()) + sb["cash"]) / V0 - 1)
                        nh += len(sb["w"])
                        offi &= sb["official"]
                        degr |= sb["degraded"]
                if len(rets) == H:
                    a = acc.setdefault(d, {"book": book, "date": d, "turnover": to / H, "n_hold": nh / H,
                                           "official_day": bool(offi), "degraded_day": bool(degr)})
                    a[f"ret_{tag}"] = float(np.mean(rets))
        rows.extend(acc.values())
    return pd.DataFrame(rows).sort_values(["book", "date"]).reset_index(drop=True) if rows else pd.DataFrame()


def settle(P: dict, cal: list[str], upto: str) -> dict:
    decs = fs.read_jsonl(P["decisions"])
    df = replay(decs, cal, upto)
    if not df.empty:
        _write_parquet_atomic(P["daily"], df)
    return {"settled_days": int(df.date.nunique()) if not df.empty else 0, "decisions": len(decs),
            "official_decisions": sum(1 for x in decs if x.get("official"))}


def rollback_status(daily: pd.DataFrame | None, book: str = PRODUCT_BOOK, window: int = 60,
                    threshold: float = -0.09, tag: str = "ret_9.6bp") -> dict:
    """Compounded net (9.6bp) excess of the product paper book vs bench_ew over the last ``window`` settled sessions."""
    base = {"rule": ROLLBACK_RULE, "sessions": 0, "rel_excess_60d": None, "triggered": False}
    if daily is None or len(daily) == 0 or tag not in daily:
        return base
    piv = daily.pivot(index="date", columns="book", values=tag)
    if book not in piv or "bench_ew" not in piv:
        return base
    dd = piv[[book, "bench_ew"]].dropna().sort_index()
    base["sessions"] = int(len(dd))
    if len(dd) < window:
        return base
    last = dd.tail(window)
    rel = float((1 + last[book]).prod() - (1 + last["bench_ew"]).prod())
    base["rel_excess_60d"] = round(rel, 6)
    base["triggered"] = bool(rel < threshold)
    return base


# ---------------------------------------------------------------- product file

def _clean(v):
    if isinstance(v, (float, np.floating)):
        fv = float(v)
        return fv if math.isfinite(fv) else None
    return v


def build_product_file(d: str, df: pd.DataFrame, meta: dict, target: list[str], buys: set, official: bool,
                       fin_ok: bool, rollback: dict, friction: dict, generated_at: str,
                       constrained_rank_map: dict[str, int] | None = None) -> dict:
    import core7p_native_scorer as cn
    cov = meta.get("coverage", {})
    degraded = list(meta.get("degraded_pillars", []))
    status = "ok" if (fin_ok and not degraded) else "degraded"
    if constrained_rank_map is None:
        order = profit_order(df)
        constrained_rank_map = {c: i for i, c in enumerate(order, 1)}
    return {
        "schema": "core7p_top25_v1", "signal_date": d, "generated_at": generated_at, "book": PRODUCT_BOOK,
        "rule": PRODUCT_RULE, "status": status, "official": bool(official), "spec_hash": spec_hash(),
        "coverage": {"qarp_real_dedt_cov": _clean(cov.get("qarp_real_dedt_cov")), "acc_real_cov": _clean(cov.get("acc_real_cov")),
                     "ocf_nonfin_cov": _clean(cov.get("ocf_nonfin_cov")), "degraded_pillars": degraded},
        "rollback": rollback,
        "friction": {"lot": 100, "min_commission_rmb": 5, "slippage_bps": 10, "stamp_duty_sell_bps": 5,
                     "accounts": {a: {"names_buyable": v["names_buyable"], "cash_drag_pct": v["cash_drag_pct"]}
                                  for a, v in friction.items()}},
        "holdings": cn.product_rows(df, target, buys, constrained_rank_map=constrained_rank_map),
    }


def validate_product_file(obj: dict) -> list[str]:
    """Mirror of the exporter's validation (20-30 holdings, unique codes, code == ts_code[:6], dedt_ttm > 0, strict boards)."""
    err = []
    if obj.get("schema") != "core7p_top25_v1":
        err.append("schema")
    h = obj.get("holdings") or []
    if not 20 <= len(h) <= 30:
        err.append(f"n_holdings={len(h)}")
    codes = [x.get("ts_code") for x in h]
    if len(set(codes)) != len(codes):
        err.append("duplicate_codes")
    if not all(mg.is_allowed_mandate_code(c) for c in codes):
        err.append("board")
    for x in h:
        if x.get("code") != str(x.get("ts_code"))[:6]:
            err.append(f"code_mismatch:{x.get('ts_code')}")
        v = x.get("dedt_ttm")
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            err.append(f"dedt_ttm:{x.get('ts_code')}")
    blob = json.dumps(obj, ensure_ascii=False)
    if "/Users/" in blob or "/srv/" in blob or "/home/" in blob:
        err.append("absolute_path")
    return err


# ---------------------------------------------------------------- main run

def _top_ranked(df: pd.DataFrame) -> list[dict]:
    top = df.dropna(subset=["core7p_native"]).head(TOPK_KEEP)
    cols = ["ts_code", "ind_sw1_name", "core7p_native", "core7p_native_pct", "dedt_ttm", "dedt_source", "dedt_yoy",
            "rev_yoy", "roe_dedt", "latest_report_date", "latest_ann_date"]
    out = []
    for r in top[cols].itertuples(index=False):
        out.append({c: (_clean(float(v)) if isinstance(v, (float, np.floating)) else v) for c, v in zip(cols, r)})
    return out


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT.parent))
    except ValueError:
        return p.name


def _state_coverage(cov: dict) -> dict:
    return {k: _clean((cov or {}).get(k)) for k in ("qarp_real_dedt_cov", "acc_real_cov", "ocf_nonfin_cov")}


def _valid_json_dict(path: Path, required: dict[str, object] | None = None, keys: tuple[str, ...] = ()) -> dict | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    for k, v in (required or {}).items():
        if obj.get(k) != v:
            return None
    if any(k not in obj for k in keys):
        return None
    return obj


def _heal_decided(
    d: str,
    dec: dict,
    P: dict,
    st: dict,
    now: datetime,
    *,
    test: bool = False,
    is_latest: bool = True,
    gaps: list[dict] | None = None,
) -> None:
    """Reconstruct missing/incomplete product file, gaps, run_{d}.json, and latest state.json."""
    pp = P["product"] / f"core7p_top25_{d}.json"
    run_p = P["base"] / "runs" / f"run_{d}.json"

    expected_spec_hash = dec.get("spec_hash") or spec_hash()
    expected_n = len((dec.get("product") or {}).get("target") or [])
    pf_old = _valid_json_dict(
        pp,
        {"schema": "core7p_top25_v1", "signal_date": d},
        keys=("holdings", "status", "official", "spec_hash"),
    )
    need_pp = (
        pf_old is None
        or pf_old.get("spec_hash") != expected_spec_hash
        or not isinstance(pf_old.get("holdings"), list)
        or len(pf_old["holdings"]) != expected_n
    )
    run_old = _valid_json_dict(
        run_p,
        {"signal_date": d, "status": "decided"},
        keys=("official", "official_books", "product_status", "product_valid", "coverage"),
    )
    need_run = run_old is None
    if is_latest:
        st_old = _valid_json_dict(
            P["state"],
            {"last_signal_date": d, "signal_date": d, "status": "decided"},
            keys=("official", "official_books", "product_status", "product_valid", "coverage"),
        )
        need_state = st_old is None
    else:
        need_state = False

    official_books = dict(dec.get("official_books") or {})
    gaps_list = gaps if gaps is not None else fs.read_jsonl(P["gaps"])
    need_gap = (not test) and (not official_books or not all(official_books.values())) and (
        not any(g.get("signal_date") == d for g in gaps_list)
    )

    if not (need_pp or need_run or need_state or need_gap):
        return

    fin_ok = bool(dec.get("fin_healthy", False))
    late_dec = bool(dec.get("late", False))
    base_dec = bool(dec.get("official", False))
    ex_ok_dec = bool(dec.get("exdiv_ok", True))

    if need_gap:
        gap_rec = {
            "signal_date": d,
            "reason": official_reason(test, late_dec, base_dec, fin_ok, exdiv_healthy=ex_ok_dec),
            "official_books": official_books,
            "fin_failing": list(dec.get("fin_failing") or []),
            "exdiv_ok": ex_ok_dec,
            "at": dec.get("recorded_at") or now.isoformat(),
            "spec_hash": dec.get("spec_hash") or spec_hash(),
        }
        _append_jsonl_atomic(P["gaps"], gap_rec)

    if not (need_pp or need_run or need_state):
        return

    daily = pd.read_parquet(P["daily"]) if P["daily"].exists() else None
    daily_upto = daily[daily["date"] <= d] if daily is not None and "date" in daily.columns else daily
    rb = rollback_status(daily_upto)
    cov = dict(dec.get("coverage") or {})
    degraded_pillars = list(dec.get("degraded_pillars") or [])
    p_status = "ok" if (fin_ok and not degraded_pillars) else "degraded"
    p_valid = True
    if need_pp:
        sp = P["scores"] / f"v2_scores_{d}.parquet"
        if not sp.exists():
            raise RuntimeError(f"cannot self-heal {pp.name}: missing {sp.name}")
        df = pd.read_parquet(sp)
        prod = dec.get("product") or {}
        target = list(prod.get("target") or [])
        buys = set(prod.get("buys") or [])
        friction = dict(prod.get("friction") or {})
        order = profit_order(df)
        cr_map = {c: i for i, c in enumerate(order, 1)}
        pf = build_product_file(
            d, df, {"coverage": cov, "degraded_pillars": degraded_pillars},
            target, buys, bool(official_books.get(PRODUCT_BOOK, False)),
            fin_ok, rb, friction, dec.get("recorded_at") or now.isoformat(),
            constrained_rank_map=cr_map,
        )
        errs = validate_product_file(pf)
        if errs:
            pf["validation_errors"] = errs
        p_status = pf["status"]
        p_valid = not errs
        _write_json_atomic(pp, pf)
    elif pf_old is not None:
        p_status = pf_old.get("status", p_status)
        p_valid = not pf_old.get("validation_errors")

    out = {
        "signal_date": d,
        "status": "decided",
        "subbook": dec.get("subbook"),
        "official": bool(dec.get("official")),
        "official_books": official_books,
        "degraded": bool(dec.get("degraded")),
        "fin_healthy": fin_ok,
        "coverage": _state_coverage(cov),
        "degraded_pillars": degraded_pillars,
        "product_status": p_status,
        "product_valid": p_valid,
        "product_file": _rel(pp),
        "rollback": rb,
        **st,
    }
    if need_run:
        _write_json_atomic(run_p, {**out, "at": dec.get("recorded_at") or now.isoformat()})
    if need_state:
        _write_json_atomic(P["state"], {"last_signal_date": d, "updated_at": now.isoformat(), **out})


def _verify_frozen_spec() -> None:
    if not SPEC_PATH.exists():
        raise SystemExit("forward_shadow_v2 spec missing, changed or uncommitted: refusing")
    try:
        saved = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"forward_shadow_v2 spec unreadable: {exc}: refusing") from exc
    if not isinstance(saved, dict):
        raise SystemExit("forward_shadow_v2 spec invalid: refusing")
    computed_saved_hash = _canonical_spec_hash(saved)
    if (
        computed_saved_hash != saved.get("spec_hash")
        or computed_saved_hash != spec_hash()
        or not fs.committed_and_clean(SPEC_PATH)
    ):
        raise SystemExit("forward_shadow_v2 spec missing, changed or uncommitted: refusing")


@contextmanager
def _v2_lock(base_dir: Path):
    base_dir.mkdir(parents=True, exist_ok=True)
    lock_path = base_dir / ".v2.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def run(d: str | None, test: bool) -> dict:
    if not test:
        _verify_frozen_spec()
    cal = fs.official_sessions("99999999")
    if d is None:
        raise SystemExit("--date, --today-session or --prev-session is required")
    if d not in cal:
        raise SystemExit(f"{d} is not an official session")
    P = paths(test)
    with _v2_lock(P["base"]):
        return _run_locked(d, test, P, cal)


def _run_locked(d: str, test: bool, P: dict[str, Path], cal: list[str]) -> dict:
    decisions = fs.read_jsonl(P["decisions"])
    gaps = fs.read_jsonl(P["gaps"])
    now = _now()
    after = [x for x in cal if x > d]
    deadline = fs.entry_deadline(after)
    late = is_late(deadline, now)
    existing = next((x for x in decisions if x["signal_date"] == d), None)
    if existing is not None:
        latest_d = max(x["signal_date"] for x in decisions)
        st = settle(P, cal, max(d, latest_d))
        _heal_decided(d, existing, P, st, now, test=test, is_latest=(d == latest_d), gaps=gaps)
        return {"signal_date": d, "status": "already_decided", **st}
    v1 = v1_recorded(d)
    ready = v1 is not None and (FS_V1 / "scores" / f"scores_{d}.parquet").exists()
    if not ready:
        if late and not test and not any(g["signal_date"] == d for g in gaps):
            _append_jsonl_atomic(P["gaps"], {"signal_date": d, "reason": "forward_shadow_v1 not recorded by T+1 09:30",
                                             "at": now.isoformat(), "spec_hash": spec_hash()})
            return {"signal_date": d, "status": "gap_logged"}
        return {"signal_date": d, "status": "waiting_for_forward_shadow_v1"}
    if not test and pipelines_running():
        if late:
            if not any(g["signal_date"] == d for g in gaps):
                _append_jsonl_atomic(P["gaps"], {"signal_date": d, "reason": "pipelines_running_at_cutoff",
                                                 "at": now.isoformat(), "spec_hash": spec_hash()})
            return {"signal_date": d, "status": "gap_logged"}
        return {"signal_date": d, "status": "waiting_for_pipelines"}
    px = fsm.day_prices(d)
    gate = kq.check_day(px, pd.read_parquet(FS_V1 / "scores" / f"scores_{d}.parquet")["ts_code"].astype(str))
    if not gate["ok"]:
        if late and not test and not any(g["signal_date"] == d for g in gaps):
            _append_jsonl_atomic(P["gaps"], {"signal_date": d, "reason": "kline_gate_failed", "gate": gate,
                                             "at": now.isoformat(), "spec_hash": spec_hash()})
        return {"signal_date": d, "status": "kline_gate_failed", "gate": gate}
    ex_ok = exdiv_ok(d)
    df, meta = score_day(d)
    fin_ok, fin_bad = fin_healthy(meta["coverage"])
    wait = should_wait(test, late, False, fin_ok, deadline, now)
    if wait:
        return {"signal_date": d, "status": wait, "fin_failing": fin_bad, "coverage": meta["coverage"]}
    anchor = decisions[0]["anchor"] if decisions else d
    k = (len([x for x in cal if anchor <= x <= d]) - 1) % H
    books = {b: book_lists(df, b) for b in BOOKS}
    prev_t = next((x["product"]["target"] for x in reversed(decisions) if x.get("product")), [])
    order = profit_order(df)
    cr_map = {c: i for i, c in enumerate(order, 1)}
    L = books[PRODUCT_BOOK]
    ind_full = L["industry"] if len(order) <= TOPK_KEEP else df.set_index("ts_code")["ind_sw1_name"].to_dict()
    target = select_product_target(order, ind_full, prev_t, size=BOOKS[PRODUCT_BOOK]["enter"], hold_rank=BOOKS[PRODUCT_BOOK]["hold"])
    buys = set(target) - set(prev_t)
    px_close = df.set_index("ts_code")["close"].reindex(target).to_dict()
    friction = retail_friction(
        {c: (float(v) if pd.notna(v) else float("nan")) for c, v in px_close.items()},
        target_n=len(target),
    )
    top_ranked = _top_ranked(df)
    code_commit = rp.git_commit()
    _write_parquet_atomic(P["scores"] / f"v2_scores_{d}.parquet", df)

    # Re-read the clock AFTER building scores/books/product/friction, git_commit, and writing parquet, right BEFORE committing decisions.jsonl
    def _build_dec(rec_now: datetime, rec_late: bool) -> dict:
        rec_base = False if rec_late else official_base(
            test, rec_late, meta["universe"].get("universe_degraded_mode"), v1, bool(gate["ok"]),
            exdiv_healthy=ex_ok,
        )
        rec_off_books = (
            {b: False for b in BOOKS}
            if rec_late
            else {b: bool(rec_base and (fin_ok or not spec["needs_fin"])) for b, spec in BOOKS.items()}
        )
        return {
            "signal_date": d, "anchor": anchor, "subbook": k, "recorded_at": rec_now.isoformat(),
            "entry_deadline": deadline.isoformat() if deadline else None, "late": bool(rec_late),
            "spec_hash": spec_hash(), "code_commit": code_commit, "universe": df.ts_code.tolist(),
            "kline_gate": gate, "exdiv_ok": bool(ex_ok),
            "degraded": bool((not fin_ok) or meta["degraded_pillars"]), "fin_healthy": bool(fin_ok),
            "fin_failing": fin_bad, "coverage": meta["coverage"], "degraded_pillars": meta["degraded_pillars"],
            "scorer": {"universe": meta["universe"], "scores_sha256_16": meta["scores_sha256_16"],
                       "variant_diag": meta["variant_diag"], "native_coverage": meta["native_coverage"],
                       "cgo_missing_coverage": meta["cgo_missing_coverage"], "pillar_coverage": meta["pillar_coverage"],
                       "store_diag": meta["store_diag"], "keys": meta["keys"]},
            "books": books, "official_books": rec_off_books, "official": bool(rec_base),
            "product": {"target": target, "buys": sorted(buys), "prev_target_n": len(prev_t), "friction": friction},
            "top_ranked": top_ranked,
        }

    now = _now()
    late = is_late(deadline, now)
    rec = _build_dec(now, late)

    def _guard_deadline() -> None:
        if is_late(deadline, _now()):
            raise _DeadlineCrossed()

    guard = _guard_deadline if (not test and not late and deadline is not None) else None
    try:
        _append_jsonl_atomic(P["decisions"], rec, before_replace=guard)
    except _DeadlineCrossed:
        now = _now()
        late = True
        rec = _build_dec(now, late)
        _append_jsonl_atomic(P["decisions"], rec)
    else:
        if not test and not late and deadline is not None:
            post_now = _now()
            if is_late(deadline, post_now):
                _append_jsonl_atomic(P["gaps"], {
                    "signal_date": d,
                    "reason": "post_replace_deadline_crossed",
                    "at": post_now.isoformat(),
                    "spec_hash": spec_hash(),
                })
    base = rec["official"]
    official_books = rec["official_books"]
    if not all(official_books.values()) and not test:
        _append_jsonl_atomic(P["gaps"], {"signal_date": d, "reason": official_reason(test, late, base, fin_ok, exdiv_healthy=ex_ok),
                                         "official_books": official_books, "fin_failing": fin_bad, "exdiv_ok": bool(ex_ok),
                                         "at": now.isoformat(), "spec_hash": spec_hash()})
    st = settle(P, cal, d)
    daily = pd.read_parquet(P["daily"]) if P["daily"].exists() else None
    rb = rollback_status(daily)
    pf = build_product_file(d, df, meta, target, buys, official_books[PRODUCT_BOOK], fin_ok, rb, friction,
                            now.isoformat(), constrained_rank_map=cr_map)
    errs = validate_product_file(pf)
    if errs:
        pf["validation_errors"] = errs
    pp = P["product"] / f"core7p_top25_{d}.json"
    _write_json_atomic(pp, pf)
    out = {"signal_date": d, "status": "decided", "subbook": k, "official": rec["official"],
           "official_books": rec["official_books"], "degraded": rec["degraded"], "fin_healthy": fin_ok,
           "coverage": _state_coverage(meta["coverage"]), "degraded_pillars": list(meta.get("degraded_pillars", [])),
           "product_status": pf["status"], "product_valid": not errs, "product_file": _rel(pp),
           "rollback": rb, **st}
    _write_json_atomic(P["base"] / "runs" / f"run_{d}.json", {**out, "at": now.isoformat()})
    _write_json_atomic(P["state"], {"last_signal_date": d, "updated_at": now.isoformat(), **out})
    return out


# ---------------------------------------------------------------- evaluation / parity

def nw_t(x, lag: int = 5) -> float:
    return fsm.nw_t(np.asarray(x, float), lag)


def _ann(x: pd.Series):
    return round(float(x.mean()) * 244 * 100, 2) if len(x) else None


def summary(test: bool, today: str | None = None) -> dict:
    P = paths(test)
    decs = fs.read_jsonl(P["decisions"])
    par = {x["signal_date"]: x for x in fs.read_jsonl(P["parity"])}
    valid = [x for x in decs if x.get("official") and x.get("fin_healthy") and (par.get(x["signal_date"]) or {}).get("ok")]
    out = {"spec_hash": spec_hash(), "decisions": len(decs), "official": sum(1 for x in decs if x.get("official")),
           "gaps": len(fs.read_jsonl(P["gaps"])), "degraded_decisions": sum(1 for x in decs if x.get("degraded")),
           "valid_official_sessions": len(valid), "burn_in_target": 15}
    today = today or datetime.now(TZ).strftime("%Y-%m-%d")
    looks = SPEC["evaluation"]["sequential_looks"]
    due = [k for k in looks if k <= today]
    if not P["daily"].exists() or not decs:
        return out
    df = pd.read_parquet(P["daily"])
    cal = fs.official_sessions("99999999")
    anchor = decs[0]["anchor"]
    skip = set([x for x in cal if x > anchor][:SKIP])
    df = df[~df.date.isin(skip)]
    out["settled_days"] = int(df.date.nunique())
    if not due and not test:
        out["judgement"] = "before the first look date: counts only"
        return out
    offp = df.pivot(index="date", columns="book", values="official_day").fillna(False).astype(bool)
    degp = df.pivot(index="date", columns="book", values="degraded_day").fillna(False).astype(bool)
    res = {}
    for a, b in ((PRIMARY_BOOK, REF_BOOK), (PRIMARY_BOOK, "bench_ew"), (PRODUCT_BOOK, "bench_ew")):
        r = {}
        for tag in SIDES:
            piv = df.pivot(index="date", columns="book", values=f"ret_{tag}")
            if a not in piv or b not in piv:
                continue
            dd = (piv[a] - piv[b]).dropna()
            om = (offp[a] & offp[b]).reindex(dd.index).fillna(False).astype(bool)
            nd = ~(degp[a] | degp[b]).reindex(dd.index).fillna(False).astype(bool)
            o = dd[om]
            r[tag] = {"all_ann_pct": _ann(dd), "all_t": round(nw_t(dd.values), 2), "official_ann_pct": _ann(o),
                      "official_t": round(nw_t(o.values), 2), "n_official": int(o.notna().sum()),
                      "official_ex_degraded_ann_pct": _ann(dd[om & nd]), "n_degraded": int((~nd).sum()),
                      "descriptive_only": b == "bench_ew" and a == PRODUCT_BOOK}
        res[f"{a}-{b}"] = r
    out["paired"] = res
    to = df.groupby("book")["turnover"].mean() * 244
    out["turnover_ann_one_way"] = {k: round(float(v), 2) for k, v in to.items() if k != "bench_ew"}
    if due:
        out["look"] = {"date": due[-1], "boundary_t": looks[due[-1]]}
    out["rollback"] = rollback_status(df if len(df) else None)
    return out


def _top25_codes(frame: pd.DataFrame, col: str = "core7p_native") -> set[str]:
    sub = frame[["ts_code", col]].dropna()
    if sub.empty:
        return set()
    sub = sub.sort_values([col, "ts_code"], ascending=[False, True], kind="mergesort")
    return set(sub["ts_code"].head(25))


def parity(d: str, test: bool, recompute=None) -> dict:
    P = paths(test)
    p = P["scores"] / f"v2_scores_{d}.parquet"
    if not p.exists():
        return {"signal_date": d, "status": "no_stored_scores"}
    try:
        old = pd.read_parquet(p)
        if recompute is None:
            def recompute(dd):
                return score_day(dd)[0]
        sc = recompute(d)
        cols = ("core7p_native", "core7p_cgo_missing")
        old_codes = old["ts_code"].astype(str)
        sc_codes = sc["ts_code"].astype(str)
        uniq_ok = bool(old_codes.is_unique and sc_codes.is_unique)
        codes_ok = bool(uniq_ok and set(old_codes) == set(sc_codes))
        old_fin = {
            c: set(old.loc[np.isfinite(pd.to_numeric(old[c], errors="coerce")), "ts_code"].astype(str))
            for c in cols
        }
        new_fin = {
            c: set(sc.loc[np.isfinite(pd.to_numeric(sc[c], errors="coerce")), "ts_code"].astype(str))
            for c in cols
        }
        finite_sets_ok = bool(
            codes_ok and all(old_fin[c] == new_fin[c] and len(old_fin[c]) >= 100 for c in cols)
        )
        if uniq_ok:
            m = old[["ts_code", *cols]].merge(sc[["ts_code", *cols]], on="ts_code", suffixes=("", "_re"))
            n_matched = int(len(m))
            r = {}
            for c in cols:
                pair = m[[c, c + "_re"]].apply(pd.to_numeric, errors="coerce")
                pair = pair[np.isfinite(pair[c]) & np.isfinite(pair[c + "_re"])]
                sp = float(pair.corr(method="spearman").iloc[0, 1]) if len(pair) >= 3 else float("nan")
                r[c] = round(sp, 6) if np.isfinite(sp) else None
        else:
            n_matched = 0
            r = {c: None for c in cols}
        n_old, n_new = int(len(old)), int(len(sc))
        ov25 = len(_top25_codes(old, "core7p_native") & _top25_codes(sc, "core7p_native"))
        n_common_fin = len(old_fin["core7p_native"] & new_fin["core7p_native"])
        ov25_expected = min(25, n_common_fin)
        ok = (
            uniq_ok
            and finite_sets_ok
            and ov25 == ov25_expected
            and parity_ok(r, n_matched, n_old=n_old, n_new=n_new, top25_overlap=ov25, finite_sets_ok=finite_sets_ok)
        )
        rec = {
            "signal_date": d,
            "at": _now().isoformat(),
            "spearman": r,
            "n": n_matched,
            "n_old": n_old,
            "n_new": n_new,
            "n_matched": n_matched,
            "finite_sets_equal": finite_sets_ok,
            "finite_counts": {
                c: {"old": len(old_fin[c]), "new": len(new_fin[c]), "matched": len(old_fin[c] & new_fin[c])}
                for c in cols
            },
            "top25_overlap": int(ov25),
            "ok": ok,
        }
    except Exception as exc:  # noqa: BLE001
        rec = {
            "signal_date": d,
            "at": _now().isoformat(),
            "spearman": {},
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    with _v2_lock(P["base"]):
        _append_jsonl_atomic(P["parity"], rec)
    return rec


def resolve_session(today: str, which: str, cal: list[str] | None = None) -> str | None:
    return fsm.resolve_session(today, which, cal)


def _parse_ymd(val: str, flag: str) -> str:
    s = str(val).strip().replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise SystemExit(f"invalid {flag}: {val!r} (expected YYYYMMDD)")
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError as exc:
        raise SystemExit(f"invalid {flag}: {val!r}: {exc}") from exc
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-spec", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--as-of")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--parity", nargs="?", const="last", default=None)
    ap.add_argument("--today-session", action="store_true")
    ap.add_argument("--prev-session", action="store_true")
    ap.add_argument("--parity-prev", action="store_true")
    a = ap.parse_args(argv)
    if a.write_spec and a.test:
        raise SystemExit("--write-spec cannot be combined with --test")
    as_of = _parse_ymd(a.as_of, "--as-of") if a.as_of else None
    if a.date:
        a.date = _parse_ymd(a.date, "--date")
    if a.today_session or a.prev_session:
        ref_day = as_of or _now().strftime("%Y%m%d")
        d = resolve_session(ref_day, "today" if a.today_session else "prev")
        if d is None:
            print(json.dumps({"status": "not_a_session_today"}))
            return 0
        a.date = d
    if a.write_spec:
        with _v2_lock(OUT):
            dec_path = OUT / "decisions.jsonl"
            if dec_path.exists() and dec_path.read_text(encoding="utf-8").strip():
                raise SystemExit(f"refusing --write-spec: official decisions ledger already exists ({dec_path})")
            _write_json_atomic(SPEC_PATH, {**SPEC, "spec_hash": spec_hash(), "frozen_at": _now().isoformat()})
        print(f"wrote {SPEC_PATH} hash {spec_hash()}")
        return 0
    if a.summary:
        today_iso = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}" if as_of else None
        print(json.dumps(summary(a.test, today=today_iso), ensure_ascii=False, indent=1))
        return 0
    if a.parity is not None:
        d = a.parity
        if d == "last":
            decs = fs.read_jsonl(paths(a.test)["decisions"])
            d = decs[-1]["signal_date"] if decs else None
        if not d:
            print(json.dumps({"status": "no_decisions"}, ensure_ascii=False))
            return 0
        res = parity(_parse_ymd(d, "--parity"), a.test)
        print(json.dumps(res, ensure_ascii=False))
        return 1 if "error" in res else 0
    print(json.dumps(run(a.date, a.test), ensure_ascii=False, default=str))
    if a.parity_prev and (a.today_session or a.date):
        try:
            prev = resolve_session(a.date, "prev")
            if prev:
                print(json.dumps(parity(prev, a.test), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            err_rec = {
                "signal_date": a.date,
                "at": _now().isoformat(),
                "spearman": {},
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(json.dumps(err_rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
