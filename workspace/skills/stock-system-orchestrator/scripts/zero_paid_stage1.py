#!/usr/bin/env python3
"""Zero-paid data preparation for the 19:30 job after the v4.3 retirement (A2, 2026-11-20).

stage1_data_prepare.py waits for stk_factor + daily + cyq_perf (v4.3 inputs).  Two of those have no free source, so
after the retirement the 19:30 job runs only what the forward ledgers need, reusing stage 1's own functions (stage 1
itself is not modified, and v4.3 selection / AI / publish stages are not started):

  1. trading calendar persisted through the self-healing client (paid calendar + calendar_years extension);
  2. the day's daily cache (tdx-api first, as stage 1 does);
  3. the PIT freeze (universe + daily_basic) for the forward ledgers (22:15 short track, 23:30 forward shadows).

Exit 0 when the day's daily cache, PIT universe, PIT daily_basic and calendar exist; 1 otherwise.
Report: stock_data/03-working/health/zero_paid_stage1.json

  zero_paid_stage1.py [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage1_data_prepare as s1  # noqa: E402
import v43_retirement as vr  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date")
    a = ap.parse_args(argv)
    d = a.date or datetime.now(TZ).strftime("%Y%m%d")
    t0 = time.time()
    rep: dict = {"stage": "zeroPaidDataPreparation", "target_trade_date": d,
                 "started_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), "steps": {}}
    pro = s1.pl.init_tushare()
    for name, fn in (
        ("calendar", lambda: {k: v for k, v in s1.materialize_trading_calendar(pro, d).items() if k != "open_dates"}),
        ("daily", lambda: {"rows": int(len(s1.bt.get_cached(pro, d, "daily")))}),
        ("pit_snapshot", lambda: s1.freeze_pit_market_snapshot(pro, d)),
    ):
        try:
            rep["steps"][name] = {"ok": True, **(fn() or {})}
        except Exception as exc:  # noqa: BLE001
            rep["steps"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400],
                                  "trace": traceback.format_exc(limit=2)[-600:]}
    ok, chk = vr.stage1_check(d)
    rep.update(outputs=chk["outputs"], ok=ok, seconds=round(time.time() - t0, 1),
               finished_at=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
    out = s1.HEALTH_DIR / "zero_paid_stage1.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"ok": ok, "date": d, "outputs": chk["outputs"], "seconds": rep["seconds"],
                      "errors": {k: v["error"] for k, v in rep["steps"].items() if not v.get("ok")}},
                     ensure_ascii=False, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
