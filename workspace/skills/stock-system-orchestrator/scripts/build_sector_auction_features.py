#!/usr/bin/env python3
"""S5 板块竞价特征库.

反窥探纪律: 本脚本只计算竞价日 T+1 的 09:25 可知特征——
    - 竞价撮合价/量/额 (T+1 当日集合竞价, 09:25 已产生)
    - 昨收/行业分类 (来自面板 T 日行, T 日收盘后即可知, 早于 T+1 09:25)
    - auct_amt_z 的滚动窗口严格只用 <= 当前竞价日的历史竞价额 (含当日, 不含未来)
不计算、不合并、不查看任何前向收益(next_return_*)、pct_chg(T+1 当日涨跌幅)或其他"预测力"字段。

输出: stock_data/03-working/sector_auction_features.parquet
每 (industry, trade_date=竞价日 T+1) 一行:
    n_members         当日有竞价数据的成员数
    gap_breadth_2      成员 gap>=+2% 占比 (gap = 竞价撮合价/昨收 - 1)
    gap_median         成员 gap 中位数
    auct_amt_z         板块竞价总额 vs 该板块过去20日竞价总额的滚动 z 分数
    amt_concentration  板块内竞价额 HHI (sum((amt_i/amt_total)^2))
    leader_gap         板块内竞价额最大成员的 gap
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
AUCTION_DIR = WORKSPACE / "stock_data/01-root/auction_cache"
PANEL_PATH = WORKSPACE / "factor_factory/data/factors/greenfield_multifactor_panel_full.parquet"
OUT_PATH = WORKSPACE / "stock_data/03-working/sector_auction_features.parquet"

ROLL_WINDOW = 20
GAP_BREADTH_THRESHOLD = 2.0  # percent


def load_auction_data() -> pd.DataFrame:
    """Load all yearly auction_open_*.parquet files, keep only rows with real trades."""
    files = sorted(glob.glob(str(AUCTION_DIR / "auction_open_*.parquet")))
    if not files:
        raise RuntimeError(f"no auction_open_*.parquet found under {AUCTION_DIR}")
    frames = []
    for f in files:
        df = pd.read_parquet(f, columns=["ts_code", "trade_date", "close", "vol", "amount"])
        frames.append(df)
    auc = pd.concat(frames, ignore_index=True)
    auc["trade_date"] = auc["trade_date"].astype(str)
    # drop rows with no real auction trade (suspended / no order match)
    auc = auc[(auc["vol"] > 0) & (auc["amount"] > 0) & auc["close"].notna()].copy()
    auc = auc.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    return auc


def load_panel_reference() -> pd.DataFrame:
    """Load panel columns needed to attach industry + pre_close, keyed by (ts_code, trade_date=T)."""
    cols = ["ts_code", "trade_date", "close", "industry", "universe_flag"]
    panel = pd.read_parquet(PANEL_PATH, columns=cols)
    panel["trade_date"] = panel["trade_date"].astype(str)
    # respect universe_flag filter defensively (currently always 1 in this panel)
    panel = panel[panel["universe_flag"] == 1].copy()
    panel = panel.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    return panel


def build_trading_calendar(panel_dates: pd.Series) -> pd.Series:
    """Return sorted unique panel trade dates, used to find 'previous panel date' for each auction date."""
    return pd.Series(sorted(panel_dates.unique()))


def attach_prev_panel_date(auction_dates: list[str], calendar: pd.Series) -> dict[str, str]:
    """For each auction date T+1, find the latest panel trading date strictly before it (=T)."""
    cal_arr = calendar.to_numpy()
    mapping: dict[str, str] = {}
    for d in auction_dates:
        idx = np.searchsorted(cal_arr, d, side="left") - 1
        if idx >= 0:
            mapping[d] = cal_arr[idx]
    return mapping


def main() -> int:
    auc = load_auction_data()
    panel = load_panel_reference()

    calendar = build_trading_calendar(panel["trade_date"])
    auction_dates = sorted(auc["trade_date"].unique())
    prev_date_map = attach_prev_panel_date(auction_dates, calendar)

    auc["panel_ref_date"] = auc["trade_date"].map(prev_date_map)
    n_dropped_no_ref = auc["panel_ref_date"].isna().sum()
    auc = auc.dropna(subset=["panel_ref_date"]).copy()

    # attach industry + pre_close from panel's T-day row (T = panel_ref_date)
    panel_ref = panel.rename(
        columns={"trade_date": "panel_ref_date", "close": "pre_close"}
    )[["ts_code", "panel_ref_date", "pre_close", "industry"]]

    merged = auc.merge(panel_ref, on=["ts_code", "panel_ref_date"], how="inner")
    n_dropped_no_panel_row = len(auc) - len(merged)

    merged = merged[merged["pre_close"] > 0].copy()
    merged["gap"] = (merged["close"] / merged["pre_close"] - 1.0) * 100.0
    merged = merged.dropna(subset=["industry"])

    # ---- per (industry, trade_date) aggregation ----
    grp = merged.groupby(["industry", "trade_date"], sort=False)

    def _hhi(amounts: pd.Series) -> float:
        total = amounts.sum()
        if total <= 0:
            return np.nan
        shares = amounts / total
        return float((shares**2).sum())

    agg_rows = []
    for (industry, trade_date), g in grp:
        n_members = len(g)
        gap_breadth_2 = float((g["gap"] >= GAP_BREADTH_THRESHOLD).mean())
        gap_median = float(g["gap"].median())
        amt_total = float(g["amount"].sum())
        amt_concentration = _hhi(g["amount"])
        leader_idx = g["amount"].idxmax()
        leader_gap = float(g.loc[leader_idx, "gap"])
        agg_rows.append(
            {
                "industry": industry,
                "trade_date": trade_date,
                "n_members": n_members,
                "gap_breadth_2": gap_breadth_2,
                "gap_median": gap_median,
                "sector_auction_amount": amt_total,
                "amt_concentration": amt_concentration,
                "leader_gap": leader_gap,
            }
        )

    out = pd.DataFrame(agg_rows)
    out = out.sort_values(["industry", "trade_date"]).reset_index(drop=True)

    # rolling z-score of sector auction amount vs its own trailing 20-day history,
    # using only <= current day's data (min_periods enforced so early days are NaN, not leaked).
    def _rolling_z(s: pd.Series) -> pd.Series:
        roll = s.rolling(window=ROLL_WINDOW, min_periods=5)
        mean = roll.mean()
        std = roll.std(ddof=0)
        z = (s - mean) / std.replace(0, np.nan)
        return z

    out["auct_amt_z"] = out.groupby("industry")["sector_auction_amount"].transform(_rolling_z)
    out = out.drop(columns=["sector_auction_amount"])

    out = out[
        [
            "industry",
            "trade_date",
            "n_members",
            "gap_breadth_2",
            "gap_median",
            "auct_amt_z",
            "amt_concentration",
            "leader_gap",
        ]
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT_PATH, index=False)

    print(f"created={OUT_PATH}")
    print(f"rows={len(out)}")
    print(f"industries={out['industry'].nunique()}")
    print(f"trade_dates={out['trade_date'].nunique()} range={out['trade_date'].min()}..{out['trade_date'].max()}")
    print(f"dropped_auction_rows_no_prior_panel_date={n_dropped_no_ref}")
    print(f"dropped_auction_rows_no_panel_match={n_dropped_no_panel_row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
