#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 - Build active universe snapshots.

规则：
1. 剔除 ST
2. 剔除停牌
3. 剔除上市未满一年（默认 252 天）
4. 保留 60 日日均成交额位于当日全市场前 50% 的活跃股票

输出：
- data/universe/universe_YYYYMMDD.parquet
- outputs/factor_registry/universe_summary.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


@dataclass
class RuntimeConfig:
    root: Path
    workspace: Path
    stock_data_root: Path
    start_date: str
    end_date: str
    daily_cache_dir: Path
    pit_universe_dir: Path
    universe_dir: Path
    summary_path: Path
    avg_amount_lookback_days: int
    avg_amount_percentile_threshold: float
    minimum_listing_days: int
    min_price: float
    min_daily_trading_days_ratio: float
    exclude_st: bool
    exclude_suspended: bool
    st_name_patterns: list[str]
    suspended_fields_priority: list[str]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_runtime_config(root: Path) -> RuntimeConfig:
    phase1 = load_yaml(root / "config" / "phase1.yaml")
    universe_rules = load_yaml(root / "config" / "universe_rules.yaml")

    paths = phase1["paths"]
    filters = universe_rules["universe"]["filters"]
    outputs = universe_rules["universe"]["outputs"]

    workspace = Path(paths["workspace"]).expanduser()
    stock_data_root = Path(paths["stock_data_root"]).expanduser()
    return RuntimeConfig(
        root=root,
        workspace=workspace,
        stock_data_root=stock_data_root,
        start_date=str(phase1["phase1"]["start_date"]).replace("-", ""),
        end_date=str(phase1["phase1"]["end_date"]).replace("-", ""),
        daily_cache_dir=stock_data_root / "03-working" / "backtest_cache",
        pit_universe_dir=stock_data_root / "03-working" / "fundamental_cache" / "pit_market",
        universe_dir=root / "data" / "universe",
        summary_path=root / "outputs" / "factor_registry" / outputs["summary_file"],
        avg_amount_lookback_days=int(filters["avg_amount_lookback_days"]),
        avg_amount_percentile_threshold=float(filters["avg_amount_percentile_threshold"]),
        minimum_listing_days=int(filters["minimum_listing_days"]),
        min_price=float(filters["min_price"]),
        min_daily_trading_days_ratio=float(filters["min_daily_trading_days_ratio"]),
        exclude_st=bool(filters["exclude_st"]),
        exclude_suspended=bool(filters["exclude_suspended"]),
        st_name_patterns=list(universe_rules["universe"]["st_name_patterns"]),
        suspended_fields_priority=list(universe_rules["universe"]["suspended_fields_priority"]),
    )


def list_daily_files(daily_cache_dir: Path, start_date: str, end_date: str) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for path in sorted(daily_cache_dir.glob("daily_*.parquet")):
        trade_date = path.stem.split("_")[-1]
        if start_date <= trade_date <= end_date:
            out.append((trade_date, path))
    return out


class UniverseDataIntegrityError(RuntimeError):
    pass


def load_pit_universe_panel(pit_universe_dir: Path, dates: list[str]) -> pd.DataFrame:
    required = {
        "trade_date",
        "ts_code",
        "name",
        "list_status_at_date",
        "is_st",
        "is_suspended",
        "listing_days",
        "universe_flag",
        "tradable",
        "used_proxy",
        "completeness",
    }
    frames: list[pd.DataFrame] = []
    for trade_date in dates:
        path = pit_universe_dir / f"universe_{trade_date}.parquet"
        if not path.exists():
            raise UniverseDataIntegrityError(
                f"missing immutable PIT universe snapshot for {trade_date}: {path}"
            )
        frame = pd.read_parquet(path)
        missing = sorted(required - set(frame.columns))
        if missing:
            raise UniverseDataIntegrityError(
                f"PIT universe {trade_date} missing fields: {missing}"
            )
        payload_dates = set(frame["trade_date"].astype(str).str.replace("-", "", regex=False))
        if payload_dates != {trade_date}:
            raise UniverseDataIntegrityError(
                f"PIT universe payload date mismatch for {trade_date}: {sorted(payload_dates)}"
            )
        if frame["used_proxy"].fillna(True).astype(bool).any():
            raise UniverseDataIntegrityError(f"PIT universe {trade_date} contains proxy rows")
        if not (frame["completeness"].astype(str) == "complete").all():
            raise UniverseDataIntegrityError(f"PIT universe {trade_date} is incomplete")
        if frame["ts_code"].astype(str).duplicated().any():
            raise UniverseDataIntegrityError(f"PIT universe {trade_date} contains duplicate securities")
        frames.append(frame[list(required)].copy())
    if not frames:
        raise UniverseDataIntegrityError("no PIT universe dates were requested")
    out = pd.concat(frames, ignore_index=True)
    out["ts_code"] = out["ts_code"].astype(str)
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str).str.replace("-", "", regex=False),
        format="%Y%m%d",
        errors="raise",
    )
    return out


def load_all_daily_panel(file_pairs: list[tuple[str, Path]]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for trade_date, path in file_pairs:
        df = pd.read_parquet(path)
        keep_cols = [c for c in ["ts_code", "trade_date", "close", "vol", "amount"] if c in df.columns]
        chunk = df[keep_cols].copy()
        chunk["ts_code"] = chunk["ts_code"].astype(str)
        chunk["trade_date"] = pd.to_datetime(chunk["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
        chunk["amount"] = pd.to_numeric(chunk.get("amount", 0.0), errors="coerce").fillna(0.0)
        chunk["vol"] = pd.to_numeric(chunk.get("vol", 0.0), errors="coerce").fillna(0.0)
        chunk["close"] = pd.to_numeric(chunk.get("close", np.nan), errors="coerce")
        frames.append(chunk)
    if not frames:
        raise FileNotFoundError("No daily parquet files found in configured window.")
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return panel


def mark_st_flags(df: pd.DataFrame, patterns: list[str]) -> pd.Series:
    if not patterns:
        return pd.Series(False, index=df.index)
    escaped = [p.replace("*", ".*") for p in patterns]
    regex = "|".join(escaped)
    return df["name"].fillna("").str.contains(regex, regex=True)


def compute_universe_snapshots(panel: pd.DataFrame, pit_universe: pd.DataFrame, cfg: RuntimeConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    df = panel.merge(
        pit_universe,
        on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    if df["list_status_at_date"].isna().any():
        missing = int(df["list_status_at_date"].isna().sum())
        raise UniverseDataIntegrityError(
            f"daily panel has {missing} rows without exact-date PIT universe evidence"
        )
    df["is_listed_enough"] = (
        (df["list_status_at_date"].astype(str) == "L")
        & (pd.to_numeric(df["listing_days"], errors="coerce").fillna(-1) >= cfg.minimum_listing_days)
    )
    reported_suspended = df["is_suspended"].fillna(True).astype(bool)
    market_no_trade = (df["amount"] <= 0) | (df["vol"] <= 0)
    if (reported_suspended != market_no_trade).any():
        raise UniverseDataIntegrityError("PIT suspension state conflicts with exact-date market data")
    df["is_suspended"] = reported_suspended
    df["is_tradeable_price"] = df["close"].fillna(0) >= cfg.min_price
    df["is_trading_day"] = ((df["amount"] > 0) & (df["vol"] > 0)).astype(int)

    lookback = cfg.avg_amount_lookback_days
    by_stock = df.groupby("ts_code", group_keys=False)
    df["avg_amount_lookback"] = by_stock["amount"].transform(
        lambda s: s.rolling(lookback, min_periods=min(20, lookback)).mean()
    )
    df["trading_ratio_lookback"] = by_stock["is_trading_day"].transform(
        lambda s: s.rolling(lookback, min_periods=min(20, lookback)).mean()
    )

    records: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for trade_date, day in df.groupby("trade_date", sort=True):
        day = day.copy()
        trade_date_str = trade_date.strftime("%Y%m%d")
        prefilter = day[
            day["is_listed_enough"]
            & day["is_tradeable_price"]
            & (pd.to_numeric(day["universe_flag"], errors="coerce") > 0)
            & (pd.to_numeric(day["tradable"], errors="coerce") > 0)
            & (~day["is_st"] if cfg.exclude_st else True)
            & (~day["is_suspended"] if cfg.exclude_suspended else True)
            & (day["trading_ratio_lookback"].fillna(0) >= cfg.min_daily_trading_days_ratio)
        ].copy()
        if prefilter.empty:
            continue

        threshold = prefilter["avg_amount_lookback"].quantile(cfg.avg_amount_percentile_threshold)
        selected = prefilter[prefilter["avg_amount_lookback"] >= threshold].copy()
        selected["trade_date"] = trade_date_str
        selected["liquidity_threshold"] = float(threshold)
        selected["universe_flag"] = 1
        output_cols = [
            "trade_date",
            "ts_code",
            "name",
            "close",
            "amount",
            "avg_amount_lookback",
            "trading_ratio_lookback",
            "listing_days",
            "liquidity_threshold",
            "universe_flag",
            "tradable",
            "list_status_at_date",
            "is_st",
            "is_suspended",
            "used_proxy",
            "completeness",
        ]
        output = selected[output_cols].sort_values("ts_code").reset_index(drop=True)
        out_path = cfg.universe_dir / f"universe_{trade_date_str}.parquet"
        output.to_parquet(out_path, index=False)

        records.append(
            {
                "trade_date": trade_date_str,
                "path": str(out_path),
                "count": int(len(output)),
                "liquidity_threshold": float(threshold),
            }
        )
        summary_rows.append(
            {
                "trade_date": trade_date_str,
                "total": int(len(day)),
                "prefilter": int(len(prefilter)),
                "selected": int(len(output)),
                "median_amount_lookback": float(day["avg_amount_lookback"].median(skipna=True) or 0.0),
                "selection_ratio": float(len(output) / len(day)) if len(day) else 0.0,
            }
        )

    summary = {
        "window": {
            "start_date": cfg.start_date,
            "end_date": cfg.end_date,
            "lookback_days": cfg.avg_amount_lookback_days,
        },
        "rules": {
            "exclude_st": cfg.exclude_st,
            "exclude_suspended": cfg.exclude_suspended,
            "minimum_listing_days": cfg.minimum_listing_days,
            "avg_amount_percentile_threshold": cfg.avg_amount_percentile_threshold,
            "min_price": cfg.min_price,
            "min_daily_trading_days_ratio": cfg.min_daily_trading_days_ratio,
            "historical_universe_source": "immutable_exact_date_pit_market_snapshot",
            "current_stock_list_fallback_forbidden": True,
            "proxy_rows_allowed": False,
        },
        "snapshots": records,
        "daily_summary": summary_rows,
        "dates_covered": len(records),
    }
    return records, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build phase1 active universe snapshots")
    parser.add_argument(
        "--root",
        default=str(Path.home() / ".openclaw/workspace/factor_factory"),
        help="factor_factory root",
    )
    args = parser.parse_args()

    cfg = load_runtime_config(Path(args.root).expanduser().resolve())
    cfg.universe_dir.mkdir(parents=True, exist_ok=True)
    cfg.summary_path.parent.mkdir(parents=True, exist_ok=True)

    file_pairs = list_daily_files(cfg.daily_cache_dir, cfg.start_date, cfg.end_date)
    panel = load_all_daily_panel(file_pairs)
    dates = [trade_date for trade_date, _ in file_pairs]
    pit_universe = load_pit_universe_panel(cfg.pit_universe_dir, dates)
    records, summary = compute_universe_snapshots(panel, pit_universe, cfg)

    cfg.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "dates_covered": len(records),
        "summary_path": str(cfg.summary_path),
        "first_snapshot": records[0] if records else None,
        "last_snapshot": records[-1] if records else None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
