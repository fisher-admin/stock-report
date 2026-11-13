#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Factor Factory - Phase 1 orchestrator

串联执行：
1. scripts/build_universe.py
2. scripts/build_factor_panel.py
3. scripts/run_factor_audit.py
4. scripts/qlib_adapter/run_qlib_sidecar_round.py

特性：
- 基本异常捕获
- 分阶段日志落盘
- 结果摘要 JSON 输出
- 支持从指定阶段开始 / 在指定阶段结束
- 默认将每轮结果同步写入本地 Qlib Recorder
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")


@dataclass
class StageResult:
    name: str
    command: list[str]
    returncode: int | None
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    ok: bool
    stdout_tail: str
    stderr_tail: str
    log_path: str
    error: str | None = None
    artifact_hint: dict[str, Any] | None = None


@dataclass
class PipelineSummary:
    status: str
    root: str
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    stages: list[StageResult]
    failed_stage: str | None
    summary_path: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ensure_root(root: Path) -> None:
    if not root.exists():
        raise FileNotFoundError(f"factor_factory root not found: {root}")
    required = [
        root / "scripts" / "build_universe.py",
        root / "scripts" / "build_factor_panel.py",
        root / "scripts" / "run_factor_audit.py",
        root / "config" / "phase1.yaml",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing required files: " + ", ".join(missing))


def resolve_optional_path(root: Path, raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"optional path not found: {candidate}")
    return candidate


def build_stage_specs(
    root: Path,
    python_exec: str,
    include_qlib: bool = True,
    overlay_manifest: Path | None = None,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [
        {
            "name": "build_universe",
            "script": root / "scripts" / "build_universe.py",
            "artifacts": [
                root / "outputs" / "factor_registry" / "universe_summary.json",
            ],
        },
        {
            "name": "build_factor_panel",
            "script": root / "scripts" / "build_factor_panel.py",
            "artifacts": [
                root / "data" / "factors" / "panel_phase1.parquet",
                root / "data" / "prepared" / "base_factor_exposure.parquet",
                root / "outputs" / "candidate_lists" / "phase1_candidate_catalog.json",
            ],
        },
    ]
    audit_command = [
        python_exec,
        str(root / "scripts" / "run_factor_audit.py"),
        "--root",
        str(root),
    ]
    if overlay_manifest is not None:
        audit_command.extend(["--overlay-manifest", str(overlay_manifest)])
    specs.append(
        {
            "name": "run_factor_audit",
            "script": root / "scripts" / "run_factor_audit.py",
            "command": audit_command,
            "artifacts": [
                root / "outputs" / "audit_reports" / "factor_audit_phase1.json",
                root / "outputs" / "candidate_lists" / "factor_whitelist_phase1.json",
                root / "outputs" / "candidate_lists" / "factor_coldlist_phase1.json",
            ],
        }
    )
    if include_qlib:
        qlib_python = root / "qlib_lab" / "venv" / "bin" / "python"
        qlib_command = [
            str(qlib_python),
            str(root / "scripts" / "qlib_adapter" / "run_qlib_sidecar_round.py"),
            "--root",
            str(root),
            "--python",
            str(qlib_python),
        ]
        if overlay_manifest is not None:
            qlib_command.extend(["--overlay-manifest", str(overlay_manifest)])
        specs.append(
            {
                "name": "qlib_sidecar_round",
                "command": qlib_command,
                "artifacts": [
                    root / "outputs" / "evolution" / "qlib_sidecar_round_latest.json",
                    root / "outputs" / "evolution" / "qlib_recorder_sync_latest.json",
                    root / "outputs" / "evolution" / "qlib_factor_scorecard.json",
                    root / "qlib_lab" / "experiments" / "qlib_factor_arena_report.json",
                ],
            }
        )
    return specs


def stage_slice(specs: list[dict[str, Any]], start_from: str | None, stop_after: str | None) -> list[dict[str, Any]]:
    names = [s["name"] for s in specs]
    start_idx = 0
    end_idx = len(specs)
    if start_from:
        if start_from not in names:
            raise ValueError(f"Unknown start stage: {start_from}")
        start_idx = names.index(start_from)
    if stop_after:
        if stop_after not in names:
            raise ValueError(f"Unknown stop stage: {stop_after}")
        end_idx = names.index(stop_after) + 1
    if start_idx >= end_idx:
        raise ValueError("Invalid stage range: start_from is after stop_after")
    return specs[start_idx:end_idx]


def tail_text(text: str, lines: int = 40) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def detect_artifacts(artifacts: list[Path]) -> dict[str, Any]:
    info: dict[str, Any] = {"exists": [], "missing": []}
    for path in artifacts:
        if path.exists():
            item = {"path": str(path)}
            try:
                item["size"] = path.stat().st_size
            except Exception:
                pass
            info["exists"].append(item)
        else:
            info["missing"].append(str(path))
    return info


def run_stage(root: Path, spec: dict[str, Any], python_exec: str, logs_dir: Path) -> StageResult:
    if "command" in spec:
        command = [str(part) for part in spec["command"]]
    else:
        command = [python_exec, str(spec["script"]), "--root", str(root)]
    started = datetime.now(timezone.utc)
    proc = subprocess.run(command, capture_output=True, text=True)
    finished = datetime.now(timezone.utc)
    duration = (finished - started).total_seconds()

    log_path = logs_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{spec['name']}.log"
    log_payload = {
        "stage": spec["name"],
        "command": command,
        "returncode": proc.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": duration,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    log_path.write_text(json.dumps(log_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts = detect_artifacts(spec.get("artifacts", []))
    ok = proc.returncode == 0 and not artifacts.get("missing")
    error = None
    if proc.returncode != 0:
        error = f"stage exited with code {proc.returncode}"
    elif artifacts.get("missing"):
        error = "expected artifacts missing"

    return StageResult(
        name=spec["name"],
        command=command,
        returncode=proc.returncode,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_seconds=duration,
        ok=ok,
        stdout_tail=tail_text(proc.stdout),
        stderr_tail=tail_text(proc.stderr),
        log_path=str(log_path),
        error=error,
        artifact_hint=artifacts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Factor Factory Phase 1 pipeline")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="factor_factory root directory")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used for stage scripts")
    parser.add_argument(
        "--overlay-manifest",
        default="",
        help="Optional run-scoped overlay manifest to thread into the Qlib sidecar/recorder contract",
    )
    parser.add_argument("--start-from", default=None, help="Optional stage name to start from")
    parser.add_argument("--stop-after", default=None, help="Optional stage name to stop after")
    parser.add_argument("--fail-fast", action="store_true", default=True, help="Stop immediately on first failure")
    parser.add_argument(
        "--skip-qlib-sidecar",
        action="store_true",
        help="Emergency switch: do not sync this pipeline round into local Qlib",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    ensure_root(root)
    overlay_manifest = resolve_optional_path(root, args.overlay_manifest)

    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    summary_dir = root / "outputs" / "audit_reports"
    summary_dir.mkdir(parents=True, exist_ok=True)

    pipeline_started = datetime.now(timezone.utc)
    stages: list[StageResult] = []
    failed_stage: str | None = None

    try:
        specs = build_stage_specs(
            root,
            args.python,
            include_qlib=not args.skip_qlib_sidecar,
            overlay_manifest=overlay_manifest,
        )
        run_specs = stage_slice(specs, args.start_from, args.stop_after)

        for spec in run_specs:
            result = run_stage(root, spec, args.python, logs_dir)
            stages.append(result)
            if not result.ok:
                failed_stage = result.name
                if args.fail_fast:
                    break

        pipeline_finished = datetime.now(timezone.utc)
        duration = (pipeline_finished - pipeline_started).total_seconds()
        status = "ok" if failed_stage is None else "error"

        summary = PipelineSummary(
            status=status,
            root=str(root),
            started_at=pipeline_started.isoformat(),
            finished_at=pipeline_finished.isoformat(),
            duration_seconds=duration,
            stages=stages,
            failed_stage=failed_stage,
            summary_path="",
        )

    except Exception as exc:
        pipeline_finished = datetime.now(timezone.utc)
        duration = (pipeline_finished - pipeline_started).total_seconds()
        fail_log = logs_dir / f"{pipeline_started.strftime('%Y%m%dT%H%M%SZ')}-pipeline-exception.log"
        fail_log.write_text(traceback.format_exc(), encoding="utf-8")
        stages.append(
            StageResult(
                name="pipeline_exception",
                command=[],
                returncode=None,
                started_at=pipeline_started.isoformat(),
                finished_at=pipeline_finished.isoformat(),
                duration_seconds=duration,
                ok=False,
                stdout_tail="",
                stderr_tail=tail_text(str(exc)),
                log_path=str(fail_log),
                error=str(exc),
                artifact_hint=None,
            )
        )
        summary = PipelineSummary(
            status="error",
            root=str(root),
            started_at=pipeline_started.isoformat(),
            finished_at=pipeline_finished.isoformat(),
            duration_seconds=duration,
            stages=stages,
            failed_stage="pipeline_exception",
            summary_path="",
        )

    summary_path = summary_dir / f"factor_factory_pipeline_summary_{pipeline_started.strftime('%Y%m%dT%H%M%SZ')}.json"
    serializable = asdict(summary)
    serializable["stages"] = [asdict(s) for s in summary.stages]
    serializable["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(serializable, ensure_ascii=False, indent=2))
    return 0 if serializable["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
