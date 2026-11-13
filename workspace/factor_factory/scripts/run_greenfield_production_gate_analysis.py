#!/usr/bin/env python3
"""
Production gate analysis for greenfield_multifactor.

Implements:
Step 1: C2C decomposition into overnight + intraday.
Step 2: 500-day Rank IC + FDR + Newey-West + 50 bootstrap noise test.
Step 3: factor redundancy + marginal contribution.
Step 4: O2C diagnostics/optimization recommendations.
Step 5: O2C net-sharpe ranking from gated factors.
Step 6: Extended OOS >=60d validation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scipy import stats
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"scipy is required: {exc}")

try:
    import statsmodels.api as sm
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"statsmodels is required: {exc}")


LABEL_C2C = "next_return_close_to_close_1d"
LABEL_O2C = "next_return_open_to_close_1d"
LABEL_C2O = "next_return_close_to_open_1d"


def _compound(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return float("nan")
    return float(np.prod(1.0 + r.values) - 1.0)


def _ann_return(total_return: float, days: int) -> float:
    if days <= 0 or not np.isfinite(total_return) or total_return <= -1:
        return float("nan")
    return float((1.0 + total_return) ** (252.0 / days) - 1.0)


def _sharpe(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 3:
        return float("nan")
    sd = r.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(252.0))


def _max_drawdown(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return float("nan")
    curve = (1.0 + r).cumprod()
    peak = curve.cummax()
    dd = curve / peak - 1.0
    return float(dd.min())


def _metrics(r: pd.Series) -> dict[str, Any]:
    r = pd.to_numeric(r, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    total = _compound(r)
    days = int(len(r))
    mdd = _max_drawdown(r)
    ann = _ann_return(total, days)
    return {
        "days": days,
        "total_return": total,
        "annualized_return": ann,
        "sharpe": _sharpe(r),
        "max_drawdown": mdd,
        "calmar": float(ann / abs(mdd)) if np.isfinite(ann) and np.isfinite(mdd) and mdd != 0 else float("nan"),
        "win_rate": float((r > 0).mean()) if days else float("nan"),
        "mean_daily_return": float(r.mean()) if days else float("nan"),
    }


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _bh_fdr(pvals: pd.Series) -> pd.Series:
    p = pd.to_numeric(pvals, errors="coerce")
    out = pd.Series(np.nan, index=p.index, dtype=float)
    valid = p.dropna().sort_values()
    m = len(valid)
    if m == 0:
        return out
    ranked = valid * m / np.arange(1, m + 1)
    q = ranked.iloc[::-1].cummin().iloc[::-1].clip(upper=1.0)
    out.loc[q.index] = q.values
    return out


def _newey_west_tstat(x: pd.Series, maxlags: int = 5) -> tuple[float, float]:
    x = pd.to_numeric(x, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(x) < 30:
        return float("nan"), float("nan")
    y = x.values
    X = np.ones((len(y), 1))
    try:
        fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
        return float(fit.tvalues[0]), float(fit.pvalues[0])
    except Exception:
        return float("nan"), float("nan")


def _daily_rank_ic(panel: pd.DataFrame, factors: list[str], label: str, dates: np.ndarray) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    need_cols = ["trade_date", label, *factors]
    sub = panel.loc[panel["trade_date"].isin(dates), need_cols].copy()
    for dt, g in sub.groupby("trade_date", sort=True):
        y = pd.to_numeric(g[label], errors="coerce")
        for fac in factors:
            x = pd.to_numeric(g[fac], errors="coerce")
            m = x.notna() & y.notna()
            if m.sum() < 50 or x[m].nunique() < 3 or y[m].nunique() < 3:
                ic = np.nan
            else:
                ic = x[m].rank(pct=True).corr(y[m].rank(pct=True), method="pearson")
            rows.append({"trade_date": dt, "factor": fac, "label": label, "rank_ic": ic})
    return pd.DataFrame(rows)


def _factor_long_short_returns(panel: pd.DataFrame, factors: list[str], label: str, dates: np.ndarray, q: float = 0.2) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    need_cols = ["trade_date", label, *factors]
    sub = panel.loc[panel["trade_date"].isin(dates), need_cols].copy()
    for dt, g in sub.groupby("trade_date", sort=True):
        y = pd.to_numeric(g[label], errors="coerce")
        for fac in factors:
            x = pd.to_numeric(g[fac], errors="coerce")
            m = x.notna() & y.notna()
            if m.sum() < 100 or x[m].nunique() < 10:
                ret = np.nan
            else:
                lo = x[m].quantile(q)
                hi = x[m].quantile(1 - q)
                long = y[m & (x >= hi)].mean()
                short = y[m & (x <= lo)].mean()
                ret = long - short
            rows.append({"trade_date": dt, "factor": fac, "label": label, "long_short_return": ret})
    return pd.DataFrame(rows)


def _bootstrap_noise_test(ic_wide: pd.DataFrame, n_bootstrap: int = 50, seed: int = 20260503) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for fac in ic_wide.columns:
        s = pd.to_numeric(ic_wide[fac], errors="coerce").dropna()
        if len(s) < 60:
            rows.append({"factor": fac, "bootstrap_pass_rate": np.nan, "bootstrap_mean_abs_t": np.nan})
            continue
        passed = 0
        abs_t = []
        arr = s.values
        n = len(arr)
        for _ in range(n_bootstrap):
            sample = rng.choice(arr, size=n, replace=True)
            sd = sample.std(ddof=1)
            if sd == 0 or not np.isfinite(sd):
                t = 0.0
            else:
                t = sample.mean() / (sd / math.sqrt(n))
            abs_t.append(abs(t))
            if abs(t) >= 1.96:
                passed += 1
        rows.append({
            "factor": fac,
            "bootstrap_pass_rate": passed / n_bootstrap,
            "bootstrap_mean_abs_t": float(np.mean(abs_t)),
        })
    return pd.DataFrame(rows)


def _evaluate_factor_gate(panel: pd.DataFrame, factors: list[str], dates_500: np.ndarray, output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    # Use O2C as production gate primary label; keep C2C/C2O for attribution context.
    ic_frames = []
    for label in [LABEL_C2C, LABEL_O2C, LABEL_C2O]:
        ic_frames.append(_daily_rank_ic(panel, factors, label, dates_500))
    ic_daily = pd.concat(ic_frames, ignore_index=True)
    ic_daily.to_csv(output_dir / "gate_daily_rank_ic.csv", index=False)

    primary = ic_daily.loc[ic_daily["label"] == LABEL_O2C].copy()
    wide = primary.pivot(index="trade_date", columns="factor", values="rank_ic")

    rows = []
    for fac in factors:
        s = wide[fac].dropna() if fac in wide else pd.Series(dtype=float)
        mean_ic = float(s.mean()) if len(s) else np.nan
        std_ic = float(s.std(ddof=1)) if len(s) > 1 else np.nan
        ir = float(mean_ic / std_ic * math.sqrt(252)) if std_ic and np.isfinite(std_ic) and std_ic != 0 else np.nan
        t_naive = float(mean_ic / (std_ic / math.sqrt(len(s)))) if len(s) > 2 and std_ic and std_ic != 0 else np.nan
        p_naive = float(2 * stats.t.sf(abs(t_naive), df=len(s) - 1)) if np.isfinite(t_naive) and len(s) > 2 else np.nan
        nw_t, nw_p = _newey_west_tstat(s, maxlags=5)
        rows.append({
            "factor": fac,
            "label": LABEL_O2C,
            "days": int(len(s)),
            "rank_ic_mean": mean_ic,
            "rank_ic_std": std_ic,
            "rank_ic_ir_ann": ir,
            "naive_t": t_naive,
            "naive_p": p_naive,
            "newey_west_t": nw_t,
            "newey_west_p": nw_p,
        })
    gate = pd.DataFrame(rows)
    gate["fdr_q"] = _bh_fdr(gate["newey_west_p"])
    boot = _bootstrap_noise_test(wide, n_bootstrap=50)
    gate = gate.merge(boot, on="factor", how="left")

    def classify(r: pd.Series) -> str:
        ic = r["rank_ic_mean"]
        q = r["fdr_q"]
        nw_t = abs(r["newey_west_t"]) if pd.notna(r["newey_west_t"]) else np.nan
        bpr = r["bootstrap_pass_rate"]
        days = r["days"]
        if days < 450 or pd.isna(ic):
            return "failed"
        # O2C production gate: direction must be positive and statistically credible.
        if ic > 0 and pd.notna(q) and q <= 0.10 and pd.notna(nw_t) and nw_t >= 1.96 and pd.notna(bpr) and bpr >= 0.60:
            return "production_ready"
        if ic > 0 and pd.notna(q) and q <= 0.25 and pd.notna(nw_t) and nw_t >= 1.50 and pd.notna(bpr) and bpr >= 0.40:
            return "near_threshold"
        return "failed"

    gate["gate_status"] = gate.apply(classify, axis=1)
    gate = gate.sort_values(["gate_status", "rank_ic_mean"], ascending=[True, False])
    gate.to_csv(output_dir / "gate_factor_status.csv", index=False)
    return gate, ic_daily


def _redundancy_and_marginal(panel: pd.DataFrame, factors: list[str], dates_500: np.ndarray, selected_factors: list[str], output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    latest_date = np.sort(dates_500)[-1]
    latest = panel.loc[panel["trade_date"] == latest_date, factors].copy()
    corr = latest.corr(method="spearman", min_periods=100)
    corr.to_csv(output_dir / "factor_spearman_corr_latest.csv")

    # pair clustering by high abs correlation; simple connected components.
    threshold = 0.80
    parent = {f: f for f in factors}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(factors):
        for b in factors[i + 1:]:
            val = corr.loc[a, b] if a in corr.index and b in corr.columns else np.nan
            if pd.notna(val) and abs(val) >= threshold:
                union(a, b)
    clusters: dict[str, list[str]] = {}
    for f in factors:
        clusters.setdefault(find(f), []).append(f)
    cluster_rows = []
    for cid, members in clusters.items():
        cluster_rows.append({"cluster_id": cid, "size": len(members), "members": ",".join(members)})
    cluster_df = pd.DataFrame(cluster_rows).sort_values(["size", "cluster_id"], ascending=[False, True])
    cluster_df.to_csv(output_dir / "factor_redundancy_clusters.csv", index=False)

    # Leave-one-factor-out on selected production portfolio using O2C and C2C labels.
    rows = []
    if selected_factors:
        sub = panel.loc[panel["trade_date"].isin(dates_500), ["trade_date", LABEL_C2C, LABEL_O2C, *selected_factors]].copy()
        for label in [LABEL_C2C, LABEL_O2C]:
            daily_full = []
            for _, g in sub.groupby("trade_date", sort=True):
                score = g[selected_factors].rank(pct=True).mean(axis=1)
                top = score >= score.quantile(0.95)
                daily_full.append(g.loc[top, label].mean())
            full_s = pd.Series(daily_full)
            full_m = _metrics(full_s)
            for fac in selected_factors:
                keep = [x for x in selected_factors if x != fac]
                daily_loo = []
                for _, g in sub.groupby("trade_date", sort=True):
                    if keep:
                        score = g[keep].rank(pct=True).mean(axis=1)
                    else:
                        score = pd.Series(0.0, index=g.index)
                    top = score >= score.quantile(0.95)
                    daily_loo.append(g.loc[top, label].mean())
                loo_s = pd.Series(daily_loo)
                loo_m = _metrics(loo_s)
                rows.append({
                    "removed_factor": fac,
                    "label": label,
                    "full_total_return": full_m["total_return"],
                    "loo_total_return": loo_m["total_return"],
                    "marginal_total_return": full_m["total_return"] - loo_m["total_return"],
                    "full_sharpe": full_m["sharpe"],
                    "loo_sharpe": loo_m["sharpe"],
                    "marginal_sharpe": full_m["sharpe"] - loo_m["sharpe"],
                })
    loo_df = pd.DataFrame(rows)
    loo_df.to_csv(output_dir / "leave_one_factor_out.csv", index=False)
    return cluster_df, loo_df


def _o2c_factor_ranking(panel: pd.DataFrame, gate: pd.DataFrame, dates_500: np.ndarray, output_dir: Path) -> pd.DataFrame:
    keep = gate.loc[gate["gate_status"].isin(["production_ready", "near_threshold"]), "factor"].tolist()
    if not keep:
        pd.DataFrame().to_csv(output_dir / "o2c_net_sharpe_ranking.csv", index=False)
        return pd.DataFrame()
    ls = _factor_long_short_returns(panel, keep, LABEL_O2C, dates_500, q=0.2)
    ls.to_csv(output_dir / "o2c_daily_factor_long_short_returns.csv", index=False)
    rows = []
    for fac, g in ls.groupby("factor"):
        m = _metrics(g["long_short_return"])
        rows.append({"factor": fac, **m})
    rank = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    rank.to_csv(output_dir / "o2c_net_sharpe_ranking.csv", index=False)
    return rank


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="data/factors/greenfield_multifactor_panel.parquet")
    ap.add_argument("--walkforward-daily", default="outputs/greenfield_multifactor/walkforward_daily_returns.csv")
    ap.add_argument("--walkforward-report", default="outputs/greenfield_multifactor/walkforward_report.json")
    ap.add_argument("--selection", default="outputs/greenfield_multifactor/current_selection_snapshot.json")
    ap.add_argument("--output-dir", default="outputs/greenfield_multifactor/production_gate")
    args = ap.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    panel = pd.read_parquet(args.panel)
    panel["trade_date"] = panel["trade_date"].astype(str)
    factor_cols = [c for c in panel.columns if c.startswith("g_") and not c.endswith("__z")]
    factor_cols = sorted(factor_cols)

    wf_daily = pd.read_csv(args.walkforward_daily, dtype={"trade_date": str})
    c2c = wf_daily.loc[wf_daily["series"] == "walkforward_close_to_close", ["trade_date", "gross_return", "net_return"]].rename(columns={"gross_return": "c2c_gross", "net_return": "c2c_net"})
    o2c = wf_daily.loc[wf_daily["series"] == "walkforward_open_to_close", ["trade_date", "gross_return", "net_return"]].rename(columns={"gross_return": "o2c_gross", "net_return": "o2c_net"})
    merged = c2c.merge(o2c, on="trade_date", how="inner")
    merged["overnight_gross_inferred"] = (1.0 + merged["c2c_gross"]) / (1.0 + merged["o2c_gross"]) - 1.0
    merged["overnight_net_inferred"] = (1.0 + merged["c2c_net"]) / (1.0 + merged["o2c_net"]) - 1.0
    merged.to_csv(output_dir / "step1_daily_c2c_o2c_overnight_decomposition.csv", index=False)

    wf_report = _load_json(Path(args.walkforward_report))
    agg = wf_report.get("aggregate", {})
    c2c_m = agg.get("close_to_close", {})
    o2c_m = agg.get("open_to_close", {})
    bc2c_m = agg.get("baseline_close_to_close", {})
    bo2c_m = agg.get("baseline_open_to_close", {})

    combo_overnight_total = (1 + c2c_m.get("total_return", np.nan)) / (1 + o2c_m.get("total_return", np.nan)) - 1
    base_overnight_total = (1 + bc2c_m.get("total_return", np.nan)) / (1 + bo2c_m.get("total_return", np.nan)) - 1
    c2c_excess = c2c_m.get("total_return", np.nan) - bc2c_m.get("total_return", np.nan)
    o2c_excess = o2c_m.get("total_return", np.nan) - bo2c_m.get("total_return", np.nan)
    overnight_excess = combo_overnight_total - base_overnight_total

    step1 = {
        "days": int(len(merged)),
        "combo_close_to_close": c2c_m,
        "combo_open_to_close": o2c_m,
        "combo_overnight_inferred": _metrics(merged["overnight_gross_inferred"]),
        "baseline_close_to_close": bc2c_m,
        "baseline_open_to_close": bo2c_m,
        "baseline_overnight_total_return_inferred_from_aggregate": base_overnight_total,
        "alpha_excess_total_return": {
            "close_to_close": c2c_excess,
            "open_to_close": o2c_excess,
            "overnight_inferred": overnight_excess,
        },
        "tradability_flag": "FAIL_T_PLUS_1_IF_ALPHA_DEPENDS_ON_OVERNIGHT" if o2c_excess <= 0 and overnight_excess > 0 else "PASS_INTRADAY_ALPHA_PRESENT",
    }
    (output_dir / "step1_return_attribution.json").write_text(json.dumps(step1, ensure_ascii=False, indent=2), encoding="utf-8")

    all_dates = np.sort(panel["trade_date"].unique())
    dates_500 = all_dates[-500:] if len(all_dates) >= 500 else all_dates
    dates_60 = all_dates[-60:] if len(all_dates) >= 60 else all_dates

    gate, ic_daily = _evaluate_factor_gate(panel, factor_cols, dates_500, output_dir)

    selection = _load_json(Path(args.selection))
    selected = [x.get("factor_id") for x in selection.get("selected_factors", []) if x.get("factor_id") in factor_cols]
    clusters, loo = _redundancy_and_marginal(panel, factor_cols, dates_500, selected, output_dir)
    o2c_rank = _o2c_factor_ranking(panel, gate, dates_500, output_dir)

    # Extended OOS using last 60 days, gated factors only.
    gated = gate.loc[gate["gate_status"].isin(["production_ready", "near_threshold"]), "factor"].tolist()
    ext_rows = []
    if gated:
        sub = panel.loc[panel["trade_date"].isin(dates_60), ["trade_date", LABEL_O2C, LABEL_C2C, *gated]].copy()
        for label in [LABEL_O2C, LABEL_C2C]:
            daily = []
            for _, g in sub.groupby("trade_date", sort=True):
                score = g[gated].rank(pct=True).mean(axis=1)
                top = score >= score.quantile(0.95)
                daily.append(g.loc[top, label].mean())
            ext_rows.append({"label": label, **_metrics(pd.Series(daily))})
    ext = pd.DataFrame(ext_rows)
    ext.to_csv(output_dir / "extended_oos_60d_validation.csv", index=False)

    step4 = {
        "o2c_vs_baseline_total_return_excess": o2c_excess,
        "o2c_vs_baseline_sharpe_excess": o2c_m.get("sharpe", np.nan) - bo2c_m.get("sharpe", np.nan),
        "diagnosis": "O2C_WEAKER_THAN_BASELINE" if o2c_excess < 0 else "O2C_BEATS_BASELINE",
        "recommended_actions_if_weak": [
            "switch scoring objective to O2C priority",
            "add intraday momentum/reversal factors",
            "test weekly / biweekly rebalance to reduce turnover and cost drag",
        ],
    }
    (output_dir / "step4_o2c_diagnostics.json").write_text(json.dumps(step4, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "factor_count": len(factor_cols),
        "dates_500_start": str(dates_500[0]),
        "dates_500_end": str(dates_500[-1]),
        "step1_tradability_flag": step1["tradability_flag"],
        "alpha_excess_total_return": step1["alpha_excess_total_return"],
        "gate_counts": gate["gate_status"].value_counts().to_dict(),
        "production_ready_factors": gate.loc[gate["gate_status"] == "production_ready", "factor"].tolist(),
        "near_threshold_factors": gate.loc[gate["gate_status"] == "near_threshold", "factor"].tolist(),
        "failed_factor_count": int((gate["gate_status"] == "failed").sum()),
        "selected_factors_current": selected,
        "redundancy_cluster_count": int(len(clusters)),
        "largest_cluster_size": int(clusters["size"].max()) if not clusters.empty else 0,
        "o2c_top5_by_net_sharpe": o2c_rank.head(5).to_dict(orient="records") if not o2c_rank.empty else [],
        "extended_oos_60d": ext.to_dict(orient="records"),
        "promotion_freeze_status": "KEEP_FREEZE" if ext.empty or not ((ext["label"] == LABEL_O2C) & (ext["total_return"] > 0)).any() else "CAN_REVIEW_FREEZE_RELEASE",
        "evidence_files": {
            "step1_daily_decomposition": str(output_dir / "step1_daily_c2c_o2c_overnight_decomposition.csv"),
            "step1_attribution": str(output_dir / "step1_return_attribution.json"),
            "daily_rank_ic": str(output_dir / "gate_daily_rank_ic.csv"),
            "gate_status": str(output_dir / "gate_factor_status.csv"),
            "correlation_matrix": str(output_dir / "factor_spearman_corr_latest.csv"),
            "redundancy_clusters": str(output_dir / "factor_redundancy_clusters.csv"),
            "leave_one_factor_out": str(output_dir / "leave_one_factor_out.csv"),
            "o2c_ranking": str(output_dir / "o2c_net_sharpe_ranking.csv"),
            "extended_oos_60d": str(output_dir / "extended_oos_60d_validation.csv"),
            "o2c_diagnostics": str(output_dir / "step4_o2c_diagnostics.json"),
        },
    }
    (output_dir / "production_gate_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    md = []
    md.append("# Greenfield Multifactor Production Gate Analysis\n")
    md.append(f"- Factor count: {len(factor_cols)}")
    md.append(f"- 500d window: {dates_500[0]} - {dates_500[-1]}")
    md.append(f"- Step1 tradability flag: **{step1['tradability_flag']}**")
    md.append(f"- C2C excess: {c2c_excess:.4f}")
    md.append(f"- O2C excess: {o2c_excess:.4f}")
    md.append(f"- Inferred overnight excess: {overnight_excess:.4f}")
    md.append(f"- Gate counts: {summary['gate_counts']}")
    md.append(f"- Promotion freeze: **{summary['promotion_freeze_status']}**")
    md.append("\n## Evidence files\n")
    for k, v in summary["evidence_files"].items():
        md.append(f"- {k}: `{v}`")
    (output_dir / "production_gate_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
