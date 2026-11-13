#!/usr/bin/env python3
"""Spike-2 七条验收逐条实测 (PHASE2_TIERA_REPORT.md §Spike-2 验收条件 1-7)。

真实数据 = data/pit_yjyg.parquet (akshare stock_yjyg_em 真实拉取, 2024Q1-2026Q2)。
毒样本/负向测试 = 合成注入 (spec 强制; 且源头 surface 为 latest-only, 无自然修订链)。

用法: /Users/fisher/.openclaw/venv/bin/python test_acceptance.py
输出: 每条 A1..A7 打印 PASS/FAIL + 证据; 任一 FAIL 则退出码 1。
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR))
from pit_store import (TZ, SCHEMA, FuturePeriodReference, PITViolation,  # noqa: E402
                       append_rows, load_store, pit_asof, validate_store)

STORE = SPIKE_DIR / "data" / "pit_yjyg.parquet"
TMP = SPIKE_DIR / "data" / "tmp"
RESULTS: list[tuple[str, str, str]] = []  # (id, PASS/FAIL, evidence)


def record(aid: str, ok: bool, evidence: str) -> None:
    RESULTS.append((aid, "PASS" if ok else "FAIL", evidence))
    print(f"[{aid}] {'PASS' if ok else 'FAIL'} — {evidence}")


def expect_raise(fn, exc, tag: str) -> tuple[bool, str]:
    try:
        fn()
    except exc as e:
        return True, f"{tag}: 如期 raise {type(e).__name__}: {str(e)[:90]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{tag}: 异常类型不符 {type(e).__name__}: {str(e)[:90]}"
    return False, f"{tag}: 未 raise (静默通过=守卫失效)"


def shift_day(yyyymmdd: int, days: int) -> int:
    """真日历日偏移(整数直加会产出 20250431 类非法日期)。"""
    return int((pd.Timestamp(str(yyyymmdd)) + pd.Timedelta(days=days)).strftime("%Y%m%d"))


def mk_rows(symbol, field, period, pairs, *, prelim=True, source="synthetic"):
    """pairs = [(announce_yyyymmdd, value), ...] → 待 append 的行框。"""
    ann = [p[0] for p in pairs]
    return pd.DataFrame({
        "symbol": symbol, "field": field, "period": np.int32(period),
        "value": np.float64([p[1] for p in pairs]),
        "announce_date": pd.array(ann, dtype="int32"),
        "available_at": pd.to_datetime([str(a) for a in ann], format="%Y%m%d").tz_localize(TZ),
        "available_at_precision": "date",
        "is_preliminary": (prelim if isinstance(prelim, list) else [prelim] * len(pairs)),
        "source": source,
    })


# ------------------------------------------------------------------ A1 前视零记录
def a1(df: pd.DataFrame) -> None:
    rng = np.random.default_rng(20260711)
    fixed = [20240201, 20240430, 20240715, 20241031, 20250131,
             20250430, 20250715, 20251031, 20260131, 20260430, 20260710]
    days = pd.date_range("2024-02-01", "2026-07-10", freq="D")
    rand = [int(d.strftime("%Y%m%d")) for d in days[rng.choice(len(days), 20, replace=False)]]
    total_rows, replays, viol = 0, 0, 0
    con = sqlite3.connect(":memory:")
    for as_of in fixed + rand:
        for kw in ({"mode": "as_latest"}, {"mode": "as_first_reported"},
                   {"mode": "as_latest", "lag": 1}, {"mode": "as_latest", "period": 20250331}):
            res = pit_asof(df, as_of, **kw)
            replays += 1
            total_rows += len(res)
            viol += int((res["announce_date"].astype("int64") > as_of).sum())  # pandas 断言
            if len(res):  # SQL 断言(同一结果集独立复核)
                res[["symbol", "announce_date"]].astype({"announce_date": "int64"}).to_sql(
                    "r", con, index=False, if_exists="replace")
                viol += con.execute(
                    "SELECT COUNT(*) FROM r WHERE announce_date > ?", (as_of,)).fetchone()[0]
    con.close()
    record("A1", viol == 0,
           f"{len(fixed) + len(rand)} 个 as_of × 4 查询形态 = {replays} 次重放, "
           f"共 {total_rows} 行返回, announce_date>as_of 记录数 = {viol} (pandas+SQL 双断言)")


# ------------------------------------------------------------------ A2 毒样本
def a2(df: pd.DataFrame) -> None:
    as_of = 20250430
    nxt = shift_day(as_of, 1)  # as_of+1 自然日
    poison_val = 9.99e18
    p1 = mk_rows("000002", "np_forecast", 20250331, [(nxt, poison_val)])   # 当期+毒公告日
    p2 = mk_rows("000002", "np_forecast", 20260630, [(nxt, poison_val)])   # 未来期+毒公告日
    poisoned = pd.concat([df, _as_store(p1, seq=99), _as_store(p2, seq=99)], ignore_index=True)
    bad = 0
    for kw in ({"mode": "as_latest"}, {"mode": "as_first_reported"},
               {"mode": "as_latest", "period": 20250331}, {"mode": "as_latest", "lag": 1}):
        res = pit_asof(poisoned, as_of, **kw)
        bad += int((res["value"] == poison_val).sum())
        bad += int((res["announce_date"].astype("int64") > as_of).sum())
        # 毒样本不得改变期选择: 20260630 期不可出现
        bad += int((res["period"].astype("int64") == 20260630).sum())
    record("A2", bad == 0,
           f"注入 announce_date=as_of+1 毒样本 2 条(当期+未来期), 4 查询形态下命中 {bad} 次 (须=0)")


def _as_store(rows: pd.DataFrame, seq: int) -> pd.DataFrame:
    r = rows.copy()
    r["revision_seq"] = np.int16(seq)
    r["ingested_at"] = pd.Timestamp.now(tz="UTC")
    return r[list(SCHEMA)]


# ------------------------------------------------------------------ A3 修订一致性
def a3(df: pd.DataFrame) -> None:
    oks, evs = [], []
    # (a) 真实全表校验通过
    try:
        validate_store(df)
        oks.append(True); evs.append(f"真实表 {len(df)} 行 validate_store 通过")
    except PITViolation as e:
        oks.append(False); evs.append(f"真实表校验失败: {e}")
    # (b) revision_seq 与 announce_date 排序不一致 → 必 raise
    g = pd.concat([
        _as_store(mk_rows("TEST03", "np_forecast", 20241231, [(20250110, 1.0)]), seq=1),
        _as_store(mk_rows("TEST03", "np_forecast", 20241231, [(20250315, 2.0)]), seq=0),
    ], ignore_index=True)
    ok, ev = expect_raise(lambda: validate_store(pd.concat([df, g], ignore_index=True)),
                          PITViolation, "seq与公告日反序")
    oks.append(ok); evs.append(ev)
    # (c) 重复 (symbol,field,period,announce_date) → 必 raise
    d = pd.concat([
        _as_store(mk_rows("TEST03", "np_forecast", 20241231, [(20250110, 1.0)]), seq=0),
        _as_store(mk_rows("TEST03", "np_forecast", 20241231, [(20250110, 2.0)]), seq=1),
    ], ignore_index=True)
    ok, ev = expect_raise(lambda: validate_store(pd.concat([df, d], ignore_index=True)),
                          PITViolation, "重复(period,announce_date)键")
    oks.append(ok); evs.append(ev)
    # (d) append 层: 同键异值 hard fail (历史行只读)
    tmp = TMP / "a3_store.parquet"
    shutil.copyfile(STORE, tmp)
    row = df[df["revision_seq"] == 0].iloc[[0]]
    conflict = mk_rows(row.iloc[0]["symbol"], row.iloc[0]["field"], int(row.iloc[0]["period"]),
                       [(int(row.iloc[0]["announce_date"]), float(row.iloc[0]["value"]) + 123.0)])
    ok, ev = expect_raise(lambda: append_rows(tmp, conflict), PITViolation, "append同键异值")
    oks.append(ok); evs.append(ev)
    # (e) append 层: 乱序回填(新行公告日早于组内存量) hard fail
    grp = df.groupby(["symbol", "field", "period"])["announce_date"].max()
    sym, fld, per = grp.index[0]
    early = mk_rows(sym, fld, int(per), [(shift_day(int(grp.iloc[0]), -10), 42.0)])
    ok, ev = expect_raise(lambda: append_rows(tmp, early), PITViolation, "append乱序回填")
    oks.append(ok); evs.append(ev)
    record("A3", all(oks), " | ".join(evs))


# ------------------------------------------------------------------ A4 未来期禁引
def a4(df: pd.DataFrame) -> None:
    oks, evs = [], []
    for lag in (-1, -5):
        ok, ev = expect_raise(lambda lag=lag: pit_asof(df, 20250430, lag=lag),
                              FuturePeriodReference, f"lag={lag}")
        oks.append(ok); evs.append(ev)
    ok, ev = expect_raise(lambda: pit_asof(df, 20250430, period=20250331, lag=1),
                          ValueError, "period+lag同用")
    oks.append(ok); evs.append(ev)
    # 正向对照: lag=0/1 正常工作且 lag=1 的期严格早于 lag=0
    r0 = pit_asof(df, 20250430, lag=0)
    r1 = pit_asof(df, 20250430, lag=1)
    both = r0.merge(r1, on=["symbol", "field"], suffixes=("_0", "_1"))
    mono = bool((both["period_1"].astype("int64") < both["period_0"].astype("int64")).all())
    oks.append(mono and len(both) > 0)
    evs.append(f"对照: lag=0 {len(r0)}行 / lag=1 {len(r1)}行, lag1期严格早于lag0期={mono}")
    record("A4", all(oks), " | ".join(evs))


# ------------------------------------------------------------------ A5 双口径
def a5(df: pd.DataFrame) -> None:
    oks, evs = [], []
    tmp = TMP / "a5_store.parquet"
    if tmp.exists():
        tmp.unlink()
    # 修订链: 预告(20260125) → 预告修正(20260310) → 正式财报(20260428)
    append_rows(tmp, mk_rows("TEST05", "np_forecast", 20251231, [(20260125, 100e6)], prelim=True))
    append_rows(tmp, mk_rows("TEST05", "np_forecast", 20251231, [(20260310, 95e6)], prelim=True))
    append_rows(tmp, mk_rows("TEST05", "np_forecast", 20251231, [(20260428, 98e6)], prelim=False,
                             source="synthetic_official"))
    st = load_store(tmp)
    # (a) 两行并存不覆写: 3 行全在, revision_seq 0/1/2
    chain = st[(st["symbol"] == "TEST05")].sort_values("revision_seq")
    ok = list(chain["revision_seq"]) == [0, 1, 2] and list(chain["value"]) == [100e6, 95e6, 98e6]
    oks.append(ok); evs.append(f"预告→修正→正式 3 行并存 seq={list(chain['revision_seq'])} (不覆写)")
    # (b) as_latest 取正式
    r = pit_asof(st, 20260601, period=20251231, mode="as_latest")
    ok = len(r) == 1 and r.iloc[0]["value"] == 98e6 and not r.iloc[0]["is_preliminary"]
    oks.append(ok); evs.append(f"as_latest@20260601 → value={r.iloc[0]['value']:.0f}, "
                               f"is_preliminary={bool(r.iloc[0]['is_preliminary'])} (期望 98e6/False)")
    # (c) as_first_reported 取首发预告且带标记
    r = pit_asof(st, 20260601, period=20251231, mode="as_first_reported")
    ok = len(r) == 1 and r.iloc[0]["value"] == 100e6 and bool(r.iloc[0]["is_preliminary"]) \
        and r.iloc[0]["revision_seq"] == 0
    oks.append(ok); evs.append(f"as_first_reported@20260601 → value={r.iloc[0]['value']:.0f}, "
                               f"is_preliminary={bool(r.iloc[0]['is_preliminary'])} (期望 100e6/True)")
    # (d) 观察日介于预告与正式之间: 两口径同取预告(此时预告=已知全部)
    r = pit_asof(st, 20260201, period=20251231, mode="as_latest")
    ok = len(r) == 1 and r.iloc[0]["value"] == 100e6 and bool(r.iloc[0]["is_preliminary"])
    oks.append(ok); evs.append("as_latest@20260201(正式未出) → 取预告 100e6 带 is_preliminary=True")
    # (e) 仅有晚于参考日申报的期 → as_first_reported 置缺失(不复现 OpenBB 更晚兜底)
    r = pit_asof(st, 20260110, period=20251231, mode="as_first_reported")
    ok = len(r) == 0
    oks.append(ok); evs.append(f"as_first_reported@20260110(首发在 0125) → {len(r)} 行 (期望 0=缺失, 无更晚兜底)")
    # (f) 真实表口径: as_first_reported 返回全部带 is_preliminary=True (yjyg 全为预告)
    rr = pit_asof(df, 20250430, mode="as_first_reported")
    ok = bool(rr["is_preliminary"].all()) and bool((rr["revision_seq"] == 0).all())
    oks.append(ok); evs.append(f"真实表 as_first_reported@20250430: {len(rr)} 行全带 "
                               f"is_preliminary=True 且 seq=0 = {ok}")
    record("A5", all(oks), " | ".join(evs))


# ------------------------------------------------------------------ A6 拖带守卫
def a6(df: pd.DataFrame) -> None:
    oks, evs = [], []
    tmp = TMP / "a6_store.parquet"
    if tmp.exists():
        tmp.unlink()
    per = 20241231
    pend = pd.Timestamp("2024-12-31")
    d500 = int((pend + pd.Timedelta(days=500)).strftime("%Y%m%d"))
    d450 = int((pend + pd.Timedelta(days=450)).strftime("%Y%m%d"))
    d451 = int((pend + pd.Timedelta(days=451)).strftime("%Y%m%d"))
    # 期末后 500 天重述值 → 拒 (openbb §3.1 负向测试原文)
    ok, ev = expect_raise(lambda: append_rows(tmp, mk_rows("TEST06", "np_forecast", per,
                                                           [(d500, 88e6)])),
                          PITViolation, f"期末后500天({d500})重述")
    oks.append(ok); evs.append(ev)
    # 边界: 恰 450 天 → 收; 451 天 → 拒
    stats = append_rows(tmp, mk_rows("TEST06", "np_forecast", per, [(d450, 87e6)]))
    oks.append(stats["appended"] == 1); evs.append(f"恰450天({d450}) append={stats['appended']} (期望1=放行)")
    ok, ev = expect_raise(lambda: append_rows(tmp, mk_rows("TEST06B", "np_forecast", per,
                                                           [(d451, 86e6)])),
                          PITViolation, f"451天({d451})")
    oks.append(ok); evs.append(ev)
    # 真实表全表滞后分布佐证守卫在存量上同样成立
    lagd = (pd.to_datetime(df["announce_date"].astype(str), format="%Y%m%d")
            - pd.to_datetime(df["period"].astype(str), format="%Y%m%d")).dt.days
    oks.append(bool((lagd <= 450).all()))
    evs.append(f"真实表滞后分布: min={int(lagd.min())} max={int(lagd.max())} 天 (须全<=450)")
    record("A6", all(oks), " | ".join(evs))


# ------------------------------------------------------------------ A7 append 幂等
def a7(df: pd.DataFrame) -> None:
    oks, evs = [], []
    # (a) 对生产 spike 表重放同一批数据: appended 必须=0 且文件字节不变
    h0 = hashlib.sha256(STORE.read_bytes()).hexdigest()
    replay = df.drop(columns=["revision_seq", "ingested_at"])
    stats = append_rows(STORE, replay)
    h1 = hashlib.sha256(STORE.read_bytes()).hexdigest()
    ok = stats["appended"] == 0 and stats["skipped_duplicates"] == len(df) and h0 == h1
    oks.append(ok)
    evs.append(f"全表 {len(df)} 行重放: appended={stats['appended']} "
               f"skipped={stats['skipped_duplicates']} 文件hash不变={h0 == h1}")
    # (b) 新修订 append 得新 revision_seq, 且再重放该批新增=0
    tmp = TMP / "a7_store.parquet"
    shutil.copyfile(STORE, tmp)
    grp = df.groupby(["symbol", "field", "period"]).agg(
        amax=("announce_date", "max"), smax=("revision_seq", "max"))
    # 选公告滞后距 450 天守卫尚有余量(>30天)的组, 保证 +7 天新修订不触守卫
    amax_ts = pd.to_datetime(grp["amax"].astype("int64").astype(str), format="%Y%m%d").to_numpy()
    per_ts = pd.to_datetime([str(p) for _, _, p in grp.index], format="%Y%m%d").to_numpy()
    lag = ((amax_ts - per_ts) / np.timedelta64(1, "D")).astype(int)
    grp = grp[lag < 400]
    sym, fld, per = grp.index[0]
    newrev = mk_rows(sym, fld, int(per), [(shift_day(int(grp.iloc[0]["amax"]), 7), 12345.0)])
    s1 = append_rows(tmp, newrev)
    st = load_store(tmp)
    got = st[(st["symbol"] == sym) & (st["field"] == fld)
             & (st["period"] == per)]["revision_seq"].max()
    ok = s1["appended"] == 1 and got == grp.iloc[0]["smax"] + 1
    oks.append(ok); evs.append(f"新修订 append: seq={int(got)} (期望 {int(grp.iloc[0]['smax']) + 1})")
    s2 = append_rows(tmp, newrev)
    oks.append(s2["appended"] == 0 and s2["skipped_duplicates"] == 1)
    evs.append(f"新修订二遍重放: appended={s2['appended']} (期望0)")
    validate_store(load_store(tmp))
    evs.append("append 后全表不变式校验通过")
    record("A7", all(oks), " | ".join(evs))


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    df = load_store(STORE)
    print(f"真实 PIT 表: {len(df)} 行, {df['symbol'].nunique()} 股, "
          f"期 {df['period'].min()}..{df['period'].max()}\n")
    for fn in (a1, a2, a3, a4, a5, a6, a7):
        fn(df)
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print(f"\n== 汇总: {sum(1 for _, s, _ in RESULTS if s == 'PASS')} PASS / {n_fail} FAIL ==")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
