"""Self-check harness CLI for the contract layer — three interface families.

Usage (run from the factor_factory directory):

    python -m contracts                 # OFFLINE: synthetic payloads, no network
    python -m contracts --live          # LIVE: small real pulls (tushare proxy /
                                        #       akshare sina / eastmoney datacenter)
    python -m contracts --live --em     # also exercise the flaky akshare EM channel

Offline mode drives run_harness (T / E / T2, stage isolation, 勾稽) on all
three families via raw_override — safe to run any time, including while the
19:30 production pipeline is running.

Live mode pulls a deliberately tiny window (default 2 symbols x 5 open days,
full-table name snapshots, one yjyg period) and additionally runs the daily
cross-source 1e-4 reconciliation and PIT as-of assertions. Lineage lands in
`contracts/.selfcheck/run_manifest_selfcheck.json` (git-ignored).

Verdicts printed per interface (R-3.3 separation):
  contract  = compliance (harness all_green: stage isolation + key 勾稽)
  health    = source aliveness (ok-rate vs threshold: healthy/degraded/dead)

Exit code 0 = every interface contract-green AND no dead source.
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import warnings
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = PKG_DIR / ".selfcheck"

from .core import (
    DEFAULT_HEALTH_OK_RATE_THRESHOLD,
    RawPayload,
    ReconcileBreach,
    assess_source_health,
    run_harness,
)
from .fetchers import CONTRACTS


def _harness_dict(rep) -> Dict[str, Any]:
    return {
        "all_green": rep.all_green, "t_ok": rep.t_ok, "e_ok": rep.e_ok,
        "t2_ok": rep.t2_ok, "expected_keys": rep.n_keys_expected,
        "ok": rep.n_ok, "failed": rep.n_failed, "empty": rep.n_empty,
        "rows": rep.row_count, "row_failures": rep.row_failures,
        "errors": rep.errors,
    }


def _latest_settled_quarter(today: date, min_age_days: int = 45) -> date:
    """Most recent quarter end at least min_age_days old (yjyg non-empty)."""
    d = today - timedelta(days=min_age_days)
    q_month = ((d.month - 1) // 3) * 3
    if q_month == 0:
        return date(d.year - 1, 12, 31)
    last_days = {3: 31, 6: 30, 9: 30}
    return date(d.year, q_month, last_days[q_month])


# --------------------------------------------------------------------------
# offline synthetic payloads (contract-conformant, no IO)
# --------------------------------------------------------------------------
def _offline_checks(threshold: float) -> List[Dict[str, Any]]:
    import pandas as pd

    from .fetchers import (
        DailyTushareFetcher, SecurityMetaTushareFetcher, YjygDatacenterFetcher,
    )

    results = []

    # 1) daily family — 2 expected keys: 1 ok + 1 explicit-empty (勾稽)
    daily = DailyTushareFetcher()
    frame = pd.DataFrame([
        {"ts_code": "600000.SH", "trade_date": "20260706", "open": 8.68,
         "high": 8.93, "low": 8.62, "close": 8.92, "pre_close": 8.66,
         "change": 0.26, "pct_chg": 3.0, "vol": 793762.06, "amount": 698878.044},
        {"ts_code": "600000.SH", "trade_date": "20260707", "open": 8.89,
         "high": 8.97, "low": 8.78, "close": 8.89, "pre_close": 8.92,
         "change": -0.03, "pct_chg": -0.34, "vol": 517794.60, "amount": 458252.574},
    ], columns=CONTRACTS["daily_tushare"]["frozen_columns"])
    rep, _rows, _meta = run_harness(
        daily,
        {"symbols": ("600000.SH", "600999.SH"),
         "start_date": date(2026, 7, 6), "end_date": date(2026, 7, 7)},
        raw_override=RawPayload(payload=frame),
    )
    results.append({"interface": daily.interface_id, "mode": "offline",
                    "contract": _harness_dict(rep),
                    "health": assess_source_health(rep, threshold).to_dict()})

    # 2) name family — full-table snapshot >= min_rows
    meta_f = SecurityMetaTushareFetcher()
    n = CONTRACTS["security_meta_tushare"]["min_rows"] + 100
    meta_frame = pd.DataFrame({
        "ts_code": [f"{600000 + i}.SH" for i in range(n)],
        "symbol": [f"{600000 + i}" for i in range(n)],
        "name": [f"合约测试{i}" for i in range(n)],
        "area": ["上海"] * n,
        "industry": ["银行"] * n,
        "market": ["主板"] * n,
        "list_date": ["20200101"] * n,
    }, columns=CONTRACTS["security_meta_tushare"]["frozen_columns"])
    rep, _rows, _meta = run_harness(meta_f, {}, raw_override=RawPayload(payload=meta_frame))
    results.append({"interface": meta_f.interface_id, "mode": "offline",
                    "contract": _harness_dict(rep),
                    "health": assess_source_health(rep, threshold).to_dict()})

    # 3) announcement family — one conformant 27-named-key record
    yj = YjygDatacenterFetcher()
    rec = {k: None for k in CONTRACTS["yjyg_datacenter"]["frozen_keys"]}
    rec.update({"SECUCODE": "001237.SZ", "SECURITY_CODE": "001237",
                "SECURITY_NAME_ABBR": "合约测试", "NOTICE_DATE": "2026-05-19 00:00:00",
                "REPORT_DATE": "2026-03-31 00:00:00", "PREDICT_FINANCE": "净利润",
                "FORECAST_JZ": 85300000, "PREYEAR_SAME_PERIOD": 80052800,
                "INCREASE_JZ": 6.555, "PREDICT_TYPE": "略增"})
    rep, _rows, _meta = run_harness(
        yj, {"period": date(2026, 3, 31)},
        raw_override=RawPayload(payload={"records": [rec], "pages": 1}))
    results.append({"interface": yj.interface_id, "mode": "offline",
                    "contract": _harness_dict(rep),
                    "health": assess_source_health(rep, threshold).to_dict()})
    return results


# --------------------------------------------------------------------------
# live small pulls
# --------------------------------------------------------------------------
def _live_checks(symbols: List[str], n_days: int, period: date,
                 include_em: bool, out_dir: Path, threshold: float) -> List[Dict[str, Any]]:
    from .calendar_provider import last_n_open_days, load_open_days
    from .fetchers import (
        DailyAkshareEmFetcher, DailyAkshareSinaFetcher, DailyTushareFetcher,
        SecurityMetaAkshareFetcher, SecurityMetaTushareFetcher, YjygDatacenterFetcher,
    )
    from .lineage import LineageSink, timed_attempt
    from .models import CN_TZ, MarketContext, validate_daily_row_invariants
    from .pit import assert_pit_clean
    from .reconcile import reconcile_daily, reconcile_names

    out_dir.mkdir(parents=True, exist_ok=True)
    sink = LineageSink(out_dir / "run_manifest_selfcheck.json", run_id="contracts-selfcheck")
    results: List[Dict[str, Any]] = []

    open_days, cal_notes = load_open_days(sink)
    sink.put("calendar_notes", cal_notes)
    window = last_n_open_days(open_days, n_days)
    dparams = {"symbols": tuple(symbols), "start_date": window[0], "end_date": window[-1]}

    # names (tushare first: list_dates feed the market context)
    meta_ts = SecurityMetaTushareFetcher()
    with timed_attempt(sink, "name", "tushare_proxy", "stock_basic") as att:
        rep_mt, rows_mt, m_mt = run_harness(meta_ts, {})
        att.record_count = m_mt.row_count if m_mt else 0
    results.append({"interface": meta_ts.interface_id, "mode": "live",
                    "contract": _harness_dict(rep_mt),
                    "health": assess_source_health(rep_mt, threshold).to_dict()})
    basic_rows = rows_mt.get("__table__:stock_basic", [])
    ctx = MarketContext(open_days=open_days,
                        list_dates={r.symbol: r.list_date for r in basic_rows if r.list_date})

    meta_ak = SecurityMetaAkshareFetcher()
    with timed_attempt(sink, "name", "akshare_exchange", "stock_info_a_code_name") as att:
        rep_ma, rows_ma, m_ma = run_harness(meta_ak, {})
        att.record_count = m_ma.row_count if m_ma else 0
    results.append({"interface": meta_ak.interface_id, "mode": "live",
                    "contract": _harness_dict(rep_ma),
                    "health": assess_source_health(rep_ma, threshold).to_dict()})
    sink.put("name_industry_reconciliation",
             reconcile_names(basic_rows, rows_ma.get("__table__:code_name", [])))

    # daily (tushare primary + sina; EM opt-in)
    daily_sets: Dict[str, Dict[str, list]] = {}

    def run_daily(fetcher, provider, fallback_to=None):
        with timed_attempt(sink, "daily", provider, fetcher.interface_id) as att:
            rep, rows, meta = run_harness(fetcher, dparams)
            att.record_count = meta.row_count if meta else 0
            att.fallback_to = fallback_to
        results.append({"interface": fetcher.interface_id, "mode": "live",
                        "contract": _harness_dict(rep),
                        "health": assess_source_health(rep, threshold).to_dict()})
        validated = {}
        for sym, rs in (rows or {}).items():
            keep = []
            for r in rs:
                vr, _f = validate_daily_row_invariants(r, ctx)
                if vr is not None:
                    keep.append(vr)
            validated[sym] = keep
        return validated

    daily_sets["tushare_proxy"] = run_daily(DailyTushareFetcher(), "tushare_proxy")
    if include_em:
        daily_sets["akshare_em"] = run_daily(
            DailyAkshareEmFetcher(), "akshare_em", fallback_to="akshare_sina")
    daily_sets["akshare_sina"] = run_daily(DailyAkshareSinaFetcher(), "akshare_sina")

    try:
        recon = reconcile_daily(daily_sets["tushare_proxy"], daily_sets["akshare_sina"], strict=True)
        sink.put("daily_reconciliation", recon.to_dict())
        results.append({"interface": "reconcile:daily(tushare~sina)", "mode": "live",
                        "contract": {"all_green": recon.passed, **recon.to_dict()}})
    except ReconcileBreach as exc:
        sink.put("daily_reconciliation", {"passed": False, "hard_alert": str(exc)[:500]})
        results.append({"interface": "reconcile:daily(tushare~sina)", "mode": "live",
                        "contract": {"all_green": False, "hard_alert": str(exc)[:300]}})

    # announcements
    yj = YjygDatacenterFetcher()
    with timed_attempt(sink, "announcement", "eastmoney_datacenter", "yjyg") as att:
        rep_yj, rows_yj, m_yj = run_harness(yj, {"period": period})
        att.record_count = m_yj.row_count if m_yj else 0
    results.append({"interface": yj.interface_id, "mode": "live",
                    "contract": _harness_dict(rep_yj),
                    "health": assess_source_health(rep_yj, threshold).to_dict()})

    # PIT spot assertion on everything fetched
    all_rows = [r for v in daily_sets.values() for rs in v.values() for r in rs]
    all_rows += basic_rows
    all_rows += [r for rs in (rows_yj or {}).values() for r in rs]
    now_utc = datetime.now(timezone.utc)
    violations = assert_pit_clean(all_rows, now_utc)
    mid = datetime.combine(window[len(window) // 2], dtime(16, 0), tzinfo=CN_TZ)
    violations += assert_pit_clean([r for v in daily_sets.values() for rs in v.values() for r in rs], mid)
    sink.put("pit_violations", violations)
    results.append({"interface": "pit:asof(now,mid-window)", "mode": "live",
                    "contract": {"all_green": violations == 0, "violations": violations}})

    sink.flush()
    return results


def main(argv: Optional[List[str]] = None) -> int:
    warnings.filterwarnings("ignore")
    socket.setdefaulttimeout(30)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    p = argparse.ArgumentParser(
        prog="python -m contracts",
        description="Contract-layer self-check harness (three interface families).")
    p.add_argument("--live", action="store_true",
                   help="real pulls (default: offline synthetic, no network)")
    p.add_argument("--em", action="store_true",
                   help="live only: also exercise the flaky akshare EM daily channel")
    p.add_argument("--symbols", default="600000.SH,000001.SZ",
                   help="live daily symbols, comma separated")
    p.add_argument("--days", type=int, default=5, help="live daily window (open days)")
    p.add_argument("--period", default=None,
                   help="live yjyg period YYYY-MM-DD (default: latest settled quarter end)")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="live lineage/manifest output dir (default: contracts/.selfcheck)")
    p.add_argument("--health-threshold", type=float, default=DEFAULT_HEALTH_OK_RATE_THRESHOLD)
    args = p.parse_args(argv)

    if args.live:
        period = (date.fromisoformat(args.period) if args.period
                  else _latest_settled_quarter(date.today()))
        results = _live_checks(
            [s.strip() for s in args.symbols.split(",") if s.strip()],
            args.days, period, args.em, Path(args.out), args.health_threshold)
    else:
        results = _offline_checks(args.health_threshold)

    print(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    contract_green = all(r["contract"].get("all_green") for r in results)
    dead = [r["interface"] for r in results
            if r.get("health") and r["health"]["status"] == "dead"]
    n_ifaces = len(results)
    print(f"\nSELFCHECK[{'live' if args.live else 'offline'}]: "
          f"contract={'GREEN' if contract_green else 'RED'} ({n_ifaces} checks)  "
          f"dead_sources={dead if dead else 'none'}")
    return 0 if contract_green and not dead else 1


if __name__ == "__main__":
    sys.exit(main())
