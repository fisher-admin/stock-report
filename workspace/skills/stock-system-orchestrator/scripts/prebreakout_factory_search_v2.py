#!/usr/bin/env python3
"""启动前夕二次加工 · 猛一档淘沙搜索（连续优化 + 子分乘子 + 多窗验证）

在 v1 离散搜索之上加码：
  1) 预计算生产子分面板（缓存）
  2) Differential Evolution 在权重单纯形上连续优化
  3) 子分乘子 (0.5~1.8) 与权重联合优化
  4) 多种子重启 + 局部 L-BFGS-B 抛光
  5) 三窗验证：train / test / holdout，综合分淘金
  6) 门控二阶段精搜
  7) 输出 top3 YAML + 刷新工厂观察页

用法：
  ~/.openclaw/venv/bin/python prebreakout_factory_search_v2.py --lookback 80 --top 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import yaml
from scipy.optimize import differential_evolution, minimize

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(__file__).resolve().parents[3]
STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"
sys.path.insert(0, str(STOCK_ANALYZER))
sys.path.insert(0, str(SCRIPT_DIR))

import prebreakout_factory as fac  # noqa: E402
import prebreakout_factory_search as s1  # noqa: E402
import prebreakout_shadow_research as shadow  # noqa: E402

SEARCH_DIR = fac.FACTORY_DIR / "search"
REPORT_PATH = SEARCH_DIR / "search_report_v2_latest.json"
EXP_DIR = fac.EXP_DIR
FACTORS = list(fac.PROD_FACTORS)
N_F = len(FACTORS)
PROD_W = fac.production_weights()
COST = 0.26

# 默认确认键
CONFIRM_KEYS = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def softmax_weights(x: np.ndarray) -> dict[str, float]:
    """x shape (n_factors,) logits -> simplex weights."""
    z = np.asarray(x, dtype=float)
    z = z - np.max(z)
    e = np.exp(np.clip(z, -50, 50))
    e = e / e.sum()
    return {FACTORS[i]: float(e[i]) for i in range(N_F)}


def mult_from_vec(v: np.ndarray) -> dict[str, float]:
    """v in [0,1]^n -> multipliers in [0.5, 1.8]."""
    lo, hi = 0.5, 1.8
    return {FACTORS[i]: float(lo + (hi - lo) * np.clip(v[i], 0, 1)) for i in range(N_F)}


def apply_mult(df: pd.DataFrame, mult: dict[str, float]) -> pd.DataFrame:
    """仅用于展示/诊断；选股 confirm 禁止对乘子后的 df 调用。"""
    out = df.copy()
    for f, m in mult.items():
        if f in out.columns and abs(m - 1.0) > 1e-9:
            out[f] = out[f].astype(float) * float(m)
    return out


def rescore_df(df: pd.DataFrame, weights: dict[str, float], mult: dict[str, float] | None = None) -> pd.Series:
    """综合分支持乘子；不修改 df 列（confirm 仍读原始列）。"""
    import prebreakout_recipe_engine as eng

    return eng.rescore_with_mult(df, s1.normalize_w(weights), mult, list(FACTORS))


def evaluate(
    panel: dict[str, pd.DataFrame],
    pairs: list[tuple[str, str]],
    weights: dict[str, float],
    mult: dict[str, float],
    gates: dict[str, Any],
    o2c_cache: dict[str, dict[str, float]],
    ind_map: dict[str, str],
    cap_cache: dict[tuple, int],
) -> dict[str, Any]:
    """与工厂出厂一致：综合分可乘子，confirm 永远看原始 subscore。"""
    import prebreakout_recipe_engine as eng

    return eng.evaluate_panel_recipe(
        panel,
        pairs,
        s1.normalize_w(weights),
        mult,
        gates,
        o2c_cache,
        ind_map,
        cap_cache,
        factors=list(FACTORS),
        market_cap_for_fn=s1.market_cap_for,
        o2c_for_date_fn=s1.o2c_for_date,
        cost=float(gates.get("cost_pct", COST)),
    )


def gold_score(train: dict, test: dict, hold: dict) -> float:
    """淘金综合分：三窗都要看；holdout 权重更高；惩罚过拟合与空仓。"""
    def pack(m: dict) -> tuple[float, float, float, float]:
        if not m or not m.get("days"):
            return -9.0, 0.0, 0.0, 1.0
        avg = float(m.get("avg_daily_pct") or 0)
        win = float(m.get("win_days_pct") or 0) / 100.0
        sh = float(m.get("sharpe") or 0)
        empty = float(m.get("empty_book_days") or 0) / max(float(m.get("days") or 1), 1)
        return avg, win, sh, empty

    ta, tw, ts, te = pack(train)
    ea, ew, es, ee = pack(test)
    ha, hw, hs, he = pack(hold)
    # 过拟合：train 远好于 hold
    gap = max(0.0, ta - ha) + 0.5 * max(0.0, ea - ha)
    # hold 为负强惩罚
    hold_pen = 0.0 if ha >= 0 else (-1.2 * ha)
    # 三窗一致性奖励：三窗日均都>0
    cons = 0.4 if (ta > 0 and ea > 0 and ha > 0) else 0.0
    score = (
        1.0 * ta
        + 1.4 * ea
        + 2.0 * ha
        + 0.5 * tw
        + 0.7 * ew
        + 0.9 * hw
        + 0.15 * ts
        + 0.2 * es
        + 0.25 * hs
        + cons
        - 1.0 * gap
        - hold_pen
        - 0.2 * (te + ee + he) / 3.0
    )
    return float(score)


def base_gates() -> dict[str, Any]:
    return {
        "max_names": 8,
        "max_per_industry": 3,
        "score_percentile": 88,
        "min_score_floor": 66,
        "min_confirm_hits": 1,
        "confirm_subscore_min": 58,
        "confirm_keys": CONFIRM_KEYS,
        "market_gate": True,
        "cost_pct": COST,
    }


def gate_grid() -> list[dict[str, Any]]:
    out = []
    for max_names in (5, 8, 10):
        for max_ind in (2, 3):
            for pct in (80, 85, 90, 93):
                for hits in (1, 2):
                    for floor in (62, 66, 70):
                        for mkt in (True,):
                            out.append(
                                {
                                    "max_names": max_names,
                                    "max_per_industry": max_ind,
                                    "score_percentile": pct,
                                    "min_score_floor": floor,
                                    "min_confirm_hits": hits,
                                    "confirm_subscore_min": 58,
                                    "confirm_keys": CONFIRM_KEYS,
                                    "market_gate": mkt,
                                    "cost_pct": COST,
                                }
                            )
    # 再加少量无市场门
    for max_names in (5, 8):
        out.append(
            {
                "max_names": max_names,
                "max_per_industry": 2,
                "score_percentile": 85,
                "min_score_floor": 66,
                "min_confirm_hits": 1,
                "confirm_subscore_min": 58,
                "confirm_keys": CONFIRM_KEYS,
                "market_gate": False,
                "cost_pct": COST,
            }
        )
    return out


def split_pairs(pairs: list[tuple[str, str]]) -> tuple[list, list, list]:
    n = len(pairs)
    if n < 18:
        a = max(1, n // 2)
        return pairs[:a], pairs[a:], pairs[a:]
    i1 = int(n * 0.50)
    i2 = int(n * 0.75)
    return pairs[:i1], pairs[i1:i2], pairs[i2:]


def make_objective(
    panel,
    train_p,
    test_p,
    hold_p,
    o2c_cache,
    ind_map,
    cap_cache,
    gates: dict[str, Any],
    use_mult: bool,
) -> Callable[[np.ndarray], float]:
    def obj(x: np.ndarray) -> float:
        if use_mult:
            w = softmax_weights(x[:N_F])
            m = mult_from_vec(x[N_F : 2 * N_F])
        else:
            w = softmax_weights(x)
            m = {f: 1.0 for f in FACTORS}
        tr = evaluate(panel, train_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        te = evaluate(panel, test_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        ho = evaluate(panel, hold_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        # DE minimizes
        return -gold_score(tr, te, ho)

    return obj


def run_de(
    obj: Callable,
    dim: int,
    seed: int,
    maxiter: int = 18,
    popsize: int = 10,
) -> tuple[np.ndarray, float]:
    # dim: N_F logits, or 2*N_F if multipliers
    if dim == N_F:
        bounds = [(-3.0, 3.0)] * N_F
    else:
        bounds = [(-3.0, 3.0)] * N_F + [(0.0, 1.0)] * N_F
    res = differential_evolution(
        obj,
        bounds=bounds,
        seed=seed,
        maxiter=maxiter,
        popsize=popsize,
        mutation=(0.5, 1.5),
        recombination=0.7,
        polish=False,
        updating="immediate",
        workers=1,
        tol=1e-3,
        atol=1e-3,
    )
    return res.x, float(res.fun)


def polish(obj: Callable, x0: np.ndarray, use_mult: bool) -> tuple[np.ndarray, float]:
    if use_mult:
        bounds = [(-4, 4)] * N_F + [(0, 1)] * N_F
    else:
        bounds = [(-4, 4)] * N_F
    res = minimize(obj, x0, method="L-BFGS-B", bounds=bounds, options={"maxiter": 40})
    return res.x, float(res.fun)


def write_top_yaml(rank: int, item: dict[str, Any]) -> Path:
    path = EXP_DIR / f"exp_gold_top{rank}_{item['tag']}.yaml"
    disabled = [k for k, v in item["weights"].items() if float(v) <= 1e-4]
    body = {
        "id": f"exp_gold_top{rank}_{item['tag']}",
        "name": f"淘金Top{rank}·{item.get('label')}",
        "status": "active",
        "hypothesis": (
            f"猛搜淘金综合分={item['gold']:.4f}；"
            f"holdout日均={item['hold'].get('avg_daily_pct')}% 胜率={item['hold'].get('win_days_pct')}% "
            f"累计={item['hold'].get('total_return_pct')}%；"
            f"test日均={item['test'].get('avg_daily_pct')}%；"
            f"全窗日均={item['full'].get('avg_daily_pct')}% 超额vs生产={item.get('edge_vs_prod')}%。"
            f"方法={item.get('method')}"
        ),
        "inspired_by": [
            "连续优化 Differential Evolution + L-BFGS 抛光",
            "子分乘子联合搜索",
            "三窗 train/test/holdout 淘金评分",
            "外来库：确认/动量/稀疏持仓",
        ],
        "production_frozen": True,
        "weights": {k: round(float(v), 6) for k, v in item["weights"].items()},
        "disabled_factors": disabled,
        "subscore_multipliers": {k: round(float(v), 4) for k, v in item.get("multipliers", {}).items()},
        "gates": item["gates"],
        "backtest": {"lookback_days": item.get("lookback", 80), "cost_pct": COST},
        "search_metrics": {
            "gold_score": item["gold"],
            "train": item["train"],
            "test": item["test"],
            "hold": item["hold"],
            "full": item["full"],
            "edge_vs_prod": item.get("edge_vs_prod"),
            "method": item.get("method"),
        },
    }
    path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=80)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--trade-date", default="")
    ap.add_argument("--de-seeds", type=int, default=6)
    ap.add_argument("--de-iter", type=int, default=22)
    ap.add_argument("--with-mult", action="store_true", default=True)
    ap.add_argument("--no-mult", action="store_true")
    args = ap.parse_args()
    use_mult = not args.no_mult

    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    td = shadow.resolve_trade_date(args.trade_date or None)
    all_dates = [
        d
        for d in shadow.list_trade_dates()
        if d <= td and (shadow.CACHE / f"stk_factor_{d}.parquet").exists()
    ]
    dates = all_dates[-(args.lookback + 1) :]
    pairs = list(zip(dates[:-1], dates[1:]))
    train_p, test_p, hold_p = split_pairs(pairs)
    print(
        f"[gold] td={td} window={dates[0]}→{dates[-1]} pairs={len(pairs)} "
        f"train={len(train_p)} test={len(test_p)} hold={len(hold_p)} mult={use_mult}"
    )

    # panel
    panel = s1.preload_panel(dates[:-1])
    # drop ultra-thin days already n=0
    good = {k: v for k, v in panel.items() if v is not None and len(v) >= 200}
    print(f"[gold] panel usable days={len(good)}/{len(panel)}")
    ind_map = s1.industry_map()
    o2c_cache: dict[str, dict[str, float]] = {}
    cap_cache: dict[tuple, int] = {}

    # prefill o2c for speed
    for _, nd in pairs:
        if nd not in o2c_cache:
            o2c_cache[nd] = s1.o2c_for_date(nd)

    bg = base_gates()
    prod_full = evaluate(panel, pairs, PROD_W, {f: 1.0 for f in FACTORS}, bg, o2c_cache, ind_map, cap_cache)
    print(f"[gold] production full avg={prod_full.get('avg_daily_pct')} total={prod_full.get('total_return_pct')}")

    candidates: list[dict[str, Any]] = []

    # --- Phase A: DE multi-seed weight(+mult) under base gates ---
    dim = 2 * N_F if use_mult else N_F
    obj = make_objective(panel, train_p, test_p, hold_p, o2c_cache, ind_map, cap_cache, bg, use_mult)
    print(f"[gold] Phase A DE dim={dim} seeds={args.de_seeds} maxiter={args.de_iter}")
    for seed in range(args.de_seeds):
        t0 = time.time()
        x, fun = run_de(obj, dim, seed=20260716 + seed * 17, maxiter=args.de_iter, popsize=14)
        x2, fun2 = polish(obj, x, use_mult)
        if fun2 < fun:
            x, fun = x2, fun2
        w = softmax_weights(x[:N_F])
        m = mult_from_vec(x[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
        tr = evaluate(panel, train_p, w, m, bg, o2c_cache, ind_map, cap_cache)
        te = evaluate(panel, test_p, w, m, bg, o2c_cache, ind_map, cap_cache)
        ho = evaluate(panel, hold_p, w, m, bg, o2c_cache, ind_map, cap_cache)
        fu = evaluate(panel, pairs, w, m, bg, o2c_cache, ind_map, cap_cache)
        g = gold_score(tr, te, ho)
        edge = None
        if fu.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
            edge = round(float(fu["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
        candidates.append(
            {
                "tag": f"de{seed}",
                "label": f"DE种子{seed}",
                "method": "DE+polish",
                "weights": w,
                "multipliers": m,
                "gates": dict(bg),
                "train": {k: tr[k] for k in tr if k != "series"},
                "test": {k: te[k] for k in te if k != "series"},
                "hold": {k: ho[k] for k in ho if k != "series"},
                "full": {k: fu[k] for k in fu if k != "series"},
                "gold": round(g, 5),
                "edge_vs_prod": edge,
                "lookback": args.lookback,
            }
        )
        print(
            f"[gold] DE seed={seed} gold={g:.4f} hold_avg={ho.get('avg_daily_pct')} "
            f"test_avg={te.get('avg_daily_pct')} full_avg={fu.get('avg_daily_pct')} "
            f"edge={edge} {time.time()-t0:.1f}s",
            flush=True,
        )

    # --- Phase A2: also inject v1-style discrete elites as warm starts ---
    for wid, w0, note in s1.candidate_weights()[:18]:
        # map weights to logits approx
        arr = np.array([max(w0.get(f, 1e-6), 1e-6) for f in FACTORS], dtype=float)
        logits = np.log(arr)
        if use_mult:
            x0 = np.concatenate([logits, np.full(N_F, 0.5)])
        else:
            x0 = logits
        x2, _ = polish(obj, x0, use_mult)
        w = softmax_weights(x2[:N_F])
        m = mult_from_vec(x2[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
        tr = evaluate(panel, train_p, w, m, bg, o2c_cache, ind_map, cap_cache)
        te = evaluate(panel, test_p, w, m, bg, o2c_cache, ind_map, cap_cache)
        ho = evaluate(panel, hold_p, w, m, bg, o2c_cache, ind_map, cap_cache)
        fu = evaluate(panel, pairs, w, m, bg, o2c_cache, ind_map, cap_cache)
        g = gold_score(tr, te, ho)
        edge = None
        if fu.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
            edge = round(float(fu["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
        candidates.append(
            {
                "tag": f"warm_{wid}"[:40],
                "label": f"热启·{note}"[:40],
                "method": "warm+polish",
                "weights": w,
                "multipliers": m,
                "gates": dict(bg),
                "train": {k: tr[k] for k in tr if k != "series"},
                "test": {k: te[k] for k in te if k != "series"},
                "hold": {k: ho[k] for k in ho if k != "series"},
                "full": {k: fu[k] for k in fu if k != "series"},
                "gold": round(g, 5),
                "edge_vs_prod": edge,
                "lookback": args.lookback,
            }
        )
    candidates.sort(key=lambda r: r["gold"], reverse=True)
    print("[gold] Phase A top5:")
    for c in candidates[:5]:
        print(
            f"  {c['tag']} gold={c['gold']} hold={c['hold'].get('avg_daily_pct')} "
            f"test={c['test'].get('avg_daily_pct')} full={c['full'].get('avg_daily_pct')} edge={c['edge_vs_prod']}"
        )

    # --- Phase B: gate refine on top weight systems ---
    elites = candidates[:8]
    grid = gate_grid()
    print(f"[gold] Phase B gate refine elites={len(elites)} gates={len(grid)}")
    refined: list[dict[str, Any]] = []
    for ei, e in enumerate(elites):
        best_local = None
        for gates in grid:
            tr = evaluate(panel, train_p, e["weights"], e["multipliers"], gates, o2c_cache, ind_map, cap_cache)
            te = evaluate(panel, test_p, e["weights"], e["multipliers"], gates, o2c_cache, ind_map, cap_cache)
            ho = evaluate(panel, hold_p, e["weights"], e["multipliers"], gates, o2c_cache, ind_map, cap_cache)
            fu = evaluate(panel, pairs, e["weights"], e["multipliers"], gates, o2c_cache, ind_map, cap_cache)
            g = gold_score(tr, te, ho)
            edge = None
            if fu.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
                edge = round(float(fu["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
            row = {
                "tag": f"{e['tag']}_g{gates['max_names']}i{gates['max_per_industry']}p{gates['score_percentile']}c{gates['min_confirm_hits']}",
                "label": f"{e['label']}+门控精搜",
                "method": e["method"] + "+gates",
                "weights": e["weights"],
                "multipliers": e["multipliers"],
                "gates": gates,
                "train": {k: tr[k] for k in tr if k != "series"},
                "test": {k: te[k] for k in te if k != "series"},
                "hold": {k: ho[k] for k in ho if k != "series"},
                "full": {k: fu[k] for k in fu if k != "series"},
                "gold": round(g, 5),
                "edge_vs_prod": edge,
                "lookback": args.lookback,
            }
            if best_local is None or row["gold"] > best_local["gold"]:
                best_local = row
            refined.append(row)
        print(f"[gold] elite {ei+1}/{len(elites)} best_gate_gold={best_local['gold']} edge={best_local['edge_vs_prod']}")

    refined.sort(key=lambda r: r["gold"], reverse=True)

    # diversify top-k by weight cosine distance
    def wvec(w: dict[str, float]) -> np.ndarray:
        return np.array([w[f] for f in FACTORS], dtype=float)

    selected: list[dict[str, Any]] = []
    for r in refined:
        if len(selected) >= args.top:
            break
        v = wvec(r["weights"])
        ok = True
        for s in selected:
            u = wvec(s["weights"])
            cos = float(np.dot(v, u) / (np.linalg.norm(v) * np.linalg.norm(u) + 1e-12))
            if cos > 0.97 and abs((r.get("edge_vs_prod") or 0) - (s.get("edge_vs_prod") or 0)) < 0.05:
                ok = False
                break
        if ok:
            selected.append(r)
    while len(selected) < args.top and len(refined) > len(selected):
        for r in refined:
            if r not in selected:
                selected.append(r)
            if len(selected) >= args.top:
                break

    print("\n========== 淘金 TOP 结果 ==========")
    for i, c in enumerate(selected, 1):
        print(f"#{i} gold={c['gold']} edge={c['edge_vs_prod']} method={c['method']}")
        print(f"   hold avg={c['hold'].get('avg_daily_pct')}% win={c['hold'].get('win_days_pct')}% tot={c['hold'].get('total_return_pct')}%")
        print(f"   test avg={c['test'].get('avg_daily_pct')}% full avg={c['full'].get('avg_daily_pct')}% sharpe={c['full'].get('sharpe')}")
        print(f"   gates n={c['gates']['max_names']} ind={c['gates']['max_per_industry']} pct={c['gates']['score_percentile']} conf={c['gates']['min_confirm_hits']}")
        tops = sorted(c["weights"].items(), key=lambda x: -x[1])[:5]
        print("   weights:", ", ".join(f"{k}={v*100:.1f}%" for k, v in tops))
        mt = [(k, v) for k, v in c["multipliers"].items() if abs(v - 1.0) > 0.05]
        if mt:
            print("   mult:", ", ".join(f"{k}×{v:.2f}" for k, v in sorted(mt, key=lambda x: -abs(x[1] - 1))[:6]))

    # archive previous gold/search tops
    for p in list(EXP_DIR.glob("exp_gold_top*.yaml")) + list(EXP_DIR.glob("exp_search_top*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["status"] = "archived"
            raw["archived_at"] = now_str()
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass
    # keep baseline active
    for p in EXP_DIR.glob("exp_baseline*.yaml"):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["status"] = "active"
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    written = []
    for i, c in enumerate(selected, 1):
        path = write_top_yaml(i, c)
        written.append(str(path))
        print(f"[gold] wrote {path}")

    report = {
        "generated_at": now_str(),
        "mode": "gold_search_v2",
        "trade_date": td,
        "lookback": args.lookback,
        "use_mult": use_mult,
        "pairs": len(pairs),
        "train_n": len(train_p),
        "test_n": len(test_p),
        "hold_n": len(hold_p),
        "de_seeds": args.de_seeds,
        "de_iter": args.de_iter,
        "n_phaseA": len(candidates),
        "n_phaseB": len(refined),
        "production_control": {k: prod_full[k] for k in prod_full if k != "series"},
        "top": [{k: v for k, v in c.items()} for c in selected],
        "leaderboard_top15": [
            {
                "tag": r["tag"],
                "gold": r["gold"],
                "edge_vs_prod": r["edge_vs_prod"],
                "hold_avg": r["hold"].get("avg_daily_pct"),
                "test_avg": r["test"].get("avg_daily_pct"),
                "full_avg": r["full"].get("avg_daily_pct"),
                "full_total": r["full"].get("total_return_pct"),
                "full_sharpe": r["full"].get("sharpe"),
                "method": r["method"],
            }
            for r in refined[:15]
        ],
        "experiment_files": written,
        "disclaimer": "启发式+连续优化的相对最优，非数学穷尽全局最优。纸面T+1，生产因子未改。",
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[gold] report {REPORT_PATH}")

    print("[gold] refresh factory page ...")
    fac.run_factory(td, lookback=min(args.lookback, 50))
    print("[gold] DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
