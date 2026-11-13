#!/usr/bin/env python3
"""Unified state machine for stock pipeline execution runs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InvalidTransition(Exception):
    pass


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "created": {"data_ready"},
    "data_ready": {"selection_ready"},
    "selection_ready": {"ai_ready", "ai_degraded"},
    "ai_ready": {"validated"},
    "ai_degraded": {"validated"},
    "validated": {"publish_prepared"},
    "publish_prepared": {"committed"},
    "committed": {"pushed"},
    "pushed": {"remote_confirmed"},
    "remote_confirmed": {"delivered"},
    "delivered": set(),
}


@dataclass
class RunState:
    run_id: str
    trade_date: str
    state: str = "created"

    @classmethod
    def new(cls, run_id: str, trade_date: str) -> RunState:
        return cls(run_id=run_id, trade_date=trade_date, state="created")

    def advance(self, target: str) -> None:
        if target not in ALLOWED_TRANSITIONS.get(self.state, set()):
            raise InvalidTransition(f"Invalid transition: {self.state} -> {target}")
        self.state = target

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        return cls(
            run_id=str(data.get("run_id") or ""),
            trade_date=str(data.get("trade_date") or ""),
            state=str(data.get("state") or "created"),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".run_state_")
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
    def load(cls, path: Path) -> RunState:
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)
