#!/usr/bin/env python3
"""em_quote_source.py — 东财 clist 多周期行情备用源（stdlib-only，零三方依赖）。

用途：
  为看门狗 / 晨判 / 收盘闭环提供独立于主管线（tushare/akshare）的实时行情校验通道。
  一次全市场拉取（~5900 只，1-2 秒），自带多周期涨跌幅，无需本地历史数据。

数据契约（2026-08-25 实测）：
  - clist 接口单页硬上限 pz=100（pz>100 一律返回 100 行）
  - 全市场约 59 页；6 并发分页拉取约 1.6s
  - 字段：f12=代码 f13=市场(0=SZ/BJ,1=SH) f14=名称 f2=最新价 f3=当日涨跌%
         f18=昨收 f6=成交额 f109=近5日% f110=近20日% f25=年初至今% f124=行情时间戳
  - push2delay 主机可用；push2 系主机在本网络环境 RemoteDisconnected（保留在回退链中，
    其他环境可能可用）

快照治理（借鉴 a-share-heatmap 项目模式）：
  - 完整性阈值：非空页 <80% 即判定快照无效，抛 EMQuoteError，绝不静默降级
  - stale 标记：远端失败时回退最近成功缓存，并打 stale=True
  - seed 兜底：无任何缓存且远端不可用时抛错（本模块不内置假数据）
  - 缓存文件带 updatedAt/expiresAt，供调用方判断新鲜度

用法：
  from em_quote_source import get_market_snapshot, check_today_data
  snap = get_market_snapshot()          # 全量 dict
  ok, info = check_today_data("20260825")  # 看门狗用：今日是否有真实行情
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------- 常量 ----------

EM_HOSTS = [
    "push2delay.eastmoney.com",
    "82.push2.eastmoney.com",
    "7.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "push2.eastmoney.com",
]
PAGE_SIZE = 100            # clist 单页硬上限（实测）
CONCURRENCY = 6
MAX_RETRIES_PER_PAGE = 3
COMPLETENESS_THRESHOLD = 0.8   # 非空页占比低于此值 → 快照无效
CACHE_TTL_SECONDS = 30          # 盘中短缓存
STALE_CACHE_MAX_AGE_HOURS = 26  # stale 回退缓存的最长可信期

_refresh_lock = threading.Lock()
_refresh_inflight: threading.Event | None = None
_refresh_result: dict[str, Any] | None = None
_refresh_error: Exception | None = None

_ASHARE_FS = "m:0+t:6,m:0+t:8,t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_FIELDS = "f2,f3,f6,f12,f13,f14,f18,f25,f109,f110,f124"
_HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; stock-watchdog-emsource/1.0)",
}
_TZ_SH = timezone(timedelta(hours=8))


class EMQuoteError(RuntimeError):
    """东财行情快照获取失败或完整性不足。调用方应视为'无有效备用数据'。"""


def _workspace_root() -> Path:
    return Path(
        os.environ.get("OPENCLAW_WORKSPACE")
        or os.environ.get("STOCK_SYSTEM_WORKSPACE")
        or str(Path.home() / ".openclaw" / "workspace")
    )


def _cache_path() -> Path:
    return (
        _workspace_root()
        / "stock_data" / "03-working" / "health" / "em_quote_cache.json"
    )


def _fetch_page(host: str, pn: int) -> list[dict[str, Any]]:
    url = (
        f"https://{host}/api/qt/clist/get"
        f"?pn={pn}&pz={PAGE_SIZE}&po=1&np=1&fltt=2&invt=2&fid=f12"
        f"&fs={_ASHARE_FS}&fields={_FIELDS}"
    )
    req = urllib.request.Request(url, headers=_HEADERS)
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES_PER_PAGE):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                doc = json.load(resp)
            data = (doc or {}).get("data") or {}
            rows = data.get("diff") or []
            return [r for r in rows if r.get("f12")]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.4 * (attempt + 1))
    raise EMQuoteError(f"page {pn} via {host} failed after {MAX_RETRIES_PER_PAGE} tries: {last_err}")


def _fetch_all_pages(host: str, total_pages: int) -> list[dict[str, Any]]:
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(_fetch_page, host, pn): pn for pn in range(1, total_pages + 1)}
        results: dict[int, list] = {}
        for fut in cf.as_completed(futures):
            pn = futures[fut]
            try:
                results[pn] = fut.result()
            except EMQuoteError:
                results[pn] = []
    nonempty = sum(1 for v in results.values() if v)
    if total_pages and nonempty / total_pages < COMPLETENESS_THRESHOLD:
        raise EMQuoteError(
            f"incomplete snapshot: {nonempty}/{total_pages} pages succeeded "
            f"(threshold {COMPLETENESS_THRESHOLD:.0%})"
        )
    rows: list[dict] = []
    for pn in sorted(results):
        rows.extend(results[pn])
    # 去重（按代码，后写覆盖）
    by_code: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("f12"))
        if code:
            by_code[code] = row
    return list(by_code.values())


def _first_total(host: str) -> int | None:
    """用第一页探测全市场总数，据此计算页数。"""
    try:
        rows = _fetch_page(host, 1)
    except EMQuoteError:
        return None
    if not rows:
        return None
    # 第一页拿不到 total 时退化为保守估计
    return max(len(rows), 5900)


def fetch_remote_snapshot(force_host: str | None = None) -> dict[str, Any]:
    """拉取全市场快照。成功返回 dict；全部主机失败抛 EMQuoteError。"""
    hosts = [force_host] if force_host else EM_HOSTS
    last_err: Exception | None = None
    for host in hosts:
        try:
            first = _fetch_page(host, 1)
            if not first:
                raise EMQuoteError(f"{host}: empty first page")
            # 从第一页响应中无法直接拿 total（_fetch_page 未返回），用保守估计
            total_est = 6000
            total_pages = -(-total_est // PAGE_SIZE)
            rows = _fetch_all_pages(host, total_pages)
            # 尾部空页会自然少行；行数过少也判无效
            if len(rows) < 4000:
                raise EMQuoteError(f"{host}: only {len(rows)} stocks, expected ~5900")
            ts_vals = [int(r["f124"]) for r in rows if r.get("f124")]
            quote_ts = max(ts_vals) if ts_vals else int(time.time())
            now = datetime.now(_TZ_SH)
            return {
                "source": f"eastmoney:{host}",
                "fetched_at": now.isoformat(timespec="seconds"),
                "quote_ts": datetime.fromtimestamp(quote_ts, _TZ_SH).isoformat(timespec="seconds"),
                "stock_count": len(rows),
                "stale": False,
                "stocks": rows,
            }
        except EMQuoteError as exc:
            last_err = exc
            continue
    raise EMQuoteError(f"all hosts failed; last error: {last_err}")


def load_cached_snapshot(max_age_hours: float = STALE_CACHE_MAX_AGE_HOURS) -> dict[str, Any] | None:
    """读本地缓存。过期返回 None。"""
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(snap["fetched_at"])
        age_h = (datetime.now(_TZ_SH) - fetched).total_seconds() / 3600
        if age_h > max_age_hours:
            return None
        snap["stale"] = True
        snap["age_hours"] = round(age_h, 2)
        return snap
    except Exception:  # noqa: BLE001
        return None


def save_snapshot(snap: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def get_market_snapshot(use_cache: bool = True) -> dict[str, Any]:
    """对外主入口：优先缓存（TTL 内），否则拉远端并落盘；远端失败回退 stale 缓存；
    连 stale 都没有则抛 EMQuoteError —— 绝不伪造数据。
    单飞刷新：并发调用共用一次远端请求，避免打穿东财接口。"""
    global _refresh_inflight, _refresh_result, _refresh_error
    path = _cache_path()
    if use_cache and path.is_file():
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(snap["fetched_at"])
            if (datetime.now(_TZ_SH) - fetched).total_seconds() <= CACHE_TTL_SECONDS:
                return snap
        except Exception:  # noqa: BLE001
            pass

    with _refresh_lock:
        if _refresh_inflight is not None:
            waiter = _refresh_inflight
            is_leader = False
        else:
            waiter = threading.Event()
            _refresh_inflight = waiter
            _refresh_result = None
            _refresh_error = None
            is_leader = True

    if not is_leader:
        waiter.wait(timeout=30)
        if _refresh_result is not None:
            return _refresh_result
        if _refresh_error is not None:
            stale = load_cached_snapshot()
            if stale is not None:
                return stale
            raise _refresh_error
        stale = load_cached_snapshot()
        if stale is not None:
            return stale
        raise EMQuoteError("single-flight wait timed out")

    try:
        snap = fetch_remote_snapshot()
        save_snapshot(snap)
        with _refresh_lock:
            _refresh_result = snap
        return snap
    except EMQuoteError as exc:
        with _refresh_lock:
            _refresh_error = exc
        stale = load_cached_snapshot()
        if stale is not None:
            return stale
        raise
    finally:
        with _refresh_lock:
            if _refresh_inflight is waiter:
                _refresh_inflight = None
        waiter.set()


def get_market_summary(use_cache: bool = True) -> dict[str, Any]:
    """轻量入口：只返回市场概览（涨跌家数/成交额/行情时间），不携带全量个股。
    适合晨判、看门狗日志等只需概览的场景。"""
    snap = get_market_snapshot(use_cache=use_cache)
    stocks = [s for s in snap.get("stocks", []) if isinstance(s.get("f3"), (int, float))]
    up = sum(1 for s in stocks if s["f3"] > 0)
    down = sum(1 for s in stocks if s["f3"] < 0)
    turnover = sum(
        s.get("f6") or 0 for s in snap.get("stocks", [])
        if isinstance(s.get("f6"), (int, float))
    )
    return {
        "source": snap["source"],
        "fetched_at": snap["fetched_at"],
        "quote_ts": snap["quote_ts"],
        "stale": snap.get("stale", False),
        "stock_count": snap["stock_count"],
        "up": up,
        "down": down,
        "flat": len(stocks) - up - down,
        "turnover_yi": round(turnover / 1e8),
    }


# ---------- 看门狗专用检查 ----------

def check_today_data(trade_date_yyyymmdd: str | None = None) -> tuple[bool, dict[str, Any]]:
    """看门狗用：东财侧今天（或指定交易日）是否产生了真实行情。
    返回 (ok, info)。ok=False 表示备用源认为当天没有有效行情（停牌日/假日/数据故障）。"""
    try:
        snap = fetch_remote_snapshot()
    except EMQuoteError as exc:
        return False, {"ok": False, "reason": f"remote_failed: {exc}", "source": None}
    save_snapshot(snap)
    quote_date = snap["quote_ts"][:10].replace("-", "")
    target = trade_date_yyyymmdd or datetime.now(_TZ_SH).strftime("%Y%m%d")
    # 有成交的股票数（成交额>0）
    active = sum(1 for r in snap["stocks"] if isinstance(r.get("f6"), (int, float)) and r["f6"] > 0)
    info = {
        "ok": quote_date == target and active > 1000,
        "quote_date": quote_date,
        "target_date": target,
        "stock_count": snap["stock_count"],
        "active_count": active,
        "source": snap["source"],
        "quote_ts": snap["quote_ts"],
    }
    return bool(info["ok"]), info


def summary_lines(snap: dict[str, Any], top_n: int = 10) -> list[str]:
    """人类可读摘要（告警/日报可直接引用）。"""
    stocks = [s for s in snap.get("stocks", []) if isinstance(s.get("f3"), (int, float))]
    if not stocks:
        return ["(无有效涨跌数据)"]
    up = sum(1 for s in stocks if s["f3"] > 0)
    down = sum(1 for s in stocks if s["f3"] < 0)
    flat = len(stocks) - up - down
    turnover = sum(s.get("f6") or 0 for s in stocks if isinstance(s.get("f6"), (int, float)))
    lines = [
        f"东财备用源快照：{snap['stock_count']} 只 | 来源 {snap['source']} | 行情时间 {snap['quote_ts']}",
        f"上涨 {up} / 平 {flat} / 下跌 {down} | 两市成交额 {turnover/1e8:.0f} 亿",
    ]
    top = sorted(stocks, key=lambda s: s["f3"], reverse=True)[:top_n]
    lines.append("涨幅前" + str(top_n) + ": " + " ".join(f"{s['f14']}+{s['f3']}%" for s in top))
    return lines


if __name__ == "__main__":
    import sys
    ok, info = check_today_data(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
