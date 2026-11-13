#!/usr/bin/env python3
"""pit_store 生产模块单测 (Wave1 B-5) — 纯合成数据, 不触任何生产文件。

覆盖: spike2 存储层语义(幂等/revision链/双守卫/禁降精度/pit_asof 双口径/未来期禁引)
    + R-2 收紧(下界隔离 sidecar/乱序回填明文/同日键冲突明文/tolerant 隔离口径)。

用法: /Users/fisher/.openclaw/venv/bin/python test_pit_store.py   (退出码 0=全 PASS)
(venv 无 pytest, 采用与 spike2 验收同款自含断言 runner。)
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pit_store import (EARLY_GUARD_DAYS, OUT_OF_ORDER_REMEDY, SAME_DAY_REMEDY,  # noqa: E402
                       SCHEMA, TZ, FuturePeriodReference, PITViolation, append_rows,
                       append_rows_tolerant, load_store, pit_asof, quarantine_path_for,
                       validate_store)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, ev: str = "") -> None:
    RESULTS.append((name, ok, ev))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {ev}" if ev else ""))


def raises(fn, exc, contains: str | None = None) -> tuple[bool, str]:
    try:
        fn()
    except exc as e:
        if contains and contains not in str(e):
            return False, f"raise 了 {type(e).__name__} 但信息缺少明文 {contains!r}: {str(e)[:120]}"
        return True, f"如期 raise {type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        return False, f"异常类型不符 {type(e).__name__}: {str(e)[:100]}"
    return False, "未 raise (守卫失效)"


def mk(symbol, field, period, pairs, *, prelim=True, source="unit"):
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


def tmpstore(tag: str) -> Path:
    return Path(TMPDIR) / f"store_{tag}.parquet"


def t_revision_chain():
    p = tmpstore("chain")
    append_rows(p, mk("000001", "np_forecast", 20251231, [(20260125, 100e6)]))
    append_rows(p, mk("000001", "np_forecast", 20251231, [(20260310, 95e6)]))
    append_rows(p, mk("000001", "np_forecast", 20251231, [(20260428, 98e6)], prelim=False))
    st = load_store(p)
    ok = (len(st) == 3 and list(st.sort_values("revision_seq")["revision_seq"]) == [0, 1, 2]
          and list(st.sort_values("revision_seq")["value"]) == [100e6, 95e6, 98e6])
    check("revision链: 三次公告并存 seq=0/1/2 不覆写", ok, f"rows={len(st)}")
    validate_store(st)
    check("revision链: append 后全表不变式通过", True)


def t_idempotent():
    p = tmpstore("idem")
    rows = pd.concat([mk("000002", "np_forecast", 20251231, [(20260120, 1e6)]),
                      mk("000002", "np_growth_pct", 20251231, [(20260120, 30.0)])],
                     ignore_index=True)
    s1 = append_rows(p, rows)
    h0 = hashlib.sha256(p.read_bytes()).hexdigest()
    s2 = append_rows(p, rows)
    h1 = hashlib.sha256(p.read_bytes()).hexdigest()
    check("幂等: 二遍重放 appended=0 且文件字节不变",
          s1["appended"] == 2 and s2["appended"] == 0
          and s2["skipped_duplicates"] == 2 and h0 == h1,
          f"s1={s1} s2={s2}")


def t_conflict_strict():
    p = tmpstore("conflict")
    append_rows(p, mk("000003", "np_forecast", 20251231, [(20260120, 1e6)]))
    ok, ev = raises(lambda: append_rows(p, mk("000003", "np_forecast", 20251231,
                                              [(20260120, 2e6)])),
                    PITViolation, contains="人工裁决")
    check("严格口径: 同键异值 hard fail 且带 R-2.4 明文(人工裁决)", ok, ev)
    ok, ev = raises(lambda: append_rows(p, mk("000003", "np_forecast", 20251231,
                                              [(20260220, 3e6), (20260220, 4e6)])),
                    PITViolation, contains="人工裁决")
    check("严格口径: 批内同键异值 hard fail 且带 R-2.4 明文", ok, ev)


def t_out_of_order_strict():
    p = tmpstore("ooo")
    append_rows(p, mk("000004", "np_forecast", 20251231, [(20260310, 1e6)]))
    ok, ev = raises(lambda: append_rows(p, mk("000004", "np_forecast", 20251231,
                                              [(20260120, 9e5)])),
                    PITViolation, contains="整组重建")
    check("严格口径: 乱序回填 hard fail 且带 R-2.3 明文(整组重建入新表+双表对账)", ok, ev)
    ok2, ev2 = raises(lambda: append_rows(p, mk("000004", "np_forecast", 20251231,
                                                [(20260120, 9e5)])),
                      PITViolation, contains=OUT_OF_ORDER_REMEDY[:12])
    check("严格口径: 乱序明文=模块级常量 OUT_OF_ORDER_REMEDY", ok2, ev2)


def t_drag_guard():
    p = tmpstore("drag")
    d450 = int((pd.Timestamp("2024-12-31") + pd.Timedelta(days=450)).strftime("%Y%m%d"))
    d451 = int((pd.Timestamp("2024-12-31") + pd.Timedelta(days=451)).strftime("%Y%m%d"))
    s = append_rows(p, mk("000005", "np_forecast", 20241231, [(d450, 1e6)]))
    check("拖带上界: 恰 450 天放行", s["appended"] == 1)
    ok, ev = raises(lambda: append_rows(p, mk("000005B", "np_forecast", 20241231,
                                              [(d451, 1e6)])),
                    PITViolation, contains="拖带守卫")
    check("拖带上界: 451 天 hard fail", ok, ev)


def t_early_guard_quarantine():
    p = tmpstore("early")
    b400 = int((pd.Timestamp("2025-12-31") - pd.Timedelta(days=EARLY_GUARD_DAYS))
               .strftime("%Y%m%d"))
    b401 = int((pd.Timestamp("2025-12-31") - pd.Timedelta(days=EARLY_GUARD_DAYS + 1))
               .strftime("%Y%m%d"))
    good = mk("000006", "np_forecast", 20251231, [(b400, 1e6)])          # 恰 -400: 放行
    bad = mk("920099", "np_forecast", 20251231, [(b401, 2e6)])           # -401: 隔离
    s = append_rows(p, pd.concat([good, bad], ignore_index=True))
    qp = quarantine_path_for(p)
    st = load_store(p)
    check("R-2.1 下界: 恰-400天放行 / -401天隔离不入主表且整批不失败",
          s["appended"] == 1 and s["quarantined_early"] == 1 and len(st) == 1
          and st.iloc[0]["symbol"] == "000006", f"stats={s}")
    q = pd.read_parquet(qp)
    check("R-2.1 下界: 隔离 sidecar 落盘且带 reason/时间戳(不静默丢)",
          qp.exists() and len(q) == 1 and q.iloc[0]["quarantine_reason"] == "early_lag_guard"
          and q.iloc[0]["symbol"] == "920099" and "quarantined_at" in q.columns,
          f"qp={qp.name}")
    # 隔离幂等: 同批重放不使隔离表膨胀
    append_rows(p, pd.concat([good, bad], ignore_index=True))
    check("R-2.1 下界: 隔离幂等(重放不膨胀)", len(pd.read_parquet(qp)) == 1)
    # validate_store 把下界作为主表不变式(独立单行组, 只触发下界检查)
    polluted = mk("TESTQ", "np_forecast", 20251231, [(b401, 3e6)])
    polluted["revision_seq"] = np.int16(0)
    polluted["ingested_at"] = pd.Timestamp.now(tz="UTC")
    ok, ev = raises(lambda: validate_store(polluted[list(SCHEMA)]),
                    PITViolation, contains="滞后下界")
    check("R-2.1 下界: validate_store 主表内出现越界行即不变式破坏", ok, ev)


def t_precision_rejects():
    p = tmpstore("prec")
    bad = mk("000007", "np_forecast", 20251231, [(20260120, 1e6)])
    bad_naive = bad.copy()
    bad_naive["available_at"] = bad_naive["available_at"].dt.tz_localize(None)
    ok, ev = raises(lambda: append_rows(p, bad_naive), PITViolation, contains="tz-aware")
    check("R-2.2 禁降精度: naive datetime 进门即拒", ok, ev)
    bad_str = bad.copy()
    bad_str["available_at"] = "2026-01-20"
    ok, ev = raises(lambda: append_rows(p, bad_str), PITViolation, contains="降精度")
    check("R-2.2 禁降精度: str 进门即拒", ok, ev)
    append_rows(p, bad)
    st = load_store(p)
    st2 = st.copy()
    st2["available_at_precision"] = "hour"
    ok, ev = raises(lambda: validate_store(st2), PITViolation, contains="非法值")
    check("R-2.2 精度列: 取值域外(hour)即不变式破坏", ok, ev)
    st3 = st.copy()
    st3["value"] = st3["value"].astype("float32")
    ok, ev = raises(lambda: validate_store(st3), PITViolation, contains="float64")
    check("value 禁 float32 落盘", ok, ev)


def t_pit_asof():
    p = tmpstore("asof")
    append_rows(p, mk("000008", "np_forecast", 20251231, [(20260125, 100e6)]))
    append_rows(p, mk("000008", "np_forecast", 20251231, [(20260428, 98e6)], prelim=False))
    append_rows(p, mk("000008", "np_forecast", 20250630, [(20250810, 50e6)]))
    st = load_store(p)
    r = pit_asof(st, 20260601, period=20251231, mode="as_latest")
    check("pit_asof: as_latest 取最新修订(正式)",
          len(r) == 1 and r.iloc[0]["value"] == 98e6 and not r.iloc[0]["is_preliminary"])
    r = pit_asof(st, 20260601, period=20251231, mode="as_first_reported")
    check("pit_asof: as_first_reported 取首发 vintage(预告)",
          len(r) == 1 and r.iloc[0]["value"] == 100e6 and bool(r.iloc[0]["is_preliminary"]))
    r = pit_asof(st, 20260110, period=20251231, mode="as_first_reported")
    check("pit_asof: 首发晚于 as_of → 置缺失, 无更晚兜底", len(r) == 0)
    r = pit_asof(st, 20260201, lag=0)
    check("pit_asof: lag=0 取 as_of 可见最新期(20251231)",
          len(r) == 1 and int(r.iloc[0]["period"]) == 20251231)
    r = pit_asof(st, 20260201, lag=1)
    check("pit_asof: lag=1 取前一期(20250630)",
          len(r) == 1 and int(r.iloc[0]["period"]) == 20250630)
    ok, ev = raises(lambda: pit_asof(st, 20260201, lag=-1), FuturePeriodReference)
    check("pit_asof: lag=-1 未来期禁引 raise", ok, ev)
    ok, ev = raises(lambda: pit_asof(st, 20260201, period=20251231, lag=1), ValueError)
    check("pit_asof: period+lag 同用 raise", ok, ev)
    # 毒样本: announce_date > as_of 必不可见
    poison = mk("000008", "np_forecast", 20251231, [(20260202, 9.9e18)])
    poison["revision_seq"] = np.int16(99)
    poison["ingested_at"] = pd.Timestamp.now(tz="UTC")
    r = pit_asof(pd.concat([st, poison[list(SCHEMA)]], ignore_index=True), 20260201)
    check("pit_asof: 毒样本(announce=as_of+1)零命中",
          len(r) == 1 and r.iloc[0]["value"] != 9.9e18
          and int((r["announce_date"].astype("int64") > 20260201).sum()) == 0)


def t_tolerant():
    p = tmpstore("tol")
    append_rows(p, mk("000009", "np_forecast", 20251231, [(20260310, 1e6)]))
    d451 = int((pd.Timestamp("2025-12-31") + pd.Timedelta(days=451)).strftime("%Y%m%d"))
    batch = pd.concat([
        mk("000009", "np_forecast", 20251231, [(20260310, 2e6)]),        # 存量键冲突 → 隔离
        mk("000009", "np_forecast", 20251231, [(20260120, 5e5)]),        # 乱序回填 → 隔离
        mk("000010", "np_forecast", 20251231, [(d451, 3e6)]),            # 拖带上界 → 隔离
        mk("000011", "np_forecast", 20251231, [(20260405, 7e6), (20260405, 8e6)]),  # 批内冲突
        mk("000012", "np_forecast", 20251231, [(20260406, 9e6)]),        # 干净行 → append
    ], ignore_index=True)
    s = append_rows_tolerant(p, batch)
    st = load_store(p)
    ok = (s["appended"] == 1 and s["skipped_duplicates"] == 0
          and s["quarantined_conflict"] == 3 and s["quarantined_out_of_order"] == 1
          and s["quarantined_drag"] == 1 and s["quarantined_early"] == 0
          and len(st) == 2 and set(st["symbol"]) == {"000009", "000012"})
    check("tolerant: 冲突/乱序/拖带整行隔离不阻塞干净行", ok, f"stats={s}")
    # 存量精确重复(同键同值) → 幂等跳过, 不隔离
    s_dup = append_rows_tolerant(p, mk("000009", "np_forecast", 20251231, [(20260310, 1e6)]))
    check("tolerant: 存量精确重复幂等跳过不隔离",
          s_dup["appended"] == 0 and s_dup["skipped_duplicates"] == 1
          and sum(s_dup[k] for k in ("quarantined_early", "quarantined_drag",
                                     "quarantined_conflict", "quarantined_out_of_order")) == 0,
          f"stats={s_dup}")
    q = pd.read_parquet(s["quarantine_path"])
    reasons = set(q["quarantine_reason"])
    check("tolerant: 隔离表按 reason 分类落盘(待人工裁决队列)",
          reasons == {"same_key_value_conflict_store", "out_of_order_backfill",
                      "drag_guard", "same_key_value_conflict_batch"},
          f"reasons={sorted(reasons)} rows={len(q)}")
    validate_store(st)
    check("tolerant: 隔离后主表不变式完好", True)
    # 二遍重放同一批: 干净行转为幂等 skip, 隔离表不膨胀
    n_q0 = len(q)
    s2 = append_rows_tolerant(p, batch)
    check("tolerant: 二遍重放 appended=0 且隔离表不膨胀",
          s2["appended"] == 0 and s2["skipped_duplicates"] == 1
          and len(pd.read_parquet(s2["quarantine_path"])) == n_q0,
          f"s2_appended={s2['appended']}")


def t_schema_guard():
    p = tmpstore("schema")
    bad = mk("000013", "np_forecast", 20251231, [(20260120, 1e6)]).drop(columns=["source"])
    ok, ev = raises(lambda: append_rows(p, bad), PITViolation, contains="缺列")
    check("schema: 缺列进门即拒", ok, ev)
    ok, ev = raises(lambda: validate_store(pd.DataFrame({"symbol": []})), PITViolation)
    check("schema: validate_store 缺列即拒", ok, ev)
    assert "人工裁决" in SAME_DAY_REMEDY  # 明文常量存在性
    check("R-2.4 明文常量 SAME_DAY_REMEDY 含人工裁决处置", True)


def main() -> int:
    global TMPDIR
    with tempfile.TemporaryDirectory() as td:
        TMPDIR = td
        for t in (t_revision_chain, t_idempotent, t_conflict_strict, t_out_of_order_strict,
                  t_drag_guard, t_early_guard_quarantine, t_precision_rejects, t_pit_asof,
                  t_tolerant, t_schema_guard):
            t()
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n== 单测汇总: {sum(1 for _, ok, _ in RESULTS if ok)} PASS / {n_fail} FAIL ==")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
