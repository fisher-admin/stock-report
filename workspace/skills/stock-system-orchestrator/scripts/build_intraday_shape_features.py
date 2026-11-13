#!/usr/bin/env python3
"""压缩 5 分钟分钟线数据为 per-(stock, day) 的“日内形态”特征表。

用途: 作为下游 setup-review 流水线(setup_gauntlet.py 之类)的条件特征库
(conditioning features)。

=============================================================================
反偷窥(anti-snooping)红线 —— 本脚本严禁做的事情:
=============================================================================
  - 严禁计算/合并/join/甚至查看任何未来收益、次日价格、"结果"列
    (例如 next_return / o2c / 隔夜跳空 / 次日开收盘价 等)。
  - 严禁按"预测力"或与任何未来结果的相关性去筛选/排序特征——那是下游
    setup_gauntlet 的工作，不是本脚本的工作。

本脚本是纯粹的特征工程(feature engineering)任务: 只用"当天收盘前就已知"的
分钟线数据，压缩成 8 个日内形态特征。输出只包含 ts_code, trade_date 和这 8 个
特征列，没有任何 return/outcome/label 列。

=============================================================================
输入
=============================================================================
目录: stock_data/01-root/minute_cache/
  - 800 个 parquet 文件，一只股票一个，文件名形如
    minute_5min_000004_SZ.parquet (ts_code 从文件名派生: 000004_SZ -> 000004.SZ)
  - 列: ts_code(str), trade_time(str, 如 "2025-10-29 14:00:00" —— 注意不是
    pandas datetime dtype，需要自己 parse), close/open/high/low(float64),
    vol(int64), amount(float64)
  - 数据已按 trade_time 升序排列
  - 经验验证(见下方"时间分桶选择"注释): 正常交易日 49 根 bar/天
    (09:30~11:30 共 25 根 + 13:05~15:00 共 24 根); 部分股票在缓存窗口的
    首/尾或停牌等原因下只有残缺天(观测到最少 13 根)，这些残缺天不丢弃，
    用 n_bars 列标记数据质量，特征算不出来就是 NaN。

时间分桶选择(经验验证细节，见脚本注释 TIME BUCKET NOTES):
  用 pd.read_parquet 验证过 000004.SZ 全部时间戳的 HH:MM 分布:
  正常日 49 个时间点为 09:30,09:35,...,11:30 (25个，含 11:30 收盘前最后一根)
  以及 13:05,13:10,...,15:00 (24个，13:00 这个时间戳不存在，午后第一根 bar
  标记为 13:05)。因此本脚本按“时间字符串比较”而非位置索引来分桶，对残缺天
  也稳健。

=============================================================================
Holdout 排除(防御性，当前应为 no-op)
=============================================================================
minute_cache/minute_holdout_stocks.json 里有一份 ~800 个 ts_code 的holdout清单，
永远不能碰。已验证当前 minute_cache/ 目录下的800个文件与holdout清单零重合，
所以理论上今天跑不会跳过任何文件。但脚本仍然强制加载并检查，防止未来目录被
污染，并打印实际跳过了多少个。

=============================================================================
输出
=============================================================================
stock_data/03-working/intraday_shape_features.parquet
列: ts_code, trade_date, am_vol_share, pm_vol_share, vwap_slope,
    close_pos_intraday, late_strength, vol_concentration, path_smoothness,
    n_bars

trade_date 选择: 统一存成 'YYYY-MM-DD' 字符串(不是完整 datetime)，全脚本一致。
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
MINUTE_DIR = WORKSPACE / "stock_data" / "01-root" / "minute_cache"
HOLDOUT_STOCKS_PATH = MINUTE_DIR / "minute_holdout_stocks.json"
OUTPUT_DIR = WORKSPACE / "stock_data" / "03-working"
OUTPUT_PATH = OUTPUT_DIR / "intraday_shape_features.parquet"

# --holdout 模式(第六道闸/holdout复现专用): 读独立的 holdout 分钟缓存目录
# (由 ingest_minute_data.py --holdout 写入), 写独立的输出文件, 绝不覆盖/触碰
# 上面两个研究样本用的路径常量。
MINUTE_DIR_HOLDOUT = WORKSPACE / "stock_data" / "01-root" / "minute_cache_holdout"
OUTPUT_PATH_HOLDOUT = OUTPUT_DIR / "intraday_shape_features_holdout.parquet"

PROGRESS_EVERY = 50
EPS = 1e-8

# 残缺天判定阈值(仅用于报告/质量标记，不用于丢弃行)
MIN_NORMAL_BARS = 40

# --- 时间分桶边界(字符串 "HH:MM" 比较, 经验验证见上方 docstring) ---
AM_OPEN_START = "09:30"    # 开盘后 30 分钟窗口起点 [09:30, 10:00)
AM_OPEN_END = "10:00"      # 不含 10:00 本身(exclusive upper bound)
PM_CLOSE_START = "14:30"   # 收盘前 30 分钟窗口 [14:30, 15:00] (含两端)
PM_CLOSE_END = "15:00"
MORNING_SESSION_START = "09:30"
MORNING_SESSION_END = "11:30"     # inclusive
AFTERNOON_SESSION_START = "13:00"  # inclusive (13:05 是午后第一根, 13:00 本身不存在但用 >= 更稳健)
AFTERNOON_SESSION_END = "15:00"    # inclusive
LATE_BAR_TIME = "14:30"
LAST_BAR_TIME = "15:00"


def load_holdout_ts_codes() -> set[str]:
    """加载 holdout ts_code 清单(XXXXXX.SZ/.SH/.BJ 格式)。防御性排除用。"""
    if not HOLDOUT_STOCKS_PATH.exists():
        print(f"[WARN] holdout 文件不存在: {HOLDOUT_STOCKS_PATH}，跳过 holdout 排除(视为空集)")
        return set()
    try:
        data = json.loads(HOLDOUT_STOCKS_PATH.read_text(encoding="utf-8"))
        codes = set(data.get("ts_codes") or [])
        print(f"已加载 holdout 清单: {len(codes)} 个 ts_code(永不触碰)")
        return codes
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 读取 holdout 文件失败: {type(e).__name__} {e}，视为空集")
        return set()


def derive_ts_code_from_filename(path: Path) -> str | None:
    """minute_5min_000004_SZ.parquet -> 000004.SZ"""
    stem = path.stem  # minute_5min_000004_SZ
    prefix = "minute_5min_"
    if not stem.startswith(prefix):
        return None
    body = stem[len(prefix):]  # 000004_SZ
    if "_" not in body:
        return None
    code, exch = body.rsplit("_", 1)
    return f"{code}.{exch}"


def safe_div(numerator: np.ndarray | float, denominator: np.ndarray | float) -> np.ndarray | float:
    """除法保护: 分母(取绝对值)小于 EPS 时返回 NaN，而不是 inf。"""
    denom = np.asarray(denominator, dtype=float)
    num = np.asarray(numerator, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(np.abs(denom) < EPS, np.nan, num / denom)
    if result.shape == ():
        return float(result)
    return result


def compute_day_features(day_df: pd.DataFrame) -> dict:
    """对单只股票单个交易日的分钟 bar 计算 8 个形态特征。

    day_df: 该交易日的全部 bar，已含 'time_str' (HH:MM 字符串) 列，
            按 trade_time 升序排列。
    """
    n_bars = len(day_df)

    amount = day_df["amount"].to_numpy(dtype=float)
    vol = day_df["vol"].to_numpy(dtype=float)
    close = day_df["close"].to_numpy(dtype=float)
    high = day_df["high"].to_numpy(dtype=float)
    low = day_df["low"].to_numpy(dtype=float)
    time_str = day_df["time_str"].to_numpy()

    total_amount = np.nansum(amount)

    # --- 1. am_vol_share: 开盘后30分钟 [09:30, 10:00) 成交额占比 ---
    am_mask = (time_str >= AM_OPEN_START) & (time_str < AM_OPEN_END)
    am_amount = np.nansum(amount[am_mask]) if am_mask.any() else np.nan
    am_vol_share = safe_div(am_amount, total_amount) if am_mask.any() else np.nan

    # --- 2. pm_vol_share: 收盘前30分钟 [14:30, 15:00] 成交额占比 ---
    pm_mask = (time_str >= PM_CLOSE_START) & (time_str <= PM_CLOSE_END)
    pm_amount = np.nansum(amount[pm_mask]) if pm_mask.any() else np.nan
    pm_vol_share = safe_div(pm_amount, total_amount) if pm_mask.any() else np.nan

    # --- 3. vwap_slope: 下午 vwap / 上午 vwap - 1 ---
    morning_mask = (time_str >= MORNING_SESSION_START) & (time_str <= MORNING_SESSION_END)
    afternoon_mask = (time_str >= AFTERNOON_SESSION_START) & (time_str <= AFTERNOON_SESSION_END)
    if morning_mask.any() and afternoon_mask.any():
        morning_amount_sum = np.nansum(amount[morning_mask])
        morning_vol_sum = np.nansum(vol[morning_mask])
        afternoon_amount_sum = np.nansum(amount[afternoon_mask])
        afternoon_vol_sum = np.nansum(vol[afternoon_mask])
        morning_vwap = safe_div(morning_amount_sum, morning_vol_sum)
        afternoon_vwap = safe_div(afternoon_amount_sum, afternoon_vol_sum)
        if pd.notna(morning_vwap) and pd.notna(afternoon_vwap) and abs(morning_vwap) >= EPS:
            vwap_slope = afternoon_vwap / morning_vwap - 1.0
        else:
            vwap_slope = np.nan
    else:
        vwap_slope = np.nan

    # --- 4. close_pos_intraday: (day_close - day_low) / (day_high - day_low + eps) ---
    day_close = close[-1]  # 该日最后一根 bar 的收盘价(按 trade_time 升序，最后一行即最晚)
    day_high = np.nanmax(high) if n_bars else np.nan
    day_low = np.nanmin(low) if n_bars else np.nan
    day_range = day_high - day_low
    if pd.notna(day_range) and day_range >= EPS:
        close_pos_intraday = (day_close - day_low) / (day_range + EPS)
    else:
        close_pos_intraday = np.nan

    # --- 5. late_strength: 14:30 bar 收盘 -> 15:00(最后)bar 收盘 的收益 ---
    late_bar_rows = day_df[day_df["time_str"] == LATE_BAR_TIME]
    last_bar_rows = day_df[day_df["time_str"] == LAST_BAR_TIME]
    if len(late_bar_rows) and len(last_bar_rows):
        late_close = float(late_bar_rows["close"].iloc[-1])
        final_close = float(last_bar_rows["close"].iloc[-1])
        if abs(late_close) >= EPS:
            late_strength = final_close / late_close - 1.0
        else:
            late_strength = np.nan
    else:
        # 14:30 或 15:00 bar 缺失(残缺天) -> NaN，不崩溃
        late_strength = np.nan

    # --- 6. vol_concentration: 当日各 bar 成交额占比的 HHI ---
    if total_amount is not None and abs(total_amount) >= EPS and n_bars > 0:
        shares = amount / total_amount
        # 负值或 NaN 成交额(不该出现，但防御性地把 NaN share 当 0 处理)
        shares = np.nan_to_num(shares, nan=0.0)
        vol_concentration = float(np.sum(shares ** 2))
    else:
        vol_concentration = np.nan

    # --- 7. path_smoothness: 连续 5min close-to-close 收益的符号翻转次数 / bar 数 ---
    # 精确为零的收益处理: 视为"无符号"，沿用上一个非零符号(carry-forward)，
    # 既不计入翻转也不重置为待定; 若开头连续多个 0，则在遇到第一个非零符号
    # 之前不产生任何"翻转"判定(没有前序符号可比较)。
    if n_bars >= 2:
        rets = np.diff(close) / np.where(np.abs(close[:-1]) < EPS, np.nan, close[:-1])
        signs = np.sign(rets)
        flips = 0
        prev_sign = 0.0
        compared = 0
        for s in signs:
            if np.isnan(s):
                continue
            if s == 0.0:
                # 零收益: 不算翻转，也不更新 prev_sign(carry-forward 之前的符号)
                continue
            if prev_sign != 0.0:
                compared += 1
                if s != prev_sign:
                    flips += 1
            prev_sign = s
        path_smoothness = flips / n_bars if n_bars > 0 else np.nan
    else:
        path_smoothness = np.nan

    return {
        "am_vol_share": am_vol_share,
        "pm_vol_share": pm_vol_share,
        "vwap_slope": vwap_slope,
        "close_pos_intraday": close_pos_intraday,
        "late_strength": late_strength,
        "vol_concentration": vol_concentration,
        "path_smoothness": path_smoothness,
        "n_bars": int(n_bars),
    }


def process_one_file(path: Path) -> pd.DataFrame | None:
    """处理单只股票的 parquet 文件，返回该股票的 per-day 特征表(或 None 出错时)。"""
    try:
        df = pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] 读取失败，跳过: {path.name}: {type(e).__name__} {e}")
        return None

    if df.empty:
        return None

    required_cols = {"ts_code", "trade_time", "close", "open", "high", "low", "vol", "amount"}
    missing = required_cols - set(df.columns)
    if missing:
        print(f"  [WARN] 缺少必需列 {missing}，跳过: {path.name}")
        return None

    try:
        dt = pd.to_datetime(df["trade_time"], errors="coerce")
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] trade_time 解析失败，跳过: {path.name}: {type(e).__name__} {e}")
        return None

    bad_dt = dt.isna()
    if bad_dt.any():
        df = df.loc[~bad_dt].copy()
        dt = dt.loc[~bad_dt]
    if df.empty:
        return None

    df = df.copy()
    df["_dt"] = dt
    df["trade_date"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df["time_str"] = df["_dt"].dt.strftime("%H:%M")
    df = df.sort_values("_dt")

    ts_code = df["ts_code"].iloc[0]

    rows = []
    for trade_date, day_df in df.groupby("trade_date", sort=True):
        feats = compute_day_features(day_df)
        feats["ts_code"] = ts_code
        feats["trade_date"] = trade_date
        rows.append(feats)

    if not rows:
        return None

    out = pd.DataFrame(rows)
    cols = [
        "ts_code",
        "trade_date",
        "am_vol_share",
        "pm_vol_share",
        "vwap_slope",
        "close_pos_intraday",
        "late_strength",
        "vol_concentration",
        "path_smoothness",
        "n_bars",
    ]
    return out[cols]


def main() -> int:
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true",
                     help="第六道闸专用: 读 minute_cache_holdout/ (独立holdout分钟缓存), "
                          "写 intraday_shape_features_holdout.parquet, 绝不覆盖研究特征文件")
    args = ap.parse_args()

    holdout_mode = args.holdout
    input_dir = MINUTE_DIR_HOLDOUT if holdout_mode else MINUTE_DIR
    output_path = OUTPUT_PATH_HOLDOUT if holdout_mode else OUTPUT_PATH

    if not input_dir.exists():
        print(f"[FATAL] 输入目录不存在: {input_dir}")
        return 1

    holdout_codes = load_holdout_ts_codes()

    files = sorted(input_dir.glob("minute_5min_*.parquet"))
    print(f"模式: {'holdout(第六道闸)' if holdout_mode else '研究样本'}  输入目录={input_dir}  输出={output_path}")
    print(f"发现 {len(files)} 个分钟线缓存文件，开始逐文件处理(不会一次性拼接进内存)...")

    skipped_holdout = 0
    skipped_non_holdout = 0
    skipped_error = 0
    processed = 0
    per_stock_frames: list[pd.DataFrame] = []

    for i, path in enumerate(files):
        ts_code = derive_ts_code_from_filename(path)

        if holdout_mode:
            # 镜像防御: holdout模式下, 任何不在holdout清单里的文件(=可能是研究样本混入或
            # 未知代码)一律剔除不处理, 确保holdout复现绝对只用holdout 800只的数据。
            if ts_code is not None and ts_code not in holdout_codes:
                skipped_non_holdout += 1
                continue
        else:
            # 研究样本模式: 原有防御性排除逻辑不变(holdout清单里的文件永不处理)。
            if ts_code is not None and ts_code in holdout_codes:
                skipped_holdout += 1
                continue

        result = process_one_file(path)
        if result is None:
            skipped_error += 1
            continue

        per_stock_frames.append(result)
        processed += 1

        if (i + 1) % PROGRESS_EVERY == 0:
            print(f"  进度 {i + 1}/{len(files)}  已成功处理 {processed}  出错跳过 {skipped_error}  "
                  f"holdout跳过 {skipped_holdout}  非holdout跳过 {skipped_non_holdout}")

    if holdout_mode:
        print(f"\n文件遍历完成: 总数 {len(files)}, 成功处理 {processed}, 出错跳过 {skipped_error}, "
              f"非holdout防御性跳过 {skipped_non_holdout}(预期为0)")
    else:
        print(f"\n文件遍历完成: 总数 {len(files)}, 成功处理 {processed}, 出错跳过 {skipped_error}, holdout防御性跳过 {skipped_holdout}(预期为0)")

    if not per_stock_frames:
        print("[FATAL] 没有任何可用数据，未写出输出文件。")
        return 1

    print("正在合并所有股票的特征表...")
    final_df = pd.concat(per_stock_frames, ignore_index=True)
    final_df = final_df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".parquet.tmp")
    final_df.to_parquet(tmp_path, index=False)
    tmp_path.replace(output_path)

    print(f"\n输出完成: {output_path}")
    print(f"总行数 = {len(final_df)}, 唯一 ts_code = {final_df['ts_code'].nunique()}, 唯一 trade_date = {final_df['trade_date'].nunique()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
