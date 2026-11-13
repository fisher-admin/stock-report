#!/usr/bin/env python3
"""竞价弱转强(WTS)前瞻OOS纸面跟踪(2026-07-02 冻结,规则见 wts_frozen_rules.json)。

每个交易日收盘后运行:
  1) 增量更新竞价缓存(调 ingest_auction_data.py 拉最新日期);
  2) 对"今天"(=T+1)生成冻结规则信号:昨日强势股 × 今日9:25竞价特征;
  3) 用今日 开盘→收盘(O2C) 记录真实后验,追加到 ledger CSV(不可回改);
  4) 打印累积统计(距复议门槛60笔还差多少)。
纪律:规则参数一律读冻结JSON,本脚本不含任何可调阈值;修改规则=作废重计。
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

WS = Path("/Users/fisher/.openclaw/workspace")
RULES = WS / "factor_factory" / "config" / "wts_frozen_rules.json"
AUCTION_DIR = WS / "stock_data" / "01-root" / "auction_cache"
LEDGER = WS / "stock_data" / "03-working" / "health" / "wts_tracking_ledger.csv"
CACHE_DIR = WS / "stock_data" / "03-working" / "backtest_cache"
PY = sys.executable
INGEST = WS / "skills" / "stock-system-orchestrator" / "scripts" / "ingest_auction_data.py"


def load_daily(date: str) -> pd.DataFrame | None:
    p = CACHE_DIR / f"daily_{date}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["ts_code", "open", "close", "pre_close", "pct_chg"])
    return df[df["close"] > 0]


def main() -> int:
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    # 1) 增量更新竞价(近10天,checkpoint自动去重)
    try:
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
        end = date.today().strftime("%Y%m%d")
        subprocess.run([PY, str(INGEST), "--only", "open", "--start", start, "--end", end],
                       timeout=600, capture_output=True)
    except Exception as e:
        print(f"[wts] 竞价增量更新失败(非致命,用现有缓存): {e}")

    frames = [pd.read_parquet(p) for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    auct = pd.concat(frames, ignore_index=True)
    auct["trade_date"] = auct["trade_date"].astype(str)
    cal = sorted(auct["trade_date"].unique())
    t1 = cal[-1]                      # 今天(信号日/交易日)
    t0 = cal[-2]                      # 昨天(条件日)
    d0, d1 = load_daily(t0), load_daily(t1)
    if d0 is None or d1 is None:
        print(f"[wts] daily 缓存缺 {t0} 或 {t1},跳过本日"); return 0

    a1 = auct[auct["trade_date"] == t1][["ts_code", "close", "vol"]].rename(
        columns={"close": "auct_price", "vol": "auct_vol"})
    m = (d0[d0["pct_chg"] >= 5.0][["ts_code", "close", "pct_chg"]].rename(columns={"close": "close0", "pct_chg": "pct0"})
         .merge(a1, on="ts_code")
         .merge(d1[["ts_code", "open", "close"]].rename(columns={"open": "open1", "close": "close1"}), on="ts_code"))
    m = m[(m["open1"] > 0) & (m["close0"] > 0) & (m["auct_price"] > 0)]
    m["gap"] = m["auct_price"] / m["close0"] - 1
    m = m[m["gap"] <= 0.07]
    m["o2c"] = m["close1"] / m["open1"] - 1
    m["Tbkt"] = m["pct0"].apply(lambda x: "涨停" if x >= 9.5 else ("强7-9.5" if x >= 7 else "中5-7"))
    m["Gbkt"] = m["gap"].apply(lambda g: "低开<-2%" if g <= -0.02 else ("微低开-2~0" if g < 0 else ("平开0~2" if g < 0.02 else ("高开2~5" if g < 0.05 else "高开5~7"))))
    m["avr_q"] = m.groupby("Tbkt")["auct_vol"].rank(pct=True)
    m["Vbkt"] = pd.cut(m["avr_q"], [0, 0.33, 0.67, 1.0], labels=["竞价缩量", "竞价中量", "竞价放量"]).astype(str)

    rows = []
    for r in rules["rules"]:
        tb, gb, vb = r["cond"]
        hit = m[(m["Tbkt"] == tb) & (m["Gbkt"] == gb) & (m["Vbkt"] == vb)]
        for _, row in hit.iterrows():
            rows.append({"signal_date": t1, "rule_id": r["id"], "ts_code": row["ts_code"],
                         "gap_pct": round(row["gap"] * 100, 3), "o2c_pct": round(row["o2c"] * 100, 3),
                         "net30_pct": round(row["o2c"] * 100 - 0.30, 3)})
    # 追加 ledger(同日重复运行去重)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    old = []
    if LEDGER.exists():
        with open(LEDGER, newline="", encoding="utf-8") as f:
            old = [x for x in csv.DictReader(f)]
    old = [x for x in old if x["signal_date"] != t1]
    allrows = old + rows
    with open(LEDGER, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["signal_date", "rule_id", "ts_code", "gap_pct", "o2c_pct", "net30_pct"])
        w.writeheader()
        w.writerows(allrows)
    # 累积统计
    df = pd.DataFrame(allrows)
    print(f"[wts] {t1} 新信号 {len(rows)} 笔;ledger 累积 {len(df)} 笔")
    if len(df):
        df["net30_pct"] = df["net30_pct"].astype(float)
        for rid, g in df.groupby("rule_id"):
            n = len(g); mu = g["net30_pct"].mean()
            sd = g["net30_pct"].std(ddof=1) if n > 1 else 0
            t = mu / (sd / n ** 0.5) if sd > 1e-9 else 0
            print(f"  {rid}: n={n}/60  净@30bp均值 {mu:+.3f}%  t={t:.2f}  胜率{100*(g['net30_pct']>0).mean():.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
