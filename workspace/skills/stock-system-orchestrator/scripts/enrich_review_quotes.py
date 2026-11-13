#!/usr/bin/env python3
"""复盘行情补全（2026-06-28 #2/优化④）：用 akshare 逐股补齐历史选股缺失的开/收盘价，消除幸存者偏差。

背景：本地行情库每天只存当日 tracked 的少量股，历史选股的入场(T+1)/出场行情常缺 → 复盘可结算样本偏少且
有幸存者偏差（只有还在被追踪的股能结算）。本脚本对三策略 recommendation_snapshot 里出现过的全部个股，
用 akshare(新浪/东财) 拉其复盘窗口内的真实日线开/收，写入本地缓存 review_quote_cache.db；
generate_o2c_review.load_quote_union 会把缓存并入行情并集 → 有效复盘样本从 ~10-18 天扩到 20-30+ 天。

诚实化：只补真实后验行情；缓存只读不可变地参与复盘；增量(已缓存窗口跳过)、失败隔离。
用法：python3 enrich_review_quotes.py [--days-buffer 12] [--max-workers 3]（慢，建议后台跑；可重复，断点续）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
CACHE_DB = WORKSPACE / "stock_data" / "03-working" / "review_quote_cache.db"
_SR = WORKSPACE / "stock_data" / "03-working" / "strategy_research"
STRAT_DBS = {
    "greenfield_o2c_v1": _SR / "greenfield_o2c_v1" / "greenfield_o2c_v1.db",
    "prebreakout_v41": _SR / "prebreakout_v41" / "prebreakout_v41.db",
    "t1_factor_v1": _SR / "t1_factor_v1" / "t1_factor_v1.db",
}


def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quote_cache ("
        " code TEXT NOT NULL, date TEXT NOT NULL, open REAL, close REAL,"
        " source TEXT, fetched_at TEXT, UNIQUE(code, date))"
    )
    conn.commit()


def collect_codes_window() -> tuple[list[str], str, str]:
    codes: set[str] = set()
    rdates: set[str] = set()
    for sid, db in STRAT_DBS.items():
        if not db.exists():
            continue
        c = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            for code, rd in c.execute("SELECT DISTINCT stock_code, recommend_date FROM recommendation_snapshot WHERE strategy_id=?", (sid,)):
                import re
                m = re.search(r"(\d{6})", str(code))
                if m:
                    codes.add(m.group(1))
                if rd:
                    rdates.add(str(rd))
        except sqlite3.Error:
            pass
        c.close()
    lo = min(rdates) if rdates else "20260101"
    hi = max(rdates) if rdates else time.strftime("%Y%m%d")
    return sorted(codes), lo, hi


def fetch_one(code: str, start: str, end: str) -> list[tuple]:
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
    rows = []
    for _, r in df.iterrows():
        d = str(r.get("日期")).replace("-", "")[:8]
        op, cl = r.get("开盘"), r.get("收盘")
        if d and op is not None and cl is not None:
            rows.append((code, d, float(op), float(cl)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-buffer", type=int, default=12, help="窗口尾部多留几日(覆盖多日横盘出场)")
    ap.add_argument("--max-workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help=">0 仅处理前 N 只(调试)")
    ap.add_argument("--force", action="store_true", help="忽略缓存,全部重拉")
    args = ap.parse_args()

    codes, lo, hi = collect_codes_window()
    if args.limit > 0:
        codes = codes[: args.limit]
    # 窗口尾部按自然日多留 buffer*~1.6（交易日→自然日）
    end_dt = time.strftime("%Y%m%d", time.localtime(time.mktime(time.strptime(hi, "%Y%m%d")) + args.days_buffer * 86400 * 1.6))
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    ensure_cache(conn)
    done = set()
    if not args.force:
        # 已缓存且覆盖到窗口尾部的 code 跳过（粗判：该 code 缓存里最大日期 >= hi）。
        for code, mx in conn.execute("SELECT code, MAX(date) FROM quote_cache GROUP BY code"):
            if mx and str(mx) >= hi:
                done.add(str(code))
    todo = [c for c in codes if c not in done]
    print(f"[enrich] codes={len(codes)} window={lo}..{end_dt} cached={len(done)} todo={len(todo)} workers={args.max_workers}")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    ok = fail = added = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futs = {ex.submit(fetch_one, c, lo, end_dt): c for c in todo}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                rows = fut.result()
            except Exception as exc:  # noqa: BLE001
                fail += 1
                if fail <= 5:
                    print(f"  [FAIL] {code}: {type(exc).__name__} {str(exc)[:60]}")
                continue
            for (cc, d, op, cl) in rows:
                conn.execute("INSERT INTO quote_cache(code,date,open,close,source,fetched_at) VALUES(?,?,?,?,?,?) "
                             "ON CONFLICT(code,date) DO UPDATE SET open=excluded.open, close=excluded.close, fetched_at=excluded.fetched_at",
                             (cc, d, op, cl, "akshare_hist", now))
                added += 1
            conn.commit()
            ok += 1
            if ok % 25 == 0:
                print(f"  ...{ok}/{len(todo)} done (+{added} rows)")
    total = conn.execute("SELECT COUNT(*) FROM quote_cache").fetchone()[0]
    conn.close()
    print(f"[enrich] done: ok={ok} fail={fail} rows_added={added} cache_total={total} path={CACHE_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
