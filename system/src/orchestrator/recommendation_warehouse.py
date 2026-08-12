#!/usr/bin/env python3
"""Recommendation warehouse helpers for Fisher's stock pipeline."""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import sys
import time
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd

from orchestrator_common import (
    detect_local_http_proxy,
    HEALTH_DIR,
    PUBLISHED_REPO,
    TARGET_STRATEGY,
    TARGET_STRATEGY_VERSION,
    WORKING_REPO,
    WORKING_STRATEGY_JSON,
    find_strategy,
    load_json,
    normalize_code,
    now_str,
    write_json,
)
from trading_calendar_store import load_open_trade_dates, next_open_trade_date

STOCK_WORKING_DIR = WORKING_REPO.parent
BACKTEST_CACHE_DIR = STOCK_WORKING_DIR / "backtest_cache"
AI_ANALYSIS_DIR = STOCK_WORKING_DIR / "ai_analysis"
SELECTION_HISTORY_DIR = STOCK_WORKING_DIR / "selection_history"
VERIFY_HISTORY_DIR = STOCK_WORKING_DIR / "verify_history"
WAREHOUSE_DIR = STOCK_WORKING_DIR / "recommendation_warehouse"
EXPORT_DIR = WAREHOUSE_DIR / "exports"
DB_PATH = WAREHOUSE_DIR / "recommendations.db"
SYNC_REPORT_PATH = HEALTH_DIR / "recommendation_db_sync.json"
INDUSTRY_RAW_DIR = STOCK_WORKING_DIR / "alpha_mining" / "tushare_pro_demo" / "raw"
TARGET_STRATEGY_NAME = "启动前夕 v4.3 对照"
TARGET_STRATEGY_DISPLAY = "启动前夕"
TRADITIONAL_STRATEGY_SOURCE = "traditional"
O2C_STRATEGY_ID = "greenfield_o2c_v1"
O2C_STRATEGY_NAME = "O2C日内因子 v1"
O2C_STRATEGY_DISPLAY = "O2C日内因子"
# 20260710修: 发布仓(live)在前。旧工作克隆的 greenfield_top20.json 自20260626起不再被
# 管线写入(冻结在0702快照), 原"工作仓在前"的顺序让仓库O2C同步静默停更两周
# (o2c_snapshot_date一直是20260626)。strategy_backtests.json 等仍由管线写工作克隆, 不受此影响。
GREENFIELD_TOP20_CANDIDATES = [
    PUBLISHED_REPO / "data/latest/greenfield_top20.json",
    WORKING_REPO / "data/latest/greenfield_top20.json",
]
HISTORY_DIR_CANDIDATES = [
    WORKING_REPO / "data" / "history",
    WORKING_REPO / "history",
    PUBLISHED_REPO / "data" / "history",
    PUBLISHED_REPO / "history",
]
TARGET_SYNC_RETRY_ATTEMPTS = int(os.environ.get("OPENCLAW_STAGE5_TARGET_RETRY_ATTEMPTS", "3"))
TARGET_SYNC_BACKOFF_BASE_SECONDS = int(os.environ.get("OPENCLAW_STAGE5_TARGET_BACKOFF_BASE_SECONDS", "30"))
SINA_QUOTE_ENDPOINT = "https://hq.sinajs.cn/list="
TENCENT_QUOTE_ENDPOINT = "https://qt.gtimg.cn/q="
SINA_BATCH_SIZE = 60
TENCENT_BATCH_SIZE = 60
SINA_RE = re.compile(r'^var hq_str_(?P<symbol>[a-z0-9]+)="(?P<body>.*)";$')
STRATEGY_VERSION_BY_ID = {
    TARGET_STRATEGY: TARGET_STRATEGY_VERSION,
    O2C_STRATEGY_ID: O2C_STRATEGY_ID,
}
DEFAULT_HOLDING_PERIOD_DAYS = {
    TARGET_STRATEGY: 5,
    O2C_STRATEGY_ID: 1,
}
DEFAULT_ROUND_TRIP_COST = {
    TARGET_STRATEGY: 0.003,
    O2C_STRATEGY_ID: 0.003,
}
DEFAULT_BENCHMARK = {
    TARGET_STRATEGY: "all_a_tradable_equal_weight",
    O2C_STRATEGY_ID: "all_a_tradable_equal_weight",
}


def parse_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    return int(number) if number is not None else None


def parse_bool(value: Any) -> bool | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def ensure_str_list(value: Any) -> list[str]:
    if value in (None, "", "None"):
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def next_trade_date_from_calendar(
    recommend_date: str,
    trade_dates: list[str] | None = None,
    exchange_trade_dates: list[str] | None = None,
) -> str | None:
    dates = exchange_trade_dates if exchange_trade_dates is not None else (trade_dates if trade_dates is not None else load_open_trade_dates())
    return next_open_trade_date(recommend_date, open_dates=dates)


def next_trading_open(
    recommend_date: str,
    trade_dates: list[str] | None = None,
    exchange_trade_dates: list[str] | None = None,
) -> str | None:
    next_trade_date = next_trade_date_from_calendar(
        recommend_date,
        trade_dates=trade_dates,
        exchange_trade_dates=exchange_trade_dates,
    )
    if next_trade_date is None:
        return None
    dt = datetime.strptime(next_trade_date, "%Y%m%d")
    return dt.strftime("%Y-%m-%dT09:30:00+08:00")


def normalize_stock_code(value: Any) -> str:
    text = normalize_code(value)
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


def quote_symbol_for_code(code: str) -> str | None:
    normalized = normalize_stock_code(code)
    if not normalized:
        return None
    if normalized.startswith(("0", "3")):
        return f"sz{normalized}"
    if normalized.startswith("6"):
        return f"sh{normalized}"
    if normalized.startswith(("4", "8")):
        return f"bj{normalized}"
    return None


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def normalize_quote_trade_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _quote_http_get(url: str, *, timeout_seconds: int = 10) -> str:
    proxy_url = os.environ.get("OPENCLAW_HTTP_PROXY") or detect_local_http_proxy()
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url})) if proxy_url else build_opener()
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 OpenClaw/stock-system",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with opener.open(request, timeout=timeout_seconds) as response:
        raw = response.read()
    for encoding in ("gbk", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def fetch_sina_quotes(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    quotes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    if not symbols:
        return quotes, errors
    for batch in chunked(symbols, SINA_BATCH_SIZE):
        url = SINA_QUOTE_ENDPOINT + ",".join(batch)
        try:
            text = _quote_http_get(url)
        except Exception as exc:
            for symbol in batch:
                errors[symbol] = f"sina request failed: {type(exc).__name__}: {exc}"
            continue
        for line in text.splitlines():
            match = SINA_RE.match(line.strip())
            if not match:
                continue
            symbol = match.group("symbol")
            body = match.group("body")
            fields = body.split(",")
            if len(fields) < 32:
                errors[symbol] = "sina payload too short"
                continue
            trade_date = normalize_quote_trade_date(fields[30])
            close = parse_float(fields[3])
            prev_close = parse_float(fields[2])
            pct_chg = None
            if close not in (None, 0) and prev_close not in (None, 0):
                pct_chg = round((close / prev_close - 1.0) * 100.0, 4)
            quotes[symbol] = {
                "symbol": symbol,
                "trade_date": trade_date,
                "close": close,
                "pct_chg": pct_chg,
                "source": "sina",
                "raw_time": fields[31] if len(fields) > 31 else None,
            }
        unresolved = [symbol for symbol in batch if symbol not in quotes and symbol not in errors]
        for symbol in unresolved:
            errors[symbol] = "sina quote missing"
    return quotes, errors


def fetch_tencent_quotes(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    quotes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    if not symbols:
        return quotes, errors
    for batch in chunked(symbols, TENCENT_BATCH_SIZE):
        url = TENCENT_QUOTE_ENDPOINT + ",".join(batch)
        try:
            text = _quote_http_get(url)
        except Exception as exc:
            for symbol in batch:
                errors[symbol] = f"tencent request failed: {type(exc).__name__}: {exc}"
            continue
        for line in text.splitlines():
            raw = line.strip()
            if not raw or "~" not in raw or "=" not in raw:
                continue
            prefix, payload = raw.split("=", 1)
            symbol = prefix.replace("v_", "").strip()
            fields = payload.strip('" ;').split("~")
            if len(fields) < 31:
                errors[symbol] = "tencent payload too short"
                continue
            close = parse_float(fields[3])
            prev_close = parse_float(fields[4])
            pct_chg = None
            if close not in (None, 0) and prev_close not in (None, 0):
                pct_chg = round((close / prev_close - 1.0) * 100.0, 4)
            trade_date = normalize_quote_trade_date(fields[30])
            quotes[symbol] = {
                "symbol": symbol,
                "trade_date": trade_date,
                "close": close,
                "pct_chg": pct_chg,
                "source": "tencent",
                "raw_time": fields[30] if len(fields) > 30 else None,
            }
        unresolved = [symbol for symbol in batch if symbol not in quotes and symbol not in errors]
        for symbol in unresolved:
            errors[symbol] = "tencent quote missing"
    return quotes, errors


def backoff_schedule() -> list[int]:
    attempts = max(1, TARGET_SYNC_RETRY_ATTEMPTS)
    base = max(1, TARGET_SYNC_BACKOFF_BASE_SECONDS)
    return [base * (2 ** index) for index in range(attempts)]


def collect_target_snapshot_with_backoff(target_date: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    last_snapshots: dict[str, list[dict[str, Any]]] = {}
    last_notes: dict[str, Any] = {}
    delays = backoff_schedule()
    for attempt, delay in enumerate(delays, start=1):
        snapshots, notes = collect_daily_recommendations()
        last_snapshots = snapshots
        last_notes = notes
        current_date = notes.get("current_snapshot_date")
        if current_date == target_date and snapshots.get(target_date):
            notes["target_date_attempt"] = attempt
            return snapshots, notes
        if attempt < len(delays):
            print(
                f"[stage5] target snapshot {target_date} not ready "
                f"(current={current_date or 'none'}); sleeping {delay}s before retry {attempt + 1}"
            )
            time.sleep(delay)
    raise TimeoutError(
        f"recommendation snapshot did not reach target_date={target_date}; "
        f"current_snapshot_date={last_notes.get('current_snapshot_date') or 'none'}"
    )


def existing_price_for_codes(conn: sqlite3.Connection, target_date: str, codes: list[str]) -> set[str]:
    if not codes:
        return set()
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"""
        SELECT stock_code
        FROM price_daily_cache
        WHERE trade_date = ? AND stock_code IN ({placeholders})
        """,
        [target_date, *codes],
    ).fetchall()
    return {str(row["stock_code"]) for row in rows}


def insert_price_rows(
    conn: sqlite3.Connection,
    *,
    target_date: str,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    now = now_str()
    payload = []
    for row in rows:
        payload.append(
            (
                row["stock_code"],
                row.get("ts_code"),
                target_date,
                None,
                None,
                None,
                row.get("close"),
                row.get("pct_chg"),
                row.get("source_file"),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO price_daily_cache (
            stock_code, ts_code, trade_date, open, high, low, close, pct_chg, source_file, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code, trade_date) DO UPDATE SET
            ts_code=COALESCE(excluded.ts_code, price_daily_cache.ts_code),
            close=COALESCE(excluded.close, price_daily_cache.close),
            pct_chg=COALESCE(excluded.pct_chg, price_daily_cache.pct_chg),
            source_file=excluded.source_file,
            updated_at=excluded.updated_at
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def backfill_target_date_prices(
    conn: sqlite3.Connection,
    *,
    target_date: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    codes = sorted({item["stock_code"] for item in items if item.get("stock_code")})
    symbols = {quote_symbol_for_code(code): code for code in codes}
    symbols = {symbol: code for symbol, code in symbols.items() if symbol}
    if not symbols:
        return {"ok": False, "reason": "no symbols", "filled_codes": []}

    delays = backoff_schedule()
    report: dict[str, Any] = {
        "target_date": target_date,
        "attempts": [],
        "ok": False,
        "filled_codes": [],
    }
    for attempt, delay in enumerate(delays, start=1):
        filled_before = existing_price_for_codes(conn, target_date, codes)
        missing_codes = [code for code in codes if code not in filled_before]
        if not missing_codes:
            report["ok"] = True
            report["filled_codes"] = sorted(filled_before)
            return report

        missing_symbols = [symbol for symbol, code in symbols.items() if code in missing_codes]
        attempt_info: dict[str, Any] = {
            "attempt": attempt,
            "missing_codes_before": missing_codes,
            "sources": [],
        }
        report["attempts"].append(attempt_info)

        source_rows: list[dict[str, Any]] = []

        sina_quotes, sina_errors = fetch_sina_quotes(missing_symbols)
        attempt_info["sources"].append(
            {
                "name": "sina",
                "quotes": len(sina_quotes),
                "errors": list(sina_errors.values())[:5],
            }
        )
        for symbol, quote in sina_quotes.items():
            if quote.get("trade_date") != target_date:
                continue
            code = symbols[symbol]
            source_rows.append(
                {
                    "stock_code": code,
                    "ts_code": to_ts_code(code),
                    "close": quote.get("close"),
                    "pct_chg": quote.get("pct_chg"),
                    "source_file": f"public_api:sina:{target_date}",
                }
            )

        if not source_rows:
            tencent_quotes, tencent_errors = fetch_tencent_quotes(missing_symbols)
            attempt_info["sources"].append(
                {
                    "name": "tencent",
                    "quotes": len(tencent_quotes),
                    "errors": list(tencent_errors.values())[:5],
                }
            )
            for symbol, quote in tencent_quotes.items():
                if quote.get("trade_date") != target_date:
                    continue
                code = symbols[symbol]
                source_rows.append(
                    {
                        "stock_code": code,
                        "ts_code": to_ts_code(code),
                        "close": quote.get("close"),
                        "pct_chg": quote.get("pct_chg"),
                        "source_file": f"public_api:tencent:{target_date}",
                    }
                )

        if source_rows:
            inserted = insert_price_rows(conn, target_date=target_date, rows=source_rows)
            attempt_info["inserted_rows"] = inserted

        filled_after = existing_price_for_codes(conn, target_date, codes)
        attempt_info["filled_after"] = len(filled_after)
        if len(filled_after) == len(codes):
            report["ok"] = True
            report["filled_codes"] = sorted(filled_after)
            return report

        if attempt < len(delays):
            print(
                f"[stage5] target-date prices not ready for {target_date}; "
                f"filled {len(filled_after)}/{len(codes)} codes, sleeping {delay}s before retry {attempt + 1}"
            )
            time.sleep(delay)

    raise TimeoutError(
        f"price data did not reach target_date={target_date}; "
        f"filled={len(existing_price_for_codes(conn, target_date, codes))}/{len(codes)}"
    )


def to_ts_code(code: str | None) -> str | None:
    text = normalize_stock_code(code)
    if not text:
        return None
    if text.startswith(("0", "3")):
        return f"{text}.SZ"
    if text.startswith("6"):
        return f"{text}.SH"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def trim_line(value: Any, limit: int = 240) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.replace("\r", "\n")
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:limit] if line else None


def ensure_dirs() -> None:
    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_dim (
            stock_code TEXT PRIMARY KEY,
            ts_code TEXT,
            stock_name TEXT,
            sector_name TEXT,
            market_board TEXT,
            first_seen_date TEXT,
            last_seen_date TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_daily_cache (
            stock_code TEXT NOT NULL,
            ts_code TEXT,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            open_qfq REAL,
            high_qfq REAL,
            low_qfq REAL,
            close_qfq REAL,
            pre_close_qfq REAL,
            pct_chg REAL,
            source_file TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (stock_code, trade_date)
        );

        CREATE TABLE IF NOT EXISTS recommendation_fact (
            strategy_id TEXT NOT NULL,
            strategy_source TEXT DEFAULT 'traditional',
            strategy_name TEXT NOT NULL,
            recommend_date TEXT NOT NULL,
            rank_no INTEGER,
            stock_code TEXT NOT NULL,
            ts_code TEXT,
            stock_name TEXT,
            sector_name TEXT,
            market_board TEXT,
            recommend_reason TEXT,
            ai_view TEXT,
            ai_decision TEXT,
            ai_score REAL,
            ai_confidence TEXT,
            ai_summary TEXT,
            ai_source_date TEXT,
            ai_source_stale INTEGER DEFAULT 0,
            ai_evidence_time TEXT,
            ai_risk_tags_json TEXT,
            ai_explanation TEXT,
            ai_effectiveness_eligible INTEGER DEFAULT 0,
            ai_exclusion_reason TEXT,
            recommend_price REAL,
            next_trade_date TEXT,
            next_day_price REAL,
            next_day_return_pct REAL,
            latest_price_date TEXT,
            latest_price REAL,
            cumulative_return_pct REAL,
            cumulative_recommend_count INTEGER DEFAULT 0,
            source_kind TEXT,
            source_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, recommend_date, stock_code)
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
            run_at TEXT PRIMARY KEY,
            latest_trade_date TEXT,
            latest_price_date TEXT,
            total_rows INTEGER,
            latest_date_rows INTEGER,
            history_dates INTEGER,
            exported_csv TEXT,
            exported_json TEXT,
            ok INTEGER NOT NULL,
            notes_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_recommendation_fact_code
        ON recommendation_fact(stock_code);

        CREATE INDEX IF NOT EXISTS idx_recommendation_fact_date
        ON recommendation_fact(recommend_date);

        CREATE INDEX IF NOT EXISTS idx_recommendation_fact_ai_view
        ON recommendation_fact(ai_view);

        CREATE VIEW IF NOT EXISTS vw_prebreakout_recommendations AS
        SELECT
            recommend_date,
            stock_code,
            stock_name,
            sector_name,
            recommend_reason,
            ai_view,
            ai_decision,
            ai_score,
            recommend_price,
            next_trade_date,
            next_day_price,
            latest_price_date,
            latest_price,
            next_day_return_pct,
            cumulative_return_pct,
            cumulative_recommend_count,
            forward_return_1d,
            forward_return_3d,
            forward_return_5d,
            rank_no
        FROM recommendation_fact
        WHERE strategy_id = 'prebreakout_v41'
        ORDER BY recommend_date DESC, rank_no ASC;

        CREATE VIEW IF NOT EXISTS vw_prebreakout_sector_stats AS
        SELECT
            COALESCE(NULLIF(sector_name, ''), '未知') AS sector_name,
            COUNT(*) AS recommendation_count,
            COUNT(DISTINCT stock_code) AS unique_stock_count,
            ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
            ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
            ROUND(AVG(CASE WHEN ai_effectiveness_eligible = 1 THEN ai_score END), 2) AS avg_ai_score
        FROM recommendation_fact
        WHERE strategy_id = 'prebreakout_v41'
        GROUP BY COALESCE(NULLIF(sector_name, ''), '未知')
        ORDER BY recommendation_count DESC, avg_cumulative_return_pct DESC;

        CREATE VIEW IF NOT EXISTS vw_prebreakout_ai_view_stats AS
        SELECT
            COALESCE(NULLIF(ai_view, ''), '未标注') AS ai_view,
            COUNT(*) AS recommendation_count,
            ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
            ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
            ROUND(AVG(ai_score), 2) AS avg_ai_score
        FROM recommendation_fact
        WHERE strategy_id = 'prebreakout_v41'
          AND ai_effectiveness_eligible = 1
        GROUP BY COALESCE(NULLIF(ai_view, ''), '未标注')
        ORDER BY recommendation_count DESC, avg_cumulative_return_pct DESC;
        """
    )
    conn.commit()


_FORWARD_RETURN_COLUMNS = [
    ("strategy_source", "TEXT DEFAULT 'traditional'"),
    ("forward_return_1d", "REAL"),
    ("forward_return_3d", "REAL"),
    ("forward_return_5d", "REAL"),
]
_RECOMMENDATION_CONTRACT_COLUMNS = [
    ("strategy_version", "TEXT"),
    ("signal_data_cutoff", "TEXT"),
    ("planned_entry_time", "TEXT"),
    ("holding_period_days", "INTEGER"),
    ("data_sources_json", "TEXT"),
    ("used_proxy", "INTEGER DEFAULT 0"),
    ("completeness_status", "TEXT DEFAULT 'pending_settlement'"),
    ("round_trip_cost", "REAL"),
    ("benchmark", "TEXT"),
    ("settlement_status", "TEXT DEFAULT 'pending_settlement'"),
    ("rank_change", "INTEGER DEFAULT 0"),
    ("ai_evidence_time", "TEXT"),
    ("ai_risk_tags_json", "TEXT"),
    ("ai_explanation", "TEXT"),
    ("ai_effectiveness_eligible", "INTEGER DEFAULT 0"),
    ("ai_exclusion_reason", "TEXT"),
]
_PRICE_CACHE_QFQ_COLUMNS = [
    ("open_qfq", "REAL"),
    ("high_qfq", "REAL"),
    ("low_qfq", "REAL"),
    ("close_qfq", "REAL"),
    ("pre_close_qfq", "REAL"),
]


def _migrate_forward_return_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(recommendation_fact)").fetchall()}
    added = False
    for col_name, col_type in _FORWARD_RETURN_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE recommendation_fact ADD COLUMN {col_name} {col_type}")
            added = True
    if added:
        conn.execute("DROP VIEW IF EXISTS vw_prebreakout_recommendations")
    conn.commit()


def _migrate_recommendation_contract_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(recommendation_fact)").fetchall()}
    for col_name, col_type in _RECOMMENDATION_CONTRACT_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE recommendation_fact ADD COLUMN {col_name} {col_type}")
    conn.execute("DROP VIEW IF EXISTS vw_prebreakout_ai_view_stats")
    conn.execute(
        """
        CREATE VIEW vw_prebreakout_ai_view_stats AS
        SELECT
            COALESCE(NULLIF(ai_view, ''), '未标注') AS ai_view,
            COUNT(*) AS recommendation_count,
            ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
            ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
            ROUND(AVG(ai_score), 2) AS avg_ai_score
        FROM recommendation_fact
        WHERE strategy_id = 'prebreakout_v41'
          AND ai_effectiveness_eligible = 1
        GROUP BY COALESCE(NULLIF(ai_view, ''), '未标注')
        ORDER BY recommendation_count DESC, avg_cumulative_return_pct DESC
        """
    )
    conn.commit()


def _migrate_price_cache_qfq_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(price_daily_cache)").fetchall()}
    for col_name, col_type in _PRICE_CACHE_QFQ_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE price_daily_cache ADD COLUMN {col_name} {col_type}")
    conn.commit()


def prepare_recommendation_fact_record(
    item: dict[str, Any],
    *,
    strict: bool = False,
    trade_dates: list[str] | None = None,
    exchange_trade_dates: list[str] | None = None,
) -> dict[str, Any]:
    prepared = dict(item)
    raw = item.get("raw") or {}
    strategy_id = prepared.get("strategy_id") or TARGET_STRATEGY
    recommend_date = str(prepared.get("recommend_date") or "")
    source_kind = str(prepared.get("source_kind") or "")
    prepared["strategy_name"] = clean_text(prepared.get("strategy_name")) or (
        O2C_STRATEGY_NAME if strategy_id == O2C_STRATEGY_ID else TARGET_STRATEGY_NAME
    )
    explicit_data_sources = ensure_str_list(prepared.get("data_sources") or raw.get("data_sources"))
    source_path_fallback_allowed = source_kind in {
        "current_strategy_snapshot",
        "greenfield_o2c_snapshot",
        "history_json",
        "selection_history",
    }
    data_sources = ensure_str_list(
        explicit_data_sources
        or ([prepared.get("source_path")] if source_path_fallback_allowed and prepared.get("source_path") else [])
    )
    used_proxy = parse_bool(prepared.get("used_proxy"))
    if used_proxy is None:
        used_proxy = parse_bool(raw.get("used_proxy"))
    if used_proxy is None:
        used_proxy = any("proxy" in source.lower() for source in data_sources)
    prepared["strategy_version"] = (
        clean_text(prepared.get("strategy_version"))
        or clean_text(raw.get("strategy_version"))
        or STRATEGY_VERSION_BY_ID.get(strategy_id)
    )
    prepared["signal_data_cutoff"] = (
        clean_text(prepared.get("signal_data_cutoff"))
        or clean_text(raw.get("signal_data_cutoff"))
        or (f"{recommend_date}T15:00:00+08:00" if recommend_date else None)
    )
    prepared["planned_entry_time"] = (
        clean_text(prepared.get("planned_entry_time"))
        or clean_text(raw.get("planned_entry_time"))
        or next_trading_open(
            recommend_date,
            trade_dates=trade_dates,
            exchange_trade_dates=exchange_trade_dates,
        )
    )
    prepared["holding_period_days"] = (
        parse_int(prepared.get("holding_period_days"))
        or parse_int(raw.get("holding_period_days"))
        or DEFAULT_HOLDING_PERIOD_DAYS.get(strategy_id)
    )
    prepared["data_sources"] = data_sources
    prepared["used_proxy"] = bool(used_proxy)
    prepared["completeness_status"] = clean_text(prepared.get("completeness_status")) or "pending_settlement"
    prepared["round_trip_cost"] = (
        parse_float(prepared.get("round_trip_cost"))
        or parse_float(raw.get("round_trip_cost"))
        or DEFAULT_ROUND_TRIP_COST.get(strategy_id)
    )
    benchmark = clean_text(prepared.get("benchmark")) or clean_text(raw.get("benchmark"))
    if benchmark in ("", "hs300", None):
        benchmark = DEFAULT_BENCHMARK.get(strategy_id)
    prepared["benchmark"] = benchmark
    prepared["settlement_status"] = clean_text(prepared.get("settlement_status")) or "pending_settlement"
    prepared["ai_evidence_time"] = clean_text(
        prepared.get("ai_evidence_time")
        or raw.get("ai_evidence_time")
        or raw.get("generated_at")
        or raw.get("created_at")
    )
    prepared["ai_risk_tags"] = ensure_str_list(
        prepared.get("ai_risk_tags")
        or raw.get("ai_risk_tags")
        or raw.get("ai_risks")
    )
    prepared["ai_explanation"] = trim_line(
        prepared.get("ai_explanation")
        or prepared.get("ai_summary")
        or raw.get("ai_explanation")
        or raw.get("ai_summary")
        or raw.get("ai_conclusion"),
        limit=600,
    )
    ai_contract = dict(prepared)
    if not ai_contract.get("ai_source_date"):
        ai_contract["ai_source_date"] = raw.get("ai_source_date") or raw.get("ai_analysis_date")
    ai_contract["ai_evidence_time"] = prepared["ai_evidence_time"]
    has_ai_evidence = any(
        prepared.get(field) not in (None, "", [], {})
        for field in ("ai_view", "ai_decision", "ai_score", "ai_summary", "ai_explanation")
    )
    prepared["ai_effectiveness_eligible"] = bool(
        has_ai_evidence and _ai_record_is_same_day_evidence(ai_contract, recommend_date)
    )
    prepared["ai_exclusion_reason"] = (
        None
        if prepared["ai_effectiveness_eligible"]
        else ("evidence_date_mismatch_or_missing" if has_ai_evidence else "no_ai_evidence")
    )
    requested_rank_change = parse_int(prepared.get("rank_change"))
    if requested_rank_change is None:
        requested_rank_change = parse_int(raw.get("rank_change"))
    if requested_rank_change not in (None, 0):
        raise ValueError("AI and downstream systems may not change quantitative rank")
    prepared["rank_change"] = 0
    if strict:
        missing = [
            field
            for field in (
                "strategy_name",
                "strategy_version",
                "signal_data_cutoff",
                "planned_entry_time",
                "holding_period_days",
                "benchmark",
                "round_trip_cost",
            )
            if prepared.get(field) in (None, "", [], {})
        ]
        if not explicit_data_sources and not (source_path_fallback_allowed and prepared.get("source_path")):
            missing.append("data_sources")
        if prepared.get("planned_entry_time") in (None, "", [], {}):
            missing.append("planned_entry_time")
        if missing:
            raise ValueError(f"recommendation contract missing required fields: {', '.join(missing)}")
    return prepared


def _settlement_state(
    *,
    latest_price_date: str | None,
    entry_trade_date: str | None,
    primary_exit_trade_date: str | None,
    entry_open_qfq: float | None,
    primary_return_pct: float | None,
    missing_qfq: bool,
) -> tuple[str, str]:
    if not entry_trade_date or not latest_price_date or latest_price_date < entry_trade_date:
        return "pending_settlement", "pending_settlement"
    if primary_exit_trade_date is None or latest_price_date < primary_exit_trade_date:
        return "pending_settlement", "pending_settlement"
    if missing_qfq or entry_open_qfq in (None, 0) or primary_return_pct is None:
        return "data_missing", "data_missing"
    return "complete", "settled"


def repair_recommendation_metrics(
    *,
    conn: sqlite3.Connection | None = None,
    trade_dates: list[str] | None = None,
    hydrate_price_cache: bool = True,
) -> dict[str, Any]:
    own_conn = conn is None
    if own_conn:
        conn = connect_db()
        init_db(conn)
        _migrate_forward_return_columns(conn)
        _migrate_recommendation_contract_columns(conn)
        _migrate_price_cache_qfq_columns(conn)
    if trade_dates is None:
        trade_dates = available_trade_dates()
    price_cache_info = (
        ensure_price_cache(conn, trade_dates)
        if hydrate_price_cache
        else {
            "dates_requested": len(trade_dates),
            "dates_with_daily": 0,
            "dates_with_qfq": 0,
            "rows_upserted": 0,
        }
    )
    result = recompute_metrics(conn, trade_dates)
    ai_result = repair_ai_effectiveness_flags(conn)
    report = {
        "rows_rebuilt": int(result.get("rows") or 0),
        "latest_price_date": result.get("latest_price_date"),
        "price_cache": price_cache_info,
        "ai_integrity": ai_result,
    }
    if own_conn:
        conn.close()
    return report


def repair_ai_effectiveness_flags(conn: sqlite3.Connection) -> dict[str, Any]:
    """Rebuild historical AI eligibility without guessing missing evidence times."""
    rows = conn.execute(
        """
        SELECT strategy_id, recommend_date, stock_code,
               ai_view, ai_decision, ai_score, ai_summary, ai_explanation,
               ai_source_date, ai_evidence_time
        FROM recommendation_fact
        """
    ).fetchall()
    updates: list[tuple[int, str | None, str, str, str]] = []
    eligible_rows = 0
    future_backfill_rows = 0
    has_ai_rows = 0
    for row in rows:
        has_ai = any(
            row[field] not in (None, "")
            for field in (
                "ai_view",
                "ai_decision",
                "ai_score",
                "ai_summary",
                "ai_explanation",
                "ai_source_date",
            )
        )
        target = _normalized_ai_date(row["recommend_date"])
        source = _normalized_ai_date(row["ai_source_date"])
        evidence = _normalized_ai_date(row["ai_evidence_time"])
        eligible = False
        reason: str | None
        if not has_ai:
            reason = "no_ai_evidence"
        else:
            has_ai_rows += 1
            if (source and target and source > target) or (
                evidence and target and evidence > target
            ):
                reason = "future_backfill"
                future_backfill_rows += 1
            elif source is None:
                reason = "missing_source_date"
            elif evidence is None:
                reason = "missing_evidence_time"
            elif source != target or evidence != target:
                reason = "evidence_date_mismatch"
            else:
                eligible = True
                eligible_rows += 1
                reason = None
        updates.append(
            (
                int(eligible),
                reason,
                str(row["strategy_id"]),
                str(row["recommend_date"]),
                str(row["stock_code"]),
            )
        )
    conn.executemany(
        """
        UPDATE recommendation_fact
        SET ai_effectiveness_eligible = ?, ai_exclusion_reason = ?
        WHERE strategy_id = ? AND recommend_date = ? AND stock_code = ?
        """,
        updates,
    )
    conn.commit()
    return {
        "rows_checked": len(rows),
        "has_ai_rows": has_ai_rows,
        "eligible_rows": eligible_rows,
        "future_backfill_rows": future_backfill_rows,
        "excluded_rows": len(rows) - eligible_rows,
    }


def discover_history_files() -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for directory in HISTORY_DIR_CANDIDATES:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.stem not in discovered:
                discovered[path.stem] = path
    return discovered


def load_history_items(path: Path) -> list[dict[str, Any]]:
    try:
        payload = load_json(path)
    except Exception:
        return []
    recommendations = payload.get("recommendations") or []
    items: list[dict[str, Any]] = []
    for row in recommendations:
        code = normalize_stock_code(row.get("code") or row.get("ts_code"))
        if not code:
            continue
        item = {
            "recommend_date": clean_text(row.get("trade_date")) or path.stem,
            "stock_code": code,
            "ts_code": clean_text(row.get("ts_code")) or to_ts_code(code),
            "stock_name": clean_text(row.get("name")),
            "rank_no": parse_int(row.get("rank")),
            "score": parse_float(row.get("score")),
            "recommend_price": parse_float(row.get("price") or row.get("close")),
            "source_kind": "history_json",
            "source_path": str(path),
            "strategy_version": clean_text(row.get("strategy_version")),
            "signal_data_cutoff": clean_text(row.get("signal_data_cutoff")),
            "planned_entry_time": clean_text(row.get("planned_entry_time")),
            "holding_period_days": parse_int(row.get("holding_period_days")),
            "data_sources": ensure_str_list(row.get("data_sources")),
            "used_proxy": parse_bool(row.get("used_proxy")),
            "round_trip_cost": parse_float(row.get("round_trip_cost")),
            "benchmark": clean_text(row.get("benchmark")),
            "rank_change": parse_int(row.get("rank_change")),
            "raw": row,
        }
        items.append(item)
    return items


def load_selection_history_items(path: Path) -> list[dict[str, Any]]:
    try:
        payload = load_json(path)
    except Exception:
        return []
    trade_date = clean_text(payload.get("trade_date")) or path.stem
    items: list[dict[str, Any]] = []
    for row in payload.get("stocks", []):
        ts_code = clean_text(row.get("ts_code"))
        code = normalize_stock_code(ts_code or row.get("code"))
        if not code:
            continue
        items.append(
            {
                "recommend_date": trade_date,
                "stock_code": code,
                "ts_code": ts_code or to_ts_code(code),
                "stock_name": clean_text(row.get("name")),
                "rank_no": parse_int(row.get("rank")),
                "score": parse_float(row.get("score")),
                "recommend_price": parse_float(row.get("price")),
                "source_kind": "selection_history",
                "source_path": str(path),
                "strategy_version": clean_text(row.get("strategy_version")),
                "signal_data_cutoff": clean_text(row.get("signal_data_cutoff")),
                "planned_entry_time": clean_text(row.get("planned_entry_time")),
                "holding_period_days": parse_int(row.get("holding_period_days")),
                "data_sources": ensure_str_list(row.get("data_sources")),
                "used_proxy": parse_bool(row.get("used_proxy")),
                "round_trip_cost": parse_float(row.get("round_trip_cost")),
                "benchmark": clean_text(row.get("benchmark")),
                "rank_change": parse_int(row.get("rank_change")),
                "raw": row,
            }
        )
    return items


def load_current_strategy_items() -> tuple[str | None, list[dict[str, Any]]]:
    if not WORKING_STRATEGY_JSON.exists():
        return None, []
    strategy_doc = load_json(WORKING_STRATEGY_JSON)
    latest_trade_date = clean_text(strategy_doc.get("latest_trade_date"))
    target = find_strategy(strategy_doc, TARGET_STRATEGY) or {}
    items: list[dict[str, Any]] = []
    for row in target.get("top20", []):
        code = normalize_stock_code(row.get("code") or row.get("ts_code"))
        if not code:
            continue
        items.append(
            {
                "recommend_date": latest_trade_date,
                "stock_code": code,
                "ts_code": clean_text(row.get("ts_code")) or to_ts_code(code),
                "stock_name": clean_text(row.get("name")),
                "rank_no": parse_int(row.get("rank")),
                "score": parse_float(row.get("score")),
                "recommend_price": parse_float(row.get("price") or row.get("close")),
                "source_kind": "current_strategy_snapshot",
                "source_path": str(WORKING_STRATEGY_JSON),
                "strategy_version": clean_text(row.get("strategy_version")),
                "signal_data_cutoff": clean_text(row.get("signal_data_cutoff")),
                "planned_entry_time": clean_text(row.get("planned_entry_time")),
                "holding_period_days": parse_int(row.get("holding_period_days")),
                "data_sources": ensure_str_list(row.get("data_sources")),
                "used_proxy": parse_bool(row.get("used_proxy")),
                "round_trip_cost": parse_float(row.get("round_trip_cost")),
                "benchmark": clean_text(row.get("benchmark")),
                "rank_change": parse_int(row.get("rank_change")),
                "raw": row,
            }
        )
    return latest_trade_date, items


def load_current_o2c_items() -> tuple[str | None, list[dict[str, Any]], Path | None]:
    path = next((candidate for candidate in GREENFIELD_TOP20_CANDIDATES if candidate.exists()), None)
    if path is None:
        return None, [], None
    try:
        payload = load_json(path)
    except Exception:
        return None, [], path
    latest_trade_date = clean_text(payload.get("latest_trade_date") or payload.get("trade_date"))
    rows = payload.get("top20") or payload.get("stocks") or []
    if not isinstance(rows, list):
        return latest_trade_date, [], path
    items: list[dict[str, Any]] = []
    for idx, row in enumerate(rows[:20], start=1):
        if not isinstance(row, dict):
            continue
        code = normalize_stock_code(row.get("code") or row.get("ts_code"))
        if not code:
            continue
        items.append(
            {
                "strategy_id": O2C_STRATEGY_ID,
                "strategy_source": "o2c_factor",
                "strategy_name": O2C_STRATEGY_NAME,
                "recommend_date": latest_trade_date,
                "stock_code": code,
                "ts_code": clean_text(row.get("ts_code")) or to_ts_code(code),
                "stock_name": clean_text(row.get("name") or row.get("stock_name")),
                "rank_no": parse_int(row.get("rank") or row.get("rank_no")) or idx,
                "score": parse_float(row.get("score")),
                "recommend_price": parse_float(row.get("price") or row.get("close")),
                "source_kind": "greenfield_o2c_snapshot",
                "source_path": str(path),
                "strategy_version": clean_text(row.get("strategy_version")),
                "signal_data_cutoff": clean_text(row.get("signal_data_cutoff")),
                "planned_entry_time": clean_text(row.get("planned_entry_time")),
                "holding_period_days": parse_int(row.get("holding_period_days")),
                "data_sources": ensure_str_list(row.get("data_sources")),
                "used_proxy": parse_bool(row.get("used_proxy")),
                "round_trip_cost": parse_float(row.get("round_trip_cost")),
                "benchmark": clean_text(row.get("benchmark")),
                "rank_change": parse_int(row.get("rank_change")),
                "raw": row,
            }
        )
    return latest_trade_date, items, path


def persist_current_snapshot_as_selection_history(trade_date: str | None, items: list[dict[str, Any]]) -> Path | None:
    if not trade_date or not items:
        return None
    path = SELECTION_HISTORY_DIR / f"{trade_date}.json"
    if path.exists():
        return path
    payload = {
        "trade_date": trade_date,
        "timestamp": now_str(),
        "stocks": [],
    }
    for item in sorted(items, key=lambda row: (row.get("rank_no") is None, row.get("rank_no") or 999, row.get("stock_code") or "")):
        payload["stocks"].append(
            {
                "ts_code": item.get("ts_code"),
                "name": item.get("stock_name"),
                "rank": item.get("rank_no"),
                "score": item.get("score"),
                "price": item.get("recommend_price"),
                "strategy_version": item.get("strategy_version"),
                "signal_data_cutoff": item.get("signal_data_cutoff"),
                "planned_entry_time": item.get("planned_entry_time"),
                "holding_period_days": item.get("holding_period_days"),
                "data_sources": item.get("data_sources"),
                "used_proxy": item.get("used_proxy"),
                "round_trip_cost": item.get("round_trip_cost"),
                "benchmark": item.get("benchmark"),
                "rank_change": item.get("rank_change"),
            }
        )
    write_json(path, payload)
    return path


def merge_item(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "raw":
            raw = dict(merged.get("raw") or {})
            raw.update(value or {})
            merged["raw"] = raw
            continue
        if value not in (None, "", [], {}):
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value
            elif key in {"source_kind", "source_path"}:
                merged[key] = value
    return merged


def snapshot_key(item: dict[str, Any]) -> str:
    strategy_id = item.get("strategy_id") or TARGET_STRATEGY
    return f"{strategy_id}:{item.get('stock_code') or ''}"


def collect_daily_recommendations() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    snapshots: dict[str, dict[str, dict[str, Any]]] = {}
    notes = {
        "history_file_dates": [],
        "selection_history_dates": [],
        "current_snapshot_date": None,
    }

    for trade_date, path in discover_history_files().items():
        items = load_history_items(path)
        if not items:
            continue
        notes["history_file_dates"].append(trade_date)
        day_map = snapshots.setdefault(trade_date, {})
        for item in items:
            key = snapshot_key(item)
            day_map[key] = merge_item(day_map.get(key, {}), item)

    if SELECTION_HISTORY_DIR.exists():
        for path in sorted(SELECTION_HISTORY_DIR.glob("*.json")):
            items = load_selection_history_items(path)
            if not items:
                continue
            notes["selection_history_dates"].append(path.stem)
            day_map = snapshots.setdefault(path.stem, {})
            for item in items:
                key = snapshot_key(item)
                day_map[key] = merge_item(day_map.get(key, {}), item)

    current_date, current_items = load_current_strategy_items()
    if current_date and current_items:
        notes["current_snapshot_date"] = current_date
        # Persist current snapshot to selection_history so future syncs retain it.
        # This is the key guard against data loss when strategy_backtests.json is overwritten.
        persisted = persist_current_snapshot_as_selection_history(current_date, current_items)
        if persisted:
            notes.setdefault("persisted_snapshot", current_date)
        day_map = snapshots.setdefault(current_date, {})
        for item in current_items:
            key = snapshot_key(item)
            day_map[key] = merge_item(day_map.get(key, {}), item)

    o2c_date, o2c_items, o2c_path = load_current_o2c_items()
    if o2c_date and o2c_items:
        notes["o2c_snapshot_date"] = o2c_date
        notes["o2c_snapshot_path"] = str(o2c_path) if o2c_path else None
        day_map = snapshots.setdefault(o2c_date, {})
        for item in o2c_items:
            key = snapshot_key(item)
            day_map[key] = merge_item(day_map.get(key, {}), item)

    normalized = {
        trade_date: sorted(day_map.values(), key=lambda row: (row.get("rank_no") is None, row.get("rank_no") or 999, row.get("stock_code")))
        for trade_date, day_map in snapshots.items()
    }
    return normalized, notes


def looks_like_index_entry(row: dict[str, Any]) -> bool:
    name = str(row.get("name") or row.get("stock_name") or "").strip()
    code = str(row.get("code") or "").strip()
    index_markers = ["指数", "沪深", "中证", "创业板", "科创", "A50", "金龙"]
    if any(marker in name for marker in index_markers):
        return True
    return code.startswith("sh") or code.startswith("sz")


def _normalized_ai_date(value: Any) -> str | None:
    text = str(value or "").strip().replace("-", "")
    date = text[:8]
    return date if len(date) == 8 and date.isdigit() else None


def _ai_record_is_same_day_evidence(row: dict[str, Any], recommendation_date: str) -> bool:
    """Require explicit source and creation dates; a dated filename is not evidence."""
    target = _normalized_ai_date(recommendation_date)
    source_date = _normalized_ai_date(
        row.get("ai_source_date") or row.get("ai_analysis_date") or row.get("trade_date")
    )
    evidence_time = row.get("ai_evidence_time") or row.get("generated_at") or row.get("created_at")
    evidence_date = _normalized_ai_date(evidence_time)
    return bool(target and source_date == target and evidence_date == target)


def load_same_day_ai_maps() -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    same_day: dict[str, dict[str, dict[str, Any]]] = {}
    latest_by_code: dict[str, dict[str, Any]] = {}

    if not AI_ANALYSIS_DIR.exists():
        return same_day, latest_by_code

    for path in sorted(AI_ANALYSIS_DIR.glob("*.json")):
        if path.suffix != ".json" or not path.stem.isdigit():
            continue
        trade_date = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, list):
            continue
        day_map = same_day.setdefault(trade_date, {})
        for row in payload:
            if not isinstance(row, dict) or not _ai_record_is_same_day_evidence(row, trade_date):
                continue
            if looks_like_index_entry(row):
                continue
            code = normalize_stock_code(row.get("code"))
            if not code:
                continue
            strategy_source = clean_text(row.get("strategy_source")) or TRADITIONAL_STRATEGY_SOURCE
            mapped = {
                "recommend_reason": trim_line(row.get("buy_reason")),
                "ai_view": clean_text(row.get("operation_advice")),
                "ai_decision": clean_text(row.get("decision_type")),
                "ai_score": parse_float(row.get("sentiment_score")),
                "ai_confidence": clean_text(row.get("confidence_level")),
                "ai_summary": trim_line(row.get("analysis_summary"), limit=600),
                "sector_name": clean_text(row.get("sector_position") or row.get("hot_topics")),
                "ai_source_date": trade_date,
                "ai_evidence_time": clean_text(
                    row.get("ai_evidence_time") or row.get("generated_at") or row.get("created_at")
                ),
                "ai_risk_tags": ensure_str_list(row.get("ai_risk_tags") or row.get("ai_risks")),
            }
            ai_key = f"{strategy_source}:{code}"
            day_map[ai_key] = mapped
            day_map.setdefault(code, mapped)
            latest = latest_by_code.get(ai_key) or latest_by_code.get(code)
            if latest is None or trade_date >= str(latest.get("ai_source_date") or ""):
                latest_by_code[ai_key] = mapped
                latest_by_code.setdefault(code, mapped)
    return same_day, latest_by_code


def load_industry_map() -> dict[str, dict[str, Any]]:
    latest_file = None
    if INDUSTRY_RAW_DIR.exists():
        files = sorted(INDUSTRY_RAW_DIR.glob("industry_*.parquet"))
        if files:
            latest_file = files[-1]
    if latest_file is None:
        return {}

    df = pd.read_parquet(latest_file)
    mapping: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        ts_code = clean_text(row.get("ts_code"))
        code = normalize_stock_code(ts_code)
        if not code:
            continue
        mapping[code] = {
            "ts_code": ts_code,
            "stock_name": clean_text(row.get("name")),
            "sector_name": clean_text(row.get("industry")),
            "market_board": clean_text(row.get("market")),
        }
    return mapping


def build_recommend_reason(item: dict[str, Any]) -> str | None:
    if clean_text(item.get("recommend_reason")):
        return clean_text(item.get("recommend_reason"))
    raw = item.get("raw") or {}
    for key in ("buy_reason", "ai_points", "ai_summary", "ai_conclusion"):
        reason = trim_line(raw.get(key), limit=240)
        if reason:
            return reason
    factor_scores = raw.get("factor_scores") or raw.get("sub_scores") or {}
    if isinstance(factor_scores, dict) and factor_scores:
        top = sorted(
            ((str(name), parse_float(score) or 0.0) for name, score in factor_scores.items()),
            key=lambda pair: pair[1],
            reverse=True,
        )[:3]
        labels = [name for name, _ in top if name]
        if labels:
            return "量化因子靠前：" + "、".join(labels)
    return None


def enrich_item(
    item: dict[str, Any],
    industry_map: dict[str, dict[str, Any]],
    same_day_ai: dict[str, dict[str, dict[str, Any]]],
    latest_ai: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    enriched = dict(item)
    raw = item.get("raw") or {}
    code = item["stock_code"]
    trade_date = item["recommend_date"]
    strategy_source = item.get("strategy_source") or TRADITIONAL_STRATEGY_SOURCE

    industry = industry_map.get(code, {})
    for key in ("ts_code", "stock_name", "sector_name", "market_board"):
        if enriched.get(key) in (None, "", [], {}):
            enriched[key] = industry.get(key)

    embedded_is_same_day = _ai_record_is_same_day_evidence(raw, trade_date)
    embedded_ai = {
        "ai_view": clean_text(raw.get("ai_advice")) if embedded_is_same_day else None,
        "ai_decision": clean_text(raw.get("ai_decision")) if embedded_is_same_day else None,
        "ai_score": parse_float(raw.get("ai_score")) if embedded_is_same_day else None,
        "ai_confidence": clean_text(raw.get("ai_confidence")) if embedded_is_same_day else None,
        "ai_summary": (
            trim_line(raw.get("ai_summary") or raw.get("ai_conclusion"), limit=600)
            if embedded_is_same_day
            else None
        ),
        "sector_name": clean_text(raw.get("industry_name")),
        "ai_source_date": trade_date if embedded_is_same_day else None,
        "ai_evidence_time": (
            clean_text(raw.get("ai_evidence_time") or raw.get("generated_at") or raw.get("created_at"))
            if embedded_is_same_day
            else None
        ),
        "ai_risk_tags": (
            ensure_str_list(raw.get("ai_risk_tags") or raw.get("ai_risks"))
            if embedded_is_same_day
            else []
        ),
    }
    for key, value in embedded_ai.items():
        if enriched.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            enriched[key] = value

    ai_lookup_key = f"{strategy_source}:{code}"
    day_ai = (same_day_ai.get(trade_date) or {}).get(ai_lookup_key) or (same_day_ai.get(trade_date) or {}).get(code)
    if day_ai and _ai_record_is_same_day_evidence(day_ai, trade_date):
        for key, value in day_ai.items():
            if enriched.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                enriched[key] = value
        enriched["ai_source_stale"] = 0

    enriched["recommend_reason"] = build_recommend_reason(enriched)
    if enriched.get("ai_source_stale") is None:
        enriched["ai_source_stale"] = 0
    return enriched


def available_trade_dates(*, up_to: str | None = None) -> list[str]:
    dates: list[str] = []
    for path in sorted(BACKTEST_CACHE_DIR.glob("daily_*.parquet")):
        stem = path.stem.replace("daily_", "")
        if stem.isdigit() and (not up_to or stem <= up_to):
            dates.append(stem)
    return dates


def load_stk_factor_qfq_map(trade_date: str) -> dict[str, dict[str, Any]]:
    path = BACKTEST_CACHE_DIR / f"stk_factor_{trade_date}.parquet"
    if not path.exists():
        return {}
    try:
        df = pd.read_parquet(path, columns=["ts_code", "open_qfq", "high_qfq", "low_qfq", "close_qfq", "pre_close_qfq"])
    except Exception:
        return {}
    mapping: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = normalize_stock_code(row.get("ts_code"))
        if not code:
            continue
        mapping[code] = {
            "open_qfq": parse_float(row.get("open_qfq")),
            "high_qfq": parse_float(row.get("high_qfq")),
            "low_qfq": parse_float(row.get("low_qfq")),
            "close_qfq": parse_float(row.get("close_qfq")),
            "pre_close_qfq": parse_float(row.get("pre_close_qfq")),
        }
    return mapping


def ensure_price_cache(conn: sqlite3.Connection, dates: list[str]) -> dict[str, int]:
    stats = {
        "dates_requested": len(dates),
        "dates_with_daily": 0,
        "dates_with_qfq": 0,
        "rows_upserted": 0,
    }
    if not dates:
        return stats
    now = now_str()
    for trade_date in dates:
        path = BACKTEST_CACHE_DIR / f"daily_{trade_date}.parquet"
        if not path.exists():
            continue
        stats["dates_with_daily"] += 1
        df = pd.read_parquet(path)
        qfq_map = load_stk_factor_qfq_map(trade_date)
        if qfq_map:
            stats["dates_with_qfq"] += 1
        existing_rows = {
            str(row["stock_code"]): row
            for row in conn.execute(
                """
                SELECT stock_code, open, high, low, close, open_qfq, high_qfq, low_qfq, close_qfq, pre_close_qfq
                FROM price_daily_cache
                WHERE trade_date = ?
                """,
                (trade_date,),
            ).fetchall()
        }
        rows = []
        for _, row in df.iterrows():
            ts_code = clean_text(row.get("ts_code"))
            code = normalize_stock_code(ts_code)
            if not code:
                continue
            qfq = qfq_map.get(code) or {}
            existing = existing_rows.get(code)
            existing_complete = bool(
                existing
                and existing["open"] is not None
                and existing["close"] is not None
                and existing["open_qfq"] is not None
                and existing["close_qfq"] is not None
                and existing["pre_close_qfq"] is not None
            )
            qfq_complete = bool(
                qfq.get("open_qfq") is not None
                and qfq.get("close_qfq") is not None
                and qfq.get("pre_close_qfq") is not None
            )
            if existing_complete and qfq_complete:
                continue
            rows.append(
                (
                    code,
                    ts_code,
                    trade_date,
                    parse_float(row.get("open")),
                    parse_float(row.get("high")),
                    parse_float(row.get("low")),
                    parse_float(row.get("close")),
                    qfq.get("open_qfq"),
                    qfq.get("high_qfq"),
                    qfq.get("low_qfq"),
                    qfq.get("close_qfq"),
                    qfq.get("pre_close_qfq"),
                    parse_float(row.get("pct_chg")),
                    str(path),
                    now,
                )
            )
        if not rows:
            continue
        conn.executemany(
            """
            INSERT INTO price_daily_cache (
                stock_code, ts_code, trade_date, open, high, low, close,
                open_qfq, high_qfq, low_qfq, close_qfq, pre_close_qfq,
                pct_chg, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                ts_code=excluded.ts_code,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                open_qfq=COALESCE(excluded.open_qfq, price_daily_cache.open_qfq),
                high_qfq=COALESCE(excluded.high_qfq, price_daily_cache.high_qfq),
                low_qfq=COALESCE(excluded.low_qfq, price_daily_cache.low_qfq),
                close_qfq=COALESCE(excluded.close_qfq, price_daily_cache.close_qfq),
                pre_close_qfq=COALESCE(excluded.pre_close_qfq, price_daily_cache.pre_close_qfq),
                pct_chg=excluded.pct_chg,
                source_file=excluded.source_file,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        stats["rows_upserted"] += len(rows)
    conn.commit()
    return stats


def lookup_close(conn: sqlite3.Connection, stock_code: str, trade_date: str | None) -> float | None:
    if not trade_date:
        return None
    row = conn.execute(
        "SELECT close FROM price_daily_cache WHERE stock_code = ? AND trade_date = ?",
        (stock_code, trade_date),
    ).fetchone()
    return parse_float(row["close"]) if row else None


def lookup_price_row(conn: sqlite3.Connection, stock_code: str, trade_date: str | None) -> sqlite3.Row | None:
    if not trade_date:
        return None
    return conn.execute(
        """
        SELECT open, close, open_qfq, close_qfq, high_qfq, low_qfq, pre_close_qfq
        FROM price_daily_cache
        WHERE stock_code = ? AND trade_date = ?
        """,
        (stock_code, trade_date),
    ).fetchone()


def lookup_qfq_open(conn: sqlite3.Connection, stock_code: str, trade_date: str | None) -> float | None:
    row = lookup_price_row(conn, stock_code, trade_date)
    value = parse_float(row["open_qfq"]) if row else None
    return value if value is not None and value > 0 else None


def lookup_qfq_close(conn: sqlite3.Connection, stock_code: str, trade_date: str | None) -> float | None:
    row = lookup_price_row(conn, stock_code, trade_date)
    value = parse_float(row["close_qfq"]) if row else None
    return value if value is not None and value > 0 else None


def compute_holding_return_pct(entry_open_qfq: float | None, exit_close_qfq: float | None, round_trip_cost: float | None) -> float | None:
    if entry_open_qfq is None or entry_open_qfq <= 0 or exit_close_qfq is None or exit_close_qfq <= 0:
        return None
    gross_pct = (exit_close_qfq / entry_open_qfq - 1.0) * 100.0
    cost_pct = (round_trip_cost or 0.0) * 100.0
    return round(gross_pct - cost_pct, 4)


def sync_stock_dim(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    now = now_str()
    rows = []
    for item in items:
        rows.append(
            (
                item["stock_code"],
                item.get("ts_code"),
                item.get("stock_name"),
                item.get("sector_name"),
                item.get("market_board"),
                item["recommend_date"],
                item["recommend_date"],
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO stock_dim (
            stock_code, ts_code, stock_name, sector_name, market_board, first_seen_date, last_seen_date, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code) DO UPDATE SET
            ts_code=COALESCE(excluded.ts_code, stock_dim.ts_code),
            stock_name=COALESCE(excluded.stock_name, stock_dim.stock_name),
            sector_name=COALESCE(excluded.sector_name, stock_dim.sector_name),
            market_board=COALESCE(excluded.market_board, stock_dim.market_board),
            first_seen_date=MIN(stock_dim.first_seen_date, excluded.first_seen_date),
            last_seen_date=MAX(stock_dim.last_seen_date, excluded.last_seen_date),
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def upsert_recommendations(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    now = now_str()
    exchange_trade_dates = load_open_trade_dates()
    prepared_items = [
        prepare_recommendation_fact_record(
            item,
            strict=item.get("source_kind") in {"current_strategy_snapshot", "greenfield_o2c_snapshot"},
            trade_dates=available_trade_dates(),
            exchange_trade_dates=exchange_trade_dates,
        )
        for item in items
    ]
    rows = []
    for item in prepared_items:
        strategy_id = item.get("strategy_id") or TARGET_STRATEGY
        strategy_source = item.get("strategy_source") or TRADITIONAL_STRATEGY_SOURCE
        strategy_name = item.get("strategy_name") or (
            O2C_STRATEGY_NAME if strategy_id == O2C_STRATEGY_ID else TARGET_STRATEGY_NAME
        )
        rows.append(
            (
                strategy_id,
                strategy_source,
                strategy_name,
                item["recommend_date"],
                item.get("rank_no"),
                item["stock_code"],
                item.get("ts_code"),
                item.get("stock_name"),
                item.get("sector_name"),
                item.get("market_board"),
                item.get("recommend_reason"),
                item.get("ai_view"),
                item.get("ai_decision"),
                item.get("ai_score"),
                item.get("ai_confidence"),
                item.get("ai_summary"),
                item.get("ai_source_date"),
                int(bool(item.get("ai_source_stale"))),
                item.get("ai_evidence_time"),
                json.dumps(item.get("ai_risk_tags") or [], ensure_ascii=False),
                item.get("ai_explanation"),
                int(bool(item.get("ai_effectiveness_eligible"))),
                item.get("ai_exclusion_reason"),
                item.get("recommend_price"),
                item.get("strategy_version"),
                item.get("signal_data_cutoff"),
                item.get("planned_entry_time"),
                item.get("holding_period_days"),
                json.dumps(item.get("data_sources") or [], ensure_ascii=False),
                int(bool(item.get("used_proxy"))),
                item.get("completeness_status") or "pending_settlement",
                item.get("round_trip_cost"),
                item.get("benchmark"),
                item.get("settlement_status") or "pending_settlement",
                item.get("rank_change") if item.get("rank_change") is not None else 0,
                item.get("source_kind"),
                item.get("source_path"),
                now,
                now,
            )
        )
    conn.execute("SAVEPOINT upsert_recommendations")
    try:
        conn.executemany(
            """
            INSERT INTO recommendation_fact (
                strategy_id, strategy_source, strategy_name, recommend_date, rank_no, stock_code, ts_code, stock_name,
                sector_name, market_board, recommend_reason, ai_view, ai_decision, ai_score,
                ai_confidence, ai_summary, ai_source_date, ai_source_stale,
                ai_evidence_time, ai_risk_tags_json, ai_explanation, ai_effectiveness_eligible,
                ai_exclusion_reason, recommend_price,
                strategy_version, signal_data_cutoff, planned_entry_time, holding_period_days, data_sources_json,
                used_proxy, completeness_status, round_trip_cost, benchmark, settlement_status, rank_change,
                source_kind, source_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, recommend_date, stock_code) DO UPDATE SET
                strategy_source=COALESCE(excluded.strategy_source, recommendation_fact.strategy_source),
                strategy_name=COALESCE(excluded.strategy_name, recommendation_fact.strategy_name),
                strategy_version=COALESCE(recommendation_fact.strategy_version, excluded.strategy_version),
                rank_no=COALESCE(excluded.rank_no, recommendation_fact.rank_no),
                ts_code=COALESCE(excluded.ts_code, recommendation_fact.ts_code),
                stock_name=COALESCE(excluded.stock_name, recommendation_fact.stock_name),
                sector_name=COALESCE(excluded.sector_name, recommendation_fact.sector_name),
                market_board=COALESCE(excluded.market_board, recommendation_fact.market_board),
                recommend_reason=COALESCE(excluded.recommend_reason, recommendation_fact.recommend_reason),
                ai_view=COALESCE(excluded.ai_view, recommendation_fact.ai_view),
                ai_decision=COALESCE(excluded.ai_decision, recommendation_fact.ai_decision),
                ai_score=COALESCE(excluded.ai_score, recommendation_fact.ai_score),
                ai_confidence=COALESCE(excluded.ai_confidence, recommendation_fact.ai_confidence),
                ai_summary=COALESCE(excluded.ai_summary, recommendation_fact.ai_summary),
                ai_source_date=COALESCE(excluded.ai_source_date, recommendation_fact.ai_source_date),
                ai_source_stale=CASE
                    WHEN excluded.ai_source_date IS NOT NULL THEN excluded.ai_source_stale
                    ELSE recommendation_fact.ai_source_stale
                END,
                ai_evidence_time=COALESCE(excluded.ai_evidence_time, recommendation_fact.ai_evidence_time),
                ai_risk_tags_json=COALESCE(excluded.ai_risk_tags_json, recommendation_fact.ai_risk_tags_json),
                ai_explanation=COALESCE(excluded.ai_explanation, recommendation_fact.ai_explanation),
                ai_effectiveness_eligible=excluded.ai_effectiveness_eligible,
                ai_exclusion_reason=excluded.ai_exclusion_reason,
                recommend_price=COALESCE(excluded.recommend_price, recommendation_fact.recommend_price),
                signal_data_cutoff=COALESCE(excluded.signal_data_cutoff, recommendation_fact.signal_data_cutoff),
                planned_entry_time=COALESCE(excluded.planned_entry_time, recommendation_fact.planned_entry_time),
                holding_period_days=COALESCE(excluded.holding_period_days, recommendation_fact.holding_period_days),
                data_sources_json=COALESCE(excluded.data_sources_json, recommendation_fact.data_sources_json),
                used_proxy=COALESCE(excluded.used_proxy, recommendation_fact.used_proxy),
                completeness_status=COALESCE(excluded.completeness_status, recommendation_fact.completeness_status),
                round_trip_cost=COALESCE(excluded.round_trip_cost, recommendation_fact.round_trip_cost),
                benchmark=COALESCE(excluded.benchmark, recommendation_fact.benchmark),
                settlement_status=COALESCE(excluded.settlement_status, recommendation_fact.settlement_status),
                rank_change=COALESCE(excluded.rank_change, recommendation_fact.rank_change),
                source_kind=COALESCE(excluded.source_kind, recommendation_fact.source_kind),
                source_path=COALESCE(excluded.source_path, recommendation_fact.source_path),
                updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.execute("RELEASE SAVEPOINT upsert_recommendations")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT upsert_recommendations")
        conn.execute("RELEASE SAVEPOINT upsert_recommendations")
        conn.rollback()
        raise


def recompute_metrics(conn: sqlite3.Connection, trade_dates: list[str]) -> dict[str, Any]:
    if not trade_dates:
        return {"latest_price_date": None, "rows": 0}
    latest_price_date = trade_dates[-1]

    rows = conn.execute(
        """
        SELECT strategy_id, recommend_date, stock_code, recommend_price, round_trip_cost, holding_period_days
        FROM recommendation_fact
        ORDER BY recommend_date, rank_no
        """
    ).fetchall()
    counts = Counter((row["strategy_id"], row["stock_code"]) for row in rows)
    updates = []
    for row in rows:
        recommend_date = row["recommend_date"]
        stock_code = row["stock_code"]
        pos = bisect_right(trade_dates, recommend_date)
        entry_trade_date = trade_dates[pos] if pos < len(trade_dates) else None
        round_trip_cost = parse_float(row["round_trip_cost"]) or DEFAULT_ROUND_TRIP_COST.get(row["strategy_id"], 0.003)
        holding_period_days = parse_int(row["holding_period_days"]) or DEFAULT_HOLDING_PERIOD_DAYS.get(row["strategy_id"], 5)
        entry_open_qfq = lookup_qfq_open(conn, stock_code, entry_trade_date)
        latest_price = lookup_qfq_close(conn, stock_code, latest_price_date)
        next_day_price = lookup_qfq_close(conn, stock_code, entry_trade_date)
        horizon_returns: dict[int, float | None] = {}
        entry_qfq_missing = entry_trade_date is not None and entry_open_qfq is None
        primary_missing_qfq = entry_qfq_missing
        primary_exit_trade_date = None
        for horizon in (1, 3, 5):
            exit_trade_date = None
            if entry_trade_date is not None:
                entry_pos = trade_dates.index(entry_trade_date)
                exit_idx = entry_pos + horizon - 1
                if exit_idx < len(trade_dates):
                    exit_trade_date = trade_dates[exit_idx]
            exit_close_qfq = lookup_qfq_close(conn, stock_code, exit_trade_date)
            if horizon == holding_period_days:
                primary_exit_trade_date = exit_trade_date
                primary_missing_qfq = entry_qfq_missing or (
                    exit_trade_date is not None and exit_close_qfq is None
                )
            horizon_returns[horizon] = compute_holding_return_pct(entry_open_qfq, exit_close_qfq, round_trip_cost)
        next_return = horizon_returns[1]
        cumulative_return = horizon_returns.get(holding_period_days)
        completeness_status, settlement_status = _settlement_state(
            latest_price_date=latest_price_date,
            entry_trade_date=entry_trade_date,
            primary_exit_trade_date=primary_exit_trade_date,
            entry_open_qfq=entry_open_qfq,
            primary_return_pct=cumulative_return,
            missing_qfq=primary_missing_qfq,
        )
        recommend_price = entry_open_qfq
        updates.append(
            (
                entry_trade_date,
                next_day_price,
                next_return,
                latest_price_date,
                latest_price,
                cumulative_return,
                horizon_returns[1],
                horizon_returns[3],
                horizon_returns[5],
                recommend_price,
                completeness_status,
                settlement_status,
                counts.get((row["strategy_id"], stock_code), 0),
                now_str(),
                row["strategy_id"],
                recommend_date,
                stock_code,
            )
        )
    conn.executemany(
        """
        UPDATE recommendation_fact
        SET
            next_trade_date = ?,
            next_day_price = ?,
            next_day_return_pct = ?,
            latest_price_date = ?,
            latest_price = ?,
            cumulative_return_pct = ?,
            forward_return_1d = ?,
            forward_return_3d = ?,
            forward_return_5d = ?,
            recommend_price = ?,
            completeness_status = ?,
            settlement_status = ?,
            cumulative_recommend_count = ?,
            updated_at = ?
        WHERE strategy_id = ? AND recommend_date = ? AND stock_code = ?
        """,
        updates,
    )
    conn.commit()
    return {"latest_price_date": latest_price_date, "rows": len(rows)}


def backfill_forward_returns(conn: sqlite3.Connection | None = None, trade_dates: list[str] | None = None) -> dict[str, Any]:
    own_conn = conn is None
    if own_conn:
        conn = connect_db()
        init_db(conn)
        _migrate_forward_return_columns(conn)
        _migrate_price_cache_qfq_columns(conn)
    if trade_dates is None:
        trade_dates = available_trade_dates()
    result = recompute_metrics(conn, trade_dates)
    filled = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM recommendation_fact
        WHERE strategy_id = ?
          AND (forward_return_1d IS NOT NULL OR forward_return_3d IS NOT NULL OR forward_return_5d IS NOT NULL)
        """,
        (TARGET_STRATEGY,),
    ).fetchone()["c"]
    result = {"total_rows": result.get("rows", 0), "filled_rows": filled}
    if own_conn:
        conn.close()
    return result


def export_csv(conn: sqlite3.Connection) -> Path:
    path = EXPORT_DIR / "prebreakout_recommendations.csv"
    rows = conn.execute(
        """
        SELECT
            recommend_date,
            stock_code,
            stock_name,
            COALESCE(sector_name, '未知') AS sector_name,
            recommend_reason,
            ai_view,
            ai_decision,
            ai_score,
            recommend_price,
            next_trade_date,
            next_day_price,
            latest_price_date,
            latest_price,
            next_day_return_pct,
            cumulative_return_pct,
            cumulative_recommend_count,
            forward_return_1d,
            forward_return_3d,
            forward_return_5d
        FROM recommendation_fact
        WHERE strategy_id = ?
        ORDER BY recommend_date DESC, rank_no ASC
        """,
        (TARGET_STRATEGY,),
    ).fetchall()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "推荐日期",
                "股票代码",
                "股票名称",
                "所属板块",
                "推荐原因",
                "AI分析观点",
                "AI决策",
                "AI评分",
                "推荐当日价",
                "推荐次日日期",
                "推荐次日价",
                "当前价日期",
                "当前价",
                "次日涨幅(%)",
                "累计涨幅(%)",
                "累计推荐次数",
                "1日远期收益(%)",
                "3日远期收益(%)",
                "5日远期收益(%)",
            ]
        )
        for row in rows:
            writer.writerow(list(row))
    return path




def _validate_date_continuity(conn: sqlite3.Connection) -> dict[str, Any]:
    """校验DB最新日期是否与strategy_backtests.json一致，防止历史沉淀断裂."""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT recommend_date FROM recommendation_fact WHERE strategy_id = ? ORDER BY recommend_date DESC LIMIT 10",
        (TARGET_STRATEGY,)
    ).fetchall()]
    if not dates:
        return {"ok": False, "error": "no dates in db", "dates": [], "latest_date": None}

    latest_in_db = dates[0]
    dates_set = set(dates)

    # Check against strategy_backtests.json latest_trade_date
    source_date = None
    stale_vs_source = False
    try:
        if WORKING_STRATEGY_JSON.exists():
            doc = load_json(WORKING_STRATEGY_JSON)
            source_date = clean_text(doc.get("latest_trade_date"))
            if source_date and source_date not in dates_set:
                stale_vs_source = True
    except Exception:
        pass

    # Check for gaps in the last 5 db dates
    sorted_dates = sorted(dates_set)
    gap_warning = None
    if len(sorted_dates) >= 2:
        # simple gap: if consecutive db dates differ by >5 calendar days, flag it
        for i in range(len(sorted_dates) - 1):
            from datetime import datetime
            d1 = datetime.strptime(sorted_dates[i], "%Y%m%d")
            d2 = datetime.strptime(sorted_dates[i + 1], "%Y%m%d")
            delta = (d2 - d1).days
            if delta > 5:  # more than a week gap between consecutive trading dates
                gap_warning = f"gap detected: {sorted_dates[i]} -> {sorted_dates[i+1]} ({delta} days)"
                break

    ok = not stale_vs_source
    return {
        "ok": ok,
        "latest_date": latest_in_db,
        "source_date": source_date,
        "stale_vs_source": stale_vs_source,
        "dates_in_db": len(dates),
        "gap_warning": gap_warning,
        "recent_dates": dates[:5],
    }

def build_summary(
    conn: sqlite3.Connection,
    strategy_id: str = TARGET_STRATEGY,
    strategy_name: str = TARGET_STRATEGY_DISPLAY,
) -> dict[str, Any]:
    metric_semantics = {
        "next_day_return_pct": "1d net return from T+1 open_qfq entry to 1-trading-day close_qfq exit, after round-trip cost.",
        "cumulative_return_pct": "primary-holding-period net return from T+1 open_qfq entry to configured holding-period close_qfq exit, after round-trip cost.",
    }
    total_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM recommendation_fact WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()["c"]
    date_row = conn.execute(
        """
        SELECT MIN(recommend_date) AS first_date, MAX(recommend_date) AS last_date,
               COUNT(DISTINCT recommend_date) AS date_count,
               COUNT(DISTINCT stock_code) AS unique_stock_count
        FROM recommendation_fact
        WHERE strategy_id = ?
        """,
        (strategy_id,),
    ).fetchone()
    raw_latest_date = date_row["last_date"]
    latest_evaluable_row = conn.execute(
        """
        SELECT MAX(recommend_date) AS latest_date
        FROM recommendation_fact
        WHERE strategy_id = ?
          AND next_trade_date IS NOT NULL
          AND next_day_price IS NOT NULL
          AND next_day_return_pct IS NOT NULL
        """,
        (strategy_id,),
    ).fetchone()
    latest_evaluable_date = latest_evaluable_row["latest_date"] if latest_evaluable_row else None
    # latest_recommend_date is a freshness contract and must reflect the newest
    # recommendation snapshot, even before next-day performance is evaluable.
    # Keep latest_evaluable_recommend_date separately for performance consumers.
    latest_date = raw_latest_date
    latest_rows = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM recommendation_fact
        WHERE strategy_id = ? AND recommend_date = ?
        """,
        (strategy_id, latest_date),
    ).fetchone()["c"] if latest_date else 0
    latest_price_row = conn.execute(
        """
        SELECT MAX(latest_price_date) AS latest_price_date
        FROM recommendation_fact
        WHERE strategy_id = ?
        """,
        (strategy_id,),
    ).fetchone()
    ai_view_stats = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                COALESCE(NULLIF(ai_view, ''), '未标注') AS ai_view,
                COUNT(*) AS recommendation_count,
                ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
                ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
                ROUND(AVG(ai_score), 2) AS avg_ai_score,
                ROUND(AVG(CASE WHEN next_day_return_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS hit_rate
            FROM recommendation_fact
            WHERE strategy_id = ? AND recommend_date = ?
              AND ai_effectiveness_eligible = 1
            GROUP BY COALESCE(NULLIF(ai_view, ''), '未标注')
            ORDER BY recommendation_count DESC, avg_cumulative_return_pct DESC
            """,
            (strategy_id, latest_date),
        ).fetchall()
    ]
    sector_stats = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                COALESCE(NULLIF(sector_name, ''), '未知') AS sector_name,
                COUNT(*) AS recommendation_count,
                COUNT(DISTINCT stock_code) AS unique_stock_count,
                ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
                ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
                ROUND(AVG(CASE WHEN ai_effectiveness_eligible = 1 THEN ai_score END), 2) AS avg_ai_score
            FROM recommendation_fact
            WHERE strategy_id = ? AND recommend_date = ?
            GROUP BY COALESCE(NULLIF(sector_name, ''), '未知')
            ORDER BY recommendation_count DESC, avg_cumulative_return_pct DESC
            LIMIT 20
            """,
            (strategy_id, latest_date),
        ).fetchall()
    ]
    top_repeat = [
        dict(row)
        for row in conn.execute(
            """
            SELECT stock_code, stock_name, COUNT(*) AS recommend_count,
                   ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct
            FROM recommendation_fact
            WHERE strategy_id = ?
            GROUP BY stock_code, stock_name
            ORDER BY recommend_count DESC, avg_cumulative_return_pct DESC
            LIMIT 20
            """,
            (strategy_id,),
        ).fetchall()
    ]
    score_bucket = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                CASE
                    WHEN ai_score >= 80 THEN '80-100'
                    WHEN ai_score >= 60 THEN '60-79'
                    WHEN ai_score >= 40 THEN '40-59'
                    WHEN ai_score IS NULL THEN '未评分'
                    ELSE '0-39'
                END AS bucket,
                COUNT(*) AS recommendation_count,
                ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
                ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct
            FROM recommendation_fact
            WHERE strategy_id = ? AND recommend_date = ?
              AND ai_effectiveness_eligible = 1
            GROUP BY bucket
            ORDER BY recommendation_count DESC
            """,
            (strategy_id, latest_date),
        ).fetchall()
    ]
    date_stats = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                recommend_date,
                MAX(next_trade_date) AS next_trade_date,
                COUNT(*) AS sample_count,
                ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
                ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
                ROUND(AVG(CASE WHEN next_day_return_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS next_day_hit_rate_pct
            FROM recommendation_fact
            WHERE strategy_id = ?
              AND next_trade_date IS NOT NULL
              AND next_day_price IS NOT NULL
              AND next_day_return_pct IS NOT NULL
            GROUP BY recommend_date
            ORDER BY recommend_date DESC
            """,
            (strategy_id,),
        ).fetchall()
    ]
    performance = conn.execute(
        """
        SELECT
            ROUND(AVG(next_day_return_pct), 4) AS avg_next_day_return_pct,
            ROUND(AVG(cumulative_return_pct), 4) AS avg_cumulative_return_pct,
            ROUND(AVG(CASE WHEN next_day_return_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS next_day_hit_rate_pct
        FROM recommendation_fact
        WHERE strategy_id = ? AND recommend_date = ?
        """,
        (strategy_id, latest_date),
    ).fetchone()
    latest_sample = [
        dict(row)
        for row in conn.execute(
            """
            SELECT recommend_date, stock_code, stock_name, sector_name, ai_view, ai_score,
                   recommend_price, next_trade_date, next_day_price, next_day_return_pct,
                   latest_price, cumulative_return_pct, cumulative_recommend_count,
                   strategy_version, signal_data_cutoff, planned_entry_time, holding_period_days,
                   data_sources_json, used_proxy, completeness_status, round_trip_cost,
                   benchmark, settlement_status, rank_change
            FROM recommendation_fact
            WHERE strategy_id = ? AND recommend_date = ?
            ORDER BY rank_no ASC
            LIMIT 20
            """,
            (strategy_id, latest_date),
        ).fetchall()
    ]
    return {
        "generated_at": now_str(),
        "strategy_id": strategy_id,
        "strategy_source": "o2c_factor" if strategy_id == O2C_STRATEGY_ID else TRADITIONAL_STRATEGY_SOURCE,
        "strategy_name": strategy_name,
        "db_path": str(DB_PATH),
        "metric_semantics": metric_semantics,
        "total_rows": total_rows,
        "latest_recommend_date": latest_date,
        "latest_raw_recommend_date": raw_latest_date,
        "latest_evaluable_recommend_date": latest_evaluable_date,
        "latest_date_row_count": latest_rows,
        "latest_price_date": latest_price_row["latest_price_date"] if latest_price_row else None,
        "date_range": {
            "from": date_row["first_date"],
            "to": date_row["last_date"],
            "date_count": date_row["date_count"],
        },
        "unique_stock_count": date_row["unique_stock_count"],
        "performance": dict(performance) if performance else {},
        "date_stats": date_stats,
        "ai_view_stats": ai_view_stats,
        "sector_stats": sector_stats,
        "score_bucket_stats": score_bucket,
        "top_repeat_recommendations": top_repeat,
        "latest_sample": latest_sample,
    }


def export_summary_json(
    conn: sqlite3.Connection,
    strategy_id: str = TARGET_STRATEGY,
    filename: str = "prebreakout_summary.json",
    strategy_name: str = TARGET_STRATEGY_DISPLAY,
) -> Path:
    path = EXPORT_DIR / filename
    write_json(path, build_summary(conn, strategy_id=strategy_id, strategy_name=strategy_name))
    return path


def export_detail_json(
    conn: sqlite3.Connection,
    strategy_id: str = TARGET_STRATEGY,
    filename: str = "prebreakout_recommendations.json",
) -> Path:
    path = EXPORT_DIR / filename
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                recommend_date,
                rank_no,
                stock_code,
                ts_code,
                stock_name,
                COALESCE(sector_name, '未知') AS sector_name,
                market_board,
                recommend_reason,
                ai_view,
                ai_decision,
                ai_score,
                ai_confidence,
                ai_summary,
                ai_source_date,
                ai_source_stale,
                ai_evidence_time,
                ai_risk_tags_json,
                ai_explanation,
                ai_effectiveness_eligible,
                ai_exclusion_reason,
                recommend_price,
                next_trade_date,
                next_day_price,
                next_day_return_pct,
                latest_price_date,
                latest_price,
                cumulative_return_pct,
                cumulative_recommend_count,
                strategy_version,
                signal_data_cutoff,
                planned_entry_time,
                holding_period_days,
                data_sources_json,
                used_proxy,
                completeness_status,
                round_trip_cost,
                benchmark,
                settlement_status,
                rank_change
            FROM recommendation_fact
            WHERE strategy_id = ?
            ORDER BY recommend_date DESC, rank_no ASC
            """,
            (strategy_id,),
        ).fetchall()
    ]
    write_json(
        path,
        {
            "generated_at": now_str(),
            "strategy_id": strategy_id,
            "metric_semantics": {
                "next_day_return_pct": "1d net return from T+1 open_qfq entry to 1-trading-day close_qfq exit, after round-trip cost.",
                "cumulative_return_pct": "primary-holding-period net return from T+1 open_qfq entry to configured holding-period close_qfq exit, after round-trip cost.",
            },
            "rows": rows,
        },
    )
    return path


def sync_exports_to_published_repo(
    latest_trade_date: str | None,
    summary_path: Path,
    detail_path: Path,
    csv_path: Path,
    extra_paths: list[Path] | None = None,
) -> dict[str, Any]:
    del latest_trade_date, summary_path, detail_path, csv_path, extra_paths
    return {
        "ok": True,
        "publication_mode": "local_only",
        "raw_data_published": False,
        "published_repo": None,
        "analytics_dir": None,
        "note": "Recommendation database exports remain local; GitHub Pages receives only allowlisted result summaries through the v2 publisher.",
    }


def record_sync_run(
    conn: sqlite3.Connection,
    summary: dict[str, Any],
    csv_path: Path,
    json_path: Path,
    ok: bool,
    notes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO sync_runs (
            run_at, latest_trade_date, latest_price_date, total_rows, latest_date_rows,
            history_dates, exported_csv, exported_json, ok, notes_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_str(),
            summary.get("latest_recommend_date"),
            summary.get("latest_price_date"),
            summary.get("total_rows"),
            summary.get("latest_date_row_count"),
            (summary.get("date_range") or {}).get("date_count"),
            str(csv_path),
            str(json_path),
            int(ok),
            json.dumps(notes, ensure_ascii=False),
        ),
    )
    conn.commit()


def sync_recommendation_warehouse(target_date: str | None = None) -> dict[str, Any]:
    conn = connect_db()
    try:
        init_db(conn)
        _migrate_forward_return_columns(conn)
        _migrate_recommendation_contract_columns(conn)
        _migrate_price_cache_qfq_columns(conn)
        init_db(conn)  # recreate views if migration dropped them
        if target_date:
            snapshots, notes = collect_target_snapshot_with_backoff(target_date)
        else:
            snapshots, notes = collect_daily_recommendations()
        same_day_ai, latest_ai = load_same_day_ai_maps()
        industry_map = load_industry_map()
        trade_dates = available_trade_dates(up_to=target_date)
        price_cache_info = ensure_price_cache(conn, trade_dates)

        target_day_items = snapshots.get(target_date, []) if target_date else []
        target_price_sync = None
        if target_date and target_day_items:
            target_price_sync = backfill_target_date_prices(conn, target_date=target_date, items=target_day_items)
            if target_date not in trade_dates:
                trade_dates.append(target_date)
                trade_dates = sorted(set(trade_dates))

        enriched_items: list[dict[str, Any]] = []
        for trade_date in sorted(snapshots):
            if target_date and trade_date > target_date:
                continue
            for item in snapshots[trade_date]:
                enriched = enrich_item(item, industry_map, same_day_ai, latest_ai)
                if enriched.get("recommend_price") is None:
                    enriched["recommend_price"] = lookup_close(conn, enriched["stock_code"], trade_date)
                enriched_items.append(enriched)

        sync_stock_dim(conn, enriched_items)
        upsert_recommendations(conn, enriched_items)
        ai_integrity = repair_ai_effectiveness_flags(conn)
        metric_info = recompute_metrics(conn, trade_dates)
        summary = build_summary(conn)
        summary["latest_price_date"] = metric_info.get("latest_price_date")
        o2c_summary = build_summary(conn, strategy_id=O2C_STRATEGY_ID, strategy_name=O2C_STRATEGY_DISPLAY)
        o2c_summary["latest_price_date"] = metric_info.get("latest_price_date")

        # 校验最近3个交易日是否连续，防止历史沉淀断裂
        date_check = _validate_date_continuity(conn)
        date_check_notes = {
            "date_continuity_check": date_check,
            "history_dates_check": notes.get("selection_history_dates", [])[-5:] if notes.get("selection_history_dates") else [],
        }
        summary["notes"] = {
            **notes,
            **date_check_notes,
            "same_day_ai_dates": sorted(same_day_ai.keys()),
            "industry_map_size": len(industry_map),
            "trade_dates_loaded": len(trade_dates),
            "price_cache": price_cache_info,
            "ai_integrity": ai_integrity,
        }
        if target_price_sync is not None:
            summary["notes"]["target_price_sync"] = target_price_sync

        csv_path = export_csv(conn)
        detail_path = export_detail_json(conn)
        o2c_detail_path = export_detail_json(conn, strategy_id=O2C_STRATEGY_ID, filename="o2c_factor_recommendations.json")
        o2c_summary_path = export_summary_json(
            conn,
            strategy_id=O2C_STRATEGY_ID,
            filename="o2c_factor_summary.json",
            strategy_name=O2C_STRATEGY_DISPLAY,
        )
        json_path = EXPORT_DIR / "prebreakout_summary.json"
        write_json(json_path, summary)
        if target_date:
            if str(summary.get("latest_recommend_date") or "") != target_date:
                raise TimeoutError(
                    f"recommendation warehouse latest_recommend_date={summary.get('latest_recommend_date')} "
                    f"did not reach target_date={target_date}"
                )
            if str(summary.get("latest_price_date") or "") != target_date:
                raise TimeoutError(
                    f"recommendation warehouse latest_price_date={summary.get('latest_price_date')} "
                    f"did not reach target_date={target_date}"
                )
        publish = sync_exports_to_published_repo(
            summary.get("latest_recommend_date"),
            json_path,
            detail_path,
            csv_path,
            extra_paths=[o2c_summary_path, o2c_detail_path],
        )
        try:
            from strategy_publication_layer import build_publication_layer

            publication_layer = build_publication_layer()
        except Exception as exc:
            publication_layer = {"ok": False, "error": str(exc)}
        ok = (
            bool(summary.get("latest_recommend_date"))
            and int(summary.get("latest_date_row_count") or 0) == 20
            and publish.get("ok", False)
            and date_check.get("ok", False)
            and publication_layer.get("ok", False)
        )
        payload = {
            "generated_at": now_str(),
            "ok": ok,
            "db_path": str(DB_PATH),
            "csv_export": str(csv_path),
            "detail_export": str(detail_path),
            "summary_export": str(json_path),
            "o2c_detail_export": str(o2c_detail_path),
            "o2c_summary_export": str(o2c_summary_path),
            "o2c_latest_recommend_date": o2c_summary.get("latest_recommend_date"),
            "o2c_latest_date_row_count": o2c_summary.get("latest_date_row_count"),
            "latest_recommend_date": summary.get("latest_recommend_date"),
            "latest_date_row_count": summary.get("latest_date_row_count"),
            "total_rows": summary.get("total_rows"),
            "unique_stock_count": summary.get("unique_stock_count"),
            "date_range": summary.get("date_range"),
            "latest_price_date": summary.get("latest_price_date"),
            "notes": summary.get("notes"),
            "published_repo_sync": publish,
            "strategy_publication_layer": publication_layer,
        }
        record_sync_run(conn, summary, csv_path, json_path, ok, payload["notes"])
        write_json(SYNC_REPORT_PATH, payload)
        return payload
    finally:
        conn.close()
