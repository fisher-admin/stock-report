#!/usr/bin/env python3
"""T1 Alpha191 真·样本外（walk-forward）信号检验。

诚实化（2026-06）：旧 run_t1_walkforward.py 把全样本拟合的固定 portfolio 套到每个 test 窗（样本内自评）；
本脚本逐窗只用「训练窗」拟合方向(sign IC)+权重(|ICIR|)，再在「测试窗」用合成分算截面 rank-IC 与 top20 收益，
训练/测试严格不重叠，得到的才是可对外的样本外指标。前瞻收益用清洗后的标签：
  - label_next_close_return：T 日信号 → 次日收盘收益（研究口径）。
  - label_exec_return：T+1 开盘买 → T+2 开盘卖（可执行口径），扣 round-trip 成本后看净超额。

用法：python3 run_t1_oos_ic.py [--train 60] [--test 20] [--cost-bps 20]
输出：outputs/t1_alpha191/oos_ic_report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"
OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "t1_alpha191"
PANEL = FACTORS_DIR / "gtja_alpha191_panel.parquet"
LABELS = FACTORS_DIR / "t1_labels.parquet"

TARGET_RESEARCH = "label_next_close_return"
TARGET_EXEC = "label_exec_return"


def load() -> tuple[pd.DataFrame, list[str]]:
    panel = pd.read_parquet(PANEL)
    labels = pd.read_parquet(LABELS)
    df = panel.merge(labels, on=["ts_code", "trade_date"], how="inner")
    factors = sorted(c for c in panel.columns if c.startswith("alpha"))
    df = df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    return df, factors


def _daily_rank_ic(df: pd.DataFrame, col: str, target: str) -> pd.Series:
    """逐交易日的截面 Spearman rank-IC（factor 值 vs 目标收益），返回按日序列。"""
    out = {}
    for date, g in df.groupby("trade_date", sort=True):
        sub = g[[col, target]].dropna()
        if len(sub) < 30 or sub[col].nunique() < 5:
            continue
        out[date] = sub[col].corr(sub[target], method="spearman")
    return pd.Series(out, dtype=float).dropna()


def fit_signs_weights(train_df: pd.DataFrame, factors: list[str], target: str) -> tuple[dict, dict]:
    """只用训练窗拟合：sign = sign(平均 rank-IC)，weight = |ICIR| = |mean(IC)/std(IC)|（退 |mean IC|）。"""
    signs, weights = {}, {}
    for f in factors:
        ic = _daily_rank_ic(train_df, f, target)
        if len(ic) < 5:
            signs[f], weights[f] = 1.0, 0.0
            continue
        mean_ic = float(ic.mean())
        std_ic = float(ic.std())
        signs[f] = 1.0 if mean_ic >= 0 else -1.0
        icir = mean_ic / std_ic if std_ic and np.isfinite(std_ic) else 0.0
        weights[f] = abs(icir) if np.isfinite(icir) and icir != 0 else abs(mean_ic)
    total = sum(weights.values())
    if total > 0:
        weights = {f: w / total for f, w in weights.items()}
    return signs, weights


def composite_score(day_df: pd.DataFrame, factors: list[str], signs: dict, weights: dict) -> pd.Series:
    """当日截面：每因子 z-score 标准化 × sign 方向对齐 × 权重求和（与 build_t1_portfolio 一致）。"""
    score = pd.Series(0.0, index=day_df.index)
    for f in factors:
        w = weights.get(f, 0.0)
        if w <= 0 or f not in day_df.columns:
            continue
        col = day_df[f].astype(float)
        sd = col.std()
        if not np.isfinite(sd) or sd == 0:
            continue
        z = ((col - col.mean()) / sd).fillna(0.0)
        score = score + z * signs.get(f, 1.0) * w
    return score


def evaluate_window(df: pd.DataFrame, test_dates: list[str], factors: list[str],
                    signs: dict, weights: dict, target: str, top_n: int, cost: float) -> list[dict]:
    rows = []
    for date in test_dates:
        day = df[df["trade_date"] == date].copy()
        if len(day) < 30:
            continue
        day["score"] = composite_score(day, factors, signs, weights)
        sub = day[["score", target]].dropna()
        if len(sub) < 30 or sub["score"].nunique() < 5:
            continue
        ic = float(sub["score"].corr(sub[target], method="spearman"))
        top = day.nlargest(top_n, "score")
        top_ret = float(top[target].dropna().mean()) if top[target].notna().any() else np.nan
        uni_ret = float(day[target].dropna().mean()) if day[target].notna().any() else np.nan
        rows.append({
            "trade_date": str(date),
            "oos_rank_ic": ic,
            "top20_ret": top_ret,
            "universe_ret": uni_ret,
            "top20_excess": (top_ret - uni_ret) if np.isfinite(top_ret) and np.isfinite(uni_ret) else np.nan,
            "top20_net_excess": (top_ret - uni_ret - cost) if np.isfinite(top_ret) and np.isfinite(uni_ret) else np.nan,
        })
    return rows


def summarize(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "n_days": 0}
    df = pd.DataFrame(rows)
    ic = df["oos_rank_ic"].dropna()
    net = df["top20_net_excess"].dropna()
    exc = df["top20_excess"].dropna()
    return {
        "label": label,
        "n_days": int(len(df)),
        "oos_rank_ic_mean": round(float(ic.mean()), 5) if len(ic) else None,
        "oos_rank_ic_std": round(float(ic.std()), 5) if len(ic) else None,
        "oos_icir": round(float(ic.mean() / ic.std()), 4) if len(ic) > 1 and ic.std() else None,
        "ic_positive_rate": round(float((ic > 0).mean()), 4) if len(ic) else None,
        "top20_excess_mean_daily": round(float(exc.mean()), 6) if len(exc) else None,
        "top20_net_excess_mean_daily": round(float(net.mean()), 6) if len(net) else None,
        "top20_net_excess_positive_rate": round(float((net > 0).mean()), 4) if len(net) else None,
    }


def walk_forward(df: pd.DataFrame, factors: list[str], target: str,
                 train: int, test: int, top_n: int, cost: float) -> list[dict]:
    dates = sorted(df["trade_date"].unique())
    rows: list[dict] = []
    for i in range(train, len(dates) - test + 1, test):
        train_dates = dates[i - train:i]
        test_dates = dates[i:i + test]
        train_df = df[df["trade_date"].isin(train_dates)]
        signs, weights = fit_signs_weights(train_df, factors, target)
        rows.extend(evaluate_window(df, test_dates, factors, signs, weights, target, top_n, cost))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--test", type=int, default=20)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=20.0, help="round-trip 成本(基点)，默认 20bps=0.20%")
    args = ap.parse_args()
    cost = args.cost_bps / 10000.0

    df, factors = load()
    n_dates = df["trade_date"].nunique()
    print(f"面板: {len(df)} 行, {df['ts_code'].nunique()} 股, {n_dates} 交易日, {len(factors)} 因子")
    print(f"walk-forward: train={args.train} test={args.test} step={args.test} top_n={args.top_n} cost={cost:.4f}")

    report = {
        "generated_for": "T1 Alpha191 真·样本外(walk-forward)信号检验",
        "panel_rows": int(len(df)),
        "stock_count": int(df["ts_code"].nunique()),
        "trade_days": int(n_dates),
        "factor_count": len(factors),
        "config": {"train": args.train, "test": args.test, "top_n": args.top_n, "cost_bps": args.cost_bps},
        "results": {},
    }
    for label, target in (("research_next_close", TARGET_RESEARCH), ("executable_t1open_t2open", TARGET_EXEC)):
        if target not in df.columns:
            continue
        rows = walk_forward(df, factors, target, args.train, args.test, args.top_n, cost)
        summary = summarize(rows, label)
        report["results"][label] = summary
        print(f"\n[{label}] {json.dumps(summary, ensure_ascii=False)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "oos_ic_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
