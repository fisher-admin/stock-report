#!/usr/bin/env python3
"""Small-sample check: Sina hfq adjustment factors vs the frozen Tushare adj_factor cross-sections (B-stage seed).

Picks stocks whose frozen adj_factor changed inside the frozen window (ex-rights events) plus a few unchanged ones,
fetches Sina hfq factors once per stock (>= 3 s apart, no retries, logged), and compares
  (a) event dates: the dates where the factor steps,
  (b) step ratios f_t / f_{t-1} at those dates (what actually matters for adjusted returns),
  (c) level ratio Sina / Tushare across the window (should be constant if only the base differs).
Research only; writes the report next to the probe logs.

  sina_adj_offline_check.py --n-events 12 --n-flat 4 --out DIR
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

WS = Path("/srv/hermes-stock/workspace")
CACHE = WS / "stock_data/03-working/backtest_cache"


def frozen_panel() -> pd.DataFrame:
    fr = []
    for p in sorted(CACHE.glob("adj_factor_2*.parquet")):
        f = pd.read_parquet(p, columns=["ts_code", "trade_date", "adj_factor"])
        fr.append(f)
    x = pd.concat(fr, ignore_index=True)
    x["trade_date"] = x["trade_date"].astype(str)
    return x.pivot_table(index="trade_date", columns="ts_code", values="adj_factor").sort_index()


def sina_hfq(ts_code: str, sess: requests.Session) -> pd.Series:
    code, ex = ts_code.split(".")
    sym = f"{ex.lower()}{code}"
    r = sess.get(f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js", timeout=15)
    r.raise_for_status()
    t = r.text
    data, _ = json.JSONDecoder().raw_decode(t[t.find("{"):])
    s = pd.Series({it["d"].replace("-", ""): float(it["f"]) for it in data.get("data", [])}).sort_index()
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-events", type=int, default=12)
    ap.add_argument("--n-flat", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    P = frozen_panel()
    P = P.loc[:, P.notna().mean() > 0.95]
    steps = (P / P.shift(1) - 1).abs() > 1e-6
    n_steps = steps.sum()
    ev = n_steps[n_steps >= 1].index.tolist()
    flat = n_steps[n_steps == 0].index.tolist()
    pick = sorted(ev)[:: max(1, len(ev) // a.n_events)][: a.n_events] + sorted(flat)[:: max(1, len(flat) // a.n_flat)][: a.n_flat]
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    rows, log = [], out / "requests.log"
    for i, c in enumerate(pick):
        if i:
            time.sleep(3)
        t0 = time.time()
        try:
            s = sina_hfq(c, sess)
            err = None
        except Exception as e:  # noqa: BLE001
            s, err = pd.Series(dtype=float), f"{type(e).__name__}"
        with log.open("a") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {c} rows={len(s)} sec={time.time() - t0:.1f} err={err}\n")
        if err:
            print("stop:", c, err)
            break
        tu = P[c].dropna()
        # step function: Sina gives effective dates; value on a trading day = last factor on or before it
        sina_on = s.reindex(sorted(set(s.index) | set(tu.index))).ffill().reindex(tu.index)
        lvl = (sina_on / tu).dropna()
        tu_steps = tu[(tu / tu.shift(1) - 1).abs() > 1e-6]
        sn_steps = sina_on[(sina_on / sina_on.shift(1) - 1).abs() > 1e-9]
        ratio_t = (tu / tu.shift(1)).loc[tu_steps.index]
        ratio_s = (sina_on / sina_on.shift(1)).reindex(tu_steps.index)
        rows.append({"ts_code": c, "window": f"{tu.index[0]}..{tu.index[-1]}", "tushare_steps": list(tu_steps.index),
                     "sina_steps": list(sn_steps.index), "same_dates": list(tu_steps.index) == list(sn_steps.index),
                     "max_step_ratio_diff": float((ratio_t - ratio_s).abs().max()) if len(ratio_t) else 0.0,
                     "level_ratio_cv": float(lvl.std() / lvl.mean()) if len(lvl) > 1 else 0.0,
                     "level_ratio": float(lvl.mean()) if len(lvl) else None})
    rep = pd.DataFrame(rows)
    rep.to_json(out / "sina_adj_check.json", orient="records", force_ascii=False, indent=1)
    print(rep[["ts_code", "same_dates", "max_step_ratio_diff", "level_ratio_cv", "tushare_steps"]].to_string())
    print("frozen window", P.index[0], "..", P.index[-1], "stocks", P.shape[1], "with events", len(ev))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
