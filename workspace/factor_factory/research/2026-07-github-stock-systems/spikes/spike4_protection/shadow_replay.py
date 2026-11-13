"""影子重放器 (spike4): 对历史推荐台账重放 ProtectionEngine, 回答"如果当时有保护会挡掉什么".

数据源 (只读, uri=true&mode=ro):
  /Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db
  - recommendation_fact: 推荐台账 (20260224~20260710, 92 交易日)
  - price_daily_cache:   日线 (mtm/后验超额/交易日历派生)

隔离 (验收3): MemLockStore 构造注入, 进程内零文件残留; 生产 DB 只读连接.
确定性 (验收2): 无墙钟时间戳, 全部迭代排序, 浮点定长格式化 —— 两次重放逐字节一致.
MARKET_STRESS: 阈值未拍板 -> 按 3%/5%/8% 多档并列输出分布 (不落锁, 供拍板).

用法:
  python shadow_replay.py --out out/run1          # 全量重放, 产出报告
  python shadow_replay.py --out out/run1 --no-write  # 只跑引擎不落文件 (残留检查用)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from calendar_util import TradingCalendar
from lock_store import MemLockStore
from protection_engine import ProtectionEngine
from protection_rules import (ConsecutiveLossGuard, LedgerView, LowEdgePause,
                              PostFailureCooldown, StrategyDrawdownBrake)

DB = "/Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db"

# --- 影子参数 (spike 演示口径, 非生产定稿; 生产参数须经拍板) ---
# 定标依据 (对本台账 92 交易日实测分布, 取尾部使锁标记最差片段而非淹没全账):
#   10日窗 mtm<=-5% 破线数: med=37, p90=93, max=113  -> trade_limit=90
#   20日窗 equity 回撤:      med=6.0%, p90=11.4%, max=12.5% -> max_drawdown=0.11
#   10日窗超额和:            med=-0.43, min=-5.13   -> required_excess=-2.0 (200样本均值<=-1%)
SHADOW_PARAMS = {
    "hold_days": 5,                     # 推荐持有窗 (交易日)
    "consecutive_loss": dict(lookback_days=10, loss_line=-0.05, trade_limit=90,
                             stop_duration_days=3),
    "drawdown_brake": dict(lookback_days=20, max_drawdown=0.11, trade_limit=10),
    "low_edge": dict(lookback_days=10, required_excess=-2.0, trade_limit=100,
                     stop_duration_days=3),
    "post_failure_cooldown": dict(cooldown_days=2),
    "market_stress_thresholds": [0.03, 0.05, 0.08],
}

# 已记录的管线级事故 (来源: 工作台账 memory/2026-07 与任务 #65/#68, 人工编目)
INCIDENTS = [
    {"day": "20260709", "scope": "strategy", "scope_key": "prebreakout_v41",
     "incident_id": "prebreakout_ai_chronic_failure_fixed", "fixed_confirmed": True},
    {"day": "20260710", "scope": "global", "scope_key": "*",
     "incident_id": "pipeline_0710_five_root_causes_fixed", "fixed_confirmed": True},
]


def board_limit_pct(stock_code: str, stock_name: str) -> float:
    """涨跌停幅度分板块 (规则表达式, 不用一刀切): 主板10/创业板科创板20/北交所30/ST 5."""
    name = stock_name or ""
    if "ST" in name.upper():
        return 5.0
    if stock_code.startswith(("300", "301")):
        return 20.0
    if stock_code.startswith(("688", "689")):
        return 20.0
    if stock_code.startswith(("43", "83", "87", "88", "920")):
        return 30.0
    return 10.0


def load_data():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    recs = [dict(zip(("strategy_id", "recommend_date", "stock_code", "stock_name",
                      "recommend_price"), r))
            for r in cur.execute(
                "SELECT strategy_id, recommend_date, stock_code, stock_name, recommend_price "
                "FROM recommendation_fact ORDER BY recommend_date, strategy_id, stock_code")]
    cal_days = [r[0] for r in cur.execute(
        "SELECT DISTINCT trade_date FROM price_daily_cache WHERE trade_date>='20251201' "
        "ORDER BY trade_date")]
    codes = sorted({r["stock_code"] for r in recs})
    prices: dict[tuple, tuple] = {}
    for chunk_start in range(0, len(codes), 500):
        chunk = codes[chunk_start:chunk_start + 500]
        q = (f"SELECT stock_code, trade_date, close, low, pct_chg FROM price_daily_cache "
             f"WHERE trade_date>='20260201' AND stock_code IN ({','.join('?' * len(chunk))})")
        for sc, td, close, low, pct in cur.execute(q, chunk):
            prices[(sc, td)] = (close, low, pct)
    mkt = {r[0]: r[1] / 100.0 for r in cur.execute(
        "SELECT trade_date, AVG(pct_chg) FROM price_daily_cache "
        "WHERE trade_date>='20260201' AND pct_chg IS NOT NULL GROUP BY trade_date")}
    con.close()
    return recs, cal_days, prices, mkt


def precompute(recs, cal: TradingCalendar, prices, mkt, hold_days: int, last_day: str):
    """逐推荐预计算: mtm 标记(ConsecutiveLossGuard)/已实现超额(LowEdgePause)/策略日收益(Brake).

    可知性纪律: 每条记录带 eval_day = 该信息在现实中可得之日; 重放循环按 as_of 截断.
    """
    rec_marks, rec_excess = [], []
    daily_acc: dict[tuple, list] = {}   # (strategy_id, day) -> [day_return,...]
    loss_line = SHADOW_PARAMS["consecutive_loss"]["loss_line"]
    for rec in recs:
        d0 = rec["recommend_date"]
        entry = rec["recommend_price"]
        if not cal.is_trading_day(d0) or entry is None or entry <= 0:
            continue
        # 持有窗 = D+1..D+hold 交易日
        window = []
        cur_d = d0
        for _ in range(hold_days):
            try:
                cur_d = cal.next_trading_day(cur_d)
            except Exception:
                break
            if cur_d > last_day:
                break
            window.append(cur_d)
        closes = []           # (day, close) 窗内可得收盘 (close<=0 视为坏数据剔除)
        for day in window:
            p = prices.get((rec["stock_code"], day))
            if p is not None and p[0] is not None and p[0] > 0:
                closes.append((day, p[0]))
        # --- mtm 标记 ---
        if len(window) == hold_days:      # 窗口完整走完才产出终局标记
            if not closes:
                rec_marks.append({"strategy_id": rec["strategy_id"],
                                  "stock_code": rec["stock_code"],
                                  "recommend_date": d0,
                                  "eval_day": window[-1], "value": None})  # 全窗无价: 缺失
            else:
                breach_day, worst = None, 0.0
                for day, close in closes:
                    m = close / entry - 1.0
                    worst = min(worst, m)
                    if breach_day is None and m <= loss_line:
                        breach_day = day
                        breach_val = m
                if breach_day is not None:
                    rec_marks.append({"strategy_id": rec["strategy_id"],
                                      "stock_code": rec["stock_code"],
                                      "recommend_date": d0,
                                      "eval_day": breach_day, "value": breach_val})
                else:
                    rec_marks.append({"strategy_id": rec["strategy_id"],
                                      "stock_code": rec["stock_code"],
                                      "recommend_date": d0,
                                      "eval_day": window[-1], "value": worst})
        # --- 已实现超额 (D+hold 收盘可知) ---
        if len(window) == hold_days and closes and closes[-1][0] == window[-1]:
            stock_ret = closes[-1][1] / entry - 1.0
            mkt_ret, ok = 1.0, True
            for day in window:
                if day in mkt:
                    mkt_ret *= (1.0 + mkt[day])
                else:
                    ok = False
            excess = stock_ret - (mkt_ret - 1.0) if ok else None
            rec_excess.append({"strategy_id": rec["strategy_id"], "eval_day": window[-1],
                               "excess": excess,
                               "stock_code": rec["stock_code"], "recommend_date": d0})
        elif len(window) == hold_days:
            rec_excess.append({"strategy_id": rec["strategy_id"], "eval_day": window[-1],
                               "excess": None,
                               "stock_code": rec["stock_code"], "recommend_date": d0})
        # --- 策略日收益 (equity 曲线原料): 窗内逐日收益 ---
        prev = entry
        for day in window:
            p = prices.get((rec["stock_code"], day))
            if p is not None and p[0] is not None and p[0] > 0 and prev > 0:
                daily_acc.setdefault((rec["strategy_id"], day), []).append(p[0] / prev - 1.0)
                prev = p[0]
            # 停牌/坏数据日: 无标记, 不 ffill (缺席即缺席)
    strategy_daily = []
    for (sid, day) in sorted(daily_acc):
        vals = daily_acc[(sid, day)]
        strategy_daily.append({"strategy_id": sid, "day": day,
                               "day_return": sum(vals) / len(vals)})
    return rec_marks, rec_excess, strategy_daily


def posthoc_excess_lookup(rec_excess):
    return {(r["strategy_id"], r["recommend_date"], r["stock_code"]): r["excess"]
            for r in rec_excess}


def market_stress_series(recs, cal: TradingCalendar, prices, hold_days: int,
                         replay_days: list[str]):
    """逐日 exit_blocked 占比 (LIMIT_DOWN_SEALED 代理口径 + SUSPENDED), 多档阈值分布."""
    # 持有集: rec 在 (rec_date, rec_date+hold] 交易日内算活跃持有
    holding_by_day: dict[str, list] = {d: [] for d in replay_days}
    for rec in recs:
        d0 = rec["recommend_date"]
        if not cal.is_trading_day(d0):
            continue
        cur_d = d0
        for _ in range(hold_days):
            try:
                cur_d = cal.next_trading_day(cur_d)
            except Exception:
                break
            if cur_d in holding_by_day:
                holding_by_day[cur_d].append(rec)
    series = []
    for day in replay_days:
        holds = holding_by_day[day]
        if not holds:
            continue
        n_blocked = 0
        blocked_kinds = []
        for rec in holds:
            p = prices.get((rec["stock_code"], day))
            if p is None:
                n_blocked += 1
                blocked_kinds.append((rec["stock_code"], "SUSPENDED"))
                continue
            close, low, pct = p
            limit = board_limit_pct(rec["stock_code"], rec["stock_name"])
            if (pct is not None and close is not None and low is not None
                    and pct <= -(limit - 0.15) and close == low):
                n_blocked += 1
                blocked_kinds.append((rec["stock_code"], "LIMIT_DOWN_SEALED"))
        series.append({"day": day, "n_holdings": len(holds), "n_blocked": n_blocked,
                       "share": n_blocked / len(holds),
                       "detail": ";".join(f"{c}:{k}" for c, k in sorted(blocked_kinds))})
    return series


def _project_weekdays(last_day: str, n: int) -> list[str]:
    """台账末日之后的确定性工作日投影 (仅用于锁 end 物化, 非真实日历; 报告标注)."""
    from datetime import date, timedelta
    d = date(int(last_day[:4]), int(last_day[4:6]), int(last_day[6:8]))
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return out


def run_shadow(out_dir: str | None):
    recs, cal_days, prices, mkt = load_data()
    # 尾部投影: 台账末日附近触发的锁 (如 0710 事故冷却) 需要未来交易日物化 end
    cal = TradingCalendar(cal_days + _project_weekdays(cal_days[-1], 15))
    rec_dates = sorted({r["recommend_date"] for r in recs})
    first, last = rec_dates[0], rec_dates[-1]
    replay_days = cal.days_between(first, last)
    hold = SHADOW_PARAMS["hold_days"]

    rec_marks, rec_excess, strategy_daily = precompute(recs, cal, prices, mkt, hold, last)
    excess_lut = posthoc_excess_lookup(rec_excess)

    rules = [
        ConsecutiveLossGuard(**SHADOW_PARAMS["consecutive_loss"]),
        StrategyDrawdownBrake(**SHADOW_PARAMS["drawdown_brake"]),
        LowEdgePause(**SHADOW_PARAMS["low_edge"]),
        PostFailureCooldown(**SHADOW_PARAMS["post_failure_cooldown"]),
    ]
    store = MemLockStore(cal)   # 影子: 纯内存注入, 零残留 (验收3)
    engine = ProtectionEngine(rules, store, cal)

    recs_by_day: dict[str, list] = {}
    for r in recs:
        recs_by_day.setdefault(r["recommend_date"], []).append(r)

    blocked_rows, event_rows, lock_rows = [], [], []
    for day in replay_days:
        # ① 开盘相位: 当日推荐过发布闸 (锁在昨日 after_review 评估产生)
        for rec in recs_by_day.get(day, []):
            if store.is_locked(rec["strategy_id"], day, "open", "entry"):
                lk = store.longest_lock(rec["strategy_id"], day, "open")
                ex = excess_lut.get((rec["strategy_id"], day, rec["stock_code"]))
                blocked_rows.append({
                    "lock_effective_date": day,
                    "blocked_rec": f"{rec['strategy_id']}/{rec['stock_code']}/{rec['stock_name'] or ''}",
                    "posthoc_excess_5d": "NA" if ex is None else f"{ex:.6f}",
                    "reason_code": lk.reason_code, "created_by": lk.created_by,
                    "lock_id": lk.id, "lock_end_date": lk.lock_end_date,
                })
        # ② 复盘后相位: as-of 截断台账 -> 引擎评估
        ledger = LedgerView(
            rec_marks=[m for m in rec_marks if m["eval_day"] <= day],
            rec_excess=[r for r in rec_excess if r["eval_day"] <= day],
            strategy_daily=[r for r in strategy_daily if r["day"] <= day],
            incidents=[i for i in INCIDENTS if i["day"] <= day],
        )
        run_rec = engine.run(day, ledger, now=f"{day}T20:45:00+08:00")
        for ev in run_rec.events:
            event_rows.append({"day": day, "display": ev.display()})
        for lk in run_rec.new_locks:
            lock_rows.append({"day": day, "lock_id": lk.id, "scope": lk.scope,
                              "scope_key": lk.scope_key, "reason_code": lk.reason_code,
                              "lock_end_date": lk.lock_end_date,
                              "unlock_at_phase": lk.unlock_at_phase,
                              "created_by": lk.created_by, "detail": lk.reason_detail})

    stress = market_stress_series(recs, cal, prices, hold, replay_days)

    result = {
        "replay_days": replay_days, "n_recs": len(recs),
        "blocked": blocked_rows, "events": event_rows, "locks": lock_rows,
        "stress": stress, "store": store,
    }
    if out_dir:
        write_outputs(out_dir, result)
    return result


def _fmt_csv(rows, cols):
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r[c]) for c in cols))
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: str, res: dict):
    os.makedirs(out_dir, exist_ok=True)
    files = {}
    files["blocked_recommendations.csv"] = _fmt_csv(
        res["blocked"], ["lock_effective_date", "blocked_rec", "posthoc_excess_5d",
                         "reason_code", "created_by", "lock_id", "lock_end_date"])
    files["protection_events.csv"] = _fmt_csv(res["events"], ["day", "display"])
    files["locks_fired.csv"] = _fmt_csv(
        res["locks"], ["day", "lock_id", "scope", "scope_key", "reason_code",
                       "lock_end_date", "unlock_at_phase", "created_by", "detail"])
    files["market_stress.csv"] = _fmt_csv(
        res["stress"], ["day", "n_holdings", "n_blocked", "share", "detail"])

    # 汇总 md
    ev_counts: dict[str, int] = {}
    for e in res["events"]:
        cat = e["display"].split(":")[0]
        ev_counts[cat] = ev_counts.get(cat, 0) + 1
    ths = SHADOW_PARAMS["market_stress_thresholds"]
    stress_lines = []
    shares = [s["share"] for s in res["stress"]]
    for th in ths:
        days_hit = [s["day"] for s in res["stress"] if s["share"] >= th]
        stress_lines.append(
            f"| {th:.0%} | {len(days_hit)} | {','.join(days_hit) if days_hit else '-'} |")
    md = []
    md.append("# spike4 影子重放报告 (确定性输出, 无墙钟)\n")
    md.append(f"- 重放交易日: {len(res['replay_days'])} ({res['replay_days'][0]}~{res['replay_days'][-1]})")
    md.append(f"- 台账推荐数: {res['n_recs']}")
    md.append(f"- 影子参数: {SHADOW_PARAMS}")
    md.append(f"- 落锁次数: {len(res['locks'])}; 被挡推荐: {len(res['blocked'])}")
    md.append(f"- 事件计数: {dict(sorted(ev_counts.items()))}\n")
    md.append("## MARKET_STRESS 多档阈值分布 (未拍板, 并列供决策)\n")
    if shares:
        srt = sorted(shares)
        md.append(f"- 日度 exit_blocked 占比: n={len(shares)}, max={max(shares):.4f}, "
                  f"p95={srt[int(0.95 * (len(srt) - 1))]:.4f}, "
                  f"p50={srt[len(srt) // 2]:.4f}, 非零天数={sum(1 for s in shares if s > 0)}")
    md.append("\n| 阈值 | 触发天数 | 触发日 |\n|---|---|---|")
    md.extend(stress_lines)
    md.append("\n## 落锁明细 (逐条溯源)\n")
    md.append("| day | scope | key | reason_code | end | created_by |\n|---|---|---|---|---|---|")
    for lk in res["locks"]:
        md.append(f"| {lk['day']} | {lk['scope']} | {lk['scope_key']} | {lk['reason_code']} "
                  f"| {lk['lock_end_date']}/{lk['unlock_at_phase']} | {lk['created_by']} |")
    md.append("\n## 被挡推荐三列报告 (锁生效日 / 被挡推荐 / 后验超额) 见 blocked_recommendations.csv\n")
    files["shadow_report.md"] = "\n".join(md) + "\n"

    h = hashlib.sha256()
    for name in sorted(files):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(files[name])
        h.update(name.encode())
        h.update(files[name].encode())
    digest = h.hexdigest()
    with open(os.path.join(out_dir, "SHA256"), "w") as f:
        f.write(digest + "\n")
    print(f"outputs -> {out_dir}  combined_sha256={digest}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    res = run_shadow(None if args.no_write else args.out)
    if args.no_write:
        print(f"engine-only run: locks={len(res['locks'])} blocked={len(res['blocked'])} "
              f"events={len(res['events'])}")
