#!/usr/bin/env python3
"""v4.3 retirement (A2, zero-paid architecture, approved by fisher 2026-11-20).

v4.3 needs Tushare cyq_perf / stk_factor; under the zero-paid decision (11-19) it can never run again.  This module is
the single switch and the one-off close-out:

  status                 retired?  (config/v43_retirement.json, env STOCK_V43_RETIRED=0|1 wins)
  retire                 write the flag (write-once; refuses to overwrite a different record)
  archive                one-off archive of the full v4.3 live record (read-only copies + summary), write-once
  close-tier             closing record for the tier ledger (v4.3 Top-20 x EW6): stopped because the data source was
                         retired, not because of a verdict; the ledger itself is never edited
  stage1-check --date D  retired-mode 19:30 success check: the zero-paid data preparation outputs exist for D

Retired-mode effects (each reader calls is_retired()):
  - 19:30 stock_pipeline_gate.sh runs stage 1 data preparation only (PIT freeze, calendar, tdx daily) and is silent
    when its outputs exist; v4.3 selection / AI / publish stages are not started;
  - greenfield_forward_shadow.sh skips the tier-ledger completeness alarm;
  - jobs that only consume the v4.3 release are paused in Hermes (reversible), see the retirement record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
EXIT_OK, EXIT_ERROR = 0, 2


def workspace() -> Path:
    return Path(os.environ.get("STOCK_SYSTEM_WORKSPACE") or os.environ.get("OPENCLAW_WORKSPACE_DIR")
                or Path(__file__).resolve().parents[3])


def flag_path(ws: Path | None = None) -> Path:
    return (ws or workspace()) / "config/v43_retirement.json"


def now_text() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def is_retired(ws: Path | None = None) -> bool:
    env = os.environ.get("STOCK_V43_RETIRED")
    if env in ("0", "1"):
        return env == "1"
    p = flag_path(ws)
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("retired"))
    except (OSError, ValueError):
        return False


def record(ws: Path | None = None) -> dict:
    try:
        return json.loads(flag_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def retire(ws: Path | None = None, *, last_signal_date: str, reason: str) -> dict:
    p = flag_path(ws)
    if p.exists():
        cur = json.loads(p.read_text(encoding="utf-8"))
        if cur.get("retired") and cur.get("last_signal_date") == last_signal_date:
            return {"status": "unchanged", **cur}
        raise RuntimeError(f"{p} exists with a different record; edit it by hand if that is intended")
    rec = {"retired": True, "retired_at": now_text(), "last_signal_date": last_signal_date, "reason": reason,
           "approved_by": "fisher 2026-11-20 (zero-paid architecture A2)",
           "successor": "#/today = forward_shadow_v1 EW6 buffered_top100 holdings (paper record of forward validation)",
           "archive": "stock_data/03-working/archive/v43_live_record/"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"status": "retired", **rec}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive(ws: Path | None = None) -> dict:
    w = ws or workspace()
    wk = w / "stock_data/03-working"
    out = wk / "archive/v43_live_record"
    if (out / "manifest.json").exists():
        return {"status": "unchanged", **json.loads((out / "manifest.json").read_text(encoding="utf-8"))}
    out.mkdir(parents=True, exist_ok=True)
    db = wk / "recommendation_warehouse/recommendations.db"
    # 1) the v4.3 fact rows as a standalone sqlite (sqlite backup API: consistent read-only copy)
    facts = out / "v43_recommendation_fact.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(facts)
    cols = [r[1] for r in src.execute("pragma table_info(recommendation_fact)")]
    dst.execute(f"create table recommendation_fact ({', '.join(cols)})")
    rows = src.execute("select * from recommendation_fact where strategy_id = 'prebreakout_v41' "
                       "order by recommend_date, rank_no").fetchall()
    dst.executemany(f"insert into recommendation_fact values ({', '.join('?' * len(cols))})", rows)
    dst.commit()
    stats = src.execute("""
        select count(*), count(distinct recommend_date), min(recommend_date), max(recommend_date),
               sum(next_day_return_pct is not null), avg(next_day_return_pct),
               avg(case when next_day_return_pct > 0 then 1.0 when next_day_return_pct is not null then 0.0 end)
        from recommendation_fact where strategy_id = 'prebreakout_v41'""").fetchone()
    versions = src.execute("select strategy_version, source_kind, count(distinct recommend_date) "
                           "from recommendation_fact where strategy_id = 'prebreakout_v41' group by 1, 2").fetchall()
    dst.close()
    src.close()
    # 2) the last published public contracts of v4.3 (as they were on the site)
    copied = []
    pub = w / "stock-report/data/latest"
    for name in ("recommendation_state.json", "review_track_latest.json", "strategy_evaluation.json",
                 "candidate_state.json", "run_manifest.json", "double_pass_monitor.json", "dual_track_state.json"):
        p = pub / name
        if p.exists():
            shutil.copy2(p, out / f"public_{name}")
            copied.append(name)
    # 3) the tier ledger as of retirement (the ledger itself stays where it is, untouched)
    tl = w / "factor_factory/data/forward_shadow/tier_ledger.jsonl"
    if tl.exists():
        shutil.copy2(tl, out / "tier_ledger_at_retirement.jsonl")
    manifest = {
        "created_at": now_text(), "strategy_id": "prebreakout_v41", "strategy_name": "启动前夕 v4.3",
        "recommendations": stats[0], "recommend_days": stats[1], "first_recommend_date": stats[2],
        "last_recommend_date": stats[3], "settled_next_day": stats[4],
        "mean_next_day_return_pct": round(stats[5], 4) if stats[5] is not None else None,
        "next_day_hit_rate": round(stats[6], 4) if stats[6] is not None else None,
        "versions": [{"strategy_version": v, "source_kind": k, "days": n} for v, k, n in versions],
        "note": "next-day close-to-close per recommendation as stored in the warehouse; no costs; for the record only",
        "files": {p.name: _sha(p) for p in sorted(out.iterdir()) if p.is_file() and p.name != "manifest.json"},
        "public_contracts_copied": copied,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"status": "archived", **manifest}


def close_tier(ws: Path | None = None) -> dict:
    w = ws or workspace()
    tl = w / "factor_factory/data/forward_shadow/tier_ledger.jsonl"
    out = w / "factor_factory/data/forward_shadow/tier_ledger_closure.json"
    if out.exists():
        return {"status": "unchanged", **json.loads(out.read_text(encoding="utf-8"))}
    rows = [json.loads(x) for x in tl.read_text(encoding="utf-8").splitlines() if x.strip()] if tl.exists() else []
    dates = sorted({str(r.get("signal_date")) for r in rows if r.get("signal_date")})
    rec = {"closed_at": now_text(), "status": "closed",
           "reason": "data_source_retired: v4.3 needs Tushare cyq_perf/stk_factor; fisher's zero-paid decision "
                     "(2026-11-19) retires v4.3 (A2, 2026-11-20). Stopped because the data source was retired, "
                     "NOT because of a verdict.",
           "rows": len(rows), "first_signal_date": dates[0] if dates else None,
           "last_signal_date": dates[-1] if dates else None,
           "ledger_sha256": _sha(tl) if tl.exists() else None,
           "settlement": "tier_monitor.py settle keeps settling rows already written until they mature; no new rows",
           "alarms": "the 23:30 completeness alarm (missing tier row) is off from this date"}
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"status": "closed", **rec}


def stage1_check(day: str, ws: Path | None = None) -> tuple[bool, dict]:
    wk = (ws or workspace()) / "stock_data/03-working"
    need = {"daily": wk / f"backtest_cache/daily_{day}.parquet",
            "pit_universe": wk / f"fundamental_cache/pit_market/v2/universe_{day}.parquet",
            "pit_daily_basic": wk / f"fundamental_cache/pit_market/v2/daily_basic_{day}.parquet",
            "calendar": wk / "health/trading_calendar.json"}
    have = {k: p.exists() for k, p in need.items()}
    return all(have.values()), {"date": day, "outputs": have}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("retire")
    r.add_argument("--last-signal-date", required=True)
    r.add_argument("--reason", default="zero-paid: Tushare cyq_perf/stk_factor have no free equivalent")
    sub.add_parser("archive")
    sub.add_parser("close-tier")
    c = sub.add_parser("stage1-check")
    c.add_argument("--date", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            out = {"retired": is_retired(), **record()}
        elif a.cmd == "retire":
            out = retire(last_signal_date=a.last_signal_date, reason=a.reason)
        elif a.cmd == "archive":
            out = archive()
        elif a.cmd == "close-tier":
            out = close_tier()
        else:
            ok, out = stage1_check(a.date)
            print(json.dumps(out, ensure_ascii=False))
            return EXIT_OK if ok else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return EXIT_ERROR
    print(json.dumps(out, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
