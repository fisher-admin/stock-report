#!/usr/bin/env python3
"""factor_gauntlet v2 — one referee for every greenfield factor (plan Phase 1 / D5).

Success unit: net excess return over the *investable* equal-weight benchmark, not IC.

Gates (all on Research Panel v2 + D4 labels; holdout >= HOLDOUT_START is locked):
  g1_ic            daily rank IC vs h-day forward excess return, Newey-West t (lag h-1),
                   discovery / validation split and per-year sign stability
  g2_tails         decile and vigintile excess returns; top-tail vs bottom-tail decomposition
  g3_portfolios    P1 avoid-the-losers (drop bottom 30%, EW) and P2 buffered top-100
                   (enter top 15%, exit beyond top 35%, industry cap 15%) vs the investable EW
                   benchmark with the same rebalance, costs and blocked trades
  g4_residual      IC of the factor after cross-sectional regression on baseline styles
                   (size, value, 20d momentum, 5d reversal, turnover, 20d volatility)
  g5_placebo       calendar-block placebo: the factor panel shifted by whole 20-session blocks
                   (circular, per code) keeps its own persistence but loses its timing;
                   real |IC| must exceed placebo mean + 2 sd
  fdr              Benjamini-Hochberg over every *research* test in the append-only registry;
                   p-values are one-sided in the pre-registered direction from Phase 3 on
                   (Phase 2 ledger records keep their original two-sided p)

Registry: outputs/gauntlet_v2/registry.jsonl. A pre-registration record (spec hash, panel and
label hashes, code commit) is written before evaluation; the result record references it.
Changing a definition is a new factor_id.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_labels_v2 as rl  # noqa: E402
import research_panel_v2 as rp  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "outputs/gauntlet_v2"
REGISTRY = OUT / "registry.jsonl"
DISCOVERY = ("20210201", "20241231")
VALIDATION = ("20250101", "20260630")
HOLDOUT_START = "20260701"
FDR_Q = 0.10
TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    family: str
    hypothesis: str
    direction: int = 1          # +1: high factor -> high return
    horizon: int = 5            # 5 or 20 (D4 labels)
    role: str = "research"      # research | calibration | control
    definition: str = ""        # human-readable formula (hashed)

    def spec_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Context:
    panel: pd.DataFrame          # universe rows: ts_code, trade_date, industry, circ_mv, pb, turnover_rate_f
    labels: pd.DataFrame         # aligned 1:1 with panel rows
    sessions: list[str]
    codes: list[str]
    mats: dict[str, np.ndarray]
    s_idx: dict[str, int] = field(default_factory=dict)
    c_idx: dict[str, int] = field(default_factory=dict)
    panel_sha: str = ""
    labels_sha: str = ""
    allow_holdout: bool = False

    def __post_init__(self):
        self.s_idx = {d: i for i, d in enumerate(self.sessions)}
        self.c_idx = {c: i for i, c in enumerate(self.codes)}

    def wide_to_rows(self, wide: np.ndarray) -> np.ndarray:
        t = self.panel["trade_date"].map(self.s_idx).to_numpy()
        j = self.panel["ts_code"].map(self.c_idx).to_numpy()
        return wide[t, j]


def load_context(allow_holdout: bool = False) -> Context:
    panel = pd.read_parquet(DATA / "research_panel_v2.parquet",
                            columns=["ts_code", "trade_date", "industry", "circ_mv", "pb", "turnover_rate_f"])
    labels = pd.read_parquet(DATA / "research_labels_v2.parquet")
    if not (panel["ts_code"].to_numpy() == labels["ts_code"].to_numpy()).all():
        raise RuntimeError("labels are not aligned with the panel")
    z = np.load(DATA / "research_matrices_v2.npz", allow_pickle=False)
    mats = {k: z[k] for k in z.files if k not in ("sessions", "codes")}
    pman = json.loads((DATA / "research_panel_v2.manifest.json").read_text())
    lman = json.loads((DATA / "research_labels_v2.manifest.json").read_text())
    if lman["panel_sha256"] != pman["sha256"]:
        raise RuntimeError("labels were built from a different panel")
    return Context(panel, labels, [str(x) for x in z["sessions"]], [str(x) for x in z["codes"]], mats,
                   panel_sha=pman["sha256"], labels_sha=lman["labels_sha256"], allow_holdout=allow_holdout)


# ---------------------------------------------------------------- statistics

def newey_west_t(x: np.ndarray, lag: int) -> float:
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return float("nan")
    m = x.mean()
    e = x - m
    s = (e @ e) / n
    for k in range(1, lag + 1):
        w = 1 - k / (lag + 1)
        s += 2 * w * (e[k:] @ e[:-k]) / n
    return float(m / math.sqrt(s / n)) if s > 0 else float("nan")


def t_to_p(t: float) -> float:
    """Two-sided normal p-value (n is in the hundreds). Phase 2 ledger records used this."""
    return float(math.erfc(abs(t) / math.sqrt(2))) if np.isfinite(t) else 1.0


def t_to_p_one_sided(t: float) -> float:
    """One-sided p in the pre-registered direction: IC is already direction-adjusted, so only
    t > 0 is evidence for the hypothesis; a significant wrong-sign result gets p near 1."""
    return float(0.5 * math.erfc(t / math.sqrt(2))) if np.isfinite(t) else 1.0


P_SIDED = "one_sided_preregistered_direction"  # since 2026-10-14 (Phase 3); earlier records are two-sided


def bh_fdr(pvals: dict[str, float], q: float = FDR_Q) -> dict[str, bool]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    cutoff = 0
    for i, (_, p) in enumerate(items, start=1):
        if p <= q * i / m:
            cutoff = i
    return {k: (i < cutoff) for i, (k, _) in enumerate(items)}


def daily_rank_ic(dates: np.ndarray, f: np.ndarray, y: np.ndarray, min_n: int = 100) -> pd.Series:
    df = pd.DataFrame({"d": dates, "f": f, "y": y}).dropna()
    df = df[df.groupby("d")["f"].transform("size") >= min_n]
    df["rf"] = df.groupby("d")["f"].rank()
    df["ry"] = df.groupby("d")["y"].rank()
    return df.groupby("d")[["rf", "ry"]].apply(lambda g: g["rf"].corr(g["ry"]))


# ---------------------------------------------------------------- gates

def _mask(ctx: Context, h: int) -> np.ndarray:
    d = ctx.panel["trade_date"].to_numpy()
    keep = np.ones(len(d), dtype=bool) if ctx.allow_holdout else (d < HOLDOUT_START)
    return keep & np.isfinite(ctx.labels[f"fwd_open_{h}"].to_numpy())


def excess(ctx: Context, h: int) -> np.ndarray:
    y = ctx.labels[f"fwd_open_{h}"].to_numpy()
    d = ctx.panel["trade_date"].to_numpy()
    mean = pd.Series(y).groupby(d).transform("mean").to_numpy()
    return y - mean


def g1_ic(ctx: Context, f: np.ndarray, spec: FactorSpec) -> dict[str, Any]:
    h = spec.horizon
    m = _mask(ctx, h)
    d = ctx.panel["trade_date"].to_numpy()
    ic = daily_rank_ic(d[m], spec.direction * f[m], excess(ctx, h)[m])
    out = {"mean_ic": float(ic.mean()), "nw_t": newey_west_t(ic.to_numpy(), h - 1), "days": int(len(ic))}
    for name, (a, b) in (("discovery", DISCOVERY), ("validation", VALIDATION)):
        s = ic[(ic.index >= a) & (ic.index <= b)]
        out[name] = {"mean_ic": float(s.mean()) if len(s) else None, "nw_t": newey_west_t(s.to_numpy(), h - 1),
                     "days": int(len(s))}
    yearly = ic.groupby(ic.index.str[:4]).mean()
    out["yearly_mean_ic"] = {k: round(float(v), 4) for k, v in yearly.items()}
    out["yearly_positive_share"] = float((yearly > 0).mean())
    out["p_value"] = t_to_p_one_sided(out["nw_t"])
    out["p_sided"] = P_SIDED
    out["pass"] = bool(out["nw_t"] >= 2.0 and (out["discovery"]["mean_ic"] or 0) > 0
                       and (out["validation"]["mean_ic"] or 0) > 0)
    return out


def g2_tails(ctx: Context, f: np.ndarray, spec: FactorSpec) -> dict[str, Any]:
    h = spec.horizon
    m = _mask(ctx, h)
    d = ctx.panel["trade_date"].to_numpy()[m]
    df = pd.DataFrame({"d": d, "f": spec.direction * f[m], "x": excess(ctx, h)[m]}).dropna()
    df["q20"] = df.groupby("d")["f"].rank(pct=True).mul(20).clip(upper=19.999).astype(int)
    by = df.groupby("q20")["x"].mean() * 1e4
    dec = df.assign(q10=df["q20"] // 2).groupby("q10")["x"].mean() * 1e4
    bottom30 = df[df.groupby("d")["f"].rank(pct=True) <= 0.30]["x"].mean() * 1e4
    top = float(by.iloc[-1])
    bot = float(by.iloc[0])
    return {"vigintile_excess_bps": {int(k): round(float(v), 2) for k, v in by.items()},
            "decile_excess_bps": {int(k): round(float(v), 2) for k, v in dec.items()},
            "top_vigintile_bps": round(top, 2), "bottom_vigintile_bps": round(bot, 2),
            "bottom30_mean_bps": round(float(bottom30), 2),
            "edge_in_bottom_tail": bool(abs(bot) > abs(top) and bot < 0),
            "decile_monotonicity": float(pd.Series(dec.values).corr(pd.Series(range(len(dec))), method="spearman"))}


def _plan_from_rows(ctx: Context, scores: np.ndarray, rule: Callable, h: int) -> list[tuple[int, dict[int, float]]]:
    dates = sorted(d for d in ctx.panel["trade_date"].unique() if ctx.allow_holdout or d < HOLDOUT_START)
    reb = dates[::h]
    panel = ctx.panel.assign(_s=scores)
    g = {d: x for d, x in panel[panel["trade_date"].isin(set(reb))].groupby("trade_date")}
    plan, prev = [], set()
    for d in reb:
        day = g[d]
        chosen = rule(day, prev)
        prev = set(chosen)
        cols = [ctx.c_idx[c] for c in chosen]
        if cols:
            plan.append((ctx.s_idx[d], {j: 1.0 / len(cols) for j in cols}))
    return plan


def rule_ew_all(day: pd.DataFrame, prev: set) -> list[str]:
    return day["ts_code"].tolist()


def rule_avoid_losers(day: pd.DataFrame, prev: set, drop: float = 0.30) -> list[str]:
    r = day["_s"].rank(pct=True)
    return day.loc[r.isna() | (r > drop), "ts_code"].tolist()


def rule_buffered_topk(day: pd.DataFrame, prev: set, k: int = 100, enter: float = 0.15, exit_: float = 0.35,
                       ind_cap: float = 0.15) -> list[str]:
    d = day.dropna(subset=["_s"]).copy()
    d["pct"] = d["_s"].rank(pct=True, ascending=False)  # 0 = best
    keep = d[d["ts_code"].isin(prev) & (d["pct"] <= exit_)].sort_values("pct")
    cand = d[(d["pct"] <= enter) & ~d["ts_code"].isin(keep["ts_code"])].sort_values("pct")
    cap = max(1, int(math.floor(k * ind_cap)))
    chosen, per_ind = [], {}
    for _, row in pd.concat([keep, cand]).iterrows():
        ind = row.get("industry") or "UNKNOWN"
        if per_ind.get(ind, 0) >= cap:
            continue
        chosen.append(row["ts_code"])
        per_ind[ind] = per_ind.get(ind, 0) + 1
        if len(chosen) >= k:
            break
    return chosen


def summarize_vs_bench(port: pd.DataFrame, bench: pd.DataFrame, h: int) -> dict[str, Any]:
    j = port[["signal_date", "period_ret", "turnover", "cost"]].merge(
        bench[["signal_date", "period_ret"]], on="signal_date", suffixes=("", "_b"))
    j = j.iloc[1:]  # first period carries the initial build-up cost on both sides
    ex = (j["period_ret"] - j["period_ret_b"]).to_numpy()
    per_year = 252 / h
    yearly = pd.Series(ex, index=j["signal_date"].str[:4].to_numpy()).groupby(level=0).sum()
    return {"periods": int(len(j)), "ann_excess_pct": round(float(ex.mean() * per_year * 100), 2),
            "ir": round(float(ex.mean() / ex.std() * math.sqrt(per_year)), 3) if ex.std() > 0 else None,
            "t": round(float(newey_west_t(ex, 1)), 2),
            "ann_ret_pct": round(float(((1 + j["period_ret"]).prod() ** (per_year / len(j)) - 1) * 100), 2),
            "bench_ann_ret_pct": round(float(((1 + j["period_ret_b"]).prod() ** (per_year / len(j)) - 1) * 100), 2),
            "avg_turnover": round(float(j["turnover"].mean()), 3),
            "yearly_excess_pct": {k: round(float(v * 100), 2) for k, v in yearly.items()},
            "years_positive_share": float((yearly > 0).mean())}


def g3_portfolios(ctx: Context, f: np.ndarray, spec: FactorSpec, cost: rl.CostModel | None = None,
                  bench_cache: dict | None = None) -> dict[str, Any]:
    h, cost = spec.horizon, cost or rl.CostModel()
    s = spec.direction * f
    key = (h, asdict(cost).__repr__())
    if bench_cache is not None and key in bench_cache:
        bench = bench_cache[key]
    else:
        bench = rl.portfolio_periods(_plan_from_rows(ctx, s, rule_ew_all, h), ctx.mats, ctx.sessions, h, cost)
        if bench_cache is not None:
            bench_cache[key] = bench
    p1 = rl.portfolio_periods(_plan_from_rows(ctx, s, rule_avoid_losers, h), ctx.mats, ctx.sessions, h, cost)
    p2 = rl.portfolio_periods(_plan_from_rows(ctx, s, rule_buffered_topk, h), ctx.mats, ctx.sessions, h, cost)
    r1, r2 = summarize_vs_bench(p1, bench, h), summarize_vs_bench(p2, bench, h)
    return {"P1_avoid_losers": r1, "P2_buffered_top100": r2,
            "pass": bool((r1["ann_excess_pct"] > 0 and r1["t"] >= 2.0) or (r2["ann_excess_pct"] > 0 and r2["t"] >= 2.0))}


def baseline_styles(ctx: Context) -> pd.DataFrame:
    m = ctx.mats
    ac = pd.DataFrame(m["adj_close"]).ffill()
    r = pd.DataFrame(m["c2c"])
    styles = {
        "size": np.log(ctx.panel["circ_mv"].astype(float).clip(lower=1)).to_numpy(),
        "value": (1.0 / ctx.panel["pb"].astype(float).where(ctx.panel["pb"] > 0)).to_numpy(),
        "turnover": np.log1p(ctx.panel["turnover_rate_f"].astype(float)).to_numpy(),
        "mom20": ctx.wide_to_rows((ac / ac.shift(20) - 1).to_numpy()),
        "rev5": ctx.wide_to_rows(-(ac / ac.shift(5) - 1).to_numpy()),
        "vol20": ctx.wide_to_rows(r.rolling(20, min_periods=15).std().to_numpy()),
    }
    return pd.DataFrame(styles)


def g4_residual(ctx: Context, f: np.ndarray, spec: FactorSpec, styles: pd.DataFrame) -> dict[str, Any]:
    h = spec.horizon
    m = _mask(ctx, h)
    d = ctx.panel["trade_date"].to_numpy()
    X = styles.copy()
    X["f"] = spec.direction * f
    X["d"] = d
    X = X[m].dropna()
    # A factor that *is* a baseline style must not be residualized on itself (circular rejection).
    corr = {c: float(X[[c, "f"]].rank().corr().iloc[0, 1]) for c in styles.columns}
    self_styles = [c for c, r in corr.items() if abs(r) > 0.95]
    X = X.drop(columns=self_styles)

    def resid(g: pd.DataFrame) -> pd.Series:
        Z = g.drop(columns=["f", "d"]).rank(pct=True).to_numpy()
        Z = np.column_stack([np.ones(len(Z)), Z])
        y = g["f"].rank(pct=True).to_numpy()
        beta, *_ = np.linalg.lstsq(Z, y, rcond=None)
        return pd.Series(y - Z @ beta, index=g.index)

    res = X.groupby("d", group_keys=False)[list(X.columns)].apply(resid)
    y = excess(ctx, h)
    ic_raw = daily_rank_ic(X["d"].to_numpy(), X["f"].to_numpy(), y[X.index])
    ic_res = daily_rank_ic(X["d"].to_numpy(), res.reindex(X.index).to_numpy(), y[X.index])
    out = {"raw_ic": float(ic_raw.mean()), "residual_ic": float(ic_res.mean()),
           "styles_excluded_as_self": self_styles,
           "residual_nw_t": newey_west_t(ic_res.to_numpy(), h - 1),
           "retained_share": float(ic_res.mean() / ic_raw.mean()) if ic_raw.mean() else None,
           "rank_corr_with_styles": {c: round(r, 3) for c, r in corr.items()}}
    out["pass"] = bool(out["residual_nw_t"] >= 2.0)
    return out


def g5_placebo(ctx: Context, f_wide: np.ndarray, spec: FactorSpec, n_trials: int = 20, block: int = 20,
               seed: int = 20261014) -> dict[str, Any]:
    h = spec.horizon
    m = _mask(ctx, h)
    d = ctx.panel["trade_date"].to_numpy()
    y = excess(ctx, h)
    real = daily_rank_ic(d[m], spec.direction * ctx.wide_to_rows(f_wide)[m], y[m]).mean()
    rng = np.random.default_rng(seed)
    n = f_wide.shape[0]
    shifts = rng.choice(np.arange(2, max(3, n // block - 1)), size=n_trials, replace=False) * block
    plac = []
    for k in shifts:
        shifted = np.roll(f_wide, int(k), axis=0)
        plac.append(daily_rank_ic(d[m], spec.direction * ctx.wide_to_rows(shifted)[m], y[m]).mean())
    pm, ps = float(np.mean(plac)), float(np.std(plac, ddof=1))
    return {"real_ic": float(real), "placebo_mean": pm, "placebo_sd": ps, "n_trials": n_trials,
            "block_sessions": block, "sigma": float((real - pm) / ps) if ps > 0 else None,
            "pass": bool(real > pm + 2 * ps)}


# ---------------------------------------------------------------- registry

def _append(rec: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with REGISTRY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def registry_records() -> list[dict[str, Any]]:
    if not REGISTRY.exists():
        return []
    return [json.loads(x) for x in REGISTRY.read_text(encoding="utf-8").splitlines() if x.strip()]


def preregister(ctx: Context, spec: FactorSpec) -> str:
    reg_id = f"{spec.factor_id}@{spec.spec_hash()}"
    _append({"kind": "preregistration", "registration_id": reg_id, "at": datetime.now(TZ).isoformat(),
             "spec": asdict(spec), "panel_sha256": ctx.panel_sha, "labels_sha256": ctx.labels_sha,
             "code_commit": rp.git_commit(), "holdout_locked": not ctx.allow_holdout})
    return reg_id


def fdr_status(reg_id: str, p_value: float) -> dict[str, Any]:
    pv = {r["registration_id"]: r["p_value"] for r in registry_records()
          if r.get("kind") == "result" and r.get("role") == "research"}
    pv[reg_id] = p_value
    passed = bh_fdr(pv)
    return {"family_size": len(pv), "q": FDR_Q, "survives": bool(passed.get(reg_id, False))}


def evaluate(ctx: Context, spec: FactorSpec, f_wide: np.ndarray, styles: pd.DataFrame | None = None,
             bench_cache: dict | None = None, portfolios: bool = True) -> dict[str, Any]:
    reg_id = preregister(ctx, spec)
    f = ctx.wide_to_rows(f_wide)
    styles = styles if styles is not None else baseline_styles(ctx)
    res: dict[str, Any] = {"registration_id": reg_id, "spec": asdict(spec)}
    res["g1_ic"] = g1_ic(ctx, f, spec)
    res["g2_tails"] = g2_tails(ctx, f, spec)
    res["g4_residual"] = g4_residual(ctx, f, spec, styles)
    res["g5_placebo"] = g5_placebo(ctx, f_wide, spec)
    if portfolios:
        res["g3_portfolios"] = g3_portfolios(ctx, f, spec, bench_cache=bench_cache)
    p = res["g1_ic"]["p_value"]
    res["fdr"] = fdr_status(reg_id, p) if spec.role == "research" else {"family_size": None, "survives": None,
                                                                         "note": f"role={spec.role}: outside FDR family"}
    gates = ["g1_ic", "g4_residual", "g5_placebo"] + (["g3_portfolios"] if portfolios else [])
    res["verdict"] = "PASS" if all(res[g]["pass"] for g in gates) and res["fdr"].get("survives") in (True, None) \
        else "REJECT"
    res["failed_gates"] = [g for g in gates if not res[g]["pass"]]
    _append({"kind": "result", "registration_id": reg_id, "role": spec.role, "at": datetime.now(TZ).isoformat(),
             "p_value": p, "p_sided": P_SIDED, "verdict": res["verdict"], "failed_gates": res["failed_gates"],
             "summary": {"mean_ic": res["g1_ic"]["mean_ic"], "nw_t": res["g1_ic"]["nw_t"],
                         "residual_t": res["g4_residual"]["residual_nw_t"], "placebo_sigma": res["g5_placebo"]["sigma"]}})
    return res
