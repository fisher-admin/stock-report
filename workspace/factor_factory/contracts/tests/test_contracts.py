#!/usr/bin/env python
"""Contract-layer offline unit tests (no network). Bare asserts; exit 0 = green.
(venv has no pytest — run directly or via `python -m contracts.tests.test_contracts`.)

Migrated from spike3_provider/test_acceptance.py (34 assertions) plus the
R-3 revisions (PHASE3_SPIKES_REPORT.md):

  A1 harness three-stage isolation (positive + stage-leak negative + 勾稽 negative)
  A2 poison samples (EM kline col drift / future-date row / yjyg key-count drift)
  A3 reconcile poison (0.05% artificial diff must be rejected + names mismatch explicit)
  A4 PIT as-of assertion incl. injected future-available_at poison row
  A5 limit-band exemption table positive & negative
  A6 lineage WARN on sink write failure
  A7 R-3.1 pre_close per-source capability table (fail-closed enforcement)
  A8 R-3.3 source health separate from contract compliance
  A9 R-3.4 name reconciliation NFKC + status-affix normalization
Plus: unit self-check (implied VWAP) fail-closed, adjust=qfq query ban.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # factor_factory dir

import pandas as pd
from pydantic import ValidationError

from contracts.core import (
    ContractViolation, ErrorType, FailureRecord, FetchMeta, HarnessReport,
    RawPayload, ReconcileBreach, StrictRow, assess_source_health, run_harness,
)
from contracts.fetchers import (
    CONTRACTS, DailyAkshareEmFetcher, DailyAkshareSinaFetcher, DailyQuery,
    DailyTushareFetcher, YjygDatacenterFetcher, _pre_close_capability_check,
    _unit_self_check, pre_close_capability,
)
from contracts.lineage import LineageSink, ProviderAttempt
from contracts.models import (
    CN_TZ, DailyBarV1, EarningsPreannounceV1, MarketContext, SecurityMetaV1,
    close_dt, validate_daily_row_invariants,
)
from contracts.pit import assert_pit_clean, pit_filter
from contracts.reconcile import (
    names_equivalent, normalize_security_name, reconcile_daily, reconcile_names,
)

PASS = []


def ok(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    PASS.append((name, cond, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    assert cond, f"{name}: {detail}"


# ---------------------------------------------------------------------------
# synthetic market context: weekday calendar 2026-06-01..2026-07-31
# ---------------------------------------------------------------------------
def synth_calendar():
    days = set()
    d = date(2026, 6, 1)
    while d <= date(2026, 7, 31):
        if d.weekday() < 5:
            days.add(d)
        d += timedelta(days=1)
    return days


OPEN_DAYS = synth_calendar()
CTX = MarketContext(
    open_days=OPEN_DAYS,
    list_dates={
        "688001.SH": date(2026, 7, 1),   # STAR, freshly listed
        "300999.SZ": date(2026, 7, 1),   # GEM, freshly listed
        "600100.SH": date(2026, 7, 6),   # main board, freshly listed
        "600000.SH": date(1999, 11, 10),
        "830799.BJ": date(2022, 1, 1),
    },
)


def bar(symbol, td, o, h, l, c, pre, vol=1_000_000, amt=None, source="test"):
    return DailyBarV1(
        symbol=symbol, trade_date=td, open=o, high=h, low=l, close=c,
        pre_close=pre, volume=vol, amount=amt if amt is not None else c * vol,
        adj_factor=None, is_suspended=False, source=source,
        fetched_at=datetime.now(timezone.utc), available_at=close_dt(td),
    )


print("== A1: self-check harness (offline synthetic) ==")


class FakeTushareDaily(DailyTushareFetcher):
    """pull_raw overridden: serves a frozen-contract-conformant frame."""

    def __init__(self, frame):
        self._frame = frame

    def pull_raw(self, q):
        return RawPayload(payload=self._frame)


def tushare_frame(rows):
    return pd.DataFrame(rows, columns=CONTRACTS["daily_tushare"]["frozen_columns"])


good_frame = tushare_frame([
    {"ts_code": "600000.SH", "trade_date": "20260706", "open": 8.68, "high": 8.93,
     "low": 8.62, "close": 8.92, "pre_close": 8.66, "change": 0.26, "pct_chg": 3.0,
     "vol": 793762.06, "amount": 698878.044},
    {"ts_code": "600000.SH", "trade_date": "20260707", "open": 8.89, "high": 8.97,
     "low": 8.78, "close": 8.89, "pre_close": 8.92, "change": -0.03, "pct_chg": -0.34,
     "vol": 517794.60, "amount": 458252.574},
])
params = {"symbols": ("600000.SH", "600999.SH"), "start_date": date(2026, 7, 6),
          "end_date": date(2026, 7, 7)}
rep, rows_by_key, meta = run_harness(FakeTushareDaily(good_frame), params)
ok("harness green on conformant payload", rep.all_green, "; ".join(rep.errors))
ok("T: params identity-retrievable", rep.t_ok)
ok("E: no Row leak in raw", rep.e_ok)
ok("T2 勾稽: 2 expected = 1 ok + 0 failed + 1 empty",
   rep.n_keys_expected == 2 and rep.n_ok == 1 and rep.n_failed == 0 and rep.n_empty == 1)
ok("unit conversion 手->股 / 千元->元",
   rows_by_key["600000.SH"][0].volume == 79376206
   and abs(rows_by_key["600000.SH"][0].amount - 698878044.0) < 1e-6)


class LeakyFetcher(FakeTushareDaily):
    """pull_raw smuggles a standardized Row into raw (stage leak)."""

    def pull_raw(self, q):
        leaked = bar("600000.SH", date(2026, 7, 6), 8.68, 8.93, 8.62, 8.92, 8.66)
        return RawPayload(payload={"frames": [leaked], "df": self._frame})

    def to_rows(self, q, raw):
        return super().to_rows(q, RawPayload(payload=raw.payload["df"], fetched_at=raw.fetched_at))


rep2, _, _ = run_harness(LeakyFetcher(good_frame), params)
ok("E: stage-isolation assertion catches Row instance inside raw",
   not rep2.all_green and any("stage isolation" in e for e in rep2.errors))


class UnaccountedFetcher(FakeTushareDaily):
    """to_rows silently drops the missing symbol from every channel."""

    def to_rows(self, q, raw):
        rows, m = super().to_rows(q, raw)
        m2 = m.model_copy(update={"empty_keys": []})   # drop empty channel => 勾稽 must break
        return rows, m2


rep3, _, _ = run_harness(UnaccountedFetcher(good_frame), params)
ok("T2 勾稽: silently-dropped key detected",
   not rep3.all_green and any("reconciliation broken" in e or "!=" in e for e in rep3.errors))

try:
    DailyQuery(symbols=("600000.SH",), start_date=date(2026, 7, 6),
               end_date=date(2026, 7, 7), bogus_param=1)
    ok("StrictQuery extra='forbid' rejects unknown param", False)
except ValidationError:
    ok("StrictQuery extra='forbid' rejects unknown param", True)

try:
    DailyQuery(symbols=("600000.SH",), start_date=date(2026, 7, 6),
               end_date=date(2026, 7, 7), adjust="qfq")
    ok("adjust=qfq banned at query stage", False)
except ValidationError:
    ok("adjust=qfq banned at query stage", True)

try:
    DailyBarV1(symbol="600000.SH", trade_date=date(2026, 7, 6), open=8.0, high=9.0,
               low=7.9, close=8.5, pre_close=8.0, volume=100, amount=850.0,
               is_suspended=False, source="t", fetched_at=datetime.now(timezone.utc),
               available_at=close_dt(date(2026, 7, 6)), sneaky_field=1)
    ok("StrictRow extra='forbid' rejects unknown field", False)
except ValidationError:
    ok("StrictRow extra='forbid' rejects unknown field", True)


print("== A2: poison samples ==")

# (a) EM kline column drift: 13 cols (f116 materializes) and 11 cols
em = DailyAkshareEmFetcher()
em_cols = CONTRACTS["daily_akshare_em"]["frozen_columns"]
base_row = {"日期": date(2026, 7, 6), "股票代码": "600000", "开盘": 8.68, "收盘": 8.92,
            "最高": 8.93, "最低": 8.62, "成交量": 793762.06, "成交额": 698878044.0,
            "振幅": 3.58, "涨跌幅": 3.0, "涨跌额": 0.26, "换手率": 2.7}
df13 = pd.DataFrame([{**base_row, "f116": 1.0}])          # 13 cols
df11 = pd.DataFrame([{k: v for k, v in base_row.items() if k != "换手率"}])  # 11 cols
q1 = em.to_query({"symbols": ("600000.SH",), "start_date": date(2026, 7, 6),
                  "end_date": date(2026, 7, 6)})
for label, dfp in [("13-col (f116 appears)", df13), ("11-col (col dropped)", df11)]:
    rows_p, meta_p = em.to_rows(q1, RawPayload(payload={"frames": {"600000.SH": dfp}}))
    ok(f"EM kline poison {label} rejected",
       "600000.SH" not in rows_p
       and any(f.error_type == ErrorType.schema for f in meta_p.failures))

# conformant 12-col EM frame passes (control)
df12 = pd.DataFrame([base_row], columns=em_cols)
rows_c, meta_c = em.to_rows(q1, RawPayload(payload={"frames": {"600000.SH": df12}}))
ok("EM kline conformant 12-col control accepted",
   "600000.SH" in rows_c and rows_c["600000.SH"][0].pre_close == 8.66)

# (b) future / out-of-calendar dates rejected by TradingCalendarV1 membership
future_bar = bar("600000.SH", date(2026, 7, 12), 8.9, 9.0, 8.8, 8.95, 8.9)  # Sunday
r, fails = validate_daily_row_invariants(future_bar, CTX)
ok("future/non-open date row rejected (calendar)",
   r is None and fails and fails[0].error_type == ErrorType.calendar)
beyond = bar("600000.SH", date(2026, 8, 3), 8.9, 9.0, 8.8, 8.95, 8.9)  # beyond horizon
r2, fails2 = validate_daily_row_invariants(beyond, CTX)
ok("beyond-calendar-horizon row rejected",
   r2 is None and fails2 and fails2[0].error_type == ErrorType.calendar)

# (c) yjyg key-count drift: frozen=27 server keys; poison = 26 and 28 keys
#     (R-3.2: named-set contract calibrated against runtime, not positional)
yj = YjygDatacenterFetcher()
frozen_keys = list(CONTRACTS["yjyg_datacenter"]["frozen_keys"])
good_rec = {k: None for k in frozen_keys}
good_rec.update({"SECUCODE": "001237.SZ", "SECURITY_CODE": "001237",
                 "SECURITY_NAME_ABBR": "测试", "NOTICE_DATE": "2026-05-19 00:00:00",
                 "REPORT_DATE": "2026-03-31 00:00:00", "PREDICT_FINANCE": "净利润",
                 "FORECAST_JZ": 85300000, "PREYEAR_SAME_PERIOD": 80052800,
                 "INCREASE_JZ": 6.555, "PREDICT_TYPE": "略增"})
qy = yj.to_query({"period": date(2026, 3, 31)})
rec26 = {k: v for k, v in good_rec.items() if k != "PREDICT_HBMEAN"}
rec28 = {**good_rec, "NEW_UPSTREAM_COL": 1}
for label, rec in [("26-key (missing col)", rec26), ("28-key (added col)", rec28)]:
    try:
        yj.to_rows(qy, RawPayload(payload={"records": [rec], "pages": 1}))
        ok(f"yjyg poison {label} rejected", False)
    except ContractViolation as exc:
        ok(f"yjyg poison {label} rejected", "expected_nkeys" in str(exc))
rows_y, meta_y = yj.to_rows(qy, RawPayload(payload={"records": [good_rec], "pages": 1}))
ok("yjyg conformant 27-key control accepted", meta_y.row_count == 1)


print("== A3: cross-source reconciliation poison ==")
d1 = date(2026, 7, 6)
d2 = date(2026, 7, 7)
rows_a = {"600000.SH": [bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, 8.66, source="tushare_proxy"),
                        bar("600000.SH", d2, 8.89, 8.97, 8.78, 8.89, 8.92, source="tushare_proxy")]}
rows_b_good = {"600000.SH": [bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, None, source="akshare_sina"),
                             bar("600000.SH", d2, 8.89, 8.97, 8.78, 8.89, None, source="akshare_sina")]}
rep_ok = reconcile_daily(rows_a, rows_b_good, strict=True)
ok("identical overlap passes 1e-4 gate", rep_ok.passed and rep_ok.n_overlap_bars == 2)

rows_b_bad = {"600000.SH": [bar("600000.SH", d1, 8.68, 8.93, 8.62, round(8.92 * 1.0005, 6), None, source="akshare_sina"),
                            rows_b_good["600000.SH"][1]]}
try:
    reconcile_daily(rows_a, rows_b_bad, strict=True)
    ok("artificial 0.05% diff rejected + hard alert", False)
except ReconcileBreach as exc:
    ok("artificial 0.05% diff rejected + hard alert", "HARD ALERT" in str(exc))

now = datetime.now(timezone.utc)
meta_a = [SecurityMetaV1(symbol="600000.SH", name="浦发银行", industry="银行",
                         list_date=date(1999, 11, 10), source="tushare_proxy",
                         fetched_at=now, available_at=now),
          SecurityMetaV1(symbol="000001.SZ", name="平安银行", industry="银行",
                         list_date=date(1991, 4, 3), source="tushare_proxy",
                         fetched_at=now, available_at=now)]
meta_b = [SecurityMetaV1(symbol="600000.SH", name="浦发银行X", source="akshare_exchange",
                         fetched_at=now, available_at=now),
          SecurityMetaV1(symbol="000001.SZ", name="平安银行", source="akshare_exchange",
                         fetched_at=now, available_at=now)]
nm = reconcile_names(meta_a, meta_b)
ok("name mismatch list explicit in manifest payload",
   nm["n_name_mismatches"] == 1 and nm["name_mismatches"][0]["symbol"] == "600000.SH")


print("== A4: PIT as-of ==")
as_of = datetime.combine(date(2026, 7, 7), time(16, 0), tzinfo=CN_TZ)
daily_rows = rows_a["600000.SH"] + [bar("600000.SH", date(2026, 7, 10), 8.98, 9.11, 8.92, 9.06, 8.98)]
visible = pit_filter(daily_rows, as_of)
ok("daily: rows past as_of invisible", len(visible) == 2 and assert_pit_clean(daily_rows, as_of) == 0)
poison_pit = bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, 8.66)
poison_pit = poison_pit.model_copy(update={"available_at": as_of + timedelta(days=1)})
ok("daily: injected future-available_at row excluded",
   poison_pit not in pit_filter(daily_rows + [poison_pit], as_of))

yj_rows = [EarningsPreannounceV1(
    symbol="001237.SZ", name="测试", indicator="净利润", period=date(2026, 3, 31),
    ann_date=date(2026, 5, 19), forecast_value=8.5e7, prev_year_value=8.0e7,
    change_pct=6.5, predict_type="略增", is_preliminary=True, source="eastmoney_datacenter",
    fetched_at=now, available_at=datetime.combine(date(2026, 5, 19), time(0, 0), tzinfo=CN_TZ))]
early = datetime.combine(date(2026, 4, 30), time(15, 0), tzinfo=CN_TZ)
ok("announcement: pre-announcement as_of hides row",
   len(pit_filter(yj_rows, early)) == 0 and assert_pit_clean(yj_rows, early) == 0)
ok("names: snapshot semantics (available_at=fetched_at) filtered for historical as_of",
   len(pit_filter(meta_a, early)) == 0)
try:
    pit_filter(daily_rows, datetime(2026, 7, 7, 16, 0))
    ok("naive as_of rejected", False)
except ValueError:
    ok("naive as_of rejected", True)


print("== A5: limit-band exemption table ==")
d0 = date(2026, 7, 1)
star_days = CTX.first_n_open_days(d0, 5)
day3 = star_days[2]
star_bar = bar("688001.SH", day3, 10.0, 13.6, 9.9, 13.5, 10.0)  # +35%
r_star, f_star = validate_daily_row_invariants(star_bar, CTX)
ok("STAR listing day-3 +35% passes with limit_exempt",
   r_star is not None and r_star.limit_exempt == "star_gem_first5" and not f_star)

main_bar = bar("600000.SH", date(2026, 7, 6), 10.0, 12.6, 9.9, 12.5, 10.0)  # +25%
r_main, f_main = validate_daily_row_invariants(main_bar, CTX)
ok("main board +25% is a failure (no exemption)",
   r_main is None and f_main and f_main[0].error_type == ErrorType.limit_band)

ipo_bar = bar("600100.SH", date(2026, 7, 6), 10.0, 14.5, 9.9, 14.4, 10.0)  # +44% day1
r_ipo, _ = validate_daily_row_invariants(ipo_bar, CTX)
ok("main board IPO day-1 +44% exempt as ipo_day1",
   r_ipo is not None and r_ipo.limit_exempt == "ipo_day1")

bj_bar = bar("830799.BJ", date(2026, 7, 6), 10.0, 12.6, 9.9, 12.5, 10.0)  # +25% BJ
r_bj, f_bj = validate_daily_row_invariants(bj_bar, CTX)
ok("BJ 30cm whitelist: +25% passes without exemption tag",
   r_bj is not None and r_bj.limit_exempt is None and not f_bj)

star_day6 = CTX.first_n_open_days(d0, 6)[5]
late_bar = bar("688001.SH", star_day6, 10.0, 13.6, 9.9, 13.5, 10.0)
r_late, f_late = validate_daily_row_invariants(late_bar, CTX)
ok("STAR day-6 +35% no longer exempt -> failure",
   r_late is None and f_late[0].error_type == ErrorType.limit_band)

try:
    DailyBarV1(symbol="688001.SH", trade_date=day3, open=10.0, high=12.0, low=9.9,
               close=13.5, pre_close=10.0, volume=100, amount=1350.0, is_suspended=False,
               source="t", fetched_at=now, available_at=close_dt(day3))
    ok("exemption never waives OHLC coherence", False)
except ValidationError:
    ok("exemption never waives OHLC coherence", True)


print("== A6: lineage WARN on sink write failure ==")
records = []


class Capture(logging.Handler):
    def emit(self, rec):
        records.append(rec)


logging.getLogger("factor_factory.contracts.lineage").addHandler(Capture())
bad_sink = LineageSink(Path("/dev/null/nonexistent/run_manifest.json"), run_id="warn-test")
wrote = bad_sink.record(ProviderAttempt(
    run_id="warn-test", data_type="daily", provider="tushare_proxy",
    operation="daily", success=True, latency_ms=1))
ok("lineage write failure returns False + WARN emitted + recorded in warnings",
   wrote is False
   and any("LINEAGE_WRITE_FAILED" in r.getMessage() for r in records)
   and any("LINEAGE_WRITE_FAILED" in w for w in bad_sink.warnings))


print("== bonus: unit self-check fail-closed ==")
bad_unit_rows = [DailyBarV1(
    symbol="600000.SH", trade_date=d1, open=8.68, high=8.93, low=8.62, close=8.92,
    pre_close=8.66, volume=793762, amount=698878044.0,   # volume left in 手 => ratio ~100
    is_suspended=False, source="t", fetched_at=now, available_at=close_dt(d1))]
fails_u: list = []
_unit_self_check("600000.SH", bad_unit_rows, fails_u)
ok("implied-VWAP unit breach -> failure + rows rejected (no auto-correct)",
   len(bad_unit_rows) == 0 and fails_u and fails_u[0].error_type == ErrorType.unit)


print("== A7 (R-3.1): pre_close per-source capability table ==")
ok("capability declarations frozen in contracts.yaml",
   pre_close_capability("daily_tushare") == "source_native"
   and pre_close_capability("daily_akshare_em") == "derived_same_row_delta"
   and pre_close_capability("daily_akshare_sina") == "unavailable")

try:
    pre_close_capability("security_meta_tushare")
    ok("undeclared capability is a ContractViolation", False)
except ContractViolation as exc:
    ok("undeclared capability is a ContractViolation", "capabilities.pre_close" in str(exc))

# native source emitting None => row-level failure, row removed (fail-closed)
rows_nat = [bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, 8.66),
            bar("600000.SH", d2, 8.89, 8.97, 8.78, 8.89, 8.92).model_copy(
                update={"pre_close": None})]
fails_cap: list = []
_pre_close_capability_check("daily_tushare", "600000.SH", rows_nat, fails_cap)
ok("source_native + pre_close=None row rejected",
   len(rows_nat) == 1 and rows_nat[0].pre_close == 8.66
   and fails_cap and fails_cap[0].error_type == ErrorType.schema
   and "capability breach" in fails_cap[0].message)

# 'unavailable' source suddenly emitting a value => also a failure
rows_unav = [bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, None, source="akshare_sina"),
             bar("600000.SH", d2, 8.89, 8.97, 8.78, 8.89, 8.92, source="akshare_sina")]
fails_cap2: list = []
_pre_close_capability_check("daily_akshare_sina", "600000.SH", rows_unav, fails_cap2)
ok("unavailable + pre_close value appeared -> row rejected",
   len(rows_unav) == 1 and rows_unav[0].pre_close is None and len(fails_cap2) == 1)

# conformant rows untouched in both regimes
rows_ok_nat = [bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, 8.66)]
rows_ok_unav = [bar("600000.SH", d1, 8.68, 8.93, 8.62, 8.92, None)]
f1: list = []
f2: list = []
_pre_close_capability_check("daily_tushare", "600000.SH", rows_ok_nat, f1)
_pre_close_capability_check("daily_akshare_sina", "600000.SH", rows_ok_unav, f2)
ok("conformant rows pass capability check", len(rows_ok_nat) == 1 and len(rows_ok_unav) == 1
   and not f1 and not f2)

# capability is surfaced in FetchMeta.notes (explicit, not implicit)
_, meta_note = FakeTushareDaily(good_frame).to_rows(
    FakeTushareDaily(good_frame).to_query(params), RawPayload(payload=good_frame))
ok("FetchMeta.notes carries pre_close_capability tag",
   any("pre_close_capability=source_native" in n for n in meta_note.notes))


print("== A8 (R-3.3): source health separate from contract compliance ==")
# flagship: all keys fail at fetch => 勾稽 still holds (contract GREEN) but health DEAD
dead_raw = RawPayload(payload={"frames": {
    "600000.SH": {"__fetch_error__": "RemoteDisconnected: peer closed"},
    "000001.SZ": {"__fetch_error__": "ProxyError: connection refused"},
}})
rep_dead, _, _ = run_harness(
    DailyAkshareEmFetcher(),
    {"symbols": ("600000.SH", "000001.SZ"), "start_date": d1, "end_date": d2},
    raw_override=dead_raw)
health_dead = assess_source_health(rep_dead)
ok("harness green (勾稽成立) while 0/2 keys ok",
   rep_dead.all_green and rep_dead.n_ok == 0 and rep_dead.n_failed == 2)
ok("health verdict = dead, decoupled from all_green", health_dead.status == "dead")

hb = HarnessReport(interface_id="x", t_ok=True, e_ok=True, t2_ok=True,
                   n_keys_expected=10, n_ok=4, n_failed=6)
ok("ok-rate 40% < 50% threshold -> degraded",
   assess_source_health(hb).status == "degraded")
hb2 = HarnessReport(interface_id="x", t_ok=True, e_ok=True, t2_ok=True,
                    n_keys_expected=10, n_ok=9, n_failed=1)
ok("ok-rate 90% -> healthy", assess_source_health(hb2).status == "healthy")
ok("threshold is configurable (40% healthy at threshold=0.3)",
   assess_source_health(hb, ok_rate_threshold=0.3).status == "healthy")

health_logs = []


class HCapture(logging.Handler):
    def emit(self, rec):
        health_logs.append(rec.getMessage())


logging.getLogger("factor_factory.contracts.health").addHandler(HCapture())
assess_source_health(rep_dead)
ok("degradation alert emitted (WARN) on dead source",
   any("SOURCE_HEALTH_DEAD" in m for m in health_logs))


print("== A9 (R-3.4): name reconciliation normalization ==")
ok("NFKC: full-width letter normalized (万科Ａ -> 万科A)",
   normalize_security_name("万科Ａ")[0] == "万科A"
   and names_equivalent("万科A", "万科Ａ"))
ok("XD prefix + source truncation tolerated (XD招商银 ~ 招商银行)",
   names_equivalent("XD招商银", "招商银行"))
ok("N prefix stripped (N瑞星 ~ 瑞星)", names_equivalent("N瑞星", "瑞星"))
ok("STAR -U suffix stripped (百济神州-U ~ 百济神州)",
   names_equivalent("百济神州-U", "百济神州"))
ok("ST is part of the name, never stripped",
   not names_equivalent("ST德豪", "德豪润达")
   and normalize_security_name("*ST海润")[1] is False)
ok("genuinely different names remain mismatched",
   not names_equivalent("浦发银行", "浦发银行X"))

meta_a9 = [SecurityMetaV1(symbol="000002.SZ", name="万科A", source="tushare_proxy",
                          fetched_at=now, available_at=now),
           SecurityMetaV1(symbol="600036.SH", name="招商银行", source="tushare_proxy",
                          fetched_at=now, available_at=now),
           SecurityMetaV1(symbol="600000.SH", name="浦发银行", source="tushare_proxy",
                          fetched_at=now, available_at=now)]
meta_b9 = [SecurityMetaV1(symbol="000002.SZ", name="万科Ａ", source="akshare_exchange",
                          fetched_at=now, available_at=now),
           SecurityMetaV1(symbol="600036.SH", name="XD招商银", source="akshare_exchange",
                          fetched_at=now, available_at=now),
           SecurityMetaV1(symbol="600000.SH", name="浦发银行X", source="akshare_exchange",
                          fetched_at=now, available_at=now)]
nm9 = reconcile_names(meta_a9, meta_b9)
ok("raw mismatch list is FULL (3 raw) while normalized verdict keeps 1",
   nm9["n_name_mismatches_raw"] == 3 and len(nm9["name_mismatches_raw"]) == 3
   and nm9["n_name_mismatches"] == 1
   and nm9["name_mismatches"][0]["symbol"] == "600000.SH")
ok("normalization rule recorded in manifest payload",
   "NFKC" in nm9["name_normalization"])


n_pass = sum(1 for _, c, _ in PASS if c)
print(f"\n=== CONTRACTS OFFLINE TESTS: {n_pass}/{len(PASS)} PASS ===")
sys.exit(0 if n_pass == len(PASS) else 1)
