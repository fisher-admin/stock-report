#!/usr/bin/env python3
"""Audit cron job delivery configuration and recent delivery status.

Consumes `cron list` JSON saved to stdin/file in future if needed, but for now
reads from a checked-in JSON snapshot path when present. This keeps a reusable
audit format in workspace health.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

WORKSPACE = Path("/Users/fisher/.openclaw/workspace")
OUT = WORKSPACE / "stock_data/03-working/health/cron_delivery_audit.json"
SNAPSHOT = WORKSPACE / "stock_data/03-working/health/cron_jobs_snapshot.json"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def classify(job: dict) -> dict:
    delivery = job.get("delivery") or {}
    state = job.get("state") or {}
    issues = []
    mode = delivery.get("mode") or "announce"
    if mode in {"announce", "webhook"} and not (delivery.get("to") or delivery.get("channel")):
        issues.append("DELIVERY_TARGET_MISSING")
    if mode != "none" and state.get("lastDeliveryStatus") in {"not-delivered", "unknown", "not-requested"}:
        issues.append(f"LAST_DELIVERY_{str(state.get('lastDeliveryStatus')).upper().replace('-', '_')}")
    if state.get("lastStatus") == "error":
        issues.append("LAST_RUN_ERROR")
    if state.get("lastErrorReason") == "timeout":
        issues.append("LAST_RUN_TIMEOUT")
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "enabled": job.get("enabled"),
        "delivery": delivery,
        "state": state,
        "issues": issues,
        "ok": not issues,
    }


def main() -> int:
    payload = {
        "generated_at": now_str(),
        "ok": True,
        "jobs": [],
        "summary": {
            "total": 0,
            "problematic": 0,
        },
        "source": str(SNAPSHOT),
    }
    if not SNAPSHOT.exists():
        payload["ok"] = False
        payload["error"] = "snapshot missing"
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(OUT))
        return 1

    raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    jobs = raw.get("jobs") or raw
    audited = [classify(job) for job in jobs]
    payload["jobs"] = audited
    payload["summary"]["total"] = len(audited)
    payload["summary"]["problematic"] = sum(1 for item in audited if not item["ok"])
    payload["ok"] = payload["summary"]["problematic"] == 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(OUT))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
