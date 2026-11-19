#!/usr/bin/env python3
"""Backfill track for forward_shadow_v1 (fisher 2026-11-19: "开补齐轨"). Never touches the official ledger.

The official v1 ledger (data/forward_shadow/books.jsonl, gaps.jsonl, contract forward_shadow_v1.json) stays as it is:
it is the record of what actually ran before each 09:30 open.  This track replays EVERY official session since the
anchor with the same frozen code (scripts/forward_shadow.py, same spec hash) and grades each day by its inputs:

  A  official v1 record exists (scores_<D>.parquet written on time); scores are taken from that file
  B  no official record, but the evening-frozen PIT inputs of D are on disk (pit_market/v2 daily_basic_<D>,
     universe_<D>, backtest_cache daily_<D>): scores recomputed by forward_shadow.py from those exact files
  C  frozen inputs missing; with --build-c, substitute inputs are built under v1_backfill/c_inputs/ from the
     Eastmoney data center valuation of D (em_valuation_fetch.py) plus a one-day carry of the previous frozen
     daily_basic (free/float ratio) and PIT snapshot; without them the day stays C_pending

Every gap is filled unconditionally (no choice after seeing returns).  Rebalance books are recomputed on this track
with this track's own previous rebalance (buffered_top100 is path dependent); a rebalance after a pending C day is
marked provisional and recomputed once that day is filled.  The track is derived and fully rewritten on each run:

  data/forward_shadow/v1_backfill/{books.jsonl, settled.jsonl, scores/, summary.json, run.json}

Evaluation (fixed in advance): primary = A+B days; sensitivity = A only and A+B+C; if they disagree in sign,
no conclusion (FABLE playbook M15).

  --upto YYYYMMDD     last signal date (default: last session with a daily cache before today)
  --build-c           build C inputs where an em_valuation_<D> file exists
  --verify            also recompute A days and compare with the official scores (reproducibility check)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.dont_write_bytecode = True

import forward_shadow as fs  # noqa: E402  (frozen v1 code, used read-only)

rp, gv, TZ = fs.rp, fs.gv, fs.TZ
V1 = fs.FS
OUT = V1 / "v1_backfill"
BOOK_KEYS = ("avoid_losers_70", "buffered_top100", "benchmark_ew")


def sha16(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else None


C_DIR = OUT / "c_inputs"
EM_DIR = rp.STOCK_WORKING / "fundamental_cache/em_valuation"   # skills/stock-analyzer/em_valuation_fetch.py


def _real_daily_basic(d: str) -> pd.DataFrame | None:
    for p in [rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet"] + [q / f"daily_basic_{d}.parquet" for q in ORIG_DB_DIRS]:
        if p.exists():
            f = pd.read_parquet(p)
            if {"free_share", "float_share", "turnover_rate_f"}.issubset(f.columns):
                return f
    return None


def build_c_inputs(d: str, cal: list[str]) -> dict | None:
    """Substitute daily_basic + PIT universe for a day whose frozen inputs are missing (grade C).

    daily_basic: Eastmoney data center valuation of d (pe_ttm, pb, circ_mv, shares; checked vs tdx close and the
    previous RDS file) + turnover_rate_f = vol / free_share (Tushare definition, %), free_share = previous real
    file's free/float ratio x d's float_share (offline check 11-16 -> 11-17: 99.8% within 0.5%).
    universe: previous real PIT snapshot carried one day (name from Eastmoney, ST if either says so, listing_days
    + calendar days, suspended = no trade in d's daily file -> universe_flag/tradable 0)."""
    em_p = EM_DIR / f"em_valuation_{d}.parquet"
    daily_p = rp.BACKTEST_CACHE / f"daily_{d}.parquet"
    prev = [x for x in cal if x < d]
    base_d = next((x for x in reversed(prev[-10:]) if _real_daily_basic(x) is not None), None)
    snap_d = next((x for x in reversed(prev[-10:]) if rp.load_pit_snapshot(x)[1] and C_DIR.as_posix() not in rp.load_pit_snapshot(x)[1]), None)
    if not (em_p.exists() and daily_p.exists() and base_d and snap_d):
        return None
    em = pd.read_parquet(em_p).set_index("ts_code")
    daily = pd.read_parquet(daily_p, columns=["ts_code", "vol"]).set_index("ts_code")
    base = _real_daily_basic(base_d).set_index("ts_code")
    ratio = (base["free_share"] / base["float_share"]).where(base["float_share"] > 0)
    db = em[["close", "pe", "pe_ttm", "pb", "ps_ttm", "total_share", "float_share", "total_mv", "circ_mv"]].copy()
    db["free_share"] = ratio.reindex(db.index) * db["float_share"]
    db["turnover_rate_f"] = daily["vol"].reindex(db.index) / db["free_share"]
    db["turnover_rate"] = daily["vol"].reindex(db.index) / db["float_share"]
    db = db.reset_index().assign(trade_date=d, source="C_eastmoney_datacenter+carry", carry_base=base_d)
    (C_DIR / "daily_basic").mkdir(parents=True, exist_ok=True)
    db.to_parquet(C_DIR / "daily_basic" / f"daily_basic_{d}.parquet", index=False)

    snap, src = rp.load_pit_snapshot(snap_d)
    u = snap.copy()
    u["trade_date"] = d
    u["listing_days"] = u["listing_days"] + (pd.Timestamp(d) - pd.Timestamp(snap_d)).days
    nm = em["name"].reindex(u["ts_code"]).values
    u["name"] = np.where(pd.notna(nm), nm, u["name"])
    u["is_st"] = u["is_st"].astype(bool) | u["name"].astype(str).str.contains("ST")
    traded = daily["vol"].reindex(u["ts_code"]).fillna(0).values > 0
    u["is_suspended"] = (u["list_status_at_date"].astype(str) == "L") & ~traded
    u.loc[u["is_suspended"], ["universe_flag", "tradable"]] = 0
    u["used_proxy"], u["completeness"] = False, "complete"
    (C_DIR / "pit_universe").mkdir(parents=True, exist_ok=True)
    u.to_parquet(C_DIR / "pit_universe" / f"universe_{d}.parquet", index=False)
    return {"em_sha16": sha16(em_p), "carry_daily_basic_from": base_d, "carry_universe_from": snap_d,
            "universe_carry_src": src, "turnover_f_coverage": round(float(db["turnover_rate_f"].notna().mean()), 4)}


# C inputs are visible only at the lowest priority, i.e. only where no frozen file exists
ORIG_DB_DIRS = tuple(fs.FORWARD_DB_DIRS)
ORIG_PIT_DIRS_FN = rp._pit_universe_dirs
fs.FORWARD_DB_DIRS = (*ORIG_DB_DIRS, C_DIR / "daily_basic")
rp._pit_universe_dirs = lambda: [*ORIG_PIT_DIRS_FN(), C_DIR / "pit_universe"]


def recompute_scores(d: str, cal: list[str]) -> tuple[pd.DataFrame | None, dict]:
    """forward_shadow.run() steps 1-2 for date d, from the frozen local files only."""
    sessions = [x for x in cal if x <= d][-fs.WINDOW:]
    w = fs.window_mats(sessions)
    db = fs.load_daily_basic(d)
    if d in w["_db_missing"] or db is None:
        return None, {"missing": "daily_basic"}
    try:
        uni = fs.universe(d, sessions)
    except RuntimeError as e:
        return None, {"missing": "pit_universe", "detail": str(e)[:120]}
    fv = fs.factor_values(w, d, uni, fs.industry_map())
    comp = fs.composite(fv)
    snap, src = rp.load_pit_snapshot(d)
    nxt = [x for x in cal if x > d]
    cutoff = datetime.strptime((nxt[0] if nxt else d) + "0930", "%Y%m%d%H%M").replace(tzinfo=TZ)
    db_path = next((p for p in [rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet"] +
                    [q / f"daily_basic_{d}.parquet" for q in fs.FORWARD_DB_DIRS] if p.exists()), None)
    written = {k: datetime.fromtimestamp(p.stat().st_mtime, TZ) for k, p in
               (("daily_basic", db_path), ("pit_universe", Path(src) if src else None),
                ("daily", rp.BACKTEST_CACHE / f"daily_{d}.parquet")) if p is not None and p.exists()}
    inputs = {"pit_snapshot": src, "pit_sha16": sha16(Path(src)) if src else None,
              "daily_sha16": sha16(rp.BACKTEST_CACHE / f"daily_{d}.parquet"),
              "daily_basic_path": str(db_path) if db_path else None,
              "written_at": {k: v.isoformat(timespec="minutes") for k, v in written.items()},
              # B1 = the day's own inputs were on disk before T+1 09:30; B2 = they arrived later (same date's data)
              "on_disk_before_cutoff": all(v <= cutoff for v in written.values())}
    return fv.assign(comp_rank_ew6=comp).reset_index(names="ts_code"), inputs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upto")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--build-c", action="store_true", help="build substitute inputs for C days (Eastmoney + carry)")
    a = ap.parse_args(argv)
    saved = json.loads(fs.SPEC_PATH.read_text())
    if saved.get("spec_hash") != fs.spec_hash():
        raise SystemExit("forward_shadow_v1 code no longer matches its frozen contract: refusing to backfill")
    official = {b["signal_date"]: b for b in fs.read_jsonl(V1 / "books.jsonl")}
    anchor = min(official)
    cal = fs.official_sessions("99999999")
    today = datetime.now(TZ).strftime("%Y%m%d")
    upto = a.upto or max(x for x in cal if x < today and (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists())
    days = [x for x in cal if anchor <= x <= upto]
    (OUT / "scores").mkdir(parents=True, exist_ok=True)

    rows, prev_book, pending_before, verify = [], None, False, {}
    for i, d in enumerate(days):
        is_reb = i % fs.H == 0
        off = official.get(d)
        grade, inputs, scores = None, {}, None
        if off is not None and off.get("official") and (V1 / "scores" / f"scores_{d}.parquet").exists():
            grade = "A"
            scores = pd.read_parquet(V1 / "scores" / f"scores_{d}.parquet")
            inputs = {"official_scores_sha16": sha16(V1 / "scores" / f"scores_{d}.parquet")}
            if a.verify:
                rec, _ = recompute_scores(d, cal)
                if rec is not None:
                    m = scores.merge(rec, on="ts_code", suffixes=("", "_re"))
                    diff = (m.comp_rank_ew6 - m.comp_rank_ew6_re).abs()
                    verify[d] = {"n": int(len(m)), "n_official": int(len(scores)), "max_abs_diff": float(diff.max()),
                                 "spearman": round(float(m[["comp_rank_ew6", "comp_rank_ew6_re"]].corr("spearman").iloc[0, 1]), 8)}
        else:
            scores, inputs = recompute_scores(d, cal)
            if scores is None and a.build_c:
                c_meta = build_c_inputs(d, cal)
                if c_meta:
                    scores, inputs = recompute_scores(d, cal)
                    inputs["c_build"] = c_meta
            used_c = any(C_DIR.as_posix() in str(inputs.get(k) or "") for k in ("daily_basic_path", "pit_snapshot"))
            grade = "C_pending" if scores is None else ("C" if used_c else "B")
        rec = {"signal_date": d, "grade": grade, "is_rebalance": bool(is_reb), "anchor": anchor,
               "official_in_v1": bool(off and off.get("official")), "inputs": inputs, "spec_hash": fs.spec_hash()}
        if scores is not None:
            scores.to_parquet(OUT / "scores" / f"scores_{d}.parquet", index=False)
            rec["universe_size"] = int(len(scores))
            rec["composite_coverage"] = float(scores.comp_rank_ew6.notna().mean())
        if is_reb:
            if scores is None:
                pending_before = True
            else:
                day = scores.rename(columns={"comp_rank_ew6": "_s"})
                rec["avoid_losers_70"] = gv.rule_avoid_losers(day, set())
                rec["buffered_top100"] = gv.rule_buffered_topk(day, set(prev_book) if prev_book else set())
                rec["benchmark_ew"] = day["ts_code"].tolist()
                rec["provisional"] = bool(pending_before)          # depends on a pending C rebalance
                if grade == "A" and off.get("is_rebalance"):
                    rec["matches_official_book"] = {k: sorted(rec[k]) == sorted(off.get(k, [])) for k in BOOK_KEYS}
                prev_book = rec["buffered_top100"]
        rows.append(rec)

    books_path, settled_path = OUT / "books.jsonl", OUT / "settled.jsonl"
    tmp = books_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            if r["is_rebalance"] and "buffered_top100" in r:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    tmp.replace(books_path)
    if settled_path.exists():
        settled_path.unlink()                                    # derived: recomputed from the books every run
    P = {"base": OUT, "books": books_path, "settled": settled_path, "gaps": OUT / "gaps.jsonl", "state": OUT / "state.json"}
    st = fs.settle(P, upto, cal)
    settled = fs.read_jsonl(settled_path)
    grade_of = {r["signal_date"]: r["grade"] for r in rows}
    for s in settled:
        s["grade"] = grade_of.get(s["signal_date"])
    with settled_path.open("w", encoding="utf-8") as fh:
        for s in settled:
            fh.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
    counts = {g: sum(1 for r in rows if r["grade"] == g) for g in ("A", "B", "C", "C_pending")}
    counts["B_late_inputs"] = [r["signal_date"] for r in rows
                               if r["grade"] == "B" and not r["inputs"].get("on_disk_before_cutoff", True)]
    reb = [{"signal_date": r["signal_date"], "grade": r["grade"], "provisional": r.get("provisional"),
            "matches_official_book": r.get("matches_official_book")} for r in rows if r["is_rebalance"]]
    summary = {"generated_at": datetime.now(TZ).isoformat(timespec="seconds"), "anchor": anchor, "upto": upto,
               "sessions": len(days), "grades": counts, "rebalances": reb, "settle": st,
               "settled_rows": len(settled), "verify_A": verify, "code_commit": rp.git_commit(),
               "spec_hash": fs.spec_hash()}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
