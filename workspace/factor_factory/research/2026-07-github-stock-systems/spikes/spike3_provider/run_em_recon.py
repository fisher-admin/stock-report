#!/usr/bin/env python
"""EM channel evidence run: akshare stock_zh_a_hist (push2his host is flaky
from this machine — succeeds intermittently). Pulls the spec-named akshare EM
daily source, validates the frozen 12-col contract live, reconciles whatever
overlap it yields against the saved tushare parquet at |rel diff|<=1e-4, and
writes manifest/run_manifest_em_recon.json (separate from the run of record).

Replay: /Users/fisher/.openclaw/venv/bin/python run_em_recon.py [attempts]
"""
from __future__ import annotations

import json
import logging
import socket
import sys
from datetime import date, datetime, timezone
from pathlib import Path

socket.setdefaulttimeout(30)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import warnings

warnings.filterwarnings("ignore")

import pandas as pd

from contract.core import ReconcileBreach, run_harness
from contract.fetchers import DailyAkshareEmFetcher
from contract.lineage import LineageSink, timed_attempt
from contract.models import DailyBarV1
from contract.reconcile import reconcile_daily

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("spike3.em")


def load_tushare_rows():
    df = pd.read_parquet(HERE / "data" / "daily_tushare.parquet")
    rows = {}
    for rec in df.to_dict("records"):
        row = DailyBarV1(**{**rec,
                            "trade_date": date.fromisoformat(str(rec["trade_date"])[:10]),
                            "fetched_at": datetime.fromisoformat(str(rec["fetched_at"])),
                            "available_at": datetime.fromisoformat(str(rec["available_at"]))})
        rows.setdefault(row.symbol, []).append(row)
    return rows


def main() -> int:
    max_attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    ts_rows = load_tushare_rows()
    symbols = tuple(sorted(ts_rows.keys()))
    start_d = min(r.trade_date for v in ts_rows.values() for r in v)
    end_d = max(r.trade_date for v in ts_rows.values() for r in v)
    sink = LineageSink(HERE / "manifest" / "run_manifest_em_recon.json", run_id="spike3-em-recon")
    params = {"symbols": symbols, "start_date": start_d, "end_date": end_d}

    for attempt_no in range(1, max_attempts + 1):
        fetcher = DailyAkshareEmFetcher()
        with timed_attempt(sink, "daily", "akshare_em", f"stock_zh_a_hist(try{attempt_no})") as att:
            rep, rows_em, meta = run_harness(fetcher, params)
            att.record_count = meta.row_count if meta else 0
        n_ok = rep.n_ok
        log.info("try %d: harness green=%s ok=%d/%d rows=%d",
                 attempt_no, rep.all_green, n_ok, rep.n_keys_expected, rep.row_count)
        sink.put(f"harness_try{attempt_no}", {
            "all_green": rep.all_green, "ok": rep.n_ok, "failed": rep.n_failed,
            "empty": rep.n_empty, "rows": rep.row_count, "errors": rep.errors,
            "failure_sample": [f.model_dump() for f in (meta.failures[:3] if meta else [])],
        })
        if n_ok == 0:
            continue
        try:
            recon = reconcile_daily(ts_rows, rows_em, strict=True)
            sink.put("em_reconciliation", recon.to_dict())
            frames = []
            for rows in rows_em.values():
                frames.extend(r.model_dump(mode="json") for r in rows)
            pd.DataFrame(frames).to_parquet(HERE / "data" / "daily_akshare_em.parquet")
            print(json.dumps(recon.to_dict(), ensure_ascii=False, indent=1))
            print(f"\nEM RECON: PASS (overlap={recon.n_overlap_bars} bars, "
                  f"max_rel_diff={recon.max_rel_diff})")
            return 0
        except ReconcileBreach as exc:
            sink.put("em_reconciliation", {"passed": False, "hard_alert": str(exc)[:500]})
            print(f"EM RECON: HARD ALERT — {exc}")
            return 2
    print("EM RECON: PENDING — host yielded 0 keys in all attempts (see manifest)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
