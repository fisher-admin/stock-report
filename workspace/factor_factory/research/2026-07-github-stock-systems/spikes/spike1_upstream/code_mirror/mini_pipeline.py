#!/usr/bin/env python
"""spike-1 mini pipeline — rehearses the 4 factor_factory integration points
of PhaseContext without touching production scripts (isolation rule).

Each integration point is exactly ONE line of judgment, tagged ①-④, and
mirrors a real production call site (see PROPOSED_PATCHES.md for the exact
diffs that Wave1 will apply):

  ① stage0 builds PhaseContext once and writes it into run_manifest
     (prod: skills/stock-system-orchestrator/scripts/run_pipeline.py main()).
  ② bare-run guard reads refuse_bare_run_if_partial(ctx, ...) — i.e.
     ``is_partial_bar or phase is PREMARKET`` (R-1) — instead of scattered
     weekday/"15:05" string comparisons (prod: run_pipeline.py L433-440).
  ③ backtest_cache write is preceded by guard_backtest_cache_write(ctx,...)
     (prod: factor_factory/scripts/greenfield_multifactor_lib.py atomic writer).
  ④ pipeline_alert lag judgment compares against ctx.effective_trade_date
     (prod: skills/stock-system-orchestrator/scripts/stock_pipeline_alert.py
      _ak_latest_trade_date).

The Discord alert is STUBBED (written to alerts.jsonl + stderr): the real
webhook belongs to production pipeline_alert and firing it from a spike
would be a production side effect. The seam is identical.

Exit codes:
  0 ok | 2 non_trading refusal | 3 calendar unavailable (fail-closed)
  4 partial-bar cache write blocked | 5 bare-run refusal (partial-bar OR
    premarket, R-1 revision: 07:47-type premarket bare runs are refused)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.phase_context import (  # noqa: E402
    CN_TZ,
    PartialBarCacheWriteBlocked,
    Phase,
    PhaseContext,
    PhaseContextError,
    build_phase_context,
    guard_backtest_cache_write,
    refuse_bare_run_if_partial,
)


def _emit_alert(outdir: Path, payload: dict) -> None:
    """Discord alert stub: same seam as production pipeline_alert webhook."""
    outdir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": datetime.now(CN_TZ).isoformat(), **payload}, ensure_ascii=False)
    with (outdir / "alerts.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"DISCORD_ALERT_STUB {line}", file=sys.stderr)


def _load_calendar_file(path: Path):
    dates = json.loads(path.read_text(encoding="utf-8"))["trade_dates"]

    def provider():
        return dates

    return provider


def _netdown_provider():
    raise ConnectionError("simulated network down: akshare unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake-now", default="", help="ISO datetime, interpreted as Asia/Shanghai (injected clock)")
    ap.add_argument("--calendar-file", default="", help="JSON {trade_dates:[YYYYMMDD,...]} injected calendar")
    ap.add_argument("--simulate-netdown", action="store_true", help="calendar provider raises ConnectionError")
    ap.add_argument("--cache-file", default="", help="optional local calendar cache path")
    ap.add_argument("--trade-date", default="", help="explicit target trade date (like prod --trade-date)")
    ap.add_argument("--explicit-intraday", action="store_true", help="deliberate intraday cache artifact flag")
    ap.add_argument("--latest-data-date", default="", help="YYYYMMDD latest published/available data date, for ④")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest_path = outdir / "run_manifest.json"
    manifest: dict = {"run_id": f"spike1-{datetime.now(CN_TZ).strftime('%Y%m%dT%H%M%S')}", "stages": []}

    now_fn = None
    if args.fake_now:
        fake = datetime.fromisoformat(args.fake_now)
        now_fn = lambda: fake  # noqa: E731

    provider = None
    if args.simulate_netdown:
        provider = _netdown_provider
    elif args.calendar_file:
        provider = _load_calendar_file(Path(args.calendar_file))

    cache_path = Path(args.cache_file) if args.cache_file else None

    # ---------------- stage0: build PhaseContext once ----------------
    try:
        ctx: PhaseContext = build_phase_context(now_fn=now_fn, calendar_provider=provider, cache_path=cache_path)
    except PhaseContextError as exc:
        # fail-closed: no context -> no pipeline. Non-zero exit + alert.
        _emit_alert(outdir, {"severity": "critical", "kind": "phase_context_unavailable", "error": str(exc)})
        manifest["fatal"] = f"phase_context_unavailable: {exc}"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[mini_pipeline] FATAL fail-closed: {exc}", file=sys.stderr)
        return 3

    manifest["phase_context"] = ctx.to_dict()  # ← ONE-LINE INTEGRATION ①: phase context into run_manifest
    manifest["stages"].append("stage0")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # non_trading refusal (acceptance 3): pipeline must not enter fetch stage
    if ctx.phase == Phase.NON_TRADING and not args.trade_date:  # ← non-trading gate at stage0
        _emit_alert(outdir, {"severity": "info", "kind": "non_trading_refusal", "phase": ctx.phase.value})
        print("[mini_pipeline] non_trading day: refusing to enter fetch stage", file=sys.stderr)
        return 2

    # ---------------- bare-run guard (integration ②, R-1: partial-bar OR premarket) ----------------
    if refuse_bare_run_if_partial(ctx, bool(args.trade_date)):  # ← ONE-LINE INTEGRATION ②: replaces weekday/"15:05" string math
        _emit_alert(outdir, {"severity": "warn", "kind": "bare_run_refused", "phase": ctx.phase.value})
        print("[mini_pipeline] partial-bar/premarket phase without --trade-date: bare run refused", file=sys.stderr)
        return 5

    # ---------------- fetch stage (marker only) ----------------
    manifest["stages"].append("fetch")

    # ---------------- backtest_cache write (integration ③) ----------------
    cache_sandbox = outdir / "backtest_cache_sandbox"
    cache_sandbox.mkdir(parents=True, exist_ok=True)
    try:
        guard_backtest_cache_write(ctx, explicit_intraday_flag=args.explicit_intraday)  # ← ONE-LINE INTEGRATION ③: pre-write assert
    except PartialBarCacheWriteBlocked as exc:
        _emit_alert(outdir, {"severity": "critical", "kind": "partial_bar_cache_write_blocked", "error": str(exc)})
        manifest["cache_write"] = "blocked"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[mini_pipeline] cache write BLOCKED: {exc}", file=sys.stderr)
        return 4
    cache_file = cache_sandbox / f"cyq_perf_{ctx.effective_trade_date}.json"
    cache_file.write_text(json.dumps({"trade_date": ctx.effective_trade_date, "rows": 1}), encoding="utf-8")
    manifest["cache_write"] = str(cache_file)
    manifest["stages"].append("backtest_cache")

    # ---------------- pipeline_alert lag judgment (integration ④) ----------------
    if args.latest_data_date:
        lag_behind = args.latest_data_date < ctx.effective_trade_date  # ← ONE-LINE INTEGRATION ④: close-aware lag via effective_trade_date
        manifest["lag_check"] = {"latest_data_date": args.latest_data_date,
                                 "effective_trade_date": ctx.effective_trade_date,
                                 "behind": lag_behind}
        if lag_behind:
            _emit_alert(outdir, {"severity": "critical", "kind": "data_behind_effective_trade_date",
                                 "latest": args.latest_data_date, "effective": ctx.effective_trade_date})
        manifest["stages"].append("pipeline_alert")

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["phase_context"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
