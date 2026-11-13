#!/usr/bin/env python3
"""启动前夕 · 因子二次加工厂（Factory）

定位：
  - 原料 = 生产 score_stock_prebreakout 的子分（不修改 pipeline.PREBREAKOUT_CONFIG）
  - 加工 = 实验配置中的权重重组 / 禁用因子 / 子分乘子 / 门控
  - 质检 = 与生产权重对照的 T+1 开→收回测 + 当日名单
  - 外来库 = 仅作为实验 hypothesis/inspired_by 文档，不直接灌装代码

用法：
  ~/.openclaw/venv/bin/python prebreakout_factory.py
  ~/.openclaw/venv/bin/python prebreakout_factory.py --trade-date 20260715 --lookback 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from copy import deepcopy
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

import backtest as bt  # noqa: E402
import pipeline as pl  # noqa: E402
import prebreakout_recipe_engine as eng  # noqa: E402
import prebreakout_shadow_research as shadow  # noqa: E402

EXP_DIR = (
    WORKSPACE
    / "stock_data"
    / "03-working"
    / "strategy_research"
    / "prebreakout_shadow"
    / "experiments"
)
FACTORY_DIR = WORKSPACE / "stock_data" / "03-working" / "strategy_research" / "prebreakout_shadow" / "factory"
HEALTH_DIR = WORKSPACE / "stock_data" / "03-working" / "health"
FACTORY_LATEST = HEALTH_DIR / "prebreakout_factory_latest.json"
FACTORY_STATUS = HEALTH_DIR / "prebreakout_factory_status.json"
PUBLISHED_WATCH = WORKSPACE / "stock-report" / "data" / "latest" / "prebreakout_factory_watch.json"
WORKING_WATCH = (
    WORKSPACE
    / "stock_data"
    / "03-working"
    / "stock-report-repo"
    / "data"
    / "latest"
    / "prebreakout_factory_watch.json"
)

PROD_FACTORS = list(pl.PREBREAKOUT_CONFIG["factors"].keys())
PROD_WEIGHTS = {k: float(v["weight"]) for k, v in pl.PREBREAKOUT_CONFIG["factors"].items()}
PROD_VERSION = str(pl.PREBREAKOUT_CONFIG.get("version") or "4.x")


def now_str() -> str:
    return eng.now_str()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    eng.write_json_atomic(path, payload)


def _status_running(run_id: str, trade_date: str, started_at: str) -> None:
    write_json(
        FACTORY_STATUS,
        {
            "ok": None,
            "state": "running",
            "run_id": run_id,
            "started_at": started_at,
            "trade_date": trade_date,
            "pid": os.getpid(),
            "production_config_fingerprint": eng.production_config_fingerprint(),
        },
    )


def _status_final(**payload: Any) -> None:
    write_json(FACTORY_STATUS, payload)


def load_experiments(exp_dir: Path = EXP_DIR) -> list[dict[str, Any]]:
    if not exp_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(exp_dir.glob("*.yaml")) + sorted(exp_dir.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"[factory] skip bad yaml {path.name}: {exc}")
            continue
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        raw["_path"] = str(path)
        out.append(raw)
    return out


def production_weights() -> dict[str, float]:
    return dict(PROD_WEIGHTS)


def resolve_weights(exp: dict[str, Any]) -> dict[str, float]:
    """实验权重 → 归一化；null = 生产权重；disabled → 0。"""
    base = production_weights()
    custom = exp.get("weights")
    if custom is None:
        weights = dict(base)
    else:
        weights = {k: float(base.get(k, 0.0)) for k in PROD_FACTORS}
        for k, v in (custom or {}).items():
            if k in weights:
                weights[k] = float(v)
    for k in exp.get("disabled_factors") or []:
        if k in weights:
            weights[k] = 0.0
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 1e-12:
        return production_weights()
    return {k: max(0.0, w) / total for k, w in weights.items()}


def weight_delta(exp_w: dict[str, float]) -> list[dict[str, Any]]:
    rows = []
    for k in PROD_FACTORS:
        pw = PROD_WEIGHTS[k]
        ew = float(exp_w.get(k, 0.0))
        rows.append(
            {
                "factor": k,
                "desc": (pl.PREBREAKOUT_CONFIG["factors"].get(k) or {}).get("desc") or k,
                "production": round(pw, 4),
                "experiment": round(ew, 4),
                "delta": round(ew - pw, 4),
            }
        )
    return rows


def rescore_row(sub_scores: dict[str, Any], weights: dict[str, float], multipliers: dict[str, float] | None = None) -> float:
    mult = multipliers or {}
    total = 0.0
    for k, w in weights.items():
        if w <= 0:
            continue
        try:
            s = float(sub_scores.get(k) or 0.0)
        except (TypeError, ValueError):
            s = 0.0
        m = float(mult.get(k, 1.0) or 1.0)
        total += s * m * w
    return round(float(total), 1)


def score_universe(trade_date: str) -> list[dict[str, Any]]:
    """生产硬过滤 + 生产子分（原料）。"""
    stk = shadow.load_parquet("stk_factor", trade_date)
    cyq = shadow.load_parquet("cyq_perf", trade_date)
    if stk is None or len(stk) == 0:
        return []
    if cyq is None:
        cyq = pd.DataFrame()
    if "pct_change" not in stk.columns and "pct_chg" in stk.columns:
        stk = stk.copy()
        stk["pct_change"] = stk["pct_chg"]
    scored = bt.score_from_bulk(stk, cyq, mode="prebreakout", trade_date=trade_date)
    # 附带生产分，便于对照
    for i, row in enumerate(scored):
        row["prod_score"] = row.get("score")
        row["prod_rank"] = i + 1
    return scored


def gates_from_exp(exp: dict[str, Any]) -> dict[str, Any]:
    g = dict(exp.get("gates") or {})
    g.setdefault("max_names", 8)
    g.setdefault("max_per_industry", 3)
    g.setdefault("score_percentile", 90)
    g.setdefault("min_score_floor", 70)
    g.setdefault("min_confirm_hits", 2)
    g.setdefault("confirm_subscore_min", 60)
    g.setdefault(
        "confirm_keys",
        ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"],
    )
    g.setdefault("market_gate", True)
    return g


def apply_experiment_selection(
    scored: list[dict[str, Any]],
    exp: dict[str, Any],
    industry_map: dict[str, str],
    name_map: dict[str, str],
    mom20: float | None,
    mom_src: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    weights = resolve_weights(exp)
    mult = exp.get("subscore_multipliers") or {}
    gates = gates_from_exp(exp)

    rescored: list[dict[str, Any]] = []
    for row in scored:
        subs = row.get("sub_scores") or {}
        if not isinstance(subs, dict):
            subs = {}
        exp_score = rescore_row(subs, weights, mult)
        item = dict(row)
        item["score"] = exp_score
        item["sub_scores"] = subs
        item["confirm_hits"] = shadow.confirm_hits(subs) if hasattr(shadow, "confirm_hits") else _confirm_hits(subs, gates)
        # use experiment confirm logic
        item["confirm_hits"] = _confirm_hits(subs, gates)
        rescored.append(item)
    rescored.sort(key=lambda x: float(x.get("score") or 0), reverse=True)

    if gates.get("market_gate"):
        market_cap, market_note = shadow.market_cap_from_mom20(mom20, mom_src)
        # respect experiment max_names as upper bound
        market_cap = min(int(market_cap), int(gates["max_names"]))
    else:
        market_cap, market_note = int(gates["max_names"]), "market_gate_off"

    # temporarily override shadow module constants via local selection
    picks, meta = _select_with_gates(
        rescored,
        industry_map,
        name_map,
        market_cap=market_cap,
        market_note=market_note,
        gates=gates,
    )
    meta["weights"] = {k: round(v, 4) for k, v in weights.items()}
    meta["weight_delta"] = weight_delta(weights)
    meta["experiment_id"] = exp.get("id")
    return picks, meta


def _confirm_hits(subs: dict[str, Any], gates: dict[str, Any]) -> int:
    keys = gates.get("confirm_keys") or []
    thr = float(gates.get("confirm_subscore_min") or 60)
    n = 0
    for k in keys:
        try:
            if float(subs.get(k) or 0) >= thr:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def _select_with_gates(
    scored: list[dict[str, Any]],
    industry_map: dict[str, str],
    name_map: dict[str, str],
    market_cap: int,
    market_note: str,
    gates: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "universe_after_hard_filter": len(scored),
        "market_cap": market_cap,
        "market_note": market_note,
        "score_percentile": gates.get("score_percentile"),
        "min_score_floor": gates.get("min_score_floor"),
        "min_confirm_hits": gates.get("min_confirm_hits"),
        "max_per_industry": gates.get("max_per_industry"),
        "max_names": gates.get("max_names"),
    }
    if market_cap <= 0 or not scored:
        meta.update({"threshold_score": None, "selected": 0, "empty_book": True, "candidates_after_score_confirm": 0})
        return [], meta

    scores = np.array([float(x.get("score") or 0) for x in scored], dtype=float)
    scores = scores[np.isfinite(scores)]
    thr = float(np.nanpercentile(scores, float(gates.get("score_percentile") or 90))) if len(scores) else 70.0
    if not np.isfinite(thr):
        thr = float(gates.get("min_score_floor") or 70)
    thr = max(thr, float(gates.get("min_score_floor") or 70))
    meta["threshold_score"] = round(thr, 2)

    prod_rank = {str(x.get("ts_code")): x.get("prod_rank") for x in scored}
    min_hits = int(gates.get("min_confirm_hits") or 0)
    max_ind = int(gates.get("max_per_industry") or 3)

    candidates: list[dict[str, Any]] = []
    for row in scored:
        sc = float(row.get("score") or 0)
        if sc < thr:
            continue
        hits = int(row.get("confirm_hits") or 0)
        if hits < min_hits:
            continue
        tc = str(row["ts_code"])
        ind = industry_map.get(tc) or "未知"
        if ind in ("nan", "None", ""):
            ind = "未知"
        candidates.append(
            {
                "ts_code": tc,
                "name": name_map.get(tc) or row.get("name") or tc,
                "score": sc,
                "prod_score": row.get("prod_score"),
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
                "industry": ind,
                "confirm_hits": hits,
                "prod_rank": prod_rank.get(tc) or row.get("prod_rank"),
                "sub_scores": {
                    k: round(float(v), 1)
                    for k, v in (row.get("sub_scores") or {}).items()
                    if isinstance(v, (int, float))
                },
            }
        )
    meta["candidates_after_score_confirm"] = len(candidates)
    candidates.sort(key=lambda x: (-float(x["score"]), -int(x["confirm_hits"])))
    picked: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for c in candidates:
        if len(picked) >= market_cap:
            break
        ind = c["industry"]
        if industry_counts.get(ind, 0) >= max_ind:
            continue
        industry_counts[ind] = industry_counts.get(ind, 0) + 1
        c = dict(c)
        c["shadow_rank"] = len(picked) + 1
        picked.append(c)
    meta["selected"] = len(picked)
    meta["industry_counts"] = industry_counts
    meta["empty_book"] = len(picked) == 0
    return picked, meta


def t1_o2c_map(trade_date: str, next_date: str) -> dict[str, float]:
    """T+1 开→收：key=ts_code, value=o2c%."""
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


def _day_stats_from_codes(
    codes: list[str],
    o2c: dict[str, float],
    nav: float,
    cost: float,
    signal_date: str,
    trade_date: str,
) -> tuple[float, dict[str, Any]]:
    rets = [o2c[c] for c in codes if c in o2c]
    if not rets:
        avg = 0.0
        win = None
        n = 0
    else:
        avg = float(np.mean(rets)) - cost
        win = float(np.mean([1.0 if r > 0 else 0.0 for r in rets]) * 100)
        n = len(rets)
    nav2 = nav * (1.0 + avg / 100.0)
    return nav2, {
        "signal_date": signal_date,
        "trade_date": trade_date,
        "n": n,
        "avg_o2c_pct": round(avg, 4),
        "win_rate_pct": None if win is None else round(win, 2),
        "empty_book": n == 0,
    }


def _summarize_series(series: list[dict[str, Any]], nav: float) -> dict[str, Any]:
    if not series:
        return {
            "days": 0,
            "avg_daily_pct": None,
            "win_days_pct": None,
            "total_return_pct": None,
            "sharpe": None,
            "active_days": 0,
            "empty_book_days": 0,
        }
    avgs = [r["avg_o2c_pct"] for r in series if r.get("avg_o2c_pct") is not None]
    nonempty = [r for r in series if not r.get("empty_book")]
    win_days = sum(1 for r in nonempty if (r.get("avg_o2c_pct") or 0) > 0)
    arr = np.array(avgs, dtype=float) if avgs else np.array([0.0])
    sharpe = float(arr.mean() / arr.std() * np.sqrt(252)) if arr.std() > 1e-12 else 0.0
    return {
        "days": len(series),
        "active_days": len(nonempty),
        "avg_daily_pct": round(float(arr.mean()), 4),
        "win_days_pct": round(win_days / max(len(nonempty), 1) * 100, 2) if nonempty else None,
        "total_return_pct": round((nav - 1.0) * 100, 4),
        "sharpe": round(sharpe, 3),
        "empty_book_days": sum(1 for r in series if r.get("empty_book")),
    }


def backtest_real_production(
    dates: list[str],
    cost: float,
) -> dict[str, Any]:
    """真实生产名单对照：读 selection_history，缺失日记 unavailable。"""
    series: list[dict[str, Any]] = []
    nav = 1.0
    covered = 0
    missing = 0
    for i, td in enumerate(dates[:-1]):
        nd = dates[i + 1]
        o2c = t1_o2c_map(td, nd)
        if not o2c:
            continue
        prod = shadow.load_prod_top20(td) or []
        codes = [str(p.get("ts_code") or p.get("code") or "") for p in prod]
        codes = [c for c in codes if c]
        if not codes:
            missing += 1
            series.append(
                {
                    "signal_date": td,
                    "trade_date": nd,
                    "n": 0,
                    "avg_o2c_pct": None,
                    "win_rate_pct": None,
                    "empty_book": True,
                    "control_available": False,
                }
            )
            continue
        covered += 1
        # 仅用有对照的日参与 nav；缺失日不摊 0 以免虚高
        rets = [o2c[c] for c in codes if c in o2c]
        if not rets:
            missing += 1
            series.append(
                {
                    "signal_date": td,
                    "trade_date": nd,
                    "n": 0,
                    "avg_o2c_pct": None,
                    "control_available": False,
                    "empty_book": True,
                }
            )
            continue
        avg = float(np.mean(rets)) - cost
        nav *= 1.0 + avg / 100.0
        series.append(
            {
                "signal_date": td,
                "trade_date": nd,
                "n": len(rets),
                "avg_o2c_pct": round(avg, 4),
                "win_rate_pct": round(float(np.mean([1.0 if r > 0 else 0.0 for r in rets]) * 100), 2),
                "empty_book": False,
                "control_available": True,
                "cum_nav_pct": round((nav - 1.0) * 100, 4),
            }
        )

    usable = [r for r in series if r.get("control_available") and r.get("avg_o2c_pct") is not None]
    if len(usable) < max(5, (len(dates) - 1) // 4):
        status = "unavailable"
    elif missing > 0:
        status = "partial"
    else:
        status = "available"

    if usable:
        avgs = [float(r["avg_o2c_pct"]) for r in usable]
        arr = np.array(avgs, dtype=float)
        sharpe = float(arr.mean() / arr.std() * np.sqrt(252)) if arr.std() > 1e-12 else 0.0
        summary = {
            "days": len(usable),
            "active_days": len(usable),
            "avg_daily_pct": round(float(arr.mean()), 4),
            "win_days_pct": round(sum(1 for a in avgs if a > 0) / max(len(avgs), 1) * 100, 2),
            "total_return_pct": round((nav - 1.0) * 100, 4) if status != "unavailable" else None,
            "sharpe": round(sharpe, 3),
            "empty_book_days": 0,
            "coverage_days": covered,
            "missing_days": missing,
        }
    else:
        summary = {
            "days": 0,
            "active_days": 0,
            "avg_daily_pct": None,
            "win_days_pct": None,
            "total_return_pct": None,
            "sharpe": None,
            "empty_book_days": 0,
            "coverage_days": covered,
            "missing_days": missing,
        }

    return {
        "kind": "real_selection_history",
        "status": status,
        "summary": summary,
        "daily_series": series,
        "note": "真实生产 Top 名单 T+1 开→收；缺历史日则 control_unavailable，禁止称战胜生产。",
    }


def backtest_experiment(
    exp: dict[str, Any],
    dates: list[str],
    industry_map: dict[str, str],
    name_map: dict[str, str],
    real_prod_control: dict[str, Any] | None = None,
    *,
    score_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """逐日：加工名单 → 次日开→收；研究基线=生产权重+实验门控；真生产对照单独字段。"""
    bt_cfg = exp.get("backtest") or {}
    cost = float(bt_cfg.get("cost_pct") or 0.26)
    # 研究基线：生产权重 + 本实验门控（非真实生产策略）
    baseline_exp = {
        "id": "research_baseline_prod_weights",
        "weights": None,
        "disabled_factors": [],
        "gates": gates_from_exp(exp),
    }

    series_exp: list[dict[str, Any]] = []
    series_base: list[dict[str, Any]] = []
    excluded_proxy_dates: list[str] = []
    proxy_exclusion_reasons: dict[str, str] = {}
    nav_e, nav_b = 1.0, 1.0

    for i, td in enumerate(dates[:-1]):
        nd = dates[i + 1]
        try:
            if score_cache is not None and td in score_cache:
                scored = score_cache[td]
            else:
                scored = score_universe(td)
                if score_cache is not None:
                    score_cache[td] = scored
        except bt.ProxyDataForbidden as exc:
            # Historical proxy chip caches are intentionally forbidden from
            # ranking/backtests. Exclude the contaminated signal day while
            # preserving the original next-trading-day pairing for clean days.
            # This is an explicit degraded research sample, never a proxy score.
            excluded_proxy_dates.append(td)
            proxy_exclusion_reasons[td] = str(exc)
            continue
        if len(scored) < 50:
            continue
        mom20, mom_src = shadow.market_mom20(td)
        picks_e, _ = apply_experiment_selection(scored, exp, industry_map, name_map, mom20, mom_src)
        picks_b, _ = apply_experiment_selection(scored, baseline_exp, industry_map, name_map, mom20, mom_src)
        o2c = t1_o2c_map(td, nd)
        if not o2c:
            continue

        nav_e, row_e = _day_stats_from_codes(
            [p["ts_code"] for p in picks_e], o2c, nav_e, cost, td, nd
        )
        nav_b, row_b = _day_stats_from_codes(
            [p["ts_code"] for p in picks_b], o2c, nav_b, cost, td, nd
        )
        row_e["cum_nav_pct"] = round((nav_e - 1.0) * 100, 4)
        row_b["cum_nav_pct"] = round((nav_b - 1.0) * 100, 4)
        series_exp.append(row_e)
        series_base.append(row_b)

    se = _summarize_series(series_exp, nav_e)
    sb = _summarize_series(series_base, nav_b)
    edge_vs_baseline = None
    if se.get("avg_daily_pct") is not None and sb.get("avg_daily_pct") is not None:
        edge_vs_baseline = round(float(se["avg_daily_pct"]) - float(sb["avg_daily_pct"]), 4)

    real = real_prod_control or backtest_real_production(dates, cost)
    edge_vs_real = None
    rs = (real.get("summary") or {})
    if (
        real.get("status") in ("available", "partial")
        and se.get("avg_daily_pct") is not None
        and rs.get("avg_daily_pct") is not None
    ):
        edge_vs_real = round(float(se["avg_daily_pct"]) - float(rs["avg_daily_pct"]), 4)

    beats_real = bool(edge_vs_real is not None and edge_vs_real > 0 and real.get("status") == "available")

    return {
        "experiment_id": exp.get("id"),
        "lookback_dates": len(dates) - 1,
        "data_quality": {
            "policy": "proxy_dates_excluded_without_scoring",
            "proxy_signal_dates_excluded": len(excluded_proxy_dates),
            "excluded_proxy_dates": excluded_proxy_dates,
            "exclusion_reasons": proxy_exclusion_reasons,
            "evaluated_signal_dates": len(series_exp),
        },
        "cost_pct": cost,
        "experiment": {"summary": se, "daily_series": series_exp},
        # 兼容旧字段名，但明确 kind，禁止 UI 把它叫「真生产」
        "production_control": {
            "kind": "research_baseline_prod_weights_plus_exp_gates",
            "status": "available",
            "summary": sb,
            "daily_series": series_base,
            "note": "研究基线：生产权重 + 本实验门控，不是真实生产策略。",
        },
        "research_baseline": {
            "kind": "research_baseline_prod_weights_plus_exp_gates",
            "status": "available",
            "summary": sb,
            "daily_series": series_base,
        },
        "real_production_control": real,
        "edge_avg_daily_pct": edge_vs_baseline,  # 相对研究基线（兼容旧字段）
        "edge_vs_research_baseline": edge_vs_baseline,
        "edge_vs_real_production": edge_vs_real,
        "beats_production": beats_real,  # 仅真生产 available 时才可能为 True
        "beats_research_baseline": bool(edge_vs_baseline is not None and edge_vs_baseline > 0),
        "control_label": (
            "vs_real_production"
            if real.get("status") == "available"
            else "vs_research_baseline_only"
        ),
    }


def run_factory(
    trade_date: str,
    lookback: int = 30,
    force_champion: str | None = None,
) -> dict[str, Any]:
    run_id = os.environ.get("OPENCLAW_RUN_ID") or str(uuid.uuid4())
    started_at = now_str()
    _status_running(run_id, trade_date, started_at)

    lock_path = HEALTH_DIR / "prebreakout_factory.lock"
    lock_fd, got_lock = eng.try_flock(lock_path)
    if not got_lock:
        _status_final(
            ok=False,
            state="lock_busy",
            run_id=run_id,
            started_at=started_at,
            finished_at=now_str(),
            trade_date=trade_date,
            error="lock_busy: another factory instance is running",
            production_config_fingerprint=eng.production_config_fingerprint(),
        )
        print("[factory] WARN lock_busy — skip")
        return {"ok": False, "error": "lock_busy", "trade_date": trade_date}

    try:
        return _run_factory_body(
            trade_date=trade_date,
            lookback=lookback,
            force_champion=force_champion,
            run_id=run_id,
            started_at=started_at,
        )
    finally:
        if lock_fd is not None:
            try:
                import fcntl

                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass


def _run_factory_body(
    trade_date: str,
    lookback: int,
    force_champion: str | None,
    run_id: str,
    started_at: str,
) -> dict[str, Any]:
    FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    fp = eng.production_config_fingerprint()
    exps = [e for e in load_experiments() if str(e.get("status") or "active") == "active"]
    if not exps:
        _status_final(
            ok=False,
            state="failed",
            run_id=run_id,
            started_at=started_at,
            finished_at=now_str(),
            trade_date=trade_date,
            error="no active experiments",
            production_config_fingerprint=fp,
        )
        raise SystemExit("[factory] no active experiments in " + str(EXP_DIR))

    industry_map = shadow.load_industry_map()
    name_map = shadow.load_name_map()
    all_dates = [d for d in shadow.list_trade_dates() if d <= trade_date]
    dates = [d for d in all_dates if (shadow.CACHE / f"stk_factor_{d}.parquet").exists()]
    if len(dates) < 5:
        _status_final(
            ok=False,
            state="failed",
            run_id=run_id,
            started_at=started_at,
            finished_at=now_str(),
            trade_date=trade_date,
            error="insufficient stk_factor dates",
            production_config_fingerprint=fp,
        )
        raise SystemExit("[factory] insufficient stk_factor dates")
    window = dates[-(lookback + 1) :] if len(dates) > lookback else dates

    print(f"[factory] trade_date={trade_date} experiments={len(exps)} backtest_window={len(window)} days")
    scored_today = score_universe(trade_date)
    if len(scored_today) < 50:
        _status_final(
            ok=False,
            state="failed",
            run_id=run_id,
            started_at=started_at,
            finished_at=now_str(),
            trade_date=trade_date,
            error=f"score_universe too small: {len(scored_today)}",
            production_config_fingerprint=fp,
        )
        raise SystemExit(f"[factory] score_universe empty/small @ {trade_date}")

    mom20, mom_src = shadow.market_mom20(trade_date)
    # 真生产对照只算一次，各实验复用
    real_ctrl = backtest_real_production(window, cost=0.26)
    print(
        f"[factory] real production control status={real_ctrl.get('status')} "
        f"coverage={(real_ctrl.get('summary') or {}).get('coverage_days')}",
        flush=True,
    )

    # One run shares immutable raw scores across experiments, never across runs.
    # Selection copies each row before applying experiment-specific weights.
    score_cache = {trade_date: scored_today}
    results: list[dict[str, Any]] = []
    for exp in exps:
        print(f"[factory] run {exp.get('id')} ...")
        bt_cfg = exp.get("backtest") or {}
        lb = int(bt_cfg.get("lookback_days") or lookback)
        w = dates[-(lb + 1) :] if len(dates) > lb else dates
        # 窗口不同时按窗口重算真对照（成本缓存简化：用主 window 的 real_ctrl 若长度接近）
        rc = real_ctrl if w == window else backtest_real_production(w, cost=float(bt_cfg.get("cost_pct") or 0.26))
        bt_res = backtest_experiment(exp, w, industry_map, name_map, real_prod_control=rc,
                                   score_cache=score_cache)
        picks, meta = apply_experiment_selection(scored_today, exp, industry_map, name_map, mom20, mom_src)
        results.append(
            {
                "id": exp.get("id"),
                "name": exp.get("name"),
                "status": exp.get("status"),
                "hypothesis": exp.get("hypothesis"),
                "inspired_by": exp.get("inspired_by") or [],
                "production_frozen": bool(exp.get("production_frozen", True)),
                "weight_delta": meta.get("weight_delta"),
                "weights": meta.get("weights"),
                "gates": gates_from_exp(exp),
                "today": {
                    "trade_date": trade_date,
                    "picks": picks,
                    "selection_meta": {k: v for k, v in meta.items() if k not in ("weight_delta", "weights")},
                    "comparison": shadow.compare_with_prod(picks, shadow.load_prod_top20(trade_date)),
                },
                "backtest": bt_res,
            }
        )
        print(
            f"  today n={len(picks)} | edge_vs_baseline={bt_res.get('edge_vs_research_baseline')} "
            f"edge_vs_real={bt_res.get('edge_vs_real_production')} "
            f"real_status={(bt_res.get('real_production_control') or {}).get('status')} "
            f"exp_ret={bt_res['experiment']['summary'].get('total_return_pct')}"
        )

    champion = None
    force_id = (force_champion or "").strip()
    if force_id:
        champion = next((r for r in results if r.get("id") == force_id), None)
        if champion is None:
            print(f"[factory] WARN force_champion={force_id!r} not in active results; fallback auto")
    if champion is None:
        obs_ids = {
            e.get("id")
            for e in load_experiments()
            if str(e.get("status") or "active") == "active" and e.get("observation_champion")
        }
        if obs_ids:
            champion = next((r for r in results if r.get("id") in obs_ids), None)
            if champion:
                print(f"[factory] champion from observation_champion flag: {champion.get('id')}")
    if champion is None:
        # 不再用「假战胜生产」选冠军；优先研究基线 edge，否则 baseline
        ranked = sorted(
            results,
            key=lambda r: ((r.get("backtest") or {}).get("edge_vs_research_baseline") or -999),
            reverse=True,
        )
        champion = next((r for r in ranked if r["id"] == "exp_baseline_gates"), None) or ranked[0]

    ch_bt = champion.get("backtest") or {}
    real_status = (ch_bt.get("real_production_control") or {}).get("status") or "unavailable"
    excluded_proxy_dates = sorted(
        {
            trade_day
            for result in results
            for trade_day in ((result.get("backtest") or {}).get("data_quality") or {}).get("excluded_proxy_dates", [])
        }
    )
    payload = {
        "generated_at": now_str(),
        "factory_version": "2026-07-16.factory.2",
        "run_id": run_id,
        "trade_date": trade_date,
        "production_version": PROD_VERSION,
        "production_weights": {k: round(v, 4) for k, v in PROD_WEIGHTS.items()},
        "production_frozen": True,
        "production_config_fingerprint": fp,
        "confirm_mode": "raw_subscores",
        "note": (
            "二次加工厂：原料=生产子分；加工=实验权重/门控；"
            "研究基线=生产权重+实验门控；真生产对照=selection_history。"
            "冠军/edge 未经真 holdout+纸面晋级前仅供研究展示。不写回生产。"
        ),
        "champion_selection": (
            f"forced:{force_id}" if force_id and champion and champion.get("id") == force_id else "auto_or_observation_flag"
        ),
        "market_mom20_pct": None if mom20 is None else round(mom20, 4),
        "market_mom20_source": mom_src,
        "market_mom20_label": "liquidity_proxy_mom20",
        "experiments": results,
        "champion_experiment_id": champion.get("id"),
        "real_production_control_status": real_status,
        "operational_status": (
            "degraded_proxy_history_excluded" if excluded_proxy_dates else "healthy"
        ),
        "data_quality": {
            "policy": "proxy_dates_excluded_without_scoring",
            "proxy_signal_dates_excluded": len(excluded_proxy_dates),
            "excluded_proxy_dates": excluded_proxy_dates,
            "proxy_data_used_for_scoring": False,
        },
        "champion": {
            "id": champion.get("id"),
            "name": champion.get("name"),
            "edge_avg_daily_pct": ch_bt.get("edge_vs_research_baseline"),
            "edge_vs_research_baseline": ch_bt.get("edge_vs_research_baseline"),
            "edge_vs_real_production": ch_bt.get("edge_vs_real_production"),
            "beats_production": ch_bt.get("beats_production"),
            "control_label": ch_bt.get("control_label"),
            "today_picks": (champion.get("today") or {}).get("picks") or [],
            "selection_meta": (champion.get("today") or {}).get("selection_meta") or {},
            "comparison": (champion.get("today") or {}).get("comparison") or {},
            "backtest_summary": ((ch_bt.get("experiment") or {}).get("summary")),
            "production_control_summary": ((ch_bt.get("research_baseline") or ch_bt.get("production_control") or {}).get("summary")),
            "real_production_control": ch_bt.get("real_production_control"),
        },
    }

    write_json(FACTORY_LATEST, payload)
    write_json(FACTORY_DIR / f"run_{trade_date}.json", payload)

    public = build_factory_public_watch(payload)
    watch_ok = True
    watch_errors: list[str] = []
    for path in (PUBLISHED_WATCH, WORKING_WATCH):
        try:
            write_json(path, public)
            print(f"[factory] wrote public watch {path}")
        except Exception as exc:
            watch_ok = False
            watch_errors.append(f"{path}: {exc}")
            print(f"[factory] WARN public write {path}: {exc}")

    finished = now_str()
    if not watch_ok:
        _status_final(
            ok=False,
            state="partial_fail",
            run_id=run_id,
            started_at=started_at,
            finished_at=finished,
            trade_date=trade_date,
            n_experiments=len(results),
            champion=champion.get("id"),
            edge_avg_daily_pct=ch_bt.get("edge_vs_research_baseline"),
            real_production_control_status=real_status,
            operational_status=payload["operational_status"],
            data_quality=payload["data_quality"],
            production_config_fingerprint=fp,
            error="watch_write_failed: " + "; ".join(watch_errors),
            outputs=[str(FACTORY_LATEST)],
        )
        print("[factory] status ok=false because watch write failed")
    else:
        _status_final(
            ok=True,
            state="completed",
            run_id=run_id,
            started_at=started_at,
            finished_at=finished,
            generated_at=finished,
            trade_date=trade_date,
            n_experiments=len(results),
            champion=champion.get("id"),
            edge_avg_daily_pct=ch_bt.get("edge_vs_research_baseline"),
            real_production_control_status=real_status,
            operational_status=payload["operational_status"],
            data_quality=payload["data_quality"],
            production_config_fingerprint=fp,
            confirm_mode="raw_subscores",
            outputs=[str(FACTORY_LATEST), str(PUBLISHED_WATCH)],
        )

    print(f"[factory] champion={champion.get('id')} wrote {FACTORY_LATEST}")
    return payload


def _public_pick_row(p: dict[str, Any]) -> dict[str, Any]:
    """观察页名单行：含加工分/生产分/价量，便于直接展示 Top。"""
    return {
        "rank": p.get("shadow_rank") or p.get("rank"),
        "ts_code": p.get("ts_code"),
        "name": p.get("name"),
        "industry": p.get("industry"),
        "score": p.get("score"),
        "prod_score": p.get("prod_score"),
        "confirm_hits": p.get("confirm_hits"),
        "prod_rank": p.get("prod_rank"),
        "price": p.get("price"),
        "change_pct": p.get("change_pct"),
        "sub_scores": p.get("sub_scores") or {},
        "status": p.get("status") or "pending",
        "trade_date": p.get("trade_date") or "",
    }


def build_factory_public_watch(factory: dict[str, Any]) -> dict[str, Any]:
    """观察页合同：含实验列表、各实验今日 Top 名单、冠军当日名单。"""
    ch = factory.get("champion") or {}
    picks = ch.get("today_picks") or []
    latest_list = [_public_pick_row(p) for p in picks]

    exp_cards = []
    for e in factory.get("experiments") or []:
        bt = e.get("backtest") or {}
        se = (bt.get("experiment") or {}).get("summary") or {}
        sp = (bt.get("production_control") or {}).get("summary") or {}
        today_picks = (e.get("today") or {}).get("picks") or []
        real = bt.get("real_production_control") or {}
        exp_cards.append(
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "hypothesis": e.get("hypothesis"),
                "inspired_by": e.get("inspired_by") or [],
                "is_champion": e.get("id") == factory.get("champion_experiment_id"),
                "edge_avg_daily_pct": bt.get("edge_vs_research_baseline", bt.get("edge_avg_daily_pct")),
                "edge_vs_research_baseline": bt.get("edge_vs_research_baseline"),
                "edge_vs_real_production": bt.get("edge_vs_real_production"),
                "beats_production": bt.get("beats_production"),
                "beats_research_baseline": bt.get("beats_research_baseline"),
                "control_label": bt.get("control_label"),
                "real_production_control_status": real.get("status"),
                "experiment_total_return_pct": se.get("total_return_pct"),
                "experiment_avg_daily_pct": se.get("avg_daily_pct"),
                "experiment_win_days_pct": se.get("win_days_pct"),
                "experiment_sharpe": se.get("sharpe"),
                "production_total_return_pct": sp.get("total_return_pct"),
                "production_avg_daily_pct": sp.get("avg_daily_pct"),
                "production_win_days_pct": sp.get("win_days_pct"),
                "today_n": len(today_picks),
                "today_picks": [_public_pick_row(p) for p in today_picks],
                "weight_delta": e.get("weight_delta") or [],
                "daily_series": (bt.get("experiment") or {}).get("daily_series") or [],
            }
        )

    ch_bt_series = []
    for e in factory.get("experiments") or []:
        if e.get("id") == factory.get("champion_experiment_id"):
            ch_bt_series = ((e.get("backtest") or {}).get("experiment") or {}).get("daily_series") or []
            break

    real_status = factory.get("real_production_control_status") or (
        (ch.get("real_production_control") or {}).get("status")
    )
    prod_top = shadow.load_prod_top20(str(factory.get("trade_date") or ""))
    return {
        "generated_at": factory.get("generated_at") or now_str(),
        "title": "启动前夕 · 因子二次加工厂",
        "honesty_banner": (
            "【研究展示·非决策依据·非买入建议】原料=生产 prebreakout 子分（配置指纹可核验）；"
            "二次加工=实验权重/门控；日均超额默认相对「研究基线」(生产权重+实验门控)，"
            "不是真实生产策略。真生产对照缺失时禁止称「战胜生产」。"
            "冠军未经真 holdout+纸面晋级前不可上生产。confirm=原始子分。"
        ),
        "research_only": True,
        "promotion_candidate": False,
        "shadow_id": "prebreakout_factory_v1",
        "shadow_version": factory.get("factory_version"),
        "trade_date": factory.get("trade_date"),
        "latest_signal_date": factory.get("trade_date"),
        "production_version": factory.get("production_version"),
        "production_frozen": True,
        "production_config_fingerprint": factory.get("production_config_fingerprint"),
        "confirm_mode": factory.get("confirm_mode") or "raw_subscores",
        "market_mom20_pct": factory.get("market_mom20_pct"),
        "market_mom20_source": factory.get("market_mom20_source"),
        "market_mom20_label": factory.get("market_mom20_label") or "liquidity_proxy_mom20",
        "market_note": (ch.get("selection_meta") or {}).get("market_note"),
        "selection_meta": ch.get("selection_meta") or {},
        "comparison": ch.get("comparison") or {},
        "champion_experiment_id": factory.get("champion_experiment_id"),
        "champion": ch,
        "experiments": exp_cards,
        "latest_picks": latest_list,
        "production_top20": prod_top,
        "real_production_control_status": real_status,
        "operational_status": factory.get("operational_status") or "healthy",
        "data_quality": factory.get("data_quality") or {},
        "production_control_available": real_status == "available" and len(prod_top) > 0,
        "daily_series": [
            {
                "signal_date": r.get("signal_date"),
                "n": r.get("n"),
                "avg_o2c_pct": r.get("avg_o2c_pct"),
                "win_rate_pct": r.get("win_rate_pct"),
                "cum_nav_pct": r.get("cum_nav_pct"),
            }
            for r in ch_bt_series
        ],
        "ledger_stats": ch.get("backtest_summary") or {},
        "cumulative": {
            "settled_n": (ch.get("backtest_summary") or {}).get("active_days"),
            "avg_o2c_pct": (ch.get("backtest_summary") or {}).get("avg_daily_pct"),
            "win_rate_pct": (ch.get("backtest_summary") or {}).get("win_days_pct"),
            "signal_dates": (ch.get("backtest_summary") or {}).get("days"),
            "cum_nav_pct": (ch.get("backtest_summary") or {}).get("total_return_pct"),
        },
        "rules": {
            "mode": "secondary_processing_factory",
            "does_not_modify_production_factors": True,
            "production_weights": factory.get("production_weights"),
            "edge_definition": "edge_avg_daily_pct = vs research baseline (prod weights + exp gates)",
            "beats_production_requires": "real selection_history control status=available",
        },
        "factory": {
            "n_experiments": len(exp_cards),
            "experiments_dir": str(EXP_DIR),
            "run_id": factory.get("run_id"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Prebreakout factor secondary-processing factory")
    ap.add_argument("--trade-date", default="")
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument(
        "--champion",
        default="",
        help="强制指定 champion 实验 id（例如 exp_gold_top2_...）；观察页以该组合为主展示",
    )
    args = ap.parse_args()
    td = shadow.resolve_trade_date(args.trade_date or None)
    try:
        run_factory(td, lookback=int(args.lookback), force_champion=(args.champion or None))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        write_json(
            FACTORY_STATUS,
            {
                "ok": False,
                "state": "failed",
                "generated_at": now_str(),
                "finished_at": now_str(),
                "trade_date": td,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": tb[-2000:],
                "production_config_fingerprint": eng.production_config_fingerprint(),
            },
        )
        print(f"[factory] FATAL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
