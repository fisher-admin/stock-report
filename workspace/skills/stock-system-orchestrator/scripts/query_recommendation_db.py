#!/usr/bin/env python3
"""Query helper for the recommendation warehouse."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

from recommendation_warehouse import DB_PATH, TARGET_STRATEGY, build_summary, connect_db


def render_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "(no rows)"
    headers = rows[0].keys()
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row[header] if row[header] is not None else "")))
    lines = []
    lines.append(" | ".join(header.ljust(widths[header]) for header in headers))
    lines.append("-+-".join("-" * widths[header] for header in headers))
    for row in rows:
        lines.append(" | ".join(str(row[header] if row[header] is not None else "").ljust(widths[header]) for header in headers))
    return "\n".join(lines)


def query_rows(conn: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    if args.stats == "summary":
        summary = build_summary(conn)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return []

    if args.stats == "sector":
        return conn.execute("SELECT * FROM vw_prebreakout_sector_stats LIMIT ?", (args.limit,)).fetchall()
    if args.stats == "ai_view":
        return conn.execute("SELECT * FROM vw_prebreakout_ai_view_stats LIMIT ?", (args.limit,)).fetchall()

    sql = """
        SELECT
            recommend_date,
            stock_code,
            stock_name,
            COALESCE(sector_name, '未知') AS sector_name,
            ai_view,
            ai_decision,
            ai_score,
            recommend_price,
            next_day_price,
            latest_price,
            cumulative_return_pct,
            cumulative_recommend_count
        FROM recommendation_fact
        WHERE strategy_id = ?
    """
    params: list[object] = [TARGET_STRATEGY]
    if args.date:
        sql += " AND recommend_date = ?"
        params.append(args.date)
    if args.code:
        sql += " AND stock_code = ?"
        params.append(args.code)
    if args.sector:
        sql += " AND COALESCE(sector_name, '') LIKE ?"
        params.append(f"%{args.sector}%")
    if args.ai_view:
        sql += " AND COALESCE(ai_view, '') LIKE ?"
        params.append(f"%{args.ai_view}%")
    sql += " ORDER BY recommend_date DESC, rank_no ASC LIMIT ?"
    params.append(args.limit)
    return conn.execute(sql, params).fetchall()


def output_rows(rows: list[sqlite3.Row], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
        return
    if output_format == "csv":
        writer = csv.writer(sys.stdout)
        if rows:
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow([row[key] for key in row.keys()])
        return
    print(render_table(rows))


def main() -> int:
    parser = argparse.ArgumentParser(description="Query Fisher recommendation warehouse")
    parser.add_argument("--date", help="Filter by recommend date, e.g. 20260310")
    parser.add_argument("--code", help="Filter by stock code, e.g. 600519")
    parser.add_argument("--sector", help="Filter by sector/industry keyword")
    parser.add_argument("--ai-view", help="Filter by AI recommendation view")
    parser.add_argument("--stats", choices=["summary", "sector", "ai_view"], help="Show aggregate stats")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        print(f"recommendation db missing: {DB_PATH}", file=sys.stderr)
        return 1

    conn = connect_db()
    try:
        rows = query_rows(conn, args)
        if rows:
            output_rows(rows, args.format)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
