#!/usr/bin/env python3
"""O2C 日度复盘积累器：用真实后验行情计算 O2C Top20 的次日表现，写策略库 + 产出 review_state_o2c.json。

链路：
  greenfield_o2c_v1.db.recommendation_snapshot（历史 top20 推荐，按 rank_no）
    → 对每个 recommend_date 取下一个有效交易日（行情并集去重日历）
    → 用本地行情并集（所有 backups/*stock_analysis*.db 的 stock_daily）取真实开/收/涨跌幅
    → 计算 开盘到收盘收益 / 次日收益 / 相对市场均值的超额 / hit
    → 写 review_samples（逐股）+ daily_review_summary（逐日）
    → 汇总产出 stock-report/data/latest/review_state_o2c.json（date_stats + review_gate）

诚实化铁律：
  - 复盘必须用真实后验行情（recommend_date 之后的交易日），不得用当日/预测数据替代。
  - 缺行情的股票 data_complete=0（不计入），缺行情的日 data_complete=0（不算有效复盘日）。
  - 只有 data_complete=1 的交易日才计入「有效复盘样本日」（valid_review_days）。
  - 不写源数据库；只写 O2C 策略库与发布只读 JSON。
  - 周末/无后验日不伪造；同 (recommend_date, review_date, stock_code) upsert，失败不覆盖已有成功。
  - 无本地真·指数 → 基准用「本地行情并集全样本等权均值 pct_chg」（市场宽度代理），并在 summary 注明，不冒充指数。

升级门槛（review_gate，配合发布层的 ai_coverage/data_freshness/review_samples 共同决定 O2C 是否可交易）：
  valid_review_days>=3 且 平均超额>0 且 平均命中率>=50 且 最近3个有效日不连续为负 → pass，否则 fail（0 日则 n/a）。

用法：
  python3 generate_o2c_review.py                 # 回填全部有后验日的历史推荐
  python3 generate_o2c_review.py --recommend-date 20260611
  python3 generate_o2c_review.py --backfill-days 10
  python3 generate_o2c_review.py --rebuild       # 清空两表后全量重算
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
QUOTE_GLOB = str(WORKSPACE / "stock_data" / "03-working" / "backups" / "*stock_analysis*.db")
O2C_MANIFEST_PATH = WORKSPACE / "factor_factory" / "config" / "o2c_frozen_manifest.json"


def load_o2c_manifest() -> dict[str, Any] | None:
    """读取 O2C 参数冻结签名 manifest（只读，不存在则返回 None，不阻断主流程）。"""
    if not O2C_MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(O2C_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_manifest_mismatch(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """仅对 greenfield_o2c_v1 关键参数做代码现值 vs manifest 冻结值的一致性校验。
    只在运行期打印告警，不修改任何参数，不阻断复盘写入。"""
    result: dict[str, Any] = {"checked": manifest is not None, "mismatches": [], "manifest_version": None}
    if manifest is None:
        result["note"] = f"manifest 未找到（{O2C_MANIFEST_PATH}），跳过校验"
        return result
    result["manifest_version"] = manifest.get("manifest_version")
    reg = STRATEGY_REGISTRY.get("greenfield_o2c_v1", {})
    checks = [
        ("hold_days", reg.get("hold_days"), manifest.get("holding_horizon", {}).get("hold_days")),
        ("required_review_days", REQUIRED_REVIEW_DAYS, manifest.get("review_gate", {}).get("required_review_days")),
        ("ir_min", IR_MIN, manifest.get("review_gate", {}).get("ir_min")),
        ("min_settle_per_day_default", 10, manifest.get("review_gate", {}).get("min_settle_per_day")),
    ]
    for field, code_val, manifest_val in checks:
        if manifest_val is not None and code_val != manifest_val:
            result["mismatches"].append({"field": field, "code_value": code_val, "manifest_value": manifest_val})
    if result["mismatches"]:
        print(f"[o2c-review] WARNING: manifest_mismatch detected vs {O2C_MANIFEST_PATH.name}: "
              f"{result['mismatches']}")
    return result
QUOTE_EXTRA = [
    WORKSPACE / "stock_data" / "03-working" / "ai_engine_stock_analysis.db",
    WORKSPACE / "stock_data" / "03-working" / "workspace_stock_analysis.db",
]
LATEST_DIR = WORKSPACE / "stock-report" / "data" / "latest"
_SR = WORKSPACE / "stock_data" / "03-working" / "strategy_research"

# 多策略复盘注册表（2026-06-28 #3 通用化）：复盘逻辑本就策略无关（选股→次日收益→命中/超额）。
STRATEGY_REGISTRY = {
    # hold_days：入场恒为 T+1 开盘（无未来函数），持有 hold_days 个交易日后于该日收盘出场。
    #   O2C 日内 hold=0(T+1 开→收)；T1 次日 hold=1(持有1日)；启动前夕多日 hold=4(持有~5个交易日)。
    # hold_days 取自 optimize_strategy_horizons.py 的真·指数 IR 横盘寻优（2026-06-28）：
    #   O2C=0(日内最优 IR+2.39)；启动前夕=2(T+1开→T+3收,2日动量, IR+3.14, 远胜5日的≈0)；T1=0(各横盘皆负,退役)。
    "greenfield_o2c_v1": {"name": "O2C日内因子", "source": "o2c_factor", "hold_days": 0, "horizon_label": "T+1 日内(开→收)",
                          "db": _SR / "greenfield_o2c_v1" / "greenfield_o2c_v1.db", "out": LATEST_DIR / "review_state_o2c.json"},
    "prebreakout_v41": {"name": "启动前夕", "source": "traditional", "hold_days": 2, "horizon_label": "T+1开盘进、T+3收盘出(持有2日)",
                        "db": _SR / "prebreakout_v41" / "prebreakout_v41.db", "out": LATEST_DIR / "review_state_prebreakout.json"},
    "t1_factor_v1": {"name": "T+1胜率因子", "source": "t1_factor", "hold_days": 0, "horizon_label": "T+1 日内(已退役为研究信号)",
                     "db": _SR / "t1_factor_v1" / "t1_factor_v1.db", "out": LATEST_DIR / "review_state_t1.json"},
}
# 运行期由 --strategy 解析填充（默认 O2C，兼容旧用法）。
STRATEGY_ID = "greenfield_o2c_v1"
STRATEGY_NAME = "O2C日内因子"
STRATEGY_SOURCE = "o2c_factor"
HOLD_DAYS = 0
HORIZON_LABEL = "T+1 日内(开→收)"
O2C_DB = STRATEGY_REGISTRY["greenfield_o2c_v1"]["db"]
REVIEW_OUT = STRATEGY_REGISTRY["greenfield_o2c_v1"]["out"]
TOPN = 20
MIN_SETTLE = 10            # 单日 settled 股票数下限：>=10/20 才算 data_complete=1 有效复盘日
# 升级门槛（2026-06-28 优化）：从"命中率≥50%"改为"风险调整后正超额(IR)+足够样本"——
# 低命中但高 IR 的策略仍是正期望，命中率不应作硬门槛；IR 才是相对真·指数的正确风控标尺。
REQUIRED_REVIEW_DAYS = 20  # 有效复盘交易日下限（统计可信度，约束小样本过度乐观）
IR_MIN = 0.5               # 年化信息比率下限（相对沪深300的风险调整后正超额）
HIT_RATE_MIN = 50.0        # 仅作信息项展示，不再作硬门槛
BENCHMARK_NOTE = "入场=T+1开盘(无未来函数)，按各策略横盘出场；基准=沪深300真·指数同窗口(入场开→出场收)收益；超额=个股横盘收益−基准。"


# --------------------------------------------------------------------------- #
# 行情并集（所有 backup stock_daily），形成去重交易日历 + (date,code)->行情
# --------------------------------------------------------------------------- #
def load_quote_union() -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, float]]:
    """返回 (quotes, market_mean)。quotes[yyyymmdd][code] = {open, close, pct_chg}；market_mean[yyyymmdd]=均值涨跌幅。"""
    paths = sorted(glob.glob(QUOTE_GLOB)) + [str(p) for p in QUOTE_EXTRA if p.exists()]
    quotes: dict[str, dict[str, dict[str, float]]] = {}
    for db in paths:
        try:
            # immutable=1：备份快照可能带 WAL 锁，普通 mode=ro 会 "unable to open"；快照只读不可变，可安全 immutable。
            conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=20)
        except sqlite3.Error:
            continue
        try:
            cur = conn.execute("SELECT code, date, open, close, pct_chg FROM stock_daily")
        except sqlite3.Error:
            conn.close()
            continue
        for code, date, op, cl, pct in cur:
            d = str(date).replace("-", "")
            c = str(code)
            day = quotes.setdefault(d, {})
            # 后读的库不覆盖已有（首个非空即可，避免快照间细微差异反复抖动）
            if c not in day:
                day[c] = {
                    "open": _f(op), "close": _f(cl),
                    "pct_chg": _f(pct),
                }
        conn.close()
    # 并入 akshare 补全缓存（enrich_review_quotes.py 产物）：补历史选股缺失的开/收，消除幸存者偏差。
    cache_db = WORKSPACE / "stock_data" / "03-working" / "review_quote_cache.db"
    if cache_db.exists():
        try:
            cc = sqlite3.connect(f"file:{cache_db}?mode=ro&immutable=1", uri=True, timeout=20)
            for code, date, op, cl in cc.execute("SELECT code, date, open, close FROM quote_cache"):
                d, c = str(date).replace("-", ""), str(code)
                day = quotes.setdefault(d, {})
                cur = day.get(c)
                # 缓存仅补缺：本地并集已有该股开/收则不覆盖（本地为权威），否则用缓存补上。
                if cur is None:
                    day[c] = {"open": _f(op), "close": _f(cl), "pct_chg": None}
                else:
                    if cur.get("open") is None:
                        cur["open"] = _f(op)
                    if cur.get("close") is None:
                        cur["close"] = _f(cl)
            cc.close()
        except sqlite3.Error:
            pass
    market_mean: dict[str, float] = {}
    for d, day in quotes.items():
        vals = [v["pct_chg"] for v in day.values() if v["pct_chg"] is not None]
        if vals:
            market_mean[d] = sum(vals) / len(vals)
    return quotes, market_mean


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def next_trade_date(calendar: list[str], rd: str) -> str | None:
    for d in calendar:
        if d > rd:
            return d
    return None


def nth_trade_date(calendar: list[str], rd: str, n: int) -> str | None:
    """rd 之后第 n 个交易日（n=1 即 T+1）。calendar 升序。越界返回 None。"""
    after = [d for d in calendar if d > rd]
    return after[n - 1] if 0 < n <= len(after) else None


def load_index_prices() -> dict[str, dict[str, float]]:
    """akshare(新浪)真·指数开收盘价。返回 {yyyymmdd: {'open': x, 'close': y}}（沪深300 sh000300）。
    2026-06-28：换真·指数基准；按横盘需要支持任意窗口（入场开盘→出场收盘）的指数收益。"""
    out: dict[str, dict[str, float]] = {}
    try:
        import akshare as ak  # noqa: PLC0415
        df = ak.stock_zh_index_daily(symbol="sh000300")
    except Exception:
        return out
    for _, row in df.iterrows():
        d = str(row["date"]).replace("-", "")
        out[d] = {"open": _f(row.get("open")), "close": _f(row.get("close"))}
    return out


# --------------------------------------------------------------------------- #
# DB 表
# --------------------------------------------------------------------------- #
def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_samples (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          recommend_date TEXT NOT NULL,
          review_date TEXT NOT NULL,
          stock_code TEXT NOT NULL,
          ts_code TEXT,
          stock_name TEXT,
          rank_no INTEGER,
          raw_action TEXT,
          final_action TEXT,
          open_price REAL,
          close_price REAL,
          next_open_price REAL,
          next_close_price REAL,
          open_to_close_return_pct REAL,
          next_day_return_pct REAL,
          excess_return_pct REAL,
          hit INTEGER,
          ai_advice TEXT,
          ai_analysis_type TEXT,
          data_complete INTEGER,
          generated_at TEXT NOT NULL,
          UNIQUE(strategy_id, recommend_date, review_date, stock_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_review_summary (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          recommend_date TEXT NOT NULL,
          review_date TEXT NOT NULL,
          sample_count INTEGER,
          hit_rate_pct REAL,
          avg_open_to_close_return_pct REAL,
          avg_next_day_return_pct REAL,
          avg_excess_return_pct REAL,
          buy_count INTEGER,
          watch_count INTEGER,
          avoid_count INTEGER,
          ai_buy_hit_rate_pct REAL,
          ai_watch_hit_rate_pct REAL,
          data_complete INTEGER,
          generated_at TEXT NOT NULL,
          UNIQUE(strategy_id, recommend_date, review_date)
        )
        """
    )
    conn.commit()


def upsert_sample(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    cols = ", ".join(rec.keys())
    ph = ", ".join(f":{k}" for k in rec)
    setc = ", ".join(f"{k}=excluded.{k}" for k in rec if k not in ("strategy_id", "recommend_date", "review_date", "stock_code"))
    conn.execute(
        f"INSERT INTO review_samples ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(strategy_id, recommend_date, review_date, stock_code) DO UPDATE SET {setc}",
        rec,
    )


def upsert_summary(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    cols = ", ".join(rec.keys())
    ph = ", ".join(f":{k}" for k in rec)
    setc = ", ".join(f"{k}=excluded.{k}" for k in rec if k not in ("strategy_id", "recommend_date", "review_date"))
    conn.execute(
        f"INSERT INTO daily_review_summary ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(strategy_id, recommend_date, review_date) DO UPDATE SET {setc}",
        rec,
    )


# --------------------------------------------------------------------------- #
# 逐日复盘计算
# --------------------------------------------------------------------------- #
def review_one_date(conn: sqlite3.Connection, rd: str, calendar: list[str],
                    quotes: dict[str, dict[str, dict[str, float]]], market_mean: dict[str, float],
                    index_px: dict[str, dict[str, float]], now: str) -> dict[str, Any] | None:
    # 横盘：入场=T+1 开盘（无未来函数），出场=T+(1+HOLD_DAYS) 收盘。基准用指数同窗口（入场开→出场收）。
    entry_day = nth_trade_date(calendar, rd, 1)
    exit_day = nth_trade_date(calendar, rd, 1 + HOLD_DAYS)
    if not entry_day or not exit_day:
        return None  # 横盘尚未走完（最新若干日），不伪造
    picks = conn.execute(
        "SELECT rank_no, stock_code, ts_code, stock_name, raw_action, adjusted_action, ai_view, ai_summary "
        "FROM recommendation_snapshot WHERE strategy_id=? AND recommend_date=? ORDER BY rank_no LIMIT ?",
        (STRATEGY_ID, rd, TOPN),
    ).fetchall()
    if not picks:
        return None
    # 真·指数同窗口收益：(指数出场日收盘 − 指数入场日开盘) / 指数入场日开盘；缺失才回退本地宽度均值。
    ie, xe = index_px.get(entry_day) or {}, index_px.get(exit_day) or {}
    bench = None
    if ie.get("open") and xe.get("close") is not None:
        bench = (xe["close"] - ie["open"]) / ie["open"] * 100
    if bench is None:
        bench = market_mean.get(exit_day)
    settled: list[dict[str, Any]] = []
    for rank_no, code, ts_code, name, raw_action, adj_action, ai_view, ai_summary in picks:
        code = str(code)
        rec_q = quotes.get(rd, {}).get(code)
        en_q = quotes.get(entry_day, {}).get(code)
        ex_q = quotes.get(exit_day, {}).get(code)
        en_open = (en_q or {}).get("open")
        ex_close = (ex_q or {}).get("close")
        data_complete = 1 if (en_open and ex_close is not None) else 0
        # 推荐日当天 open→close（信息项，留作对照）。
        otc = None
        if rec_q and rec_q.get("open") and rec_q.get("close") is not None and rec_q["open"]:
            otc = (rec_q["close"] - rec_q["open"]) / rec_q["open"] * 100
        # 横盘收益：入场开盘 → 出场收盘。
        hret = (ex_close - en_open) / en_open * 100 if (en_open and ex_close is not None) else None
        excess = (hret - bench) if (hret is not None and bench is not None) else None
        hit = (1 if (hret is not None and hret > 0) else 0) if hret is not None else None
        sample = {
            "strategy_id": STRATEGY_ID, "recommend_date": rd, "review_date": exit_day, "stock_code": code,
            "ts_code": ts_code, "stock_name": name, "rank_no": rank_no,
            "raw_action": raw_action, "final_action": adj_action,
            "open_price": en_open, "close_price": ex_close,
            "next_open_price": en_open, "next_close_price": ex_close,
            "open_to_close_return_pct": _round(otc), "next_day_return_pct": _round(hret),
            "excess_return_pct": _round(excess), "hit": hit,
            "ai_advice": adj_action, "ai_analysis_type": "o2c_ai" if ai_summary else None,
            "data_complete": data_complete, "generated_at": now,
        }
        upsert_sample(conn, sample)
        if data_complete:
            settled.append(sample)

    n = len(settled)
    day_complete = 1 if n >= MIN_SETTLE else 0
    hits = [s for s in settled if s["hit"] is not None]
    summary = {
        "strategy_id": STRATEGY_ID, "recommend_date": rd, "review_date": exit_day,
        "sample_count": n,
        "hit_rate_pct": _round(100.0 * sum(s["hit"] for s in hits) / len(hits)) if hits else None,
        "avg_open_to_close_return_pct": _avg([s["open_to_close_return_pct"] for s in settled]),
        "avg_next_day_return_pct": _avg([s["next_day_return_pct"] for s in settled]),
        "avg_excess_return_pct": _avg([s["excess_return_pct"] for s in settled]),
        "buy_count": sum(1 for s in settled if s["final_action"] == "main"),
        "watch_count": sum(1 for s in settled if s["final_action"] == "watch"),
        "avoid_count": sum(1 for s in settled if s["final_action"] == "avoid"),
        "ai_buy_hit_rate_pct": _hit_rate([s for s in settled if s["final_action"] == "main"]),
        "ai_watch_hit_rate_pct": _hit_rate([s for s in settled if s["final_action"] == "watch"]),
        "data_complete": day_complete, "generated_at": now,
    }
    upsert_summary(conn, summary)
    conn.commit()
    return summary


def _round(v: Any) -> float | None:
    return round(v, 4) if isinstance(v, (int, float)) else None


def _avg(vals: list[Any]) -> float | None:
    xs = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(xs) / len(xs), 4) if xs else None


def _hit_rate(rows: list[dict[str, Any]]) -> float | None:
    hs = [r["hit"] for r in rows if r["hit"] is not None]
    return round(100.0 * sum(hs) / len(hs), 2) if hs else None


# --------------------------------------------------------------------------- #
# 汇总 → review_state_o2c.json（date_stats + review_gate）
# --------------------------------------------------------------------------- #
def build_review_state(conn: sqlite3.Connection, now: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT recommend_date, review_date, sample_count, hit_rate_pct, avg_next_day_return_pct, "
        "avg_excess_return_pct, data_complete FROM daily_review_summary WHERE strategy_id=? ORDER BY recommend_date DESC",
        (STRATEGY_ID,),
    ).fetchall()
    valid = [r for r in rows if r[6] == 1]   # data_complete=1
    date_stats = [
        {
            "recommend_date": r[0], "next_trade_date": r[1], "sample_count": r[2],
            "next_day_hit_rate_pct": r[3],
            "avg_next_day_return_pct": r[4], "avg_cumulative_return_pct": r[4],
            "avg_excess_return_pct": r[5],
        }
        for r in valid
    ]
    # review_gate：valid_days>=3 且 平均超额>0 且 平均命中率>=50 且 最近3个有效日不连续为负
    valid_days = len(valid)
    hit_rates = [r[3] for r in valid if r[3] is not None]
    excesses = [r[5] for r in valid if r[5] is not None]
    rets = [r[4] for r in valid if r[4] is not None]
    avg_hit = round(sum(hit_rates) / len(hit_rates), 2) if hit_rates else None
    avg_excess = round(sum(excesses) / len(excesses), 4) if excesses else None
    # 信息比率 IR = 日均超额 / 日超额标准差 × √252（相对真·指数；越高越稳定地跑赢基准）。
    info_ratio = None
    if len(excesses) >= 2:
        m = sum(excesses) / len(excesses)
        var = sum((x - m) ** 2 for x in excesses) / (len(excesses) - 1)
        sd = var ** 0.5
        if sd > 1e-9:
            info_ratio = round((m / sd) * (252 ** 0.5), 3)
    # valid 已按 recommend_date DESC 排序 → 取前 3 个为「最近 3 个有效日」
    recent3 = [r[4] for r in valid[:3] if r[4] is not None]
    recent_consecutive_negative = len(recent3) >= 3 and all(x < 0 for x in recent3)

    if valid_days == 0:
        status = "n/a"
        summary = "尚无有效复盘样本，暂不作为买入依据"
    else:
        # 门槛=足够样本 + 相对真·指数的风险调整后正超额（IR）+ 平均超额>0 + 最近不连负。命中率仅作信息项。
        conds = [
            valid_days >= REQUIRED_REVIEW_DAYS,
            avg_excess is not None and avg_excess > 0,
            info_ratio is not None and info_ratio >= IR_MIN,
            not recent_consecutive_negative,
        ]
        if all(conds):
            status = "pass"
            summary = f"{STRATEGY_NAME} 已通过复盘门槛（{HORIZON_LABEL}；有效样本日 {valid_days}，IR {info_ratio}≥{IR_MIN}，超额 {avg_excess}%，命中率 {avg_hit}%）"
        else:
            status = "fail"
            miss = []
            if valid_days < REQUIRED_REVIEW_DAYS:
                miss.append(f"有效样本日 {valid_days}/{REQUIRED_REVIEW_DAYS}（样本不足，结论暂不可信）")
            if not (avg_excess is not None and avg_excess > 0):
                miss.append(f"平均超额 {avg_excess}%（需>0）")
            if not (info_ratio is not None and info_ratio >= IR_MIN):
                miss.append(f"IR {info_ratio}（需≥{IR_MIN}）")
            if recent_consecutive_negative:
                miss.append("最近3个有效日连续为负")
            summary = f"{STRATEGY_NAME}（{HORIZON_LABEL}）未达升级门槛：" + "；".join(miss) + "，暂不作为买入依据"

    review_gate = {
        "valid_review_days": valid_days,
        "required_review_days": REQUIRED_REVIEW_DAYS,
        "hit_rate_pct": avg_hit,
        "avg_excess_return_pct": avg_excess,
        "information_ratio": info_ratio,
        "ir_min": IR_MIN,
        "horizon_label": HORIZON_LABEL,
        "avg_next_day_return_pct": round(sum(rets) / len(rets), 4) if rets else None,
        "recent_consecutive_negative": recent_consecutive_negative,
        "status": status,
        "summary": summary,
        "benchmark_note": BENCHMARK_NOTE,
    }
    all_dates = sorted({r[0] for r in rows}, reverse=True)
    return {
        "generated_at": now,
        "strategy_id": STRATEGY_ID,
        "strategy_name": STRATEGY_NAME,
        "strategy_source": STRATEGY_SOURCE,
        "review_basis": "real_forward_quotes",
        "benchmark_note": BENCHMARK_NOTE,
        "min_settle_per_day": MIN_SETTLE,
        "total_review_dates": len(rows),
        "valid_review_days": valid_days,
        "latest_recommend_date": all_dates[0] if all_dates else None,
        "latest_evaluable_recommend_date": (valid[0][0] if valid else None),
        "date_range": {"from": all_dates[-1] if all_dates else None, "to": all_dates[0] if all_dates else None,
                       "date_count": len(all_dates)},
        "performance": {
            "avg_next_day_return_pct": review_gate["avg_next_day_return_pct"],
            "avg_cumulative_return_pct": review_gate["avg_next_day_return_pct"],
            "next_day_hit_rate_pct": avg_hit,
            "avg_excess_return_pct": avg_excess,
        },
        "date_stats": date_stats,
        "review_gate": review_gate,
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    global MIN_SETTLE, STRATEGY_ID, STRATEGY_NAME, STRATEGY_SOURCE, O2C_DB, REVIEW_OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="greenfield_o2c_v1", choices=list(STRATEGY_REGISTRY),
                    help="复盘哪套策略（默认 O2C）")
    ap.add_argument("--all", action="store_true", help="依次复盘全部三套策略")
    ap.add_argument("--recommend-date", default=None, help="只复盘单个推荐日")
    ap.add_argument("--backfill-days", type=int, default=0, help="只复盘最近 N 个推荐日")
    ap.add_argument("--rebuild", action="store_true", help="清空两表后全量重算")
    ap.add_argument("--min-settle", type=int, default=MIN_SETTLE)
    args = ap.parse_args()
    if args.all:
        rc = 0
        for sid in STRATEGY_REGISTRY:
            rc |= _run_one_strategy(sid, args)
        return rc
    return _run_one_strategy(args.strategy, args)


def _run_one_strategy(sid: str, args) -> int:
    global MIN_SETTLE, STRATEGY_ID, STRATEGY_NAME, STRATEGY_SOURCE, O2C_DB, REVIEW_OUT, HOLD_DAYS, HORIZON_LABEL
    reg = STRATEGY_REGISTRY[sid]
    STRATEGY_ID, STRATEGY_NAME, STRATEGY_SOURCE = sid, reg["name"], reg["source"]
    HOLD_DAYS, HORIZON_LABEL = reg.get("hold_days", 0), reg.get("horizon_label", "")
    O2C_DB, REVIEW_OUT = reg["db"], reg["out"]
    MIN_SETTLE = args.min_settle

    if not O2C_DB.exists():
        print(json.dumps({"ok": False, "reason": "missing_o2c_db", "path": str(O2C_DB)}, ensure_ascii=False))
        return 1
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    quotes, market_mean = load_quote_union()
    calendar = sorted(quotes.keys())
    if not calendar:
        print(json.dumps({"ok": False, "reason": "no_quote_union"}, ensure_ascii=False))
        return 1
    index_px = load_index_prices()  # 真·指数开收盘价（沪深300，新浪源）

    conn = sqlite3.connect(str(O2C_DB), timeout=30)
    ensure_tables(conn)
    if args.rebuild:
        conn.execute("DELETE FROM review_samples WHERE strategy_id=?", (STRATEGY_ID,))
        conn.execute("DELETE FROM daily_review_summary WHERE strategy_id=?", (STRATEGY_ID,))
        conn.commit()

    rdates = [r[0] for r in conn.execute(
        "SELECT DISTINCT recommend_date FROM recommendation_snapshot WHERE strategy_id=? ORDER BY recommend_date",
        (STRATEGY_ID,),
    ).fetchall()]
    if args.recommend_date:
        rdates = [d for d in rdates if d == args.recommend_date]
    elif args.backfill_days > 0:
        rdates = rdates[-args.backfill_days:]

    processed, valid = 0, 0
    for rd in rdates:
        summ = review_one_date(conn, rd, calendar, quotes, market_mean, index_px, now)
        if summ is None:
            continue
        processed += 1
        if summ["data_complete"] == 1:
            valid += 1
        print(f"  {rd} -> {summ['review_date']}: settled={summ['sample_count']} "
              f"hit={summ['hit_rate_pct']} excess={summ['avg_excess_return_pct']} valid={summ['data_complete']}")

    state = build_review_state(conn, now)
    conn.close()
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_OUT.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest_check = check_manifest_mismatch(load_o2c_manifest()) if sid == "greenfield_o2c_v1" else None

    out = {
        "ok": True, "processed_dates": processed, "valid_review_days": valid,
        "review_gate": state["review_gate"], "review_state": str(REVIEW_OUT),
        "calendar_dates": len(calendar),
    }
    if manifest_check is not None:
        out["manifest_mismatch"] = manifest_check
    print("[o2c-review] " + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
