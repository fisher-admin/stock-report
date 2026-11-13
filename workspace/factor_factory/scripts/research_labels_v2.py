#!/usr/bin/env python3
"""D4 — uncensored forward-return labels and the investable equal-weight benchmark.

Execution convention (plan §3, pre-registered):
  signal at close T  ->  buy at the open of T+1  ->  sell at the open of T+1+h  (h = 5, 20)
  * entry blocked if T+1 is suspended or opens at limit-up      -> label NaN, entry_blocked = 1
  * exit delayed while the security is suspended or opens at limit-down; first sellable open wins
    (at most MAX_EXIT_DELAY sessions, else marked at the last traded close: exit_kind = "stale")
  * a security that stops trading for good (delisting) is marked at its last traded close
    (exit_kind = "delist")

Labels never require universe membership after T, so names that turn ST, crash out of the
universe or delist after entry keep their realized return (no post-entry censoring).

Prices: full per-code history (data/price_hist/daily, data/delisted_hist/daily). Total-return
index from exchange pct_chg (ex-rights adjusted vs pre_close); open adjusted by the same-day
open/close ratio. Where a production stk_factor close exists (2026-01-05+) and disagrees with the
daily close, stk_factor is canonical (lab DATA_AUDIT: snapshot-corrupted daily closes).
Limits: main board 10% (ST 5% from PIT stock_st), ChiNext 20%.

Outputs (factor_factory/data/):
  research_labels_v2.parquet           one row per panel (ts_code, trade_date)
  research_benchmark_v2.parquet        investable EW benchmark per rebalance period, h = 5/20
  research_labels_v2.manifest.json     hashes, coverage, rules
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_panel_v2 as rp  # noqa: E402

PRICE_DIRS = (ROOT / "data/price_hist/daily", ROOT / "data/delisted_hist/daily")
HORIZONS = (5, 20)
MAX_EXIT_DELAY = 20
STAMP_DUTY_CUT = "20230828"  # sell-side stamp duty 10bp -> 5bp


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 2.5
    transfer_bps: float = 0.1
    slippage_bps: float = 10.0  # lab default; B1 open-VWAP median ~0bp, p75 ~50bp (sensitivity)

    def buy(self) -> float:
        return (self.commission_bps + self.transfer_bps + self.slippage_bps) / 1e4

    def sell(self, trade_date: str) -> float:
        stamp = 10.0 if trade_date < STAMP_DUTY_CUT else 5.0
        return (self.commission_bps + self.transfer_bps + self.slippage_bps + stamp) / 1e4


# ---------------------------------------------------------------- price matrices

def load_prices(codes: list[str], sessions: list[str]) -> dict[str, pd.DataFrame]:
    frames = []
    for code in codes:
        for d in PRICE_DIRS:
            p = d / f"{code}.parquet"
            if p.exists():
                f = pd.read_parquet(p, columns=["ts_code", "trade_date", "open", "high", "low", "close",
                                                "pre_close", "pct_chg", "vol"])
                if not f.empty:
                    frames.append(f)
                break
    long = pd.concat(frames, ignore_index=True)
    long["trade_date"] = long["trade_date"].astype(str)
    long = long.drop_duplicates(["ts_code", "trade_date"], keep="last")
    long = long[long["trade_date"].isin(set(sessions))]
    wide = {c: long.pivot(index="trade_date", columns="ts_code", values=c).reindex(index=sessions, columns=codes)
            for c in ("open", "close", "pre_close", "pct_chg", "vol")}
    return wide


def apply_canonical_close(w: dict[str, pd.DataFrame]) -> dict[str, int]:
    """stk_factor close/pct_change override the daily close where they disagree (2026-01-05+)."""
    fixed = {}
    for d in w["close"].index:
        p = rp.BACKTEST_CACHE / f"stk_factor_{d}.parquet"
        if not p.exists():
            continue
        s = pd.read_parquet(p, columns=["ts_code", "close", "pct_change"]).set_index("ts_code")
        s = s.reindex(w["close"].columns)
        cur = w["close"].loc[d]
        bad = s["close"].notna() & (s["close"] > 0) & cur.notna() & ((cur - s["close"]).abs() > 1e-6)
        if bad.any():
            w["close"].loc[d, bad] = s.loc[bad, "close"]
            w["pct_chg"].loc[d, bad] = s.loc[bad, "pct_change"]
            fixed[d] = int(bad.sum())
    return fixed


def limit_ratio(codes: pd.Index, st_mask: pd.DataFrame) -> pd.DataFrame:
    base = np.where(codes.str.startswith(("300", "301")), 0.20, 0.10)
    lim = pd.DataFrame(np.broadcast_to(base, st_mask.shape).copy(), index=st_mask.index, columns=codes)
    main = ~codes.str.startswith(("300", "301"))
    lim.loc[:, main] = lim.loc[:, main].where(~st_mask.loc[:, main], 0.05)
    return lim


def st_matrix(sessions: list[str], codes: list[str]) -> pd.DataFrame:
    sets = rp.load_stock_st_sets()
    idx = {c: i for i, c in enumerate(codes)}
    m = np.zeros((len(sessions), len(codes)), dtype=bool)
    for i, d in enumerate(sessions):
        for c in sets.get(d, ()):
            j = idx.get(c)
            if j is not None:
                m[i, j] = True
    return pd.DataFrame(m, index=sessions, columns=codes)


def build_matrices(codes: list[str], sessions: list[str]) -> tuple[dict[str, np.ndarray], dict]:
    w = load_prices(codes, sessions)
    fixed = apply_canonical_close(w)
    close, opn, pre, vol = w["close"], w["open"], w["pre_close"], w["vol"]
    traded = (vol > 0) & close.notna() & (close > 0) & opn.notna() & (opn > 0)
    r = (w["pct_chg"] / 100.0).where(traded)
    adj_close = (1.0 + r.fillna(0.0)).cumprod()
    adj_open = adj_close * (opn / close)
    st = st_matrix(sessions, codes)
    lim = limit_ratio(pd.Index(codes), st)
    up = ((pre * (1 + lim)) + 1e-9).round(2)
    dn = ((pre * (1 - lim)) + 1e-9).round(2)
    can_buy = traded & (opn < up - 0.001)
    can_sell = traded & (opn > dn + 0.001)
    # last session on which the security traded at all (delisting detection)
    tr = traded.to_numpy()
    last_trade = np.where(tr.any(axis=0), len(sessions) - 1 - np.argmax(tr[::-1], axis=0), -1)
    # mark price at each open: the adjusted open if traded, else the last traded adjusted close
    mark = adj_open.where(traded).fillna(adj_close.where(traded).ffill())
    mats = {"adj_open": adj_open.to_numpy(), "adj_close": adj_close.where(traded).to_numpy(),
            "mark_open": mark.to_numpy(),
            "can_buy": can_buy.to_numpy(), "can_sell": can_sell.to_numpy(), "traded": tr,
            "last_trade": last_trade, "c2c": r.to_numpy(), "vol": vol.to_numpy(),
            "close_raw": close.to_numpy()}
    meta = {"canonical_close_overrides": fixed, "codes_with_prices": int(close.notna().any().sum())}
    return mats, meta


# ---------------------------------------------------------------- labels

def label_one(mats: dict[str, np.ndarray], t: int, j: int, h: int, n_sessions: int,
              delisted: bool) -> tuple[float, int, str]:
    """Return (gross_return, exit_delay, exit_kind) for signal session t, code column j."""
    e = t + 1
    if e >= n_sessions or not mats["can_buy"][e, j]:
        return np.nan, 0, "entry_blocked"
    entry = mats["adj_open"][e, j]
    x0 = e + h
    last = mats["last_trade"][j]
    if delisted and last < x0:
        # security stops trading before the planned exit: mark at its last traded close
        px = mats["adj_close"][last, j]
        return px / entry - 1.0, max(0, last - e), "delist"
    if x0 >= n_sessions:
        return np.nan, 0, "beyond_sample"
    for x in range(x0, min(n_sessions, x0 + MAX_EXIT_DELAY + 1)):
        if mats["can_sell"][x, j]:
            return mats["adj_open"][x, j] / entry - 1.0, x - x0, "normal" if x == x0 else "delayed"
    if x0 + MAX_EXIT_DELAY >= n_sessions:
        return np.nan, 0, "beyond_sample"
    # still unsellable after the cap: mark at the last traded close inside the window
    window = mats["traded"][x0:x0 + MAX_EXIT_DELAY + 1, j]
    k = x0 + (len(window) - 1 - int(np.argmax(window[::-1]))) if window.any() else e
    return mats["adj_close"][k, j] / entry - 1.0, MAX_EXIT_DELAY, "stale"


def build_labels(panel: pd.DataFrame, mats: dict[str, np.ndarray], sessions: list[str], codes: list[str],
                 delisted_codes: set[str]) -> pd.DataFrame:
    s_idx = {d: i for i, d in enumerate(sessions)}
    c_idx = {c: i for i, c in enumerate(codes)}
    t = panel["trade_date"].map(s_idx).to_numpy()
    j = panel["ts_code"].map(c_idx).to_numpy()
    out = {"ts_code": panel["ts_code"].to_numpy(), "trade_date": panel["trade_date"].to_numpy()}
    n = len(sessions)
    is_del = np.array([c in delisted_codes for c in panel["ts_code"]])
    for h in HORIZONS:
        g = np.full(len(panel), np.nan)
        delay = np.zeros(len(panel), dtype=np.int16)
        kind = np.empty(len(panel), dtype=object)
        for k in range(len(panel)):
            g[k], delay[k], kind[k] = label_one(mats, int(t[k]), int(j[k]), h, n, bool(is_del[k]))
        out[f"fwd_open_{h}"] = g
        out[f"exit_delay_{h}"] = delay
        out[f"exit_kind_{h}"] = kind
    df = pd.DataFrame(out)
    df["entry_blocked"] = (df["exit_kind_5"] == "entry_blocked").astype(np.int8)
    return df


# ---------------------------------------------------------------- investable benchmark

def portfolio_periods(weights_by_period: list[tuple[int, dict[int, float]]], mats: dict[str, np.ndarray],
                      sessions: list[str], h: int, cost: CostModel) -> pd.DataFrame:
    """Simulate a rebalance-every-h portfolio at T+1 opens with drift, blocked trades and costs.

    weights_by_period: [(signal_session_index, {code_col: target_weight})]. Targets are executed at
    the open of signal+1; a name that cannot be sold keeps its drifted weight; a blocked buy stays cash.
    """
    MK, CS, CB, LT = mats["mark_open"], mats["can_sell"], mats["can_buy"], mats["last_trade"]
    rows = []
    delist_liquidations = 0
    held: dict[int, float] = {}  # code col -> value at the current rebalance open (equity units)
    equity = 1.0
    for k, (t, target) in enumerate(weights_by_period):
        e = t + 1
        if e >= len(sessions):
            break
        # revalue holdings from the previous rebalance open to this one
        if k > 0:
            prev_e = weights_by_period[k - 1][0] + 1
            cash = equity - sum(held.values())
            new_held = {}
            for j, v in held.items():
                m0, m1 = MK[prev_e, j], MK[e, j]
                v1 = v * m1 / m0 if np.isfinite(m0) and np.isfinite(m1) and m0 > 0 else v
                if LT[j] < e and LT[j] < len(sessions) - 1:
                    cash += v1  # stopped trading for good: written off at its last traded close
                    delist_liquidations += 1
                else:
                    new_held[j] = v1
            held = new_held
            equity = cash + sum(held.values())
        cash = equity - sum(held.values())
        tgt_val = {j: w * equity for j, w in target.items()}
        sell_rate, buy_rate = cost.sell(sessions[e]), cost.buy()
        sold = bought = cost_paid = 0.0
        blocked_sell = blocked_buy = 0
        for j in list(held):
            want = tgt_val.get(j, 0.0)
            if held[j] > want + 1e-12:
                if CS[e, j]:
                    amt = held[j] - want
                    held[j] -= amt
                    cash += amt * (1 - sell_rate)
                    sold += amt
                    cost_paid += amt * sell_rate
                    if held[j] <= 1e-12:
                        del held[j]
                else:
                    blocked_sell += 1
        for j, want in tgt_val.items():
            cur = held.get(j, 0.0)
            if want > cur + 1e-12:
                if CB[e, j]:
                    amt = min(want - cur, max(cash, 0.0))
                    if amt <= 0:
                        continue
                    held[j] = cur + amt * (1 - buy_rate)
                    cash -= amt
                    bought += amt
                    cost_paid += amt * buy_rate
                else:
                    blocked_buy += 1
        equity = cash + sum(held.values())
        rows.append({"signal_date": sessions[t], "trade_open": sessions[e], "equity_after_trades": equity,
                     "turnover": (sold + bought) / max(equity, 1e-12), "cost": cost_paid,
                     "n_hold": len(held), "blocked_buys": blocked_buy, "blocked_sells": blocked_sell,
                     "cash_w": cash / max(equity, 1e-12), "delist_liquidations": delist_liquidations})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["period_ret"] = df["equity_after_trades"].pct_change()
    df.loc[df.index[0], "period_ret"] = df["equity_after_trades"].iloc[0] - 1.0
    df["horizon"] = h
    return df


def investable_benchmark(panel: pd.DataFrame, mats: dict[str, np.ndarray], sessions: list[str],
                         codes: list[str], h: int, cost: CostModel) -> pd.DataFrame:
    s_idx = {d: i for i, d in enumerate(sessions)}
    c_idx = {c: i for i, c in enumerate(codes)}
    dates = sorted(panel["trade_date"].unique())
    reb = dates[::h]
    by_date = panel.groupby("trade_date")["ts_code"].apply(list)
    plan = []
    for d in reb:
        cols = [c_idx[c] for c in by_date[d]]
        w = 1.0 / len(cols)
        plan.append((s_idx[d], {j: w for j in cols}))
    return portfolio_periods(plan, mats, sessions, h, cost)


def c2c_ew_daily(panel: pd.DataFrame, mats: dict[str, np.ndarray], sessions: list[str],
                 codes: list[str]) -> pd.Series:
    """Non-investable reference: universe(T) equal-weight mean of the T+1 close-to-close return."""
    s_idx = {d: i for i, d in enumerate(sessions)}
    c_idx = {c: i for i, c in enumerate(codes)}
    t = panel["trade_date"].map(s_idx).to_numpy()
    j = panel["ts_code"].map(c_idx).to_numpy()
    nxt = np.where(t + 1 < len(sessions), t + 1, t)
    r = mats["c2c"][nxt, j]
    s = pd.Series(r, index=panel["trade_date"].to_numpy())
    s[t + 1 >= len(sessions)] = np.nan
    return s.groupby(level=0).mean()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--panel", default=str(ROOT / "data/research_panel_v2.parquet"))
    args = ap.parse_args(argv)
    started = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    panel_path = Path(args.panel)
    pman = json.loads(panel_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    panel = pd.read_parquet(panel_path, columns=["ts_code", "trade_date"])
    # official calendar ends with the price history (20261009): late signals are "beyond_sample"
    sessions = rp.official_sessions("99999999")
    sessions = [d for d in sessions if d >= panel["trade_date"].min()]
    codes = sorted(panel["ts_code"].unique())
    missing_px = [c for c in codes if not any((d / f"{c}.parquet").exists() for d in PRICE_DIRS)]
    mats, mmeta = build_matrices(codes, sessions)
    delisted = {p.stem for p in (ROOT / "data/delisted_hist/daily").glob("*.parquet")}
    labels = build_labels(panel, mats, sessions, codes, delisted)
    cost = CostModel()
    bench = pd.concat([investable_benchmark(panel, mats, sessions, codes, h, cost) for h in HORIZONS],
                      ignore_index=True)
    c2c = c2c_ew_daily(panel, mats, sessions, codes)
    # wide matrices for D5 (factor construction on full, uncensored price history)
    np.savez_compressed(ROOT / "data/research_matrices_v2.npz", sessions=np.array(sessions), codes=np.array(codes),
                        **mats)
    out_l = ROOT / "data/research_labels_v2.parquet"
    out_b = ROOT / "data/research_benchmark_v2.parquet"
    labels.to_parquet(out_l, index=False, compression="zstd")
    bench.to_parquet(out_b, index=False)
    c2c.rename("c2c_ew_ret").to_frame().reset_index(names="trade_date").to_parquet(
        ROOT / "data/research_c2c_ew_reference_v2.parquet", index=False)
    cover = {}
    for h in HORIZONS:
        k = labels[f"exit_kind_{h}"].value_counts().to_dict()
        cover[str(h)] = {"rows": int(len(labels)), "labelled": int(labels[f"fwd_open_{h}"].notna().sum()),
                         "exit_kind": {str(a): int(b) for a, b in k.items()}}
    manifest = {
        "schema_version": "research_labels_v2.1", "built_at": started,
        "panel_sha256": pman["sha256"], "labels_sha256": rp.sha256_file(out_l), "benchmark_sha256": rp.sha256_file(out_b),
        "sessions": [sessions[0], sessions[-1]], "codes": len(codes), "codes_without_price_history": missing_px,
        "cost_model": asdict(cost), "stamp_duty_cut": STAMP_DUTY_CUT, "max_exit_delay": MAX_EXIT_DELAY,
        "coverage": cover, **mmeta, "code_commit": rp.git_commit(),
    }
    (ROOT / "data/research_labels_v2.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"coverage": cover, "codes_without_price_history": len(missing_px)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
