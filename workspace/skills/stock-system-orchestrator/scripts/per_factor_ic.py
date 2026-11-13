#!/usr/bin/env python3
"""逐因子已实现 IC/ICIR（2026-06-28 优化②）：在去幸存者偏差的完整复盘窗口上，
量化每个因子对「真实横盘后验收益」的截面预测力，标出噪声/反向因子。

口径（诚实披露其局限）：
  - 入场=T+1 开盘，按各策略最优横盘出场（hold_days 取自 STRATEGY_REGISTRY），收益=入场开→出场收。
  - 行情用 generate_o2c_review.load_quote_union（已并入 akshare 补全缓存，去幸存者偏差）。
  - 因子来自 recommendation_snapshot.evidence_json：O2C=factor_zscores、启动前夕=factor_scores。
  - IC=当日「因子值 vs 后验收益」的 Spearman 截面秩相关；ICIR=mean(IC)/std(IC)。
  - **局限**：只有 top20 入选股的因子值（无全市场），故为「选中集内」IC（限制了截面方差），
    只能判「在已选中的股里因子排序是否还预测」，强弱被低估；正向但弱不代表无用，反向才是明确坏信号。
    样本仍只 ~20-30 天 + 多因子同时检验 → 单因子结论需谨慎，看一致性(%正)而非单点。

用法：python3 per_factor_ic.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_o2c_review as R  # noqa: E402

FACTOR_FIELD = {"greenfield_o2c_v1": "factor_zscores", "prebreakout_v41": "factor_scores"}
MIN_PICKS_PER_DAY = 8


def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            rk[order[k]] = avg
        i = j + 1
    return rk


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 1e-12 or vb <= 1e-12:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / (va ** 0.5 * vb ** 0.5)


def _spearman(a: list[float], b: list[float]) -> float | None:
    return _pearson(_rank(a), _rank(b))


def analyze(sid: str, quotes, calendar) -> dict:
    field = FACTOR_FIELD[sid]
    hold = R.STRATEGY_REGISTRY[sid]["hold_days"]
    db = R.STRATEGY_REGISTRY[sid]["db"]
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rdates = [r[0] for r in conn.execute(
        "SELECT DISTINCT recommend_date FROM recommendation_snapshot WHERE strategy_id=? ORDER BY recommend_date", (sid,))]
    daily_ic: dict[str, list[float]] = {}
    valid_days = 0
    for rd in rdates:
        entry = R.nth_trade_date(calendar, rd, 1)
        exit_ = R.nth_trade_date(calendar, rd, 1 + hold)
        if not entry or not exit_:
            continue
        rows = conn.execute(
            "SELECT stock_code, evidence_json FROM recommendation_snapshot WHERE strategy_id=? AND recommend_date=? ORDER BY rank_no LIMIT 20",
            (sid, rd)).fetchall()
        recs = []  # (factors dict, ret)
        for code, ev in rows:
            m = re.search(r"(\d{6})", str(code))
            c = m.group(1) if m else str(code)
            en = quotes.get(entry, {}).get(c)
            ex = quotes.get(exit_, {}).get(c)
            if not (en and en.get("open") and ex and ex.get("close") is not None):
                continue
            ret = (ex["close"] - en["open"]) / en["open"] * 100
            try:
                f = (json.loads(ev) or {}).get(field) or {}
            except (json.JSONDecodeError, TypeError):
                f = {}
            if isinstance(f, dict) and f:
                recs.append((f, ret))
        if len(recs) < MIN_PICKS_PER_DAY:
            continue
        valid_days += 1
        rets = [r[1] for r in recs]
        # 当日所有出现的因子
        names = set()
        for f, _ in recs:
            names |= set(f.keys())
        for nm in names:
            xs, ys = [], []
            for f, ret in recs:
                v = f.get(nm)
                if isinstance(v, (int, float)):
                    xs.append(float(v)); ys.append(ret)
            ic = _spearman(xs, ys) if len(xs) >= MIN_PICKS_PER_DAY else None
            if ic is not None:
                daily_ic.setdefault(nm, []).append(ic)
    conn.close()
    out = []
    for nm, ics in daily_ic.items():
        n = len(ics)
        m = sum(ics) / n
        icir = None
        if n >= 2:
            sd = (sum((x - m) ** 2 for x in ics) / (n - 1)) ** 0.5
            if sd > 1e-9:
                icir = m / sd
        pos = 100.0 * sum(1 for x in ics if x > 0) / n
        out.append({"factor": nm, "n": n, "mean_ic": round(m, 4),
                    "icir": round(icir, 3) if icir is not None else None, "pct_pos": round(pos, 1)})
    out.sort(key=lambda r: (r["mean_ic"] if r["mean_ic"] is not None else 0), reverse=True)
    return {"strategy": sid, "valid_days": valid_days, "factors": out}


def main() -> int:
    quotes, _ = R.load_quote_union()
    calendar = sorted(quotes.keys())
    print(f"行情交易日(含补全) {calendar[0]}..{calendar[-1]}\n")
    for sid in ("greenfield_o2c_v1", "prebreakout_v41"):
        res = analyze(sid, quotes, calendar)
        print(f"===== {R.STRATEGY_REGISTRY[sid]['name']} ({sid})  有效复盘日={res['valid_days']}  横盘={R.STRATEGY_REGISTRY[sid]['horizon_label']} =====")
        print(f"  {'因子':<28}{'天数':>5}{'平均IC':>9}{'ICIR':>8}{'%正':>7}  判读")
        for f in res["factors"]:
            ic = f["mean_ic"]; icir = f["icir"]
            verdict = "✅有正向预测" if (ic > 0.03 and (icir or 0) > 0.3 and f["pct_pos"] >= 55) else \
                      ("🔴反向(应砍/反号)" if ic < -0.03 and (icir or 0) < -0.3 else "⚪噪声(近零/不稳)")
            print(f"  {f['factor']:<28}{f['n']:>5}{ic:>+9.4f}{(f'{icir:+.3f}' if icir is not None else '—'):>8}{f['pct_pos']:>6.0f}%  {verdict}")
        print()
    print("注：选中集内 IC（限制截面方差），强度被低估；反向因子(🔴)是明确坏信号，噪声(⚪)需更多样本，正向(✅)是可保留/加权候选。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
