#!/usr/bin/env python3
"""Run and record a complete local Qlib sidecar round."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
PYTHON = ROOT / "qlib_lab/venv/bin/python"
TZ = ZoneInfo("Asia/Shanghai")


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def resolve_optional_manifest(root: Path, overlay_manifest: Path | None) -> Path | None:
    if overlay_manifest is None:
        return None
    candidate = overlay_manifest.expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"overlay manifest not found: {candidate}")
    return candidate


def build_steps(root: Path, python: Path, round_name: str, overlay_manifest: Path | None = None) -> list[dict[str, object]]:
    adapter = root / "scripts/qlib_adapter"
    record_cmd = [
        str(python),
        str(adapter / "record_factor_factory_round.py"),
        "--round-name",
        round_name,
    ]
    if overlay_manifest is not None:
        record_cmd.extend(["--overlay-manifest", str(overlay_manifest)])
    return [
        {
            "name": "export_panel_to_qlib",
            "cmd": [str(python), str(adapter / "export_panel_to_qlib.py")],
        },
        {
            "name": "build_qlib_dataset_config",
            "cmd": [str(python), str(adapter / "build_qlib_dataset_config.py")],
        },
        {
            "name": "run_qlib_factor_arena",
            "cmd": [str(python), str(adapter / "run_qlib_factor_arena.py")],
        },
        {
            "name": "sync_qlib_results_to_factor_factory",
            "cmd": [str(python), str(adapter / "sync_qlib_results_to_factor_factory.py")],
        },
        {
            "name": "record_factor_factory_round",
            "cmd": record_cmd,
        },
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=PYTHON)
    parser.add_argument("--overlay-manifest", type=Path, default=None)
    parser.add_argument("--round-name", default=f"factor_factory_round_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=ROOT / "outputs/evolution/qlib_sidecar_round_latest.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overlay_manifest = resolve_optional_manifest(args.root, args.overlay_manifest)
    results = []
    for step in build_steps(args.root, args.python, args.round_name, overlay_manifest=overlay_manifest):
        started_at = iso_now()
        proc = subprocess.run(
            step["cmd"],
            cwd=args.root,
            text=True,
            capture_output=True,
            check=False,
        )
        result = {
            "name": step["name"],
            "cmd": step["cmd"],
            "started_at": started_at,
            "finished_at": iso_now(),
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
        results.append(result)
        if proc.returncode != 0:
            break

    summary = {
        "generated_at": iso_now(),
        "round_name": args.round_name,
        "status": "ok" if all(item["returncode"] == 0 for item in results) and len(results) == 5 else "failed",
        "overlay_manifest": str(overlay_manifest) if overlay_manifest is not None else None,
        "steps": results,
    }
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
