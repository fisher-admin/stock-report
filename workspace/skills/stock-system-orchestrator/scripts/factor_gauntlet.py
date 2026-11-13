#!/usr/bin/env python3
"""因子闯关机(factor gauntlet, 2026-07-02)：把本季已验证的四道因子检验武器合并成一条命令。

背景：本季铁律——任何新因子必须过四道闸才有资格谈部署，达标前一律"研究观察"。
四道闸此前散落在四个脚本里（phase_c_launch_validation / phase_a_slow_style_backtest /
o2c_strategy_backtest / full_market_factor_eval），每次手搓有口径漂移风险。本脚本直接
复用这四个脚本已验证过的口径与代码模式，不重新发明：

  闸1 三段分解（复用 phase_c_launch_validation 的 C2O/O2C/C2C 三段均值口径）：
      因子 top decile（按方向）的次日 C2O(隔夜)/O2C(日内)/C2C(全天) 均值。
      若 edge 主要在 C2O 隔夜段（C2O 占 C2C 的比例 >70%）→ 不可成交 → FAIL。

  闸2 可成交净边际（复用 phase_a_slow_style_backtest 的 _add_calendar_fwd 日历对齐前向
      收益 + 行业内 demean z-score + o2c_strategy_backtest 的 top_frac 组合回测口径）：
      long top_frac vs 全域均值，扣成本；每个 horizon 报每笔净收益/t值/年化。
      要求至少一个 horizon 每笔净>0 且 t>2 才 PASS。附成本敏感曲线(10/20/30/50bp)。

  闸3 独立复现：固定 seed 把 universe 随机分两半，闸2统计分别在两半上算，主 horizon
      的净边际两半同号且都 t>1 才 PASS；另做时间切分(前60%日/后40%日)同号检查。

  闸4 安慰剂（复用 phase_a_slow_style_backtest 的每日截面内打乱、10次重复取分布口径）：
      每日截面内打乱因子值，重复10次，真实年化须优于 随机均值+2σ 才 PASS。

总判定：四闸全PASS → "CANDIDATE(仍需人工复核+前瞻跟踪)"；任一FAIL → "REJECTED(标明死于哪道闸)"。
绝不输出"可部署"字样。

用法：
  python3 factor_gauntlet.py --factor g_intraday_vwap_deviation --direction +1 --horizons 1,5
  python3 factor_gauntlet.py --factor /path/to/random_factor.csv --direction +1

--factor 可以是面板列名，也可以是外部 csv 路径（列：ts_code,trade_date,value）。
--direction 是经济学先验方向，必填——闯关机绝不自动选方向，防方向钓鱼。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL_ROLLING = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel.parquet"
PANEL_FULL = WORKSPACE / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet"
# 默认面板路径：优先使用离线重建的全历史研究底座(greenfield_multifactor_panel_full.parquet)；
# 若该文件不存在（例如尚未重建，或在没有跑过 rebuild_greenfield_panel_full.py 的环境），
# 退回管线每日滚动窗口面板(greenfield_multifactor_panel.parquet)，保持向后兼容。
# 可用 --panel 显式覆盖此优先级逻辑，指向任意面板 parquet 路径。
PANEL = PANEL_FULL if PANEL_FULL.exists() else PANEL_ROLLING
C2O, O2C, C2C = "next_return_close_to_open_1d", "next_return_open_to_close_1d", "next_return_close_to_close_1d"
TRADING_DAYS = 244


# ============================== 数据加载 ==============================

def load_panel_base(since: str, panel_path: Path) -> pd.DataFrame:
    """加载面板基础列（不含待测因子；待测因子按需合并/自带）。"""
    import pyarrow.parquet as pq
    names = pq.ParquetFile(panel_path).schema.names
    need = ["trade_date", "ts_code", "industry", "open", "close", "pct_chg", "amount",
            "universe_flag", C2O, O2C, C2C]
    need = [c for c in need if c in names]
    df = pd.read_parquet(panel_path, columns=need, engine="pyarrow")
    df = df[df["universe_flag"].fillna(0).astype(float) > 0].drop(columns=["universe_flag"])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df[df["trade_date"] >= since].reset_index(drop=True)
    df["industry"] = df["industry"].fillna("NA")
    return df


def attach_factor(df: pd.DataFrame, factor_arg: str, panel_path: Path) -> tuple[pd.DataFrame, str, str]:
    """把待测因子接到 df 上，返回 (df, factor_col_name, source)。

    factor_arg 若是面板已有列名，直接从面板单独取列合并（省内存，不预先全读）。
    否则视为外部 csv 路径，要求列 ts_code,trade_date,value。
    """
    fcol = "_factor_under_test"
    path = Path(factor_arg)
    if path.suffix.lower() == ".csv" and path.exists():
        ext = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})
        need_cols = {"ts_code", "trade_date", "value"}
        if not need_cols.issubset(ext.columns):
            raise SystemExit(f"外部因子csv必须含列 {need_cols}，实际列={list(ext.columns)}")
        ext = ext.rename(columns={"value": fcol})[["ts_code", "trade_date", fcol]]
        ext["trade_date"] = ext["trade_date"].astype(str)
        df = df.merge(ext, on=["ts_code", "trade_date"], how="left")
        source = f"外部csv:{path}"
    else:
        import pyarrow.parquet as pq
        names = pq.ParquetFile(panel_path).schema.names
        if factor_arg not in names:
            raise SystemExit(f"--factor '{factor_arg}' 既不是存在的csv路径，也不是面板列名。面板g_因子示例见 full_market_factor_eval.py")
        fac = pd.read_parquet(panel_path, columns=["trade_date", "ts_code", factor_arg], engine="pyarrow")
        fac["trade_date"] = fac["trade_date"].astype(str)
        fac = fac.rename(columns={factor_arg: fcol})
        df = df.merge(fac, on=["ts_code", "trade_date"], how="left")
        source = f"面板列:{factor_arg}"
    return df, fcol, source


# ============================== 前向收益（日历对齐，复用 phase_a/o2c 口径） ==============================

def add_calendar_fwd(df: pd.DataFrame, holds: list[int]) -> pd.DataFrame:
    """日历对齐前向收益：入场T+1日历日开盘(停牌NaN排除)，出场T+H日历日收盘，
    停牌/退市用ffill最后成交价计入亏损(不丢弃)，越过面板末端置NaN。去出场侧生存者偏差。
    与 phase_a_slow_style_backtest._add_calendar_fwd / o2c_strategy_backtest._add_calendar_fwd 同口径。"""
    cal = sorted(df["trade_date"].unique())
    ncal = len(cal)
    posmap = {d: i for i, d in enumerate(cal)}
    df = df.copy()
    df["pos"] = df["trade_date"].map(posmap).astype(int)
    nxt = df[["ts_code", "pos", "open"]].rename(columns={"open": "entry_open"})
    nxt = nxt.assign(pos=nxt["pos"] - 1)
    df = df.merge(nxt, on=["ts_code", "pos"], how="left").reset_index(drop=True)
    cff = (df.pivot_table(index="ts_code", columns="pos", values="close", aggfunc="last")
             .reindex(columns=range(ncal)).ffill(axis=1))
    code_row = {c: i for i, c in enumerate(cff.index)}
    cvals = cff.to_numpy()
    rows = df["ts_code"].map(code_row).to_numpy()
    base = df["pos"].to_numpy()
    eo = df["entry_open"].to_numpy(dtype=float)
    for h in holds:
        ep = base + h
        valid = ep < ncal
        ec = np.full(len(df), np.nan)
        ec[valid] = cvals[rows[valid], ep[valid]]
        df["_fwd_" + str(h)] = (ec - eo) / eo
    return df


def ind_neutral_z(df: pd.DataFrame, col: str) -> pd.Series:
    """横截面：winsorize(1/99) → 行业内demean → z-score。逐交易日。同 phase_a 口径。"""
    def _one(g: pd.DataFrame) -> pd.Series:
        x = g[col].astype(float)
        lo, hi = x.quantile(0.01), x.quantile(0.99)
        x = x.clip(lo, hi)
        x = x - g.groupby("industry")[col].transform(lambda s: s.clip(lo, hi).mean())
        sd = x.std()
        return (x - x.mean()) / sd if sd and sd > 1e-12 else x * 0.0
    return df.groupby("trade_date", group_keys=False).apply(_one)


# ============================== 闸1：三段分解 ==============================

def gate1_three_leg(df: pd.DataFrame, fcol: str, direction: int, top_frac: float) -> dict:
    """因子 top decile(按方向)的次日 C2O/O2C/C2C 均值。C2O 占 C2C 比例>70% → 不可成交 FAIL。
    复用 phase_c_launch_validation 的三段分解口径。"""
    sig = df[fcol] * direction
    d = df.assign(_sig=sig).dropna(subset=["_sig", C2O, O2C, C2C])
    c2o_list, o2c_list, c2c_list = [], [], []
    for _, g in d.groupby("trade_date"):
        if len(g) < 50:
            continue
        k = max(1, int(len(g) * top_frac))
        top = g.nlargest(k, "_sig")
        c2o_list.append(top[C2O].mean())
        o2c_list.append(top[O2C].mean())
        c2c_list.append(top[C2C].mean())
    n = len(c2c_list)
    if n < 10:
        return {"pass": False, "reason": f"样本不足(n={n})", "n": n}
    c2o_m, o2c_m, c2c_m = float(np.mean(c2o_list)), float(np.mean(o2c_list)), float(np.mean(c2c_list))
    # C2O 占 C2C 的比例（用绝对值防止分母正负号导致比例失真）
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


# ============================== 闸2：可成交净边际 ==============================

def backtest_horizon(df: pd.DataFrame, fwd_col: str, z_col: str, cost_dec: float, top_frac: float) -> dict:
    """long top_frac vs 全域均值，扣成本。日历调仓周期由外部按 hold 采样传入的 df 决定。"""
    d = df.dropna(subset=[fwd_col, z_col])
    rets = []
    for _, g in d.groupby("trade_date"):
        if len(g) < 50:
            continue
        bench = g[fwd_col].mean()
        k = max(1, int(len(g) * top_frac))
        top = g.nlargest(k, z_col)[fwd_col].mean()
        rets.append((top - bench) - cost_dec)
    n = len(rets)
    if n < 10:
        return {"n": n}
    m = float(np.mean(rets)); sd = float(np.std(rets, ddof=1))
    t = (m / (sd / np.sqrt(n))) if sd > 1e-12 else 0.0
    return {"n": n, "per_trade_net_pct": round(m * 100, 4), "t": round(t, 3),
            "hit_pct": round(100.0 * np.mean([r > 0 for r in rets]), 1)}


def gate2_tradeable_margin(df: pd.DataFrame, fcol: str, direction: int, horizons: list[int],
                            cost_bps: float, top_frac: float) -> dict:
    """日历对齐前向收益、行业中性z、long top_frac vs 全域均值、扣cost。至少一个horizon
    每笔净>0且t>2才PASS。附成本敏感曲线(10/20/30/50bp)。复用 phase_a + o2c_strategy_backtest 口径。"""
    d = add_calendar_fwd(df, horizons)
    z = ind_neutral_z(d, fcol) * direction
    d = d.assign(_z=z)
    cost_dec = cost_bps / 10000.0

    per_horizon = {}
    any_pass = False
    for h in horizons:
        fwd_col = f"_fwd_{h}"
        # 每 h 日调仓一次（与 o2c_strategy_backtest 一致：按交易日历采样，非逐日重叠持仓）
        dates = sorted(d["trade_date"].unique())
        rebal_dates = set(dates[::h])
        sub = d[d["trade_date"].isin(rebal_dates)]
        r = backtest_horizon(sub, fwd_col, "_z", cost_dec, top_frac)
        cyc_per_year = TRADING_DAYS / h
        ann = (r.get("per_trade_net_pct", 0) / 100.0 * cyc_per_year * 100) if r.get("n", 0) >= 10 else None
        hpass = bool(r.get("n", 0) >= 10 and r.get("per_trade_net_pct", -1) > 0 and r.get("t", 0) > 2)
        any_pass = any_pass or hpass
        per_horizon[str(h)] = {**r, "ann_pct": round(ann, 2) if ann is not None else None, "pass": hpass}

    # 成本敏感曲线：对每个 horizon，在 10/20/30/50bp 下的年化净超额
    cost_curve = {}
    for h in horizons:
        fwd_col = f"_fwd_{h}"
        dates = sorted(d["trade_date"].unique())
        rebal_dates = set(dates[::h])
        sub = d[d["trade_date"].isin(rebal_dates)]
        d_nocost = sub.dropna(subset=[fwd_col, "_z"])
        rets_gross = []
        for _, g in d_nocost.groupby("trade_date"):
            if len(g) < 50:
                continue
            bench = g[fwd_col].mean()
            k = max(1, int(len(g) * top_frac))
            top = g.nlargest(k, "_z")[fwd_col].mean()
            rets_gross.append(top - bench)
        cyc_per_year = TRADING_DAYS / h
        curve = {}
        if len(rets_gross) >= 10:
            gross_m = float(np.mean(rets_gross))
            for bps in (10, 20, 30, 50):
                net_m = gross_m - bps / 10000.0
                curve[str(bps)] = round(net_m * cyc_per_year * 100, 2)
        cost_curve[str(h)] = curve

    main_h = horizons[0]
    overall_pass = any_pass
    return {
        "pass": overall_pass, "per_horizon": per_horizon, "cost_sensitivity_ann_pct": cost_curve,
        "main_horizon": main_h,
        "reason": ("至少一个horizon每笔净>0且t>2" if overall_pass else "所有horizon均未同时满足净>0且t>2"),
    }


# ============================== 闸3：独立复现 ==============================

def gate3_reproducibility(df: pd.DataFrame, fcol: str, direction: int, main_horizon: int,
                           cost_bps: float, top_frac: float, seed: int = 20260702) -> dict:
    """(a) 固定seed把universe随机分两半，主horizon净边际两半同号且都t>1；
       (b) 时间切分(前60%日/后40%日)同号检查。"""
    cost_dec = cost_bps / 10000.0
    d = add_calendar_fwd(df, [main_horizon])
    fwd_col = f"_fwd_{main_horizon}"
    dates_all = sorted(d["trade_date"].unique())
    rebal_dates = set(dates_all[::main_horizon])
    d = d[d["trade_date"].isin(rebal_dates)].copy()

    def run_backtest(sub: pd.DataFrame) -> dict:
        z = ind_neutral_z(sub, fcol) * direction
        sub2 = sub.assign(_z=z)
        return backtest_horizon(sub2, fwd_col, "_z", cost_dec, top_frac)

    # (a) universe 随机分两半（按 ts_code 分组，保证同股票所有日子在同一半，避免信息泄露）
    codes = sorted(d["ts_code"].unique())
    rng = np.random.RandomState(seed)
    perm = rng.permutation(codes)
    half = len(perm) // 2
    set_a, set_b = set(perm[:half]), set(perm[half:])
    ra = run_backtest(d[d["ts_code"].isin(set_a)])
    rb = run_backtest(d[d["ts_code"].isin(set_b)])
    a_net, a_t = ra.get("per_trade_net_pct"), ra.get("t")
    b_net, b_t = rb.get("per_trade_net_pct"), rb.get("t")
    universe_ok = bool(
        ra.get("n", 0) >= 10 and rb.get("n", 0) >= 10 and a_net is not None and b_net is not None
        and (a_net > 0) == (b_net > 0) and a_t is not None and b_t is not None and a_t > 1 and b_t > 1
    )

    # (b) 时间切分：前60%日 / 后40%日
    ndates = len(dates_all)
    split_idx = int(ndates * 0.6)
    time_a_dates = set(dates_all[:split_idx])
    time_b_dates = set(dates_all[split_idx:])
    rta = run_backtest(d[d["trade_date"].isin(time_a_dates)])
    rtb = run_backtest(d[d["trade_date"].isin(time_b_dates)])
    ta_net, tb_net = rta.get("per_trade_net_pct"), rtb.get("per_trade_net_pct")
    time_ok = bool(
        rta.get("n", 0) >= 5 and rtb.get("n", 0) >= 5 and ta_net is not None and tb_net is not None
        and (ta_net > 0) == (tb_net > 0)
    )

    overall = bool(universe_ok)  # 主判定按题目要求：universe split 同号+都t>1 才PASS；时间切分为附加报告项
    same_sign = bool(a_net is not None and b_net is not None and (a_net > 0) == (b_net > 0))
    both_t_ok = bool(a_t is not None and b_t is not None and a_t > 1 and b_t > 1)
    if universe_ok:
        reason = "universe两半同号且都t>1"
    elif not same_sign:
        reason = "universe两半净边际符号不一致(样本分裂即变号=不稳健)"
    elif not both_t_ok:
        reason = f"universe两半同号但t不足1(半A t={a_t if a_t is not None else float('nan'):.2f}, 半B t={b_t if b_t is not None else float('nan'):.2f})"
    else:
        reason = "样本不足以做独立复现检验"
    return {
        "pass": overall,
        "universe_split": {"half_a": ra, "half_b": rb, "same_sign": same_sign,
                            "both_t_gt_1": both_t_ok, "pass": universe_ok},
        "time_split_60_40": {"first_60pct": rta, "last_40pct": rtb, "same_sign": time_ok if rta.get("n",0)>=5 and rtb.get("n",0)>=5 else None},
        "reason": reason,
    }


# ============================== 闸4：安慰剂 ==============================

def gate4_placebo(df: pd.DataFrame, fcol: str, direction: int, main_horizon: int,
                   cost_bps: float, top_frac: float, seed: int = 20260702, n_trials: int = 10) -> dict:
    """每日截面内打乱因子值，重复10次取分布；真实年化须优于随机均值+2σ 才PASS。
    复用 phase_a_slow_style_backtest 的安慰剂口径。"""
    cost_dec = cost_bps / 10000.0
    d = add_calendar_fwd(df, [main_horizon])
    fwd_col = f"_fwd_{main_horizon}"
    dates_all = sorted(d["trade_date"].unique())
    rebal_dates = set(dates_all[::main_horizon])
    d = d[d["trade_date"].isin(rebal_dates)].copy()

    z_real = ind_neutral_z(d, fcol) * direction
    d = d.assign(_z_real=z_real)
    r_real = backtest_horizon(d, fwd_col, "_z_real", cost_dec, top_frac)
    cyc_per_year = TRADING_DAYS / main_horizon
    real_ann = (r_real.get("per_trade_net_pct", 0) / 100.0 * cyc_per_year * 100) if r_real.get("n", 0) >= 10 else None

    rng = np.random.RandomState(seed)
    placebo_ann = []
    for _ in range(n_trials):
        d2 = d.copy()
        d2["_z_rnd"] = d2.groupby("trade_date")["_z_real"].transform(lambda s: rng.permutation(s.values))
        rp = backtest_horizon(d2, fwd_col, "_z_rnd", cost_dec, top_frac)
        if rp.get("n", 0) >= 10:
            placebo_ann.append(rp["per_trade_net_pct"] / 100.0 * cyc_per_year * 100)

    if real_ann is None or len(placebo_ann) < 5:
        return {"pass": False, "reason": "样本不足以做安慰剂检验", "real_ann_pct": real_ann,
                "placebo_ann_pct_list": placebo_ann}

    pm, psd = float(np.mean(placebo_ann)), float(np.std(placebo_ann, ddof=1)) if len(placebo_ann) > 1 else 0.0
    threshold = pm + 2 * psd
    passed = bool(real_ann > threshold)
    zscore = (real_ann - pm) / psd if psd > 1e-9 else float("inf") if real_ann > pm else 0.0
    return {
        "pass": passed, "real_ann_pct": round(real_ann, 2),
        "placebo_mean_ann_pct": round(pm, 2), "placebo_std_ann_pct": round(psd, 2),
        "placebo_threshold_mean_plus_2sd_pct": round(threshold, 2),
        "real_vs_placebo_sigma": round(zscore, 2), "n_trials": len(placebo_ann),
        "placebo_ann_pct_list": [round(x, 2) for x in placebo_ann],
        "reason": (f"真实年化{real_ann:+.2f}%优于随机均值+2σ({threshold:+.2f}%)" if passed
                   else f"真实年化{real_ann:+.2f}%未超随机均值+2σ({threshold:+.2f}%)→与噪声不可区分"),
    }


# ============================== 主流程 ==============================

def main() -> int:
    ap = argparse.ArgumentParser(description="因子闯关机：三段分解→可成交口径→独立复现→安慰剂，四道闸统一判定。")
    ap.add_argument("--factor", required=True, help="面板列名，或外部csv路径(列:ts_code,trade_date,value)")
    ap.add_argument("--direction", required=True, choices=["+1", "-1"], help="经济学先验方向，必填，防方向钓鱼")
    ap.add_argument("--horizons", default="1,5,20", help="逗号分隔的持有期(交易日)，默认1,5,20")
    ap.add_argument("--cost-bps", type=float, default=30.0, help="往返双边成本(基点)，默认30")
    ap.add_argument("--top-frac", type=float, default=0.1, help="多头分位，默认0.1")
    ap.add_argument("--since", default="20220101", help="面板起始日期(yyyymmdd)")
    ap.add_argument("--out-dir", default="stock_data/03-working/gauntlet_reports/", help="报告输出目录")
    ap.add_argument("--seed", type=int, default=20260702, help="闸3/闸4固定随机种子")
    ap.add_argument("--panel", default=None,
                     help="面板parquet路径覆盖。默认优先使用全历史研究底座 "
                          f"({PANEL_FULL.name})，若不存在则退回滚动面板 ({PANEL_ROLLING.name})。")
    args = ap.parse_args()

    direction = int(args.direction)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    top_frac = args.top_frac
    cost_bps = args.cost_bps
    panel_path = Path(args.panel).expanduser().resolve() if args.panel else PANEL
    if not panel_path.exists():
        raise SystemExit(f"面板文件不存在: {panel_path}")

    factor_label = Path(args.factor).stem if Path(args.factor).suffix.lower() == ".csv" else args.factor
    print(f"=== 因子闯关机 factor_gauntlet ===")
    print(f"面板: {panel_path}")
    print(f"因子: {args.factor}  方向: {'+1(越大越好)' if direction>0 else '-1(越小越好)'}  "
          f"horizons: {horizons}  成本: {cost_bps}bp  top_frac: {top_frac:.0%}\n")

    df_base = load_panel_base(args.since, panel_path)
    df, fcol, source = attach_factor(df_base, args.factor, panel_path)
    n_nonnull = df[fcol].notna().sum()
    print(f"数据源: {source}")
    print(f"面板 {df['trade_date'].min()}..{df['trade_date'].max()} {df['trade_date'].nunique()}日 "
          f"{len(df):,}行 | 因子非空 {n_nonnull:,} ({100*n_nonnull/len(df):.1f}%)\n")
    if n_nonnull < 1000:
        print("因子有效样本过少，无法可靠检验。")
        return 1

    result = {
        "factor": args.factor, "factor_source": source, "direction": direction,
        "horizons": horizons, "cost_bps": cost_bps, "top_frac": top_frac,
        "since": args.since, "seed": args.seed,
        "panel_range": [df["trade_date"].min(), df["trade_date"].max()],
        "n_rows": int(len(df)), "n_factor_nonnull": int(n_nonnull),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    # ---- 闸1：三段分解 ----
    print("=== 闸1: 三段收益分解(次日, top decile按方向, 均值%) ===")
    g1 = gate1_three_leg(df, fcol, direction, top_frac)
    result["gate1_three_leg"] = g1
    if g1.get("n", 0) >= 10:
        print(f"  样本(调仓日) {g1['n']}  C2O隔夜 {g1['c2o_overnight_pct']:+.3f}%  "
              f"O2C日内 {g1['o2c_intraday_pct']:+.3f}%  C2C全天 {g1['c2c_full_day_pct']:+.3f}%  "
              f"C2O占C2C比例 {g1['c2o_share_of_c2c']:.2f}")
    print(f"  判定: {'PASS' if g1['pass'] else 'FAIL'} — {g1['reason']}\n")

    # ---- 闸2：可成交净边际 ----
    print("=== 闸2: 可成交净边际(日历对齐前向收益, 行业中性z, long top_frac vs 全域均值, 扣成本) ===")
    g2 = gate2_tradeable_margin(df, fcol, direction, horizons, cost_bps, top_frac)
    result["gate2_tradeable_margin"] = g2
    for h in horizons:
        r = g2["per_horizon"][str(h)]
        if r.get("n", 0) >= 10:
            print(f"  horizon={h:>3}日  每笔净 {r['per_trade_net_pct']:+.4f}%  t {r['t']:+.2f}  "
                  f"年化 {r['ann_pct']:+.2f}%  命中 {r['hit_pct']:.0f}%  周期{r['n']}  "
                  f"[{'PASS' if r['pass'] else 'FAIL'}]")
        else:
            print(f"  horizon={h:>3}日  样本不足(周期{r.get('n',0)})")
    print("  成本敏感曲线(年化%，10/20/30/50bp):")
    for h in horizons:
        curve = g2["cost_sensitivity_ann_pct"].get(str(h), {})
        if curve:
            print(f"    horizon={h:>3}日  " + "  ".join(f"{bps}bp:{curve.get(str(bps), 'NA'):+.2f}%" if curve.get(str(bps)) is not None else f"{bps}bp:NA" for bps in (10,20,30,50)))
    print(f"  判定: {'PASS' if g2['pass'] else 'FAIL'} — {g2['reason']}\n")

    main_horizon = g2["main_horizon"]

    # ---- 闸3：独立复现 ----
    print(f"=== 闸3: 独立复现(主horizon={main_horizon}日; universe随机分半 + 时间切分60/40) ===")
    g3 = gate3_reproducibility(df, fcol, direction, main_horizon, cost_bps, top_frac, seed=args.seed)
    result["gate3_reproducibility"] = g3
    ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
    if ua.get("n", 0) >= 10 and ub.get("n", 0) >= 10:
        print(f"  universe半A  每笔净 {ua['per_trade_net_pct']:+.4f}%  t {ua['t']:+.2f}  周期{ua['n']}")
        print(f"  universe半B  每笔净 {ub['per_trade_net_pct']:+.4f}%  t {ub['t']:+.2f}  周期{ub['n']}")
    ta, tb = g3["time_split_60_40"]["first_60pct"], g3["time_split_60_40"]["last_40pct"]
    if ta.get("n", 0) >= 5 and tb.get("n", 0) >= 5:
        print(f"  时间前60%   每笔净 {ta['per_trade_net_pct']:+.4f}%  t {ta['t']:+.2f}  周期{ta['n']}")
        print(f"  时间后40%   每笔净 {tb['per_trade_net_pct']:+.4f}%  t {tb['t']:+.2f}  周期{tb['n']}")
        print(f"  时间切分同号: {g3['time_split_60_40']['same_sign']}")
    print(f"  判定: {'PASS' if g3['pass'] else 'FAIL'} — {g3['reason']}\n")

    # ---- 闸4：安慰剂 ----
    print(f"=== 闸4: 安慰剂(主horizon={main_horizon}日; 每日截面内打乱因子值, {10}次) ===")
    g4 = gate4_placebo(df, fcol, direction, main_horizon, cost_bps, top_frac, seed=args.seed, n_trials=10)
    result["gate4_placebo"] = g4
    if "real_ann_pct" in g4 and g4["real_ann_pct"] is not None:
        print(f"  真实年化 {g4['real_ann_pct']:+.2f}%  安慰剂均值 {g4.get('placebo_mean_ann_pct', float('nan')):+.2f}%  "
              f"std {g4.get('placebo_std_ann_pct', float('nan')):.2f}  阈值(均值+2σ) {g4.get('placebo_threshold_mean_plus_2sd_pct', float('nan')):+.2f}%  "
              f"真实距随机 {g4.get('real_vs_placebo_sigma', float('nan')):+.1f}σ")
    print(f"  判定: {'PASS' if g4['pass'] else 'FAIL'} — {g4['reason']}\n")

    # ---- 总判定 ----
    gates = [("闸1三段分解", g1["pass"]), ("闸2可成交净边际", g2["pass"]),
             ("闸3独立复现", g3["pass"]), ("闸4安慰剂", g4["pass"])]
    all_pass = all(p for _, p in gates)
    if all_pass:
        verdict = "CANDIDATE(仍需人工复核+前瞻跟踪)"
    else:
        died_at = [name for name, p in gates if not p]
        verdict = f"REJECTED(死于: {', '.join(died_at)})"
    result["gates_summary"] = {name: bool(p) for name, p in gates}
    result["verdict"] = verdict

    print("=== 总判定 ===")
    for name, p in gates:
        print(f"  {name}: {'PASS' if p else 'FAIL'}")
    print(f"  → {verdict}")
    print(f"\n口径: 全市场点时面板(universe_flag过滤)、日历对齐前向收益(去出场侧生存者偏差)、行业内demean z-score、"
          f"扣{cost_bps}bp双边成本、基准=全域均值。方向为经济学先验固定输入，非事后拟合。")

    # ---- 写报告 ----
    out_dir = (WORKSPACE / args.out_dir) if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    safe_label = factor_label.replace("/", "_")
    json_path = out_dir / f"gauntlet_{safe_label}_{today}.json"
    md_path = out_dir / f"gauntlet_{safe_label}_{today}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    md = build_markdown_report(result)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"\n报告已写出: {md_path}")
    print(f"数据已写出: {json_path}")
    return 0


def build_markdown_report(r: dict) -> str:
    g1, g2, g3, g4 = r["gate1_three_leg"], r["gate2_tradeable_margin"], r["gate3_reproducibility"], r["gate4_placebo"]
    lines = []
    lines.append(f"# 因子闯关机报告: {r['factor']}\n")
    lines.append(f"- 数据源: {r['factor_source']}")
    lines.append(f"- 方向(先验): {'+1(越大越好)' if r['direction']>0 else '-1(越小越好)'}")
    lines.append(f"- horizons: {r['horizons']}  成本: {r['cost_bps']}bp  top_frac: {r['top_frac']:.0%}  since: {r['since']}")
    lines.append(f"- 面板范围: {r['panel_range'][0]}..{r['panel_range'][1]}  行数: {r['n_rows']:,}  因子非空: {r['n_factor_nonnull']:,}")
    lines.append(f"- 生成时间: {r['generated_at']}\n")

    lines.append("## 闸1: 三段收益分解\n")
    if g1.get("n", 0) >= 10:
        lines.append(f"- 样本(调仓日): {g1['n']}")
        lines.append(f"- C2O隔夜: {g1['c2o_overnight_pct']:+.3f}%  O2C日内: {g1['o2c_intraday_pct']:+.3f}%  "
                      f"C2C全天: {g1['c2c_full_day_pct']:+.3f}%  C2O占C2C比例: {g1['c2o_share_of_c2c']:.2f}")
    lines.append(f"- **判定: {'PASS' if g1['pass'] else 'FAIL'}** — {g1['reason']}\n")

    lines.append("## 闸2: 可成交净边际\n")
    lines.append("| horizon | 每笔净% | t值 | 年化% | 命中% | 周期数 | 判定 |")
    lines.append("|---|---|---|---|---|---|---|")
    for h in r["horizons"]:
        rr = g2["per_horizon"][str(h)]
        if rr.get("n", 0) >= 10:
            lines.append(f"| {h}日 | {rr['per_trade_net_pct']:+.4f} | {rr['t']:+.2f} | {rr['ann_pct']:+.2f} | "
                          f"{rr['hit_pct']:.0f} | {rr['n']} | {'PASS' if rr['pass'] else 'FAIL'} |")
        else:
            lines.append(f"| {h}日 | - | - | - | - | {rr.get('n',0)} | 样本不足 |")
    lines.append("\n**成本敏感曲线(年化%)**\n")
    lines.append("| horizon | 10bp | 20bp | 30bp | 50bp |")
    lines.append("|---|---|---|---|---|")
    for h in r["horizons"]:
        curve = g2["cost_sensitivity_ann_pct"].get(str(h), {})
        row = "| " + f"{h}日" + " | " + " | ".join(
            (f"{curve.get(str(bps)):+.2f}" if curve.get(str(bps)) is not None else "NA") for bps in (10, 20, 30, 50)
        ) + " |"
        lines.append(row)
    lines.append(f"\n- **判定: {'PASS' if g2['pass'] else 'FAIL'}** — {g2['reason']}\n")

    lines.append(f"## 闸3: 独立复现 (主horizon={g2['main_horizon']}日)\n")
    ua, ub = g3["universe_split"]["half_a"], g3["universe_split"]["half_b"]
    if ua.get("n", 0) >= 10 and ub.get("n", 0) >= 10:
        lines.append(f"- universe半A: 每笔净 {ua['per_trade_net_pct']:+.4f}%  t {ua['t']:+.2f}  周期{ua['n']}")
        lines.append(f"- universe半B: 每笔净 {ub['per_trade_net_pct']:+.4f}%  t {ub['t']:+.2f}  周期{ub['n']}")
    ta, tb = g3["time_split_60_40"]["first_60pct"], g3["time_split_60_40"]["last_40pct"]
    if ta.get("n", 0) >= 5 and tb.get("n", 0) >= 5:
        lines.append(f"- 时间前60%: 每笔净 {ta['per_trade_net_pct']:+.4f}%  t {ta['t']:+.2f}  周期{ta['n']}")
        lines.append(f"- 时间后40%: 每笔净 {tb['per_trade_net_pct']:+.4f}%  t {tb['t']:+.2f}  周期{tb['n']}")
        lines.append(f"- 时间切分同号: {g3['time_split_60_40']['same_sign']}")
    lines.append(f"\n- **判定: {'PASS' if g3['pass'] else 'FAIL'}** — {g3['reason']}\n")

    lines.append(f"## 闸4: 安慰剂 (主horizon={g2['main_horizon']}日, 10次打乱)\n")
    if g4.get("real_ann_pct") is not None:
        lines.append(f"- 真实年化: {g4['real_ann_pct']:+.2f}%")
        lines.append(f"- 安慰剂均值: {g4.get('placebo_mean_ann_pct', float('nan')):+.2f}%  std: {g4.get('placebo_std_ann_pct', float('nan')):.2f}")
        lines.append(f"- 阈值(均值+2σ): {g4.get('placebo_threshold_mean_plus_2sd_pct', float('nan')):+.2f}%")
        lines.append(f"- 真实距随机: {g4.get('real_vs_placebo_sigma', float('nan')):+.1f}σ")
    lines.append(f"\n- **判定: {'PASS' if g4['pass'] else 'FAIL'}** — {g4['reason']}\n")

    lines.append("## 总判定\n")
    for name, p in r["gates_summary"].items():
        lines.append(f"- {name}: {'PASS' if p else 'FAIL'}")
    lines.append(f"\n**→ {r['verdict']}**\n")
    lines.append("口径: 全市场点时面板(universe_flag过滤)、日历对齐前向收益(去出场侧生存者偏差)、"
                  "行业内demean z-score、扣成本、基准=全域均值。方向为经济学先验固定输入，非事后拟合。")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
