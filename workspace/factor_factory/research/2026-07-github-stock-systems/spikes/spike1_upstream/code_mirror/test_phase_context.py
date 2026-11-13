"""spike-1 unit tests for src/core/phase_context.py (cn-only PhaseContext).

All scenarios use an injected clock + injected calendar: no network, no
wall-clock dependence. Calendar facts used: 2026-07-06..10 are Mon-Fri
trading days, 2026-07-11/12 weekend, 2026-07-13 Monday trading day.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.core.phase_context import (
    PartialBarCacheWriteBlocked,
    Phase,
    PhaseContextError,
    build_phase_context,
    guard_backtest_cache_write,
    load_trading_calendar,
    refuse_bare_run_if_partial,
)

CAL = ["20260703", "20260706", "20260707", "20260708", "20260709", "20260710",
       "20260713", "20260714", "20260715", "20260716", "20260717"]
CAL_HOLIDAY_0708 = [d for d in CAL if d != "20260708"]  # synthetic mid-week holiday


def ctx_at(iso: str, cal=None, **kw):
    cal = CAL if cal is None else cal
    return build_phase_context(
        now_fn=lambda: datetime.fromisoformat(iso),
        calendar_provider=lambda: cal,
        **kw,
    )


# ---------------- acceptance 2: 14:59 / 15:01 ----------------

def test_1459_closing_auction_effective_prev_session():
    ctx = ctx_at("2026-07-10T14:59:00")
    assert ctx.phase == Phase.CLOSING_AUCTION
    assert ctx.effective_trade_date == "20260709"
    assert ctx.is_partial_bar is True


def test_1501_postmarket_effective_today():
    ctx = ctx_at("2026-07-10T15:01:00")
    assert ctx.phase == Phase.POSTMARKET
    assert ctx.effective_trade_date == "20260710"
    assert ctx.is_partial_bar is False


# ---------------- phase boundary lattice ----------------

@pytest.mark.parametrize("hhmm,expected,partial", [
    ("09:29", Phase.PREMARKET, False),
    ("09:30", Phase.INTRADAY, True),
    ("11:29", Phase.INTRADAY, True),
    ("11:30", Phase.LUNCH_BREAK, True),
    ("12:59", Phase.LUNCH_BREAK, True),
    ("13:00", Phase.INTRADAY, True),
    ("14:56", Phase.INTRADAY, True),
    ("14:57", Phase.CLOSING_AUCTION, True),
    ("14:59", Phase.CLOSING_AUCTION, True),
    ("15:00", Phase.POSTMARKET, False),
    ("23:59", Phase.POSTMARKET, False),
])
def test_phase_boundaries(hhmm, expected, partial):
    ctx = ctx_at(f"2026-07-10T{hhmm}:00")
    assert ctx.phase == expected
    assert ctx.is_partial_bar is partial


def test_premarket_effective_is_prev_session():
    ctx = ctx_at("2026-07-10T07:47:00")  # the 0710 incident's bare pre-market run time
    assert ctx.phase == Phase.PREMARKET
    assert ctx.effective_trade_date == "20260709"


# ---------------- R-1 revision: premarket bare runs must be refused ----------------
# PHASE3_SPIKES_REPORT §2 R-1 / spike-1 F-1: premarket is_partial_bar=False
# (upstream semantics, preserved), so integration point ② must refuse on
# `is_partial_bar or phase is PREMARKET` — never weaker than the production
# guard's 00:00-15:05 window.

def test_r1_premarket_0747_bare_run_refused():
    """The 0710 incident moment (07:47 premarket bare run) MUST be blocked."""
    ctx = ctx_at("2026-07-10T07:47:00")
    assert ctx.phase == Phase.PREMARKET
    assert ctx.is_partial_bar is False  # upstream semantics untouched
    assert refuse_bare_run_if_partial(ctx, has_explicit_trade_date=False) is True


def test_r1_premarket_with_explicit_trade_date_allowed():
    # operator intent (--trade-date) still passes; data labels with the
    # previous completed session via effective_trade_date (S6 semantics)
    ctx = ctx_at("2026-07-10T07:47:00")
    assert refuse_bare_run_if_partial(ctx, has_explicit_trade_date=True) is False
    assert ctx.effective_trade_date == "20260709"


@pytest.mark.parametrize("iso,refused", [
    ("2026-07-10T00:01:00", True),    # premarket (midnight edge of prod 00:00-15:05 window)
    ("2026-07-10T07:47:00", True),    # premarket, real 0710 incident time
    ("2026-07-10T09:29:00", True),    # premarket, last minute before open
    ("2026-07-10T10:00:00", True),    # intraday (partial bar)
    ("2026-07-10T12:00:00", True),    # lunch break (partial bar)
    ("2026-07-10T14:58:00", True),    # closing auction (partial bar)
    ("2026-07-10T15:00:00", False),   # postmarket: bare run allowed
    ("2026-07-10T19:30:00", False),   # postmarket: the nightly pipeline slot
    ("2026-07-12T10:00:00", False),   # non_trading: handled by stage0 gate, not ②
])
def test_r1_bare_run_refusal_lattice(iso, refused):
    ctx = ctx_at(iso)
    assert refuse_bare_run_if_partial(ctx, has_explicit_trade_date=False) is refused


def test_r1_mini_pipeline_0747_premarket_bare_run_exits_5(tmp_path: Path):
    """Pipeline-level regression: the rehearsal pipeline refuses the 07:47
    premarket bare run (exit 5) before any fetch/cache stage is entered."""
    import subprocess
    import sys as _sys

    repo = Path(__file__).resolve().parents[1]
    cal_file = tmp_path / "cal.json"
    cal_file.write_text(json.dumps({"trade_dates": CAL}), encoding="utf-8")
    outdir = tmp_path / "out"
    proc = subprocess.run(
        [_sys.executable, str(repo / "spike_harness" / "mini_pipeline.py"),
         "--fake-now", "2026-07-10T07:47:00",
         "--calendar-file", str(cal_file),
         "--outdir", str(outdir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 5, proc.stderr
    manifest = json.loads((outdir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["phase_context"]["phase"] == "premarket"
    assert manifest["phase_context"]["effective_trade_date"] == "20260709"
    assert "fetch" not in manifest["stages"]  # refused before fetch stage
    assert not (outdir / "backtest_cache_sandbox").exists()  # nothing written
    alerts = [json.loads(l) for l in (outdir / "alerts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(a.get("kind") == "bare_run_refused" for a in alerts)


# ---------------- acceptance 3: non-trading days ----------------

def test_weekend_non_trading_effective_prev_friday():
    ctx = ctx_at("2026-07-12T10:00:00")  # Sunday
    assert ctx.phase == Phase.NON_TRADING
    assert ctx.is_partial_bar is False
    assert ctx.effective_trade_date == "20260710"


def test_midweek_holiday_non_trading():
    ctx = ctx_at("2026-07-08T10:00:00", cal=CAL_HOLIDAY_0708)
    assert ctx.phase == Phase.NON_TRADING
    assert ctx.effective_trade_date == "20260707"


# ---------------- acceptance 4: fail-closed (network down) ----------------

def _netdown():
    raise ConnectionError("simulated: akshare unreachable")


def test_netdown_without_cache_raises_fail_closed():
    with pytest.raises(PhaseContextError, match="calendar_unavailable"):
        build_phase_context(
            now_fn=lambda: datetime.fromisoformat("2026-07-10T16:00:00"),
            calendar_provider=_netdown,
        )


def test_netdown_with_cache_falls_back_with_warning(tmp_path: Path):
    cache = tmp_path / "cal.json"
    cache.write_text(json.dumps({"trade_dates": CAL}), encoding="utf-8")
    dates, source, warnings = load_trading_calendar(_netdown, cache_path=cache)
    assert source == "cached"
    assert dates == sorted(CAL)
    assert any(w.startswith("calendar_fetch_failed_used_cache") for w in warnings)


def test_netdown_with_corrupt_cache_raises(tmp_path: Path):
    cache = tmp_path / "cal.json"
    cache.write_text("{not json", encoding="utf-8")
    with pytest.raises(PhaseContextError, match="calendar_unavailable"):
        load_trading_calendar(_netdown, cache_path=cache)


def test_stale_calendar_raises_instead_of_natural_day():
    # upstream fail-open would fall back to the natural day; we must raise
    with pytest.raises(PhaseContextError, match="calendar_stale"):
        ctx_at("2026-08-01T16:00:00")  # beyond CAL coverage


def test_empty_calendar_raises():
    with pytest.raises(PhaseContextError):
        ctx_at("2026-07-10T16:00:00", cal=[])


# ---------------- acceptance 5: 0710-type cache pollution replay ----------------

def test_partial_bar_cache_write_blocked_intraday():
    ctx = ctx_at("2026-07-10T11:00:00")  # workday intraday moment
    assert ctx.is_partial_bar is True
    with pytest.raises(PartialBarCacheWriteBlocked):
        guard_backtest_cache_write(ctx)


def test_partial_bar_cache_write_allowed_with_explicit_flag():
    ctx = ctx_at("2026-07-10T11:00:00")
    assert guard_backtest_cache_write(ctx, explicit_intraday_flag=True) is True


def test_postmarket_cache_write_allowed():
    ctx = ctx_at("2026-07-10T19:30:00")
    assert guard_backtest_cache_write(ctx) is True


def test_bare_run_refusal_helper():
    intraday = ctx_at("2026-07-10T10:00:00")
    premarket = ctx_at("2026-07-10T07:47:00")  # R-1: premarket also refused
    postmarket = ctx_at("2026-07-10T19:30:00")
    assert refuse_bare_run_if_partial(intraday, has_explicit_trade_date=False) is True
    assert refuse_bare_run_if_partial(intraday, has_explicit_trade_date=True) is False
    assert refuse_bare_run_if_partial(premarket, has_explicit_trade_date=False) is True
    assert refuse_bare_run_if_partial(postmarket, has_explicit_trade_date=False) is False


# ---------------- design invariants ----------------

def test_no_unknown_phase_member():
    assert "UNKNOWN" not in Phase.__members__
    assert "unknown" not in {p.value for p in Phase}


def test_to_dict_is_json_safe_and_complete():
    ctx = ctx_at("2026-07-10T15:30:00")
    d = json.loads(json.dumps(ctx.to_dict()))
    assert set(d) == {"phase", "now_local", "effective_trade_date",
                      "is_partial_bar", "calendar_source", "warnings"}
    assert d["calendar_source"] == "injected"


def test_naive_datetime_interpreted_as_shanghai():
    ctx = ctx_at("2026-07-10T15:01:00")
    assert ctx.now_local.endswith("+08:00")
