#!/usr/bin/env python3
"""S3_intraday_shape_daily (分时形态日频) 两格CANDIDATE前瞻OOS纸面跟踪(2026-07-03冻结,
规则见 factor_factory/config/s3_frozen_cells.json)。

每个交易日收盘后运行:
  1) 增量拉取当日(T)全市场5min分钟线(逐股 pro.stk_mins), 缓存到
     stock_data/01-root/minute_cache_daily/minute_5min_<T>.parquet(单日一文件, 当日已存在则跳过);
  2) 对当日截面复用 build_intraday_shape_features.compute_day_features() 算8个日内形态特征
     (import复用, 不复制公式), 在前瞻人群(当日universe_flag>0且非688且非.BJ全市场)内做
     同日三分位(am_share)/中位二分(smooth)/符号(late_sign)分桶;
  3) 命中两冻结格的股票 = T+1交易名单, 当日写入 pending(status=pending, signal_date=T,
     trade_date=T+1);
  4) 对昨日pending, 用其trade_date(=T)当日的daily缓存open/close算O2C, 扣med成本(SlippageTable)
     与p75成本双轨记账, 回填status=settled; 涨停一字/高开>7%记为miss(不计收益, 如实标记);
  5) ledger逐笔带frozen_cell_signature哈希/regime_tag/med净/p75净, 追加到
     stock_data/03-working/health/s3_tracking_ledger.csv(不可回改, 同日重跑去重);
  6) 打印累积统计(距60笔, med与p75两轨, 单一regime占比); 若已结算笔的p75净累计<0且med净
     累计<0已连续20笔, 打印熔断预警。

纪律: 规则参数一律读 factor_factory/config/s3_frozen_cells.json (两格定义/事件域过滤链/
成本模式/人群定义与registry.gate6_verdict逐字一致); 本脚本不含任何可调阈值/不发明未注册规则。
分位/中位口径必须在"前瞻人群"(当日全市场可交易域)内计算, 不是800研究样本。
不触碰 setup_registry.json / wts_frozen_rules.json / 研究与holdout特征文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

WS = Path("/Users/fisher/.openclaw/workspace")
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from setup_gauntlet import (  # noqa: E402
    SlippageTable,
    liquidity_bucket_from_amount_ma,
    gap_bucket,
    realized_cost_dec,
    tstat,
)
from build_intraday_shape_features import compute_day_features  # noqa: E402

FROZEN_CELLS_PATH = WS / "factor_factory" / "config" / "s3_frozen_cells.json"
SLIPPAGE_PATH = WS / "stock_data" / "03-working" / "health" / "slippage_calibration.json"
BACKTEST_CACHE_DIR = WS / "stock_data" / "03-working" / "backtest_cache"
MINUTE_DAILY_DIR = WS / "stock_data" / "01-root" / "minute_cache_daily"
REGIME_FEATURES_PATH = WS / "stock_data" / "03-working" / "market_regime_features.parquet"
LEDGER_PATH = WS / "stock_data" / "03-working" / "health" / "s3_tracking_ledger.csv"
STATUS_PATH = WS / "stock_data" / "03-working" / "health" / "s3_tracking_status.json"

REVIEW_THRESHOLD_N = 60
FUSE_LOOKBACK_N = 20
SLEEP_BASE = 0.30  # 20260708: 0.15→0.30, 0.15踩tushare~400/分冷却线致全市场拉取~93分钟(超agent turn超时触发重试并发碰撞); 0.30约150/分留足余量, 拉取降到~40分钟
BACKOFF_SCHEDULE = (1, 2, 4)
MAX_CONSECUTIVE_FAILURES = 10

LEDGER_FIELDS = [
    "signal_date", "trade_date", "ts_code", "cell_label", "frozen_cell_signature",
    "status", "regime_tag", "gap_pct", "o2c_pct",
    "cost_med_bp", "net_med_pct", "cost_p75_bp", "net_p75_pct", "miss_reason",
    # top-20观察名单(20260704 topN_verdict: R_oversold_N20——两格并集内late_strength升序前20)
    "late_strength", "oversold_rank", "in_top20",
]
WATCHLIST_OUT = WS / "stock-report" / "data" / "latest" / "s3_watchlist.json"
REGISTRY_PATH = WS / "factor_factory" / "config" / "setup_registry.json"


# ============================== 冻结规则加载 ==============================

def load_frozen_cells() -> dict:
    with open(FROZEN_CELLS_PATH, "r", encoding="utf-8") as f:
        doc = json.load(f)
    # 自检: 现算签名必须与文件内落盘签名一致(防止文件被静默改动而未重新签名)
    check = {k: v for k, v in doc.items()
             if k not in ("frozen_cell_signature", "frozen_cell_signature_note")}
    import hashlib
    canonical = json.dumps(check, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stored = doc.get("frozen_cell_signature")
    if recomputed != stored:
        raise SystemExit(
            f"[FATAL] s3_frozen_cells.json 签名自检失败! 文件可能被就地修改未重新签名。"
            f"stored={stored} recomputed={recomputed} —— 拒绝执行,防止漂移。"
        )
    return doc


# ============================== Tushare 分钟数据增量拉取 ==============================

def get_pro():
    sys.path.insert(0, str(WS / "skills" / "stock-analyzer"))
    from pipeline import init_tushare
    return init_tushare()


def fetch_mins_with_retry(pro, ts_code: str, start_dt: str, end_dt: str, freq: str = "5min"):
    last_exc = None
    for attempt in range(len(BACKOFF_SCHEDULE) + 1):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq=freq, start_date=start_dt, end_date=end_dt)
            return df, True
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < len(BACKOFF_SCHEDULE):
                time.sleep(BACKOFF_SCHEDULE[attempt])
    print(f"    [FAIL] stk_mins {ts_code}: {type(last_exc).__name__} {str(last_exc)[:80]}")
    return None, False


def load_daily_cache(date: str) -> pd.DataFrame | None:
    p = BACKTEST_CACHE_DIR / f"daily_{date}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["trade_date"] = df["trade_date"].astype(str)
    return df


STOCK_LIST_PATH = WS / "stock_data" / "01-root" / "stock_list.csv"
_ST_NAME_PAT = None  # lazy
_STOCK_LIST_CACHE: dict | None = None


def _load_stock_list() -> dict:
    """stock_list.csv(4925行,含次新) -> {ts_code: {name, list_date}}。缓存一次。"""
    global _STOCK_LIST_CACHE
    if _STOCK_LIST_CACHE is None:
        try:
            sl = pd.read_csv(STOCK_LIST_PATH, dtype={"list_date": str})
            _STOCK_LIST_CACHE = {
                r["ts_code"]: {"name": str(r["name"]), "list_date": str(r["list_date"] or "")}
                for _, r in sl.iterrows()
            }
        except Exception as _e:  # noqa: BLE001
            print(f"[s3] stock_list加载失败({type(_e).__name__}),ST/次新过滤退化为跳过: {_e}")
            _STOCK_LIST_CACHE = {}
    return _STOCK_LIST_CACHE


def forward_population(date: str) -> pd.DataFrame | None:
    """前瞻人群(冻结定义,registry.forward_population_frozen): 当日universe_flag>0且非688且非.BJ。

    20260710审计修正: 原实现只做了 close>0/vol>0/板块排除, 【漏了universe_flag语义中的
    ST/退市/次新排除】——234笔ST/退市股混入ledger(用户发现名称缺失暴露)。本修正=向冻结
    定义收敛(非判据修改): universe_flag的实质语义(可交易+非ST/退市+非次新+板块排除)
    用stock_list.csv名称正则(与build_universe同款name代理口径)+上市>=90自然日近似实现;
    不在stock_list者(已摘牌)一并排除。"""
    import re
    df = load_daily_cache(date)
    if df is None:
        return None
    df = df[(df["close"] > 0) & (df["vol"] > 0)].copy()
    df = df[~df["ts_code"].str.startswith("688")].copy()
    df = df[~df["ts_code"].str.endswith(".BJ")].copy()

    sl = _load_stock_list()
    if sl:
        bad = re.compile(r"ST|退")
        def _ok(code: str) -> bool:
            meta = sl.get(code)
            if meta is None:
                return False  # 不在在市名单=已摘牌/未知,排除
            if bad.search(meta["name"]):
                return False  # ST/*ST/退市整理期
            ld = meta["list_date"]
            if ld and ld.isdigit() and len(ld) == 8:
                try:
                    days = (datetime.strptime(date, "%Y%m%d") - datetime.strptime(ld, "%Y%m%d")).days
                    if days < 90:  # 上市<90自然日≈60交易日(次新,与S8硬化同口径)
                        return False
                except ValueError:
                    pass
            return True
        n_before = len(df)
        df = df[df["ts_code"].map(_ok)].copy()
        print(f"[s3] universe过滤(ST/退/次新/摘牌,冻结定义收敛): {n_before} -> {len(df)}")
    return df


MINUTE_FLUSH_EVERY = 300  # 每拉N只落一次断点(20260707加固:防SIGTERM/休眠整天丢失)


def _flush_partial(frames: list, partial_path: Path) -> None:
    """把已拉分钟数据原子落到断点文件(tmp+replace)。中途被杀时最多丢最近一批。"""
    if not frames:
        return
    out = pd.concat(frames, ignore_index=True)
    tmp = partial_path.with_suffix(".partial.tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(partial_path)


def write_status(signal_date: str, generation_ok: bool, generation_err: str, n_new: int) -> None:
    """写运行状态供健康告警读取(20260707加固: S3失败不再静默,可被health_alert发现)。
    generation_ok=False 表示阶段A结算已完成但阶段B新信号生成失败(分钟拉取被中断)。"""
    try:
        wl_gen_at = None
        wl_path = WS / "stock-report" / "data" / "latest" / "s3_watchlist.json"
        if wl_path.exists():
            wl_gen_at = json.loads(wl_path.read_text()).get("generated_at")
        doc = {
            "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_date": signal_date,
            "generation_ok": bool(generation_ok),
            "generation_err": generation_err[:200] if generation_err else "",
            "n_new_signals": int(n_new),
            "watchlist_generated_at": wl_gen_at,
        }
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        tmp.replace(STATUS_PATH)
    except Exception as _e:  # noqa: BLE001
        print(f"[s3] 状态文件写入失败(非致命): {type(_e).__name__}: {_e}")


def ensure_minute_cache_for_day(pro, date: str, ts_codes: list[str]) -> Path:
    """按日增量拉取,带断点续拉(20260707加固事故: 20-30分钟无值守拉取曾被系统空闲休眠
    SIGTERM杀掉,仅末尾一次落盘→整天丢失且无残留)。
    - 若 minute_5min_<date>.parquet 已存在则整日跳过。
    - 否则逐股拉取, 每 MINUTE_FLUSH_EVERY 只原子落一次断点文件 .partial.parquet;
      重跑时读断点、跳过已拉股票、只续拉剩余(SIGTERM/休眠中断后重跑即续)。
    - 全部拉完才写最终文件并删断点; 连续失败早停则保留断点并抛错(下次续拉)。"""
    import signal as _signal
    MINUTE_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MINUTE_DAILY_DIR / f"minute_5min_{date}.parquet"
    if out_path.exists():
        print(f"[s3] {date} 分钟缓存已存在, 跳过拉取: {out_path}")
        return out_path

    partial_path = MINUTE_DAILY_DIR / f"minute_5min_{date}.partial.parquet"
    start_dt = f"{date[:4]}-{date[4:6]}-{date[6:8]} 09:00:00"
    end_dt = f"{date[:4]}-{date[4:6]}-{date[6:8]} 15:30:00"

    frames: list = []
    done_codes: set = set()
    if partial_path.exists():
        try:
            prev = pd.read_parquet(partial_path)
            if len(prev) and "ts_code" in prev.columns:
                frames.append(prev)
                done_codes = set(prev["ts_code"].astype(str).unique())
                print(f"[s3] 发现断点文件, 已有{len(done_codes)}只, 续拉剩余: {partial_path}")
        except Exception as _e:  # noqa: BLE001
            print(f"[s3] 断点文件损坏({type(_e).__name__}: {_e}), 从头拉")
            frames, done_codes = [], set()

    remaining = [c for c in ts_codes if str(c) not in done_codes]
    print(f"[s3] {date} 待拉 {len(remaining)}/{len(ts_codes)} 只(已续拉{len(done_codes)})")

    # SIGTERM/SIGINT 时尽力落一次断点再退(周期性flush是主保险,这是补充)。
    def _on_term(signum, _frame):
        print(f"[s3] 收到信号{signum}, 落断点后退出(已拉{len(frames)}批)")
        _flush_partial(frames, partial_path)
        raise SystemExit(f"[s3] 被信号{signum}中断, 断点已存, 重跑即续")
    _prev_term = _signal.signal(_signal.SIGTERM, _on_term)
    _prev_int = _signal.signal(_signal.SIGINT, _on_term)

    consecutive_failures = 0
    ok_count = 0
    since_flush = 0
    finished_all = True
    t0 = time.time()
    try:
        for i, ts_code in enumerate(remaining, start=1):
            df, ok = fetch_mins_with_retry(pro, ts_code, start_dt, end_dt)
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"[s3] 连续失败{consecutive_failures}次, 提前停止(已拉{ok_count}只), 保留断点续拉")
                    finished_all = False
                    break
                time.sleep(SLEEP_BASE)
                continue
            consecutive_failures = 0
            if df is not None and len(df) > 0:
                frames.append(df)
                ok_count += 1
                since_flush += 1
            if since_flush >= MINUTE_FLUSH_EVERY:
                _flush_partial(frames, partial_path)
                since_flush = 0
                elapsed = time.time() - t0
                print(f"  [s3] 分钟拉取进度 {i}/{len(remaining)}  已用时{elapsed:.0f}s  断点已落")
            time.sleep(SLEEP_BASE)
    finally:
        _signal.signal(_signal.SIGTERM, _prev_term)
        _signal.signal(_signal.SIGINT, _prev_int)

    if not frames:
        raise SystemExit(f"[FATAL] {date} 未拉取到任何分钟数据(0/{len(ts_codes)}成功),中止本日流程。")

    if not finished_all:
        _flush_partial(frames, partial_path)
        raise SystemExit(f"[s3] {date} 分钟拉取未完成(连续失败早停), 已存断点{partial_path}, 下次重跑续拉。")

    out = pd.concat(frames, ignore_index=True)
    if "ts_code" in out.columns and "trade_time" in out.columns:
        out = out.drop_duplicates(subset=["ts_code", "trade_time"])  # 断点重叠去重
    tmp = out_path.with_suffix(".parquet.tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(out_path)
    partial_path.unlink(missing_ok=True)  # 成功后删断点
    elapsed = time.time() - t0
    print(f"[s3] {date} 分钟缓存写入完成: {out_path}  成功{ok_count}/{len(remaining)}只(本轮)  用时{elapsed:.0f}s")
    return out_path


# ============================== 特征计算(复用 compute_day_features) ==============================

def build_features_for_day(minute_path: Path, date: str) -> pd.DataFrame:
    """对单日分钟缓存(全市场当日), 逐股调用 compute_day_features 算8个日内形态特征。
    输出: ts_code, trade_date(YYYYMMDD), am_vol_share, late_strength, path_smoothness, n_bars, ...
    """
    df = pd.read_parquet(minute_path)
    if df.empty:
        return pd.DataFrame()
    dt = pd.to_datetime(df["trade_time"], errors="coerce")
    bad = dt.isna()
    if bad.any():
        df = df.loc[~bad].copy()
        dt = dt.loc[~bad]
    df = df.copy()
    df["_dt"] = dt
    df["time_str"] = df["_dt"].dt.strftime("%H:%M")
    df = df.sort_values(["ts_code", "_dt"])

    rows = []
    for ts_code, day_df in df.groupby("ts_code", sort=True):
        feats = compute_day_features(day_df)
        feats["ts_code"] = ts_code
        feats["trade_date"] = date
        rows.append(feats)
    return pd.DataFrame(rows)


# ============================== 分桶(与setup_gauntlet.load_setup_s3同一写法) ==============================

def bucket_features(feat: pd.DataFrame) -> pd.DataFrame:
    ev = feat.copy()
    ev["am_share"] = pd.qcut(ev["am_vol_share"].rank(method="first"), 3,
                              labels=["低", "中", "高"]) if ev["am_vol_share"].notna().sum() >= 3 else np.nan
    late_sign_raw = np.sign(ev["late_strength"])
    late_sign_raw = pd.Series(np.where(late_sign_raw == 0, np.nan, late_sign_raw), index=ev.index)
    ev["late_sign"] = late_sign_raw.map({1.0: "强", -1.0: "弱"})
    med_smooth = ev["path_smoothness"].median()
    ev["smooth"] = np.where(ev["path_smoothness"] <= med_smooth, "低", "高")
    ev.loc[ev["path_smoothness"].isna() | pd.isna(med_smooth), "smooth"] = None
    ev["am_share"] = ev["am_share"].astype(str)
    ev["late_sign"] = ev["late_sign"].astype(str)
    ev["smooth"] = ev["smooth"].astype(str)
    return ev


def amount_ma20_wan(date: str) -> pd.DataFrame:
    """从最近~30个交易日的daily缓存滚动计算20日均成交额(万元), 独立于滞后更新的panel_full。"""
    files = sorted(BACKTEST_CACHE_DIR.glob("daily_*.parquet"))
    dates_avail = sorted(p.stem.replace("daily_", "") for p in files if p.stem.replace("daily_", "") <= date)
    window = dates_avail[-30:]
    frames = []
    for d in window:
        p = BACKTEST_CACHE_DIR / f"daily_{d}.parquet"
        df = pd.read_parquet(p, columns=["ts_code", "trade_date", "amount"])
        df["trade_date"] = df["trade_date"].astype(str)
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["ts_code", "amount_ma_20_wan"])
    allf = pd.concat(frames, ignore_index=True)
    allf = allf.sort_values(["ts_code", "trade_date"])
    ma = allf.groupby("ts_code")["amount"].apply(lambda s: s.tail(20).mean())
    out = ma.reset_index()
    out.columns = ["ts_code", "amount_ma20_raw"]
    # amount单位为千元(与panel_full.amount同单位,已实测验证一致); 万元换算 /10,
    # 与 setup_gauntlet.load_setup_s3 的 amt_ma20_wan = amount_ma_20 / 10.0 同一换算。
    out["amount_ma_20_wan"] = out["amount_ma20_raw"] / 10.0
    return out[["ts_code", "amount_ma_20_wan"]]


def get_regime_tag(date: str) -> str:
    if REGIME_FEATURES_PATH.exists():
        try:
            rg = pd.read_parquet(REGIME_FEATURES_PATH, columns=["trade_date", "regime_tag"])
            rg["trade_date"] = rg["trade_date"].astype(str)
            hit = rg[rg["trade_date"] == date]
            if len(hit):
                return str(hit["regime_tag"].iloc[0])
        except Exception:
            pass
    # 现算(同classify_regime规则)
    df = load_daily_cache(date)
    if df is None:
        return "NA"
    valid = df.dropna(subset=["pct_chg"])
    if len(valid) == 0:
        return "NA"
    limitup = int((valid["pct_chg"] >= 9.5).sum())
    if limitup >= 80:
        return "活跃(现算,amount_z20未知从简判定)"
    if limitup <= 25:
        return "冰点"
    return "中性"


# ============================== ledger 读写 ==============================

def load_ledger() -> pd.DataFrame:
    if not LEDGER_PATH.exists():
        return pd.DataFrame(columns=LEDGER_FIELDS)
    df = pd.read_csv(LEDGER_PATH, dtype=str)
    # 兼容旧schema(20260703首批无top20列): 缺列补空,rewrite时统一到新schema。
    for c in LEDGER_FIELDS:
        if c not in df.columns:
            df[c] = ""
    # dtype=str 会得到 arrow string 列,结算回写 float 会 TypeError——统一转 object。
    return df.astype(object)


def save_ledger(df: pd.DataFrame) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, columns=LEDGER_FIELDS)
    tmp.replace(LEDGER_PATH)


# ============================== 信号生成(step 1-3) ==============================

def generate_signals(frozen: dict, signal_date: str, trade_date: str, pro) -> pd.DataFrame:
    pop = forward_population(signal_date)
    if pop is None:
        raise SystemExit(f"[FATAL] daily缓存缺 {signal_date}, 中止(前瞻人群无法定义)。")
    print(f"[s3] 前瞻人群(T={signal_date}, 剔688/.BJ, 可交易): {len(pop)} 只")

    ts_codes = sorted(pop["ts_code"].unique().tolist())
    minute_path = ensure_minute_cache_for_day(pro, signal_date, ts_codes)

    feat = build_features_for_day(minute_path, signal_date)
    if feat.empty:
        raise SystemExit(f"[FATAL] {signal_date} 特征计算结果为空, 中止。")
    n_before_nbars = len(feat)
    feat = feat[feat["n_bars"] >= 40].copy()
    print(f"[s3] n_bars>=40过滤: {n_before_nbars} -> {len(feat)}")

    feat = feat.merge(pop[["ts_code"]], on="ts_code", how="inner")
    print(f"[s3] 与前瞻人群inner join后: {len(feat)}")

    ev = bucket_features(feat)
    ev = ev.dropna(subset=["am_vol_share", "late_strength", "path_smoothness"])
    ev = ev[(ev["am_share"] != "nan") & (ev["late_sign"] != "nan") & (ev["smooth"] != "nan")].copy()
    print(f"[s3] 三特征分桶后(剔late_sign=0/NaN等): {len(ev)}")

    # top-20观察名单(topN_verdict 20260704): 两格并集内按 late_strength 升序(最超卖在前)排名。
    cell_masks = []
    for cell in frozen["cells_candidate"]:
        cell_masks.append((ev["am_share"] == cell["am_share"]) & (ev["late_sign"] == cell["late_sign"])
                          & (ev["smooth"] == cell["smooth"]))
    union_mask = cell_masks[0]
    for m in cell_masks[1:]:
        union_mask = union_mask | m
    basket = ev[union_mask].copy()
    basket["oversold_rank"] = basket["late_strength"].rank(ascending=True, method="first").astype(int)
    rank_map = dict(zip(basket["ts_code"], basket["oversold_rank"]))

    signals = []
    regime = get_regime_tag(signal_date)
    for cell, mask in zip(frozen["cells_candidate"], cell_masks):
        hit = ev[mask]
        for _, row in hit.iterrows():
            rk = int(rank_map.get(row["ts_code"], 0))
            signals.append({
                "signal_date": signal_date,
                "trade_date": trade_date,
                "ts_code": row["ts_code"],
                "cell_label": cell["label"],
                "frozen_cell_signature": frozen["frozen_cell_signature"],
                "status": "pending",
                "regime_tag": regime,
                "gap_pct": "", "o2c_pct": "",
                "cost_med_bp": "", "net_med_pct": "",
                "cost_p75_bp": "", "net_p75_pct": "",
                "miss_reason": "",
                "late_strength": round(float(row["late_strength"]), 6),
                "oversold_rank": rk,
                "in_top20": "1" if 0 < rk <= 20 else "0",
            })
    return pd.DataFrame(signals, columns=LEDGER_FIELDS)


# ============================== 结算(step 4) ==============================

def settle_pending(frozen: dict, ledger: pd.DataFrame, slip: SlippageTable) -> pd.DataFrame:
    """对全部pending笔结算。liq档三分位阈值必须在"当日(trade_date)前瞻人群整体"截面上计算
    (与setup_gauntlet.load_setup_s3同一"同日截面"精神),因此按trade_date分组批量处理,
    不可逐行调用liquidity_bucket_from_amount_ma(那样单行Series的分位数会退化,見上文注释教训)。"""
    if ledger.empty:
        return ledger
    pending_mask = ledger["status"] == "pending"
    if not pending_mask.any():
        return ledger

    for t, idxs in ledger[pending_mask].groupby("trade_date").groups.items():
        d1 = load_daily_cache(t)
        if d1 is None:
            continue  # T日(=trade_date)缓存还没有, 下次再结算, 留在pending
        d1_map = d1.set_index("ts_code")

        # liq档三分位阈值: 在当日"前瞻人群"整体(剔688/.BJ的全市场可交易域)上计算,
        # 而非仅在待结算子集上算, 保持与信号生成阶段/gate6同一"同日截面"基准。
        pop_t = forward_population(t)
        ma20_t = amount_ma20_wan(t) if pop_t is not None else None
        if pop_t is not None and ma20_t is not None:
            pop_liq = pop_t[["ts_code"]].merge(ma20_t, on="ts_code", how="left")
            pop_liq["liq_bkt"] = liquidity_bucket_from_amount_ma(pop_liq["amount_ma_20_wan"])
            liq_map = pop_liq.set_index("ts_code")["liq_bkt"]
        else:
            liq_map = pd.Series(dtype=str)

        for idx in idxs:
            row = ledger.loc[idx]
            ts_code = row["ts_code"]
            signal_date = row["signal_date"]
            d0 = load_daily_cache_for_signal_close(signal_date)
            d0_map = d0.set_index("ts_code") if d0 is not None else None

            if ts_code not in d1_map.index:
                ledger.loc[idx, "status"] = "miss"
                ledger.loc[idx, "miss_reason"] = "T+1 daily缓存无该股(退市/停牌)"
                continue
            rec1 = d1_map.loc[ts_code]
            open1 = float(rec1["open"])
            close1 = float(rec1["close"])
            close0 = float(d0_map.loc[ts_code]["close"]) if (d0_map is not None and ts_code in d0_map.index) else np.nan

            if open1 <= 0 or close1 <= 0:
                ledger.loc[idx, "status"] = "miss"
                ledger.loc[idx, "miss_reason"] = "T+1开盘或收盘价非正"
                continue

            is_yizi = abs(open1 - close1) < 1e-9
            gap = (open1 / close0 - 1.0) if (pd.notna(close0) and close0 > 0) else np.nan
            if is_yizi:
                ledger.loc[idx, "status"] = "miss"
                ledger.loc[idx, "miss_reason"] = "T+1一字(open1==close1),gate6同规则不计收益"
                continue
            if pd.notna(gap) and gap > 0.07:
                ledger.loc[idx, "status"] = "miss"
                ledger.loc[idx, "miss_reason"] = f"T+1高开>7%(gap={gap:.4f}),gate6同规则不计收益"
                continue

            o2c = close1 / open1 - 1.0

            liq_bkt = liq_map.get(ts_code, "中流动")
            if pd.isna(liq_bkt):
                liq_bkt = "中流动"
            gap_val = gap if pd.notna(gap) else 0.0
            gbkt = gap_bucket(pd.Series([gap_val])).iloc[0]

            cost_med = realized_cost_dec(pd.Series([liq_bkt]), pd.Series([gbkt]), slip, "med").iloc[0]
            cost_p75 = realized_cost_dec(pd.Series([liq_bkt]), pd.Series([gbkt]), slip, "p75").iloc[0]

            ledger.loc[idx, "status"] = "settled"
            ledger.loc[idx, "gap_pct"] = round(gap * 100, 4) if pd.notna(gap) else ""
            ledger.loc[idx, "o2c_pct"] = round(o2c * 100, 4)
            ledger.loc[idx, "cost_med_bp"] = round(cost_med * 10000, 2)
            ledger.loc[idx, "net_med_pct"] = round((o2c - cost_med) * 100, 4)
            ledger.loc[idx, "cost_p75_bp"] = round(cost_p75 * 10000, 2)
            ledger.loc[idx, "net_p75_pct"] = round((o2c - cost_p75) * 100, 4)
    return ledger


def load_daily_cache_for_signal_close(signal_date: str) -> pd.DataFrame | None:
    return load_daily_cache(signal_date)


# ============================== 统计打印(step 5-6) ==============================

def print_stats(ledger: pd.DataFrame) -> None:
    settled = ledger[ledger["status"] == "settled"].copy()
    for col in ("net_med_pct", "net_p75_pct"):
        settled[col] = pd.to_numeric(settled[col], errors="coerce")

    n_pending = int((ledger["status"] == "pending").sum())
    n_miss = int((ledger["status"] == "miss").sum())
    n_settled = len(settled)
    print(f"\n[s3] ledger累积: 总{len(ledger)}笔  settled={n_settled}  pending={n_pending}  miss={n_miss}")

    for label, g in settled.groupby("cell_label"):
        n = len(g)
        med = g["net_med_pct"].dropna().to_numpy()
        p75 = g["net_p75_pct"].dropna().to_numpy()
        t_med = tstat(med) if len(med) else 0.0
        t_p75 = tstat(p75) if len(p75) else 0.0
        print(f"  格{label}: settled n={n}/{REVIEW_THRESHOLD_N}  "
              f"med净均值={med.mean() if len(med) else float('nan'):+.4f}% t={t_med:.2f}  "
              f"p75净均值={p75.mean() if len(p75) else float('nan'):+.4f}% t={t_p75:.2f}")
        if len(g):
            regime_counts = g["regime_tag"].value_counts(normalize=True)
            top_regime, top_share = regime_counts.index[0], regime_counts.iloc[0]
            print(f"    regime分布: {regime_counts.to_dict()}  单一regime最高占比={top_share:.0%}({top_regime})")
            if n >= REVIEW_THRESHOLD_N and top_share > 0.70:
                print(f"    [注意] 满{REVIEW_THRESHOLD_N}笔但单一regime贡献>70%, 依registry不得冻结,须分层复核")

    # 全体两格合计
    if n_settled:
        med_all = settled["net_med_pct"].dropna().to_numpy()
        p75_all = settled["net_p75_pct"].dropna().to_numpy()
        print(f"  合计(两格): settled n={n_settled}  "
              f"med净均值={med_all.mean():+.4f}% t={tstat(med_all):.2f}  "
              f"p75净均值={p75_all.mean():+.4f}% t={tstat(p75_all):.2f}")

    # 熔断预警: 已结算笔按时间序, 最近20笔p75净和med净都<0则预警
    if n_settled >= FUSE_LOOKBACK_N:
        settled_sorted = settled.sort_values(["trade_date", "ts_code"])
        recent = settled_sorted.tail(FUSE_LOOKBACK_N)
        p75_cum = recent["net_p75_pct"].dropna().sum()
        med_cum = recent["net_med_pct"].dropna().sum()
        if p75_cum < 0 and med_cum < 0:
            print(f"\n🚨 熔断预警: 最近{FUSE_LOOKBACK_N}笔已结算, p75净累计={p75_cum:+.4f}% (<0) "
                  f"且med净累计={med_cum:+.4f}% (<0) —— 提前预警,不等满60笔。")


# ============================== main ==============================

def _name_map() -> dict:
    """名称映射: stock_list.csv为主(4925行含次新,7/4刷新)+panel_full兜底。
    20260710修: 原只读panel_full——面板结构性不含ST/退市/次新且冻结在7/2,
    看板top20曾5/20无名称(001312伪名事故的S3侧同类,当时只修了发布层)。"""
    m: dict = {}
    try:
        pn = pd.read_parquet(WS / "factor_factory" / "data" / "factors" / "greenfield_multifactor_panel_full.parquet",
                             columns=["ts_code", "trade_date", "name"])
        pn = pn.sort_values("trade_date").drop_duplicates("ts_code", keep="last")
        m.update(dict(zip(pn["ts_code"], pn["name"].astype(str))))
    except Exception:
        pass
    for code, meta in _load_stock_list().items():
        nm = (meta.get("name") or "").strip()
        if nm and not (m.get(code) or "").strip():
            m[code] = nm
    return m


def write_watchlist(ledger: pd.DataFrame, frozen: dict) -> None:
    """输出 S3 top-20 观察看板数据(研究观察·非买入建议): 最新名单 + 每日组合净(med/p75双轨) + 累计。"""
    d = ledger.copy()
    d = d[d["in_top20"].astype(str) == "1"]
    if d.empty:
        print("[s3] watchlist: 无top20行,跳过")
        return
    names = _name_map()
    latest_sig = str(max(d["signal_date"].astype(str)))
    cur = d[d["signal_date"].astype(str) == latest_sig].copy()
    cur["oversold_rank"] = pd.to_numeric(cur["oversold_rank"], errors="coerce")
    cur = cur.sort_values("oversold_rank")
    top20 = [{
        "rank": int(r["oversold_rank"]),
        "ts_code": r["ts_code"],
        "name": names.get(r["ts_code"], ""),
        "cell": r["cell_label"],
        "late_strength_pct": round(float(r["late_strength"]) * 100, 2) if str(r["late_strength"]) not in ("", "nan") else None,
        "status": r["status"],
        "trade_date": r["trade_date"],
        "gap_pct": float(r["gap_pct"]) if str(r["gap_pct"]) not in ("", "nan") else None,
        "o2c_pct": float(r["o2c_pct"]) if str(r["o2c_pct"]) not in ("", "nan") else None,
        "net_med_pct": float(r["net_med_pct"]) if str(r["net_med_pct"]) not in ("", "nan") else None,
        "net_p75_pct": float(r["net_p75_pct"]) if str(r["net_p75_pct"]) not in ("", "nan") else None,
        "miss_reason": r["miss_reason"] or None,
    } for _, r in cur.iterrows()]

    st = d[d["status"] == "settled"].copy()
    daily, cum_med, cum_p75, curve = [], 0.0, 0.0, []
    peak, mdd = 0.0, 0.0
    for sig, g in sorted(st.groupby("signal_date"), key=lambda kv: str(kv[0])):
        nm = pd.to_numeric(g["net_med_pct"], errors="coerce").mean()
        np75 = pd.to_numeric(g["net_p75_pct"], errors="coerce").mean()
        cum_med += float(nm); cum_p75 += float(np75)
        peak = max(peak, cum_med); mdd = min(mdd, cum_med - peak)
        daily.append({"signal_date": str(sig), "n": int(len(g)),
                      "day_net_med_pct": round(float(nm), 3), "day_net_p75_pct": round(float(np75), 3),
                      "cum_med_pct": round(cum_med, 3), "cum_p75_pct": round(cum_p75, 3)})
    n_days = len(daily)
    hit_days = sum(1 for x in daily if x["day_net_med_pct"] > 0)
    reg = {}
    try:
        reg = json.load(open(REGISTRY_PATH)).get("setups", [])
        reg = next((s.get("topN_verdict") or {} for s in reg if s.get("id") == "S3_intraday_shape_daily"), {})
    except Exception:
        pass
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": "S3 分时形态 · top-20 观察名单",
        "honesty_banner": "研究观察名单·非买入建议。CANDIDATE处于前瞻60笔审判制;中位滑点口径成立,保守(p75)成本口径为负——执行质量决定生死。",
        "frozen_cell_signature": frozen.get("frozen_cell_signature"),
        "ranking_rule": "两冻结格并集内按尾盘强度(late_strength)升序取前20(最超卖优先)——topN_verdict 20260704",
        "latest_signal_date": latest_sig,
        "latest_top20": top20,
        "daily_series": daily,
        "cumulative": {
            "n_days": n_days, "hit_days": hit_days,
            "hit_days_pct": round(100 * hit_days / n_days, 1) if n_days else None,
            "cum_net_med_pct": round(cum_med, 3), "cum_net_p75_pct": round(cum_p75, 3),
            "max_drawdown_med_pct": round(mdd, 3),
            "progress_60": f"{n_days}/60",
        },
        "backtest_context": {
            "research": reg.get("research"), "holdout_second_look": reg.get("holdout_second_look"),
            "p75_warning": reg.get("p75"),
        },
    }
    WATCHLIST_OUT.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[s3] watchlist已输出: {WATCHLIST_OUT}  最新名单{len(top20)}只  已结算日{n_days}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal-date", default=None, help="信号日T(默认: daily缓存最新日期)")
    args = ap.parse_args()

    frozen = load_frozen_cells()
    print(f"[s3] frozen_cell_signature={frozen['frozen_cell_signature']}")

    if args.signal_date:
        signal_date = args.signal_date
    else:
        files = sorted(BACKTEST_CACHE_DIR.glob("daily_*.parquet"))
        if not files:
            raise SystemExit("[FATAL] backtest_cache下无daily_*.parquet, 无法确定信号日")
        signal_date = files[-1].stem.replace("daily_", "")

    all_dates = sorted(p.stem.replace("daily_", "") for p in BACKTEST_CACHE_DIR.glob("daily_*.parquet"))
    if signal_date not in all_dates:
        raise SystemExit(f"[FATAL] daily缓存缺 {signal_date}, 中止并打印原因: 需先跑日线增量拉取。")
    idx = all_dates.index(signal_date)
    pro = get_pro()
    if idx + 1 >= len(all_dates):
        # 设计修正(20260704): 信号日=最新缓存日时,用交易日历定T+1并照常生成pending——
        # 观察名单必须当晚可见,不能等T+1缓存到位才出现。
        trade_date = None
        try:
            from datetime import datetime as _dt, timedelta as _td
            _start = (_dt.strptime(signal_date, "%Y%m%d") + _td(days=1)).strftime("%Y%m%d")
            _end = (_dt.strptime(signal_date, "%Y%m%d") + _td(days=14)).strftime("%Y%m%d")
            cal = pro.trade_cal(exchange="SSE", start_date=_start, end_date=_end)
            opens = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str))
            if opens:
                trade_date = opens[0]
                print(f"[s3] {signal_date} 为最新缓存日, 按交易日历定 T+1={trade_date}(pending, 待结算)")
        except Exception as _e:
            print(f"[s3] 交易日历获取失败({_e}), 本日不生成信号(退化为旧行为)")
    else:
        trade_date = all_dates[idx + 1]

    ledger = load_ledger()
    slip = SlippageTable(SLIPPAGE_PATH)

    # ── 阶段A(廉价,只需日频缓存,与阶段B昂贵分钟拉取解耦; 20260707加固事故):
    #    先结算pending并立即持久化ledger+watchlist。即便阶段B分钟拉取被系统休眠SIGTERM
    #    杀掉,结算成果与看板刷新已落盘,不再被"新信号生成"整体拖累而丢失。
    ledger = settle_pending(frozen, ledger, slip)
    save_ledger(ledger)
    print(f"[s3] 阶段A结算完成, ledger已落盘: {LEDGER_PATH}  总行数={len(ledger)}")
    try:
        write_watchlist(ledger, frozen)
    except Exception as _e:
        print(f"[s3] watchlist输出失败(非致命): {type(_e).__name__}: {_e}")

    # ── 阶段B(昂贵,可能中途被杀): 生成新信号。失败不影响已持久化的阶段A结算。
    gen_ok, gen_err = True, ""
    new_signals = pd.DataFrame(columns=LEDGER_FIELDS)
    if trade_date is not None:
        try:
            new_signals = generate_signals(frozen, signal_date, trade_date, pro)
        except SystemExit as _e:
            gen_ok, gen_err = False, str(_e)
            print(f"[s3] 阶段B新信号生成失败(阶段A结算已保全): {_e}")
    else:
        print("[s3] 无T+1交易日(信号日无后继交易日历), 本轮不生成新信号")

    if len(new_signals):
        # 同日重跑去重: 剔除本次signal_date已有的旧pending再并入
        if not ledger.empty:
            ledger = ledger[ledger["signal_date"].astype(str) != str(signal_date)].copy()
        ledger = pd.concat([ledger, new_signals], ignore_index=True)
        print(f"[s3] {signal_date} 新增信号(pending, T+1={trade_date}): {len(new_signals)} 笔")
        for label, g in new_signals.groupby("cell_label"):
            print(f"  格{label}: {len(g)}只  样例={g['ts_code'].head(5).tolist()}")
        save_ledger(ledger)
        try:
            write_watchlist(ledger, frozen)
        except Exception as _e:
            print(f"[s3] watchlist输出失败(非致命): {type(_e).__name__}: {_e}")

    write_status(signal_date, gen_ok, gen_err, len(new_signals))
    print_stats(ledger)
    return 0 if gen_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
