#!/usr/bin/env python3
"""启动前夕二次加工 · 自动搜索最优 2–3 套因子组合

流程：
  1) 预计算生产子分（原料，不改 pipeline）
  2) 算法生成大量权重/门控/消融候选
  3) 快评 T+1 开→收（等权、扣成本）
  4) Walk-forward 验证，按综合分排序
  5) 写出 top3 实验 YAML + search_report.json，并刷新观察页

用法：
  ~/.openclaw/venv/bin/python prebreakout_factory_search.py --lookback 60 --top 3
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(__file__).resolve().parents[3]
STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"
sys.path.insert(0, str(STOCK_ANALYZER))
sys.path.insert(0, str(SCRIPT_DIR))

import prebreakout_factory as fac  # noqa: E402
import prebreakout_shadow_research as shadow  # noqa: E402

SEARCH_DIR = fac.FACTORY_DIR / "search"
CACHE_SCORES_DIR = SEARCH_DIR / "score_cache"
REPORT_PATH = SEARCH_DIR / "search_report_latest.json"
EXP_DIR = fac.EXP_DIR

FACTORS = list(fac.PROD_FACTORS)
PROD_W = fac.production_weights()
COST_PCT = 0.26


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_SCORES_DIR.mkdir(parents=True, exist_ok=True)
    EXP_DIR.mkdir(parents=True, exist_ok=True)


def cache_path(trade_date: str) -> Path:
    return CACHE_SCORES_DIR / f"subscores_{trade_date}.parquet"


def build_or_load_day(trade_date: str) -> pd.DataFrame | None:
    """返回列: ts_code, prod_score, + 各因子子分."""
    p = cache_path(trade_date)
    if p.exists():
        try:
            return pd.read_parquet(p)
        except Exception:
            pass
    scored = fac.score_universe(trade_date)
    if not scored:
        return None
    rows = []
    for r in scored:
        subs = r.get("sub_scores") or {}
        row = {
            "ts_code": r.get("ts_code"),
            "prod_score": float(r.get("score") or r.get("prod_score") or 0),
            "prod_rank": int(r.get("prod_rank") or 0),
            "price": r.get("price"),
            "change_pct": r.get("change_pct"),
        }
        for f in FACTORS:
            try:
                row[f] = float(subs.get(f) or 0)
            except (TypeError, ValueError):
                row[f] = 0.0
        rows.append(row)
    df = pd.DataFrame(rows)
    try:
        df.to_parquet(p, index=False)
    except Exception:
        # parquet optional
        df.to_csv(p.with_suffix(".csv"), index=False)
    return df


def preload_panel(dates: list[str]) -> dict[str, pd.DataFrame]:
    panel: dict[str, pd.DataFrame] = {}
    for i, d in enumerate(dates):
        t0 = time.time()
        df = build_or_load_day(d)
        if df is not None and len(df) >= 50:
            panel[d] = df
        print(f"[search] panel {i+1}/{len(dates)} {d} n={0 if df is None else len(df)} {time.time()-t0:.1f}s")
    return panel


def normalize_w(w: dict[str, float]) -> dict[str, float]:
    out = {f: max(0.0, float(w.get(f, 0.0))) for f in FACTORS}
    s = sum(out.values())
    if s <= 1e-12:
        return dict(PROD_W)
    return {k: v / s for k, v in out.items()}


def rescore_df(df: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    w = normalize_w(weights)
    acc = np.zeros(len(df), dtype=float)
    for f, wf in w.items():
        if wf <= 0 or f not in df.columns:
            continue
        acc += df[f].to_numpy(dtype=float) * wf
    return pd.Series(acc, index=df.index)


def confirm_hits_arr(df: pd.DataFrame, keys: list[str], thr: float) -> np.ndarray:
    hits = np.zeros(len(df), dtype=int)
    for k in keys:
        if k not in df.columns:
            continue
        hits += (df[k].to_numpy(dtype=float) >= thr).astype(int)
    return hits


def industry_map() -> dict[str, str]:
    return shadow.load_industry_map()


def pick_top(
    df: pd.DataFrame,
    scores: pd.Series,
    ind_map: dict[str, str],
    *,
    top_n: int,
    max_per_industry: int,
    score_pct: float,
    min_score: float,
    min_confirm: int,
    confirm_keys: list[str],
    confirm_thr: float,
) -> list[str]:
    if top_n <= 0 or df is None or len(df) == 0:
        return []
    s = scores.to_numpy(dtype=float)
    thr = float(np.nanpercentile(s[np.isfinite(s)], score_pct)) if np.isfinite(s).any() else min_score
    if not np.isfinite(thr):
        thr = min_score
    thr = max(thr, min_score)
    hits = confirm_hits_arr(df, confirm_keys, confirm_thr)
    order = np.argsort(-s)
    picked: list[str] = []
    ind_cnt: dict[str, int] = {}
    for idx in order:
        if s[idx] < thr:
            break
        if hits[idx] < min_confirm:
            continue
        tc = str(df.iloc[idx]["ts_code"])
        ind = ind_map.get(tc) or "未知"
        if ind_cnt.get(ind, 0) >= max_per_industry:
            continue
        ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
        picked.append(tc)
        if len(picked) >= top_n:
            break
    return picked


def o2c_for_date(next_date: str) -> dict[str, float]:
    daily = shadow.load_parquet("daily", next_date)
    if daily is None or len(daily) == 0:
        return {}
    out: dict[str, float] = {}
    for _, r in daily.iterrows():
        tc = str(r.get("ts_code") or "")
        o = float(r.get("open") or 0)
        c = float(r.get("close") or 0)
        if tc and o > 0 and c > 0:
            out[tc] = (c / o - 1.0) * 100.0
    return out


def market_cap_for(td: str, enable: bool, max_names: int) -> int:
    if not enable:
        return max_names
    mom, src = shadow.market_mom20(td)
    cap, _ = shadow.market_cap_from_mom20(mom, src)
    return min(int(cap), int(max_names))


def evaluate_strategy(
    panel: dict[str, pd.DataFrame],
    date_pairs: list[tuple[str, str]],
    weights: dict[str, float],
    gates: dict[str, Any],
    o2c_cache: dict[str, dict[str, float]],
    ind_map: dict[str, str],
    mom_cap_cache: dict[tuple, int],
) -> dict[str, Any]:
    w = normalize_w(weights)
    daily_rets: list[float] = []
    win_days = 0
    active = 0
    empty = 0
    n_picks_sum = 0
    nav = 1.0
    series = []
    mkt_on = bool(gates.get("market_gate", True))
    max_names = int(gates.get("max_names", 8))
    for td, nd in date_pairs:
        df = panel.get(td)
        if df is None or len(df) < 50:
            continue
        scores = rescore_df(df, w)
        cap_key = (td, max_names, mkt_on)
        top_n = mom_cap_cache.get(cap_key)
        if top_n is None:
            top_n = market_cap_for(td, mkt_on, max_names)
            mom_cap_cache[cap_key] = top_n
        picks = pick_top(
            df,
            scores,
            ind_map,
            top_n=top_n,
            max_per_industry=int(gates.get("max_per_industry", 3)),
            score_pct=float(gates.get("score_percentile", 90)),
            min_score=float(gates.get("min_score_floor", 70)),
            min_confirm=int(gates.get("min_confirm_hits", 2)),
            confirm_keys=list(gates.get("confirm_keys") or []),
            confirm_thr=float(gates.get("confirm_subscore_min", 60)),
        )
        o2c = o2c_cache.get(nd)
        if o2c is None:
            o2c = o2c_for_date(nd)
            o2c_cache[nd] = o2c
        if not o2c:
            continue
        if not picks:
            avg = 0.0
            empty += 1
            n = 0
        else:
            rets = [o2c[c] for c in picks if c in o2c]
            if not rets:
                avg = 0.0
                empty += 1
                n = 0
            else:
                avg = float(np.mean(rets)) - float(gates.get("cost_pct", COST_PCT))
                n = len(rets)
                active += 1
                n_picks_sum += n
                if avg > 0:
                    win_days += 1
        daily_rets.append(avg)
        nav *= 1.0 + avg / 100.0
        series.append({"signal_date": td, "trade_date": nd, "n": n, "avg_o2c_pct": round(avg, 4), "cum_nav_pct": round((nav - 1) * 100, 4)})

    if not daily_rets:
        return {
            "days": 0,
            "avg_daily_pct": None,
            "win_days_pct": None,
            "total_return_pct": None,
            "sharpe": None,
            "active_days": 0,
            "empty_book_days": 0,
            "avg_n": 0,
            "series": [],
        }
    arr = np.array(daily_rets, dtype=float)
    sharpe = float(arr.mean() / arr.std() * np.sqrt(252)) if arr.std() > 1e-12 else 0.0
    return {
        "days": len(arr),
        "avg_daily_pct": round(float(arr.mean()), 4),
        "win_days_pct": round(win_days / max(active, 1) * 100, 2) if active else 0.0,
        "total_return_pct": round((nav - 1.0) * 100, 4),
        "sharpe": round(sharpe, 3),
        "active_days": active,
        "empty_book_days": empty,
        "avg_n": round(n_picks_sum / max(active, 1), 2) if active else 0,
        "series": series,
    }


def score_objective(train: dict[str, Any], test: dict[str, Any]) -> float:
    """综合目标：优先测试集日均收益 + 胜率 + sharpe，惩罚过拟合。"""
    if not test or test.get("days", 0) < 5:
        return -999.0
    t_avg = float(test.get("avg_daily_pct") or 0)
    t_win = float(test.get("win_days_pct") or 0) / 100.0
    t_sh = float(test.get("sharpe") or 0)
    tr_avg = float(train.get("avg_daily_pct") or 0)
    # 过拟合惩罚：训练集远好于测试集
    gap = max(0.0, tr_avg - t_avg)
    # 空仓过多略惩罚（没有交易也没有信息）
    empty_ratio = float(test.get("empty_book_days") or 0) / max(float(test.get("days") or 1), 1)
    empty_pen = 0.15 * empty_ratio
    return 1.6 * t_avg + 0.8 * t_win + 0.25 * t_sh - 0.9 * gap - empty_pen


def candidate_weights() -> list[tuple[str, dict[str, float], str]]:
    """生成权重候选：(id, weights, note)."""
    cands: list[tuple[str, dict[str, float], str]] = []
    # 1) production
    cands.append(("w_prod", dict(PROD_W), "生产 v4.3 权重"))

    # 2) equal
    eq = {f: 1.0 / len(FACTORS) for f in FACTORS}
    cands.append(("w_equal", eq, "等权"))

    # 3) confirm-heavy / chip-light family
    confirm = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support", "rsi_volume"]
    core_chip = ["chip_concentration", "volatility_squeeze"]
    for conf_share in (0.55, 0.65, 0.75):
        chip_share = 1.0 - conf_share - 0.12  # leave for mom/liq/kdj
        if chip_share < 0.1:
            continue
        w = {f: 0.0 for f in FACTORS}
        for f in confirm:
            w[f] = conf_share / len(confirm)
        for f in core_chip:
            w[f] = chip_share / len(core_chip)
        w["momentum"] = 0.06
        w["liquidity"] = 0.04
        w["kdj_position"] = 0.02
        cands.append((f"w_confirm_{int(conf_share*100)}", normalize_w(w), f"确认族{int(conf_share*100)}%"))

    # 4) drop weak factors systematically
    drop_sets = [
        (["kdj_position"], "drop_kdj"),
        (["kdj_position", "liquidity"], "drop_kdj_liq"),
        (["kdj_position", "rsi_volume"], "drop_kdj_rsi"),
        (["chip_concentration"], "drop_chip_conc"),
        (["volatility_squeeze"], "drop_squeeze"),
        (["kdj_position", "chip_concentration"], "drop_kdj_chip"),
    ]
    for drops, tag in drop_sets:
        w = dict(PROD_W)
        for d in drops:
            w[d] = 0.0
        # redistribute mass to confirm factors
        mass = sum(PROD_W[d] for d in drops)
        for f in confirm:
            w[f] = w.get(f, 0) + mass / len(confirm)
        cands.append((f"w_{tag}", normalize_w(w), f"消融{','.join(drops)}"))

    # 5) momentum boosts
    for mom_w in (0.08, 0.12, 0.16, 0.20):
        w = dict(PROD_W)
        delta = mom_w - w["momentum"]
        w["momentum"] = mom_w
        # take from chip_concentration
        w["chip_concentration"] = max(0.05, w["chip_concentration"] - delta)
        cands.append((f"w_mom_{int(mom_w*100)}", normalize_w(w), f"动量{int(mom_w*100)}%"))

    # 6) dirichlet random around production (seeded)
    rng = np.random.default_rng(20260716)
    base = np.array([PROD_W[f] for f in FACTORS], dtype=float)
    base = np.clip(base, 0.01, None)
    for i in range(80):
        # concentration: higher alpha -> closer to base
        alpha = base * rng.uniform(8, 40)
        draw = rng.dirichlet(alpha)
        w = {f: float(draw[j]) for j, f in enumerate(FACTORS)}
        cands.append((f"w_dir_{i:03d}", w, "Dirichlet扰动生产权重"))

    # 7) extreme two-factor focus (keep small residual)
    pairs = list(itertools.combinations(
        ["macd_early_signal", "turnover_stability", "volume_warmup", "chip_support", "momentum", "volatility_squeeze"],
        2,
    ))
    for a, b in pairs:
        w = {f: 0.02 for f in FACTORS}
        w[a] = 0.40
        w[b] = 0.40
        cands.append((f"w_pair_{a[:6]}_{b[:6]}", normalize_w(w), f"双核{a}+{b}"))

    # 8) IC-style heuristic: inverse to "chip heaviness"
    w = dict(PROD_W)
    w["chip_concentration"] *= 0.5
    w["macd_early_signal"] *= 1.4
    w["volume_warmup"] *= 1.5
    w["momentum"] *= 1.8
    w["turnover_stability"] *= 1.2
    cands.append(("w_anti_quiet", normalize_w(w), "反安静票：降筹码提确认/动量"))

    # dedupe by rounded weights
    seen = set()
    uniq = []
    for cid, w, note in cands:
        key = tuple(round(normalize_w(w)[f], 4) for f in FACTORS)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((cid, normalize_w(w), note))
    return uniq


def candidate_gates() -> list[tuple[str, dict[str, Any]]]:
    confirm_keys = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"]
    base = []
    for max_names in (5, 8, 12):
        for max_ind in (2, 3):
            for pct in (85, 90, 95):
                for min_hits in (1, 2):
                    for mkt in (True, False):
                        gid = f"g_n{max_names}_i{max_ind}_p{pct}_c{min_hits}_m{int(mkt)}"
                        base.append(
                            (
                                gid,
                                {
                                    "max_names": max_names,
                                    "max_per_industry": max_ind,
                                    "score_percentile": pct,
                                    "min_score_floor": 68 if pct >= 90 else 65,
                                    "min_confirm_hits": min_hits,
                                    "confirm_subscore_min": 60,
                                    "confirm_keys": confirm_keys,
                                    "market_gate": mkt,
                                    "cost_pct": COST_PCT,
                                },
                            )
                        )
    # keep a leaner subset: market_gate True primary + a few False
    lean = [g for g in base if g[1]["market_gate"] is True]
    lean += [g for g in base if g[1]["market_gate"] is False and g[1]["max_names"] in (8, 12) and g[1]["score_percentile"] == 90][:6]
    return lean


def write_exp_yaml(rank: int, item: dict[str, Any]) -> Path:
    path = EXP_DIR / f"exp_search_top{rank}_{item['id']}.yaml"
    body = {
        "id": f"exp_search_top{rank}_{item['id']}",
        "name": f"搜索Top{rank}·{item.get('note') or item['id']}",
        "status": "active",
        "hypothesis": (
            f"自动搜索综合分={item.get('objective'):.4f}；"
            f"测试集日均={item['test'].get('avg_daily_pct')}% 胜率日={item['test'].get('win_days_pct')}% "
            f"累计={item['test'].get('total_return_pct')}%；"
            f"相对生产对照超额日均={item.get('edge_vs_prod')}%。"
            f"配置：{item.get('note')}"
        ),
        "inspired_by": [
            "算法搜索：Dirichlet扰动/消融/确认提权/动量提权/双核",
            "外来库共性：稀疏持仓、确认过滤、弱因子剔除",
            "内部诊断：筹码过重、KDJ装饰",
        ],
        "production_frozen": True,
        "weights": {k: round(float(v), 6) for k, v in item["weights"].items()},
        "disabled_factors": [k for k, v in item["weights"].items() if float(v) <= 1e-9],
        "subscore_multipliers": {},
        "gates": item["gates"],
        "backtest": {"lookback_days": item.get("lookback", 60), "cost_pct": COST_PCT},
        "search_metrics": {
            "objective": item.get("objective"),
            "train": item.get("train"),
            "test": item.get("test"),
            "full": item.get("full"),
            "edge_vs_prod": item.get("edge_vs_prod"),
        },
    }
    path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=60)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--trade-date", default="")
    ap.add_argument("--max-combos", type=int, default=0, help="0=auto")
    args = ap.parse_args()
    ensure_dirs()

    td = shadow.resolve_trade_date(args.trade_date or None)
    all_dates = [d for d in shadow.list_trade_dates() if d <= td and (shadow.CACHE / f"stk_factor_{d}.parquet").exists()]
    if len(all_dates) < 25:
        print("[search] insufficient dates", len(all_dates))
        return 1
    dates = all_dates[-(args.lookback + 1) :]
    pairs = list(zip(dates[:-1], dates[1:]))
    # walk-forward split 70/30
    split = max(10, int(len(pairs) * 0.7))
    train_pairs = pairs[:split]
    test_pairs = pairs[split:]
    print(f"[search] trade_date={td} window={dates[0]}→{dates[-1]} pairs={len(pairs)} train={len(train_pairs)} test={len(test_pairs)}")

    # preload score panel (slow once)
    panel = preload_panel(dates[:-1])  # signal dates
    if len(panel) < 15:
        print("[search] panel too small")
        return 1

    ind_map = industry_map()
    o2c_cache: dict[str, dict[str, float]] = {}
    mom_cap_cache: dict[str, int] = {}

    weights_list = candidate_weights()
    gates_list = candidate_gates()
    print(f"[search] weight_candidates={len(weights_list)} gate_candidates={len(gates_list)}")

    # production reference with baseline gates
    base_gates = {
        "max_names": 8,
        "max_per_industry": 3,
        "score_percentile": 90,
        "min_score_floor": 70,
        "min_confirm_hits": 2,
        "confirm_subscore_min": 60,
        "confirm_keys": ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"],
        "market_gate": True,
        "cost_pct": COST_PCT,
    }
    prod_full = evaluate_strategy(panel, pairs, PROD_W, base_gates, o2c_cache, ind_map, mom_cap_cache)
    print(f"[search] production control full avg_daily={prod_full.get('avg_daily_pct')} total={prod_full.get('total_return_pct')}")

    # Stage A: evaluate all weights under base_gates quickly
    weight_results = []
    for i, (wid, w, note) in enumerate(weights_list):
        train = evaluate_strategy(panel, train_pairs, w, base_gates, o2c_cache, ind_map, mom_cap_cache)
        test = evaluate_strategy(panel, test_pairs, w, base_gates, o2c_cache, ind_map, mom_cap_cache)
        full = evaluate_strategy(panel, pairs, w, base_gates, o2c_cache, ind_map, mom_cap_cache)
        obj = score_objective(train, test)
        edge = None
        if full.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
            edge = round(float(full["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
        weight_results.append(
            {
                "id": wid,
                "note": note,
                "weights": w,
                "gates": base_gates,
                "train": {k: train[k] for k in train if k != "series"},
                "test": {k: test[k] for k in test if k != "series"},
                "full": {k: full[k] for k in full if k != "series"},
                "full_series": full.get("series"),
                "objective": round(obj, 5),
                "edge_vs_prod": edge,
            }
        )
        if (i + 1) % 20 == 0:
            print(f"[search] weights progress {i+1}/{len(weights_list)} best_obj={max(r['objective'] for r in weight_results):.4f}")

    weight_results.sort(key=lambda r: r["objective"], reverse=True)
    top_w = weight_results[:12]
    print("[search] top weights:")
    for r in top_w[:5]:
        print(f"  {r['id']} obj={r['objective']} test_avg={r['test'].get('avg_daily_pct')} edge={r['edge_vs_prod']} | {r['note']}")

    # Stage B: gate search on top weights
    combos = []
    for wr in top_w:
        for gid, gates in gates_list:
            train = evaluate_strategy(panel, train_pairs, wr["weights"], gates, o2c_cache, ind_map, mom_cap_cache)
            test = evaluate_strategy(panel, test_pairs, wr["weights"], gates, o2c_cache, ind_map, mom_cap_cache)
            full = evaluate_strategy(panel, pairs, wr["weights"], gates, o2c_cache, ind_map, mom_cap_cache)
            obj = score_objective(train, test)
            edge = None
            if full.get("avg_daily_pct") is not None and prod_full.get("avg_daily_pct") is not None:
                edge = round(float(full["avg_daily_pct"]) - float(prod_full["avg_daily_pct"]), 4)
            combos.append(
                {
                    "id": f"{wr['id']}__{gid}",
                    "note": f"{wr['note']} + {gid}",
                    "weights": wr["weights"],
                    "gates": gates,
                    "train": {k: train[k] for k in train if k != "series"},
                    "test": {k: test[k] for k in test if k != "series"},
                    "full": {k: full[k] for k in full if k != "series"},
                    "full_series": full.get("series"),
                    "objective": round(obj, 5),
                    "edge_vs_prod": edge,
                    "lookback": args.lookback,
                }
            )
    combos.sort(key=lambda r: r["objective"], reverse=True)

    # diversify top3: not all same weight family
    selected: list[dict[str, Any]] = []
    used_w = set()
    for c in combos:
        wkey = tuple(round(c["weights"][f], 4) for f in FACTORS)
        if wkey in used_w and len(selected) < args.top:
            # allow one duplicate family only if much better empty
            continue
        selected.append(c)
        used_w.add(wkey)
        if len(selected) >= args.top:
            break
    # fill if needed
    for c in combos:
        if len(selected) >= args.top:
            break
        if c in selected:
            continue
        selected.append(c)

    print("[search] TOP RESULTS:")
    for i, c in enumerate(selected, 1):
        print(
            f"  #{i} {c['id']}\n"
            f"     obj={c['objective']} edge_vs_prod={c['edge_vs_prod']}\n"
            f"     test avg={c['test'].get('avg_daily_pct')}% win={c['test'].get('win_days_pct')}% total={c['test'].get('total_return_pct')}%\n"
            f"     full avg={c['full'].get('avg_daily_pct')}% sharpe={c['full'].get('sharpe')} active={c['full'].get('active_days')}\n"
            f"     {c['note']}"
        )

    # archive old search yamls status optional - write new top yamls
    # deactivate previous search_top yamls
    for p in EXP_DIR.glob("exp_search_top*.yaml"):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw["status"] = "archived"
            raw["archived_at"] = now_str()
            p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    written = []
    for i, c in enumerate(selected, 1):
        path = write_exp_yaml(i, c)
        written.append(str(path))
        print(f"[search] wrote {path}")

    report = {
        "generated_at": now_str(),
        "trade_date": td,
        "lookback": args.lookback,
        "n_weight_candidates": len(weights_list),
        "n_gate_candidates": len(gates_list),
        "n_combos_evaluated": len(combos),
        "production_control": {k: prod_full[k] for k in prod_full if k != "series"},
        "top": [
            {
                **{k: v for k, v in c.items() if k != "full_series"},
            }
            for c in selected
        ],
        "leaderboard_top20": [
            {
                "id": c["id"],
                "objective": c["objective"],
                "edge_vs_prod": c["edge_vs_prod"],
                "test_avg_daily_pct": c["test"].get("avg_daily_pct"),
                "test_win_days_pct": c["test"].get("win_days_pct"),
                "test_total_return_pct": c["test"].get("total_return_pct"),
                "full_avg_daily_pct": c["full"].get("avg_daily_pct"),
                "full_sharpe": c["full"].get("sharpe"),
                "note": c["note"],
            }
            for c in combos[:20]
        ],
        "experiment_files": written,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[search] report {REPORT_PATH}")

    # refresh factory public page with new active experiments (only top search + keep non-search active optional)
    # deactivate non-search experiments to focus observation on winners? User wants 2-3 best - keep search tops active, archive old manual ones or leave them.
    # Leave baseline active for comparison.
    for p in EXP_DIR.glob("exp_*.yaml"):
        if p.name.startswith("exp_search_top"):
            continue
        if p.name.startswith("exp_baseline"):
            continue
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if raw.get("status") == "active" and not str(raw.get("id", "")).startswith("exp_search_top"):
                # keep baseline; archive other manual experiments to declutter
                if raw.get("id") != "exp_baseline_gates":
                    raw["status"] = "archived"
                    raw["archived_reason"] = "superseded_by_auto_search"
                    p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    print("[search] running factory with winners ...")
    fac.run_factory(td, lookback=min(args.lookback, 40))
    print("[search] DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
