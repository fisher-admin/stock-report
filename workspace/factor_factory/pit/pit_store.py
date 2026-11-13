#!/usr/bin/env python3
"""PIT 双时间存储 + pit_asof 单入口 API — 生产模块 (Wave1 B-5, 2026-07-11)。

来源: spike2 (research/2026-07-github-stock-systems/spikes/spike2_pit/pit_store.py,
7/7 验收 PASS) 直接采纳 + PHASE3_SPIKES_REPORT.md §R-2 四条增量收紧:

  R-2.1 公告滞后**下界** sanity 守卫: announce_date >= period - EARLY_GUARD_DAYS(默认400天,
        可配置)。越界 = 显式告警(logger.warning) + 隔离到 sidecar 隔离表
        (<store>.quarantine.parquet, 带 quarantine_reason/quarantined_at 列), **不静默丢**。
        动机: spike2 真实表实测 -481 天上游污染样本(920099 疑招股书盈利预测混入)。
  R-2.2 `available_at_precision` 列({'date','minute','second'})为**正式字段**:
        "禁降精度"不仅约束类型(datetime64 tz-aware, naive/date/str 进店即拒),
        源头精度须由该列显式诚实承载(日精度源标 'date', 不伪造分钟)。
  R-2.3 乱序回填(迟到的更早公告)明文: append-only 修订链不可插入历史, hard fail;
        **唯一合法处置 = 整组重建入新表 + 双表对账后切换, 禁止原地重排 revision_seq**
        (错误信息中明文给出该处置路径)。
  R-2.4 同日多公告 (symbol,field,period,announce_date) 重复键异值处置明文:
        源带盘中时刻 → 以 available_at 细分; 日精度源 → hard fail, **人工裁决入库**
        (错误信息中明文)。生产摄取层可用 append_rows_tolerant 将冲突行隔离待裁决。

规格冻结引证(沿 spike2 原文):
- qlib.md §1.2 字段表 + pit_asof(df, as_of, period, lag) 签名 + 5 验收条件
  (借 qlib PIT 数据模型: 公告日=可见性唯一依据; 不借 _next 链表指针,
   parquet 列式下 (period, announce_date, revision_seq) 排序即等价修订链)
- openbb.md §3.1 增量: available_at 带时区 datetime 禁降精度 / as_first_reported
  无更早申报即置缺失(不复现 OpenBB 更晚兜底) / 450 天拖带守卫 /
  预告不覆写正式(两行并存, revision 链相连, is_preliminary 标记)

表 schema (全列必填):
  symbol                 str      标的(6位代码)
  field                  str      指标名(如 np_forecast, np_growth_pct)
  period                 int32    报告期末日 yyyymmdd (事件时间)
  value                  float64  值(禁 float32 落盘)
  announce_date          int32    公告日 yyyymmdd (观察时间, **可见性唯一依据**)
  available_at           datetime64[ns, Asia/Shanghai]  披露时刻(禁降精度为 date)
  available_at_precision str      {'date','minute','second'} 源头精度显式记录 (R-2.2)
  is_preliminary         bool     预告=True, 正式财报=False
  revision_seq           int16    同 (symbol,field,period) 内按 announce_date 升序, 0=首发
  source                 str      数据源标记
  ingested_at            datetime64[ns, UTC]  入库时间(审计用, 不参与可见性)

两个入库入口:
- append_rows           严格口径(spike2 原语义 + R-2.1 下界隔离): 拖带/键冲突/乱序回填
                        一律 hard fail — 单测与验收用。
- append_rows_tolerant  生产摄取口径: 上述三类行为**整行隔离到 sidecar + 告警**而非
                        使整批失败(单一污染组不阻塞当日全量增量), 余量走严格 append。
                        主存储不变式与严格口径完全一致(隔离行永不入主表)。
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("factor_factory.pit")

TZ = "Asia/Shanghai"
DRAG_GUARD_DAYS = 450   # openbb.md §3.1 机制二: 期末后 450 天以上的申报剔除(拖带守卫, 上界)
EARLY_GUARD_DAYS = 400  # R-2.1: 公告早于期末 400 天以上 = 上游污染嫌疑(下界, 可配置)

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

OUT_OF_ORDER_REMEDY = ("唯一合法处置=整组重建入新表+双表对账后切换, "
                       "禁止原地重排 revision_seq (R-2.3)")
SAME_DAY_REMEDY = ("同日多公告处置: 源带盘中时刻以 available_at 细分; "
                   "日精度源人工裁决入库 (R-2.4)")


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


def _lag_days(df: pd.DataFrame) -> pd.Series:
    """公告滞后 = announce_date - period (日历日)。"""
    if len(df) == 0:
        return pd.Series([], dtype="int64", index=df.index)
    return (_int_dates_to_ts(df["announce_date"]) - _int_dates_to_ts(df["period"])).dt.days


def quarantine_path_for(store_path: str | Path) -> Path:
    p = Path(store_path)
    return p.with_name(p.stem + ".quarantine.parquet")


def _atomic_write(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".parquet.tmp")
    os.close(fd)
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _quarantine(store_path: str | Path, rows: pd.DataFrame, reason: str,
                quarantine_path: str | Path | None = None) -> Path:
    """R-2.1 隔离表: 显式告警 + 落 sidecar, 不静默丢。幂等(同键同值同因不累积)。"""
    qp = Path(quarantine_path) if quarantine_path else quarantine_path_for(store_path)
    q = rows.copy()
    q["quarantine_reason"] = reason
    q["quarantined_at"] = pd.Timestamp.now(tz="UTC")
    if qp.exists():
        old = pd.read_parquet(qp)
        q = pd.concat([old, q], ignore_index=True)
        q = q.drop_duplicates(KEY + ["value", "quarantine_reason"], keep="first")
    _atomic_write(qp, q)
    logger.warning(
        "[PIT隔离] %d 行因 %r 被隔离到 %s (不入主表, 待人工处置), 样例: %s",
        len(rows), reason, qp, rows[KEY].head(3).to_dict("records"))
    return qp


# ---------------------------------------------------------------- validation

def validate_store(df: pd.DataFrame, *, drag_guard_days: int = DRAG_GUARD_DAYS,
                   early_guard_days: int = EARLY_GUARD_DAYS) -> None:
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
    # R-2.2: 精度列为正式字段, 取值域受限
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

    # -- 重复 (symbol,field,period,announce_date) hard fail (qlib.md §1.2 验收3后半 + R-2.4)
    dup = df.duplicated(KEY, keep=False)
    if dup.any():
        raise PITViolation(
            f"重复 (symbol,field,period,announce_date) 键 {int(dup.sum())} 行; {SAME_DAY_REMEDY}; "
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

    # -- 滞后双边守卫: 上界 450 天拖带(openbb.md §3.1 机制二) + 下界 R-2.1
    lag_days = _lag_days(df)
    drag = lag_days > drag_guard_days
    if drag.any():
        raise PITViolation(
            f"拖带守卫: {int(drag.sum())} 行公告晚于期末 {drag_guard_days} 天以上, "
            f"样例: {df.loc[drag, KEY].head(3).to_dict('records')} "
            f"(最大滞后 {int(lag_days.max())} 天)")
    early = lag_days < -early_guard_days
    if early.any():
        raise PITViolation(
            f"滞后下界守卫(R-2.1): {int(early.sum())} 行公告早于期末 {early_guard_days} 天以上 "
            f"(最早 {int(lag_days.min())} 天; 此类行应在入库时被隔离, 主表内出现即不变式破坏), "
            f"样例: {df.loc[early, KEY].head(3).to_dict('records')}")


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
    # available_at 禁降精度: 进门就查, 不等 validate_store (R-2.2)
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
                early_guard_days: int = EARLY_GUARD_DAYS,
                validate: bool = True,
                quarantine_path: str | Path | None = None) -> dict:
    """增量 append(严格口径): 幂等(重复行跳过)、同键异值 hard fail、乱序回填 hard fail、
    revision_seq 只增不改、历史行只读(qlib.md §1.2 验收5 + 红线1)。
    R-2.1: 滞后下界越界行 = 告警 + 隔离 sidecar(不入主表, 不使整批失败, 不静默丢)。
    返回 {'appended': n, 'skipped_duplicates': m, 'total': N, 'quarantined_early': q}。
    """
    path = Path(path)
    inc = _normalize_incoming(new_df)

    # R-2.1 滞后下界守卫: 越界=显式告警+隔离, 不静默丢、不 hard fail 整批
    q_early = 0
    if len(inc):
        lag_days = _lag_days(inc)
        early = lag_days < -early_guard_days
        if early.any():
            _quarantine(path, inc.loc[early], "early_lag_guard", quarantine_path)
            q_early = int(early.sum())
            inc = inc.loc[~early].copy()

    # 拖带守卫(上界)在入口执行(openbb §3.1: 500 天重述值必须在这里被拒)
    if len(inc):
        lag_days = _lag_days(inc)
        drag = lag_days > drag_guard_days
        if drag.any():
            raise PITViolation(
                f"拖带守卫拒收: {int(drag.sum())} 行公告晚于期末 {drag_guard_days} 天以上 "
                f"(最大 {int(lag_days[drag].max())} 天), "
                f"样例: {inc.loc[drag, KEY].head(3).to_dict('records')}")

    # 批内去重: 完全相同行(键+值+标记)只留一条; 同键异值 hard fail (R-2.4 明文)
    inc = inc.drop_duplicates(KEY + ["value", "is_preliminary"], keep="first")
    bdup = inc.duplicated(KEY, keep=False)
    if bdup.any():
        raise PITViolation(
            f"批内同 (symbol,field,period,announce_date) 键异值 {int(bdup.sum())} 行, hard fail; "
            f"{SAME_DAY_REMEDY}; "
            f"样例: {inc.loc[bdup, KEY + ['value']].head(4).to_dict('records')}")

    old = load_store(path)
    if len(old) and len(inc):
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
                f"(历史行只读, 不允许覆写); {SAME_DAY_REMEDY}; 样例: "
                f"{m.loc[conflict, KEY + ['value', 'value_old']].head(3).to_dict('records')}")
        skipped = int(same.sum())
        inc = inc.loc[~hit.to_numpy()].copy()
    else:
        skipped = 0

    if len(inc) == 0:
        return {"appended": 0, "skipped_duplicates": skipped, "total": len(old),
                "quarantined_early": q_early}

    # 乱序回填守卫: 新公告日必须晚于该组存量最大公告日(否则修订链需重建, 违反历史只读)
    inc = inc.sort_values(GROUP + ["announce_date"], kind="mergesort").reset_index(drop=True)
    if len(old):
        gmax = old.groupby(GROUP)["announce_date"].max().rename("ann_max").reset_index()
        chk = inc.merge(gmax, on=GROUP, how="left")
        late = chk["ann_max"].notna() & (chk["announce_date"] <= chk["ann_max"])
        if late.any():
            raise PITViolation(
                f"乱序回填拒收: {int(late.sum())} 行公告日 <= 组内存量最大公告日 "
                f"(append-only 修订链不可插入历史); {OUT_OF_ORDER_REMEDY}; 样例: "
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
        validate_store(out, drag_guard_days=drag_guard_days, early_guard_days=early_guard_days)

    _atomic_write(path, out)  # 原子写(tmp + rename), 不半写
    return {"appended": int(len(inc)), "skipped_duplicates": skipped, "total": int(len(out)),
            "quarantined_early": q_early}


def append_rows_tolerant(path: str | Path, new_df: pd.DataFrame, *,
                         drag_guard_days: int = DRAG_GUARD_DAYS,
                         early_guard_days: int = EARLY_GUARD_DAYS,
                         validate: bool = True,
                         quarantine_path: str | Path | None = None) -> dict:
    """生产摄取口径: 严格口径会 hard fail 整批的三类行(拖带上界/同键异值/乱序回填)
    改为**整行隔离到 sidecar + 告警**, 使单一污染组不阻塞当日全量增量; 余量走严格 append。
    主表不变式与严格口径完全一致 — 隔离行永不进入主表, 且不静默丢(R-2.1 处置模式外推)。
    冲突行的最终处置遵循 R-2.3/R-2.4 明文(整组重建对账 / 人工裁决), 隔离表即待裁决队列。

    返回 stats: appended/skipped_duplicates/total/quarantined_early
                + quarantined_drag/quarantined_conflict/quarantined_out_of_order/quarantine_path
    """
    path = Path(path)
    inc = _normalize_incoming(new_df)
    qstats = {"quarantined_drag": 0, "quarantined_conflict": 0, "quarantined_out_of_order": 0}

    def _iso(mask: pd.Series, reason: str, key: str) -> None:
        nonlocal inc
        if mask.any():
            _quarantine(path, inc.loc[mask], reason, quarantine_path)
            qstats[key] += int(mask.sum())
            inc = inc.loc[~mask].copy()

    # ① 拖带上界 → 隔离(严格口径=raise)
    if len(inc):
        _iso(_lag_days(inc) > drag_guard_days, "drag_guard", "quarantined_drag")
    # ② 批内完全重复行去重(与严格口径一致), 批内同键异值 → 整键隔离(R-2.4 待人工裁决)
    inc = inc.drop_duplicates(KEY + ["value", "is_preliminary"], keep="first")
    if len(inc):
        _iso(inc.duplicated(KEY, keep=False),
             "same_key_value_conflict_batch", "quarantined_conflict")
    # ③ 与存量对账
    old = load_store(path)
    if len(old) and len(inc):
        m = inc.merge(old[KEY + ["value", "is_preliminary"]], on=KEY, how="left",
                      suffixes=("", "_old"), indicator=True)
        m.index = inc.index
        hit = m["_merge"] == "both"
        same = hit & np.isclose(m["value"], m["value_old"], rtol=0, atol=0, equal_nan=True) \
                   & (m["is_preliminary"] == m["is_preliminary_old"])
        # ③a 同键异值 → 隔离(严格口径=raise; 历史行只读, R-2.4 待人工裁决)
        _iso(hit & ~same, "same_key_value_conflict_store", "quarantined_conflict")
        # ③b 乱序回填: 键不在存量、但公告日 <= 组内存量最大 → 隔离(严格口径=raise, R-2.3)
        if len(inc):
            m2 = inc.merge(old[KEY + ["value", "is_preliminary"]], on=KEY, how="left",
                           suffixes=("", "_old"), indicator=True)
            m2.index = inc.index
            is_new = m2["_merge"] == "left_only"
            gmax = old.groupby(GROUP)["announce_date"].max().rename("ann_max").reset_index()
            chk = inc.merge(gmax, on=GROUP, how="left")
            chk.index = inc.index
            late = is_new & chk["ann_max"].notna() & (chk["announce_date"] <= chk["ann_max"])
            _iso(late, "out_of_order_backfill", "quarantined_out_of_order")

    # ④ 余量走严格 append(内含 R-2.1 下界隔离 + 幂等跳过 + revision_seq 分配 + 全表校验)
    res = append_rows(path, inc, drag_guard_days=drag_guard_days,
                      early_guard_days=early_guard_days, validate=validate,
                      quarantine_path=quarantine_path)
    res.update(qstats)
    res["quarantine_path"] = str(Path(quarantine_path) if quarantine_path
                                 else quarantine_path_for(path))
    n_q = res["quarantined_early"] + sum(qstats.values())
    if n_q:
        logger.warning("[PIT双写] 本批共 %d 行被隔离(early=%d drag=%d conflict=%d ooo=%d), "
                       "隔离表=%s", n_q, res["quarantined_early"], qstats["quarantined_drag"],
                       qstats["quarantined_conflict"], qstats["quarantined_out_of_order"],
                       res["quarantine_path"])
    return res


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
