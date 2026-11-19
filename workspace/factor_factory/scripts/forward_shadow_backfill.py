#!/usr/bin/env python3
"""Backfill track for forward_shadow_v1 (fisher 2026-11-19: "开补齐轨"). Never touches the official ledger.

The official v1 ledger (data/forward_shadow/books.jsonl, gaps.jsonl, contract forward_shadow_v1.json) stays as it is:
it is the record of what actually ran before each 09:30 open.  This track replays EVERY official session since the
anchor with the same frozen code (scripts/forward_shadow.py, same spec hash) and grades each day by its inputs:

  A  official v1 record exists (scores_<D>.parquet written on time); scores are taken from that file
  B  no official record, but the evening-frozen PIT inputs of D are on disk (pit_market/v2 daily_basic_<D>,
     universe_<D>, backtest_cache daily_<D>): scores recomputed by forward_shadow.py from those exact files
  C  frozen inputs missing; filled later from a substitute source (Eastmoney data center / carry) -- pending
     until such a file is supplied via --c-daily-basic-dir / --c-universe-dir

Every gap is filled unconditionally (no choice after seeing returns).  Rebalance books are recomputed on this track
with this track's own previous rebalance (buffered_top100 is path dependent); a rebalance after a pending C day is
marked provisional and recomputed once that day is filled.  The track is derived and fully rewritten on each run:

  data/forward_shadow/v1_backfill/{books.jsonl, settled.jsonl, scores/, summary.json, run.json}

Evaluation (fixed in advance): primary = A+B days; sensitivity = A only and A+B+C; if they disagree in sign,
no conclusion (FABLE playbook M15).

  --upto YYYYMMDD     last signal date (default: last session with a daily cache before today)
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
    inputs = {"pit_snapshot": src, "pit_sha16": sha16(Path(src)) if src else None,
              "daily_sha16": sha16(rp.BACKTEST_CACHE / f"daily_{d}.parquet")}
    return fv.assign(comp_rank_ew6=comp).reset_index(names="ts_code"), inputs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upto")
    ap.add_argument("--verify", action="store_true")
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
            grade = "B" if scores is not None else "C_pending"
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
    counts = {g: sum(1 for r in rows if r["grade"] == g) for g in ("A", "B", "C_pending")}
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
