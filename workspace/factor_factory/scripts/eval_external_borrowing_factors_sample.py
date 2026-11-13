#!/usr/bin/env python3
"""Sample walk-forward for the 2026-08-25 external-borrowing factor batch.

Does NOT rebuild the full greenfield panel.  Loads a 400-stock sample from the
existing panel, computes the three new factors, then reports:
  - overall rank IC / ICIR / t-stat (C2C and O2C)
  - walk-forward folds (train 60 / val 20 / test 20 / step 20 — fits the 133-day panel)
  - quintile Q5-Q1 spread
  - cross-sectional Spearman vs a few existing factors (orthogonality)

Output:
  outputs/greenfield_multifactor/external_borrowing_sample_wf_20260825.json
  outputs/greenfield_multifactor/external_borrowing_sample_wf_20260825.md
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
sys.path.insert(0, str(ROOT / "scripts"))

from greenfield_multifactor_lib import (  # noqa: E402
    add_greenfield_factors,
    build_walkforward_slices,
    spearman_corr,
)

PANEL_PATH = ROOT / "data/factors/greenfield_multifactor_panel.parquet"
OUT_DIR = ROOT / "outputs/greenfield_multifactor"
NEW_FACTORS = ["g_pattern_dtw_sim"]
ORTHO_PEERS = [
    "g_trend_quality_20",
    "g_long_kdj_oversold",
    "g_long_c2c_calm_rebound",
    "f_volume_ratio_5",
]
LABELS = {
    "c2c": "next_return_close_to_close_1d",
    "o2c": "next_return_open_to_close_1d",
}
SAMPLE_N = 1000
TZ = timezone(timedelta(hours=8))


def _tstat(xs: list[float]) -> float:
    a = np.asarray(xs, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) < 3:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    if se == 0:
        return float("nan")
    return float(a.mean() / se)


def daily_rank_ic(df: pd.DataFrame, factor: str, label: str) -> list[float]:
    ics: list[float] = []
    for _, day in df.groupby("trade_date", sort=True):
        sub = day[[factor, label]].dropna()
        if len(sub) < 30:
            continue
        ic = spearman_corr(sub[factor], sub[label])
        if pd.notna(ic):
            ics.append(float(ic))
    return ics


def quintile_spread(df: pd.DataFrame, factor: str, label: str) -> dict:
    spreads: list[float] = []
    for _, day in df.groupby("trade_date", sort=True):
        sub = day[[factor, label]].dropna()
        if len(sub) < 50:
            continue
        try:
            q = pd.qcut(sub[factor].rank(method="first"), 5, labels=False)
        except ValueError:
            continue
        means = sub.groupby(q)[label].mean()
        if 0 in means.index and 4 in means.index:
            spreads.append(float(means.loc[4] - means.loc[0]))
    arr = np.asarray(spreads, dtype=float)
    return {
        "days": int(len(arr)),
        "mean_q5_minus_q1": float(arr.mean()) if len(arr) else float("nan"),
        "tstat": _tstat(spreads),
    }


def ortho_rho(df: pd.DataFrame, factor: str, peers: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for peer in peers:
        if peer not in df.columns:
            out[peer] = float("nan")
            continue
        ics: list[float] = []
        for _, day in df.groupby("trade_date", sort=True):
            sub = day[[factor, peer]].dropna()
            if len(sub) < 30:
                continue
            ic = spearman_corr(sub[factor], sub[peer])
            if pd.notna(ic):
                ics.append(float(ic))
        out[peer] = float(np.mean(ics)) if ics else float("nan")
    return out


def ic_summary(ics: list[float]) -> dict:
    arr = np.asarray(ics, dtype=float)
    if len(arr) == 0:
        return {"days": 0, "mean": float("nan"), "std": float("nan"), "icir": float("nan"), "tstat": float("nan")}
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else float("nan")
    icir = mean / std * np.sqrt(252) if std and std == std and std != 0 else float("nan")
    return {
        "days": int(len(arr)),
        "mean": mean,
        "std": std,
        "icir": float(icir) if icir == icir else float("nan"),
        "tstat": _tstat(ics),
    }


def main() -> int:
    t0 = datetime.now(TZ)
    schema_cols = pd.read_parquet(PANEL_PATH, columns=["ts_code", "trade_date"])
    counts = schema_cols.groupby("ts_code").size().sort_values(ascending=False)
    sample_codes = counts.head(SAMPLE_N).index.tolist()
    print(f"sample {len(sample_codes)} stocks from {counts.shape[0]} (longest history)")

    # Load full rows for the sample (panel is 335MB; filter after read of needed cols).
    needed = list(
        dict.fromkeys(
            [
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "ret_1d",
                "f_volume_ratio_5",
                "f_intraday_reversal",
                "f_close_pos_20",
                "kdj_j",
                "next_return_close_to_close_1d",
                "next_return_open_to_close_1d",
                *ORTHO_PEERS,
            ]
        )
    )
    # add_greenfield_factors needs many primitive columns already on the panel;
    # load all columns for the sample via row filter after a column-subset if possible.
    # Safest: read whole panel columns that exist, then filter stocks.
    import pyarrow.parquet as pq

    all_cols = pq.read_schema(PANEL_PATH).names
    load_cols = [c for c in all_cols if c in set(needed) | set(all_cols)]
    # Actually load all columns — 400 stocks * 133 days is tiny vs 304k rows.
    # Filter by ts_code using filters if pyarrow supports it.
    try:
        df = pd.read_parquet(PANEL_PATH, filters=[("ts_code", "in", sample_codes)])
    except Exception:
        df = pd.read_parquet(PANEL_PATH)
        df = df[df["ts_code"].isin(sample_codes)].copy()
    print(f"loaded sample rows={len(df)} dates={df['trade_date'].nunique()} stocks={df['ts_code'].nunique()}")

    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    df = add_greenfield_factors(df)

    missing = [c for c in NEW_FACTORS if c not in df.columns]
    if missing:
        raise SystemExit(f"new factors missing after compute: {missing}")

    dates = sorted(df["trade_date"].astype(str).unique().tolist())
    folds = build_walkforward_slices(
        dates, train_days=60, validation_days=20, test_days=20, step_days=20, min_train_days=40
    )
    print(f"dates={len(dates)} folds={len(folds)}")

    report: dict = {
        "generated_at": t0.isoformat(timespec="seconds"),
        "panel": str(PANEL_PATH),
        "sample_n_stocks": int(df["ts_code"].nunique()),
        "sample_n_rows": int(len(df)),
        "date_min": dates[0],
        "date_max": dates[-1],
        "n_dates": len(dates),
        "n_folds": len(folds),
        "walkforward": {
            "train_days": 60,
            "validation_days": 20,
            "test_days": 20,
            "step_days": 20,
            "min_train_days": 40,
            "note": "shortened vs production 252/63/63 because current panel only has 133 dates",
        },
        "factors": {},
    }

    for factor in NEW_FACTORS:
        cov = float(df[factor].notna().mean())
        entry: dict = {"coverage": cov, "labels": {}, "ortho_mean_abs_rho": {}, "walkforward": []}
        entry["ortho_mean_abs_rho"] = ortho_rho(df, factor, ORTHO_PEERS)
        for lab_name, lab_col in LABELS.items():
            ics = daily_rank_ic(df, factor, lab_col)
            entry["labels"][lab_name] = {
                "overall": ic_summary(ics),
                "quintile": quintile_spread(df, factor, lab_col),
            }
        for i, fold in enumerate(folds, 1):
            test = df[df["trade_date"].astype(str).isin(fold["test_dates"])]
            fold_row = {
                "fold": i,
                "test_start": fold["test_dates"][0],
                "test_end": fold["test_dates"][-1],
                "n_test_dates": len(fold["test_dates"]),
            }
            for lab_name, lab_col in LABELS.items():
                ics = daily_rank_ic(test, factor, lab_col)
                fold_row[lab_name] = ic_summary(ics)
            entry["walkforward"].append(fold_row)
        report["factors"][factor] = entry
        print(factor, "cov", f"{cov:.2%}", "c2c IC", entry["labels"]["c2c"]["overall"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "external_borrowing_sample_wf_20260825.json"
    md_path = OUT_DIR / "external_borrowing_sample_wf_20260825.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")
    elapsed = (datetime.now(TZ) - t0).total_seconds()
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(f"elapsed {elapsed:.1f}s")
    return 0


def render_md(report: dict) -> str:
    lines = [
        "# 外部借鉴三因子 · 抽样 walk-forward（2026-08-25）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 样本：{report['sample_n_stocks']} 只股票 × {report['n_dates']} 日（{report['date_min']}–{report['date_max']}）",
        f"- 行数：{report['sample_n_rows']}",
        f"- 窗口：train {report['walkforward']['train_days']} / val {report['walkforward']['validation_days']} / test {report['walkforward']['test_days']} / step {report['walkforward']['step_days']}（{report['walkforward']['note']}）",
        f"- folds：{report['n_folds']}",
        "",
        "## 总览",
        "",
        "| 因子 | 覆盖率 | C2C IC | C2C t | C2C ICIR | O2C IC | O2C t | C2C Q5-Q1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for fid, e in report["factors"].items():
        c = e["labels"]["c2c"]["overall"]
        o = e["labels"]["o2c"]["overall"]
        q = e["labels"]["c2c"]["quintile"]
        lines.append(
            f"| `{fid}` | {e['coverage']:.1%} | {c['mean']:.4f} | {c['tstat']:.2f} | {c['icir']:.2f} | "
            f"{o['mean']:.4f} | {o['tstat']:.2f} | {q['mean_q5_minus_q1']:.5f} |"
        )
    lines += ["", "## 正交性（截面 Spearman 日均）", ""]
    peers = ORTHO_PEERS
    lines.append("| 因子 | " + " | ".join(f"`{p}`" for p in peers) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(peers)) + "|")
    for fid, e in report["factors"].items():
        cells = " | ".join(f"{e['ortho_mean_abs_rho'].get(p, float('nan')):.3f}" for p in peers)
        lines.append(f"| `{fid}` | {cells} |")
    lines += ["", "## Walk-forward 测试窗", ""]
    for fid, e in report["factors"].items():
        lines.append(f"### `{fid}`")
        lines.append("")
        lines.append("| fold | test | C2C IC | C2C t | O2C IC | O2C t |")
        lines.append("|---:|---|---:|---:|---:|---:|")
        for fr in e["walkforward"]:
            lines.append(
                f"| {fr['fold']} | {fr['test_start']}–{fr['test_end']} | "
                f"{fr['c2c']['mean']:.4f} | {fr['c2c']['tstat']:.2f} | "
                f"{fr['o2c']['mean']:.4f} | {fr['o2c']['tstat']:.2f} |"
            )
        lines.append("")
    lines += [
        "## 解读门槛（抽样，非正式晋升）",
        "",
        "- 这是 400 股 × 133 日的研究样本，不能作为晋升依据。",
        "- 关注方向：IC 符号是否与假设一致、|rho| 对现有因子是否 < 0.30、测试窗是否同号。",
        "- 下一步：若方向稳定，再抽样扩大或全量重建面板后走正式 walk-forward。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
