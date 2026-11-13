#!/usr/bin/env python3
"""每日滚动分组前瞻监控（forward_shadow_cohort_v1）：每个官方信号日 D 新开一组、持有 5 个交易日。

只读生产缓存与 v1 runner 当晚写出的 scores_<D>.parquet；只写
factor_factory/data/forward_shadow/cohort_ledger.jsonl 与 factor_factory/outputs/forward_shadow/cohort_monitor.json。

  record  23:30 任务在 v1 runner 记录成功后调用：把 D 的 cohort_top20 / cohort_top100 / 后 30% 剔除名单 /
          universe 追加进台账（append-only；一个信号日只记一次；晚于 E1 09:30 的行标 official=false，不计入统计）
  settle  逐组结算 T+1 / T+5：E1 开盘入场（涨停开盘买不进的剔除），E1 收盘或 E5 收盘出场（停牌按最后成交收盘，
          跌停收盘标 blocked_exit），扣 0.3% 往返；对照同组 bench_ew；汇总 NW t、环境分组、滚动均值、
          相邻组重叠率、5 份资金滚动曲线与冲击压力，并按预注册口径给出 verdict

规格冻结于 contracts/forward_shadow_cohort_v1.json（hash 不符即拒绝运行）。与 forward_shadow_v1 并行，不修改 v1。
只前瞻：不回补冻结前或入场开盘已过的任何信号日。

  python cohort_monitor.py record --date 20261020
  python cohort_monitor.py settle
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_impact as ci  # noqa: E402
import tier_monitor as tm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FS = ROOT / "data/forward_shadow"
OUT = ROOT / "outputs/forward_shadow"
LEDGER = FS / "cohort_ledger.jsonl"
RESULT = OUT / "cohort_monitor.json"
SPEC_PATH = ROOT / "contracts/forward_shadow_cohort_v1.json"

# Constants are the frozen spec's numbers (test_cohort_monitor asserts they still match the spec text).
HOLD = 5
HORIZONS = (1, HOLD)
NW_LAG = HOLD - 1
ROUND_TRIP_COST = 0.003
TOP20 = (20, 4)      # (names, max per industry)
TOP100 = (100, 15)
DROP = 0.30
MIN_COHORTS_VERDICT = 120
INTERIM_COHORTS = 60
T_THRESHOLD = 2.0
ROLLING = 20
SLEEVES = HOLD
IMPACT_CAPITALS = (1e6, 5e6)
BOOKS = ("cohort_top20", "cohort_top100", "cohort_avoid70", "bench_ew")
PRIMARY = "cohort_top100"


def load_spec(path: Path = SPEC_PATH) -> dict:
    raw = json.loads(path.read_text())
    body = {k: v for k, v in raw.items() if k not in ("spec_hash", "frozen_at")}
    h = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    if h != raw.get("spec_hash"):
        raise SystemExit(f"cohort spec changed after freezing ({h} != {raw.get('spec_hash')}): refusing to run")
    return raw


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


# ---------------------------------------------------------------- record

def pick_top(scores: pd.DataFrame, k: int, cap: int) -> list[dict]:
    """Top-k by composite (desc, ties by code), at most `cap` names per industry; skipped names are replaced
    by the next eligible one."""
    s = scores.dropna(subset=["comp_rank_ew6"]).sort_values(["comp_rank_ew6", "ts_code"], ascending=[False, True])
    out, per_ind = [], {}
    for rank, r in enumerate(s.itertuples(index=False), start=1):
        ind = str(r.industry) if pd.notna(r.industry) else "UNKNOWN"
        if per_ind.get(ind, 0) >= cap:
            continue
        per_ind[ind] = per_ind.get(ind, 0) + 1
        out.append({"code": r.ts_code, "industry": ind, "composite": round(float(r.comp_rank_ew6), 6), "rank": rank})
        if len(out) == k:
            break
    return out


def bottom_excluded(scores: pd.DataFrame) -> list[str]:
    pct = scores.set_index("ts_code")["comp_rank_ew6"].rank(pct=True)
    return sorted(pct.index[pct.notna() & (pct <= DROP)])


def record(d: str, now: datetime | None = None, spec: dict | None = None) -> str:
    """Append cohort D. Returns 'recorded' | 'late' | 'exists' | 'before_start' | 'no_v1_record' | 'no_scores'."""
    spec = spec or load_spec()
    if any(r["signal_date"] == d for r in read_jsonl(LEDGER)):
        return "exists"
    v1 = next((b for b in read_jsonl(FS / "books.jsonl") if b["signal_date"] == d and b.get("official")), None)
    if v1 is None:
        return "no_v1_record"
    if datetime.fromisoformat(v1["recorded_at"]) < datetime.fromisoformat(spec["frozen_at"]):
        return "before_start"  # spec: starts at the first official signal recorded after freezing
    sp = FS / "scores" / f"scores_{d}.parquet"
    if not sp.exists():
        return "no_scores"
    scores = pd.read_parquet(sp, columns=["ts_code", "industry", "comp_rank_ew6"])
    now = now or datetime.now().astimezone()
    deadline = v1.get("entry_deadline")
    late = bool(deadline and now >= datetime.fromisoformat(deadline))
    rec = {"signal_date": d, "recorded_at": now.isoformat(), "entry_deadline": deadline,
           "official": not late, "late": late, "spec_hash": spec["spec_hash"], "fs_spec_hash": v1.get("spec_hash"),
           "universe": sorted(scores["ts_code"]), "bottom30": bottom_excluded(scores),
           "cohort_top20": pick_top(scores, *TOP20), "cohort_top100": pick_top(scores, *TOP100)}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return "late" if late else "recorded"


def book_codes(r: dict) -> dict[str, list[str]]:
    excl = set(r["bottom30"])
    return {"cohort_top20": [m["code"] for m in r["cohort_top20"]],
            "cohort_top100": [m["code"] for m in r["cohort_top100"]],
            "cohort_avoid70": [c for c in r["universe"] if c not in excl],
            "bench_ew": list(r["universe"])}


# ---------------------------------------------------------------- pure math

def book_stats(cohorts: list[dict], h: int) -> dict:
    out = {}
    for b in BOOKS:
        pairs = [(c["ret"][b], c["ret"]["bench_ew"]) for c in cohorts
                 if c["ret"].get(b) is not None and c["ret"].get("bench_ew") is not None]
        ex = [a - e for a, e in pairs]
        out[b] = {"cohorts": len(pairs),
                  "mean_net": _f(np.mean([a for a, _ in pairs])) if pairs else None,
                  "mean_excess": _f(np.mean(ex)) if ex and b != "bench_ew" else None,
                  "hit_excess": _f(np.mean([x > 0 for x in ex])) if ex and b != "bench_ew" else None,
                  "nw_t_excess": _f(tm.nw_t(ex, max(h - 1, 0))) if b != "bench_ew" else None,
                  "nw_lag": max(h - 1, 0)}
    return out


def regime_split(cohorts: list[dict]) -> dict:
    out = {}
    for name, cond in (("up", lambda x: x > 0), ("down", lambda x: x <= 0)):
        ex = [c["ret"][PRIMARY] - c["ret"]["bench_ew"] for c in cohorts
              if c["ret"].get(PRIMARY) is not None and c["ret"].get("bench_ew") is not None and cond(c["ret"]["bench_ew"])]
        out[name] = {"cohorts": len(ex), "mean_excess": _f(np.mean(ex)) if ex else None}
    return out


def rolling_mean(cohorts: list[dict], window: int = ROLLING) -> list[dict]:
    ex = [(c["signal_date"], c["ret"][PRIMARY] - c["ret"]["bench_ew"]) for c in cohorts
          if c["ret"].get(PRIMARY) is not None and c["ret"].get("bench_ew") is not None]
    return [{"signal_date": d, "mean_excess": _f(np.mean([x for _, x in ex[i - window + 1: i + 1]]))}
            for i, (d, _) in enumerate(ex) if i + 1 >= window]


def sleeve_curve(cohorts: list[dict], session_index: dict[str, int]) -> list[dict]:
    """5 sleeves, each 1/5 of capital; the cohort signalled at session i goes to sleeve i % 5 and compounds there
    for its 5-day window (cohort i enters at i+1 open and exits at i+5 close, so cohort i+5 of the same sleeve
    starts at the next open: windows tile exactly). A missing cohort leaves its sleeve in cash (not reassigned)."""
    nav = {b: [1.0 / SLEEVES] * SLEEVES for b in (PRIMARY, "bench_ew")}
    out = []
    for c in sorted(cohorts, key=lambda c: c["exit"]):
        j = session_index[c["signal_date"]] % SLEEVES
        for b in nav:
            r = c["ret"].get(b)
            if r is not None:
                nav[b][j] *= 1 + r
        out.append({"date": c["exit"], "sleeve_nav": _f(sum(nav[PRIMARY])), "bench_nav": _f(sum(nav["bench_ew"])),
                    "excess": _f(sum(nav[PRIMARY]) - sum(nav["bench_ew"]))})
    return out


def overlap(rows: list[dict], sessions: list[str]) -> list[dict]:
    """Share of cohort_top100 names already in the previous session's cohort (consecutive official sessions only)."""
    by = {r["signal_date"]: set(book_codes(r)[PRIMARY]) for r in rows}
    out = []
    for a, b in zip(sessions, sessions[1:]):
        if a in by and b in by and by[b]:
            out.append({"signal_date": b, "overlap": _f(len(by[a] & by[b]) / len(by[b]))})
    return out


def verdict(primary: dict) -> dict:
    n = primary["cohorts"]
    if n < MIN_COHORTS_VERDICT:
        stage = "interim_snapshot" if n >= INTERIM_COHORTS else "insufficient_sample"
        return {"status": stage, "cohorts": n, "required": MIN_COHORTS_VERDICT,
                "message": f"cohort_top100 已结算 T+5 官方组 {n}/{MIN_COHORTS_VERDICT}，未达预注册样本量，不下结论"
                           + ("（已到 60 组中期快照，只报告不判定）" if stage == "interim_snapshot" else "")}
    m, t = primary["mean_excess"], primary["nw_t_excess"]
    ok = m is not None and m > 0 and t is not None and t >= T_THRESHOLD
    return {"status": "pass" if ok else "fail", "cohorts": n, "required": MIN_COHORTS_VERDICT,
            "message": f"按预注册口径：cohort_top100 − bench_ew 均值 {m:+.4f}，NW t = {t:.2f}（需 > 0 且 ≥ {T_THRESHOLD}）"}


def _f(v) -> float | None:
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), 6)


# ---------------------------------------------------------------- settle

def settle(now: datetime | None = None, spec: dict | None = None) -> dict:
    import forward_shadow as fs  # noqa: E402
    import research_labels_v2 as rl  # noqa: E402
    import research_panel_v2 as rp  # noqa: E402

    spec = spec or load_spec()
    now = now or datetime.now().astimezone()
    rows = read_jsonl(LEDGER)
    official = sorted([r for r in rows if r.get("official")], key=lambda r: r["signal_date"])
    base = {"contract_version": "forward_shadow_cohort_v1", "generated_at": now.isoformat(),
            "spec_hash": spec["spec_hash"], "ledger_cohorts": len(rows), "official_cohorts": len(official),
            "late_excluded": sorted(r["signal_date"] for r in rows if not r.get("official"))}
    if not official:
        return {**base, "status": "collecting", "matured": {"h1": 0, "h5": 0}, "message": "台账尚无官方分组"}
    cal = fs.official_sessions("99999999")
    avail = [x for x in cal if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    first = official[0]["signal_date"]
    sess = [x for x in cal if first <= x <= avail[-1]]
    idx = {d: i for i, d in enumerate(sess)}
    cal_idx = {d: i for i, d in enumerate(cal)}
    base.update(first_signal=first, last_signal=official[-1]["signal_date"], as_of=avail[-1])
    if len(sess) < 2:
        return {**base, "status": "collecting", "matured": {"h1": 0, "h5": 0},
                "message": "尚无已结算分组（需 E1 开盘及收盘数据）"}
    w = fs.window_mats(sess)
    codes_all = list(w["open"].columns)
    O, C, PRE, traded = w["open"], w["close"], w["pre_close"], w["traded"]
    adj_open, adj_close_f = w["adj_open"], w["adj_close"].ffill()

    def limits(day: str) -> pd.Series:
        snap, _ = rp.load_pit_snapshot(day)
        st = pd.DataFrame(False, index=[day], columns=codes_all)
        if snap is not None:
            st.loc[day] = snap.set_index("ts_code")["is_st"].astype(bool).reindex(codes_all).fillna(False).to_numpy()
        return rl.limit_ratio(pd.Index(codes_all), st).iloc[0]

    per_h: dict[int, list[dict]] = {h: [] for h in HORIZONS}
    blocked = {"entry_limit_up": 0, "exit_suspended": 0, "exit_limit_down": 0}
    adv_cache: dict = {}
    stress = {c: [] for c in IMPACT_CAPITALS}
    for r in official:
        d = r["signal_date"]
        i0 = idx.get(d)
        if i0 is None or i0 + 1 >= len(sess):
            continue
        e1 = i0 + 1
        lim_e1 = limits(sess[e1])
        up = (PRE.iloc[e1] * (1 + lim_e1) + 1e-9).round(2)
        can = traded.iloc[e1] & (O.iloc[e1] < up - 0.001)
        books = book_codes(r)
        blocked["entry_limit_up"] += int((~can.reindex(books[PRIMARY]).fillna(False)).sum())
        for h in HORIZONS:
            ex = i0 + h
            if ex >= len(sess):
                continue
            net = tm.stock_returns(adj_open.iloc[e1], adj_close_f.iloc[ex], can, ROUND_TRIP_COST)
            ret = {b: tm.cohort_mean(net, codes)[0] for b, codes in books.items()}
            per_h[h].append({"signal_date": d, "entry": sess[e1], "exit": sess[ex], "ret": ret})
            if h == HOLD:
                held = [c for c in books[PRIMARY] if c in codes_all and bool(can.get(c, False))]
                lim_x = limits(sess[ex])
                down = (PRE.iloc[ex] * (1 - lim_x) - 1e-9).round(2)
                blocked["exit_suspended"] += int((~traded.iloc[ex].reindex(held).fillna(False)).sum())
                blocked["exit_limit_down"] += int((traded.iloc[ex] & (C.iloc[ex] <= down + 0.001)).reindex(held)
                                                  .fillna(False).sum())
                adv = tm.adv20(d, sess, adv_cache, rp)
                sel = net.reindex(books[PRIMARY]).dropna()
                for cap in IMPACT_CAPITALS:
                    imp = ci.round_trip_impact(cap, adv).reindex(sel.index)
                    if imp.notna().any():
                        stress[cap].append(float((sel - imp).dropna().mean()))

    stats = {f"h{h}": book_stats(per_h[h], h) for h in HORIZONS}
    primary = stats[f"h{HOLD}"][PRIMARY]
    windows = [{"signal_date": c["signal_date"], "entry": c["entry"], "exit": c["exit"],
                "window": f"{c['signal_date']} → {c['entry']}～{c['exit']}",
                **{b: _f(c["ret"].get(b)) for b in BOOKS}} for c in per_h[HOLD]]
    pending = [{"signal_date": r["signal_date"], "window_start": sess[idx[r["signal_date"]] + 1]
                if idx.get(r["signal_date"]) is not None and idx[r["signal_date"]] + 1 < len(sess) else None}
               for r in official if r["signal_date"] not in {c["signal_date"] for c in per_h[HOLD]}]
    return {**base, "status": "ok" if per_h[1] else "collecting",
            "matured": {"h1": len(per_h[1]), "h5": len(per_h[HOLD])},
            "stats": stats, "verdict": verdict(primary),
            "regime_h5": regime_split(per_h[HOLD]),
            "rolling20_h5": rolling_mean(per_h[HOLD]),
            "overlap_top100": overlap(official, [x for x in cal if first <= x <= official[-1]["signal_date"]]),
            "sleeve_curve": sleeve_curve(per_h[HOLD], cal_idx),
            "blocked": blocked,
            "impact_stress": {"note": "压力测试叠加层，只报告不判定：平方根冲击 10bp@1%ADV 双边，ADV=信号日前 20 日均成交额，"
                                      "cohort_top100 T+5",
                              "capital_per_name_yuan": list(IMPACT_CAPITALS),
                              "mean_net_after_impact": {str(int(c)): _f(np.mean(v)) if v else None
                                                        for c, v in stress.items()}},
            "windows": windows, "pending": pending}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "settle"])
    ap.add_argument("--date")
    a = ap.parse_args(argv)
    spec = load_spec()
    if a.cmd == "record":
        if not a.date:
            raise SystemExit("record needs --date")
        print(f"cohort record {a.date}: {record(a.date, spec=spec)}")
        return 0
    res = settle(spec=spec)
    OUT.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    m = res.get("matured", {})
    print(f"cohort_monitor: {res['status']} · 台账 {res['ledger_cohorts']} 组 · 已结算 T+1 {m.get('h1', 0)} / "
          f"T+5 {m.get('h5', 0)} · {res.get('verdict', {}).get('message', res.get('message', ''))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
