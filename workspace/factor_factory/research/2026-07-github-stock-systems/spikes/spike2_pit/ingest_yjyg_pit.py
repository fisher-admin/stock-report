#!/usr/bin/env python3
"""Spike-2 ingest: akshare stock_yjyg_em 业绩预告 → PIT 双时间表 (2026-07-11)。

与现行生产脚本 ingest_pead_preannounce.py 的关键差异:
- 生产: 同 (ts_code, end_date) 多次预告 keep='first' → 修订链被压扁。
- 本 spike: **保留全部公告**, revision_seq 显式建链; 生产的"首次预告"口径由
  pit_asof(mode='as_first_reported') 等价复现且带 is_preliminary 标记。

契约断言(akshare_audit.md §2.11 验收条件):
- 原始载荷 27 键 + reset_index = **28 列位置契约**, 列数变即上游改 schema → 直接拒纳。
- 关键列非空率阈值: 公告日期/预测指标 >= 99%, 预测数值 >= 80%。

PIT 锚: 公告日期(NOTICE_DATE) — 源头精度为日(时间恒为 00:00:00, 本脚本实测),
available_at 以 Asia/Shanghai 当日 00:00 构造并显式标 precision='date'
(openbb.md §3.1: 字段类型禁降精度; 源头精度用 precision 列诚实记录, 不伪造分钟)。

用法: python ingest_yjyg_pit.py [--start 2024] [--end 20260630] [--fallback-cache]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR))
from pit_store import TZ, PITViolation, append_rows  # noqa: E402

OUT = SPIKE_DIR / "data" / "pit_yjyg.parquet"
LOCAL_CACHE = Path("/Users/fisher/.openclaw/workspace/stock_data/03-working/"
                   "fundamental_cache/pead_preannounce.parquet")  # 只读
EXPECTED_POSITIONAL_COLS = 28  # akshare_audit.md §2.11: 27 raw keys + index
KEY_FIELD_EXACT = "归属于上市公司股东的净利润"  # 精确匹配, 避免 contains 误吸扣非


def raw_contract_probe(period: str) -> dict:
    """直连东财原始端点, 断言 28 列位置契约(akshare 位置重命名的前提)。"""
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    params = {"sortColumns": "NOTICE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
              "pageSize": "50", "pageNumber": "1",
              "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
              "filter": f" (REPORT_DATE='{period[:4]}-{period[4:6]}-{period[6:]}')"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    rows = r.json()["result"]["data"]
    raw = pd.DataFrame(rows)
    raw.reset_index(inplace=True)
    n = len(raw.columns)
    if n != EXPECTED_POSITIONAL_COLS:
        raise PITViolation(
            f"yjyg 28 列位置契约破坏: 原始载荷+index={n} 列 (期 {period}); "
            "上游改 schema, 位置重命名将全表错位 → 拒纳")
    notice = pd.to_datetime(raw["NOTICE_DATE"], errors="coerce")
    time_all_midnight = bool(((notice.dt.hour == 0) & (notice.dt.minute == 0)).all())
    return {"period": period, "n_cols": n, "n_rows": len(raw),
            "notice_date_all_midnight": time_all_midnight}


def periods(start_year: int, end: str) -> list[str]:
    out = []
    for y in range(start_year, int(end[:4]) + 1):
        for mmdd in ("0331", "0630", "0930", "1231"):
            p = f"{y}{mmdd}"
            if p <= end:
                out.append(p)
    return out


def _code6(x) -> str | None:
    m = re.search(r"(\d{6})", str(x))
    return m.group(1) if m else None


def transform(df: pd.DataFrame, period: str, source: str) -> pd.DataFrame:
    """akshare 输出 → PIT 行 (保留全部公告, 不做 keep='first')。"""
    need = ["股票代码", "公告日期", "预测指标", "预测数值", "上年同期值", "业绩变动幅度", "预告类型"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise PITViolation(f"yjyg 关键列缺失: {missing} (期 {period})")
    # 关键列非空率阈值 (akshare_audit.md §2.11)
    for col, thr in (("公告日期", 0.99), ("预测指标", 0.99)):
        rate = df[col].notna().mean()
        if rate < thr:
            raise PITViolation(f"yjyg 关键列 {col} 非空率 {rate:.1%} < {thr:.0%} (期 {period})")

    d = df[df["预测指标"].astype(str) == KEY_FIELD_EXACT].copy()
    if len(d) == 0:
        return pd.DataFrame()
    d["symbol"] = d["股票代码"].map(_code6)
    ann = pd.to_datetime(d["公告日期"], errors="coerce")
    d["announce_date"] = ann.dt.strftime("%Y%m%d")
    fv = pd.to_numeric(d["预测数值"], errors="coerce")
    pv = pd.to_numeric(d["上年同期值"], errors="coerce")
    gp = pd.to_numeric(d["业绩变动幅度"], errors="coerce")
    derived = (fv - pv) / pv.abs() * 100.0  # 负基数防翻转(沿用生产口径)
    d["np_forecast"] = fv
    d["np_growth_pct"] = gp.where(gp.notna(), derived)
    d = d.dropna(subset=["symbol", "announce_date"])

    rows = []
    for fname in ("np_forecast", "np_growth_pct"):
        r = d[["symbol", "announce_date", fname]].rename(columns={fname: "value"}).copy()
        r["field"] = fname
        r = r.dropna(subset=["value"])
        rows.append(r)
    out = pd.concat(rows, ignore_index=True)
    out["period"] = int(period)
    out["announce_date"] = out["announce_date"].astype("int64")
    out["value"] = out["value"].astype("float64")
    # 双时间: 源头精度=日 → tz-aware 当日 00:00 + precision='date' (禁降精度反向: 类型保持 datetime)
    out["available_at"] = pd.to_datetime(
        out["announce_date"].astype(str), format="%Y%m%d").dt.tz_localize(TZ)
    out["available_at_precision"] = "date"
    out["is_preliminary"] = True  # 业绩预告 = 预告口径(openbb §3.1 机制三)
    out["source"] = source
    # 同键同值批内完全重复(同日同股多条净利润预告行) → append 层会去重; 同键异值 hard fail
    return out


def pull_akshare(start_year: int, end: str) -> tuple[pd.DataFrame, list[dict]]:
    import akshare as ak
    frames, probes = [], []
    plist = periods(start_year, end)
    probes.append(raw_contract_probe(plist[-1]))  # 契约探针: 至少验一期原始 28 列
    for per in plist:
        try:
            df = ak.stock_yjyg_em(date=per)
        except Exception as e:
            print(f"  {per}: 拉取失败 {type(e).__name__} {str(e)[:60]}")
            continue
        if df is None or len(df) == 0:
            print(f"  {per}: 空")
            continue
        t = transform(df, per, source="akshare.stock_yjyg_em")
        if len(t):
            frames.append(t)
        print(f"  {per}: akshare {len(df)} 行 → PIT {len(t)} 行")
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame(), probes
    return pd.concat(frames, ignore_index=True), probes


def pull_local_cache(start_year: int, end: str) -> pd.DataFrame:
    """兜底: 生产缓存(keep='first', 无自然修订链)构造 — 修订/毒样本测试届时合成注入。"""
    df = pd.read_parquet(LOCAL_CACHE)  # 只读
    df = df[(df["end_date"] >= f"{start_year}0101") & (df["end_date"] <= end)]
    rows = []
    for fname, col in (("np_forecast", "forecast_value"), ("np_growth_pct", "growth_pct")):
        r = df[["ts_code", "ann_date", "end_date", col]].rename(
            columns={"ts_code": "symbol", col: "value"}).copy()
        r["field"] = fname
        r = r.dropna(subset=["value"])
        rows.append(r)
    out = pd.concat(rows, ignore_index=True)
    out["period"] = out["end_date"].astype("int64")
    out["announce_date"] = out["ann_date"].astype("int64")
    out["value"] = out["value"].astype("float64")
    out["available_at"] = pd.to_datetime(
        out["announce_date"].astype(str), format="%Y%m%d").dt.tz_localize(TZ)
    out["available_at_precision"] = "date"
    out["is_preliminary"] = True
    out["source"] = "local_cache.pead_preannounce"
    return out.drop(columns=["ann_date", "end_date"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2024)
    ap.add_argument("--end", type=str, default="20260630")
    ap.add_argument("--fallback-cache", action="store_true")
    ap.add_argument("--out", type=str, default=str(OUT))
    args = ap.parse_args()

    probes = []
    if args.fallback_cache:
        rows = pull_local_cache(args.start, args.end)
        print(f"本地缓存兜底: {len(rows)} 行")
    else:
        rows, probes = pull_akshare(args.start, args.end)
        if len(rows) == 0:
            print("akshare 全空 → 本地缓存兜底")
            rows = pull_local_cache(args.start, args.end)

    if len(rows) == 0:
        print("无数据")
        return 1
    stats = append_rows(args.out, rows)
    n_rev = None
    st = pd.read_parquet(args.out)
    n_rev = int((st.groupby(["symbol", "field", "period"])["revision_seq"].max() > 0).sum())
    print(f"契约探针: {probes}")
    print(f"append 结果: {stats}")
    print(f"含自然修订链(revision_seq>0)的组数: {n_rev}")
    print(f"表: {args.out}  行数 {len(st)}  期 {st['period'].min()}..{st['period'].max()}  "
          f"公告日 {st['announce_date'].min()}..{st['announce_date'].max()}  "
          f"唯一股 {st['symbol'].nunique()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
