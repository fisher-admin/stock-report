#!/usr/bin/env python3
"""隔夜研究 A/B 族所需的事件与指数数据拉取(research-only, 只读接口, 结果缓存到 outputs/overnight_v0/events/)。
用法: python3 fetch_events.py [group ...]   group in: small global disclosure perstock toplist
"""
from __future__ import annotations
import glob, os, sys, time, warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd
warnings.filterwarnings("ignore")
WS = Path(os.environ.get("STOCK_WS") or Path(__file__).resolve().parents[3])  # 本机=~/.openclaw/workspace, VPS=/srv/hermes-stock/workspace
sys.path.insert(0, str(WS / "skills/stock-analyzer"))
import credentials, data_provider  # noqa: E402

OUT = WS / "factor_factory/outputs/overnight_v0/events"; OUT.mkdir(parents=True, exist_ok=True)
MIN_DIR = WS / "stock_data/01-root/minute_cache"
try: _pk = credentials.get_promax_api_key()
except Exception: _pk = None
cli = data_provider.create_data_client(provider="auto", rds_key=credentials.get_rds_api_key(), promax_key=_pk)

def call(api, p, tries=4):
    err = None
    for i in range(tries):
        try: return cli._direct(api, p)
        except Exception as e:  # noqa: BLE001
            err = e; time.sleep(1.5 * (i + 1))
    print("FAIL", api, p, str(err)[:120], flush=True); return None

def many(api, plist, name, workers=6):
    f = OUT / f"{name}.parquet"
    if f.exists(): print("cached", name); return
    t = time.time()
    with ThreadPoolExecutor(workers) as ex: res = list(ex.map(lambda p: call(api, p), plist))
    bad = sum(r is None for r in res); res = [r for r in res if r is not None and len(r)]
    df = pd.concat(res, ignore_index=True).drop_duplicates() if res else pd.DataFrame()
    for c in df.columns:
        if df[c].dtype == object: df[c] = df[c].astype("string")
    df.to_parquet(f); print(f"{name}: calls={len(plist)} failed={bad} rows={len(df)} {time.time()-t:.0f}s", flush=True)

def research_codes():
    cs = sorted(os.path.basename(f)[12:-8].replace("_", ".") for f in glob.glob(str(MIN_DIR / "minute_5min_*.parquet")))
    return [c for c in cs if c[:2] in ("60", "00")]

def trade_days(a="20211201", b="20260710"):
    d = pd.read_parquet(OUT / "index_daily.parquet"); d = d[d.ts_code == "000300.SH"]
    return sorted(x for x in d.trade_date.astype(str).unique() if a <= x <= b)

def main():
    groups = sys.argv[1:] or ["small", "disclosure", "perstock", "toplist"]
    S, E = "20211201", "20260710"
    if "small" in groups:
        many("index_daily", [{"ts_code": c, "start_date": S, "end_date": E} for c in ["000300.SH", "000905.SH", "000852.SH", "000001.SH"]], "index_daily")
        many("fut_daily", [{"ts_code": c, "start_date": S, "end_date": E} for c in ["IF.CFX", "IC.CFX", "IM.CFX"]], "fut_daily")
        mths = pd.period_range("2021-11", "2026-07", freq="M")
        many("index_weight", [{"index_code": c, "start_date": m.start_time.strftime("%Y%m%d"), "end_date": m.end_time.strftime("%Y%m%d")}
                              for c in ["000300.SH", "000905.SH", "000852.SH"] for m in mths if m.month in (5, 6, 11, 12)], "index_weight")
        rng = [(m.start_time.strftime("%Y%m%d"), m.end_time.strftime("%Y%m%d")) for m in pd.period_range("2021-12", "2026-07", freq="M")]
        many("repurchase", [{"start_date": a, "end_date": b} for a, b in rng], "repurchase")
        half = []
        for a, b in rng: half += [(a, a[:6] + "10"), (a[:6] + "11", a[:6] + "20"), (a[:6] + "21", b)]
        many("stk_holdertrade", [{"start_date": a, "end_date": b} for a, b in half], "stk_holdertrade")
    if "global" in groups:
        yrs = [("20211201", "20221231"), ("20230101", "20231231"), ("20240101", "20241231"), ("20250101", "20251231"), ("20260101", E)]
        many("index_global", [{"ts_code": c, "start_date": a, "end_date": b} for c in ["SPX", "HSI", "XIN9"] for a, b in yrs], "index_global", workers=1)
    if "limit" in groups:
        wk = pd.date_range("2021-12-01", "2026-07-10", freq="7D")
        many("limit_list_d", [{"start_date": a.strftime("%Y%m%d"), "end_date": (a + pd.Timedelta(days=6)).strftime("%Y%m%d")} for a in wk], "limit_list_d", workers=2)
    if "holdout_ext" in groups:   # 时间留出段(2026-06 至 2026-11-13)的补充数据, 单独文件, 不覆盖训练期缓存
        S2, E2 = "20260601", "20261113"
        many("index_daily", [{"ts_code": c, "start_date": S2, "end_date": E2} for c in ["000300.SH", "000905.SH", "000852.SH", "000001.SH"]], "ext_index_daily")
        many("fut_daily", [{"ts_code": c, "start_date": S2, "end_date": E2} for c in ["IF.CFX", "IC.CFX", "IM.CFX"]], "ext_fut_daily")
        many("index_global", [{"ts_code": c, "start_date": S2, "end_date": E2} for c in ["SPX", "HSI", "XIN9"]], "ext_index_global", workers=1)
        wk = pd.date_range(S2, E2, freq="7D")
        many("limit_list_d", [{"start_date": a.strftime("%Y%m%d"), "end_date": (a + pd.Timedelta(days=6)).strftime("%Y%m%d")} for a in wk], "ext_limit_list_d", workers=2)
        days = [d.strftime("%Y%m%d") for d in pd.date_range(S2, E2) if d.dayofweek < 7]
        many("forecast", [{"ann_date": d} for d in days], "ext_forecast", workers=2)
        many("dividend", [{"ex_date": d} for d in days if pd.Timestamp(d).dayofweek < 5], "ext_dividend", workers=2)
        rng = [(m.start_time.strftime("%Y%m%d"), m.end_time.strftime("%Y%m%d")) for m in pd.period_range("2026-06", "2026-11", freq="M")]
        many("repurchase", [{"start_date": a, "end_date": b} for a, b in rng], "ext_repurchase")
        half = []
        for a, b in rng: half += [(a, a[:6] + "10"), (a[:6] + "11", a[:6] + "20"), (a[:6] + "21", b)]
        many("stk_holdertrade", [{"start_date": a, "end_date": b} for a, b in half], "ext_stk_holdertrade")
        many("disclosure_date", [{"end_date": q, "fields": "ts_code,ann_date,end_date,pre_date,actual_date,modify_date"} for q in ["20260630", "20260930"]], "ext_disclosure_date")
    if "disclosure" in groups:
        qs = [q.end_time.strftime("%Y%m%d") for q in pd.period_range("2021Q3", "2026Q1", freq="Q")]
        many("disclosure_date", [{"end_date": q, "fields": "ts_code,ann_date,end_date,pre_date,actual_date,modify_date"} for q in qs], "disclosure_date", workers=4)
        many("express", [{"period": q} for q in qs], "express", workers=4)
    if "perstock" in groups:
        codes = research_codes()
        many("forecast", [{"ts_code": c} for c in codes], "forecast")
        many("dividend", [{"ts_code": c} for c in codes], "dividend")
    if "toplist" in groups:
        days = trade_days()
        many("top_list", [{"trade_date": d} for d in days], "top_list")
    if "topinst" in groups:
        many("top_inst", [{"trade_date": d} for d in trade_days()], "top_inst")

if __name__ == "__main__":
    main()
