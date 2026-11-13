#!/usr/bin/env python3
"""B1·真实滑点/开盘容量标定(2026-07-03)。

用 800 只样本股的 5 分钟线,标定"T+1 开盘买入"的真实执行成本:
  slip_X = (开盘后X分钟真实VWAP / 开盘价) − 1   (买方视角,正=比开盘价买贵)
分层:流动性三档(面板 avg_amount_lookback) × 开盘涨跌档(当日开盘 vs 前收)。
另产出:开盘竞价可成交容量表(竞价成交额分位,按流动性档)。
输出:stock_data/03-working/health/slippage_calibration.{md,json} —— 供 factor_gauntlet
成本模块与一切回测引用,替代拍脑袋滑点。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

WS = Path("/Users/fisher/.openclaw/workspace")
MIN_DIR = WS / "stock_data" / "01-root" / "minute_cache"
AUCTION_DIR = WS / "stock_data" / "01-root" / "auction_cache"
PANEL = WS / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet"
OUT_MD = WS / "stock_data" / "03-working" / "health" / "slippage_calibration.md"
OUT_JSON = WS / "stock_data" / "03-working" / "health" / "slippage_calibration.json"


def main() -> int:
    # 流动性分档(全历史均值,粗分即可)
    liq = pd.read_parquet(PANEL, columns=["ts_code", "avg_amount_lookback"])
    liq = liq.groupby("ts_code")["avg_amount_lookback"].mean()
    q1, q2 = liq.quantile(1 / 3), liq.quantile(2 / 3)
    def liq_bkt(v):
        return "低流动" if v <= q1 else ("中流动" if v <= q2 else "高流动")

    files = sorted(MIN_DIR.glob("minute_5min_*.parquet"))
    print(f"分钟文件 {len(files)} 只")
    rows = []
    for i, p in enumerate(files):
        df = pd.read_parquet(p)
        if len(df) < 100:
            continue
        code = df["ts_code"].iloc[0]
        lb = liq_bkt(liq.get(code, np.nan)) if code in liq.index else None
        if lb is None:
            continue
        df["dt"] = pd.to_datetime(df["trade_time"])
        df["date"] = df["dt"].dt.strftime("%Y%m%d")
        df = df.sort_values("dt")
        # 单位自检(首文件): vwap=amount/vol 应≈价格;若差100倍则vol为手
        for date, day in df.groupby("date"):
            day = day[(day["dt"].dt.time >= pd.Timestamp("09:30").time())]
            if len(day) < 6:
                continue
            b = day.iloc[:6]  # 前30分钟(6根5min)
            o = float(b["open"].iloc[0])
            if o <= 0:
                continue
            pc = None  # 前收难取,用首bar开盘 vs 当日聚合另算 gap 时用面板;此处仅按开盘涨跌不可得,改用当日首bar方向
            def vwap(n):
                v = b["vol"].iloc[:n].sum()
                a = b["amount"].iloc[:n].sum()
                if v <= 0:
                    return np.nan
                px = a / v
                # 单位自检:若 px 偏离价格100倍,说明vol单位是手
                if px > o * 20:
                    px = a / (v * 100)
                return px
            rows.append({"ts_code": code, "date": date, "liq": lb, "open": o,
                         "slip5": vwap(1) / o - 1, "slip15": vwap(3) / o - 1, "slip30": vwap(6) / o - 1})
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(files)}")
    d = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan).dropna(subset=["slip15"])
    print(f"标定样本 {len(d):,} 股·日")

    # 接面板 pct_chg 前收推 gap:用面板 open/pre_close
    pn = pd.read_parquet(PANEL, columns=["ts_code", "trade_date", "open", "pre_close", "pct_chg"])
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn = pn.rename(columns={"trade_date": "date"})
    d = d.merge(pn[["ts_code", "date", "pre_close", "pct_chg"]], on=["ts_code", "date"], how="left")
    d["gap"] = d["open"] / d["pre_close"] - 1
    def gap_bkt(g):
        if pd.isna(g): return "未知"
        if g <= -0.02: return "低开"
        if g < 0.02: return "平开"
        if g < 0.05: return "高开2-5"
        return "高开>5"
    d["gapb"] = d["gap"].apply(gap_bkt)

    res = {"n_total": int(len(d)), "strata": []}
    lines = ["# 滑点标定(买方,开盘后VWAP vs 开盘价)", "",
             f"样本: {len(d):,} 股·日(800只×5min)  生成: 20260703", "",
             "| 流动性 | 开盘档 | n | slip5中位 | slip15中位 | slip15 p75 | slip15 p90 | slip30中位 |",
             "|---|---|---|---|---|---|---|---|"]
    for (lb, gb), g in d.groupby(["liq", "gapb"]):
        if len(g) < 300 or gb == "未知":
            continue
        row = {"liq": lb, "gap": gb, "n": int(len(g)),
               "slip5_med_bp": round(float(g["slip5"].median()) * 1e4, 1),
               "slip15_med_bp": round(float(g["slip15"].median()) * 1e4, 1),
               "slip15_p75_bp": round(float(g["slip15"].quantile(0.75)) * 1e4, 1),
               "slip15_p90_bp": round(float(g["slip15"].quantile(0.90)) * 1e4, 1),
               "slip30_med_bp": round(float(g["slip30"].median()) * 1e4, 1)}
        res["strata"].append(row)
        lines.append(f"| {lb} | {gb} | {row['n']} | {row['slip5_med_bp']}bp | {row['slip15_med_bp']}bp | "
                     f"{row['slip15_p75_bp']}bp | {row['slip15_p90_bp']}bp | {row['slip30_med_bp']}bp |")

    # 竞价容量表
    lines += ["", "## 开盘竞价可成交容量(竞价成交额,万元)", "",
              "| 流动性 | p25 | 中位 | p75 |", "|---|---|---|---|"]
    au = pd.concat([pd.read_parquet(p) for p in sorted(AUCTION_DIR.glob("auction_open_2026*.parquet"))])
    au["liq"] = au["ts_code"].map(lambda c: liq_bkt(liq[c]) if c in liq.index else None)
    au = au.dropna(subset=["liq"])
    cap = {}
    for lb, g in au.groupby("liq"):
        amt = g["amount"] / 1e4
        cap[lb] = {"p25_wan": round(float(amt.quantile(0.25)), 1), "med_wan": round(float(amt.median()), 1),
                   "p75_wan": round(float(amt.quantile(0.75)), 1)}
        lines.append(f"| {lb} | {cap[lb]['p25_wan']} | {cap[lb]['med_wan']} | {cap[lb]['p75_wan']} |")
    res["auction_capacity_2026"] = cap
    lines += ["", "口径: slip=开盘后X分钟VWAP/开盘价-1(买方为正即买贵);容量=2026年开盘竞价成交额分位。",
              "用法: 回测成本=佣金印花+此表对应档slip15中位(保守用p75);仓位≤竞价额的10-20%。"]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {OUT_MD}\n写出 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
