#!/usr/bin/env python3
"""集合竞价数据全量落地(改造方案 B0, 2026-07-02)。

拉取 Tushare stk_auction_o(开盘集合竞价)与 stk_auction_c(收盘集合竞价),
全市场约5500行/日, 按年份分区写 parquet, 断点续拉。

用法:
    python ingest_auction_data.py --start 20220101 --end 20260702
    python ingest_auction_data.py --only open
    python ingest_auction_data.py --only close --start 20250101

输出:
    stock_data/01-root/auction_cache/auction_open_YYYY.parquet
    stock_data/01-root/auction_cache/auction_close_YYYY.parquet
    stock_data/01-root/auction_cache/ingest_checkpoint.json

断点续拉:checkpoint 记录已完成的 trade_date(分 open/close),重跑自动跳过。
空数据(接口对早期日期可能返回空)记录为已完成但不计入失败计数。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
CACHE_DIR = WORKSPACE / "stock_data" / "01-root" / "auction_cache"
CHECKPOINT_PATH = CACHE_DIR / "ingest_checkpoint.json"

SLEEP_BASE = 0.15
BACKOFF_SCHEDULE = (1, 2, 4)
MAX_CONSECUTIVE_FAILURES = 10
PROGRESS_EVERY = 50

KIND_CONFIG = {
    "open": {"api": "stk_auction_o", "prefix": "auction_open"},
    "close": {"api": "stk_auction_c", "prefix": "auction_close"},
}


def get_pro():
    sys.path.insert(0, str(WORKSPACE / "skills" / "stock-analyzer"))
    from pipeline import init_tushare
    return init_tushare()


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("open", [])
    data.setdefault("close", [])
    return data


def save_checkpoint(checkpoint: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(CHECKPOINT_PATH)


def get_trade_dates(pro, start: str, end: str) -> list[str]:
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    cal = cal[cal["is_open"] == 1]
    dates = sorted(cal["cal_date"].tolist())
    return dates


def fetch_with_retry(pro, api_name: str, trade_date: str):
    """调用接口, 遇异常按 1/2/4s 退避重试最多3次。返回 (df_or_None, ok:bool)。"""
    fn = getattr(pro, api_name)
    last_exc = None
    for attempt in range(len(BACKOFF_SCHEDULE) + 1):
        try:
            df = fn(trade_date=trade_date)
            return df, True
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < len(BACKOFF_SCHEDULE):
                time.sleep(BACKOFF_SCHEDULE[attempt])
            else:
                print(f"    [FAIL] {api_name} {trade_date}: {type(e).__name__} {str(e)[:80]}")
    return None, False


def year_file(prefix: str, year: str) -> Path:
    return CACHE_DIR / f"{prefix}_{year}.parquet"


def append_to_year_file(prefix: str, year: str, df: pd.DataFrame) -> None:
    path = year_file(prefix, year)
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df
    combined = combined.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    combined = combined.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, index=False)
    tmp.replace(path)


def run_kind(pro, kind: str, trade_dates: list[str], checkpoint: dict) -> dict:
    cfg = KIND_CONFIG[kind]
    api_name = cfg["api"]
    prefix = cfg["prefix"]
    done_set = set(checkpoint[kind])

    pending = [d for d in trade_dates if d not in done_set]
    print(f"\n=== [{kind}] 待拉取 {len(pending)} / 共 {len(trade_dates)} 交易日 (已完成 {len(done_set)}) ===")

    consecutive_failures = 0
    total_rows = 0
    empty_dates = 0
    processed = 0

    # buffer rows per year to reduce parquet rewrites; flush per year-change or at end
    year_buffers: dict[str, list[pd.DataFrame]] = {}

    def flush_year(year: str):
        if year in year_buffers and year_buffers[year]:
            df = pd.concat(year_buffers[year], ignore_index=True)
            append_to_year_file(prefix, year, df)
            year_buffers[year] = []

    current_year = None
    for trade_date in pending:
        year = trade_date[:4]
        if current_year is not None and year != current_year:
            flush_year(current_year)
        current_year = year

        df, ok = fetch_with_retry(pro, api_name, trade_date)
        if not ok:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                flush_year(current_year)
                save_checkpoint(checkpoint)
                print(f"\n[STOP] 连续失败 {consecutive_failures} 次, 已保存进度并停止。")
                return checkpoint
            time.sleep(SLEEP_BASE)
            continue

        consecutive_failures = 0
        if df is None or len(df) == 0:
            empty_dates += 1
        else:
            year_buffers.setdefault(year, []).append(df)
            total_rows += len(df)

        checkpoint[kind].append(trade_date)
        processed += 1

        if processed % PROGRESS_EVERY == 0:
            flush_year(current_year)
            save_checkpoint(checkpoint)
            print(f"  [{kind}] 进度 {processed}/{len(pending)}  最新={trade_date}  累计行数={total_rows}  空日={empty_dates}")

        time.sleep(SLEEP_BASE)

    if current_year is not None:
        flush_year(current_year)
    save_checkpoint(checkpoint)
    print(f"[{kind}] 完成本轮: 新增交易日 {processed}, 新增行数 {total_rows}, 空数据日 {empty_dates}")
    return checkpoint


def summarize() -> None:
    print("\n=== 汇总统计 ===")
    for kind, cfg in KIND_CONFIG.items():
        prefix = cfg["prefix"]
        files = sorted(CACHE_DIR.glob(f"{prefix}_*.parquet"))
        if not files:
            print(f"[{kind}] 无文件")
            continue
        total_rows = 0
        min_date = None
        max_date = None
        print(f"[{kind}] 文件清单:")
        for f in files:
            df = pd.read_parquet(f, columns=["trade_date"])
            n = len(df)
            total_rows += n
            if n > 0:
                dmin, dmax = df["trade_date"].min(), df["trade_date"].max()
                min_date = dmin if min_date is None else min(min_date, dmin)
                max_date = dmax if max_date is None else max(max_date, dmax)
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"    {f.name}: {n} 行, {size_mb:.2f} MB")
        print(f"[{kind}] 合计行数={total_rows}  日期范围={min_date}..{max_date}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20220101")
    ap.add_argument("--end", default="20260702")
    ap.add_argument("--only", choices=["open", "close"], default=None)
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pro = get_pro()
    trade_dates = get_trade_dates(pro, args.start, args.end)
    print(f"交易日历: {args.start}..{args.end} 共 {len(trade_dates)} 个交易日")

    checkpoint = load_checkpoint()
    kinds = [args.only] if args.only else ["open", "close"]
    for kind in kinds:
        checkpoint = run_kind(pro, kind, trade_dates, checkpoint)

    summarize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
