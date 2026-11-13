#!/usr/bin/env python
"""Spike-3 live driver: real pulls (~20 symbols x 20 open days), harness,
cross-source reconciliation, PIT assertions, lineage manifest.

Outputs (all inside the spike dir — additive, isolated):
  data/daily_tushare.parquet, data/daily_akshare_sina.parquet,
  data/security_meta_tushare.parquet, data/security_meta_akshare.parquet,
  data/yjyg.parquet
  manifest/run_manifest_live.json  (attempt chain + reconcile + PIT + harness)
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import warnings
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
socket.setdefaulttimeout(30)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pandas as pd

from contract.calendar_provider import last_n_open_days, load_open_days
from contract.core import ReconcileBreach, run_harness
from contract.fetchers import (
    DailyAkshareEmFetcher, DailyAkshareSinaFetcher, DailyTushareFetcher,
    SecurityMetaAkshareFetcher, SecurityMetaTushareFetcher, YjygDatacenterFetcher,
)
from contract.lineage import LineageSink, ProviderAttempt, timed_attempt
from contract.models import CN_TZ, MarketContext, validate_daily_row_invariants
from contract.pit import assert_pit_clean, pit_filter
from contract.reconcile import reconcile_daily, reconcile_names

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("spike3.run")

UNIVERSE = [
    "600000.SH", "600036.SH", "600519.SH", "601318.SH", "600030.SH",
    "600887.SH", "601988.SH", "000001.SZ", "000002.SZ", "000651.SZ",
    "000858.SZ", "002415.SZ", "002594.SZ", "300059.SZ", "300750.SZ",
    "300308.SZ", "688111.SH", "688981.SH", "688008.SH", "688036.SH",
]
N_DAYS = 20


def harness_summary(rep):
    return {
        "interface": rep.interface_id, "all_green": rep.all_green,
        "t_ok": rep.t_ok, "e_ok": rep.e_ok, "t2_ok": rep.t2_ok,
        "expected_keys": rep.n_keys_expected, "ok": rep.n_ok,
        "failed": rep.n_failed, "empty": rep.n_empty,
        "rows": rep.row_count, "row_failures": rep.row_failures,
        "errors": rep.errors,
    }


def rows_to_frame(rows_by_key):
    recs = []
    for rows in rows_by_key.values():
        for r in rows:
            recs.append(r.model_dump(mode="json"))
    return pd.DataFrame(recs)


def main() -> int:
    sink = LineageSink(HERE / "manifest" / "run_manifest_live.json", run_id="spike3-live")
    summary = {}

    # environment pin (akshare 卡: 版本入 run_manifest)
    import akshare as _ak
    import tushare as _ts
    sink.put("environment", {
        "python": sys.version.split()[0],
        "akshare": getattr(_ak, "__version__", "?"),
        "tushare": getattr(_ts, "__version__", "?"),
        "pandas": pd.__version__,
    })

    # ---- calendar --------------------------------------------------------
    open_days, cal_notes = load_open_days(sink)
    sink.put("calendar_notes", cal_notes)
    window = last_n_open_days(open_days, N_DAYS)
    start_d, end_d = window[0], window[-1]
    log.info("window: %s .. %s (%d open days)", start_d, end_d, len(window))
    summary["window"] = {"start": str(start_d), "end": str(end_d), "n_open_days": len(window)}

    # ---- names/industry (tushare first: list_dates feed MarketContext) ---
    meta_ts = SecurityMetaTushareFetcher()
    with timed_attempt(sink, "name", "tushare_proxy", "stock_basic") as att:
        rep_meta_ts, rows_meta_ts, m_meta_ts = run_harness(meta_ts, {})
        att.record_count = m_meta_ts.row_count if m_meta_ts else 0
    summary["harness_security_meta_tushare"] = harness_summary(rep_meta_ts)
    basic_rows = rows_meta_ts["__table__:stock_basic"]
    list_dates = {r.symbol: r.list_date for r in basic_rows if r.list_date}
    ctx = MarketContext(open_days=open_days, list_dates=list_dates)

    meta_ak = SecurityMetaAkshareFetcher()
    with timed_attempt(sink, "name", "akshare_exchange", "stock_info_a_code_name") as att:
        rep_meta_ak, rows_meta_ak, m_meta_ak = run_harness(meta_ak, {})
        att.record_count = m_meta_ak.row_count if m_meta_ak else 0
    summary["harness_security_meta_akshare"] = harness_summary(rep_meta_ak)
    ak_rows = rows_meta_ak["__table__:code_name"]

    name_recon = reconcile_names(basic_rows, ak_rows)
    sink.put("name_industry_reconciliation", name_recon)
    summary["name_reconcile"] = {k: v for k, v in name_recon.items()
                                 if not isinstance(v, list)}

    # ---- daily: tushare primary -----------------------------------------
    dparams = {"symbols": tuple(UNIVERSE), "start_date": start_d, "end_date": end_d}
    daily_sets = {}
    expected_eff_date = end_d

    def run_daily(fetcher, provider, fallback_to=None):
        with timed_attempt(sink, "daily", provider, fetcher.interface_id) as att:
            rep, rows, meta = run_harness(fetcher, dparams)
            att.record_count = meta.row_count if meta else 0
            att.fallback_to = fallback_to
            if meta and meta.row_count:
                max_td = max(r.trade_date for rr in rows.values() for r in rr)
                open_lag = [d for d in window if d > max_td]
                att.freshness_lag_days = len(open_lag)
            if rep.n_ok == 0:
                att.error_type = "empty" if not meta or not meta.failures else "exception"
                raise RuntimeError(
                    f"{fetcher.interface_id}: 0/{rep.n_keys_expected} keys succeeded; "
                    f"first failure: {meta.failures[0].message if meta and meta.failures else 'n/a'}")
        return rep, rows, meta

    rep_ts, rows_ts, meta_ts_daily = run_daily(DailyTushareFetcher(), "tushare_proxy")
    summary["harness_daily_tushare"] = harness_summary(rep_ts)

    # market-context invariants (T2 cross-table) on tushare rows
    inv_failures = []
    validated_ts = {}
    for sym, rows in rows_ts.items():
        keep = []
        for r in rows:
            vr, fails = validate_daily_row_invariants(r, ctx)
            inv_failures.extend(f.model_dump() for f in fails)
            if vr is not None:
                keep.append(vr)
        validated_ts[sym] = keep
    summary["daily_tushare_invariants"] = {
        "rows_in": sum(len(v) for v in rows_ts.values()),
        "rows_ok": sum(len(v) for v in validated_ts.values()),
        "row_failures": len(inv_failures),
        "limit_exempt_rows": sum(1 for v in validated_ts.values() for r in v if r.limit_exempt),
    }
    sink.put("daily_invariant_failures", inv_failures[:50])
    daily_sets["tushare_proxy"] = validated_ts

    # ---- daily: akshare EM (spec source; host flaky from this machine —
    #      real failures land in the per-key failure channel) ---------------
    em_error = None
    validated_em = {}
    try:
        rep_em, rows_em, meta_em = run_daily(
            DailyAkshareEmFetcher(), "akshare_em", fallback_to="akshare_sina")
        summary["harness_daily_akshare_em"] = harness_summary(rep_em)
        for sym, rows in rows_em.items():
            keep = []
            for r in rows:
                vr, _f = validate_daily_row_invariants(r, ctx)
                if vr is not None:
                    keep.append(vr)
            validated_em[sym] = keep
        daily_sets["akshare_em"] = validated_em
    except RuntimeError as exc:
        em_error = str(exc)
        log.warning("akshare_em dead: %s", em_error[:160])
        summary["daily_akshare_em"] = {"status": "all_keys_failed", "error": em_error[:300]}

    # ---- daily: akshare Sina (reachable akshare channel) ------------------
    rep_sina, rows_sina, meta_sina = run_daily(DailyAkshareSinaFetcher(), "akshare_sina")
    summary["harness_daily_akshare_sina"] = harness_summary(rep_sina)
    validated_sina = {}
    for sym, rows in rows_sina.items():
        keep = []
        for r in rows:
            vr, fails = validate_daily_row_invariants(r, ctx)
            if vr is not None:
                keep.append(vr)
        validated_sina[sym] = keep
    daily_sets["akshare_sina"] = validated_sina

    # ---- cross-source reconciliation -------------------------------------
    try:
        recon = reconcile_daily(validated_ts, validated_sina, strict=True)
        sink.put("daily_reconciliation", recon.to_dict())
        summary["daily_reconcile"] = recon.to_dict()
    except ReconcileBreach as exc:
        sink.put("daily_reconciliation", {"passed": False, "hard_alert": str(exc)[:500]})
        summary["daily_reconcile"] = {"passed": False, "hard_alert": str(exc)[:500]}
        raise

    # spec-named akshare stock_zh_a_hist channel, gated on whatever overlap
    # the flaky host yielded this run (partial coverage is honest coverage)
    if any(validated_em.values()):
        try:
            recon_em = reconcile_daily(validated_ts, validated_em, strict=True)
            sink.put("daily_reconciliation_em", recon_em.to_dict())
            summary["daily_reconcile_em"] = recon_em.to_dict()
        except ReconcileBreach as exc:
            sink.put("daily_reconciliation_em", {"passed": False, "hard_alert": str(exc)[:500]})
            summary["daily_reconcile_em"] = {"passed": False, "hard_alert": str(exc)[:500]}
            raise

    # ---- announcements: yjyg ---------------------------------------------
    yj = YjygDatacenterFetcher()
    yparams = {"period": date(2026, 3, 31)}
    with timed_attempt(sink, "announcement", "eastmoney_datacenter", "yjyg") as att:
        rep_yj, rows_yj, meta_yj = run_harness(yj, yparams)
        att.record_count = meta_yj.row_count if meta_yj else 0
    summary["harness_yjyg"] = harness_summary(rep_yj)
    yj_rows = next(iter(rows_yj.values())) if rows_yj else []

    # ---- PIT as-of assertions (three tables) ------------------------------
    all_daily = [r for v in validated_ts.values() for r in v] + \
                [r for v in validated_sina.values() for r in v]
    pit_report = {}
    for label, rows, as_of in [
        ("daily@mid_window", all_daily,
         datetime.combine(window[len(window) // 2], dtime(16, 0), tzinfo=CN_TZ)),
        ("daily@now", all_daily, datetime.now(timezone.utc)),
        ("names@now", basic_rows + ak_rows, datetime.now(timezone.utc)),
        ("names@historical", basic_rows + ak_rows,
         datetime.combine(date(2026, 1, 1), dtime(15, 0), tzinfo=CN_TZ)),
        ("yjyg@2026-05-01", yj_rows,
         datetime.combine(date(2026, 5, 1), dtime(0, 0), tzinfo=CN_TZ)),
        ("yjyg@now", yj_rows, datetime.now(timezone.utc)),
    ]:
        visible = pit_filter(rows, as_of)
        violations = assert_pit_clean(rows, as_of)
        pit_report[label] = {"total": len(rows), "visible": len(visible),
                             "hidden": len(rows) - len(visible), "violations": violations}
    sink.put("pit_asof_report", pit_report)
    summary["pit"] = pit_report

    # ---- persist ----------------------------------------------------------
    (HERE / "data").mkdir(exist_ok=True)
    rows_to_frame(validated_ts).to_parquet(HERE / "data" / "daily_tushare.parquet")
    rows_to_frame(validated_sina).to_parquet(HERE / "data" / "daily_akshare_sina.parquet")
    if any(validated_em.values()):
        rows_to_frame(validated_em).to_parquet(HERE / "data" / "daily_akshare_em.parquet")
    rows_to_frame(rows_meta_ts).to_parquet(HERE / "data" / "security_meta_tushare.parquet")
    rows_to_frame(rows_meta_ak).to_parquet(HERE / "data" / "security_meta_akshare.parquet")
    if rows_yj:
        rows_to_frame(rows_yj).to_parquet(HERE / "data" / "yjyg.parquet")

    sink.put("summary", summary)
    sink.flush()
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))

    greens = [summary[k]["all_green"] for k in summary if k.startswith("harness_")]
    print(f"\nHARNESS GREEN: {sum(greens)}/{len(greens)}  "
          f"RECONCILE: {'PASS' if summary['daily_reconcile'].get('passed') else 'FAIL'}  "
          f"PIT violations: {sum(v['violations'] for v in pit_report.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
