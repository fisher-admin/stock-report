#!/usr/bin/env python3
"""启动前夕二次加工 · 淘沙猛档 v3

在 v2 之上再猛一档，目标：沙子里淘出更稳的真金（生产仍冻结）。

加码点：
  1) 更长 lookback（默认 100）
  2) 更深 DE：多种子 × 更大 pop × 更多迭代
  3) 更宽子分乘子 [0.25, 2.2]（允许软消融）
  4) 注入 v2 真金 + 离散精英作热启
  5) 因子消融（硬清零后抛光）
  6) 扩大门控网格 + 确认键子集搜索
  7) 多窗稳定性复评：3 个 holdout 切片，防单窗运气
  8) 邻域 DE 精搜 Top 精英
  9) 综合分 = 主窗淘金 + 稳定性奖励

用法：
  PYTHONUNBUFFERED=1 ~/.openclaw/venv/bin/python prebreakout_factory_search_v3.py \\
    --lookback 100 --de-seeds 10 --de-iter 26 --top 3
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml
from scipy.optimize import differential_evolution, minimize

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(__file__).resolve().parents[3]
STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"
sys.path.insert(0, str(STOCK_ANALYZER))
sys.path.insert(0, str(SCRIPT_DIR))

import prebreakout_factory as fac  # noqa: E402
import prebreakout_factory_search as s1  # noqa: E402
import prebreakout_factory_search_v2 as v2  # noqa: E402
import prebreakout_shadow_research as shadow  # noqa: E402

SEARCH_DIR = fac.FACTORY_DIR / "search"
REPORT_PATH = SEARCH_DIR / "search_report_v3_latest.json"
EXP_DIR = fac.EXP_DIR
FACTORS = list(fac.PROD_FACTORS)
N_F = len(FACTORS)
PROD_W = fac.production_weights()
COST = 0.26
CONFIRM_KEYS = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"]
# 猛档乘子范围：允许更狠压制/放大
MULT_LO, MULT_HI = 0.25, 2.2


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def softmax_weights(x: np.ndarray) -> dict[str, float]:
    z = np.asarray(x, dtype=float)
    z = z - np.max(z)
    e = np.exp(np.clip(z, -50, 50))
    e = e / e.sum()
    return {FACTORS[i]: float(e[i]) for i in range(N_F)}


def mult_from_vec(v: np.ndarray) -> dict[str, float]:
    return {FACTORS[i]: float(MULT_LO + (MULT_HI - MULT_LO) * np.clip(v[i], 0, 1)) for i in range(N_F)}


def pack_vec(w: dict[str, float], m: dict[str, float] | None, use_mult: bool) -> np.ndarray:
    arr = np.array([max(float(w.get(f, 1e-6)), 1e-6) for f in FACTORS], dtype=float)
    logits = np.log(arr)
    if not use_mult:
        return logits
    mv = []
    for f in FACTORS:
        val = float((m or {}).get(f, 1.0))
        t = (val - MULT_LO) / (MULT_HI - MULT_LO)
        mv.append(float(np.clip(t, 0, 1)))
    return np.concatenate([logits, np.array(mv, dtype=float)])


def evaluate(panel, pairs, weights, mult, gates, o2c_cache, ind_map, cap_cache):
    return v2.evaluate(panel, pairs, weights, mult, gates, o2c_cache, ind_map, cap_cache)


def gold_score(train: dict, test: dict, hold: dict) -> float:
    return v2.gold_score(train, test, hold)


def stability_bonus(fold_holds: list[dict]) -> float:
    """多窗 hold 稳定性：均值 + 一致为正 - 波动惩罚。"""
    avgs = []
    for h in fold_holds:
        if h and h.get("avg_daily_pct") is not None:
            avgs.append(float(h["avg_daily_pct"]))
    if not avgs:
        return -2.0
    mean = float(np.mean(avgs))
    std = float(np.std(avgs)) if len(avgs) > 1 else 0.0
    pos = sum(1 for a in avgs if a > 0) / len(avgs)
    return 0.9 * mean + 0.8 * pos - 0.35 * std


def composite_gold(train, test, hold, fold_holds: list[dict] | None = None) -> float:
    g = gold_score(train, test, hold)
    if fold_holds:
        g = g + 0.85 * stability_bonus(fold_holds)
    return float(g)


def base_gates() -> dict[str, Any]:
    return {
        "max_names": 8,
        "max_per_industry": 3,
        "score_percentile": 88,
        "min_score_floor": 66,
        "min_confirm_hits": 1,
        "confirm_subscore_min": 58,
        "confirm_keys": list(CONFIRM_KEYS),
        "market_gate": True,
        "cost_pct": COST,
    }


def confirm_key_sets() -> list[list[str]]:
    base = CONFIRM_KEYS
    sets = [list(base)]
    # 子集 3 键
    for comb in itertools.combinations(base, 3):
        sets.append(list(comb))
    # 加动量/流动性作确认
    sets.append(["macd_early_signal", "volume_warmup", "momentum", "liquidity"])
    sets.append(["volume_warmup", "chip_support", "liquidity", "kdj_position"])
    sets.append(["macd_early_signal", "turnover_stability", "rsi_volume", "momentum"])
    # 去重
    seen = set()
    out = []
    for s in sets:
        key = tuple(sorted(s))
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def gate_grid_v3(full: bool = True) -> list[dict[str, Any]]:
    """扩大门控：比 v2 更密 + 确认键变体；控制在 ~280 量级以免一夜跑不完。"""
    out: list[dict[str, Any]] = []
    if full:
        # 主网格：比 v2 更密（+n=3/4、pct=75/88），但 floor 只取关键档
        for max_names in (3, 4, 5, 6, 8, 10):
            for max_ind in (1, 2, 3):
                if max_ind > max_names:
                    continue
                for pct in (75, 80, 85, 88, 90, 93):
                    for hits in (1, 2):
                        floor = 62 if max_names <= 4 else 66
                        out.append(
                            {
                                "max_names": max_names,
                                "max_per_industry": max_ind,
                                "score_percentile": pct,
                                "min_score_floor": floor,
                                "min_confirm_hits": hits,
                                "confirm_subscore_min": 58,
                                "confirm_keys": list(CONFIRM_KEYS),
                                "market_gate": True,
                                "cost_pct": COST,
                            }
                        )
        # 补少量高 floor
        for max_names in (5, 8):
            for pct in (80, 85, 90):
                out.append(
                    {
                        "max_names": max_names,
                        "max_per_industry": 2,
                        "score_percentile": pct,
                        "min_score_floor": 70,
                        "min_confirm_hits": 1,
                        "confirm_subscore_min": 58,
                        "confirm_keys": list(CONFIRM_KEYS),
                        "market_gate": True,
                        "cost_pct": COST,
                    }
                )
        # 无市场门
        for max_names in (4, 5, 8):
            for pct in (80, 85):
                out.append(
                    {
                        "max_names": max_names,
                        "max_per_industry": 2,
                        "score_percentile": pct,
                        "min_score_floor": 66,
                        "min_confirm_hits": 1,
                        "confirm_subscore_min": 58,
                        "confirm_keys": list(CONFIRM_KEYS),
                        "market_gate": False,
                        "cost_pct": COST,
                    }
                )
        # 确认键变体 × 稀疏热点（v2 没有这块）
        conf_sets = confirm_key_sets()
        for ckeys in conf_sets[1:]:
            for max_names in (4, 5, 8):
                for pct in (80, 85):
                    for hits in (1, 2):
                        out.append(
                            {
                                "max_names": max_names,
                                "max_per_industry": 2 if max_names <= 5 else 3,
                                "score_percentile": pct,
                                "min_score_floor": 66,
                                "min_confirm_hits": hits,
                                "confirm_subscore_min": 58,
                                "confirm_keys": list(ckeys),
                                "market_gate": True,
                                "cost_pct": COST,
                            }
                        )
    else:
        out = [base_gates()]
    seen = set()
    uniq = []
    for g in out:
        key = json.dumps(g, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            uniq.append(g)
    return uniq


def split_pairs(pairs: list[tuple[str, str]]) -> tuple[list, list, list]:
    return v2.split_pairs(pairs)


def multi_holdout_folds(pairs: list[tuple[str, str]]) -> list[tuple[list, list, list]]:
    """3 套时间切分：标准 / 后移 hold / 前中后三等分。"""
    n = len(pairs)
    folds = []
    # fold0: 50/25/25
    folds.append(split_pairs(pairs))
    # fold1: 40/30/30
    if n >= 24:
        i1, i2 = int(n * 0.40), int(n * 0.70)
        folds.append((pairs[:i1], pairs[i1:i2], pairs[i2:]))
    # fold2: 60/20/20（更长 train）
    if n >= 24:
        i1, i2 = int(n * 0.60), int(n * 0.80)
        folds.append((pairs[:i1], pairs[i1:i2], pairs[i2:]))
    return folds


def make_objective(
    panel,
    train_p,
    test_p,
    hold_p,
    o2c_cache,
    ind_map,
    cap_cache,
    gates,
    use_mult,
    fast: bool = True,
):
    """fast=True：DE 内层只用 train+hold（跳过 test），抛光后再全量评分。"""

    def obj(x: np.ndarray) -> float:
        if use_mult:
            w = softmax_weights(x[:N_F])
            m = mult_from_vec(x[N_F : 2 * N_F])
        else:
            w = softmax_weights(x)
            m = {f: 1.0 for f in FACTORS}
        tr = evaluate(panel, train_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        ho = evaluate(panel, hold_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        if fast:
            # 用 hold 同时充当 test 代理，压低过拟合但仍只付 2 窗成本
            return -gold_score(tr, ho, ho)
        te = evaluate(panel, test_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        return -gold_score(tr, te, ho)

    return obj


def run_de(obj, dim, seed, maxiter=26, popsize=16, bounds=None) -> tuple[np.ndarray, float]:
    if bounds is None:
        if dim == N_F:
            bounds = [(-3.5, 3.5)] * N_F
        else:
            bounds = [(-3.5, 3.5)] * N_F + [(0.0, 1.0)] * N_F
    res = differential_evolution(
        obj,
        bounds=bounds,
        seed=seed,
        maxiter=maxiter,
        popsize=popsize,
        mutation=(0.4, 1.6),
        recombination=0.75,
        polish=False,
        updating="immediate",
        workers=1,
        tol=5e-4,
        atol=5e-4,
    )
    return res.x, float(res.fun)


def polish(obj, x0, use_mult, maxiter=60) -> tuple[np.ndarray, float]:
    if use_mult:
        bounds = [(-5, 5)] * N_F + [(0, 1)] * N_F
    else:
        bounds = [(-5, 5)] * N_F
    res = minimize(obj, x0, method="L-BFGS-B", bounds=bounds, options={"maxiter": maxiter})
    return res.x, float(res.fun)


def score_candidate(
    panel,
    pairs,
    train_p,
    test_p,
    hold_p,
    folds,
    w,
    m,
    gates,
    o2c_cache,
    ind_map,
    cap_cache,
    prod_full,
    tag,
    label,
    method,
    lookback,
    with_stability=True,
) -> dict[str, Any]:
    tr = evaluate(panel, train_p, w, m, gates, o2c_cache, ind_map, cap_cache)
    te = evaluate(panel, test_p, w, m, gates, o2c_cache, ind_map, cap_cache)
    ho = evaluate(panel, hold_p, w, m, gates, o2c_cache, ind_map, cap_cache)
    fu = evaluate(panel, pairs, w, m, gates, o2c_cache, ind_map, cap_cache)
    fold_holds = []
    fold_details = []
    if with_stability:
        for fi, (trf, tef, hof) in enumerate(folds):
            hh = evaluate(panel, hof, w, m, gates, o2c_cache, ind_map, cap_cache)
            fold_holds.append(hh)
            fold_details.append(
                {
                    "fold": fi,
                    "hold_avg": hh.get("avg_daily_pct"),
                    "hold_win": hh.get("win_days_pct"),
                    "hold_total": hh.get("total_return_pct"),
                    "hold_days": hh.get("days"),
                }
            )
    g = composite_gold(tr, te, ho, fold_holds if with_stability else None)
    edge = None
    if fu.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
        edge = round(float(fu["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
    return {
        "tag": tag[:48],
        "label": label[:48],
        "method": method,
        "weights": w,
        "multipliers": m,
        "gates": gates,
        "train": {k: tr[k] for k in tr if k != "series"},
        "test": {k: te[k] for k in te if k != "series"},
        "hold": {k: ho[k] for k in ho if k != "series"},
        "full": {k: fu[k] for k in fu if k != "series"},
        "folds": fold_details,
        "stability": round(stability_bonus(fold_holds), 4) if fold_holds else None,
        "gold": round(g, 5),
        "edge_vs_prod": edge,
        "lookback": lookback,
    }


def load_prev_gold_warmstarts() -> list[tuple[str, dict, dict, str]]:
    """从现有 gold yaml / search report 注入。"""
    out = []
    for p in sorted(EXP_DIR.glob("exp_gold_top*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            w = raw.get("weights") or {}
            m = raw.get("subscore_multipliers") or {f: 1.0 for f in FACTORS}
            if w:
                out.append((f"prev_{p.stem[-20:]}", w, m, f"继承{p.name}"))
        except Exception:
            pass
    for rep_name in ("search_report_v2_latest.json", "search_report_latest.json"):
        rp = SEARCH_DIR / rep_name
        if not rp.exists():
            continue
        try:
            rep = json.loads(rp.read_text(encoding="utf-8"))
            for i, t in enumerate(rep.get("top") or []):
                w = t.get("weights") or {}
                m = t.get("multipliers") or {f: 1.0 for f in FACTORS}
                if w:
                    out.append((f"rep_{rep_name[:6]}_{i}", w, m, f"报告热启{i}"))
        except Exception:
            pass
    return out


def write_top_yaml(rank: int, item: dict[str, Any]) -> Path:
    path = EXP_DIR / f"exp_gold_top{rank}_{item['tag']}.yaml"
    disabled = [k for k, v in item["weights"].items() if float(v) <= 1e-4]
    soft_drop = [k for k, v in item.get("multipliers", {}).items() if float(v) <= 0.40]
    body = {
        "id": f"exp_gold_top{rank}_{item['tag']}",
        "name": f"淘沙真金Top{rank}·{item.get('label')}",
        "status": "active",
        "hypothesis": (
            f"v3猛档综合分={item['gold']:.4f} 稳定性={item.get('stability')}；"
            f"holdout日均={item['hold'].get('avg_daily_pct')}% 胜率={item['hold'].get('win_days_pct')}% "
            f"累计={item['hold'].get('total_return_pct')}%；"
            f"test日均={item['test'].get('avg_daily_pct')}%；"
            f"全窗日均={item['full'].get('avg_daily_pct')}% 超额vs生产={item.get('edge_vs_prod')}%。"
            f"方法={item.get('method')}"
        ),
        "inspired_by": [
            "v3 淘沙猛档：深DE + 宽乘子 + 消融 + 确认键 + 多窗稳定性 + 邻域精搜",
            "连续优化 Differential Evolution + L-BFGS 抛光",
            "生产 PREBREAKOUT 冻结",
        ],
        "production_frozen": True,
        "weights": {k: round(float(v), 6) for k, v in item["weights"].items()},
        "disabled_factors": disabled,
        "soft_drop_factors": soft_drop,
        "subscore_multipliers": {k: round(float(v), 4) for k, v in item.get("multipliers", {}).items()},
        "gates": item["gates"],
        "backtest": {"lookback_days": item.get("lookback", 100), "cost_pct": COST},
        "search_metrics": {
            "gold_score": item["gold"],
            "stability": item.get("stability"),
            "folds": item.get("folds"),
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
    ap.add_argument("--lookback", type=int, default=100)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--trade-date", default="")
    ap.add_argument("--de-seeds", type=int, default=8)
    ap.add_argument("--de-iter", type=int, default=20)
    ap.add_argument("--de-pop", type=int, default=12)
    ap.add_argument("--elites", type=int, default=10)
    ap.add_argument("--no-mult", action="store_true")
    ap.add_argument("--skip-ablation", action="store_true")
    ap.add_argument("--skip-neighborhood", action="store_true")
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
    folds = multi_holdout_folds(pairs)
    print(
        f"[v3] td={td} window={dates[0]}→{dates[-1]} pairs={len(pairs)} "
        f"train={len(train_p)} test={len(test_p)} hold={len(hold_p)} folds={len(folds)} "
        f"mult=[{MULT_LO},{MULT_HI}] seeds={args.de_seeds} iter={args.de_iter} pop={args.de_pop}",
        flush=True,
    )

    panel = s1.preload_panel(dates[:-1])
    good = {k: v for k, v in panel.items() if v is not None and len(v) >= 200}
    print(f"[v3] panel usable days={len(good)}/{len(panel)}", flush=True)
    ind_map = s1.industry_map()
    o2c_cache: dict[str, dict[str, float]] = {}
    cap_cache: dict[tuple, int] = {}
    for _, nd in pairs:
        if nd not in o2c_cache:
            o2c_cache[nd] = s1.o2c_for_date(nd)

    bg = base_gates()
    prod_full = evaluate(panel, pairs, PROD_W, {f: 1.0 for f in FACTORS}, bg, o2c_cache, ind_map, cap_cache)
    print(
        f"[v3] production full avg={prod_full.get('avg_daily_pct')} "
        f"total={prod_full.get('total_return_pct')} sharpe={prod_full.get('sharpe')}",
        flush=True,
    )

    candidates: list[dict[str, Any]] = []
    dim = 2 * N_F if use_mult else N_F
    obj = make_objective(panel, train_p, test_p, hold_p, o2c_cache, ind_map, cap_cache, bg, use_mult)

    # ========== Phase A: Deep DE ==========
    print(f"[v3] Phase A deep DE dim={dim}", flush=True)
    for seed in range(args.de_seeds):
        t0 = time.time()
        x, fun = run_de(obj, dim, seed=20260716 + seed * 31 + 7, maxiter=args.de_iter, popsize=args.de_pop)
        x2, fun2 = polish(obj, x, use_mult)
        if fun2 < fun:
            x, fun = x2, fun2
        w = softmax_weights(x[:N_F])
        m = mult_from_vec(x[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
        row = score_candidate(
            panel, pairs, train_p, test_p, hold_p, folds, w, m, bg,
            o2c_cache, ind_map, cap_cache, prod_full,
            tag=f"de{seed}", label=f"DE深搜种子{seed}", method="DE+polish",
            lookback=args.lookback, with_stability=True,
        )
        candidates.append(row)
        print(
            f"[v3] DE seed={seed} gold={row['gold']:.4f} stab={row['stability']} "
            f"hold={row['hold'].get('avg_daily_pct')} test={row['test'].get('avg_daily_pct')} "
            f"full={row['full'].get('avg_daily_pct')} edge={row['edge_vs_prod']} "
            f"{time.time()-t0:.1f}s",
            flush=True,
        )

    # ========== Phase A2: warm starts (v1 discrete + prev gold) ==========
    print("[v3] Phase A2 warm starts", flush=True)
    warm_items: list[tuple[str, dict, dict | None, str]] = []
    for wid, w0, note in s1.candidate_weights()[:28]:
        warm_items.append((f"warm_{wid}"[:36], w0, None, f"热启·{note}"[:40]))
    for wid, w0, m0, note in load_prev_gold_warmstarts():
        warm_items.append((wid[:36], w0, m0, note[:40]))

    seen_w = set()
    for tag, w0, m0, note in warm_items:
        key = tuple(round(float(w0.get(f, 0)), 4) for f in FACTORS)
        if key in seen_w:
            continue
        seen_w.add(key)
        x0 = pack_vec(w0, m0, use_mult)
        x2, _ = polish(obj, x0, use_mult)
        w = softmax_weights(x2[:N_F])
        m = mult_from_vec(x2[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
        row = score_candidate(
            panel, pairs, train_p, test_p, hold_p, folds, w, m, bg,
            o2c_cache, ind_map, cap_cache, prod_full,
            tag=tag, label=note, method="warm+polish",
            lookback=args.lookback, with_stability=True,
        )
        candidates.append(row)

    candidates.sort(key=lambda r: r["gold"], reverse=True)
    print("[v3] Phase A top6:", flush=True)
    for c in candidates[:6]:
        print(
            f"  {c['tag']} gold={c['gold']} stab={c['stability']} "
            f"hold={c['hold'].get('avg_daily_pct')} full={c['full'].get('avg_daily_pct')} edge={c['edge_vs_prod']}",
            flush=True,
        )

    # ========== Phase A3: factor ablation on top elites ==========
    if not args.skip_ablation:
        print("[v3] Phase A3 factor ablation", flush=True)
        abl_base = candidates[:6]
        drop_sets = [
            ["kdj_position"],
            ["liquidity"],
            ["rsi_volume"],
            ["volatility_squeeze"],
            ["chip_concentration"],
            ["kdj_position", "rsi_volume"],
            ["volatility_squeeze", "chip_concentration"],
            ["liquidity", "kdj_position"],
            ["momentum"],
            ["turnover_stability"],
        ]
        for bi, base in enumerate(abl_base):
            for drops in drop_sets:
                w0 = dict(base["weights"])
                for d in drops:
                    w0[d] = 1e-8
                # renorm via logits polish with soft mult from base
                x0 = pack_vec(w0, base["multipliers"], use_mult)
                # hard zero: after polish, re-zero and renorm
                x2, _ = polish(obj, x0, use_mult, maxiter=40)
                w = softmax_weights(x2[:N_F])
                for d in drops:
                    w[d] = 0.0
                s = sum(w.values()) or 1.0
                w = {k: v / s for k, v in w.items()}
                m = mult_from_vec(x2[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
                for d in drops:
                    m[d] = MULT_LO
                row = score_candidate(
                    panel, pairs, train_p, test_p, hold_p, folds, w, m, bg,
                    o2c_cache, ind_map, cap_cache, prod_full,
                    tag=f"abl{bi}_{'_'.join(d[:3] for d in drops)}"[:40],
                    label=f"消融{','.join(drops)}",
                    method="ablation+polish",
                    lookback=args.lookback,
                    with_stability=True,
                )
                candidates.append(row)
            print(f"[v3] ablation base {bi+1}/{len(abl_base)} done", flush=True)

    candidates.sort(key=lambda r: r["gold"], reverse=True)

    # ========== Phase B: expanded gate refine ==========
    elites = candidates[: args.elites]
    grid = gate_grid_v3(full=True)
    print(f"[v3] Phase B gate refine elites={len(elites)} gates={len(grid)}", flush=True)
    refined: list[dict[str, Any]] = []
    for ei, e in enumerate(elites):
        t0 = time.time()
        best_local = None
        for gi, gates in enumerate(grid):
            # stability only on every 7th gate + best path end, to keep runtime sane
            with_stab = (gi % 7 == 0)
            row = score_candidate(
                panel, pairs, train_p, test_p, hold_p, folds,
                e["weights"], e["multipliers"], gates,
                o2c_cache, ind_map, cap_cache, prod_full,
                tag=(
                    f"{e['tag']}_g{gates['max_names']}i{gates['max_per_industry']}"
                    f"p{gates['score_percentile']}c{gates['min_confirm_hits']}"
                    f"k{len(gates.get('confirm_keys') or [])}"
                ),
                label=f"{e['label']}+门控",
                method=e["method"] + "+gates",
                lookback=args.lookback,
                with_stability=with_stab,
            )
            if not with_stab:
                # provisional gold without stab; will re-stab later for top
                row["gold"] = round(gold_score(row["train"], row["test"], row["hold"]), 5)
            refined.append(row)
            if best_local is None or row["gold"] > best_local["gold"]:
                best_local = row
        print(
            f"[v3] elite {ei+1}/{len(elites)} best_gold={best_local['gold']} "
            f"edge={best_local['edge_vs_prod']} {time.time()-t0:.1f}s",
            flush=True,
        )

    # re-score top provisional with full stability
    refined.sort(key=lambda r: r["gold"], reverse=True)
    print("[v3] Phase B2 restab top40", flush=True)
    restabbed = []
    for r in refined[:40]:
        row = score_candidate(
            panel, pairs, train_p, test_p, hold_p, folds,
            r["weights"], r["multipliers"], r["gates"],
            o2c_cache, ind_map, cap_cache, prod_full,
            tag=r["tag"], label=r["label"], method=r["method"] + "+restab",
            lookback=args.lookback, with_stability=True,
        )
        restabbed.append(row)
    # merge restabbed preference
    refined = restabbed + refined[40:]
    refined.sort(key=lambda r: r["gold"], reverse=True)

    # ========== Phase C: neighborhood DE around top 5 ==========
    if not args.skip_neighborhood:
        print("[v3] Phase C neighborhood DE", flush=True)
        seeds_nb = refined[:5]
        for ni, e in enumerate(seeds_nb):
            t0 = time.time()
            x_center = pack_vec(e["weights"], e["multipliers"], use_mult)
            # tight bounds around center
            bounds = []
            for j in range(N_F):
                c = float(x_center[j])
                bounds.append((c - 1.2, c + 1.2))
            if use_mult:
                for j in range(N_F):
                    c = float(x_center[N_F + j])
                    bounds.append((max(0.0, c - 0.25), min(1.0, c + 0.25)))
            # objective under this elite's best gates
            obj_g = make_objective(
                panel, train_p, test_p, hold_p, o2c_cache, ind_map, cap_cache, e["gates"], use_mult
            )
            x, fun = run_de(obj_g, dim, seed=9090 + ni * 13, maxiter=max(12, args.de_iter // 2), popsize=12, bounds=bounds)
            x2, fun2 = polish(obj_g, x, use_mult)
            if fun2 < fun:
                x = x2
            w = softmax_weights(x[:N_F])
            m = mult_from_vec(x[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
            row = score_candidate(
                panel, pairs, train_p, test_p, hold_p, folds, w, m, e["gates"],
                o2c_cache, ind_map, cap_cache, prod_full,
                tag=f"nb{ni}_{e['tag']}"[:48],
                label=f"邻域精搜·{e['label']}"[:48],
                method="neighborhood+DE",
                lookback=args.lookback,
                with_stability=True,
            )
            refined.append(row)
            print(
                f"[v3] neighborhood {ni+1}/5 gold={row['gold']} hold={row['hold'].get('avg_daily_pct')} "
                f"edge={row['edge_vs_prod']} {time.time()-t0:.1f}s",
                flush=True,
            )

    refined.sort(key=lambda r: r["gold"], reverse=True)

    # diversify top-k
    def wvec(w: dict[str, float]) -> np.ndarray:
        return np.array([float(w[f]) for f in FACTORS], dtype=float)

    selected: list[dict[str, Any]] = []
    for r in refined:
        if len(selected) >= args.top:
            break
        v = wvec(r["weights"])
        ok = True
        for s in selected:
            u = wvec(s["weights"])
            cos = float(np.dot(v, u) / (np.linalg.norm(v) * np.linalg.norm(u) + 1e-12))
            # also diversify by gate fingerprint a bit
            same_gate = (
                r["gates"].get("max_names") == s["gates"].get("max_names")
                and r["gates"].get("score_percentile") == s["gates"].get("score_percentile")
            )
            if cos > 0.965 and same_gate and abs((r.get("edge_vs_prod") or 0) - (s.get("edge_vs_prod") or 0)) < 0.08:
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

    print("\n========== 淘沙猛档 v3 · 真金 TOP ==========", flush=True)
    for i, c in enumerate(selected, 1):
        print(f"#{i} gold={c['gold']} stab={c['stability']} edge={c['edge_vs_prod']} method={c['method']}", flush=True)
        print(
            f"   hold avg={c['hold'].get('avg_daily_pct')}% win={c['hold'].get('win_days_pct')}% "
            f"tot={c['hold'].get('total_return_pct')}%",
            flush=True,
        )
        print(
            f"   test avg={c['test'].get('avg_daily_pct')}% full avg={c['full'].get('avg_daily_pct')}% "
            f"sharpe={c['full'].get('sharpe')} empty={c['full'].get('empty_book_days')}",
            flush=True,
        )
        print(
            f"   gates n={c['gates']['max_names']} ind={c['gates']['max_per_industry']} "
            f"pct={c['gates']['score_percentile']} conf={c['gates']['min_confirm_hits']} "
            f"keys={c['gates'].get('confirm_keys')}",
            flush=True,
        )
        if c.get("folds"):
            print(f"   folds: {c['folds']}", flush=True)
        tops = sorted(c["weights"].items(), key=lambda x: -x[1])[:5]
        print("   weights:", ", ".join(f"{k}={v*100:.1f}%" for k, v in tops), flush=True)
        mt = [(k, v) for k, v in c["multipliers"].items() if abs(v - 1.0) > 0.08]
        if mt:
            print(
                "   mult:",
                ", ".join(f"{k}×{v:.2f}" for k, v in sorted(mt, key=lambda x: -abs(x[1] - 1))[:8]),
                flush=True,
            )

    # archive previous gold
    for p in list(EXP_DIR.glob("exp_gold_top*.yaml")) + list(EXP_DIR.glob("exp_search_top*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["status"] = "archived"
            raw["archived_at"] = now_str()
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass
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
        print(f"[v3] wrote {path}", flush=True)

    report = {
        "generated_at": now_str(),
        "mode": "gold_search_v3_hard",
        "trade_date": td,
        "lookback": args.lookback,
        "use_mult": use_mult,
        "mult_range": [MULT_LO, MULT_HI],
        "pairs": len(pairs),
        "train_n": len(train_p),
        "test_n": len(test_p),
        "hold_n": len(hold_p),
        "n_folds": len(folds),
        "de_seeds": args.de_seeds,
        "de_iter": args.de_iter,
        "de_pop": args.de_pop,
        "n_phaseA": len(candidates),
        "n_phaseB": len(refined),
        "production_control": {k: prod_full[k] for k in prod_full if k != "series"},
        "top": [{k: v for k, v in c.items()} for c in selected],
        "leaderboard_top20": [
            {
                "tag": r["tag"],
                "gold": r["gold"],
                "stability": r.get("stability"),
                "edge_vs_prod": r["edge_vs_prod"],
                "hold_avg": r["hold"].get("avg_daily_pct"),
                "test_avg": r["test"].get("avg_daily_pct"),
                "full_avg": r["full"].get("avg_daily_pct"),
                "full_total": r["full"].get("total_return_pct"),
                "full_sharpe": r["full"].get("sharpe"),
                "method": r["method"],
                "gates": {
                    "max_names": r["gates"].get("max_names"),
                    "pct": r["gates"].get("score_percentile"),
                    "ind": r["gates"].get("max_per_industry"),
                    "conf": r["gates"].get("min_confirm_hits"),
                    "mkt": r["gates"].get("market_gate"),
                },
            }
            for r in refined[:20]
        ],
        "experiment_files": written,
        "disclaimer": (
            "淘沙猛档 v3：启发式+连续优化+多窗稳定性的相对最优，非数学穷尽。"
            "纸面 T+1 开盘→收盘扣 0.26%；生产 PREBREAKOUT 未改。"
        ),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v3] report {REPORT_PATH}", flush=True)

    print("[v3] refresh factory page ...", flush=True)
    fac.run_factory(td, lookback=min(args.lookback, 60))
    print("[v3] DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
