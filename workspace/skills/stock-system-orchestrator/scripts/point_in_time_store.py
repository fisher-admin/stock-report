#!/usr/bin/env python3
"""Point-In-Time (PIT) immutable storage for stock market data.

Preserves the first-seen snapshot for every (trade_date, domain) pair to ensure
historical reproducibility. If vendor data later drifts on the same date, the store
preserves the initial version and records an immutable audit log of the drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SnapshotImmutableError(Exception):
    pass


@dataclass
class PointInTimeStore:
    base_dir: Path

    def __post_init__(self):
        self.base_dir = Path(self.base_dir)

    def _manifest_path(self, trade_date: str) -> Path:
        return self.base_dir / trade_date / "manifest.json"

    def _drift_log_path(self, trade_date: str) -> Path:
        return self.base_dir / trade_date / "drift_audit.jsonl"

    def _load_manifest(self, trade_date: str) -> dict[str, Any]:
        p = self._manifest_path(trade_date)
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_manifest(self, trade_date: str, manifest: dict[str, Any]) -> None:
        p = self._manifest_path(trade_date)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(manifest, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".manifest_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _record_drift(self, trade_date: str, drift_info: dict[str, Any]) -> None:
        p = self._drift_log_path(trade_date)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(drift_info, ensure_ascii=False) + "\n")

    def get(self, trade_date: str, domain: str) -> Path | None:
        manifest = self._load_manifest(trade_date)
        if domain in manifest:
            filename = manifest[domain].get("filename") or f"{domain}.parquet"
            target = self.base_dir / trade_date / filename
            if target.is_file():
                return target
        return None

    def has(self, trade_date: str, domain: str) -> bool:
        return self.get(trade_date, domain) is not None

    def put(
        self,
        trade_date: str,
        domain: str,
        content: bytes | str,
        ext: str = "parquet",
        strict: bool = False,
    ) -> Path:
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
        byte_length = len(raw_bytes)

        target_dir = self.base_dir / trade_date
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{domain}.{ext}"

        manifest = self._load_manifest(trade_date)
        if domain in manifest:
            existing = manifest[domain]
            if existing.get("sha256") == sha256_hash:
                return target_file

            if strict:
                raise SnapshotImmutableError(
                    f"Snapshot for date={trade_date}, domain={domain} is immutable. "
                    f"Existing sha={existing.get('sha256')}, new sha={sha256_hash}"
                )

            # Preserve the pristine first snapshot and record vendor drift in audit log
            drift_entry = {
                "trade_date": trade_date,
                "domain": domain,
                "frozen_sha256": existing.get("sha256"),
                "incoming_sha256": sha256_hash,
                "incoming_bytes": byte_length,
                "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
                "action": "preserved_initial_snapshot",
            }
            self._record_drift(trade_date, drift_entry)
            return target_file

        fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=f".{domain}_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        manifest[domain] = {
            "domain": domain,
            "filename": target_file.name,
            "sha256": sha256_hash,
            "byte_length": byte_length,
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        self._save_manifest(trade_date, manifest)
        return target_file
