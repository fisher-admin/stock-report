#!/usr/bin/env python3
"""Durable deployment receipt for stock pipeline publish verification."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


@dataclass(frozen=True)
class DeploymentReceipt:
    run_id: str
    trade_date: str
    local_sha: str
    remote_sha: str
    remote_confirmed: bool
    confirmed_at: str

    def matches(self, run_id: str, trade_date: str) -> bool:
        return bool(
            self.remote_confirmed
            and self.local_sha
            and self.local_sha == self.remote_sha
            and self.run_id == run_id
            and self.trade_date == trade_date
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "local_sha": self.local_sha,
            "remote_sha": self.remote_sha,
            "remote_confirmed": self.remote_confirmed,
            "confirmed_at": self.confirmed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeploymentReceipt:
        return cls(
            run_id=str(data.get("run_id") or ""),
            trade_date=str(data.get("trade_date") or ""),
            local_sha=str(data.get("local_sha") or ""),
            remote_sha=str(data.get("remote_sha") or ""),
            remote_confirmed=bool(data.get("remote_confirmed")),
            confirmed_at=str(data.get("confirmed_at") or ""),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".receipt_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> DeploymentReceipt | None:
        path = Path(path)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except Exception:
            return None
