#!/usr/bin/env python3
"""启动前夕 · 日淘轻量搜索（跟随 19:30 主管线）

目标：每个交易日自动淘出 Top3 配方，刷观察页，持续对照生产。
  - 轻量：DE seeds/iter 收紧，瘦门控，跳过邻域深搜
  - 非阻塞：失败写 status ok=false，不拖垮主线
  - Top3：#1 综合金 · #2 可交易优先（默认观察冠军）· #3 风格对照
  - 生产 PREBREAKOUT 冻结

用法：
  ~/.openclaw/venv/bin/python prebreakout_factory_search_daily.py
  ~/.openclaw/venv/bin/python prebreakout_factory_search_daily.py --lookback 70 --top 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
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
REPORT_PATH = SEARCH_DIR / "search_report_daily_latest.json"
STATUS_PATH = fac.HEALTH_DIR / "prebreakout_daily_pan_status.json"
EXP_DIR = fac.EXP_DIR
FACTORS = list(fac.PROD_FACTORS)
N_F = len(FACTORS)
PROD_W = fac.production_weights()
COST = 0.26
CONFIRM_KEYS = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"]
MULT_LO, MULT_HI = 0.35, 1.9


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_status(ok: bool, **extra: Any) -> None:
    payload = {"ok": ok, "generated_at": now_str(), **extra}
    fac.write_json(STATUS_PATH, payload)


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
        t = (val - MULT_LO) / max(MULT_HI - MULT_LO, 1e-9)
        mv.append(float(np.clip(t, 0, 1)))
    return np.concatenate([logits, np.array(mv, dtype=float)])


def evaluate(panel, pairs, weights, mult, gates, o2c_cache, ind_map, cap_cache):
    return v2.evaluate(panel, pairs, weights, mult, gates, o2c_cache, ind_map, cap_cache)


def gold_score(train, test, hold) -> float:
    return v2.gold_score(train, test, hold)


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


def lean_gate_grid() -> list[dict[str, Any]]:
    out = []
    for max_names in (4, 5, 8):
        for max_ind in (2, 3):
            if max_ind > max_names:
                continue
            for pct in (80, 85, 90):
                for hits in (1, 2):
                    out.append(
                        {
                            "max_names": max_names,
                            "max_per_industry": max_ind,
                            "score_percentile": pct,
                            "min_score_floor": 66,
                            "min_confirm_hits": hits,
                            "confirm_subscore_min": 58,
                            "confirm_keys": list(CONFIRM_KEYS),
                            "market_gate": True,
                            "cost_pct": COST,
                        }
                    )
    # 少量可交易热点：确认键变体
    for ckeys in (
        ["macd_early_signal", "volume_warmup", "momentum", "liquidity"],
        ["macd_early_signal", "turnover_stability", "chip_support"],
    ):
        for max_names in (4, 5, 8):
            for pct in (80, 85):
                out.append(
                    {
                        "max_names": max_names,
                        "max_per_industry": 2 if max_names <= 5 else 3,
                        "score_percentile": pct,
                        "min_score_floor": 66,
                        "min_confirm_hits": 1,
                        "confirm_subscore_min": 58,
                        "confirm_keys": list(ckeys),
                        "market_gate": True,
                        "cost_pct": COST,
                    }
                )
    seen = set()
    uniq = []
    for g in out:
        key = json.dumps(g, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(g)
    return uniq


def split_pairs(pairs):
    return v2.split_pairs(pairs)


def make_objective(panel, train_p, test_p, o2c_cache, ind_map, cap_cache, gates, use_mult):
    """日淘优化目标：只用 train(+test 弱权重)，holdout 密封不参与。"""
    import prebreakout_recipe_engine as eng

    def obj(x: np.ndarray) -> float:
        if use_mult:
            w = softmax_weights(x[:N_F])
            m = mult_from_vec(x[N_F : 2 * N_F])
        else:
            w = softmax_weights(x)
            m = {f: 1.0 for f in FACTORS}
        tr = evaluate(panel, train_p, w, m, gates, o2c_cache, ind_map, cap_cache)
        te = evaluate(panel, test_p, w, m, gates, o2c_cache, ind_map, cap_cache) if test_p else None
        return -eng.train_objective_score(tr, te)

    return obj


def run_de(obj, dim, seed, maxiter=10, popsize=8) -> tuple[np.ndarray, float]:
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


def polish(obj, x0, use_mult, maxiter=35) -> tuple[np.ndarray, float]:
    if use_mult:
        bounds = [(-4, 4)] * N_F + [(0, 1)] * N_F
    else:
        bounds = [(-4, 4)] * N_F
    res = minimize(obj, x0, method="L-BFGS-B", bounds=bounds, options={"maxiter": maxiter})
    return res.x, float(res.fun)


def score_row(panel, pairs, train_p, test_p, hold_p, w, m, gates, o2c_cache, ind_map, cap_cache, prod_full, tag, label, method, lookback):
    import prebreakout_recipe_engine as eng

    tr = evaluate(panel, train_p, w, m, gates, o2c_cache, ind_map, cap_cache)
    te = evaluate(panel, test_p, w, m, gates, o2c_cache, ind_map, cap_cache) if test_p else {
        "days": 0, "avg_daily_pct": None, "empty_book_days": 0
    }
    ho = evaluate(panel, hold_p, w, m, gates, o2c_cache, ind_map, cap_cache)
    fu = evaluate(panel, pairs, w, m, gates, o2c_cache, ind_map, cap_cache)
    # gold 仅用于报告；选优用 holdout_select_score
    g_report = gold_score(tr, te, ho)
    g_select = eng.holdout_select_score(ho, fu)
    g_train = eng.train_objective_score(tr, te if test_p else None)
    edge = None
    if fu.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
        edge = round(float(fu["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
    empty_ratio = float(fu.get("empty_book_days") or 0) / max(float(fu.get("days") or 1), 1)
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
        "gold": round(g_select, 5),  # 主排序分 = holdout
        "gold_report": round(g_report, 5),
        "train_score": round(g_train, 5),
        "holdout_score": round(g_select, 5),
        "edge_vs_prod": edge,
        "edge_label": "vs_research_baseline_prod_weights",
        "empty_ratio": round(empty_ratio, 4),
        "lookback": lookback,
    }


def tradable_score(row: dict[str, Any]) -> float:
    """可交易分：惩罚空仓，奖励活跃与 hold/full 正收益。"""
    fu = row.get("full") or {}
    ho = row.get("hold") or {}
    empty = float(row.get("empty_ratio") or 0)
    hold_avg = float(ho.get("avg_daily_pct") or 0)
    full_avg = float(fu.get("avg_daily_pct") or 0)
    active = float(fu.get("active_days") or 0)
    days = max(float(fu.get("days") or 1), 1)
    active_ratio = active / days
    gold = float(row.get("gold") or 0)
    return (
        1.2 * hold_avg
        + 0.8 * full_avg
        + 1.5 * active_ratio
        - 2.0 * empty
        + 0.08 * gold
    )


def load_warmstarts() -> list[tuple[str, dict, dict | None, str]]:
    out: list[tuple[str, dict, dict | None, str]] = []
    for pat in ("exp_gold_daily_top*.yaml", "exp_gold_top*.yaml"):
        for p in sorted(EXP_DIR.glob(pat)):
            try:
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                w = raw.get("weights") or {}
                m = raw.get("subscore_multipliers")
                if w:
                    out.append((f"warm_{p.stem}"[:36], w, m, f"继承{p.stem}"[:40]))
            except Exception:
                pass
    for rep_name in ("search_report_daily_latest.json", "search_report_v3_latest.json", "search_report_v2_latest.json"):
        rp = SEARCH_DIR / rep_name
        if not rp.exists():
            continue
        try:
            rep = json.loads(rp.read_text(encoding="utf-8"))
            for i, t in enumerate(rep.get("top") or []):
                w = t.get("weights") or {}
                m = t.get("multipliers")
                if w:
                    out.append((f"rep_{i}_{rep_name[:8]}", w, m, f"报告热启{i}"))
        except Exception:
            pass
    return out


def write_top_yaml(rank: int, item: dict[str, Any], role: str, is_obs_champ: bool) -> Path:
    path = EXP_DIR / f"exp_gold_daily_top{rank}_{item['tag']}.yaml"
    disabled = [k for k, v in item["weights"].items() if float(v) <= 1e-4]
    body = {
        "id": f"exp_gold_daily_top{rank}_{item['tag']}",
        "name": f"日淘Top{rank}·{role}·{item.get('label')}",
        "status": "active",
        "daily_pan": True,
        "role": role,
        "observation_champion": bool(is_obs_champ),
        "hypothesis": (
            f"【日淘{now_str()[:10]}】holdout选优分={item.get('holdout_score', item['gold']):.4f} 角色={role}；"
            f"holdout日均={item['hold'].get('avg_daily_pct')}% train日均={item['train'].get('avg_daily_pct')}% "
            f"全窗日均={item['full'].get('avg_daily_pct')}% 超额(研究基线)={item.get('edge_vs_prod')}；"
            f"空仓比={item.get('empty_ratio')} 方法={item.get('method')}；holdout密封不参与DE"
        ),
        "inspired_by": [
            "日淘轻量：跟随 19:30 主管线",
            "真 holdout 密封：DE/门控搜索不看 hold；Top 按 holdout 选",
            "confirm=原始子分；生产 PREBREAKOUT 冻结",
        ],
        "production_frozen": True,
        "weights": {k: round(float(v), 6) for k, v in item["weights"].items()},
        "disabled_factors": disabled,
        "subscore_multipliers": {k: round(float(v), 4) for k, v in item.get("multipliers", {}).items()},
        "gates": item["gates"],
        "backtest": {"lookback_days": item.get("lookback", 70), "cost_pct": COST},
        "search_metrics": {
            "gold_score": item["gold"],
            "holdout_score": item.get("holdout_score"),
            "train_score": item.get("train_score"),
            "gold_report": item.get("gold_report"),
            "tradable_score": round(tradable_score(item), 4),
            "empty_ratio": item.get("empty_ratio"),
            "train": item["train"],
            "test": item["test"],
            "hold": item["hold"],
            "full": item["full"],
            "edge_vs_prod": item.get("edge_vs_prod"),
            "edge_label": item.get("edge_label"),
            "method": item.get("method"),
            "role": role,
            "selection_basis": "holdout_only",
        },
    }
    path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def archive_daily_tops() -> None:
    for p in list(EXP_DIR.glob("exp_gold_daily_top*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["status"] = "archived"
            raw["archived_at"] = now_str()
            raw["observation_champion"] = False
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass


def diversify_select(rows: list[dict], top: int) -> list[dict]:
    """先按权重组取每组最佳（holdout 分），再跨组多样性挑选。"""

    def wvec(w: dict[str, float]) -> np.ndarray:
        return np.array([float(w[f]) for f in FACTORS], dtype=float)

    def wkey(w: dict[str, float]) -> tuple:
        return tuple(round(float(w[f]), 3) for f in FACTORS)

    def rank_key(r: dict) -> float:
        return float(r.get("holdout_score") if r.get("holdout_score") is not None else r.get("gold") or -999)

    # 同权重组只保留 holdout 最高的一条
    best_by_w: dict[tuple, dict] = {}
    for r in rows:
        k = wkey(r["weights"])
        if k not in best_by_w or rank_key(r) > rank_key(best_by_w[k]):
            best_by_w[k] = r
    pool = sorted(best_by_w.values(), key=rank_key, reverse=True)

    selected: list[dict] = []
    for r in pool:
        if len(selected) >= top:
            break
        v = wvec(r["weights"])
        ok = True
        for s in selected:
            u = wvec(s["weights"])
            cos = float(np.dot(v, u) / (np.linalg.norm(v) * np.linalg.norm(u) + 1e-12))
            empty_close = abs(float(r.get("empty_ratio") or 0) - float(s.get("empty_ratio") or 0)) < 0.05
            edge_close = abs(float(r.get("edge_vs_prod") or 0) - float(s.get("edge_vs_prod") or 0)) < 0.08
            if cos > 0.93 and empty_close and edge_close:
                ok = False
                break
        if ok:
            selected.append(r)

    # 不够则放宽：只卡 cos
    if len(selected) < top:
        for r in pool:
            if len(selected) >= top:
                break
            if r in selected:
                continue
            v = wvec(r["weights"])
            ok = True
            for s in selected:
                u = wvec(s["weights"])
                cos = float(np.dot(v, u) / (np.linalg.norm(v) * np.linalg.norm(u) + 1e-12))
                if cos > 0.98:
                    ok = False
                    break
            if ok:
                selected.append(r)

    # 仍不够：从原始 refined 补（允许不同门控）
    for r in rows:
        if len(selected) >= top:
            break
        if r not in selected:
            selected.append(r)
    return selected


def assign_roles(selected: list[dict]) -> tuple[list[dict], str]:
    """返回 (ordered for yaml ranks by gold), obs_champion_id。
    观察冠军：三套里可交易分最高且 full 不全空。
    """
    if not selected:
        return [], ""
    by_gold = sorted(selected, key=lambda r: r["gold"], reverse=True)
    tradable_pick = max(by_gold, key=tradable_score)
    # 若可交易明显更空仓，仍选 gold 第一
    if float(tradable_pick.get("empty_ratio") or 0) > 0.55 and by_gold[0] is not tradable_pick:
        # 在 empty_ratio<=0.45 里再找
        denser = [r for r in by_gold if float(r.get("empty_ratio") or 0) <= 0.45]
        if denser:
            tradable_pick = max(denser, key=tradable_score)
    roles = {}
    roles[id(by_gold[0])] = "综合金"
    if tradable_pick is not by_gold[0]:
        roles[id(tradable_pick)] = "可交易"
    for r in by_gold:
        if id(r) not in roles:
            roles[id(r)] = "风格对照"
    # yaml rank by gold order; obs champ is tradable_pick
    tagged = []
    for r in by_gold:
        rr = dict(r)
        rr["role"] = roles[id(r)]
        tagged.append(rr)
    champ_tag = tradable_pick["tag"]
    return tagged, champ_tag


def run_daily(args: argparse.Namespace) -> dict[str, Any]:
    t_all = time.time()
    use_mult = not args.no_mult
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    EXP_DIR.mkdir(parents=True, exist_ok=True)

    import prebreakout_recipe_engine as eng

    td = shadow.resolve_trade_date(args.trade_date or None)
    all_dates = [
        d
        for d in shadow.list_trade_dates()
        if d <= td and (shadow.CACHE / f"stk_factor_{d}.parquet").exists()
    ]
    dates = all_dates[-(args.lookback + 1) :]
    pairs = list(zip(dates[:-1], dates[1:]))
    holdout_days = int(getattr(args, "holdout_days", 20) or 20)
    train_p, test_p, hold_p, split_meta = eng.split_pairs_true_holdout(pairs, holdout_days=holdout_days)
    print(
        f"[daily] td={td} window={dates[0]}→{dates[-1]} pairs={len(pairs)} "
        f"train={len(train_p)} test={len(test_p)} hold={len(hold_p)} "
        f"holdout_sealed={split_meta.get('holdout_sealed')} degraded={split_meta.get('degraded')} "
        f"seeds={args.de_seeds} iter={args.de_iter} pop={args.de_pop}",
        flush=True,
    )

    panel = s1.preload_panel(dates[:-1])
    good = {k: v for k, v in panel.items() if v is not None and len(v) >= 200}
    print(f"[daily] panel usable={len(good)}/{len(panel)}", flush=True)
    ind_map = s1.industry_map()
    o2c_cache: dict[str, dict[str, float]] = {}
    cap_cache: dict[tuple, int] = {}
    for _, nd in pairs:
        if nd not in o2c_cache:
            o2c_cache[nd] = s1.o2c_for_date(nd)

    bg = base_gates()
    # 研究基线：生产权重 + 基础门控（非真实生产名单）
    prod_full = evaluate(panel, pairs, PROD_W, {f: 1.0 for f in FACTORS}, bg, o2c_cache, ind_map, cap_cache)
    print(f"[daily] research baseline full avg={prod_full.get('avg_daily_pct')}", flush=True)

    candidates: list[dict[str, Any]] = []
    dim = 2 * N_F if use_mult else N_F
    obj = make_objective(panel, train_p, test_p, o2c_cache, ind_map, cap_cache, bg, use_mult)

    # Phase A DE
    print(f"[daily] Phase A DE dim={dim}", flush=True)
    for seed in range(args.de_seeds):
        t0 = time.time()
        x, fun = run_de(obj, dim, seed=20260716 + seed * 41, maxiter=args.de_iter, popsize=args.de_pop)
        x2, fun2 = polish(obj, x, use_mult)
        if fun2 < fun:
            x = x2
        w = softmax_weights(x[:N_F])
        m = mult_from_vec(x[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
        row = score_row(
            panel, pairs, train_p, test_p, hold_p, w, m, bg,
            o2c_cache, ind_map, cap_cache, prod_full,
            tag=f"dde{seed}", label=f"日淘DE{seed}", method="daily-DE+polish", lookback=args.lookback,
        )
        candidates.append(row)
        print(
            f"[daily] DE seed={seed} gold={row['gold']:.3f} hold={row['hold'].get('avg_daily_pct')} "
            f"empty={row['empty_ratio']} edge={row['edge_vs_prod']} {time.time()-t0:.1f}s",
            flush=True,
        )

    # Warm starts
    print("[daily] Phase A2 warm", flush=True)
    seen = set()
    for tag, w0, m0, note in load_warmstarts()[:24]:
        key = tuple(round(float(w0.get(f, 0)), 4) for f in FACTORS)
        if key in seen:
            continue
        seen.add(key)
        x0 = pack_vec(w0, m0, use_mult)
        x2, _ = polish(obj, x0, use_mult, maxiter=25)
        w = softmax_weights(x2[:N_F])
        m = mult_from_vec(x2[N_F:]) if use_mult else {f: 1.0 for f in FACTORS}
        row = score_row(
            panel, pairs, train_p, test_p, hold_p, w, m, bg,
            o2c_cache, ind_map, cap_cache, prod_full,
            tag=tag, label=note, method="daily-warm", lookback=args.lookback,
        )
        candidates.append(row)

    # Phase A 精英先按 train_score 取（搜索阶段），再在 Phase B 后用 holdout 定冠
    candidates.sort(key=lambda r: float(r.get("train_score") or r.get("gold") or -999), reverse=True)
    print("[daily] Phase A top4 (by train_score):", flush=True)
    for c in candidates[:4]:
        print(
            f"  {c['tag']} train={c.get('train_score')} hold={c.get('holdout_score')} "
            f"empty={c['empty_ratio']} edge={c['edge_vs_prod']}",
            flush=True,
        )

    # Phase B lean gates：门控搜索用 train_score 选 best-in-elite，最终用 holdout 排序
    elites = candidates[: max(4, args.elites)]
    grid = lean_gate_grid()
    print(f"[daily] Phase B elites={len(elites)} gates={len(grid)} (gate pick by train; final by holdout)", flush=True)
    refined: list[dict[str, Any]] = []
    for ei, e in enumerate(elites):
        t0 = time.time()
        best = None
        for gates in grid:
            row = score_row(
                panel, pairs, train_p, test_p, hold_p,
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
            )
            refined.append(row)
            # 门控网格内用 train 选优，避免 holdout 泄漏进门控搜索
            if best is None or float(row.get("train_score") or -999) > float(best.get("train_score") or -999):
                best = row
        print(
            f"[daily] elite {ei+1}/{len(elites)} best_train={best.get('train_score')} "
            f"hold={best.get('holdout_score')} empty={best['empty_ratio']} {time.time()-t0:.1f}s",
            flush=True,
        )

    refined.sort(key=lambda r: float(r.get("holdout_score") or r.get("gold") or -999), reverse=True)
    selected = diversify_select(refined, args.top)
    ordered, champ_tag = assign_roles(selected)

    print("\n========== 日淘 TOP3 ==========", flush=True)
    for i, c in enumerate(ordered, 1):
        is_ch = c["tag"] == champ_tag or (champ_tag in c["tag"] and c.get("role") == "可交易")
        # champion match by exact tag from tradable_pick
        is_ch = c["tag"] == champ_tag
        print(
            f"#{i} role={c.get('role')} gold={c['gold']} empty={c['empty_ratio']} "
            f"edge={c['edge_vs_prod']} hold={c['hold'].get('avg_daily_pct')} "
            f"{'★观察冠军' if is_ch else ''}",
            flush=True,
        )

    # Fix champ_tag: assign_roles returns tradable_pick's tag; match ordered items
    obs_id = None
    archive_daily_tops()
    # also keep only one observation champion among all gold yamls
    for p in EXP_DIR.glob("exp_gold_*.yaml"):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if raw.get("observation_champion"):
                raw["observation_champion"] = False
                p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    written = []
    for i, c in enumerate(ordered, 1):
        is_ch = c["tag"] == champ_tag
        path = write_top_yaml(i, c, c.get("role") or f"Top{i}", is_ch)
        written.append(str(path))
        if is_ch:
            obs_id = f"exp_gold_daily_top{i}_{c['tag']}"
        print(f"[daily] wrote {path}", flush=True)

    if obs_id is None and ordered:
        # fallback first
        obs_id = f"exp_gold_daily_top1_{ordered[0]['tag']}"
        p = EXP_DIR / f"exp_gold_daily_top1_{ordered[0]['tag']}.yaml"
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["observation_champion"] = True
            raw["role"] = raw.get("role") or "综合金"
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # baseline stays active
    for p in EXP_DIR.glob("exp_baseline*.yaml"):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["status"] = "active"
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    report = {
        "generated_at": now_str(),
        "mode": "daily_pan_light",
        "trade_date": td,
        "lookback": args.lookback,
        "pairs": len(pairs),
        "split": split_meta,
        "holdout_sealed": True,
        "confirm_mode": "raw_subscores",
        "de_seeds": args.de_seeds,
        "de_iter": args.de_iter,
        "de_pop": args.de_pop,
        "n_candidates": len(candidates),
        "n_refined": len(refined),
        "observation_champion_id": obs_id,
        "production_control": {k: prod_full[k] for k in prod_full if k != "series"},
        "production_control_kind": "research_baseline_prod_weights",
        "top": [
            {
                "rank": i,
                "id": f"exp_gold_daily_top{i}_{c['tag']}",
                "role": c.get("role"),
                "observation_champion": c["tag"] == champ_tag,
                "gold": c["gold"],
                "holdout_score": c.get("holdout_score"),
                "train_score": c.get("train_score"),
                "edge_vs_prod": c.get("edge_vs_prod"),
                "edge_label": c.get("edge_label"),
                "empty_ratio": c.get("empty_ratio"),
                "tradable_score": round(tradable_score(c), 4),
                "hold_avg": c["hold"].get("avg_daily_pct"),
                "train_avg": c["train"].get("avg_daily_pct"),
                "full_avg": c["full"].get("avg_daily_pct"),
                "method": c["method"],
                "gates": {
                    "max_names": c["gates"].get("max_names"),
                    "pct": c["gates"].get("score_percentile"),
                    "conf": c["gates"].get("min_confirm_hits"),
                },
            }
            for i, c in enumerate(ordered, 1)
        ],
        "experiment_files": written,
        "elapsed_sec": round(time.time() - t_all, 1),
        "disclaimer": (
            "日淘轻量；DE/门控不看 holdout；Top 按 holdout 选。"
            "edge 相对研究基线非真生产。纸面T+1；生产冻结。研究展示非决策。"
        ),
    }
    eng.write_json_atomic(REPORT_PATH, report)
    print(f"[daily] report {REPORT_PATH} elapsed={report['elapsed_sec']}s", flush=True)

    # Factory refresh with forced observation champion
    print(f"[daily] refresh factory champion={obs_id}", flush=True)
    fac.run_factory(td, lookback=min(args.lookback, 60), force_champion=obs_id)

    write_status(
        True,
        state="completed",
        trade_date=td,
        observation_champion=obs_id,
        observation_champion_id=obs_id,
        top_n=len(ordered),
        elapsed_sec=report["elapsed_sec"],
        report=str(REPORT_PATH),
        experiment_files=written,
        holdout_sealed=True,
        split=split_meta,
        confirm_mode="raw_subscores",
        finished_at=now_str(),
    )
    print("[daily] DONE", flush=True)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Prebreakout daily light gold pan (Top3)")
    ap.add_argument("--lookback", type=int, default=70)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--trade-date", default="")
    ap.add_argument("--holdout-days", type=int, default=20, help="末段 holdout 交易日数，完全不参与 DE/门控搜索")
    ap.add_argument("--de-seeds", type=int, default=3)
    ap.add_argument("--de-iter", type=int, default=10)
    ap.add_argument("--de-pop", type=int, default=8)
    ap.add_argument("--elites", type=int, default=5)
    ap.add_argument("--no-mult", action="store_true")
    args = ap.parse_args()
    try:
        run_daily(args)
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[daily] FATAL: {type(exc).__name__}: {exc}\n{tb}", flush=True)
        write_status(
            False,
            state="failed",
            error=f"{type(exc).__name__}: {exc}",
            traceback=tb[-2000:],
            finished_at=now_str(),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
