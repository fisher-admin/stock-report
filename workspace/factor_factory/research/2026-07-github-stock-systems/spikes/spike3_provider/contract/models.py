"""Standard row models + market-context invariants (limit-band exemption table).

DailyBarV1 per openbb.md §2.1 field table incl. the limit-up/down exemption
table; announcement model per akshare_audit.md §2.11; name/industry model for
tushare stock_basic vs akshare stock_info_a_code_name reconciliation.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from pydantic import ConfigDict, field_validator, model_validator

from .core import ErrorType, FailureRecord, StrictRow

CN_TZ = timezone(timedelta(hours=8))
SYMBOL_RE = r"^\d{6}\.(SH|SZ|BJ)$"

# mixed main/GEM band cap per spec; BJ 30cm whitelist
BAND_DEFAULT = 0.205
BAND_BJ = 0.305

# static whitelists for the two non-derivable exemption classes (low freq)
DELIST_DAY1_WHITELIST: Set[Tuple[str, date]] = set()
RESTRUCTURE_RESUME_WHITELIST: Set[Tuple[str, date]] = set()


def close_dt(d: date) -> datetime:
    """A-share close timestamp for a trade date (daily bar available_at)."""
    return datetime.combine(d, time(15, 0), tzinfo=CN_TZ)


class DailyBarV1(StrictRow):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: Optional[float] = None   # from source only; self-computation banned.
    # None allowed ONLY for sources that do not carry it (akshare_sina daily) —
    # recorded as capability gap in FetchMeta.notes; spec-revision candidate.
    volume: int                          # unit = 股 (shares)
    amount: float                        # unit = 元 (yuan)
    adj_factor: Optional[float] = None
    is_suspended: bool
    limit_exempt: Optional[str] = None   # ipo_day1|star_gem_first5|delist_day1|restructure_resume
    source: str
    fetched_at: datetime
    available_at: datetime               # trade_date 15:00 +08:00

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        import re

        if not re.match(SYMBOL_RE, v):
            raise ValueError(f"symbol not in tushare notation: {v!r}")
        return v

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _pos_price(cls, v: float) -> float:
        if not v > 0:
            raise ValueError(f"price must be > 0, got {v}")
        return v

    @field_validator("pre_close")
    @classmethod
    def _pre_close(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not v > 0:
            raise ValueError(f"pre_close must be > 0 when present, got {v}")
        return v

    @field_validator("adj_factor")
    @classmethod
    def _adj(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not v > 0:
            raise ValueError(f"adj_factor must be > 0, got {v}")
        return v

    @field_validator("volume")
    @classmethod
    def _vol(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"volume must be >= 0, got {v}")
        return v

    @field_validator("amount")
    @classmethod
    def _amt(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"amount must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def _ohlc_coherence(self) -> "DailyBarV1":
        if self.low > min(self.open, self.close) + 1e-9:
            raise ValueError(f"low {self.low} > min(open,close) {min(self.open, self.close)}")
        if self.high < max(self.open, self.close) - 1e-9:
            raise ValueError(f"high {self.high} < max(open,close) {max(self.open, self.close)}")
        return self


class SecurityMetaV1(StrictRow):
    """Name/industry standard row (tushare stock_basic ∪ akshare code_name)."""

    model_config = ConfigDict(extra="forbid")
    symbol: str
    name: str
    industry: Optional[str] = None    # akshare source has no industry column
    list_date: Optional[date] = None  # akshare source has no list_date
    source: str
    fetched_at: datetime
    available_at: datetime            # snapshot semantics: = fetched_at

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        import re

        if not re.match(SYMBOL_RE, v):
            raise ValueError(f"symbol not in tushare notation: {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("empty name")
        return v.strip().replace("　", "").replace(" ", "")


class EarningsPreannounceV1(StrictRow):
    """stock_yjyg_em family standard row. PIT anchor = NOTICE_DATE."""

    model_config = ConfigDict(extra="forbid")
    symbol: str
    name: str
    indicator: str                    # PREDICT_FINANCE
    period: date                      # REPORT_DATE (fiscal period end)
    ann_date: date                    # NOTICE_DATE (PIT anchor)
    forecast_value: Optional[float] = None
    prev_year_value: Optional[float] = None
    change_pct: Optional[float] = None   # 百分点
    predict_type: Optional[str] = None
    is_preliminary: bool = True       # 预告 semantics — always preliminary here
    source: str
    fetched_at: datetime
    available_at: datetime            # ann_date at 00:00 +08:00 (source precision)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        import re

        if not re.match(SYMBOL_RE, v):
            raise ValueError(f"symbol not in tushare notation: {v!r}")
        return v

    @model_validator(mode="after")
    def _pit_anchor(self) -> "EarningsPreannounceV1":
        if self.ann_date < self.period - timedelta(days=370):
            raise ValueError(f"ann_date {self.ann_date} absurdly before period {self.period}")
        return self


# --------------------------------------------------------------------------
# market-context invariants (T2-stage, cross-table: calendar + basic)
# --------------------------------------------------------------------------
class MarketContext:
    """Open-day calendar + listing metadata used by row-level invariants."""

    def __init__(self, open_days: Set[date], list_dates: Dict[str, date]):
        self.open_days = open_days
        self._sorted = sorted(open_days)
        self.list_dates = list_dates

    def first_n_open_days(self, start: date, n: int) -> List[date]:
        return [d for d in self._sorted if d >= start][:n]


def board_of(symbol: str) -> str:
    code, _, suffix = symbol.partition(".")
    if suffix == "BJ":
        return "BSE"
    if code.startswith("688") or code.startswith("689"):
        return "STAR"
    if code.startswith("300") or code.startswith("301"):
        return "GEM"
    return "MAIN"


def limit_exemption(symbol: str, trade_date: date, ctx: MarketContext) -> Optional[str]:
    """Derivable exemptions first (zero maintenance), whitelists after.
    Exemption only waives the band assertion — OHLC/unit checks still apply."""
    ld = ctx.list_dates.get(symbol)
    if ld is not None:
        if trade_date == ld:
            return "ipo_day1"
        if board_of(symbol) in ("STAR", "GEM"):
            if trade_date in ctx.first_n_open_days(ld, 5):
                return "star_gem_first5"
    if (symbol, trade_date) in DELIST_DAY1_WHITELIST:
        return "delist_day1"
    if (symbol, trade_date) in RESTRUCTURE_RESUME_WHITELIST:
        return "restructure_resume"
    return None


def validate_daily_row_invariants(
    row: DailyBarV1, ctx: MarketContext
) -> Tuple[Optional[DailyBarV1], List[FailureRecord]]:
    """Row-level T2 invariants. Breach => FailureRecord, never silent repair.

    Returns (row_or_None, failures). Row may come back annotated with
    limit_exempt when an exemption legitimately waives the band assertion.
    """
    failures: List[FailureRecord] = []
    key = f"{row.symbol}:{row.trade_date.isoformat()}"

    # calendar membership (rejects future/holiday dates — myhhub 反例)
    if row.trade_date not in ctx.open_days:
        failures.append(
            FailureRecord(
                key=key,
                error_type=ErrorType.calendar,
                message=f"trade_date {row.trade_date} not an open day in TradingCalendarV1",
                row_level=True,
            )
        )
        return None, failures

    # non-suspended => volume > 0
    if not row.is_suspended and row.volume <= 0:
        failures.append(
            FailureRecord(
                key=key,
                error_type=ErrorType.unit,
                message="non-suspended row with volume<=0",
                row_level=True,
            )
        )
        return None, failures

    # limit band (only evaluable when pre_close present)
    if row.pre_close is not None:
        pct = abs(row.close / row.pre_close - 1.0)
        band = BAND_BJ if board_of(row.symbol) == "BSE" else BAND_DEFAULT
        if pct > band:
            exempt = limit_exemption(row.symbol, row.trade_date, ctx)
            if exempt is not None:
                row = row.model_copy(update={"limit_exempt": exempt})
            else:
                failures.append(
                    FailureRecord(
                        key=key,
                        error_type=ErrorType.limit_band,
                        message=f"|close/pre_close-1|={pct:.4f} > band {band} with no exemption",
                        row_level=True,
                    )
                )
                return None, failures
    return row, failures
