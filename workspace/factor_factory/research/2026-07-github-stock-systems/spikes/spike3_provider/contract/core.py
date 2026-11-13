"""Common contract skeleton: StrictQuery / StrictRow / FetchMeta / TET / harness.

Design sources (frozen research docs):
- openbb.md §2.0 common skeleton (extra='forbid', FetchMeta mandatory, TET
  three stages, stage-isolation assertion in the self-check harness)
- openbb.md §5 batch seven rules ((results, failures) dual channel, explicit
  empty-payload channel, key-level identity, failure enum, reconciliation)
- upstream_diff.md §3.3 ProviderAttempt (lineage lives in lineage.py)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel, ConfigDict


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class ContractViolation(Exception):
    """Hard failure: payload does not honor the frozen interface contract."""

    def __init__(self, interface_id: str, check: str, detail: str):
        self.interface_id = interface_id
        self.check = check
        self.detail = detail
        super().__init__(f"[{interface_id}] contract check '{check}' failed: {detail}")


class ReconcileBreach(Exception):
    """Cross-source reconciliation exceeded tolerance -> reject + hard alert."""


# --------------------------------------------------------------------------
# strict models (openbb.md §2.0: 反其道 #1/#2 — extra='forbid', never warn-then-drop)
# --------------------------------------------------------------------------
class StrictQuery(BaseModel):
    """Unknown params => ValidationError hard failure. Frozen (hashable-ish)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class StrictRow(BaseModel):
    """Standard row model. Source-specific column aliases live in each
    fetcher's to_rows, never on the model (openbb data.py L71 反其道)."""

    model_config = ConfigDict(extra="forbid")


class ErrorType(str, Enum):
    timeout = "timeout"
    empty = "empty"
    schema = "schema"
    rate_limit = "rate_limit"
    circuit_open = "circuit_open"
    exception = "exception"
    calendar = "calendar"
    unit = "unit"
    limit_band = "limit_band"
    reconcile = "reconcile"


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str                      # symbol or "symbol:trade_date" row granularity
    error_type: ErrorType
    message: str
    row_level: bool = False       # True => key survives with remaining rows


class FetchMeta(BaseModel):
    """AnnotatedResult 强化版: mandatory, not optional (openbb.md §1.2)."""

    model_config = ConfigDict(extra="forbid")
    source: str                   # tushare_proxy | akshare_em | akshare_sina | eastmoney_datacenter
    source_version: str
    fetched_at: datetime
    request_fingerprint: str
    row_count: int
    failures: List[FailureRecord] = []
    empty_keys: List[str] = []    # explicit empty-payload channel (§5 rule 3)
    notes: List[str] = []         # per-source capability gaps, explicit


# --------------------------------------------------------------------------
# TET fetcher base
# --------------------------------------------------------------------------
@dataclass
class RawPayload:
    """Opaque IO product. pull_raw must NOT standardize anything."""

    payload: Any
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TETFetcher:
    """Three stages, no instance state carried between them:
        to_query(params: dict) -> Q      (validate + normalize, fail hard)
        pull_raw(q: Q) -> RawPayload     (IO only)
        to_rows(q, raw) -> (dict[key, list[Row]], FetchMeta)
    """

    interface_id: str = ""
    source: str = ""
    data_type: str = ""           # daily | name | announcement | calendar
    query_model: Type[StrictQuery] = StrictQuery
    row_model: Type[StrictRow] = StrictRow

    def to_query(self, params: Dict[str, Any]) -> StrictQuery:
        return self.query_model(**params)

    def pull_raw(self, q: StrictQuery) -> RawPayload:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_rows(self, q: StrictQuery, raw: RawPayload) -> Tuple[Dict[str, List[StrictRow]], FetchMeta]:
        raise NotImplementedError  # pragma: no cover

    def expected_keys(self, q: StrictQuery) -> List[str]:
        """Keys the reconciliation identity is checked against (§5 rule 3/4)."""
        raise NotImplementedError  # pragma: no cover

    def source_version(self) -> str:
        return "unknown"


# --------------------------------------------------------------------------
# self-check harness (borrows Fetcher.test() thought; our implementation)
# --------------------------------------------------------------------------
def _contains_row_instance(obj: Any, depth: int = 0) -> bool:
    """Walk a raw payload; True if any StrictRow instance found (stage leak)."""
    if depth > 6:
        return False
    if isinstance(obj, StrictRow):
        return True
    if isinstance(obj, dict):
        return any(_contains_row_instance(v, depth + 1) for v in obj.values())
    if isinstance(obj, (list, tuple, set)):
        return any(_contains_row_instance(v, depth + 1) for v in obj)
    # pandas containers hold scalars/strings only; cheap check on attrs
    return False


@dataclass
class HarnessReport:
    interface_id: str
    t_ok: bool = False
    e_ok: bool = False
    t2_ok: bool = False
    n_keys_expected: int = 0
    n_ok: int = 0
    n_failed: int = 0
    n_empty: int = 0
    row_count: int = 0
    row_failures: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def all_green(self) -> bool:
        return self.t_ok and self.e_ok and self.t2_ok and not self.errors


def run_harness(
    fetcher: TETFetcher,
    params: Dict[str, Any],
    raw_override: Optional[RawPayload] = None,
) -> Tuple[HarnessReport, Optional[Dict[str, List[StrictRow]]], Optional[FetchMeta]]:
    """Run T / E / T2 with stage-isolation and reconciliation assertions.

    raw_override lets offline tests inject poison payloads without IO.
    """
    rep = HarnessReport(interface_id=fetcher.interface_id)

    # ---- T stage: query type + params identity-retrievable -------------
    q = fetcher.to_query(params)
    if not isinstance(q, fetcher.query_model):
        rep.errors.append(f"T: query type {type(q).__name__} != {fetcher.query_model.__name__}")
    for k, v in params.items():
        got = getattr(q, k, None)
        norm_got = _norm(got)
        norm_v = _norm(v)
        if norm_got != norm_v:
            rep.errors.append(f"T: param '{k}' not identity-retrievable: {norm_v!r} -> {norm_got!r}")
    rep.t_ok = not rep.errors

    # ---- E stage: raw non-empty + NO Row instances in raw ---------------
    raw = raw_override if raw_override is not None else fetcher.pull_raw(q)
    if raw is None or raw.payload is None:
        rep.errors.append("E: raw payload is None")
        return rep, None, None
    if isinstance(raw.payload, (list, dict, str)) and len(raw.payload) == 0:
        rep.errors.append("E: raw payload is empty container")
    if _contains_row_instance(raw.payload):
        rep.errors.append("E: stage isolation broken — Row instance found inside raw payload")
    rep.e_ok = rep.t_ok and not rep.errors

    # ---- T2 stage: rows are Row + invariants + failures reconciliation --
    rows_by_key, meta = fetcher.to_rows(q, raw)
    total_rows = 0
    for key, rows in rows_by_key.items():
        for r in rows:
            if not isinstance(r, fetcher.row_model):
                rep.errors.append(f"T2: key {key} row not {fetcher.row_model.__name__}: {type(r).__name__}")
                break
            total_rows += 1
    rep.row_count = total_rows
    if meta.row_count != total_rows:
        rep.errors.append(f"T2: meta.row_count {meta.row_count} != actual {total_rows}")

    expected = fetcher.expected_keys(q)
    rep.n_keys_expected = len(expected)
    ok_keys = set(rows_by_key.keys())
    failed_keys = {f.key for f in meta.failures if not f.row_level}
    empty_keys = set(meta.empty_keys)
    rep.n_ok, rep.n_failed, rep.n_empty = len(ok_keys), len(failed_keys), len(empty_keys)
    rep.row_failures = sum(1 for f in meta.failures if f.row_level)

    # 勾稽 (§5 rule 3): every expected key lands in exactly one channel
    union = ok_keys | failed_keys | empty_keys
    if set(expected) != union:
        rep.errors.append(
            f"T2: key reconciliation broken: expected {len(expected)} keys, "
            f"channels cover {len(union)} (missing={sorted(set(expected) - union)[:5]}, "
            f"extra={sorted(union - set(expected))[:5]})"
        )
    overlap = (ok_keys & failed_keys) | (ok_keys & empty_keys) | (failed_keys & empty_keys)
    if overlap:
        rep.errors.append(f"T2: keys in multiple channels: {sorted(overlap)[:5]}")
    if len(expected) != rep.n_ok + rep.n_failed + rep.n_empty:
        rep.errors.append(
            f"T2: len(keys)=={len(expected)} != ok({rep.n_ok})+failed({rep.n_failed})+empty({rep.n_empty})"
        )
    rep.t2_ok = rep.e_ok and not rep.errors
    return rep, rows_by_key, meta


def _norm(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return tuple(_norm(x) for x in v)
    return v
