#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 - Build factor panel.

职责：
1. 合并 daily / stk_factor / cyq_perf 与活跃股票池
2. 生成首批候选辅助因子（30~80 个方向中的首批实现）
3. 将 `prebreakout_v41` 作为黑盒因子暴露列并入面板
4. 生成 close-to-close / open-to-close 双标签供审计脚本使用

输出：
- data/factors/panel_phase1.parquet
- data/prepared/base_factor_exposure.parquet
- outputs/candidate_lists/phase1_candidate_catalog.json
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


@dataclass
class RuntimeConfig:
    root: Path
    stock_data_root: Path
    start_date: str
    end_date: str
    cache_dir: Path
    universe_dir: Path
    selection_history_dir: Path
    panel_out: Path
    base_out: Path
    catalog_out: Path
    base_factor_id: str


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_runtime_config(root: Path) -> RuntimeConfig:
    phase1 = load_yaml(root / "config" / "phase1.yaml")
    paths = phase1["paths"]
    return RuntimeConfig(
        root=root,
        stock_data_root=Path(paths["stock_data_root"]).expanduser(),
        start_date=str(phase1["phase1"]["start_date"]).replace("-", ""),
        end_date=str(phase1["phase1"]["end_date"]).replace("-", ""),
        cache_dir=Path(paths["stock_data_root"]).expanduser() / "03-working" / "backtest_cache",
        universe_dir=root / "data" / "universe",
        selection_history_dir=Path(paths["stock_data_root"]).expanduser() / "03-working" / "selection_history",
        panel_out=root / "data" / "factors" / "panel_phase1.parquet",
        base_out=root / "data" / "prepared" / "base_factor_exposure.parquet",
        catalog_out=root / "outputs" / "candidate_lists" / "phase1_candidate_catalog.json",
        base_factor_id=str(phase1["base_factor"]["id"]),
    )


def available_dates(cfg: RuntimeConfig) -> list[str]:
    daily_dates = {p.stem.split("_")[-1] for p in cfg.cache_dir.glob("daily_*.parquet")}
    stk_dates = {p.stem.split("_")[-1] for p in cfg.cache_dir.glob("stk_factor_*.parquet")}
    universe_dates = {p.stem.split("_")[-1] for p in cfg.universe_dir.glob("universe_*.parquet")}
    common = sorted(daily_dates & stk_dates & universe_dates)
    return [d for d in common if cfg.start_date <= d <= cfg.end_date]


def load_universe(cfg: RuntimeConfig, dates: list[str]) -> pd.DataFrame:
    frames = []
    keep_cols = [
        "trade_date",
        "ts_code",
        "name",
        "avg_amount_lookback",
        "trading_ratio_lookback",
        "listing_days",
        "liquidity_threshold",
        "universe_flag",
    ]
    for d in dates:
        path = cfg.universe_dir / f"universe_{d}.parquet"
        df = pd.read_parquet(path)
        use_cols = [c for c in keep_cols if c in df.columns]
        frames.append(df[use_cols].copy())
    universe = pd.concat(frames, ignore_index=True)
    universe["ts_code"] = universe["ts_code"].astype(str)
    universe["trade_date"] = universe["trade_date"].astype(str)
    return universe


def load_cache_frame(path: Path, columns: list[str] | None = None, trade_date_hint: str | None = None) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if columns is not None:
        keep = [c for c in columns if c in df.columns]
        df = df[keep].copy()
    if "trade_date" not in df.columns and trade_date_hint is not None:
        df["trade_date"] = str(trade_date_hint)
    if "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].astype(str)
    if "trade_date" in df.columns:
        df["trade_date"] = df["trade_date"].astype(str)
    return df


def load_terminal_next_daily_prices(cache_dir: Path, trade_date: str) -> pd.DataFrame:
    stk_dates = sorted({p.stem.split("_")[-1] for p in cache_dir.glob("stk_factor_*.parquet")})
    idx = bisect_right(stk_dates, str(trade_date))
    if idx >= len(stk_dates):
        return pd.DataFrame(columns=["ts_code", "terminal_next_open", "terminal_next_close"])
    next_date = stk_dates[idx]
    daily = load_cache_frame(
        cache_dir / f"stk_factor_{next_date}.parquet",
        ["ts_code", "open_qfq", "close_qfq"],
        trade_date_hint=next_date,
    )
    if daily.empty or not {"open_qfq", "close_qfq"}.issubset(daily.columns):
        return pd.DataFrame(columns=["ts_code", "terminal_next_open", "terminal_next_close"])
    return daily.rename(columns={"open_qfq": "terminal_next_open", "close_qfq": "terminal_next_close"})[
        ["ts_code", "terminal_next_open", "terminal_next_close"]
    ]


def coerce_float(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def load_market_panel(cfg: RuntimeConfig, dates: list[str]) -> pd.DataFrame:
    frames = []
    for d in dates:
        daily = load_cache_frame(
            cfg.cache_dir / f"daily_{d}.parquet",
            ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"],
            trade_date_hint=d,
        )
        stk = load_cache_frame(
            cfg.cache_dir / f"stk_factor_{d}.parquet",
            [
                "ts_code", "trade_date", "open_qfq", "high_qfq", "low_qfq", "close_qfq", "pre_close_qfq",
                "macd_dif", "macd_dea", "macd", "kdj_k", "kdj_d", "kdj_j",
                "rsi_6", "rsi_12", "rsi_24", "boll_upper", "boll_mid", "boll_lower", "cci"
                , "source_provider", "used_proxy", "completeness"
            ],
            trade_date_hint=d,
        )
        if "source_provider" in stk.columns:
            stk = stk.rename(columns={"source_provider": "price_source_provider"})
        required_qfq = {"open_qfq", "high_qfq", "low_qfq", "close_qfq", "pre_close_qfq"}
        if not required_qfq.issubset(stk.columns):
            raise ValueError(f"stk_factor {d} is missing required qfq prices: {sorted(required_qfq - set(stk.columns))}")
        merged = daily.merge(stk, on=["ts_code", "trade_date"], how="left")
        qfq_values = merged[sorted(required_qfq)].apply(pd.to_numeric, errors="coerce")
        if qfq_values.isna().any().any() or (qfq_values <= 0.0).any().any():
            raise ValueError(f"stk_factor {d} contains missing or invalid qfq prices")
        for price_column in ("open", "high", "low", "close", "pre_close"):
            merged[f"{price_column}_unadjusted"] = merged[price_column]
            merged[price_column] = qfq_values[f"{price_column}_qfq"]
        merged["price_basis"] = "qfq"

        cyq_path = cfg.cache_dir / f"cyq_perf_{d}.parquet"
        merged["chip_data_status"] = "unavailable"
        if cyq_path.exists():
            cyq = load_cache_frame(cyq_path, None, trade_date_hint=d)
            provenance_columns = [
                column
                for column in ("source_provider", "fallback_source", "source", "provenance")
                if column in cyq.columns
            ]
            proxy_provenance = any(
                cyq[column].astype(str).str.contains("proxy", case=False, na=False).any()
                for column in provenance_columns
            )
            proxy_flag = "used_proxy" in cyq.columns and cyq["used_proxy"].fillna(True).astype(bool).any()
            complete = "completeness" not in cyq.columns or (cyq["completeness"].astype(str) == "complete").all()
            if proxy_provenance or proxy_flag:
                merged["chip_data_status"] = "proxy_rejected"
            elif complete:
                chip_columns = [
                    column
                    for column in (
                        "ts_code", "trade_date", "cost_5pct", "cost_15pct", "cost_50pct",
                        "cost_85pct", "cost_95pct", "weight_avg", "winner_rate"
                    )
                    if column in cyq.columns
                ]
                cyq_values = cyq[chip_columns].copy()
                merged = merged.drop(columns=["chip_data_status"]).merge(
                    cyq_values, on=["ts_code", "trade_date"], how="left"
                )
                merged["chip_data_status"] = "verified_non_proxy"
        frames.append(merged)
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return panel


def load_base_exposure(cfg: RuntimeConfig, dates: list[str]) -> pd.DataFrame:
    rows = []
    for d in dates:
        path = cfg.selection_history_dir / f"{d}.json"
        if not path.exists():
            continue
        obj = json.loads(path.read_text(encoding="utf-8"))
        stocks = obj.get("stocks") or []
        if not stocks:
            continue
        prepared_rows: list[dict[str, Any]] = []
        for idx, item in enumerate(stocks, start=1):
            ts_code = str(item.get("ts_code") or item.get("code") or "")
            if not ts_code:
                continue
            rank_raw = item.get("rank") or item.get("rank_no") or idx
            try:
                rank = int(rank_raw)
            except (TypeError, ValueError):
                rank = idx
            file_score_source = str(((obj.get("source") or {}).get("score_source") or "")).strip()
            item_score_source = str(item.get("score_source") or file_score_source or "").strip()
            score = coerce_float(item.get("score"))
            score_source = item_score_source or "original_score"
            if not np.isfinite(score):
                score = float(max(1, len(stocks) - rank + 1))
                score_source = "rank_monotonic_proxy"
            prepared_rows.append(
                {
                    "trade_date": d,
                    "ts_code": ts_code,
                    "base_factor_id": cfg.base_factor_id,
                    "base_factor_score": score,
                    "base_factor_rank": rank,
                    "base_factor_selected": 1,
                    "benchmark_eligible": 1,
                    "base_factor_score_source": score_source,
                }
            )
        if not prepared_rows:
            continue
        max_score = max(float(item["base_factor_score"]) for item in prepared_rows) or 1.0
        for item in prepared_rows:
            item["base_factor_score_norm"] = item["base_factor_score"] / max_score if max_score else 0.0
            rows.append(item)
    if not rows:
        return pd.DataFrame(
            columns=[
                "trade_date", "ts_code", "base_factor_id", "base_factor_score",
                "base_factor_score_norm", "base_factor_rank", "base_factor_selected", "benchmark_eligible",
                "base_factor_score_source",
            ]
        )
    return pd.DataFrame(rows)


def add_benchmark_eligibility(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.copy()
    if "base_record_present" in out.columns:
        row_mask = out["base_record_present"].fillna(0).astype(int)
    else:
        row_mask = out.get("base_factor_selected", 0)
        if not isinstance(row_mask, pd.Series):
            row_mask = pd.Series(row_mask, index=out.index)
        row_mask = row_mask.fillna(0).astype(int)
    out["benchmark_eligible"] = row_mask
    out["benchmark_date_eligible"] = (
        out.groupby("trade_date", group_keys=False)["benchmark_eligible"].transform("max").fillna(0).astype(int)
    )
    return out


def rolling_group(df: pd.DataFrame, col: str, win: int, func: str, min_periods: int | None = None) -> pd.Series:
    min_p = min_periods or min(win, 5)
    grp = df.groupby("ts_code", group_keys=False)[col]
    if func == "mean":
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).mean())
    if func == "std":
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).std())
    if func == "min":
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).min())
    if func == "max":
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).max())
    if func == "skew":
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).skew())
    if func == "kurt":
        return grp.transform(lambda s: s.rolling(win, min_periods=min_p).kurt())
    raise ValueError(func)


def shift_group(df: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return df.groupby("ts_code", group_keys=False)[col].shift(periods)


def pct_change_group(df: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return df.groupby("ts_code", group_keys=False)[col].pct_change(periods)


def add_candidate_factors(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    out = df.copy()
    out["ret_1d"] = pd.to_numeric(out["pct_chg"], errors="coerce") / 100.0
    out["ret_5d"] = pct_change_group(out, "close", 5)
    out["ret_10d"] = pct_change_group(out, "close", 10)
    out["ret_20d"] = pct_change_group(out, "close", 20)

    out["ma_5"] = rolling_group(out, "close", 5, "mean")
    out["ma_10"] = rolling_group(out, "close", 10, "mean")
    out["ma_20"] = rolling_group(out, "close", 20, "mean")
    out["vol_ma_5"] = rolling_group(out, "vol", 5, "mean")
    out["vol_ma_20"] = rolling_group(out, "vol", 20, "mean")
    out["amount_ma_20"] = rolling_group(out, "amount", 20, "mean")
    out["amount_std_20"] = rolling_group(out, "amount", 20, "std")
    out["vol_std_20"] = rolling_group(out, "vol", 20, "std")
    out["ret_std_10"] = rolling_group(out, "ret_1d", 10, "std")
    out["ret_std_20"] = rolling_group(out, "ret_1d", 20, "std")
    out["ret_skew_20"] = rolling_group(out, "ret_1d", 20, "skew")
    out["ret_kurt_20"] = rolling_group(out, "ret_1d", 20, "kurt")
    out["vol_skew_20"] = rolling_group(out, "vol", 20, "skew")
    out["amount_skew_20"] = rolling_group(out, "amount", 20, "skew")
    out["close_min_20"] = rolling_group(out, "close", 20, "min")
    out["close_max_20"] = rolling_group(out, "close", 20, "max")
    out["hl_range"] = (pd.to_numeric(out["high"], errors="coerce") - pd.to_numeric(out["low"], errors="coerce")) / pd.to_numeric(out["close"], errors="coerce")
    out["atr_proxy_14"] = rolling_group(out, "hl_range", 14, "mean")
    out["boll_width"] = (pd.to_numeric(out["boll_upper"], errors="coerce") - pd.to_numeric(out["boll_lower"], errors="coerce")) / pd.to_numeric(out["boll_mid"], errors="coerce")

    factor_defs: list[dict[str, Any]] = []
    def reg(name: str, family: str, formula: str, phenomenon: str | None = None, mechanism: str | None = None) -> None:
        factor_defs.append({
            "id": name,
            "family": family,
            "formula": formula,
            "economic_rationale": {
                "phenomenon": phenomenon or family,
                "mechanism": mechanism or "待后续研究补充；当前作为经验 alpha 候选，不得仅凭统计显著直接晋升。",
                "evidence": "内部 walk-forward / bootstrap / OOS 审计；外部证据待补充",
                "risk_premium_vs_alpha": "empirical_alpha_unexplained",
            },
        })

    # A. price_volume_momentum
    out["f_ret_5_slope"] = out["ret_5d"]
    reg("f_ret_5_slope", "price_volume_momentum", "close.pct_change(5)")
    out["f_ret_10_slope"] = out["ret_10d"]
    reg("f_ret_10_slope", "price_volume_momentum", "close.pct_change(10)")
    out["f_ret_20_slope"] = out["ret_20d"]
    reg("f_ret_20_slope", "price_volume_momentum", "close.pct_change(20)")
    out["f_momentum_accel_5_20"] = out["ret_5d"] - out["ret_20d"]
    reg("f_momentum_accel_5_20", "price_volume_momentum", "ret_5d - ret_20d")
    out["f_momentum_second_derivative_10"] = out["ret_1d"] - 2 * shift_group(out, "ret_1d", 1) + shift_group(out, "ret_1d", 2)
    reg("f_momentum_second_derivative_10", "price_volume_momentum", "ret_t - 2*ret_t-1 + ret_t-2")
    out["f_price_vs_ma5"] = pd.to_numeric(out["close"], errors="coerce") / out["ma_5"] - 1.0
    reg("f_price_vs_ma5", "price_volume_momentum", "close / MA5 - 1")
    out["f_price_vs_ma10"] = pd.to_numeric(out["close"], errors="coerce") / out["ma_10"] - 1.0
    reg("f_price_vs_ma10", "price_volume_momentum", "close / MA10 - 1")
    out["f_price_vs_ma20"] = pd.to_numeric(out["close"], errors="coerce") / out["ma_20"] - 1.0
    reg("f_price_vs_ma20", "price_volume_momentum", "close / MA20 - 1")
    out["f_ma5_ma20_gap"] = out["ma_5"] / out["ma_20"] - 1.0
    reg("f_ma5_ma20_gap", "price_volume_momentum", "MA5 / MA20 - 1")
    out["f_close_pos_20"] = (pd.to_numeric(out["close"], errors="coerce") - out["close_min_20"]) / (out["close_max_20"] - out["close_min_20"] + 1e-9)
    reg("f_close_pos_20", "price_volume_momentum", "(close-min20)/(max20-min20)")
    out["f_up_day_ratio_10"] = out.groupby("ts_code", group_keys=False)["ret_1d"].transform(lambda s: s.gt(0).rolling(10, min_periods=5).mean())
    reg("f_up_day_ratio_10", "price_volume_momentum", "rolling_mean(ret_1d > 0, 10)")
    out["f_gap_return"] = pd.to_numeric(out["open"], errors="coerce") / pd.to_numeric(out["pre_close"], errors="coerce") - 1.0
    reg("f_gap_return", "price_volume_momentum", "open / pre_close - 1")
    out["f_intraday_reversal"] = pd.to_numeric(out["close"], errors="coerce") / pd.to_numeric(out["open"], errors="coerce") - 1.0
    reg("f_intraday_reversal", "price_volume_momentum", "close / open - 1")

    # B. volatility_compression
    out["f_realized_vol_10"] = out["ret_std_10"]
    reg("f_realized_vol_10", "volatility_compression", "rolling_std(ret_1d, 10)")
    out["f_realized_vol_20"] = out["ret_std_20"]
    reg("f_realized_vol_20", "volatility_compression", "rolling_std(ret_1d, 20)")
    out["f_vol_compression_10_20"] = out["ret_std_10"] / (out["ret_std_20"] + 1e-9) - 1.0
    reg("f_vol_compression_10_20", "volatility_compression", "vol10 / vol20 - 1")
    out["f_atr_proxy_14"] = out["atr_proxy_14"]
    reg("f_atr_proxy_14", "volatility_compression", "rolling_mean((high-low)/close, 14)")
    out["f_atr_compression_5_20"] = rolling_group(out, "hl_range", 5, "mean") / (rolling_group(out, "hl_range", 20, "mean") + 1e-9) - 1.0
    reg("f_atr_compression_5_20", "volatility_compression", "atr5 / atr20 - 1")
    out["f_boll_width"] = out["boll_width"]
    reg("f_boll_width", "volatility_compression", "(boll_upper - boll_lower) / boll_mid")
    out["f_boll_width_delta_5"] = out["boll_width"] - shift_group(out, "boll_width", 5)
    reg("f_boll_width_delta_5", "volatility_compression", "boll_width - lag5")
    out["f_range_stability_10"] = rolling_group(out, "hl_range", 10, "std")
    reg("f_range_stability_10", "volatility_compression", "rolling_std(hl_range, 10)")

    # C. liquidity_turnover_distribution
    out["f_volume_ratio_5"] = pd.to_numeric(out["vol"], errors="coerce") / (out["vol_ma_5"] + 1e-9)
    reg("f_volume_ratio_5", "liquidity_turnover_distribution", "vol / vol_ma_5")
    out["f_volume_ratio_20"] = pd.to_numeric(out["vol"], errors="coerce") / (out["vol_ma_20"] + 1e-9)
    reg("f_volume_ratio_20", "liquidity_turnover_distribution", "vol / vol_ma_20")
    out["f_amount_ratio_20"] = pd.to_numeric(out["amount"], errors="coerce") / (out["amount_ma_20"] + 1e-9)
    reg("f_amount_ratio_20", "liquidity_turnover_distribution", "amount / amount_ma_20")
    out["f_volume_cv_20"] = out["vol_std_20"] / (out["vol_ma_20"] + 1e-9)
    reg("f_volume_cv_20", "liquidity_turnover_distribution", "vol_std_20 / vol_ma_20")
    out["f_amount_cv_20"] = out["amount_std_20"] / (out["amount_ma_20"] + 1e-9)
    reg("f_amount_cv_20", "liquidity_turnover_distribution", "amount_std_20 / amount_ma_20")
    out["f_volume_skew_20"] = out["vol_skew_20"]
    reg("f_volume_skew_20", "liquidity_turnover_distribution", "rolling_skew(vol, 20)")
    out["f_amount_skew_20"] = out["amount_skew_20"]
    reg("f_amount_skew_20", "liquidity_turnover_distribution", "rolling_skew(amount, 20)")
    out["f_amihud_20"] = out.groupby("ts_code", group_keys=False).apply(lambda g: (g["ret_1d"].abs() / (g["amount"].replace(0, np.nan))).rolling(20, min_periods=5).mean()).reset_index(level=0, drop=True)
    reg("f_amihud_20", "liquidity_turnover_distribution", "rolling_mean(abs(ret_1d)/amount, 20)")
    out["f_amount_zscore_20"] = (pd.to_numeric(out["amount"], errors="coerce") - out["amount_ma_20"]) / (out["amount_std_20"] + 1e-9)
    reg("f_amount_zscore_20", "liquidity_turnover_distribution", "(amount - mean20) / std20")

    # D. chip_structure_delta. Proxy-generated chip rows are removed in
    # load_market_panel and therefore can never create these factors.
    verified_chip_columns = {"cost_15pct", "cost_50pct", "cost_85pct", "weight_avg", "winner_rate"}
    if verified_chip_columns.issubset(out.columns):
        out["f_chip_band_width"] = (pd.to_numeric(out["cost_85pct"], errors="coerce") - pd.to_numeric(out["cost_15pct"], errors="coerce")) / pd.to_numeric(out["close"], errors="coerce")
        reg("f_chip_band_width", "chip_structure_delta", "(cost_85pct - cost_15pct) / close")
        out["f_chip_band_width_delta_5"] = out["f_chip_band_width"] - shift_group(out, "f_chip_band_width", 5)
        reg("f_chip_band_width_delta_5", "chip_structure_delta", "chip_band_width - lag5")
        out["f_winner_rate_level"] = pd.to_numeric(out["winner_rate"], errors="coerce") / 100.0
        reg("f_winner_rate_level", "chip_structure_delta", "winner_rate / 100")
        out["f_winner_rate_delta_5"] = out["f_winner_rate_level"] - shift_group(out, "f_winner_rate_level", 5)
        reg("f_winner_rate_delta_5", "chip_structure_delta", "winner_rate_level - lag5")
        winner_mean_20 = rolling_group(out, "f_winner_rate_level", 20, "mean")
        winner_std_20 = rolling_group(out, "f_winner_rate_level", 20, "std")
        out["f_winner_rate_z_20"] = (out["f_winner_rate_level"] - winner_mean_20) / (winner_std_20 + 1e-9)
        reg("f_winner_rate_z_20", "chip_structure_delta", "winner_rate zscore 20")
        out["f_weight_avg_gap"] = pd.to_numeric(out["close"], errors="coerce") / pd.to_numeric(out["weight_avg"], errors="coerce") - 1.0
        reg("f_weight_avg_gap", "chip_structure_delta", "close / weight_avg - 1")
        out["f_weight_avg_gap_delta_5"] = out["f_weight_avg_gap"] - shift_group(out, "f_weight_avg_gap", 5)
        reg("f_weight_avg_gap_delta_5", "chip_structure_delta", "weight_avg_gap - lag5")
        out["f_cost50_gap"] = pd.to_numeric(out["close"], errors="coerce") / pd.to_numeric(out["cost_50pct"], errors="coerce") - 1.0
        reg("f_cost50_gap", "chip_structure_delta", "close / cost_50pct - 1")
        out["f_chip_support"] = -(pd.to_numeric(out["close"], errors="coerce") - pd.to_numeric(out["weight_avg"], errors="coerce")).abs() / (pd.to_numeric(out["close"], errors="coerce") + 1e-9)
        reg("f_chip_support", "chip_structure_delta", "-abs(close-weight_avg)/close")
        out["f_chip_concentration_second_diff"] = out["f_chip_band_width"] - 2 * shift_group(out, "f_chip_band_width", 1) + shift_group(out, "f_chip_band_width", 2)
        reg("f_chip_concentration_second_diff", "chip_structure_delta", "chip_band_width second difference")

    # E. fundamentals_event_filters (graceful proxy version)
    out["f_valuation_proxy_pbias"] = -(out["f_price_vs_ma20"].abs())
    reg("f_valuation_proxy_pbias", "fundamentals_event_filters", "-abs(price_vs_ma20)")
    out["f_quality_proxy_low_vol"] = -out["f_realized_vol_20"]
    reg("f_quality_proxy_low_vol", "fundamentals_event_filters", "-realized_vol_20")
    out["f_event_proxy_gap_strength"] = out["f_gap_return"].abs() * np.sign(out["f_intraday_reversal"].fillna(0))
    reg("f_event_proxy_gap_strength", "fundamentals_event_filters", "abs(gap_return) * sign(intraday_reversal)")
    out["f_cashflow_proxy_amount_stability"] = -out["f_amount_cv_20"]
    reg("f_cashflow_proxy_amount_stability", "fundamentals_event_filters", "-amount_cv_20")
    if {"f_chip_support", "f_winner_rate_level"}.issubset(out.columns):
        out["f_defensive_winner_support"] = out["f_chip_support"] * (1 - out["f_winner_rate_level"].clip(lower=0, upper=1))
        reg("f_defensive_winner_support", "fundamentals_event_filters", "chip_support * (1 - winner_rate_level)")

    # Phase4: rank-normalized composite interactions. 原始相乘复合因子仅保留为 legacy，不直接晋升。
    def _ranked(col: str) -> str:
        ranked = f"{col}_ranked"
        out[ranked] = out.groupby("trade_date")[col].rank(pct=True)
        return ranked
    for col in ["f_amount_ratio_20", "f_vol_compression_10_20", "f_gap_return", "f_intraday_reversal", "f_chip_support", "f_quality_proxy_low_vol"]:
        if col in out.columns:
            _ranked(col)
    interaction_specs = [
        ("f_amount_ratio_20", "f_vol_compression_10_20", "liquidity_turnover_distribution"),
        ("f_gap_return", "f_intraday_reversal", "price_volume_momentum"),
        ("f_chip_support", "f_quality_proxy_low_vol", "fundamentals_event_filters"),
    ]
    for left, right, family in interaction_specs:
        lrank, rrank = f"{left}_ranked", f"{right}_ranked"
        if lrank in out.columns and rrank in out.columns:
            name = f"{left}_x_{right}_rank_interaction"
            out[name] = out[lrank] * out[rrank]
            reg(name, family, f"rank({left}) * rank({right})", "rank-normalized interaction", "先截面 rank-normalize 再交互，降低量纲和极端值驱动的伪相关。")
    return out, factor_defs


def add_base_and_labels(panel: pd.DataFrame, base: pd.DataFrame, base_factor_id: str, cache_dir: Path) -> pd.DataFrame:
    out = panel.merge(base, on=["trade_date", "ts_code"], how="left", indicator="_base_merge")
    out = out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    out["base_record_present"] = out["_base_merge"].eq("both").astype(int)
    out = out.drop(columns=["_base_merge"])
    out["base_factor_id"] = out["base_factor_id"].fillna(base_factor_id)
    out["base_factor_selected"] = out["base_factor_selected"].fillna(0).astype(int)
    out["base_factor_score"] = out["base_factor_score"].fillna(0.0)
    out["base_factor_score_norm"] = out["base_factor_score_norm"].fillna(0.0)
    out["base_factor_rank"] = out["base_factor_rank"].fillna(9999).astype(int)
    out["base_factor_score_source"] = out["base_factor_score_source"].fillna("missing_base_record")
    out = add_benchmark_eligibility(out)
    next_close = pd.to_numeric(out.groupby("ts_code", group_keys=False)["close"].shift(-1), errors="coerce")
    next_open = pd.to_numeric(out.groupby("ts_code", group_keys=False)["open"].shift(-1), errors="coerce")
    terminal_trade_date = str(out["trade_date"].max()) if not out.empty else ""
    if terminal_trade_date:
        terminal_daily = load_terminal_next_daily_prices(cache_dir, terminal_trade_date)
        if not terminal_daily.empty:
            out = out.merge(terminal_daily, on=["ts_code"], how="left")
            terminal_mask = out["trade_date"].astype(str).eq(terminal_trade_date)
            terminal_next_close = pd.to_numeric(out["terminal_next_close"], errors="coerce")
            terminal_next_open = pd.to_numeric(out["terminal_next_open"], errors="coerce")
            next_close = next_close.where(~terminal_mask | next_close.notna(), terminal_next_close)
            next_open = next_open.where(~terminal_mask | next_open.notna(), terminal_next_open)
            out = out.drop(columns=["terminal_next_open", "terminal_next_close"])
    curr_close = pd.to_numeric(out["close"], errors="coerce")
    out["next_return_close_to_close_1d"] = next_close / curr_close.replace(0, np.nan) - 1.0
    out["next_return_open_to_close_1d"] = next_close / next_open.replace(0, np.nan) - 1.0
    out["next_return_close_to_open_1d"] = next_open / curr_close.replace(0, np.nan) - 1.0
    out["next_return_1d"] = out["next_return_open_to_close_1d"]
    label_columns = [
        "next_return_close_to_close_1d",
        "next_return_open_to_close_1d",
        "next_return_close_to_open_1d",
    ]
    finite_labels = out[label_columns].replace([np.inf, -np.inf], np.nan)
    if (finite_labels <= -1.0).any().any():
        raise ValueError("factor labels contain an impossible return at or below -100%")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build factor panel for Phase 1")
    parser.add_argument(
        "--root",
        default=str(Path.home() / ".openclaw/workspace/factor_factory"),
        help="factor_factory root",
    )
    args = parser.parse_args()

    cfg = load_runtime_config(Path(args.root).expanduser().resolve())
    cfg.panel_out.parent.mkdir(parents=True, exist_ok=True)
    cfg.base_out.parent.mkdir(parents=True, exist_ok=True)
    cfg.catalog_out.parent.mkdir(parents=True, exist_ok=True)

    dates = available_dates(cfg)
    universe = load_universe(cfg, dates)
    market = load_market_panel(cfg, dates)
    base = load_base_exposure(cfg, dates)

    panel = universe.merge(market, on=["trade_date", "ts_code"], how="left")
    panel, factor_defs = add_candidate_factors(panel)
    panel = add_base_and_labels(panel, base, cfg.base_factor_id, cfg.cache_dir)
    panel = panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    market_cap_col = "total_mv" if "total_mv" in panel.columns else ("circ_mv" if "circ_mv" in panel.columns else "amount")
    market_cap_coverage = float(pd.to_numeric(panel.get(market_cap_col), errors="coerce").notna().mean()) if market_cap_col in panel.columns else 0.0
    industry_col = "sw_industry" if "sw_industry" in panel.columns else ("industry" if "industry" in panel.columns else None)
    industry_coverage = float(panel[industry_col].notna().mean()) if industry_col else 0.0
    data_quality_summary = {
        "market_cap_col": market_cap_col,
        "market_cap_coverage": market_cap_coverage,
        "industry_col": industry_col,
        "industry_coverage": industry_coverage,
        "neutralization_mode": "full" if market_cap_coverage >= 0.90 and industry_coverage >= 0.80 else ("size_only" if market_cap_coverage >= 0.90 else "raw_or_liquidity_proxy"),
        "price_basis": "qfq",
        "proxy_chip_rows_used": 0,
        "proxy_chip_dates_rejected": int(
            panel.loc[panel.get("chip_data_status", "") == "proxy_rejected", "trade_date"].nunique()
            if "chip_data_status" in panel.columns
            else 0
        ),
    }

    panel.to_parquet(cfg.panel_out, index=False)
    base.to_parquet(cfg.base_out, index=False)

    catalog = {
        "panel_path": str(cfg.panel_out),
        "base_exposure_path": str(cfg.base_out),
        "window": {"start_date": cfg.start_date, "end_date": cfg.end_date},
        "dates": {"count": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None},
        "base_factor": {"id": cfg.base_factor_id, "black_box": True, "fixed_weight": 0.40},
        "benchmark": {
            "eligible_trade_dates": int(panel.loc[panel["benchmark_date_eligible"] == 1, "trade_date"].nunique()),
            "row_mask_column": "benchmark_eligible",
            "date_mask_column": "benchmark_date_eligible",
            "base_record_mask_column": "base_record_present",
        },
        "label_columns": [
            "next_return_1d",
            "next_return_close_to_close_1d",
            "next_return_open_to_close_1d",
            "next_return_close_to_open_1d",
        ],
        "execution_contract": {
            "signal_data_cutoff": "T close",
            "primary_entry": "T+1 open_qfq",
            "primary_label": "next_return_open_to_close_1d",
            "same_day_open_entry_forbidden": True,
        },
        "candidate_factor_count": len(factor_defs),
        "data_quality_summary": data_quality_summary,
        "candidate_factors": factor_defs,
    }
    cfg.catalog_out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "panel_path": str(cfg.panel_out),
        "base_exposure_path": str(cfg.base_out),
        "catalog_path": str(cfg.catalog_out),
        "rows": int(len(panel)),
        "dates": len(dates),
        "benchmark_eligible_dates": int(panel.loc[panel["benchmark_date_eligible"] == 1, "trade_date"].nunique()),
        "benchmark_eligible_rows": int(panel["benchmark_eligible"].sum()),
        "candidate_factor_count": len(factor_defs),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
