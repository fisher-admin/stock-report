"""PhaseContext — cn-only market phase context (spike-1 backport rehearsal).

Backport of upstream daily_stock_analysis MarketPhase/MarketPhaseContext/
is_partial_bar (src/core/trading_calendar.py @d08374898c), trimmed per
factor_factory/research/2026-07-github-stock-systems/deep/upstream_diff.md §4.3:

  - cn single market: no market parameter, close fixed at 15:00,
    closing-auction window fixed at 14:57-15:00, lunch break 11:30-13:00.
  - NO ``unknown`` phase: where upstream degrades to UNKNOWN / fail-open
    (calendar unavailable -> "treat as open", natural-day fallback), this
    module raises ``PhaseContextError`` instead (fail-closed inversion,
    upstream_diff.md §4.2 排雷表 — all 7 spots inverted or N/A here).
  - Calendar source: akshare ``tool_trade_date_hist_sina`` (our existing
    primary since #23) with an optional local JSON cache fallback.
    exchange-calendars is deliberately NOT imported.
  - Injectable clock (``now_fn``) and calendar provider so 14:59 / 15:01 /
    holiday / network-down scenarios are testable without waiting for
    real wall-clock time.

Target home after spike acceptance: factor_factory/data/phase_context.py.
Stdlib-only at import time; akshare/pandas imported lazily inside the
default calendar provider only.
"""

from __future__ import annotations

import bisect
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time, tzinfo
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

try:  # py>=3.9
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py38 fallback not supported
    ZoneInfo = None  # type: ignore

CN_TZ: tzinfo = ZoneInfo("Asia/Shanghai") if ZoneInfo else None  # type: ignore

# cn session boundaries (fixed; no session_close lookup needed)
OPEN_T = time(9, 30)
LUNCH_START_T = time(11, 30)
LUNCH_END_T = time(13, 0)
AUCTION_START_T = time(14, 57)
CLOSE_T = time(15, 0)

_MIN_CALENDAR_LEN = 100  # sanity floor: a real full calendar has thousands of sessions


class PhaseContextError(RuntimeError):
    """Fail-closed error: phase/effective date could NOT be established.

    Callers must abort (non-zero exit) and alert. Never swallow into a
    default phase or a natural-day effective date (upstream fail-open
    behaviours we deliberately invert)."""


class PartialBarCacheWriteBlocked(PhaseContextError):
    """Raised by guard_backtest_cache_write when a partial-bar-phase run
    attempts to write backtest cache without an explicit intraday flag
    (structural fix for the 2026-07-10 intraday cache pollution incident)."""


class Phase(str, Enum):
    PREMARKET = "premarket"
    INTRADAY = "intraday"
    LUNCH_BREAK = "lunch_break"
    CLOSING_AUCTION = "closing_auction"
    POSTMARKET = "postmarket"
    NON_TRADING = "non_trading"
    # NOTE: no UNKNOWN member by design (§4.3). Inference is total for any
    # timestamp covered by the calendar; anything else raises.


PARTIAL_BAR_PHASES = frozenset({Phase.INTRADAY, Phase.LUNCH_BREAK, Phase.CLOSING_AUCTION})

# R-1 (PHASE3_SPIKES_REPORT §2): phases in which a bare run (no explicit
# --trade-date) must be refused. PREMARKET is included even though its
# is_partial_bar=False (upstream semantics, deliberately preserved): today's
# daily bar does not exist yet, so a bare run would mirror the 2026-07-10
# 07:47 premarket incident shape. This keeps parity with the production
# guard's 00:00-15:05 window (run_pipeline.py L433-440) — never weaker.
BARE_RUN_REFUSAL_PHASES = frozenset(PARTIAL_BAR_PHASES | {Phase.PREMARKET})


@dataclass(frozen=True)
class PhaseContext:
    phase: Phase
    now_local: str                # iso8601, Asia/Shanghai
    effective_trade_date: str     # YYYYMMDD close-aware effective trading day
    is_partial_bar: bool
    calendar_source: str          # "akshare" | "cached" | "injected"
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "now_local": self.now_local,
            "effective_trade_date": self.effective_trade_date,
            "is_partial_bar": self.is_partial_bar,
            "calendar_source": self.calendar_source,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# calendar loading (akshare primary, local JSON cache fallback, fail-closed)
# ---------------------------------------------------------------------------

def akshare_calendar_provider() -> List[str]:
    """Fetch full cn trading calendar via akshare (sina). Raises on failure."""
    import akshare as ak  # lazy: keep module import stdlib-only

    df = ak.tool_trade_date_hist_sina()
    dates = sorted({str(d).replace("-", "")[:8] for d in df["trade_date"].astype(str)})
    if len(dates) < _MIN_CALENDAR_LEN:
        raise PhaseContextError(
            f"calendar_suspicious: akshare returned only {len(dates)} sessions"
        )
    return dates


def _write_cache_atomic(cache_path: Path, dates: Sequence[str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(cache_path.parent), prefix=f".{cache_path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"trade_dates": list(dates)}, f)
        os.replace(tmp, cache_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_trading_calendar(
    calendar_provider: Optional[Callable[[], Iterable[str]]] = None,
    cache_path: Optional[Path] = None,
) -> Tuple[List[str], str, List[str]]:
    """Return (sorted trade dates, calendar_source, warnings).

    Fail-closed: if the provider fails AND no usable cache exists,
    raises PhaseContextError. Never returns a guessed/natural calendar.
    """
    warnings: List[str] = []
    injected = calendar_provider is not None
    provider = calendar_provider or akshare_calendar_provider
    try:
        dates = sorted(set(provider()))
        if len(dates) < 2:
            raise PhaseContextError(f"calendar_suspicious: only {len(dates)} sessions")
        source = "injected" if injected else "akshare"
        if cache_path is not None and not injected:
            try:
                _write_cache_atomic(Path(cache_path), dates)
            except Exception as exc:  # cache write failure must not kill the run
                warnings.append(f"calendar_cache_write_failed:{type(exc).__name__}")
        return dates, source, warnings
    except PhaseContextError:
        raise
    except Exception as exc:
        if cache_path is not None and Path(cache_path).exists():
            try:
                payload = json.loads(Path(cache_path).read_text(encoding="utf-8"))
                dates = sorted(set(payload["trade_dates"]))
            except Exception as cache_exc:
                raise PhaseContextError(
                    f"calendar_unavailable: fetch failed ({type(exc).__name__}: {exc}) "
                    f"and cache unreadable ({type(cache_exc).__name__})"
                ) from exc
            if len(dates) < 2:
                raise PhaseContextError("calendar_unavailable: cache too small") from exc
            warnings.append(f"calendar_fetch_failed_used_cache:{type(exc).__name__}")
            return dates, "cached", warnings
        raise PhaseContextError(
            f"calendar_unavailable: {type(exc).__name__}: {exc} (no cache fallback)"
        ) from exc


# ---------------------------------------------------------------------------
# phase inference (total for calendar-covered dates; fail-closed otherwise)
# ---------------------------------------------------------------------------

def _infer_phase(now_local: datetime, is_trading_day: bool) -> Phase:
    if not is_trading_day:
        return Phase.NON_TRADING
    t = now_local.timetz().replace(tzinfo=None)
    if t < OPEN_T:
        return Phase.PREMARKET
    if t < LUNCH_START_T:
        return Phase.INTRADAY
    if t < LUNCH_END_T:
        return Phase.LUNCH_BREAK
    if t < AUCTION_START_T:
        return Phase.INTRADAY
    if t < CLOSE_T:
        return Phase.CLOSING_AUCTION
    return Phase.POSTMARKET


def build_phase_context(
    now_fn: Optional[Callable[[], datetime]] = None,
    calendar_provider: Optional[Callable[[], Iterable[str]]] = None,
    cache_path: Optional[Path] = None,
) -> PhaseContext:
    """Build the cn PhaseContext. All failure modes raise PhaseContextError.

    now_fn: returns an aware or naive datetime; naive is interpreted as
            Asia/Shanghai. Defaults to wall clock in Asia/Shanghai.
    """
    now = (now_fn or (lambda: datetime.now(CN_TZ)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    now_local = now.astimezone(CN_TZ)

    dates, source, warnings = load_trading_calendar(calendar_provider, cache_path)

    today = now_local.strftime("%Y%m%d")
    if today > dates[-1]:
        # calendar does not cover "now" -> cannot classify; fail-closed
        raise PhaseContextError(
            f"calendar_stale: max session {dates[-1]} < today {today}"
        )
    if today < dates[0]:
        raise PhaseContextError(
            f"calendar_out_of_range: min session {dates[0]} > today {today}"
        )

    date_set = set(dates)
    is_trading_day = today in date_set
    phase = _infer_phase(now_local, is_trading_day)

    if phase == Phase.POSTMARKET:
        effective = today
    else:
        # non_trading day, or trading day before close: previous completed session
        idx = bisect.bisect_left(dates, today)
        if idx == 0:
            raise PhaseContextError(
                f"no_previous_session: today {today} precedes calendar head {dates[0]}"
            )
        effective = dates[idx - 1]

    return PhaseContext(
        phase=phase,
        now_local=now_local.isoformat(),
        effective_trade_date=effective,
        is_partial_bar=phase in PARTIAL_BAR_PHASES,
        calendar_source=source,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# one-line-callable guards for the 4 production integration points
# ---------------------------------------------------------------------------

def guard_backtest_cache_write(ctx: PhaseContext, explicit_intraday_flag: bool = False) -> bool:
    """Integration point ③: call as ONE line before any backtest_cache write.

    Blocks the 2026-07-10 class of incident: an intraday (partial-bar) run
    silently writing partial-day rows into backtest cache as if they were
    completed daily bars."""
    if ctx.is_partial_bar and not explicit_intraday_flag:
        raise PartialBarCacheWriteBlocked(
            f"backtest_cache write blocked: phase={ctx.phase.value} is a partial-bar "
            f"phase at {ctx.now_local}; pass explicit_intraday_flag=True only for "
            f"deliberately-intraday artifacts"
        )
    return True


def refuse_bare_run_if_partial(ctx: PhaseContext, has_explicit_trade_date: bool) -> bool:
    """Integration point ②: ONE-line replacement for scattered weekday/clock
    string comparisons in bare-run guards. Returns True when the run must be
    refused.

    R-1 revision (PHASE3_SPIKES_REPORT §2 / spike-1 F-1): the condition is
    ``is_partial_bar or phase is PREMARKET`` — checking is_partial_bar alone
    would let a 07:47-type premarket bare run through (premarket
    is_partial_bar=False by upstream semantics) and weaken the existing
    production guard which covers the full 00:00-15:05 window. Encoding the
    compound condition HERE (single source of truth) means every call site,
    including the future production one-liner, inherits it and cannot
    re-introduce the weakened form."""
    return ctx.phase in BARE_RUN_REFUSAL_PHASES and not has_explicit_trade_date
