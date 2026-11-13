#!/usr/bin/env python3
"""Backfill T+1/T+3/T+5 verification for ALL selection_history snapshots.

Revives the verification loop (dead since 20260325) by recomputing realized
forward returns for every snapshot date against the gold price warehouse,
and writing:
    verify_history_v2/{date}.json        per-date detail
    verify_history_v2/summary_latest.json rolling summary

Usage:
    python3 ops/run_verify_backfill.py [--selection-dir D] [--out-dir D]
                                       [--db PATH] [--horizons 1,3,5]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PKG_ROOT)

from quant import datahub
from quant.verify import (SCHEMA_VERSION, DEFAULT_HORIZONS,
                          load_selection_snapshots, summarize_results,
                          verify_snapshot)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dump(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def build_assumptions(horizons) -> dict:
    return {
        "收益口径": "快照日收盘 -> 快照日后第N个交易日收盘（close-to-close，不含任何交易成本），"
                  "衡量信号质量；诚实可执行口径见 backtest_v2 回放。",
        "基线": "全市场等权日均涨跌幅（price_daily_cache 全表按日 AVG(pct_chg)）在同窗口复利，"
              "口径与个股 close-to-close 完全可比。",
        "超额": "当日组合平均收益 - 同窗口市场基线。",
        "停牌处理": "窗口终点停牌时向前携带最近可得收盘价并打 carry_forward 标记；"
                 "快照日无收盘价的个股不计入统计（no_base_close）。",
        "未到期": "窗口超出现有行情日历的观测记为 null（pending），不计入统计。",
        "日历": "price_daily_cache DISTINCT trade_date；20260608 为部分行情日（约4707只），按可得数据计算。",
        "horizons": list(horizons),
        "数据源": "recommendations.db（只读, mode=ro）+ selection_history/*.json",
        "选股口径说明": "selection_history 为生产环境真实推荐快照（热修前因子，银行占比偏高），如实回放。",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selection-dir", default=datahub.selection_history_dir())
    ap.add_argument("--out-dir", default=datahub.verify_history_v2_dir())
    ap.add_argument("--db", default=datahub.default_db_path())
    ap.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    horizons = sorted({int(x) for x in args.horizons.split(",") if x.strip()})

    snaps = load_selection_snapshots(args.selection_dir)
    if not snaps:
        print(f"no snapshots found in {args.selection_dir}", file=sys.stderr)
        return 1

    hub = datahub.DataHub(args.db)
    calendar = hub.calendar()
    base_map = hub.baseline_map()

    codes = {s["ts_code"] for snap in snaps for s in snap["stocks"]}
    first = snaps[0]["trade_date"]
    start = datahub.prev_trade_date(calendar, first, 10) or calendar[0]
    book = hub.price_book(codes, start=start)

    os.makedirs(args.out_dir, exist_ok=True)
    assumptions = build_assumptions(horizons)

    results = []
    for snap in snaps:
        res = verify_snapshot(snap, book, calendar, base_map, horizons)
        res["assumptions"] = assumptions
        _dump(os.path.join(args.out_dir, f"{res['trade_date']}.json"), res)
        results.append(res)
        if not args.quiet:
            s1 = res["stats"].get(f"t{horizons[0]}") or {}
            avg = s1.get("avg_pct")
            exc = s1.get("excess_pct")
            print(f"  {res['trade_date']}  n={res['n_stocks']:>2}  "
                  f"T+{horizons[0]} avg={'--' if avg is None else f'{avg:+.2f}%':>8}  "
                  f"excess={'--' if exc is None else f'{exc:+.2f}%':>8}")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "verify_backfill_summary",
        "label": "selection_history T+1/T+3/T+5 验证回填（历史实盘选股，热修前因子）",
        "generated_at": _now_iso(),
        "selection_dir": args.selection_dir,
        "db_path": args.db,
        "assumptions": assumptions,
        **summarize_results(results, horizons),
    }
    summary_path = os.path.join(args.out_dir, "summary_latest.json")
    _dump(summary_path, summary)

    ov = summary["overall"]
    print(f"\nbackfilled {summary['n_dates']} dates "
          f"({summary['date_range'][0]} -> {summary['date_range'][1]}) "
          f"in {time.time() - t0:.1f}s -> {summary_path}")
    for h in horizons:
        o = ov[f"t{h}"]
        print(f"  T+{h}: n_obs={o['n_observations']}  avg={o['avg_pct']:+.3f}%  "
              f"win={o['win_rate_pct']:.1f}%  "
              f"avg_excess_vs_mkt={o['avg_excess_vs_market_pct']:+.3f}%  "
              f"beat_mkt_dates={o['share_dates_beating_market_pct']:.1f}%")
    hub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
