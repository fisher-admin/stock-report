#!/usr/bin/env python3
"""隔夜前瞻影子 overnight_shadow_v1: 只记账, 不下单, 不触碰 v4.3 / forward_shadow_v1 / tier / cohort。

两本账(模型与规则全部冻结, 见 outputs/overnight_v0/frozen_v2 与 frozen_G1_v1 的 manifest):
  long_gap   纯隔夜做多: frozen_v2/model_gap.txt 预测 > 19.2bp 的前 5 只, 14:55 线均价买, 次日开盘卖(另记 09:35 / 09:40 / 10:00 / 10:30)
  avoid      隔夜避让: frozen_G1_v1/model_G1.txt 在当日合格股里预测最低 5% 与 2%, 14:55 线均价卖, 次晨买回

子命令:
  prep    备好 T 日 14:46 所需的全部历史输入(5 分钟线仓、日线、面板、事件表); 可在 T 日开盘后任意时间运行, 幂等
  signal  T 日 14:44 启动做准备, 14:45:15 起拉当日截至 14:45 的 5 分钟线(边拉边算), 复用 research/overnight_v0 的特征函数打分,
          14:50 前写 signals/T.json; 超时或输入缺失只记 gaps.jsonl("未出信号"), 不补发
  settle  次日 10:31 之后: 对尚未结算的信号记录各时点价格并结算两本账(每笔 19.2bp), 追加 settled.jsonl
  bench   只测 14:46 那次拉取的耗时, 不写账本
  --test  写到 overnight_v1/test/, 跳过时间闸门, 可用 --date 指定历史信号日(数据取自 5 分钟线仓)

数据源: 本机 tdx-api(TDX_API, 默认回环地址)。kline-history: 价格 x1000, Volume 为手, Amount 为元 x1000, Time 为右端标签, 首根 09:35 含开盘竞价。
账本: data/forward_shadow/overnight_v1/ (append-only); 缓存: outputs/overnight_v0/live/ (不入库)。
LightGBM 模型用本文件内的文本解析器预测(与 lightgbm.Booster.predict 逐行核对过), 运行环境无需安装 lightgbm。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
WS = Path(os.environ.get("STOCK_WS") or ROOT.parent)
RES = ROOT / "research/overnight_v0"
FZ = ROOT / "outputs/overnight_v0"
LIVE = Path(os.environ.get("OVN_LIVE_DIR") or FZ / "live")
LEDGER = ROOT / "data/forward_shadow/overnight_v1"
SW = WS / "stock_data/03-working"
TDX = os.environ.get("TDX_API", "http://127.0.0.1:18080")
TZ = timezone(timedelta(hours=8))
SPEC_ID = "overnight_shadow_v1"
COST = 0.00192
K = 5
HIST = 25                       # 信号日之前保留的 5 分钟线交易日数: 20 日滚动中位数按"行"滚动, 有停牌缺日的股票需要多留几天(改成 21 会让 p_gap 最多差 28bp, 已实测)
ADV_MIN = 1e5                   # 千元 = 1 亿元
START_OPEN, START_CLOSE = (14, 40, 0), (14, 48, 30)     # 允许启动的时间窗(启动后先做不依赖当日 K 线的准备)
FETCH_OPEN, GATE_CLOSE = (14, 45, 15), (14, 50, 0)      # 14:45:15 开始拉当日 K 线(14:45 根覆盖 14:40-14:45, 分笔快照 3 秒一次, 15 秒后已收定); 14:50:00 前必须落盘
MODELS = {"gap": (FZ / "frozen_v2/model_gap.txt", FZ / "frozen_v2/manifest.json"),
          "G1": (FZ / "frozen_G1_v1/model_G1.txt", FZ / "frozen_G1_v1/manifest.json")}
EXITS = ["open", "x0935", "x0940", "x1000", "x1030"]
DB_DIRS = [SW / "fundamental_cache/pit_market/v2", SW / "strategy_research/short_track/materialized/daily_basic",
           SW / "strategy_research/short_track/input_cache", SW / "fundamental_cache/pit_market", ROOT / "data/daily_basic_hist"]


def now() -> datetime:
    return datetime.now(TZ)


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def append(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=float) + "\n")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in open(p) if x.strip()] if p.exists() else []


def paths(test: bool) -> dict[str, Path]:
    base = LEDGER / "test" if test else LEDGER
    (base / "signals").mkdir(parents=True, exist_ok=True)
    gi = LEDGER / ".gitignore"
    if not gi.exists():
        gi.write_text("test/\n")
    return {"base": base, "signals": base / "signals", "settled": base / "settled.jsonl", "gaps": base / "gaps.jsonl", "runs": base / "runs.jsonl"}


# ---------------------------------------------------------------- 日历

def sessions() -> list[str]:
    days: set[str] = set()
    p = SW / "health/trading_calendar.json"
    if p.exists():
        days |= set(json.loads(p.read_text()).get("open_dates", []))
    days |= {os.path.basename(f)[:8] for f in glob.glob(str(LIVE / "bars/*.parquet"))}
    f = LIVE / "events/index_daily.parquet"
    if f.exists():
        days |= set(pd.read_parquet(f, columns=["trade_date"]).trade_date.astype(str))
    return sorted(days)


def next_session(d: str, cal: list[str]) -> str | None:
    later = [x for x in cal if x > d]
    return later[0] if later else None


# ---------------------------------------------------------------- tdx

def tdx_get(path: str, timeout: float = 8, tries: int = 2, data: bytes | None = None):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(TDX + path, data=data, headers={"Content-Type": "application/json"} if data else {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(0.3 * (i + 1))
    raise RuntimeError(f"{path}: {err}")


def ts(code6: str) -> str:
    return code6 + (".SH" if code6[0] == "6" else ".SZ")


def universe_codes() -> list[str]:
    """沪深主板 + 创业板(不含科创板/北交所), 与研究口径一致。"""
    out = []
    for c in tdx_get("/api/stock-codes", timeout=30, tries=3)["data"]["list"]:
        ex, num = c[:2], c[2:]
        if (ex == "sh" and num[:2] == "60") or (ex == "sz" and num[:3] in ("000", "001", "002", "003", "300", "301", "302")):
            out.append(ts(num))
    return sorted(set(out))


def _bars_one(a):
    code, limit = a
    t = time.time()
    try:
        L = (tdx_get(f"/api/kline-history?code={code[:6]}&type=minute5&limit={limit}").get("data") or {}).get("List") or []
    except Exception as e:  # noqa: BLE001
        return code, None, time.time() - t, str(e)[:80]
    rows = [(code, x["Time"][:10].replace("-", ""), x["Time"][11:16], x["Open"] / 1000, x["High"] / 1000, x["Low"] / 1000, x["Close"] / 1000,
             int(x["Volume"]) * 100, x["Amount"] / 1000) for x in L]
    return code, rows, time.time() - t, None


BAR_COLS = ["ts_code", "d", "t", "open", "high", "low", "close", "vol", "amount"]


def fetch_bars(codes: list[str], limit: int, workers: int) -> tuple[pd.DataFrame, dict]:
    t = time.time()
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(_bars_one, [(c, limit) for c in codes]))
    rows = [r for _, rr, _, _ in res if rr for r in rr]
    lat = sorted(x[2] for x in res)
    st = {"calls": len(codes), "limit": limit, "workers": workers, "seconds": round(time.time() - t, 1), "errors": sum(1 for x in res if x[3]),
          "empty": sum(1 for x in res if x[1] is not None and not x[1]), "p50_s": round(lat[len(lat) // 2], 2) if lat else None,
          "p95_s": round(lat[int(len(lat) * 0.95)], 2) if lat else None}
    return pd.DataFrame(rows, columns=BAR_COLS), st


def fetch_quotes(codes: list[str], workers: int = 4) -> tuple[pd.DataFrame, dict]:
    """批量快照(每次至多 50 只): 昨收(交易所口径, 含除权)、开盘、最高、最低、现价。"""
    t = time.time()

    def one(chunk):
        try:
            d = tdx_get("/api/batch-quote", data=json.dumps({"codes": [c[:6] for c in chunk]}).encode()).get("data") or []
            return [(ts(x["Code"]), x["K"]["Last"] / 1000, x["K"]["Open"] / 1000, x["K"]["High"] / 1000, x["K"]["Low"] / 1000, x["K"]["Close"] / 1000) for x in d]
        except Exception:  # noqa: BLE001
            return None
    chunks = [codes[i:i + 50] for i in range(0, len(codes), 50)]
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(one, chunks))
    q = pd.DataFrame([r for rr in res if rr for r in rr], columns=["ts_code", "pre_close", "open", "high", "low", "price"]).drop_duplicates("ts_code")
    return q, {"calls": len(chunks), "seconds": round(time.time() - t, 1), "errors": sum(1 for r in res if r is None), "rows": int(len(q))}


# ---------------------------------------------------------------- 5 分钟线仓

def store_days() -> list[str]:
    return sorted(os.path.basename(f)[:8] for f in glob.glob(str(LIVE / "bars/*.parquet")))


def store_write(bars: pd.DataFrame, only_complete_before: str | None = None) -> list[str]:
    """按日写入; 只写已收盘的完整交易日(存在 15:00 根), 已有文件只在新数据股票数更多时覆盖。"""
    (LIVE / "bars").mkdir(parents=True, exist_ok=True)
    done = []
    for d, g in bars.groupby("d"):
        if only_complete_before and d >= only_complete_before:
            continue
        if (g.t == "15:00").sum() < 0.5 * g.ts_code.nunique():
            continue
        g = g[g.groupby("ts_code").t.transform("size") >= 1].drop(columns="d").drop_duplicates(["ts_code", "t"])
        f = LIVE / "bars" / f"{d}.parquet"
        if f.exists() and pd.read_parquet(f, columns=["ts_code"]).ts_code.nunique() >= g.ts_code.nunique():
            continue
        tmp = f.with_suffix(".tmp")
        g.to_parquet(tmp, index=False)
        os.replace(tmp, f)
        done.append(d)
    return done


def store_read(days: list[str], codes: set[str] | None = None) -> pd.DataFrame:
    out = []
    for d in days:
        f = LIVE / "bars" / f"{d}.parquet"
        if f.exists():
            g = pd.read_parquet(f)
            if codes is not None:
                g = g[g.ts_code.isin(codes)]
            g.insert(1, "d", d)
            out.append(g)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=BAR_COLS)


def seed_store(seed_dir: str, days: list[str]) -> dict:
    """从 tdx_pull_minute5.py 的归档(<ts_code>.csv.gz)回填指定交易日, 仅用于初始化与历史 --test。"""
    need = [d for d in days if not (LIVE / "bars" / f"{d}.parquet").exists()]
    if not need:
        return {"seeded": 0}
    lo, hi = min(need), max(need)
    lo_s, hi_s = f"{lo[:4]}-{lo[4:6]}-{lo[6:]}", f"{hi[:4]}-{hi[4:6]}-{hi[6:]} 99"
    out = []
    for f in sorted(glob.glob(os.path.join(seed_dir, "*.csv.gz"))):
        code = os.path.basename(f)[:9]
        try:
            m = pd.read_csv(f, dtype={"trade_time": str})
        except Exception:  # noqa: BLE001
            continue
        m = m[(m.trade_time >= lo_s) & (m.trade_time <= hi_s)]
        if m.empty:
            continue
        out.append(pd.DataFrame({"ts_code": code, "d": m.trade_time.str[:10].str.replace("-", ""), "t": m.trade_time.str[11:16], "open": m.open, "high": m.high,
                                 "low": m.low, "close": m.close, "vol": m.vol_shares, "amount": m.amount_yuan}))
    if not out:
        return {"seeded": 0, "need": need}
    b = pd.concat(out, ignore_index=True)
    return {"seeded": store_write(b[b.d.isin(need)])}


# ---------------------------------------------------------------- prep: 日线 / 面板 / 事件

def build_daily(T: str, bars_days: list[str]) -> tuple[pd.DataFrame, dict]:
    """T 日之前的日线(open, close, pre_close, amount 千元)。优先生产日线缓存, 其次逐股历史日线, 缺的近日用 5 分钟线聚合(昨收取前一日收盘, 不含除权调整)。"""
    cols = ["ts_code", "trade_date", "open", "close", "pre_close", "amount"]
    fs = sorted(f for f in glob.glob(str(SW / "backtest_cache/daily_2*.parquet")) if os.path.basename(f)[6:14] < T)[-300:]
    parts = [pd.read_parquet(f, columns=cols) for f in fs]
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    d["trade_date"] = d.trade_date.astype(str)
    d = d[d.ts_code.str[:2].isin(["60", "00", "30"])]
    meta = {"cache_days": len(fs), "cache_last": os.path.basename(fs[-1])[6:14] if fs else None, "per_stock_rows": 0, "bars_days": []}
    first = d.trade_date.min() if len(d) else T
    dd = ROOT / "data/price_hist/daily"
    if len(fs) < 262 and dd.exists():
        old = []
        for f in glob.glob(str(dd / "*.parquet")):
            c = os.path.basename(f)[:9]
            if c[:2] not in ("60", "00", "30"):
                continue
            x = pd.read_parquet(f, columns=cols[1:]).astype({"trade_date": str})
            x = x[x.trade_date < first].sort_values("trade_date").drop_duplicates("trade_date").tail(300)
            x.insert(0, "ts_code", c)
            old.append(x)
        if old:
            o = pd.concat(old, ignore_index=True)
            meta["per_stock_rows"] = int(len(o))
            d = pd.concat([o, d], ignore_index=True)
    d = d.drop_duplicates(["ts_code", "trade_date"], keep="last")
    have = set(d.trade_date.unique())
    miss = [x for x in bars_days if x < T and x not in have and x > (max(have) if have else "0")]
    if miss:
        b = store_read(miss).sort_values(["ts_code", "d", "t"])
        g = b.groupby(["ts_code", "d"], sort=False)
        a = pd.DataFrame({"open": g.open.first(), "close": g.close.last(), "amount": g.amount.sum() / 1000}).reset_index().rename(columns={"d": "trade_date"})
        a = pd.concat([d, a], ignore_index=True).sort_values(["ts_code", "trade_date"])
        a["pre_close"] = a.pre_close.fillna(a.groupby("ts_code").close.shift(1))
        d = a
        meta["bars_days"] = miss
    d = d[d.trade_date >= sorted(d.trade_date.unique())[-300:][0]].sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    meta["last"] = d.trade_date.max()
    meta["days"] = int(d.trade_date.nunique())
    return d[cols], meta


def static_meta() -> pd.DataFrame:
    """行业(申万一级)与名称: 取研究面板 2025 年以来最后已知值前推(与时间留出集的做法一致)。"""
    f = LIVE / "static_meta.parquet"
    src = ROOT / "data/research_panel_v2.parquet"
    if f.exists() and (not src.exists() or f.stat().st_mtime >= src.stat().st_mtime):
        return pd.read_parquet(f)
    import pyarrow.dataset as ds
    t = ds.dataset(src).to_table(columns=["ts_code", "trade_date", "name", "sw2021_l1"], filter=ds.field("trade_date") >= "20250101").to_pandas()
    m = t.sort_values("trade_date").groupby("ts_code").last()[["name", "sw2021_l1"]].reset_index()
    f.parent.mkdir(parents=True, exist_ok=True)
    m.to_parquet(f, index=False)
    return m


def _first_existing(dirs, name):
    for d in dirs:
        if (d / name).exists():
            return d / name
    return None


def build_panel(T: str, daily: pd.DataFrame, codes: list[str]) -> tuple[pd.DataFrame, dict]:
    days = sorted(daily.trade_date.unique())[-(HIST + 3):]
    a = daily.sort_values(["ts_code", "trade_date"]).copy()
    a["avg_amount_lookback"] = a.groupby("ts_code").amount.transform(lambda s: s.rolling(60, min_periods=40).mean())
    P = a[a.trade_date.isin(days) & a.ts_code.isin(codes)][["ts_code", "trade_date", "avg_amount_lookback"]]
    today = P[P.trade_date == days[-1]].assign(trade_date=T)        # T 日只知道截至 T-1 的均额
    P = pd.concat([P, pd.DataFrame({"ts_code": [c for c in codes if c not in set(today.ts_code)], "trade_date": T, "avg_amount_lookback": np.nan}), today], ignore_index=True)
    meta = {"db_missing": [], "st_source": {}}
    db = []
    for d in days:
        f = _first_existing(DB_DIRS, f"daily_basic_{d}.parquet")
        if f is None:
            meta["db_missing"].append(d)
            continue
        db.append(pd.read_parquet(f, columns=["ts_code", "trade_date", "float_share", "circ_mv"]).astype({"trade_date": str}))
    P = P.merge(pd.concat(db).drop_duplicates(["ts_code", "trade_date"]), on=["ts_code", "trade_date"], how="left") if db else P.assign(float_share=np.nan, circ_mv=np.nan)
    sm = static_meta().set_index("ts_code")
    st_rows = []
    last_st = None
    ind_fill = None
    for d in days + [T]:
        f = _first_existing([SW / "fundamental_cache/pit_market/v2", SW / "fundamental_cache/pit_market"], f"universe_{d}.parquet")
        g = ROOT / "data/price_hist/stock_st" / f"stock_st_{d}.parquet"
        if f is not None:
            u = pd.read_parquet(f, columns=["ts_code", "name", "is_st", "sw2021_l1_name"])
            ind_fill = u.drop_duplicates("ts_code").set_index("ts_code").sw2021_l1_name
            last_st = set(u[u.is_st.astype(bool) | u.name.astype(str).str.contains("ST")].ts_code)
            meta["st_source"][d] = "pit_universe"
        elif g.exists():
            last_st = set(pd.read_parquet(g, columns=["ts_code"]).ts_code)
            meta["st_source"][d] = "stock_st"
        else:
            meta["st_source"][d] = "carried" if last_st is not None else "name_only"
        st_rows.append((d, last_st))
    stm = dict(st_rows)
    base = sm.name.reindex(P.ts_code).fillna("").astype(str).str.replace("ST", "", regex=False).values
    is_st = np.array([(stm.get(d) is not None and c in stm[d]) for c, d in zip(P.ts_code, P.trade_date)]) if any(v is not None for v in stm.values()) \
        else sm.name.reindex(P.ts_code).fillna("").astype(str).str.contains("ST").values
    P["name"] = np.where(is_st, "ST" + pd.Series(base).astype(str).values, base)
    P["sw2021_l1"] = sm.sw2021_l1.reindex(P.ts_code).values
    if ind_fill is not None:                                        # 研究面板没有覆盖到的股票, 用最近的 PIT 快照行业补(同一套申万一级名称)
        known = set(sm.sw2021_l1.dropna().unique())
        fill = ind_fill[ind_fill.isin(known)].reindex(P.ts_code).values
        P["sw2021_l1"] = P.sw2021_l1.where(P.sw2021_l1.notna(), pd.Series(fill, index=P.index))
    P["market_board"] = np.where(P.ts_code.str[:2] == "30", "创业板", "主板")
    meta["st_today"] = int(is_st[P.trade_date.values == T].sum())
    meta["industry_missing_today"] = int(P[P.trade_date == T].sw2021_l1.isna().sum())
    return P, meta


def refresh_events(T: str, cal: list[str]) -> dict:
    """事件与指数表的增量刷新(尽力而为; 失败只记录, 不阻断)。每天十余次调用。"""
    ev = LIVE / "events"
    ev.mkdir(parents=True, exist_ok=True)
    out = {"ok": [], "failed": []}
    try:
        os.environ.setdefault("STOCK_SYSTEM_WORKSPACE", str(WS))
        os.environ.setdefault("OPENCLAW_WORKSPACE_DIR", str(WS))
        sys.path.insert(0, str(WS / "skills/stock-analyzer"))
        import credentials
        import data_provider
        try:
            pk = credentials.get_promax_api_key()
        except Exception:  # noqa: BLE001
            pk = None
        cli = data_provider.create_data_client(provider="auto", rds_key=credentials.get_rds_api_key(), promax_key=pk)
    except Exception as e:  # noqa: BLE001
        out["failed"].append(f"provider: {type(e).__name__}")
        return out
    prev = [d for d in cal if d < T][-6:]
    nxt = [d for d in cal if d >= T][:3]
    a, b = prev[0], T
    qs = [q.end_time.strftime("%Y%m%d") for q in pd.period_range(end=pd.Timestamp(T), periods=3, freq="Q")]
    plan = {"index_daily": [{"ts_code": c, "start_date": a, "end_date": b} for c in ("000300.SH", "000905.SH", "000852.SH")],
            "fut_daily": [{"ts_code": c, "start_date": a, "end_date": b} for c in ("IF.CFX", "IC.CFX")],
            "index_global": [{"ts_code": c, "start_date": a, "end_date": b} for c in ("SPX", "HSI")],
            "limit_list_d": [{"start_date": a, "end_date": prev[-1]}],
            "forecast": [{"ann_date": d} for d in prev[-3:] + [T]],
            "dividend": [{"ex_date": d} for d in nxt],
            "repurchase": [{"start_date": a, "end_date": b}],
            "stk_holdertrade": [{"start_date": a, "end_date": b}],
            "disclosure_date": [{"end_date": q, "fields": "ts_code,ann_date,end_date,pre_date,actual_date,modify_date"} for q in qs]}
    for api, ps in plan.items():
        got = []
        for p in ps:
            try:
                r = cli._direct(api, p)
                if r is not None and len(r):
                    got.append(r)
            except Exception as e:  # noqa: BLE001
                out["failed"].append(f"{api}: {type(e).__name__}")
            time.sleep(0.5)
        if not got:
            continue
        new = pd.concat(got, ignore_index=True)
        if api in ("forecast", "dividend"):
            new = new[new.ts_code.astype(str).str[:2].isin(["60", "00"])]      # 与训练时的覆盖一致(仅主板)
        f = ev / f"{api}.parquet"
        old = pd.read_parquet(f) if f.exists() else None
        if api == "disclosure_date" and old is not None:
            old = old[~old.end_date.astype(str).isin(set(new.end_date.astype(str)))]   # 计划披露日会被修正, 整期替换
        df = new if old is None else pd.concat([old, new[[c for c in old.columns if c in new.columns]]], ignore_index=True)
        for c in df.columns:
            if df[c].dtype == object or str(df[c].dtype) in ("string", "str"):
                df[c] = df[c].astype(object).where(df[c].notna(), None)
        df = df.astype({c: "string" for c in df.columns if df[c].dtype == object}).drop_duplicates()
        tmp = f.with_suffix(".tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, f)
        out["ok"].append(api)
    return out


def events_asof() -> dict:
    o = {}
    for n, c, flt in [("index_daily", "trade_date", None), ("fut_daily", "trade_date", None), ("limit_list_d", "trade_date", None), ("forecast", "ann_date", None),
                      ("repurchase", "ann_date", None), ("stk_holdertrade", "ann_date", None), ("SPX", "trade_date", "SPX"), ("HSI", "trade_date", "HSI")]:
        f = LIVE / "events" / f"{'index_global' if flt else n}.parquet"
        if not f.exists():
            o[n] = None
            continue
        d = pd.read_parquet(f)
        if flt:
            d = d[d.ts_code == flt]
        o[n] = str(d[c].dropna().astype(str).max()) if len(d) else None
    return o


def cmd_prep(a) -> dict:
    cal = sessions()
    t0 = time.time()
    n = now()
    T = a.date or (n.strftime("%Y%m%d") if n.strftime("%Y%m%d") in cal and n.hour < 15 else next_session(n.strftime("%Y%m%d"), cal))
    prev = [d for d in cal if d < T][-HIST:]
    info = {"target": T, "started_at": n.isoformat(), "warnings": []}
    if a.seed_dir:
        info["seed"] = seed_store(a.seed_dir, prev + ([T] if a.test else []) + ([next_session(T, cal)] if a.test and next_session(T, cal) else []))
    miss = [d for d in prev if d not in store_days()]
    try:
        codes = universe_codes()
        (LIVE / "universe.json").write_text(json.dumps(codes))
    except Exception as e:  # noqa: BLE001
        if (LIVE / "universe.json").exists():
            codes = json.loads((LIVE / "universe.json").read_text())
        else:
            codes = sorted(pd.read_parquet(LIVE / "bars" / f"{store_days()[-1]}.parquet", columns=["ts_code"]).ts_code.unique())
            (LIVE / "universe.json").write_text(json.dumps(codes))
        info["warnings"].append(f"universe from cache: {str(e)[:60]}")
    if miss and not a.no_fetch:
        k = len([d for d in cal if min(miss) <= d <= n.strftime("%Y%m%d")])
        bars, st = fetch_bars(codes, min(800, 48 * (k + 1)), a.workers)
        info["bars_fetch"] = st
        info["bars_written"] = store_write(bars, only_complete_before=None if n.hour >= 15 else n.strftime("%Y%m%d"))
        miss = [d for d in prev if d not in store_days()]
    if miss:
        info["warnings"].append(f"bars missing: {miss}")
    daily, dm = build_daily(T, store_days())
    info["daily"] = dm
    if dm["last"] != prev[-1]:
        info["warnings"].append(f"daily last {dm['last']} != {prev[-1]}")
    panel, pm = build_panel(T, daily, codes)
    info["panel"] = {k: v for k, v in pm.items() if k != "st_source"}
    info["panel"]["st_source_today"] = pm["st_source"].get(T)
    tmp = LIVE / "daily_hist.tmp"
    daily.to_parquet(tmp, index=False)
    os.replace(tmp, LIVE / "daily_hist.parquet")
    tmp = LIVE / "panel_live.tmp"
    panel.to_parquet(tmp, index=False)
    os.replace(tmp, LIVE / "panel_live.parquet")
    if not a.no_events:
        info["events_refresh"] = refresh_events(T, cal)
    info["events_asof"] = events_asof()
    info["bars_days"] = [d for d in prev if d in store_days()]
    info["ready"] = not miss and dm["last"] == prev[-1]
    info["seconds"] = round(time.time() - t0, 1)
    info["built_at"] = now().isoformat()
    (LIVE / "prep_state.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=float))
    return {"status": "prepared" if info["ready"] else "prep_incomplete", **{k: info[k] for k in ("target", "ready", "warnings", "seconds")}}


# ---------------------------------------------------------------- LightGBM 文本模型预测(纯 numpy)

class TextGBM:
    def __init__(self, path: Path):
        self.trees = []
        cur = None
        for line in Path(path).read_text().splitlines():
            if line.startswith("Tree="):
                cur = {}
                self.trees.append(cur)
            elif line.startswith("end of trees"):
                break
            elif cur is not None and "=" in line:
                k, v = line.split("=", 1)
                if k in ("split_feature", "left_child", "right_child", "decision_type"):
                    cur[k] = np.array(v.split(), dtype=np.int64)
                elif k in ("threshold", "leaf_value"):
                    cur[k] = np.array(v.split(), dtype=np.float64)
                elif k in ("num_leaves", "num_cat"):
                    cur[k] = int(v)
        if any(t.get("num_cat", 0) for t in self.trees):
            raise ValueError("categorical splits not supported")

    def predict(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        out = np.zeros(len(X))
        rows = np.arange(len(X))
        for t in self.trees:
            if t["num_leaves"] <= 1:
                out += t["leaf_value"][0]
                continue
            node = np.zeros(len(X), dtype=np.int64)
            act = np.ones(len(X), dtype=bool)
            while act.any():
                i = node[act]
                v = X[rows[act], t["split_feature"][i]]
                dt = t["decision_type"][i]
                dl = (dt & 2) > 0
                mt = (dt >> 2) & 3
                nan = np.isnan(v)
                v0 = np.where(nan & (mt != 2), 0.0, v)
                miss = ((mt == 2) & nan) | ((mt == 1) & (np.abs(v0) <= 1e-35))
                left = np.where(miss, dl, v0 <= t["threshold"][i])
                node[act] = np.where(left, t["left_child"][i], t["right_child"][i])
                act = node >= 0
            out += t["leaf_value"][~node]
        return out


# ---------------------------------------------------------------- 研究代码复用: 内存表替身

class _Tab:
    def __init__(self, df):
        self.df = df

    def to_pandas(self):
        return self.df


class PqShim:
    """替换研究模块里的 pq: 逐股分钟线/日线与面板直接从内存取, 不落盘。"""
    store: dict[str, pd.DataFrame] = {}

    @classmethod
    def read_table(cls, path, columns=None, **kw):
        df = cls.store[os.path.basename(str(path))]
        return _Tab(df[list(columns)] if columns else df)


def load_models() -> dict:
    out = {}
    for k, (mp_, mf) in MODELS.items():
        man = json.loads(mf.read_text())
        s = sha(mp_)
        want = man["models"]["gap"]["sha256"] if k == "gap" else man["model_sha256"]
        if s != want:
            raise RuntimeError(f"model {k} sha256 mismatch with manifest")
        out[k] = {"model": TextGBM(mp_), "features": man["features"], "sha256": s, "version": man["version"]}
    return out


def code_fingerprint() -> dict:
    fs = [Path(__file__)] + [RES / n for n in ("overnight_factor_model.py", "day_timing.py", "factors_cde.py", "event_factors.py", "stage3_model.py")]
    h = hashlib.sha256(b"".join(f.read_bytes() for f in fs)).hexdigest()[:16]
    try:
        c = subprocess.run(["git", "-C", str(WS), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        c = ""
    return {"code_sha16": h, "code_commit": c}


MC = ["trade_time", "close", "open", "high", "low", "vol", "amount"]
_W: dict = {}


def _stream_worker(args):
    """子进程: 一边拉当日 5 分钟线(线程), 一边对到手的股票调用研究代码的三个逐股特征函数。"""
    codes, T, live, threads = args
    import overnight_factor_model as ofm
    import factors_cde as fcd
    hist, today = _W["hist"], _W.get("today") or {}
    pre = f"{T[:4]}-{T[4:6]}-{T[6:]} "

    def get(c):
        if not live:
            return c, today.get(c), None
        _, rows, _, err = _bars_one((c, 48))
        if not rows:
            return c, None, err or "no bars"
        b = pd.DataFrame(rows, columns=BAR_COLS)
        return c, b[(b.d == T) & (b.t <= "14:45")], None
    out = []
    with ThreadPoolExecutor(threads) as ex:
        for c, tb, err in ex.map(get, codes):
            if tb is None or tb.empty:
                out.append((c, None, None, None, None, err or "no bars today"))
                continue
            m = tb.assign(trade_time=pre + tb.t + ":00")[MC]
            h = hist.get(c)
            key = f"minute_5min_{c.replace('.', '_')}.parquet"
            PqShim.store[key] = pd.concat([h, m], ignore_index=True) if h is not None else m.reset_index(drop=True)
            try:
                r, err = (ofm.load_stock(c), fcd.d_feats_one(c), None), None      # e_feats_one 只用日线, 已在 warm 阶段算好
            except Exception as e:  # noqa: BLE001
                r, err = (None, None, None), f"{type(e).__name__}: {str(e)[:60]}"
            PqShim.store.pop(key, None)
            out.append((c, *r, tb, err))
    return out


class Scorer:
    """T 日 14:45 截面打分, 分三步: warm(14:45 之前可做的全部准备) -> stream(拉取 + 逐股特征) -> finish(截面合成 + 预测)。"""

    def __init__(self, T: str, N: str, score_codes: list[str], dayfeat: set[str], work: Path, procs: int, threads: int):
        self.T, self.N, self.score_codes, self.dayfeat, self.work, self.procs, self.threads = T, N, score_codes, dayfeat, work, procs, threads
        self.tm: dict = {}
        self.degraded: list[str] = []
        self.cal = sessions()
        self.prev = [d for d in self.cal if d < T][-HIST:]

    def warm(self, q0: pd.DataFrame) -> None:
        """q0: 全股池的昨收与开盘价(开盘后即固定)。"""
        t0 = time.time()
        T = self.T
        daily = pd.read_parquet(LIVE / "daily_hist.parquet")
        self.panel = pd.read_parquet(LIVE / "panel_live.parquet")
        daily = daily[daily.trade_date < T]
        q0 = q0.drop_duplicates("ts_code").set_index("ts_code")
        self.q0 = q0[(q0.pre_close > 0) & (q0.open > 0)]
        self.codes = sorted(set(self.score_codes) & set(self.q0.index) & set(daily.ts_code))
        hist = store_read(self.prev, set(self.codes)).sort_values(["ts_code", "d", "t"])
        hist["trade_time"] = hist.d.str[:4] + "-" + hist.d.str[4:6] + "-" + hist.d.str[6:] + " " + hist.t + ":00"
        _W["hist"] = {c: g[MC] for c, g in hist.groupby("ts_code", sort=False)}
        trow = pd.DataFrame({"ts_code": self.codes, "trade_date": T, "open": self.q0.open.reindex(self.codes).values, "close": np.nan,
                             "pre_close": self.q0.pre_close.reindex(self.codes).values})
        dl = pd.concat([daily[daily.ts_code.isin(self.codes)][["ts_code", "trade_date", "open", "close", "pre_close"]], trow], ignore_index=True)
        store = {"panel_live.parquet": self.panel}
        for c, g in dl.groupby("ts_code", sort=False):
            store[f"{c}.parquet"] = g.drop(columns="ts_code").reset_index(drop=True)
        work = self.work
        self.ev = work / "events"
        self.ev.mkdir(parents=True, exist_ok=True)
        os.environ.update({"STOCK_WS": str(WS), "OVN_OUT": str(work), "OVN_EVENTS": str(self.ev), "OVN_MIN_DIR": str(work / "minute"),
                           "OVN_DAILY_DIR": str(work / "daily"), "OVN_PANEL": str(work / "panel_live.parquet")})
        sys.path.insert(0, str(RES))
        import overnight_factor_model as ofm
        import day_timing as dtm
        import factors_cde as fcd
        import event_factors as evf
        import stage3_model as s3
        self.mods = (ofm, dtm, fcd, evf, s3)
        PqShim.store = store
        ofm.pq = dtm.pq = fcd.pq = PqShim
        self._events_static()
        self.models = load_models()
        self.EF0 = {}
        for c in self.codes:                                          # E 族只用日线(含 T 日开盘价), 14:45 之前就能算完
            try:
                self.EF0[c] = fcd.e_feats_one(c)
            except Exception:  # noqa: BLE001
                pass
        self.tm["warm"] = round(time.time() - t0, 1)

    def _events_static(self) -> None:
        """事件工作目录里不依赖当日行情的部分: T 日及之后的日历占位、T 日基差占位、计划披露日视同实际。"""
        T, ev, src = self.T, self.ev, LIVE / "events"
        for f in src.glob("*.parquet"):
            if f.name not in ("index_daily.parquet", "fut_daily.parquet", "limit_list_d.parquet", "disclosure_date.parquet"):
                if (ev / f.name).exists() or (ev / f.name).is_symlink():
                    (ev / f.name).unlink()
                os.symlink(f, ev / f.name)
        fut_days = [d for d in self.cal if d >= T][:15]
        for name in ("index_daily", "fut_daily"):
            if not (src / f"{name}.parquet").exists():
                self.degraded.append(f"{name} missing")
                x = pd.DataFrame({"ts_code": pd.Series(dtype=str), "trade_date": pd.Series(dtype=str), "close": pd.Series(dtype=float)})
            else:
                x = pd.read_parquet(src / f"{name}.parquet")[["ts_code", "trade_date", "close"]].astype({"trade_date": str})
            x = x[x.trade_date < T]
            cs = sorted(x.ts_code.unique()) or ["000300.SH"]
            ph = pd.DataFrame([(c, T, 1.0) for c in cs] + ([(cs[0], d, np.nan) for d in fut_days[1:]] if name == "index_daily" else []), columns=["ts_code", "trade_date", "close"])
            pd.concat([x, ph], ignore_index=True).to_parquet(ev / f"{name}.parquet", index=False)
        if (src / "disclosure_date.parquet").exists():
            dd = pd.read_parquet(src / "disclosure_date.parquet")
            for c in dd.columns:
                dd[c] = dd[c].astype(object).where(dd[c].notna(), None)
            plan = dd.actual_date.isna() & dd.pre_date.notna() & (dd.pre_date.astype(str) >= T)
            dd.loc[plan, "actual_date"] = dd.loc[plan, "pre_date"]
            dd.astype({c: "string" for c in dd.columns}).to_parquet(ev / "disclosure_date.parquet", index=False)
        else:
            self.degraded.append("disclosure_date missing")
        if (src / "limit_list_d.parquet").exists():
            ll = pd.read_parquet(src / "limit_list_d.parquet")[["ts_code", "trade_date", "first_time", "limit_times", "limit"]].astype({"trade_date": str, "first_time": str})
            self.ll = ll[ll.trade_date < T]
        else:
            self.ll = pd.DataFrame(columns=["ts_code", "trade_date", "first_time", "limit_times", "limit"])
        if not len(self.ll) or self.ll.trade_date.max() < self.prev[-1]:
            self.degraded.append(f"limit_list_d as of {self.ll.trade_date.max() if len(self.ll) else None} (T-1 连板类特征缺失)")

    def stream(self, live: bool, today: dict | None = None) -> None:
        t0 = time.time()
        _W["today"] = today
        parts = [(self.codes[i::self.procs], self.T, live, max(1, self.threads // self.procs)) for i in range(self.procs)]
        if self.procs > 1:
            with mp.get_context("fork").Pool(self.procs) as pool:
                res = [r for part in pool.map(_stream_worker, parts) for r in part]
        else:
            res = _stream_worker(parts[0])
        bad = [r[0] for r in res if r[4] is None]
        self.first_pass_errors = len(bad)
        if live and bad:                                            # 第一遍没拿到当日线的股票, 低并发再拉一次
            again = {r[0]: r for r in _stream_worker((bad, self.T, True, 4))}
            res = [again.get(r[0], r) if r[4] is None else r for r in res]
        self.LS = {c: a for c, a, d, e, tb, err in res if a is not None}
        self.DF = {c: d for c, a, d, e, tb, err in res if d is not None}
        self.EF = {c: self.EF0[c] for c in self.LS if c in self.EF0}
        self.today = pd.concat([tb for _, _, _, _, tb, _ in res if tb is not None], ignore_index=True) if any(r[4] is not None for r in res) else pd.DataFrame(columns=BAR_COLS)
        self.errors = {c: err for c, *_, err in res if err}
        self.tm["stream"] = round(time.time() - t0, 1)

    def finish(self, q1: pd.DataFrame) -> dict:
        """q1: 全股池 14:45 之后的快照(最高价/现价), 用于合成当日触板名单; 已拉 K 线的股票改用 K 线最高价。"""
        t0 = time.time()
        T, N, work, ev = self.T, self.N, self.work, self.ev
        ofm, dtm, fcd, evf, s3 = self.mods
        pn = self.panel[self.panel.trade_date == T].set_index("ts_code")
        q = self.q0[["pre_close", "open"]].join(q1.drop_duplicates("ts_code").set_index("ts_code")[["high", "price"]], how="left")
        lim = pd.Series(np.where(pn.market_board == "创业板", 0.20, np.where(pn.name.astype(str).str.contains("ST"), 0.05, 0.10)), index=pn.index).reindex(q.index)
        up = (q.pre_close * (1 + lim) + 1e-9).round(2)
        g = self.today.sort_values("t").groupby("ts_code")
        hi, last = q.high.copy(), q.price.copy()
        bh, bl = g.high.max(), g.close.last()
        hi.loc[bh.index.intersection(hi.index)] = bh
        last.loc[bl.index.intersection(last.index)] = bl
        touch = (hi >= up - 1e-6).fillna(False)
        syn = pd.DataFrame({"ts_code": q.index[touch], "trade_date": T, "first_time": "144500", "limit_times": 1, "limit": np.where(last[touch] >= up[touch] - 1e-6, "U", "Z")})
        ll = pd.concat([self.ll, syn], ignore_index=True)
        ll["first_time"] = pd.to_numeric(ll.first_time.astype(str).str.replace(":", "", regex=False), errors="coerce")
        ll.to_parquet(ev / "limit_list_d.parquet", index=False)
        codes = sorted(self.LS)
        w = dtm.build_wide(live={"date": T, "next": N, "codes": codes, "map": lambda f, xs: (self.LS[c] for c in xs)})
        w.to_parquet(work / "frame_wide.parquet")
        D = dtm.build_days(w[w.ts_code.isin(self.dayfeat)])
        D.loc[T, "month_end"] = float(N[:6] != T[:6])      # 末行的月末标记在研究代码里恒为 1, 这里按真实下一交易日改正
        D.to_parquet(work / "day_frame.parquet")
        cache = {"d_feats_one": self.DF, "e_feats_one": self.EF}
        fcd.main(mapper=lambda f, xs: [cache[f.__name__][c] for c in xs if c in cache[f.__name__]])
        argv = sys.argv
        sys.argv = [argv[0], "--flags-only"]
        try:
            evf.main()
        except Exception as e:  # noqa: BLE001
            self.degraded.append(f"event flags failed: {type(e).__name__}: {str(e)[:80]}")
            if (work / "event_flags.parquet").exists():
                (work / "event_flags.parquet").unlink()
        finally:
            sys.argv = argv
        full, _ = s3.build()
        s = full[full.trade_date == T].copy()
        for k, mdl in self.models.items():
            s["p_" + k] = mdl["model"].predict(s[mdl["features"]].values.astype(np.float32)) if len(s) else []
        s.to_parquet(work / "scored_T.parquet")                     # 当日全部特征与预测值, 供口径核对
        self.tm["finish"] = round(time.time() - t0, 1)
        X = self.models["G1"]["features"]
        wT = w[w.trade_date == T]
        day = D.loc[T]
        return {"s": s, "timings": self.tm, "degraded": self.degraded, "models": {k: {"sha256": v["sha256"], "version": v["version"]} for k, v in self.models.items()},
                "feature_missing": {c: round(float(s[c].isna().mean()), 4) for c in X if len(s) and s[c].isna().any()},
                "day_feats": {c: (None if pd.isna(day.get(c)) else round(float(day.get(c)), 6)) for c in s3.DAY_FEATS if c != "c_mkt_touch_1445"},
                "n_frame": int(len(wT)), "n_dayfeat": int(wT.ts_code.isin(self.dayfeat).sum()), "n_touch_synth": int(len(syn)), "n_scored_codes": len(codes),
                "n_stream_errors": len(self.errors), "stream_errors": {"first_pass": getattr(self, "first_pass_errors", 0),
                                                                       "final": pd.Series(list(self.errors.values()), dtype=object).str[:40].value_counts().head(5).to_dict()},
                "names": pn.name.to_dict()}


# ---------------------------------------------------------------- signal

def dayfeat_codes() -> set[str]:
    f = LEDGER / "dayfeat_codes.txt"
    return set(f.read_text().split())


def gap(P: dict, T: str, reason: str, extra: dict | None = None) -> dict:
    rec = {"signal_date": T, "kind": "no_signal", "reason": reason, "at": now().isoformat(), **(extra or {})}
    append(P["gaps"], rec)
    return {"status": "no_signal", "reason": reason, "signal_date": T}


def cmd_signal(a) -> dict:
    """实盘时序: 14:40 之后即可启动做准备(读历史、取昨收与开盘价、算 E 族), 等到 14:45:15 才开始拉当日 K 线, 14:50:00 前必须落盘。"""
    P = paths(a.test)
    started = now()
    cal = sessions()
    T = a.date or started.strftime("%Y%m%d")
    out = P["signals"] / f"{T}.json"
    if out.exists():
        return {"status": "already_recorded", "signal_date": T}
    N = next_session(T, cal)
    live_fetch = not a.test or (a.date is None) or a.live
    if not a.test:
        if T not in cal:
            return {"status": "not_a_session", "signal_date": T}
        hms = (started.hour, started.minute, started.second)
        if T != started.strftime("%Y%m%d") or hms < START_OPEN or hms >= START_CLOSE:
            return gap(P, T, f"started {started.strftime('%H:%M:%S')}, outside 14:40:00-14:48:30")
    st = json.loads((LIVE / "prep_state.json").read_text()) if (LIVE / "prep_state.json").exists() else {}
    if st.get("target") != T or not st.get("ready"):
        return gap(P, T, f"prep not ready for {T} (prep target {st.get('target')}, ready {st.get('ready')})")
    panel = pd.read_parquet(LIVE / "panel_live.parquet", columns=["ts_code", "trade_date", "avg_amount_lookback"])
    pt = panel[panel.trade_date == T]
    universe = sorted(pt.ts_code)
    dayfeat = dayfeat_codes()
    score_codes = sorted(set(pt[pt.avg_amount_lookback >= ADV_MIN].ts_code) | (dayfeat & set(universe))) if a.universe == "adv" else universe
    work = LIVE / "work" / (("test_" if a.test else "") + T)
    work.mkdir(parents=True, exist_ok=True)
    sc = Scorer(T, N, score_codes, dayfeat, work, a.procs, a.workers)
    fetch = {}
    if live_fetch:
        q0, fetch["quotes_open_preclose"] = fetch_quotes(universe)
        if len(q0) < 0.9 * len(universe):
            return gap(P, T, f"quote snapshot incomplete ({len(q0)}/{len(universe)})")
        sc.warm(q0)
        if not a.test:
            wait = (datetime(started.year, started.month, started.day, *FETCH_OPEN, tzinfo=TZ) - now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
        fetch["bars_started_at"] = now().isoformat()
        with ThreadPoolExecutor(1) as ex:
            fut = ex.submit(fetch_quotes, universe)
            sc.stream(True)
            q1, fetch["quotes_high_price"] = fut.result()
        fetch["bars"] = {"calls": len(sc.codes), "seconds": sc.tm["stream"], "errors": len(sc.errors), "workers": a.workers}
    else:                                                           # 历史回放: 全部取自线仓, 快照由 14:45 前的 K 线与日线昨收还原
        bars = store_read([T])
        if bars.empty:
            return gap(P, T, "no bars in store for replay")
        b = bars[bars.t <= "14:45"].sort_values("t")
        g = b.groupby("ts_code")
        quote = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "price": g.close.last()})
        dly = pd.read_parquet(LIVE / "daily_hist.parquet")
        pc = None
        f = SW / f"backtest_cache/daily_{T}.parquet"
        g2 = ROOT / "data/price_hist/daily"
        if f.exists():
            pc = pd.read_parquet(f, columns=["ts_code", "pre_close"]).set_index("ts_code").pre_close
            fetch["replay_pre_close"] = "daily_cache"
        elif g2.exists():
            rows = {}
            for c in quote.index:
                h = g2 / f"{c}.parquet"
                if h.exists():
                    x = pd.read_parquet(h, columns=["trade_date", "pre_close"])
                    x = x[x.trade_date.astype(str) == T]
                    if len(x):
                        rows[c] = float(x.pre_close.iloc[0])
            pc = pd.Series(rows, dtype=float)
            fetch["replay_pre_close"] = "per_stock_daily"
        last = dly[dly.trade_date < T].sort_values("trade_date").groupby("ts_code").close.last()
        quote["pre_close"] = (pc.reindex(quote.index) if pc is not None else pd.Series(np.nan, index=quote.index)).fillna(last.reindex(quote.index))
        q0 = q1 = quote.reset_index()
        fetch["replay"] = True
        sc.warm(q0)
        sc.stream(False, {c: g_ for c, g_ in b[b.ts_code.isin(sc.codes)].assign(d=T)[BAR_COLS].groupby("ts_code", sort=False)})
    r = sc.finish(q1)
    s = r["s"]
    s["locked_dn"] = s.locked_dn.astype(bool)
    s["pr"] = s.p_G1.rank(pct=True)
    px = np.exp(s.log_px)
    long_c = s[s.p_gap > COST].sort_values("p_gap", ascending=False).head(K)
    names = r["names"]
    rec = {"spec_id": SPEC_ID, "signal_date": T, "next_session": N, "test": bool(a.test), "official": False, "replay": not live_fetch,
           "started_at": started.isoformat(), "deadline": f"{T[:4]}-{T[4:6]}-{T[6:]}T14:50:00+08:00", "cost_bp": COST * 1e4,
           "models": r["models"], **code_fingerprint(), "dayfeat_codes_sha16": hashlib.sha256((LEDGER / "dayfeat_codes.txt").read_bytes()).hexdigest()[:16],
           "universe": {"mode": a.universe, "n_universe": len(universe), "n_score_codes": len(score_codes), "n_scored": r["n_scored_codes"], "n_frame": r["n_frame"],
                        "n_dayfeat_in_frame": r["n_dayfeat"], "n_elig": int(len(s)), "n_touch_limit_synth": r["n_touch_synth"], "n_stream_errors": r["n_stream_errors"], "stream_errors": r["stream_errors"]},
           "fetch": fetch, "timings_s": r["timings"], "inputs_asof": st.get("events_asof"), "prep_warnings": st.get("warnings"),
           "degraded": r["degraded"], "feature_missing_rate_in_elig": r["feature_missing"], "day_feats": r["day_feats"],
           "long_gap": {"model": "gap", "rule": f"pred > {COST * 1e4:.1f}bp, top {K}", "n_pred_gt_cost": int((s.p_gap > COST).sum()),
                        "max_pred_bp": round(float(s.p_gap.max()) * 1e4, 1) if len(s) else None,
                        "picks": [{"ts_code": c, "name": names.get(c), "pred_bp": round(p * 1e4, 1), "px_1445": round(float(x), 3)} for c, p, x in zip(long_c.ts_code, long_c.p_gap, px.reindex(long_c.index))]},
           "exit_watch": {"note": "p_gap 最高的 20 只(不论是否超过成本), 结算时与做多、避让名单一起记录次日早盘 1 分钟路径, 供日后冻结的离场规则回放",
                          "top20_p_gap": list(s.sort_values("p_gap", ascending=False).head(20).ts_code)},
           "avoid": {"model": "G1", "q05": sorted(s[s.pr <= 0.05].ts_code), "q02": sorted(s[s.pr <= 0.02].ts_code),
                     "blocked_limit_down": sorted(s[(s.pr <= 0.05) & s.locked_dn].ts_code),
                     "pred_bp_q05_threshold": round(float(s.p_G1[s.pr <= 0.05].max()) * 1e4, 1) if len(s) else None},
           "elig": {"ts_code": list(s.ts_code), "p_gap_bp": [round(x * 1e4, 2) for x in s.p_gap], "p_G1_bp": [round(x * 1e4, 2) for x in s.p_G1],
                    "px_1445": [round(float(x), 3) for x in px], "locked_dn": [int(x) for x in s.locked_dn]}}
    written = now()
    rec["written_at"] = written.isoformat()
    rec["seconds_total"] = round((written - started).total_seconds(), 1)
    if not a.test and (written.hour, written.minute, written.second) >= GATE_CLOSE:
        return gap(P, T, f"missed 14:50 deadline (finished {written.strftime('%H:%M:%S')})", {"timings_s": rec["timings_s"], "fetch": fetch})
    if not len(s):
        return gap(P, T, "no eligible stocks", {"degraded": r["degraded"]})
    rec["official"] = not a.test
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, default=float))
    os.replace(tmp, out)
    append(P["runs"], {"cmd": "signal", "signal_date": T, "at": written.isoformat(), "seconds": rec["seconds_total"], "n_elig": len(s), "degraded": len(r["degraded"])})
    return {"status": "recorded", "signal_date": T, "n_elig": int(len(s)), "long_picks": [p["ts_code"] for p in rec["long_gap"]["picks"]], "avoid_q05": len(rec["avoid"]["q05"]),
            "avoid_q02": len(rec["avoid"]["q02"]), "seconds_total": rec["seconds_total"], "timings_s": rec["timings_s"], "fetch": fetch, "degraded": r["degraded"]}


# ---------------------------------------------------------------- settle

def _vw(g: pd.DataFrame) -> float:
    a, v = g.amount.sum(), g.vol.sum()
    lo, hi = g.low.min(), g.high.max()
    w = a / v if v > 0 else np.nan
    return float(w) if (w >= lo * 0.999 and w <= hi * 1.001) else float((g.high.mean() + g.low.mean() + g.close.mean()) / 3)


def price_points(bT: pd.DataFrame, bN: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    nT = {c: g.set_index("t") for c, g in bT.groupby("ts_code")}
    nN = {c: g.set_index("t") for c, g in bN.groupby("ts_code")}
    for c, g in nT.items():
        if "14:55" not in g.index or "15:00" not in g.index:
            continue
        r = {"e1455": _vw(g.loc[["14:55"]]), "e1455_low": float(g.low["14:55"]), "px1445": float(g.close["14:45"]) if "14:45" in g.index else np.nan, "close_T": float(g.close["15:00"])}
        h = nN.get(c)
        if h is not None and "09:35" in h.index:
            r["open"] = float(h.open["09:35"])
            r["x0935"] = _vw(h.loc[["09:35"]])
            r["flat0935"] = bool(h.high["09:35"] == h.low["09:35"])
            if "09:40" in h.index:
                r["x0940"] = _vw(h.loc[["09:35", "09:40"]])
            ts4 = [t for t in ("09:45", "09:50", "09:55", "10:00") if t in h.index]
            if len(ts4) == 4:
                r["x1000"] = _vw(h.loc[ts4])
            if "10:30" in h.index:
                r["x1030"] = float(h.close["10:30"])
        rows[c] = r
    return pd.DataFrame.from_dict(rows, orient="index")


def shadow_books(T: str) -> dict[str, set[str]]:
    """T 日在现有前瞻影子账本里的持仓: 取信号日早于 T 的最近一次调仓记录(T+1 开盘建仓)。"""
    out = {"avoid_losers_70": set(), "buffered_top100": set()}
    rows = [r for r in read_jsonl(ROOT / "data/forward_shadow/books.jsonl") if r.get("is_rebalance") and r.get("signal_date", "9") < T]
    if rows:
        for k in out:
            out[k] = set(rows[-1].get(k) or [])
        out["_as_of"] = rows[-1]["signal_date"]
    return out


def save_paths(P: dict, sig: dict, E: pd.DataFrame) -> dict:
    """次日早盘 1 分钟路径(09:31..11:30 每分钟末价与成交量, 手): 做多入选、p_gap 前 20、避让 5% 三组。只存原始数据, 不做任何判断。"""
    T, N = sig["signal_date"], sig["next_session"]
    groups = {"long": [p["ts_code"] for p in sig["long_gap"]["picks"]], "top20_p_gap": (sig.get("exit_watch") or {}).get("top20_p_gap", []), "avoid_q05": sig["avoid"]["q05"]}
    codes = sorted({c for v in groups.values() for c in v})

    def one(c):
        try:
            L = (tdx_get(f"/api/minute?code={c[:6]}&date={N}", timeout=20, tries=3).get("data") or {}).get("List") or []
            return c, [x["Price"] / 1000 for x in L[:120]], [int(x["Number"]) for x in L[:120]]
        except Exception:  # noqa: BLE001
            return c, None, None
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, codes))
    out = {"signal_date": T, "date": N, "first_minute": "09:31", "groups": groups, "stocks": {}}
    for c, p, v in res:
        if p:
            r = E.loc[c] if c in E.index else {}
            out["stocks"][c] = {"buy_1455_vwap": r.get("e1455"), "close_T": r.get("close_T"), "pre_close_N": r.get("pre_close_N"), "open": r.get("open"), "price": p, "vol_hand": v}
    d = P["base"] / "paths"
    d.mkdir(exist_ok=True)
    (d / f"{N}.json").write_text(json.dumps(out, ensure_ascii=False, default=float))
    return {"file": f"paths/{N}.json", "requested": len(codes), "saved": len(out["stocks"])}


def bp(x) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x) * 1e4, 1)


def settle_one(sig: dict, a, P: dict, cal: list[str]) -> dict:
    T, N = sig["signal_date"], sig["next_session"]
    n = now()
    today = n.strftime("%Y%m%d")
    codes = sig["elig"]["ts_code"]
    info = {}
    live = (today == N) and not a.replay
    if live or (N not in store_days() and today >= N and not a.replay):
        uni = json.loads((LIVE / "universe.json").read_text()) if (LIVE / "universe.json").exists() else codes
        k = len([d for d in cal if T <= d <= today])
        bars, info["bars_fetch"] = fetch_bars(sorted(set(uni) | set(codes)), min(800, 48 * (k + 1)), a.workers)
        info["bars_written"] = store_write(bars, only_complete_before=None if n.hour >= 15 else today)
        bT, bN = bars[bars.d == T], bars[bars.d == N]
        if bT.empty:
            bT = store_read([T], set(codes))
    else:
        bT, bN = store_read([T], set(codes)), store_read([N], set(codes))
    bT, bN = bT[bT.ts_code.isin(codes)], bN[bN.ts_code.isin(codes)]
    if bN.empty or not (bN.t == "10:30").any():
        return {"status": "not_ready", "signal_date": T, "reason": f"no 10:30 bar for {N} yet"}
    pp = price_points(bT, bN)
    pc, src = None, "close_T(未做除权调整)"
    if live:
        q, info["quotes"] = fetch_quotes(codes)
        if len(q):
            pc, src = q.set_index("ts_code").pre_close, "quote"
    if pc is None and (SW / f"backtest_cache/daily_{N}.parquet").exists():
        pc, src = pd.read_parquet(SW / f"backtest_cache/daily_{N}.parquet", columns=["ts_code", "pre_close"]).set_index("ts_code").pre_close, "daily_cache"
    pp["pre_close_N"] = (pc.reindex(pp.index) if pc is not None else pd.Series(np.nan, index=pp.index))
    pp["pre_close_N"] = pp.pre_close_N.where(pp.pre_close_N > 0).fillna(pp.close_T)
    E = pd.DataFrame(sig["elig"]).set_index("ts_code").join(pp, how="left")
    for x in EXITS:
        E["y_" + x] = (E.close_T / E.e1455) * (E[x] / E.pre_close_N) - 1 if x in E else np.nan
    E["pr"] = E.p_G1_bp.rank(pct=True)
    have = E.y_open.notna()
    rec = {"spec_id": SPEC_ID, "signal_date": T, "exit_date": N, "test": bool(a.test), "settled_at": n.isoformat(), "cost_bp": COST * 1e4, "pre_close_source": src,
           "signal_written_at": sig.get("written_at"), "signal_official": sig.get("official"), **info,
           "universe": {"n_elig": int(len(E)), "n_priced": int(have.sum()), "gross_bp": {x: bp(E["y_" + x].mean()) for x in EXITS}}}
    # 做多账: 主口径 14:55 线均价买、次日开盘卖; 保护价(14:45 价 x1.003)成交与否单列
    picks = [p["ts_code"] for p in sig["long_gap"]["picks"]]
    trades = []
    for c in picks:
        r = E.loc[c] if c in E.index else None
        if r is None or pd.isna(r.get("y_open")):
            trades.append({"ts_code": c, "status": "unpriced"})
            continue
        cap = r.px_1445 * 1.003
        trades.append({"ts_code": c, "pred_bp": r.p_gap_bp, "buy_1455_vwap": round(r.e1455, 4), "filled_under_protect": bool(r.e1455_low <= cap), "close_T": r.close_T,
                       "pre_close_N": r.pre_close_N, "open": r.get("open"), "x0935": r.get("x0935"), "x0940": r.get("x0940"), "x1000": r.get("x1000"), "x1030": r.get("x1030"),
                       "open_flat_0935": bool(r.get("flat0935")), "gross_bp": {x: bp(r["y_" + x]) for x in EXITS}, "net_bp": {x: bp(r["y_" + x] - COST) for x in EXITS}})
    ok = [t for t in trades if "net_bp" in t]
    rec["long_gap"] = {"main_exit": "open", "n_picks": len(picks), "n_priced": len(ok), "trades": trades,
                       "trade_gross_bp": {x: (round(float(np.mean([t["gross_bp"][x] for t in ok if t["gross_bp"][x] is not None])), 1) if ok else None) for x in EXITS},
                       "book_net_bp": {x: round(float(sum(t["net_bp"][x] or 0 for t in ok)) / K, 2) for x in EXITS}}
    # 避让账: 每次避让的价值 = -y - 成本; 主口径买回 = 09:35+09:40 均价(与回测一致); 14:45 跌停封死的卖不出, 不计
    books = shadow_books(T)
    def avoid_stats(m):
        f = E[m & have & (E.locked_dn == 0)]
        o = {"n": int(len(f)), "n_flagged": int(m.sum()), "blocked_limit_down": int((m & (E.locked_dn == 1)).sum()), "unpriced": int((m & ~have).sum())}
        if len(f):
            o["y_bp"] = {x: bp(f["y_" + x].mean()) for x in EXITS}
            o["stepaside_net_bp"] = {x: bp((-f["y_" + x] - COST).mean()) for x in EXITS}
            o["stepaside_win"] = {x: round(float((-f["y_" + x] - COST > 0).mean()), 3) for x in EXITS}
        return o
    rec["avoid"] = {"main_buyback": "x0940", "books_as_of": books.get("_as_of")}
    for nm, th in (("q05", 0.05), ("q02", 0.02)):
        m = E.index.isin(sig["avoid"][nm])
        o = avoid_stats(m)
        o["book_add_bp"] = round(float((-E.y_x0940 - COST)[m & have & (E.locked_dn == 0)].sum() / max(have.sum(), 1) * 1e4), 2)   # 等权持有全部合格股的人当日因避让多赚
        o["in_forward_shadow_books"] = {b: {**avoid_stats(m & E.index.isin(books[b])), "codes": sorted(set(E.index[m]) & books[b])} for b in ("avoid_losers_70", "buffered_top100")}
        rec["avoid"][nm] = o
    if not a.replay:
        rec["minute_paths"] = save_paths(P, sig, E)
    append(P["settled"], rec)
    append(P["runs"], {"cmd": "settle", "signal_date": T, "at": n.isoformat()})
    return {"status": "settled", "signal_date": T, "exit_date": N, "universe_gross_bp_open": rec["universe"]["gross_bp"]["open"],
            "long": {"n": len(ok), "trade_gross_bp_open": rec["long_gap"]["trade_gross_bp"]["open"], "book_net_bp_open": rec["long_gap"]["book_net_bp"]["open"]},
            "avoid_q05": {k: rec["avoid"]["q05"].get(k) for k in ("n", "stepaside_net_bp", "book_add_bp")}, "pre_close_source": src}


def cmd_settle(a) -> dict:
    P = paths(a.test)
    cal = sessions()
    done = {r["signal_date"] for r in read_jsonl(P["settled"])}
    n = now()
    out = []
    for f in sorted(P["signals"].glob("*.json")):
        sig = json.loads(f.read_text())
        T, N = sig["signal_date"], sig.get("next_session")
        if (a.date and T != a.date) or T in done or not N:
            continue
        if not a.test and (n.strftime("%Y%m%d") < N or (n.strftime("%Y%m%d") == N and (n.hour, n.minute) < (10, 31))):
            out.append({"status": "not_ready", "signal_date": T})
            continue
        out.append(settle_one(sig, a, P, cal))
    return {"status": "ok", "results": out} if out else {"status": "nothing_to_settle"}


def cmd_bench(a) -> dict:
    """只测 14:46 那次拉取: 全股池快照 + 打分股票的当日 5 分钟线。"""
    codes = universe_codes()
    t = time.time()
    q, qs = fetch_quotes(codes)
    f = LIVE / "panel_live.parquet"
    sc = codes
    if a.universe == "adv" and f.exists():
        p = pd.read_parquet(f)
        p = p[p.trade_date == p.trade_date.max()]
        sc = sorted(set(p[p.avg_amount_lookback >= ADV_MIN].ts_code) | (dayfeat_codes() & set(codes)))
    b, bs = fetch_bars(sc, 48, a.workers)
    n48 = b.groupby("ts_code").size()
    return {"status": "bench", "at": now().isoformat(), "universe": len(codes), "quotes": qs, "bars": bs, "bars_codes_with_rows": int(len(n48)),
            "bars_codes_lt40_rows": int((n48 < 40).sum()), "seconds_total": round(time.time() - t, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "signal", "settle", "bench"])
    ap.add_argument("--date")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--replay", action="store_true", help="settle: 只用线仓, 不访问 tdx")
    ap.add_argument("--universe", choices=["adv", "all"], default="adv", help="14:46 拉取范围: adv = 前一日 60 日均额 >= 1 亿 + 市场变量用的研究股票; all = 全部主板+创业板")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--seed-dir", default=os.environ.get("OVN_TDX_SEED"))
    ap.add_argument("--live", action="store_true", help="signal --test --date 最近交易日: 仍走实时拉取路径(收盘后或周末演练计时用)")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-events", action="store_true")
    a = ap.parse_args(argv)
    LIVE.mkdir(parents=True, exist_ok=True)
    r = {"prep": cmd_prep, "signal": cmd_signal, "settle": cmd_settle, "bench": cmd_bench}[a.cmd](a)
    print(json.dumps(r, ensure_ascii=False, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
