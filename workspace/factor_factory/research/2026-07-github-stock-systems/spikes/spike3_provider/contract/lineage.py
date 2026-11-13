"""Provider lineage: ProviderAttempt records -> run_manifest attachment.

Per upstream_diff.md §3.3 with the three 反其道 decisions:
  1. lineage sink write failure is NOT fail-open swallowed — it emits a WARN
     (observability breaking is itself an incident);
  2. freshness_lag_days is a first-class field;
  3. circuit-breaker state stays in-process (not implemented in this spike —
     enum value reserved; Wave1 scope).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("spike3.lineage")


@dataclass
class ProviderAttempt:
    run_id: str
    data_type: str                 # daily|auction|minute|calendar|name|announcement
    provider: str                  # tushare_proxy|akshare_em|akshare_sina|eastmoney_datacenter
    operation: str
    success: bool
    latency_ms: int
    error_type: Optional[str] = None    # timeout|empty|schema|rate_limit|circuit_open|exception
    error_message: Optional[str] = None
    fallback_to: Optional[str] = None
    record_count: Optional[int] = None
    freshness_lag_days: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LineageSink:
    """Append-only attempt chain, flushed into a run_manifest JSON attachment."""

    def __init__(self, manifest_path: Path, run_id: Optional[str] = None):
        self.manifest_path = Path(manifest_path)
        self.run_id = run_id or f"spike3-{uuid.uuid4().hex[:8]}"
        self.attempts: List[ProviderAttempt] = []
        self.extra: Dict[str, Any] = {}
        self.warnings: List[str] = []

    def record(self, attempt: ProviderAttempt) -> bool:
        self.attempts.append(attempt)
        return self.flush()

    def put(self, key: str, value: Any) -> None:
        self.extra[key] = value

    def flush(self) -> bool:
        """Write manifest. Failure => WARN (never raises, never silent)."""
        doc = {
            "run_id": self.run_id,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "attempts": [asdict(a) for a in self.attempts],
            "warnings": self.warnings,
            **self.extra,
        }
        try:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str))
            tmp.replace(self.manifest_path)
            return True
        except Exception as exc:  # lineage write failure itself must WARN
            msg = f"LINEAGE_WRITE_FAILED path={self.manifest_path} err={type(exc).__name__}: {exc}"
            self.warnings.append(msg)
            logger.warning(msg)
            return False


def timed_attempt(sink: LineageSink, data_type: str, provider: str, operation: str):
    """Context manager recording one attempt (success or failure) with latency."""

    class _Ctx:
        def __init__(self):
            self.record_count: Optional[int] = None
            self.freshness_lag_days: Optional[int] = None
            self.fallback_to: Optional[str] = None
            self.error_type: Optional[str] = None

        def __enter__(self):
            self._t0 = time.monotonic()
            return self

        def __exit__(self, exc_type, exc, tb):
            latency = int((time.monotonic() - self._t0) * 1000)
            if exc is None:
                sink.record(
                    ProviderAttempt(
                        run_id=sink.run_id, data_type=data_type, provider=provider,
                        operation=operation, success=True, latency_ms=latency,
                        record_count=self.record_count,
                        freshness_lag_days=self.freshness_lag_days,
                        fallback_to=self.fallback_to,
                    )
                )
                return False
            etype = self.error_type or _classify(exc)
            sink.record(
                ProviderAttempt(
                    run_id=sink.run_id, data_type=data_type, provider=provider,
                    operation=operation, success=False, latency_ms=latency,
                    error_type=etype, error_message=f"{type(exc).__name__}: {str(exc)[:200]}",
                    fallback_to=self.fallback_to,
                )
            )
            return False  # never swallow: caller decides fallback

    return _Ctx()


def _classify(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return "timeout"
    if "contractviolation" in name or "validationerror" in name:
        return "schema"
    if "connection" in name or "connection" in text or "proxy" in text:
        return "exception"
    return "exception"
