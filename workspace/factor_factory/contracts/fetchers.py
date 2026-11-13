"""Concrete TET fetchers for the three interface families.

Every fetcher: pull_raw = IO only; to_rows = frozen-contract assertion +
named mapping to StrictRow + (results, failures, empty) triple channel.

R-3.1 (PHASE3_SPIKES_REPORT.md): pre_close carries a per-source CAPABILITY
declared in contracts.yaml (source_native | derived_same_row_delta |
unavailable). None is legal ONLY when the source declares 'unavailable';
a native/derived source emitting None — or an 'unavailable' source suddenly
emitting values — is a row-level contract failure (fail-closed, no repair).
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError, field_validator

from .core import (
    ContractViolation,
    ErrorType,
    FailureRecord,
    FetchMeta,
    RawPayload,
    StrictQuery,
    TETFetcher,
)
from .models import (
    CN_TZ,
    DailyBarV1,
    EarningsPreannounceV1,
    SecurityMetaV1,
    close_dt,
)

_CONTRACTS_PATH = Path(__file__).resolve().parent / "contracts.yaml"
CONTRACTS: Dict[str, Any] = yaml.safe_load(_CONTRACTS_PATH.read_text())

# R-3.1: legal values of the per-interface pre_close capability declaration
PRE_CLOSE_CAPABILITIES = ("source_native", "derived_same_row_delta", "unavailable")


def pre_close_capability(interface_id: str) -> str:
    cap = CONTRACTS[interface_id].get("capabilities", {}).get("pre_close")
    if cap not in PRE_CLOSE_CAPABILITIES:
        raise ContractViolation(interface_id, "capabilities.pre_close",
                                f"missing or invalid declaration: {cap!r}")
    return cap


def _pre_close_capability_check(
    interface_id: str, sym: str, rows: List["DailyBarV1"], failures: List[FailureRecord]
) -> None:
    """R-3.1 enforcement. Violating rows are removed (fail-closed, row-level)."""
    cap = pre_close_capability(interface_id)
    bad_idx = []
    for i, r in enumerate(rows):
        if cap == "unavailable" and r.pre_close is not None:
            bad_idx.append((i, "source declared pre_close unavailable but a value appeared"))
        elif cap != "unavailable" and r.pre_close is None:
            bad_idx.append((i, f"pre_close is None but capability is {cap}"))
    for i, msg in reversed(bad_idx):
        r = rows[i]
        failures.append(FailureRecord(
            key=f"{sym}:{r.trade_date.isoformat()}", error_type=ErrorType.schema,
            message=f"pre_close capability breach: {msg}", row_level=True))
        del rows[i]


def _akshare_version() -> str:
    try:
        import akshare

        return getattr(akshare, "__version__", "unknown")
    except Exception:
        return "unavailable"


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
class DailyQuery(StrictQuery):
    symbols: Tuple[str, ...]
    start_date: date
    end_date: date
    adjust: str = ""          # PIT ban: qfq/hfq refused at query stage

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: Tuple[str, ...]) -> Tuple[str, ...]:
        import re

        if not v:
            raise ValueError("symbols empty")
        for s in v:
            if not re.match(r"^\d{6}\.(SH|SZ|BJ)$", s):
                raise ValueError(f"bad symbol {s!r}")
        return tuple(v)

    @field_validator("adjust")
    @classmethod
    def _adjust(cls, v: str) -> str:
        if v in ("qfq", "hfq"):
            raise ValueError(f"adjust={v!r} banned by PIT contract (request-time adjusted prices)")
        if v != "":
            raise ValueError(f"unknown adjust {v!r}")
        return v


class MetaQuery(StrictQuery):
    """Full-table snapshot query (no params)."""


class YjygQuery(StrictQuery):
    period: date              # fiscal report period end, e.g. 2026-03-31


# --------------------------------------------------------------------------
# daily: tushare proxy (primary)
# --------------------------------------------------------------------------
class DailyTushareFetcher(TETFetcher):
    interface_id = "daily_tushare"
    source = "tushare_proxy"
    data_type = "daily"
    query_model = DailyQuery
    row_model = DailyBarV1

    def source_version(self) -> str:
        from .tushare_client import tushare_version

        return f"tushare=={tushare_version()}"

    def expected_keys(self, q: DailyQuery) -> List[str]:
        return list(q.symbols)

    def pull_raw(self, q: DailyQuery) -> RawPayload:
        from .tushare_client import get_pro

        pro = get_pro()
        df = pro.daily(
            ts_code=",".join(q.symbols),
            start_date=q.start_date.strftime("%Y%m%d"),
            end_date=q.end_date.strftime("%Y%m%d"),
        )
        return RawPayload(payload=df)

    def to_rows(self, q: DailyQuery, raw: RawPayload) -> Tuple[Dict[str, List[DailyBarV1]], FetchMeta]:
        df = raw.payload
        c = CONTRACTS[self.interface_id]
        if list(df.columns) != c["frozen_columns"]:
            raise ContractViolation(self.interface_id, "frozen_columns",
                                    f"{list(df.columns)} != {c['frozen_columns']}")
        rows_by_key: Dict[str, List[DailyBarV1]] = {}
        failures: List[FailureRecord] = []
        empty_keys: List[str] = []
        by_symbol = {s: g for s, g in df.groupby("ts_code")} if len(df) else {}
        for sym in q.symbols:
            g = by_symbol.get(sym)
            if g is None or len(g) == 0:
                empty_keys.append(sym)
                continue
            rows: List[DailyBarV1] = []
            for rec in g.to_dict("records"):
                td = _yyyymmdd(rec["trade_date"])
                try:
                    rows.append(DailyBarV1(
                        symbol=sym,
                        trade_date=td,
                        open=float(rec["open"]), high=float(rec["high"]),
                        low=float(rec["low"]), close=float(rec["close"]),
                        pre_close=float(rec["pre_close"]),
                        volume=int(round(float(rec["vol"]) * 100)),        # 手 -> 股
                        amount=float(rec["amount"]) * 1000.0,               # 千元 -> 元
                        adj_factor=None,
                        is_suspended=False,
                        source=self.source, fetched_at=raw.fetched_at,
                        available_at=close_dt(td),
                    ))
                except (ValidationError, ValueError, TypeError) as exc:
                    failures.append(FailureRecord(
                        key=f"{sym}:{td}", error_type=ErrorType.schema,
                        message=str(exc)[:200], row_level=True))
            _pre_close_capability_check(self.interface_id, sym, rows, failures)  # R-3.1
            _unit_self_check(sym, rows, failures)
            if rows:
                rows_by_key[sym] = rows
            else:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.schema,
                                              message="all rows rejected", row_level=False))
        meta = FetchMeta(
            source=self.source, source_version=self.source_version(),
            fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
            row_count=sum(len(v) for v in rows_by_key.values()),
            failures=failures, empty_keys=empty_keys,
            notes=["pre_close_capability=source_native"],
        )
        return rows_by_key, meta


# --------------------------------------------------------------------------
# daily: akshare EM (stock_zh_a_hist) — spec source; host currently blocked,
# real attempts become lineage failure evidence.
# --------------------------------------------------------------------------
class DailyAkshareEmFetcher(TETFetcher):
    interface_id = "daily_akshare_em"
    source = "akshare_em"
    data_type = "daily"
    query_model = DailyQuery
    row_model = DailyBarV1

    def source_version(self) -> str:
        return f"akshare=={_akshare_version()}"

    def expected_keys(self, q: DailyQuery) -> List[str]:
        return list(q.symbols)

    def pull_raw(self, q: DailyQuery) -> RawPayload:
        import akshare as ak

        frames: Dict[str, Any] = {}
        for sym in q.symbols:
            code = sym.split(".")[0]
            try:
                df = ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=q.start_date.strftime("%Y%m%d"),
                    end_date=q.end_date.strftime("%Y%m%d"),
                    adjust=q.adjust, timeout=30,
                )
                frames[sym] = df
            except Exception as exc:  # captured as data, classified in T2
                frames[sym] = {"__fetch_error__": f"{type(exc).__name__}: {str(exc)[:200]}"}
            _time.sleep(0.3)
        return RawPayload(payload={"frames": frames})

    def to_rows(self, q: DailyQuery, raw: RawPayload) -> Tuple[Dict[str, List[DailyBarV1]], FetchMeta]:
        c = CONTRACTS[self.interface_id]
        frames = raw.payload["frames"]
        rows_by_key: Dict[str, List[DailyBarV1]] = {}
        failures: List[FailureRecord] = []
        empty_keys: List[str] = []
        for sym in q.symbols:
            obj = frames.get(sym)
            if obj is None:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.empty,
                                              message="symbol missing from raw payload"))
                continue
            if isinstance(obj, dict) and "__fetch_error__" in obj:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.exception,
                                              message=obj["__fetch_error__"]))
                continue
            df = obj
            if len(df) == 0:
                empty_keys.append(sym)
                continue
            if list(df.columns) != c["frozen_columns"]:
                # e.g. f116 starts being returned -> 13 cols; or upstream drops one
                failures.append(FailureRecord(
                    key=sym, error_type=ErrorType.schema,
                    message=f"kline column contract broken: {list(df.columns)}"))
                continue
            rows: List[DailyBarV1] = []
            for rec in df.to_dict("records"):
                td = rec["日期"] if isinstance(rec["日期"], date) else _yyyymmdd(str(rec["日期"]).replace("-", ""))
                try:
                    close = float(rec["收盘"])
                    chg = float(rec["涨跌额"])
                    rows.append(DailyBarV1(
                        symbol=sym, trade_date=td,
                        open=float(rec["开盘"]), high=float(rec["最高"]),
                        low=float(rec["最低"]), close=close,
                        pre_close=round(close - chg, 4),   # same-row source delta
                        volume=int(round(float(rec["成交量"]) * 100)),   # 手 -> 股
                        amount=float(rec["成交额"]),                      # already 元
                        adj_factor=None, is_suspended=False,
                        source=self.source, fetched_at=raw.fetched_at,
                        available_at=close_dt(td),
                    ))
                except (ValidationError, ValueError, TypeError) as exc:
                    failures.append(FailureRecord(key=f"{sym}:{td}", error_type=ErrorType.schema,
                                                  message=str(exc)[:200], row_level=True))
            _pre_close_capability_check(self.interface_id, sym, rows, failures)  # R-3.1
            _unit_self_check(sym, rows, failures)
            if rows:
                rows_by_key[sym] = rows
            else:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.schema,
                                              message="all rows rejected"))
        meta = FetchMeta(
            source=self.source, source_version=self.source_version(),
            fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
            row_count=sum(len(v) for v in rows_by_key.values()),
            failures=failures, empty_keys=empty_keys,
            notes=["pre_close_capability=derived_same_row_delta (收盘-涨跌额, source-carried delta)"],
        )
        return rows_by_key, meta


# --------------------------------------------------------------------------
# daily: akshare Sina (stock_zh_a_daily) — reachable akshare channel
# --------------------------------------------------------------------------
class DailyAkshareSinaFetcher(TETFetcher):
    interface_id = "daily_akshare_sina"
    source = "akshare_sina"
    data_type = "daily"
    query_model = DailyQuery
    row_model = DailyBarV1

    def source_version(self) -> str:
        return f"akshare=={_akshare_version()}"

    def expected_keys(self, q: DailyQuery) -> List[str]:
        return list(q.symbols)

    def pull_raw(self, q: DailyQuery) -> RawPayload:
        import akshare as ak

        frames: Dict[str, Any] = {}
        for sym in q.symbols:
            code, _, suffix = sym.partition(".")
            sina_symbol = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[suffix] + code
            try:
                df = ak.stock_zh_a_daily(
                    symbol=sina_symbol,
                    start_date=q.start_date.strftime("%Y%m%d"),
                    end_date=q.end_date.strftime("%Y%m%d"),
                    adjust=q.adjust,
                )
                frames[sym] = df
            except Exception as exc:
                frames[sym] = {"__fetch_error__": f"{type(exc).__name__}: {str(exc)[:200]}"}
            _time.sleep(0.8)  # sina 自认封 IP; be gentle
        return RawPayload(payload={"frames": frames})

    def to_rows(self, q: DailyQuery, raw: RawPayload) -> Tuple[Dict[str, List[DailyBarV1]], FetchMeta]:
        c = CONTRACTS[self.interface_id]
        frames = raw.payload["frames"]
        rows_by_key: Dict[str, List[DailyBarV1]] = {}
        failures: List[FailureRecord] = []
        empty_keys: List[str] = []
        for sym in q.symbols:
            obj = frames.get(sym)
            if obj is None:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.empty,
                                              message="symbol missing from raw payload"))
                continue
            if isinstance(obj, dict) and "__fetch_error__" in obj:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.exception,
                                              message=obj["__fetch_error__"]))
                continue
            df = obj
            if len(df) == 0:
                empty_keys.append(sym)
                continue
            if list(df.columns) != c["frozen_columns"]:
                failures.append(FailureRecord(
                    key=sym, error_type=ErrorType.schema,
                    message=f"sina daily column contract broken: {list(df.columns)}"))
                continue
            rows: List[DailyBarV1] = []
            for rec in df.to_dict("records"):
                td = rec["date"] if isinstance(rec["date"], date) else _yyyymmdd(str(rec["date"]).replace("-", ""))
                try:
                    rows.append(DailyBarV1(
                        symbol=sym, trade_date=td,
                        open=float(rec["open"]), high=float(rec["high"]),
                        low=float(rec["low"]), close=float(rec["close"]),
                        pre_close=None,                       # capability gap, explicit
                        volume=int(round(float(rec["volume"]))),   # already 股
                        amount=float(rec["amount"]),               # already 元
                        adj_factor=None, is_suspended=False,
                        source=self.source, fetched_at=raw.fetched_at,
                        available_at=close_dt(td),
                    ))
                except (ValidationError, ValueError, TypeError) as exc:
                    failures.append(FailureRecord(key=f"{sym}:{td}", error_type=ErrorType.schema,
                                                  message=str(exc)[:200], row_level=True))
            _pre_close_capability_check(self.interface_id, sym, rows, failures)  # R-3.1
            _unit_self_check(sym, rows, failures)
            if rows:
                rows_by_key[sym] = rows
            else:
                failures.append(FailureRecord(key=sym, error_type=ErrorType.schema,
                                              message="all rows rejected"))
        meta = FetchMeta(
            source=self.source, source_version=self.source_version(),
            fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
            row_count=sum(len(v) for v in rows_by_key.values()),
            failures=failures, empty_keys=empty_keys,
            notes=["pre_close_capability=unavailable",
                   "pre_close unavailable from sina daily; band invariant not evaluable on this source"],
        )
        return rows_by_key, meta


# --------------------------------------------------------------------------
# names/industry: tushare stock_basic vs akshare stock_info_a_code_name
# --------------------------------------------------------------------------
class SecurityMetaTushareFetcher(TETFetcher):
    interface_id = "security_meta_tushare"
    source = "tushare_proxy"
    data_type = "name"
    query_model = MetaQuery
    row_model = SecurityMetaV1

    def source_version(self) -> str:
        from .tushare_client import tushare_version

        return f"tushare=={tushare_version()}"

    def expected_keys(self, q: MetaQuery) -> List[str]:
        return ["__table__:stock_basic"]

    def pull_raw(self, q: MetaQuery) -> RawPayload:
        from .tushare_client import get_pro

        pro = get_pro()
        df = pro.stock_basic(exchange="", list_status="L",
                             fields="ts_code,symbol,name,area,industry,market,list_date")
        return RawPayload(payload=df)

    def to_rows(self, q: MetaQuery, raw: RawPayload) -> Tuple[Dict[str, List[SecurityMetaV1]], FetchMeta]:
        c = CONTRACTS[self.interface_id]
        df = raw.payload
        if list(df.columns) != c["frozen_columns"]:
            raise ContractViolation(self.interface_id, "frozen_columns",
                                    f"{list(df.columns)} != {c['frozen_columns']}")
        if len(df) < c["min_rows"]:
            raise ContractViolation(self.interface_id, "min_rows", f"{len(df)} < {c['min_rows']}")
        failures: List[FailureRecord] = []
        rows: List[SecurityMetaV1] = []
        for rec in df.to_dict("records"):
            try:
                ld = rec.get("list_date")
                rows.append(SecurityMetaV1(
                    symbol=str(rec["ts_code"]),
                    name=str(rec["name"]),
                    industry=(str(rec["industry"]) if rec.get("industry") not in (None, "", float("nan")) and str(rec.get("industry")) != "nan" else None),
                    list_date=_yyyymmdd(str(ld)) if ld and str(ld) != "nan" else None,
                    source=self.source, fetched_at=raw.fetched_at,
                    available_at=raw.fetched_at,
                ))
            except (ValidationError, ValueError, TypeError) as exc:
                failures.append(FailureRecord(key=str(rec.get("ts_code", "?")),
                                              error_type=ErrorType.schema,
                                              message=str(exc)[:200], row_level=True))
        meta = FetchMeta(
            source=self.source, source_version=self.source_version(),
            fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
            row_count=len(rows), failures=failures, empty_keys=[],
        )
        return {"__table__:stock_basic": rows}, meta


class SecurityMetaAkshareFetcher(TETFetcher):
    interface_id = "security_meta_akshare"
    source = "akshare_exchange"
    data_type = "name"
    query_model = MetaQuery
    row_model = SecurityMetaV1

    def source_version(self) -> str:
        return f"akshare=={_akshare_version()}"

    def expected_keys(self, q: MetaQuery) -> List[str]:
        return ["__table__:code_name"]

    def pull_raw(self, q: MetaQuery) -> RawPayload:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        return RawPayload(payload=df)

    def to_rows(self, q: MetaQuery, raw: RawPayload) -> Tuple[Dict[str, List[SecurityMetaV1]], FetchMeta]:
        c = CONTRACTS[self.interface_id]
        df = raw.payload
        if list(df.columns) != c["frozen_columns"]:
            raise ContractViolation(self.interface_id, "frozen_columns",
                                    f"{list(df.columns)} != {c['frozen_columns']}")
        if len(df) < c["min_rows"]:
            raise ContractViolation(self.interface_id, "min_rows", f"{len(df)} < {c['min_rows']}")
        failures: List[FailureRecord] = []
        rows: List[SecurityMetaV1] = []
        for rec in df.to_dict("records"):
            code = str(rec["code"]).zfill(6)
            try:
                rows.append(SecurityMetaV1(
                    symbol=f"{code}.{_suffix_of(code)}",
                    name=str(rec["name"]),
                    industry=None, list_date=None,           # capability gaps, explicit
                    source=self.source, fetched_at=raw.fetched_at,
                    available_at=raw.fetched_at,
                ))
            except (ValidationError, ValueError, TypeError) as exc:
                failures.append(FailureRecord(key=code, error_type=ErrorType.schema,
                                              message=str(exc)[:200], row_level=True))
        meta = FetchMeta(
            source=self.source, source_version=self.source_version(),
            fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
            row_count=len(rows), failures=failures, empty_keys=[],
            notes=["listed-only survivor universe; industry/list_date not provided"],
        )
        return {"__table__:code_name": rows}, meta


def _suffix_of(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    raise ValueError(f"cannot infer exchange for code {code}")


# --------------------------------------------------------------------------
# announcements: yjyg (eastmoney datacenter family, named 27-key contract)
# --------------------------------------------------------------------------
class YjygDatacenterFetcher(TETFetcher):
    interface_id = "yjyg_datacenter"
    source = "eastmoney_datacenter"
    data_type = "announcement"
    query_model = YjygQuery
    row_model = EarningsPreannounceV1

    def source_version(self) -> str:
        return f"akshare=={_akshare_version()} (request mirrored from stock_yjyg_em impl)"

    def expected_keys(self, q: YjygQuery) -> List[str]:
        return [f"__table__:yjyg:{q.period.isoformat()}"]

    def pull_raw(self, q: YjygQuery) -> RawPayload:
        import requests

        url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        params = {
            "sortColumns": "NOTICE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
            "pageSize": "500", "pageNumber": "1",
            "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
            "filter": f" (REPORT_DATE='{q.period.isoformat()}')",
        }
        records: List[dict] = []
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        total_pages = int(j["result"]["pages"])
        records.extend(j["result"]["data"])
        for page in range(2, total_pages + 1):
            params["pageNumber"] = str(page)
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            records.extend(r.json()["result"]["data"])
            _time.sleep(0.3)
        return RawPayload(payload={"records": records, "pages": total_pages})

    def to_rows(self, q: YjygQuery, raw: RawPayload) -> Tuple[Dict[str, List[EarningsPreannounceV1]], FetchMeta]:
        c = CONTRACTS[self.interface_id]
        frozen = set(c["frozen_keys"])
        records = raw.payload["records"]
        table_key = self.expected_keys(q)[0]
        failures: List[FailureRecord] = []
        empty_keys: List[str] = []
        if not records:
            empty_keys.append(table_key)
            meta = FetchMeta(source=self.source, source_version=self.source_version(),
                             fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
                             row_count=0, failures=[], empty_keys=empty_keys)
            return {}, meta
        rows: List[EarningsPreannounceV1] = []
        for i, rec in enumerate(records):
            keys = set(rec.keys())
            if len(keys) != c["expected_nkeys"] or keys != frozen:
                # datacenter 族: schema drift => reject the whole payload, fail loud
                raise ContractViolation(
                    self.interface_id, "expected_nkeys",
                    f"record#{i} has {len(keys)} keys (expect {c['expected_nkeys']}); "
                    f"missing={sorted(frozen - keys)[:4]} extra={sorted(keys - frozen)[:4]}")
            sec = str(rec["SECUCODE"])
            try:
                ann = _dc_date(rec["NOTICE_DATE"])
                period = _dc_date(rec["REPORT_DATE"])
                rows.append(EarningsPreannounceV1(
                    symbol=sec, name=str(rec["SECURITY_NAME_ABBR"]),
                    indicator=str(rec["PREDICT_FINANCE"]),
                    period=period, ann_date=ann,
                    forecast_value=_maybe_float(rec["FORECAST_JZ"]),
                    prev_year_value=_maybe_float(rec["PREYEAR_SAME_PERIOD"]),
                    change_pct=_maybe_float(rec["INCREASE_JZ"]),
                    predict_type=(str(rec["PREDICT_TYPE"]) if rec["PREDICT_TYPE"] else None),
                    is_preliminary=True,
                    source=self.source, fetched_at=raw.fetched_at,
                    available_at=datetime.combine(ann, time(0, 0), tzinfo=CN_TZ),
                ))
            except (ValidationError, ValueError, TypeError) as exc:
                failures.append(FailureRecord(key=f"{sec}:{i}", error_type=ErrorType.schema,
                                              message=str(exc)[:200], row_level=True))
        meta = FetchMeta(
            source=self.source, source_version=self.source_version(),
            fetched_at=raw.fetched_at, request_fingerprint=q.fingerprint(),
            row_count=len(rows), failures=failures, empty_keys=empty_keys,
        )
        return {table_key: rows}, meta


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _yyyymmdd(s: Any) -> date:
    s = str(s)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _dc_date(s: Any) -> date:
    return date(int(str(s)[:4]), int(str(s)[5:7]), int(str(s)[8:10]))


def _maybe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN -> None


def _unit_self_check(sym: str, rows: List[DailyBarV1], failures: List[FailureRecord]) -> None:
    """implied-VWAP unit check (audit §3.1 correct-posture, fail-closed variant):
    median(amount / (close*volume)) must sit near 1; ~100 or ~0.01 => unit error.
    No auto-correction — breach is a failure, not a repair."""
    ratios = [r.amount / (r.close * r.volume) for r in rows if r.volume > 0 and r.close > 0]
    if not ratios:
        return
    ratios.sort()
    med = ratios[len(ratios) // 2]
    if not (0.2 <= med <= 5.0):
        failures.append(FailureRecord(
            key=sym, error_type=ErrorType.unit,
            message=f"implied-VWAP unit check failed: median amount/(close*volume)={med:.4g} outside [0.2,5]",
            row_level=True))
        rows.clear()  # reject: unit-broken rows never leave the fetcher
