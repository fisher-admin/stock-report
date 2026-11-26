#!/usr/bin/env python3
"""Core7P-native scorer (v2 track): aegis Core7P pillar math with zero-paid fundamentals.

Follows ``aegis_scorer.score_cross_section`` pillar math (same windows, FWL size + SW1
neutralisation, qarp = ep/bp/dy/roe (min_valid 2) and acc = jump/rev_jump/sue/sue_mv with
exp(-age/60) decay (min_valid 1)), with intentional PIT/forecast differences documented in
``SPEC`` (``contracts/forward_shadow_v2.json``):

* fundamentals come from ``ZeroPaidFinStoreV2`` (seed + causal ``em_ann_store``, strict
  non-mixed ``dedt_ttm``) rather than live Tushare VIP tables;
* forecast events use the relaxed ``-45d`` pre-period-end window while income/express keep
  ``eff_ann >= end_date``;
* universe & market return are filtered by ``mandate_guard.is_allowed_mandate_board``
  (strict ``60/00/30`` ASCII regex) BEFORE market-return, neutralisation, and coverage math;
* ``core7p_native`` uses ``neg_roc_20`` in place of ``cgo`` (since ``cyq_perf`` is unavailable).

Two variants are computed from the same pillars (no tuning, constants fixed in the v2 contract):

* primary    ``core7p_native``      keys CORE_7P_NOCGO_KEYS = [neg_roc_60, neg_roc_20, rpo_clip_60, vpd_20,
                                    ivol_60, qarp_pit, acc_pit], min_valid 5 of 7.  Drives the books.
* diagnostic ``core7p_cgo_missing`` v1 no_cgo behaviour: STD keys with the cgo pillar missing (NaN, no
                                    substitute), min_valid 5 of the 6 present.

DOCUMENTED DISCREPANCY (kept visible, not resolved silently): v1's no_cgo mode (multi_v1 contract:
"no substitute") keeps cgo as a MISSING pillar, whereas the v2 plan uses neg_roc_20 in cgo's place.
cyq_perf is gone on the VPS, so cgo can not be computed there at all.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.dont_write_bytecode = True

import aegis_scorer as ag  # noqa: E402  (frozen; pure functions + loaders only, never its writers)
import mandate_guard as mg  # noqa: E402
from zero_paid_fin_v2 import REAL_DEDT_SOURCES, ZeroPaidFinStoreV2  # noqa: E402

CORE_7P_NOCGO_KEYS = ["neg_roc_60", "neg_roc_20", "rpo_clip_60", "vpd_20", "ivol_60", "qarp_pit", "acc_pit"]
CORE_7P_CGO_MISSING_KEYS = list(ag.CORE_7P_STD_KEYS)   # [neg_roc_60, rpo_clip_60, cgo, vpd_20, ivol_60, qarp_pit, acc_pit]
MIN_VALID = 5
PILLAR_DEGRADED_BELOW = 0.50          # a pillar finite on < 50% of the universe is reported as degraded
FIN_MIN = {"qarp_real_dedt_cov": 0.90, "acc_real_cov": 0.90, "ocf_nonfin_cov": 0.85}


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def spearman(a, b) -> float:
    s = pd.DataFrame({"a": np.asarray(a, dtype=float), "b": np.asarray(b, dtype=float)}).dropna()
    if len(s) < 3:
        return float("nan")
    return float(s["a"].rank().corr(s["b"].rank()))


def topk_overlap(df: pd.DataFrame, a: str, b: str, k: int = 25) -> int:
    def top(col):
        d = df[["ts_code", col]].dropna().sort_values([col, "ts_code"], ascending=[False, True])
        return set(d["ts_code"].head(k))
    return len(top(a) & top(b))


def _names(cfg, signal_date: str, codes: list[str]) -> dict[str, str]:
    """PIT snapshot name first, stock_list.csv fallback (the v1 scores parquet has no name column)."""
    out: dict[str, str] = {}
    try:
        snap, _, _ = ag._load_pit_snapshot_flexible(cfg, signal_date, allow_proxy=True)
        if snap is not None and "name" in snap.columns:
            out.update({str(c): str(n) for c, n in zip(snap["ts_code"], snap["name"]) if pd.notna(n)})
    except Exception:  # noqa: BLE001
        pass
    slist = cfg.workspace / "stock_data/01-root/stock_list.csv"
    if slist.exists():
        try:
            sdf = pd.read_csv(slist)
            for c, n in zip(sdf.get("ts_code", []), sdf.get("name", [])):
                if pd.notna(c) and pd.notna(n):
                    out.setdefault(str(c), str(n))
        except Exception:  # noqa: BLE001
            pass
    return {c: out.get(c, c) for c in codes}


def load_mv_pb_hist(cfg, cal_upto: list[str], back: int = 140) -> pd.DataFrame:
    """Event-level total_mv / pb history as aegis' PITFundamentalStore sees it: research_panel_v2 up to
    RESEARCH_PANEL_END, daily_basic (hist or forward dirs) after it.  Limited to the last ``back`` sessions
    (the PIT event window is 130 sessions)."""
    lo = cal_upto[-back] if len(cal_upto) >= back else cal_upto[0]
    frames = []
    if cfg.panel_path.exists():
        bp = pd.read_parquet(cfg.panel_path, columns=["ts_code", "trade_date", "total_mv", "pb"],
                             filters=[("trade_date", ">=", lo), ("trade_date", "<=", ag.RESEARCH_PANEL_END)])
        bp["trade_date"] = bp["trade_date"].astype(str)
        bp["ts_code"] = bp["ts_code"].astype(str)
        frames.append(bp)
    for d in cal_upto:
        if d > ag.RESEARCH_PANEL_END and d >= lo:
            one = ag.load_daily_basic_one(cfg, d)
            if one is not None:
                frames.append(one[["ts_code", "trade_date", "total_mv", "pb"]])
    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", "total_mv", "pb"])
    return pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])


def score_date(signal_date: str, ws=None, store: ZeroPaidFinStoreV2 | None = None, cfg=None):
    """Return (scores_df, meta) for one signal date (strictly causal, hard mandate boards).

    scores_df rows = the strict-mandate universe on the signal date (rank_mask), sorted by core7p_native desc."""
    signal_date = str(signal_date).replace("-", "")
    ws = Path(ws) if ws else ROOT.parent
    cfg = cfg or ag.ScorerConfig(workspace=ws)
    cal_all = ag.get_official_calendar(cfg, "99999999")
    if signal_date not in cal_all:
        raise ValueError(f"{signal_date} is not an official trading session.")
    cal_upto = [d for d in cal_all if d <= signal_date]
    sessions = cal_upto[-cfg.window_sessions:]
    cal_pit = np.array(cal_upto, dtype="U8")

    mats, codes, name_lookup, uni_prov = ag.load_window_prices_and_universe(cfg, sessions, cal_upto)
    store = store or ZeroPaidFinStoreV2(workspace=ws)
    ind_map = store.industry_map(signal_date)
    ind_list = []
    for c in codes:
        ind_val = str(ind_map.get(c) or "").strip()
        ind_list.append("" if ind_val == "UNKNOWN" else ind_val)
    ind_names = sorted({v for v in ind_list if v})
    nid = {name: i for i, name in enumerate(ind_names)}
    ind_vec = np.array([nid[v] if v else -1 for v in ind_list], dtype=np.int16)
    val_data, _cyq_meta = ag.load_valuation_and_cyq(cfg, sessions, codes)   # cyq unused: cgo is never a native pillar

    strict_board = mg.is_allowed_mandate_board(codes)
    mv_hist = load_mv_pb_hist(cfg, cal_upto)
    pit_f = store.pit_fundamentals(signal_date, codes, cal_pit, val_data["total_mv"], val_data["pb"], ind_names=ind_list,
                                   mv_pb_hist=mv_hist)

    cal_idx = pd.Index(sessions)
    O, C, PC, V, traded, U = mats["O"], mats["C"], mats["PC"], mats["V"], mats["traded"], mats["U"]
    U_strict = np.asarray(U, dtype=bool) & strict_board[None, :]
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
    m_ret = r_df.where(pd.DataFrame(U_strict, index=cal_idx, columns=codes)).mean(axis=1)
    beta = r_df.rolling(60, min_periods=45).cov(m_ret).div(m_ret.rolling(60, min_periods=45).var(), axis=0)

    raw_p = {
        "neg_roc_60": -roc_df.rolling(60, min_periods=45).mean().iloc[-1].to_numpy(dtype=np.float64),
        "neg_roc_20": -roc_df.rolling(20, min_periods=15).mean().iloc[-1].to_numpy(dtype=np.float64),
        "rpo_clip_60": rpo_clip_df.rolling(60, min_periods=45).mean().iloc[-1].to_numpy(dtype=np.float64),
        "vpd_20": -ac_df.rolling(20, min_periods=15).corr(v_df).iloc[-1].to_numpy(dtype=np.float64),
        "ivol_60": -(r_df - beta.mul(m_ret, axis=0)).rolling(20, min_periods=15).std().iloc[-1].to_numpy(dtype=np.float64),
    }
    raw_rank_mask = np.asarray(U[-1], dtype=bool)
    rank_mask = raw_rank_mask & strict_board
    with np.errstate(divide="ignore", invalid="ignore"):
        lnmv = np.log(np.where(val_data["circ_mv"] > 0, val_data["circ_mv"], np.nan))
        R_si = {k: ag.size_ind_neutralize_1d(raw_p[k], lnmv, ind_vec, rank_mask) for k in raw_p}
        pb_d = val_data["pb"]
        bp = ag.ind_pct_rank_1d(np.where(pb_d > 0, 1.0 / pb_d, np.nan), ind_vec, rank_mask)
        dy = ag.ind_pct_rank_1d(np.where(pb_d > 0, np.nan_to_num(val_data["dv_lag1"], nan=0.0), np.nan), ind_vec, rank_mask)
    ep_d = ag.ind_pct_rank_1d(pit_f["ep_dedt"], ind_vec, rank_mask)
    roe_d = ag.ind_pct_rank_1d(pit_f["roe_dedt"], ind_vec, rank_mask)
    qarp = ag.ew_combine_1d({"ep": ep_d, "bp": bp, "dy": dy, "roe": roe_d}, ["ep", "bp", "dy", "roe"], min_valid=2)
    R_si["qarp_pit"] = ag.size_ind_neutralize_1d(qarp, lnmv, ind_vec, rank_mask)

    age = np.clip(pit_f["ev_age"], 0.0, 130.0)
    dec = np.where(np.isfinite(age), np.exp(-age / 60.0), 0.0)
    acc_parts = {}
    for col in ("jump", "rev_jump", "sue", "sue_mv"):
        rk = ag.ind_pct_rank_1d(pit_f[col], ind_vec, rank_mask)
        acc_parts[col] = np.where(np.isfinite(rk), 0.5 + (rk - 0.5) * dec, np.nan)
    acc_raw = ag.ew_combine_1d(acc_parts, ["jump", "rev_jump", "sue", "sue_mv"], min_valid=1)
    R_si["acc_pit"] = ag.size_ind_neutralize_1d(acc_raw, lnmv, ind_vec, rank_mask)

    primary = ag.ew_combine_1d(R_si, CORE_7P_NOCGO_KEYS, min_valid=MIN_VALID)
    R_v1 = dict(R_si)
    R_v1["cgo"] = np.full(len(codes), np.nan)               # v1 no_cgo: pillar missing, no substitute
    cgo_missing = ag.ew_combine_1d(R_v1, CORE_7P_CGO_MISSING_KEYS, min_valid=MIN_VALID)

    u_idx = np.where(rank_mask)[0]
    u_codes = [codes[j] for j in u_idx]
    ag.assert_no_forbidden_boards(u_codes)
    mg.assert_strict_mandate(u_codes)

    with np.errstate(divide="ignore", invalid="ignore"):
        cov = ZeroPaidFinStoreV2.coverage(pit_f, rank_mask)
    ep_fin = np.isfinite(np.asarray(pit_f["ep_dedt"], dtype=np.float64))
    real_src = np.isin(np.asarray(pit_f["dedt_source"], dtype=object), REAL_DEDT_SOURCES)
    acc_fin = np.isfinite(np.asarray(pit_f["jump"], dtype=np.float64)) | np.isfinite(np.asarray(pit_f["sue"], dtype=np.float64))
    rev_fin = np.isfinite(np.asarray(pit_f["rev_ttm"], dtype=np.float64))
    nm = _names(cfg, signal_date, u_codes)
    df = pd.DataFrame({
        "ts_code": u_codes,
        "name": [nm[c] for c in u_codes],
        "ind_sw1_name": [ind_list[j] if ind_list[j] else "UNKNOWN" for j in u_idx],
        "close": C[-1, u_idx], "circ_mv": val_data["circ_mv"][u_idx],
        "pe_ttm": val_data["pe_ttm"][u_idx], "pb": val_data["pb"][u_idx],
        "core7p_native": primary[u_idx], "core7p_cgo_missing": cgo_missing[u_idx],
        **{f"{k}_si": R_si[k][u_idx] for k in CORE_7P_NOCGO_KEYS},
        "dedt_ttm": pit_f["dedt_ttm"][u_idx], "dedt_yoy": pit_f["dedt_yoy"][u_idx], "rev_yoy": pit_f["rev_yoy"][u_idx],
        "roe_dedt": pit_f["roe_dedt"][u_idx], "ocf_ttm": pit_f["ocf_ttm"][u_idx],
        "ocf_applicable": np.asarray(pit_f["ocf_applicable"], dtype=bool)[u_idx],
        "dedt_source": np.asarray(pit_f["dedt_source"], dtype=object)[u_idx],
        "latest_report_date": np.asarray(pit_f["latest_report_date"], dtype=object)[u_idx],
        "latest_ann_date": np.asarray(pit_f["latest_ann_date"], dtype=object)[u_idx],
        "ev_age": pit_f["ev_age"][u_idx],
        "_cov_qarp_real": (ep_fin & real_src)[u_idx],
        "_cov_acc_real": acc_fin[u_idx],
        "_cov_rev_ttm": rev_fin[u_idx],
    })
    df["core7p_native_pct"] = df["core7p_native"].rank(pct=True)
    df = df.sort_values(["core7p_native", "ts_code"], ascending=[False, True]).reset_index(drop=True)

    pillar_cov = {k: round(float(np.isfinite(R_si[k][u_idx]).mean()), 4) if len(u_idx) else 0.0 for k in CORE_7P_NOCGO_KEYS}
    degraded = sorted(k for k, v in pillar_cov.items() if v < PILLAR_DEGRADED_BELOW)
    sp = spearman(df["core7p_native"], df["core7p_cgo_missing"])
    ov = topk_overlap(df, "core7p_native", "core7p_cgo_missing", 25)
    meta = {
        "signal_date": signal_date,
        "coverage": {k: (None if (isinstance(v, float) and not np.isfinite(v)) else v) for k, v in cov.items()},
        "degraded_pillars": degraded,
        "pillar_coverage": pillar_cov,
        "universe": {"size": int(len(df)), "universe_degraded_mode": uni_prov.get("universe_degraded_mode"),
                     "source": uni_prov.get("source"), "mode": uni_prov.get("mode"),
                     "rows_aegis_rank_mask": int(raw_rank_mask.sum()),
                     "rows_strict_mandate_mask": int(rank_mask.sum())},
        "native_coverage": round(float(df["core7p_native"].notna().mean()), 4) if len(df) else 0.0,
        "cgo_missing_coverage": round(float(df["core7p_cgo_missing"].notna().mean()), 4) if len(df) else 0.0,
        "scores_sha256_16": hashlib.sha256(pd.util.hash_pandas_object(
            df[["ts_code", "core7p_native", "core7p_cgo_missing"]], index=False).to_numpy().tobytes()).hexdigest()[:16],
        "variant_diag": {"spearman_native_vs_cgo_missing": None if not np.isfinite(sp) else round(sp, 4),
                         "top25_overlap": int(ov), "n": int(df[["core7p_native", "core7p_cgo_missing"]].dropna().shape[0])},
        "keys": {"primary": CORE_7P_NOCGO_KEYS, "diagnostic_cgo_missing": CORE_7P_CGO_MISSING_KEYS, "min_valid": MIN_VALID},
        "store_diag": dict(getattr(store, "last_diag", {}) or {}),
    }
    return df, meta


# ---------------------------------------------------------------- product table helper

def product_rows(
    df: pd.DataFrame,
    codes: list[str],
    buys: set[str] | None = None,
    constrained_rank_map: dict[str, int] | None = None,
) -> list[dict]:
    """Holdings rows of the core7p_top25_v1 product file for the ordered ``codes``."""
    buys = buys or set()
    ix = df.set_index("ts_code")
    has_cr_col = "constrained_rank" in ix.columns
    rows = []
    for i, c in enumerate(codes, 1):
        r = ix.loc[c]
        pct = _num(r["core7p_native_pct"])
        row = {
            "rank": i, "ts_code": c, "code": c[:6], "name": str(r["name"]), "industry": str(r["ind_sw1_name"]),
            "score_pct": None if pct is None else round(pct, 4),
            "dedt_ttm": _num(r["dedt_ttm"]), "dedt_yoy": _num(r["dedt_yoy"]), "rev_yoy": _num(r["rev_yoy"]),
            "roe_dedt": _num(r["roe_dedt"]), "ocf_ttm": _num(r["ocf_ttm"]), "ocf_applicable": bool(r["ocf_applicable"]),
            "pe_ttm": _num(r["pe_ttm"]), "pb": _num(r["pb"]), "dedt_source": str(r["dedt_source"]),
            "latest_report_date": str(r["latest_report_date"]), "latest_ann_date": str(r["latest_ann_date"]),
            "action": "BUY" if c in buys else "HOLD", "plan_status": "planned_T+1_open",
        }
        cr = None
        if constrained_rank_map is not None and c in constrained_rank_map:
            cr = int(constrained_rank_map[c])
        elif has_cr_col and pd.notna(r["constrained_rank"]):
            cr = int(r["constrained_rank"])
        if cr is not None:
            row["constrained_rank"] = cr
        rows.append(row)
    return rows


if __name__ == "__main__":   # pragma: no cover
    import json
    import time
    t0 = time.time()
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if d is None:
        raise SystemExit("usage: core7p_native_scorer.py YYYYMMDD")
    _df, _meta = score_date(d)
    print(json.dumps({**_meta, "seconds": round(time.time() - t0, 1)}, ensure_ascii=False, default=str, indent=1))
