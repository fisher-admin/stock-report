#!/usr/bin/env python3
"""Synthetic-data unit tests for fisher-quant (plain asserts, no framework).

Run:  python3 tests/test_backtest_v2.py
Covers: calendar helpers, board thresholds, limit-up skip, suspension skip,
cost subtraction, holding-period exits, carry-forward, equal-weight daily
portfolio compounding, baseline excess math, verify forward-return math,
bank filtering. All expectations are hand-computed from synthetic prices.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from quant.datahub import (PriceBook, is_bank, limit_up_threshold,
                           next_trade_date, normalize_ts_code,
                           prev_trade_date)
from quant.backtest_v2 import (CostModel, build_trades, equity_metrics,
                               normalize_selections, run_backtest,
                               split_bank_variant)
from quant.verify import (baseline_window_pct, summarize_results,
                          verify_snapshot)

# Synthetic 8-day trading calendar (opaque sorted strings to the engine).
CAL = [f"202501{i:02d}" for i in range(1, 9)]
FLAT_BASELINE = {d: 1.0 for d in CAL}  # market +1.0% every day


def approx(a, b, tol=1e-9):
    assert a is not None and b is not None, f"None in approx({a}, {b})"
    assert abs(a - b) <= tol * (1.0 + abs(b)), f"{a} != {b} (tol={tol})"


def frame(rows):
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "open", "close"])


# Stock M: the well-behaved main-board reference series.
M = "600100.SH"
M_ROWS = [
    (M, CAL[0], 10.0, 10.0),
    (M, CAL[1], 10.2, 10.5),   # entry day for a CAL[0] selection
    (M, CAL[2], 10.6, 10.8),   # T+1 exit
    (M, CAL[3], 10.7, 10.4),
    (M, CAL[4], 10.5, 11.0),   # T+3 exit
    (M, CAL[5], 11.1, 11.2),
    (M, CAL[6], 11.0, 10.9),   # T+5 exit
    (M, CAL[7], 11.0, 11.5),
]


def test_calendar_helpers():
    assert next_trade_date(CAL, CAL[0], 1) == CAL[1]
    assert next_trade_date(CAL, CAL[0], 3) == CAL[3]
    assert next_trade_date(CAL, "20250100", 1) == CAL[0]   # non-trade input
    assert next_trade_date(CAL, CAL[-1], 1) is None        # beyond calendar
    assert prev_trade_date(CAL, CAL[3], 1) == CAL[2]
    assert prev_trade_date(CAL, CAL[0], 1) is None
    assert normalize_ts_code("600000") == "600000.SH"
    assert normalize_ts_code("000001") == "000001.SZ"
    assert normalize_ts_code("920662") == "920662.BJ"
    assert normalize_ts_code("300750.SZ") == "300750.SZ"


def test_board_thresholds():
    approx(limit_up_threshold("600519.SH"), 0.098)   # main board SH
    approx(limit_up_threshold("000001.SZ"), 0.098)   # main board SZ
    approx(limit_up_threshold("300750.SZ"), 0.198)   # ChiNext
    approx(limit_up_threshold("301269.SZ"), 0.198)   # ChiNext new range
    approx(limit_up_threshold("688981.SH"), 0.198)   # STAR
    approx(limit_up_threshold("920662.BJ"), 0.298)   # BSE 92x
    approx(limit_up_threshold("830799.BJ"), 0.298)   # BSE 8xx
    approx(limit_up_threshold("430047.BJ"), 0.298)   # BSE 43x


def test_cost_model_composition():
    # default composition: 0.03*2 + 0.05 + 0.075*2 = 0.26 round-trip
    approx(CostModel().total_cost_pct, 0.26)
    approx(CostModel(total_cost_pct_override=0.5).total_cost_pct, 0.5)


def test_holding_period_exits():
    res = run_backtest([(CAL[0], [M])], frame(M_ROWS), CAL,
                       holding_days=(1, 3, 5, 10), cost=0.0)
    per = res["per_holding_period"]
    # entry at NEXT day open (10.2), exits at close of Nth day after entry
    assert per["T+1"]["best_trade"]["entry_date"] == CAL[1]
    assert per["T+1"]["best_trade"]["exit_date"] == CAL[2]
    assert per["T+3"]["best_trade"]["exit_date"] == CAL[4]
    assert per["T+5"]["best_trade"]["exit_date"] == CAL[6]
    approx(per["T+1"]["avg_return_per_trade_pct"], (10.8 / 10.2 - 1) * 100)
    approx(per["T+3"]["avg_return_per_trade_pct"], (11.0 / 10.2 - 1) * 100)
    approx(per["T+5"]["avg_return_per_trade_pct"], (10.9 / 10.2 - 1) * 100)
    # T+10 exit is beyond the calendar -> pending, not silently dropped
    assert per["T+10"]["n_trades"] == 0
    assert per["T+10"]["n_pending_exit"] == 1


def test_costs_subtract_correctly():
    gross = (10.8 / 10.2 - 1) * 100
    r0 = run_backtest([(CAL[0], [M])], frame(M_ROWS), CAL, holding_days=(1,), cost=0.0)
    r26 = run_backtest([(CAL[0], [M])], frame(M_ROWS), CAL, holding_days=(1,))  # default 0.26
    r100 = run_backtest([(CAL[0], [M])], frame(M_ROWS), CAL, holding_days=(1,), cost=1.0)
    approx(r0["per_holding_period"]["T+1"]["avg_return_per_trade_pct"], gross)
    approx(r26["per_holding_period"]["T+1"]["avg_return_per_trade_pct"], gross - 0.26)
    approx(r100["per_holding_period"]["T+1"]["avg_return_per_trade_pct"], gross - 1.0)
    approx(r26["per_holding_period"]["T+1"]["avg_gross_return_per_trade_pct"], gross)


def test_limit_up_skip():
    # main board: limit gate = 10 * 1.098 * 0.995 = 10.9251
    lu, ok, cn = "600200.SH", "600300.SH", "300100.SZ"
    rows = [(lu, CAL[0], 10.0, 10.0), (lu, CAL[1], 10.9252, 11.0),
            (lu, CAL[2], 11.0, 11.1),
            (ok, CAL[0], 10.0, 10.0), (ok, CAL[1], 10.9250, 11.0),
            (ok, CAL[2], 11.0, 11.1),
            # ChiNext gate = 10 * 1.198 * 0.995 = 11.9201: +15% open is fine
            (cn, CAL[0], 10.0, 10.0), (cn, CAL[1], 11.5, 11.6),
            (cn, CAL[2], 11.7, 11.8)]
    res = run_backtest([(CAL[0], [lu, ok, cn])], frame(rows), CAL, holding_days=(1,))
    assert res["skips"]["limit_up"] == 1
    assert res["skips"]["examples"]["limit_up"][0]["ts_code"] == lu
    assert res["per_holding_period"]["T+1"]["n_trades"] == 2
    assert res["per_holding_period"]["T+1"]["n_skipped_limit_up"] == 1


def test_suspended_skip():
    s = "600400.SH"
    rows = [(s, CAL[0], 10.0, 10.0),  # no CAL[1] row -> suspended on entry day
            (s, CAL[2], 10.5, 10.6), (s, CAL[3], 10.6, 10.7)]
    res = run_backtest([(CAL[0], [s])], frame(rows), CAL, holding_days=(1,))
    assert res["skips"]["suspended"] == 1
    assert res["per_holding_period"]["T+1"]["n_trades"] == 0
    assert res["per_holding_period"]["T+1"]["n_skipped_suspended"] == 1


def test_carry_forward_exit():
    cf = "600500.SH"
    rows = [(cf, CAL[0], 10.0, 10.0), (cf, CAL[1], 10.0, 10.0),
            (cf, CAL[2], 10.4, 10.5),  # CAL[3] missing (suspended at exit)
            (cf, CAL[4], 11.0, 11.0)]
    trades, _ = build_trades(normalize_selections([(CAL[0], [cf])]),
                             PriceBook(frame(rows)), CAL, CostModel(0, 0, 0),
                             holding_days=(2,))
    ex = trades[0]["exits"][2]
    assert ex["exit_date"] == CAL[3]          # exit date is the calendar date
    assert ex["carry_forward"] is True        # ...but priced at last close
    approx(ex["exit_close"], 10.5)
    approx(ex["gross_pct"], (10.5 / 10.0 - 1) * 100)


def test_daily_portfolio_compounding_and_annualization():
    res = run_backtest([(CAL[0], [M])], frame(M_ROWS), CAL,
                       baseline=FLAT_BASELINE, holding_days=(1,))
    p = res["per_holding_period"]["T+1"]["portfolio"]
    half = 0.26 / 2 / 100
    r1 = 10.5 / 10.2 - 1 - half          # entry day: open->close minus半成本
    r2 = 10.8 / 10.5 - 1 - half          # exit day: close->close minus半成本
    total = (1 + r1) * (1 + r2) - 1
    assert p["n_days"] == 2
    approx(p["total_return_pct"], total * 100, 1e-9)
    approx(p["annualized_return_pct"],
           ((1 + total) ** (252 / 2) - 1) * 100, 1e-9)
    # baseline over the same 2 active days at +1%/day, and the excess math
    base_total = (1.01 ** 2 - 1) * 100
    approx(p["baseline_total_return_pct"], base_total, 1e-9)
    approx(p["excess_total_return_pct"], total * 100 - base_total, 1e-9)
    # per-trade baseline window = entry..exit (2 days at +1%)
    per = res["per_holding_period"]["T+1"]
    approx(per["avg_baseline_per_trade_pct"], base_total, 1e-9)
    approx(per["avg_excess_per_trade_pct"],
           per["avg_return_per_trade_pct"] - base_total, 1e-9)


def test_equity_metrics_drawdown():
    m = equity_metrics([0.10, -0.20, 0.05])
    approx(m["total_return_pct"], (1.1 * 0.8 * 1.05 - 1) * 100)
    approx(m["max_drawdown_pct"], -20.0)   # peak 1.1 -> trough 0.88


def test_pending_entry_on_last_date():
    res = run_backtest([(CAL[-1], [M])], frame(M_ROWS), CAL, holding_days=(1,))
    assert res["skips"]["pending_entry"] == 1
    assert res["per_holding_period"]["T+1"]["n_trades"] == 0


def test_verify_forward_returns():
    x, y, z, w = "600601.SH", "600602.SH", "600603.SH", "600604.SH"
    rows = [(x, CAL[0], 10.0, 10.0), (x, CAL[1], 10.4, 10.5),
            (x, CAL[2], 10.4, 10.3), (x, CAL[3], 10.8, 11.0),
            (y, CAL[0], 20.0, 20.0), (y, CAL[1], 19.2, 19.0),
            (y, CAL[2], 19.0, 19.4), (y, CAL[3], 19.5, 19.6),
            # z: suspended after CAL[1] -> t3 carries CAL[1] close
            (z, CAL[0], 10.0, 10.0), (z, CAL[1], 10.3, 10.4),
            # w: no base close on snapshot date at all
            (w, CAL[1], 5.0, 5.0)]
    snap = {"trade_date": CAL[0],
            "stocks": [{"ts_code": c, "name": c, "rank": i + 1, "score": None}
                       for i, c in enumerate([x, y, z, w])]}
    res = verify_snapshot(snap, PriceBook(frame(rows)), CAL, FLAT_BASELINE,
                          horizons=(1, 3))
    by = {r["ts_code"]: r for r in res["stocks"]}
    approx(by[x]["t1_pct"], 5.0)                       # 10.5/10-1
    approx(by[x]["t3_pct"], 10.0)                      # 11.0/10-1
    approx(by[y]["t1_pct"], -5.0)
    approx(by[z]["t3_pct"], 4.0)                       # carried 10.4/10-1
    assert "t3_carry_forward" in by[z]["flags"]
    assert by[w]["t1_pct"] is None and "no_base_close" in by[w]["flags"]
    assert res["n_no_base_close"] == 1
    t1 = res["stats"]["t1"]
    assert t1["n"] == 3                                # x, y, z (w excluded)
    approx(t1["avg_pct"], (5.0 - 5.0 + 4.0) / 3)
    approx(t1["win_rate_pct"], 2 / 3 * 100)
    approx(t1["baseline_pct"], 1.0)                    # one day at +1%
    approx(t1["excess_pct"], t1["avg_pct"] - 1.0)
    t3 = res["stats"]["t3"]
    approx(t3["baseline_pct"], (1.01 ** 3 - 1) * 100)  # compounded window
    assert t3["best"]["ts_code"] == x and t3["worst"]["ts_code"] == y


def test_verify_pending_horizon():
    rows = [(M, CAL[6], 10.0, 10.0), (M, CAL[7], 10.0, 10.2)]
    snap = {"trade_date": CAL[6], "stocks": [{"ts_code": M, "name": "m", "rank": 1}]}
    res = verify_snapshot(snap, PriceBook(frame(rows)), CAL, FLAT_BASELINE,
                          horizons=(1, 3))
    r = res["stocks"][0]
    approx(r["t1_pct"], 2.0)
    assert r["t3_pct"] is None                 # beyond calendar -> pending
    assert res["stats"]["t3"]["n"] == 0
    assert baseline_window_pct(FLAT_BASELINE, CAL, CAL[6], 3) is None


def test_summary_aggregation():
    rows = M_ROWS
    book = PriceBook(frame(rows))
    snaps = [{"trade_date": CAL[0], "stocks": [{"ts_code": M, "name": "m", "rank": 1}]},
             {"trade_date": CAL[1], "stocks": [{"ts_code": M, "name": "m", "rank": 1}]}]
    results = [verify_snapshot(s, book, CAL, FLAT_BASELINE, (1,)) for s in snaps]
    summary = summarize_results(results, (1,))
    assert summary["n_dates"] == 2
    assert summary["overall"]["t1"]["n_observations"] == 2
    exp = ((10.5 / 10.0 - 1) * 100 + (10.8 / 10.5 - 1) * 100) / 2
    approx(summary["overall"]["t1"]["avg_pct"], exp)
    assert len(summary["per_date"]) == 2


def test_bank_filter():
    sels = [(CAL[0], [{"ts_code": "601128.SH", "name": "常熟银行"},
                      {"ts_code": "002958.SZ", "name": "青农商行"},
                      {"ts_code": M, "name": "正常股"}])]
    smap = {"601128.SH": {"name": "常熟银行", "sector": "银行"}}
    filtered, n_removed, removed = split_bank_variant(sels, smap)
    assert n_removed == 2                      # sector hit + name fallback hit
    assert [it["ts_code"] for it in filtered[0][1]] == [M]
    assert is_bank(name="苏农银行") and is_bank(name="青农商行")
    # 张家港行: name carries no hint -> relies on the stock_dim sector path
    assert not is_bank(name="张家港行") and is_bank(name="张家港行", sector="银行")
    assert not is_bank(name="工商时报", sector="传媒")


def test_normalize_selections_dedup():
    sels = normalize_selections([(CAL[0], [M, M, {"ts_code": "000001.SZ"}])])
    assert len(sels[0][1]) == 2


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} tests passed")
    sys.exit(1 if failed else 0)
