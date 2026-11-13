#!/usr/bin/env python3
"""Champion (v4.3) vs Challenger (v4.4) rolling replay over the last N signal days — RESEARCH BACKTEST.

20261010.  A historical replay, not forward evidence: it never writes the forward shadow ledgers,
the dual-track evaluation DB or the v4.4 hold book (selection_state/prebreakout_v44_book.json).
v4.4 runs with OPENCLAW_PREBREAKOUT_HOLD_DAYS=1 (the hold book short-circuits without touching
state) and the 5-day hold is simulated here.

Selection (point in time): for every signal day T both lists come from the production
run_strategy_suite.build_top20 path, which reads only the T-dated caches; the v4.4 veto gate uses
the last 21 daily bars <= T.  Known non-PIT inputs are disclosed in the output (current industry
map and current names for the ST filter).

Execution: enter at the T+1 open.  v4.3 rebalances every trading day; v4.4 rebalances every
`hold` signal days and holds the basket in between.  Returns are adjustment-safe:
open->open = (close_t / open_t) * (open_t+1 / pre_close_t+1); pre_close is the exchange's
ex-rights adjusted previous close.  Costs are charged on actual turnover
(cost_per_side = round_trip / 2 on every share sold and every share bought).

Outputs:
  local  stock_data/03-working/research/strategy_arena/arena_replay_<end>.json (+ _latest.json)
         includes per-day pick lists and a production-parity check (stays local)
  public --export-repo REPO -> REPO/data/latest/strategy_arena_ledger.json (aggregates only;
         no per-stock rows, so the committed public boundary stays satisfied)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

WS = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", Path.home() / ".openclaw" / "workspace"))
ANALYZER = WS / "skills" / "stock-analyzer"
OUT_DIR = WS / "stock_data" / "03-working" / "research" / "strategy_arena"
PROD_DB = WS / "stock_data" / "03-working" / "strategy_research" / "prebreakout_v41" / "prebreakout_v41.db"
TRADING_DAYS_PER_YEAR = 242


def mean(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def rnd(x, d=4):
    return None if x is None or not math.isfinite(x) else round(x, d)


# --------------------------------------------------------------------------- prices
class Prices:
    def __init__(self, cache_dir: Path, dates: list[str]):
        import pandas as pd
        self.dates = dates
        self.bar: dict[str, dict[str, tuple[float, float, float]]] = {}
        for d in dates:
            f = pd.read_parquet(cache_dir / f"daily_{d}.parquet", columns=["ts_code", "open", "close", "pre_close"])
            f = f.dropna()
            self.bar[d] = {r.ts_code: (float(r.open), float(r.close), float(r.pre_close))
                           for r in f.itertuples(index=False) if r.open > 0 and r.close > 0 and r.pre_close > 0}
        self.missing_legs = 0

    def idx(self, d):
        return self.dates.index(d)

    def intraday(self, code, d):
        b = self.bar[d].get(code)
        if not b:
            self.missing_legs += 1
            return 0.0
        return b[1] / b[0] - 1

    def gap(self, code, d):
        """pre_close(d) -> open(d): overnight leg into day d."""
        b = self.bar[d].get(code)
        if not b:
            self.missing_legs += 1
            return 0.0
        return b[0] / b[2] - 1

    def close_to_close(self, code, d):
        b = self.bar[d].get(code)
        if not b:
            self.missing_legs += 1
            return 0.0
        return b[1] / b[2] - 1

    def fwd_net(self, code, t, days, cost):
        """Net return from the open of the day after t through the close of t+days (None if not settled)."""
        i = self.idx(t)
        if i + days >= len(self.dates):
            return None
        g = 1 + self.intraday(code, self.dates[i + 1])
        for k in range(2, days + 1):
            g *= 1 + self.close_to_close(code, self.dates[i + k])
        return g - 1 - cost


# --------------------------------------------------------------------------- selection
def select_day(rss, v5gate, weights43, weights44, t, name_map, capture):
    capture.clear()
    p43 = rss.build_top20(weights43, t, name_map, {}, top_n=20, mode="prebreakout")
    p44 = rss.build_top20(weights44, t, name_map, {}, top_n=20, mode="prebreakout_v44")
    meta = dict(rss.LAST_SELECTION_META)
    gate = (meta.get("veto_gate") or {})
    return ([p["code"] for p in p43], [p["code"] for p in p44],
            {"candidates": list(capture.get("in", [])), "survivors": list(capture.get("out", [])),
             "vetoed": sorted((gate.get("vetoed_percentile") or {}).keys()),
             "gate": gate.get("gate")})


# --------------------------------------------------------------------------- portfolio
def simulate(prices: Prices, signals: list[str], baskets: dict[str, list[str]], hold: int, cost_rt: float):
    """Equal-weight book; rebalance at the open after every `hold`-th signal day; mark daily at the open
    (final day at the close).  Returns daily rows and summary."""
    dates = prices.dates
    rebal = {signals[i] for i in range(0, len(signals), hold)}
    weights: dict[str, float] = {}
    equity, rows, costs, turnovers = 1.0, [], 0.0, []
    start = prices.idx(signals[0]) + 1
    for i in range(start, len(dates)):
        day, prev_sig = dates[i], dates[i - 1]
        cost_today = 0.0
        if prev_sig in rebal and prev_sig in baskets:
            target = {c: 1.0 / len(baskets[prev_sig]) for c in baskets[prev_sig]} if baskets[prev_sig] else {}
            total = sum(weights.values()) or 1.0
            cur = {c: w / total for c, w in weights.items()}
            traded = sum(abs(target.get(c, 0) - cur.get(c, 0)) for c in set(target) | set(cur))
            cost_today = traded * cost_rt / 2
            turnovers.append(traded / 2)
            equity *= 1 - cost_today
            costs += cost_today
            weights = dict(target)
        if not weights:
            continue
        last = i == len(dates) - 1
        nxt = None if last else dates[i + 1]
        new_w = {}
        for c, w in weights.items():
            g = 1 + prices.intraday(c, day)
            if nxt:
                g *= 1 + prices.gap(c, nxt)
            new_w[c] = w * g
        gross = sum(new_w.values()) / sum(weights.values())
        weights = new_w
        equity *= gross
        rows.append({"date": day, "equity": equity, "ret": gross * (1 - cost_today) - 1, "cost": cost_today,
                     "to_close_only": last})
    rets = [r["ret"] for r in rows]
    peak, mdd = 1.0, 0.0
    for r in rows:
        peak = max(peak, r["equity"])
        mdd = min(mdd, r["equity"] / peak - 1)
    sd = stdev(rets)
    years = len(rows) / TRADING_DAYS_PER_YEAR if rows else None
    return rows, {
        "days": len(rows), "cum_net": equity - 1, "max_drawdown": mdd,
        "sharpe": (mean(rets) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)) if sd else None,
        "daily_mean": mean(rets), "daily_vol": sd, "hit_days": mean([1.0 if r > 0 else 0.0 for r in rets]),
        "total_cost": costs, "friction_annualized": (costs / years) if years else None,
        "avg_turnover_per_rebalance": mean(turnovers), "rebalances": len(turnovers),
    }


def production_parity(signals, b43):
    if not PROD_DB.exists():
        return None
    out = []
    with sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True) as con:
        for t in signals:
            rows = con.execute("select ts_code from recommendation_snapshot where recommend_date=?", (t,)).fetchall()
            prod = {r[0] for r in rows if r[0]}
            if prod:
                out.append({"date": t, "production_n": len(prod), "overlap": len(prod & set(b43[t]))})
    return out


def export_only(repo: Path | None) -> int:
    """Copy the last replay's public ledger; write an explicit 'unavailable' stub if none exists."""
    if repo is None:
        print("FATAL: --export-only needs --export-repo", file=sys.stderr)
        return 2
    src = OUT_DIR / "strategy_arena_ledger_public.json"
    dst = repo / "data/latest/strategy_arena_ledger.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        text = src.read_text(encoding="utf-8")
        json.loads(text)  # refuse to publish a corrupt ledger
    else:
        text = json.dumps({"contract_version": "strategy_arena_ledger_v1", "status": "unavailable",
                           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                           "reason": "尚未运行历史回放"}, ensure_ascii=False) + "\n"
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dst)
    print(f"strategy_arena_ledger: exported ({'replay' if src.exists() else 'stub'}) -> {dst}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--signals", type=int, default=30)
    ap.add_argument("--end", default="")
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--cost", type=float, default=0.003)
    ap.add_argument("--stress-cost", type=float, default=0.005)
    ap.add_argument("--export-repo", type=Path, default=None)
    ap.add_argument("--export-only", action="store_true",
                    help="publisher step2f: copy the latest public ledger into --export-repo (no replay)")
    ap.add_argument("--skip-if-pipeline-running", action="store_true",
                    help="nightly closure: exit 0 without replaying while the selection pipeline holds its lock")
    args = ap.parse_args(argv)

    if args.export_only:
        return export_only(args.export_repo)
    if args.skip_if_pipeline_running:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from production_release_gate import pipeline_lock_active
        lock = WS / "stock_data" / "03-working" / "health" / "stock_pipeline_gate.lockdir"
        if pipeline_lock_active(lock):
            print("strategy_arena_replay: selection pipeline still running; skipped (ledger keeps previous window)")
            return 0

    os.environ["OPENCLAW_PREBREAKOUT_HOLD_DAYS"] = "1"  # never touch the real v4.4 hold book
    sys.path.insert(0, str(ANALYZER))
    import run_strategy_suite as rss
    import prebreakout_v5_gate as v5gate

    capture: dict = {}
    original_gate = v5gate.apply_v5_gate

    def recording_gate(items, *a, **k):
        capture["in"] = [it["ts_code"] for it in items]
        out, meta = original_gate(items, *a, **k)
        capture["out"] = [it["ts_code"] for it in out]
        return out, meta
    v5gate.apply_v5_gate = recording_gate
    rss.v5gate.apply_v5_gate = recording_gate

    cache = rss.CACHE
    all_dates = sorted(set(d for d in (p.name[6:14] for p in cache.glob("daily_*.parquet")))
                       & set(p.name[11:19] for p in cache.glob("stk_factor_*.parquet"))
                       & set(p.name[9:17] for p in cache.glob("cyq_perf_*.parquet")))
    end = args.end or all_dates[-1]
    upto = [d for d in all_dates if d <= end]
    signals = upto[-args.signals:]
    price_dates = all_dates[all_dates.index(signals[0]):]
    w43 = next(s["weights"] for s in rss.BASE_STRATEGIES if s["id"] == "prebreakout_v41")
    w44 = next(s["weights"] for s in rss.BASE_STRATEGIES if s["id"] == "prebreakout_v44")
    name_map = rss.build_name_map()

    t0 = time.time()
    b43, b44, gates = {}, {}, {}
    for t in signals:
        b43[t], b44[t], gates[t] = select_day(rss, v5gate, w43, w44, t, name_map, capture)
        print(f"  {t}: v4.3={len(b43[t])} v4.4={len(b44[t])} candidates={len(gates[t]['candidates'])} "
              f"vetoed={len(gates[t]['vetoed'])}", flush=True)
    prices = Prices(cache, price_dates)

    curves, summary = {}, {}
    for label, cost in (("base", args.cost), ("stress", args.stress_cost)):
        r43, s43 = simulate(prices, signals, b43, 1, cost)
        r44, s44 = simulate(prices, signals, b44, args.hold, cost)
        curves[label] = (r43, r44)
        summary[label] = {"v43": s43, "v44": s44}

    # per-signal basket statistics (equal weight, net of one round trip)
    per_signal, groups = [], {"consensus": [], "challenger_only": [], "champion_excluded": [],
                              "vetoed_cand": [], "survivor_cand": [], "v43_would_veto": [], "v43_kept": []}
    for t in signals:
        f1 = lambda c: prices.fwd_net(c, t, 1, args.cost)  # noqa: E731
        f5 = lambda c: prices.fwd_net(c, t, 5, args.cost)  # noqa: E731
        s43, s44 = set(b43[t]), set(b44[t])
        r = {"date": t, "settled_t1": f1(b43[t][0]) is not None if b43[t] else False,
             "settled_t5": f5(b43[t][0]) is not None if b43[t] else False,
             "overlap": len(s43 & s44), "v44_rebalance": signals.index(t) % args.hold == 0}
        for lab, basket in (("v43", b43[t]), ("v44", b44[t])):
            t1 = [f1(c) for c in basket]
            t5 = [f5(c) for c in basket]
            r[f"{lab}_t1"] = rnd(mean(t1), 5)
            r[f"{lab}_t5"] = rnd(mean(t5), 5)
            r[f"{lab}_hit_t1"] = rnd(mean([1.0 if x > 0 else 0.0 for x in t1 if x is not None]), 3)
            r[f"{lab}_hit_t5"] = rnd(mean([1.0 if x > 0 else 0.0 for x in t5 if x is not None]), 3)
        if r["settled_t5"]:
            groups["consensus"] += [f5(c) for c in s44 & s43]
            groups["challenger_only"] += [f5(c) for c in s44 - s43]
            groups["champion_excluded"] += [f5(c) for c in s43 - s44]
            vet = set(gates[t]["vetoed"])
            groups["vetoed_cand"].append(mean([f5(c) for c in vet]))
            groups["survivor_cand"].append(mean([f5(c) for c in gates[t]["survivors"]]))
            groups["v43_would_veto"] += [f5(c) for c in s43 & vet]
            groups["v43_kept"] += [f5(c) for c in s43 - vet]
        r["vetoed_n"] = len(gates[t]["vetoed"])
        r["candidates_n"] = len(gates[t]["candidates"])
        r["v43_in_vetoed"] = len(s43 & set(gates[t]["vetoed"]))
        per_signal.append(r)

    paired = [(v, s) for v, s in zip(groups["vetoed_cand"], groups["survivor_cand"]) if v is not None and s is not None]
    attribution = {
        "consensus_t5_mean": rnd(mean(groups["consensus"]), 5), "consensus_n": len(groups["consensus"]),
        "challenger_only_t5_mean": rnd(mean(groups["challenger_only"]), 5), "challenger_only_n": len(groups["challenger_only"]),
        "champion_excluded_t5_mean": rnd(mean(groups["champion_excluded"]), 5), "champion_excluded_n": len(groups["champion_excluded"]),
        "veto": {
            "days": len(paired),
            "vetoed_t5_mean": rnd(mean([v for v, _ in paired]), 5),
            "survivor_t5_mean": rnd(mean([s for _, s in paired]), 5),
            "days_vetoed_worse": sum(1 for v, s in paired if v < s),
            "avoidance_spread_t5": rnd(mean([s - v for v, s in paired]), 5),
            "v43_picks_vetoed_n": len(groups["v43_would_veto"]),
            "v43_picks_vetoed_t5_mean": rnd(mean(groups["v43_would_veto"]), 5),
            "v43_picks_kept_t5_mean": rnd(mean(groups["v43_kept"]), 5),
            "note": "T+5 净收益（含一次往返成本）按信号日等权；候选 = 通过硬过滤、进入否决门的全部股票",
        },
    }

    def curve_rows(label):
        r43, r44 = curves[label]
        by = {r["date"]: r for r in r43}
        out = []
        for r in r44:
            a = by.get(r["date"])
            if a:
                out.append({"date": r["date"], "v43": rnd(a["equity"] - 1, 5), "v44": rnd(r["equity"] - 1, 5),
                            "spread": rnd(r["equity"] - a["equity"], 5), "v43_ret": rnd(a["ret"], 5),
                            "v44_ret": rnd(r["ret"], 5), "v43_cost": rnd(a["cost"], 5), "v44_cost": rnd(r["cost"], 5)})
        return out

    clean = lambda s: {k: rnd(v, 5) if isinstance(v, float) else v for k, v in s.items()}  # noqa: E731
    base_rows = curve_rows("base")
    diffs = [r["v44_ret"] - r["v43_ret"] for r in base_rows if r["v44_ret"] is not None and r["v43_ret"] is not None]
    sd_diff = stdev(diffs)
    significance = {
        "paired_days": len(diffs),
        "daily_diff_mean": rnd(mean(diffs), 6),
        "daily_diff_t": rnd(mean(diffs) / (sd_diff / math.sqrt(len(diffs))), 3) if sd_diff else None,
        "sharpe_se_approx": rnd(math.sqrt(TRADING_DAYS_PER_YEAR / len(diffs)), 2) if diffs else None,
        "note": "配对日收益差（挑战者−冠军）的 t 统计量；|t|<2 视为不显著。夏普标准误≈√(242/样本日数)。",
    }
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    public = {
        "contract_version": "strategy_arena_ledger_v1", "generated_at": now, "kind": "historical_replay_backtest",
        "honesty_banner": ("历史回放（回测）· 非前瞻证据 · 非买入建议。两套策略均以当前代码在历史数据上逐日重放，"
                           f"样本仅 {len(signals)} 个信号日，统计意义有限；v4.4 尚未完成前瞻验证。"),
        "window": {"signal_from": signals[0], "signal_to": signals[-1], "signals": len(signals),
                   "price_to": price_dates[-1], "curve_days": len(curves["base"][0])},
        "methodology": {
            "selection": "逐日调用生产 build_top20（v4.3=prebreakout，v4.4=prebreakout_v44 含否决门与增强排序），仅读取信号日及之前的缓存",
            "entry": "T+1 开盘入场；净值按开盘到开盘复利（最后一日按收盘计价）；复权安全（使用交易所前收盘价）",
            "v43_rebalance": "每个交易日按当日 Top-20 等权调仓",
            "v44_rebalance": f"每 {args.hold} 个信号日调仓一次，期间持有不动（模拟 5 日持仓簿）",
            "cost": f"按实际换手计费：单边 = 往返成本/2；基准往返 {args.cost:.3%}（含印花税与佣金滑点），压力 {args.stress_cost:.3%}",
            "metrics": "T+1 = 次日开盘到收盘；T+5 = 次日开盘到第 5 日收盘；均已扣一次往返成本；夏普按 242 日年化",
            "not_point_in_time": ["行业映射为当前版本（行业约束）", "ST 过滤使用当前股票名称"],
            "isolation": "回放不写入前瞻影子账本、双轨评估库或 v4.4 持仓簿",
        },
        "summary": {k: {"v43": clean(v["v43"]), "v44": clean(v["v44"])} for k, v in summary.items()},
        "curve": base_rows,
        "significance": significance,
        "curve_stress": [{"date": r["date"], "v43": r["v43"], "v44": r["v44"]} for r in curve_rows("stress")],
        "per_signal": [{k: v for k, v in r.items()} for r in per_signal],
        "attribution": attribution,
    }
    parity = production_parity(signals, b43)
    local = {**public, "picks": {t: {"v43": b43[t], "v44": b44[t]} for t in signals},
             "gate_samples": {t: {"vetoed_n": len(gates[t]["vetoed"]), "gate": gates[t]["gate"]} for t in signals},
             "production_parity": parity, "missing_price_legs": prices.missing_legs,
             "runtime_seconds": round(time.time() - t0, 1)}
    if parity:
        public["methodology"]["production_parity"] = (
            f"v4.3 重放名单与当日实际发布名单平均重合 {mean([p['overlap'] / p['production_n'] for p in parity]):.0%}"
            f"（{len(parity)} 个有记录的信号日；差异来自其后的代码修正，如 20261009 流动性单位）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in (f"arena_replay_{signals[-1]}.json", "arena_replay_latest.json"):
        (OUT_DIR / name).write_text(json.dumps(local, ensure_ascii=False, indent=1), encoding="utf-8")
    pub_path = OUT_DIR / "strategy_arena_ledger_public.json"
    pub_path.write_text(json.dumps(public, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.export_repo:
        dst = args.export_repo / "data/latest/strategy_arena_ledger.json"
        dst.write_text(pub_path.read_text(encoding="utf-8"), encoding="utf-8")
    b = summary["base"]
    print(json.dumps({"signals": f"{signals[0]}..{signals[-1]}", "v43": clean(b["v43"]), "v44": clean(b["v44"]),
                      "attribution": attribution, "significance": significance, "parity": public["methodology"].get("production_parity"),
                      "missing_legs": prices.missing_legs, "secs": local["runtime_seconds"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
