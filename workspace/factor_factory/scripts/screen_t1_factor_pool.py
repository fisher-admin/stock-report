#!/usr/bin/env python3
"""T1 扩因子池海选：单因子 OOS ICIR 双门槛 + 相关矩阵控共线 → 低相关子集 walk-forward 终评。

用户授权「投入扩真实因子池」后用此脚本回答：把真实可计算的 alpha001-101（WorldQuant-101，
alpha102-191 为重复变体已排除）全量纳入、按 |ICIR| 海选 + 控共线后，能否找到样本外可交易(ICIR>0.3、
扣费净超额>0)的低相关因子组合。流程：
  ① 单因子全样本日 rank-IC → mean_ic/icir/sign_stability；
  ② 贪心选：按 |icir| 降序，仅保留 |icir|≥icir_thr 且与已选因子 |corr|<corr_thr 的因子（控共线，上限 max_k）；
  ③ 选中子集做真·walk-forward（训练窗拟合方向/权重 → 测试窗合成分测 OOS rank-IC + top20 扣费净超额）。

用法：python3 screen_t1_factor_pool.py [--panel research_panel_101.parquet] [--icir 0.3] [--corr 0.6] [--max-k 15]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"
OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "t1_alpha191"
TARGET = "label_exec_return"  # 可执行口径


def daily_ic(df: pd.DataFrame, col: str) -> pd.Series:
    out = {}
    for d, g in df.groupby("trade_date", sort=True):
        s = g[[col, TARGET]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) < 30 or s[col].nunique() < 5:
            continue
        out[d] = s[col].corr(s[TARGET], method="spearman")
    return pd.Series(out, dtype=float).dropna()


def per_factor_stats(df: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    rows = []
    for f in factors:
        ic = daily_ic(df, f)
        if len(ic) < 10:
            rows.append((f, len(ic), np.nan, np.nan, np.nan))
            continue
        m, sd = float(ic.mean()), float(ic.std())
        icir = m / sd if sd else np.nan
        stab = float((np.sign(ic) == np.sign(m)).mean())
        rows.append((f, len(ic), m, icir, stab))
    res = pd.DataFrame(rows, columns=["factor", "n_days", "mean_ic", "icir", "sign_stability"])
    return res.reindex(res["icir"].abs().sort_values(ascending=False).index)


def select_low_corr(df: pd.DataFrame, stats: pd.DataFrame, icir_thr: float, corr_thr: float, max_k: int) -> list[str]:
    """贪心：按 |icir| 降序，保留 |icir|≥阈值 且 与已选 |corr|<阈值 的因子。corr 用因子值（抽样行）算。"""
    cand = stats[stats["icir"].abs() >= icir_thr]["factor"].tolist()
    if not cand:
        return []
    sample = df[cand].replace([np.inf, -np.inf], np.nan)
    if len(sample) > 50000:
        sample = sample.sample(50000, random_state=0)
    corr = sample.corr(method="spearman").abs()
    selected: list[str] = []
    for f in cand:
        if len(selected) >= max_k:
            break
        if all(corr.loc[f, g] < corr_thr for g in selected if g in corr.columns):
            selected.append(f)
    return selected


def fit_signs_weights(train_df: pd.DataFrame, factors: list[str]) -> tuple[dict, dict]:
    signs, weights = {}, {}
    for f in factors:
        ic = daily_ic(train_df, f)
        if len(ic) < 5:
            signs[f], weights[f] = 1.0, 0.0
            continue
        m, sd = float(ic.mean()), float(ic.std())
        signs[f] = 1.0 if m >= 0 else -1.0
        icir = m / sd if sd else 0.0
        weights[f] = abs(icir) if np.isfinite(icir) and icir != 0 else abs(m)
    tot = sum(weights.values())
    if tot > 0:
        weights = {f: w / tot for f, w in weights.items()}
    return signs, weights


def composite(day: pd.DataFrame, factors: list[str], signs: dict, weights: dict) -> pd.Series:
    score = pd.Series(0.0, index=day.index)
    for f in factors:
        w = weights.get(f, 0.0)
        if w <= 0 or f not in day.columns:
            continue
        col = day[f].astype(float).replace([np.inf, -np.inf], np.nan)
        sd = col.std()
        if not np.isfinite(sd) or sd == 0:
            continue
        z = ((col - col.mean()) / sd).fillna(0.0)
        score = score + z * signs.get(f, 1.0) * w
    return score


def walk_forward(df: pd.DataFrame, factors: list[str], train=60, test=20, top_n=20, cost=0.002) -> dict:
    dates = sorted(df["trade_date"].unique())
    ics, nets = [], []
    for i in range(train, len(dates) - test + 1, test):
        tr = df[df["trade_date"].isin(dates[i - train:i])]
        s, w = fit_signs_weights(tr, factors)
        for d in dates[i:i + test]:
            day = df[df["trade_date"] == d].copy()
            if len(day) < 30:
                continue
            day["score"] = composite(day, factors, s, w)
            sub = day[["score", TARGET]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(sub) < 30 or sub["score"].nunique() < 5:
                continue
            ics.append(float(sub["score"].corr(sub[TARGET], method="spearman")))
            tp = day.nlargest(top_n, "score")[TARGET].replace([np.inf, -np.inf], np.nan).dropna()
            un = day[TARGET].replace([np.inf, -np.inf], np.nan).dropna()
            if len(tp) and len(un):
                ne = float(tp.mean() - un.mean() - cost)
                if np.isfinite(ne):
                    nets.append(ne)
    ics = [x for x in ics if np.isfinite(x)]
    ic_mean = float(np.mean(ics)) if ics else None
    ic_std = float(np.std(ics, ddof=1)) if len(ics) > 1 else None
    icir = (ic_mean / ic_std) if (ic_std and ic_std > 0) else None
    net_mean = float(np.mean(nets)) if nets else None
    net_pos = float(np.mean([x > 0 for x in nets])) if nets else None
    return {
        "oos_days": len(ics),
        "oos_rank_ic_mean": round(ic_mean, 5) if ic_mean is not None else None,
        "oos_icir": round(icir, 4) if icir is not None else None,
        "top20_net_excess_mean_daily": round(net_mean, 6) if net_mean is not None else None,
        "top20_net_excess_positive_rate": round(net_pos, 4) if net_pos is not None else None,
        "tradeable": bool(icir is not None and abs(icir) >= 0.30 and net_mean is not None and net_mean > 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=str(FACTORS_DIR / "research_panel_101.parquet"))
    ap.add_argument("--icir", type=float, default=0.30)
    ap.add_argument("--corr", type=float, default=0.6)
    ap.add_argument("--max-k", type=int, default=15)
    args = ap.parse_args()

    panel = pd.read_parquet(args.panel)
    labels = pd.read_parquet(FACTORS_DIR / "t1_labels.parquet")
    df = panel.merge(labels, on=["ts_code", "trade_date"], how="inner").sort_values(["trade_date", "ts_code"])
    factors = sorted(c for c in panel.columns if c.startswith("alpha"))
    print(f"研究面板: {len(df)} 行, {df['ts_code'].nunique()} 股, {df['trade_date'].nunique()} 日, {len(factors)} 因子")

    stats = per_factor_stats(df, factors)
    n_strong = int((stats["icir"].abs() >= args.icir).sum())
    print(f"\n单因子 |ICIR|≥{args.icir}: {n_strong}/{len(factors)} 个；前10:")
    print(stats.head(10).to_string(index=False))

    selected = select_low_corr(df, stats, args.icir, args.corr, args.max_k)
    print(f"\n控共线后选中 {len(selected)} 因子(|corr|<{args.corr}): {selected}")

    full_all = walk_forward(df, factors)
    sub = walk_forward(df, selected) if selected else {"note": "无因子达标"}
    print(f"\n[全 {len(factors)} 因子] OOS: {json.dumps(full_all, ensure_ascii=False)}")
    print(f"[控共线精选 {len(selected)} 因子] OOS: {json.dumps(sub, ensure_ascii=False)}")

    report = {
        "panel": args.panel,
        "factor_count": len(factors),
        "config": {"icir_thr": args.icir, "corr_thr": args.corr, "max_k": args.max_k},
        "n_factors_icir_pass": n_strong,
        "per_factor_top15": stats.head(15).round(4).to_dict("records"),
        "selected_low_corr": selected,
        "oos_full_pool": full_all,
        "oos_selected_subset": sub,
        "verdict": (
            "扩池后仍无可交易子集（ICIR<0.3 或净超额≤0）"
            if not (isinstance(sub, dict) and sub.get("tradeable"))
            else "扩池后精选子集样本外达标，可考虑投产"
        ),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "factor_pool_screen.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论: {report['verdict']}\n已保存 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
