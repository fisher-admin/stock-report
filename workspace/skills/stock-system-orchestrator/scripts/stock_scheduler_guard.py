#!/usr/bin/env python3
"""Guard against duplicate enabled Hermes stock scheduler jobs across profiles."""

import argparse
import json
import sys
from pathlib import Path


def job_fingerprint(job: dict) -> tuple[str, str, str, str]:
    return (
        str(job.get("name") or ""),
        str((job.get("schedule") or {}).get("expr") or ""),
        str(job.get("script") or ""),
        str(job.get("workdir") or ""),
    )


def load_jobs(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid jobs payload: {path}: {exc}") from exc

    if isinstance(payload, dict):
        rows = payload.get("jobs") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"invalid jobs payload: {path}")
    return [row for row in rows if isinstance(row, dict)]


def scan_stock_jobs(hermes_home: Path) -> dict:
    stores = [("root", hermes_home / "cron/jobs.json")]
    profiles_dir = hermes_home / "profiles"
    if profiles_dir.is_dir():
        stores.extend(
            (path.parent.parent.name, path)
            for path in sorted(profiles_dir.glob("*/cron/jobs.json"))
        )

    enabled = []
    for profile, path in stores:
        for job in load_jobs(path):
            if job.get("enabled") and str(job.get("name") or "").startswith("股票系统-"):
                enabled.append({
                    "profile": profile,
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "fingerprint": job_fingerprint(job),
                })

    grouped: dict[tuple[str, str, str, str], list[dict]] = {}
    for item in enabled:
        grouped.setdefault(item["fingerprint"], []).append(item)

    duplicates = []
    for fingerprint, items in grouped.items():
        profiles = sorted({item["profile"] for item in items})
        if len(profiles) > 1:
            duplicates.append({
                "fingerprint": list(fingerprint),
                "profiles": profiles,
                "jobs": [{"profile": item["profile"], "id": item["id"]} for item in items],
            })

    return {
        "ok": not duplicates,
        "enabled_count": len(enabled),
        "duplicates": duplicates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes stock scheduler guard")
    parser.add_argument(
        "--hermes-home",
        default=str(Path.home() / ".hermes"),
        help="Hermes home directory (default: ~/.hermes)",
    )
    args = parser.parse_args()

    hermes_home = Path(args.hermes_home).expanduser().resolve()
    report = scan_stock_jobs(hermes_home)

    if not report["ok"]:
        sys.stderr.write(
            f"ERROR: Duplicate enabled stock jobs found across profiles: {json.dumps(report["duplicates"], ensure_ascii=False)}\n"
        )
        return 78

    return 0


if __name__ == "__main__":
    sys.exit(main())
