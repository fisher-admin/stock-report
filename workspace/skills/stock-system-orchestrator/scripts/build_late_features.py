#!/usr/bin/env python3
"""late_features.parquet 构建脚本 —— "T日14:45可知"的尾盘特征库(研究800样本)。

=============================================================================
冻结定义来源(逐字实现,禁止任何定义变更)
=============================================================================
本脚本的字段定义由监督者(fable-supervisor)冻结在预注册契约里:
  factor_factory/config/setup_registry.json
    -> setups[].id == "S7_late_oversold_absorb"  (features_at_decision / trade / event_universe)
    -> setups[].id == "S8_first_board_tail_absorb" (event_anchor_hardened / features_at_decision / trade)
本脚本只做特征工程,不做任何格点判定/闯关,只是把两个剧本共用的
"late_features.parquet(14:45可知,待建)" 数据依赖建出来。

=============================================================================
反偷窥(anti-snooping)红线 —— 本脚本严禁做的事情:
=============================================================================
  - 严禁计算/合并/join 任何未来收益、次日/次次日价格、"结果"列
    (next_return / o2c / 隔夜跳空 / 次日开收盘价 等一律不产出)。
  - entry_vwap_1445_1455 / morning_vwap_0935_0945 / close_px 是"执行价"字段
    (明确按契约标注为执行价,非收益),仅供闯关引擎在计算收益时使用,本脚本
    本身不据此计算任何收益/label。
  - 严禁按预测力筛选/排序特征——那是下游 setup_gauntlet 的工作。

=============================================================================
bar 时间戳语义(右标签,已审计,写死 —— 参考 calibrate_slippage_late.py B2 标定)
=============================================================================
5min bar 为"右标签"(标签=窗口终点):bar[14:45:00] 覆盖 14:40:00-14:45:00 的
连续竞价,在 14:45 已完全实现/可知。故:
  decision_px            = bar[14:45:00].close                (14:45 已知)
  entry_vwap_1445_1455   = bars[14:50:00, 14:55:00] 的 sum(amount)/sum(vol)
                            (覆盖 14:45-14:55,决策之后的执行窗口,执行价字段)
  morning_vwap_0935_0945 = bars[09:35:00, 09:40:00, 09:45:00] 的
                            sum(amount)/sum(vol) (覆盖 09:30-09:45,执行价字段,
                            供作"前一日尾盘入场"的次晨出场价)
所有 "<=14:45" 的判断口径一律按右标签(即 time_str <= "14:45" 的 bar 集合)。

=============================================================================
输入
=============================================================================
分钟: stock_data/01-root/minute_cache/minute_5min_XXXXXX_EX.parquet (研究800,
      与 build_intraday_shape_features.py 完全同源同格式)
日频面板: factor_factory/data/factors/greenfield_multifactor_panel_full.parquet
  用于: pre_close/open/close/high/pct_chg(gap_sign, limit_up_px, is_first_board_T
  的"T日/T-1日"级判定,均为全天口径,不是<=14:45截断口径) 与 universe_flag(仅
  透传,不在本脚本过滤事件域——事件域过滤是闯关脚本的工作)。
  注意: panel 对科创板(688)/北交所(.BJ)以及少量退市/改代码股票没有覆盖,
  这些股票的 panel 派生字段(gap_sign/limit_up_px/is_first_board_T/pre_close
  相关列)将为 NaN,如实记录,不影响纯分钟派生字段的计算。
上市日期: stock_data/01-root/stock_list.csv 的 list_date 列(缺失时 —— 仅
  688 科创板前 90 只不在该名单 —— 退化为"面板该股首次出现日"近似,标注口径)。
交易日历: 用 panel 全体 trade_date 的去重并集作为 A 股交易日历代理(用于计算
  "上市满 N 个交易日"与"复牌首日"的日历前一交易日)。

=============================================================================
输出
=============================================================================
stock_data/03-working/late_features.parquet
一行 = (ts_code, trade_date),列见契约字段列表(见 docstring 底部字段表)。
不含任何 return/outcome/label 列。

trade_date 格式: 'YYYYMMDD' 字符串,与 panel_full 对齐(不同于
build_intraday_shape_features.py 的 'YYYY-MM-DD',那是历史遗留,本脚本对齐
panel 更方便下游 merge)。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

VWAP_SANITY_COUNTS = {"repaired_x100": 0, "repaired_div100": 0, "nulled": 0}

WORKSPACE = Path("/Users/fisher/.openclaw/workspace")
MINUTE_DIR = WORKSPACE / "stock_data" / "01-root" / "minute_cache"
HOLDOUT_STOCKS_PATH = MINUTE_DIR / "minute_holdout_stocks.json"
PANEL_PATH = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet"
STOCK_LIST_PATH = WORKSPACE / "stock_data" / "01-root" / "stock_list.csv"
OUTPUT_DIR = WORKSPACE / "stock_data" / "03-working"
OUTPUT_PATH = OUTPUT_DIR / "late_features.parquet"

EPS = 1e-8
PROGRESS_EVERY = 50

# --- 时间字符串边界(右标签, "HH:MM"字符串比较, 与 calibrate_slippage_late.py 同口径) ---
DECISION_TIME = "14:45"
TAIL_START = "14:00"     # tail_vol_share_1400_1445 窗口起点(含)
ENTRY_WINDOW = ("14:50", "14:55")      # 覆盖 14:45-14:55, 执行价窗口
MORNING_WINDOW = ("09:35", "09:40", "09:45")  # 覆盖 09:30-09:45, 执行价窗口
FIRST_BAR_OF_DAY = "09:30"

# --- 板别涨停系数(与 calibrate_slippage_late.py limit_rate_for 完全同口径) ---
def limit_rate_for(code: str) -> tuple[float, str]:
    """返回 (涨停系数, 板别标签 bd)。
    主板(60/00/001/003开头)=0.10; 创业板(300/301)=0.20;
    科创板(688)=0.20; 北交所(.BJ / 92x等)=0.30。
    契约原文: "主板(60/00开头)1.1/创业板(300)1.2(688/BJ不在样本用途内但如出现
    照1.2/1.3不重要,标记板别bd字段)"——本实现按此外推,688 归入 1.2(20%)档,
    BJ 归入 1.3(30%)档,仅用于标记,不参与 S7/S8 事件域判定(它们已被
    event_universe 显式剔除 688/.BJ)。
    """
    if code.startswith("688"):
        return 0.20, "科创板"
    if code.startswith("300") or code.startswith("301"):
        return 0.20, "创业板"
    if code.endswith(".BJ") or code[:3] in (
        "920", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839"
    ):
        return 0.30, "北交所"
    return 0.10, "主板"


def derive_ts_code_from_filename(path: Path) -> str | None:
    """minute_5min_000004_SZ.parquet -> 000004.SZ"""
    stem = path.stem
    prefix = "minute_5min_"
    if not stem.startswith(prefix):
        return None
    body = stem[len(prefix):]
    if "_" not in body:
        return None
    code, exch = body.rsplit("_", 1)
    return f"{code}.{exch}"


def load_holdout_ts_codes() -> set[str]:
    if not HOLDOUT_STOCKS_PATH.exists():
        print(f"[WARN] holdout 文件不存在: {HOLDOUT_STOCKS_PATH}，视为空集")
        return set()
    try:
        import json
        data = json.loads(HOLDOUT_STOCKS_PATH.read_text(encoding="utf-8"))
        codes = set(data.get("ts_codes") or [])
        print(f"已加载 holdout 清单: {len(codes)} 个 ts_code(永不触碰,minute_cache_holdout 严禁读)")
        return codes
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 读取 holdout 文件失败: {type(e).__name__} {e}，视为空集")
        return set()


def safe_div(numerator, denominator):
    denom = np.asarray(denominator, dtype=float)
    num = np.asarray(numerator, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(np.abs(denom) < EPS, np.nan, num / denom)
    if result.shape == ():
        return float(result)
    return result


def pivot_time_field(df: pd.DataFrame, times: list[str], value_col: str) -> pd.DataFrame:
    """把 (date, time_str) -> value_col 转成 date 为行、time 为列的宽表,
    缺失时间点补 NaN 列,方便向量化按时刻取值。"""
    sub = df[df["time_str"].isin(times)]
    piv = sub.pivot_table(index="trade_date", columns="time_str", values=value_col, aggfunc="last")
    for t in times:
        if t not in piv.columns:
            piv[t] = np.nan
    return piv[times]


def process_one_file(path: Path) -> pd.DataFrame | None:
    """处理单只股票的分钟 parquet,向量化(按天 pivot,无逐日 Python 循环用于
    可批量算的字段;stab_minutes 需要"最后一次创日内新低"的逐日归约,用
    groupby+cummin 向量化实现,不用逐行 Python for 循环)。"""
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

    dt = pd.to_datetime(df["trade_time"], errors="coerce")
    bad_dt = dt.isna()
    if bad_dt.any():
        df = df.loc[~bad_dt].copy()
        dt = dt.loc[~bad_dt]
    if df.empty:
        return None

    df = df.copy()
    df["_dt"] = dt
    df["trade_date"] = df["_dt"].dt.strftime("%Y%m%d")
    df["time_str"] = df["_dt"].dt.strftime("%H:%M")
    df = df.sort_values("_dt").reset_index(drop=True)

    ts_code = df["ts_code"].iloc[0]

    # ---- <=14:45 子集(右标签口径:decision-time 全部计算只用这个子集) ----
    d1445 = df[df["time_str"] <= DECISION_TIME].copy()
    if d1445.empty:
        return None

    # n_bars_1445
    n_bars_1445 = d1445.groupby("trade_date").size()

    # decision_px: bar[14:45].close (若当日无14:45 bar则该日为NaN,不用<=14:45最后一根替代,
    # 因为契约字段名为"bar[14:45]收价"是明确锚定该时刻,数据残缺时应如实NaN而非静默替代)
    exact_1445 = df[df["time_str"] == DECISION_TIME]
    decision_px = exact_1445.groupby("trade_date")["close"].last()

    # day_high_1445: max(high) over bars<=14:45
    day_high_1445 = d1445.groupby("trade_date")["high"].max()

    # vwap_1445: sum(amount)/sum(vol) over bars<=14:45
    amt_sum_1445 = d1445.groupby("trade_date")["amount"].sum()
    vol_sum_1445 = d1445.groupby("trade_date")["vol"].sum()
    vwap_1445 = safe_div(amt_sum_1445.to_numpy(), vol_sum_1445.to_numpy())
    vwap_1445 = pd.Series(vwap_1445, index=amt_sum_1445.index)

    # tail_vol_share_1400_1445: vol(14:00-14:45)/vol(<=14:45)  [14:00,14:45] 含两端(右标签下即
    # time_str in [14:00,14:05,...,14:45])
    tail_mask = (d1445["time_str"] >= TAIL_START) & (d1445["time_str"] <= DECISION_TIME)
    tail_vol_sum = d1445.loc[tail_mask].groupby("trade_date")["vol"].sum()
    tail_vol_sum = tail_vol_sum.reindex(vol_sum_1445.index, fill_value=0.0)
    tail_vol_share_1400_1445 = safe_div(tail_vol_sum.to_numpy(), vol_sum_1445.to_numpy())
    tail_vol_share_1400_1445 = pd.Series(tail_vol_share_1400_1445, index=vol_sum_1445.index)

    # ---- stab_minutes: 14:45 距"bar low 创当日新低"(限<=14:45子集)的最后一次时刻的分钟差 ----
    # 向量化: 按 trade_date 分组, 对 low 做累计最小值(cummin), 找到"low == 当前为止累计最小值"
    # 的行(即每次刷新新低的 bar), 取每个 trade_date 内最后一次刷新新低的 bar 的 time_str,
    # 换算为该 bar 的右标签时刻(即该 bar 自身的 time_str, 因为右标签=bar自身即完结时刻),
    # 再与 "14:45" 做分钟差。若首 bar(09:30)即为最低(即从未被刷新过第二次即最初值本身是
    # cummin 的起点), 则从该首 bar 起算(与"从最后一次创新低时刻"逻辑自然一致,因为首bar
    # 本身也是"创新低"事件——它建立了初始最小值)。
    d1445 = d1445.sort_values(["trade_date", "_dt"])
    d1445["_cummin_low"] = d1445.groupby("trade_date")["low"].cummin()
    # 一个 bar "创当日新低" <=> 该 bar 的 low 等于截至该 bar 为止的累计最小值 AND
    # (是该分组第一行 OR 严格小于前一行的累计最小值)。用 shift 比较避免并列错记多次。
    prev_cummin = d1445.groupby("trade_date")["_cummin_low"].shift(1)
    is_new_low = (d1445["_cummin_low"] < prev_cummin) | prev_cummin.isna()
    new_low_bars = d1445.loc[is_new_low, ["trade_date", "_dt"]]
    last_new_low_dt = new_low_bars.groupby("trade_date")["_dt"].max()
    # 14:45 该行的实际时间戳(用于分钟差计算,按该 trade_date 的实际 _dt)
    decision_dt = exact_1445.groupby("trade_date")["_dt"].last()
    decision_dt_aligned = decision_dt.reindex(last_new_low_dt.index)
    stab_minutes = (decision_dt_aligned - last_new_low_dt).dt.total_seconds() / 60.0

    # ---- 执行价字段 ----
    entry_amt = pivot_time_field(df, list(ENTRY_WINDOW), "amount")
    entry_vol = pivot_time_field(df, list(ENTRY_WINDOW), "vol")
    entry_amt_sum = entry_amt.sum(axis=1, skipna=True)
    entry_vol_sum = entry_vol.sum(axis=1, skipna=True)
    # 若两个窗口 bar 都缺失(sum=0 且原始全 NaN),应为 NaN 而非 0
    entry_has_any = entry_amt.notna().any(axis=1)
    entry_vwap_1445_1455 = safe_div(entry_amt_sum.to_numpy(), entry_vol_sum.to_numpy())
    entry_vwap_1445_1455 = pd.Series(entry_vwap_1445_1455, index=entry_amt.index)
    entry_vwap_1445_1455 = entry_vwap_1445_1455.where(entry_has_any, np.nan)

    morning_amt = pivot_time_field(df, list(MORNING_WINDOW), "amount")
    morning_vol = pivot_time_field(df, list(MORNING_WINDOW), "vol")
    morning_amt_sum = morning_amt.sum(axis=1, skipna=True)
    morning_vol_sum = morning_vol.sum(axis=1, skipna=True)
    morning_has_any = morning_amt.notna().any(axis=1)
    morning_vwap_0935_0945 = safe_div(morning_amt_sum.to_numpy(), morning_vol_sum.to_numpy())
    morning_vwap_0935_0945 = pd.Series(morning_vwap_0935_0945, index=morning_amt.index)
    morning_vwap_0935_0945 = morning_vwap_0935_0945.where(morning_has_any, np.nan)

    # close_px: 当日(全天,不限<=14:45)最后一根 bar 的 close
    close_px = df.groupby("trade_date")["close"].last()

    # ---- VWAP 单位一致性校验与自愈(20260706 监督者补,事故: 2024-04~06 七个交易日的
    # 分钟摄取批次 amount/vol 单位错位, VWAP 系统性=真实价格的 1/100, 336只x1884(股,日),
    # 曾把 S7 事件毛收益污染成 +101 倍假 alpha)。
    # 物理边界: VWAP 必须落在当日 bar 价格区间 [min(low)*0.995, max(high)*1.005] 内;
    # 越界时: x100 后入界 → 判定单位错位, 自愈(计数); /100 后入界 → 同理; 均不入界 → NaN(计数)。
    day_lo = df.groupby("trade_date")["low"].min()
    day_hi = df.groupby("trade_date")["high"].max()

    def _sanitize_vwap(vw: pd.Series, tag: str) -> pd.Series:
        lo = day_lo.reindex(vw.index) * 0.995
        hi = day_hi.reindex(vw.index) * 1.005
        ok = (vw >= lo) & (vw <= hi)
        fix_up = (~ok) & ((vw * 100 >= lo) & (vw * 100 <= hi))
        fix_dn = (~ok) & (~fix_up) & ((vw / 100 >= lo) & (vw / 100 <= hi))
        bad = (~ok) & (~fix_up) & (~fix_dn) & vw.notna() & lo.notna()
        out_vw = vw.copy()
        out_vw[fix_up] = vw[fix_up] * 100
        out_vw[fix_dn] = vw[fix_dn] / 100
        out_vw[bad] = np.nan
        VWAP_SANITY_COUNTS["repaired_x100"] += int(fix_up.sum())
        VWAP_SANITY_COUNTS["repaired_div100"] += int(fix_dn.sum())
        VWAP_SANITY_COUNTS["nulled"] += int(bad.sum())
        return out_vw

    vwap_1445 = _sanitize_vwap(vwap_1445, "vwap_1445")
    entry_vwap_1445_1455 = _sanitize_vwap(entry_vwap_1445_1455, "entry")
    morning_vwap_0935_0945 = _sanitize_vwap(morning_vwap_0935_0945, "morning")

    # ---- 组装该股票的输出表 ----
    all_dates = sorted(set(n_bars_1445.index) | set(close_px.index))
    out = pd.DataFrame(index=pd.Index(all_dates, name="trade_date"))
    out["n_bars_1445"] = n_bars_1445.reindex(out.index)
    out["decision_px"] = decision_px.reindex(out.index)
    out["day_high_1445"] = day_high_1445.reindex(out.index)
    out["drawdown_from_high"] = safe_div(out["decision_px"].to_numpy(), out["day_high_1445"].to_numpy()) - 1.0
    out["vwap_1445"] = vwap_1445.reindex(out.index)
    out["vwap_dev_1445"] = safe_div(out["decision_px"].to_numpy(), out["vwap_1445"].to_numpy()) - 1.0
    out["stab_minutes"] = stab_minutes.reindex(out.index)
    out["tail_vol_share_1400_1445"] = tail_vol_share_1400_1445.reindex(out.index)
    out["entry_vwap_1445_1455"] = entry_vwap_1445_1455.reindex(out.index)
    out["morning_vwap_0935_0945"] = morning_vwap_0935_0945.reindex(out.index)
    out["close_px"] = close_px.reindex(out.index)

    out = out.reset_index()
    out.insert(0, "ts_code", ts_code)
    return out


def build_trading_calendar(panel: pd.DataFrame) -> np.ndarray:
    """A股交易日历代理: panel 全体 trade_date 去重排序(YYYYMMDD字符串)。"""
    cal = np.array(sorted(panel["trade_date"].unique()))
    return cal


def compute_listing_trading_days(
    ts_codes: pd.Series, trade_dates: pd.Series, list_date_map: dict, calendar: np.ndarray,
    fallback_first_seen: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (trading_days_since_listing, used_fallback_flag) 数组,向量化(np.searchsorted)。
    trading_days_since_listing: T 在交易日历中的位置 - list_date 在交易日历中的位置
    (若 list_date 早于日历起点,则用日历起点代替——上市已久,不影响 >=60 判定)。
    """
    list_dates = ts_codes.map(list_date_map)
    used_fallback = list_dates.isna().to_numpy()
    # fallback: 该股票在面板中首次出现日 (若都没有则 NaN, 该行 is_first_board_T 判 NaN)
    fb = ts_codes.map(fallback_first_seen)
    list_dates = list_dates.where(~list_dates.isna(), fb)

    cal_pos = np.searchsorted(calendar, calendar)  # 0..len-1 identity, kept for clarity
    def pos_of(date_arr):
        # date_arr: array of 'YYYYMMDD' strings (or NaN)
        pos = np.searchsorted(calendar, date_arr)
        pos = np.clip(pos, 0, len(calendar) - 1)
        return pos

    valid = ~list_dates.isna()
    list_pos = np.full(len(list_dates), np.nan)
    list_pos[valid.to_numpy()] = pos_of(list_dates[valid].to_numpy())

    t_pos = pos_of(trade_dates.to_numpy())

    trading_days = t_pos - list_pos + 1  # +1: list_date 当天算第1个交易日
    trading_days = np.where(valid.to_numpy(), trading_days, np.nan)
    return trading_days, used_fallback


def main() -> int:
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    if not MINUTE_DIR.exists():
        print(f"[FATAL] 输入目录不存在: {MINUTE_DIR}")
        return 1

    holdout_codes = load_holdout_ts_codes()

    files = sorted(MINUTE_DIR.glob("minute_5min_*.parquet"))
    print(f"发现 {len(files)} 个分钟线缓存文件(minute_cache/, 研究800样本)")

    skipped_holdout = 0
    skipped_error = 0
    processed = 0
    per_stock_frames: list[pd.DataFrame] = []

    for i, path in enumerate(files):
        ts_code = derive_ts_code_from_filename(path)
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
            print(f"  进度 {i + 1}/{len(files)}  已成功处理 {processed}  出错跳过 {skipped_error}  holdout跳过 {skipped_holdout}")

    print(f"\n文件遍历完成: 总数 {len(files)}, 成功处理 {processed}, 出错跳过 {skipped_error}, holdout防御性跳过 {skipped_holdout}(预期为0)")

    if not per_stock_frames:
        print("[FATAL] 没有任何可用数据，未写出输出文件。")
        return 1

    print("正在合并所有股票的分钟派生特征...")
    feat = pd.concat(per_stock_frames, ignore_index=True)
    feat = feat.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    print(f"分钟派生特征行数(合并后): {len(feat):,}")

    # ---------------------------------------------------------------
    # 板别 + 涨停价 + 未封板判定 (纯 ts_code 前缀规则,不依赖面板——因面板对
    # 688/BJ 完全无覆盖,若依赖面板 join 这两板块将全部丢字段)
    # ---------------------------------------------------------------
    board_info = feat["ts_code"].drop_duplicates().apply(lambda c: pd.Series(limit_rate_for(c), index=["limit_rate", "bd"]))
    board_info["ts_code"] = feat["ts_code"].drop_duplicates().to_numpy()
    board_info = board_info.set_index("ts_code")
    feat = feat.merge(board_info, on="ts_code", how="left")

    # ---------------------------------------------------------------
    # 面板 join: pre_close/open/close/high/pct_chg (全天口径,非<=14:45截断)
    # 用于 gap_sign / limit_up_px / is_first_board_T。688/.BJ/部分退市股票
    # 面板无覆盖 -> 这些字段 NaN,如实记录(不影响上面的分钟派生字段)。
    # ---------------------------------------------------------------
    print("读取日频面板(pre_close/open/close/high/pct_chg/universe_flag)...")
    panel = pd.read_parquet(
        PANEL_PATH,
        columns=["ts_code", "trade_date", "pre_close", "open", "close", "high", "pct_chg", "universe_flag"],
    )
    panel["trade_date"] = panel["trade_date"].astype(str)
    panel = panel.rename(columns={
        "open": "panel_open", "close": "panel_close", "high": "panel_high",
    })

    feat = feat.merge(
        panel[["ts_code", "trade_date", "pre_close", "panel_open", "panel_close", "panel_high", "pct_chg", "universe_flag"]],
        on=["ts_code", "trade_date"], how="left",
    )

    # limit_up_px = round(pre_close * (1+limit_rate), 2)
    feat["limit_up_px"] = np.round(feat["pre_close"] * (1.0 + feat["limit_rate"]), 2)
    # not_sealed_1445: decision_px < limit_up_px * 0.99
    not_sealed_1445 = (feat["decision_px"] < (feat["limit_up_px"] * 0.99)).astype(object)
    not_sealed_1445[feat["limit_up_px"].isna() | feat["decision_px"].isna()] = np.nan
    feat["not_sealed_1445"] = not_sealed_1445

    # gap_sign: sign(open/pre_close - 1), 披露轴 (用面板 open, 即当日开盘价,非分钟09:30 bar)
    gap = safe_div(feat["panel_open"].to_numpy(), feat["pre_close"].to_numpy()) - 1.0
    feat["gap_sign"] = np.sign(gap)

    # ---------------------------------------------------------------
    # is_first_board_T: 首板硬化判定(S8 event_anchor_hardened 口径,逐字)
    #   close>=high*0.999 且 (主板 pct_chg∈[9.7,10.3] 或 创业板∈[19.6,20.4])
    #   且 T-1 同口径非涨停 且 上市>=60交易日
    #   (全天口径: 用面板 panel_close/panel_high/pct_chg,不是<=14:45截断字段)
    #
    # 重要: T-1 必须是该股票【面板自身连续交易日序列】里的前一行,而不是我们
    # minute_cache 日期集合里的前一行——两者可能有缺口(面板对某些日期有记录而
    # minute_cache 当天缺失,或反之),用 feat(分钟日历)做 shift 会错位地跳过
    # 真实的 T-1。故这里先在 panel 自身(其原始行序=该股实际连续交易日)上算好
    # limit_up_today/limit_up_prev,再按 (ts_code, trade_date) merge 回 feat。
    # ---------------------------------------------------------------
    is_mainboard_p = ~(panel["ts_code"].str.startswith("300") | panel["ts_code"].str.startswith("301")
                       | panel["ts_code"].str.startswith("688") | panel["ts_code"].str.endswith(".BJ"))
    is_chinext_p = panel["ts_code"].str.startswith("300") | panel["ts_code"].str.startswith("301")

    close_at_high_p = panel["panel_close"] >= (panel["panel_high"] * 0.999)
    pct_band_main_p = panel["pct_chg"].between(9.7, 10.3)
    pct_band_chinext_p = panel["pct_chg"].between(19.6, 20.4)
    limit_up_today_p = close_at_high_p & ((is_mainboard_p & pct_band_main_p) | (is_chinext_p & pct_band_chinext_p))

    panel_flag = panel[["ts_code", "trade_date"]].copy()
    panel_flag["limit_up_today"] = limit_up_today_p
    panel_flag = panel_flag.sort_values(["ts_code", "trade_date"])
    panel_flag["limit_up_prev"] = panel_flag.groupby("ts_code")["limit_up_today"].shift(1)

    feat = feat.merge(
        panel_flag[["ts_code", "trade_date", "limit_up_prev"]],
        on=["ts_code", "trade_date"], how="left",
    )

    is_mainboard = ~(feat["ts_code"].str.startswith("300") | feat["ts_code"].str.startswith("301")
                     | feat["ts_code"].str.startswith("688") | feat["ts_code"].str.endswith(".BJ"))
    is_chinext = feat["ts_code"].str.startswith("300") | feat["ts_code"].str.startswith("301")

    close_at_high = feat["panel_close"] >= (feat["panel_high"] * 0.999)
    pct_band_main = feat["pct_chg"].between(9.7, 10.3)
    pct_band_chinext = feat["pct_chg"].between(19.6, 20.4)
    limit_up_today = close_at_high & ((is_mainboard & pct_band_main) | (is_chinext & pct_band_chinext))

    limit_up_prev_filled = np.where(feat["limit_up_prev"].isna(), False, feat["limit_up_prev"].to_numpy()).astype(bool)
    t_minus_1_not_limit = ~limit_up_prev_filled
    # 若无法确定 T-1(该股票面板首行,无前序,或该日在面板中根本没有前一交易日记录),
    # 视为未知 -> NaN 处理,不能判定首板
    t_minus_1_unknown = feat["limit_up_prev"].isna()

    # 上市>=60交易日
    print("计算上市交易日数(list_date -> 交易日历序号)...")
    calendar = build_trading_calendar(panel)
    stock_list = pd.read_csv(STOCK_LIST_PATH, dtype={"list_date": str})
    list_date_map = stock_list.set_index("ts_code")["list_date"].to_dict()
    # fallback: 面板中该股票首次出现的 trade_date (仅用于 stock_list.csv 未覆盖的股票,
    # 即少量688科创板; 如实标注为近似口径)
    fallback_first_seen = panel.groupby("ts_code")["trade_date"].min().to_dict()

    trading_days_since_listing, used_fallback_listing = compute_listing_trading_days(
        feat["ts_code"], feat["trade_date"], list_date_map, calendar, fallback_first_seen,
    )
    feat["_trading_days_since_listing"] = trading_days_since_listing
    feat["_listing_date_is_fallback"] = feat["ts_code"].map(
        lambda c: pd.isna(list_date_map.get(c))
    )
    listed_ge_60 = feat["_trading_days_since_listing"] >= 60

    is_first_board_T = (
        limit_up_today
        & t_minus_1_not_limit
        & (~t_minus_1_unknown)
        & listed_ge_60
        & is_mainboard.to_numpy().__or__(is_chinext.to_numpy())  # 主板或创业板(688/BJ天然被 is_mainboard=False 排除)
    )
    # 面板缺失(pct_chg/panel_close 等 NaN)的行 -> NaN 而非 False
    panel_missing = feat["pct_chg"].isna() | feat["panel_close"].isna() | feat["panel_high"].isna()
    feat["is_first_board_T"] = is_first_board_T.astype(object)
    feat.loc[panel_missing | t_minus_1_unknown | feat["_trading_days_since_listing"].isna(), "is_first_board_T"] = np.nan
    feat.loc[~panel_missing & ~t_minus_1_unknown & feat["_trading_days_since_listing"].notna(), "is_first_board_T"] = (
        is_first_board_T[~panel_missing & ~t_minus_1_unknown & feat["_trading_days_since_listing"].notna()]
    )

    # ---------------------------------------------------------------
    # paused_resume_first_day: 当日为复牌首日(尽力而为近似)。
    # 口径: 用全市场交易日历(calendar), 找到 T 在日历中的前一个交易日 prev_cal_day;
    # 若该股票在 prev_cal_day 没有任何分钟 bar(即 minute_cache 里没有该 (ts_code,
    # prev_cal_day) 记录) 且 T 当天有数据(必然,因为本行来自 feat), 且 T 不是该股票
    # 在 minute_cache 里的第一天(首日上市/数据起点不算"复牌"), 则标记为 True。
    # ---------------------------------------------------------------
    print("计算复牌首日标记(尽力而为近似,基于分钟缓存覆盖缺口)...")
    stock_dates = feat[["ts_code", "trade_date"]].drop_duplicates()
    stock_dates_sorted = stock_dates.sort_values(["ts_code", "trade_date"])
    stock_dates_sorted["_first_seen_minute"] = stock_dates_sorted.groupby("ts_code")["trade_date"].transform("min")
    date_set = set(zip(stock_dates["ts_code"], stock_dates["trade_date"]))

    cal_pos_map = {d: i for i, d in enumerate(calendar)}

    def prev_calendar_day(d: str) -> str | None:
        pos = cal_pos_map.get(d)
        if pos is None or pos == 0:
            return None
        return calendar[pos - 1]

    unique_dates = sorted(stock_dates["trade_date"].unique())
    prev_day_map = {d: prev_calendar_day(d) for d in unique_dates}
    stock_dates_sorted["_prev_cal_day"] = stock_dates_sorted["trade_date"].map(prev_day_map)

    def has_bar(row):
        pcd = row["_prev_cal_day"]
        if pcd is None:
            return True  # 无法判定前一交易日(日历起点) -> 不标记为复牌首日(保守)
        return (row["ts_code"], pcd) in date_set

    stock_dates_sorted["_had_prev_bar"] = stock_dates_sorted.apply(has_bar, axis=1)
    stock_dates_sorted["paused_resume_first_day"] = (
        (~stock_dates_sorted["_had_prev_bar"])
        & (stock_dates_sorted["trade_date"] != stock_dates_sorted["_first_seen_minute"])
    )
    feat = feat.merge(
        stock_dates_sorted[["ts_code", "trade_date", "paused_resume_first_day"]],
        on=["ts_code", "trade_date"], how="left",
    )

    # ---------------------------------------------------------------
    # 最终列裁剪 + 清理中间列(严禁遗留任何收益/结果列)
    # ---------------------------------------------------------------
    final_cols = [
        "ts_code", "trade_date",
        "n_bars_1445", "decision_px",
        "day_high_1445", "drawdown_from_high",
        "vwap_1445", "vwap_dev_1445",
        "stab_minutes",
        "gap_sign",
        "tail_vol_share_1400_1445",
        "limit_up_px", "not_sealed_1445",
        "bd",
        "is_first_board_T",
        "entry_vwap_1445_1455",
        "morning_vwap_0935_0945",
        "close_px",
        "paused_resume_first_day",
    ]
    out = feat[final_cols].copy()
    out["n_bars_1445"] = out["n_bars_1445"].astype("Int64")
    out["is_first_board_T"] = out["is_first_board_T"].astype("boolean")
    out["not_sealed_1445"] = out["not_sealed_1445"].astype("boolean")
    out["paused_resume_first_day"] = out["paused_resume_first_day"].astype("boolean")

    out = out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_suffix(".parquet.tmp")
    out.to_parquet(tmp_path, index=False)
    tmp_path.replace(OUTPUT_PATH)

    # ---------------------------------------------------------------
    # 自检报告
    # ---------------------------------------------------------------
    print(f"\n(护栏) VWAP单位自愈统计: {VWAP_SANITY_COUNTS}")
    print(f"\n输出完成: {OUTPUT_PATH}")
    print(f"总行数 = {len(out):,}, 唯一 ts_code = {out['ts_code'].nunique()}, "
          f"唯一 trade_date = {out['trade_date'].nunique()}, "
          f"日期范围 = [{out['trade_date'].min()}, {out['trade_date'].max()}]")
    print("\n各字段非空率:")
    for c in final_cols:
        if c in ("ts_code", "trade_date"):
            continue
        non_null = out[c].notna().sum()
        print(f"  {c:32s} {non_null:>9,} / {len(out):,}  ({non_null/len(out)*100:5.1f}%)")

    print(f"\n(诊断) 688科创板行数: {(out['bd']=='科创板').sum():,}  北交所行数: {(out['bd']=='北交所').sum():,}")
    print(f"(诊断) 面板缺失(pre_close为NaN)行数: {out['limit_up_px'].isna().sum():,} "
          f"(688/BJ/退市股票的预期结果,不代表分钟派生字段缺失)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
