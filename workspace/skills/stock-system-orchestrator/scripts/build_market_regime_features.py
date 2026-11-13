#!/usr/bin/env python3
"""L1 Regime 特征库 (剧本引擎三层架构第1层: Regime闸).

参见 factor_factory/docs/setup_engine_plan_v1.md 第1层定义:
    "市场级: 涨停家数/连板高度/成交额/行业热度加速 → 每个剧本声明自己的适用regime,不适用时禁火"
    S4 剧本声明的决策点是 "T日收盘" —— 即本文件除 auct_ 前缀列外的全部列都只用 T 日面板本身的
    收盘数据计算,在 T 日收盘后即可知,供下游剧本在 T+1 决策点读取作为"regime闸"。

反窥探纪律: 只算特征,不碰任何 next_return_*/前瞻收益字段,不做任何预测力/IC筛选。

行语义(每 trade_date=T 一行):
    limitup_count      T 日 pct_chg>=9.5 的家数 (T 日收盘可知)
    limitdown_count    T 日 pct_chg<=-9.5 的家数 (T 日收盘可知)
    breadth            T 日上涨(pct_chg>0)家数占比 (T 日收盘可知)
    amount_z20         T 日全市场成交额 vs 其自身过去20日滚动均值/标准差的 z 分数 (T 日收盘可知,
                       滚动窗口只用 <= T 日的历史数据)
    hot_sector_share   T 日涨幅(按 industry 分组的平均 pct_chg)前3板块的成交额之和 / 全市场成交额
                       (T 日收盘可知)
    regime_tag         三态标签,见 classify_regime() 注释 (T 日收盘可知)

    auct_gap_up_breadth  次日9:25版,单独前缀 auct_ 以示区分: 这一列存的是 T 日之后下一个"竞价日"
                         (即 T+1, 若 T+1 无竞价数据则该行为空) 09:25 集合竞价 gap>+1% 的家数占比。
                         这是本文件中唯一一个"T+1 9:25 可知"而非"T 日收盘可知"的列 —— 之所以仍挂在
                         T 行上,是为了让下游在读 T 行做 T+1 regime 闸判断时,能在同一行里同时看到
                         "T 收盘定的 regime_tag" 和 "T+1 早盘竞价的确认/证伪信号"。使用方必须清楚
                         这一列的时间戳与其余列不同,不可在"T 日收盘时点"当作已知量使用。

输出: stock_data/03-working/market_regime_features.parquet
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL_PATH = WORKSPACE / "factor_factory/data/factors/greenfield_multifactor_panel_full.parquet"
AUCTION_DIR = WORKSPACE / "stock_data/01-root/auction_cache"
OUT_PATH = WORKSPACE / "stock_data/03-working/market_regime_features.parquet"

ROLL_WINDOW = 20
LIMIT_UP_THRESHOLD = 9.5
LIMIT_DOWN_THRESHOLD = -9.5
TOP_N_HOT_SECTORS = 3
AUCT_GAP_UP_THRESHOLD = 1.0  # percent

# --- regime_tag 阈值 (写死在此, 未经调参的先验粗分, 仅供工程占位用, 不构成任何回测结论) ---
REGIME_ACTIVE_LIMITUP_MIN = 80        # 活跃: 涨停家数 >= 80 且 amount_z20 > 0.5
REGIME_ACTIVE_AMOUNT_Z_MIN = 0.5
REGIME_COLD_LIMITUP_MAX = 25          # 冰点: 涨停家数 <= 25
# 其余 = 中性


def load_panel() -> pd.DataFrame:
    cols = ["trade_date", "ts_code", "pct_chg", "amount", "industry", "universe_flag"]
    panel = pd.read_parquet(PANEL_PATH, columns=cols)
    panel["trade_date"] = panel["trade_date"].astype(str)
    panel = panel[panel["universe_flag"] == 1].copy()
    panel["pct_chg"] = pd.to_numeric(panel["pct_chg"], errors="coerce")
    panel["amount"] = pd.to_numeric(panel["amount"], errors="coerce")
    return panel


def build_daily_market_features(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for trade_date, day in panel.groupby("trade_date", sort=True):
        valid = day.dropna(subset=["pct_chg"])
        n = len(valid)
        limitup_count = int((valid["pct_chg"] >= LIMIT_UP_THRESHOLD).sum())
        limitdown_count = int((valid["pct_chg"] <= LIMIT_DOWN_THRESHOLD).sum())
        breadth = float((valid["pct_chg"] > 0).mean()) if n > 0 else np.nan
        total_amount = float(day["amount"].sum())

        # hot_sector_share: industry 内平均 pct_chg 排序取前3, 其成交额占全市场比
        by_ind = day.groupby("industry").agg(ind_ret=("pct_chg", "mean"), ind_amt=("amount", "sum"))
        by_ind = by_ind.sort_values("ind_ret", ascending=False)
        hot_amt = float(by_ind["ind_amt"].head(TOP_N_HOT_SECTORS).sum())
        hot_sector_share = hot_amt / total_amount if total_amount > 0 else np.nan

        rows.append(
            {
                "trade_date": trade_date,
                "n_stocks": n,
                "limitup_count": limitup_count,
                "limitdown_count": limitdown_count,
                "breadth": breadth,
                "total_amount": total_amount,
                "hot_sector_share": hot_sector_share,
            }
        )

    out = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)

    # amount_z20: rolling z-score using only <= current day's history (min_periods enforced)
    roll = out["total_amount"].rolling(window=ROLL_WINDOW, min_periods=5)
    mean = roll.mean()
    std = roll.std(ddof=0)
    out["amount_z20"] = (out["total_amount"] - mean) / std.replace(0, np.nan)
    out = out.drop(columns=["total_amount"])
    return out


def classify_regime(row: pd.Series) -> str:
    """未经调参的先验粗分, 阈值写死:
    活跃 = limitup_count >= 80 且 amount_z20 > 0.5
    冰点 = limitup_count <= 25
    中性 = 其余
    """
    limitup = row["limitup_count"]
    amount_z = row["amount_z20"]
    if pd.notna(limitup) and limitup >= REGIME_ACTIVE_LIMITUP_MIN and pd.notna(amount_z) and amount_z > REGIME_ACTIVE_AMOUNT_Z_MIN:
        return "活跃"
    if pd.notna(limitup) and limitup <= REGIME_COLD_LIMITUP_MAX:
        return "冰点"
    return "中性"


def build_auction_gap_up_breadth() -> pd.DataFrame:
    """Full-market auction gap-up breadth per auction trade_date (09:25, own-day auction only)."""
    files = sorted(glob.glob(str(AUCTION_DIR / "auction_open_*.parquet")))
    if not files:
        raise RuntimeError(f"no auction_open_*.parquet found under {AUCTION_DIR}")
    frames = []
    for f in files:
        df = pd.read_parquet(f, columns=["ts_code", "trade_date", "close", "vol", "amount"])
        frames.append(df)
    auc = pd.concat(frames, ignore_index=True)
    auc["trade_date"] = auc["trade_date"].astype(str)
    auc = auc[(auc["vol"] > 0) & (auc["amount"] > 0) & auc["close"].notna()].copy()
    auc = auc.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")

    panel_ref = pd.read_parquet(PANEL_PATH, columns=["ts_code", "trade_date", "close", "universe_flag"])
    panel_ref["trade_date"] = panel_ref["trade_date"].astype(str)
    panel_ref = panel_ref[panel_ref["universe_flag"] == 1].copy()
    calendar = pd.Series(sorted(panel_ref["trade_date"].unique())).to_numpy()

    auction_dates = sorted(auc["trade_date"].unique())
    prev_map = {}
    for d in auction_dates:
        idx = np.searchsorted(calendar, d, side="left") - 1
        if idx >= 0:
            prev_map[d] = calendar[idx]

    auc["panel_ref_date"] = auc["trade_date"].map(prev_map)
    auc = auc.dropna(subset=["panel_ref_date"]).copy()

    ref = panel_ref.rename(columns={"trade_date": "panel_ref_date", "close": "pre_close"})[
        ["ts_code", "panel_ref_date", "pre_close"]
    ]
    merged = auc.merge(ref, on=["ts_code", "panel_ref_date"], how="inner")
    merged = merged[merged["pre_close"] > 0].copy()
    merged["gap"] = (merged["close"] / merged["pre_close"] - 1.0) * 100.0

    grp = merged.groupby("trade_date")
    breadth = grp["gap"].apply(lambda s: float((s >= AUCT_GAP_UP_THRESHOLD).mean()))
    breadth = breadth.reset_index().rename(columns={"gap": "auct_gap_up_breadth"})

    # this table is keyed by auction trade_date (=T+1); to attach to the *prior* panel row (T),
    # we also carry panel_ref_date (=T) so the caller can join on T.
    # NOTE: panel data has occasional single-day gaps (missing panel rows on real trading days,
    # e.g. 20260408-20260410, 20260515, 20260520 ...). When a panel gap occurs, several
    # consecutive auction days can map to the SAME panel_ref_date=T via "nearest prior panel date".
    # To keep this a clean 1:1 (one row per T), we only keep the EARLIEST auction_trade_date
    # (i.e. the trading day immediately following T) for each panel_ref_date — later auction
    # days that fall inside the same gap are dropped from this join (their own auct_gap_up_breadth
    # values are still computed above and remain queryable by auction_trade_date if needed).
    ref_date_lookup = (
        auc.drop_duplicates(subset=["trade_date"])[["trade_date", "panel_ref_date"]]
        .sort_values("trade_date")
        .drop_duplicates(subset=["panel_ref_date"], keep="first")
    )
    breadth = breadth.merge(ref_date_lookup, on="trade_date", how="left")
    breadth = breadth.rename(columns={"trade_date": "auction_trade_date"})
    return breadth


def main() -> int:
    panel = load_panel()
    market = build_daily_market_features(panel)
    market["regime_tag"] = market.apply(classify_regime, axis=1)

    auct = build_auction_gap_up_breadth()
    # attach auct_gap_up_breadth (T+1's own auction breadth) onto row T (=panel_ref_date)
    auct_on_T = auct.dropna(subset=["panel_ref_date"]).rename(columns={"panel_ref_date": "trade_date"})[
        ["trade_date", "auct_gap_up_breadth"]
    ]
    assert auct_on_T["trade_date"].is_unique, "auct_on_T must be one row per panel trade_date"
    market = market.merge(auct_on_T, on="trade_date", how="left")
    assert market["trade_date"].is_unique, "market_regime_features must be one row per trade_date"

    market = market[
        [
            "trade_date",
            "n_stocks",
            "limitup_count",
            "limitdown_count",
            "breadth",
            "amount_z20",
            "hot_sector_share",
            "regime_tag",
            "auct_gap_up_breadth",
        ]
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    market.to_parquet(OUT_PATH, index=False)

    print(f"created={OUT_PATH}")
    print(f"rows={len(market)}")
    print(f"trade_dates_range={market['trade_date'].min()}..{market['trade_date'].max()}")
    print(f"regime_tag_counts={market['regime_tag'].value_counts().to_dict()}")
    print(f"auct_gap_up_breadth_coverage={market['auct_gap_up_breadth'].notna().mean():.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
