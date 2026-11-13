#!/usr/bin/env python
"""spike-1 acceptance runner (mechanically re-runnable).

Exercises acceptance conditions 2-5 of PHASE2_TIERA_REPORT.md §Spike-1
against the fork branch `spike1-phasecontext` mini pipeline, using an
injected clock + injected calendar (no wall-clock wait, no network).

Usage:
  /Users/fisher/.openclaw/venv/bin/python run_acceptance.py
  (must be run while the fork has branch spike1-phasecontext checked out,
   or pass --fork-ref to run against a worktree; default assumes files
   are present at the fork path on the current checkout)

Exit code 0 = all scenarios PASS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
FORK = Path("/Users/fisher/.openclaw/workspace/daily_stock_analysis")
PIPELINE = FORK / "spike_harness" / "mini_pipeline.py"
PY = FORK / "venv" / "bin" / "python"
CAL = SPIKE_DIR / "fixtures" / "calendar_202607.json"
OUT_ROOT = SPIKE_DIR / "outputs" / "acceptance_runs"

RESULTS = []


def run(name, args, expect_exit):
    outdir = OUT_ROOT / name
    if outdir.exists():
        shutil.rmtree(outdir)
    cmd = [str(PY), str(PIPELINE), "--outdir", str(outdir)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    manifest = {}
    mpath = outdir / "run_manifest.json"
    if mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    alerts = []
    apath = outdir / "alerts.jsonl"
    if apath.exists():
        alerts = [json.loads(l) for l in apath.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = proc.returncode == expect_exit
    return proc, manifest, alerts, ok, outdir


def check(name, cond, detail):
    status = "PASS" if cond else "FAIL"
    RESULTS.append((status, name, detail))
    print(f"[{status}] {name} — {detail}")


def main() -> int:
    cal_args = ["--calendar-file", str(CAL)]

    # ---- Acceptance 2a: 14:59 closing_auction, effective = prev session ----
    p, m, a, ok, _ = run("s1_1459", ["--fake-now", "2026-07-10T14:59:00"] + cal_args, expect_exit=5)
    pc = m.get("phase_context", {})
    check("A2a exit=5 (partial-bar bare run refused at 14:59)", ok, f"exit={p.returncode}")
    check("A2a manifest phase=closing_auction", pc.get("phase") == "closing_auction", f"phase={pc.get('phase')}")
    check("A2a effective_trade_date=20260709 (prev session)",
          pc.get("effective_trade_date") == "20260709", f"eff={pc.get('effective_trade_date')}")
    check("A2a is_partial_bar=True", pc.get("is_partial_bar") is True, f"partial={pc.get('is_partial_bar')}")

    # ---- Acceptance 2b: 15:01 postmarket, effective = today, full run ----
    p, m, a, ok, _ = run("s2_1501", ["--fake-now", "2026-07-10T15:01:00",
                                     "--latest-data-date", "20260710"] + cal_args, expect_exit=0)
    pc = m.get("phase_context", {})
    check("A2b exit=0 (postmarket full run)", ok, f"exit={p.returncode}")
    check("A2b manifest phase=postmarket", pc.get("phase") == "postmarket", f"phase={pc.get('phase')}")
    check("A2b effective_trade_date=20260710 (today)",
          pc.get("effective_trade_date") == "20260710", f"eff={pc.get('effective_trade_date')}")
    check("A2b lag_check behind=False", m.get("lag_check", {}).get("behind") is False,
          f"lag={m.get('lag_check')}")

    # ---- Acceptance 3: holiday -> non_trading + fetch stage refused ----
    p, m, a, ok, _ = run("s3_holiday", ["--fake-now", "2026-07-12T10:00:00"] + cal_args, expect_exit=2)
    pc = m.get("phase_context", {})
    check("A3 exit=2 (non_trading refusal)", ok, f"exit={p.returncode}")
    check("A3 manifest phase=non_trading", pc.get("phase") == "non_trading", f"phase={pc.get('phase')}")
    check("A3 fetch stage NOT entered", "fetch" not in m.get("stages", ["fetch"]),
          f"stages={m.get('stages')}")

    # ---- Acceptance 4: calendar unreachable -> exit!=0 + alert (fail-closed) ----
    p, m, a, ok, _ = run("s4_netdown", ["--fake-now", "2026-07-10T16:00:00", "--simulate-netdown"],
                         expect_exit=3)
    kinds = [x.get("kind") for x in a]
    check("A4 exit=3 (fail-closed, non-zero)", ok, f"exit={p.returncode}")
    check("A4 alert emitted (Discord seam stub)", "phase_context_unavailable" in kinds,
          f"alerts={kinds}")
    check("A4 no phase_context in manifest (no guessed context)",
          "phase_context" not in m, f"manifest_keys={sorted(m)}")

    # ---- Acceptance 5: 0710-type intraday cache pollution replay ----
    p, m, a, ok, outdir = run("s5_0710_replay",
                              ["--fake-now", "2026-07-10T11:00:00", "--trade-date", "20260710"] + cal_args,
                              expect_exit=4)
    kinds = [x.get("kind") for x in a]
    sandbox = outdir / "backtest_cache_sandbox"
    written = list(sandbox.glob("*")) if sandbox.exists() else []
    check("A5 exit=4 (partial-bar cache write blocked)", ok, f"exit={p.returncode}")
    check("A5 block alert emitted", "partial_bar_cache_write_blocked" in kinds, f"alerts={kinds}")
    check("A5 sandbox cache empty (nothing written)", len(written) == 0,
          f"files={[f.name for f in written]}")

    # ---- Supplementary S6a (R-1 revision): 07:47 premarket bare run MUST be refused ----
    # PHASE3_SPIKES_REPORT §2 R-1: integration point ② is
    # `is_partial_bar or phase is PREMARKET` (premarket is_partial_bar=False by
    # upstream semantics) — parity with the production 00:00-15:05 guard window.
    # (Pre-R-1 this scenario expected exit 0; the guard now refuses it.)
    p, m, a, ok, outdir = run("s6a_0747_premarket_refused", ["--fake-now", "2026-07-10T07:47:00"] + cal_args,
                              expect_exit=5)
    pc = m.get("phase_context", {})
    kinds = [x.get("kind") for x in a]
    sandbox = outdir / "backtest_cache_sandbox"
    check("S6a premarket bare run REFUSED (exit=5, R-1)", ok, f"exit={p.returncode}")
    check("S6a phase=premarket, effective=20260709 (NOT today)",
          pc.get("phase") == "premarket" and pc.get("effective_trade_date") == "20260709",
          f"phase={pc.get('phase')} eff={pc.get('effective_trade_date')}")
    check("S6a bare_run_refused alert emitted", "bare_run_refused" in kinds, f"alerts={kinds}")
    check("S6a nothing written (refused before fetch/cache stages)",
          "fetch" not in m.get("stages", ["fetch"]) and not sandbox.exists(),
          f"stages={m.get('stages')}")

    # ---- Supplementary S6b: explicit --trade-date at 07:47 keeps effective-date safety ----
    # operator-intent runs still pass ②; the original S6 demonstration holds:
    # premarket data is auto-labeled with the PREVIOUS completed session.
    p, m, a, ok, outdir = run("s6b_0747_explicit_date",
                              ["--fake-now", "2026-07-10T07:47:00", "--trade-date", "20260710"] + cal_args,
                              expect_exit=0)
    pc = m.get("phase_context", {})
    cache_name = Path(m.get("cache_write", "")).name
    check("S6b premarket run with explicit --trade-date exits 0", ok, f"exit={p.returncode}")
    check("S6b cache labeled with completed session (cyq_perf_20260709)",
          cache_name == "cyq_perf_20260709.json", f"cache={cache_name}")

    # ---- Supplementary: cached-calendar fallback keeps run alive with warning ----
    cache_file = OUT_ROOT / "s7_cache" / "calendar_cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CAL, cache_file)
    p, m, a, ok, _ = run("s7_cached_fallback",
                         ["--fake-now", "2026-07-10T16:00:00", "--simulate-netdown",
                          "--cache-file", str(cache_file)], expect_exit=0)
    pc = m.get("phase_context", {})
    check("S7 netdown+cache -> calendar_source=cached with warning",
          ok and pc.get("calendar_source") == "cached"
          and any(w.startswith("calendar_fetch_failed_used_cache") for w in pc.get("warnings", [])),
          f"exit={p.returncode} source={pc.get('calendar_source')} warnings={pc.get('warnings')}")

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
