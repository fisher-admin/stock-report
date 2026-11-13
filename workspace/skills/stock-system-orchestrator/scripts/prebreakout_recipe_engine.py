#!/usr/bin/env python3
"""启动前夕 · 配方统一计算引擎（P0）

契约：
  1. 搜索 / 工厂 / 回测 共用同一套「打分 + 确认 + 门控」逻辑
  2. confirm 永远看 **原始 subscore**（乘子只影响综合分，不影响确认键）
  3. 交易日解析统一读 CLI → env → 缓存最新
  4. JSON 原子写入；生产配置指纹可核验

用法：由 prebreakout_factory / search_* 导入，一般不单独跑。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(__file__).resolve().parents[3]
STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"

import sys

sys.path.insert(0, str(STOCK_ANALYZER))
sys.path.insert(0, str(SCRIPT_DIR))

import pipeline as pl  # noqa: E402

CONFIRM_KEYS_DEFAULT = ["macd_early_signal", "volume_warmup", "turnover_stability", "chip_support"]
COST_DEFAULT = 0.26

# 与生产 PREBREAKOUT 指纹基线（启动时固化，运行中比对漂移）
_PROD_CFG_FINGERPRINT: str | None = None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json_atomic(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def production_config_fingerprint() -> str:
    """对 PREBREAKOUT_CONFIG 做稳定 hash，用于证明未改生产。"""
    global _PROD_CFG_FINGERPRINT
    if _PROD_CFG_FINGERPRINT is not None:
        return _PROD_CFG_FINGERPRINT
    cfg = pl.PREBREAKOUT_CONFIG
    blob = json.dumps(cfg, ensure_ascii=False, sort_keys=True, default=str)
    _PROD_CFG_FINGERPRINT = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return _PROD_CFG_FINGERPRINT


def resolve_trade_date(
    explicit: str | None = None,
    *,
    list_trade_dates_fn=None,
    cache_dir: Path | None = None,
) -> str:
    """优先级：CLI explicit → OPENCLAW_TARGET_TRADE_DATE / TARGET_TRADE_DATE → 最新缓存日。"""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    for key in ("OPENCLAW_TARGET_TRADE_DATE", "TARGET_TRADE_DATE", "OPENCLAW_TRADE_DATE"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    if list_trade_dates_fn is not None:
        dates = list(list_trade_dates_fn())
        if not dates:
            raise SystemExit("[recipe_engine] no trade dates available")
        if cache_dir is not None:
            for d in reversed(dates):
                if (Path(cache_dir) / f"stk_factor_{d}.parquet").exists():
                    return d
        return dates[-1]
    raise SystemExit("[recipe_engine] resolve_trade_date: no explicit/env date and no list_fn")


def normalize_weights(weights: dict[str, float], factors: list[str], fallback: dict[str, float]) -> dict[str, float]:
    out = {f: max(0.0, float(weights.get(f, 0.0))) for f in factors}
    s = sum(out.values())
    if s <= 1e-12:
        return dict(fallback)
    return {k: v / s for k, v in out.items()}


def confirm_hits_raw(subs_or_row: dict[str, Any] | pd.Series, keys: list[str], thr: float) -> int:
    """确认命中：永远看原始分，不受 subscore_multipliers 影响。"""
    n = 0
    for k in keys:
        try:
            if isinstance(subs_or_row, pd.Series):
                v = float(subs_or_row.get(k) if k in subs_or_row.index else 0 or 0)
            else:
                v = float(subs_or_row.get(k) or 0)
        except (TypeError, ValueError):
            continue
        if v >= thr:
            n += 1
    return n


def confirm_hits_arr_raw(df: pd.DataFrame, keys: list[str], thr: float) -> np.ndarray:
    hits = np.zeros(len(df), dtype=int)
    for k in keys:
        if k not in df.columns:
            continue
        hits += (df[k].to_numpy(dtype=float) >= thr).astype(int)
    return hits


def rescore_with_mult(
    df: pd.DataFrame,
    weights: dict[str, float],
    mult: dict[str, float] | None,
    factors: list[str],
) -> pd.Series:
    """综合分 = sum(raw_sub * mult * weight)；confirm 不在此函数内。"""
    w = weights
    m = mult or {}
    acc = np.zeros(len(df), dtype=float)
    for f in factors:
        wf = float(w.get(f, 0.0) or 0.0)
        if wf <= 0 or f not in df.columns:
            continue
        mf = float(m.get(f, 1.0) or 1.0)
        acc += df[f].to_numpy(dtype=float) * mf * wf
    return pd.Series(acc, index=df.index)


def pick_top_codes(
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
    """选股：scores 可为乘子后综合分；confirm 始终用 df 原始列。"""
    if top_n <= 0 or df is None or len(df) == 0:
        return []
    s = scores.to_numpy(dtype=float)
    thr = float(np.nanpercentile(s[np.isfinite(s)], score_pct)) if np.isfinite(s).any() else min_score
    if not np.isfinite(thr):
        thr = min_score
    thr = max(thr, min_score)
    hits = confirm_hits_arr_raw(df, confirm_keys, confirm_thr)
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


def evaluate_panel_recipe(
    panel: dict[str, pd.DataFrame],
    pairs: list[tuple[str, str]],
    weights: dict[str, float],
    mult: dict[str, float] | None,
    gates: dict[str, Any],
    o2c_cache: dict[str, dict[str, float]],
    ind_map: dict[str, str],
    cap_cache: dict[tuple, int],
    *,
    factors: list[str],
    market_cap_for_fn,
    o2c_for_date_fn,
    cost: float = COST_DEFAULT,
) -> dict[str, Any]:
    """面板回测入口：confirm on raw。与工厂出厂规则对齐。"""
    mult = mult or {f: 1.0 for f in factors}
    daily: list[float] = []
    win_days = 0
    active = 0
    empty = 0
    n_sum = 0
    nav = 1.0
    series = []
    mkt_on = bool(gates.get("market_gate", True))
    max_names = int(gates.get("max_names", 8))
    confirm_keys = list(gates.get("confirm_keys") or CONFIRM_KEYS_DEFAULT)
    confirm_thr = float(gates.get("confirm_subscore_min", 60))
    cost_pct = float(gates.get("cost_pct", cost))

    for td, nd in pairs:
        df0 = panel.get(td)
        if df0 is None or len(df0) < 50:
            continue
        scores = rescore_with_mult(df0, weights, mult, factors)
        key = (td, max_names, mkt_on)
        if key not in cap_cache:
            cap_cache[key] = market_cap_for_fn(td, mkt_on, max_names)
        top_n = cap_cache[key]
        picks = pick_top_codes(
            df0,
            scores,
            ind_map,
            top_n=top_n,
            max_per_industry=int(gates.get("max_per_industry", 3)),
            score_pct=float(gates.get("score_percentile", 90)),
            min_score=float(gates.get("min_score_floor", 70)),
            min_confirm=int(gates.get("min_confirm_hits", 2)),
            confirm_keys=confirm_keys,
            confirm_thr=confirm_thr,
        )
        if nd not in o2c_cache:
            o2c_cache[nd] = o2c_for_date_fn(nd)
        o2c = o2c_cache[nd]
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
                avg = float(np.mean(rets)) - cost_pct
                n = len(rets)
                active += 1
                n_sum += n
                if avg > 0:
                    win_days += 1
        daily.append(avg)
        nav *= 1.0 + avg / 100.0
        series.append(
            {
                "signal_date": td,
                "trade_date": nd,
                "n": n,
                "avg_o2c_pct": round(avg, 4),
                "cum_nav_pct": round((nav - 1) * 100, 4),
            }
        )

    if not daily:
        return {
            "days": 0,
            "active_days": 0,
            "avg_daily_pct": None,
            "win_days_pct": None,
            "total_return_pct": None,
            "sharpe": None,
            "empty_book_days": 0,
            "avg_n": None,
            "series": [],
            "confirm_mode": "raw_subscores",
        }
    arr = np.array(daily, dtype=float)
    sharpe = float(arr.mean() / arr.std() * np.sqrt(252)) if arr.std() > 1e-12 else 0.0
    return {
        "days": len(daily),
        "active_days": active,
        "avg_daily_pct": round(float(arr.mean()), 4),
        "win_days_pct": round(win_days / max(active, 1) * 100, 2) if active else None,
        "total_return_pct": round((nav - 1.0) * 100, 4),
        "sharpe": round(sharpe, 3),
        "empty_book_days": empty,
        "avg_n": round(n_sum / max(active, 1), 2) if active else 0.0,
        "series": series,
        "confirm_mode": "raw_subscores",
    }


def train_objective_score(train: dict[str, Any], test: dict[str, Any] | None = None) -> float:
    """优化目标：只用 train（+ 可选 test 弱权重），禁止 holdout。"""

    def pack(m: dict | None) -> tuple[float, float, float]:
        if not m or not m.get("days"):
            return -9.0, 0.0, 1.0
        avg = float(m.get("avg_daily_pct") or 0)
        sh = float(m.get("sharpe") or 0)
        empty = float(m.get("empty_book_days") or 0) / max(float(m.get("days") or 1), 1)
        return avg, sh, empty

    ta, ts, te = pack(train)
    score = 1.6 * ta + 0.25 * ts - 1.5 * te
    if test is not None:
        ea, es, ee = pack(test)
        score += 0.35 * ea + 0.1 * es - 0.4 * ee
    return float(score)


def holdout_select_score(hold: dict[str, Any], full: dict[str, Any] | None = None) -> float:
    """最终选 Top：仅 holdout（full 仅极弱参考且可关）。"""
    if not hold or not hold.get("days"):
        return -99.0
    ha = float(hold.get("avg_daily_pct") or 0)
    hs = float(hold.get("sharpe") or 0)
    he = float(hold.get("empty_book_days") or 0) / max(float(hold.get("days") or 1), 1)
    score = 2.5 * ha + 0.4 * hs - 2.0 * he
    if full and full.get("days"):
        # 仅展示辅助，权重极低
        score += 0.05 * float(full.get("avg_daily_pct") or 0)
    return float(score)


def split_pairs_true_holdout(
    pairs: list[tuple[str, str]],
    holdout_days: int = 20,
) -> tuple[list, list, list, dict[str, Any]]:
    """train / test / hold；hold 完全不参与优化。"""
    n = len(pairs)
    meta: dict[str, Any] = {"n_pairs": n, "holdout_requested": holdout_days}
    if n < 12:
        a = max(1, n // 2)
        train, hold = pairs[:a], pairs[a:]
        meta.update({"mode": "tiny", "degraded": True, "holdout_days": len(hold)})
        return train, [], hold, meta

    h = min(holdout_days, max(5, n // 3))
    if n - h < 8:
        h = max(5, n // 5)
        meta["degraded"] = True
    else:
        meta["degraded"] = n < (holdout_days + 25)

    hold = pairs[-h:]
    rest = pairs[:-h]
    if len(rest) < 6:
        train, test = rest, []
    else:
        cut = int(len(rest) * 0.72)
        cut = max(4, min(cut, len(rest) - 1))
        train, test = rest[:cut], rest[cut:]
    meta.update(
        {
            "mode": "true_holdout",
            "holdout_days": len(hold),
            "train_days": len(train),
            "test_days": len(test),
            "holdout_sealed": True,
        }
    )
    return train, test, hold, meta


def try_flock(lock_path: Path):
    """返回 (fd_or_None, acquired: bool)。调用方负责 close。"""
    import fcntl

    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(f"pid={os.getpid()} at={now_str()}\n")
        fd.flush()
        return fd, True
    except BlockingIOError:
        fd.close()
        return None, False
