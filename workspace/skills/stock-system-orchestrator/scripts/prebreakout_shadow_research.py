#!/usr/bin/env python3
"""启动前夕 · 因子优化影子研究（prebreakout_shadow_v5）

约束（2026-07-16）：
  - **不修改** pipeline.PREBREAKOUT_CONFIG / score_stock_prebreakout 任何生产因子与权重。
  - **不新建** 前端观察页；产物落 health/ + strategy_research/，供日志/Discord/人工读 JSON 观察。
  - 生产 prebreakout_v41 发布链路完全不动；本脚本只做纸面对照与前瞻跟踪。

研究层策略（在生产全市场打分结果之上后处理）：
  1) 市场门：沪深300 20 日涨跌分档，弱市压缩/清零可发布只数
  2) 稀疏化：综合分须 ≥ 当日通过硬过滤池的 90 分位，且绝对分 ≥ 动态下限
  3) 行业上限：单行业最多 max_per_industry 只
  4) 名额上限：max_names（默认 8），允许空仓
  5) 确认计数：至少 n_confirm 个确认子分过门槛（MACD 早期 / 温和放量 / 量稳定 等）

用法：
  ~/.openclaw/venv/bin/python prebreakout_shadow_research.py
  ~/.openclaw/venv/bin/python prebreakout_shadow_research.py --trade-date 20260715
  ~/.openclaw/venv/bin/python prebreakout_shadow_research.py --settle-only
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(__file__).resolve().parents[3]
STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"
sys.path.insert(0, str(STOCK_ANALYZER))
sys.path.insert(0, str(SCRIPT_DIR))

import backtest as bt  # noqa: E402
import pipeline as pl  # noqa: E402

HEALTH_DIR = WORKSPACE / "stock_data" / "03-working" / "health"
SHADOW_DIR = WORKSPACE / "stock_data" / "03-working" / "strategy_research" / "prebreakout_shadow"
CACHE = bt.CACHE_DIR
INDUSTRY_MAP_PATH = pl.WORKING_DIR / "cache" / "industry_map.json"
SELECTION_HISTORY = pl.WORKING_DIR / "selection_history"
STOCK_LIST = pl.ROOT_DIR / "stock_list.csv"

LATEST_PATH = HEALTH_DIR / "prebreakout_shadow_latest.json"
STATUS_PATH = HEALTH_DIR / "prebreakout_shadow_status.json"
LEDGER_PATH = HEALTH_DIR / "prebreakout_shadow_ledger.csv"
SNAPSHOT_DIR = SHADOW_DIR / "daily"
# 观察页公开数据（GitHub Pages data/latest，不改生产策略合同）
PUBLISHED_WATCH = WORKSPACE / "stock-report" / "data" / "latest" / "prebreakout_shadow_research_watch.json"
WORKING_WATCH = (
    WORKSPACE / "stock_data" / "03-working" / "stock-report-repo" / "data" / "latest" / "prebreakout_shadow_research_watch.json"
)

SHADOW_ID = "prebreakout_shadow_v5"
SHADOW_VERSION = "2026-07-16.1"

# 研究超参数（只影响影子层，不写回生产）
MAX_NAMES = 8
MAX_PER_INDUSTRY = 3
SCORE_PERCENTILE = 90.0
MIN_SCORE_FLOOR = 70.0  # 与分位取 max；弱市可被市场门压到 0 只
MIN_CONFIRM_HITS = 2
CONFIRM_SUBSCORE_MIN = 60.0
CONFIRM_KEYS = ("macd_early_signal", "volume_warmup", "turnover_stability", "chip_support")

# 市场门：HS300 近 20 日涨跌 → 影子最大名额
# 弱市宁可空仓，也不硬凑 20 只。
def market_cap_from_mom20(mom20_pct: float | None, source: str = "") -> tuple[int, str]:
    """按 20 日市场强度分档压缩影子名额。弱市允许空仓（研究观察的关键信号）。"""
    tag = f"src={source}" if source else "src=?"
    if mom20_pct is None:
        return MAX_NAMES, f"mom_unavailable→default_cap ({tag})"
    if mom20_pct < -6.0:
        return 0, f"risk_off mom20={mom20_pct:.2f}%→cap=0 ({tag})"
    if mom20_pct < -3.0:
        return 3, f"caution mom20={mom20_pct:.2f}%→cap=3 ({tag})"
    if mom20_pct < 0.5:
        return 5, f"neutral mom20={mom20_pct:.2f}%→cap=5 ({tag})"
    return MAX_NAMES, f"risk_on mom20={mom20_pct:.2f}%→cap={MAX_NAMES} ({tag})"


LEDGER_FIELDS = [
    "signal_date",
    "trade_date",
    "ts_code",
    "name",
    "industry",
    "score",
    "prod_rank",
    "shadow_rank",
    "confirm_hits",
    "status",
    "gap_pct",
    "o2c_pct",
    "miss_reason",
]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def list_trade_dates() -> list[str]:
    dates = sorted({p.stem.replace("daily_", "") for p in CACHE.glob("daily_*.parquet") if p.stem.replace("daily_", "").isdigit()})
    return dates


def resolve_trade_date(explicit: str | None) -> str:
    """CLI → OPENCLAW_TARGET_TRADE_DATE / TARGET_TRADE_DATE → 最新有 stk_factor 的交易日。"""
    import prebreakout_recipe_engine as eng

    try:
        return eng.resolve_trade_date(
            explicit,
            list_trade_dates_fn=list_trade_dates,
            cache_dir=CACHE,
        )
    except SystemExit as exc:
        # 保持 shadow 原有错误语义
        msg = str(exc)
        if "no trade dates" in msg or "no list_fn" in msg:
            raise SystemExit("[shadow] no daily_*.parquet in backtest_cache") from exc
        raise


def next_trade_date(signal_date: str) -> str | None:
    dates = list_trade_dates()
    if signal_date not in dates:
        return None
    i = dates.index(signal_date)
    if i + 1 < len(dates):
        return dates[i + 1]
    return None


def load_parquet(data_type: str, trade_date: str) -> pd.DataFrame | None:
    path = CACHE / f"{data_type}_{trade_date}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def load_industry_map() -> dict[str, str]:
    try:
        if INDUSTRY_MAP_PATH.exists():
            raw = json.loads(INDUSTRY_MAP_PATH.read_text(encoding="utf-8"))
            return {str(k): str(v or "") for k, v in (raw or {}).items()}
    except Exception:
        pass
    return {}


def load_name_map() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        if STOCK_LIST.exists():
            df = pd.read_csv(STOCK_LIST)
            code_col = "ts_code" if "ts_code" in df.columns else df.columns[0]
            name_col = "name" if "name" in df.columns else None
            if name_col:
                for _, r in df.iterrows():
                    out[str(r[code_col])] = str(r[name_col])
    except Exception:
        pass
    return out


def market_mom20(trade_date: str) -> tuple[float | None, str]:
    """市场 20 日强度代理（缓存无指数日线时用大盘股等权）。

    返回 (mom20_pct, source)。全市场中位数会系统性偏弱（小票拖累），不可直接当 HS300。
    优先：000300/399300 若在 daily 缓存；否则：成交额 Top30% 股票 20 日收益的等权均值。
    """
    dates = [d for d in list_trade_dates() if d <= trade_date][-21:]
    if len(dates) < 21:
        return None, "insufficient_history"

    # 1) 真·指数（若缓存含）
    closes: list[float] = []
    for d in dates:
        df = load_parquet("daily", d)
        if df is None:
            closes = []
            break
        hit = df[df["ts_code"].astype(str).isin(["000300.SH", "399300.SZ"])]
        if len(hit) == 0:
            closes = []
            break
        closes.append(float(hit.iloc[0]["close"]))
    if len(closes) >= 21 and closes[0] > 0:
        return (closes[-1] / closes[0] - 1.0) * 100.0, "hs300_cache"

    # 2) 大盘股等权：以信号日成交额 Top30% 为宇宙，两端 close 算 20 日收益再等权
    d0, d1 = dates[0], dates[-1]
    a = load_parquet("daily", d0)
    b = load_parquet("daily", d1)
    if a is None or b is None:
        return None, "daily_missing"
    b = b.copy()
    b["amount"] = pd.to_numeric(b.get("amount"), errors="coerce")
    b = b.dropna(subset=["amount"])
    if len(b) < 500:
        return None, "amount_sparse"
    thr_amt = float(b["amount"].quantile(0.70))
    large = set(b.loc[b["amount"] >= thr_amt, "ts_code"].astype(str))
    a_close = a.set_index("ts_code")["close"]
    b_close = b.set_index("ts_code")["close"]
    common = [c for c in large if c in a_close.index and c in b_close.index]
    if len(common) < 200:
        return None, "large_cap_sparse"
    rets = (b_close.loc[common] / a_close.loc[common] - 1.0) * 100.0
    rets = rets.replace([np.inf, -np.inf], np.nan).dropna()
    if len(rets) < 200:
        return None, "rets_sparse"
    return float(rets.mean()), "large_cap_ew_top30pct_amount"


def confirm_hits(sub_scores: dict[str, Any]) -> int:
    hits = 0
    for k in CONFIRM_KEYS:
        try:
            if float(sub_scores.get(k) or 0) >= CONFIRM_SUBSCORE_MIN:
                hits += 1
        except (TypeError, ValueError):
            continue
    return hits


def load_prod_top20(trade_date: str) -> list[dict[str, Any]]:
    path = SELECTION_HISTORY / f"{trade_date}.json"
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    stocks = doc.get("stocks") or doc.get("top20") or []
    out = []
    for i, s in enumerate(stocks):
        if not isinstance(s, dict):
            continue
        tc = str(s.get("ts_code") or s.get("code") or "")
        if not tc:
            continue
        if "." not in tc and len(tc) == 6:
            tc = tc + (".SH" if tc.startswith("6") else ".SZ")
        out.append(
            {
                "ts_code": tc,
                "name": s.get("name") or "",
                "score": s.get("score"),
                "rank": s.get("rank") or i + 1,
            }
        )
    return out


def apply_shadow_selection(
    scored: list[dict[str, Any]],
    industry_map: dict[str, str],
    name_map: dict[str, str],
    market_cap: int,
    market_note: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在生产打分排序结果上叠加研究过滤，不改分数公式。"""
    meta: dict[str, Any] = {
        "universe_after_hard_filter": len(scored),
        "market_cap": market_cap,
        "market_note": market_note,
        "score_percentile": SCORE_PERCENTILE,
        "min_score_floor": MIN_SCORE_FLOOR,
        "min_confirm_hits": MIN_CONFIRM_HITS,
        "max_per_industry": MAX_PER_INDUSTRY,
        "max_names": MAX_NAMES,
    }
    if market_cap <= 0 or not scored:
        meta["threshold_score"] = None
        meta["candidates_after_score"] = 0
        meta["selected"] = 0
        return [], meta

    scores = np.array([float(x.get("score") or 0) for x in scored], dtype=float)
    scores = scores[np.isfinite(scores)]
    if len(scores) == 0:
        meta["threshold_score"] = MIN_SCORE_FLOOR
        meta["candidates_after_score_confirm"] = 0
        meta["selected"] = 0
        return [], meta
    thr = float(np.nanpercentile(scores, SCORE_PERCENTILE))
    if not np.isfinite(thr):
        thr = MIN_SCORE_FLOOR
    thr = max(thr, MIN_SCORE_FLOOR)
    meta["threshold_score"] = round(thr, 2)

    # production rank map
    prod_rank = {str(x["ts_code"]): i + 1 for i, x in enumerate(scored)}

    candidates: list[dict[str, Any]] = []
    for row in scored:
        sc = float(row.get("score") or 0)
        if sc < thr:
            continue
        subs = row.get("sub_scores") or {}
        if not isinstance(subs, dict):
            subs = {}
        hits = confirm_hits(subs)
        if hits < MIN_CONFIRM_HITS:
            continue
        tc = str(row["ts_code"])
        ind = industry_map.get(tc) or industry_map.get(tc.split(".")[0]) or "未知"
        if ind in ("nan", "None", ""):
            ind = "未知"
        candidates.append(
            {
                "ts_code": tc,
                "name": name_map.get(tc) or row.get("name") or tc,
                "score": sc,
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
                "industry": ind,
                "confirm_hits": hits,
                "prod_rank": prod_rank.get(tc),
                "sub_scores": {k: round(float(v), 1) for k, v in subs.items() if isinstance(v, (int, float))},
                "chip_conc": row.get("chip_conc"),
                "volume_ratio": row.get("volume_ratio"),
                "rsi_6": row.get("rsi_6"),
                "macd_dif": row.get("macd_dif"),
            }
        )
    meta["candidates_after_score_confirm"] = len(candidates)

    # 行业上限 + 名额上限（已按 score 降序）
    candidates.sort(key=lambda x: (-float(x["score"]), -int(x["confirm_hits"])))
    picked: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for c in candidates:
        if len(picked) >= market_cap:
            break
        ind = c["industry"]
        if industry_counts.get(ind, 0) >= MAX_PER_INDUSTRY:
            continue
        industry_counts[ind] = industry_counts.get(ind, 0) + 1
        c = dict(c)
        c["shadow_rank"] = len(picked) + 1
        picked.append(c)

    meta["selected"] = len(picked)
    meta["industry_counts"] = industry_counts
    meta["empty_book"] = len(picked) == 0
    return picked, meta


def load_ledger() -> pd.DataFrame:
    if not LEDGER_PATH.exists():
        return pd.DataFrame(columns=LEDGER_FIELDS)
    try:
        return pd.read_csv(LEDGER_PATH, dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=LEDGER_FIELDS)


def save_ledger(df: pd.DataFrame) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(LEDGER_PATH, index=False, columns=LEDGER_FIELDS)


def settle_pending(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger.empty:
        return ledger
    rows = ledger.to_dict(orient="records")
    changed = 0
    for r in rows:
        if str(r.get("status") or "") != "pending":
            continue
        td = str(r.get("trade_date") or "")
        tc = str(r.get("ts_code") or "")
        if not td or not tc:
            continue
        daily = load_parquet("daily", td)
        if daily is None:
            continue
        hit = daily[daily["ts_code"].astype(str) == tc]
        if len(hit) == 0:
            r["status"] = "miss"
            r["miss_reason"] = "no_bar_on_trade_date"
            changed += 1
            continue
        o = float(hit.iloc[0].get("open") or 0)
        c = float(hit.iloc[0].get("close") or 0)
        pre = float(hit.iloc[0].get("pre_close") or 0)
        if o <= 0 or c <= 0:
            r["status"] = "miss"
            r["miss_reason"] = "bad_prices"
            changed += 1
            continue
        # 一字近涨停开盘：不可买
        if pre > 0 and o >= pre * 1.095 * 0.995 and not tc.startswith(("30", "68")):
            r["status"] = "miss"
            r["miss_reason"] = "limit_up_open"
            r["gap_pct"] = round((o / pre - 1.0) * 100, 4) if pre else ""
            changed += 1
            continue
        if pre > 0 and o >= pre * 1.195 * 0.995 and tc.startswith("30"):
            r["status"] = "miss"
            r["miss_reason"] = "limit_up_open_chinext"
            r["gap_pct"] = round((o / pre - 1.0) * 100, 4) if pre else ""
            changed += 1
            continue
        gap = (o / pre - 1.0) * 100 if pre > 0 else 0.0
        o2c = (c / o - 1.0) * 100
        r["status"] = "settled"
        r["gap_pct"] = round(gap, 4)
        r["o2c_pct"] = round(o2c, 4)
        r["miss_reason"] = ""
        changed += 1
    if changed:
        print(f"[shadow] settled/missed {changed} pending rows")
    return pd.DataFrame(rows, columns=LEDGER_FIELDS)


def append_signals(
    ledger: pd.DataFrame,
    signal_date: str,
    trade_date: str | None,
    picks: list[dict[str, Any]],
) -> pd.DataFrame:
    if not picks:
        return ledger
    if trade_date is None:
        print("[shadow] no T+1 trade_date yet; write snapshot only, skip ledger pending")
        return ledger
    # 同日重跑去重
    if not ledger.empty:
        ledger = ledger[ledger["signal_date"].astype(str) != str(signal_date)].copy()
    new_rows = []
    for p in picks:
        new_rows.append(
            {
                "signal_date": signal_date,
                "trade_date": trade_date,
                "ts_code": p["ts_code"],
                "name": p.get("name") or "",
                "industry": p.get("industry") or "",
                "score": p.get("score"),
                "prod_rank": p.get("prod_rank") or "",
                "shadow_rank": p.get("shadow_rank") or "",
                "confirm_hits": p.get("confirm_hits") or "",
                "status": "pending",
                "gap_pct": "",
                "o2c_pct": "",
                "miss_reason": "",
            }
        )
    add = pd.DataFrame(new_rows, columns=LEDGER_FIELDS)
    out = pd.concat([ledger, add], ignore_index=True) if not ledger.empty else add
    print(f"[shadow] appended {len(new_rows)} pending signals for {signal_date} → T+1={trade_date}")
    return out


def ledger_stats(ledger: pd.DataFrame) -> dict[str, Any]:
    if ledger.empty:
        return {"settled_n": 0, "pending_n": 0, "avg_o2c_pct": None, "win_rate_pct": None}
    settled = ledger[ledger["status"].astype(str) == "settled"]
    pending = ledger[ledger["status"].astype(str) == "pending"]
    o2cs = pd.to_numeric(settled["o2c_pct"], errors="coerce").dropna()
    return {
        "settled_n": int(len(settled)),
        "pending_n": int(len(pending)),
        "miss_n": int((ledger["status"].astype(str) == "miss").sum()),
        "avg_o2c_pct": round(float(o2cs.mean()), 4) if len(o2cs) else None,
        "win_rate_pct": round(float((o2cs > 0).mean() * 100), 2) if len(o2cs) else None,
        "signal_dates": int(ledger["signal_date"].nunique()) if "signal_date" in ledger else 0,
    }


def build_daily_series(ledger: pd.DataFrame) -> list[dict[str, Any]]:
    """按信号日聚合已结算 T+1 开→收，并附累计净值（复利）。"""
    if ledger.empty:
        return []
    settled = ledger[ledger["status"].astype(str) == "settled"].copy()
    if settled.empty:
        return []
    settled["o2c_num"] = pd.to_numeric(settled["o2c_pct"], errors="coerce")
    settled = settled.dropna(subset=["o2c_num"])
    rows: list[dict[str, Any]] = []
    nav = 1.0
    for sig, g in settled.groupby("signal_date", sort=True):
        avg = float(g["o2c_num"].mean())
        win = float((g["o2c_num"] > 0).mean() * 100)
        nav *= 1.0 + avg / 100.0
        rows.append(
            {
                "signal_date": str(sig),
                "n": int(len(g)),
                "avg_o2c_pct": round(avg, 4),
                "win_rate_pct": round(win, 2),
                "cum_nav_pct": round((nav - 1.0) * 100, 4),
            }
        )
    return rows


def build_public_watch(payload: dict[str, Any], ledger: pd.DataFrame) -> dict[str, Any]:
    """观察页专用合同：字段稳定、可缺省，绝不编造收益。"""
    stats = payload.get("ledger_stats") or ledger_stats(ledger)
    daily = build_daily_series(ledger)
    picks = payload.get("picks") or []
    # 今日名单带 status：若当日信号已有 ledger 行则并入
    status_map: dict[str, dict[str, Any]] = {}
    if not ledger.empty and payload.get("trade_date"):
        day = ledger[ledger["signal_date"].astype(str) == str(payload.get("trade_date"))]
        for _, r in day.iterrows():
            status_map[str(r.get("ts_code"))] = {
                "status": r.get("status") or "pending",
                "trade_date": r.get("trade_date") or "",
                "gap_pct": r.get("gap_pct") if r.get("gap_pct") != "" else None,
                "o2c_pct": r.get("o2c_pct") if r.get("o2c_pct") != "" else None,
                "miss_reason": r.get("miss_reason") or "",
            }
    latest_list = []
    for p in picks:
        tc = str(p.get("ts_code") or "")
        st = status_map.get(tc) or {"status": "pending", "trade_date": payload.get("next_trade_date") or ""}
        latest_list.append(
            {
                "rank": p.get("shadow_rank"),
                "ts_code": tc,
                "name": p.get("name"),
                "industry": p.get("industry"),
                "score": p.get("score"),
                "confirm_hits": p.get("confirm_hits"),
                "prod_rank": p.get("prod_rank"),
                "status": st.get("status") or "pending",
                "trade_date": st.get("trade_date") or "",
                "gap_pct": st.get("gap_pct"),
                "o2c_pct": st.get("o2c_pct"),
                "miss_reason": st.get("miss_reason") or None,
            }
        )
    return {
        "generated_at": payload.get("generated_at") or now_str(),
        "title": "启动前夕 · 影子研究观察",
        "honesty_banner": (
            "研究观察名单·非买入建议。影子层复用生产 prebreakout_v41 打分，"
            "仅叠加市场门/稀疏化/行业上限后处理；不修改现网因子权重，不进入 main 买入通道。"
        ),
        "shadow_id": SHADOW_ID,
        "shadow_version": SHADOW_VERSION,
        "trade_date": payload.get("trade_date"),
        "latest_signal_date": payload.get("trade_date"),
        "next_trade_date": payload.get("next_trade_date"),
        "market_mom20_pct": payload.get("market_mom20_pct"),
        "market_mom20_source": payload.get("market_mom20_source"),
        "market_note": (payload.get("selection_meta") or {}).get("market_note"),
        "selection_meta": payload.get("selection_meta") or {},
        "comparison": payload.get("comparison") or {},
        "ledger_stats": stats,
        "latest_picks": latest_list,
        "production_top20": payload.get("production_top20") or [],
        "daily_series": daily,
        "cumulative": {
            "settled_n": stats.get("settled_n"),
            "avg_o2c_pct": stats.get("avg_o2c_pct"),
            "win_rate_pct": stats.get("win_rate_pct"),
            "signal_dates": stats.get("signal_dates"),
            "cum_nav_pct": daily[-1]["cum_nav_pct"] if daily else None,
        },
        "rules": {
            "max_names": MAX_NAMES,
            "max_per_industry": MAX_PER_INDUSTRY,
            "score_percentile": SCORE_PERCENTILE,
            "min_score_floor": MIN_SCORE_FLOOR,
            "min_confirm_hits": MIN_CONFIRM_HITS,
            "confirm_keys": list(CONFIRM_KEYS),
            "does_not_modify_production_factors": True,
        },
    }


def write_public_watch(watch: dict[str, Any]) -> None:
    for path in (PUBLISHED_WATCH, WORKING_WATCH):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[shadow] wrote public watch: {path}")
        except Exception as exc:
            print(f"[shadow] WARN public watch write failed {path}: {exc}")


def compare_with_prod(picks: list[dict[str, Any]], prod: list[dict[str, Any]]) -> dict[str, Any]:
    shadow_set = {p["ts_code"] for p in picks}
    prod_set = {p["ts_code"] for p in prod}
    inter = shadow_set & prod_set
    return {
        "prod_n": len(prod),
        "shadow_n": len(picks),
        "overlap_n": len(inter),
        "overlap_codes": sorted(inter),
        "shadow_only": sorted(shadow_set - prod_set),
        "prod_only_sample": sorted(prod_set - shadow_set)[:20],
    }


def run_for_date(trade_date: str, settle_only: bool = False) -> dict[str, Any]:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    ledger = load_ledger()
    ledger = settle_pending(ledger)

    payload: dict[str, Any] = {
        "generated_at": now_str(),
        "shadow_id": SHADOW_ID,
        "shadow_version": SHADOW_VERSION,
        "trade_date": trade_date,
        "research_only": True,
        "does_not_modify_production_factors": True,
        "production_strategy_id": "prebreakout_v41",
        "note": "影子研究：复用生产 score_stock_prebreakout 分数，仅后处理筛选；不发 main、不改 v4.3 权重。",
    }

    if settle_only:
        save_ledger(ledger)
        stats = ledger_stats(ledger)
        # 不覆盖 full 产物：仅更新 status + 若 latest 存在则补 ledger_stats
        if LATEST_PATH.exists():
            try:
                prev = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and prev.get("mode") == "full":
                    prev["ledger_stats"] = stats
                    prev["settle_refreshed_at"] = now_str()
                    write_json(LATEST_PATH, prev)
            except Exception:
                pass
        write_json(
            STATUS_PATH,
            {"ok": True, "mode": "settle_only", "generated_at": now_str(), "ledger_stats": stats},
        )
        # 结算后刷新观察页累计曲线
        if LATEST_PATH.exists():
            try:
                prev = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
                if isinstance(prev, dict):
                    prev["ledger_stats"] = stats
                    write_public_watch(build_public_watch(prev, ledger))
            except Exception as exc:
                print(f"[shadow] WARN refresh public watch after settle: {exc}")
        print("[shadow] settle-only done:", stats)
        return {"mode": "settle_only", "ledger_stats": stats, "generated_at": now_str()}

    stk = load_parquet("stk_factor", trade_date)
    cyq = load_parquet("cyq_perf", trade_date)
    if stk is None or len(stk) == 0:
        raise SystemExit(f"[shadow] missing stk_factor_{trade_date}.parquet")
    if cyq is None:
        cyq = pd.DataFrame()

    # 关键字段兼容 score_from_bulk
    if "pct_change" not in stk.columns and "pct_chg" in stk.columns:
        stk = stk.copy()
        stk["pct_change"] = stk["pct_chg"]

    print(f"[shadow] scoring full market with PRODUCTION prebreakout factors @ {trade_date} ...")
    scored = bt.score_from_bulk(stk, cyq, mode="prebreakout", trade_date=trade_date)
    print(f"[shadow] production hard-filter survivors: {len(scored)}")

    mom20, mom_src = market_mom20(trade_date)
    market_cap, market_note = market_cap_from_mom20(mom20, mom_src)
    industry_map = load_industry_map()
    name_map = load_name_map()
    # enrich names from scored price rows if stock_list thin
    picks, sel_meta = apply_shadow_selection(scored, industry_map, name_map, market_cap, market_note)

    prod = load_prod_top20(trade_date)
    # map prod ranks for display when overlap
    prod_rank_map = {p["ts_code"]: p.get("rank") for p in prod}
    for p in picks:
        if not p.get("prod_rank"):
            p["prod_rank"] = prod_rank_map.get(p["ts_code"])

    t1 = next_trade_date(trade_date)
    ledger = append_signals(ledger, trade_date, t1, picks)
    save_ledger(ledger)

    comparison = compare_with_prod(picks, prod)
    stats = ledger_stats(ledger)

    payload.update(
        {
            "mode": "full",
            "market_mom20_pct": None if mom20 is None else round(mom20, 4),
            "market_mom20_source": mom_src,
            "selection_meta": sel_meta,
            "feature_coverage": getattr(bt, "LAST_FEATURE_COVERAGE", None),
            "picks": picks,
            "production_top20": prod[:20],
            "comparison": comparison,
            "next_trade_date": t1,
            "ledger_stats": stats,
            "artifacts": {
                "latest": str(LATEST_PATH),
                "ledger": str(LEDGER_PATH),
                "snapshot": str(SNAPSHOT_DIR / f"{trade_date}.json"),
            },
        }
    )

    write_json(LATEST_PATH, payload)
    write_json(SNAPSHOT_DIR / f"{trade_date}.json", payload)
    write_json(
        STATUS_PATH,
        {
            "ok": True,
            "generated_at": now_str(),
            "trade_date": trade_date,
            "selected": len(picks),
            "empty_book": sel_meta.get("empty_book"),
            "market_note": market_note,
            "overlap_n": comparison.get("overlap_n"),
            "ledger_stats": stats,
        },
    )
    # 观察页数据源（GitHub Pages）
    write_public_watch(build_public_watch(payload, ledger))

    print(
        f"[shadow] {trade_date}: selected={len(picks)} "
        f"overlap_prod={comparison.get('overlap_n')}/{len(prod)} "
        f"market={market_note}"
    )
    for p in picks[:12]:
        print(
            f"  #{p['shadow_rank']} {p['ts_code']} {p.get('name')} "
            f"score={p['score']} ind={p.get('industry')} "
            f"confirm={p.get('confirm_hits')} prod_rank={p.get('prod_rank')}"
        )
    if not picks:
        print("[shadow] empty book (intentional under gates) — research observation still valid")
    print("[shadow] ledger_stats:", stats)
    print(f"[shadow] wrote {LATEST_PATH}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Prebreakout shadow research (no prod factor changes)")
    ap.add_argument("--trade-date", default="", help="YYYYMMDD signal date")
    ap.add_argument("--settle-only", action="store_true", help="only settle ledger pending rows")
    args = ap.parse_args()
    td = resolve_trade_date(args.trade_date or None)
    try:
        run_for_date(td, settle_only=bool(args.settle_only))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        write_json(
            STATUS_PATH,
            {"ok": False, "generated_at": now_str(), "error": f"{type(exc).__name__}: {exc}"},
        )
        print(f"[shadow] FATAL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
