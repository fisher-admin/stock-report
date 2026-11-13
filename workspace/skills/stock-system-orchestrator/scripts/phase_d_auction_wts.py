#!/usr/bin/env python3
"""Phase D · B2:竞价弱转强(auction weak-to-strong)研究(2026-07-02 夜)。

【精确问题】T日强势股(大涨/涨停),T+1 9:25 竞价结束时的可知特征(高开幅度×竞价量能),
能否找出 T+1 O2C 段(开盘买→收盘卖,完全可成交)净收益显著为正的子桶?
此前证伪的是"按T日信号无条件T+1开盘买"(隔夜溢价已price-in);竞价特征是T日收盘时不存在的新信息。

【口径纪律】
- 面板 _full(1266日,含5-6月缺口):T+1 必须真实存在于面板且为日历上的真·次交易日
  (用竞价数据的完整1087日日历定次日,缺口边缘的T一律剔除,防错位标签)。
- 出场收益自算:O2C = close_{T+1}/open_{T+1} − 1(面板T+1行),不依赖预置标签。
- 对账:竞价收盘价(=开盘价)与面板 open_{T+1} 中位偏差须<0.5%,否则数据有错,中止。
- 可成交:gap>+7% 剔除(一字/顶死);成本报 毛/净@30bp/净@50bp 双档。
- 竞价量能用【截面分位】(条件子集内),规避竞价vol与日线vol单位差异。
- 每个"净@30bp>0且t>2"的桶,做 universe 分半复现(固定seed)+日内打乱安慰剂,过了才算数。

用法:python3 phase_d_auction_wts.py [--strong-min 5]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

WS = Path("/Users/fisher/.openclaw/workspace")
PANEL = WS / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet"
AUCTION_DIR = WS / "stock_data" / "01-root" / "auction_cache"


def load_auction_open() -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(AUCTION_DIR.glob("auction_open_*.parquet"))]
    a = pd.concat(frames, ignore_index=True)
    a["trade_date"] = a["trade_date"].astype(str)
    # 竞价"close"=9:25撮合价=当日开盘价;vol/amount=竞价成交量/额
    return a[["ts_code", "trade_date", "close", "vol", "amount", "vwap"]].rename(
        columns={"close": "auct_price", "vol": "auct_vol", "amount": "auct_amount"})


def tstat(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 1e-12 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strong-min", type=float, default=5.0, help="T日强势最低涨幅(百分数)")
    ap.add_argument("--cost1", type=float, default=0.003, help="净档1:30bp往返")
    ap.add_argument("--cost2", type=float, default=0.005, help="净档2:50bp往返(含滑点)")
    args = ap.parse_args()

    print("加载竞价(开盘)…")
    auct = load_auction_open()
    cal = sorted(auct["trade_date"].unique())
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    print(f"竞价日历 {cal[0]}..{cal[-1]} {len(cal)}日  行数{len(auct):,}")

    print("加载面板…")
    cols = ["ts_code", "trade_date", "open", "close", "pct_chg", "vol", "universe_flag", "industry"]
    pn = pd.read_parquet(PANEL, columns=cols)
    pn["trade_date"] = pn["trade_date"].astype(str)
    pn = pn[pn["universe_flag"].fillna(0).astype(float) > 0]
    pdates = set(pn["trade_date"].unique())

    # T日强势子集(2022起,与竞价重叠)
    strong = pn[(pn["trade_date"] >= "20220104") & (pn["pct_chg"] >= args.strong_min)].copy()
    strong["t1"] = strong["trade_date"].map(nxt)
    # T+1 必须是真·次交易日且在面板中(剔缺口边缘)
    strong = strong.dropna(subset=["t1"])
    strong = strong[strong["t1"].isin(pdates)]
    print(f"T日强势(≥{args.strong_min}%)样本 {len(strong):,}  覆盖 {strong['trade_date'].nunique()} 个T日")

    # 接 T+1 面板行(开/收) 与 T+1 竞价特征
    p1 = pn[["ts_code", "trade_date", "open", "close"]].rename(
        columns={"trade_date": "t1", "open": "open1", "close": "close1"})
    m = strong.merge(p1, on=["ts_code", "t1"], how="inner")
    m = m.merge(auct.rename(columns={"trade_date": "t1"}), on=["ts_code", "t1"], how="inner")
    m = m[(m["open1"] > 0) & (m["close"] > 0) & (m["auct_price"] > 0)]
    print(f"接上T+1面板+竞价后样本 {len(m):,}")

    # 对账:竞价撮合价 vs 面板开盘价
    dev = (m["auct_price"] - m["open1"]).abs() / m["open1"]
    print(f"对账 |竞价价−开盘价|/开盘: 中位 {dev.median():.5f}  90分位 {dev.quantile(0.9):.5f}")
    if dev.median() > 0.005:
        print("❌ 对账失败(>0.5%),竞价与面板可能错位,中止"); return 1

    # 特征(9:25全部可知)
    m["gap"] = m["auct_price"] / m["close"] - 1.0                       # 高开幅度
    m["o2c"] = m["close1"] / m["open1"] - 1.0                           # T+1 日内(可成交段)
    # 可成交:剔一字/顶死高开
    m = m[m["gap"] <= 0.07]
    # 竞价量能:条件子集内按 T日桶×T+1日 做截面分位(规避单位问题)
    def bucket_T(x):
        if x >= 9.5: return "涨停"
        if x >= 7.0: return "强7-9.5"
        return "中5-7"
    m["Tbkt"] = m["pct_chg"].apply(bucket_T)
    m["avr_q"] = m.groupby(["t1", "Tbkt"])["auct_vol"].rank(pct=True)   # 竞价量分位(同日同桶内)
    def bucket_gap(g):
        if g <= -0.02: return "低开<-2%"
        if g < 0.0:    return "微低开-2~0"
        if g < 0.02:   return "平开0~2"
        if g < 0.05:   return "高开2~5"
        return "高开5~7"
    m["Gbkt"] = m["gap"].apply(bucket_gap)
    m["Vbkt"] = pd.cut(m["avr_q"], [0, 0.33, 0.67, 1.0], labels=["竞价缩量", "竞价中量", "竞价放量"])

    print(f"\n=== 弱转强矩阵:T日桶 × 高开桶 × 竞价量桶 → T+1 O2C ===")
    print(f"{'T日':<8}{'高开':<10}{'竞价量':<8}{'n':>7}{'毛O2C%':>9}{'t':>7}{'胜率%':>7}{'净@30bp%':>10}{'净@50bp%':>10}")
    cands = []
    for (tb, gb, vb), g in m.groupby(["Tbkt", "Gbkt", "Vbkt"], observed=True):
        if len(g) < 200: continue
        x = g["o2c"].values
        mu = np.nanmean(x); t = tstat(x)
        hit = 100 * np.nanmean(x > 0)
        n30 = mu - args.cost1; n50 = mu - args.cost2
        flag = " ←" if (n30 > 0 and t > 2) else ""
        print(f"{tb:<8}{gb:<10}{str(vb):<8}{len(g):>7}{mu*100:>+9.3f}{t:>7.1f}{hit:>6.0f}%{n30*100:>+10.3f}{n50*100:>+10.3f}{flag}")
        if n30 > 0 and t > 2:
            cands.append(((tb, gb, str(vb)), g))

    # 候选桶:分半复现 + 安慰剂
    if not cands:
        print("\n→ 无任何桶达到 净@30bp>0 且 t>2:竞价弱转强在可成交口径下无候选。")
    else:
        print(f"\n=== {len(cands)} 个候选桶 → 分半复现+安慰剂 ===")
        rng = np.random.RandomState(20260702)
        for key, g in cands:
            codes = g["ts_code"].unique()
            rng.shuffle(codes)
            half = set(codes[: len(codes) // 2])
            a = g[g["ts_code"].isin(half)]["o2c"].values
            b = g[~g["ts_code"].isin(half)]["o2c"].values
            na, nb = np.nanmean(a) - args.cost1, np.nanmean(b) - args.cost1
            ta, tb_ = tstat(a), tstat(b)
            # 安慰剂:同日同T桶内打乱竞价量分位,看该桶选出的组合是否仍"正"
            plac = []
            base = m[(m["Tbkt"] == key[0]) & (m["Gbkt"] == key[1])]
            for _ in range(10):
                sh = base.copy()
                sh["avr_q"] = sh.groupby("t1")["avr_q"].transform(lambda s: rng.permutation(s.values))
                sh["Vb"] = pd.cut(sh["avr_q"], [0, 0.33, 0.67, 1.0], labels=["竞价缩量", "竞价中量", "竞价放量"])
                sel = sh[sh["Vb"].astype(str) == key[2]]["o2c"].values
                if len(sel) > 50:
                    plac.append(np.nanmean(sel) - args.cost1)
            pm, ps = (np.mean(plac), np.std(plac)) if plac else (np.nan, np.nan)
            real = np.nanmean(g["o2c"].values) - args.cost1
            z = (real - pm) / ps if plac and ps > 1e-9 else 0.0
            ok = (na > 0 and nb > 0 and ta > 1 and tb_ > 1 and z > 2)
            print(f"  {key}: 半A净{na*100:+.3f}%(t{ta:.1f}) 半B净{nb*100:+.3f}%(t{tb_:.1f}) "
                  f"安慰剂z={z:+.1f}σ → {'✅ 存活(转入冻结OOS跟踪,仍非部署)' if ok else '⚪ 未复现/与随机不可区分'}")

    # 整体:竞价量分位在各T桶内对O2C的IC(截面)
    print("\n=== 竞价量分位 vs T+1 O2C 的日均截面IC(各T桶) ===")
    for tb, g in m.groupby("Tbkt"):
        ics = []
        for _, d in g.groupby("t1"):
            if len(d) >= 30:
                ic = d["avr_q"].corr(d["o2c"], method="spearman")
                if pd.notna(ic): ics.append(ic)
        if len(ics) >= 30:
            mu = np.mean(ics); t = mu / (np.std(ics, ddof=1) / np.sqrt(len(ics)))
            print(f"  {tb:<8} IC {mu:+.4f}  t {t:+.2f}  日 {len(ics)}")

    print("\n口径:面板_full(缺口边缘T剔除)、T+1为真·次交易日、竞价价与开盘价已对账、"
          f"gap>7%剔除、成本{args.cost1*1e4:.0f}/{args.cost2*1e4:.0f}bp双档、竞价量用同日同桶截面分位。"
          "\n判定纪律:候选桶须过 分半复现+安慰剂;即便存活也只是'转入冻结OOS跟踪',不是可部署。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
