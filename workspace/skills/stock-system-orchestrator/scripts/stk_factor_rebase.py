#!/usr/bin/env python3
"""Rebase cached provider stk_factor rows to tonight's qfq base (2026-10-30).

The short-track research used to refetch 21 days x ~5,500 securities every
night because provider qfq prices are expressed against the latest adjustment
factor, so yesterday's cache sits on a different base.  Provider rows carry the
raw price, the day's ``adj_factor`` and base-independent hfq prices, so:

* qfq at base T = raw x adj(d) / adj(T), at provider precision (5 decimals);
* MACD is linear in price and scales by the same base ratio (3 decimals);
* a security whose base did not change keeps its cached values bit for bit.

Validation (docs/stock-system-phase2-validation-2026-10-30.md): on the
20261029 window the rebased inputs gave the identical v4.4 Top-20 in the same
order; prices differ from a full refetch by <= 0.00011, MACD by <= 1 tick.

The math is only valid while a security's hfq series is continuous across the
cached frames: close_hfq(d) == pre_close_hfq(next open day) holds through every
dividend or split.  A break means the provider rescaled that security's
adjustment history (Beijing 920 code migration) or corrected a factor, so it
must be refetched by code instead of rebased.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PRICE_COLUMNS = ("open_qfq", "high_qfq", "low_qfq", "close_qfq")
MACD_COLUMNS = ("macd_dif", "macd_dea")
REBASE_COLUMNS = frozenset({"ts_code", "trade_date", "open", "high", "low", "close", "adj_factor",
                            "close_hfq", "close_qfq", "pre_close_hfq", *PRICE_COLUMNS, *MACD_COLUMNS})
PRICE_DECIMALS = 5
MACD_DECIMALS = 3
ADJ_DECIMALS = 4


def base_adj(frame: pd.DataFrame) -> pd.Series:
    """Adjustment factor of the qfq base each row was published against."""
    indexed = frame.set_index("ts_code")
    qfq = pd.to_numeric(indexed["close_qfq"], errors="coerce")
    hfq = pd.to_numeric(indexed["close_hfq"], errors="coerce")
    return (hfq / qfq.where(qfq != 0)).round(ADJ_DECIMALS)


def broken_hfq_chain(frames: dict[str, pd.DataFrame], dates: list[str]) -> set[str]:
    """Securities whose hfq series is not continuous between adjacent frames."""
    broken: set[str] = set()
    for previous, current in zip(dates, dates[1:]):
        left = frames[previous].set_index("ts_code")
        right = frames[current].set_index("ts_code")
        common = left.index.intersection(right.index)
        close = pd.to_numeric(left.loc[common, "close_hfq"], errors="coerce")
        pre_close = pd.to_numeric(right.loc[common, "pre_close_hfq"], errors="coerce")
        ok = np.isclose(close.to_numpy(float), pre_close.to_numpy(float), rtol=2e-5, atol=1e-3)
        broken.update(str(code) for code in common[~ok])
    return broken


def gap_codes(frames: dict[str, pd.DataFrame], dates: list[str]) -> set[str]:
    """Securities whose cached rows may be incomplete.

    A night's cache can miss rows the provider filled in later, typically a new
    listing's first day.  Covers codes missing between two present days and
    codes that first appear after the window start (new listings, resumptions);
    a by-code refetch restores whatever the provider now has.
    """
    codes = {date: set(frames[date]["ts_code"].astype(str)) for date in dates}
    missing: set[str] = set()
    for before, day, after in zip(dates, dates[1:], dates[2:]):
        missing |= (codes[before] & codes[after]) - codes[day]
    seen_later = set().union(*(codes[date] for date in dates[1:])) if len(dates) > 1 else set()
    return missing | (seen_later - codes[dates[0]])


def rebase_frame(frame: pd.DataFrame, target_adj: pd.Series) -> tuple[pd.DataFrame, set[str]]:
    """Rebase one cached day to ``target_adj`` (indexed by ts_code).

    Returns the rebased frame and the codes that could not be rebased (no
    target base or unusable values); the caller must refetch those by code.
    """
    out = frame.copy()
    codes = out["ts_code"].astype(str)
    own = base_adj(frame).reindex(codes).to_numpy(float)
    target = pd.to_numeric(target_adj, errors="coerce").reindex(codes).to_numpy(float)
    unusable = ~np.isfinite(own) | ~np.isfinite(target) | (target <= 0)
    ratio = np.where(unusable, 1.0, own / np.where(target > 0, target, 1.0))
    changed = ~unusable & (ratio != 1.0)
    if changed.any():
        adj = pd.to_numeric(out["adj_factor"], errors="coerce").to_numpy(float)
        for column in PRICE_COLUMNS:
            raw = pd.to_numeric(out[column.replace("_qfq", "")], errors="coerce").to_numpy(float)
            rebased = np.round(raw * adj / np.where(target > 0, target, 1.0), PRICE_DECIMALS)
            out[column] = np.where(changed, rebased, pd.to_numeric(out[column], errors="coerce"))
        for column in MACD_COLUMNS:
            values = pd.to_numeric(out[column], errors="coerce").to_numpy(float)
            out[column] = np.where(changed, np.round(values * ratio, MACD_DECIMALS), values)
    return out, set(codes[unusable])


def replace_codes(frame: pd.DataFrame, refetched: pd.DataFrame, codes: set[str], trade_date: str) -> pd.DataFrame:
    """Swap the rows of ``codes`` for their by-code refetch on ``trade_date``."""
    keep = frame[~frame["ts_code"].astype(str).isin(codes)]
    fresh = refetched[(refetched["trade_date"].astype(str) == trade_date)
                      & refetched["ts_code"].astype(str).isin(codes)]
    merged = pd.concat([keep, fresh.reindex(columns=frame.columns)], ignore_index=True)
    return merged.sort_values("ts_code").reset_index(drop=True)
