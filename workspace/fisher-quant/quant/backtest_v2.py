"""Honest backtest engine v2 — pure functions over plain data structures.

Execution model (the "honest" rules):
- A selection made on day T is entered at the NEXT trading day's OPEN, never
  at T's close (which is unobtainable in live trading).
- Entry is SKIPPED when the entry-day open is at/near limit-up vs the previous
  close (untradeable in practice):
      open >= prev_close * (1 + board_threshold) * LIMIT_UP_SAFETY
- Entry is SKIPPED when the stock has no price row on the entry date
  (suspension / partial data day).
- Holding period T+N exits at the CLOSE of the Nth trading day AFTER the
  entry day (entry day itself is day 0). N >= 1 also respects A-share T+1.
- Round-trip costs are subtracted from every trade's gross return; in the
  daily portfolio series half the cost is charged on the entry day and half
  on the exit day.
- If the exit-day price row is missing (suspended at exit), the last
  available close on-or-before the exit date is carried forward and the
  trade is flagged `carry_forward` — disclosed, not hidden.

NO hardcoded result numbers anywhere: every metric is computed from inputs.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from quant.datahub import (PriceBook, is_bank, limit_up_threshold,
                           next_trade_date, stock_code_of)

TRADING_DAYS_PER_YEAR = 252
LIMIT_UP_SAFETY = 0.995
DEFAULT_HOLDING_DAYS = (1, 3, 5, 10)
_SKIP_EXAMPLE_CAP = 20


# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------

@dataclass
class CostModel:
    """Round-trip friction in PERCENT units.

    Default composition: commission 0.03% x 2 sides + stamp duty 0.05% (sell
    only) + slippage 0.075% x 2 sides = 0.26% round-trip total. Pass
    `total_cost_pct_override` (or a bare float to the engine) to change the
    total directly.
    """

    commission_pct_per_side: float = 0.03
    stamp_duty_sell_pct: float = 0.05
    slippage_pct_per_side: float = 0.075
    total_cost_pct_override: Optional[float] = None

    @property
    def total_cost_pct(self) -> float:
        if self.total_cost_pct_override is not None:
            return float(self.total_cost_pct_override)
        return (self.commission_pct_per_side * 2
                + self.stamp_duty_sell_pct
                + self.slippage_pct_per_side * 2)

    def as_dict(self) -> dict:
        return {
            "commission_pct_per_side": self.commission_pct_per_side,
            "stamp_duty_sell_pct": self.stamp_duty_sell_pct,
            "slippage_pct_per_side": self.slippage_pct_per_side,
            "total_cost_pct_override": self.total_cost_pct_override,
            "total_cost_pct": round(self.total_cost_pct, 6),
        }


def _coerce_cost(cost: Union[None, float, int, CostModel]) -> CostModel:
    if cost is None:
        return CostModel()
    if isinstance(cost, CostModel):
        return cost
    return CostModel(total_cost_pct_override=float(cost))


# --------------------------------------------------------------------------
# Input normalization
# --------------------------------------------------------------------------

def normalize_selections(selections: Iterable) -> List[Tuple[str, List[dict]]]:
    """Normalize to [(sel_date, [{'ts_code':..., 'name':...}, ...]), ...].

    Accepts items as plain ts_code strings or dicts with ts_code/name keys.
    Sorted by date; duplicate codes within one date are dropped.
    """
    out: List[Tuple[str, List[dict]]] = []
    for sel_date, items in selections:
        seen = set()
        norm: List[dict] = []
        for it in items or []:
            if isinstance(it, dict):
                code = it.get("ts_code") or it.get("code")
                name = it.get("name") or it.get("stock_name")
            else:
                code, name = it, None
            if not code:
                continue
            code = str(code).strip().upper()
            if code in seen:
                continue
            seen.add(code)
            norm.append({"ts_code": code, "name": name})
        out.append((str(sel_date), norm))
    out.sort(key=lambda x: x[0])
    return out


def _coerce_baseline(baseline) -> Dict[str, float]:
    """Accept dict / DataFrame / None -> {trade_date: pct}."""
    if baseline is None:
        return {}
    if isinstance(baseline, dict):
        return {str(k): float(v) for k, v in baseline.items() if v is not None}
    df = baseline
    col = "mkt_pct_chg" if "mkt_pct_chg" in df.columns else df.columns[1]
    return {str(d): float(v) for d, v in zip(df["trade_date"], df[col])
            if v is not None and not (isinstance(v, float) and math.isnan(v))}


# --------------------------------------------------------------------------
# Trade construction
# --------------------------------------------------------------------------

def build_trades(selections, book: PriceBook, calendar: List[str],
                 cost: CostModel, holding_days: Sequence[int],
                 threshold_fn=limit_up_threshold) -> Tuple[List[dict], dict]:
    """Replay selections under honest execution rules.

    Returns (trades, skips). Each trade dict:
      {sel_date, ts_code, name, entry_date, entry_open, prev_close,
       exits: {N: {exit_date, exit_close, gross_pct, net_pct, carry_forward}
               or None when still pending}}
    """
    holding_days = sorted(set(int(n) for n in holding_days))
    skips = {"limit_up": 0, "suspended": 0, "no_prev_close": 0,
             "no_price_data": 0, "bad_open": 0, "pending_entry": 0}
    examples: Dict[str, List[dict]] = {k: [] for k in skips}

    def _skip(reason: str, sel_date: str, code: str, **extra) -> None:
        skips[reason] += 1
        if len(examples[reason]) < _SKIP_EXAMPLE_CAP:
            examples[reason].append({"sel_date": sel_date, "ts_code": code, **extra})

    trades: List[dict] = []
    for sel_date, items in selections:
        entry_date = next_trade_date(calendar, sel_date, 1)
        for it in items:
            code = it["ts_code"]
            if entry_date is None:
                _skip("pending_entry", sel_date, code)
                continue
            if not book.has(code):
                _skip("no_price_data", sel_date, code)
                continue
            row = book.row_on(code, entry_date)
            if row is None:
                _skip("suspended", sel_date, code, entry_date=entry_date)
                continue
            entry_open, entry_close = row
            if not (entry_open and entry_open > 0) or math.isnan(entry_open):
                _skip("bad_open", sel_date, code, entry_date=entry_date)
                continue
            prev = book.close_before(code, entry_date)
            if prev is None:
                _skip("no_prev_close", sel_date, code, entry_date=entry_date)
                continue
            prev_close = prev[1]
            threshold = threshold_fn(code)
            limit_price = prev_close * (1.0 + threshold) * LIMIT_UP_SAFETY
            if entry_open >= limit_price:
                _skip("limit_up", sel_date, code, entry_date=entry_date,
                      open_gap_pct=round((entry_open / prev_close - 1) * 100, 2))
                continue

            exits: Dict[int, Optional[dict]] = {}
            for n in holding_days:
                exit_date = next_trade_date(calendar, entry_date, n)
                if exit_date is None:
                    exits[n] = None  # beyond available calendar -> pending
                    continue
                exit_row = book.row_on(code, exit_date)
                if exit_row is not None:
                    exit_close, carry = exit_row[1], False
                else:
                    cb = book.close_on_or_before(code, exit_date)
                    # entry row exists, so cb is at worst the entry-day close
                    exit_close, carry = cb[1], True
                gross = (exit_close / entry_open - 1.0) * 100.0
                exits[n] = {
                    "exit_date": exit_date,
                    "exit_close": exit_close,
                    "gross_pct": gross,
                    "net_pct": gross - cost.total_cost_pct,
                    "carry_forward": carry,
                }
            trades.append({
                "sel_date": sel_date, "ts_code": code, "name": it.get("name"),
                "entry_date": entry_date, "entry_open": entry_open,
                "prev_close": prev_close, "exits": exits,
            })
    skips["examples"] = examples
    return trades, skips


# --------------------------------------------------------------------------
# Equal-weight daily portfolio
# --------------------------------------------------------------------------

def _position_daily_returns(trade: dict, n: int, book: PriceBook,
                            calendar: List[str], cost: CostModel):
    """Yield (date, daily_return_fraction) for one position, holding T+N.

    Entry day: open -> close, charged half the round-trip cost.
    Middle days: close-to-close; missing rows carry the last close (0%).
    Exit day: charged the other half of the round-trip cost.
    """
    ex = trade["exits"].get(n)
    if ex is None:
        return
    half_cost = cost.total_cost_pct / 2.0 / 100.0
    i = bisect.bisect_left(calendar, trade["entry_date"])
    j = bisect.bisect_left(calendar, ex["exit_date"])
    days = calendar[i:j + 1]
    prev_close = None
    for k, d in enumerate(days):
        row = book.row_on(trade["ts_code"], d)
        if k == 0:
            close = row[1]  # entry row exists by construction
            r = close / trade["entry_open"] - 1.0 - half_cost
        else:
            close = row[1] if row is not None else prev_close
            r = close / prev_close - 1.0
            if k == len(days) - 1:
                r -= half_cost
        prev_close = close
        yield d, r


def equity_metrics(returns: Sequence[float]) -> dict:
    """Total/annualized return and max drawdown from a daily return series."""
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in returns:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    total = equity - 1.0
    n = len(returns)
    annualized = None
    if n > 0 and (1.0 + total) > 0:
        annualized = (1.0 + total) ** (TRADING_DAYS_PER_YEAR / n) - 1.0
    return {
        "n_days": n,
        "total_return_pct": total * 100.0,
        "annualized_return_pct": None if annualized is None else annualized * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
    }


def _compound_pct(values: Iterable[float]) -> float:
    """Compound a series of PERCENT returns -> total percent."""
    eq = 1.0
    for v in values:
        eq *= (1.0 + v / 100.0)
    return (eq - 1.0) * 100.0


def _trade_brief(t: dict, ex: dict) -> dict:
    return {"ts_code": t["ts_code"], "name": t["name"],
            "sel_date": t["sel_date"], "entry_date": t["entry_date"],
            "exit_date": ex["exit_date"], "net_pct": round(ex["net_pct"], 4)}


# --------------------------------------------------------------------------
# Engine entry point
# --------------------------------------------------------------------------

def run_backtest(selections, prices: pd.DataFrame, calendar: List[str],
                 baseline=None, holding_days: Sequence[int] = DEFAULT_HOLDING_DAYS,
                 cost: Union[None, float, CostModel] = None,
                 label: Optional[str] = None,
                 threshold_fn=limit_up_threshold) -> dict:
    """Replay (date, [codes]) selections with honest execution.

    selections: iterable of (sel_date, [ts_code or {'ts_code','name'}]).
    prices: long-format frame (ts_code, trade_date, open, close, ...).
    calendar: full trading calendar (sorted "YYYYMMDD" strings).
    baseline: {date: market mean pct_chg} or DataFrame; optional.
    cost: CostModel, bare float (total round-trip pct), or None for default.
    """
    cost = _coerce_cost(cost)
    holding_days = sorted(set(int(n) for n in holding_days))
    selections = normalize_selections(selections)
    base_map = _coerce_baseline(baseline)
    book = prices if isinstance(prices, PriceBook) else PriceBook(prices)

    trades, skips = build_trades(selections, book, calendar, cost,
                                 holding_days, threshold_fn)
    n_entries = sum(len(items) for _, items in selections)

    per_period: Dict[str, dict] = {}
    for n in holding_days:
        executed = [t for t in trades if t["exits"].get(n) is not None]
        pending = len(trades) - len(executed)

        nets = np.array([t["exits"][n]["net_pct"] for t in executed], dtype=float)
        grosses = np.array([t["exits"][n]["gross_pct"] for t in executed], dtype=float)
        n_carry = sum(1 for t in executed if t["exits"][n]["carry_forward"])

        # --- equal-weight daily portfolio across overlapping batches ---
        acc: Dict[str, List[float]] = {}
        for t in executed:
            for d, r in _position_daily_returns(t, n, book, calendar, cost):
                acc.setdefault(d, []).append(r)
        port_dates = sorted(acc)
        port_rets = [float(np.mean(acc[d])) for d in port_dates]
        port = equity_metrics(port_rets)

        # --- baseline over the SAME active dates ---
        baseline_block = None
        if base_map:
            base_rets = [base_map.get(d, 0.0) / 100.0 for d in port_dates]
            base = equity_metrics(base_rets)
            baseline_block = {
                "baseline_total_return_pct": base["total_return_pct"],
                "baseline_annualized_return_pct": base["annualized_return_pct"],
                "excess_total_return_pct":
                    port["total_return_pct"] - base["total_return_pct"],
                "excess_annualized_return_pct": None
                    if port["annualized_return_pct"] is None
                    or base["annualized_return_pct"] is None
                    else port["annualized_return_pct"] - base["annualized_return_pct"],
            }

        # --- per-trade baseline window (entry..exit, close-to-close) ---
        avg_trade_baseline = avg_trade_excess = None
        if base_map and len(executed):
            tb = []
            for t in executed:
                ex = t["exits"][n]
                i = bisect.bisect_left(calendar, t["entry_date"])
                j = bisect.bisect_left(calendar, ex["exit_date"])
                tb.append(_compound_pct(base_map.get(d, 0.0)
                                        for d in calendar[i:j + 1]))
            avg_trade_baseline = float(np.mean(tb))
            avg_trade_excess = float(np.mean(nets)) - avg_trade_baseline

        block = {
            "holding_days": n,
            "n_trades": int(len(executed)),
            "n_pending_exit": int(pending),
            "n_carry_forward_exit": int(n_carry),
            "n_skipped_limit_up": int(skips["limit_up"]),
            "n_skipped_suspended": int(skips["suspended"]),
            "win_rate_pct": float((nets > 0).mean() * 100.0) if len(nets) else None,
            "avg_return_per_trade_pct": float(nets.mean()) if len(nets) else None,
            "median_return_per_trade_pct": float(np.median(nets)) if len(nets) else None,
            "avg_gross_return_per_trade_pct": float(grosses.mean()) if len(grosses) else None,
            "avg_baseline_per_trade_pct": avg_trade_baseline,
            "avg_excess_per_trade_pct": avg_trade_excess,
            "portfolio": {
                "n_days": port["n_days"],
                "start_date": port_dates[0] if port_dates else None,
                "end_date": port_dates[-1] if port_dates else None,
                "total_return_pct": port["total_return_pct"],
                "annualized_return_pct": port["annualized_return_pct"],
                "max_drawdown_pct": port["max_drawdown_pct"],
                **(baseline_block or {}),
            },
        }
        if len(executed):
            best = max(executed, key=lambda t: t["exits"][n]["net_pct"])
            worst = min(executed, key=lambda t: t["exits"][n]["net_pct"])
            block["best_trade"] = _trade_brief(best, best["exits"][n])
            block["worst_trade"] = _trade_brief(worst, worst["exits"][n])
        per_period[f"T+{n}"] = block

    return {
        "engine": "backtest_v2",
        "label": label,
        "n_selection_dates": len(selections),
        "selection_date_range": [selections[0][0], selections[-1][0]] if selections else None,
        "n_selected_entries": int(n_entries),
        "cost_model": cost.as_dict(),
        "execution_rules": {
            "entry": "选股日次一交易日开盘价买入（非当日收盘）",
            "limit_up_skip": (
                "入场日开盘 >= 前收 × (1+板块涨停阈值) × "
                f"{LIMIT_UP_SAFETY} 视为一字/近涨停开盘，放弃买入并计数"
            ),
            "board_thresholds": "主板9.8% / 创业板·科创板19.8% / 北交所29.8%",
            "suspended_skip": "入场日无行情记录（停牌/数据缺失）则放弃买入并计数",
            "exit": "T+N 持有期：入场日之后第 N 个交易日收盘价卖出",
            "carry_forward": "卖出日停牌时按其前最近可得收盘价结算并打 carry_forward 标记",
            "cost_application": "逐笔收益直接扣减整段往返成本；日度组合曲线买/卖日各扣一半",
        },
        "skips": skips,
        "per_holding_period": per_period,
    }


# --------------------------------------------------------------------------
# Banks-excluded variant
# --------------------------------------------------------------------------

def split_bank_variant(selections, sector_map: Optional[dict] = None):
    """Filter bank stocks out of selections.

    Bank detection: stock_dim sector_name contains 银行, falling back to the
    stock name containing 银行/农商/商行. Returns (filtered, n_removed, removed).
    """
    sector_map = sector_map or {}
    filtered, removed = [], []
    for sel_date, items in normalize_selections(selections):
        keep = []
        for it in items:
            dim = sector_map.get(it["ts_code"], {})
            name = it.get("name") or dim.get("name")
            if is_bank(name=name, sector=dim.get("sector")):
                removed.append({"sel_date": sel_date, "ts_code": it["ts_code"],
                                "name": name})
            else:
                keep.append(it)
        if keep:
            filtered.append((sel_date, keep))
    return filtered, len(removed), removed


def run_backtest_with_bank_variant(selections, prices, calendar, baseline=None,
                                   holding_days=DEFAULT_HOLDING_DAYS, cost=None,
                                   sector_map=None, label=None) -> dict:
    """Run the engine on all selections and on a banks-excluded variant."""
    book = prices if isinstance(prices, PriceBook) else PriceBook(prices)
    full = run_backtest(selections, book, calendar, baseline=baseline,
                        holding_days=holding_days, cost=cost, label=label)
    ex_sel, n_removed, removed = split_bank_variant(selections, sector_map)
    ex_label = f"{label}（剔除银行）" if label else "剔除银行"
    ex_banks = run_backtest(ex_sel, book, calendar, baseline=baseline,
                            holding_days=holding_days, cost=cost, label=ex_label)
    bank_names = sorted({r["name"] for r in removed if r.get("name")})
    return {
        "all": full,
        "ex_banks": ex_banks,
        "n_bank_entries_removed": n_removed,
        "bank_names_removed": bank_names,
    }
