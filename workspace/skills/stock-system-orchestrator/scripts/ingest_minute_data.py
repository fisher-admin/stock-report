#!/usr/bin/env python3
"""分钟数据首批脚本(改造方案 B0, 2026-07-02; 分页修复 2026-07-03)。

两种模式:
  --sample            固定随机种子(seed=20260702)从全A抽800只研究样本 + 独立800只holdout样本,
                       拉 5min K线 2022-01-01 → 今。
                       [2026-07-03 修复] stk_mins 接口单次调用只返回约8000行(~166个交易日),
                       --sample 模式改为按【日期窗口分页】(默认每窗约6个月, 见 SAMPLE_WINDOW_DAYS),
                       从 start 到 end 逐窗拉取并按 ts_code+trade_time 去重合并;
                       每只股票已有数据会先探测其 trade_time 的 min/max, 完全落在已覆盖区间内的窗口直接跳过,
                       实现增量续拉。checkpoint 按 "<ts_code>__<window_start>_<window_end>" 记录已完成的窗口,
                       可随时中断续跑。
  --limitup-events     从 stock_data/01-root/*.parquet 日线缓存中找全部涨停事件(pct_chg>=9.5),
                       拉事件股 ±5 交易日窗口的 5min K线。
  --smoke              冒烟模式: 只拉 3 只股票 x 1 个月, 验证脚本正确性, 不写入正式缓存位置逻辑之外的东西。

输出:
    stock_data/01-root/minute_cache/minute_5min_<ts_code_no_dot>.parquet   (按股票分区, 追加合并去重)
    stock_data/01-root/minute_cache/minute_ingest_checkpoint.json          (记录 ts_code+完成窗口)
    stock_data/01-root/minute_cache/minute_research_stocks.json           (--sample 模式研究样本)
    stock_data/01-root/minute_cache/minute_holdout_stocks.json            (--sample 模式独立复现样本, 首轮研究禁止触碰)

限速/退避: 同 ingest_auction_data.py, 每次调用间 sleep 0.15s 起步, 异常按 1/2/4s 退避重试最多3次,
连续10次失败停止并保存进度。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
ROOT_CACHE = WORKSPACE / "stock_data" / "01-root"
MINUTE_DIR = ROOT_CACHE / "minute_cache"
CHECKPOINT_PATH = MINUTE_DIR / "minute_ingest_checkpoint.json"
RESEARCH_STOCKS_PATH = MINUTE_DIR / "minute_research_stocks.json"
HOLDOUT_STOCKS_PATH = MINUTE_DIR / "minute_holdout_stocks.json"

# --holdout 模式(第六道闸/holdout复现专用): 独立输出目录, 绝不写入研究样本目录
# (MINUTE_DIR)。checkpoint 也是独立文件, 与 --sample 的 checkpoint 完全隔离,
# 避免任何窗口完成状态的混淆。
HOLDOUT_MINUTE_DIR = ROOT_CACHE / "minute_cache_holdout"
HOLDOUT_CHECKPOINT_PATH = HOLDOUT_MINUTE_DIR / "minute_ingest_holdout_checkpoint.json"

SLEEP_BASE = 0.15
BACKOFF_SCHEDULE = (1, 2, 4)
MAX_CONSECUTIVE_FAILURES = 10
PROGRESS_EVERY = 50

SAMPLE_SEED = 20260702
SAMPLE_SIZE = 800
HOLDOUT_SIZE = 800

DEFAULT_START = "20220101"
LIMITUP_PCT_THRESHOLD = 9.5
LIMITUP_WINDOW_DAYS = 5  # +/- N trading days

# stk_mins 单次调用大约只返回 ~8000 行(约166个交易日), 必须分页拉取。
# 每窗约6个月(183天), 一个 2022-01-01→今 的区间大约需要 7-8 个窗口/股。
SAMPLE_WINDOW_DAYS = 183


def get_pro():
    sys.path.insert(0, str(WORKSPACE / "skills" / "stock-analyzer"))
    from pipeline import init_tushare
    return init_tushare()


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    # structure: {"<mode>": {"<ts_code>__<window_start>_<window_end>": true, ...}}
    data.setdefault("sample", {})
    data.setdefault("limitup", {})
    data.setdefault("smoke", {})
    # 2026-07-03 分页修复前遗留的旧格式 key 是 "<ts_code>" -> "<range_key>"(单次整段调用的产物,
    # 已证实截断到最近~166个交易日); 与新的按窗口分页 key("<ts_code>__<win_start>_<win_end>")不兼容,
    # 留着只会变成死数据, 这里清掉以免误判"已完成"。
    data["sample"] = {k: v for k, v in data["sample"].items() if "__" in k}
    return data


def save_checkpoint(checkpoint: dict) -> None:
    MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(CHECKPOINT_PATH)


def safe_stock_file(ts_code: str, target_dir: Path = MINUTE_DIR) -> Path:
    return target_dir / f"minute_5min_{ts_code.replace('.', '_')}.parquet"


def append_minute_rows(ts_code: str, df: pd.DataFrame, target_dir: Path = MINUTE_DIR) -> int:
    """写入/合并单只股票的5min数据, 去重 by ts_code+trade_time, 返回写入后总行数。
    target_dir 默认研究样本目录, --holdout 模式调用时传 HOLDOUT_MINUTE_DIR, 绝不混写。"""
    path = safe_stock_file(ts_code, target_dir)
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df
    combined = combined.drop_duplicates(subset=["ts_code", "trade_time"], keep="last")
    combined = combined.sort_values(["trade_time"]).reset_index(drop=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, index=False)
    tmp.replace(path)
    return len(combined)


def fetch_mins_with_retry(pro, ts_code: str, start_dt: str, end_dt: str, freq: str = "5min"):
    """调用 stk_mins, 异常按 1/2/4s 退避重试最多3次。返回 (df_or_None, ok:bool)。"""
    last_exc = None
    for attempt in range(len(BACKOFF_SCHEDULE) + 1):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq=freq, start_date=start_dt, end_date=end_dt)
            return df, True
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < len(BACKOFF_SCHEDULE):
                time.sleep(BACKOFF_SCHEDULE[attempt])
            else:
                print(f"    [FAIL] stk_mins {ts_code} {start_dt}..{end_dt}: {type(e).__name__} {str(e)[:80]}")
    return None, False


# ---------------------------------------------------------------------------
# sample mode
# ---------------------------------------------------------------------------

def build_or_load_sample_universe(pro) -> tuple[list[str], list[str]]:
    """从全A股票代码全集里, 用固定种子抽800只研究样本 + 另留800只holdout样本。
    若已有落盘的样本文件, 直接复用(保证跨次运行一致)。
    """
    if RESEARCH_STOCKS_PATH.exists() and HOLDOUT_STOCKS_PATH.exists():
        research = json.loads(RESEARCH_STOCKS_PATH.read_text(encoding="utf-8"))["ts_codes"]
        holdout = json.loads(HOLDOUT_STOCKS_PATH.read_text(encoding="utf-8"))["ts_codes"]
        print(f"复用已存在的样本文件: research={len(research)} holdout={len(holdout)}")
        return research, holdout

    sb = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,list_date")
    all_codes = sorted(sb["ts_code"].dropna().unique().tolist())
    print(f"全A股票代码全集: {len(all_codes)} 只")

    rng = random.Random(SAMPLE_SEED)
    shuffled = all_codes[:]
    rng.shuffle(shuffled)

    research = sorted(shuffled[:SAMPLE_SIZE])
    holdout = sorted(shuffled[SAMPLE_SIZE:SAMPLE_SIZE + HOLDOUT_SIZE])

    MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    RESEARCH_STOCKS_PATH.write_text(
        json.dumps(
            {
                "purpose": "分钟数据研究样本(可用于首轮建模/因子挖掘)",
                "seed": SAMPLE_SEED,
                "size": len(research),
                "ts_codes": research,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    HOLDOUT_STOCKS_PATH.write_text(
        json.dumps(
            {
                "purpose": "独立复现样本(holdout) —— 首轮研究/建模/因子挖掘禁止触碰,仅用于最终样本外复现验证。"
                "任何在 research 样本上定型的策略/因子/参数, 必须在此样本上做最终一次性检验, 严禁用它调参或反复试错。",
                "seed": SAMPLE_SEED,
                "size": len(holdout),
                "ts_codes": holdout,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"新建样本文件: research={len(research)} -> {RESEARCH_STOCKS_PATH}")
    print(f"新建样本文件: holdout={len(holdout)} -> {HOLDOUT_STOCKS_PATH}")
    return research, holdout


def build_date_windows(start: str, end: str, window_days: int) -> list[tuple[str, str]]:
    """把 [start, end] (YYYYMMDD) 切成若干个约 window_days 天的窗口, 前闭后闭, 相邻窗口不重叠。"""
    from datetime import datetime, timedelta

    start_d = datetime.strptime(start, "%Y%m%d")
    end_d = datetime.strptime(end, "%Y%m%d")
    windows = []
    cur = start_d
    while cur <= end_d:
        win_end = min(cur + timedelta(days=window_days - 1), end_d)
        windows.append((cur.strftime("%Y%m%d"), win_end.strftime("%Y%m%d")))
        cur = win_end + timedelta(days=1)
    return windows


def existing_coverage(ts_code: str, target_dir: Path = MINUTE_DIR) -> tuple[str, str] | None:
    """返回该股票已有 parquet 里 trade_time 的 (min_date, max_date), 形如 YYYYMMDD; 无文件返回 None。"""
    path = safe_stock_file(ts_code, target_dir)
    if not path.exists():
        return None
    try:
        existing = pd.read_parquet(path, columns=["trade_time"])
    except Exception:
        return None
    if len(existing) == 0:
        return None
    tt = pd.to_datetime(existing["trade_time"], errors="coerce").dropna()
    if len(tt) == 0:
        return None
    return tt.min().strftime("%Y%m%d"), tt.max().strftime("%Y%m%d")


def run_sample_mode(pro, checkpoint: dict, start: str, end: str, limit: int | None) -> dict:
    research, _holdout = build_or_load_sample_universe(pro)
    codes = research if limit is None else research[:limit]

    windows = build_date_windows(start, end, SAMPLE_WINDOW_DAYS)
    done = checkpoint["sample"]

    print(f"\n=== [sample] 共 {len(codes)} 只股票  区间 {start}..{end}  切分为 {len(windows)} 个窗口 ===")

    consecutive_failures = 0
    total_rows = 0
    stocks_touched = 0
    stop = False

    for stock_idx, ts_code in enumerate(codes, start=1):
        coverage = existing_coverage(ts_code)
        stock_new_rows = 0
        stock_had_call = False

        for win_start, win_end in windows:
            key = f"{ts_code}__{win_start}_{win_end}"
            if done.get(key):
                continue
            # 若该股票已有数据完整覆盖此窗口(min<=win_start 且 max>=win_end), 直接标记完成并跳过, 不发请求。
            if coverage is not None and coverage[0] <= win_start and coverage[1] >= win_end:
                done[key] = True
                continue

            start_dt = f"{win_start[:4]}-{win_start[4:6]}-{win_start[6:8]} 09:00:00"
            end_dt = f"{win_end[:4]}-{win_end[4:6]}-{win_end[6:8]} 15:30:00"

            df, ok = fetch_mins_with_retry(pro, ts_code, start_dt, end_dt)
            stock_had_call = True
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    save_checkpoint(checkpoint)
                    print(f"\n[STOP] 连续失败 {consecutive_failures} 次, 已保存进度并停止。")
                    stop = True
                    break
                time.sleep(SLEEP_BASE)
                continue

            consecutive_failures = 0
            if df is not None and len(df) > 0:
                append_minute_rows(ts_code, df)
                total_rows += len(df)
                stock_new_rows += len(df)

            done[key] = True
            time.sleep(SLEEP_BASE)

        if stop:
            break

        stocks_touched += 1
        if stock_had_call:
            save_checkpoint(checkpoint)
        if stock_idx % PROGRESS_EVERY == 0:
            save_checkpoint(checkpoint)
            print(
                f"  [sample] 进度 {stock_idx}/{len(codes)}  最新={ts_code}  "
                f"本轮累计新增行数={total_rows}"
            )

    save_checkpoint(checkpoint)
    print(f"[sample] 完成/中止: 处理股票 {stocks_touched}/{len(codes)}, 本轮新增行数 {total_rows}")
    return checkpoint


# ---------------------------------------------------------------------------
# holdout mode (第六道闸/holdout复现专用)
# ---------------------------------------------------------------------------
# 独立的 checkpoint 落盘/加载(与 --sample 的 checkpoint 完全隔离的文件),
# 复用同一套"按日期窗口分页 + 已覆盖区间探测跳过 + 连续失败退避停止"的机制,
# 唯一区别是: 目标股票池=holdout 800只, 输出目录=HOLDOUT_MINUTE_DIR,
# 绝不落入研究样本目录 MINUTE_DIR。

def load_holdout_checkpoint() -> dict:
    if HOLDOUT_CHECKPOINT_PATH.exists():
        try:
            data = json.loads(HOLDOUT_CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("holdout", {})
    return data


def save_holdout_checkpoint(checkpoint: dict) -> None:
    HOLDOUT_MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HOLDOUT_CHECKPOINT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(HOLDOUT_CHECKPOINT_PATH)


def run_holdout_mode(pro, start: str, end: str, limit: int | None) -> dict:
    if not HOLDOUT_STOCKS_PATH.exists():
        raise SystemExit(f"[FATAL] holdout 股票清单不存在: {HOLDOUT_STOCKS_PATH}(须先跑过 --sample 生成)")
    holdout = json.loads(HOLDOUT_STOCKS_PATH.read_text(encoding="utf-8"))["ts_codes"]
    codes = holdout if limit is None else holdout[:limit]

    HOLDOUT_MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint = load_holdout_checkpoint()
    done = checkpoint["holdout"]

    windows = build_date_windows(start, end, SAMPLE_WINDOW_DAYS)
    print(f"\n=== [holdout] 共 {len(codes)} 只股票(holdout独立样本)  区间 {start}..{end}  "
          f"切分为 {len(windows)} 个窗口  输出目录={HOLDOUT_MINUTE_DIR} ===")

    consecutive_failures = 0
    total_rows = 0
    stocks_touched = 0
    stop = False

    for stock_idx, ts_code in enumerate(codes, start=1):
        coverage = existing_coverage(ts_code, HOLDOUT_MINUTE_DIR)
        stock_had_call = False

        for win_start, win_end in windows:
            key = f"{ts_code}__{win_start}_{win_end}"
            if done.get(key):
                continue
            if coverage is not None and coverage[0] <= win_start and coverage[1] >= win_end:
                done[key] = True
                continue

            start_dt = f"{win_start[:4]}-{win_start[4:6]}-{win_start[6:8]} 09:00:00"
            end_dt = f"{win_end[:4]}-{win_end[4:6]}-{win_end[6:8]} 15:30:00"

            df, ok = fetch_mins_with_retry(pro, ts_code, start_dt, end_dt)
            stock_had_call = True
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    save_holdout_checkpoint(checkpoint)
                    print(f"\n[STOP] 连续失败 {consecutive_failures} 次, 已保存进度并停止。")
                    stop = True
                    break
                time.sleep(SLEEP_BASE)
                continue

            consecutive_failures = 0
            if df is not None and len(df) > 0:
                append_minute_rows(ts_code, df, HOLDOUT_MINUTE_DIR)
                total_rows += len(df)

            done[key] = True
            time.sleep(SLEEP_BASE)

        if stop:
            break

        stocks_touched += 1
        if stock_had_call:
            save_holdout_checkpoint(checkpoint)
        if stock_idx % PROGRESS_EVERY == 0:
            save_holdout_checkpoint(checkpoint)
            print(
                f"  [holdout] 进度 {stock_idx}/{len(codes)}  最新={ts_code}  "
                f"本轮累计新增行数={total_rows}"
            )

    save_holdout_checkpoint(checkpoint)
    print(f"[holdout] 完成/中止: 处理股票 {stocks_touched}/{len(codes)}, 本轮新增行数 {total_rows}")
    return checkpoint


# ---------------------------------------------------------------------------
# limitup-events mode
# ---------------------------------------------------------------------------

def find_limitup_events() -> pd.DataFrame:
    """扫描 01-root/*.parquet 日线缓存, 找 pct_chg>=9.5 的涨停事件。
    返回 DataFrame[ts_code, trade_date]。
    """
    files = sorted(ROOT_CACHE.glob("*.parquet"))
    events = []
    print(f"扫描日线缓存 {len(files)} 个文件寻找涨停事件(pct_chg>={LIMITUP_PCT_THRESHOLD})...")
    for i, f in enumerate(files):
        try:
            df = pd.read_parquet(f, columns=["ts_code", "trade_date", "pct_chg"])
        except Exception:
            continue
        hit = df[df["pct_chg"] >= LIMITUP_PCT_THRESHOLD]
        if len(hit):
            events.append(hit[["ts_code", "trade_date"]])
        if (i + 1) % 1000 == 0:
            print(f"  已扫描 {i + 1}/{len(files)} 文件, 累计事件 {sum(len(e) for e in events)}")
    if not events:
        return pd.DataFrame(columns=["ts_code", "trade_date"])
    out = pd.concat(events, ignore_index=True).drop_duplicates()
    print(f"共找到涨停事件 {len(out)} 条, 涉及股票 {out['ts_code'].nunique()} 只")
    return out


def trading_day_window(pro, trade_date: str, n: int) -> tuple[str, str]:
    """给定 trade_date, 返回前后各 n 个交易日的日期范围 (start, end)。"""
    # pull a generous calendar window around the date, then slice by is_open
    from datetime import datetime, timedelta

    dt = datetime.strptime(trade_date, "%Y%m%d")
    cal_start = (dt - timedelta(days=30)).strftime("%Y%m%d")
    cal_end = (dt + timedelta(days=30)).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="SSE", start_date=cal_start, end_date=cal_end)
    open_days = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())
    if trade_date not in open_days:
        # fall back to nearest
        open_days_all = open_days + [trade_date]
        open_days_all.sort()
        idx = open_days_all.index(trade_date)
    else:
        idx = open_days.index(trade_date)
    lo = max(0, idx - n)
    hi = min(len(open_days) - 1, idx + n)
    return open_days[lo], open_days[hi]


def run_limitup_mode(pro, checkpoint: dict, limit_events: int | None) -> dict:
    events = find_limitup_events()
    if limit_events is not None:
        events = events.head(limit_events)

    done = checkpoint["limitup"]
    consecutive_failures = 0
    total_rows = 0
    processed = 0
    pending_rows = events.to_dict("records")
    print(f"\n=== [limitup-events] 待处理 {len(pending_rows)} 条涨停事件 ===")

    for row in pending_rows:
        ts_code = row["ts_code"]
        trade_date = row["trade_date"]
        key = f"{ts_code}__{trade_date}"
        if done.get(key):
            continue

        win_start, win_end = trading_day_window(pro, trade_date, LIMITUP_WINDOW_DAYS)
        start_dt = f"{win_start[:4]}-{win_start[4:6]}-{win_start[6:8]} 09:00:00"
        end_dt = f"{win_end[:4]}-{win_end[4:6]}-{win_end[6:8]} 15:30:00"

        df, ok = fetch_mins_with_retry(pro, ts_code, start_dt, end_dt)
        if not ok:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                save_checkpoint(checkpoint)
                print(f"\n[STOP] 连续失败 {consecutive_failures} 次, 已保存进度并停止。")
                return checkpoint
            time.sleep(SLEEP_BASE)
            continue

        consecutive_failures = 0
        if df is not None and len(df) > 0:
            append_minute_rows(ts_code, df)
            total_rows += len(df)

        done[key] = True
        processed += 1
        if processed % PROGRESS_EVERY == 0:
            save_checkpoint(checkpoint)
            print(f"  [limitup-events] 进度 {processed}/{len(pending_rows)}  最新={key}  本轮累计新增行数={total_rows}")

        time.sleep(SLEEP_BASE)

    save_checkpoint(checkpoint)
    print(f"[limitup-events] 完成: 新增事件 {processed}, 本轮新增行数 {total_rows}")
    return checkpoint


# ---------------------------------------------------------------------------
# smoke mode
# ---------------------------------------------------------------------------

def run_smoke(pro) -> None:
    sb = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,list_date")
    codes = sorted(sb["ts_code"].dropna().unique().tolist())[:3]
    print(f"[smoke] 冒烟测试股票: {codes}")

    start_dt = "2026-06-01 09:00:00"
    end_dt = "2026-06-30 15:30:00"

    for ts_code in codes:
        df, ok = fetch_mins_with_retry(pro, ts_code, start_dt, end_dt)
        if ok and df is not None:
            print(f"[smoke] {ts_code}: {len(df)} 行, 列={df.columns.tolist()}")
            if len(df):
                print(df.head(2).to_string())
        else:
            print(f"[smoke] {ts_code}: 拉取失败")
        time.sleep(SLEEP_BASE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="模式A: 800只研究样本 + 800只holdout, 拉5min 2022至今")
    ap.add_argument("--holdout", action="store_true",
                     help="模式D(第六道闸专用): 只拉 minute_holdout_stocks.json 的800只holdout样本, "
                          "写入独立目录 stock_data/01-root/minute_cache_holdout/, 绝不触碰研究样本目录/文件")
    ap.add_argument("--limitup-events", action="store_true", help="模式B: 涨停事件±5交易日窗口5min")
    ap.add_argument("--smoke", action="store_true", help="冒烟测试: 只拉3只股票x1个月")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default="20260702")
    ap.add_argument("--limit", type=int, default=None, help="限制处理的股票/事件数量(用于测试)")
    args = ap.parse_args()

    pro = get_pro()

    if args.smoke:
        run_smoke(pro)
        return 0

    if args.holdout:
        run_holdout_mode(pro, args.start, args.end, args.limit)
        # --holdout 可与其他模式同时指定么？为避免任何混淆, 单独跑, 跑完直接返回。
        return 0

    MINUTE_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint = load_checkpoint()

    if args.sample:
        checkpoint = run_sample_mode(pro, checkpoint, args.start, args.end, args.limit)

    if args.limitup_events:
        checkpoint = run_limitup_mode(pro, checkpoint, args.limit)

    if not args.sample and not args.limitup_events and not args.smoke:
        print("请指定 --sample 或 --holdout 或 --limitup-events 或 --smoke")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
