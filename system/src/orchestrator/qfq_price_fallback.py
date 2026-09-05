#!/usr/bin/env python3
"""Fill price gaps only when official adjustment evidence proves their qfq basis."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

QFQ_COLUMNS = ["trade_date", "ts_code", "open_qfq", "close_qfq"]
KEYS = ["trade_date", "ts_code"]


def _official_qfq(frame: pd.DataFrame, *, require_provider: bool = False) -> pd.DataFrame:
    # stk_factor cache files are endpoint evidence, but can also contain an
    # explicitly marked local technical fallback. Its raw prices are not qfq.
    if "adj_factor" not in frame.columns:
        return frame.iloc[:0].copy()
    factor = pd.to_numeric(frame.adj_factor, errors="coerce")
    trusted = np.isfinite(factor) & (factor > 0)
    if "source_provider" in frame.columns:
        provider = frame.source_provider.fillna("").astype(str)
        allowed = {"tushare", "tushare_stk_factor", "tushare_pro"}
        if not require_provider:
            allowed.add("")  # Legacy official endpoint caches predate the label.
        trusted &= provider.isin(allowed)
    elif require_provider:
        trusted &= False
    if "used_proxy" in frame.columns:
        trusted &= ~frame.used_proxy.fillna(False).astype(bool)
    return frame.loc[trusted].copy()


def _anchors(stk: pd.DataFrame) -> pd.DataFrame:
    if not {"close", "close_qfq", "adj_factor"}.issubset(stk.columns):
        return pd.DataFrame(columns=["ts_code", "qfq_scale"])
    anchor = stk.copy()
    for col in ("close", "close_qfq", "adj_factor"):
        anchor[col] = pd.to_numeric(anchor[col], errors="coerce")
    anchor["qfq_scale"] = anchor.close_qfq / (anchor.close * anchor.adj_factor)
    anchor = anchor[np.isfinite(anchor.qfq_scale) & (anchor.qfq_scale > 0)]
    return anchor.sort_values("trade_date").drop_duplicates("ts_code", keep="last")[["ts_code", "qfq_scale"]]


def merge_qfq_with_daily_fallback(stk: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    stk = stk.copy() if stk is not None and not stk.empty else pd.DataFrame(columns=QFQ_COLUMNS)
    for key in KEYS:
        stk[key] = stk[key].astype(str)
    if stk.duplicated(KEYS).any():
        raise ValueError("duplicate official qfq price keys")
    stk = _official_qfq(stk)
    official = stk[QFQ_COLUMNS].copy()
    official["price_source"] = "official_stk_factor_qfq"
    if daily is None or daily.empty:
        return official.reset_index(drop=True)
    daily = daily.copy()
    for key in KEYS:
        daily[key] = daily[key].astype(str)
    if daily.duplicated(KEYS).any():
        raise ValueError("duplicate daily price keys")
    missing = daily.merge(stk[KEYS], on=KEYS, how="left", indicator=True)
    missing = missing[missing._merge == "left_only"].drop(columns="_merge")
    if {"open_qfq", "close_qfq"}.issubset(missing.columns):
        fill = _official_qfq(missing, require_provider=True)[QFQ_COLUMNS].copy()
        fill["price_source"] = "official_daily_qfq"
    elif {"open", "close", "adj_factor"}.issubset(missing.columns):
        fill = missing.merge(_anchors(stk), on="ts_code", how="inner")
        factor = pd.to_numeric(fill.adj_factor, errors="coerce")
        factor = factor.where(np.isfinite(factor) & (factor > 0))
        for raw in ("open", "close"):
            fill[f"{raw}_qfq"] = pd.to_numeric(fill[raw], errors="coerce") * factor * fill.qfq_scale
        fill = fill[QFQ_COLUMNS].copy()
        fill["price_source"] = "official_daily_adj_factor_anchored_qfq"
    else:
        return official.reset_index(drop=True)
    for col in ("open_qfq", "close_qfq"):
        fill[col] = pd.to_numeric(fill[col], errors="coerce")
        fill = fill[np.isfinite(fill[col]) & (fill[col] > 0)]
    return pd.concat([official, fill], ignore_index=True)


def load_cached_qfq_prices(cache_dir: Path, client: Any, *, minimum_fallback_date: str) -> pd.DataFrame:
    """Cache official factors for recent gaps; never infer that a raw bar is adjusted."""
    frames = [pd.read_parquet(path) for path in sorted(cache_dir.glob("stk_factor_*.parquet"))]
    stk = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=QFQ_COLUMNS)
    stk = _official_qfq(stk)
    frames = [pd.read_parquet(path) for path in sorted(cache_dir.glob("daily_*.parquet"))]
    daily = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if daily.empty or {"open_qfq", "close_qfq"}.issubset(daily.columns):
        return merge_qfq_with_daily_fallback(stk, daily)
    for frame in (stk, daily):
        for key in KEYS:
            frame[key] = frame[key].astype(str)
    gaps = daily.merge(stk[KEYS], on=KEYS, how="left", indicator=True)
    gaps = gaps[(gaps._merge == "left_only") & (gaps.trade_date >= minimum_fallback_date)]
    gaps = gaps[gaps.ts_code.isin(_anchors(stk).ts_code)]
    factors = []
    for trade_date in sorted(gaps.trade_date.unique()):
        path = cache_dir / f"adj_factor_{trade_date}.parquet"
        frame = pd.read_parquet(path) if path.exists() else client.adj_factor(trade_date=trade_date)
        if frame is None or frame.empty or not {*KEYS, "adj_factor"}.issubset(frame.columns):
            raise ValueError(f"missing official adjustment factors for {trade_date}")
        frame = frame[[*KEYS, "adj_factor"]].copy()
        for key in KEYS:
            frame[key] = frame[key].astype(str)
        frame.adj_factor = pd.to_numeric(frame.adj_factor, errors="coerce")
        if (set(frame.trade_date) != {trade_date} or frame.duplicated(KEYS).any()
                or not (np.isfinite(frame.adj_factor) & (frame.adj_factor > 0)).all()):
            raise ValueError(f"invalid official adjustment factors for {trade_date}")
        expected = set(gaps.loc[gaps.trade_date == trade_date, "ts_code"])
        if not expected.issubset(set(frame.ts_code)):
            raise ValueError(f"incomplete official adjustment factors for {trade_date}")
        if not path.exists():
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=cache_dir)
            os.close(fd)
            try:
                frame.to_parquet(temporary, index=False)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        factors.append(frame)
    if factors:
        daily = daily.drop(columns="adj_factor", errors="ignore").merge(pd.concat(factors), on=KEYS, how="left")
    # Historical raw rows outside the frozen observation window are not required.
    return merge_qfq_with_daily_fallback(stk, daily)
