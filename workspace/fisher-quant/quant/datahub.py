"""fisher-quant read-only data access layer.

GOLD data source: recommendations.db (SQLite). This module NEVER writes to it:
every connection is opened strictly read-only via SQLite URI (mode=ro).

Workspace-wide conventions honored here:
- trade_date is an 8-char string "YYYYMMDD".
- The trading calendar is DISTINCT trade_date of price_daily_cache.
- pct_chg is in PERCENT units (2.5 means +2.5%).
- ts_code looks like "000001.SZ"; stock_code is the bare 6-digit code
  (price_daily_cache primary key is (stock_code, trade_date), so all price
  queries filter on stock_code to hit the index).
"""

from __future__ import annotations

import bisect
import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

PRICE_COLUMNS = ["stock_code", "ts_code", "trade_date",
                 "open", "high", "low", "close", "pct_chg"]

# SQLite default variable limit is 999; stay well below it.
_SQL_IN_CHUNK = 400


# --------------------------------------------------------------------------
# Workspace-relative default paths (package lives at <workspace>/fisher-quant)
# --------------------------------------------------------------------------

def workspace_root() -> str:
    """Workspace root dir. Override with env FISHER_QUANT_WORKSPACE if needed."""
    env = os.environ.get("FISHER_QUANT_WORKSPACE")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))      # .../fisher-quant/quant
    return os.path.dirname(os.path.dirname(here))          # workspace root


def default_db_path() -> str:
    return os.path.join(workspace_root(), "stock_data", "03-working",
                        "recommendation_warehouse", "recommendations.db")


def selection_history_dir() -> str:
    return os.path.join(workspace_root(), "stock_data", "03-working",
                        "selection_history")


def verify_history_v2_dir() -> str:
    return os.path.join(workspace_root(), "stock_data", "03-working",
                        "verify_history_v2")


def strategy_research_dir() -> str:
    return os.path.join(workspace_root(), "stock_data", "03-working",
                        "strategy_research")


# --------------------------------------------------------------------------
# Code helpers
# --------------------------------------------------------------------------

def stock_code_of(ts_code) -> str:
    """'000001.SZ' -> '000001'; passthrough for bare codes."""
    return str(ts_code).strip().upper().split(".")[0]


def normalize_ts_code(code) -> str:
    """Best-effort bare-code -> ts_code ('600000' -> '600000.SH')."""
    c = str(code).strip().upper()
    if "." in c:
        return c
    if c.startswith(("6", "9")) and not c.startswith(("92",)):
        # 6xxxxx Shanghai A; 900xxx Shanghai B (rare)
        return c + ".SH"
    if c.startswith(("4", "8", "92")):
        return c + ".BJ"
    return c + ".SZ"


# --------------------------------------------------------------------------
# Board rules
# --------------------------------------------------------------------------

# ChiNext (创业板) and STAR (科创板) have a 20% daily limit; BSE (北交所) 30%;
# main board 10%. We return slightly conservative thresholds (19.8/29.8/9.8)
# so that, combined with the engine's 0.995 safety factor, opens at-or-near
# the rounded limit price are reliably caught as untradeable.
_CHINEXT_PREFIXES = ("300", "301", "302")
_STAR_PREFIXES = ("688", "689")
_BSE_PREFIXES = ("43", "83", "87", "88", "92")

BANK_NAME_HINTS = ("银行", "农商", "商行")


def limit_up_threshold(ts_code) -> float:
    """Board-aware daily limit-up threshold as a FRACTION (e.g. 0.098).

    主板 -> 9.8% | 创业板(300/301/302)、科创板(688/689) -> 19.8% |
    北交所(8xx/43x/92x) -> 29.8%
    """
    code = stock_code_of(ts_code)
    if code.startswith(_CHINEXT_PREFIXES) or code.startswith(_STAR_PREFIXES):
        return 0.198
    if code.startswith("8") or code.startswith(_BSE_PREFIXES):
        return 0.298
    return 0.098


def is_bank(name=None, sector=None) -> bool:
    """Bank detector: stock_dim sector '银行', with name-substring fallback."""
    if sector and "银行" in str(sector):
        return True
    if name and any(h in str(name) for h in BANK_NAME_HINTS):
        return True
    return False


# --------------------------------------------------------------------------
# Pure trade-date calendar helpers (calendar = sorted list of "YYYYMMDD")
# --------------------------------------------------------------------------

def next_trade_date(calendar: List[str], date, n: int = 1) -> Optional[str]:
    """Nth trading day strictly AFTER `date` (date need not be a trading day)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    pos = bisect.bisect_right(calendar, str(date)) + (n - 1)
    return calendar[pos] if pos < len(calendar) else None


def prev_trade_date(calendar: List[str], date, n: int = 1) -> Optional[str]:
    """Nth trading day strictly BEFORE `date`."""
    if n < 1:
        raise ValueError("n must be >= 1")
    pos = bisect.bisect_left(calendar, str(date)) - n
    return calendar[pos] if pos >= 0 else None


def trade_dates_between(calendar: List[str], start, end) -> List[str]:
    """Inclusive [start, end] slice of the calendar."""
    i = bisect.bisect_left(calendar, str(start))
    j = bisect.bisect_right(calendar, str(end))
    return calendar[i:j]


# --------------------------------------------------------------------------
# PriceBook: fast per-stock lookups over a long-format price frame
# --------------------------------------------------------------------------

class PriceBook:
    """Per-stock (dates, opens, closes) arrays with bisect lookups.

    Rows without a valid positive close are dropped (treated as non-trading
    days), which is how suspensions and partial days surface in the cache.
    """

    def __init__(self, prices: Optional[pd.DataFrame]):
        self._stocks: Dict[str, Tuple[List[str], np.ndarray, np.ndarray]] = {}
        if prices is None or len(prices) == 0:
            return
        df = prices.copy()
        df["ts_code"] = df["ts_code"].astype(str)
        df["trade_date"] = df["trade_date"].astype(str)
        df["open"] = pd.to_numeric(df["open"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df[df["close"].notna() & (df["close"] > 0)]
        df = df.sort_values(["ts_code", "trade_date"])
        for code, g in df.groupby("ts_code", sort=False):
            self._stocks[code] = (
                list(g["trade_date"]),
                g["open"].to_numpy(dtype=float),
                g["close"].to_numpy(dtype=float),
            )

    def has(self, ts_code) -> bool:
        return str(ts_code) in self._stocks

    def codes(self) -> List[str]:
        return list(self._stocks)

    def row_on(self, ts_code, date) -> Optional[Tuple[float, float]]:
        """(open, close) exactly on `date`, else None. open may be NaN."""
        s = self._stocks.get(str(ts_code))
        if not s:
            return None
        dates, opens, closes = s
        d = str(date)
        i = bisect.bisect_left(dates, d)
        if i < len(dates) and dates[i] == d:
            return float(opens[i]), float(closes[i])
        return None

    def close_before(self, ts_code, date) -> Optional[Tuple[str, float]]:
        """(trade_date, close) of the last bar strictly BEFORE `date`."""
        s = self._stocks.get(str(ts_code))
        if not s:
            return None
        dates, _, closes = s
        i = bisect.bisect_left(dates, str(date)) - 1
        return (dates[i], float(closes[i])) if i >= 0 else None

    def close_on_or_before(self, ts_code, date) -> Optional[Tuple[str, float]]:
        """(trade_date, close) of the last bar ON or BEFORE `date`."""
        s = self._stocks.get(str(ts_code))
        if not s:
            return None
        dates, _, closes = s
        i = bisect.bisect_right(dates, str(date)) - 1
        return (dates[i], float(closes[i])) if i >= 0 else None

    def last_date(self, ts_code) -> Optional[str]:
        s = self._stocks.get(str(ts_code))
        return s[0][-1] if s else None


# --------------------------------------------------------------------------
# DataHub: the read-only SQLite gateway
# --------------------------------------------------------------------------

class DataHub:
    """Read-only gateway to recommendations.db.

    Heavy artifacts (calendar + market baseline) are computed once per
    instance with a single GROUP BY scan and cached in memory.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._baseline_df: Optional[pd.DataFrame] = None
        self._calendar: Optional[List[str]] = None

    # -- connection ------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not os.path.exists(self.db_path):
                raise FileNotFoundError(f"gold db not found: {self.db_path}")
            uri = f"file:{self.db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DataHub":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- calendar + market baseline (one shared scan) ---------------------

    def _ensure_baseline(self) -> None:
        if self._baseline_df is None:
            df = pd.read_sql_query(
                "SELECT trade_date, AVG(pct_chg) AS mkt_pct_chg, "
                "COUNT(pct_chg) AS n_stocks "
                "FROM price_daily_cache GROUP BY trade_date ORDER BY trade_date",
                self.conn,
            )
            df["trade_date"] = df["trade_date"].astype(str)
            self._baseline_df = df
            self._calendar = list(df["trade_date"])

    def calendar(self) -> List[str]:
        """Sorted DISTINCT trade_date list (the workspace trading calendar)."""
        self._ensure_baseline()
        return self._calendar

    def market_baseline(self, start=None, end=None) -> pd.DataFrame:
        """All-stock equal-weight mean pct_chg per day (percent units)."""
        self._ensure_baseline()
        df = self._baseline_df
        if start is not None:
            df = df[df["trade_date"] >= str(start)]
        if end is not None:
            df = df[df["trade_date"] <= str(end)]
        return df.reset_index(drop=True)

    def baseline_map(self) -> Dict[str, float]:
        """{trade_date: market mean pct_chg} for O(1) lookups."""
        self._ensure_baseline()
        return dict(zip(self._baseline_df["trade_date"],
                        self._baseline_df["mkt_pct_chg"]))

    # -- calendar convenience wrappers ------------------------------------

    def next_trade_date(self, date, n: int = 1) -> Optional[str]:
        return next_trade_date(self.calendar(), date, n)

    def prev_trade_date(self, date, n: int = 1) -> Optional[str]:
        return prev_trade_date(self.calendar(), date, n)

    # -- prices ------------------------------------------------------------

    def prices(self, ts_codes: Iterable, start=None, end=None) -> pd.DataFrame:
        """Long-format OHLC frame for the given codes (chunked IN queries).

        Accepts ts_codes with or without exchange suffix. Queries by
        stock_code so the (stock_code, trade_date) primary key is used.
        """
        codes = sorted({stock_code_of(c) for c in ts_codes if c})
        if not codes:
            return pd.DataFrame(columns=PRICE_COLUMNS)
        cond, tail = "", []
        if start is not None:
            cond += " AND trade_date >= ?"
            tail.append(str(start))
        if end is not None:
            cond += " AND trade_date <= ?"
            tail.append(str(end))
        frames = []
        for i in range(0, len(codes), _SQL_IN_CHUNK):
            chunk = codes[i:i + _SQL_IN_CHUNK]
            ph = ",".join("?" * len(chunk))
            sql = (f"SELECT {', '.join(PRICE_COLUMNS)} FROM price_daily_cache "
                   f"WHERE stock_code IN ({ph}){cond} "
                   f"ORDER BY stock_code, trade_date")
            frames.append(pd.read_sql_query(sql, self.conn, params=chunk + tail))
        df = pd.concat(frames, ignore_index=True)
        # Fill missing ts_code from stock_code (defensive: column is nullable).
        mask = df["ts_code"].isna() | (df["ts_code"] == "")
        if mask.any():
            df.loc[mask, "ts_code"] = df.loc[mask, "stock_code"].map(normalize_ts_code)
        df["trade_date"] = df["trade_date"].astype(str)
        return df

    def price_book(self, ts_codes: Iterable, start=None, end=None) -> PriceBook:
        return PriceBook(self.prices(ts_codes, start=start, end=end))

    # -- dimensions ---------------------------------------------------------

    def sector_map(self) -> Dict[str, Dict[str, Optional[str]]]:
        """{ts_code: {'name':..., 'sector':...}} from stock_dim."""
        rows = self.conn.execute(
            "SELECT ts_code, stock_code, stock_name, sector_name FROM stock_dim"
        ).fetchall()
        out: Dict[str, Dict[str, Optional[str]]] = {}
        for ts, sc, name, sector in rows:
            key = str(ts) if ts else normalize_ts_code(sc)
            out[key] = {"name": name, "sector": sector}
        return out
