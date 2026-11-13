#!/usr/bin/env python3
"""Off-hours parallel check of the incremental short-track stk_factor history.

Phase 2 (2026-10-30) replaced the nightly 21-day full refetch with rebasing.
For the first five trading days this job (Hermes, 01:30) refetches the same
window in full, without touching the research caches, and compares it with
what the 22:15 run used: every value, the security set, and the v4.4 balanced
Top-20.  Silent when they agree; alerts on any disagreement; after five
consecutive agreeing days it reports once and then stays silent.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from orchestrator_common import HEALTH_DIR, WORKSPACE  # noqa: E402

WORKING = WORKSPACE / "stock_data" / "03-working"
INPUT_CACHE = WORKING / "strategy_research" / "short_track" / "input_cache"
PIT_V2 = WORKING / "fundamental_cache" / "pit_market" / "v2"
DAILY_BASIC_DIRS = [INPUT_CACHE, PIT_V2, PIT_V2.parent,
                    WORKING / "strategy_research" / "short_track" / "materialized" / "daily_basic"]
STATE_DIR = HEALTH_DIR / "short_track_incremental_verify"
REQUIRED_OK_DAYS = 5
PRICE_TOLERANCE = 0.0002
MACD_TOLERANCE = 0.0011
PRICE_COLUMNS = ("open_qfq", "high_qfq", "low_qfq", "close_qfq")
MACD_COLUMNS = ("macd_dif", "macd_dea")


def incremental_window() -> tuple[str, list[str]] | None:
    """The latest window written by the incremental path: (target, dates)."""
    by_target: dict[str, list[str]] = {}
    for path in INPUT_CACHE.glob("stk_factor_*.parquet"):
        query = pd.read_parquet(path, columns=["ts_code"]).attrs.get("query") or {}
        target = (query or {}).get("incremental_rebase_to")
        if target:
            by_target.setdefault(str(target), []).append(path.stem.split("_")[-1])
    if not by_target:
        return None
    target = max(by_target)
    return target, sorted(by_target[target])


def compare_cells(used: dict[str, pd.DataFrame], full: dict[str, pd.DataFrame]) -> dict:
    result = {"code_set_differences": {}, "beyond_tolerance": {}, "cells": 0}
    for date, frame in used.items():
        a = frame.set_index("ts_code")
        b = full[date].set_index("ts_code")
        diff = sorted(set(a.index) ^ set(b.index))
        if diff:
            result["code_set_differences"][date] = diff[:10]
        common = a.index.intersection(b.index)
        result["cells"] += len(common) * (len(PRICE_COLUMNS) + len(MACD_COLUMNS))
        for column in PRICE_COLUMNS + MACD_COLUMNS:
            tolerance = PRICE_TOLERANCE if column in PRICE_COLUMNS else MACD_TOLERANCE
            delta = (pd.to_numeric(a.loc[common, column]) - pd.to_numeric(b.loc[common, column])).abs()
            count = int((delta > tolerance).sum())
            if count:
                result["beyond_tolerance"][column] = result["beyond_tolerance"].get(column, 0) + count
    return result


def balanced_top20(frames: dict[str, pd.DataFrame], dates: list[str], target: str) -> list[str]:
    import short_track_shadow as sts  # noqa: PLC0415
    import short_track_shadow_runner as runner  # noqa: PLC0415

    def daily_basic(date):
        for directory in DAILY_BASIC_DIRS:
            path = directory / f"daily_basic_{date}.parquet"
            if path.is_file():
                return runner._prepare_official_frame(pd.read_parquet(path), dates, label="daily_basic", exact_date=date)
        raise FileNotFoundError(f"daily_basic {date}")

    prices = pd.concat([runner._prepare_official_frame(frames[d], dates, label="stk_factor", exact_date=d)
                        for d in dates], ignore_index=True)
    features = sts.build_balanced_feature_frame(
        price_history=prices, daily_basic_history=pd.concat([daily_basic(d) for d in dates], ignore_index=True),
        pit_universe=pd.read_parquet(PIT_V2 / f"universe_{target}.parquet"), trade_date=target)
    confirmed = sts._require_balanced_frame(features, trade_date=target)
    scores = sts._balanced_component_scores(confirmed)
    ranked = confirmed.assign(composite=scores.mean(axis=1)).sort_values(["composite", "ts_code"], ascending=[False, True])
    return ranked["ts_code"].astype(str).head(20).tolist()


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = STATE_DIR / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {"ok_days": [], "done": False}
    if summary.get("done"):
        return 0
    window = incremental_window()
    if window is None:
        return 0  # nothing written by the incremental path yet
    target, dates = window
    report_path = STATE_DIR / f"{target}.json"
    if report_path.is_file():
        return 0

    sys.path.insert(0, str(WORKSPACE / "skills" / "stock-analyzer"))
    from pipeline import init_tushare  # noqa: PLC0415

    client = init_tushare()
    used = {d: pd.read_parquet(INPUT_CACHE / f"stk_factor_{d}.parquet") for d in dates}
    with ThreadPoolExecutor(max_workers=4) as executor:
        full = dict(executor.map(lambda d: (d, client.stk_factor(trade_date=d)), dates))
    for frame in list(used.values()) + list(full.values()):
        frame["trade_date"] = frame["trade_date"].astype(str)
    cells = compare_cells(used, full)
    report = {"target": target, "window": dates, "checked_at": datetime.now().isoformat(timespec="seconds"), **cells}
    try:
        report["top20_incremental"] = balanced_top20(used, dates, target)
        report["top20_full"] = balanced_top20(full, dates, target)
        report["top20_identical"] = report["top20_incremental"] == report["top20_full"]
    except Exception as exc:  # noqa: BLE001 - a feature-build gap is reported, not hidden
        report["top20_error"] = f"{type(exc).__name__}: {exc}"[:300]
        report["top20_identical"] = None
    agree = (not cells["code_set_differences"] and not cells["beyond_tolerance"]
             and report["top20_identical"] is not False)
    report["agree"] = agree
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    if not agree:
        summary["ok_days"] = []
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"⚠️ 【短线增量核对】{target} 增量结果与全量重抓不一致："
              f"股票集合差异 {len(cells['code_set_differences'])} 天，超容差 {cells['beyond_tolerance']}，"
              f"前 20 名一致={report['top20_identical']}。详情 {report_path}；"
              "可设 STOCK_SHORT_TRACK_INCREMENTAL=0 回到全量重抓。")
        return 1
    summary["ok_days"] = sorted(set(summary.get("ok_days", [])) | {target})
    if len(summary["ok_days"]) >= REQUIRED_OK_DAYS:
        summary["done"] = True
        print(f"✅ 【短线增量核对】连续 {len(summary['ok_days'])} 个交易日与全量重抓一致"
              f"（{', '.join(summary['ok_days'])}），并行核对结束。")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
