#!/usr/bin/env python3
"""Spike-2 · PIT 双时间存储 + pit_asof 单入口 API (2026-07-11)。

规格来源(冻结引证):
- qlib.md §1.2 字段表 + pit_asof(df, as_of, period, lag) 签名 + 5 验收条件
  (借 qlib PIT 数据模型: 公告日=可见性唯一依据; 不借 _next 链表指针,
   parquet 列式下 (period, announce_date, revision_seq) 排序即等价修订链)
- openbb.md §3.1 增量: available_at 带时区 datetime 禁降精度 / as_first_reported
  无更早申报即置缺失(不复现 OpenBB 更晚兜底) / 450 天拖带守卫 /
  预告不覆写正式(两行并存, revision 链相连, is_preliminary 标记)
- akshare_audit.md §2.11 yjyg 契约(28 列位置契约断言在 ingest 层)

表 schema (全列必填):
  symbol                 str      标的(6位代码)
  field                  str      指标名(如 np_forecast, np_growth_pct)
  period                 int32    报告期末日 yyyymmdd (事件时间)
  value                  float64  值(禁 float32 落盘)
  announce_date          int32    公告日 yyyymmdd (观察时间, **可见性唯一依据**)
  available_at           datetime64[ns, Asia/Shanghai]  披露时刻(禁降精度为 date)
  available_at_precision str      {'date','minute','second'} 源头精度显式记录
  is_preliminary         bool     预告=True, 正式财报=False
  revision_seq           int16    同 (symbol,field,period) 内按 announce_date 升序, 0=首发
  source                 str      数据源标记
  ingested_at            datetime64[ns, UTC]  入库时间(审计用, 不参与可见性)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "Asia/Shanghai"
DRAG_GUARD_DAYS = 450  # openbb.md §3.1 机制二: 期末后 450 天以上的申报剔除(拖带守卫)

SCHEMA = {
    "symbol": "str",
    "field": "str",
    "period": "int32",
    "value": "float64",
    "announce_date": "int32",
    "available_at": f"datetime64[ns, {TZ}]",
    "available_at_precision": "str",
    "is_preliminary": "bool",
    "revision_seq": "int16",
    "source": "str",
    "ingested_at": "datetime64[ns, UTC]",
}
KEY = ["symbol", "field", "period", "announce_date"]
GROUP = ["symbol", "field", "period"]
_PRECISIONS = {"date", "minute", "second"}


class PITViolation(Exception):
    """存储层不变式被破坏(修订链/重复键/拖带/精度)。"""


class FuturePeriodReference(ValueError):
    """未来期禁引(qlib pit.py#L32-36 语义: Ref($$x, -1) 类引用直接 raise)。"""


# ---------------------------------------------------------------- helpers

def _check_yyyymmdd(v, name: str) -> None:
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是 yyyymmdd 整数, 得到 {v!r}")
    if not (19900101 <= iv <= 20991231):
        raise ValueError(f"{name}={iv} 不在合理 yyyymmdd 范围")
    pd.to_datetime(str(iv), format="%Y%m%d")  # 非法日期直接抛


def _int_dates_to_ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.astype("int64").astype(str), format="%Y%m%d")


# ---------------------------------------------------------------- validation

def validate_store(df: pd.DataFrame, *, drag_guard_days: int = DRAG_GUARD_DAYS) -> None:
    """全表不变式校验; 任一破坏即 PITViolation (hard fail, 不静默修复)。"""
    missing = [c for c in SCHEMA if c not in df.columns]
    if missing:
        raise PITViolation(f"缺列: {missing}")
    if len(df) == 0:
        return

    # -- available_at 禁降精度 (openbb.md §3.1 反面①: SEC accepted_date 被 .date() 化的反向执行)
    av = df["available_at"]
    if not pd.api.types.is_datetime64_any_dtype(av):
        raise PITViolation(f"available_at 必须是 datetime64, 得到 {av.dtype} (禁 date/str 降精度)")
    if getattr(av.dtype, "tz", None) is None:
        raise PITViolation("available_at 必须带时区(tz-aware); naive datetime 视为降精度, 拒收")
    if av.isna().any():
        raise PITViolation("available_at 存在 NaT")
    bad_prec = set(df["available_at_precision"].unique()) - _PRECISIONS
    if bad_prec:
        raise PITViolation(f"available_at_precision 非法值: {bad_prec}")

    # -- value 精度 (qlib float32 反模式的反向执行)
    if df["value"].dtype != np.float64:
        raise PITViolation(f"value 必须 float64 落盘, 得到 {df['value'].dtype}")

    # -- 日期合法性 + 双时间一致性 (announce_date 与 available_at 同日)
    for col in ("period", "announce_date"):
        try:
            _int_dates_to_ts(df[col])
        except Exception as e:
            raise PITViolation(f"{col} 含非法 yyyymmdd: {e}")
    av_day = av.dt.tz_convert(TZ).dt.strftime("%Y%m%d").astype("int64")
    mism = (av_day != df["announce_date"].astype("int64"))
    if mism.any():
        raise PITViolation(
            f"announce_date 与 available_at 日不一致 {int(mism.sum())} 行, "
            f"样例: {df.loc[mism, KEY].head(3).to_dict('records')}")

    # -- 重复 (symbol,field,period,announce_date) hard fail (qlib.md §1.2 验收3后半)
    dup = df.duplicated(KEY, keep=False)
    if dup.any():
        raise PITViolation(
            f"重复 (symbol,field,period,announce_date) 键 {int(dup.sum())} 行, "
            f"样例: {df.loc[dup, KEY].head(3).to_dict('records')}")

    # -- 修订一致性: 组内 revision_seq 排序与 announce_date 排序严格一致且 0..n-1 连续
    g = df.sort_values(GROUP + ["revision_seq"], kind="mergesort")
    grp = g.groupby(GROUP, sort=False)
    seq_ok = grp["revision_seq"].apply(
        lambda s: bool((s.to_numpy() == np.arange(len(s))).all()))
    if not seq_ok.all():
        bad = seq_ok[~seq_ok].index[:3].tolist()
        raise PITViolation(f"revision_seq 非 0..n-1 连续, 组样例: {bad}")
    ann_ok = grp["announce_date"].apply(
        lambda s: bool((np.diff(s.to_numpy()) > 0).all()) if len(s) > 1 else True)
    if not ann_ok.all():
        bad = ann_ok[~ann_ok].index[:3].tolist()
        raise PITViolation(
            f"revision_seq 升序下 announce_date 非严格递增(修订链与公告日排序不一致), 组样例: {bad}")

    # -- 450 天拖带守卫 (openbb.md §3.1 机制二)
    lag_days = (_int_dates_to_ts(df["announce_date"]) - _int_dates_to_ts(df["period"])).dt.days
    drag = lag_days > drag_guard_days
    if drag.any():
        raise PITViolation(
            f"拖带守卫: {int(drag.sum())} 行公告晚于期末 {drag_guard_days} 天以上, "
            f"样例: {df.loc[drag, KEY].head(3).to_dict('records')} "
            f"(最大滞后 {int(lag_days.max())} 天)")


# ---------------------------------------------------------------- append

def _normalize_incoming(new_df: pd.DataFrame) -> pd.DataFrame:
    """入库前规范化(不含 revision_seq——由 append_rows 分配)。"""
    need = [c for c in SCHEMA if c not in ("revision_seq", "ingested_at")]
    missing = [c for c in need if c not in new_df.columns]
    if missing:
        raise PITViolation(f"入库数据缺列: {missing}")
    d = new_df[need].copy()
    d["symbol"] = d["symbol"].astype(str)
    d["field"] = d["field"].astype(str)
    d["period"] = d["period"].astype("int32")
    d["value"] = d["value"].astype("float64")
    d["announce_date"] = d["announce_date"].astype("int32")
    d["is_preliminary"] = d["is_preliminary"].astype(bool)
    d["source"] = d["source"].astype(str)
    # available_at 禁降精度: 进门就查, 不等 validate_store
    if not pd.api.types.is_datetime64_any_dtype(d["available_at"]):
        raise PITViolation(
            f"available_at 必须是 datetime64 (得到 {d['available_at'].dtype}); "
            "date/str 属降精度, 拒收 — 源头只有日期时须显式构造 tz-aware 时刻并标 precision='date'")
    if getattr(d["available_at"].dtype, "tz", None) is None:
        raise PITViolation("available_at 必须 tz-aware; naive datetime 拒收")
    d["available_at"] = d["available_at"].dt.tz_convert(TZ)
    d["ingested_at"] = pd.Timestamp.now(tz="UTC")
    return d


def load_store(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in SCHEMA.items()})
    return pd.read_parquet(path)


def append_rows(path: str | Path, new_df: pd.DataFrame, *,
                drag_guard_days: int = DRAG_GUARD_DAYS,
                validate: bool = True) -> dict:
    """增量 append: 幂等(重复行跳过)、同键异值 hard fail、乱序回填 hard fail、
    revision_seq 只增不改、历史行只读(qlib.md §1.2 验收5 + 红线1)。
    返回 {'appended': n, 'skipped_duplicates': m, 'total': N}。
    """
    path = Path(path)
    inc = _normalize_incoming(new_df)

    # 拖带守卫在入口执行(openbb §3.1: 500 天重述值必须在这里被拒)
    lag_days = (_int_dates_to_ts(inc["announce_date"]) - _int_dates_to_ts(inc["period"])).dt.days
    drag = lag_days > drag_guard_days
    if drag.any():
        raise PITViolation(
            f"拖带守卫拒收: {int(drag.sum())} 行公告晚于期末 {drag_guard_days} 天以上 "
            f"(最大 {int(lag_days[drag].max())} 天), "
            f"样例: {inc.loc[drag, KEY].head(3).to_dict('records')}")

    # 批内去重: 完全相同行(键+值+标记)只留一条; 同键异值 hard fail
    inc = inc.drop_duplicates(KEY + ["value", "is_preliminary"], keep="first")
    bdup = inc.duplicated(KEY, keep=False)
    if bdup.any():
        raise PITViolation(
            f"批内同 (symbol,field,period,announce_date) 键异值 {int(bdup.sum())} 行, hard fail; "
            f"样例: {inc.loc[bdup, KEY + ['value']].head(4).to_dict('records')}")

    old = load_store(path)
    if len(old):
        # 与存量对账: 同键同值→幂等跳过; 同键异值→hard fail
        m = inc.merge(old[KEY + ["value", "is_preliminary"]], on=KEY, how="left",
                      suffixes=("", "_old"), indicator=True)
        hit = m["_merge"] == "both"
        same = hit & np.isclose(m["value"], m["value_old"], rtol=0, atol=0, equal_nan=True) \
                   & (m["is_preliminary"] == m["is_preliminary_old"])
        conflict = hit & ~same
        if conflict.any():
            raise PITViolation(
                f"同 (symbol,field,period,announce_date) 键值冲突 {int(conflict.sum())} 行 "
                f"(历史行只读, 不允许覆写), 样例: "
                f"{m.loc[conflict, KEY + ['value', 'value_old']].head(3).to_dict('records')}")
        skipped = int(same.sum())
        inc = inc.loc[~hit.to_numpy()].copy()
    else:
        skipped = 0

    if len(inc) == 0:
        return {"appended": 0, "skipped_duplicates": skipped, "total": len(old)}

    # 乱序回填守卫: 新公告日必须晚于该组存量最大公告日(否则修订链需重建, 违反历史只读)
    inc = inc.sort_values(GROUP + ["announce_date"], kind="mergesort").reset_index(drop=True)
    if len(old):
        gmax = old.groupby(GROUP)["announce_date"].max().rename("ann_max").reset_index()
        chk = inc.merge(gmax, on=GROUP, how="left")
        late = chk["ann_max"].notna() & (chk["announce_date"] <= chk["ann_max"])
        if late.any():
            raise PITViolation(
                f"乱序回填拒收: {int(late.sum())} 行公告日 <= 组内存量最大公告日 "
                f"(append-only 修订链不可插入历史), 样例: "
                f"{chk.loc[late, KEY].head(3).to_dict('records')}")
        # 分配 revision_seq: 组内按 announce_date 升序, 起点 = 存量 max + 1
        gseq = old.groupby(GROUP)["revision_seq"].max().rename("seq_base").reset_index()
        inc = inc.merge(gseq, on=GROUP, how="left")
        base = inc["seq_base"].fillna(-1).astype("int64")
        inc = inc.drop(columns=["seq_base"])
    else:
        base = pd.Series(-1, index=inc.index, dtype="int64")
    rank = inc.groupby(GROUP, sort=False).cumcount()
    inc["revision_seq"] = (base + 1 + rank).astype("int16")

    out = pd.concat([old, inc[list(SCHEMA)]], ignore_index=True) if len(old) else inc[list(SCHEMA)]
    if validate:
        validate_store(out, drag_guard_days=drag_guard_days)

    # 原子写(tmp + rename), 不半写
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".parquet.tmp")
    os.close(fd)
    try:
        out.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"appended": int(len(inc)), "skipped_duplicates": skipped, "total": int(len(out))}


# ---------------------------------------------------------------- pit_asof

def pit_asof(df: pd.DataFrame, as_of: int, *,
             period: int | None = None,
             lag: int = 0,
             mode: str = "as_latest") -> pd.DataFrame:
    """单一 PIT 查询入口(剥夺各脚本自行 join 的权力)。

    语义(qlib.md §1.2 冻结): 给定观察日 as_of, 只可见 announce_date <= as_of 的记录。
    - period=None: 每 (symbol, field) 取 as_of 可见的最新报告期(P 语义); lag=k 取其前 k 期。
    - period=yyyymmdd: 钉死单一报告期(PRef 语义); 不可与 lag!=0 同用。
    - mode='as_latest': 组内取可见集中 revision_seq 最大(最新修订) — 研究对照口径。
    - mode='as_first_reported': 组内取可见集中 revision_seq 最小(原始 vintage) —
      回测唯一合法口径; **无不晚于 as_of 的申报时置缺失, 绝不取更晚申报兜底**
      (openbb.md §3.1: 收紧于 OpenBB 的 fallback 行为)。
    - lag < 0: 未来期引用, 直接 raise (qlib pit.py#L32-36 语义)。
    """
    _check_yyyymmdd(as_of, "as_of")
    if not isinstance(lag, (int, np.integer)) or isinstance(lag, bool):
        raise ValueError(f"lag 必须是整数, 得到 {lag!r}")
    if lag < 0:
        raise FuturePeriodReference(
            f"lag={lag}: 引用未来报告期被禁止 (等价 qlib Ref($$x, -{abs(lag)}), pit.py#L32-36)")
    if period is not None and lag != 0:
        raise ValueError("period(PRef 钉死单期) 与 lag!=0(相对偏移) 不可同时使用")
    if mode not in ("as_latest", "as_first_reported"):
        raise ValueError(f"mode 必须是 as_latest|as_first_reported, 得到 {mode!r}")
    if period is not None:
        _check_yyyymmdd(period, "period")

    empty = df.iloc[0:0]
    vis = df[df["announce_date"].astype("int64") <= int(as_of)]
    if len(vis) == 0:
        return empty

    if period is not None:
        vis = vis[vis["period"].astype("int64") == int(period)]
        # 该期在 as_of 前无任何申报 → 置缺失(空框), 不向更晚申报兜底
        if len(vis) == 0:
            return empty
    else:
        # 每 (symbol, field) 可见期按 period 降序 dense rank; 目标 = 第 lag+1 新的期
        uniq = vis[["symbol", "field", "period"]].drop_duplicates()
        rk = uniq.groupby(["symbol", "field"])["period"].rank(
            method="dense", ascending=False).astype(int)
        target = uniq[rk == lag + 1]
        if len(target) == 0:
            return empty
        vis = vis.merge(target, on=["symbol", "field", "period"], how="inner")

    picker = "idxmax" if mode == "as_latest" else "idxmin"
    idx = getattr(vis.groupby(GROUP, sort=False)["revision_seq"], picker)()
    out = vis.loc[idx.to_numpy()].reset_index(drop=True)

    # 出口自检(红线2机械化): 返回集内不允许存在 announce_date > as_of
    leak = (out["announce_date"].astype("int64") > int(as_of)).sum()
    if leak:
        raise PITViolation(f"pit_asof 出口自检失败: {leak} 行 announce_date > as_of")
    return out
