#!/usr/bin/env python3
"""DOUBLE-PASS 分档前瞻监控：v4.3 Top-20 × 绿地 comp_rank_ew6 的分档台账 + 周期收益结算。

三步，全部只读生产路径，只写 factor_factory/data/forward_shadow/tier_ledger.jsonl 与
factor_factory/outputs/forward_shadow/tier_monitor.json：

  record  forward_shadow_digest.py 每晚调用 record_day()：把当日 v4.3 Top-20 的分档追加进台账
          （append-only；信号日只记一次；晚于次日 09:30 入场截止时间的记录标 official=false，不计入统计）
  seed    seed_from_digest()：把已在截止前提交到 git 的 digest_<date>.md 分档表转成台账行（仅 20261014 首日使用）
  settle  逐信号日结算 T+1 / T+5：次日开盘入场，T+1 收盘或第 5 个交易日收盘出场，涨停开盘买不进的剔除，
          扣一次往返成本（0.3%，同 v4.3 合同），对照全体 universe 等权基准；
          汇总各档均值/胜率/超额/Newey-West t，并按预注册口径给出 verdict

规格冻结于 contracts/tier_monitor_v1.json（--write-spec），结果出来后不得修改判定口径。
只前瞻：不回补任何入场开盘已过的信号日。

  python tier_monitor.py settle
  python tier_monitor.py seed --date 20261014
  python tier_monitor.py --write-spec
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_impact as ci  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
FS = ROOT / "data/forward_shadow"
OUT = ROOT / "outputs/forward_shadow"
LEDGER = FS / "tier_ledger.jsonl"
RESULT = OUT / "tier_monitor.json"
SPEC_PATH = ROOT / "contracts/tier_monitor_v1.json"
DIGEST_REL = "factor_factory/outputs/forward_shadow/digest_{d}.md"

TZ_NAME = "Asia/Shanghai"
IMPACT_CAPITALS = (1e6, 5e6)  # yuan per name: stress overlay only, not part of the frozen spec
ADV_DAYS = 20
HORIZONS = (1, 5)
ROUND_TRIP_COST = 0.003
MIN_DAYS_VERDICT = 60
T_THRESHOLD = 2.0
TIER_OF_BUCKET = {"double": "DOUBLE", "keep": "KEEP", "b30": "DANGER", "oou_b30": "DANGER", "oou_safe": "DANGER",
                  "oou": "DANGER"}
GROUPS_LIQ = ("DOUBLE", "KEEP", "DANGER", "V43_ALL")
GROUPS = ("DOUBLE", "KEEP", "DANGER", "V43_ALL", "BENCH")
LABELS = {"DOUBLE": "双料王牌 (DOUBLE-PASS)", "KEEP": "保留 (KEEP-70)", "DANGER": "警示 (DANGER)",
          "V43_ALL": "v4.3 Top-20 全体", "BENCH": "全体 universe 等权基准"}

SPEC = {
    "spec_id": "tier_monitor_v1",
    "definition": "DOUBLE-PASS = v4.3 当日 Top-20 中进入 comp_rank_ew6 buffered_top100 或综合分前 20% 的股票；"
                  "KEEP = 在 avoid_losers_70 内；DANGER = 综合分后 30% 或不在绿地 universe",
    "cohort": "信号日 D 的分档名单，等权；每个信号日一个样本（日内等权，跨日再平均）",
    "execution": "T+1 开盘入场；涨停开盘买不进的剔除；T+1 = 次日开盘到次日收盘，T+5 = 次日开盘到第 5 个交易日收盘；"
                 "出场日停牌则以最后成交收盘计价；已复权收益",
    "cost": {"round_trip": ROUND_TRIP_COST, "note": "同 v4.3 发布合同，仅一次往返成本，不含冲击成本"},
    "benchmark": "BENCH = 当日绿地 universe 全体等权，同一入场/出场规则（可投资基准）",
    "forward_only": "信号须在次日 09:30 前入账；晚于截止的行标 official=false 且不计入任何统计；绝不回补",
    "evaluation": {
        "primary": "T+5：DOUBLE 与同日 v4.3 其余入选（KEEP+DANGER 合并）的配对净收益差，逐信号日取差，"
                   "Newey-West t（滞后 4）",
        "when": f"DOUBLE 非空的已结算信号日 ≥ {MIN_DAYS_VERDICT} 之前不下结论",
        "success": f"均值差 > 0 且 t ≥ {T_THRESHOLD}",
        "reported_not_gated": ["T+1 同口径", "DOUBLE 相对基准与 v4.3 全体的超额", "逐股胜率", "累计曲线（T+1 日再平衡）",
                               "空仓日（DOUBLE 为空）计数"],
        "caveat": "相邻信号日的名单与持有窗口重叠，故用 Newey-West 滞后修正；DOUBLE 每日仅数只，噪声大，"
                  "样本不足时的任何方向都不构成结论",
    },
    "no_production_effect": "只读；不写 #/today、v4.3 产物或任何生产健康文件",
}


def spec_hash() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def append(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- record

def _members(x: pd.DataFrame) -> list[dict]:
    rows = []
    for r in x.itertuples():
        rows.append({"code": r.code, "name": r.name, "industry": r.industry, "v43_rank": None if pd.isna(r.v43_rank)
                     else int(r.v43_rank), "bucket": r.bucket, "tier": TIER_OF_BUCKET[r.bucket],
                     "composite": None if pd.isna(r.composite) else float(r.composite),
                     "pct": None if pd.isna(r.pct) else float(r.pct)})
    return rows


def record_day(d: str, x: pd.DataFrame, rec: dict, source: str, now: datetime | None = None) -> str:
    """Append the tier assignment for signal date d. Returns 'recorded' | 'late' | 'exists' | 'empty'."""
    if x is None or x.empty:
        return "empty"
    if any(r["signal_date"] == d for r in read_jsonl(LEDGER)):
        return "exists"
    now = now or datetime.now().astimezone()
    deadline = rec.get("entry_deadline")
    late = bool(deadline and now >= datetime.fromisoformat(deadline))
    append(LEDGER, {"signal_date": d, "recorded_at": now.isoformat(), "entry_deadline": deadline,
                    "official": not late, "late": late, "source": source, "spec_hash": spec_hash(),
                    "fs_spec_hash": rec.get("spec_hash"), "members": _members(x)})
    return "late" if late else "recorded"


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(WORKSPACE), *args], capture_output=True, text=True, check=True).stdout


def seed_from_digest(d: str) -> str:
    """Rebuild the tier row for d from the digest markdown that was committed before the entry deadline."""
    if any(r["signal_date"] == d for r in read_jsonl(LEDGER)):
        return "exists"
    rel = DIGEST_REL.format(d=d)
    sha, when = _git("log", "--diff-filter=A", "--format=%H|%cI", "--", rel).strip().splitlines()[-1].split("|")
    md = _git("show", f"{sha}:{rel}")
    deadline = re.search(r"entry deadline `([^`]+)`", md).group(1)
    spec_h = re.search(r"spec `([0-9a-f]+)`", md).group(1)
    if datetime.fromisoformat(when) >= datetime.fromisoformat(deadline):
        raise SystemExit(f"digest for {d} was committed {when}, after the entry deadline {deadline}: refusing to seed")
    lines = md.split("## v4.3 Top-20 vs comp_rank_ew6")[1].split("\n## ")[0].splitlines()
    head = next(i for i, ln in enumerate(lines) if ln.startswith("| v43_rank"))
    cols = [c.strip() for c in lines[head].strip("|").split("|")]
    rows = []
    for ln in lines[head + 2:]:
        if not ln.startswith("|"):
            break
        row = dict(zip(cols, [c.strip() for c in ln.strip().strip("|").split("|")]))
        if "bucket" not in row:  # first digest format (no bucket column): derive it from tier + reason
            t, why = row["tier"], row["reason"]
            row["bucket"] = ("double" if "DOUBLE" in t else "keep" if "KEEP" in t
                             else "oou" if "universe" in why else "b30")
        rows.append(row)
    x = pd.DataFrame({
        "v43_rank": [int(r["v43_rank"]) for r in rows], "code": [r["code"] for r in rows],
        "name": [r["name"] for r in rows], "industry": [r["industry"] for r in rows],
        "composite": [float(r["composite"]) if r["composite"] else np.nan for r in rows],
        "pct": [float(r["pct"]) if r["pct"] else np.nan for r in rows], "bucket": [r["bucket"] for r in rows]})
    append(LEDGER, {"signal_date": d, "recorded_at": when, "entry_deadline": deadline, "official": True, "late": False,
                    "source": f"digest_md@{sha[:7]} (committed {when})", "spec_hash": spec_hash(),
                    "fs_spec_hash": spec_h, "members": _members(x)})
    return "seeded"


# ---------------------------------------------------------------- pure math

def nw_t(x: list[float] | np.ndarray, lag: int) -> float | None:
    a = np.asarray([v for v in x if v is not None and np.isfinite(v)], dtype=float)
    n = len(a)
    if n < 3:
        return None
    m = a.mean()
    e = a - m
    s = float((e * e).sum() / n)
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float((e[l:] * e[:-l]).sum() / n)
    return float(m / np.sqrt(s / n)) if s > 0 else None


def stock_returns(adj_open_entry: pd.Series, adj_close_exit: pd.Series, can_buy: pd.Series,
                  cost: float = ROUND_TRIP_COST) -> pd.Series:
    """Net return per code for one (signal, horizon); NaN where the buy is blocked or prices are missing."""
    gross = adj_close_exit / adj_open_entry - 1.0
    return (gross - cost).where(can_buy.fillna(False).astype(bool))


def cohort_mean(rets: pd.Series, codes: list[str]) -> tuple[float | None, int, int]:
    """(equal-weight mean net return over tradable members, tradable n, requested n)."""
    sel = rets.reindex(codes).dropna()
    return (float(sel.mean()) if len(sel) else None), int(len(sel)), len(codes)


def summarize(days: list[dict], h: int) -> dict:
    """days: [{date, ret: {group: x|None}, n: {...}, trades: {group: [net returns]}}] already restricted to matured days."""
    lag = max(h - 1, 0)
    out = {}
    for g in GROUPS:
        r = [(d["ret"].get(g), d["ret"].get("BENCH")) for d in days if d["ret"].get(g) is not None]
        nets = [a for a, _ in r]
        ex = [a - b for a, b in r if b is not None]
        trades = [t for d in days for t in d["trades"].get(g, [])]
        out[g] = {
            "label": LABELS[g], "days": len(nets), "trades": len(trades),
            "mean_net": float(np.mean(nets)) if nets else None,
            "mean_excess": float(np.mean(ex)) if ex and g != "BENCH" else None,
            "hit_trade": float(np.mean([t > 0 for t in trades])) if trades else None,
            "hit_day_excess": float(np.mean([e > 0 for e in ex])) if ex and g != "BENCH" else None,
            "nw_t_excess": nw_t(ex, lag) if g != "BENCH" else None,
        }
    return out


def spread(days: list[dict], a: str, b: str, h: int) -> dict:
    """Per-signal-day paired difference a − b (days where both are non-empty)."""
    diffs = [d["ret"][a] - d["ret"][b] for d in days if d["ret"].get(a) is not None and d["ret"].get(b) is not None]
    return {"a": a, "b": b, "days": len(diffs), "mean": float(np.mean(diffs)) if diffs else None,
            "hit_day": float(np.mean([v > 0 for v in diffs])) if diffs else None, "nw_t": nw_t(diffs, max(h - 1, 0)),
            "lag": max(h - 1, 0)}


def verdict(sp5: dict) -> dict:
    n = sp5["days"]
    if n < MIN_DAYS_VERDICT:
        return {"status": "insufficient_sample", "days": n, "required": MIN_DAYS_VERDICT,
                "message": f"DOUBLE 非空的 T+5 已结算信号日 {n}/{MIN_DAYS_VERDICT}，未达预注册样本量，不下结论"}
    ok = sp5["mean"] is not None and sp5["mean"] > 0 and sp5["nw_t"] is not None and sp5["nw_t"] >= T_THRESHOLD
    return {"status": "pass" if ok else "fail", "days": n, "required": MIN_DAYS_VERDICT,
            "message": f"按预注册口径：均值差 {sp5['mean']:+.4f}，Newey-West t = {sp5['nw_t']:.2f}"
                       f"（需 > 0 且 ≥ {T_THRESHOLD}）"}


def adv20(signal_date: str, sessions: list[str], cache: dict, rp) -> pd.Series:
    """Mean daily amount (yuan) over the ADV_DAYS sessions ending at signal_date (daily.amount is 千元)."""
    i = sessions.index(signal_date)
    parts = []
    for d in sessions[max(0, i - ADV_DAYS + 1): i + 1]:
        if d not in cache:
            p = rp.BACKTEST_CACHE / f"daily_{d}.parquet"
            cache[d] = pd.read_parquet(p, columns=["ts_code", "amount"]).set_index("ts_code")["amount"] if p.exists() else None
        if cache[d] is not None:
            parts.append(cache[d])
    return (pd.concat(parts, axis=1).mean(axis=1) * 1e3) if parts else pd.Series(dtype=float)


# ---------------------------------------------------------------- settle

def settle(now: datetime | None = None) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    import forward_shadow as fs  # noqa: E402
    import research_labels_v2 as rl  # noqa: E402
    import research_panel_v2 as rp  # noqa: E402

    now = now or datetime.now().astimezone()
    rows = read_jsonl(LEDGER)
    official = sorted([r for r in rows if r.get("official")], key=lambda r: r["signal_date"])
    base = {"contract_version": "tier_monitor_v1", "generated_at": now.isoformat(), "spec_hash": spec_hash(),
            "spec": SPEC, "groups": LABELS, "ledger_days": len(rows), "official_days": len(official),
            "late_days_excluded": sorted(r["signal_date"] for r in rows if not r.get("official"))}
    if not official:
        return {**base, "status": "collecting", "message": "台账尚无官方信号日"}
    cal = fs.official_sessions("99999999")
    avail = [x for x in cal if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    last_avail = avail[-1]
    first = official[0]["signal_date"]
    sess = [x for x in cal if first <= x <= last_avail]
    base.update(first_signal=first, last_signal=official[-1]["signal_date"], as_of=last_avail)
    if len(sess) < 2:
        return {**base, "status": "collecting", "matured": {"h1": 0, "h5": 0}, "pending_days": len(official),
                "message": "尚无已结算信号日（需次日开盘及收盘数据）", "days": []}
    w = fs.window_mats(sess)
    codes_all = list(w["open"].columns)
    O, C, PRE = w["open"], w["close"], w["pre_close"]
    traded = w["traded"]
    adj_open, adj_close = w["adj_open"], w["adj_close"]
    adj_close_f = adj_close.ffill()
    idx = {d: i for i, d in enumerate(sess)}

    def buy_mask(entry_date: str) -> pd.Series:
        i = idx[entry_date]
        snap, _ = rp.load_pit_snapshot(entry_date)
        st = pd.DataFrame(False, index=[entry_date], columns=codes_all)
        if snap is not None:
            st.loc[entry_date] = snap.set_index("ts_code")["is_st"].astype(bool).reindex(codes_all).fillna(False).to_numpy()
        lim = rl.limit_ratio(pd.Index(codes_all), st).iloc[0]
        up = (PRE.iloc[i] * (1 + lim) + 1e-9).round(2)
        return traded.iloc[i] & (O.iloc[i] < up - 0.001)

    per_h: dict[int, list[dict]] = {h: [] for h in HORIZONS}
    detail: dict[str, dict] = {}
    pending = 0
    adv_cache: dict = {}
    liq: dict[str, list] = {g: [] for g in GROUPS_LIQ}
    stress: dict[float, dict[str, list]] = {c: {g: [] for g in GROUPS_LIQ} for c in IMPACT_CAPITALS}
    breach: dict[float, dict[str, list]] = {c: {g: [] for g in GROUPS_LIQ} for c in IMPACT_CAPITALS}
    for r in official:
        d = r["signal_date"]
        i0 = idx.get(d)
        if i0 is None or i0 + 1 >= len(sess):
            pending += 1
            continue
        entry = sess[i0 + 1]
        can = buy_mask(entry)
        members = r["members"]
        by_group = {
            "DOUBLE": [m["code"] for m in members if m["tier"] == "DOUBLE"],
            "KEEP": [m["code"] for m in members if m["tier"] == "KEEP"],
            "DANGER": [m["code"] for m in members if m["tier"] == "DANGER"],
            "V43_ALL": [m["code"] for m in members],
        }
        by_group["NON_DOUBLE"] = by_group["KEEP"] + by_group["DANGER"]
        sp = FS / "scores" / f"scores_{d}.parquet"
        bench_codes = list(pd.read_parquet(sp, columns=["ts_code"])["ts_code"]) if sp.exists() else []
        bench_codes = [c for c in bench_codes if c in set(codes_all)]
        det = {"date": d, "entry": entry, "n": {g: len(v) for g, v in by_group.items()}, "r": {}, "tradable": {},
               "members": []}
        adv = adv20(d, sess, adv_cache, rp)
        for g in GROUPS_LIQ:
            liq[g] += [float(v) for v in adv.reindex(by_group[g]).dropna()]
        for h in HORIZONS:
            if i0 + h >= len(sess):
                continue
            exit_d = sess[i0 + h]
            net = stock_returns(adj_open.iloc[i0 + 1], adj_close_f.iloc[i0 + h], can)
            ret, trades, ntrad = {}, {}, {}
            for g, codes in by_group.items():
                m, n_ok, _ = cohort_mean(net, codes)
                ret[g] = m
                ntrad[g] = n_ok
                trades[g] = [float(v) for v in net.reindex(codes).dropna()]
            bm, n_b, _ = cohort_mean(net, bench_codes) if bench_codes else (None, 0, 0)
            ret["BENCH"], ntrad["BENCH"], trades["BENCH"] = bm, n_b, []
            per_h[h].append({"date": d, "exit": exit_d, "ret": ret, "trades": trades})
            det["r"][f"h{h}"] = ret
            det["tradable"][f"h{h}"] = ntrad
            if h == 5:
                for cap in IMPACT_CAPITALS:
                    imp = ci.round_trip_impact(cap, adv)
                    brc = ci.breaches(cap, adv)
                    for g in GROUPS_LIQ:
                        sel = net.reindex(by_group[g]).dropna()
                        sel = sel[imp.reindex(sel.index).notna()]
                        if len(sel):
                            stress[cap][g].append(float((sel - imp.reindex(sel.index)).mean()))
                            breach[cap][g].append(float(brc.reindex(sel.index).mean()))
                det["members"] = [{**{k: m[k] for k in ("code", "name", "industry", "v43_rank", "tier", "bucket")},
                                   "ret1": _f(net_1(adj_open, adj_close_f, i0, can, m["code"], len(sess))),
                                   "ret5": _f(net.get(m["code"]))} for m in members if m["tier"] == "DOUBLE"]
        detail[d] = det

    stats = {f"h{h}": summarize(per_h[h], h) for h in HORIZONS}
    spreads = {f"h{h}": {"DOUBLE_vs_NON_DOUBLE": spread(per_h[h], "DOUBLE", "NON_DOUBLE", h),
                          "DOUBLE_vs_V43_ALL": spread(per_h[h], "DOUBLE", "V43_ALL", h),
                          "DOUBLE_vs_BENCH": spread(per_h[h], "DOUBLE", "BENCH", h)} for h in HORIZONS}
    curve, cum = [], {g: 1.0 for g in GROUPS}
    for d in per_h[1]:
        for g in GROUPS:
            cum[g] *= 1 + (d["ret"].get(g) or 0.0)
        curve.append({"date": d["date"], **{g: round(cum[g] - 1, 6) for g in GROUPS},
                      "double_empty": d["ret"].get("DOUBLE") is None})
    day_rows = []
    for d, det in sorted(detail.items()):
        day_rows.append({"date": d, "entry": det["entry"], "counts": det["n"],
                         "t1": _fd(det["r"].get("h1")), "t5": _fd(det["r"].get("h5")),
                         "tradable_t5": det["tradable"].get("h5"), "double_members": det["members"]})
    return {**base, "status": "ok" if per_h[1] else "collecting",
            "matured": {"h1": len(per_h[1]), "h5": len(per_h[5])},
            "pending_days": len(official) - len(per_h[1]),
            "stats": stats, "spreads": spreads, "verdict": verdict(spreads["h5"]["DOUBLE_vs_NON_DOUBLE"]),
            "curve": curve, "days": day_rows,
            "cost": {"round_trip": ROUND_TRIP_COST},
            "liquidity": {g: {"names": len(v), "median_adv_yi": _f(np.median(v) / 1e8) if v else None,
                              "share_adv_below_1yi": _f(np.mean([x < 1e8 for x in v])) if v else None}
                          for g, v in liq.items()},
            "impact_stress": {"note": "压力测试叠加层，不属于预注册口径：平方根冲击 10bp@1%ADV，双边，"
                                      "ADV = 信号日前 20 日均成交额；仅 T+5；不改变 verdict",
                              "capital_per_name_yuan": list(IMPACT_CAPITALS),
                              "groups": {str(int(c)): {g: {"days": len(stress[c][g]),
                                                         "mean_net_after_impact": _f(np.mean(stress[c][g])) if stress[c][g] else None,
                                                         "share_over_5pct_adv": _f(np.mean(breach[c][g])) if breach[c][g] else None}
                                                     for g in GROUPS_LIQ} for c in IMPACT_CAPITALS}},
            "double_empty_days": sum(1 for d in per_h[1] if d["ret"].get("DOUBLE") is None)}


def _f(v) -> float | None:
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), 5)


def _fd(r: dict | None) -> dict | None:
    return None if r is None else {k: _f(v) for k, v in r.items()}


def net_1(adj_open, adj_close_f, i0, can, code, n_sess) -> float | None:
    if i0 + 1 >= n_sess or code not in adj_open.columns or not bool(can.get(code, False)):
        return None
    return float(adj_close_f.iloc[i0 + 1][code] / adj_open.iloc[i0 + 1][code] - 1 - ROUND_TRIP_COST)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["settle", "seed"], default="settle")
    ap.add_argument("--date")
    ap.add_argument("--write-spec", action="store_true")
    a = ap.parse_args(argv)
    if a.write_spec:
        SPEC_PATH.write_text(json.dumps({**SPEC, "spec_hash": spec_hash(),
                                         "frozen_at": datetime.now().astimezone().isoformat()},
                                        ensure_ascii=False, indent=2))
        print(f"wrote {SPEC_PATH} hash {spec_hash()}")
        return 0
    if a.cmd == "seed":
        print(seed_from_digest(a.date))
        return 0
    res = settle()
    OUT.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    m = res.get("matured", {})
    v = res.get("verdict", {})
    print(f"tier_monitor: {res['status']} · 台账 {res['ledger_days']} 日 · 已结算 T+1 {m.get('h1', 0)} / T+5 {m.get('h5', 0)}"
          f" · {v.get('message', res.get('message', ''))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
