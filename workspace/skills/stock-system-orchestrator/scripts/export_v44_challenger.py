#!/usr/bin/env python3
"""Export the public v4.4 challenger (shadow) contract: data/latest/v44_challenger_state.json.

20261010: the v4.4 shadow track (fc931d9) is scored every run but never published, because the
publication layer keeps RETAINED_STRATEGIES = [prebreakout_v41].  run_strategy_suite persists the
shadow list to stock_data/03-working/selection_state/prebreakout_v44_latest.json; this script turns
it into a sanitized public contract and computes the Champion (v4.3) vs Challenger (v4.4) delta
against the already-published recommendation_state.json.

Research display only: v4.4 has no execution authority, never changes the v4.3 list, and is
not forward-validated.  Missing input writes an explicit "unavailable" contract (no 404 on Pages).
The maintenance step (maintenance_mode.py apply) scrubs this file like every publishable data file.

Exit 0 always on missing/stale input (non-blocking for the publisher); 2 on a write error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from statistics import NormalDist

WS = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", Path.home() / ".openclaw" / "workspace"))
DEFAULT_REPO = Path(os.environ.get("OPENCLAW_PUBLISHED_REPO", WS / "stock-report"))
V44_LATEST = WS / "stock_data" / "03-working" / "selection_state" / "prebreakout_v44_latest.json"
OUT_REL = "data/latest/v44_challenger_state.json"
CONTRACT = "v44_challenger_v1"
HONESTY = ("影子策略 · 仅研究观察 · 非买入建议。v4.4 挑战者尚未完成前瞻验证，不参与生产名单、"
           "不改变 v4.3 排名、无任何执行权限。")
COMPONENT_LABELS = {"core": "纯化核心（波动收敛+换手稳定）", "vpd_20": "量价背离 VPD", "smf_20_rev": "聪明钱反转 SMF"}
AI_FIELDS = ("ai_score", "ai_advice", "ai_conclusion", "ai_points", "ai_risks", "ai_catalysts", "ai_evidence_time")
_N01 = NormalDist()


def code6(value) -> str:
    return str(value or "").split(".")[0][:6]


def num(value, digits=4):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if v == v and abs(v) != float("inf") else None


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build(v44: dict | None, rec: dict | None, now: str) -> dict:
    base = {"contract_version": CONTRACT, "generated_at": now, "title": "v4.4 挑战者策略",
            "subtitle": "前沿候选因子试验区 · 5 日持仓", "honesty_banner": HONESTY, "research_only": True,
            "execution_authority": "observe_only_no_auto_order", "effectiveness_status": "not_validated"}
    champion = [r for r in (rec or {}).get("final_recommendations") or [] if isinstance(r, dict)]
    champion_date = str((rec or {}).get("trade_date") or "")
    strat = (v44 or {}).get("strategy") or {}
    picks = [p for p in strat.get("top20") or [] if isinstance(p, dict)]
    if not picks:
        return {**base, "status": "unavailable", "reason": "v4.4 影子名单尚未生成（首次运行或当日影子失败）",
                "champion_trade_date": champion_date, "top20": [], "delta": None}

    meta = strat.get("selection_meta") or {}
    gate = meta.get("veto_gate") or {}
    hold = meta.get("hold_book") or {}
    vetoed_pct = gate.get("vetoed_percentile") or {}
    trade_date = str(v44.get("latest_trade_date") or "")
    weights = {k: float(v) for k, v in (strat.get("factor_weights") or {}).items()}

    rows = []
    for p in picks:
        comps = p.get("challenger_components") or {}
        row = {
            "rank": p.get("rank"), "code": code6(p.get("code")), "ts_code": str(p.get("code") or ""),
            "name": p.get("name") or code6(p.get("code")), "industry": p.get("industry") or p.get("industry_name") or "",
            "close": num(p.get("close"), 2), "change_pct": num(p.get("change"), 2), "score": num(p.get("score"), 1),
            "booster_score": num(p.get("booster_score")), "challenger_pct": num(p.get("challenger_pct")),
            "hold_status": p.get("hold_status") or ("new" if hold.get("rebalanced") else "held"),
            "factor_scores": {k: num(v, 1) for k, v in (p.get("factor_scores") or {}).items()
                              if weights.get(k, 1) > 0},
            "components": {k: {"label": COMPONENT_LABELS[k], "z": num(comps.get(k)),
                               "pct": num(_N01.cdf(float(comps[k])) * 100, 1) if comps.get(k) is not None else None}
                           for k in COMPONENT_LABELS},
            "chip": {"chip_conc": num(p.get("chip_conc")), "winner_rate": num(p.get("winner_rate"), 1),
                     "volume_ratio": num(p.get("volume_ratio"), 2), "rsi_6": num(p.get("rsi_6"), 1)},
        }
        if p.get("ai_conclusion"):
            row.update({k: p.get(k) for k in AI_FIELDS if p.get(k) not in (None, "", [])})
            row["ai_source"] = "shared_same_day_analysis"
        rows.append(row)

    v44_codes = [r["code"] for r in rows]
    champ_by = {code6(r.get("stock_code") or r.get("code")): r for r in champion}
    name_of = lambda r: r.get("stock_name") or r.get("name") or ""  # noqa: E731
    consensus = [{"code": c, "name": next(r["name"] for r in rows if r["code"] == c),
                  "champion_rank": champ_by[c].get("rank_no") or champ_by[c].get("rank"),
                  "challenger_rank": next(r["rank"] for r in rows if r["code"] == c)}
                 for c in v44_codes if c in champ_by]
    challenger_only = [{"code": r["code"], "name": r["name"], "challenger_rank": r["rank"],
                        "challenger_pct": r["challenger_pct"]} for r in rows if r["code"] not in champ_by]
    excluded = []
    for c, r in champ_by.items():
        if c in v44_codes:
            continue
        ts = str(r.get("ts_code") or r.get("display_code") or "")
        pct = vetoed_pct.get(ts)
        excluded.append({"code": c, "name": name_of(r), "champion_rank": r.get("rank_no") or r.get("rank"),
                         "reason": "vetoed" if pct is not None else "ranked_out",
                         "challenger_pct": num(pct)})
    excluded.sort(key=lambda x: (x["reason"] != "vetoed", x["champion_rank"] or 999))

    return {
        **base, "status": "ok", "trade_date": trade_date, "champion_trade_date": champion_date,
        "aligned": bool(trade_date) and trade_date == champion_date, "source_generated_at": v44.get("generated_at"),
        "strategy": {"id": strat.get("id"), "name": strat.get("name"), "version": "4.4",
                     "challenger_version": (gate.get("gate") or {}).get("version"),
                     "holding_period_days": hold.get("hold_days"),
                     "factor_weights": {k: round(v, 4) for k, v in weights.items() if v > 0},
                     "dropped_factors": sorted(k for k, v in weights.items() if v <= 0),
                     "composite_weights": {"core": 0.4, "vpd_20": 0.337854, "smf_20_rev": 0.262146},
                     "factor_notes": {
                         "core": "波动收敛（布林带收窄）与 5 日换手稳定性等权后再做截面正态化",
                         "vpd_20": "近 20 日价格与成交量负相关（量价背离）",
                         "smf_20_rev": "近 20 日隔夜涨幅减日内涨幅（聪明钱因子方向取反）",
                         "veto": "挑战者综合分处于当日全市场后 25% 的候选一律否决",
                         "booster": "幸存者按 0.5×z(v4.4 分) + 0.5×z(挑战者分) 重排"}},
        "veto_gate": {k: (gate.get("gate") or {}).get(k) for k in ("status", "universe", "scored", "vetoed", "quantile")}
        | {"vetoed_candidates": gate.get("vetoed_candidates")},
        "hold_book": {k: hold.get(k) for k in ("hold_days", "hold_day", "rebalanced", "rebalance_date", "carried")},
        "top20": rows,
        "delta": {"consensus": consensus, "challenger_only": challenger_only, "champion_excluded": excluded,
                  "counts": {"consensus": len(consensus), "challenger_only": len(challenger_only),
                             "vetoed": sum(1 for x in excluded if x["reason"] == "vetoed"),
                             "ranked_out": sum(1 for x in excluded if x["reason"] == "ranked_out")}},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--v44-latest", type=Path, default=V44_LATEST)
    args = ap.parse_args(argv)
    rec = load(args.repo / "data/latest/recommendation_state.json")
    doc = build(load(args.v44_latest), rec, datetime.now().astimezone().isoformat(timespec="seconds"))
    out = args.repo / OUT_REL
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)
    except OSError as exc:
        print(f"FATAL: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    d = doc.get("delta") or {}
    print(f"v44_challenger: status={doc['status']} trade_date={doc.get('trade_date')} "
          f"aligned={doc.get('aligned')} top20={len(doc['top20'])} counts={d.get('counts')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
