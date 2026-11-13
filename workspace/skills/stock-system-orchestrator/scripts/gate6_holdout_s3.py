#!/usr/bin/env python3
"""S3_intraday_shape_daily 第六道闸 —— holdout一次性复现 runner(2026-07-03)。

背景/纪律(逐字对齐 factor_factory/config/setup_registry.json 的
S3_intraday_shape_daily.gate6_holdout 字段, 由监督者在holdout数据拉取前预注册):

  - 只测2个格: (am_share=高, late_sign=弱, smooth=低) 与 (am_share=高, late_sign=弱, smooth=高)。
  - 完全相同定义: 事件域构建/12格分档规则与研究样本内 setup_gauntlet.load_setup_s3() 逐字一致，
    唯一区别是三分位/中位二分阈值改在 holdout 群体内部重新计算(而不是复用研究样本的阈值/复用
    研究样本的行)——这是"同日截面"分档的题中之义: 每个"同日截面"本来就只能用该截面自身的
    数据算分位, holdout是完全独立的800只股票+同一段日期, 同日截面在holdout群体内部重算是唯一
    合理的做法, 不是"放水"也不是"抄近路"。
  - 只跑一次, 不允许因结果不理想而调整定义/换种子/重跑。
  - 禁止查看/计算其余10格 —— 本脚本代码层面直接只构造/输出这2个格的统计, 其余10格从不计算、
    从不落盘、从不打印。
  - 判定: 逐格 med档净>0且t>=2 → 该格CANDIDATE; 否则REJECTED。p75档只作敏感性披露, 不参与判定。

本runner不重新实现成本模型/分桶逻辑/t统计, 而是直接从 setup_gauntlet.py 导入
(SlippageTable / liquidity_bucket_from_amount_ma / gap_bucket / realized_cost_dec /
cell_stats / yearly_breakdown / build_calendar_next_map / load_daily_ohlc_for_dates)，
确保与研究样本内跑法"完全相同定义"，不会因为重写产生任何细微口径漂移。

输入:
  stock_data/03-working/intraday_shape_features_holdout.parquet
    (由 build_intraday_shape_features.py --holdout 生成, 800只holdout股票)
  factor_factory/data/factors/greenfield_multifactor_panel_full.parquet (只读)
  stock_data/01-root/auction_cache/auction_open_*.parquet (只读)
  stock_data/03-working/health/slippage_calibration.json (只读)

输出(仅此二者, 无其他派生文件):
  gauntlet_reports/setup_S3_gate6_holdout_20260703.md
  gauntlet_reports/setup_S3_gate6_holdout_20260703.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from setup_gauntlet import (  # noqa: E402
    WORKSPACE,
    PANEL_FULL,
    AUCTION_DIR,
    SLIPPAGE_PATH,
    OUT_DIR,
    SlippageTable,
    liquidity_bucket_from_amount_ma,
    gap_bucket,
    realized_cost_dec,
    cell_stats,
    yearly_breakdown,
    build_calendar_next_map,
    load_daily_ohlc_for_dates,
)

HOLDOUT_FEATURES_PATH = WORKSPACE / "stock_data" / "03-working" / "intraday_shape_features_holdout.parquet"
REGISTRY_PATH = WORKSPACE / "factor_factory" / "config" / "setup_registry.json"

# ============================== 冻结契约: 只测这2格 ==============================
# 逐字来自 registry.setups[S3_intraday_shape_daily].gate6_holdout.cells_under_test:
#   ["高","弱","低"] 与 ["高","弱","高"]  ->  (am_share, late_sign, smooth)
CELLS_UNDER_TEST = [
    {"am_share": "高", "late_sign": "弱", "smooth": "低", "label": "高弱低"},
    {"am_share": "高", "late_sign": "弱", "smooth": "高", "label": "高弱高"},
]


def load_registry() -> dict:
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_holdout_event_universe() -> tuple[pd.DataFrame, dict]:
    """在holdout特征上按registry冻结定义构建事件域(逐字复刻
    setup_gauntlet.load_setup_s3 的过滤链, 唯一改动: 输入文件换成holdout特征文件;
    三分位/中位二分的分桶统计口径不变, 但是在holdout群体自身的同日截面内计算(见上方docstring))。
    返回 (完整qualified事件df[含全部12格所需列], diagnostics dict)。
    """
    print("加载holdout分时形态特征(intraday_shape_features_holdout.parquet)...")
    feat = pd.read_parquet(HOLDOUT_FEATURES_PATH)
    n_feat_total = len(feat)
    sample_before = feat["trade_date"].iloc[0]
    feat["trade_date"] = pd.to_datetime(feat["trade_date"]).dt.strftime("%Y%m%d")
    sample_after = feat["trade_date"].iloc[0]
    print(f"  trade_date格式转换自检: 转换前样例={sample_before!r}  转换后样例={sample_after!r}")
    n_unique_codes = feat["ts_code"].nunique()
    print(f"  行数={n_feat_total}  unique ts_code={n_unique_codes}(应为holdout约800只)")

    n_late_zero_raw = int((feat["late_strength"] == 0).sum())
    n_late_nan_raw = int(feat["late_strength"].isna().sum())
    print(f"  late_strength诊断(全量,筛选前): ==0行数={n_late_zero_raw}  NaN行数={n_late_nan_raw}  总行数={n_feat_total}")

    n_before_nbars = len(feat)
    feat = feat[feat["n_bars"] >= 40].copy()
    n_after_nbars = len(feat)
    print(f"  n_bars>=40过滤: {n_before_nbars} -> {n_after_nbars} (剔除{n_before_nbars - n_after_nbars})")

    print("加载面板(panel_full)...")
    cols = ["ts_code", "trade_date", "open", "close", "pct_chg", "amount", "amount_ma_20", "universe_flag"]
    pn = pd.read_parquet(PANEL_FULL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn = pn[pn["universe_flag"].fillna(0).astype(float) > 0].copy()

    n_before_universe_join = len(feat)
    ev = feat.merge(pn, on=["ts_code", "trade_date"], how="inner")
    n_after_universe_join = len(ev)
    print(f"  universe_flag>0+日历对齐(inner join panel_full): {n_before_universe_join} -> {n_after_universe_join}")

    cal = sorted(pn["trade_date"].unique())
    next_map = build_calendar_next_map(cal)
    ev["t1"] = ev["trade_date"].map(next_map)
    n_before_t1_drop = len(ev)
    ev = ev.dropna(subset=["t1"]).copy()
    pdates = set(pn["trade_date"].unique())
    ev = ev[ev["t1"].isin(pdates)].copy()
    n_after_t1_join = len(ev)
    print(f"  T+1日历映射(next_map,且T+1须在panel日历内): {n_before_t1_drop} -> {n_after_t1_join}")

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

    is_yizi = (ev["open1"] - ev["close1"]).abs() < 1e-9
    n_yizi = int(is_yizi.sum())
    ev = ev[~is_yizi].copy()
    n_after_yizi_drop = len(ev)
    print(f"  一字剔除: {n_yizi}行  剩余={n_after_yizi_drop}")

    n_before_gap = len(ev)
    ev = ev[ev["t1_gap"] <= 0.07].copy()
    n_after_gap_drop = len(ev)
    print(f"  gap<=+7%过滤: {n_before_gap} -> {n_after_gap_drop} (剔除{n_before_gap - n_after_gap_drop})")

    n_qualify_before_late_sign_exclusion = len(ev)
    n_late_zero_in_qualified = int((ev["late_strength"] == 0).sum())
    n_late_nan_in_qualified = int(ev["late_strength"].isna().sum())

    # ---- 三个决策特征(registry逐字, 阈值在holdout群体自身的同日截面内计算) ----
    ev["am_share"] = ev.groupby("trade_date")["am_vol_share"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 3, labels=["低", "中", "高"])
        if s.notna().sum() >= 3 else pd.Series([np.nan] * len(s), index=s.index))

    late_sign_raw = np.sign(ev["late_strength"])
    late_sign_raw = pd.Series(np.where(late_sign_raw == 0, np.nan, late_sign_raw), index=ev.index)
    ev["late_sign"] = late_sign_raw.map({1.0: "强", -1.0: "弱"})

    med_smooth = ev.groupby("trade_date")["path_smoothness"].transform("median")
    ev["smooth"] = np.where(ev["path_smoothness"] <= med_smooth, "低", "高")
    ev.loc[ev["path_smoothness"].isna() | med_smooth.isna(), "smooth"] = None

    ev["o2c"] = ev["close1"] / ev["open1"] - 1.0

    amt_ma20_wan = (ev["amount_ma_20"] / 10.0) if "amount_ma_20" in ev.columns else pd.Series(np.nan, index=ev.index)
    ev["liq_bkt"] = liquidity_bucket_from_amount_ma(amt_ma20_wan)
    ev["gap_bkt"] = gap_bucket(ev["t1_gap"])

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

    diagnostics = {
        "n_feat_rows_total_raw": int(n_feat_total),
        "n_unique_ts_code_raw": int(n_unique_codes),
        "n_late_strength_zero_raw": n_late_zero_raw,
        "n_late_strength_nan_raw": n_late_nan_raw,
        "n_bars_filter": {"before": int(n_before_nbars), "after": int(n_after_nbars)},
        "universe_join": {"before": int(n_before_universe_join), "after": int(n_after_universe_join)},
        "t1_calendar_join": {"before": int(n_before_t1_drop), "after": int(n_after_t1_join)},
        "t1_ao_join": {"before": int(n_before_ao_join), "after": int(n_after_ao_join)},
        "t1_ohlc_join": int(n_after_t1_ohlc_join),
        "n_yizi_dropped": n_yizi,
        "gap_le_7pct_filter": {"before": int(n_before_gap), "after": int(n_after_gap_drop)},
        "n_qualify_before_late_sign_exclusion": int(n_qualify_before_late_sign_exclusion),
        "n_late_strength_zero_in_qualified": n_late_zero_in_qualified,
        "n_late_strength_nan_in_qualified": n_late_nan_in_qualified,
        "n_dropped_at_bucket_dropna_total": int(n_dropped_at_bucket_dropna),
        "n_events_final": int(len(ev)),
    }
    return ev, diagnostics


def compute_cell_report(ev: pd.DataFrame, cell: dict, slip: SlippageTable) -> dict:
    """只为指定的1个格点计算 med+p75 两档统计 + 逐年表。不涉及任何其他格点。"""
    mask = ((ev["am_share"] == cell["am_share"]) & (ev["late_sign"] == cell["late_sign"])
            & (ev["smooth"] == cell["smooth"]))
    sub = ev[mask].copy()

    out = {"cell": {"am_share": cell["am_share"], "late_sign": cell["late_sign"], "smooth": cell["smooth"]},
           "label": cell["label"], "n": int(len(sub))}

    for mode in ("med", "p75"):
        cost_dec = realized_cost_dec(sub["liq_bkt"], sub["gap_bkt"], slip, mode)
        stats = cell_stats(sub["o2c"].to_numpy(), cost_dec.to_numpy())
        out[f"stats_{mode}"] = stats
        if mode == "med":
            sub_for_year = sub.copy()
            sub_for_year["_cost_dec_med"] = cost_dec.to_numpy()
            out["yearly_med"] = yearly_breakdown(sub_for_year, "trade_date", "o2c", "_cost_dec_med")

    # ---- 生产一致性披露(2026-07-03监督者补充指令,不参与判定) ----
    # universe 混有.BJ(北交所)股票，而生产系统排除BJ。判定仍严格按 gate6_holdout.success_criteria
    # 冻结标准(全样本口径,上面的 stats_med/stats_p75)执行；这里只【附加披露】剔除.BJ后的同口径
    # n/净/t，供生产一致性参考，不改变、不参与、不影响 judge_cell() 的判定。
    sub_ex_bj = sub[~sub["ts_code"].str.endswith(".BJ")].copy()
    ex_bj_block = {"n": int(len(sub_ex_bj)), "n_bj_excluded": int(len(sub) - len(sub_ex_bj))}
    for mode in ("med", "p75"):
        cost_dec_ex_bj = realized_cost_dec(sub_ex_bj["liq_bkt"], sub_ex_bj["gap_bkt"], slip, mode)
        ex_bj_block[f"stats_{mode}"] = cell_stats(sub_ex_bj["o2c"].to_numpy(), cost_dec_ex_bj.to_numpy())
    out["disclosure_ex_bj"] = ex_bj_block

    return out


def judge_cell(stats_med: dict) -> str:
    """gate6_holdout.success_criteria逐字: med档净>0且t>=2 -> CANDIDATE; 否则REJECTED。"""
    n = stats_med.get("n", 0)
    net = stats_med.get("net_pct")
    t = stats_med.get("t_net")
    if n == 0 or net is None or t is None:
        return "REJECTED(样本为空或统计无法计算)"
    if net > 0 and t >= 2:
        return "CANDIDATE"
    return "REJECTED"


def build_markdown(result: dict) -> str:
    lines = ["# S3_intraday_shape_daily 第六道闸: holdout一次性复现报告\n"]
    lines.append(f"- 生成时间: {result['generated_at']}")
    lines.append(f"- 契约: factor_factory/config/setup_registry.json "
                  f"S3_intraday_shape_daily.gate6_holdout (frozen_at={result['contract_frozen_at']})")
    lines.append("- 纪律: 只测2个格(高|弱|低 与 高|弱|高)，完全相同定义，holdout群体内算同日截面三分位/"
                  "中位，只跑一次，禁止查看/计算其余10格，禁止任何调整。")
    lines.append(f"\n## 事件域(holdout, 与研究样本S3逐字相同过滤链)\n")
    diag = result["diagnostics"]
    lines.append(f"- 特征文件总行数(holdout): {diag['n_feat_rows_total_raw']}  unique ts_code: {diag['n_unique_ts_code_raw']}")
    lines.append(f"- n_bars>=40过滤: {diag['n_bars_filter']['before']} -> {diag['n_bars_filter']['after']}")
    lines.append(f"- universe_flag>0+日历对齐: {diag['universe_join']['before']} -> {diag['universe_join']['after']}")
    lines.append(f"- T+1日历映射: {diag['t1_calendar_join']['before']} -> {diag['t1_calendar_join']['after']}")
    lines.append(f"- T+1竞价开盘join: {diag['t1_ao_join']['before']} -> {diag['t1_ao_join']['after']}")
    lines.append(f"- T+1面板OHLC join+正值过滤: -> {diag['t1_ohlc_join']}")
    lines.append(f"- 一字剔除: {diag['n_yizi_dropped']}行")
    lines.append(f"- gap<=+7%过滤: {diag['gap_le_7pct_filter']['before']} -> {diag['gap_le_7pct_filter']['after']}")
    lines.append(f"- late_sign=0/NaN等剔除(其余过滤后、三特征分桶dropna前后): "
                  f"合格事件(late_sign剔除前)={diag['n_qualify_before_late_sign_exclusion']}  "
                  f"late=0: {diag['n_late_strength_zero_in_qualified']}  late=NaN: {diag['n_late_strength_nan_in_qualified']}  "
                  f"分桶dropna共剔除: {diag['n_dropped_at_bucket_dropna_total']}")
    lines.append(f"- 最终合格事件总数(12格潜在全集,仅用于对账,本报告只输出2格): {diag['n_events_final']}\n")

    lines.append("## 两格判定结果(唯一输出的格点)\n")
    lines.append("| 格点 | n | 毛O2C%(med) | t毛 | 胜率% | 净%(med) | t净(med) | 净%(p75) | t净(p75) | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cr in result["cells"]:
        sm = cr["stats_med"]
        sp = cr["stats_p75"]
        lines.append(f"| {cr['label']} | {cr['n']} | {sm.get('gross_o2c_pct','NA')} | {sm.get('t_gross','NA')} | "
                      f"{sm.get('hit_pct','NA')} | {sm.get('net_pct','NA')} | {sm.get('t_net','NA')} | "
                      f"{sp.get('net_pct','NA')} | {sp.get('t_net','NA')} | **{cr['judgment']}** |")

    lines.append("\n## 附加披露: 剔除.BJ(北交所)股票后同口径数字(2026-07-03监督者补充指令,不参与判定)\n")
    lines.append("说明: universe内混有.BJ股票，而生产系统排除BJ。判定仍严格按 gate6_holdout.success_criteria "
                  "冻结标准(全样本口径，上表)执行；本表仅作生产一致性披露，不改变、不参与总判定。\n")
    lines.append("| 格点 | 全样本n | 剔除.BJ后n | .BJ剔除数 | 净%(med,ex-BJ) | t净(med,ex-BJ) | 净%(p75,ex-BJ) | t净(p75,ex-BJ) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cr in result["cells"]:
        db = cr["disclosure_ex_bj"]
        dsm = db["stats_med"]
        dsp = db["stats_p75"]
        lines.append(f"| {cr['label']} | {cr['n']} | {db['n']} | {db['n_bj_excluded']} | "
                      f"{dsm.get('net_pct','NA')} | {dsm.get('t_net','NA')} | "
                      f"{dsp.get('net_pct','NA')} | {dsp.get('t_net','NA')} |")

    lines.append("\n## 逐年表(med档)\n")
    for cr in result["cells"]:
        lines.append(f"### 格点 {cr['label']}\n")
        yb = cr["yearly_med"]
        lines.append("| 年 | n | 净收益之和% | 净收益均值% | 贡献占比 |")
        lines.append("|---|---|---|---|---|")
        for yr in yb["years"]:
            lines.append(f"| {yr['year']} | {yr['n']} | {yr['net_sum_pct']:.4f} | "
                          f"{yr.get('net_mean_pct', float('nan')):.4f} | {yr.get('contribution_share','NA')} |")
        lines.append(f"\n年n之和={sum(y['n'] for y in yb['years'])}  格n={cr['n']}\n")

    lines.append("## 与研究样本内参照对比\n")
    lines.append("| 格点 | 研究样本内净%(med) | 研究样本内t | holdout净%(med) | holdout t | holdout净%(p75,敏感性) | holdout t(p75) |")
    lines.append("|---|---|---|---|---|---|---|")
    for cr in result["cells"]:
        ref = result["insample_reference"].get(cr["label"], {})
        sm = cr["stats_med"]
        sp = cr["stats_p75"]
        lines.append(f"| {cr['label']} | {ref.get('net_pct','NA')} | {ref.get('t','NA')} | "
                      f"{sm.get('net_pct','NA')} | {sm.get('t_net','NA')} | {sp.get('net_pct','NA')} | {sp.get('t_net','NA')} |")

    lines.append("\n## n量级对账(holdout vs 研究样本对应格)\n")
    for cr in result["cells"]:
        lines.append(f"- {cr['label']}: holdout n={cr['n']}  研究样本参照见对照表(数量级应相近，"
                      f"holdout与研究样本均约800只股票x同一段日历,理论上同量级)")

    lines.append("\n## 总判定\n")
    for cr in result["cells"]:
        lines.append(f"- {cr['label']}: **{cr['judgment']}**")

    lines.append("\n---\n本报告只输出registry预注册的2个格点,其余10格未计算、未落盘、未打印，"
                  "符合gate6_holdout纪律(禁止多重比较)。只跑一次，无论结果如何均不再重跑/调整。\n")
    return "\n".join(lines) + "\n"


def main() -> int:
    registry = load_registry()
    s3_def = None
    for s in registry["setups"]:
        if s["id"] == "S3_intraday_shape_daily":
            s3_def = s
            break
    if s3_def is None:
        raise SystemExit("registry中找不到S3_intraday_shape_daily")
    gate6 = s3_def["gate6_holdout"]

    # 硬校验: 代码里冻结的CELLS_UNDER_TEST必须与registry里的cells_under_test逐字一致，
    # 防止代码被意外改动导致跑出registry未授权的格点。
    registry_cells = gate6["cells_under_test"]
    code_cells = [[c["am_share"], c["late_sign"], c["smooth"]] for c in CELLS_UNDER_TEST]
    if registry_cells != code_cells:
        raise SystemExit(f"[FATAL] 代码冻结的格点与registry.gate6_holdout.cells_under_test不一致！"
                          f"registry={registry_cells} code={code_cells} —— 拒绝执行。")

    if not HOLDOUT_FEATURES_PATH.exists():
        raise SystemExit(f"[FATAL] holdout特征文件不存在: {HOLDOUT_FEATURES_PATH}"
                          f"(须先跑 build_intraday_shape_features.py --holdout)")

    print("=== S3 第六道闸: holdout一次性复现 ===")
    print(f"契约: {REGISTRY_PATH}  gate6_holdout.frozen_at={gate6['frozen_at']}  frozen_by={gate6['frozen_by']}")
    print(f"只测2格: {[c['label'] for c in CELLS_UNDER_TEST]}\n")

    slip = SlippageTable(SLIPPAGE_PATH)
    ev, diagnostics = build_holdout_event_universe()
    print(f"\n事件域构建完成: 最终合格事件={len(ev):,}  覆盖T日 {ev['trade_date'].nunique()}"
          f"  唯一ts_code={ev['ts_code'].nunique()}")

    cells_out = []
    for cell in CELLS_UNDER_TEST:
        print(f"\n--- 计算格点 {cell['label']} (am_share={cell['am_share']} late_sign={cell['late_sign']} smooth={cell['smooth']}) ---")
        cr = compute_cell_report(ev, cell, slip)
        cr["judgment"] = judge_cell(cr["stats_med"])
        cells_out.append(cr)
        sm, sp = cr["stats_med"], cr["stats_p75"]
        print(f"  n={cr['n']}  med: 净{sm.get('net_pct','NA')}% t{sm.get('t_net','NA')} 胜率{sm.get('hit_pct','NA')}%  "
              f"p75(敏感性): 净{sp.get('net_pct','NA')}% t{sp.get('t_net','NA')}")
        print(f"  → 判定: {cr['judgment']}")

    result = {
        "setup_id": "S3_intraday_shape_daily",
        "gate": "gate6_holdout",
        "contract_frozen_at": gate6["frozen_at"],
        "contract_frozen_by": gate6["frozen_by"],
        "cells_tested": [c["label"] for c in CELLS_UNDER_TEST],
        "insample_reference": gate6["insample_reference"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "diagnostics": diagnostics,
        "cells": cells_out,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = "20260703"
    json_path = OUT_DIR / f"setup_S3_gate6_holdout_{today}.json"
    md_path = OUT_DIR / f"setup_S3_gate6_holdout_{today}.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_markdown(result))

    print(f"\n=== 完成 ===")
    print(f"报告: {md_path}")
    print(f"数据: {json_path}")
    for cr in cells_out:
        print(f"  {cr['label']}: {cr['judgment']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
