#!/usr/bin/env python3
"""Phase B 数据接入:业绩预告(PEAD)历史拉取+缓存(2026-06-29)。

时点干净:stock_yjyg_em 带【公告日期】→可严格按公告日对齐前向收益,杜绝前视(本季头号风险)。
只取【归母净利润】预告,信号=净利润同比变动幅度(业绩变动幅度;缺则由 预测数值 vs 上年同期值 推)。
方向先验:同比增速越高越好(PEAD:正向盈余意外后价格继续上漂)。

输出:stock_data/03-working/fundamental_cache/pead_preannounce.parquet
列:ts_code, ann_date, end_date(报告期), growth_pct, forecast_value, prev_value, yg_type
用法:python3 ingest_pead_preannounce.py [--start 2021]
"""
from __future__ import annotations

import argparse
import re
import signal
import time
from pathlib import Path

import pandas as pd

WORKSPACE = Path(__file__).resolve().parents[3]
OUT = WORKSPACE / "stock_data" / "03-working" / "fundamental_cache" / "pead_preannounce.parquet"

# ---- PIT 双写 (2026-07-11 Wave1 B-5, additive) --------------------------------
# 主输出 OUT 与其读路径一字不改; 在主输出成功后追加写 append-only PIT 双时间店
# (factor_factory/pit/pit_store.py, spike2 采纳 + R-2 收紧)。PIT 写失败只 WARN,
# 绝不影响主输出与退出码(异常全隔离)。切读(消费端改读 PIT)= 双写稳定一周后单独 commit。
PIT_STORE = (WORKSPACE / "stock_data" / "03-working" / "fundamental_cache"
             / "pit" / "pit_yjyg.parquet")
PIT_MODULE = WORKSPACE / "factor_factory" / "pit" / "pit_store.py"
PIT_FIELD_EXACT = "归属于上市公司股东的净利润"  # 精确匹配, 不吸扣非(spike2 §3 发现3)


def _load_pit_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ff_pit_store", PIT_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pit_rows_from_yjyg(df: pd.DataFrame, per: str, tz: str) -> pd.DataFrame:
    """akshare stock_yjyg_em 单期输出 → PIT 行(保留全部公告, 不做 keep='first')。"""
    need = ["股票代码", "公告日期", "预测指标", "预测数值", "上年同期值", "业绩变动幅度"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"yjyg 关键列缺失: {missing} (期 {per})")
    for col, thr in (("公告日期", 0.99), ("预测指标", 0.99)):  # akshare_audit.md §2.11 阈值
        rate = df[col].notna().mean()
        if rate < thr:
            raise ValueError(f"yjyg 关键列 {col} 非空率 {rate:.1%} < {thr:.0%} (期 {per})")
    d = df[df["预测指标"].astype(str) == PIT_FIELD_EXACT].copy()
    if len(d) == 0:
        return pd.DataFrame()
    def _code6(x):
        m = re.search(r"(\d{6})", str(x))
        return m.group(1) if m else None
    d["symbol"] = d["股票代码"].map(_code6)
    d["announce_date"] = pd.to_datetime(d["公告日期"], errors="coerce").dt.strftime("%Y%m%d")
    fv = pd.to_numeric(d["预测数值"], errors="coerce")
    pv = pd.to_numeric(d["上年同期值"], errors="coerce")
    gp = pd.to_numeric(d["业绩变动幅度"], errors="coerce")
    derived = (fv - pv) / pv.abs() * 100.0  # 与主输出同口径: 负基数分母取绝对值
    d["np_forecast"] = fv
    d["np_growth_pct"] = gp.where(gp.notna(), derived)
    d = d.dropna(subset=["symbol", "announce_date"])
    rows = []
    for fname in ("np_forecast", "np_growth_pct"):
        r = d[["symbol", "announce_date", fname]].rename(columns={fname: "value"}).copy()
        r["field"] = fname
        rows.append(r.dropna(subset=["value"]))
    out = pd.concat(rows, ignore_index=True)
    out["period"] = int(per)
    out["announce_date"] = out["announce_date"].astype("int64")
    out["value"] = out["value"].astype("float64")
    # 双时间: NOTICE_DATE 源头精度=日 → tz-aware 当日 00:00 + precision='date' (R-2.2 禁降精度)
    out["available_at"] = pd.to_datetime(
        out["announce_date"].astype(str), format="%Y%m%d").dt.tz_localize(tz)
    out["available_at_precision"] = "date"
    out["is_preliminary"] = True  # 业绩预告 = 预告口径
    out["source"] = "akshare.stock_yjyg_em"
    return out


def _pit_dual_write(raw_frames: list[tuple[str, pd.DataFrame]]) -> None:
    """PIT 追加写(生产 tolerant 口径: 污染行隔离 sidecar, 不阻塞整批)。调用方负责异常隔离。"""
    pit = _load_pit_module()
    frames = []
    for per, df in raw_frames:
        t = _pit_rows_from_yjyg(df, per, pit.TZ)
        if len(t):
            frames.append(t)
    if not frames:
        print("  [PIT双写] 无可写行, 跳过")
        return
    rows = pd.concat(frames, ignore_index=True)
    stats = pit.append_rows_tolerant(PIT_STORE, rows)
    n_q = (stats["quarantined_early"] + stats["quarantined_drag"]
           + stats["quarantined_conflict"] + stats["quarantined_out_of_order"])
    print(f"  [PIT双写] {PIT_STORE.name}: appended={stats['appended']} "
          f"skipped={stats['skipped_duplicates']} total={stats['total']} quarantined={n_q}")
    if n_q:
        print(f"  WARN [PIT双写] {n_q} 行被隔离待人工裁决 (early={stats['quarantined_early']} "
              f"drag={stats['quarantined_drag']} conflict={stats['quarantined_conflict']} "
              f"ooo={stats['quarantined_out_of_order']}) → {stats['quarantine_path']}")
# ---- PIT 双写段结束 ------------------------------------------------------------


class _TO(Exception):
    pass


def _alarm(s, f):
    raise _TO()


def periods(start_year: int) -> list[str]:
    out = []
    for y in range(start_year, 2027):
        for mmdd in ("0331", "0630", "0930", "1231"):
            out.append(f"{y}{mmdd}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2021)
    args = ap.parse_args()
    import akshare as ak
    signal.signal(signal.SIGALRM, _alarm)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    pit_raw: list[tuple[str, pd.DataFrame]] = []  # PIT 双写用原始载荷(additive, 主路径不读)
    for per in periods(args.start):
        signal.alarm(45)
        try:
            df = ak.stock_yjyg_em(date=per)
            signal.alarm(0)
        except _TO:
            print(f"  {per}: 超时跳过"); continue
        except Exception as e:
            signal.alarm(0)
            print(f"  {per}: {type(e).__name__} {str(e)[:60]}"); continue
        if df is None or len(df) == 0:
            print(f"  {per}: 空"); continue
        pit_raw.append((per, df))  # PIT 双写留档(不影响下方主路径)
        # 只取归母净利润预告
        d = df[df["预测指标"].astype(str).str.contains("净利润", na=False)].copy()
        if len(d) == 0:
            print(f"  {per}: 无净利润预告"); continue
        def code6(x):
            m = re.search(r"(\d{6})", str(x))
            return m.group(1) if m else None
        d["code6"] = d["股票代码"].map(code6)
        d["ann_date"] = pd.to_datetime(d["公告日期"], errors="coerce").dt.strftime("%Y%m%d")
        fv = pd.to_numeric(d.get("预测数值"), errors="coerce")
        pv = pd.to_numeric(d.get("上年同期值"), errors="coerce")
        gp = pd.to_numeric(d.get("业绩变动幅度"), errors="coerce")
        # 业绩变动幅度缺失则由 预测数值 vs 上年同期值 推同比%(分母取绝对值,防负基数符号翻转)
        derived = (fv - pv) / pv.abs() * 100.0
        d["growth_pct"] = gp.where(gp.notna(), derived)
        d["end_date"] = per
        d["forecast_value"] = fv
        d["prev_value"] = pv
        d["yg_type"] = d.get("预告类型")
        keep = d.dropna(subset=["code6", "ann_date"])[
            ["code6", "ann_date", "end_date", "growth_pct", "forecast_value", "prev_value", "yg_type"]]
        keep = keep.rename(columns={"code6": "ts_code"})
        frames.append(keep)
        print(f"  {per}: {len(keep)} 条净利润预告  非空growth {keep['growth_pct'].notna().mean():.0%}")
        time.sleep(0.3)
    if not frames:
        print("无数据"); return 1
    allp = pd.concat(frames, ignore_index=True)
    # 同股同报告期多次预告:保留最早一次公告(首次预告,最贴近 PEAD 事件)
    allp = allp.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(["ts_code", "end_date"], keep="first")
    allp.to_parquet(OUT, index=False)
    print(f"\n✅ 写出 {OUT}  共 {len(allp)} 条  {allp['ann_date'].min()}..{allp['ann_date'].max()}  唯一股 {allp['ts_code'].nunique()}")
    # ---- PIT 双写(additive): 主输出已落盘, 此处任何失败只 WARN, 不影响退出码 ----
    try:
        _pit_dual_write(pit_raw)
    except BaseException as e:  # noqa: BLE001 — 双写异常全隔离, 主输出不受扰
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        print(f"  WARN [PIT双写] 失败(主输出不受影响): {type(e).__name__}: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
