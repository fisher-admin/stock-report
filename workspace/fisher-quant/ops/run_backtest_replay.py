#!/usr/bin/env python3
"""Honest backtest replay of the PRODUCTION selections (prebreakout_v41).

Replays selection_history snapshots — the selections the live system actually
published (pre-hotfix factors, bank-heavy) — through backtest_v2 with honest
execution (next-open entry, limit-up/suspension skips, round-trip costs) and
writes the full metrics to:

    strategy_research/backtest_v2_prebreakout_<lastdate>.json

Usage:
    python3 ops/run_backtest_replay.py [--selection-dir D] [--out-dir D]
        [--db PATH] [--holding 1,3,5,10] [--total-cost-pct 0.26]
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
from quant.backtest_v2 import (DEFAULT_HOLDING_DAYS, CostModel,
                               run_backtest_with_bank_variant)
from quant.verify import load_selection_snapshots

SCHEMA_VERSION = "backtest_v2.1"
STRATEGY_ID = "prebreakout_v41"
LABEL = "历史实盘选股回放（热修前因子）"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_assumptions(cost: CostModel, holding_days) -> dict:
    return {
        "样本": "selection_history 生产快照 = 系统历史上真实发布的推荐（热修前因子，银行占比偏高），"
              "如实回放，不做任何事后修饰；20260415 快照已被隔离，构成天然缺口。",
        "入场": "选股日次一交易日开盘价买入；最后一个快照日(无次日行情)整批记为 pending_entry。",
        "涨停不可买": "入场日开盘 >= 前收×(1+板块阈值)×0.995 视为无法成交，剔除并计数；"
                   "阈值：主板9.8%、创业板/科创板19.8%、北交所29.8%（ts_code 前缀判定）。",
        "停牌": "入场日无行情记录则放弃买入；持有期内停牌按最近收盘价携带（carry_forward 标记）。",
        "出场": "T+N = 入场日之后第 N 个交易日收盘卖出；超出现有日历的出场记为 pending_exit。",
        "成本": {
            "说明": "整段往返成本，逐笔从毛收益中扣除；日度组合曲线买/卖日各计一半。",
            "构成": "佣金 0.03%×2 + 印花税 0.05%(卖出) + 滑点 0.075%×2（合计默认 0.26%，可配置）",
            "total_cost_pct": round(cost.total_cost_pct, 6),
        },
        "组合口径": "等权日度组合：每个交易日对全部在持仓位的当日收益取均值后复利；"
                  "年化按 252 个交易日。",
        "基线": "全市场等权日均涨跌幅（close-to-close）在组合相同活跃日期上复利；"
              "入场日组合腿为 open->close 而基线为 close-to-close，存在隔夜缺口口径差，如实披露。",
        "已知局限": [
            "未识别 ST 个股的 5% 涨跌幅限制（无 ST 标志数据），主板按 9.8% 处理",
            "价格为后端缓存原值，复权处理依赖上游（持有期 ≤10 交易日，除权影响有限但非零）",
            "20260608 为部分行情日（约4707只）；当日缺行情的个股按停牌规则处理",
        ],
        "holding_days": list(holding_days),
        "数据源": "recommendations.db（只读, mode=ro）+ selection_history/*.json",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selection-dir", default=datahub.selection_history_dir())
    ap.add_argument("--out-dir", default=datahub.strategy_research_dir())
    ap.add_argument("--db", default=datahub.default_db_path())
    ap.add_argument("--holding", default=",".join(map(str, DEFAULT_HOLDING_DAYS)))
    ap.add_argument("--total-cost-pct", type=float, default=None,
                    help="override total round-trip cost in percent")
    args = ap.parse_args()

    t0 = time.time()
    holding_days = sorted({int(x) for x in args.holding.split(",") if x.strip()})
    cost = (CostModel(total_cost_pct_override=args.total_cost_pct)
            if args.total_cost_pct is not None else CostModel())

    snaps = load_selection_snapshots(args.selection_dir)
    snaps = [s for s in snaps if s.get("stocks")]
    if not snaps:
        print(f"no snapshots found in {args.selection_dir}", file=sys.stderr)
        return 1
    selections = [(s["trade_date"], s["stocks"]) for s in snaps]

    hub = datahub.DataHub(args.db)
    calendar = hub.calendar()
    base_map = hub.baseline_map()
    sector_map = hub.sector_map()

    codes = {st["ts_code"] for s in snaps for st in s["stocks"]}
    start = datahub.prev_trade_date(calendar, snaps[0]["trade_date"], 10) or calendar[0]
    book = hub.price_book(codes, start=start)

    variants = run_backtest_with_bank_variant(
        selections, book, calendar, baseline=base_map,
        holding_days=holding_days, cost=cost, sector_map=sector_map,
        label=LABEL)

    last_date = snaps[-1]["trade_date"]
    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": "backtest_replay",
        "strategy_id": STRATEGY_ID,
        "label": LABEL,
        "generated_at": _now_iso(),
        "source": {
            "selection_dir": args.selection_dir,
            "db_path": args.db,
            "n_snapshot_dates": len(snaps),
            "snapshot_date_range": [snaps[0]["trade_date"], last_date],
        },
        "assumptions": build_assumptions(cost, holding_days),
        "variants": variants,
    }

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"backtest_v2_prebreakout_{last_date}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # ---- console report ----
    def _fmt(v, suffix="%"):
        return "--" if v is None else f"{v:+.2f}{suffix}"

    for key, title in (("all", "全部选股"), ("ex_banks", "剔除银行")):
        res = variants[key]
        print(f"\n[{title}] dates={res['n_selection_dates']} "
              f"entries={res['n_selected_entries']} "
              f"skips: limit_up={res['skips']['limit_up']} "
              f"suspended={res['skips']['suspended']} "
              f"pending_entry={res['skips']['pending_entry']}")
        print(f"  {'period':>6} {'trades':>6} {'win%':>7} {'avg/trade':>10} "
              f"{'total':>9} {'annual':>10} {'maxDD':>8} {'mkt_total':>10} {'excess':>9}")
        for n in holding_days:
            b = res["per_holding_period"][f"T+{n}"]
            p = b["portfolio"]
            print(f"  {'T+' + str(n):>6} {b['n_trades']:>6} "
                  f"{_fmt(b['win_rate_pct'], ''):>7} {_fmt(b['avg_return_per_trade_pct']):>10} "
                  f"{_fmt(p['total_return_pct']):>9} {_fmt(p['annualized_return_pct']):>10} "
                  f"{_fmt(p['max_drawdown_pct']):>8} "
                  f"{_fmt(p.get('baseline_total_return_pct')):>10} "
                  f"{_fmt(p.get('excess_total_return_pct')):>9}")
    print(f"\nbanks removed: {variants['n_bank_entries_removed']} entries "
          f"({len(variants['bank_names_removed'])} distinct names)")
    print(f"wrote {out_path} in {time.time() - t0:.1f}s")
    hub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
