#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
TASK_HEADER_RE = re.compile(r"^###\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^- ([^：]+)：\s*(.*)$")
LOG_PATH_RE = re.compile(
    r"(?<!\S)(?P<path>(?:/[^\s`]+|[^\s`]+\.(?:log|txt|json|csv|md|py|js|ts|tsx|jsx|sh|yaml|yml|toml|parquet)))(?=$|[\s,.;])"
)

NON_EVIDENCE_PATH_PATTERNS = (
    "memory/",
    "ACTIVE_TASKS.md",
)

CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".yaml", ".yml", ".toml", ".json"}
RESULT_SUFFIXES = {".json", ".csv", ".parquet", ".md", ".txt", ".log"}


@dataclass
class TaskCard:
    task_id: str
    fields: dict[str, str]

    @property
    def status(self) -> str:
        return self.fields.get("状态", "").strip().lower()

    @property
    def objective(self) -> str:
        return self.fields.get("目标", "").strip()

    @property
    def next_action(self) -> str:
        return self.fields.get("下一条具体动作", "").strip()

    @property
    def evidence_target(self) -> str:
        return self.fields.get("证据目标", "").strip()

    @property
    def updated_text(self) -> str:
        return self.fields.get("最近更新时间", "").strip()


@dataclass
class EvidenceSummary:
    code_paths: list[Path]
    result_paths: list[Path]
    missing_paths: list[Path]
    newest_code_at: datetime | None
    newest_result_at: datetime | None

    @property
    def reference_at(self) -> datetime | None:
        if self.newest_result_at is not None:
            return self.newest_result_at
        return self.newest_code_at


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check ACTIVE_TASKS.md for stalled tasks.")
    parser.add_argument("--workspace", type=Path, required=True, help="OpenClaw workspace path.")
    parser.add_argument("--stale-minutes", type=int, default=20, help="Minutes without evidence before nagging.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional report directory. Defaults to <workspace>/ops/task_watchdog",
    )
    return parser.parse_args()


def parse_task_cards(text: str) -> list[TaskCard]:
    cards: list[TaskCard] = []
    current_id: str | None = None
    current_fields: dict[str, str] = {}
    in_active = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            if line == "## Active":
                in_active = True
                continue
            if in_active:
                break
        if not in_active:
            continue
        header_match = TASK_HEADER_RE.match(line)
        if header_match:
            if current_id:
                cards.append(TaskCard(task_id=current_id, fields=current_fields))
            current_id = header_match.group(1).strip()
            current_fields = {}
            continue
        field_match = FIELD_RE.match(line)
        if current_id and field_match:
            current_fields[field_match.group(1).strip()] = field_match.group(2).strip()

    if current_id:
        cards.append(TaskCard(task_id=current_id, fields=current_fields))
    return cards


def extract_paths(text: str, workspace: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()

    for token in re.findall(r"`([^`]+)`", text):
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key not in seen:
            seen.add(key)
            paths.append(candidate)

    for match in LOG_PATH_RE.finditer(text):
        token = match.group("path").strip().strip(",.;")
        if "/" not in token and "." not in Path(token).name:
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key not in seen:
            seen.add(key)
            paths.append(candidate)
    return paths


def classify_path(path: Path, workspace: Path) -> str:
    rel = str(path)
    try:
        rel = str(path.resolve().relative_to(workspace.resolve()))
    except Exception:
        rel = str(path)

    if any(pattern in rel for pattern in NON_EVIDENCE_PATH_PATTERNS):
        return "ignore"
    if path.suffix in CODE_SUFFIXES and ("skills/" in rel or "/skills/" in rel or "/ops/" in rel or "ops/" in rel):
        return "code"
    if path.suffix in RESULT_SUFFIXES:
        return "result"
    return "other"


def newest_mtime(paths: Iterable[Path]) -> datetime | None:
    newest: datetime | None = None
    for path in paths:
        if not path.exists():
            continue
        dt = datetime.fromtimestamp(path.stat().st_mtime, tz=TZ)
        if newest is None or dt > newest:
            newest = dt
    return newest


def summarize_evidence(card: TaskCard, workspace: Path) -> EvidenceSummary:
    raw_paths = extract_paths(card.evidence_target, workspace)
    code_paths: list[Path] = []
    result_paths: list[Path] = []
    missing_paths: list[Path] = []

    for path in raw_paths:
        kind = classify_path(path, workspace)
        if kind == "ignore":
            continue
        if not path.exists():
            missing_paths.append(path)
            continue
        if kind == "code":
            code_paths.append(path)
        elif kind == "result":
            result_paths.append(path)

    return EvidenceSummary(
        code_paths=code_paths,
        result_paths=result_paths,
        missing_paths=missing_paths,
        newest_code_at=newest_mtime(code_paths),
        newest_result_at=newest_mtime(result_paths),
    )


def format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def format_paths(paths: Iterable[Path], workspace: Path) -> list[str]:
    out: list[str] = []
    for path in paths:
        try:
            out.append(str(path.resolve().relative_to(workspace.resolve())))
        except Exception:
            out.append(str(path))
    return out


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    tasks_path = workspace / "memory/core/ACTIVE_TASKS.md"
    report_dir = args.output_dir.resolve() if args.output_dir else (workspace / "ops/task_watchdog")
    report_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(TZ)
    board_text = tasks_path.read_text(encoding="utf-8")
    cards = parse_task_cards(board_text)
    board_mtime = datetime.fromtimestamp(tasks_path.stat().st_mtime, tz=TZ)

    task_reports = []
    stalled = []
    for card in cards:
        if card.status != "active":
            continue

        evidence = summarize_evidence(card, workspace)
        reference = evidence.reference_at
        idle_minutes = int((now - reference).total_seconds() // 60) if reference else 10**9
        reasons = []
        severity = "ok"

        if not card.next_action:
            reasons.append("缺少下一条具体动作")
            severity = "warning"
        if not card.evidence_target:
            reasons.append("缺少证据目标")
            severity = "blocking"
        if not evidence.result_paths:
            reasons.append("没有可识别的结果型证据路径")
            severity = "blocking"
        if evidence.missing_paths and not evidence.result_paths:
            reasons.append("证据目标文件尚不存在")
            severity = "blocking"
        if reference is None:
            reasons.append("没有任何可用证据更新时间")
            severity = "blocking"
        elif idle_minutes >= args.stale_minutes:
            reasons.append(f"超过 {args.stale_minutes} 分钟没有结果文件或代码产物更新")
            severity = "blocking"

        report = {
            "taskId": card.task_id,
            "objective": card.objective,
            "status": card.status,
            "nextAction": card.next_action,
            "evidenceTarget": card.evidence_target,
            "boardUpdatedAt": format_dt(board_mtime),
            "newestCodeAt": format_dt(evidence.newest_code_at),
            "newestResultAt": format_dt(evidence.newest_result_at),
            "referenceAt": format_dt(reference),
            "idleMinutes": idle_minutes,
            "severity": severity,
            "codePaths": format_paths(evidence.code_paths, workspace),
            "resultPaths": format_paths(evidence.result_paths, workspace),
            "missingPaths": format_paths(evidence.missing_paths, workspace),
            "reasons": reasons,
        }
        task_reports.append(report)
        if reasons:
            stalled.append(report)

    result = {
        "checkedAt": format_dt(now),
        "workspace": str(workspace),
        "staleThresholdMinutes": args.stale_minutes,
        "boardUpdatedAt": format_dt(board_mtime),
        "activeTaskCount": len(task_reports),
        "stalledTaskCount": len(stalled),
        "needsNag": bool(stalled),
        "tasks": task_reports,
        "policy": {
            "freshnessSource": "code/result evidence only",
            "boardMtimeCountsAsProgress": False,
            "diaryOrTaskBoardPathsCountAsEvidence": False,
        },
    }

    json_path = report_dir / "latest.json"
    md_path = report_dir / "latest.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"checked_at: {result['checkedAt']}",
        f"board_updated_at: {result['boardUpdatedAt']}",
        f"active_tasks: {result['activeTaskCount']}",
        f"stalled_tasks: {result['stalledTaskCount']}",
        f"needs_nag: {str(result['needsNag']).lower()}",
        "policy: freshness=code/result evidence only; board/diary updates do NOT count",
        "",
    ]
    if stalled:
        for item in stalled:
            lines.extend(
                [
                    f"- {item['taskId']}: {item['objective']}",
                    f"  - severity: {item['severity']}",
                    f"  - idle_minutes: {item['idleMinutes']}",
                    f"  - reference_at: {item['referenceAt']}",
                    f"  - newest_code_at: {item['newestCodeAt']}",
                    f"  - newest_result_at: {item['newestResultAt']}",
                    f"  - next_action: {item['nextAction'] or 'n/a'}",
                    f"  - result_paths: {', '.join(item['resultPaths']) or 'n/a'}",
                    f"  - code_paths: {', '.join(item['codePaths']) or 'n/a'}",
                    f"  - reasons: {'; '.join(item['reasons'])}",
                ]
            )
    else:
        lines.append("- all active tasks have fresh code/result evidence within threshold")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
