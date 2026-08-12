#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 - Factor audit engine.

实现内容：
1. 与 base factor 的正交性筛选（|rho| < 0.3）
2. Walk-forward 审计
3. 极端涨幅样本剔除（每交易日前 5%）
4. 双边 0.2% 摩擦成本后的收益、夏普、胜率
5. Monte Carlo block bootstrap 鲁棒性评估

输出：
- outputs/audit_reports/factor_audit_phase1.json
- outputs/candidate_lists/factor_whitelist_phase1.json
- outputs/candidate_lists/factor_coldlist_phase1.json
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


DEFAULT_LABEL_MODE = "open_to_close"


@dataclass
class RuntimeConfig:
    root: Path
    panel_path: Path
    catalog_path: Path
    audit_report_path: Path
    whitelist_path: Path
    coldlist_path: Path
    start_date: str
    end_date: str
    base_weight: float
    orth_threshold: float
    top_n: int
    round_trip_cost: float
    stress_round_trip_cost: float
    train_months: int
    validation_months: int
    test_months: int
    step_months: int
    n_sims: int
    block_size: int
    trim_top_pct: float
    min_rank_ic_abs: float
    min_win_rate_lift: float
    min_sharpe_lift: float
    max_turnover: float
    max_drawdown_deterioration: float
    min_rank_icir: float
    max_rank_ic_pvalue_newey_west: float
    max_rank_ic_qvalue_fdr: float
    newey_west_lags: int
    min_wf_ic_positive_ratio: float
    min_wf_icir: float
    noise_n_sims: int
    noise_std_frac: float
    max_noise_decay_pct: float
    max_noise_false_positive_rate: float
    phase2_gates_enabled: bool
    label_column: str
    selection_overlay: SelectionOverlay | None = None


@dataclass(frozen=True)
class SelectionOverlay:
    manifest_path: Path
    contract_version: str | None = None
    candidate_id: str | None = None
    top_n: int | None = None
    max_names_per_industry: int | None = None
    industry_blacklist: tuple[str, ...] = ()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _coerce_optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    coerced = int(value)
    if coerced <= 0:
        raise ValueError(f"overlay {field_name} must be > 0, got {value}")
    return coerced


def load_selection_overlay(path_value: str | Path | None) -> SelectionOverlay | None:
    if path_value is None or not str(path_value).strip():
        return None
    manifest_path = Path(path_value).expanduser().resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    constraints = payload.get("overlay_constraints") or payload.get("overlay_bundle") or {}

    blacklist: list[str] = []
    seen: set[str] = set()
    for item in constraints.get("industry_blacklist", []) or []:
        name = str(item).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        blacklist.append(name)

    return SelectionOverlay(
        manifest_path=manifest_path,
        contract_version=(payload.get("contract_version") or "").strip() or None,
        candidate_id=(payload.get("candidate_id") or payload.get("recommended_candidate_id") or "").strip() or None,
        top_n=_coerce_optional_positive_int(constraints.get("top_n"), "top_n"),
        max_names_per_industry=_coerce_optional_positive_int(
            constraints.get("max_names_per_industry"),
            "max_names_per_industry",
        ),
        industry_blacklist=tuple(blacklist),
    )


def effective_top_n(top_n: int, selection_overlay: SelectionOverlay | None = None) -> int:
    if selection_overlay is not None and selection_overlay.top_n is not None:
        return int(selection_overlay.top_n)
    return int(top_n)


def selection_overlay_summary(
    selection_overlay: SelectionOverlay | None,
    default_top_n: int | None = None,
) -> dict[str, Any] | None:
    if selection_overlay is None:
        return None
    return {
        "manifest_path": str(selection_overlay.manifest_path),
        "contract_version": selection_overlay.contract_version,
        "candidate_id": selection_overlay.candidate_id,
        "top_n": selection_overlay.top_n if selection_overlay.top_n is not None else default_top_n,
        "max_names_per_industry": selection_overlay.max_names_per_industry,
        "industry_blacklist": list(selection_overlay.industry_blacklist),
    }


def load_runtime_config(root: Path) -> RuntimeConfig:
    phase1 = load_yaml(root / "config" / "phase1.yaml")
    costs = load_yaml(root / "config" / "backtest_costs.yaml")
    paths = phase1["paths"]
    phase = phase1["phase1"]
    wf = phase1["walk_forward"]
    mc = phase1["monte_carlo"]
    outlier = phase1["outlier_filter"]
    gate = phase1["quality_gate"]
    phase2 = phase1.get("phase2_statistical_gate", {}) or {}
    aux = phase1["auxiliary_factor_pool"]
    return RuntimeConfig(
        root=root,
        panel_path=root / "data" / "factors" / "panel_phase1.parquet",
        catalog_path=root / "outputs" / "candidate_lists" / "phase1_candidate_catalog.json",
        audit_report_path=root / "outputs" / "audit_reports" / "factor_audit_phase1.json",
        whitelist_path=root / "outputs" / "candidate_lists" / "factor_whitelist_phase1.json",
        coldlist_path=root / "outputs" / "candidate_lists" / "factor_coldlist_phase1.json",
        start_date=str(phase["start_date"]).replace("-", ""),
        end_date=str(phase["end_date"]).replace("-", ""),
        base_weight=float(phase1["base_factor"]["fixed_weight"]),
        orth_threshold=float(aux["orthogonal_threshold"]),
        top_n=int(costs["portfolio_rules"]["max_position_count"]),
        round_trip_cost=float(costs["friction"]["round_trip_cost"]),
        stress_round_trip_cost=float(costs["friction"].get("stress_round_trip_cost", 0.005)),
        train_months=int(wf["train_months"]),
        validation_months=int(wf["validation_months"]),
        test_months=int(wf["test_months"]),
        step_months=int(wf["step_months"]),
        n_sims=int(mc["n_sims"]),
        block_size=int(mc["block_size"]),
        trim_top_pct=float(outlier["trim_top_pct"]),
        min_rank_ic_abs=float(gate["min_rank_ic_abs"]),
        min_win_rate_lift=float(gate["min_win_rate_lift"]),
        min_sharpe_lift=float(gate["min_sharpe_lift"]),
        max_turnover=float(gate["max_turnover"]),
        max_drawdown_deterioration=float(gate["max_drawdown_deterioration"]),
        min_rank_icir=float(phase2.get("min_rank_icir", 0.30)),
        max_rank_ic_pvalue_newey_west=float(phase2.get("max_rank_ic_pvalue_newey_west", 0.05)),
        max_rank_ic_qvalue_fdr=float(phase2.get("max_rank_ic_qvalue_fdr", 0.10)),
        newey_west_lags=int(phase2.get("newey_west_lags", 5)),
        min_wf_ic_positive_ratio=float(phase2.get("min_wf_ic_positive_ratio", 0.60)),
        min_wf_icir=float(phase2.get("min_wf_icir", 0.20)),
        noise_n_sims=int(phase2.get("noise_n_sims", 50)),
        noise_std_frac=float(phase2.get("noise_std_frac", 0.05)),
        max_noise_decay_pct=float(phase2.get("max_noise_decay_pct", 25.0)),
        max_noise_false_positive_rate=float(phase2.get("max_noise_false_positive_rate", 0.05)),
        phase2_gates_enabled=bool(phase2.get("enabled", True)),
        label_column=resolve_label_column(DEFAULT_LABEL_MODE),
    )


def apply_output_suffix(cfg: RuntimeConfig, suffix: str | None) -> RuntimeConfig:
    if not suffix:
        return cfg
    suffix = suffix.strip()
    if not suffix:
        return cfg

    def _with_suffix(path: Path) -> Path:
        return path.with_name(f"{path.stem}.{suffix}{path.suffix}")

    cfg.audit_report_path = _with_suffix(cfg.audit_report_path)
    cfg.whitelist_path = _with_suffix(cfg.whitelist_path)
    cfg.coldlist_path = _with_suffix(cfg.coldlist_path)
    return cfg


def load_panel(cfg: RuntimeConfig) -> pd.DataFrame:
    panel = pd.read_parquet(cfg.panel_path)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    if "base_record_present" in panel.columns:
        panel["benchmark_eligible"] = panel["base_record_present"].fillna(0).astype(int)
    elif "benchmark_eligible" not in panel.columns:
        panel["benchmark_eligible"] = panel["base_factor_selected"].fillna(0).astype(int)
    else:
        panel["benchmark_eligible"] = panel["benchmark_eligible"].fillna(0).astype(int)
    if "benchmark_date_eligible" not in panel.columns:
        panel["benchmark_date_eligible"] = (
            panel.groupby("trade_date", group_keys=False)["benchmark_eligible"].transform("max").fillna(0).astype(int)
        )
    else:
        panel["benchmark_date_eligible"] = panel["benchmark_date_eligible"].fillna(0).astype(int)
    panel = panel[(panel["trade_date"] >= pd.Timestamp(cfg.start_date)) & (panel["trade_date"] <= pd.Timestamp(cfg.end_date))].copy()
    panel = panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    return panel


def resolve_label_column(label_mode: str) -> str:
    mapping = {
        "close_to_close": "next_return_close_to_close_1d",
        "open_to_close": "next_return_open_to_close_1d",
        "close_to_open": "next_return_close_to_open_1d",
    }
    key = str(label_mode or "close_to_close").strip()
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    if lowered in mapping:
        return mapping[lowered]
    if key.startswith("next_return_"):
        return key
    raise ValueError(f"unsupported label_mode={label_mode}")


def load_catalog(cfg: RuntimeConfig) -> dict[str, Any]:
    return json.loads(cfg.catalog_path.read_text(encoding="utf-8"))


def emit_progress(progress_log: Path | None, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)
    if progress_log is None:
        return
    progress_log.parent.mkdir(parents=True, exist_ok=True)
    with progress_log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    def _z(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        mean = s.mean(skipna=True)
        std = s.std(skipna=True)
        if pd.isna(std) or std == 0:
            out = pd.Series(np.nan, index=s.index, dtype=float)
            out.loc[s.notna()] = 0.0
            return out
        return (s - mean) / std
    return df.groupby("trade_date", group_keys=False)[col].apply(_z)


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    wealth = (1.0 + returns.fillna(0.0)).cumprod()
    peak = wealth.cummax()
    dd = wealth / peak - 1.0
    return float(dd.min())


def annualized_sharpe(returns: pd.Series) -> float:
    x = returns.dropna()
    if len(x) < 2:
        return 0.0
    std = float(x.std())
    if std == 0:
        return 0.0
    return float(x.mean() / std * np.sqrt(252))


def calmar_ratio(returns: pd.Series) -> float:
    x = returns.dropna()
    if x.empty:
        return 0.0
    annual = float((1.0 + x.mean()) ** 252 - 1.0)
    mdd = abs(max_drawdown(x))
    if mdd == 0:
        return 0.0
    return annual / mdd


def spearman_corr(x: pd.Series, y: pd.Series) -> float:
    pair = pd.DataFrame({"x": pd.to_numeric(x, errors="coerce"), "y": pd.to_numeric(y, errors="coerce")}).dropna()
    if len(pair) < 2:
        return float("nan")
    xr = pair["x"].rank(method="average")
    yr = pair["y"].rank(method="average")
    corr = xr.corr(yr, method="pearson")
    return float(corr) if pd.notna(corr) else float("nan")


def compute_daily_rank_ic(df: pd.DataFrame, factor_col: str, return_col: str) -> pd.Series:
    rows = []
    for trade_date, day in df.groupby("trade_date", sort=True):
        subset = day[[factor_col, return_col]].dropna()
        if len(subset) < 20:
            continue
        ic = spearman_corr(subset[factor_col], subset[return_col])
        if pd.notna(ic):
            rows.append((trade_date, float(ic)))
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series({d: v for d, v in rows}).sort_index()




def two_sided_normal_pvalue(t_stat: float) -> float:
    if not np.isfinite(t_stat):
        return 1.0
    return float(math.erfc(abs(float(t_stat)) / math.sqrt(2.0)))


def newey_west_standard_error(values: pd.Series, max_lag: int | None = None) -> float:
    x = pd.to_numeric(values, errors="coerce").dropna().astype(float).to_numpy()
    n = len(x)
    if n < 2:
        return float("nan")
    demeaned = x - float(np.mean(x))
    if max_lag is None:
        max_lag = max(1, int(math.floor(4 * (n / 100.0) ** (2 / 9))))
    max_lag = max(1, min(int(max_lag), n - 1))
    gamma0 = float(np.dot(demeaned, demeaned) / n)
    lrv = gamma0
    for lag in range(1, max_lag + 1):
        cov = float(np.dot(demeaned[lag:], demeaned[:-lag]) / n)
        weight = 1.0 - lag / (max_lag + 1.0)
        lrv += 2.0 * weight * cov
    lrv = max(lrv, 0.0)
    return float(math.sqrt(lrv / n)) if lrv > 0 else 0.0


def summarize_ic_series(ic: pd.Series, nw_lags: int | None = None) -> dict[str, Any]:
    x = pd.to_numeric(ic, errors="coerce").dropna().astype(float)
    n = int(len(x))
    if n == 0:
        return {"mean": 0.0, "abs_mean": 0.0, "std": 0.0, "icir": 0.0, "n": 0, "autocorr_lag1": 0.0, "effective_sample_size": 0.0, "t_stat": 0.0, "p_value": 1.0, "newey_west_lags": nw_lags or 0, "t_stat_newey_west": 0.0, "p_value_newey_west": 1.0, "positive_ratio": 0.0}
    mean = float(x.mean())
    std = float(x.std(ddof=1)) if n >= 2 else 0.0
    icir = float(mean / std) if std > 0 else 0.0
    se = std / math.sqrt(n) if std > 0 and n > 0 else float("nan")
    t_stat = float(mean / se) if se and np.isfinite(se) and se > 0 else 0.0
    ac = float(x.autocorr(lag=1)) if n >= 3 else 0.0
    if not np.isfinite(ac): ac = 0.0
    eff_n = float(n * (1.0 - ac) / (1.0 + ac)) if ac < 0.999 else 1.0
    auto_lag = max(1, int(math.floor(4 * (n / 100.0) ** (2 / 9)))) if nw_lags is None else int(nw_lags)
    nw_se = newey_west_standard_error(x, auto_lag)
    nw_t = float(mean / nw_se) if nw_se and np.isfinite(nw_se) and nw_se > 0 else 0.0
    return {"mean": mean, "abs_mean": abs(mean), "std": std, "icir": icir, "n": n, "autocorr_lag1": ac, "effective_sample_size": eff_n, "t_stat": t_stat, "p_value": two_sided_normal_pvalue(t_stat), "newey_west_lags": auto_lag, "t_stat_newey_west": nw_t, "p_value_newey_west": two_sided_normal_pvalue(nw_t), "positive_ratio": float((x > 0).mean())}


def benjamini_hochberg_qvalues(pvalues: list[float]) -> list[float]:
    m = len(pvalues)
    if m == 0:
        return []
    pairs = sorted([(float(p) if np.isfinite(p) else 1.0, i) for i, p in enumerate(pvalues)], key=lambda x: x[0])
    q = [1.0] * m
    prev = 1.0
    for rank, (pval, idx) in reversed(list(enumerate(pairs, start=1))):
        val = min(prev, pval * m / rank)
        prev = val
        q[idx] = float(min(max(val, 0.0), 1.0))
    return q


def run_factor_noise_tests(df: pd.DataFrame, factor_col: str, return_col: str, cfg: RuntimeConfig, base_rank_ic_abs: float, seed: int = 42) -> dict[str, Any]:
    if base_rank_ic_abs <= 0 or df.empty:
        return {"noise_robust_pass": False, "noise_decay_p50": 1.0, "noise_decay_p90": 1.0, "permutation_false_positive_rate": 1.0, "n_sims": 0, "noise_test_mode": "lightweight"}
    rng = np.random.default_rng(seed)
    decays = []
    perm_abs = []
    factor = pd.to_numeric(df[factor_col], errors="coerce")
    sigma = float(factor.std(ddof=1) or 0.0) * cfg.noise_std_frac
    for _ in range(max(1, cfg.noise_n_sims)):
        tmp = df[["trade_date", factor_col, return_col]].copy()
        tmp["__factor_noisy"] = pd.to_numeric(tmp[factor_col], errors="coerce") + rng.normal(0.0, sigma, len(tmp))
        noisy_abs = summarize_ic_series(compute_daily_rank_ic(tmp, "__factor_noisy", return_col), cfg.newey_west_lags)["abs_mean"]
        decays.append(max(0.0, (base_rank_ic_abs - noisy_abs) / max(base_rank_ic_abs, 1e-12)))
        tmp["__factor_perm"] = tmp.groupby("trade_date")[factor_col].transform(lambda s: rng.permutation(pd.to_numeric(s, errors="coerce").to_numpy()))
        perm_abs.append(summarize_ic_series(compute_daily_rank_ic(tmp, "__factor_perm", return_col), cfg.newey_west_lags)["abs_mean"])
    decay_p50 = float(np.percentile(decays, 50))
    decay_p90 = float(np.percentile(decays, 90))
    false_rate = float(np.mean(np.array(perm_abs) >= base_rank_ic_abs))
    return {"noise_robust_pass": bool(decay_p90 <= cfg.max_noise_decay_pct and false_rate <= cfg.max_noise_false_positive_rate), "noise_decay_p50": decay_p50, "noise_decay_p90": decay_p90, "permutation_false_positive_rate": false_rate, "permutation_rank_ic_abs_p95": float(np.percentile(perm_abs, 95)), "n_sims": int(max(1, cfg.noise_n_sims)), "noise_test_mode": "lightweight"}


def finalize_phase2_status(reports: list[dict[str, Any]], cfg: RuntimeConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pvals = [float(((r.get("rank_ic_stats") or {}).get("p_value_newey_west", 1.0))) for r in reports]
    qvals = benjamini_hochberg_qvalues(pvals)
    whitelist, coldlist = [], []
    for r, q in zip(reports, qvals):
        r["rank_ic_fdr_q_value"] = q
        stats = r.get("rank_ic_stats") or {}
        wf = r.get("walk_forward_ic_summary") or {}
        noise = r.get("noise_test") or {}
        details = r.setdefault("gate_details", {})
        details["icir_pass"] = bool(float(stats.get("icir", 0.0) or 0.0) > cfg.min_rank_icir)
        details["pvalue_pass"] = bool(float(stats.get("p_value_newey_west", 1.0) or 1.0) <= cfg.max_rank_ic_pvalue_newey_west)
        details["fdr_pass"] = bool(q <= cfg.max_rank_ic_qvalue_fdr)
        details["wf_ic_pass"] = bool(wf.get("wf_ic_pass", False))
        details["noise_pass"] = bool(noise.get("noise_robust_pass", False))
        details["phase2_statistical_pass"] = bool(details["icir_pass"] and details["pvalue_pass"] and details["fdr_pass"] and details["wf_ic_pass"] and details["noise_pass"])
        if cfg.phase2_gates_enabled and r.get("status") == "whitelist" and not details["phase2_statistical_pass"]:
            r["status"] = "cold_storage_phase2_stat_gate"
        if r.get("status") == "whitelist":
            whitelist.append(r)
        elif str(r.get("status", "")).startswith("rejected") or str(r.get("status", "")).startswith("cold_storage"):
            coldlist.append(r)
    return whitelist, coldlist

def orthogonal_corr(df: pd.DataFrame, factor_col: str) -> dict[str, float]:
    subset = df[[factor_col, "base_factor_score_norm"]].dropna()
    if len(subset) < 50:
        return {"pearson": 1.0, "spearman": 1.0}
    pearson = subset[factor_col].corr(subset["base_factor_score_norm"], method="pearson")
    spearman = spearman_corr(subset[factor_col], subset["base_factor_score_norm"])
    return {
        "pearson": float(pearson) if pd.notna(pearson) else 0.0,
        "spearman": float(spearman) if pd.notna(spearman) else 0.0,
    }


def trim_outliers_by_date(df: pd.DataFrame, trim_top_pct: float, return_col: str) -> pd.DataFrame:
    q = 1.0 - trim_top_pct / 100.0
    rows = []
    for _, day in df.groupby("trade_date", sort=True):
        if day[return_col].notna().sum() < 20:
            rows.append(day)
            continue
        threshold = day[return_col].quantile(q)
        rows.append(day[day[return_col] <= threshold].copy())
    return pd.concat(rows, ignore_index=True) if rows else df.iloc[0:0].copy()


def select_ranked_holdings(
    day: pd.DataFrame,
    score_col: str,
    return_col: str,
    top_n: int,
    selection_overlay: SelectionOverlay | None = None,
) -> pd.DataFrame:
    cols = ["ts_code", score_col, return_col]
    if "industry" in day.columns:
        cols.append("industry")
    subset = day[cols].dropna(subset=[score_col, return_col]).copy()
    if subset.empty:
        return subset

    if selection_overlay and selection_overlay.industry_blacklist:
        if "industry" not in subset.columns:
            subset["industry"] = "未分类"
        industry_series = subset["industry"].fillna("未分类").astype(str)
        subset = subset.loc[~industry_series.isin(selection_overlay.industry_blacklist)].copy()
        if subset.empty:
            return subset

    subset = subset.sort_values([score_col, "ts_code"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    allow_partial_selection = selection_overlay is not None
    if selection_overlay is None or selection_overlay.max_names_per_industry is None:
        if not allow_partial_selection and len(subset) < top_n:
            return subset.iloc[0:0].copy()
        return subset.head(top_n).copy()

    industry_series = subset["industry"].fillna("未分类").astype(str) if "industry" in subset.columns else pd.Series(["未分类"] * len(subset), index=subset.index)
    chosen_rows: list[int] = []
    counts: dict[str, int] = {}
    for idx, industry in industry_series.items():
        if counts.get(industry, 0) >= selection_overlay.max_names_per_industry:
            continue
        counts[industry] = counts.get(industry, 0) + 1
        chosen_rows.append(idx)
        if len(chosen_rows) >= top_n:
            break
    if not chosen_rows:
        return subset.iloc[0:0].copy()
    return subset.loc[chosen_rows].copy().sort_values([score_col, "ts_code"], ascending=[False, True], kind="mergesort").reset_index(drop=True)


def build_monthly_splits(df: pd.DataFrame, cfg: RuntimeConfig) -> list[dict[str, pd.Timestamp]]:
    dates = pd.Series(sorted(df["trade_date"].dropna().unique()))
    if dates.empty:
        return []
    min_date = dates.min()
    max_date = dates.max()
    anchor = pd.Timestamp(min_date).replace(day=1)
    splits = []
    while True:
        train_start = anchor
        train_end = train_start + pd.DateOffset(months=cfg.train_months) - pd.Timedelta(days=1)
        val_end = train_end + pd.DateOffset(months=cfg.validation_months)
        test_end = val_end + pd.DateOffset(months=cfg.test_months)
        if test_end > max_date + pd.Timedelta(days=1):
            break
        splits.append({
            "train_start": train_start,
            "train_end": train_end,
            "val_end": val_end,
            "test_end": test_end,
        })
        anchor = anchor + pd.DateOffset(months=cfg.step_months)
    return splits


def evaluate_strategy(
    df: pd.DataFrame,
    score_col: str,
    return_col: str,
    top_n: int,
    round_trip_cost: float,
    fixed_holdings_col: str | None = None,
    selection_overlay: SelectionOverlay | None = None,
) -> dict[str, Any]:
    daily_returns = []
    daily_turnover = []
    prev_holdings: set[str] = set()
    resolved_top_n = effective_top_n(top_n, selection_overlay)
    for trade_date, day in df.groupby("trade_date", sort=True):
        if fixed_holdings_col:
            subset = day.loc[
                day[fixed_holdings_col].fillna(0).astype(int) == 1,
                ["ts_code", return_col],
            ].dropna(subset=[return_col])
            if subset.empty:
                continue
            chosen = subset.copy()
        else:
            chosen = select_ranked_holdings(
                day,
                score_col,
                return_col,
                resolved_top_n,
                selection_overlay=selection_overlay,
            )
            if chosen.empty:
                continue
        holdings = set(chosen["ts_code"].astype(str))
        gross = float(chosen[return_col].mean())
        turnover = 1.0 if not prev_holdings else 1.0 - len(holdings & prev_holdings) / max(1, len(holdings | prev_holdings))
        net = gross - round_trip_cost * turnover
        daily_returns.append((trade_date, net))
        daily_turnover.append((trade_date, turnover))
        prev_holdings = holdings
    if not daily_returns:
        empty = pd.Series(dtype=float)
        return {
            "daily_returns": empty,
            "daily_turnover": empty,
            "metrics": {
                "total_return": 0.0,
                "annualized_return": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
                "calmar": 0.0,
                "win_rate": 0.0,
                "turnover": 0.0,
                "days": 0,
            },
        }
    series = pd.Series({d: v for d, v in daily_returns}).sort_index()
    turnover_series = pd.Series({d: v for d, v in daily_turnover}).sort_index()
    total = float((1.0 + series).prod() - 1.0)
    annual = float((1.0 + total) ** (252 / max(1, len(series))) - 1.0)
    metrics = {
        "total_return": total,
        "annualized_return": annual,
        "sharpe": annualized_sharpe(series),
        "max_drawdown": max_drawdown(series),
        "calmar": calmar_ratio(series),
        "win_rate": float((series > 0).mean()),
        "turnover": float(turnover_series.mean() if not turnover_series.empty else 0.0),
        "days": int(len(series)),
    }
    return {"daily_returns": series, "daily_turnover": turnover_series, "metrics": metrics}


def aggregate_oos_series(series_list: list[pd.Series]) -> pd.Series:
    if not series_list:
        return pd.Series(dtype=float)
    combined = pd.concat([s.sort_index() for s in series_list if not s.empty]).sort_index()
    if combined.empty:
        return pd.Series(dtype=float)
    if combined.index.has_duplicates:
        combined = combined.groupby(level=0).mean().sort_index()
    return combined


def build_metrics_from_series(returns: pd.Series, turnover: pd.Series | None = None) -> dict[str, Any]:
    series = returns.dropna().sort_index()
    if series.empty:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "turnover": 0.0,
            "days": 0,
        }
    total = float((1.0 + series).prod() - 1.0)
    annual = float((1.0 + total) ** (252 / max(1, len(series))) - 1.0)
    turnover_series = pd.Series(dtype=float)
    if turnover is not None and not turnover.empty:
        turnover_series = turnover.reindex(series.index).dropna()
    return {
        "total_return": total,
        "annualized_return": annual,
        "sharpe": annualized_sharpe(series),
        "max_drawdown": max_drawdown(series),
        "calmar": calmar_ratio(series),
        "win_rate": float((series > 0).mean()),
        "turnover": float(turnover_series.mean() if not turnover_series.empty else 0.0),
        "days": int(len(series)),
    }


def block_bootstrap_metrics(returns: pd.Series, n_sims: int, block_size: int, seed: int = 42) -> dict[str, float]:
    x = returns.dropna().to_numpy()
    if len(x) < max(5, block_size):
        return {"mc_sharpe_p50": 0.0, "mc_sharpe_p05": 0.0, "mc_total_return_p50": 0.0}
    rng = np.random.default_rng(seed)
    sims_sharpe = []
    sims_total = []
    blocks = [x[i:i + block_size] for i in range(0, len(x), block_size)]
    for _ in range(n_sims):
        sample = []
        while len(sample) < len(x):
            block = blocks[int(rng.integers(0, len(blocks)))]
            sample.extend(block.tolist())
        sample = np.array(sample[: len(x)], dtype=float)
        s = pd.Series(sample)
        sims_sharpe.append(annualized_sharpe(s))
        sims_total.append(float((1.0 + s).prod() - 1.0))
    return {
        "mc_sharpe_p50": float(np.percentile(sims_sharpe, 50)),
        "mc_sharpe_p05": float(np.percentile(sims_sharpe, 5)),
        "mc_total_return_p50": float(np.percentile(sims_total, 50)),
    }


def evaluate_factor(
    df: pd.DataFrame,
    factor_id: str,
    cfg: RuntimeConfig,
    comparison_df: pd.DataFrame | None = None,
    benchmark_rows_df: pd.DataFrame | None = None,
    splits: list[dict[str, pd.Timestamp]] | None = None,
    trimmed_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    comparison_df = comparison_df if comparison_df is not None else df.loc[df["benchmark_date_eligible"] == 1]
    benchmark_rows_df = benchmark_rows_df if benchmark_rows_df is not None else comparison_df.loc[comparison_df["benchmark_eligible"] == 1]
    benchmark_trade_days = int(comparison_df["trade_date"].nunique())
    benchmark_row_count = int(len(benchmark_rows_df))
    if comparison_df.empty or benchmark_rows_df.empty:
        return {
            "factor_id": factor_id,
            "status": "rejected_no_benchmark_window",
            "label_column": cfg.label_column,
            "benchmark_eligible_trade_days": benchmark_trade_days,
            "benchmark_eligible_row_count": benchmark_row_count,
        }

    orth = orthogonal_corr(benchmark_rows_df, factor_id)
    rank_ic_series = compute_daily_rank_ic(df, factor_id, cfg.label_column)
    rank_ic_stats = summarize_ic_series(rank_ic_series, cfg.newey_west_lags)
    rank_ic_mean = float(rank_ic_stats["mean"])
    rank_ic_abs = float(rank_ic_stats["abs_mean"])

    if max(abs(orth["pearson"]), abs(orth["spearman"])) >= cfg.orth_threshold:
        return {
            "factor_id": factor_id,
            "status": "rejected_correlation",
            "label_column": cfg.label_column,
            "benchmark_eligible_trade_days": benchmark_trade_days,
            "benchmark_eligible_row_count": benchmark_row_count,
            "orthogonal_corr": orth,
            "rank_ic_mean": rank_ic_mean,
        }

    splits = splits if splits is not None else build_monthly_splits(df, cfg)
    if not splits:
        return {
            "factor_id": factor_id,
            "status": "rejected_no_splits",
            "label_column": cfg.label_column,
            "benchmark_eligible_trade_days": benchmark_trade_days,
            "benchmark_eligible_row_count": benchmark_row_count,
            "orthogonal_corr": orth,
            "rank_ic_mean": rank_ic_mean,
        }

    base_weight = cfg.base_weight
    factor_weight = 1.0 - base_weight
    test_daily_returns = []
    base_daily_returns = []
    test_daily_returns_stress = []
    base_daily_returns_stress = []
    combo_daily_turnover = []
    base_daily_turnover = []
    trim_rank_ic_abs = 0.0
    split_reports = []

    trimmed_df = trimmed_df if trimmed_df is not None else trim_outliers_by_date(df, cfg.trim_top_pct, cfg.label_column)
    trimmed_ic = compute_daily_rank_ic(trimmed_df, factor_id, cfg.label_column)
    trimmed_rank_ic_stats = summarize_ic_series(trimmed_ic, cfg.newey_west_lags)
    trim_rank_ic_abs = float(trimmed_rank_ic_stats["abs_mean"])
    noise_test = run_factor_noise_tests(trimmed_df, factor_id, cfg.label_column, cfg, rank_ic_abs)

    for split in splits:
        train_mask = (df["trade_date"] >= split["train_start"]) & (df["trade_date"] <= split["train_end"])
        test_mask = (comparison_df["trade_date"] > split["val_end"]) & (comparison_df["trade_date"] <= split["test_end"])
        train_df = df.loc[train_mask]
        test_df = comparison_df.loc[test_mask].copy()
        if train_df.empty or test_df.empty:
            continue

        train_ic = compute_daily_rank_ic(train_df, factor_id, cfg.label_column)
        train_ic_stats = summarize_ic_series(train_ic, cfg.newey_west_lags)
        test_ic_stats = summarize_ic_series(compute_daily_rank_ic(test_df, factor_id, cfg.label_column), cfg.newey_west_lags)
        sign = 1.0 if float(train_ic_stats.get("mean", 0.0) or 0.0) >= 0 else -1.0

        test_df["base_score_masked"] = test_df["base_factor_score_norm"].where(test_df["benchmark_eligible"] == 1)
        test_df["base_z"] = zscore_by_date(test_df, "base_score_masked")
        test_df["factor_z"] = zscore_by_date(test_df, factor_id) * sign
        test_df["combo_score"] = base_weight * test_df["base_z"].fillna(0.0) + factor_weight * test_df["factor_z"]
        test_df["base_score_only"] = test_df["base_z"]

        combo_eval = evaluate_strategy(
            test_df,
            "combo_score",
            cfg.label_column,
            cfg.top_n,
            cfg.round_trip_cost,
            selection_overlay=cfg.selection_overlay,
        )
        base_eval = evaluate_strategy(
            test_df,
            "base_score_only",
            cfg.label_column,
            cfg.top_n,
            cfg.round_trip_cost,
            fixed_holdings_col="benchmark_eligible",
        )
        combo_stress_eval = evaluate_strategy(
            test_df,
            "combo_score",
            cfg.label_column,
            cfg.top_n,
            cfg.stress_round_trip_cost,
            selection_overlay=cfg.selection_overlay,
        )
        base_stress_eval = evaluate_strategy(
            test_df,
            "base_score_only",
            cfg.label_column,
            cfg.top_n,
            cfg.stress_round_trip_cost,
            fixed_holdings_col="benchmark_eligible",
        )
        combo_returns = combo_eval["daily_returns"]
        base_returns = base_eval["daily_returns"]
        combo_stress_returns = combo_stress_eval["daily_returns"]
        base_stress_returns = base_stress_eval["daily_returns"]
        if (
            combo_returns.empty
            or base_returns.empty
            or combo_stress_returns.empty
            or base_stress_returns.empty
        ):
            continue

        test_daily_returns.append(combo_returns)
        base_daily_returns.append(base_returns)
        test_daily_returns_stress.append(combo_stress_returns)
        base_daily_returns_stress.append(base_stress_returns)
        combo_daily_turnover.append(combo_eval["daily_turnover"])
        base_daily_turnover.append(base_eval["daily_turnover"])
        split_reports.append(
            {
                "train_start": str(split["train_start"].date()),
                "train_end": str(split["train_end"].date()),
                "test_end": str(split["test_end"].date()),
                "sign": sign,
                "train_rank_ic_stats": train_ic_stats,
                "test_rank_ic_stats": test_ic_stats,
                "combo_metrics": combo_eval["metrics"],
                "base_metrics": base_eval["metrics"],
                "combo_stress_metrics": combo_stress_eval["metrics"],
                "base_stress_metrics": base_stress_eval["metrics"],
            }
        )

    if not test_daily_returns:
        return {
            "factor_id": factor_id,
            "status": "rejected_no_test_result",
            "label_column": cfg.label_column,
            "benchmark_eligible_trade_days": benchmark_trade_days,
            "benchmark_eligible_row_count": benchmark_row_count,
            "orthogonal_corr": orth,
            "rank_ic_mean": rank_ic_mean,
        }

    raw_oos_obs = int(sum(len(s) for s in test_daily_returns))
    combo_series = aggregate_oos_series(test_daily_returns)
    base_series = aggregate_oos_series(base_daily_returns)
    combo_stress_series = aggregate_oos_series(test_daily_returns_stress)
    base_stress_series = aggregate_oos_series(base_daily_returns_stress)
    combo_turnover_series = aggregate_oos_series(combo_daily_turnover)
    base_turnover_series = aggregate_oos_series(base_daily_turnover)
    unique_oos_days = int(len(combo_series))
    overlap_obs = max(0, raw_oos_obs - unique_oos_days)
    overlap_ratio = float(overlap_obs / raw_oos_obs) if raw_oos_obs else 0.0

    combo_metrics = build_metrics_from_series(combo_series, combo_turnover_series)
    base_metrics = build_metrics_from_series(base_series, base_turnover_series)
    combo_stress_metrics = build_metrics_from_series(combo_stress_series, combo_turnover_series)
    base_stress_metrics = build_metrics_from_series(base_stress_series, base_turnover_series)

    lift = {
        "win_rate_lift": combo_metrics["win_rate"] - base_metrics["win_rate"],
        "sharpe_lift": combo_metrics["sharpe"] - base_metrics["sharpe"],
        "drawdown_delta": combo_metrics["max_drawdown"] - base_metrics["max_drawdown"],
    }
    mc = block_bootstrap_metrics(combo_series, cfg.n_sims, cfg.block_size)

    total_splits = len(split_reports)
    split_divisor = max(1, total_splits)
    wf_ic_values = [float((s.get("test_rank_ic_stats") or {}).get("mean", 0.0) or 0.0) for s in split_reports]
    wf_icirs = [float((s.get("test_rank_ic_stats") or {}).get("icir", 0.0) or 0.0) for s in split_reports]
    wf_positive_ratio = float(sum(1 for v in wf_ic_values if v > 0) / split_divisor)
    wf_icir_mean = float(np.mean(wf_icirs)) if wf_icirs else 0.0
    wf_ic_pass = bool(
        wf_positive_ratio >= cfg.min_wf_ic_positive_ratio
        and wf_icir_mean > cfg.min_wf_icir
    )
    split_summary = {
        "total_splits": total_splits,
        "better_sharpe_ratio": float(sum(1 for s in split_reports if s["combo_metrics"]["sharpe"] > s["base_metrics"]["sharpe"]) / split_divisor),
        "better_win_rate_ratio": float(sum(1 for s in split_reports if s["combo_metrics"]["win_rate"] > s["base_metrics"]["win_rate"]) / split_divisor),
        "better_drawdown_ratio": float(sum(1 for s in split_reports if s["combo_metrics"]["max_drawdown"] >= s["base_metrics"]["max_drawdown"]) / split_divisor),
        "wf_ic_positive_ratio": wf_positive_ratio,
        "wf_icir_mean": wf_icir_mean,
        "wf_ic_pass": wf_ic_pass,
    }
    walk_forward_ic_summary = {"wf_ic_values": wf_ic_values, "wf_icirs": wf_icirs, "wf_ic_positive_ratio": wf_positive_ratio, "wf_icir_mean": wf_icir_mean, "wf_ic_pass": wf_ic_pass}
    calmar_lift = combo_metrics["calmar"] - base_metrics["calmar"]
    turnover_pass = combo_metrics["turnover"] <= cfg.max_turnover
    absolute_return_pass = combo_metrics["total_return"] > 0.0
    excess_return_pass = combo_metrics["total_return"] > base_metrics["total_return"]

    signed_icir_pass = bool(float(rank_ic_stats.get("icir", 0.0) or 0.0) > cfg.min_rank_icir)
    strict_pass = (
        signed_icir_pass
        and rank_ic_abs >= cfg.min_rank_ic_abs
        and trim_rank_ic_abs >= cfg.min_rank_ic_abs
        and lift["win_rate_lift"] >= cfg.min_win_rate_lift
        and lift["sharpe_lift"] >= cfg.min_sharpe_lift
        and lift["drawdown_delta"] >= -cfg.max_drawdown_deterioration
        and absolute_return_pass
        and excess_return_pass
        and turnover_pass
    )
    override_pass = (
        signed_icir_pass
        and rank_ic_abs >= cfg.min_rank_ic_abs
        and trim_rank_ic_abs >= cfg.min_rank_ic_abs
        and lift["sharpe_lift"] >= max(cfg.min_sharpe_lift * 10.0, 0.5)
        and mc["mc_sharpe_p05"] > 0.0
        and calmar_lift > 0.30
        and split_summary["better_sharpe_ratio"] >= 0.70
        and split_summary["better_win_rate_ratio"] >= 0.50
        and absolute_return_pass
        and excess_return_pass
        and turnover_pass
    )
    support_pool_candidate = (
        not strict_pass
        and not override_pass
        and signed_icir_pass
        and rank_ic_abs >= cfg.min_rank_ic_abs
        and trim_rank_ic_abs >= cfg.min_rank_ic_abs * 0.65
        and lift["sharpe_lift"] > 0.0
        and calmar_lift > 0.0
        and split_summary["better_sharpe_ratio"] >= 0.60
        and (lift["win_rate_lift"] >= 0.0 or mc["mc_sharpe_p05"] >= 0.0)
        and turnover_pass
    )
    promotion_pass = (
        support_pool_candidate
        and rank_ic_abs >= 0.025
        and trim_rank_ic_abs >= 0.040
        and lift["sharpe_lift"] >= 1.30
        and lift["win_rate_lift"] >= 0.020
        and lift["drawdown_delta"] >= -0.16
        and calmar_lift >= 1.20
        and mc["mc_sharpe_p05"] >= -0.05
        and split_summary["better_sharpe_ratio"] >= 0.75
        and combo_metrics["turnover"] <= 0.95
        and absolute_return_pass
        and excess_return_pass
    )
    passed = strict_pass or override_pass or promotion_pass
    gate_details = {
        "strict_pass": strict_pass,
        "override_pass": override_pass,
        "promotion_pass": promotion_pass,
        "support_pool_candidate": support_pool_candidate,
        "core_signal_pass": bool(signed_icir_pass and rank_ic_abs >= cfg.min_rank_ic_abs and trim_rank_ic_abs >= cfg.min_rank_ic_abs),
        "drawdown_pass": bool(lift["drawdown_delta"] >= -cfg.max_drawdown_deterioration),
        "turnover_pass": bool(turnover_pass),
        "positive_net_absolute_return": bool(absolute_return_pass),
        "positive_net_excess_return": bool(excess_return_pass),
        "icir_pass": signed_icir_pass,
        "pvalue_pass": bool(rank_ic_stats.get("p_value_newey_west", 1.0) <= cfg.max_rank_ic_pvalue_newey_west),
        "wf_ic_pass": bool(walk_forward_ic_summary.get("wf_ic_pass")),
        "noise_pass": bool(noise_test.get("noise_robust_pass")),
        "override_reason": "strong_alpha_calmar_override" if override_pass else None,
        "promotion_reason": "support_pool_upgrade" if promotion_pass else None,
    }
    status = "whitelist" if passed else "cold_storage"

    return {
        "factor_id": factor_id,
        "status": status,
        "label_column": cfg.label_column,
        "benchmark_eligible_trade_days": benchmark_trade_days,
        "benchmark_eligible_row_count": benchmark_row_count,
        "orthogonal_corr": orth,
        "rank_ic_mean": rank_ic_mean,
        "rank_ic_abs": rank_ic_abs,
        "rank_ic_stats": rank_ic_stats,
        "trimmed_rank_ic_abs": trim_rank_ic_abs,
        "trimmed_rank_ic_stats": trimmed_rank_ic_stats,
        "walk_forward_ic_summary": walk_forward_ic_summary,
        "noise_test": noise_test,
        "oos_summary": {
            "raw_observations": raw_oos_obs,
            "unique_trade_days": unique_oos_days,
            "overlap_observations": overlap_obs,
            "overlap_ratio": overlap_ratio,
        },
        "base_metrics": base_metrics,
        "combo_metrics": combo_metrics,
        "stress_cost_metrics": {
            "round_trip_cost": cfg.stress_round_trip_cost,
            "combo_metrics": combo_stress_metrics,
            "base_metrics": base_stress_metrics,
            "net_excess_total_return": (
                combo_stress_metrics["total_return"] - base_stress_metrics["total_return"]
            ),
        },
        "lift": lift,
        "calmar_lift": calmar_lift,
        "monte_carlo": mc,
        "split_summary": split_summary,
        "support_pool_candidate": support_pool_candidate,
        "gate_details": gate_details,
        "split_reports": split_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 1 factor audit")
    parser.add_argument(
        "--root",
        default=str(Path.home() / ".openclaw/workspace/factor_factory"),
        help="factor_factory root",
    )
    parser.add_argument("--factor-ids", default="", help="Comma-separated factor ids to evaluate; empty means all available factors")
    parser.add_argument("--output-suffix", default="", help="Optional suffix for audit/whitelist/coldlist output filenames")
    parser.add_argument("--overlay-manifest", default="", help="Optional run-scoped overlay manifest consumed by the formal audit selection path")
    parser.add_argument("--label-mode", default=DEFAULT_LABEL_MODE, choices=["close_to_close", "open_to_close", "close_to_open"], help="Named return label to evaluate against")
    parser.add_argument("--label-col", default="", help="Explicit label column in panel; when provided it overrides --label-mode")
    parser.add_argument("--progress-log", default="", help="Optional JSONL progress log path")
    args = parser.parse_args()

    cfg = load_runtime_config(Path(args.root).expanduser().resolve())
    cfg = apply_output_suffix(cfg, args.output_suffix)
    cfg.label_column = (args.label_col or "").strip() or resolve_label_column(args.label_mode)
    cfg.selection_overlay = load_selection_overlay(args.overlay_manifest)
    cfg.audit_report_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.whitelist_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.coldlist_path.parent.mkdir(parents=True, exist_ok=True)

    panel = load_panel(cfg)
    if cfg.label_column not in panel.columns:
        raise ValueError(f"label column not found in panel: {cfg.label_column}")
    catalog = load_catalog(cfg)
    progress_log = Path(args.progress_log).expanduser().resolve() if str(args.progress_log or "").strip() else None
    factor_ids = [item["id"] for item in catalog.get("candidate_factors", [])]
    comparison_df = panel.loc[panel["benchmark_date_eligible"] == 1]
    benchmark_rows_df = comparison_df.loc[comparison_df["benchmark_eligible"] == 1]
    splits = build_monthly_splits(panel, cfg)
    trimmed_df = trim_outliers_by_date(panel, cfg.trim_top_pct, cfg.label_column)
    overlay_summary = selection_overlay_summary(cfg.selection_overlay, cfg.top_n)
    resolved_top_n = effective_top_n(cfg.top_n, cfg.selection_overlay)

    reserved = {
        "trade_date", "ts_code", "name", "next_return_1d", "next_return_close_to_close_1d",
        "next_return_open_to_close_1d", "next_return_close_to_open_1d", "base_factor_score_norm",
        "base_factor_score", "base_factor_rank", "base_factor_selected", "base_factor_id",
        "base_factor_score_source", "benchmark_eligible", "benchmark_date_eligible", "base_record_present", "universe_flag",

    }
    available_factor_ids = [f for f in factor_ids if f in panel.columns and f not in reserved]
    requested_factor_ids = [x.strip() for x in str(args.factor_ids or "").split(",") if x.strip()]
    if requested_factor_ids:
        requested_set = set(requested_factor_ids)
        available_factor_ids = [f for f in available_factor_ids if f in requested_set]

    emit_progress(
        progress_log,
        {
            "event": "audit_start",
            "label_mode": args.label_mode,
            "label_column": cfg.label_column,
            "factor_count": len(available_factor_ids),
            "benchmark_eligible_trade_days": int(comparison_df["trade_date"].nunique()),
            "benchmark_eligible_row_count": int(benchmark_rows_df.shape[0]),
            "split_count": len(splits),
            "resolved_top_n": resolved_top_n,
            "selection_overlay": overlay_summary,
            "output_suffix": args.output_suffix or None,
        },
    )

    reports = []
    whitelist = []
    coldlist = []
    total_factors = len(available_factor_ids)
    for idx, factor_id in enumerate(available_factor_ids, start=1):
        started = time.perf_counter()
        emit_progress(
            progress_log,
            {
                "event": "factor_start",
                "index": idx,
                "total": total_factors,
                "factor_id": factor_id,
                "label_mode": args.label_mode,
            },
        )
        report = evaluate_factor(
            panel,
            factor_id,
            cfg,
            comparison_df=comparison_df,
            benchmark_rows_df=benchmark_rows_df,
            splits=splits,
            trimmed_df=trimmed_df,
        )
        reports.append(report)
        emit_progress(
            progress_log,
            {
                "event": "factor_done",
                "index": idx,
                "total": total_factors,
                "factor_id": factor_id,
                "status": report.get("status"),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
        )
        gc.collect()

    whitelist, coldlist = finalize_phase2_status(reports, cfg)

    audit_report = {
        "status": "ok",
        "window": {"start_date": cfg.start_date, "end_date": cfg.end_date},
        "label_mode": args.label_mode,
        "label_column": cfg.label_column,
        "available_label_columns": [
            col for col in [
                "next_return_1d",
                "next_return_close_to_close_1d",
                "next_return_open_to_close_1d",
                "next_return_close_to_open_1d",
            ]
            if col in panel.columns
        ],
        "base_factor_weight": cfg.base_weight,
        "round_trip_cost": cfg.round_trip_cost,
        "stress_round_trip_cost": cfg.stress_round_trip_cost,
        "benchmark_eligible_trade_days": int(panel.loc[panel["benchmark_date_eligible"] == 1, "trade_date"].nunique()),
        "benchmark_eligible_row_count": int(panel["benchmark_eligible"].sum()),
        "factor_count": len(available_factor_ids),
        "requested_factor_ids": requested_factor_ids,
        "resolved_top_n": resolved_top_n,
        "selection_overlay": overlay_summary,
        "output_suffix": args.output_suffix or None,
        "phase2_statistical_gate_config": {
            "enabled": cfg.phase2_gates_enabled,
            "min_rank_icir": cfg.min_rank_icir,
            "signed_direction_required": True,
            "signed_direction_operator": ">",
            "max_rank_ic_pvalue_newey_west": cfg.max_rank_ic_pvalue_newey_west,
            "max_rank_ic_qvalue_fdr": cfg.max_rank_ic_qvalue_fdr,
            "newey_west_lags": cfg.newey_west_lags,
            "min_wf_ic_positive_ratio": cfg.min_wf_ic_positive_ratio,
            "min_wf_icir": cfg.min_wf_icir,
            "noise_n_sims": cfg.noise_n_sims,
            "noise_std_frac": cfg.noise_std_frac,
            "max_noise_decay_pct": cfg.max_noise_decay_pct,
            "max_noise_false_positive_rate": cfg.max_noise_false_positive_rate,
            "fdr_method": "benjamini_hochberg",
        },
        "whitelist_count": len(whitelist),
        "coldlist_count": len(coldlist),
        "reports": reports,
    }
    cfg.audit_report_path.write_text(json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg.whitelist_path.write_text(json.dumps(whitelist, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg.coldlist_path.write_text(json.dumps(coldlist, ensure_ascii=False, indent=2), encoding="utf-8")

    emit_progress(
        progress_log,
        {
            "event": "audit_done",
            "label_mode": args.label_mode,
            "label_column": cfg.label_column,
            "factor_count": len(available_factor_ids),
            "whitelist_count": len(whitelist),
            "coldlist_count": len(coldlist),
            "resolved_top_n": resolved_top_n,
            "selection_overlay": overlay_summary,
            "output_suffix": args.output_suffix or None,
            "audit_report": str(cfg.audit_report_path),
            "whitelist": str(cfg.whitelist_path),
            "coldlist": str(cfg.coldlist_path),
        },
    )

    print(json.dumps({
        "status": "ok",
        "audit_report": str(cfg.audit_report_path),
        "whitelist": str(cfg.whitelist_path),
        "coldlist": str(cfg.coldlist_path),
        "label_mode": args.label_mode,
        "label_column": cfg.label_column,
        "benchmark_eligible_trade_days": int(panel.loc[panel["benchmark_date_eligible"] == 1, "trade_date"].nunique()),
        "benchmark_eligible_row_count": int(panel["benchmark_eligible"].sum()),
        "factor_count": len(available_factor_ids),
        "requested_factor_ids": requested_factor_ids,
        "resolved_top_n": resolved_top_n,
        "selection_overlay": overlay_summary,
        "output_suffix": args.output_suffix or None,
        "phase2_statistical_gate_config": {
            "enabled": cfg.phase2_gates_enabled,
            "min_rank_icir": cfg.min_rank_icir,
            "signed_direction_required": True,
            "signed_direction_operator": ">",
            "max_rank_ic_pvalue_newey_west": cfg.max_rank_ic_pvalue_newey_west,
            "max_rank_ic_qvalue_fdr": cfg.max_rank_ic_qvalue_fdr,
            "newey_west_lags": cfg.newey_west_lags,
            "min_wf_ic_positive_ratio": cfg.min_wf_ic_positive_ratio,
            "min_wf_icir": cfg.min_wf_icir,
            "noise_n_sims": cfg.noise_n_sims,
            "noise_std_frac": cfg.noise_std_frac,
            "max_noise_decay_pct": cfg.max_noise_decay_pct,
            "max_noise_false_positive_rate": cfg.max_noise_false_positive_rate,
            "fdr_method": "benjamini_hochberg",
        },
        "whitelist_count": len(whitelist),
        "coldlist_count": len(coldlist),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
