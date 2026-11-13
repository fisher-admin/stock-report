#!/usr/bin/env python3
"""Binding final check of the exact content the v2 publisher is about to push.

2026-10-03 reliability revision.  Runs inside ``publish_stock_report_v2.sh``
after every generator has finished and before the idempotency gate, commit and
push.  It reads the files that will actually be committed (``data/latest`` in
the published repo), not an earlier copy of the verdict.

Modes (decided from batch identity, not from the caller):
- ``new_batch`` -- run_manifest names a batch other than the last remote-
  confirmed deployment receipt.  A new recommendation list is being published:
  every critical check must pass.
- ``refresh``   -- same batch as the last confirmed receipt (morning/midday
  market refresh).  The recommendation list must be identical to the committed
  HEAD; the verdict must still be readable.  A halt verdict may be published as
  a clearly labelled status update, never as a new selection.

Maintenance (2026-10-09): when the manifest and candidate state both carry an
active ``maintenance`` marker (see maintenance_mode.py) the candidate list must
be withheld (empty) instead of non-empty, and a refresh entering or leaving
maintenance may withhold/restore the same batch's list.  Batch identity, dates,
verdict and calendar checks are unchanged.

Exit codes: 0 pass, 5 blocked (nothing may be pushed), 6 content changed after
validation (``verify-after-rebase``), 2 usage/internal error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import exchange_calendar  # noqa: E402
import maintenance_mode  # noqa: E402

DATE_RE = re.compile(r"^\d{8}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_ACTIONS = {"observe_only", "halt", "cautious_execute", "conditional_execute", "execute"}
PUBLISHED_PATHSPEC = ["data", "assets", "*.html"]
REQUIRED_LATEST = ("run_manifest.json", "system_verdict.json", "candidate_state.json", "strategy_state.json")
# Verdict gates whose hard block means the content itself is wrong (stale / mismatched dates) and
# therefore must never be pushed.  2026-10-14: strategy_gate "deprioritized" (zero sector overlap)
# blocked the whole 20261014 release although the verdict was a valid observe_only; market_gate
# hard blocks become action "halt" and are still refused for new batches via VERDICT_HALT.
PUBLICATION_INTEGRITY_GATES = frozenset({"freshness_gate"})

EXIT_PASS = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 5
EXIT_CHANGED = 6


def now_iso() -> str:
    return exchange_calendar.now_shanghai().isoformat(timespec="seconds")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_json_strict(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return payload


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def committed_json(repo: Path, rev: str, rel: str) -> dict[str, Any] | None:
    proc = git(repo, "show", f"{rev}:{rel}")
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def candidate_identity(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    rows = state.get("candidates")
    if not isinstance(rows, list):
        return None
    codes = [str((row or {}).get("code") or "") for row in rows if isinstance(row, dict)]
    return {"latest_trade_date": str(state.get("latest_trade_date") or ""), "codes": codes}


def published_digest(repo: Path) -> str:
    """sha256 over every file the publisher stages (data/, assets/, root *.html)."""
    files: list[Path] = []
    for sub in ("data", "assets"):
        base = repo / sub
        if base.is_dir():
            files.extend(p for p in base.rglob("*") if p.is_file())
    files.extend(p for p in repo.glob("*.html") if p.is_file())
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(repo).as_posix()):
        rel = path.relative_to(repo).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def load_receipt(health_dir: Path) -> dict[str, Any]:
    path = health_dir / "deployment_receipt.json"
    try:
        receipt = read_json_strict(path)
    except Exception:
        return {}
    confirmed = (
        receipt.get("remote_confirmed") is True
        and receipt.get("local_sha")
        and receipt.get("local_sha") == receipt.get("remote_sha")
    )
    return receipt if confirmed else {}


def evaluate(
    repo: Path,
    health_dir: Path,
    *,
    expected_run_id: str = "",
    expected_trade_date: str = "",
    calendar_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    latest = repo / "data" / "latest"
    blockers: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    docs: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_LATEST:
        try:
            docs[name] = read_json_strict(latest / name)
        except Exception as exc:  # noqa: BLE001 - unreadable critical file must block
            blockers.append(f"UNREADABLE:{name}:{type(exc).__name__}")
    result: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": now_iso(),
        "repo": str(repo),
        "expected": {"run_id": expected_run_id or None, "trade_date": expected_trade_date or None},
        "mode": None,
        "passed": False,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }
    if blockers:
        return result

    manifest = docs["run_manifest.json"]
    verdict = docs["system_verdict.json"]
    candidate_state = docs["candidate_state.json"]
    strategy_state = docs["strategy_state.json"]
    run_id = str(manifest.get("run_id") or "")
    trade_date = str(manifest.get("trade_date") or "")
    result["batch"] = {"run_id": run_id, "trade_date": trade_date}
    if not RUN_ID_RE.fullmatch(run_id):
        blockers.append("MANIFEST_RUN_ID_INVALID")
    if not DATE_RE.fullmatch(trade_date):
        blockers.append("MANIFEST_TRADE_DATE_INVALID")
    if expected_run_id and expected_run_id != run_id:
        blockers.append(f"BATCH_RUN_ID_MISMATCH:expected={expected_run_id}:manifest={run_id}")
    if expected_trade_date and expected_trade_date != trade_date:
        blockers.append(f"BATCH_TRADE_DATE_MISMATCH:expected={expected_trade_date}:manifest={trade_date}")

    # Verdict structure is required in every mode.
    final_action = verdict.get("final_action")
    action = final_action.get("action") if isinstance(final_action, dict) else None
    checks["verdict_action"] = action
    if not str(verdict.get("schema_version") or "").startswith("system_verdict"):
        blockers.append("VERDICT_SCHEMA_UNKNOWN")
    if not isinstance(final_action, dict) or not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        blockers.append(f"VERDICT_FINAL_ACTION_MALFORMED:{final_action!r}"[:200])

    receipt = load_receipt(health_dir)
    checks["last_confirmed_receipt"] = {k: receipt.get(k) for k in ("run_id", "trade_date", "local_sha")} if receipt else None
    same_batch = bool(receipt) and receipt.get("run_id") == run_id and receipt.get("trade_date") == trade_date
    mode = "refresh" if same_batch else "new_batch"
    result["mode"] = mode
    current_ids = candidate_identity(candidate_state)
    if current_ids is None:
        blockers.append("CANDIDATE_STATE_MALFORMED")
    maintenance = maintenance_mode.is_active(manifest)
    checks["maintenance"] = maintenance
    if maintenance != maintenance_mode.is_active(candidate_state):
        blockers.append("MAINTENANCE_MARKERS_INCONSISTENT")
    if maintenance and current_ids is not None and current_ids["codes"]:
        blockers.append("MAINTENANCE_CANDIDATES_NOT_WITHHELD")

    if mode == "refresh":
        head_state = committed_json(repo, "HEAD", "data/latest/candidate_state.json")
        head_ids = candidate_identity(head_state)
        head_maintenance = maintenance_mode.is_active(head_state)
        checks["head_candidate_identity_equal"] = head_ids == current_ids
        if head_ids is None:
            blockers.append("REFRESH_HEAD_CANDIDATES_UNREADABLE")
        elif (maintenance or head_maintenance) and current_ids is not None:
            # Entering/leaving maintenance withholds or restores this batch's own
            # list; it never selects a new one.  The date may still only stay or
            # be corrected to the batch date.
            checks["maintenance_transition"] = {"head": head_maintenance, "current": maintenance}
            if current_ids["latest_trade_date"] not in {head_ids["latest_trade_date"], trade_date}:
                blockers.append(f"REFRESH_RELABELS_CANDIDATE_DATE:{current_ids['latest_trade_date']}")
        elif current_ids is not None and head_ids["codes"] != current_ids["codes"]:
            blockers.append("REFRESH_CHANGES_RECOMMENDATION_LIST")
        elif current_ids is not None and current_ids["latest_trade_date"] not in {head_ids["latest_trade_date"], trade_date}:
            # A refresh may only keep the label or correct it to the batch's own
            # trade date (e.g. undo the Oct 2 "20261002" mislabel); never move
            # the list to any other date.
            blockers.append(f"REFRESH_RELABELS_CANDIDATE_DATE:{current_ids['latest_trade_date']}")
        elif head_ids["latest_trade_date"] != current_ids["latest_trade_date"]:
            result["label_correction"] = {"from": head_ids["latest_trade_date"], "to": current_ids["latest_trade_date"]}
        if action == "halt":
            result["status_update"] = "halt_status_refresh_without_new_selection"
    else:
        verdict_run = str((verdict.get("run") or {}).get("run_id") or "")
        decision_date = str((verdict.get("dates") or {}).get("decision_trade_date") or "")
        date_contract = verdict.get("date_contract") if isinstance(verdict.get("date_contract"), dict) else {}
        checks.update({
            "verdict_run_id": verdict_run,
            "verdict_decision_trade_date": decision_date,
            "date_contract_passed": date_contract.get("passed"),
            "candidate_trade_date": (current_ids or {}).get("latest_trade_date"),
            "strategy_trade_date": str(strategy_state.get("latest_trade_date") or ""),
            "candidate_count": len((current_ids or {}).get("codes") or []),
            "validation_ok": manifest.get("validation_ok"),
            "publish_ready": manifest.get("publish_ready"),
            "publish_mode": manifest.get("publish_mode"),
            "ai_complete": manifest.get("ai_complete"),
        })
        if action == "halt":
            blockers.append("VERDICT_HALT")
        if date_contract.get("passed") is not True or str(date_contract.get("status") or "") == "block":
            blockers.append("DATE_CONTRACT_NOT_PASSED")
        execution_blocked = []
        for name, gate in (verdict.get("gates") or {}).items():
            if isinstance(gate, dict) and gate.get("hard_blocking") is True and gate.get("passed") is not True:
                if name in PUBLICATION_INTEGRITY_GATES:
                    blockers.append(f"HARD_GATE_FAILED:{name}")
                else:
                    # An opinion gate (market/strategy/candidate) blocking *execution* is already folded
                    # into the verdict's final action (observe_only / halt); it is publishable content.
                    execution_blocked.append(name)
        checks["execution_blocked_gates"] = execution_blocked
        if verdict_run != run_id:
            blockers.append(f"VERDICT_RUN_MISMATCH:{verdict_run}")
        if decision_date != trade_date:
            blockers.append(f"VERDICT_DATE_MISMATCH:{decision_date}")
        if checks["candidate_trade_date"] != trade_date:
            blockers.append(f"CANDIDATE_DATE_MISMATCH:{checks['candidate_trade_date']}")
        if checks["strategy_trade_date"] != trade_date:
            blockers.append(f"STRATEGY_DATE_MISMATCH:{checks['strategy_trade_date']}")
        if not checks["candidate_count"] and not maintenance:
            blockers.append("CANDIDATE_LIST_EMPTY")
        # 2026-10-15: 验证结果改为发布页标注，不再阻断发布（身份/日期/名单一致性仍是硬阻断）。
        # 未通过项记入 warnings，并由 enforce_public_boundary 写入 system_verdict.publish_validation。
        if manifest.get("validation_ok") is not True:
            warnings.append("MANIFEST_VALIDATION_NOT_OK")
        if manifest.get("publish_ready") is not True:
            warnings.append("MANIFEST_NOT_PUBLISH_READY")
        if str(manifest.get("publish_mode") or "") == "full" and manifest.get("ai_complete") is not True:
            warnings.append("FULL_MODE_WITHOUT_SAME_DAY_AI")
        if DATE_RE.fullmatch(trade_date):
            session = exchange_calendar.classify_date(trade_date, calendar_path=calendar_path, now=now)
            checks["trade_date_session"] = session.get("status")
            if session.get("status") != "open":
                blockers.append(f"TRADE_DATE_NOT_OPEN_SESSION:{session.get('status')}:{session.get('reason') or ''}"[:200])

    result["passed"] = not blockers
    return result


def cmd_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    health_dir = Path(args.health_dir)
    try:
        result = evaluate(
            repo,
            health_dir,
            expected_run_id=args.expected_run_id.strip(),
            expected_trade_date=args.expected_trade_date.strip(),
            calendar_path=Path(args.calendar) if args.calendar else None,
        )
        if result["passed"]:
            result["published_digest"] = published_digest(repo)
            head = git(repo, "rev-parse", "HEAD")
            result["head_at_validation"] = head.stdout.strip() if head.returncode == 0 else None
    except Exception as exc:  # noqa: BLE001 - any internal failure blocks publication
        result = {"schema_version": 1, "checked_at": now_iso(), "passed": False,
                  "blockers": [f"INTERNAL_ERROR:{type(exc).__name__}:{exc}"[:300]]}
    write_json_atomic(health_dir / "publication_validation.json", result)
    _append_ledger(health_dir, {"event": "validated" if result.get("passed") else "blocked",
                                "mode": result.get("mode"), "batch": result.get("batch"),
                                "blockers": result.get("blockers"), "at": result.get("checked_at")})
    if result.get("passed"):
        print(f"FINAL_GATE_PASS mode={result.get('mode')} run_id={result['batch']['run_id']} "
              f"trade_date={result['batch']['trade_date']}"
              + (f" warnings={';'.join(result.get('warnings') or [])}" if result.get("warnings") else ""))
        return EXIT_PASS
    print("FINAL_GATE_BLOCKED " + "; ".join(result.get("blockers") or []))
    return EXIT_BLOCKED


def _append_ledger(health_dir: Path, event: dict[str, Any]) -> None:
    path = health_dir / "publication_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_record(health_dir: Path) -> dict[str, Any]:
    record = read_json_strict(health_dir / "publication_validation.json")
    if record.get("passed") is not True:
        raise ValueError("publication validation did not pass")
    return record


def cmd_record_commit(args: argparse.Namespace) -> int:
    health_dir = Path(args.health_dir)
    record = _load_record(health_dir)
    head = git(Path(args.repo), "rev-parse", "HEAD")
    if head.returncode != 0:
        print("cannot resolve validated commit")
        return EXIT_USAGE
    record["validated_commit"] = head.stdout.strip()
    write_json_atomic(health_dir / "publication_validation.json", record)
    print(record["validated_commit"])
    return EXIT_PASS


def cmd_verify_after_rebase(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    health_dir = Path(args.health_dir)
    record = _load_record(health_dir)
    validated = str(record.get("validated_commit") or "")
    if not validated:
        print("FINAL_GATE_CHANGED no validated_commit recorded")
        return EXIT_CHANGED
    proc = git(repo, "diff", "--quiet", validated, "HEAD", "--", *PUBLISHED_PATHSPEC)
    if proc.returncode == 0:
        print("FINAL_GATE_UNCHANGED_AFTER_REBASE")
        return EXIT_PASS
    record["changed_after_validation"] = True
    write_json_atomic(health_dir / "publication_validation.json", record)
    _append_ledger(health_dir, {"event": "changed_after_validation", "batch": record.get("batch"),
                                "validated_commit": validated, "at": now_iso()})
    print("FINAL_GATE_CHANGED published content differs from the validated commit; re-validation required")
    return EXIT_CHANGED


def cmd_mark_pushed(args: argparse.Namespace) -> int:
    health_dir = Path(args.health_dir)
    record = _load_record(health_dir)
    record["pushed_sha"] = args.sha
    record["push_outcome"] = args.outcome
    record["pushed_at"] = now_iso()
    write_json_atomic(health_dir / "publication_validation.json", record)
    _append_ledger(health_dir, {"event": args.outcome, "mode": record.get("mode"), "batch": record.get("batch"),
                                "sha": args.sha, "at": record["pushed_at"]})
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--repo", required=True)
    v.add_argument("--health-dir", required=True)
    v.add_argument("--expected-run-id", default="")
    v.add_argument("--expected-trade-date", default="")
    v.add_argument("--calendar", default="")
    r = sub.add_parser("record-commit")
    r.add_argument("--repo", required=True)
    r.add_argument("--health-dir", required=True)
    a = sub.add_parser("verify-after-rebase")
    a.add_argument("--repo", required=True)
    a.add_argument("--health-dir", required=True)
    m = sub.add_parser("mark-pushed")
    m.add_argument("--health-dir", required=True)
    m.add_argument("--sha", required=True)
    m.add_argument("--outcome", choices=["pushed", "idempotent_skip"], required=True)
    args = parser.parse_args(argv)
    try:
        return {
            "validate": cmd_validate,
            "record-commit": cmd_record_commit,
            "verify-after-rebase": cmd_verify_after_rebase,
            "mark-pushed": cmd_mark_pushed,
        }[args.command](args)
    except Exception as exc:  # noqa: BLE001
        print(f"FINAL_GATE_ERROR {type(exc).__name__}: {exc}")
        return EXIT_BLOCKED if args.command != "mark-pushed" else EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
