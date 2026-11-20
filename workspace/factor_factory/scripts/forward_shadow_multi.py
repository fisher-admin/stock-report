#!/usr/bin/env python3
"""forward_shadow_multi_v1: same-stage forward shadow for EW6 continuation and Aegis-Alpha v1 (passive, paper only).

Replaces the approved-but-unbuilt forward_shadow_retain_v1 (notes/mainsys_retain_forward_change_draft.md) and adds
the Aegis books (notes/aegis_v1_redteam_audit.md). Frozen contracts forward_shadow_v1 / cohort / tier / overnight_v1 /
x_jump_ms_v1 / x_jump_ms_exit_v2 are only READ (scores_<D>, books.jsonl, x_jump_ms_v1 signals + ann_store).

Nightly for signal date D, after forward_shadow_v1 (23:30) and x_jump_ms_v1 have run:
  1. readiness: forward_shadow_v1 recorded D; scores_<D>.parquet exists; K-line quality gate passes on D's prices
  2. scores: comp_rank_ew6 = forward_shadow_v1 scores_<D> (official, not recomputed); aegis_micro_5p / aegis_core_7p
     from scripts/aegis_scorer.py on that universe; STAR / BSE dropped unconditionally
  3. decision: the one sub-book k with (session index since anchor) % 5 == k rebalances; its entry list (ordered) and
     hold zone per book are appended to decisions.jsonl (append-only)
  4. official ledger: a decision is official only if written before the T+1 09:30 open, the deadline is computable
     (fail-closed), the universe is the official one (no proxy), the K-line gate passed, and (Core7P books) the
     x_jump_ms_v1 announcement fetch for D or the previous session succeeded. Anything else: official = false + gaps.jsonl.
  5. continuous track: every decision (official or not) drives the deterministic paper portfolio; a missing night
     means that sub-book simply holds. No manual reruns: a date already in decisions.jsonl is never re-decided.
  6. settlement: daily.parquet is recomputed from decisions.jsonl + prices every run (derived, deterministic).

Pre-deadline retries: the same command is scheduled at 23:40 and after the A2 pre-market self-heal (07:40, 08:55);
it is idempotent. 09:35 run registers the gap for a still-missing D.

  --write-spec      freeze contracts/forward_shadow_multi_v1.json (commit before the first run)
  --date YYYYMMDD   signal date (default: latest session with a forward_shadow_v1 scores file)
  --test            write under multi_v1/test/, no deadline guard, no contract check
  --summary         print the evaluation summary (counts only before the first look date)
  --parity D        recompute D's Aegis scores now and compare with the stored ones (burn-in check)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.dont_write_bytecode = True

import forward_shadow as fs  # noqa: E402  (read-only helpers: calendar, deadline, ledger io, PIT snapshot)
import kline_quality_gate as kq  # noqa: E402

TZ = fs.TZ
rp, rl = fs.rp, fs.rl
SPEC_PATH = ROOT / "contracts/forward_shadow_multi_v1.json"
FS_V1 = ROOT / "data/forward_shadow"
XJ = ROOT / "data/forward_shadow/x_jump_ms_v1"
OUT = ROOT / "data/forward_shadow/multi_v1"
H = 5
SIDES = {"9.6bp": 0.00096, "15.1bp": 0.00151, "20bp": 0.0020}
SKIP = 6                     # research convention ex[6:]: drop the first 6 sessions after the anchor
ANN_WAIT = timedelta(minutes=60)   # before deadline-60min, wait for the x_jump_ms_v1 announcement fetch
TOPK_KEEP = 400              # topk_cap4: ranked candidates stored per decision
BOOKS = {
    # name: (signal column, enter pct, hold pct, kind, needs_ann_store, judged)
    "ew6_retain_15_30": ("comp_rank_ew6", 0.15, 0.30, "pct", False, True),
    "rebal_15": ("comp_rank_ew6", 0.15, 0.15, "pct", False, True),
    "aegis_micro5p_10_30": ("aegis_micro_5p", 0.10, 0.30, "pct", False, True),
    "aegis_core7p_10_30": ("aegis_core_7p", 0.10, 0.30, "pct", True, True),
    "aegis_core7p_top30_h90_cap4": ("aegis_core_7p", 30, 90, "topk_cap4", True, False),
}
CAP = 4

SPEC = {
    "spec_id": "forward_shadow_multi_v1",
    "docs": ["factor_factory/research/offense_v0/notes/aegis_v1_redteam_audit.md",
             "factor_factory/research/offense_v0/notes/mainsys_retain_forward_change_draft.md"],
    "supersedes": "forward_shadow_retain_v1 (approved 2026-11-17, never deployed)",
    "universe": "rows of forward_shadow_v1 scores_<D>.parquet minus STAR (688/689) and BSE (.BJ, 8*, 4*, 92*), no switch",
    "signals": {"comp_rank_ew6": "forward_shadow_v1 scores_<D> column, not recomputed",
                "aegis_micro_5p": "scripts/aegis_scorer.py (RESEARCH_SPEC_aegis_alpha_v1 Arm-1, min_valid 4)",
                "aegis_core_7p": "scripts/aegis_scorer.py (Arm-2, min_valid 5, PIT side='right')",
                "cgo_outage": "cyq_perf ffill(limit=5); beyond 5 sessions the cgo pillar is missing (no substitute)",
                "rank": "pct rank over universe rows with a finite score"},
    "books": {k: {"signal": v[0], "enter": v[1], "hold": v[2], "kind": v[3], "needs_ann_store": v[4],
                  "judged": v[5]} for k, v in BOOKS.items()},
    "book_rules": {"pct": "kept = held & pct >= 1-hold; size = #(pct >= 1-enter & buyable); fill with the best unheld "
                          "entry names; equal weight 1/N on every rebalance (pre-registered v1.0 rule)",
                   "topk_cap4": "descriptive only (parameters chosen after the confirmation read): size 30, keep held "
                                "names ranked <= 90, fill by rank with <= 4 names per SW L1 industry, equal weight; "
                                "decision stores the top 400 ranked names + industries of (top 400 | hold zone)",
                   "missed_rebalance": "a sub-book whose signal night has no decision keeps its old book and is "
                                       "non-official until its next decided rebalance",
                   "bench_ew": "equal weight of universe(D-1), daily, suspended = 0"},
    "rhythm": "5 staggered sub-books; sub-book k rebalances when (official sessions since anchor) % 5 == k; book "
              "return = mean of the 5 sub-book daily returns",
    "execution": "forward_shadow_v1 D4: T+1 open; buy blocked if not traded or open >= limit-up - 0.001; sell blocked if "
                 "not traded or open <= limit-down + 0.001 (held, sold at the first sellable open, cash until next "
                 "rebalance); prices = stk_factor OHLCV first, daily_<d> fills missing codes",
    "costs_one_way": SIDES,
    "official": "written before T+1 09:30 Asia/Shanghai, clock re-read after scoring right before the write; deadline "
                "not computable -> false; universe from proxy -> false; K-line gate fail (incl. NaN prices on traded "
                "rows) -> no decision; Core7P books also need x_jump_ms_v1 ann fetch ok for D or D-1, and no decision "
                "is written while x_jump_ms_v1 runs or (until deadline - 60 min) while that fetch is not ok; every "
                "decision with any non-official book gets a gaps.jsonl row (late / non_official_inputs / core7p_ann_stale)",
    "schedule": "Hermes weekdays 23:50 --today-session (+ parity of the previous session), 07:40 and 08:55 "
                "--prev-session after the A2 pre-market self-heal, 09:35 --prev-session cut-off; ops/hermes/stock/"
                "forward_shadow_multi.sh",
    "continuous_track": "all decisions drive the paper portfolio; reported separately; never re-decided",
    "evaluation": {
        "primary_1": "aegis_core7p_10_30 - ew6_retain_15_30 daily net (9.6bp), Newey-West t lag 5, official days",
        "primary_2": "ew6_retain_15_30 - rebal_15 (approved retain rule): pass = diff > 0 & t >= 1.5 & turnover ratio "
                     "<= 0.7 at 2027-11-30; close = diff <= 0 & t <= -1; else continue to 2028-11-30",
        "secondary": "aegis_micro5p_10_30 - ew6_retain_15_30; each Aegis book vs bench_ew; 15.1bp and 20bp versions",
        "power_note": "sd of the daily-diff series ~8.6%/yr (dev); a true +6%/yr gives expected t ~0.7 after 1 year and "
                      "~1.0 after 2 years, so the looks mainly detect harm; passing a boundary early is unlikely",
        "sequential_looks_primary_1": {"2027-05-31": 3.46, "2027-11-30": 2.45, "2028-05-31": 2.00, "2028-11-30": 1.73},
        "futility": "at any look from 2027-11-30: t <= -1.0 -> close the Aegis book",
        "before_first_look": "status and counts only (no return statistics printed)",
        "warm_up": "the first 6 sessions after the anchor are excluded (research ex[6:])",
        "reported": "9.6bp / 15.1bp / 20bp; official days; official days without cgo-degraded days; one-way annual "
                    "turnover per book and the retain / rebal turnover ratio",
        "degraded_days": "report with and without cgo-degraded days",
    },
    "burn_in_for_today": "15 valid official sessions (gaps do not reset the count): official, no proxy, K-line gate ok, "
                         "parity Spearman >= 0.999 between the stored score and a next-day recompute",
    "writes": "factor_factory/data/forward_shadow/multi_v1/ only",
}


def spec_hash() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()[:16]


def paths(test: bool) -> dict[str, Path]:
    base = OUT / "test" if test else OUT
    for sub in ("", "scores", "runs"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return {"base": base, "decisions": base / "decisions.jsonl", "gaps": base / "gaps.jsonl",
            "parity": base / "parity.jsonl", "daily": base / "daily.parquet", "state": base / "state.json",
            "scores": base / "scores"}


def board_ok(codes) -> np.ndarray:
    idx = pd.Index([str(c) for c in codes])
    bad = idx.str.endswith(".BJ") | idx.str.startswith(("688", "689", "8", "4", "92"))
    return np.asarray(~bad & idx.str.endswith((".SH", ".SZ")))


# ---------------------------------------------------------------- inputs

def v1_recorded(d: str) -> dict | None:
    return next((b for b in fs.read_jsonl(FS_V1 / "books.jsonl") if b.get("signal_date") == d), None)


def ann_fetch_ok(d: str, prev: str | None) -> bool:
    for r in fs.read_jsonl(XJ / "signals.jsonl"):
        if r.get("signal_date") in (d, prev) and (r.get("ann_fetch") or {}).get("ok") is True:
            return True
    return False


def x_jump_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "x_jump_ms_shadow.py"], capture_output=True).returncode == 0
    except OSError:
        return False


def day_prices(d: str) -> pd.DataFrame | None:
    """stk_factor OHLCV first, daily_<d> fills codes stk_factor lacks."""
    cols = ["ts_code", "open", "high", "low", "close", "pre_close", "vol"]
    fr = []
    for name in (f"stk_factor_{d}.parquet", f"daily_{d}.parquet"):
        p = rp.BACKTEST_CACHE / name
        if p.exists():
            f = pd.read_parquet(p)
            f = f[[c for c in cols if c in f.columns]].copy()
            f["ts_code"] = f["ts_code"].astype(str)
            fr.append(f)
    if not fr:
        return None
    x = pd.concat(fr, ignore_index=True).drop_duplicates("ts_code", keep="first")
    return x[board_ok(x.ts_code)].set_index("ts_code")


def score_day(d: str, P: dict) -> tuple[pd.DataFrame, dict]:
    import aegis_scorer as ae
    cfg = ae.ScorerConfig(workspace=ROOT.parent)
    v1 = pd.read_parquet(FS_V1 / "scores" / f"scores_{d}.parquet")
    v1["ts_code"] = v1["ts_code"].astype(str)
    v1 = v1[board_ok(v1.ts_code)]
    sc, _, prov = ae.score_cross_section(d, cfg)
    df = v1[["ts_code", "comp_rank_ew6"]].merge(sc[["ts_code", "aegis_micro_5p", "aegis_core_7p", "ind_sw1_name"]],
                                                on="ts_code", how="left")
    prov["universe_rows_v1"] = int(len(v1))
    prov["aegis_rows_matched"] = int(df.aegis_micro_5p.notna().sum())
    df.to_parquet(P["scores"] / f"multi_scores_{d}.parquet", index=False)
    return df, prov


# ---------------------------------------------------------------- decisions

def book_lists(df: pd.DataFrame, book: str) -> dict:
    col, ent, hld, kind, _, _ = BOOKS[book]
    s = df.set_index("ts_code")[col].astype(float)
    s = s[np.isfinite(s)]
    pct = s.rank(pct=True)
    order = pct.sort_values(ascending=False, kind="mergesort")
    if kind == "pct":
        entry = order[order >= 1 - ent - 1e-12].index.tolist()
        hold = order[order >= 1 - hld - 1e-12].index.tolist()
        return {"entry": entry, "hold": sorted(hold)}
    ind = df.set_index("ts_code")["ind_sw1_name"].reindex(order.index).fillna("UNKNOWN")
    entry = order.index[:TOPK_KEEP].tolist()
    hold = sorted(order.index[:hld].tolist())
    return {"entry": entry, "hold": hold, "size": ent, "industry": {c: ind[c] for c in sorted(set(entry) | set(hold))}}


# ---------------------------------------------------------------- settlement (deterministic replay)

def day_quotes(d: str, st: set) -> dict[str, tuple]:
    """code -> (r_po, r_oc, r_cc, can_buy, can_sell) for traded codes on d (D4 limit rules)."""
    f = day_prices(d)
    if f is None:
        return {}
    f = f[(f.vol > 0) & (f.open > 0) & (f.close > 0) & (f.pre_close > 0)]
    codes = pd.Index(f.index)
    lim = np.where(codes.str.startswith(("300", "301")), 0.20, np.where(codes.isin(list(st)), 0.05, 0.10))
    up = np.round(f.pre_close.values * (1 + lim) + 1e-9, 2)
    dn = np.round(f.pre_close.values * (1 - lim) + 1e-9, 2)
    po = f.open.values / f.pre_close.values - 1
    oc = f.close.values / f.open.values - 1
    cc = f.close.values / f.pre_close.values - 1
    buy = f.open.values < up - 0.001
    sell = f.open.values > dn + 0.001
    return {c: (po[i], oc[i], cc[i], bool(buy[i]), bool(sell[i])) for i, c in enumerate(codes)}


def _st(d: str) -> set:
    try:
        snap, _ = rp.load_pit_snapshot(d)
    except Exception:  # noqa: BLE001
        snap = None
    return set(snap.loc[snap["is_st"].astype(bool), "ts_code"].astype(str)) if snap is not None else set()


def _grow(w: dict, q: dict, field: int) -> dict:
    return {c: v * (1 + (q[c][field] if c in q else 0.0)) for c, v in w.items()}


def _targets(book: str, L: dict, held_keep: set, q: dict) -> list:
    """pct books: research rule (size = #buyable entry names; all kept names stay); topk: size + industry cap."""
    if BOOKS[book][3] == "pct":
        buyable = [c for c in L["entry"] if c in q and q[c][3]]
        new = list(held_keep)
        slots = max(len(buyable) - len(held_keep), 0)
        for c in buyable:
            if slots <= 0:
                break
            if c not in held_keep:
                new.append(c)
                slots -= 1
        return new
    ind, new, cnt = L["industry"], list(held_keep), {}
    for c in new:
        g = ind.get(c, "UNKNOWN"); cnt[g] = cnt.get(g, 0) + 1
    for c in L["entry"]:
        if len(new) >= L["size"]:
            break
        g = ind.get(c, "UNKNOWN")
        if c in held_keep or cnt.get(g, 0) >= CAP or not (c in q and q[c][3]):
            continue
        new.append(c); cnt[g] = cnt.get(g, 0) + 1
    return new


def replay(decisions: list[dict], cal: list[str], upto: str) -> pd.DataFrame:
    """Deterministic paper replay of every decision (official or not) with D4 execution, 1/N on rebalance."""
    if not decisions:
        return pd.DataFrame()
    anchor = decisions[0]["anchor"]
    sess = [d for d in cal if anchor <= d <= upto]
    ix = {d: i for i, d in enumerate(sess)}
    Q = {d: day_quotes(d, _st(d)) for d in sess[1:]}
    uni = {dec["signal_date"]: dec.get("universe", []) for dec in decisions}
    by_exec: dict[int, dict[int, dict]] = {}
    for dec in decisions:
        i = ix.get(dec["signal_date"])
        if i is not None and i + 1 < len(sess):
            by_exec.setdefault(i + 1, {})[dec["subbook"]] = dec
    rows = []
    last_u: list = []
    for i in range(1, len(sess)):
        d, prev = sess[i], sess[i - 1]
        u = uni.get(prev) or last_u          # continuous track: last known universe on a gap day
        if uni.get(prev):
            last_u = uni[prev]
        if u:
            q = Q[d]
            r = float(np.mean([q[c][2] if c in q else 0.0 for c in u]))
            rows.append({"book": "bench_ew", "date": d, **{f"ret_{t}": r for t in SIDES}, "turnover": 0.0,
                         "n_hold": len(u), "official_day": prev in uni, "degraded_day": False})
    for book in BOOKS:
        acc: dict[str, dict] = {}
        for tag, side in SIDES.items():
            sub = {k: {"w": {}, "cash": 1.0, "pend": set(), "official": False, "degraded": False, "live": False}
                   for k in range(H)}
            for i in range(1, len(sess)):
                d = sess[i]
                q = Q[d]
                todays = by_exec.get(i, {})
                due_k = (i - 1) % H        # sub-book whose signal date is sess[i-1]
                rets, to, nh, offi, degr = [], 0.0, 0.0, True, False
                for k, sb in sub.items():
                    V0 = sum(sb["w"].values()) + sb["cash"]
                    dec = todays.get(k)
                    if dec is not None and book in dec["books"]:
                        L = dec["books"][book]
                        hold = set(L["hold"])
                        w = _grow(sb["w"], q, 0)
                        V = sum(w.values()) + sb["cash"]
                        held_keep = {c for c in w if c in hold and c not in sb["pend"]}
                        newset = set(_targets(book, L, held_keep, q))
                        blocked = {c for c in w if c not in newset and not (c in q and q[c][4])}
                        avail = V - sum(w[c] for c in blocked)
                        tgt = {c: w[c] for c in blocked}
                        for c in newset:
                            tgt[c] = avail / len(newset)
                        amt = sum(abs(tgt.get(c, 0.0) - w.get(c, 0.0)) for c in set(tgt) | set(w))
                        cost = side * amt
                        if newset:
                            for c in newset:
                                tgt[c] -= cost / len(newset)
                            sb["cash"] = 0.0
                        else:
                            sb["cash"] = avail - cost
                        sb["w"] = {c: v for c, v in _grow(tgt, q, 1).items() if v > 0}
                        sb["pend"] = blocked
                        sb["official"] = bool(dec["official_books"].get(book, False))
                        sb["degraded"] = bool(dec.get("degraded", False))
                        sb["live"] = True
                        to += (amt / 2) / V if V > 0 else 0.0
                    elif sb["live"]:
                        if k == due_k:     # missed rebalance (gap night): holds old book, never official
                            sb["official"] = False
                        ex = {c for c in sb["pend"] if c in q and q[c][4]}
                        if ex:
                            w = _grow(sb["w"], q, 0)
                            proceeds = sum(w[c] for c in ex)
                            sb["cash"] += proceeds * (1 - side)
                            to += (proceeds / 2) / (sum(w.values()) + sb["cash"] - proceeds * (1 - side))
                            for c in ex:
                                del w[c]
                            sb["pend"] -= ex
                            sb["w"] = _grow(w, q, 1)
                        else:
                            sb["w"] = _grow(sb["w"], q, 2)
                    if sb["live"]:
                        rets.append((sum(sb["w"].values()) + sb["cash"]) / V0 - 1)
                        nh += len(sb["w"])
                        offi &= sb["official"]
                        degr |= sb["degraded"]
                if len(rets) == H:
                    a = acc.setdefault(d, {"book": book, "date": d, "turnover": to / H, "n_hold": nh / H,
                                           "official_day": bool(offi), "degraded_day": bool(degr)})
                    a[f"ret_{tag}"] = float(np.mean(rets))
        rows.extend(acc.values())
    return pd.DataFrame(rows).sort_values(["book", "date"]).reset_index(drop=True) if rows else pd.DataFrame()


# ---------------------------------------------------------------- main run

def official_base(test: bool, late: bool, prov: dict, v1: dict | None) -> bool:
    """Fail-closed: test runs, late runs (incl. deadline not computable), any proxy universe or a non-official
    forward_shadow_v1 record make the decision non-official."""
    return bool((not test) and (not late) and prov.get("universe_degraded_mode") == "none"
                and v1 is not None and v1.get("official") is True)


def is_late(deadline, now) -> bool:
    return deadline is None or now >= deadline


def should_wait_ann(test: bool, late: bool, xj_running: bool, ann_ok: bool, deadline, now) -> bool:
    """Do not write a decision while x_jump_ms_v1 is still running, nor (until deadline - 60 min) while its
    announcement fetch for D / D-1 has not succeeded: an early write would lock Core7P non-official for 5 sessions."""
    if test or late:
        return False
    return bool(xj_running or (not ann_ok and deadline - now > ANN_WAIT))


def parity_ok(spearman: dict, n: int) -> bool:
    return bool(n >= 100 and spearman and all(np.isfinite(v) and v >= 0.999 for v in spearman.values()))


def run(d: str | None, test: bool) -> dict:
    if not test:
        saved = json.loads(SPEC_PATH.read_text()) if SPEC_PATH.exists() else {}
        if saved.get("spec_hash") != spec_hash() or not fs.committed_and_clean(SPEC_PATH):
            raise SystemExit("forward_shadow_multi_v1 spec missing, changed or uncommitted: refusing")
    P = paths(test)
    cal = fs.official_sessions("99999999")
    if d is None:
        raise SystemExit("--date, --today-session or --prev-session is required")
    if d not in cal:
        raise SystemExit(f"{d} is not an official session")
    decisions = fs.read_jsonl(P["decisions"])
    gaps = fs.read_jsonl(P["gaps"])
    now = datetime.now(TZ)
    after = [x for x in cal if x > d]
    deadline = fs.entry_deadline(after)
    late = is_late(deadline, now)
    if any(x["signal_date"] == d for x in decisions):
        return {"signal_date": d, "status": "already_decided", **settle(P, cal, d)}
    prev = ([x for x in cal if x < d] or [None])[-1]
    v1 = v1_recorded(d)
    ready = v1 is not None and (FS_V1 / "scores" / f"scores_{d}.parquet").exists()
    if not ready:
        if late and not test and not any(g["signal_date"] == d for g in gaps):
            fs.append(P["gaps"], {"signal_date": d, "reason": "forward_shadow_v1 not recorded by T+1 09:30",
                                  "at": now.isoformat(), "spec_hash": spec_hash()})
            return {"signal_date": d, "status": "gap_logged"}
        return {"signal_date": d, "status": "waiting_for_forward_shadow_v1"}
    ann_ok = ann_fetch_ok(d, prev)
    if should_wait_ann(test, late, x_jump_running(), ann_ok, deadline, now):
        return {"signal_date": d, "status": "waiting_for_x_jump_ms_ann_fetch", "ann_fetch_ok": ann_ok}
    px = day_prices(d)
    gate = kq.check_day(px, pd.read_parquet(FS_V1 / "scores" / f"scores_{d}.parquet")["ts_code"].astype(str))
    if not gate["ok"]:
        if late and not test and not any(g["signal_date"] == d for g in gaps):
            fs.append(P["gaps"], {"signal_date": d, "reason": "kline_gate_failed", "gate": gate, "at": now.isoformat()})
        return {"signal_date": d, "status": "kline_gate_failed", "gate": gate}
    df, prov = score_day(d, P)
    anchor = decisions[0]["anchor"] if decisions else d
    k = (len([x for x in cal if anchor <= x <= d]) - 1) % H
    now = datetime.now(TZ)                  # re-read the clock after scoring (15-25 s), right before writing
    late = is_late(deadline, now)
    ann_ok = ann_fetch_ok(d, prev)
    base_official = official_base(test, late, prov, v1)
    rec = {"signal_date": d, "anchor": anchor, "subbook": k, "recorded_at": now.isoformat(),
           "entry_deadline": deadline.isoformat() if deadline else None, "late": bool(late), "spec_hash": spec_hash(),
           "code_commit": rp.git_commit(), "universe": df.ts_code.tolist(), "kline_gate": gate,
           "degraded": prov.get("degraded_mode") != "none", "scorer": {x: prov.get(x) for x in (
               "degraded_mode", "universe_degraded_mode", "cyq_absent_days", "micro_5p_coverage", "core_7p_coverage",
               "aegis_rows_matched", "universe_rows_v1", "scores_sha256_16", "ann_store")},
           "ann_fetch_ok": ann_ok, "books": {}, "official_books": {}}
    for b, spec in BOOKS.items():
        rec["books"][b] = book_lists(df, b)
        rec["official_books"][b] = bool(base_official and (ann_ok or not spec[4]))
    rec["official"] = bool(base_official)
    fs.append(P["decisions"], rec)
    if not all(rec["official_books"].values()) and not test:
        reason = ("late" if late else "non_official_inputs") if not rec["official"] else "core7p_ann_stale"
        fs.append(P["gaps"], {"signal_date": d, "reason": reason, "official_books": rec["official_books"],
                              "at": now.isoformat(), "spec_hash": spec_hash()})
    out = {"signal_date": d, "status": "decided", "subbook": k, "official": rec["official"],
           "official_books": rec["official_books"], "degraded": rec["degraded"], **settle(P, cal, d)}
    (P["base"] / "runs" / f"run_{d}.json").write_text(json.dumps({**out, "at": now.isoformat()}, ensure_ascii=False, indent=1))
    P["state"].write_text(json.dumps({"last_signal_date": d, "updated_at": now.isoformat(), **out}, ensure_ascii=False, indent=1))
    return out


def settle(P: dict, cal: list[str], upto: str) -> dict:
    decs = fs.read_jsonl(P["decisions"])
    df = replay(decs, cal, upto)
    if not df.empty:
        df.to_parquet(P["daily"], index=False)
    return {"settled_days": int(df.date.nunique()) if not df.empty else 0, "decisions": len(decs),
            "official_decisions": sum(1 for x in decs if x.get("official"))}


# ---------------------------------------------------------------- evaluation

def nw_t(x: np.ndarray, lag: int = 5) -> float:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return float("nan")
    e = x - x.mean()
    s = e @ e / n
    for k in range(1, lag + 1):
        s += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / np.sqrt(s / n)) if s > 0 else float("nan")


def _ann(x: pd.Series) -> float | None:
    return round(float(x.mean()) * 244 * 100, 2) if len(x) else None


def summary(test: bool, today: str | None = None) -> dict:
    P = paths(test)
    decs = fs.read_jsonl(P["decisions"])
    out = {"spec_hash": spec_hash(), "decisions": len(decs), "official": sum(1 for x in decs if x.get("official")),
           "gaps": len(fs.read_jsonl(P["gaps"])), "degraded_decisions": sum(1 for x in decs if x.get("degraded"))}
    today = today or datetime.now(TZ).strftime("%Y-%m-%d")
    looks = SPEC["evaluation"]["sequential_looks_primary_1"]
    due = [k for k in looks if k <= today]
    if not P["daily"].exists() or not decs:
        return out
    df = pd.read_parquet(P["daily"])
    cal = fs.official_sessions("99999999")
    anchor = decs[0]["anchor"]
    skip = set([x for x in cal if x > anchor][:SKIP])           # first 6 settled sessions after the anchor
    df = df[~df.date.isin(skip)]
    out["settled_days"] = int(df.date.nunique())
    if not due and not test:
        out["judgement"] = "before the first look date: counts only"
        return out
    offp = df.pivot(index="date", columns="book", values="official_day").fillna(False).astype(bool)
    degp = df.pivot(index="date", columns="book", values="degraded_day").fillna(False).astype(bool)
    res = {}
    for a, b in (("aegis_core7p_10_30", "ew6_retain_15_30"), ("ew6_retain_15_30", "rebal_15"),
                 ("aegis_micro5p_10_30", "ew6_retain_15_30"), ("aegis_core7p_10_30", "bench_ew"),
                 ("aegis_micro5p_10_30", "bench_ew")):
        r = {}
        for tag in SIDES:
            piv = df.pivot(index="date", columns="book", values=f"ret_{tag}")
            if a not in piv or b not in piv:
                continue
            dd = (piv[a] - piv[b]).dropna()
            om = (offp[a] & offp[b]).reindex(dd.index).fillna(False).astype(bool)
            nd = ~(degp[a] | degp[b]).reindex(dd.index).fillna(False).astype(bool)
            o = dd[om]
            r[tag] = {"all_ann_pct": _ann(dd), "all_t": round(nw_t(dd.values), 2),
                      "official_ann_pct": _ann(o), "official_t": round(nw_t(o.values), 2), "n_official": int(o.notna().sum()),
                      "official_ex_degraded_ann_pct": _ann(dd[om & nd]), "official_ex_degraded_t": round(nw_t(dd[om & nd].values), 2),
                      "n_degraded": int((~nd).sum())}
        res[f"{a}-{b}"] = r
    out["paired"] = res
    to = df.groupby("book")["turnover"].mean() * 244
    out["turnover_ann_one_way"] = {k: round(float(v), 2) for k, v in to.items() if k != "bench_ew"}
    if "ew6_retain_15_30" in to and "rebal_15" in to and to["rebal_15"] > 0:
        out["turnover_ratio_retain_vs_rebal"] = round(float(to["ew6_retain_15_30"] / to["rebal_15"]), 3)
    if due:
        out["look"] = {"date": due[-1], "boundary_t": looks[due[-1]]}
    return out


def parity(d: str, test: bool) -> dict:
    P = paths(test)
    p = P["scores"] / f"multi_scores_{d}.parquet"
    if not p.exists():
        return {"signal_date": d, "status": "no_stored_scores"}
    old = pd.read_parquet(p)
    import aegis_scorer as ae
    sc, _, _ = ae.score_cross_section(d, ae.ScorerConfig(workspace=ROOT.parent))
    m = old.merge(sc[["ts_code", "aegis_micro_5p", "aegis_core_7p"]], on="ts_code", suffixes=("", "_re"))
    r = {c: round(float(m[[c, c + "_re"]].corr(method="spearman").iloc[0, 1]), 6) for c in ("aegis_micro_5p", "aegis_core_7p")}
    rec = {"signal_date": d, "at": datetime.now(TZ).isoformat(), "spearman": r, "n": int(len(m)),
           "ok": parity_ok(r, len(m))}
    fs.append(P["parity"], rec)
    return rec


def resolve_session(today: str, which: str, cal: list[str] | None = None) -> str | None:
    """Trading-calendar resolution: 'today' -> today if it is a session; 'prev' -> the session before today, only on
    session days (weekends / holidays exit quietly; Monday morning resolves to Friday)."""
    cal = cal if cal is not None else fs.official_sessions("99999999")
    if today not in cal:
        return None
    if which == "today":
        return today
    before = [x for x in cal if x < today]
    return before[-1] if before else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-spec", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--parity")
    ap.add_argument("--today-session", action="store_true", help="signal date = today if today is a session")
    ap.add_argument("--prev-session", action="store_true", help="signal date = last session before today (today must be a session)")
    ap.add_argument("--parity-prev", action="store_true", help="with --today-session: also run parity for the previous session")
    a = ap.parse_args(argv)
    if a.today_session or a.prev_session:
        d = resolve_session(datetime.now(TZ).strftime("%Y%m%d"), "today" if a.today_session else "prev")
        if d is None:
            print(json.dumps({"status": "not_a_session_today"}))
            return 0
        a.date = d
    if a.write_spec:
        SPEC_PATH.write_text(json.dumps({**SPEC, "spec_hash": spec_hash(), "frozen_at": datetime.now(TZ).isoformat()},
                                        ensure_ascii=False, indent=2))
        print(f"wrote {SPEC_PATH} hash {spec_hash()}")
        return 0
    if a.summary:
        print(json.dumps(summary(a.test), ensure_ascii=False, indent=1))
        return 0
    if a.parity:
        print(json.dumps(parity(a.parity, a.test), ensure_ascii=False))
        return 0
    print(json.dumps(run(a.date, a.test), ensure_ascii=False, default=str))
    if a.parity_prev and a.today_session:
        prev = resolve_session(a.date, "prev")
        if prev:
            print(json.dumps(parity(prev, a.test), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
