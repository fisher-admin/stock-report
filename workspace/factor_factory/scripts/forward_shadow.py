#!/usr/bin/env python3
"""Phase 4 forward shadow for comp_rank_ew6 (h=5): passive, read-only on production paths.

Nightly, for signal date D (after the 22:15 research job has written D's daily_basic and PIT universe):
  1. universe(D): build_universe rules over the exact-date PIT snapshot + board filter
  2. comp_rank_ew6(D): the Phase 3 definition, computed from a trailing window of production caches
     (backtest_cache daily/stk_factor, daily_basic_hist or short-track materialized daily_basic)
  3. on rebalance dates (every 5th official session from the anchor): record T+0 books
     avoid_losers_70, buffered_top100 and the investable EW benchmark book into books.jsonl
     (append-only; a signal is official only if recorded before the T+1 09:30 open)
  4. settle every matured 5-day period with the D4 execution rules (T+1 open entry, limit-up block,
     limit-down/suspension exit delay, CostModel 10 bp/side) into settled.jsonl, regime-tagged by the
     benchmark's period return

Writes only factor_factory/data/forward_shadow/. Never touches #/today, v4.3 or stock-report.

  --write-spec     freeze factor_factory/contracts/forward_shadow_v1.json (commit before the first run)
  --date YYYYMMDD  signal date (default: latest session with a daily cache)
  --test           write to forward_shadow/test/ and skip the entry-time guard (parity/initialization runs)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_universe as bu  # noqa: E402
import gauntlet_v2 as gv  # noqa: E402
import research_labels_v2 as rl  # noqa: E402
import research_panel_v2 as rp  # noqa: E402

TZ = gv.TZ
SPEC_PATH = ROOT / "contracts/forward_shadow_v1.json"
FS = ROOT / "data/forward_shadow"
H = 5
WINDOW = 110  # sessions of trailing history (ivol_60 needs 60 + 20)
MIN_PRESENT = 4
ENTRY_OPEN = dtime(9, 30)
FORWARD_DB_DIRS = (
    # 2026-10-30: the evening data stage freezes the day here; the 22:15 short-track copy below is
    # byte-identical (copied from this store) but missing whenever that research job times out (10-26..29).
    rp.STOCK_WORKING / "fundamental_cache/pit_market/v2",
    rp.STOCK_WORKING / "strategy_research/short_track/materialized/daily_basic",
    rp.STOCK_WORKING / "strategy_research/short_track/input_cache",
    rp.STOCK_WORKING / "fundamental_cache/pit_market",
)
DB_COLS = ["ts_code", "pe_ttm", "pb", "circ_mv", "turnover_rate_f"]
FACTORS = [("ivol_60", -1), ("turn_20", -1), ("turn_abn_5_60", -1), ("val_comp_indneu", 1), ("vpd_10", 1),
           ("turn_cv_20", -1)]

SPEC = {
    "spec_id": "forward_shadow_v1",
    "signal": "comp_rank_ew6 (Phase 3 definition, spec hashes in phase3_specs.json), horizon 5",
    "factors": FACTORS,
    "composite": "per signal date, mean over the six direction-adjusted cross-sectional pct ranks on universe rows; "
                 ">=4 of 6 present",
    "universe": "factor_factory/config/universe_rules.yaml via build_universe rules over the exact-date PIT snapshot; "
                "STAR/BSE excluded; ST by PIT is_st and name patterns",
    "market_proxy_for_ivol": "EW mean c2c over SH/SZ main + ChiNext codes with a daily row",
    "books": {
        "avoid_losers_70": "universe excluding the bottom 30% by composite (NaN composite kept), equal weight",
        "buffered_top100": "K=100, enter top 15%, keep while inside top 35%, industry cap 15 names, equal weight",
        "benchmark_ew": "whole universe, equal weight (investable EW benchmark)",
    },
    "cadence": "rebalance on every 5th official session counting from the anchor (first official signal date)",
    "execution": "D4 rules: trade at the T+1 open; limit-up open blocks buys; limit-down open or suspension blocks "
                 "sells (held until sellable); delisted names written off at the last traded close; drift between "
                 "rebalances",
    "cost_model": {"commission_bps": 2.5, "transfer_bps": 0.1, "slippage_bps": 10.0,
                   "stamp_duty_bps_sell": 5.0},
    "forward_only": "a signal is official only if recorded before 09:30 Asia/Shanghai on its entry session; "
                    "missed signal dates are logged as gaps, never backfilled",
    "regime_tag": "per settled period: 'up' if the benchmark period return > 0, else 'down'",
    "evaluation": {
        "when": "after 50 matured periods per book; no promotion decision before that",
        "primary": "mean period net excess vs benchmark_ew, Newey-West t (lag 1), per book; the first period "
                   "(initial build-up trade) is excluded, as in gauntlet v2",
        "success": "t >= 2.0 on at least one book",
        "reported_not_gated": ["regime-conditional mean excess and counts (up/down)", "turnover", "blocked trades",
                               "yearly excess"],
    },
    "no_production_effect": "never writes #/today, v4.3 artifacts, stock-report or production health files",
}


def spec_hash() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()[:16]


def committed_and_clean(path: Path) -> bool:
    rel = str(path.relative_to(WORKSPACE))
    tracked = subprocess.run(["git", "-C", str(WORKSPACE), "ls-files", "--error-unmatch", rel],
                             capture_output=True).returncode == 0
    clean = subprocess.run(["git", "-C", str(WORKSPACE), "diff", "--quiet", "HEAD", "--", rel]).returncode == 0
    return tracked and clean


# ---------------------------------------------------------------- calendar & inputs

def official_sessions(upto: str) -> list[str]:
    hist = rp.official_sessions("20261009")
    cal = json.loads((rp.STOCK_WORKING / "health/trading_calendar.json").read_text())
    fwd = [d for d in cal.get("open_dates", []) if hist[-1] < d <= upto]
    return hist + fwd


def board_ok(codes: pd.Index) -> np.ndarray:
    return ~(codes.str.endswith(".BJ") | codes.str.startswith(("688", "689")))


def load_daily_basic(d: str) -> pd.DataFrame | None:
    paths = [rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet"] + [p / f"daily_basic_{d}.parquet" for p in FORWARD_DB_DIRS]
    for p in paths:
        if p.exists():
            f = pd.read_parquet(p)
            if set(DB_COLS).issubset(f.columns) and set(f["trade_date"].astype(str)) == {d}:
                return f[DB_COLS]
    return None


def window_mats(sessions: list[str]) -> dict[str, pd.DataFrame]:
    rows = []
    for d in sessions:
        p = rp.BACKTEST_CACHE / f"daily_{d}.parquet"
        if not p.exists():
            continue
        f = pd.read_parquet(p, columns=["ts_code", "trade_date", "open", "close", "pre_close", "pct_chg", "vol"])
        f["trade_date"] = f["trade_date"].astype(str)
        s = rp.BACKTEST_CACHE / f"stk_factor_{d}.parquet"
        if s.exists():  # stk_factor close is canonical where it disagrees (D4 rule)
            k = pd.read_parquet(s, columns=["ts_code", "close", "pct_change"]).rename(
                columns={"close": "close_stk", "pct_change": "pct_stk"})
            f = f.merge(k, on="ts_code", how="left")
            bad = f["close_stk"].notna() & (f["close_stk"] > 0) & ((f["close"] - f["close_stk"]).abs() > 1e-6)
            f.loc[bad, "close"] = f.loc[bad, "close_stk"]
            f.loc[bad, "pct_chg"] = f.loc[bad, "pct_stk"]
            f = f.drop(columns=["close_stk", "pct_stk"])
        rows.append(f)
    long = pd.concat(rows, ignore_index=True)
    long = long[board_ok(pd.Index(long["ts_code"].astype(str)))]
    codes = sorted(long["ts_code"].unique())
    w = {c: long.pivot(index="trade_date", columns="ts_code", values=c).reindex(index=sessions, columns=codes)
         for c in ("open", "close", "pre_close", "pct_chg", "vol")}
    traded = (w["vol"] > 0) & (w["close"] > 0) & (w["open"] > 0)
    r = (w["pct_chg"] / 100.0).where(traded)
    w["traded"] = traded
    w["c2c"] = r
    w["adj_close"] = (1 + r.fillna(0.0)).cumprod().where(traded)
    w["adj_open"] = w["adj_close"] * (w["open"] / w["close"])
    db = {}
    for d in sessions:
        f = load_daily_basic(d)
        if f is not None:
            db[d] = f.set_index("ts_code")
    for c in ("pe_ttm", "pb", "circ_mv", "turnover_rate_f"):
        w[c] = pd.DataFrame({d: db[d][c] for d in db}).T.reindex(index=sessions, columns=codes).astype(float)
    w["_db_missing"] = [d for d in sessions if d not in db]
    return w


def universe(d: str, sessions: list[str]) -> pd.DataFrame:
    daily_dates = [x for x in sessions[-90:] if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    uni, meta = rp.recompute_universe([d], daily_dates)
    if uni.empty:
        raise RuntimeError(f"no PIT universe for {d}: {meta.get('missing_pit')}")
    uni = uni[board_ok(pd.Index(uni["ts_code"].astype(str)))].copy()
    return uni


def industry_map() -> pd.Series:
    p = pd.read_parquet(ROOT / "data/research_panel_v2.parquet", columns=["ts_code", "industry"])
    return p.dropna().drop_duplicates("ts_code", keep="last").set_index("ts_code")["industry"]


# ---------------------------------------------------------------- composite

def factor_values(w: dict[str, pd.DataFrame], d: str, uni: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    r, ac, vol, t = w["c2c"], w["adj_close"], w["vol"].where(w["adj_close"].notna()), w["turnover_rate_f"]
    m = r.mean(axis=1, skipna=True)
    cov = r.rolling(60, min_periods=45).cov(m)
    beta = cov.div(m.rolling(60, min_periods=45).var(), axis=0)
    ivol = (r - beta.mul(m, axis=0)).rolling(20, min_periods=15).std()
    mean20 = t.rolling(20, min_periods=15).mean()
    vals = {
        "ivol_60": ivol.loc[d],
        "turn_20": mean20.loc[d],
        "turn_abn_5_60": (t.rolling(5, min_periods=4).mean() / t.rolling(60, min_periods=45).mean()).loc[d],
        "vpd_10": (-(ac.rolling(10, min_periods=7).corr(vol))).loc[d],
        "turn_cv_20": (t.rolling(20, min_periods=15).std() / mean20).loc[d],
    }
    pe, pb = w["pe_ttm"].loc[d], w["pb"].loc[d]
    ep = (1.0 / pe.where(pe > 0)).mask(pe.isna() & pb.notna(), -1e-6)
    bp = 1.0 / pb.where(pb > 0)
    out = pd.DataFrame(index=uni["ts_code"].astype(str))
    for k, v in vals.items():
        out[k] = v.reindex(out.index)
    out["industry"] = ind.reindex(out.index).fillna("UNKNOWN")
    out["_ep"], out["_bp"] = ep.reindex(out.index), bp.reindex(out.index)
    g = out.groupby("industry")
    out["val_comp_indneu"] = (g["_ep"].rank(pct=True) + g["_bp"].rank(pct=True)) / 2.0
    return out.drop(columns=["_ep", "_bp"])


def composite(fv: pd.DataFrame) -> pd.Series:
    ranks = pd.DataFrame({k: (sign * fv[k]).rank(pct=True) for k, sign in FACTORS})
    n = ranks.notna().sum(axis=1)
    return ranks.mean(axis=1, skipna=True).where(n >= MIN_PRESENT)


# ---------------------------------------------------------------- ledger

def paths(test: bool) -> dict[str, Path]:
    base = FS / "test" if test else FS
    base.mkdir(parents=True, exist_ok=True)
    (base / "scores").mkdir(exist_ok=True)
    (base / "runs").mkdir(exist_ok=True)
    return {"base": base, "books": base / "books.jsonl", "settled": base / "settled.jsonl", "gaps": base / "gaps.jsonl",
            "state": base / "state.json"}


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def append(p: Path, rec: dict) -> None:
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def entry_deadline(sessions_after: list[str]) -> datetime | None:
    if not sessions_after:
        return None
    nxt = sessions_after[0]
    return datetime.strptime(nxt, "%Y%m%d").replace(hour=ENTRY_OPEN.hour, minute=ENTRY_OPEN.minute, tzinfo=TZ)


# ---------------------------------------------------------------- settlement (D4 rules)

def settle(P: dict[str, Path], upto: str, sessions_all: list[str]) -> dict:
    books = [b for b in read_jsonl(P["books"]) if b.get("is_rebalance")]
    if not books:
        return {"matured_periods": 0}
    first = books[0]["signal_date"]
    sess = [d for d in sessions_all if first <= d <= upto]
    codes = sorted({c for b in books for k in ("avoid_losers_70", "buffered_top100", "benchmark_ew") for c in b[k]})
    w = window_mats(sess)
    O, C = w["open"].reindex(columns=codes), w["close"].reindex(columns=codes)
    pre, vol = w["pre_close"].reindex(columns=codes), w["vol"].reindex(columns=codes)
    traded = ((vol > 0) & (C > 0) & (O > 0)).fillna(False)
    adj_close = w["adj_close"].reindex(columns=codes)
    adj_open = w["adj_open"].reindex(columns=codes)
    st = pd.DataFrame(False, index=sess, columns=codes)
    for d in sess:
        snap, _ = rp.load_pit_snapshot(d)
        if snap is not None:
            s = snap.set_index("ts_code")["is_st"].astype(bool)
            st.loc[d] = s.reindex(codes).fillna(False).to_numpy()
    lim = rl.limit_ratio(pd.Index(codes), st)
    up, dn = ((pre * (1 + lim)) + 1e-9).round(2), ((pre * (1 - lim)) + 1e-9).round(2)
    static = pd.read_parquet(sorted(rp.STOCK_WORKING.glob(rp.STATIC_BUNDLE_GLOB))[-1], columns=["ts_code", "delist_date"])
    dl = static.set_index("ts_code")["delist_date"].reindex(codes)
    tr = traded.to_numpy()
    # Forward: a name is written off only if it has a delist_date inside the data; a suspension is not a delisting.
    last_trade = np.full(len(codes), len(sess) - 1)
    for j, c in enumerate(codes):
        if isinstance(dl[c], str) and dl[c] <= upto and tr[:, j].any():
            last_trade[j] = len(sess) - 1 - int(np.argmax(tr[::-1, j]))
    mats = {"adj_open": adj_open.to_numpy(), "adj_close": adj_close.where(traded).to_numpy(),
            "mark_open": adj_open.where(traded).fillna(adj_close.where(traded).ffill()).to_numpy(),
            "can_buy": (traded & (O < up - 0.001)).to_numpy(), "can_sell": (traded & (O > dn + 0.001)).to_numpy(),
            "traded": tr, "last_trade": last_trade}
    idx = {d: i for i, d in enumerate(sess)}
    cidx = {c: i for i, c in enumerate(codes)}
    cost = rl.CostModel()
    runs = {}
    for book in ("avoid_losers_70", "buffered_top100", "benchmark_ew"):
        plan = [(idx[b["signal_date"]], {cidx[c]: 1.0 / len(b[book]) for c in b[book]}) for b in books]
        runs[book] = rl.portfolio_periods(plan, mats, sess, H, cost)
    settled = read_jsonl(P["settled"])
    if any(r.empty for r in runs.values()):  # no execution open inside the data yet
        return {"matured_periods": len(settled) // 2, "new_settled": 0}
    bench = runs["benchmark_ew"].set_index("signal_date")
    done = {(x["book"], x["signal_date"]) for x in settled}
    new = []
    for book in ("avoid_losers_70", "buffered_top100"):
        df = runs[book].reset_index(drop=True)
        for k in range(len(df) - 1):  # period k ends at the next rebalance's execution open
            sd, nxt = df.loc[k, "signal_date"], df.loc[k + 1, "signal_date"]
            ret, bret = float(df.loc[k + 1, "period_ret"]), float(bench.loc[nxt, "period_ret"])
            rec = {"book": book, "signal_date": sd, "entry_open": df.loc[k, "trade_open"],
                   "exit_open": df.loc[k + 1, "trade_open"], "period_ret": ret, "bench_ret": bret,
                   "net_excess": ret - bret, "regime": "up" if bret > 0 else "down",
                   "turnover_at_entry": float(df.loc[k, "turnover"]), "n_hold": int(df.loc[k, "n_hold"]),
                   "blocked_buys": int(df.loc[k, "blocked_buys"]), "blocked_sells": int(df.loc[k, "blocked_sells"])}
            if (book, sd) in done:
                old = next(x for x in settled if x["book"] == book and x["signal_date"] == sd)
                if abs(old["period_ret"] - ret) > 1e-9:
                    append(P["base"] / "revisions.jsonl", {"at": datetime.now(TZ).isoformat(), "old": old, "new": rec})
                continue
            rec["settled_at"] = datetime.now(TZ).isoformat()
            append(P["settled"], rec)
            new.append(rec)
    return {"matured_periods": len(read_jsonl(P["settled"])) // 2, "new_settled": len(new)}


# ---------------------------------------------------------------- main

def run(d: str | None, test: bool) -> dict:
    if not test:
        saved = json.loads(SPEC_PATH.read_text()) if SPEC_PATH.exists() else {}
        if saved.get("spec_hash") != spec_hash() or not committed_and_clean(SPEC_PATH):
            raise SystemExit("forward_shadow_v1 spec missing, changed or uncommitted: refusing")
    P = paths(test)
    cal_all = official_sessions("99999999")
    if d is None:
        avail = [x for x in cal_all if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
        d = avail[-1]
    sessions = [x for x in cal_all if x <= d][-WINDOW:]
    if sessions[-1] != d:
        raise SystemExit(f"{d} is not an official session")
    books = read_jsonl(P["books"])
    # A rerun of a recorded date is not a gap, whenever it happens; only unrecorded dates past the open are.
    if any(b["signal_date"] == d for b in books):
        return {"signal_date": d, "status": "already_recorded", **settle(P, d, cal_all)}
    now = datetime.now(TZ)
    deadline = entry_deadline([x for x in cal_all if x > d])
    if not test and deadline is not None and now >= deadline:
        append(P["gaps"], {"signal_date": d, "reason": "entry open already passed", "at": now.isoformat()})
        return {"signal_date": d, "status": "gap_logged"}
    w = window_mats(sessions)
    if d in w["_db_missing"]:
        raise SystemExit(f"daily_basic for {d} not available yet (short-track stage not finished?)")
    uni = universe(d, sessions)
    fv = factor_values(w, d, uni, industry_map())
    comp = composite(fv)
    scores = fv.assign(comp_rank_ew6=comp)
    scores.reset_index(names="ts_code").to_parquet(P["base"] / "scores" / f"scores_{d}.parquet", index=False)
    anchor = books[0]["signal_date"] if books else d
    fwd = [x for x in cal_all if anchor <= x <= d]
    is_reb = (len(fwd) - 1) % H == 0
    rec = {"signal_date": d, "recorded_at": now.isoformat(), "entry_deadline": deadline.isoformat() if deadline else None,
           "official": not test, "spec_hash": spec_hash(), "anchor": anchor, "is_rebalance": bool(is_reb),
           "universe_size": int(len(uni)), "composite_coverage": float(comp.notna().mean()),
           "code_commit": rp.git_commit()}
    if is_reb:
        day = scores.reset_index(names="ts_code").rename(columns={"comp_rank_ew6": "_s"})
        prev = next((b for b in reversed(books) if b.get("is_rebalance")), None)
        rec["avoid_losers_70"] = gv.rule_avoid_losers(day, set())
        rec["buffered_top100"] = gv.rule_buffered_topk(day, set(prev["buffered_top100"]) if prev else set())
        rec["benchmark_ew"] = day["ts_code"].tolist()
    append(P["books"], rec)
    out = {"signal_date": d, "status": "recorded", "is_rebalance": bool(is_reb), "universe": len(uni),
           "composite_coverage": round(rec["composite_coverage"], 4), **settle(P, d, cal_all)}
    (P["base"] / "runs" / f"run_{d}.json").write_text(json.dumps({**out, "at": now.isoformat()}, indent=2))
    P["state"].write_text(json.dumps({"last_signal_date": d, "anchor": anchor, "updated_at": now.isoformat(), **out},
                                     indent=2))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-spec", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args(argv)
    if a.write_spec:
        SPEC_PATH.write_text(json.dumps({**SPEC, "spec_hash": spec_hash(), "frozen_at": datetime.now(TZ).isoformat()},
                                        ensure_ascii=False, indent=2))
        print(f"wrote {SPEC_PATH} hash {spec_hash()}")
        return 0
    print(json.dumps(run(a.date, a.test), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
