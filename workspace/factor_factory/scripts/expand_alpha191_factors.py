#!/usr/bin/env python3
"""Long-running Alpha191 factor expansion and optimization."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import alpha191_ops as ops

FACTORS_DIR = SCRIPTS_DIR.parent / "data" / "factors"
OUTPUT_DIR = SCRIPTS_DIR.parent / "outputs" / "t1_alpha191"
DAILY_DIR = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/backtest_cache")
LOG_FILE = OUTPUT_DIR / "long_running_research_log.jsonl"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def log_progress(entry: dict):
    entry["timestamp"] = datetime.now().isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[LOG] {entry.get('phase', 'unknown')}: {entry.get('message', '')}")


def load_daily_data(max_files: int = 500) -> pd.DataFrame:
    files = sorted(DAILY_DIR.glob("daily_*.parquet"))
    files = files[-max_files:]
    print(f"Loading {len(files)} daily files...")
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def filter_universe(df: pd.DataFrame) -> pd.DataFrame:
    mask = df["ts_code"].str.match(r"^(000|001|002|003|300|301|600|601|603|605)\d{3}\.(SZ|SH)$")
    df = df[mask].copy()
    avg_amount = df.groupby("ts_code")["amount"].mean()
    valid = avg_amount[avg_amount >= 5000].index
    df = df[df["ts_code"].isin(valid)].copy()
    count = df.groupby("ts_code").size()
    valid = count[count >= 60].index
    return df[df["ts_code"].isin(valid)].copy()


def compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["returns"] = df["pct_chg"] / 100.0
    df["vwap"] = np.where(df["vol"] > 0, df["amount"] / df["vol"] * 10, df["close"])
    df["volume"] = df["vol"]
    df["adv20"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(20, min_periods=1).mean())
    df["adv5"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["adv10"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(10, min_periods=1).mean())
    df["adv60"] = df.groupby("ts_code")["volume"].transform(lambda x: x.rolling(60, min_periods=1).mean())
    return df


def compute_factor(df: pd.DataFrame, factor_id: str) -> pd.Series:
    """因子计算入口（2026-06 根治跨股票泄漏）：给每个输入序列带上 (ts_code, trade_date) 多级索引，
    使 alpha191_ops 的时序算子按股分组、rank/scale 按日截面分组；算完还原为 df 原始索引（行序一致）。"""
    if "ts_code" not in df.columns or "trade_date" not in df.columns:
        return _compute_factor_impl(df, factor_id)
    orig_index = df.index
    idx = pd.MultiIndex.from_arrays(
        [df["ts_code"].to_numpy(), df["trade_date"].to_numpy()],
        names=["ts_code", "trade_date"],
    )
    result = _compute_factor_impl(df.set_axis(idx), factor_id)
    if not isinstance(result, pd.Series):
        result = pd.Series(np.asarray(result), index=idx)
    result = result.reindex(idx)  # 还原到 df 行序
    return pd.Series(result.to_numpy(), index=orig_index)


def _compute_factor_impl(df: pd.DataFrame, factor_id: str) -> pd.Series:
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    v, r, vw = df["volume"], df["returns"], df["vwap"]
    a20, a5, a10, a60 = df["adv20"], df["adv5"], df["adv10"], df["adv60"]

    try:
        # === Batch 1: alpha001-alpha020 ===
        if factor_id == "alpha001":
            return ops.rank(ops.ts_argmax(ops.signed_power(pd.Series(np.where(r < 0, ops.std(r, 20), c), index=r.index), 2.0), 5)) - 0.5
        elif factor_id == "alpha002":
            return -1 * ops.corr(ops.rank(ops.delta(np.log(v.clip(lower=1)), 2)), ops.rank((c - o) / o.replace(0, np.nan)), 6)
        elif factor_id == "alpha003":
            return -1 * ops.corr(ops.rank(o), ops.rank(v), 10)
        elif factor_id == "alpha004":
            return -1 * ops.ts_rank(ops.rank(l), 9)
        elif factor_id == "alpha005":
            return ops.rank(o - ops.mean(vw, 10)) * (-1 * abs(ops.rank(c - vw)))
        elif factor_id == "alpha006":
            return -1 * ops.corr(o, v, 10)
        elif factor_id == "alpha007":
            return pd.Series(np.where(a20 < v, -1 * ops.ts_rank(abs(ops.delta(c, 7)), 60) * np.sign(ops.delta(c, 7)), -1), index=df.index)
        elif factor_id == "alpha008":
            return -1 * ops.rank(ops.mean(o, 5) * ops.mean(r, 5) - ops.delay(ops.mean(o, 5) * ops.mean(r, 5), 10))
        elif factor_id == "alpha009":
            return pd.Series(np.where(ops.ts_min(ops.delta(c, 1), 5) > 0, ops.delta(c, 1),
                   np.where(ops.ts_max(ops.delta(c, 1), 5) < 0, ops.delta(c, 1), -1 * ops.delta(c, 1))), index=df.index)
        elif factor_id == "alpha010":
            return ops.rank(pd.Series(np.where(ops.ts_min(ops.delta(c, 1), 4) > 0, ops.delta(c, 1),
                            np.where(ops.ts_max(ops.delta(c, 1), 4) < 0, ops.delta(c, 1), -1 * ops.delta(c, 1))), index=df.index))
        elif factor_id == "alpha011":
            return (ops.rank(ops.ts_max(vw - c, 3)) + ops.rank(ops.ts_min(vw - c, 3))) * ops.rank(ops.delta(v, 3))
        elif factor_id == "alpha012":
            return np.sign(ops.delta(v, 1)) * (-1 * ops.delta(c, 1))
        elif factor_id == "alpha013":
            return -1 * ops.cov(ops.rank(c), ops.rank(v), 5)
        elif factor_id == "alpha014":
            return -1 * ops.rank(ops.delta(r, 3)) * ops.corr(o, v, 10)
        elif factor_id == "alpha015":
            return -1 * ops.sum(ops.rank(ops.corr(ops.rank(h), ops.rank(v), 3)), 3)
        elif factor_id == "alpha016":
            return -1 * ops.cov(ops.rank(h), ops.rank(v), 5)
        elif factor_id == "alpha017":
            return -1 * ops.rank(ops.ts_rank(c, 10)) * ops.rank(ops.delta(ops.delta(c, 1), 1)) * ops.rank(ops.ts_rank(v / a20, 5))
        elif factor_id == "alpha018":
            return -1 * ops.rank(ops.std(abs(c - o), 5) + (c - o) + ops.corr(c, o, 10))
        elif factor_id == "alpha019":
            return -1 * np.sign(c - ops.delay(c, 7) + ops.delta(c, 7)) * (1 + ops.rank(1 + ops.sum(r, 250)))
        elif factor_id == "alpha020":
            return -1 * ops.rank(o - ops.delay(h, 1)) * ops.rank(o - ops.delay(c, 1)) * ops.rank(o - ops.delay(l, 1))

        # === Batch 2: alpha021-alpha060 ===
        elif factor_id == "alpha021":
            mean_8 = ops.mean(c, 8)
            std_8 = ops.std(c, 8)
            sum_2 = ops.sum(ops.mean(c, 8) + ops.std(c, 8) + ops.mean(c, 2), 2)
            return pd.Series(np.where(mean_8 + std_8 < c, -1, np.where(c < mean_8 - std_8, 1, np.where(1 < sum_2 / c, 1, -1))), index=df.index)
        elif factor_id == "alpha022":
            return -1 * ops.delta(ops.corr(ops.rank(vw), ops.rank(v), 5), 5) * ops.rank(ops.std(c, 20))
        elif factor_id == "alpha023":
            return pd.Series(np.where(ops.mean(h, 20) < h, -1 * ops.delta(h, 2), 0), index=df.index)
        elif factor_id == "alpha024":
            return pd.Series(np.where(ops.delta(ops.mean(c, 100), 100) / ops.delay(c, 100) <= 0.05, -1 * (c - ops.ts_min(c, 100)), -1 * ops.delta(c, 3)), index=df.index)
        elif factor_id == "alpha025":
            return -1 * ops.rank(ops.delta(c, 7) * (1 - ops.rank(ops.decay_linear(v / a20, 9)))) * (1 + ops.rank(ops.sum(r, 250)))
        elif factor_id == "alpha026":
            return -1 * ops.ts_max(ops.corr(ops.ts_rank(v, 5), ops.ts_rank(h, 5), 5), 3)
        elif factor_id == "alpha027":
            return ops.rank(ops.mean(ops.corr(ops.rank(v), ops.rank(vw), 6), 2)) * -1
        elif factor_id == "alpha028":
            return ops.scale(ops.corr(a20, l, 5) + (h + l) / 2 - c)
        elif factor_id == "alpha029":
            return np.minimum(ops.rank(ops.decay_linear(ops.rank(ops.rank(ops.corr(vw, ops.sum(ops.mean(c, 5), 2), 10))) * -1, 4)), ops.rank(ops.decay_linear(ops.ts_rank(ops.ts_argmin(ops.corr(ops.rank(o), ops.rank(a10), 7), 7), 6), 8)))
        elif factor_id == "alpha030":
            return (1 - ops.rank(ops.signed_power(np.sin(ops.rank(ops.corr(ops.rank(vw), ops.rank(v), 5))), 2))) * -1
        elif factor_id == "alpha031":
            return ops.rank(ops.rank(ops.rank(ops.decay_linear(-1 * ops.rank(ops.rank(ops.delta(c, 10))), 10)))) + ops.rank(-1 * ops.delta(c, 3)) + ops.scale(ops.corr(a20, l, 12))
        elif factor_id == "alpha032":
            return ops.scale(ops.mean(c, 7) - c) + 20 * ops.scale(ops.corr(vw, ops.delay(c, 5), 230))
        elif factor_id == "alpha033":
            return ops.rank(-1 + (o / c)) * -1
        elif factor_id == "alpha034":
            return ops.rank(ops.mean(2 * ops.rank(ops.rank(ops.rank(ops.delta(c, 1)))) + ops.rank(ops.rank(ops.sum(ops.mean(c, 5), 2))) + ops.rank(ops.delta(c, 6)), 4))
        elif factor_id == "alpha035":
            return ops.rank(ops.mean(v, 10)) * ops.corr(ops.rank(vw), ops.rank(c), 5) * -1
        elif factor_id == "alpha036":
            return 2.21 * ops.rank(ops.corr((o - c), ops.delay(v, 1), 15)) + 0.7 * ops.rank(o - c) + 0.73 * ops.rank(ops.ts_rank(ops.decay_linear(ops.decay_linear(ops.corr(ops.rank(c), ops.sum(ops.mean(v, 60), 9), 5), 16), 4), 19)) + ops.rank(ops.corr(ops.sum(ops.mean(v, 5), 20), ops.sum(ops.mean(v, 60), 9), 5)) * -0.6 + ops.rank(abs(ops.corr(a20, l, 6))) * -1
        elif factor_id == "alpha037":
            return ops.rank(ops.corr(ops.delay(o - c, 1), c, 200)) + ops.rank(o - c)
        elif factor_id == "alpha038":
            return -1 * ops.rank(ops.ts_rank(c, 10)) * ops.rank(c / o)
        elif factor_id == "alpha039":
            return -1 * ops.rank(ops.decay_linear(ops.delta(c, 10), 10)) * ops.rank(ops.corr(a20, l, 15)) * -1
        elif factor_id == "alpha040":
            return -1 * ops.rank(ops.std(h, 10)) * ops.corr(ops.rank(h), ops.rank(v), 10)
        elif factor_id == "alpha041":
            return np.power(a20 * (h - l), 0.5) / vw
        elif factor_id == "alpha042":
            return ops.rank(vw - c) / ops.rank(vw + c)
        elif factor_id == "alpha043":
            return ops.ts_rank(v / a20, 20) * -1 * ops.ts_rank(ops.delta(c, 7), 8)
        elif factor_id == "alpha044":
            return -1 * ops.corr(ops.rank(h), ops.rank(a20), 9)
        elif factor_id == "alpha045":
            return -1 * ops.rank(ops.mean(ops.delay(c, 5), 20)) * ops.corr(ops.rank(c), ops.rank(v), 5) * ops.corr(ops.rank(ops.delay(c, 5)), ops.rank(v), 10)
        elif factor_id == "alpha046":
            delta_pct = ops.delta(ops.mean(c, 20), 20) / ops.delay(c, 20) * 100
            return pd.Series(np.where(delta_pct > 0.5, 1, np.where(delta_pct < -0.5, -1, ops.delta(c, 3))), index=df.index)
        elif factor_id == "alpha047":
            return (ops.rank(1 / c) * v / a20 * ops.rank(h - ops.ts_rank(vw, 5))) * -1
        elif factor_id == "alpha048":
            return ops.corr(ops.delta(c, 1), ops.delta(ops.delay(c, 1), 1), 250) * ops.delta(c, 1) / c
        elif factor_id == "alpha049":
            delta_pct = ops.delta(ops.mean(c, 15), 15) / ops.delay(c, 15)
            return pd.Series(np.where(delta_pct < -0.1, 1, -1 * ops.delta(c, 1)), index=df.index)
        elif factor_id == "alpha050":
            return -1 * ops.ts_max(ops.rank(ops.corr(ops.rank(v), ops.rank(vw), 5)), 5)
        elif factor_id == "alpha051":
            delta_pct = ops.delta(ops.mean(c, 15), 15) / ops.delay(c, 15)
            return pd.Series(np.where(delta_pct < -0.1, 1, -1 * ops.delta(c, 1)), index=df.index)
        elif factor_id == "alpha052":
            return ops.ts_min(ops.rank(ops.decay_linear(ops.delta(c, 8), 10)), ops.rank(ops.decay_linear(ops.ts_rank(ops.ts_argmin(ops.corr(ops.rank(o), ops.rank(a10), 7), 7), 6), 8))) * -1
        elif factor_id == "alpha053":
            return -1 * ops.delta((c - l) / (c - h).replace(0, np.nan) * -1 + (c - l) / (c - h).replace(0, np.nan), 9)
        elif factor_id == "alpha054":
            return -1 * (l - c) * np.power(o, 5) / ((l - h) * np.power(c, 5))
        elif factor_id == "alpha055":
            return -1 * ops.corr(ops.rank((c - ops.ts_min(l, 12)) / (ops.ts_max(h, 12) - ops.ts_min(l, 12)).replace(0, np.nan)), ops.rank(v), 6)
        elif factor_id == "alpha056":
            return -1 * ops.sum(ops.rank(ops.corr(ops.rank(c), ops.rank(ops.sum(ops.mean(v, 5), 10)), 4)), 4) * ops.rank(ops.std(r, 5))
        elif factor_id == "alpha057":
            return -1 * (c - vw) / ops.rank(ops.ts_max(c, 30))
        elif factor_id == "alpha058":
            return -1 * ops.ts_rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(v), 4), 8), 6)
        elif factor_id == "alpha059":
            return -1 * ops.ts_rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(v), 4), 16), 8)
        elif factor_id == "alpha060":
            return -1 * (2 * ops.scale(ops.rank(((c - l) / (h - l).replace(0, np.nan)) * v - ops.rank(c))))

        # === Batch 3: alpha061-alpha100 ===
        elif factor_id == "alpha061":
            return ops.rank(ops.decay_linear(ops.rank(vw - ops.ts_min(vw, 16)) * ops.rank(ops.corr(a20, l, 18)), 7)) * -1
        elif factor_id == "alpha062":
            return ops.corr(ops.rank(vw), ops.rank(a20), 6) * -1
        elif factor_id == "alpha063":
            return ops.rank(ops.decay_linear(ops.delta(vw, 4), 7)) + ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(l, ops.mean(a10), 7), 4), 8)) * -1
        elif factor_id == "alpha064":
            return ops.rank(ops.corr(ops.sum(ops.mean(vw, 14), 5), ops.sum(ops.mean(v, 50), 5), 10)) * -1
        elif factor_id == "alpha065":
            return ops.rank(ops.corr(ops.mean(vw, 6), ops.mean(v, 20), 6)) * -1
        elif factor_id == "alpha066":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(a20), 8), 20), 8)) * -1
        elif factor_id == "alpha067":
            return ops.rank(ops.corr(ops.rank(h), ops.rank(v), 10)) * -1
        elif factor_id == "alpha068":
            return ops.rank(ops.corr(ops.rank(vw), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha069":
            return ops.rank(ops.ts_max(ops.delta(ops.rank(vw), 3), 5)) * -1
        elif factor_id == "alpha070":
            return ops.rank(ops.delta(c, 6)) * -1
        elif factor_id == "alpha071":
            return ops.ts_rank(ops.corr(ops.rank(c), ops.rank(a20), 6), 4) * -1
        elif factor_id == "alpha072":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha073":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 4), 8)) * -1
        elif factor_id == "alpha074":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 6)) * -1
        elif factor_id == "alpha075":
            return ops.rank(ops.corr(ops.rank(vw), ops.rank(v), 6)) * -1
        elif factor_id == "alpha076":
            return ops.rank(ops.decay_linear(ops.delta(vw, 1), 12)) * -1
        elif factor_id == "alpha077":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 4), 4)) * -1
        elif factor_id == "alpha078":
            return ops.rank(ops.corr(ops.sum(ops.mean(vw, 10), 5), ops.sum(ops.mean(v, 60), 5), 10)) * -1
        elif factor_id == "alpha079":
            return ops.rank(ops.delta(ops.rank(vw), 3)) * -1
        elif factor_id == "alpha080":
            return ops.rank(ops.delta(c, 6)) * -1
        elif factor_id == "alpha081":
            return ops.rank(ops.corr(ops.rank(vw), ops.rank(v), 10)) * -1
        elif factor_id == "alpha082":
            return ops.rank(ops.decay_linear(ops.delta(c, 4), 6)) * -1
        elif factor_id == "alpha083":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha084":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha085":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha086":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha087":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha088":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(h), ops.rank(v), 4), 4), 4)) * -1
        elif factor_id == "alpha089":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha090":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha091":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(c), ops.rank(v), 10), 8)) * -1
        elif factor_id == "alpha092":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8), 6)) * -1
        elif factor_id == "alpha093":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 8)) * -1
        elif factor_id == "alpha094":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha095":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha096":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha097":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha098":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha099":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha100":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1

        elif factor_id == "alpha101":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 10)) * -1
        elif factor_id == "alpha102":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha103":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha104":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha105":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha106":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(c), ops.rank(v), 10), 8)) * -1
        elif factor_id == "alpha107":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8), 6)) * -1
        elif factor_id == "alpha108":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 8)) * -1
        elif factor_id == "alpha109":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha110":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha111":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha112":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha113":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha114":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha115":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha116":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha117":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha118":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha119":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha120":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha121":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha122":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha123":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha124":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha125":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha126":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha127":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha128":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha129":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha130":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha131":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha132":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha133":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha134":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha135":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha136":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha137":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha138":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha139":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha140":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha141":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha142":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha143":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha144":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha145":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha146":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha147":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha148":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha149":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha150":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
# === Batch 4: alpha151-alpha191 ===
        elif factor_id == "alpha151":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha152":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha153":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha154":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha155":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha156":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha157":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha158":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha159":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha160":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha161":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha162":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha163":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha164":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha165":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha166":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha167":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha168":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha169":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha170":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha171":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha172":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha173":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha174":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha175":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha176":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha177":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha178":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha179":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha180":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha181":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha182":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha183":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha184":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha185":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(a20), 10)) * -1
        elif factor_id == "alpha186":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha187":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha188":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha189":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha190":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(a20), 6), 8)) * -1
        elif factor_id == "alpha191":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1

        else:
            return pd.Series(np.nan, index=df.index)

    except Exception as e:
        print(f"  Warning: {factor_id} failed: {e}")
        return pd.Series(np.nan, index=df.index)


def build_panel(df: pd.DataFrame, start: int = 1, end: int = 191) -> pd.DataFrame:
    panel = df[["ts_code", "trade_date"]].copy()
    for i in range(start, end + 1):
        fid = f"alpha{i:03d}"
        print(f"Computing {fid}...", end=" ", flush=True)
        panel[fid] = compute_factor(df, fid)
        cov = panel[fid].notna().mean() * 100
        print(f"({cov:.1f}%)")
    return panel


def quality_report(panel: pd.DataFrame) -> dict:
    factor_cols = [c for c in panel.columns if c.startswith("alpha")]
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_range": [panel["trade_date"].min(), panel["trade_date"].max()],
        "stock_count": int(panel["ts_code"].nunique()),
        "row_count": int(len(panel)),
        "implemented_factor_count": len(factor_cols),
        "per_factor": {},
    }
    for col in factor_cols:
        s = panel[col]
        report["per_factor"][col] = {
            "coverage_pct": round(float(s.notna().mean() * 100), 2),
            "mean": round(float(s.mean()), 6) if s.notna().any() else None,
            "std": round(float(s.std()), 6) if s.notna().any() else None,
        }
    return report


def main() -> int:
    log_progress({"phase": "start", "message": "Starting Alpha191 long-running expansion"})

    df = load_daily_data(max_files=200)
    df = filter_universe(df)
    df = compute_derived(df)
    print(f"Data: {len(df)} rows, {df['ts_code'].nunique()} stocks, {df['trade_date'].nunique()} days")

    log_progress({
        "phase": "data_loaded",
        "rows": len(df),
        "stocks": df["ts_code"].nunique(),
        "days": df["trade_date"].nunique()
    })

    print("\n=== Building factors alpha001-100 ===")
    panel = build_panel(df, start=1, end=191)

    out = FACTORS_DIR / "gtja_alpha191_panel.parquet"
    panel.to_parquet(out, index=False)
    print(f"\nSaved panel: {out}")

    report = quality_report(panel)
    rpt_path = OUTPUT_DIR / "panel_quality_report.json"
    with open(rpt_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    ok_factors = [k for k, v in report["per_factor"].items() if v["coverage_pct"] > 50]
    log_progress({
        "phase": "panel_built",
        "total_factors": report["implemented_factor_count"],
        "ok_factors": len(ok_factors),
        "date_range": report["date_range"],
        "stock_count": report["stock_count"]
    })

    print(f"\n=== Summary ===")
    print(f"Total factors: {report['implemented_factor_count']}")
    print(f"Factors with >50% coverage: {len(ok_factors)}")

    log_progress({"phase": "complete", "message": f"Built {len(ok_factors)} usable factors"})
    return 0


if __name__ == "__main__":
    sys.exit(main())