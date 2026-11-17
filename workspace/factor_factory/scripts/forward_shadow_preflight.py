#!/usr/bin/env python3
"""Input readiness gate (and optional self-heal) for one forward_shadow_v1 signal date.

Research-side helper for greenfield_forward_shadow.sh. It never writes the ledger and never touches the
forward_shadow_v1 SPEC: it only checks that forward_shadow.py would see complete inputs for D, and with
--heal reruns the existing upstream producers once (stage1 PIT freeze, the 22:15 short-track stage) for
whatever is missing.

Why (2026-11-17 pre-mortem): forward_shadow.py raises without logging a gap when daily_basic/PIT for D is
missing, and silently tolerates a missing daily/stk_factor file for D (window_mats skips it), so a late or
throttled upstream either became a permanent gap (10-20..11-04, four rebalance days lost) or could become a
low-coverage official record.

Usage: forward_shadow_preflight.py --date YYYYMMDD [--heal] [--heal-timeout 1800]
Prints one JSON line; exit 0 = all inputs ready, 10 = still missing (message in "summary").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import forward_shadow as fs  # noqa: E402  (same input loaders the ledger uses)
import research_panel_v2 as rp  # noqa: E402

WS = HERE.parents[1]
ORCH = WS / "skills/stock-system-orchestrator/scripts"
ANALYZER = WS / "skills/stock-analyzer"
MIN_ROWS = 4000          # absolute floor; a full A-share cross-section is ~5,500 rows
MIN_RATIO = 0.97         # vs the median of the previous 5 sessions (20261106 daily had 4,904/5,572 rows = 88%)
MISSING_RC = 10
QUALITY_RC = 11
MIN_COVERAGE = 0.95      # recorded baseline 0.9965-0.9987
MIN_UNI_RATIO = 0.90     # vs median universe_size of the last 5 official books (baseline 2255-2297)


def _rows(path: Path, d: str) -> int:
    import pandas as pd
    if not path.exists():
        return 0
    f = pd.read_parquet(path, columns=["trade_date"])
    return int(len(f)) if set(f["trade_date"].astype(str)) == {d} else 0


def reference_rows(d: str) -> dict:
    import statistics
    prev = [x for x in fs.official_sessions(d) if x < d][-5:]
    ref = {}
    for k in ("daily", "stk_factor"):
        n = [r for r in (_rows(rp.BACKTEST_CACHE / f"{k}_{x}.parquet", x) for x in prev) if r]
        ref[k] = int(statistics.median(n)) if n else 0
    ref["daily_basic"] = ref["daily"]
    return ref


def check(d: str) -> dict:
    out = {"ref": reference_rows(d)}
    out["daily"] = _rows(rp.BACKTEST_CACHE / f"daily_{d}.parquet", d)
    out["stk_factor"] = _rows(rp.BACKTEST_CACHE / f"stk_factor_{d}.parquet", d)
    db = fs.load_daily_basic(d)
    out["daily_basic"] = 0 if db is None else int(len(db))
    pit, src = rp.load_pit_snapshot(d)
    out["pit_universe"] = 0 if pit is None else int(len(pit))
    out["pit_source"] = src
    return out


def missing(c: dict) -> list[str]:
    ref = c.get("ref") or {}
    return [k for k in ("daily", "stk_factor", "daily_basic", "pit_universe")
            if (c.get(k) or 0) < max(MIN_ROWS, MIN_RATIO * (ref.get(k) or 0))]


def deep(d: str) -> dict:
    """Dry computation with the ledger's own functions (nothing written): composite coverage and universe size."""
    import statistics
    cal = fs.official_sessions("99999999")
    sessions = [x for x in cal if x <= d][-fs.WINDOW:]
    w = fs.window_mats(sessions)
    uni = fs.universe(d, sessions)
    comp = fs.composite(fs.factor_values(w, d, uni, fs.industry_map()))
    books = [json.loads(l) for l in open(fs.FS / "books.jsonl") if l.strip()]
    sizes = [b["universe_size"] for b in books if b.get("official") and b["signal_date"] < d][-5:]
    ref = int(statistics.median(sizes)) if sizes else 0
    window_missing = [x for x in sessions[-60:] if not (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    q = {"coverage": round(float(comp.notna().mean()), 4), "universe": int(len(uni)), "universe_ref": ref,
         "window_daily_missing_60": window_missing, "db_missing_60": [x for x in w["_db_missing"] if x in sessions[-60:]]}
    q["bad"] = [k for k, bad in (("coverage", q["coverage"] < MIN_COVERAGE),
                                 ("universe", ref and q["universe"] < MIN_UNI_RATIO * ref)) if bad]
    return q


def research_running() -> bool:
    return subprocess.run(["pgrep", "-f", "run_post_publish_research.py|short_track_shadow_runner.py"],
                          capture_output=True).returncode == 0


def run(cmd: list[str], timeout: int) -> dict:
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=str(WS), capture_output=True, text=True, timeout=timeout)
        rc, tail = p.returncode, (p.stdout + p.stderr).strip().splitlines()[-3:]
    except subprocess.TimeoutExpired:
        rc, tail = "timeout", []
    return {"cmd": Path(cmd[1]).name if len(cmd) > 1 else cmd[0], "rc": rc,
            "seconds": round(time.monotonic() - t0), "tail": [t[:200] for t in tail]}


FREEZE = r"""
import sys, json
sys.path[:0] = [sys.argv[2], sys.argv[3]]
import stage1_data_prepare as s1
print(json.dumps(s1.freeze_pit_market_snapshot(s1.pl.init_tushare(), sys.argv[1]), ensure_ascii=False))
"""


def heal(d: str, c: dict, timeout: int) -> list[dict]:
    steps = []
    if research_running():
        return [{"skipped": "research job still running"}]
    if "daily" in missing(c) or "stk_factor" in missing(c):
        # Base tables come from the 19:30 pipeline (stage1); rerunning it here is out of scope.
        steps.append({"skipped": "daily/stk_factor missing: rerun the 19:30 pipeline first (not auto-healed)"})
        return steps
    v2 = rp.STOCK_WORKING / "fundamental_cache/pit_market/v2" / f"daily_basic_{d}.parquet"
    if not v2.exists():
        steps.append(run([sys.executable, "-c", FREEZE, d, str(ORCH), str(ANALYZER)], timeout=600))
    if missing(check(d)):
        steps.append(run([sys.executable, str(ORCH / "short_track_shadow_runner.py"), "--trade-date", d],
                         timeout=timeout))
    return steps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--heal", action="store_true")
    ap.add_argument("--deep", action="store_true", help="also dry-compute composite coverage / universe size")
    ap.add_argument("--heal-timeout", type=int, default=int(os.environ.get("FS_PREFLIGHT_HEAL_TIMEOUT", 1800)))
    a = ap.parse_args(argv)
    d = a.date
    from datetime import datetime
    cal = fs.official_sessions("99999999")
    books = [json.loads(l) for l in open(fs.FS / "books.jsonl") if l.strip()] if (fs.FS / "books.jsonl").exists() else []
    anchor = books[0]["signal_date"] if books else d
    reb = (len([x for x in cal if anchor <= x <= d]) - 1) % fs.H == 0
    deadline = fs.entry_deadline([x for x in cal if x > d])
    if any(b["signal_date"] == d for b in books):
        print(json.dumps({"date": d, "ok": True, "skip": "already_recorded", "summary": "already_recorded"}))
        return 0
    if deadline is not None and datetime.now(fs.TZ) >= deadline:
        # Past the entry open: forward_shadow.py itself logs the gap; never block that.
        print(json.dumps({"date": d, "ok": True, "skip": "past_deadline", "summary": "past_deadline"}))
        return 0
    c = check(d)
    rep = {"date": d, "is_rebalance": bool(reb), "entry_deadline": deadline.isoformat() if deadline else None,
           "before": c, "missing_before": missing(c)}
    if rep["missing_before"] and a.heal:
        rep["heal"] = heal(d, c, a.heal_timeout)
        c = check(d)
        rep["after"] = c
    miss = missing(c)
    rc = MISSING_RC if miss else 0
    summary = f"{d} 缺 {'/'.join(miss)}" if miss else "ready"
    if not miss and a.deep:
        try:
            rep["quality"] = q = deep(d)
            if q["bad"]:
                rc, summary = QUALITY_RC, (f"{d} 质量不达标：覆盖率 {q['coverage']:.3f}（下限 {MIN_COVERAGE}），"
                                           f"universe {q['universe']}（参考 {q['universe_ref']}）")
        except Exception as e:  # noqa: BLE001 - surfaced as not-ready, forward_shadow.py would fail the same way
            rc, summary = QUALITY_RC, f"{d} 预算失败：{type(e).__name__}: {str(e)[:120]}"
    rep["ok"], rep["summary"] = rc == 0, summary
    print(json.dumps(rep, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
