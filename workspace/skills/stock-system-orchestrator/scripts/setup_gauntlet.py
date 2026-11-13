#!/usr/bin/env python3
"""剧本(Setup)五道闸终审引擎 setup_gauntlet(2026-07-03)。

背景：factor_registry 时代的因子闯关机(factor_gauntlet.py)只验"横截面因子排序"；
本季新战场是"剧本(setup)"——由离散条件桶（T日形态×竞价特征×...）定义的事件驱动交易，
定义与格点全部预注册在 factor_factory/config/setup_registry.json（冻结，本引擎只读不改）。
五道闸口径与该 registry 的 shared_gates 字段逐字对应：

  闸1 三段分解：事件子集次日 C2O(隔夜)/O2C(日内)/C2C(全天) 均值。edge主要在C2O(占C2C>70%)
      → 不可成交 → FAIL。复用 factor_gauntlet.gate1_three_leg 的口径(移植为事件版本)。

  闸2 净边际：registry.grid 定义的每个格点，n/毛O2C/t/胜率/净收益(实测成本，见下)，
      按 registry 该 setup 的 success_criteria 判定。

  闸3 独立复现：universe 按 ts_code 固定 seed 分半，同号且各 t>1；外加强制逐年表
      (>=2年净贡献为正，且单年净贡献占比<=70%，防止单年爆发绑架结论)。

  闸4 安慰剂：决策点特征在(同日,同条件桶)内打乱x10，真实净收益须 > 随机均值+2σ。

  闸5 容量频率：年触发次数/日均触发数/触发股竞价成交额 p25-p75，只披露不判死
      （复用 phase_d_auction_wts 的容量口径 + slippage_calibration.json 的 auction_capacity_2026）。

成本模型(registry.cost_model)：单边佣金+印花固定8bp（双边16bp）+ 滑点表
(health/slippage_calibration.json) 按 (流动性三分位 × 当日gap档) 查 slip15_med_bp（或
--cost-mode p75 时查 slip15_p75_bp）；查不到该liq×gap组合时，退回同流动性档"平开"档兜底。

剧本装载器（按 --setup id 分支，registry 字段全部逐字执行，不得跑后改定义）：
  - S1_close_auction_anomaly：collapse_auction_close 特征，见 load_setup_s1()。
  - S6_list_auction_confirm：清单确认规则，见 load_setup_s6()。
  - S5_sector_auction_resonance：板块竞价共振(sector_auction_features.parquet)，见 load_setup_s5()。
    闸4安慰剂在此setup下是板块级三元组打乱(gate4_placebo_board_shuffle)，非通用gate4_placebo。
  - S3_intraday_shape_daily：分时形态日频(intraday_shape_features.parquet)，见 load_setup_s3()。
    闸4安慰剂在此setup下是行级三特征捆绑打乱(gate4_placebo_row_bundle_shuffle)。
  - S4_regime_gated_revival：regime门控死形态复活(market_regime_features.parquet)，见 load_setup_s4()。
    闸4安慰剂在此setup下是regime标签按【交易日为块】随机重排(gate4_placebo_day_block_shuffle)——
    regime_tag/amt_regime是市场级标签(同日全部股票同值,无截面可打乱)，打乱单位是交易日而非
    行/板块，与S3/S5均不同。
  未注册的 pending_grid 状态 setup 一律拒绝运行（registry 里格点未冻结，不可跑）。

用法：
  python3 setup_gauntlet.py --setup S1_close_auction_anomaly --cost-mode med
  python3 setup_gauntlet.py --setup S1_close_auction_anomaly --selftest-random   # 验收:必须REJECTED于闸4
  python3 setup_gauntlet.py --setup S6_list_auction_confirm --cost-mode p75

绝不输出"可部署"字样；总判定只在 {CANDIDATE, REJECTED, INSUFFICIENT} 三态中选一。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
REGISTRY_PATH = WORKSPACE / "factor_factory" / "config" / "setup_registry.json"
PANEL_FULL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet"
AUCTION_DIR = WORKSPACE / "stock_data" / "01-root" / "auction_cache"
SECTOR_AUCTION_FEATURES_PATH = WORKSPACE / "stock_data" / "03-working" / "sector_auction_features.parquet"
INTRADAY_SHAPE_FEATURES_PATH = WORKSPACE / "stock_data" / "03-working" / "intraday_shape_features.parquet"
MARKET_REGIME_FEATURES_PATH = WORKSPACE / "stock_data" / "03-working" / "market_regime_features.parquet"
LATE_FEATURES_PATH = WORKSPACE / "stock_data" / "03-working" / "late_features.parquet"
BACKTEST_CACHE_DIR = WORKSPACE / "stock_data" / "03-working" / "backtest_cache"
SLIPPAGE_PATH = WORKSPACE / "stock_data" / "03-working" / "health" / "slippage_calibration.json"
REC_DB_PATH = WORKSPACE / "stock_data" / "03-working" / "recommendation_warehouse" / "recommendations.db"
OUT_DIR = WORKSPACE / "stock_data" / "03-working" / "gauntlet_reports"

TRADING_DAYS = 244
COMMISSION_STAMP_BPS_ONE_WAY = 8.0  # 佣金+印花，单边8bp（题目：单边佣金印花8bp×2=双边16bp）
# S7/S8冻结成本(题目逐字): 佣金8bp单边x2 + 冲击备付(med档5bp/p75档15bp单边)x2
# net_med = 毛 - 0.0026 ；net_p75 = 毛 - 0.0046 （不查SlippageTable，两档为registry冻结常数）
S7S8_COST_DEC = {"med": (COMMISSION_STAMP_BPS_ONE_WAY * 2 + 5.0 * 2) / 10000.0,
                 "p75": (COMMISSION_STAMP_BPS_ONE_WAY * 2 + 15.0 * 2) / 10000.0}


# ============================== registry / 成本模型 ==============================

def load_registry() -> dict:
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_setup_def(registry: dict, setup_id: str) -> dict:
    for s in registry.get("setups", []):
        if s.get("id") == setup_id:
            return s
    raise SystemExit(f"registry中找不到setup id={setup_id}。可用id: "
                      f"{[s['id'] for s in registry.get('setups', [])]}")


class SlippageTable:
    """封装 health/slippage_calibration.json 的 strata 查表：(liq档, gap档) -> slip15_med/p75_bp。
    查不到该 liq×gap 组合时，退回同 liq 档"平开"档兜底（口径来自题目第2条）。"""

    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = json.load(f)
        self.strata = {(s["liq"], s["gap"]): s for s in self.raw.get("strata", [])}
        self.capacity = self.raw.get("auction_capacity_2026", {})

    def slip_bp(self, liq: str, gap: str, mode: str = "med") -> tuple[float, str]:
        """返回 (slip_bp, source_note)。source_note 标注是否走了兜底路径。"""
        key = "slip15_med_bp" if mode == "med" else "slip15_p75_bp"
        row = self.strata.get((liq, gap))
        if row is not None and key in row:
            return float(row[key]), f"{liq}×{gap}(实测)"
        fallback = self.strata.get((liq, "平开"))
        if fallback is not None and key in fallback:
            return float(fallback[key]), f"{liq}×平开(兜底,原档{liq}×{gap}未标定)"
        return 0.0, f"{liq}×{gap}(无标定且无兜底,按0bp滑点保守报告)"

    def capacity_bucket(self, liq: str) -> dict:
        return self.capacity.get(liq, {})


def liquidity_bucket_from_amount_ma(amount_ma20_wan: pd.Series) -> pd.Series:
    """按20日均成交额(万元)三分位打流动性档，档名与 slippage_calibration.json 的 strata.liq
    对齐(高流动/中流动/低流动)。三分位阈值用当期截面自身计算(与 calibrate_slippage.py 一致的
    相对分档思路)，避免依赖外部未见文件里的绝对阈值定义。"""
    q1, q2 = amount_ma20_wan.quantile([1 / 3, 2 / 3])
    def _bkt(x: float) -> str:
        if pd.isna(x):
            return "中流动"
        if x <= q1:
            return "低流动"
        if x <= q2:
            return "中流动"
        return "高流动"
    return amount_ma20_wan.apply(_bkt)


def gap_bucket(gap: pd.Series) -> pd.Series:
    """gap档命名与 slippage_calibration.json 的 strata.gap 对齐：低开/平开/高开2-5/高开>5。"""
    def _b(g: float) -> str:
        if pd.isna(g):
            return "平开"
        if g < -0.005:
            return "低开"
        if g < 0.02:
            return "平开"
        if g < 0.05:
            return "高开2-5"
        return "高开>5"
    return gap.apply(_b)


def realized_cost_dec(liq_bkt: pd.Series, gap_bkt_: pd.Series, slip: SlippageTable, mode: str) -> pd.Series:
    """逐行查表得到 实测成本(小数) = 双边佣金印花16bp + slip15(bp,按mode)。
    返回值为小数(如0.0024=24bp)。"""
    out = np.empty(len(liq_bkt), dtype=float)
    notes = []
    cache: dict[tuple[str, str], tuple[float, str]] = {}
    liq_arr = liq_bkt.to_numpy()
    gap_arr = gap_bkt_.to_numpy()
    for i in range(len(liq_bkt)):
        key = (liq_arr[i], gap_arr[i])
        if key not in cache:
            cache[key] = slip.slip_bp(key[0], key[1], mode)
        slip_bp, note = cache[key]
        notes.append(note)
        out[i] = (COMMISSION_STAMP_BPS_ONE_WAY * 2 + slip_bp) / 10000.0
    return pd.Series(out, index=liq_bkt.index)


# ============================== 通用：日历、t统计 ==============================

def tstat(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 1e-12 else 0.0


def build_calendar_next_map(dates: list[str]) -> dict[str, str]:
    cal = sorted(set(dates))
    return {d: cal[i + 1] for i, d in enumerate(cal[:-1])}


def build_calendar_offset_map(dates: list[str], k: int) -> dict[str, str]:
    """整体市场日历上，交易日 d -> d 之后第 k 个交易日(k>=1)。用于S8需要T+2(在T+1基础上再+1)
    的场景；与build_calendar_next_map(k=1)同一实现，泛化以支持k=2。"""
    cal = sorted(set(dates))
    return {cal[i]: cal[i + k] for i in range(len(cal) - k)}


def forward_defer_to_next_available(target_dates: pd.Series, ts_codes: pd.Series,
                                     available_dates_by_code: dict[str, "np.ndarray"],
                                     market_calendar: list[str]) -> tuple[pd.Series, pd.Series]:
    """S7/S8专用：给定一批(ts_code, target_date)，若该股在target_date当天在
    available_dates_by_code(通常=late_features实际建有分钟特征的日期，即"有bar日"，
    停牌/无法建特征的日子该股不会出现)里没有行，则顺延到市场日历上target_date之后、
    且该股确实有bar的最近一天(如实=停牌/数据缺口顺延到下一有bar日，而非跳过整行)。

    返回 (resolved_dates, n_deferred_flag)：resolved_dates与输入等长，每行给出实际使用的
    日期(原目标日或顺延后的日期；若市场日历耗尽仍未找到，返回None，调用方应dropna剔除并
    计入"顺延后仍不可得"计数)；n_deferred_flag标出该行是否发生了顺延(供计数披露)。

    实现：对每个ts_code只做一次"该股可用日期排序数组"的二分查找(np.searchsorted)，
    在target_date(含)之后找该股第一个>=target_date的可用日——若target_date本身可用则
    顺延距离为0(不计入顺延计数)，否则顺延距离>0(计入顺延计数)。
    """
    cal_arr = np.array(sorted(set(market_calendar)))
    resolved = np.empty(len(target_dates), dtype=object)
    deferred_flag = np.zeros(len(target_dates), dtype=bool)
    codes = ts_codes.to_numpy()
    targets = target_dates.to_numpy()
    for i in range(len(target_dates)):
        code = codes[i]
        tgt = targets[i]
        avail = available_dates_by_code.get(code)
        if avail is None or len(avail) == 0:
            resolved[i] = None
            continue
        pos = np.searchsorted(avail, tgt, side="left")
        if pos >= len(avail):
            resolved[i] = None  # 该股在target_date之后再无可用日(如临近数据末尾)，如实置空
            continue
        found = avail[pos]
        resolved[i] = found
        if found != tgt:
            deferred_flag[i] = True
    return pd.Series(resolved, index=target_dates.index), pd.Series(deferred_flag, index=target_dates.index)


def load_daily_ohlc_for_dates(trade_dates: set[str]) -> pd.DataFrame:
    """从 backtest_cache/daily_<trade_date>.parquet 逐日加载OHLC，缺失日回退panel_full同日切片。
    只加载调用方需要的日期，避免全量扫描。"""
    frames = []
    missing = set(trade_dates)
    for d in sorted(trade_dates):
        p = BACKTEST_CACHE_DIR / f"daily_{d}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["ts_code", "trade_date", "open", "close"])
            df["trade_date"] = df["trade_date"].astype(str)
            frames.append(df)
            missing.discard(d)
    if missing:
        cols = ["ts_code", "trade_date", "open", "close"]
        pn = pd.read_parquet(PANEL_FULL, columns=cols)
        pn["trade_date"] = pn["trade_date"].astype(str)
        pn = pn[pn["trade_date"].isin(missing)]
        frames.append(pn)
    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", "open", "close"])
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"])
    return out


# ============================== 闸1：三段分解（事件版本） ==============================

def gate1_three_leg_event(event_df: pd.DataFrame) -> dict:
    """事件子集（已在 loader 里定好一行一事件）次日 C2O/O2C/C2C 均值。
    需要列: c2o, o2c, c2c（loader按各setup定义算好）。C2O占C2C>70% → FAIL。
    口径移植自 factor_gauntlet.gate1_three_leg，仅把"每日top decile"换成"事件子集整体"。"""
    d = event_df.dropna(subset=["c2o", "o2c", "c2c"])
    n = len(d)
    if n < 30:
        return {"pass": False, "reason": f"事件样本不足(n={n})", "n": n}
    c2o_m, o2c_m, c2c_m = float(d["c2o"].mean()), float(d["o2c"].mean()), float(d["c2c"].mean())
    if abs(c2c_m) < 1e-12:
        c2o_share = float("inf") if abs(c2o_m) > 1e-12 else 0.0
    else:
        c2o_share = c2o_m / c2c_m
    fillable = c2o_share <= 0.70
    return {
        "pass": bool(fillable), "n": n,
        "c2o_overnight_pct": round(c2o_m * 100, 4),
        "o2c_intraday_pct": round(o2c_m * 100, 4),
        "c2c_full_day_pct": round(c2c_m * 100, 4),
        "c2o_share_of_c2c": round(c2o_share, 3),
        "reason": ("edge主要在C2O隔夜段(占C2C>70%)→不可成交" if not fillable else "edge非集中于隔夜段→可成交性初步过关"),
    }


# ============================== 闸2：净边际（按格点） ==============================

def cell_stats(o2c: np.ndarray, cost_dec: np.ndarray) -> dict:
    """单格点统计：n/毛O2C均值%/t/胜率%/净(逐行扣实测成本)均值%/净t。"""
    n = len(o2c)
    if n == 0:
        return {"n": 0}
    net = o2c - cost_dec
    gross_m = float(np.nanmean(o2c))
    t_gross = tstat(o2c)
    hit = float(100.0 * np.nanmean(o2c > 0))
    net_m = float(np.nanmean(net))
    t_net = tstat(net)
    return {
        "n": int(n), "gross_o2c_pct": round(gross_m * 100, 4), "t_gross": round(t_gross, 3),
        "hit_pct": round(hit, 1), "net_pct": round(net_m * 100, 4), "t_net": round(t_net, 3),
        "avg_cost_bp": round(float(np.nanmean(cost_dec)) * 10000, 2),
    }


def yearly_breakdown(sub: pd.DataFrame, date_col: str, o2c_col: str, cost_col: str) -> dict:
    """强制逐年表：按年net_pct求和贡献占比，判定 >=2年净正 且 单年净贡献占比<=70%。"""
    d = sub.copy()
    d["_year"] = d[date_col].str[:4]
    rows = []
    for yr, g in d.groupby("_year"):
        net = (g[o2c_col] - g[cost_col]).to_numpy()
        rows.append({"year": yr, "n": int(len(g)), "net_sum_pct": float(np.nansum(net) * 100),
                     "net_mean_pct": float(np.nanmean(net) * 100) if len(net) else None})
    total_sum = sum(r["net_sum_pct"] for r in rows) if rows else 0.0
    n_years_positive = sum(1 for r in rows if r["net_sum_pct"] > 0)
    max_share = 0.0
    if abs(total_sum) > 1e-9:
        for r in rows:
            share = r["net_sum_pct"] / total_sum
            r["contribution_share"] = round(share, 3)
            max_share = max(max_share, abs(share))
    else:
        for r in rows:
            r["contribution_share"] = None
    ok = bool(n_years_positive >= 2 and (abs(total_sum) < 1e-9 or max_share <= 0.70) and len(rows) >= 2)
    return {
        "years": rows, "n_years_total": len(rows), "n_years_net_positive": n_years_positive,
        "max_single_year_contribution_share": round(max_share, 3) if rows else None,
        "pass": ok,
        "reason": (f">=2年净正({n_years_positive}年)且单年贡献占比<=70%({max_share:.0%})" if ok
                   else f"逐年表未达标(净正年数={n_years_positive}, 单年最大贡献占比={max_share:.0%}, 覆盖{len(rows)}年)"),
    }


# ============================== 闸3：独立复现 ==============================

def gate3_reproducibility(sub: pd.DataFrame, o2c_col: str, cost_col: str,
                           code_col: str = "ts_code", date_col: str = "trade_date",
                           seed: int = 20260703) -> dict:
    """universe按ts_code固定seed分半，主格点净边际两半同号且各t>1；外加强制逐年表。"""
    codes = sorted(sub[code_col].unique())
    rng = np.random.RandomState(seed)
    perm = rng.permutation(codes)
    half = len(perm) // 2
    set_a, set_b = set(perm[:half]), set(perm[half:])
    a = sub[sub[code_col].isin(set_a)]
    b = sub[sub[code_col].isin(set_b)]
    ra = cell_stats(a[o2c_col].to_numpy(), a[cost_col].to_numpy())
    rb = cell_stats(b[o2c_col].to_numpy(), b[cost_col].to_numpy())
    a_net, a_t = ra.get("net_pct"), ra.get("t_net")
    b_net, b_t = rb.get("net_pct"), rb.get("t_net")
    same_sign = bool(a_net is not None and b_net is not None and (a_net > 0) == (b_net > 0))
    both_t_ok = bool(a_t is not None and b_t is not None and a_t > 1 and b_t > 1)
    enough_n = bool(ra.get("n", 0) >= 10 and rb.get("n", 0) >= 10)
    universe_ok = bool(enough_n and same_sign and both_t_ok)

    yearly = yearly_breakdown(sub, date_col, o2c_col, cost_col)

    overall = bool(universe_ok and yearly["pass"])
    if universe_ok and yearly["pass"]:
        reason = "universe两半同号且各t>1，且逐年表达标"
    elif not enough_n:
        reason = "universe分半样本不足(<10)"
    elif not same_sign:
        reason = "universe两半净边际符号不一致(样本分裂即变号=不稳健)"
    elif not both_t_ok:
        reason = f"universe两半同号但t不足1(半A t={a_t}, 半B t={b_t})"
    else:
        reason = f"universe分半PASS但逐年表未达标: {yearly['reason']}"
    return {
        "pass": overall,
        "universe_split": {"half_a": ra, "half_b": rb, "same_sign": same_sign,
                            "both_t_gt_1": both_t_ok, "pass": universe_ok},
        "yearly_breakdown": yearly,
        "reason": reason,
    }


# ============================== 闸4：安慰剂 ==============================

def gate4_placebo(sub: pd.DataFrame, feature_col: str, condition_cols: list[str],
                   select_fn, o2c_col: str = "o2c", cost_col: str = "_cost_dec",
                   seed: int = 20260703, n_trials: int = 10,
                   universe_df: pd.DataFrame | None = None) -> dict:
    """决策点特征在(同日,同条件桶)内打乱x10，真实净收益须 > 随机均值+2σ。
    select_fn(df_with_shuffled_feature) -> 布尔mask，标出打乱后仍会被选中的行(桶定义按特征重算)。
    real_mean 必须算在 sub（该格点/规则的真实选中子集）上；安慰剂的打乱+重选则必须在完整universe
    (universe_df，若未传则退回sub本身)上进行，否则两侧比较的不是同一件事。之前的实现把"真实"也
    错误地算在了整片universe上，导致所有格点的real_mean恒为同一个全局常数——已修复(最小diff)。"""
    d = sub.copy()
    real_net = (d[o2c_col] - d[cost_col]).to_numpy()
    real_mean = float(np.nanmean(real_net)) if len(real_net) else float("nan")

    universe = sub if universe_df is None else universe_df

    rng = np.random.RandomState(seed)
    placebo_means = []
    group_cols = condition_cols if condition_cols else ["trade_date"]
    for _ in range(n_trials):
        d2 = universe.copy()
        d2[feature_col] = d2.groupby(group_cols)[feature_col].transform(
            lambda s: rng.permutation(s.values) if len(s) > 1 else s.values)
        mask = select_fn(d2)
        picked = d2[mask]
        if len(picked) >= 20:
            net_p = (picked[o2c_col] - picked[cost_col]).to_numpy()
            placebo_means.append(float(np.nanmean(net_p)))

    if not np.isfinite(real_mean) or len(placebo_means) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验(真实均值或有效安慰剂重复数不足)",
                "real_net_mean_pct": round(real_mean * 100, 4) if np.isfinite(real_mean) else None,
                "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means]}

    pm, psd = float(np.mean(placebo_means)), (float(np.std(placebo_means, ddof=1)) if len(placebo_means) > 1 else 0.0)
    threshold = pm + 2 * psd
    passed = bool(real_mean > threshold)
    zscore = (real_mean - pm) / psd if psd > 1e-9 else (float("inf") if real_mean > pm else 0.0)
    return {
        "pass": passed,
        "real_net_mean_pct": round(real_mean * 100, 4),
        "placebo_mean_pct": round(pm * 100, 4), "placebo_std_pct": round(psd * 100, 4),
        "placebo_threshold_mean_plus_2sigma_pct": round(threshold * 100, 4),
        "real_vs_placebo_sigma": round(zscore, 2) if np.isfinite(zscore) else None,
        "n_trials_valid": len(placebo_means),
        "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means],
        "reason": (f"真实净{real_mean*100:+.4f}%优于安慰剂均值+2σ({threshold*100:+.4f}%)" if passed
                   else f"真实净{real_mean*100:+.4f}%未超安慰剂均值+2σ({threshold*100:+.4f}%)→与噪声不可区分"),
    }


# ============================== 闸4(S5专用)：板块级安慰剂 ==============================

def gate4_placebo_board_shuffle(board_all: pd.DataFrame, members_all: pd.DataFrame, cell: dict,
                                 o2c_col: str = "o2c", cost_col: str = "_cost_dec",
                                 seed: int = 20260703, n_trials: int = 10) -> dict:
    """S5专用安慰剂：registry原文"同日内跨板块打乱板块触发特征(整板块的三特征作为一组一起打乱到
    别的板块头上,成员归属不变)x10"。与通用gate4_placebo(逐行打乱单一特征列)不同——这里打乱的
    单位是"板块"，且sector_strength/sector_amt/leader三个特征必须作为一组(同一板块的三元组)整体
    搬到同日另一个板块头上，而不是三个特征各自独立打乱(那样会破坏三元组内部的真实相关结构，
    不是registry要的"板块触发特征"打乱)。

    board_all: 一行一(industry, trade_date)，含 gap_breadth_2(用于sector_strength三分位重算)、
               auct_amt_z、leader_gap 三个原始列，且已限定 n_members>=8 的合格板块全集。
    members_all: 一行一(ts_code, trade_date)事件全集（已过gap/universe/非一字筛选，未按格点筛选），
               含 industry(前日) 列用于对齐打乱后的板块特征。
    cell: {"sector_strength":.., "sector_amt":.., "leader":..} 待检验格点定义。
    """
    real_mask = ((members_all["sector_strength"] == cell["sector_strength"])
                 & (members_all["sector_amt"] == cell["sector_amt"])
                 & (members_all["leader"] == cell["leader"]))
    real_sub = members_all[real_mask]
    real_net = (real_sub[o2c_col] - real_sub[cost_col]).to_numpy()
    real_mean = float(np.nanmean(real_net)) if len(real_net) else float("nan")

    rng = np.random.RandomState(seed)
    raw_cols = ["gap_breadth_2", "auct_amt_z", "leader_gap"]
    placebo_means = []
    for _ in range(n_trials):
        b2 = board_all.copy()
        # 同日内跨板块打乱：三特征组成的三元组作为一个单元，整体重新分配给同日另一板块(成员归属不变，
        # 只是"该板块被认为长了哪一组触发特征")。
        # 评审must_fix(20260703): 原 groupby[cols].transform(sample) 逐列应用、共享rng在列间前进，
        # 三列各得不同排列→捆绑被悄悄打散(与S3同款bug)。移植S3修复：显式按日行位置置换,三列整块搬家。
        _vals = b2[raw_cols].to_numpy()
        _new = _vals.copy()
        for _ip in b2.groupby("trade_date").indices.values():
            if len(_ip) > 1:
                _new[_ip] = _vals[_ip[rng.permutation(len(_ip))]]
        b2[raw_cols] = _new
        # 用打乱后的板块特征重算三个决策桶（分桶规则不变，只是输入的原始值被打乱后的板块值替换）
        b2["sector_strength_shuf"] = b2.groupby("trade_date")["gap_breadth_2"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=["弱", "中", "强"])
            if s.notna().sum() >= 3 else pd.Series([None] * len(s), index=s.index))
        b2["sector_amt_shuf"] = np.where(b2["auct_amt_z"] >= 1.0, "高", "低")
        b2["sector_amt_shuf"] = np.where(b2["auct_amt_z"].isna(), None, b2["sector_amt_shuf"])
        b2["leader_shuf"] = np.where(b2["leader_gap"] >= 5.0, "强", "弱")

        m2 = members_all.drop(columns=["sector_strength", "sector_amt", "leader"], errors="ignore").merge(
            b2[["industry", "trade_date", "sector_strength_shuf", "sector_amt_shuf", "leader_shuf"]],
            on=["industry", "trade_date"], how="inner")
        mask = ((m2["sector_strength_shuf"].astype(str) == cell["sector_strength"])
                & (m2["sector_amt_shuf"].astype(str) == cell["sector_amt"])
                & (m2["leader_shuf"].astype(str) == cell["leader"]))
        picked = m2[mask]
        if len(picked) >= 20:
            net_p = (picked[o2c_col] - picked[cost_col]).to_numpy()
            placebo_means.append(float(np.nanmean(net_p)))

    if not np.isfinite(real_mean) or len(placebo_means) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验(真实均值或有效安慰剂重复数不足)",
                "real_net_mean_pct": round(real_mean * 100, 4) if np.isfinite(real_mean) else None,
                "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means]}

    pm = float(np.mean(placebo_means))
    psd = float(np.std(placebo_means, ddof=1)) if len(placebo_means) > 1 else 0.0
    threshold = pm + 2 * psd
    passed = bool(real_mean > threshold)
    zscore = (real_mean - pm) / psd if psd > 1e-9 else (float("inf") if real_mean > pm else 0.0)
    return {
        "pass": passed,
        "real_net_mean_pct": round(real_mean * 100, 4),
        "placebo_mean_pct": round(pm * 100, 4), "placebo_std_pct": round(psd * 100, 4),
        "placebo_threshold_mean_plus_2sigma_pct": round(threshold * 100, 4),
        "real_vs_placebo_sigma": round(zscore, 2) if np.isfinite(zscore) else None,
        "n_trials_valid": len(placebo_means),
        "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means],
        "reason": (f"真实净{real_mean*100:+.4f}%优于安慰剂均值+2σ({threshold*100:+.4f}%)" if passed
                   else f"真实净{real_mean*100:+.4f}%未超安慰剂均值+2σ({threshold*100:+.4f}%)→与噪声不可区分"),
    }


# ============================== 闸4(S3专用)：行级三特征捆绑安慰剂 ==============================

def gate4_placebo_row_bundle_shuffle(ev_all: pd.DataFrame, cell: dict,
                                      raw_cols: list[str] = ("am_vol_share", "late_strength", "path_smoothness"),
                                      o2c_col: str = "o2c", cost_col: str = "_cost_dec",
                                      seed: int = 20260703, n_trials: int = 10) -> dict:
    """S3专用安慰剂：registry原文"同日内行级打乱三特征捆绑x10"——与S5的gate4_placebo_board_shuffle
    同一"捆绑打乱"思路，但打乱单位是"行"(个股-事件)而非"板块"：同一天内，把
    (am_vol_share, late_strength, path_smoothness) 三元组作为一个整体，在当日全体事件行之间
    整体重新洗牌分配(而不是三个特征各自独立打乱)，这样三元组内部的真实相关结构被打乱到别的
    行头上，但同一行内三特征的联合分布不受破坏——这正是"捆绑"(而非逐特征独立)打乱要保留/
    要打乱的部分之间的界限。

    ev_all: 完整事件全集(已过n_bars/universe/T+1/一字/gap筛选，未按late_sign/格点筛选)，
            含三个原始列 + trade_date。
    cell: {"am_share":.., "late_sign":.., "smooth":..} 待检验格点定义(格点值使用重打乱后
          重新分桶算出的桶标签)。

    real_mean 算在 ev_all 中真实(未打乱)匹配 cell 的行上(与该cell的真实bucketing一致，
    由调用方在ev_all上已经算好的 am_share/late_sign/smooth 列决定)；
    placebo 则在每次试验中，对完整 ev_all 做同日三元组捆绑打乱，重新计算三个决策桶
    (与真实计算规则完全一致：am_share按同日三分位、late_sign按符号且0/NaN排除、smooth按
    同日中位二分)，再用同一个cell定义重新选中子集比较——两侧必须在"同一套重算规则"下比较，
    只是输入的原始值一个是真的、一个是同日打乱过的。
    """
    real_mask = ((ev_all["am_share"].astype(str) == cell["am_share"])
                 & (ev_all["late_sign"].astype(str) == cell["late_sign"])
                 & (ev_all["smooth"].astype(str) == cell["smooth"]))
    real_sub = ev_all[real_mask]
    real_net = (real_sub[o2c_col] - real_sub[cost_col]).to_numpy()
    real_mean = float(np.nanmean(real_net)) if len(real_net) else float("nan")

    rng = np.random.RandomState(seed)
    raw_cols = list(raw_cols)
    placebo_means = []
    placebo_n_picked = []  # 自检用：捆绑完好+同日阈值同一群体 => 每次trial选中数应==真实格n(不变量)
    for _ in range(n_trials):
        d2 = ev_all.copy()
        # 同日内行级打乱：三特征组成的三元组作为一个单元，整体重新分配给同日另一行(个股-事件)。
        # 注意：不能用 groupby[cols].transform(lambda g: g.sample(...)) —— pandas 对
        # DataFrameGroupBy.transform 按"逐列"应用该lambda，共享rng状态在列间前进，三列各得一个
        # 不同的排列，捆绑会被悄悄打散(已在本仓pandas 3.0.0实测验证)。这里用显式的按日行位置
        # 置换：一天内生成一个行排列，把三列作为整块按该排列搬家，保证registry"三特征捆绑"逐字成立。
        vals = d2[raw_cols].to_numpy()
        new_vals = vals.copy()
        for ip in d2.groupby("trade_date").indices.values():
            if len(ip) > 1:
                new_vals[ip] = vals[ip[rng.permutation(len(ip))]]
        d2[raw_cols] = new_vals

        # 用打乱后的原始值，按与真实计算完全一致的规则重新分桶
        d2["am_share_shuf"] = d2.groupby("trade_date")["am_vol_share"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=["低", "中", "高"])
            if s.notna().sum() >= 3 else pd.Series([None] * len(s), index=s.index))
        late_sign_shuf = np.sign(d2["late_strength"])
        late_sign_shuf = pd.Series(np.where(late_sign_shuf == 0, np.nan, late_sign_shuf), index=d2.index)
        d2["late_sign_shuf"] = late_sign_shuf.map({1.0: "强", -1.0: "弱"})
        med_shuf = d2.groupby("trade_date")["path_smoothness"].transform("median")
        d2["smooth_shuf"] = np.where(d2["path_smoothness"] <= med_shuf, "低", "高")
        d2.loc[d2["path_smoothness"].isna() | med_shuf.isna(), "smooth_shuf"] = None

        mask = ((d2["am_share_shuf"].astype(str) == cell["am_share"])
                & (d2["late_sign_shuf"].astype(str) == cell["late_sign"])
                & (d2["smooth_shuf"].astype(str) == cell["smooth"]))
        picked = d2[mask]
        placebo_n_picked.append(int(len(picked)))
        if len(picked) >= 20:
            net_p = (picked[o2c_col] - picked[cost_col]).to_numpy()
            placebo_means.append(float(np.nanmean(net_p)))

    if not np.isfinite(real_mean) or len(placebo_means) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验(真实均值或有效安慰剂重复数不足)",
                "real_net_mean_pct": round(real_mean * 100, 4) if np.isfinite(real_mean) else None,
                "real_n": int(len(real_sub)), "placebo_n_picked_list": placebo_n_picked,
                "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means]}

    pm = float(np.mean(placebo_means))
    psd = float(np.std(placebo_means, ddof=1)) if len(placebo_means) > 1 else 0.0
    threshold = pm + 2 * psd
    passed = bool(real_mean > threshold)
    zscore = (real_mean - pm) / psd if psd > 1e-9 else (float("inf") if real_mean > pm else 0.0)
    return {
        "pass": passed,
        "real_net_mean_pct": round(real_mean * 100, 4),
        "real_n": int(len(real_sub)),
        "placebo_mean_pct": round(pm * 100, 4), "placebo_std_pct": round(psd * 100, 4),
        "placebo_threshold_mean_plus_2sigma_pct": round(threshold * 100, 4),
        "real_vs_placebo_sigma": round(zscore, 2) if np.isfinite(zscore) else None,
        "n_trials_valid": len(placebo_means),
        "placebo_n_picked_list": placebo_n_picked,
        "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means],
        "reason": (f"真实净{real_mean*100:+.4f}%优于安慰剂均值+2σ({threshold*100:+.4f}%)" if passed
                   else f"真实净{real_mean*100:+.4f}%未超安慰剂均值+2σ({threshold*100:+.4f}%)→与噪声不可区分"),
    }


# ============================== 闸4(S4专用)：regime按【交易日为块】随机重排安慰剂 ==============================

def gate4_placebo_day_block_shuffle(ev_all: pd.DataFrame, day_regime: pd.DataFrame, cell: dict,
                                     o2c_col: str = "o2c", cost_col: str = "_cost_dec",
                                     seed: int = 20260703, n_trials: int = 10) -> dict:
    """S4专用安慰剂：registry原文"regime标签按【日】块随机重排x10——regime是市场级无截面,
    故打乱单位是交易日"。与S3(行级)/S5(板块级)不同——regime_tag/amt_regime对同一天的全部股票
    取值相同(市场级、无截面可打乱)，因此本函数把(regime_tag, amt_regime)这一对市场级标签
    "整天"打乱到日历上的另一个交易日头上(日历天洗牌，股票归属/触发事件本身不变)，而不是在
    行或板块层面打乱——这是registry明确要求的、与S3/S5均不同的第三种打乱单位。

    ev_all: 完整事件全集(已过universe/688/.BJ/触发/T+1非一字/gap筛选，未按regime格点筛选)，
            一行一(ts_code, trade_date=T)事件，含 trade_date 列(用于对齐打乱后的regime标签)，
            以及已经算好的 o2c/_cost_dec 列。真实格点选择使用ev_all自带的regime_tag/amt_regime列
            (由调用方在合并day_regime后算好，与本函数打乱后重算的规则完全一致)。
    day_regime: 一行一(trade_date=T)的市场级regime表，含 regime_tag(三态字符串)与
            amt_regime(已按amount_z20符号算好的二值字符串"正"/"非正"，NaN已剔除)，
            且已限定为ev_all实际出现过的T日全集(供打乱池使用；只打乱ev_all实际用到的交易日，
            不牵扯整个regime文件里ev_all未触发的日期，符合"按事件覆盖的交易日为块"重排)。
    cell: {"regime_tag":.., "amt_regime":..} 待检验格点定义。

    自检不变量(placebo_n_picked类)：块重排只是把(regime_tag, amt_regime)这个日级标签对整体
    搬到另一天头上，事件行本身(ts_code-T)一个不少一个不多地保留——因此每次trial里，6格
    (3 regime_tag x 2 amt_regime)选中数之和必须恒等于事件总数len(ev_all)(3态x2值=6个互斥
    完全覆盖的桶，无第7种可能，日级标签打乱不改变这个恒等式)。下面逐trial计算并断言，若
    违反说明打乱实现有bug(不是regime科学问题，是自检代码问题)。
    """
    real_mask = ((ev_all["regime_tag"].astype(str) == cell["regime_tag"])
                 & (ev_all["amt_regime"].astype(str) == cell["amt_regime"]))
    real_sub = ev_all[real_mask]
    real_net = (real_sub[o2c_col] - real_sub[cost_col]).to_numpy()
    real_mean = float(np.nanmean(real_net)) if len(real_net) else float("nan")

    rng = np.random.RandomState(seed)
    days = day_regime["trade_date"].to_numpy()
    regime_arr = day_regime["regime_tag"].to_numpy()
    amt_arr = day_regime["amt_regime"].to_numpy()
    n_days = len(days)

    ev_days = ev_all[["trade_date"]].copy()
    # 提前把trade_date -> 事件行的位置索引整理好，避免每次trial都重新groupby
    date_to_pos = ev_days.groupby("trade_date").indices  # {trade_date: 行位置array}

    placebo_means = []
    placebo_n_picked = []  # 自检：块重排后6格n之和应恒等于事件总数(不变量)
    n_events_total = len(ev_all)
    for _ in range(n_trials):
        perm = rng.permutation(n_days)
        shuffled_regime = regime_arr[perm]
        shuffled_amt = amt_arr[perm]
        day_to_shuf = {days[i]: (shuffled_regime[i], shuffled_amt[i]) for i in range(n_days)}

        regime_shuf = np.empty(n_events_total, dtype=object)
        amt_shuf = np.empty(n_events_total, dtype=object)
        for d, pos in date_to_pos.items():
            r_val, a_val = day_to_shuf.get(d, (None, None))
            regime_shuf[pos] = r_val
            amt_shuf[pos] = a_val

        mask6 = {}
        n_sum_check = 0
        for rt in ["活跃", "中性", "冰点"]:
            for ar in ["正", "非正"]:
                m = (regime_shuf == rt) & (amt_shuf == ar)
                mask6[(rt, ar)] = m
                n_sum_check += int(m.sum())
        # 自检不变量：6格互斥完全覆盖(排除极少数day_regime本身缺失/NaN映射不到的行，
        # 那些行的regime_shuf/amt_shuf为None，不落入任何格，属预期内的"该日无regime数据"缺口)
        n_unmapped = int(sum(1 for x in regime_shuf if x is None))
        assert n_sum_check + n_unmapped == n_events_total, (
            f"placebo_n_picked不变量违反: 6格之和({n_sum_check})+未映射({n_unmapped})"
            f" != 事件总数({n_events_total})")

        mask = mask6[(cell["regime_tag"], cell["amt_regime"])]
        picked = ev_all[mask]
        placebo_n_picked.append(int(mask.sum()))
        if len(picked) >= 20:
            net_p = (picked[o2c_col] - picked[cost_col]).to_numpy()
            placebo_means.append(float(np.nanmean(net_p)))

    if not np.isfinite(real_mean) or len(placebo_means) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验(真实均值或有效安慰剂重复数不足)",
                "real_net_mean_pct": round(real_mean * 100, 4) if np.isfinite(real_mean) else None,
                "real_n": int(len(real_sub)), "placebo_n_picked_list": placebo_n_picked,
                "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means]}

    pm = float(np.mean(placebo_means))
    psd = float(np.std(placebo_means, ddof=1)) if len(placebo_means) > 1 else 0.0
    threshold = pm + 2 * psd
    passed = bool(real_mean > threshold)
    zscore = (real_mean - pm) / psd if psd > 1e-9 else (float("inf") if real_mean > pm else 0.0)
    return {
        "pass": passed,
        "real_net_mean_pct": round(real_mean * 100, 4),
        "real_n": int(len(real_sub)),
        "placebo_mean_pct": round(pm * 100, 4), "placebo_std_pct": round(psd * 100, 4),
        "placebo_threshold_mean_plus_2sigma_pct": round(threshold * 100, 4),
        "real_vs_placebo_sigma": round(zscore, 2) if np.isfinite(zscore) else None,
        "n_trials_valid": len(placebo_means),
        "placebo_n_picked_list": placebo_n_picked,
        "placebo_n_picked_invariant_ok": True,  # 走到这里说明上面assert对全部trial均未违反
        "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means],
        "reason": (f"真实净{real_mean*100:+.4f}%优于安慰剂均值+2σ({threshold*100:+.4f}%)" if passed
                   else f"真实净{real_mean*100:+.4f}%未超安慰剂均值+2σ({threshold*100:+.4f}%)→与噪声不可区分"),
    }


# ============================== 闸4(S7/S8专用)：双安慰剂——行级捆绑x10 且 日块重排x10 ==============================

def gate4_placebo_row_bundle_shuffle_generic(ev_all: pd.DataFrame, cell: dict,
                                              raw_cols: list[str], bucket_cols: list[str],
                                              bucketize_fn, o2c_col: str = "o2c",
                                              cost_col: str = "_cost_dec",
                                              seed: int = 20260703, n_trials: int = 10) -> dict:
    """S7/S8共用：行级特征捆绑(同日内)打乱x10，泛化自gate4_placebo_row_bundle_shuffle(S3专用，
    硬编码了3个特定列名)。这里raw_cols/bucket_cols/bucketize_fn由调用方传入，逻辑与S3版本逐字
    一致(同日内按行位置显式置换，捆绑整体搬家，避免transform在多列间前进rng状态悄悄打散捆绑——
    与S3/S5同一bug教训)，不修改/不复用同一份代码体(独立函数，不碰原S3专用函数)。

    ev_all: 完整事件全集(已过全部事件域过滤，未按格点筛选)，含raw_cols原始列 + trade_date +
            bucket_cols(真实分桶列，由调用方在ev_all上已算好)。
    cell: {bucket_col_1: value1, bucket_col_2: value2, ...} 待检验格点定义(键=bucket_cols)。
    bucketize_fn(df_with_shuffled_raw_cols) -> df，须原地或返回新增了bucket_cols同名列(按与
        真实计算完全一致的规则，用打乱后的raw_cols重新分桶)。
    """
    real_mask = pd.Series(True, index=ev_all.index)
    for bc in bucket_cols:
        real_mask &= (ev_all[bc].astype(str) == str(cell[bc]))
    real_sub = ev_all[real_mask]
    real_net = (real_sub[o2c_col] - real_sub[cost_col]).to_numpy()
    real_mean = float(np.nanmean(real_net)) if len(real_net) else float("nan")

    rng = np.random.RandomState(seed)
    placebo_means = []
    placebo_n_picked = []
    for _ in range(n_trials):
        d2 = ev_all.copy()
        vals = d2[raw_cols].to_numpy()
        new_vals = vals.copy()
        for ip in d2.groupby("trade_date").indices.values():
            if len(ip) > 1:
                new_vals[ip] = vals[ip[rng.permutation(len(ip))]]
        d2[raw_cols] = new_vals
        d2 = bucketize_fn(d2)
        mask = pd.Series(True, index=d2.index)
        for bc in bucket_cols:
            mask &= (d2[bc].astype(str) == str(cell[bc]))
        picked = d2[mask]
        placebo_n_picked.append(int(len(picked)))
        if len(picked) >= 20:
            net_p = (picked[o2c_col] - picked[cost_col]).to_numpy()
            placebo_means.append(float(np.nanmean(net_p)))

    if not np.isfinite(real_mean) or len(placebo_means) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验(真实均值或有效安慰剂重复数不足)",
                "real_net_mean_pct": round(real_mean * 100, 4) if np.isfinite(real_mean) else None,
                "real_n": int(len(real_sub)), "placebo_n_picked_list": placebo_n_picked,
                "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means]}

    pm = float(np.mean(placebo_means))
    psd = float(np.std(placebo_means, ddof=1)) if len(placebo_means) > 1 else 0.0
    threshold = pm + 2 * psd
    passed = bool(real_mean > threshold)
    zscore = (real_mean - pm) / psd if psd > 1e-9 else (float("inf") if real_mean > pm else 0.0)
    return {
        "pass": passed,
        "real_net_mean_pct": round(real_mean * 100, 4),
        "real_n": int(len(real_sub)),
        "placebo_mean_pct": round(pm * 100, 4), "placebo_std_pct": round(psd * 100, 4),
        "placebo_threshold_mean_plus_2sigma_pct": round(threshold * 100, 4),
        "real_vs_placebo_sigma": round(zscore, 2) if np.isfinite(zscore) else None,
        "n_trials_valid": len(placebo_means),
        "placebo_n_picked_list": placebo_n_picked,
        "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means],
        "reason": (f"真实净{real_mean*100:+.4f}%优于安慰剂均值+2σ({threshold*100:+.4f}%)" if passed
                   else f"真实净{real_mean*100:+.4f}%未超安慰剂均值+2σ({threshold*100:+.4f}%)→与噪声不可区分"),
    }


def gate4_placebo_day_block_bundle_shuffle(ev_all: pd.DataFrame, cell: dict,
                                            raw_cols: list[str], bucket_cols: list[str],
                                            bucketize_fn, o2c_col: str = "o2c",
                                            cost_col: str = "_cost_dec",
                                            seed: int = 20260703, n_trials: int = 10) -> dict:
    """S7/S8专用第二种安慰剂(与上面行级捆绑并列，二者均须z>2)：题目原文"把事件日的特征捆绑
    按【交易日】整块重排到其他事件日，打破日期与隔夜市场收益的对齐"——即S4的日块重排思路，
    但S4打乱的是"市场级单值标签"(regime_tag对同日全部股票同值)，这里打乱的是"某一整个事件日
    的全部事件行(含各行各自的原始特征值)"作为一个不可分的块，整体搬到另一个事件日的日期头上
    (该目标日原有的事件行被搬走的块整体替换，事件行数=各日事件数一一对应，只是日期标签互换)。
    这样能检测"日期(=隔夜市场收益的代理)与特征-收益关系"是否只是巧合的时间对齐假阳
    (S4教训：regime条件边际与随机日期切块不可区分，系时间序列自相关假阳)。

    实现：取事件全集实际出现过的交易日集合，每次trial对这些"日"做一个随机排列perm，把
    trade_date=d_i的全部事件行的trade_date整体替换为trade_date=perm(d_i)——注意：只重打乱
    trade_date这一个标签列本身(其余原始特征/o2c/成本列保持不变，随行走)，这样"哪一天发生了
    什么"被打乱，但"同一事件行内部各列的联合关系"完好无损（这正是"打破日期与隔夜市场收益对齐"
    的题中之义：隔夜市场收益是通过trade_date时间戳与外部市场状态关联的，与本剧本自身的o2c/
    成本列无关——它们是该行自身的执行结果，不随"日期标签换头"变化。真正被检验的是：格点定义
    (bucket_cols)是否只因为"恰好发生在某些日期"而看起来有edge，与随便挑其他日期比没有差异。

    由于bucket_cols(真实分桶)本身多为"同日截面三分位/中位二分"，这类分桶阈值内嵌了"同日"
    这一分组维度——为保证两侧比较对称，本函数在重打乱trade_date后，用bucketize_fn基于新的
    trade_date分组重新计算分桶(与真实计算规则完全一致，只是分组的"日期身份"已被打乱到不同
    日期上，日期内成员集合不变——这与"把某天的整批事件搬到另一天"是同一件事的两种描述)。
    """
    real_mask = pd.Series(True, index=ev_all.index)
    for bc in bucket_cols:
        real_mask &= (ev_all[bc].astype(str) == str(cell[bc]))
    real_sub = ev_all[real_mask]
    real_net = (real_sub[o2c_col] - real_sub[cost_col]).to_numpy()
    real_mean = float(np.nanmean(real_net)) if len(real_net) else float("nan")

    days = sorted(ev_all["trade_date"].unique())
    n_days = len(days)
    day_to_pos = {d: i for i, d in enumerate(days)}

    rng = np.random.RandomState(seed)
    placebo_means = []
    placebo_n_picked = []
    n_events_total = len(ev_all)
    for _ in range(n_trials):
        perm = rng.permutation(n_days)
        # d_i(位置i的日期) -> perm对应的新日期标签(即把第i天的整批事件行的日期标签换成
        # days[perm[i]])。这是一一映射(每天恰好换到一个新的日期身份，块大小=各日事件数不变)。
        remap = {days[i]: days[perm[i]] for i in range(n_days)}
        d2 = ev_all.copy()
        d2["trade_date"] = d2["trade_date"].map(remap)
        d2 = bucketize_fn(d2)
        mask = pd.Series(True, index=d2.index)
        for bc in bucket_cols:
            mask &= (d2[bc].astype(str) == str(cell[bc]))
        picked = d2[mask]
        placebo_n_picked.append(int(len(picked)))
        if len(picked) >= 20:
            net_p = (picked[o2c_col] - picked[cost_col]).to_numpy()
            placebo_means.append(float(np.nanmean(net_p)))

    if not np.isfinite(real_mean) or len(placebo_means) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验(真实均值或有效安慰剂重复数不足)",
                "real_net_mean_pct": round(real_mean * 100, 4) if np.isfinite(real_mean) else None,
                "real_n": int(len(real_sub)), "placebo_n_picked_list": placebo_n_picked,
                "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means]}

    pm = float(np.mean(placebo_means))
    psd = float(np.std(placebo_means, ddof=1)) if len(placebo_means) > 1 else 0.0
    threshold = pm + 2 * psd
    passed = bool(real_mean > threshold)
    zscore = (real_mean - pm) / psd if psd > 1e-9 else (float("inf") if real_mean > pm else 0.0)
    return {
        "pass": passed,
        "real_net_mean_pct": round(real_mean * 100, 4),
        "real_n": int(len(real_sub)),
        "placebo_mean_pct": round(pm * 100, 4), "placebo_std_pct": round(psd * 100, 4),
        "placebo_threshold_mean_plus_2sigma_pct": round(threshold * 100, 4),
        "real_vs_placebo_sigma": round(zscore, 2) if np.isfinite(zscore) else None,
        "n_trials_valid": len(placebo_means),
        "n_events_total": n_events_total,
        "placebo_n_picked_list": placebo_n_picked,
        "placebo_net_mean_pct_list": [round(x * 100, 4) for x in placebo_means],
        "reason": (f"真实净{real_mean*100:+.4f}%优于安慰剂均值+2σ({threshold*100:+.4f}%)" if passed
                   else f"真实净{real_mean*100:+.4f}%未超安慰剂均值+2σ({threshold*100:+.4f}%)→与噪声不可区分"),
    }


def gate4_day_cluster_overnight_check(ev_all: pd.DataFrame, cell: dict, bucket_cols: list[str],
                                       o2c_col: str = "o2c", cost_col: str = "_cost_dec") -> dict:
    """S7/S8闸4第二检验(监督者20260706修订版): 日聚合去市场均值检验。

    审计背景: 原实现(gate4_placebo_day_block_bundle_shuffle)对行级特征是恒等操作——
    特征/收益/成本随行走,日期标签一一置换后按新日期重分桶,分组成员集合不变→cell选中
    完全相同的行→placebo均值≡真实均值(合成数据实测证实),所有格必然FAIL。
    S4日块重排有效的前提是特征为市场级每日单值标签,不适用于行级特征。

    本检验直接命中原设计意图(隔夜beta时序假阳,S4教训/红队要求): 格点的edge必须
    在【扣除同日事件全集均值】后依然显著——即必须跑赢自己所在日的事件截面,
    不能靠"恰好在隔夜大盘漂移好的日子里有事件"获得表面edge。
    判据: demeaned日均差跨日 t>2 且均值>0(与z>2同强度口径)。"""
    real_mask = pd.Series(True, index=ev_all.index)
    for bc in bucket_cols:
        real_mask &= (ev_all[bc].astype(str) == str(cell[bc]))
    net = ev_all[o2c_col] - ev_all[cost_col]
    cell_day = net[real_mask].groupby(ev_all.loc[real_mask, "trade_date"]).mean()
    univ_day = net.groupby(ev_all["trade_date"]).mean()
    aligned = pd.DataFrame({"cell": cell_day, "univ": univ_day}).dropna()
    n_days = len(aligned)
    if n_days < 30:
        return {"pass": False, "reason": f"有效事件日不足({n_days}<30),无法做日聚合检验",
                "n_days": n_days}
    diff = aligned["cell"] - aligned["univ"]
    mean_d = float(diff.mean())
    sd = float(diff.std(ddof=1))
    t_demeaned = mean_d / (sd / np.sqrt(n_days)) if sd > 1e-12 else 0.0
    raw_mean = float(aligned["cell"].mean())
    raw_t = raw_mean / (aligned["cell"].std(ddof=1) / np.sqrt(n_days)) if aligned["cell"].std(ddof=1) > 1e-12 else 0.0
    passed = bool(t_demeaned > 2.0 and mean_d > 0)
    return {
        "pass": passed,
        "n_days": n_days,
        "demeaned_day_mean_pct": round(mean_d * 100, 4),
        "demeaned_day_t": round(t_demeaned, 2),
        "raw_day_mean_pct": round(raw_mean * 100, 4),
        "raw_day_t": round(raw_t, 2),
        "reason": (f"去同日事件均值后日均差{mean_d*100:+.4f}%/t={t_demeaned:.2f}"
                   + ("(跑赢自身所在日截面,非隔夜beta搭车)" if passed
                      else "→edge与'挑对了日子'不可区分(隔夜beta假阳嫌疑)")),
    }


def gate4_placebo_dual(ev_all: pd.DataFrame, cell: dict, raw_cols: list[str], bucket_cols: list[str],
                        bucketize_fn, o2c_col: str = "o2c", cost_col: str = "_cost_dec",
                        seed: int = 20260703, n_trials: int = 10) -> dict:
    """S7/S8专用总闸4包装(监督者20260706修订): 双检验=行级捆绑打乱x10(截面选择力)
    且 日聚合去市场均值(隔夜beta假阳),两者均过才PASS。
    原第二检验(日块重排)经审计对行级特征为恒等操作已废弃,替换理由见
    gate4_day_cluster_overnight_check文档字符串;修订发生在任何真格收益判定运行之前
    (预注册修订,registry条目同步注明)。"""
    row_bundle = gate4_placebo_row_bundle_shuffle_generic(
        ev_all, cell, raw_cols, bucket_cols, bucketize_fn, o2c_col, cost_col, seed, n_trials)
    day_cluster = gate4_day_cluster_overnight_check(ev_all, cell, bucket_cols, o2c_col, cost_col)
    both_pass = bool(row_bundle["pass"] and day_cluster["pass"])
    reason = (f"行级捆绑{'PASS' if row_bundle['pass'] else 'FAIL'}(z={row_bundle.get('real_vs_placebo_sigma')}) "
              f"且 日聚合去市场均值{'PASS' if day_cluster['pass'] else 'FAIL'}(t={day_cluster.get('demeaned_day_t')})"
              f" → {'两者均PASS' if both_pass else '至少一项FAIL，未达双检验标准'}")
    return {"pass": both_pass, "row_bundle_shuffle": row_bundle, "day_cluster_overnight": day_cluster,
            "reason": reason}


# ============================== 闸5：容量频率（只披露，不判死） ==============================

def gate5_capacity(sub: pd.DataFrame, liq_col: str, auct_amount_col: str,
                    date_col: str, n_calendar_years: float, slip: SlippageTable) -> dict:
    n_total = len(sub)
    n_days = sub[date_col].nunique()
    per_year = n_total / n_calendar_years if n_calendar_years > 0 else None
    per_day = n_total / n_days if n_days > 0 else None
    amt_stats = {}
    if auct_amount_col in sub.columns:
        amt = sub[auct_amount_col].dropna()
        if len(amt) > 0:
            amt_stats = {
                "p25_amount": float(amt.quantile(0.25)), "median_amount": float(amt.quantile(0.5)),
                "p75_amount": float(amt.quantile(0.75)),
            }
    by_liq = {}
    if liq_col in sub.columns:
        for liq, g in sub.groupby(liq_col):
            by_liq[str(liq)] = {"n": int(len(g)), "capacity_2026_wan": slip.capacity_bucket(str(liq))}
    return {
        "n_triggers_total": int(n_total), "n_trigger_days": int(n_days),
        "triggers_per_year_est": round(per_year, 1) if per_year is not None else None,
        "triggers_per_day_avg": round(per_day, 2) if per_day is not None else None,
        "trigger_auction_amount_yuan": amt_stats,
        "by_liquidity_bucket": by_liq,
        "note": "只披露，不作为通过/拒绝判据。",
    }


# ============================== S1: 收盘竞价异象 ==============================

def diagnose_amount_unit(auct: pd.DataFrame, panel_amount: pd.DataFrame, sample_date: str) -> dict:
    """单位自检：打印/返回 竞价amount 与 面板amount(全天) 的中位比值分布，判断竞价amount是否需要
    单位换算。按题目预期：比值应主要落在(0,0.2)——若明显偏离(如数十倍甚至上百倍)，说明竞价
    parquet的amount口径与预想的'收盘竞价窗口'不符(可能是全天数据或不同单位)，如实报告。"""
    a = auct[auct["trade_date"] == sample_date][["ts_code", "amount"]].rename(columns={"amount": "ca_amount"})
    p = panel_amount[panel_amount["trade_date"] == sample_date][["ts_code", "amount"]].rename(columns={"amount": "panel_amount_qianyuan"})
    m = a.merge(p, on="ts_code", how="inner")
    m = m[(m["ca_amount"] > 0) & (m["panel_amount_qianyuan"] > 0)]
    if len(m) == 0:
        return {"sample_date": sample_date, "n": 0, "note": "样本日无重叠数据，无法自检"}
    # 面板amount单位是千元(tushare标准)；此处把面板amount换算成元，与竞价amount(元)同单位比较
    ratio = m["ca_amount"] / (m["panel_amount_qianyuan"] * 1000.0)
    med = float(ratio.median())
    p25, p75 = float(ratio.quantile(0.25)), float(ratio.quantile(0.75))
    in_expected_range = bool(0.0 < med < 0.2)
    return {
        "sample_date": sample_date, "n": int(len(m)),
        "ratio_median": round(med, 4), "ratio_p25": round(p25, 4), "ratio_p75": round(p75, 4),
        "expected_range": "(0, 0.2)", "in_expected_range": in_expected_range,
        "note": ("比值落在预期(0,0.2)区间，竞价amount(元)/面板amount(千元换算后)口径一致，ca_amt_share按此计算。"
                  if in_expected_range else
                  "⚠️ 比值明显偏离预期(0,0.2)区间——auction_close parquet的amount很可能不是'仅收盘集合竞价窗口'"
                  "口径，而是接近全天口径(或存在其他单位差异)。ca_amt_share据此计算出的'收盘竞价占比'会系统性"
                  "偏大，本报告已如实标注，结果解读需打折扣；根因排查请找B0/ingest_auction_data.py的数据源定义，"
                  "不在本引擎范围内自行修正数据。"),
    }


def load_setup_s1(cost_mode: str, slip: SlippageTable, random_decision_feature: bool, seed: int) -> dict:
    """S1_close_auction_anomaly 装载器。
    特征: ca_amt_share=收盘竞价amount/T日全天amount(同日截面), ca_move=竞价窗内close/open-1符号,
    Tbkt=T日涨幅桶。事件域: universe_flag>0 且 T日|pct_chg|<=9.4(排除涨跌停) 且 T+1非一字 且
    T+1 gap<=+7%。trade: T+1开盘入场，T+1收盘出场(O2C)。grid: 3 Tbkt x 3 ca_amt_share三分位 x
    2 ca_move符号 = 18格。"""
    print("加载竞价收盘窗口数据(auction_close_*)与竞价开盘窗口数据(auction_open_*)...")
    frames = [pd.read_parquet(p) for p in sorted(AUCTION_DIR.glob("auction_close_*.parquet"))]
    ac_raw = pd.concat(frames, ignore_index=True)
    ac_raw["trade_date"] = ac_raw["trade_date"].astype(str)
    ac = ac_raw.rename(columns={"close": "ca_close", "open": "ca_open", "amount": "ca_amount", "vol": "ca_vol"})

    frames_o = [pd.read_parquet(p) for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    ao = pd.concat(frames_o, ignore_index=True)
    ao["trade_date"] = ao["trade_date"].astype(str)
    ao = ao.rename(columns={"close": "auct_open_price", "amount": "auct_open_amount", "vol": "auct_open_vol"})

    print("加载面板(panel_full)...")
    cols = ["ts_code", "trade_date", "open", "close", "pct_chg", "amount", "amount_ma_20",
            "universe_flag", "industry"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn_all_dates = pn[["ts_code", "trade_date", "amount"]].copy()  # 用于单位自检(不做universe过滤)
    pn = pn[pn["universe_flag"].fillna(0).astype(float) > 0].copy()

    # ---- 单位自检：抽样若干天打印ca_amount(收盘竞价窗amount) vs 面板全天amount的中位比值 ----
    cal_all = sorted(pn["trade_date"].unique())
    sample_dates = [cal_all[i] for i in (len(cal_all) // 4, len(cal_all) // 2, 3 * len(cal_all) // 4)] if len(cal_all) >= 4 else cal_all[:1]
    unit_checks = [diagnose_amount_unit(ac_raw, pn_all_dates, d) for d in sample_dates]

    cal = sorted(ac["trade_date"].unique())
    next_map = build_calendar_next_map(cal)

    # T日事件域：universe_flag>0 且 |pct_chg|<=9.4(排除涨跌停锁死)
    ev = pn[pn["pct_chg"].abs() <= 9.4].copy()
    ev = ev.merge(ac[["ts_code", "trade_date", "ca_close", "ca_open", "ca_amount", "ca_vol"]],
                  on=["ts_code", "trade_date"], how="inner")
    ev["t1"] = ev["trade_date"].map(next_map)
    ev = ev.dropna(subset=["t1"])
    pdates = set(pn["trade_date"].unique())
    ev = ev[ev["t1"].isin(pdates)].copy()

    # T+1 竞价开盘窗口特征：定gap、判一字
    ao1 = ao.rename(columns={"trade_date": "t1"})[["ts_code", "t1", "auct_open_price", "auct_open_amount"]]
    ev = ev.merge(ao1, on=["ts_code", "t1"], how="inner")

    # T+1 面板行(开盘价用于gap/入场，收盘价用于出场)：优先daily_<date>缓存，回退panel_full
    t1_dates = set(ev["t1"].unique())
    p1 = load_daily_ohlc_for_dates(t1_dates).rename(
        columns={"trade_date": "t1", "open": "open1", "close": "close1"})
    ev = ev.merge(p1, on=["ts_code", "t1"], how="inner")
    ev = ev[(ev["open1"] > 0) & (ev["close1"] > 0) & (ev["auct_open_price"] > 0) & (ev["close"] > 0)]

    # T+1 gap（用auction_open定义，即竞价撮合价/T日收盘-1；对账见下）
    ev["t1_gap"] = ev["auct_open_price"] / ev["close"] - 1.0
    dev_check = (ev["auct_open_price"] - ev["open1"]).abs() / ev["open1"].replace(0, np.nan)
    ea_med = float(dev_check.median()) if len(dev_check) else float("nan")
    # 一字判定：T+1 auction_open价 == T+1面板open == T+1面板close 且日内无波动 视为一字(不可成交)
    is_yizi = (ev["open1"] - ev["close1"]).abs() < 1e-9
    ev = ev[~is_yizi]
    ev = ev[ev["t1_gap"] <= 0.07]

    # ---- 特征 ----
    ev["Tbkt"] = np.select(
        [ev["pct_chg"] >= 5.0, ev["pct_chg"] <= -2.0],
        ["强>=5", "弱<=-2"], default="中性(-2,5)")
    ev["ca_move"] = np.sign(ev["ca_close"] / ev["ca_open"].replace(0, np.nan) - 1.0)
    ev["ca_amt_share"] = ev["ca_amount"] / (ev["amount"] * 1000.0)  # 面板amount(千元)换算元后同单位相除

    if random_decision_feature:
        rng = np.random.RandomState(seed)
        ev["ca_amt_share"] = rng.permutation(ev["ca_amt_share"].to_numpy())
        ev["ca_move"] = rng.permutation(ev["ca_move"].to_numpy())

    ev["ca_amt_share_terc"] = ev.groupby("trade_date")["ca_amt_share"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 3, labels=["低", "中", "高"]) if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))

    # ---- 出场收益：C2O/O2C/C2C（用于闸1；trade用O2C） ----
    ev["c2o"] = ev["open1"] / ev["close"] - 1.0
    ev["o2c"] = ev["close1"] / ev["open1"] - 1.0
    ev["c2c"] = ev["close1"] / ev["close"] - 1.0

    # ---- 成本：liq档(按amount_ma_20的万元) x gap档 ----
    amt_ma20_wan = (ev["amount_ma_20"] / 10.0) if "amount_ma_20" in ev.columns else pd.Series(np.nan, index=ev.index)
    # amount_ma_20单位与amount一致(千元)，换算为万元需/10
    ev["liq_bkt"] = liquidity_bucket_from_amount_ma(amt_ma20_wan)
    ev["gap_bkt"] = gap_bucket(ev["t1_gap"])
    ev["_cost_dec"] = realized_cost_dec(ev["liq_bkt"], ev["gap_bkt"], slip, cost_mode)

    ev["ts_code"] = ev["ts_code"].astype(str)
    n_years = (pd.to_datetime(ev["trade_date"], format="%Y%m%d").max()
               - pd.to_datetime(ev["trade_date"], format="%Y%m%d").min()).days / 365.25 if len(ev) else 1.0

    grid_cells = []
    for tb in ["强>=5", "中性(-2,5)", "弱<=-2"]:
        for terc in ["低", "中", "高"]:
            for mv in [1.0, -1.0]:
                grid_cells.append({"Tbkt": tb, "ca_amt_share_terc": terc, "ca_move": mv})

    return {
        "event_df": ev, "grid_cells": grid_cells, "grid_group_cols": ["Tbkt", "ca_amt_share_terc", "ca_move"],
        "feature_col": "ca_amt_share", "condition_cols": ["trade_date", "Tbkt"],
        "n_calendar_years": max(n_years, 0.5),
        "reconciliation": {"t1_gap_vs_open1_median_dev": round(ea_med, 5) if np.isfinite(ea_med) else None,
                           "threshold": 0.005, "ok": bool(np.isfinite(ea_med) and ea_med < 0.005)},
        "unit_check": unit_checks,
        "success_criteria_text": get_setup_def(load_registry(), "S1_close_auction_anomaly")["success_criteria"],
        "n_min_per_cell": 300,
    }


# ============================== S6: 清单竞价确认 ==============================

def load_recommendation_fact(strategy_ids: list[str]) -> pd.DataFrame:
    uri = f"file:{REC_DB_PATH}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        placeholders = ",".join("?" for _ in strategy_ids)
        q = (f"SELECT strategy_id, recommend_date, rank_no, ts_code, stock_code "
             f"FROM recommendation_fact WHERE strategy_id IN ({placeholders}) AND rank_no<=20")
        df = pd.read_sql_query(q, con, params=strategy_ids)
    finally:
        con.close()
    df["recommend_date"] = df["recommend_date"].astype(str)
    return df


def load_setup_s6(cost_mode: str, slip: SlippageTable, random_decision_feature: bool, seed: int) -> dict:
    """S6_list_auction_confirm 装载器。数据=recommendations.db 的 recommendation_fact
    (strategy_id in prebreakout_v41/greenfield_o2c_v1，各日rank_no<=20)，T+1竞价特征(auction_open)，
    结果=T+1 O2C(daily_*.parquet或panel_full)。4条确认规则按registry逐字；对照=同清单未确认部分；
    分半按recommend_date奇偶周切分(时间型复现)。有效清单日<40则INSUFFICIENT。"""
    print("加载 recommendation_fact(prebreakout_v41/greenfield_o2c_v1, rank<=20)...")
    rec = load_recommendation_fact(["prebreakout_v41", "greenfield_o2c_v1"])
    rec = rec[rec["ts_code"].notna() & (rec["ts_code"] != "")]

    print("加载竞价开盘窗口(auction_open_*)...")
    frames_o = [pd.read_parquet(p) for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    ao = pd.concat(frames_o, ignore_index=True)
    ao["trade_date"] = ao["trade_date"].astype(str)
    ao = ao.rename(columns={"close": "auct_open_price", "amount": "auct_open_amount"})

    cal = sorted(ao["trade_date"].unique())
    next_map = build_calendar_next_map(cal)
    rec["t1"] = rec["recommend_date"].map(next_map)
    rec = rec.dropna(subset=["t1"])

    cols = ["ts_code", "trade_date", "close", "amount_ma_20", "universe_flag"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pdates = set(pn["trade_date"].unique())
    rec = rec[rec["t1"].isin(pdates)].copy()

    # T日收盘价(用于定义T+1 gap基准)：rec的T日列是recommend_date，需以此对齐panel的trade_date
    t_close = pn[["ts_code", "trade_date", "close"]].rename(
        columns={"close": "close_T", "trade_date": "recommend_date"})
    rec = rec.merge(t_close, on=["ts_code", "recommend_date"], how="inner")

    ao1 = ao.rename(columns={"trade_date": "t1"})[["ts_code", "t1", "auct_open_price", "auct_open_amount"]]
    rec = rec.merge(ao1, on=["ts_code", "t1"], how="inner")

    t1_dates = set(rec["t1"].unique())
    p1 = load_daily_ohlc_for_dates(t1_dates).rename(columns={"trade_date": "t1", "open": "open1", "close": "close1"})
    rec = rec.merge(p1, on=["ts_code", "t1"], how="inner")
    rec = rec[(rec["open1"] > 0) & (rec["close1"] > 0) & (rec["auct_open_price"] > 0) & (rec["close_T"] > 0)]

    rec["gap"] = rec["auct_open_price"] / rec["close_T"] - 1.0
    is_yizi = (rec["open1"] - rec["close1"]).abs() < 1e-9
    rec = rec[~is_yizi]
    rec = rec[rec["gap"] <= 0.07]

    if random_decision_feature:
        rng = np.random.RandomState(seed)
        rec["auct_open_amount"] = rng.permutation(rec["auct_open_amount"].to_numpy())
        rec["gap"] = rng.permutation(rec["gap"].to_numpy())

    # avr_list = T+1竞价成交额在当日清单内的rank(百分位)
    rec["avr_list"] = rec.groupby("t1")["auct_open_amount"].rank(pct=True)

    rec["o2c"] = rec["close1"] / rec["open1"] - 1.0

    liq_map = pn[["ts_code", "trade_date", "amount_ma_20"]].rename(columns={"trade_date": "recommend_date"})
    rec = rec.merge(liq_map, on=["ts_code", "recommend_date"], how="left")
    amt_ma20_wan = rec["amount_ma_20"] / 10.0
    rec["liq_bkt"] = liquidity_bucket_from_amount_ma(amt_ma20_wan)
    rec["gap_bkt"] = gap_bucket(rec["gap"])
    rec["_cost_dec"] = realized_cost_dec(rec["liq_bkt"], rec["gap_bkt"], slip, cost_mode)

    rec["ts_code"] = rec["ts_code"].astype(str)
    n_valid_days = rec["recommend_date"].nunique()

    # 4条预注册确认规则(不许跑后加)
    median_avr = rec.groupby("t1")["avr_list"].transform("median")  # 当日清单内中位(供R_a/R_c参考基准)
    rules = {
        "R_a": (rec["gap"] >= -0.02) & (rec["gap"] <= 0.05) & (rec["avr_list"] >= 0.5),
        "R_b": (rec["gap"] >= -0.02) & (rec["gap"] <= 0.05),
        "R_c": (rec["avr_list"] >= 0.5),
        "R_d": (rec["gap"] >= 0.0) & (rec["gap"] <= 0.05) & (rec["avr_list"] >= (2.0 / 3.0)),
    }

    n_years = (pd.to_datetime(rec["recommend_date"], format="%Y%m%d").max()
               - pd.to_datetime(rec["recommend_date"], format="%Y%m%d").min()).days / 365.25 if len(rec) else 1.0

    return {
        "event_df": rec, "rules": rules, "n_valid_list_days": n_valid_days,
        "feature_col": "avr_list", "condition_cols": ["t1"],
        "n_calendar_years": max(n_years, 0.5),
        "success_criteria_text": get_setup_def(load_registry(), "S6_list_auction_confirm")["success_criteria"],
        "min_valid_days": 40,
    }


# ============================== S5: 板块竞价共振 ==============================

def load_setup_s5(cost_mode: str, slip: SlippageTable, random_decision_feature: bool, seed: int) -> dict:
    """S5_sector_auction_resonance 装载器（registry逐字，格点已由监督者20260703冻结）。

    数据: sector_auction_features.parquet(板块级,110行业x1087竞价日,一行=(industry,trade_date=竞价日D)，
    列 n_members/gap_breadth_2/gap_median/auct_amt_z/amt_concentration/leader_gap，且该文件的
    build脚本已注明leader_gap/gap_median是"百分数"单位(如5.0代表+5%)，故 leader>=+5% 按
    leader_gap>=5.0 比较（已用数据分布核验:leader_gap>=5.0命中约5.5%样本、>=0.05命中约42%——
    后者显然不是"领头羊强启动"该有的稀有度，故按百分数单位逐字对齐registry"+5%"）。

    事件域(registry逐字): 竞价日D，板块n_members>=8；个股=该板块成员，前日(T=D的上一交易日，
    与build_sector_auction_features.py的panel_ref_date同一定义) universe_flag>0，个股自身
    gap∈[-2%,+1%](闭区间，滞后者，未随板块跳)，D日非一字。gap定义=D日auction_open撮合价/
    前日panel收盘-1（与S1/S6的gap定义同一口径）。前日行(T)同时提供该股所属industry(用于对齐
    板块行)与流动性分档所需的amount_ma_20(前日已知，不构成D日开盘前的信息泄漏)。

    决策特征(registry逐字): sector_strength=板块gap_breadth_2在当日跨板块(限n_members>=8的
    合格板块全集)三分位; sector_amt=板块auct_amt_z>=1(高)/<1(低)二分(NaN一律剔除，特征未定义);
    leader=板块leader_gap>=5.0(强)/<5.0(弱)二分。grid=3x2x2=12格，全部入账。

    trade: 入场D日开盘，出场D日收盘(O2C)——D日open/close取自panel_full同日行(复用
    load_daily_ohlc_for_dates工具函数，优先daily_<date>缓存/回退panel_full，与S1/S6一致口径)。
    """
    print("加载板块竞价特征(sector_auction_features.parquet)...")
    saf = pd.read_parquet(SECTOR_AUCTION_FEATURES_PATH)
    saf["trade_date"] = saf["trade_date"].astype(str)
    n_saf_total = len(saf)
    board = saf[saf["n_members"] >= 8].copy()
    print(f"  板块-竞价日总行数={n_saf_total}  n_members>=8合格板块行数={len(board)}")

    print("加载竞价开盘窗口(auction_open_*)与面板(panel_full)...")
    frames_o = [pd.read_parquet(p, columns=["ts_code", "trade_date", "close"])
                for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    ao = pd.concat(frames_o, ignore_index=True)
    ao["trade_date"] = ao["trade_date"].astype(str)
    ao = ao.rename(columns={"close": "auct_open_price"})

    cols = ["ts_code", "trade_date", "close", "industry", "universe_flag", "amount_ma_20"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)

    # 前日映射：D的上一个panel交易日=T，定义与build_sector_auction_features.py的panel_ref_date一致
    cal = sorted(pn["trade_date"].unique())
    prev_map = {cal[i]: cal[i - 1] for i in range(1, len(cal))}
    ao["prev_date"] = ao["trade_date"].map(prev_map)
    ao = ao.dropna(subset=["prev_date"]).copy()

    prev_row = pn.rename(columns={
        "trade_date": "prev_date", "close": "pre_close",
        "industry": "industry", "universe_flag": "universe_flag", "amount_ma_20": "amount_ma_20"})
    ev = ao.merge(prev_row, on=["ts_code", "prev_date"], how="inner")
    ev = ev[ev["pre_close"] > 0].copy()

    # 个股gap(D日竞价撮合价/前日收盘-1)，事件域筛选: gap∈[-2%,+1%]闭区间 + 前日universe_flag>0
    ev["gap"] = ev["auct_open_price"] / ev["pre_close"] - 1.0
    ev = ev[(ev["gap"] >= -0.02) & (ev["gap"] <= 0.01)]
    ev = ev[ev["universe_flag"].fillna(0).astype(float) > 0]

    # 与合格板块(n_members>=8)行连接：industry用前日行的industry，trade_date=D，对齐board的
    # (industry, trade_date)。inner join天然实现了"n_members>=8"过滤(board已预筛)。
    ev = ev.merge(board, on=["industry", "trade_date"], how="inner", suffixes=("", "_board"))

    # D日非一字 + D日O2C出场收益：D日open/close取自load_daily_ohlc_for_dates(缓存优先/面板兜底)
    d_dates = set(ev["trade_date"].unique())
    dday = load_daily_ohlc_for_dates(d_dates).rename(columns={"open": "open_D", "close": "close_D"})
    ev = ev.merge(dday, on=["ts_code", "trade_date"], how="inner")
    ev = ev[(ev["open_D"] > 0) & (ev["close_D"] > 0)]
    is_yizi = (ev["open_D"] - ev["close_D"]).abs() < 1e-9
    n_yizi = int(is_yizi.sum())
    ev = ev[~is_yizi].copy()

    ev["o2c"] = ev["close_D"] / ev["open_D"] - 1.0

    if random_decision_feature:
        rng = np.random.RandomState(seed)
        ev["gap_breadth_2"] = rng.permutation(ev["gap_breadth_2"].to_numpy())
        ev["auct_amt_z"] = rng.permutation(ev["auct_amt_z"].to_numpy())
        ev["leader_gap"] = rng.permutation(ev["leader_gap"].to_numpy())
        board = board.copy()
        board["gap_breadth_2"] = rng.permutation(board["gap_breadth_2"].to_numpy())
        board["auct_amt_z"] = rng.permutation(board["auct_amt_z"].to_numpy())
        board["leader_gap"] = rng.permutation(board["leader_gap"].to_numpy())

    # ---- 三个决策特征(registry逐字) ----
    # sector_strength: 板块gap_breadth_2在"当日跨板块(限合格板块全集)"的三分位——注意三分位必须
    # 在board(合格板块全集,一行一板块)上按天计算，而不是在ev(一行一成员，会因成员数不同重复加权
    # 板块)上计算，否则等价于按板块成员数加权的三分位，偏离registry"跨板块"的原意(板块是分位的
    # 统计单位，不是成员)。计算好后再映射回ev。
    board["sector_strength"] = board.groupby("trade_date")["gap_breadth_2"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 3, labels=["弱", "中", "强"])
        if s.notna().sum() >= 3 else pd.Series([None] * len(s), index=s.index))
    board["sector_amt"] = np.where(board["auct_amt_z"] >= 1.0, "高", "低")
    board.loc[board["auct_amt_z"].isna(), "sector_amt"] = None
    board["leader"] = np.where(board["leader_gap"] >= 5.0, "强", "弱")

    ev = ev.drop(columns=["sector_strength", "sector_amt", "leader"], errors="ignore").merge(
        board[["industry", "trade_date", "sector_strength", "sector_amt", "leader"]],
        on=["industry", "trade_date"], how="left")
    n_before_feature_dropna = len(ev)
    ev = ev.dropna(subset=["sector_strength", "sector_amt", "leader"]).copy()
    n_dropped_undefined_feature = n_before_feature_dropna - len(ev)
    ev["sector_strength"] = ev["sector_strength"].astype(str)
    ev["sector_amt"] = ev["sector_amt"].astype(str)
    ev["leader"] = ev["leader"].astype(str)

    # ---- 成本：liq档(前日amount_ma_20,万元) x gap档(D日gap) ----
    amt_ma20_wan = (ev["amount_ma_20"] / 10.0) if "amount_ma_20" in ev.columns else pd.Series(np.nan, index=ev.index)
    ev["liq_bkt"] = liquidity_bucket_from_amount_ma(amt_ma20_wan)
    ev["gap_bkt"] = gap_bucket(ev["gap"])
    ev["_cost_dec"] = realized_cost_dec(ev["liq_bkt"], ev["gap_bkt"], slip, cost_mode)

    ev["ts_code"] = ev["ts_code"].astype(str)
    n_years = (pd.to_datetime(ev["trade_date"], format="%Y%m%d").max()
               - pd.to_datetime(ev["trade_date"], format="%Y%m%d").min()).days / 365.25 if len(ev) else 1.0

    grid_cells = []
    for ss in ["弱", "中", "强"]:
        for sa in ["低", "高"]:
            for ld_ in ["弱", "强"]:
                grid_cells.append({"sector_strength": ss, "sector_amt": sa, "leader": ld_})

    return {
        "event_df": ev, "board_all": board, "grid_cells": grid_cells,
        "grid_group_cols": ["sector_strength", "sector_amt", "leader"],
        "n_calendar_years": max(n_years, 0.5),
        "success_criteria_text": get_setup_def(load_registry(), "S5_sector_auction_resonance")["success_criteria"],
        "n_min_per_cell": 300,
        "diagnostics": {
            "n_saf_rows_total": int(n_saf_total),
            "n_saf_rows_n_members_ge_8": int(len(board)),
            "n_yizi_dropped": n_yizi,
            "n_dropped_feature_undefined_(sector_amt_nan_mostly)": int(n_dropped_undefined_feature),
        },
    }


# ============================== S3: 分时形态日频 ==============================

def load_setup_s3(cost_mode: str, slip: SlippageTable, random_decision_feature: bool, seed: int) -> dict:
    """S3_intraday_shape_daily 装载器(registry逐字，格点已由监督者20260703冻结)。

    数据: intraday_shape_features.parquet(800只研究样本x1087日，2022-2026，T收盘可知；
    该800只为随机抽样，非全市场——holdout另有800只留作幸存后复现，本轮禁碰，见registry note)。
    trade_date在该特征文件里是'YYYY-MM-DD'字符串，而panel_full/auction_open用'YYYYMMDD'，
    合并前必须转换，否则join为空(下面print一次转换前后样本自检)。

    事件域(registry逐字): T日，800只研究样本内，universe_flag>0，n_bars>=40(数据完整日)；
    T+1非一字且gap<=+7%(与S1同口径，无下限，仅+7%上限+一字排除)。

    决策特征(registry逐字): am_share=am_vol_share同日截面三分位(低/中/高)；
    late_sign=late_strength符号(尾盘30分钟收强/收弱)——0或NaN视为"符号未定义"，整行剔除出
    grid(不强行分进任一桶)，剔除计数分别报告(0值/NaN值)供审计对账；
    smooth=path_smoothness同日截面中位二分(<=中位→低，>中位→高；registry明确"中位二分"而非
    三分位，与am_share的三分位处理刻意不同，不可混淆；等于中位的行按保守/确定性规则并入"低"桶，
    与"高"桶严格用">"区分，避免二义性)。

    trade: 入场T+1开盘，出场T+1收盘(O2C)，无隔夜腿参与(与S5/S6同理，闸1结构性PASS)。
    grid: 3 am_share x 2 late_sign x 2 smooth = 12格，全部入账。
    """
    print("加载分时形态特征(intraday_shape_features.parquet)...")
    feat = pd.read_parquet(INTRADAY_SHAPE_FEATURES_PATH)
    n_feat_total = len(feat)
    sample_before = feat["trade_date"].iloc[0]
    feat["trade_date"] = pd.to_datetime(feat["trade_date"]).dt.strftime("%Y%m%d")
    sample_after = feat["trade_date"].iloc[0]
    print(f"  trade_date格式转换自检: 转换前样例={sample_before!r}  转换后样例={sample_after!r}"
          f"  (应为'YYYY-MM-DD'->'YYYYMMDD')")
    n_unique_codes = feat["ts_code"].nunique()
    print(f"  行数={n_feat_total}  unique ts_code={n_unique_codes}(registry预期800只研究样本)")

    # late_strength==0 / NaN 计数：在任何其他筛选之前，先在全量特征文件上如实统计(供审计对账用)
    n_late_zero_raw = int((feat["late_strength"] == 0).sum())
    n_late_nan_raw = int(feat["late_strength"].isna().sum())
    print(f"  late_strength诊断(全量,筛选前): ==0行数={n_late_zero_raw}  NaN行数={n_late_nan_raw}  "
          f"总行数={n_feat_total}")

    # n_bars>=40过滤(event_universe文本逐字要求，即便此数据几乎是no-op也照做)
    n_before_nbars = len(feat)
    feat = feat[feat["n_bars"] >= 40].copy()
    n_after_nbars = len(feat)
    print(f"  n_bars>=40过滤: {n_before_nbars} -> {n_after_nbars} (剔除{n_before_nbars - n_after_nbars})")

    print("加载面板(panel_full)...")
    cols = ["ts_code", "trade_date", "open", "close", "pct_chg", "amount", "amount_ma_20", "universe_flag"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn = pn[pn["universe_flag"].fillna(0).astype(float) > 0].copy()

    # universe_flag>0过滤 + 日历对齐：inner join天然只保留panel_full有的T日(registry"日历对齐")
    n_before_universe_join = len(feat)
    ev = feat.merge(pn, on=["ts_code", "trade_date"], how="inner")
    n_after_universe_join = len(ev)
    print(f"  universe_flag>0+日历对齐(inner join panel_full): {n_before_universe_join} -> {n_after_universe_join}")

    # T+1映射
    cal = sorted(pn["trade_date"].unique())
    next_map = build_calendar_next_map(cal)
    ev["t1"] = ev["trade_date"].map(next_map)
    n_before_t1_drop = len(ev)
    ev = ev.dropna(subset=["t1"]).copy()
    pdates = set(pn["trade_date"].unique())
    ev = ev[ev["t1"].isin(pdates)].copy()
    n_after_t1_join = len(ev)
    print(f"  T+1日历映射(next_map,且T+1须在panel日历内): {n_before_t1_drop} -> {n_after_t1_join}")

    # T+1竞价开盘(gap用) + T+1面板日线(open1/close1用)，与S1/S5同一模式
    print("加载竞价开盘窗口(auction_open_*)...")
    frames_o = [pd.read_parquet(p, columns=["ts_code", "trade_date", "close", "amount"])
                for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    ao = pd.concat(frames_o, ignore_index=True)
    ao["trade_date"] = ao["trade_date"].astype(str)
    ao = ao.rename(columns={"close": "auct_open_price", "amount": "auct_open_amount"})
    ao1 = ao.rename(columns={"trade_date": "t1"})[["ts_code", "t1", "auct_open_price", "auct_open_amount"]]
    n_before_ao_join = len(ev)
    ev = ev.merge(ao1, on=["ts_code", "t1"], how="inner")
    n_after_ao_join = len(ev)
    print(f"  T+1竞价开盘窗口join: {n_before_ao_join} -> {n_after_ao_join}")

    t1_dates = set(ev["t1"].unique())
    p1 = load_daily_ohlc_for_dates(t1_dates).rename(columns={"trade_date": "t1", "open": "open1", "close": "close1"})
    ev = ev.merge(p1, on=["ts_code", "t1"], how="inner")
    ev = ev[(ev["open1"] > 0) & (ev["close1"] > 0) & (ev["auct_open_price"] > 0) & (ev["close"] > 0)].copy()
    n_after_t1_ohlc_join = len(ev)
    print(f"  T+1面板OHLC join + 正值过滤: -> {n_after_t1_ohlc_join}")

    ev["t1_gap"] = ev["auct_open_price"] / ev["close"] - 1.0

    # 一字判定：T+1开盘==T+1收盘(无日内波动)视为一字(不可成交)
    is_yizi = (ev["open1"] - ev["close1"]).abs() < 1e-9
    n_yizi = int(is_yizi.sum())
    ev = ev[~is_yizi].copy()
    n_after_yizi_drop = len(ev)
    print(f"  一字剔除: {n_yizi}行  剩余={n_after_yizi_drop}")

    # gap<=+7%上限过滤(registry逐字："T+1非一字且gap<=+7%"——与S1一致，无下限，不发明未注册的下界)
    n_before_gap = len(ev)
    ev = ev[ev["t1_gap"] <= 0.07].copy()
    n_after_gap_drop = len(ev)
    print(f"  gap<=+7%过滤: {n_before_gap} -> {n_after_gap_drop} (剔除{n_before_gap - n_after_gap_drop})")

    # late_strength==0/NaN 在"其余过滤之后、late_sign剔除之前"这一群体内的计数(用于12格对账)
    n_qualify_before_late_sign_exclusion = len(ev)
    n_late_zero_in_qualified = int((ev["late_strength"] == 0).sum())
    n_late_nan_in_qualified = int(ev["late_strength"].isna().sum())

    if random_decision_feature:
        rng = np.random.RandomState(seed)
        ev["am_vol_share"] = rng.permutation(ev["am_vol_share"].to_numpy())
        ev["late_strength"] = rng.permutation(ev["late_strength"].to_numpy())
        ev["path_smoothness"] = rng.permutation(ev["path_smoothness"].to_numpy())

    # ---- 三个决策特征(registry逐字) ----
    # am_share: am_vol_share同日截面三分位，与S1的ca_amt_share_terc同一qcut-of-rank写法
    ev["am_share"] = ev.groupby("trade_date")["am_vol_share"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 3, labels=["低", "中", "高"])
        if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))

    # late_sign: late_strength符号。0/NaN一律排除出grid(不强行分桶)——registry该特征只有"收强/收弱"
    # 两个命名状态，0是"未定义符号"，与NaN同等对待剔除，而非归入弱(或强)桶(那样会掺入非本意样本，
    # 是更保守/更不利于让格点看起来达标的处理，符合"有歧义按更容易REJECTED的方向解读"的纪律)。
    late_sign_raw = np.sign(ev["late_strength"])
    late_sign_raw = pd.Series(np.where(late_sign_raw == 0, np.nan, late_sign_raw), index=ev.index)
    ev["late_sign"] = late_sign_raw.map({1.0: "强", -1.0: "弱"})

    # smooth: path_smoothness同日截面中位二分。tie-handling: <=中位 -> "低"，>中位 -> "高"
    # (确定性、保守：等于中位的行不会被两边都认领，也不会被丢弃，落入更"平"的一侧)。
    med_smooth = ev.groupby("trade_date")["path_smoothness"].transform("median")
    ev["smooth"] = np.where(ev["path_smoothness"] <= med_smooth, "低", "高")
    ev.loc[ev["path_smoothness"].isna() | med_smooth.isna(), "smooth"] = None

    # ---- 出场收益：纯O2C(无隔夜腿参与，c2o/c2c留空供闸1硬编码PASS时展示) ----
    # 注意：o2c/成本在 late_sign 剔除之前计算——闸4安慰剂要在"late_sign剔除前的完整事件域"上做
    # 同日捆绑打乱再重选(真实分桶的三分位/中位阈值也是在这同一群体上算的，见上)，因此该群体的
    # 每一行都必须已带 o2c/_cost_dec；真实格点与安慰剂两侧由此共用同一套逐行成本与同一套分桶
    # 阈值基准，比较才对称(参见 gate4_placebo docstring 里记录的历史bug教训)。
    ev["o2c"] = ev["close1"] / ev["open1"] - 1.0

    # ---- 成本：liq档(amount_ma_20万元) x gap档(t1_gap) ----
    # liq三分位在"late_sign剔除前"的合格事件域上计算(与分桶阈值同一群体基准，保证真实/安慰剂
    # 成本一致；阈值与post-剔除群体的差异极小，且该分档本就是相对分位代理)。
    amt_ma20_wan = (ev["amount_ma_20"] / 10.0) if "amount_ma_20" in ev.columns else pd.Series(np.nan, index=ev.index)
    ev["liq_bkt"] = liquidity_bucket_from_amount_ma(amt_ma20_wan)
    ev["gap_bkt"] = gap_bucket(ev["t1_gap"])
    ev["_cost_dec"] = realized_cost_dec(ev["liq_bkt"], ev["gap_bkt"], slip, cost_mode)

    # 闸4安慰剂用的完整事件域快照(late_sign=0/NaN行仍在内——它们的三元组参与同日打乱池；
    # 但其late_sign为NaN，真实/安慰剂的cell选择都永远选不中它们，与"不塞进任一档"纪律一致)。
    ev_universe_pre_late = ev.copy()

    # late_sign为NaN(0或NaN原值)的行整行剔除出grid——这是registry"符号"特征的题中之义，
    # 与am_share/smooth的NaN处理(该两者也用NaN表示未定义，同样会被下面的dropna一并剔除)一致。
    n_before_late_sign_dropna = len(ev)
    ev = ev.dropna(subset=["am_share", "late_sign", "smooth"]).copy()
    n_after_late_sign_dropna = len(ev)
    n_dropped_at_bucket_dropna = n_before_late_sign_dropna - n_after_late_sign_dropna
    print(f"  三特征分桶后dropna(late_sign=0/NaN为主因，另含am_share/smooth自身NaN): "
          f"{n_before_late_sign_dropna} -> {n_after_late_sign_dropna} (剔除{n_dropped_at_bucket_dropna})")

    ev["am_share"] = ev["am_share"].astype(str)
    ev["late_sign"] = ev["late_sign"].astype(str)
    ev["smooth"] = ev["smooth"].astype(str)

    ev["ts_code"] = ev["ts_code"].astype(str)
    n_years = (pd.to_datetime(ev["trade_date"], format="%Y%m%d").max()
               - pd.to_datetime(ev["trade_date"], format="%Y%m%d").min()).days / 365.25 if len(ev) else 1.0

    grid_cells = []
    for am in ["低", "中", "高"]:
        for ls in ["强", "弱"]:
            for sm in ["低", "高"]:
                grid_cells.append({"am_share": am, "late_sign": ls, "smooth": sm})

    return {
        "event_df": ev, "event_universe_pre_late": ev_universe_pre_late,
        "grid_cells": grid_cells, "grid_group_cols": ["am_share", "late_sign", "smooth"],
        "n_calendar_years": max(n_years, 0.5),
        "success_criteria_text": get_setup_def(load_registry(), "S3_intraday_shape_daily")["success_criteria"],
        "n_min_per_cell": 300,
        "diagnostics": {
            "n_feat_rows_total_raw": int(n_feat_total),
            "n_unique_ts_code_raw": int(n_unique_codes),
            "n_late_strength_zero_raw": n_late_zero_raw,
            "n_late_strength_nan_raw": n_late_nan_raw,
            "n_bars_filter": {"before": int(n_before_nbars), "after": int(n_after_nbars)},
            "universe_join": {"before": int(n_before_universe_join), "after": int(n_after_universe_join)},
            "t1_calendar_join": {"before": int(n_before_t1_drop), "after": int(n_after_t1_join)},
            "t1_ohlc_join": int(n_after_t1_ohlc_join),
            "n_yizi_dropped": n_yizi,
            "gap_le_7pct_filter": {"before": int(n_before_gap), "after": int(n_after_gap_drop)},
            "n_qualify_before_late_sign_exclusion": int(n_qualify_before_late_sign_exclusion),
            "n_late_strength_zero_in_qualified": n_late_zero_in_qualified,
            "n_late_strength_nan_in_qualified": n_late_nan_in_qualified,
            "n_dropped_at_bucket_dropna_total": int(n_dropped_at_bucket_dropna),
            "n_events_final": int(len(ev)),
        },
    }


# ============================== S4: regime门控死形态复活 ==============================

def load_setup_s4(cost_mode: str, slip: SlippageTable, random_decision_feature: bool, seed: int) -> dict:
    """S4_regime_gated_revival 装载器(registry逐字，格点已冻结，本函数仿S3/S5结构)。

    数据: panel_full(触发特征pct_chg/f_close_pos_20/f_volume_ratio_20 + universe_flag/industry/
    amount_ma_20都在此)，market_regime_features.parquet(T日收盘可知的regime_tag/amount_z20，
    见build_market_regime_features.py注释)，auction_open(T+1 gap/一字判定，与S1/S3/S5同口径)。

    事件域(registry逐字，固定不调): T日 universe_flag>0 且非688(ts_code不以'688'开头)且非.BJ
    (ts_code不以'.BJ'结尾)；触发="放量突破代理": pct_chg∈[3,9.4] 且 f_close_pos_20>=0.9 且
    f_volume_ratio_20>=2(闭区间边界按题目原文3和9.4取<=/>=)；T+1非一字且gap<=+7%(与S1/S3/S5
    同一口径：T+1 auction_open撮合价定gap，T+1面板open==close视为一字)。

    决策特征(registry逐字): regime_tag=T日市场regime三态(活跃/中性/冰点，来自
    market_regime_features.parquet，未调参先验粗分，本引擎不重新分类只读取)；
    amt_regime=T日amount_z20符号(市场放量/缩量)——市场级amount_z20是连续值，"符号"按
    >0记"正"(放量)/<=0记"非正"(缩量)二分(0记非正，保守且确定性处理，与smooth的中位二分
    tie-handling同一纪律：等于阈值的行不会两边都认领)；amount_z20为NaN(全量仅4/1303日，
    该特征本身在T=市场级尚不足20日滚动样本时未定义)的交易日整日剔除出grid(不强行分桶，
    与S3的late_sign=NaN处理同一纪律)。

    grid: 3 regime_tag x 2 amt_regime = 6格，全部入账。
    trade: 入场T+1开盘，出场T+1收盘(O2C)，无隔夜腿参与(与S3/S5/S6同理，闸1结构性PASS)。
    """
    print("加载面板(panel_full)，取触发特征列...")
    cols = ["ts_code", "trade_date", "open", "close", "pct_chg", "amount", "amount_ma_20",
            "universe_flag", "f_close_pos_20", "f_volume_ratio_20"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)
    n_pn_total = len(pn)

    # universe_flag>0 且 非688 且 非.BJ (registry_default口径，本剧本event_universe逐字重申)
    pn = pn[pn["universe_flag"].fillna(0).astype(float) > 0].copy()
    n_after_universe = len(pn)
    pn = pn[~pn["ts_code"].str.startswith("688")].copy()
    n_after_ex688 = len(pn)
    pn = pn[~pn["ts_code"].str.endswith(".BJ")].copy()
    n_after_exbj = len(pn)
    print(f"  面板行数: 总={n_pn_total} universe_flag>0后={n_after_universe} "
          f"排除688后={n_after_ex688} 排除.BJ后={n_after_exbj}")

    # 触发(固定不调): 放量突破代理
    n_before_trigger = len(pn)
    trig = ((pn["pct_chg"] >= 3.0) & (pn["pct_chg"] <= 9.4)
            & (pn["f_close_pos_20"] >= 0.9) & (pn["f_volume_ratio_20"] >= 2.0))
    ev = pn[trig].copy()
    n_after_trigger = len(ev)
    print(f"  触发过滤(pct_chg∈[3,9.4]且f_close_pos_20>=0.9且f_volume_ratio_20>=2): "
          f"{n_before_trigger} -> {n_after_trigger}")

    print("加载市场regime特征(market_regime_features.parquet)...")
    mrf = pd.read_parquet(MARKET_REGIME_FEATURES_PATH)
    mrf["trade_date"] = mrf["trade_date"].astype(str)
    n_mrf_total = len(mrf)
    n_amt_z20_nan = int(mrf["amount_z20"].isna().sum())
    mrf["amt_regime"] = np.where(mrf["amount_z20"] > 0.0, "正", "非正")
    mrf.loc[mrf["amount_z20"].isna(), "amt_regime"] = None
    print(f"  regime特征行数={n_mrf_total}  amount_z20为NaN的交易日={n_amt_z20_nan}(该日整日剔除出grid)")
    print(f"  regime_tag分布(全量regime文件): {mrf['regime_tag'].value_counts().to_dict()}")

    # T日 join regime(市场级，同日全部股票同值)：inner join天然剔除regime文件缺失的T日
    n_before_regime_join = len(ev)
    ev = ev.merge(mrf[["trade_date", "regime_tag", "amt_regime", "amount_z20"]], on="trade_date", how="inner")
    n_after_regime_join = len(ev)
    print(f"  T日regime join: {n_before_regime_join} -> {n_after_regime_join}")

    # T+1映射 + 竞价开盘窗口(gap用) + T+1面板OHLC(入场/出场用)，与S1/S3/S5同一模式
    cal = sorted(pn["trade_date"].unique())
    next_map = build_calendar_next_map(cal)
    ev["t1"] = ev["trade_date"].map(next_map)
    n_before_t1_drop = len(ev)
    ev = ev.dropna(subset=["t1"]).copy()
    pdates = set(pn["trade_date"].unique())
    ev = ev[ev["t1"].isin(pdates)].copy()
    n_after_t1_join = len(ev)
    print(f"  T+1日历映射: {n_before_t1_drop} -> {n_after_t1_join}")

    print("加载竞价开盘窗口(auction_open_*)...")
    frames_o = [pd.read_parquet(p, columns=["ts_code", "trade_date", "close", "amount"])
                for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    ao = pd.concat(frames_o, ignore_index=True)
    ao["trade_date"] = ao["trade_date"].astype(str)
    ao = ao.rename(columns={"close": "auct_open_price", "amount": "auct_open_amount"})
    ao1 = ao.rename(columns={"trade_date": "t1"})[["ts_code", "t1", "auct_open_price", "auct_open_amount"]]
    n_before_ao_join = len(ev)
    ev = ev.merge(ao1, on=["ts_code", "t1"], how="inner")
    n_after_ao_join = len(ev)
    print(f"  T+1竞价开盘窗口join: {n_before_ao_join} -> {n_after_ao_join}")

    t1_dates = set(ev["t1"].unique())
    p1 = load_daily_ohlc_for_dates(t1_dates).rename(columns={"trade_date": "t1", "open": "open1", "close": "close1"})
    ev = ev.merge(p1, on=["ts_code", "t1"], how="inner")
    ev = ev[(ev["open1"] > 0) & (ev["close1"] > 0) & (ev["auct_open_price"] > 0) & (ev["close"] > 0)].copy()
    n_after_t1_ohlc_join = len(ev)
    print(f"  T+1面板OHLC join + 正值过滤: -> {n_after_t1_ohlc_join}")

    ev["t1_gap"] = ev["auct_open_price"] / ev["close"] - 1.0

    # 一字判定：T+1开盘==T+1收盘(无日内波动)视为一字(不可成交)
    is_yizi = (ev["open1"] - ev["close1"]).abs() < 1e-9
    n_yizi = int(is_yizi.sum())
    ev = ev[~is_yizi].copy()
    n_after_yizi_drop = len(ev)
    print(f"  一字剔除: {n_yizi}行  剩余={n_after_yizi_drop}")

    # gap<=+7%上限过滤(registry逐字，无下限)
    n_before_gap = len(ev)
    ev = ev[ev["t1_gap"] <= 0.07].copy()
    n_after_gap_drop = len(ev)
    print(f"  gap<=+7%过滤: {n_before_gap} -> {n_after_gap_drop} (剔除{n_before_gap - n_after_gap_drop})")

    # 达标事件全集(amt_regime剔除NaN日之前)：供安慰剂打乱池使用，与真实分桶同一群体基准
    n_qualify_before_amt_regime_exclusion = len(ev)

    if random_decision_feature:
        # selftest-random: 按题目"regime标签按日随机重排应REJECTED"要求——把regime_tag/amt_regime
        # 在(与真实打乱同样的)日级别上随机置换(而非逐行独立打乱，逐行会破坏"市场级同日同值"这个
        # 前提，不是本剧本要测的对抗场景)。仿照下面day-block placebo的同一实现，但这里是把
        # "决策特征"本身替换为随机版本(供闸2-4整体在随机特征下跑一遍)。
        rng = np.random.RandomState(seed)
        day_tab = ev.drop_duplicates(subset=["trade_date"])[["trade_date", "regime_tag", "amt_regime"]].reset_index(drop=True)
        day_tab["regime_tag"] = rng.permutation(day_tab["regime_tag"].to_numpy())
        day_tab["amt_regime"] = rng.permutation(day_tab["amt_regime"].to_numpy())
        ev = ev.drop(columns=["regime_tag", "amt_regime"]).merge(day_tab, on="trade_date", how="left")

    # amt_regime为NaN(amount_z20当日未定义)的行整行剔除出grid——与S3的late_sign=NaN处理同一纪律
    n_before_amt_regime_dropna = len(ev)
    ev = ev.dropna(subset=["regime_tag", "amt_regime"]).copy()
    n_after_amt_regime_dropna = len(ev)
    n_dropped_amt_regime_nan = n_before_amt_regime_dropna - n_after_amt_regime_dropna
    print(f"  amt_regime(amount_z20 NaN日)剔除: {n_before_amt_regime_dropna} -> {n_after_amt_regime_dropna} "
          f"(剔除{n_dropped_amt_regime_nan})")

    ev["regime_tag"] = ev["regime_tag"].astype(str)
    ev["amt_regime"] = ev["amt_regime"].astype(str)

    # ---- 出场收益：纯O2C(无隔夜腿参与) ----
    ev["o2c"] = ev["close1"] / ev["open1"] - 1.0

    # ---- 成本：liq档(amount_ma_20万元) x gap档(t1_gap) ----
    amt_ma20_wan = (ev["amount_ma_20"] / 10.0) if "amount_ma_20" in ev.columns else pd.Series(np.nan, index=ev.index)
    ev["liq_bkt"] = liquidity_bucket_from_amount_ma(amt_ma20_wan)
    ev["gap_bkt"] = gap_bucket(ev["t1_gap"])
    ev["_cost_dec"] = realized_cost_dec(ev["liq_bkt"], ev["gap_bkt"], slip, cost_mode)

    # 闸4(day-block)安慰剂用的完整事件域快照(amt_regime剔除NaN日之前)——打乱池需要与真实分桶
    # 同一群体基准；同时准备"事件覆盖的交易日 x (regime_tag, amt_regime)"这张日级表供打乱使用
    # (day-block打乱的对象是这张表，而非ev本身逐行——市场级标签一天只有一个值)。
    ev_universe_pre_amt_regime = ev.copy()  # 注：此时已完成上面的dropna，用作真实分桶群体基准
    # 为day-block placebo准备"事件覆盖的T日集合 x regime标签"日级表：只保留事件全集
    # (ev_universe_pre_amt_regime，即真实触发且已过T+1一字/gap筛选)实际出现过的T日，
    # 这样打乱池的"日历天"就是"本剧本事件覆盖到的交易日"集合，而非regime文件里的全部1303天。
    event_trade_dates = set(ev_universe_pre_amt_regime["trade_date"].unique())
    day_regime_table = (mrf[mrf["trade_date"].isin(event_trade_dates)]
                        [["trade_date", "regime_tag", "amt_regime"]].drop_duplicates(subset=["trade_date"])
                        .reset_index(drop=True))

    ev["ts_code"] = ev["ts_code"].astype(str)
    n_years = (pd.to_datetime(ev["trade_date"], format="%Y%m%d").max()
               - pd.to_datetime(ev["trade_date"], format="%Y%m%d").min()).days / 365.25 if len(ev) else 1.0

    grid_cells = []
    for rt in ["活跃", "中性", "冰点"]:
        for ar in ["正", "非正"]:
            grid_cells.append({"regime_tag": rt, "amt_regime": ar})

    return {
        "event_df": ev, "event_universe_pre_amt_regime": ev_universe_pre_amt_regime,
        "day_regime_table": day_regime_table,
        "grid_cells": grid_cells, "grid_group_cols": ["regime_tag", "amt_regime"],
        "n_calendar_years": max(n_years, 0.5),
        "success_criteria_text": get_setup_def(load_registry(), "S4_regime_gated_revival")["success_criteria"],
        "n_min_per_cell": 300,
        "diagnostics": {
            "n_panel_rows_total": int(n_pn_total),
            "n_after_universe_flag": int(n_after_universe),
            "n_after_ex688": int(n_after_ex688),
            "n_after_exBJ": int(n_after_exbj),
            "n_after_trigger": int(n_after_trigger),
            "n_mrf_rows_total": int(n_mrf_total),
            "n_amount_z20_nan_days_in_mrf": n_amt_z20_nan,
            "regime_tag_value_counts_full_mrf": mrf["regime_tag"].value_counts().to_dict(),
            "t1_calendar_join": {"before": int(n_before_t1_drop), "after": int(n_after_t1_join)},
            "t1_ao_join": {"before": int(n_before_ao_join), "after": int(n_after_ao_join)},
            "t1_ohlc_join": int(n_after_t1_ohlc_join),
            "n_yizi_dropped": n_yizi,
            "gap_le_7pct_filter": {"before": int(n_before_gap), "after": int(n_after_gap_drop)},
            "n_qualify_before_amt_regime_exclusion": int(n_qualify_before_amt_regime_exclusion),
            "n_dropped_amt_regime_nan": int(n_dropped_amt_regime_nan),
            "n_events_final": int(len(ev)),
            "n_placebo_day_pool": int(len(day_regime_table)),
        },
    }


# ============================== S7: 尾盘超卖承接 ==============================

def _small_day_drop(ev: pd.DataFrame, date_col: str = "trade_date", min_n: int = 6) -> tuple[pd.DataFrame, dict]:
    """题目/registry逐字："同日事件数<6则该日整日剔除(披露)"。返回过滤后df + 披露信息
    (剔除天数、剔除行数)，供报告如实展示，不静默丢弃。"""
    day_n = ev.groupby(date_col)[date_col].transform("count")
    keep = day_n >= min_n
    n_days_total = ev[date_col].nunique()
    n_days_dropped = ev.loc[~keep, date_col].nunique()
    n_rows_dropped = int((~keep).sum())
    out = ev[keep].copy()
    disclosure = {
        "min_events_per_day": min_n, "n_days_total_before": int(n_days_total),
        "n_days_dropped_small_day": int(n_days_dropped), "n_rows_dropped_small_day": n_rows_dropped,
        "n_days_after": int(out[date_col].nunique()) if len(out) else 0,
    }
    return out, disclosure


def load_setup_s7(cost_mode: str, random_decision_feature: bool, seed: int) -> dict:
    """S7_late_oversold_absorb 装载器(registry逐字，格点20260706冻结)。

    数据: late_features.parquet(14:45可知，研究800，见文件头schema)，panel_full(universe_flag/
    pct_chg/涨跌停代理/日历对齐)。若late_features.parquet尚不存在(另一任务并行构建中)，本函数
    会抛FileNotFoundError；--selftest-random在文件不存在时改用合成假数据驱动(见main()/selftest
    专用分支)，与本函数本身无关(本函数只认真实/合成后落盘的parquet，两者schema一致)。

    事件域(registry逐字): T日: late_features全体(即"研究800内"，该文件本身即研究800样本口径)，
    universe_flag>0(join panel_full)，非688，非.BJ，n_bars_1445>=40，not_sealed_1445(14:45未封板，
    late_features已算好的布尔列)，drawdown_from_high<=-0.03(距日内高点回落>=3%)，
    非paused_resume_first_day(非停牌复牌首日)；同日事件数<6则该日整日剔除(披露)。

    决策特征(registry逐字): vwap_dev_1445=decision_px/当日VWAP@1445-1，同日事件域内三分位
    (深/中/浅，deep/mid/shallow——命名依"回落越深"的经济学直觉：dev越负=偏离VWAP越深)；
    stab_minutes二分(>=30企稳 vs <30破位)。grid=3x2=6格。

    trade: entry_px=T日entry_vwap_1445_1455；exit_px=T+1(日历对齐，停牌顺延到下一有late_features
    行的日子，顺延计数披露)morning_vwap_0935_0945；敏感口径=T+1 close_px仅披露不参与判定。
    毛收益=exit_px/entry_px-1；成本=S7S8_COST_DEC冻结常数(med=0.0026/p75=0.0046)。

    披露列: gap_sign/tail_vol_share_1400_1445/C2O-O2C分解(entry_px->T+1开盘 vs T+1开盘->exit_px，
    需panel_full T+1 open)。
    """
    if not LATE_FEATURES_PATH.exists():
        raise FileNotFoundError(f"late_features.parquet不存在: {LATE_FEATURES_PATH}"
                                 f"(由另一任务并行构建，尚未出现；--selftest走合成假数据分支)")
    print("加载late_features.parquet...")
    feat = pd.read_parquet(LATE_FEATURES_PATH)
    feat["ts_code"] = feat["ts_code"].astype(str)
    feat["trade_date"] = feat["trade_date"].astype(str)
    n_feat_total = len(feat)
    print(f"  late_features行数={n_feat_total}  unique ts_code={feat['ts_code'].nunique()}")

    print("加载面板(panel_full)...")
    cols = ["ts_code", "trade_date", "open", "close", "universe_flag", "amount_ma_20"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["ts_code"] = pn["ts_code"].astype(str)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn_u = pn[pn["universe_flag"].fillna(0).astype(float) > 0].copy()

    n_before_universe = len(feat)
    ev = feat.merge(pn_u[["ts_code", "trade_date", "amount_ma_20"]], on=["ts_code", "trade_date"], how="inner")
    n_after_universe = len(ev)
    print(f"  universe_flag>0(join panel_full): {n_before_universe} -> {n_after_universe}")

    n_before_exch = len(ev)
    ev = ev[~ev["ts_code"].str.startswith("688")].copy()
    ev = ev[~ev["ts_code"].str.endswith(".BJ")].copy()
    n_after_exch = len(ev)
    print(f"  排除688/.BJ: {n_before_exch} -> {n_after_exch}")

    # not_sealed_1445为NaN(实测: late_features约60%行为NaN，对应14:45前数据不足以判定的日子，
    # 如全天封板/分钟数据缺口)按保守口径视为"未确认未封板"整行剔除(不强行当作True/False)，
    # 与smooth/late_sign等特征NaN剔除同一纪律(有歧义则剔除，不硬塞入事件域)。
    n_before_core = len(ev)
    core_mask = ((ev["n_bars_1445"] >= 40) & (ev["not_sealed_1445"].fillna(False).astype(bool))
                 & (ev["drawdown_from_high"] <= -0.03)
                 & (~ev["paused_resume_first_day"].fillna(False).astype(bool)))
    # n_bars_1445是nullable Int64，比较结果可能含<NA>(如n_bars_1445本身为NaN)——显式fillna(False)
    # 避免依赖pandas版本对"布尔索引器含<NA>"的隐式处理行为，保证跨版本确定性剔除(不当作True)。
    ev = ev[core_mask.fillna(False).astype(bool)].copy()
    n_after_core = len(ev)
    print(f"  核心事件域(n_bars_1445>=40且not_sealed_1445且drawdown<=-3%且非停牌复牌首日): "
          f"{n_before_core} -> {n_after_core}")

    ev, small_day_disclosure = _small_day_drop(ev, "trade_date", 6)
    print(f"  同日事件数<6整日剔除: 剔除天数={small_day_disclosure['n_days_dropped_small_day']} "
          f"剔除行数={small_day_disclosure['n_rows_dropped_small_day']}  剩余={len(ev)}")

    # ---- 决策特征(registry逐字)：同日事件域内三分位/二分 ----
    if random_decision_feature:
        rng = np.random.RandomState(seed)
        ev["vwap_dev_1445"] = rng.permutation(ev["vwap_dev_1445"].to_numpy())
        ev["stab_minutes"] = rng.permutation(ev["stab_minutes"].to_numpy())

    ev["vwap_dev_terc"] = ev.groupby("trade_date")["vwap_dev_1445"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 3, labels=["深", "中", "浅"])
        if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))
    ev["stab_bkt"] = np.where(ev["stab_minutes"] >= 30, ">=30", "<30")
    ev.loc[ev["stab_minutes"].isna(), "stab_bkt"] = None

    n_before_bucket_dropna = len(ev)
    ev = ev.dropna(subset=["vwap_dev_terc", "stab_bkt"]).copy()
    n_dropped_bucket_nan = n_before_bucket_dropna - len(ev)
    print(f"  分桶dropna(三分位/二分自身NaN，如某日不足3只): {n_before_bucket_dropna} -> {len(ev)} "
          f"(剔除{n_dropped_bucket_nan})")

    # ---- T+1日历对齐(停牌顺延到下一有late_features行的日子，顺延计数披露) ----
    market_cal = sorted(pn["trade_date"].unique())
    next_map = build_calendar_next_map(market_cal)
    ev["t1_target"] = ev["trade_date"].map(next_map)
    n_before_t1_target = len(ev)
    ev = ev.dropna(subset=["t1_target"]).copy()
    print(f"  T日在市场日历有下一交易日: {n_before_t1_target} -> {len(ev)}")

    avail_by_code = {code: np.array(sorted(g["trade_date"].to_numpy()))
                      for code, g in feat.groupby("ts_code")}
    resolved, deferred_flag = forward_defer_to_next_available(
        ev["t1_target"], ev["ts_code"], avail_by_code, market_cal)
    ev["t1"] = resolved
    ev["t1_deferred"] = deferred_flag
    n_before_t1_resolve = len(ev)
    ev = ev.dropna(subset=["t1"]).copy()
    n_deferred = int(ev["t1_deferred"].sum())
    print(f"  T+1顺延解析(停牌顺延到下一有late_features行的日子): 可解析={len(ev)}/{n_before_t1_resolve}  "
          f"其中发生顺延(非T+1当天)={n_deferred}行")

    # T+1 late_features行(取exit_px=morning_vwap_0935_0945 与 T+1 close_px敏感口径)
    t1_feat = feat[["ts_code", "trade_date", "morning_vwap_0935_0945", "close_px"]].rename(
        columns={"trade_date": "t1", "morning_vwap_0935_0945": "exit_vwap_t1", "close_px": "close_t1_sensitivity"})
    n_before_t1_join = len(ev)
    ev = ev.merge(t1_feat, on=["ts_code", "t1"], how="inner")
    n_after_t1_join = len(ev)
    print(f"  T+1(顺延解析后)late_features行join: {n_before_t1_join} -> {n_after_t1_join}")

    # T+1 panel开盘价(供C2O/O2C分解披露用)
    t1_dates = set(ev["t1"].unique())
    p1 = load_daily_ohlc_for_dates(t1_dates).rename(columns={"trade_date": "t1", "open": "open_t1"})
    ev = ev.merge(p1[["ts_code", "t1", "open_t1"]], on=["ts_code", "t1"], how="left")

    ev = ev[(ev["entry_vwap_1445_1455"] > 0) & (ev["exit_vwap_t1"] > 0)].copy()
    n_final = len(ev)
    print(f"  正值过滤(entry/exit VWAP>0): -> {n_final}")

    # 价格一致性守卫(20260706监督者补,事故护栏: 分钟摄取批次单位错位曾致VWAP=真实价1/100,
    # 制造+101倍假alpha;源头已在build_late_features自愈,此处为第二道防线,防未来批次复发):
    # 入场价须与同日14:45决策价同数量级;出场价须与出场日收盘同数量级。越界=数据错,剔除并计数。
    _r_in = ev["entry_vwap_1445_1455"] / ev["decision_px"]
    _r_out = ev["exit_vwap_t1"] / ev["close_t1_sensitivity"]
    _consistent = _r_in.between(0.6, 1.67) & (_r_out.between(0.6, 1.67) | _r_out.isna())
    n_guard_drop = int((~_consistent).sum())
    ev = ev[_consistent].copy()
    print(f"  价格一致性守卫(0.6-1.67x同日价): 剔除{n_guard_drop}行 -> {len(ev)}")

    # ---- 交易收益(registry逐字) ----
    ev["gross"] = ev["exit_vwap_t1"] / ev["entry_vwap_1445_1455"] - 1.0
    ev["_cost_dec"] = S7S8_COST_DEC[cost_mode]
    ev["net"] = ev["gross"] - ev["_cost_dec"]
    ev["sensitivity_close_gross"] = ev["close_t1_sensitivity"] / ev["entry_vwap_1445_1455"] - 1.0  # 仅披露

    # 披露：C2O(entry_px->T+1开盘) / O2C(T+1开盘->exit_px) 分解(仅审计披露，不作否决项——
    # gate1_rewrite_prereg已把闸1改写为"持仓区间净须为正"，即上面的gross/net本身)
    ev["c2o_disclose"] = np.where(ev["open_t1"].notna() & (ev["open_t1"] > 0),
                                   ev["open_t1"] / ev["entry_vwap_1445_1455"] - 1.0, np.nan)
    ev["o2c_disclose"] = np.where(ev["open_t1"].notna() & (ev["open_t1"] > 0),
                                   ev["exit_vwap_t1"] / ev["open_t1"] - 1.0, np.nan)

    ev["ts_code"] = ev["ts_code"].astype(str)
    ev["vwap_dev_terc"] = ev["vwap_dev_terc"].astype(str)
    ev["stab_bkt"] = ev["stab_bkt"].astype(str)
    n_years = (pd.to_datetime(ev["trade_date"], format="%Y%m%d").max()
               - pd.to_datetime(ev["trade_date"], format="%Y%m%d").min()).days / 365.25 if len(ev) else 1.0

    grid_cells = []
    for terc in ["深", "中", "浅"]:
        for sb in [">=30", "<30"]:
            grid_cells.append({"vwap_dev_terc": terc, "stab_bkt": sb})

    def _bucketize(d2: pd.DataFrame) -> pd.DataFrame:
        d2 = d2.copy()
        d2["vwap_dev_terc"] = d2.groupby("trade_date")["vwap_dev_1445"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=["深", "中", "浅"])
            if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))
        d2["stab_bkt"] = np.where(d2["stab_minutes"] >= 30, ">=30", "<30")
        d2.loc[d2["stab_minutes"].isna(), "stab_bkt"] = None
        d2["vwap_dev_terc"] = d2["vwap_dev_terc"].astype(str)
        d2["stab_bkt"] = d2["stab_bkt"].astype(str)
        return d2

    return {
        "event_df": ev, "grid_cells": grid_cells, "grid_group_cols": ["vwap_dev_terc", "stab_bkt"],
        "raw_cols": ["vwap_dev_1445", "stab_minutes"], "bucket_cols": ["vwap_dev_terc", "stab_bkt"],
        "bucketize_fn": _bucketize,
        "n_calendar_years": max(n_years, 0.5),
        "success_criteria_text": get_setup_def(load_registry(), "S7_late_oversold_absorb")["success_criteria"],
        "n_min_per_cell": 300,
        "cost_dec": S7S8_COST_DEC[cost_mode],
        "diagnostics": {
            "n_feat_rows_total": int(n_feat_total),
            "n_after_universe_flag": int(n_after_universe),
            "n_after_ex688_exBJ": int(n_after_exch),
            "n_after_core_event_universe": int(n_after_core),
            "small_day_disclosure": small_day_disclosure,
            "n_dropped_bucket_nan": int(n_dropped_bucket_nan),
            "n_t1_deferred_to_next_available_bar": int(n_deferred),
            "n_events_final": int(n_final),
        },
    }


# ============================== S8: 首板次日尾盘承接 ==============================

def load_setup_s8(cost_mode: str, random_decision_feature: bool, seed: int) -> dict:
    """S8_first_board_tail_absorb 装载器(registry逐字，格点20260706冻结)。

    事件锚(registry逐字，硬化定义): T日首板: close>=high*0.999 且 pct_chg∈[9.7,10.3](主板60/00)
    或∈[19.6,20.4](创业板300)；T-1同口径非涨停；上市>=60交易日(近似：本引擎只有listing_days=
    (trade_date-list_date).dt.days这一日历天列，非交易天列，按60交易日≈84自然日的保守换算取
    listing_days>=90作近似口径，如实披露此近似——见口径清单)；688/BJ排除。

    事件域(registry逐字): 首板事件(T日锚定)→事件行=T+1日该股(T+1需有late_features行)；过滤:
    universe_flag>0，T+1 n_bars_1445>=40，T+1 not_sealed_1445，T+1非一字跌停(用panel: T+1
    low<high 或 pct_chg>-9.7 近似判定"非一字跌停"，如实披露此近似口径)，T+1非停牌；
    同日(T+1日)事件数<6则该日整日剔除(披露)。

    决策特征(registry逐字): pullback_depth=decision_px(T+1)/close(T日,panel)-1，同事件日
    (T+1日)截面三分位；stab_minutes同S7定义，二分。grid=3x2=6格。

    trade: entry=T+1 entry_vwap_1445_1455；exit=T+2(日历对齐，停牌顺延，顺延计数披露)
    morning_vwap_0935_0945；敏感=T+2 close_px仅披露。成本同S7(S7S8_COST_DEC)。

    披露: 低开子集重叠(gap_sign<0且gap<-2%需gap值——late_features只有符号，用panel T+1
    open/pre_close算出gap值用于该披露)。
    """
    if not LATE_FEATURES_PATH.exists():
        raise FileNotFoundError(f"late_features.parquet不存在: {LATE_FEATURES_PATH}"
                                 f"(由另一任务并行构建，尚未出现；--selftest走合成假数据分支)")
    print("加载late_features.parquet...")
    feat = pd.read_parquet(LATE_FEATURES_PATH)
    feat["ts_code"] = feat["ts_code"].astype(str)
    feat["trade_date"] = feat["trade_date"].astype(str)

    print("加载面板(panel_full)，定首板锚...")
    cols = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg",
            "universe_flag", "amount_ma_20", "listing_days"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["ts_code"] = pn["ts_code"].astype(str)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn = pn.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    n_pn_total = len(pn)
    pn_u = pn[pn["universe_flag"].fillna(0).astype(float) > 0].copy()
    pn_u = pn_u[~pn_u["ts_code"].str.startswith("688")].copy()
    pn_u = pn_u[~pn_u["ts_code"].str.endswith(".BJ")].copy()
    print(f"  面板总行数={n_pn_total}  universe_flag>0+非688+非.BJ后={len(pn_u)}")

    # 首板硬化判定(registry逐字): close>=high*0.999 且 pct_chg窄带双确认(主板/创业板不同带宽)
    is_gem = pn_u["ts_code"].str.startswith("300")
    band_main = pn_u["pct_chg"].between(9.7, 10.3)
    band_gem = pn_u["pct_chg"].between(19.6, 20.4)
    sealed = pn_u["close"] >= pn_u["high"] * 0.999
    is_limit_today = sealed & np.where(is_gem, band_gem, band_main)
    pn_u = pn_u.assign(_is_limit_today=is_limit_today)

    # T-1同口径非涨停：按ts_code分组shift(1)取T-1行的_is_limit_today。先转nullable boolean
    # dtype再shift，避免shift在纯numpy bool列上引入NaN导致隐式object降级(FutureWarning)。
    pn_u["_is_limit_today"] = pn_u["_is_limit_today"].astype("boolean")
    pn_u["_is_limit_prev"] = pn_u.groupby("ts_code")["_is_limit_today"].shift(1)
    # 上市>=60交易日近似: listing_days(日历天)>=90 (60交易日*≈1.4自然日/交易日的保守换算)
    MIN_LISTING_CALENDAR_DAYS_APPROX = 90
    first_board = pn_u[pn_u["_is_limit_today"]
                        & (~pn_u["_is_limit_prev"].fillna(False).astype(bool))
                        & (pn_u["listing_days"].fillna(0) >= MIN_LISTING_CALENDAR_DAYS_APPROX)].copy()
    n_first_board = len(first_board)
    print(f"  首板事件(T日锚, close>=high*0.999且pct_chg窄带且T-1非涨停且listing_days>=90近似60交易日): "
          f"{n_first_board}行  覆盖股票={first_board['ts_code'].nunique()}")

    # T+1映射(市场日历)
    market_cal = sorted(pn["trade_date"].unique())
    next_map = build_calendar_next_map(market_cal)
    first_board["t1_target"] = first_board["trade_date"].map(next_map)
    first_board = first_board.dropna(subset=["t1_target"]).copy()
    first_board = first_board.rename(columns={"trade_date": "t_board_date", "close": "close_T"})

    # 首板事件锚定到T+1的late_features行(T+1需有late_features行，此处按"T+1当天"精确对齐，
    # 不做顺延——registry该处是"T+1需有late_features行"这个过滤条件本身，缺失即剔除，不同于
    # 交易(exit T+2)阶段的日历对齐顺延；两处口径不同，逐字对齐registry两句话的不同语义)
    ev = first_board.merge(
        feat.rename(columns={"trade_date": "t1_target"}),
        left_on=["ts_code", "t1_target"], right_on=["ts_code", "t1_target"], how="inner")
    n_after_t1_feat_join = len(ev)
    print(f"  T+1需有late_features行(inner join): {n_first_board} -> {n_after_t1_feat_join}")

    # universe_flag>0 (T+1当天，非T日；registry写"T+1 n_bars>=40"等均指T+1状态)
    ev = ev.merge(pn_u[["ts_code", "trade_date", "amount_ma_20"]].rename(columns={"trade_date": "t1_target"}),
                  on=["ts_code", "t1_target"], how="inner")
    n_after_t1_universe = len(ev)
    print(f"  T+1 universe_flag>0(join pn_u): {n_after_t1_feat_join} -> {n_after_t1_universe}")

    # not_sealed_1445为NaN同S7口径：保守视为"未确认未封板"整行剔除；n_bars_1445比较结果同S7
    # 也显式fillna(False)避免依赖pandas对<NA>布尔索引器的隐式处理。
    n_before_core = len(ev)
    core_mask = ((ev["n_bars_1445"] >= 40) & (ev["not_sealed_1445"].fillna(False).astype(bool))
                 & (~ev["paused_resume_first_day"].fillna(False).astype(bool)))
    ev = ev[core_mask.fillna(False).astype(bool)].copy()
    n_after_core = len(ev)
    print(f"  T+1核心过滤(n_bars_1445>=40且not_sealed_1445且非停牌复牌首日): {n_before_core} -> {n_after_core}")

    # T+1非一字跌停：用panel T+1行的low<high 或 pct_chg>-9.7 近似判定(如实披露此近似口径)
    p_t1 = pn[["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg"]].rename(
        columns={"trade_date": "t1_target", "open": "open_t1", "high": "high_t1", "low": "low_t1",
                 "close": "close_t1", "pre_close": "pre_close_t1", "pct_chg": "pct_chg_t1"})
    ev = ev.merge(p_t1, on=["ts_code", "t1_target"], how="inner")
    n_before_yizidt = len(ev)
    not_yizi_die = (ev["low_t1"] < ev["high_t1"]) | (ev["pct_chg_t1"] > -9.7)
    ev = ev[not_yizi_die].copy()
    n_after_yizidt = len(ev)
    print(f"  T+1非一字跌停(近似口径: low<high 或 pct_chg>-9.7): {n_before_yizidt} -> {n_after_yizidt}")

    ev, small_day_disclosure = _small_day_drop(ev, "t1_target", 6)
    print(f"  同日(T+1)事件数<6整日剔除: 剔除天数={small_day_disclosure['n_days_dropped_small_day']} "
          f"剔除行数={small_day_disclosure['n_rows_dropped_small_day']}  剩余={len(ev)}")

    # ---- 决策特征(registry逐字)：pullback_depth=decision_px(T+1)/close(T日)-1，同事件日(T+1)三分位 ----
    ev["pullback_depth"] = ev["decision_px"] / ev["close_T"] - 1.0

    if random_decision_feature:
        rng = np.random.RandomState(seed)
        ev["pullback_depth"] = rng.permutation(ev["pullback_depth"].to_numpy())
        ev["stab_minutes"] = rng.permutation(ev["stab_minutes"].to_numpy())

    ev["pullback_terc"] = ev.groupby("t1_target")["pullback_depth"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 3, labels=["深", "中", "浅"])
        if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))
    ev["stab_bkt"] = np.where(ev["stab_minutes"] >= 30, ">=30", "<30")
    ev.loc[ev["stab_minutes"].isna(), "stab_bkt"] = None

    n_before_bucket_dropna = len(ev)
    ev = ev.dropna(subset=["pullback_terc", "stab_bkt"]).copy()
    n_dropped_bucket_nan = n_before_bucket_dropna - len(ev)
    print(f"  分桶dropna: {n_before_bucket_dropna} -> {len(ev)} (剔除{n_dropped_bucket_nan})")

    # ---- T+2日历对齐(T+1基础上再顺延一步，停牌顺延到下一有late_features行的日子，计数披露) ----
    ev["t2_target"] = ev["t1_target"].map(next_map)
    n_before_t2_target = len(ev)
    ev = ev.dropna(subset=["t2_target"]).copy()
    print(f"  T+1在市场日历有下一交易日(T+2 target): {n_before_t2_target} -> {len(ev)}")

    avail_by_code = {code: np.array(sorted(g["trade_date"].to_numpy()))
                      for code, g in feat.groupby("ts_code")}
    resolved, deferred_flag = forward_defer_to_next_available(
        ev["t2_target"], ev["ts_code"], avail_by_code, market_cal)
    ev["t2"] = resolved
    ev["t2_deferred"] = deferred_flag
    n_before_t2_resolve = len(ev)
    ev = ev.dropna(subset=["t2"]).copy()
    n_deferred = int(ev["t2_deferred"].sum())
    print(f"  T+2顺延解析: 可解析={len(ev)}/{n_before_t2_resolve}  其中发生顺延={n_deferred}行")

    t2_feat = feat[["ts_code", "trade_date", "morning_vwap_0935_0945", "close_px"]].rename(
        columns={"trade_date": "t2", "morning_vwap_0935_0945": "exit_vwap_t2", "close_px": "close_t2_sensitivity"})
    n_before_t2_join = len(ev)
    ev = ev.merge(t2_feat, on=["ts_code", "t2"], how="inner")
    n_after_t2_join = len(ev)
    print(f"  T+2(顺延解析后)late_features行join: {n_before_t2_join} -> {n_after_t2_join}")

    # T+2 panel开盘价(供C2O/O2C分解披露)
    t2_dates = set(ev["t2"].unique())
    p2 = load_daily_ohlc_for_dates(t2_dates).rename(columns={"trade_date": "t2", "open": "open_t2"})
    ev = ev.merge(p2[["ts_code", "t2", "open_t2"]], on=["ts_code", "t2"], how="left")

    ev = ev[(ev["entry_vwap_1445_1455"] > 0) & (ev["exit_vwap_t2"] > 0)].copy()
    n_final = len(ev)
    print(f"  正值过滤(entry/exit VWAP>0): -> {n_final}")

    # 价格一致性守卫(同S7,20260706监督者补,单位错位事故第二道防线)
    _r_in = ev["entry_vwap_1445_1455"] / ev["decision_px"]
    _r_out = ev["exit_vwap_t2"] / ev["close_t2_sensitivity"]
    _consistent = _r_in.between(0.6, 1.67) & (_r_out.between(0.6, 1.67) | _r_out.isna())
    n_guard_drop = int((~_consistent).sum())
    ev = ev[_consistent].copy()
    print(f"  价格一致性守卫(0.6-1.67x同日价): 剔除{n_guard_drop}行 -> {len(ev)}")

    # ---- 交易收益(registry逐字) ----
    ev["gross"] = ev["exit_vwap_t2"] / ev["entry_vwap_1445_1455"] - 1.0
    ev["_cost_dec"] = S7S8_COST_DEC[cost_mode]
    ev["net"] = ev["gross"] - ev["_cost_dec"]
    ev["sensitivity_close_gross"] = ev["close_t2_sensitivity"] / ev["entry_vwap_1445_1455"] - 1.0  # 仅披露

    ev["c2o_disclose"] = np.where(ev["open_t2"].notna() & (ev["open_t2"] > 0),
                                   ev["open_t2"] / ev["entry_vwap_1445_1455"] - 1.0, np.nan)
    ev["o2c_disclose"] = np.where(ev["open_t2"].notna() & (ev["open_t2"] > 0),
                                   ev["exit_vwap_t2"] / ev["open_t2"] - 1.0, np.nan)

    # 低开子集重叠披露(registry逐字): gap_sign<0 且 gap<-2%——late_features只有gap_sign符号，
    # 用panel T+1 open_t1/pre_close_t1算出gap值供该披露(如实标注：不是late_features原生列，
    # 是本引擎用panel近似算出，仅用于披露轴，不参与判定)
    ev["gap_t1_value_disclose"] = ev["open_t1"] / ev["pre_close_t1"] - 1.0
    ev["is_low_open_lt_neg2pct_disclose"] = (ev["gap_sign"] < 0) & (ev["gap_t1_value_disclose"] < -0.02)

    ev["ts_code"] = ev["ts_code"].astype(str)
    ev["pullback_terc"] = ev["pullback_terc"].astype(str)
    ev["stab_bkt"] = ev["stab_bkt"].astype(str)
    ev["trade_date"] = ev["t1_target"]  # 供gate3/gate4通用函数按trade_date分组使用(=T+1事件日)
    n_years = (pd.to_datetime(ev["trade_date"], format="%Y%m%d").max()
               - pd.to_datetime(ev["trade_date"], format="%Y%m%d").min()).days / 365.25 if len(ev) else 1.0

    grid_cells = []
    for terc in ["深", "中", "浅"]:
        for sb in [">=30", "<30"]:
            grid_cells.append({"pullback_terc": terc, "stab_bkt": sb})

    def _bucketize(d2: pd.DataFrame) -> pd.DataFrame:
        d2 = d2.copy()
        d2["pullback_terc"] = d2.groupby("trade_date")["pullback_depth"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=["深", "中", "浅"])
            if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))
        d2["stab_bkt"] = np.where(d2["stab_minutes"] >= 30, ">=30", "<30")
        d2.loc[d2["stab_minutes"].isna(), "stab_bkt"] = None
        d2["pullback_terc"] = d2["pullback_terc"].astype(str)
        d2["stab_bkt"] = d2["stab_bkt"].astype(str)
        return d2

    low_open_overlap_rate = float(ev["is_low_open_lt_neg2pct_disclose"].mean()) if len(ev) else None

    return {
        "event_df": ev, "grid_cells": grid_cells, "grid_group_cols": ["pullback_terc", "stab_bkt"],
        "raw_cols": ["pullback_depth", "stab_minutes"], "bucket_cols": ["pullback_terc", "stab_bkt"],
        "bucketize_fn": _bucketize,
        "n_calendar_years": max(n_years, 0.5),
        "success_criteria_text": get_setup_def(load_registry(), "S8_first_board_tail_absorb")["success_criteria"],
        "n_min_per_cell": 300,
        "cost_dec": S7S8_COST_DEC[cost_mode],
        "low_open_overlap_rate_disclose": low_open_overlap_rate,
        "diagnostics": {
            "n_panel_rows_total": int(n_pn_total),
            "n_first_board_events": int(n_first_board),
            "n_after_t1_feat_join": int(n_after_t1_feat_join),
            "n_after_t1_universe": int(n_after_t1_universe),
            "n_after_core": int(n_after_core),
            "n_after_yizi_die_filter": int(n_after_yizidt),
            "small_day_disclosure": small_day_disclosure,
            "n_dropped_bucket_nan": int(n_dropped_bucket_nan),
            "n_t2_deferred_to_next_available_bar": int(n_deferred),
            "n_events_final": int(n_final),
            "approximation_note_listing_days": (
                f"上市>=60交易日 近似为 listing_days(日历天,非交易天)>={MIN_LISTING_CALENDAR_DAYS_APPROX}"),
            "approximation_note_yizi_dietian": "T+1非一字跌停 近似为 low_t1<high_t1 或 pct_chg_t1>-9.7",
        },
    }


# ============================== S1 主流程 ==============================

def run_s1(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    slip = SlippageTable(SLIPPAGE_PATH)
    ld = load_setup_s1(cost_mode, slip, random_decision_feature, seed)
    ev = ld["event_df"]
    print(f"事件样本(T日候选,已剔涨跌停/一字/gap>7%): {len(ev):,}  覆盖T日 {ev['trade_date'].nunique()}")
    print("单位自检(ca_amount vs 面板amount中位比值):")
    for u in ld["unit_check"]:
        print(f"  样本日{u.get('sample_date')}: n={u.get('n')} 中位比值={u.get('ratio_median')} "
              f"预期区间{u.get('expected_range')} 落在预期内={u.get('in_expected_range')}")
        print(f"    {u.get('note')}")
    rec_ok = ld["reconciliation"]["ok"]
    print(f"对账(T+1竞价撮合价 vs T+1面板开盘价, 中位偏差): {ld['reconciliation']['t1_gap_vs_open1_median_dev']} "
          f"(阈值<{ld['reconciliation']['threshold']}) → {'OK' if rec_ok else '⚠️FAIL,结果需打折扣'}")

    result: dict = {
        "setup_id": "S1_close_auction_anomaly", "cost_mode": cost_mode, "seed": seed,
        "selftest_random": random_decision_feature,
        "n_events": int(len(ev)), "n_trade_dates": int(ev["trade_date"].nunique()),
        "unit_check": ld["unit_check"], "reconciliation": ld["reconciliation"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if len(ev) < 300:
        result["verdict"] = "INSUFFICIENT(总事件样本<300,推迟终审)"
        return result

    # ---- 闸1：三段分解（整体事件子集） ----
    # 注：闸1检验的是"事件域本身"的隔夜/日内收益结构，与决策特征(ca_amt_share/ca_move)无关——
    # 即便决策特征被替换为纯随机(--selftest-random)，闸1的PASS/FAIL也不会变(它测的是市场结构，
    # 不是待测信号)。验收要求"必须死于闸4"，因此selftest-random模式下闸1仅如实记录、不作为否决
    # 依据，流程强制继续往下走，让闸4(安慰剂)去证明它才是拦下随机信号的那道闸。
    print("\n=== 闸1: 三段分解(事件整体, 次日C2O/O2C/C2C均值) ===")
    g1 = gate1_three_leg_event(ev)
    result["gate1_three_leg"] = g1
    print(f"  n={g1.get('n')}  C2O {g1.get('c2o_overnight_pct')}%  O2C {g1.get('o2c_intraday_pct')}%  "
          f"C2C {g1.get('c2c_full_day_pct')}%  C2O占比 {g1.get('c2o_share_of_c2c')}  → {'PASS' if g1['pass'] else 'FAIL'}: {g1['reason']}")
    if random_decision_feature and not g1["pass"]:
        print("  [selftest-random] 闸1测的是事件域市场结构(与决策特征无关)，非否决依据，流程继续走向闸2-4以验证闸4的拦截力。")

    # ---- 闸2：净边际（18格） ----
    print(f"\n=== 闸2: 净边际(18格: 3 Tbkt x 3 ca_amt_share三分位 x 2 ca_move符号, cost_mode={cost_mode}) ===")
    cells_out = []
    passing_cells = []
    for cell in ld["grid_cells"]:
        mask = ((ev["Tbkt"] == cell["Tbkt"]) & (ev["ca_amt_share_terc"].astype(str) == cell["ca_amt_share_terc"])
                & (ev["ca_move"] == cell["ca_move"]))
        sub = ev[mask]
        stats = cell_stats(sub["o2c"].to_numpy(), sub["_cost_dec"].to_numpy())
        cell_result = {**cell, **stats}
        n_ok = stats.get("n", 0) >= ld["n_min_per_cell"]
        net_ok = n_ok and stats.get("net_pct", -1) > 0 and stats.get("t_net", 0) > 2
        cell_result["meets_n_min"] = n_ok
        cell_result["gate2_pass"] = bool(net_ok)
        cells_out.append(cell_result)
        if net_ok:
            passing_cells.append((cell, sub))
        flag = " ←候选" if net_ok else ""
        print(f"  {cell['Tbkt']:<10}{cell['ca_amt_share_terc']:<4}move{cell['ca_move']:+.0f}  "
              f"n={stats.get('n',0):>6}  毛O2C {stats.get('gross_o2c_pct','NA')}%  t {stats.get('t_gross','NA')}  "
              f"胜率 {stats.get('hit_pct','NA')}%  净 {stats.get('net_pct','NA')}%  t净 {stats.get('t_net','NA')}{flag}")
    result["gate2_grid"] = cells_out
    result["gate2_pass"] = bool(len(passing_cells) > 0)
    print(f"  → 达标格点数: {len(passing_cells)}/{len(cells_out)}")

    if not passing_cells:
        result["gates_summary"] = {"gate1_three_leg": g1["pass"], "gate2_net_margin": False}
        result["verdict"] = "REJECTED(死于: 闸2净边际，无一格达标)"
        return result

    # ---- 对每个达标格点跑闸3/闸4/闸5，任一格全通过则CANDIDATE ----
    cell_verdicts = []
    for cell, sub in passing_cells:
        label = f"{cell['Tbkt']}|{cell['ca_amt_share_terc']}|move{cell['ca_move']:+.0f}"
        print(f"\n--- 候选格 {label}: 闸3独立复现 ---")
        g3 = gate3_reproducibility(sub, "o2c", "_cost_dec", seed=seed)
        ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
        print(f"  半A净{ua.get('net_pct','NA')}%(t{ua.get('t_net','NA')}) 半B净{ub.get('net_pct','NA')}%(t{ub.get('t_net','NA')}) "
              f"逐年:{g3['yearly_breakdown']['reason']} → {'PASS' if g3['pass'] else 'FAIL'}")

        print(f"--- 候选格 {label}: 闸4安慰剂 ---")
        def _select_fn(d2: pd.DataFrame, cell=cell) -> pd.Series:
            terc2 = d2.groupby("trade_date")["ca_amt_share"].transform(
                lambda s: pd.qcut(s.rank(method="first"), 3, labels=["低", "中", "高"]) if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))
            return (d2["Tbkt"] == cell["Tbkt"]) & (terc2.astype(str) == cell["ca_amt_share_terc"]) & (d2["ca_move"] == cell["ca_move"])
        g4 = gate4_placebo(sub, "ca_amt_share", ["trade_date", "Tbkt"], _select_fn, o2c_col="o2c", cost_col="_cost_dec",
                            seed=seed, n_trials=10, universe_df=ev)
        print(f"  真实净{g4.get('real_net_mean_pct','NA')}% vs 安慰剂均值{g4.get('placebo_mean_pct','NA')}%±{g4.get('placebo_std_pct','NA')} "
              f"→ {'PASS' if g4['pass'] else 'FAIL'}: {g4['reason']}")

        print(f"--- 候选格 {label}: 闸5容量频率(只披露) ---")
        g5 = gate5_capacity(sub, "liq_bkt", "ca_amount", "trade_date", ld["n_calendar_years"], slip)
        print(f"  年触发 {g5['triggers_per_year_est']}  日均 {g5['triggers_per_day_avg']}  "
              f"竞价额中位 {g5['trigger_auction_amount_yuan'].get('median_amount','NA')}元")

        cell_pass = bool(g3["pass"] and g4["pass"])
        cell_verdicts.append({"cell": cell, "gate3": g3, "gate4": g4, "gate5": g5, "pass": cell_pass})

    result["cell_verdicts"] = cell_verdicts
    any_full_pass = any(cv["pass"] for cv in cell_verdicts)
    result["gates_summary"] = {
        "gate1_three_leg": g1["pass"], "gate2_net_margin": True,
        "gate3_reproducibility_any_cell": any(cv["gate3"]["pass"] for cv in cell_verdicts),
        "gate4_placebo_any_cell": any(cv["gate4"]["pass"] for cv in cell_verdicts),
    }
    # 闸1只在非selftest模式下才能作为否决依据(它测市场结构而非决策特征本身，见上方注释)。
    if (not g1["pass"]) and (not random_decision_feature):
        result["verdict"] = "REJECTED(死于: 闸1三段分解，edge集中于不可成交隔夜段)"
    elif any_full_pass:
        result["verdict"] = "CANDIDATE(仍需人工复核+前瞻跟踪；本引擎绝不判定为可部署)"
    else:
        died = []
        if not any(cv["gate3"]["pass"] for cv in cell_verdicts):
            died.append("闸3独立复现")
        if not any(cv["gate4"]["pass"] for cv in cell_verdicts):
            died.append("闸4安慰剂")
        result["verdict"] = f"REJECTED(死于: {', '.join(died) if died else '闸3/闸4'}，达标格点均未能同时通过闸3+闸4)"
    return result


# ============================== S6 主流程 ==============================

def run_s6(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    slip = SlippageTable(SLIPPAGE_PATH)
    ld = load_setup_s6(cost_mode, slip, random_decision_feature, seed)
    rec = ld["event_df"]
    n_valid_days = ld["n_valid_list_days"]
    print(f"清单确认事件样本: {len(rec):,}  有效清单日: {n_valid_days}")

    result: dict = {
        "setup_id": "S6_list_auction_confirm", "cost_mode": cost_mode, "seed": seed,
        "selftest_random": random_decision_feature,
        "n_events": int(len(rec)), "n_valid_list_days": int(n_valid_days),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if n_valid_days < ld["min_valid_days"]:
        result["verdict"] = f"INSUFFICIENT(有效清单日{n_valid_days}<{ld['min_valid_days']},推迟终审)"
        return result

    # ---- 闸1：三段分解（清单确认整体） ----
    rec = rec.assign(c2o=np.nan, o2c=rec["o2c"], c2c=np.nan)  # 清单确认交易口径本身就是O2C，无隔夜腿(入场即T+1开盘)
    print("\n=== 闸1: 三段分解 ===")
    print("  S6交易口径为T+1开盘入场/T+1收盘出场(纯O2C日内段)，无隔夜腿参与，天然immune于'C2O占比'不可成交问题。")
    g1 = {"pass": True, "reason": "S6交易结构本身即O2C日内段，无隔夜腿，闸1天然通过（非事后放水，是结构决定）",
          "n": int(len(rec)), "c2o_overnight_pct": None, "o2c_intraday_pct": round(float(rec["o2c"].mean()) * 100, 4),
          "c2c_full_day_pct": None, "c2o_share_of_c2c": None}
    result["gate1_three_leg"] = g1
    print(f"  → PASS: {g1['reason']}")

    # ---- 闸2：净边际，4条确认规则 vs 对照(同清单未确认部分) ----
    print(f"\n=== 闸2: 净边际(4条预注册确认规则 vs 对照=同清单未确认部分, cost_mode={cost_mode}) ===")
    rules_out = {}
    passing_rules = []
    for rname, mask in ld["rules"].items():
        confirmed = rec[mask]
        unconfirmed = rec[~mask]
        cstats = cell_stats(confirmed["o2c"].to_numpy(), confirmed["_cost_dec"].to_numpy())
        ustats = cell_stats(unconfirmed["o2c"].to_numpy(), unconfirmed["_cost_dec"].to_numpy())
        # 两比例z检验：确认子集胜率 vs 未确认子集胜率
        n1, n2 = cstats.get("n", 0), ustats.get("n", 0)
        z_prop = None
        hit_diff_pp = None
        if n1 >= 20 and n2 >= 20:
            p1, p2 = cstats["hit_pct"] / 100.0, ustats["hit_pct"] / 100.0
            p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
            se = np.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2)) if 0 < p_pool < 1 else 0.0
            z_prop = float((p1 - p2) / se) if se > 1e-9 else 0.0
            hit_diff_pp = round((p1 - p2) * 100, 2)
        n_valid = n1 >= 20 and n2 >= 20
        rule_pass = bool(n_valid and hit_diff_pp is not None and hit_diff_pp >= 5.0 and z_prop is not None
                          and abs(z_prop) > 2 and cstats.get("net_pct", -1) > 0)
        rules_out[rname] = {"confirmed": cstats, "unconfirmed": ustats,
                             "hit_diff_pp": hit_diff_pp, "z_prop": round(z_prop, 3) if z_prop is not None else None,
                             "pass": rule_pass}
        print(f"  {rname}: 确认n={n1} 净{cstats.get('net_pct','NA')}%  胜率{cstats.get('hit_pct','NA')}%  |  "
              f"对照n={n2} 净{ustats.get('net_pct','NA')}%  胜率{ustats.get('hit_pct','NA')}%  |  "
              f"胜率差{hit_diff_pp}pp  z{round(z_prop,2) if z_prop is not None else 'NA'}  "
              f"→ {'PASS' if rule_pass else 'FAIL'}")
        if rule_pass:
            passing_rules.append((rname, mask, confirmed))
    result["gate2_rules"] = rules_out
    result["gate2_pass"] = bool(len(passing_rules) > 0)

    if not passing_rules:
        result["gates_summary"] = {"gate1_three_leg": True, "gate2_net_margin": False}
        result["verdict"] = "REJECTED(死于: 闸2净边际，4条规则均未达标)"
        return result

    # ---- 对每条达标规则跑闸3(时间型复现:按recommend_date奇偶周)/闸4/闸5 ----
    rec["_iso_week"] = pd.to_datetime(rec["recommend_date"], format="%Y%m%d").dt.isocalendar().week.astype(int)
    rule_verdicts = []
    for rname, mask, confirmed in passing_rules:
        print(f"\n--- 规则{rname}: 闸3独立复现(recommend_date奇偶周分半 + 逐年表) ---")
        odd = confirmed[confirmed["_iso_week"] % 2 == 1]
        even = confirmed[confirmed["_iso_week"] % 2 == 0]
        ra = cell_stats(odd["o2c"].to_numpy(), odd["_cost_dec"].to_numpy())
        rb = cell_stats(even["o2c"].to_numpy(), even["_cost_dec"].to_numpy())
        a_net, a_t = ra.get("net_pct"), ra.get("t_net")
        b_net, b_t = rb.get("net_pct"), rb.get("t_net")
        same_sign = bool(a_net is not None and b_net is not None and (a_net > 0) == (b_net > 0))
        both_t_ok = bool(a_t is not None and b_t is not None and a_t > 1 and b_t > 1)
        enough_n = bool(ra.get("n", 0) >= 10 and rb.get("n", 0) >= 10)
        week_split_ok = bool(enough_n and same_sign and both_t_ok)
        yearly = yearly_breakdown(confirmed, "recommend_date", "o2c", "_cost_dec")
        g3_pass = bool(week_split_ok and yearly["pass"])
        print(f"  奇周净{ra.get('net_pct','NA')}%(t{ra.get('t_net','NA')}) 偶周净{rb.get('net_pct','NA')}%(t{rb.get('t_net','NA')}) "
              f"逐年:{yearly['reason']} → {'PASS' if g3_pass else 'FAIL'}")
        g3 = {"pass": g3_pass, "odd_week": ra, "even_week": rb, "same_sign": same_sign, "both_t_gt_1": both_t_ok,
              "yearly_breakdown": yearly}

        print(f"--- 规则{rname}: 闸4安慰剂(同日清单内打乱avr_list/gap, x10) ---")
        def _select_fn(d2: pd.DataFrame, rname=rname) -> pd.Series:
            if rname == "R_a":
                return (d2["gap"] >= -0.02) & (d2["gap"] <= 0.05) & (d2["avr_list"] >= 0.5)
            if rname == "R_b":
                return (d2["gap"] >= -0.02) & (d2["gap"] <= 0.05)
            if rname == "R_c":
                return (d2["avr_list"] >= 0.5)
            return (d2["gap"] >= 0.0) & (d2["gap"] <= 0.05) & (d2["avr_list"] >= (2.0 / 3.0))
        g4 = gate4_placebo(confirmed, "avr_list", ["t1"], _select_fn, o2c_col="o2c", cost_col="_cost_dec",
                            seed=seed, n_trials=10, universe_df=rec)
        print(f"  真实净{g4.get('real_net_mean_pct','NA')}% vs 安慰剂均值{g4.get('placebo_mean_pct','NA')}%±{g4.get('placebo_std_pct','NA')} "
              f"→ {'PASS' if g4['pass'] else 'FAIL'}: {g4['reason']}")

        print(f"--- 规则{rname}: 闸5容量频率(只披露) ---")
        g5 = gate5_capacity(confirmed, "liq_bkt", "auct_open_amount", "recommend_date", ld["n_calendar_years"], slip)
        print(f"  年触发 {g5['triggers_per_year_est']}  日均 {g5['triggers_per_day_avg']}  "
              f"竞价额中位 {g5['trigger_auction_amount_yuan'].get('median_amount','NA')}元")

        rule_pass = bool(g3["pass"] and g4["pass"])
        rule_verdicts.append({"rule": rname, "gate3": g3, "gate4": g4, "gate5": g5, "pass": rule_pass})

    result["rule_verdicts"] = rule_verdicts
    any_full_pass = any(rv["pass"] for rv in rule_verdicts)
    result["gates_summary"] = {
        "gate1_three_leg": True, "gate2_net_margin": True,
        "gate3_reproducibility_any_rule": any(rv["gate3"]["pass"] for rv in rule_verdicts),
        "gate4_placebo_any_rule": any(rv["gate4"]["pass"] for rv in rule_verdicts),
    }
    if any_full_pass:
        result["verdict"] = "CANDIDATE(仍需人工复核+前瞻跟踪；本引擎绝不判定为可部署)"
    else:
        died = []
        if not any(rv["gate3"]["pass"] for rv in rule_verdicts):
            died.append("闸3独立复现")
        if not any(rv["gate4"]["pass"] for rv in rule_verdicts):
            died.append("闸4安慰剂")
        result["verdict"] = f"REJECTED(死于: {', '.join(died) if died else '闸3/闸4'}，达标规则均未能同时通过闸3+闸4)"
    return result


# ============================== S5 主流程 ==============================

def run_s5(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    slip = SlippageTable(SLIPPAGE_PATH)
    ld = load_setup_s5(cost_mode, slip, random_decision_feature, seed)
    ev = ld["event_df"]
    diag = ld["diagnostics"]
    print(f"事件样本(D日候选,已过n_members>=8/前日universe_flag>0/gap∈[-2%,+1%]/D日非一字/三特征齐全): "
          f"{len(ev):,}  覆盖D日 {ev['trade_date'].nunique()}")
    print(f"  诊断: 板块-竞价日总行数={diag['n_saf_rows_total']}  n_members>=8合格板块行数={diag['n_saf_rows_n_members_ge_8']}  "
          f"D日一字剔除={diag['n_yizi_dropped']}  三特征未定义剔除(主要是auct_amt_z早期NaN)="
          f"{diag['n_dropped_feature_undefined_(sector_amt_nan_mostly)']}")

    result: dict = {
        "setup_id": "S5_sector_auction_resonance", "cost_mode": cost_mode, "seed": seed,
        "selftest_random": random_decision_feature,
        "n_events": int(len(ev)), "n_trade_dates": int(ev["trade_date"].nunique()),
        "diagnostics": diag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if len(ev) < 300:
        result["verdict"] = "INSUFFICIENT(总事件样本<300,推迟终审)"
        return result

    # ---- 闸1：三段分解 ----
    # S5交易口径本身即D日开盘入场/D日收盘出场(纯O2C日内段，无隔夜腿参与)——与S6同理，天然
    # immune于"C2O占比>70%不可成交"的问题(该问题针对的是"隔夜段edge无法在T+1开盘前用出场价捕获"，
    # 而S5的入场点就是D日开盘本身，不存在隔夜段可言)。如实按结构判定PASS，非事后放水。
    print("\n=== 闸1: 三段分解 ===")
    print("  S5交易结构为D日开盘入场/D日收盘出场(纯O2C日内段)，无隔夜腿，闸1天然通过（结构决定，非事后放水）。")
    g1 = {"pass": True, "reason": "S5交易结构本身即O2C日内段(入场=D日开盘,无隔夜腿)，闸1天然通过",
          "n": int(len(ev)), "c2o_overnight_pct": None, "o2c_intraday_pct": round(float(ev["o2c"].mean()) * 100, 4),
          "c2c_full_day_pct": None, "c2o_share_of_c2c": None}
    result["gate1_three_leg"] = g1
    print(f"  → PASS: {g1['reason']}")

    # ---- 闸2：净边际（12格） ----
    print(f"\n=== 闸2: 净边际(12格: 3 sector_strength x 2 sector_amt x 2 leader, cost_mode={cost_mode}) ===")
    cells_out = []
    passing_cells = []
    for cell in ld["grid_cells"]:
        mask = ((ev["sector_strength"] == cell["sector_strength"]) & (ev["sector_amt"] == cell["sector_amt"])
                & (ev["leader"] == cell["leader"]))
        sub = ev[mask]
        stats = cell_stats(sub["o2c"].to_numpy(), sub["_cost_dec"].to_numpy())
        cell_result = {**cell, **stats}
        n_ok = stats.get("n", 0) >= ld["n_min_per_cell"]
        net_ok = n_ok and stats.get("net_pct", -1) > 0 and stats.get("t_net", 0) > 2
        cell_result["meets_n_min"] = n_ok
        cell_result["gate2_pass"] = bool(net_ok)
        cells_out.append(cell_result)
        if net_ok:
            passing_cells.append((cell, sub))
        flag = " ←候选" if net_ok else ""
        print(f"  strength={cell['sector_strength']:<2} amt={cell['sector_amt']:<2} leader={cell['leader']:<2}  "
              f"n={stats.get('n',0):>7}  毛O2C {stats.get('gross_o2c_pct','NA')}%  t {stats.get('t_gross','NA')}  "
              f"胜率 {stats.get('hit_pct','NA')}%  净 {stats.get('net_pct','NA')}%  t净 {stats.get('t_net','NA')}{flag}")
    result["gate2_grid"] = cells_out
    result["gate2_pass"] = bool(len(passing_cells) > 0)
    n_sum_cells = sum(c.get("n", 0) for c in cells_out)
    print(f"  → 达标格点数: {len(passing_cells)}/{len(cells_out)}  12格n之和={n_sum_cells}  事件总数={len(ev)}  "
          f"(差异={len(ev) - n_sum_cells}, 应为0——分桶为三个独立完全覆盖的二/三分类乘积，理论上无遗漏无重复)")
    result["grid_n_reconciliation"] = {
        "sum_of_cell_n": int(n_sum_cells), "n_events": int(len(ev)), "diff": int(len(ev) - n_sum_cells),
    }

    if not passing_cells:
        result["gates_summary"] = {"gate1_three_leg": g1["pass"], "gate2_net_margin": False}
        result["verdict"] = "REJECTED(死于: 闸2净边际，无一格达标)"
        return result

    # ---- 对每个达标格点跑闸3/闸4(板块级)/闸5 ----
    board_all = ld["board_all"]
    cell_verdicts = []
    for cell, sub in passing_cells:
        label = f"strength={cell['sector_strength']}|amt={cell['sector_amt']}|leader={cell['leader']}"
        print(f"\n--- 候选格 {label}: 闸3独立复现 ---")
        g3 = gate3_reproducibility(sub, "o2c", "_cost_dec", seed=seed)
        ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
        print(f"  半A净{ua.get('net_pct','NA')}%(t{ua.get('t_net','NA')}) 半B净{ub.get('net_pct','NA')}%(t{ub.get('t_net','NA')}) "
              f"逐年:{g3['yearly_breakdown']['reason']} → {'PASS' if g3['pass'] else 'FAIL'}")

        print(f"--- 候选格 {label}: 闸4安慰剂(同日内跨板块打乱板块三特征三元组,x10) ---")
        g4 = gate4_placebo_board_shuffle(board_all, ev, cell, o2c_col="o2c", cost_col="_cost_dec",
                                         seed=seed, n_trials=10)
        print(f"  真实净{g4.get('real_net_mean_pct','NA')}% vs 安慰剂均值{g4.get('placebo_mean_pct','NA')}%±{g4.get('placebo_std_pct','NA')} "
              f"→ {'PASS' if g4['pass'] else 'FAIL'}: {g4['reason']}")

        print(f"--- 候选格 {label}: 闸5容量频率(只披露) ---")
        g5 = gate5_capacity(sub, "liq_bkt", "amount_ma_20", "trade_date", ld["n_calendar_years"], slip)
        print(f"  年触发 {g5['triggers_per_year_est']}  日均 {g5['triggers_per_day_avg']}")

        cell_pass = bool(g3["pass"] and g4["pass"])
        cell_verdicts.append({"cell": cell, "gate3": g3, "gate4": g4, "gate5": g5, "pass": cell_pass})

    result["cell_verdicts"] = cell_verdicts
    any_full_pass = any(cv["pass"] for cv in cell_verdicts)
    result["gates_summary"] = {
        "gate1_three_leg": g1["pass"], "gate2_net_margin": True,
        "gate3_reproducibility_any_cell": any(cv["gate3"]["pass"] for cv in cell_verdicts),
        "gate4_placebo_any_cell": any(cv["gate4"]["pass"] for cv in cell_verdicts),
    }
    if any_full_pass:
        result["verdict"] = "CANDIDATE(仍需人工复核+前瞻跟踪；本引擎绝不判定为可部署)"
    else:
        died = []
        if not any(cv["gate3"]["pass"] for cv in cell_verdicts):
            died.append("闸3独立复现")
        if not any(cv["gate4"]["pass"] for cv in cell_verdicts):
            died.append("闸4安慰剂")
        result["verdict"] = f"REJECTED(死于: {', '.join(died) if died else '闸3/闸4'}，达标格点均未能同时通过闸3+闸4)"
    return result


# ============================== S3 主流程 ==============================

def run_s3(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    slip = SlippageTable(SLIPPAGE_PATH)
    ld = load_setup_s3(cost_mode, slip, random_decision_feature, seed)
    ev = ld["event_df"]
    diag = ld["diagnostics"]
    print(f"事件样本(T日候选,已过n_bars>=40/universe_flag>0/T+1日历对齐/T+1非一字/gap<=7%/三特征齐全): "
          f"{len(ev):,}  覆盖T日 {ev['trade_date'].nunique()}")
    print(f"  诊断: 特征文件总行数={diag['n_feat_rows_total_raw']}  unique ts_code={diag['n_unique_ts_code_raw']}  "
          f"late_strength=0(全量,筛选前)={diag['n_late_strength_zero_raw']}  "
          f"late_strength=NaN(全量,筛选前)={diag['n_late_strength_nan_raw']}")
    print(f"  诊断: 达标事件(过滤后,late_sign剔除前)={diag['n_qualify_before_late_sign_exclusion']}  "
          f"其中late_strength=0={diag['n_late_strength_zero_in_qualified']}  "
          f"late_strength=NaN={diag['n_late_strength_nan_in_qualified']}  "
          f"三特征分桶dropna共剔除={diag['n_dropped_at_bucket_dropna_total']}")

    result: dict = {
        "setup_id": "S3_intraday_shape_daily", "cost_mode": cost_mode, "seed": seed,
        "selftest_random": random_decision_feature,
        "n_events": int(len(ev)), "n_trade_dates": int(ev["trade_date"].nunique()),
        "diagnostics": diag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if len(ev) < 300:
        result["verdict"] = "INSUFFICIENT(总事件样本<300,推迟终审)"
        return result

    # ---- 闸1：三段分解 ----
    # S3交易结构本身即T+1开盘入场/T+1收盘出场(纯O2C日内段，无隔夜腿参与)——与S5/S6同理，
    # 天然immune于"C2O占比>70%不可成交"问题(该问题针对的是隔夜段edge无法用出场价捕获，而S3
    # 的入场点就是T+1开盘本身，不存在隔夜段可言)。如实按结构判定PASS，非事后放水，
    # 仍需显式填充 result["gate1_three_leg"]（不得静默跳过）。
    print("\n=== 闸1: 三段分解 ===")
    print("  S3交易结构为T+1开盘入场/T+1收盘出场(纯O2C日内段)，无隔夜腿，闸1天然通过（结构决定，非事后放水）。")
    g1 = {"pass": True, "reason": "S3交易结构本身即O2C日内段(入场=T+1开盘,无隔夜腿)，闸1天然通过",
          "n": int(len(ev)), "c2o_overnight_pct": None, "o2c_intraday_pct": round(float(ev["o2c"].mean()) * 100, 4),
          "c2c_full_day_pct": None, "c2o_share_of_c2c": None}
    result["gate1_three_leg"] = g1
    print(f"  → PASS: {g1['reason']}")

    # ---- 闸2：净边际（12格） ----
    print(f"\n=== 闸2: 净边际(12格: 3 am_share x 2 late_sign x 2 smooth, cost_mode={cost_mode}) ===")
    cells_out = []
    passing_cells = []
    for cell in ld["grid_cells"]:
        mask = ((ev["am_share"] == cell["am_share"]) & (ev["late_sign"] == cell["late_sign"])
                & (ev["smooth"] == cell["smooth"]))
        sub = ev[mask]
        stats = cell_stats(sub["o2c"].to_numpy(), sub["_cost_dec"].to_numpy())
        cell_result = {**cell, **stats}
        n_ok = stats.get("n", 0) >= ld["n_min_per_cell"]
        net_ok = n_ok and stats.get("net_pct", -1) > 0 and stats.get("t_net", 0) > 2
        cell_result["meets_n_min"] = n_ok
        cell_result["gate2_pass"] = bool(net_ok)
        cells_out.append(cell_result)
        if net_ok:
            passing_cells.append((cell, sub))
        flag = " ←候选" if net_ok else ""
        print(f"  am_share={cell['am_share']:<2} late_sign={cell['late_sign']:<2} smooth={cell['smooth']:<2}  "
              f"n={stats.get('n',0):>7}  毛O2C {stats.get('gross_o2c_pct','NA')}%  t {stats.get('t_gross','NA')}  "
              f"胜率 {stats.get('hit_pct','NA')}%  净 {stats.get('net_pct','NA')}%  t净 {stats.get('t_net','NA')}{flag}")
    result["gate2_grid"] = cells_out
    result["gate2_pass"] = bool(len(passing_cells) > 0)

    # 对账：12格n之和 + 被late_sign=0/NaN剔除的行数(在"其余过滤后、late_sign剔除前"这同一
    # 群体内计数) 应等于 该同一群体的事件总数(n_qualify_before_late_sign_exclusion)。
    # 注意：am_share/smooth自身的NaN剔除(n_dropped_at_bucket_dropna_total中，非late_sign
    # 部分)理论上应为0或极小(两者的qcut/中位数在样本充足时不会产生NaN，除非某日不足3只)，
    # 若非0会导致下方diff非零，如实报告，不强行凑整。
    n_sum_cells = sum(c.get("n", 0) for c in cells_out)
    n_qualify_pre = diag["n_qualify_before_late_sign_exclusion"]
    n_late_excl = diag["n_late_strength_zero_in_qualified"] + diag["n_late_strength_nan_in_qualified"]
    recon_diff = n_qualify_pre - n_sum_cells - n_late_excl
    print(f"  → 达标格点数: {len(passing_cells)}/{len(cells_out)}  12格n之和={n_sum_cells}  "
          f"+被剔除late_strength=0/NaN行数={n_late_excl}  vs 事件总数(late_sign剔除前)={n_qualify_pre}  "
          f"(差异={recon_diff}, 非零可能来自am_share/smooth自身少量NaN，如实报告不强行凑整)")
    result["grid_n_reconciliation"] = {
        "sum_of_cell_n": int(n_sum_cells),
        "n_late_strength_zero_or_nan_excluded": int(n_late_excl),
        "n_events_before_late_sign_exclusion": int(n_qualify_pre),
        "diff": int(recon_diff),
    }

    if not passing_cells:
        result["gates_summary"] = {"gate1_three_leg": g1["pass"], "gate2_net_margin": False}
        result["verdict"] = "REJECTED(死于: 闸2净边际，无一格达标)"
        return result

    # ---- 对每个达标格点跑闸3/闸4(行级三特征捆绑打乱)/闸5 ----
    cell_verdicts = []
    for cell, sub in passing_cells:
        label = f"am_share={cell['am_share']}|late_sign={cell['late_sign']}|smooth={cell['smooth']}"
        print(f"\n--- 候选格 {label}: 闸3独立复现 ---")
        g3 = gate3_reproducibility(sub, "o2c", "_cost_dec", seed=seed)
        ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
        print(f"  半A净{ua.get('net_pct','NA')}%(t{ua.get('t_net','NA')}) 半B净{ub.get('net_pct','NA')}%(t{ub.get('t_net','NA')}) "
              f"逐年:{g3['yearly_breakdown']['reason']} → {'PASS' if g3['pass'] else 'FAIL'}")

        print(f"--- 候选格 {label}: 闸4安慰剂(同日内行级打乱三特征捆绑,x10) ---")
        # 打乱池=late_sign剔除前的完整事件域(真实分桶阈值就是在这个群体上算的，两侧同一套重算
        # 规则/同一套成本，比较才对称；late=0/NaN行的三元组参与打乱但永远选不中任何格)。
        g4 = gate4_placebo_row_bundle_shuffle(ld["event_universe_pre_late"], cell,
                                               o2c_col="o2c", cost_col="_cost_dec",
                                               seed=seed, n_trials=10)
        print(f"  真实净{g4.get('real_net_mean_pct','NA')}% vs 安慰剂均值{g4.get('placebo_mean_pct','NA')}%±{g4.get('placebo_std_pct','NA')} "
              f"→ {'PASS' if g4['pass'] else 'FAIL'}: {g4['reason']}")

        print(f"--- 候选格 {label}: 闸5容量频率(只披露) ---")
        # 竞价额分位用T+1竞价开盘窗口amount(auct_open_amount,元)——入场时点的真实竞价额，
        # 与shared_gates"触发股竞价额分位如实报告"对齐(S1/S6同口径)，而非amount_ma_20代理。
        g5 = gate5_capacity(sub, "liq_bkt", "auct_open_amount", "trade_date", ld["n_calendar_years"], slip)
        print(f"  年触发 {g5['triggers_per_year_est']}  日均 {g5['triggers_per_day_avg']}  "
              f"竞价额中位 {g5['trigger_auction_amount_yuan'].get('median_amount','NA')}元")

        cell_pass = bool(g3["pass"] and g4["pass"])
        cell_verdicts.append({"cell": cell, "gate3": g3, "gate4": g4, "gate5": g5, "pass": cell_pass})

    result["cell_verdicts"] = cell_verdicts
    any_full_pass = any(cv["pass"] for cv in cell_verdicts)
    result["gates_summary"] = {
        "gate1_three_leg": g1["pass"], "gate2_net_margin": True,
        "gate3_reproducibility_any_cell": any(cv["gate3"]["pass"] for cv in cell_verdicts),
        "gate4_placebo_any_cell": any(cv["gate4"]["pass"] for cv in cell_verdicts),
    }
    if any_full_pass:
        # registry kill_criteria 对 S3 有额外一条(其余setup没有)："若有格达标,CANDIDATE前须在
        # holdout 800只上复现同向才可冻结(样本外复现是本剧本特有的第六道闸)"。本引擎本轮禁碰
        # holdout文件，因此即便格点全过研究样本内五道闸，也绝不能输出与S1/S5/S6同款的无条件
        # "CANDIDATE"——必须明确标注: 第六道闸(holdout复现)尚未执行，须监督者另行批准后执行，
        # 冻结前不得视为CANDIDATE。
        result["verdict"] = ("CANDIDATE_PENDING_HOLDOUT(研究样本内全过五闸;S3特有第六道闸=holdout "
                              "800只复现,尚未执行,须监督者另行批准后执行;冻结前不得视为CANDIDATE)")
    else:
        died = []
        if not any(cv["gate3"]["pass"] for cv in cell_verdicts):
            died.append("闸3独立复现")
        if not any(cv["gate4"]["pass"] for cv in cell_verdicts):
            died.append("闸4安慰剂")
        result["verdict"] = f"REJECTED(死于: {', '.join(died) if died else '闸3/闸4'}，达标格点均未能同时通过闸3+闸4)"
    return result


# ============================== S4 主流程 ==============================

def run_s4(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    slip = SlippageTable(SLIPPAGE_PATH)
    ld = load_setup_s4(cost_mode, slip, random_decision_feature, seed)
    ev = ld["event_df"]
    diag = ld["diagnostics"]
    print(f"事件样本(T日候选,已过universe_flag>0/非688/非.BJ/触发/T+1非一字/gap<=7%/regime齐全): "
          f"{len(ev):,}  覆盖T日 {ev['trade_date'].nunique()}")
    print(f"  诊断: 面板总行数={diag['n_panel_rows_total']} universe_flag>0后={diag['n_after_universe_flag']} "
          f"排除688后={diag['n_after_ex688']} 排除.BJ后={diag['n_after_exBJ']} 触发后={diag['n_after_trigger']}")
    print(f"  诊断: regime文件行数={diag['n_mrf_rows_total']} amount_z20为NaN天数={diag['n_amount_z20_nan_days_in_mrf']} "
          f"regime_tag分布(全量regime文件)={diag['regime_tag_value_counts_full_mrf']}")
    print(f"  诊断: 达标事件(过滤后,amt_regime剔除前)={diag['n_qualify_before_amt_regime_exclusion']}  "
          f"amt_regime(amount_z20 NaN日)剔除={diag['n_dropped_amt_regime_nan']}  "
          f"最终事件={diag['n_events_final']}  安慰剂打乱池交易日数={diag['n_placebo_day_pool']}")

    result: dict = {
        "setup_id": "S4_regime_gated_revival", "cost_mode": cost_mode, "seed": seed,
        "selftest_random": random_decision_feature,
        "n_events": int(len(ev)), "n_trade_dates": int(ev["trade_date"].nunique()),
        "diagnostics": diag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if len(ev) < 300:
        result["verdict"] = "INSUFFICIENT(总事件样本<300,推迟终审)"
        return result

    # ---- 闸1：三段分解 ----
    # S4交易结构本身即T+1开盘入场/T+1收盘出场(纯O2C日内段，无隔夜腿参与)——与S3/S5/S6同理，
    # 天然immune于"C2O占比>70%不可成交"问题。如实按结构判定PASS，非事后放水。
    print("\n=== 闸1: 三段分解 ===")
    print("  S4交易结构为T+1开盘入场/T+1收盘出场(纯O2C日内段)，无隔夜腿，闸1天然通过（结构决定，非事后放水）。")
    g1 = {"pass": True, "reason": "S4交易结构本身即O2C日内段(入场=T+1开盘,无隔夜腿)，闸1天然通过",
          "n": int(len(ev)), "c2o_overnight_pct": None, "o2c_intraday_pct": round(float(ev["o2c"].mean()) * 100, 4),
          "c2c_full_day_pct": None, "c2o_share_of_c2c": None}
    result["gate1_three_leg"] = g1
    print(f"  → PASS: {g1['reason']}")

    # ---- 闸2：净边际（6格） ----
    print(f"\n=== 闸2: 净边际(6格: 3 regime_tag x 2 amt_regime, cost_mode={cost_mode}) ===")
    cells_out = []
    passing_cells = []
    for cell in ld["grid_cells"]:
        mask = ((ev["regime_tag"] == cell["regime_tag"]) & (ev["amt_regime"] == cell["amt_regime"]))
        sub = ev[mask]
        stats = cell_stats(sub["o2c"].to_numpy(), sub["_cost_dec"].to_numpy())
        cell_result = {**cell, **stats}
        n_ok = stats.get("n", 0) >= ld["n_min_per_cell"]
        net_ok = n_ok and stats.get("net_pct", -1) > 0 and stats.get("t_net", 0) > 2
        cell_result["meets_n_min"] = n_ok
        cell_result["gate2_pass"] = bool(net_ok)
        cells_out.append(cell_result)
        if net_ok:
            passing_cells.append((cell, sub))
        flag = " ←候选" if net_ok else ""
        print(f"  regime={cell['regime_tag']:<2} amt={cell['amt_regime']:<2}  "
              f"n={stats.get('n',0):>7}  毛O2C {stats.get('gross_o2c_pct','NA')}%  t {stats.get('t_gross','NA')}  "
              f"胜率 {stats.get('hit_pct','NA')}%  净 {stats.get('net_pct','NA')}%  t净 {stats.get('t_net','NA')}{flag}")
    result["gate2_grid"] = cells_out
    result["gate2_pass"] = bool(len(passing_cells) > 0)

    # 对账：6格n之和应等于事件总数(3态x2值=6个互斥完全覆盖的桶，dropna已在loader里做完，
    # ev本身每一行都必属于且只属于其中一格，无遗漏无重复)。
    n_sum_cells = sum(c.get("n", 0) for c in cells_out)
    print(f"  → 达标格点数: {len(passing_cells)}/{len(cells_out)}  6格n之和={n_sum_cells}  事件总数={len(ev)}  "
          f"(差异={len(ev) - n_sum_cells}, 应为0)")
    result["grid_n_reconciliation"] = {
        "sum_of_cell_n": int(n_sum_cells), "n_events": int(len(ev)), "diff": int(len(ev) - n_sum_cells),
    }

    if not passing_cells:
        result["gates_summary"] = {"gate1_three_leg": g1["pass"], "gate2_net_margin": False}
        result["verdict"] = "REJECTED(死于: 闸2净边际，无一格达标)"
        return result

    # ---- 对每个达标格点跑闸3/闸4(day-block)/闸5 ----
    day_regime_table = ld["day_regime_table"]
    ev_universe_pre_amt_regime = ld["event_universe_pre_amt_regime"]
    cell_verdicts = []
    for cell, sub in passing_cells:
        label = f"regime={cell['regime_tag']}|amt={cell['amt_regime']}"
        print(f"\n--- 候选格 {label}: 闸3独立复现 ---")
        g3 = gate3_reproducibility(sub, "o2c", "_cost_dec", seed=seed)
        ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
        print(f"  半A净{ua.get('net_pct','NA')}%(t{ua.get('t_net','NA')}) 半B净{ub.get('net_pct','NA')}%(t{ub.get('t_net','NA')}) "
              f"逐年:{g3['yearly_breakdown']['reason']} → {'PASS' if g3['pass'] else 'FAIL'}")

        print(f"--- 候选格 {label}: 闸4安慰剂(regime标签按【交易日为块】随机重排,x10) ---")
        g4 = gate4_placebo_day_block_shuffle(ev_universe_pre_amt_regime, day_regime_table, cell,
                                              o2c_col="o2c", cost_col="_cost_dec", seed=seed, n_trials=10)
        print(f"  真实净{g4.get('real_net_mean_pct','NA')}% vs 安慰剂均值{g4.get('placebo_mean_pct','NA')}%±{g4.get('placebo_std_pct','NA')} "
              f"→ {'PASS' if g4['pass'] else 'FAIL'}: {g4['reason']}")
        if g4.get("placebo_n_picked_invariant_ok"):
            print(f"  placebo_n_picked不变量自检: 全部{len(g4.get('placebo_n_picked_list', []))}次trial的"
                  f"6格n之和+未映射行 == 事件总数 均成立(通过)")

        print(f"--- 候选格 {label}: 闸5容量频率(只披露) ---")
        g5 = gate5_capacity(sub, "liq_bkt", "auct_open_amount", "trade_date", ld["n_calendar_years"], slip)
        print(f"  年触发 {g5['triggers_per_year_est']}  日均 {g5['triggers_per_day_avg']}  "
              f"竞价额中位 {g5['trigger_auction_amount_yuan'].get('median_amount','NA')}元")

        cell_pass = bool(g3["pass"] and g4["pass"])
        cell_verdicts.append({"cell": cell, "gate3": g3, "gate4": g4, "gate5": g5, "pass": cell_pass})

    result["cell_verdicts"] = cell_verdicts
    any_full_pass = any(cv["pass"] for cv in cell_verdicts)
    result["gates_summary"] = {
        "gate1_three_leg": g1["pass"], "gate2_net_margin": True,
        "gate3_reproducibility_any_cell": any(cv["gate3"]["pass"] for cv in cell_verdicts),
        "gate4_placebo_any_cell": any(cv["gate4"]["pass"] for cv in cell_verdicts),
    }
    if any_full_pass:
        result["verdict"] = "CANDIDATE(仍需人工复核+前瞻跟踪；本引擎绝不判定为可部署)"
    else:
        died = []
        if not any(cv["gate3"]["pass"] for cv in cell_verdicts):
            died.append("闸3独立复现")
        if not any(cv["gate4"]["pass"] for cv in cell_verdicts):
            died.append("闸4安慰剂")
        result["verdict"] = (f"REJECTED(死于: {', '.join(died) if died else '闸3/闸4'}，"
                              f"达标格点均未能同时通过闸3+闸4——即证实: 死形态+对时机也救不活)")
    return result


# ============================== S7/S8 共用主流程 ==============================

def _run_s7_or_s8(setup_id: str, feature_terc_col: str, cost_mode: str, seed: int,
                   random_decision_feature: bool) -> dict:
    """S7与S8结构完全一致(只差特征列名/事件锚不同，装载器已各自处理)，主流程共用一份实现，
    避免复制粘贴产生的两处不同步风险(题目本身也允许"用新包装函数实现"复用共用逻辑，只要不碰
    原有S1-S6的run_*/gate*函数体，这里是新函数，不违反纪律)。"""
    ld = load_setup_s7(cost_mode, random_decision_feature, seed) if setup_id == "S7_late_oversold_absorb" \
        else load_setup_s8(cost_mode, random_decision_feature, seed)
    ev = ld["event_df"]
    diag = ld["diagnostics"]
    n_trade_dates = ev["trade_date"].nunique()
    print(f"事件样本(最终, 已过全部事件域过滤+分桶): {len(ev):,}  覆盖事件日 {n_trade_dates}")

    result: dict = {
        "setup_id": setup_id, "cost_mode": cost_mode, "seed": seed,
        "selftest_random": random_decision_feature,
        "n_events": int(len(ev)), "n_trade_dates": int(n_trade_dates),
        "diagnostics": diag,
        "cost_formula": ("成本(冻结)=佣金8bp单边x2 + 冲击备付(med档5bp/p75档15bp单边)x2 → "
                          "net_med=毛-0.0026, net_p75=毛-0.0046(小数)"),
        "cost_dec_used": ld["cost_dec"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if setup_id == "S8_first_board_tail_absorb":
        result["low_open_overlap_rate_disclose"] = ld.get("low_open_overlap_rate_disclose")

    if len(ev) < 300:
        result["verdict"] = "INSUFFICIENT(总事件样本<300,推迟终审)"
        return result

    # ---- 闸1(改写, registry gate1_rewrite_prereg逐字): 持仓区间(entry->exit)净须为正；
    # C2O/O2C分解仅披露供审计，不作否决项 ----
    print("\n=== 闸1(改写): 持仓区间(含隔夜)净收益须为正；C2O/O2C分解仅披露 ===")
    gross_m = float(ev["gross"].mean())
    net_m = float(ev["net"].mean())
    c2o_m = float(np.nanmean(ev["c2o_disclose"])) if ev["c2o_disclose"].notna().any() else None
    o2c_m = float(np.nanmean(ev["o2c_disclose"])) if ev["o2c_disclose"].notna().any() else None
    g1_pass = bool(net_m > 0)
    g1 = {
        "pass": g1_pass, "n": int(len(ev)),
        "holding_period_gross_pct": round(gross_m * 100, 4),
        "holding_period_net_pct": round(net_m * 100, 4),
        "c2o_disclose_pct": round(c2o_m * 100, 4) if c2o_m is not None else None,
        "o2c_disclose_pct": round(o2c_m * 100, 4) if o2c_m is not None else None,
        "reason": ("持仓区间(改写预注册)净收益为正(结构性含隔夜，不以C2O占比判死，C2O/O2C仅审计披露)"
                   if g1_pass else "持仓区间净收益非正——但此闸为披露改写口径，PASS/FAIL不单独否决整体，"
                   "闸2的逐格净边际才是判死依据"),
    }
    result["gate1_three_leg"] = g1
    print(f"  n={g1['n']}  持仓区间毛{g1['holding_period_gross_pct']}%  净{g1['holding_period_net_pct']}%  "
          f"C2O披露{g1['c2o_disclose_pct']}%  O2C披露{g1['o2c_disclose_pct']}%  → "
          f"{'PASS' if g1['pass'] else 'FAIL(非否决项)'}")

    # ---- 闸2：净边际（6格） ----
    print(f"\n=== 闸2: 净边际(6格: 3 {feature_terc_col} x 2 stab_bkt, cost_mode={cost_mode}) ===")
    cells_out = []
    passing_cells = []
    for cell in ld["grid_cells"]:
        mask = ((ev[feature_terc_col] == cell[feature_terc_col]) & (ev["stab_bkt"] == cell["stab_bkt"]))
        sub = ev[mask]
        stats = cell_stats(sub["gross"].to_numpy(), sub["_cost_dec"].to_numpy())
        cell_result = {**cell, **stats}
        n_ok = stats.get("n", 0) >= ld["n_min_per_cell"]
        net_ok = n_ok and stats.get("net_pct", -1) > 0 and stats.get("t_net", 0) > 2
        cell_result["meets_n_min"] = n_ok
        cell_result["gate2_pass"] = bool(net_ok)
        cells_out.append(cell_result)
        if net_ok:
            passing_cells.append((cell, sub))
        flag = " ←候选" if net_ok else ""
        print(f"  {feature_terc_col}={cell[feature_terc_col]:<2} stab_bkt={cell['stab_bkt']:<4}  "
              f"n={stats.get('n',0):>6}  毛 {stats.get('gross_o2c_pct','NA')}%  t {stats.get('t_gross','NA')}  "
              f"胜率 {stats.get('hit_pct','NA')}%  净 {stats.get('net_pct','NA')}%  t净 {stats.get('t_net','NA')}{flag}")
    result["gate2_grid"] = cells_out
    result["gate2_pass"] = bool(len(passing_cells) > 0)
    n_sum_cells = sum(c.get("n", 0) for c in cells_out)
    print(f"  → 达标格点数: {len(passing_cells)}/{len(cells_out)}  6格n之和={n_sum_cells}  事件总数={len(ev)}  "
          f"(差异={len(ev) - n_sum_cells}, 应为0)")
    result["grid_n_reconciliation"] = {
        "sum_of_cell_n": int(n_sum_cells), "n_events": int(len(ev)), "diff": int(len(ev) - n_sum_cells),
    }

    if not passing_cells:
        result["gates_summary"] = {"gate1_rewrite_holding_period": g1["pass"], "gate2_net_margin": False}
        result["verdict"] = "REJECTED(死于: 闸2净边际，无一格达标)"
        return result

    # ---- 对每个达标格点跑闸3/闸4(双安慰剂)/闸5 ----
    raw_cols = ld["raw_cols"]
    bucket_cols = ld["bucket_cols"]
    bucketize_fn = ld["bucketize_fn"]
    cell_verdicts = []
    for cell, sub in passing_cells:
        label = f"{feature_terc_col}={cell[feature_terc_col]}|stab_bkt={cell['stab_bkt']}"
        print(f"\n--- 候选格 {label}: 闸3独立复现 ---")
        g3 = gate3_reproducibility(sub, "gross", "_cost_dec", seed=seed)
        ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
        print(f"  半A净{ua.get('net_pct','NA')}%(t{ua.get('t_net','NA')}) 半B净{ub.get('net_pct','NA')}%(t{ub.get('t_net','NA')}) "
              f"逐年:{g3['yearly_breakdown']['reason']} → {'PASS' if g3['pass'] else 'FAIL'}")

        print(f"--- 候选格 {label}: 闸4双安慰剂(行级捆绑x10 且 日块重排x10，均须z>2) ---")
        g4 = gate4_placebo_dual(ev, cell, raw_cols, bucket_cols, bucketize_fn,
                                 o2c_col="gross", cost_col="_cost_dec", seed=seed, n_trials=10)
        print(f"  {g4['reason']}")

        print(f"--- 候选格 {label}: 闸5容量频率(只披露) ---")
        # S7/S8未算liq_bkt(registry未要求按流动性档分bucket，只要求"如实报告"频率/竞价额分位)；
        # 传入不存在的列名，gate5_capacity的if liq_col in sub.columns检查会自动跳过按流动性分组
        # (不产生虚假的按trade_date分组的"流动性桶")。
        g5 = gate5_capacity(sub, "_no_liq_bucket_for_s7_s8",
                             "amount_ma_20", "trade_date", ld["n_calendar_years"], SlippageTable(SLIPPAGE_PATH))
        print(f"  年触发 {g5['triggers_per_year_est']}  日均 {g5['triggers_per_day_avg']}")

        cell_pass = bool(g3["pass"] and g4["pass"])
        cell_verdicts.append({"cell": cell, "gate3": g3, "gate4": g4, "gate5": g5, "pass": cell_pass})

    result["cell_verdicts"] = cell_verdicts
    any_full_pass = any(cv["pass"] for cv in cell_verdicts)
    result["gates_summary"] = {
        "gate1_rewrite_holding_period": g1["pass"], "gate2_net_margin": True,
        "gate3_reproducibility_any_cell": any(cv["gate3"]["pass"] for cv in cell_verdicts),
        "gate4_dual_placebo_any_cell": any(cv["gate4"]["pass"] for cv in cell_verdicts),
    }
    if any_full_pass:
        result["verdict"] = ("CANDIDATE_PENDING_HOLDOUT(研究样本内全过五闸(含双安慰剂);"
                              "registry要求幸存格仅最优1格进gate6 holdout一次性复现,尚未执行,"
                              "须监督者另行批准后执行;冻结前不得视为CANDIDATE)")
    else:
        died = []
        if not any(cv["gate3"]["pass"] for cv in cell_verdicts):
            died.append("闸3独立复现")
        if not any(cv["gate4"]["pass"] for cv in cell_verdicts):
            died.append("闸4双安慰剂")
        result["verdict"] = f"REJECTED(死于: {', '.join(died) if died else '闸3/闸4'}，达标格点均未能同时通过闸3+闸4)"
    return result


def run_s7(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    return _run_s7_or_s8("S7_late_oversold_absorb", "vwap_dev_terc", cost_mode, seed, random_decision_feature)


def run_s8(cost_mode: str, seed: int, random_decision_feature: bool) -> dict:
    return _run_s7_or_s8("S8_first_board_tail_absorb", "pullback_terc", cost_mode, seed, random_decision_feature)


# ============================== 报告输出 ==============================

def write_reports(result: dict, setup_id: str, suffix: str = "") -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    # 反伪造纪律(评审must_fix#1): med/p75 两档报告绝不允许同名互相覆盖,cost_mode 必须进文件名。
    cost_mode = str(result.get("cost_mode") or "med")
    tag = f"{setup_id}{suffix}_{cost_mode}_{today}"
    json_path = OUT_DIR / f"setup_{tag}.json"
    md_path = OUT_DIR / f"setup_{tag}.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    md = build_markdown_report(result)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md_path, json_path


def build_markdown_report(r: dict) -> str:
    lines = [f"# 剧本闯关机报告: {r['setup_id']}\n"]
    lines.append(f"- cost_mode: {r.get('cost_mode')}  seed: {r.get('seed')}  "
                  f"selftest_random: {r.get('selftest_random')}")
    lines.append(f"- 生成时间: {r.get('generated_at')}")
    lines.append(f"- 事件样本数: {r.get('n_events')}")
    if "unit_check" in r:
        lines.append("\n## 单位自检(ca_amount vs 面板amount)\n")
        for u in r["unit_check"]:
            lines.append(f"- {u.get('sample_date')}: n={u.get('n')} 中位比值={u.get('ratio_median')} "
                          f"落在预期(0,0.2)={u.get('in_expected_range')} — {u.get('note')}")
    if "reconciliation" in r:
        rc = r["reconciliation"]
        lines.append(f"\n## 对账\n- T+1竞价撮合价 vs T+1面板开盘价中位偏差: {rc.get('t1_gap_vs_open1_median_dev')} "
                      f"(阈值<{rc.get('threshold')}) → {'OK' if rc.get('ok') else 'FAIL'}")
    if r.get("verdict", "").startswith("INSUFFICIENT"):
        lines.append(f"\n## 总判定\n\n**→ {r['verdict']}**\n")
        return "\n".join(lines) + "\n"

    lines.append(f"\n## 闸1: 三段分解\n\n{json.dumps(r.get('gate1_three_leg', {}), ensure_ascii=False, indent=2)}\n")

    if "gate2_grid" in r and r.get("setup_id") == "S5_sector_auction_resonance":
        lines.append("## 闸2: 净边际(格点表)\n")
        lines.append("| sector_strength | sector_amt | leader | n | 毛O2C% | t | 胜率% | 净% | t净 | 达标 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in r["gate2_grid"]:
            lines.append(f"| {c['sector_strength']} | {c['sector_amt']} | {c['leader']} | {c.get('n',0)} | "
                          f"{c.get('gross_o2c_pct','NA')} | {c.get('t_gross','NA')} | {c.get('hit_pct','NA')} | "
                          f"{c.get('net_pct','NA')} | {c.get('t_net','NA')} | {'PASS' if c.get('gate2_pass') else '-'} |")
        if "grid_n_reconciliation" in r:
            gr = r["grid_n_reconciliation"]
            lines.append(f"\n12格n之和={gr.get('sum_of_cell_n')}  事件总数={gr.get('n_events')}  差异={gr.get('diff')}\n")
    elif "gate2_grid" in r and r.get("setup_id") == "S3_intraday_shape_daily":
        lines.append("## 闸2: 净边际(格点表)\n")
        lines.append("| am_share | late_sign | smooth | n | 毛O2C% | t | 胜率% | 净% | t净 | 达标 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in r["gate2_grid"]:
            lines.append(f"| {c['am_share']} | {c['late_sign']} | {c['smooth']} | {c.get('n',0)} | "
                          f"{c.get('gross_o2c_pct','NA')} | {c.get('t_gross','NA')} | {c.get('hit_pct','NA')} | "
                          f"{c.get('net_pct','NA')} | {c.get('t_net','NA')} | {'PASS' if c.get('gate2_pass') else '-'} |")
        if "grid_n_reconciliation" in r:
            gr = r["grid_n_reconciliation"]
            lines.append(f"\n12格n之和={gr.get('sum_of_cell_n')}  "
                          f"+被剔除late_strength=0/NaN行数={gr.get('n_late_strength_zero_or_nan_excluded')}  "
                          f"vs 事件总数(late_sign剔除前)={gr.get('n_events_before_late_sign_exclusion')}  "
                          f"差异={gr.get('diff')}\n")
    elif "gate2_grid" in r and r.get("setup_id") == "S4_regime_gated_revival":
        lines.append("## 闸2: 净边际(格点表)\n")
        lines.append("| regime_tag | amt_regime | n | 毛O2C% | t | 胜率% | 净% | t净 | 达标 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for c in r["gate2_grid"]:
            lines.append(f"| {c['regime_tag']} | {c['amt_regime']} | {c.get('n',0)} | "
                          f"{c.get('gross_o2c_pct','NA')} | {c.get('t_gross','NA')} | {c.get('hit_pct','NA')} | "
                          f"{c.get('net_pct','NA')} | {c.get('t_net','NA')} | {'PASS' if c.get('gate2_pass') else '-'} |")
        if "grid_n_reconciliation" in r:
            gr = r["grid_n_reconciliation"]
            lines.append(f"\n6格n之和={gr.get('sum_of_cell_n')}  事件总数={gr.get('n_events')}  差异={gr.get('diff')}\n")
    elif "gate2_grid" in r and r.get("setup_id") in ("S7_late_oversold_absorb", "S8_first_board_tail_absorb"):
        feat_col = "vwap_dev_terc" if r.get("setup_id") == "S7_late_oversold_absorb" else "pullback_terc"
        lines.append("## 闸2: 净边际(格点表)\n")
        lines.append(f"| {feat_col} | stab_bkt | n | 毛% | t | 胜率% | 净% | t净 | 达标 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for c in r["gate2_grid"]:
            lines.append(f"| {c[feat_col]} | {c['stab_bkt']} | {c.get('n',0)} | "
                          f"{c.get('gross_o2c_pct','NA')} | {c.get('t_gross','NA')} | {c.get('hit_pct','NA')} | "
                          f"{c.get('net_pct','NA')} | {c.get('t_net','NA')} | {'PASS' if c.get('gate2_pass') else '-'} |")
        if "grid_n_reconciliation" in r:
            gr = r["grid_n_reconciliation"]
            lines.append(f"\n6格n之和={gr.get('sum_of_cell_n')}  事件总数={gr.get('n_events')}  差异={gr.get('diff')}\n")
        if r.get("cost_formula"):
            lines.append(f"\n{r['cost_formula']}\n")
        if r.get("low_open_overlap_rate_disclose") is not None:
            lines.append(f"\n低开子集重叠率(gap_sign<0且gap<-2%,仅披露): "
                          f"{r['low_open_overlap_rate_disclose']*100:.2f}%\n")
    elif "gate2_grid" in r:
        lines.append("## 闸2: 净边际(格点表)\n")
        lines.append("| Tbkt | ca_amt_share | ca_move | n | 毛O2C% | t | 胜率% | 净% | t净 | 达标 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in r["gate2_grid"]:
            lines.append(f"| {c['Tbkt']} | {c['ca_amt_share_terc']} | {c['ca_move']:+.0f} | {c.get('n',0)} | "
                          f"{c.get('gross_o2c_pct','NA')} | {c.get('t_gross','NA')} | {c.get('hit_pct','NA')} | "
                          f"{c.get('net_pct','NA')} | {c.get('t_net','NA')} | {'PASS' if c.get('gate2_pass') else '-'} |")
    if "gate2_rules" in r:
        lines.append("## 闸2: 净边际(4条预注册确认规则)\n")
        lines.append("| 规则 | 确认n | 确认净% | 确认胜率% | 对照n | 对照净% | 对照胜率% | 胜率差pp | z | 达标 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for rname, rr in r["gate2_rules"].items():
            c, u = rr["confirmed"], rr["unconfirmed"]
            lines.append(f"| {rname} | {c.get('n',0)} | {c.get('net_pct','NA')} | {c.get('hit_pct','NA')} | "
                          f"{u.get('n',0)} | {u.get('net_pct','NA')} | {u.get('hit_pct','NA')} | "
                          f"{rr.get('hit_diff_pp','NA')} | {rr.get('z_prop','NA')} | {'PASS' if rr.get('pass') else '-'} |")

    if "cell_verdicts" in r:
        lines.append("\n## 闸3/闸4/闸5: 达标格点逐一复核\n")
        is_s5 = r.get("setup_id") == "S5_sector_auction_resonance"
        is_s3 = r.get("setup_id") == "S3_intraday_shape_daily"
        is_s4 = r.get("setup_id") == "S4_regime_gated_revival"
        is_s7 = r.get("setup_id") == "S7_late_oversold_absorb"
        is_s8 = r.get("setup_id") == "S8_first_board_tail_absorb"
        for cv in r["cell_verdicts"]:
            cell = cv["cell"]
            if is_s5:
                cell_label = f"strength={cell['sector_strength']} | amt={cell['sector_amt']} | leader={cell['leader']}"
            elif is_s3:
                cell_label = f"am_share={cell['am_share']} | late_sign={cell['late_sign']} | smooth={cell['smooth']}"
            elif is_s4:
                cell_label = f"regime={cell['regime_tag']} | amt_regime={cell['amt_regime']}"
            elif is_s7:
                cell_label = f"vwap_dev_terc={cell['vwap_dev_terc']} | stab_bkt={cell['stab_bkt']}"
            elif is_s8:
                cell_label = f"pullback_terc={cell['pullback_terc']} | stab_bkt={cell['stab_bkt']}"
            else:
                cell_label = f"{cell['Tbkt']} | {cell['ca_amt_share_terc']} | move{cell['ca_move']:+.0f}"
            lines.append(f"### 格点 {cell_label}\n")
            lines.append(f"- 闸3独立复现: {'PASS' if cv['gate3']['pass'] else 'FAIL'} — {cv['gate3'].get('reason', cv['gate3'].get('yearly_breakdown',{}).get('reason',''))}")
            lines.append(f"- 闸4安慰剂: {'PASS' if cv['gate4']['pass'] else 'FAIL'} — {cv['gate4'].get('reason')}")
            lines.append(f"- 闸5容量(披露): 年触发{cv['gate5'].get('triggers_per_year_est')} "
                          f"日均{cv['gate5'].get('triggers_per_day_avg')} "
                          f"竞价额中位{cv['gate5']['trigger_auction_amount_yuan'].get('median_amount','NA')}元\n")
    if "rule_verdicts" in r:
        lines.append("\n## 闸3/闸4/闸5: 达标规则逐一复核\n")
        for rv in r["rule_verdicts"]:
            lines.append(f"### 规则 {rv['rule']}\n")
            lines.append(f"- 闸3独立复现(奇偶周+逐年表): {'PASS' if rv['gate3']['pass'] else 'FAIL'}")
            lines.append(f"- 闸4安慰剂: {'PASS' if rv['gate4']['pass'] else 'FAIL'} — {rv['gate4'].get('reason')}")
            lines.append(f"- 闸5容量(披露): 年触发{rv['gate5'].get('triggers_per_year_est')} "
                          f"日均{rv['gate5'].get('triggers_per_day_avg')} "
                          f"竞价额中位{rv['gate5']['trigger_auction_amount_yuan'].get('median_amount','NA')}元\n")

    lines.append("## 总判定\n")
    for name, p in r.get("gates_summary", {}).items():
        lines.append(f"- {name}: {'PASS' if p else 'FAIL'}")
    lines.append(f"\n**→ {r.get('verdict')}**\n")
    lines.append("\n口径: registry预注册格点/成功标准逐字执行；成本=双边佣金印花16bp+滑点表实测"
                  "(按流动性x开盘gap档查slip15，模式见cost_mode)；五道闸见setup_registry.json的shared_gates。"
                  "本引擎绝不输出'可部署'，CANDIDATE仍需人工复核+前瞻跟踪。")
    return "\n".join(lines) + "\n"


# ============================== 主入口 ==============================

def main() -> int:
    ap = argparse.ArgumentParser(description="剧本(Setup)五道闸终审引擎")
    ap.add_argument("--setup", required=True, help="setup id，如 S1_close_auction_anomaly / S6_list_auction_confirm")
    ap.add_argument("--cost-mode", choices=["med", "p75"], default="med", help="成本模式，默认med")
    ap.add_argument("--seed", type=int, default=20260703, help="闸3/闸4固定随机种子")
    ap.add_argument("--selftest-random", action="store_true",
                     help="验收模式：把决策特征替换为纯随机值，预期必须REJECTED(死于闸4)")
    ap.add_argument("--out-suffix", default="", help="报告文件名后缀，避免覆盖(如_selftest)")
    args = ap.parse_args()

    registry = load_registry()
    setup_def = get_setup_def(registry, args.setup)
    if setup_def.get("status") != "registered":
        raise SystemExit(f"setup {args.setup} 状态={setup_def.get('status')}，格点未冻结，不可跑(题目纪律：pending_grid禁跑)")

    print(f"=== 剧本闯关机 setup_gauntlet ===")
    print(f"setup: {args.setup}  cost_mode: {args.cost_mode}  selftest_random: {args.selftest_random}")
    print(f"假设: {setup_def.get('hypothesis')}\n")

    if args.setup == "S1_close_auction_anomaly":
        result = run_s1(args.cost_mode, args.seed, args.selftest_random)
    elif args.setup == "S6_list_auction_confirm":
        result = run_s6(args.cost_mode, args.seed, args.selftest_random)
    elif args.setup == "S5_sector_auction_resonance":
        result = run_s5(args.cost_mode, args.seed, args.selftest_random)
    elif args.setup == "S3_intraday_shape_daily":
        result = run_s3(args.cost_mode, args.seed, args.selftest_random)
    elif args.setup == "S4_regime_gated_revival":
        result = run_s4(args.cost_mode, args.seed, args.selftest_random)
    elif args.setup == "S7_late_oversold_absorb":
        result = run_s7(args.cost_mode, args.seed, args.selftest_random)
    elif args.setup == "S8_first_board_tail_absorb":
        result = run_s8(args.cost_mode, args.seed, args.selftest_random)
    else:
        raise SystemExit(f"setup {args.setup} 尚无装载器实现（仅S1/S3/S4/S5/S6/S7/S8已实现；其余为registered_pending_grid,格点未冻结不可跑）")

    print(f"\n=== 总判定 ===\n  → {result['verdict']}")

    suffix = args.out_suffix or ("_selftest_random" if args.selftest_random else "")
    md_path, json_path = write_reports(result, args.setup, suffix)
    print(f"\n报告已写出: {md_path}")
    print(f"数据已写出: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
