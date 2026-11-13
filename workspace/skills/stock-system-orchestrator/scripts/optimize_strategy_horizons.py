#!/usr/bin/env python3
"""策略横盘寻优（2026-06-28 #1）：三策略各扫多个持有期，按真·指数 IR 选各自最优横盘。

复用 generate_o2c_review 的复盘引擎（行情并集 / 真·指数开收盘 / T+N 交易日），对每套策略：
  入场恒为 T+1 开盘（无未来函数），持有 hold∈{0,1,2,3,5,10} 个交易日后于该日收盘出场，
  超额=个股横盘收益 − 沪深300真·指数同窗口收益；逐日聚合(settled>=MIN_SETTLE 才算有效日)，
  再算总命中率 / 平均超额 / 信息比率 IR。按 IR(有效日>=3) 推荐各策略最优持有期。

只读分析，不写库不发布。用法：python3 optimize_strategy_horizons.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_o2c_review as R  # noqa: E402

HOLDS = [0, 1, 2, 3, 5, 10]
MIN_SETTLE = 10
TOPN = 20


def _agg_for_hold(sid: str, hold: int, quotes, index_px, calendar) -> dict:
    db = R.STRATEGY_REGISTRY[sid]["db"]
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rdates = [r[0] for r in conn.execute(
        "SELECT DISTINCT recommend_date FROM recommendation_snapshot WHERE strategy_id=? ORDER BY recommend_date", (sid,))]
    daily = []  # (avg_excess, hit_rate) per valid day
    for rd in rdates:
        entry = R.nth_trade_date(calendar, rd, 1)
        exit_ = R.nth_trade_date(calendar, rd, 1 + hold)
        if not entry or not exit_:
            continue
        picks = [r[0] for r in conn.execute(
            "SELECT stock_code FROM recommendation_snapshot WHERE strategy_id=? AND recommend_date=? ORDER BY rank_no LIMIT ?",
            (sid, rd, TOPN))]
        ie, xe = index_px.get(entry) or {}, index_px.get(exit_) or {}
        bench = ((xe["close"] - ie["open"]) / ie["open"] * 100) if (ie.get("open") and xe.get("close") is not None) else None
        excs, hits = [], []
        for code in picks:
            en = quotes.get(entry, {}).get(str(code))
            ex = quotes.get(exit_, {}).get(str(code))
            if not (en and en.get("open") and ex and ex.get("close") is not None):
                continue
            ret = (ex["close"] - en["open"]) / en["open"] * 100
            hits.append(1 if ret > 0 else 0)
            if bench is not None:
                excs.append(ret - bench)
        if len(hits) >= MIN_SETTLE and excs:
            daily.append((sum(excs) / len(excs), 100.0 * sum(hits) / len(hits)))
    conn.close()
    n = len(daily)
    if n == 0:
        return {"hold": hold, "valid_days": 0, "avg_excess": None, "avg_hit": None, "ir": None}
    ex = [d[0] for d in daily]
    m = sum(ex) / n
    ir = None
    if n >= 2:
        sd = (sum((x - m) ** 2 for x in ex) / (n - 1)) ** 0.5
        if sd > 1e-9:
            ir = round(m / sd * (252 ** 0.5), 2)
    return {"hold": hold, "valid_days": n, "avg_excess": round(m, 3),
            "avg_hit": round(sum(d[1] for d in daily) / n, 1), "ir": ir}


def main() -> int:
    quotes, _ = R.load_quote_union()
    calendar = sorted(quotes.keys())
    index_px = R.load_index_prices()
    print(f"行情交易日 {calendar[0]}..{calendar[-1]} | 指数日 {len(index_px)} | 扫描持有期(交易日) {HOLDS}\n")
    best = {}
    for sid, reg in R.STRATEGY_REGISTRY.items():
        print(f"=== {reg['name']} ({sid}) ===")
        print(f"{'持有期':>8}{'有效日':>7}{'命中率':>8}{'平均超额':>10}{'IR':>8}")
        rows = [_agg_for_hold(sid, h, quotes, index_px, calendar) for h in HOLDS]
        for r in rows:
            hl = "日内(开→收)" if r["hold"] == 0 else f"{r['hold']}日"
            hit_s = f"{r['avg_hit']}%" if r["avg_hit"] is not None else "—"
            exc_s = f"{r['avg_excess']:+.3f}%" if r["avg_excess"] is not None else "—"
            ir_s = str(r["ir"]) if r["ir"] is not None else "—"
            print(f"{hl:>10}{r['valid_days']:>7}{hit_s:>9}{exc_s:>11}{ir_s:>8}")
        # 推荐：有效日>=3 中 IR 最高
        elig = [r for r in rows if r["valid_days"] >= 3 and r["ir"] is not None]
        pick = max(elig, key=lambda r: r["ir"]) if elig else None
        if pick:
            best[sid] = pick["hold"]
            print(f"  → 推荐持有期：{'日内' if pick['hold']==0 else str(pick['hold'])+'日'}"
                  f"（IR {pick['ir']}、超额 {pick['avg_excess']:+.3f}%、命中 {pick['avg_hit']}%、{pick['valid_days']}有效日）\n")
        else:
            print("  → 有效样本不足，暂不推荐\n")
    print("最优持有期(hold_days)：" + ", ".join(f"{R.STRATEGY_REGISTRY[s]['name']}={'日内' if h==0 else str(h)+'日'}" for s, h in best.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
