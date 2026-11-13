#!/usr/bin/env python3
"""Public maintenance mode for the v2 publisher (20261009).

While the system is being recalibrated the public site must not show stock-level
picks.  When enabled, ``apply`` runs after the strict contract check (which still
validates the real batch) and before the Pages preflight / final gate:

* every list of stock rows and every stock-code-keyed mapping in the publishable
  ``data/`` JSON is withheld (replaced by an empty list / removed);
* a ``maintenance`` block with the neutral calibration notice is stamped on the
  run manifest, verdict, candidate state and dual-track banners;
* any stock code left anywhere in the publishable data aborts the publish
  (fail closed: rather not publish than publish picks).

Switch (environment wins over file):
  STOCK_SYSTEM_MAINTENANCE_MODE=true|false
  $STOCK_SYSTEM_WORKSPACE/config/stock_maintenance_mode.json  {"enabled": true, ...}

CLI:  status | enable [--title T] [--message M] | disable | apply [--repo PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FLAG_ENV = "STOCK_SYSTEM_MAINTENANCE_MODE"
_WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", str(Path(__file__).resolve().parents[3])))
DEFAULT_FLAG_FILE = _WORKSPACE / "config" / "stock_maintenance_mode.json"
DEFAULT_REPO = Path(os.environ.get("OPENCLAW_PUBLISHED_REPO", str(_WORKSPACE / "stock-report")))
SH_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_TITLE = "系统校准升级中"
DEFAULT_TITLE_EN = "System under calibration — upgrading factor models"
DEFAULT_MESSAGE = (
    "选股系统正在进行计划内的数据与因子模型校准升级。校准期间暂停公开个股候选与观察名单，"
    "市场与系统运行状态照常更新，恢复后将照常发布。"
)

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", ""}
STOCK_ID_KEYS = ("ts_code", "code", "normalized_code", "stock_code", "symbol", "ticker")
STOCK_CODE = re.compile(r"^\d{6}(?:\.(?:SZ|SH|BJ))?$")
SUFFIXED_CODE_IN_TEXT = re.compile(r"\b\d{6}\.(?:SZ|SH|BJ)\b")
BARE_A_SHARE_CODE = re.compile(r"^[03468-9]\d{5}$")
# Single stock references outside row lists, e.g. ``represent_stock_name``.
STOCK_FIELD = re.compile(r"stock_(?:name|code)s?$")
STOCK_NAME_KEYS = ("name", "stock_name", "sec_name", "represent_stock_name")
REDACTED_TEXT = "（系统校准期间暂停发布个股相关内容）"
# Counts describing the withheld pick lists (not review aggregates or market statistics).
PICK_COUNT_KEYS = {
    "total_execution_count", "main_count", "watch_count", "avoid_count", "consensus_in_execution",
    "candidate_count", "eligible_candidate_count", "event_candidate_count", "top20_count",
}
PICK_COUNT_MAPS = {"role_counts", "candidate_role_counts", "strategy_counts", "top20_ready"}
MARKED_FILES = (
    "data/latest/run_manifest.json",
    "data/latest/system_verdict.json",
    "data/latest/candidate_state.json",
    "data/latest/recommendation_state.json",
    "data/latest/dual_track_state.json",
    "data/latest/prebreakout_shadow_watch.json",
)
BANNER_FILES = ("data/latest/dual_track_state.json", "data/latest/prebreakout_shadow_watch.json")
EXECUTING_ACTIONS = {"cautious_execute", "conditional_execute", "execute"}


class MaintenanceConfigError(ValueError):
    pass


class MaintenanceScrubError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(SH_TZ).isoformat(timespec="seconds")


def resolve_state(env: dict[str, str] | None = None, flag_file: Path | None = None) -> dict[str, Any]:
    """Resolved switch; malformed configuration raises (the publisher then stops)."""
    env = os.environ if env is None else env
    flag_file = DEFAULT_FLAG_FILE if flag_file is None else flag_file
    config: dict[str, Any] = {}
    if flag_file.exists():
        try:
            config = json.loads(flag_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MaintenanceConfigError(f"unreadable maintenance flag file {flag_file}: {exc}") from exc
        if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
            raise MaintenanceConfigError(f"maintenance flag file {flag_file} needs a boolean 'enabled'")

    raw = env.get(FLAG_ENV)
    if raw is not None:
        value = raw.strip().lower()
        if value in TRUE_VALUES:
            enabled, source = True, "env"
        elif value in FALSE_VALUES:
            enabled, source = False, "env"
        else:
            raise MaintenanceConfigError(f"{FLAG_ENV}={raw!r} is not a boolean")
    elif config:
        enabled, source = bool(config["enabled"]), "file"
    else:
        enabled, source = False, "default"

    return {
        "enabled": enabled,
        "source": source,
        "title": str(config.get("title") or DEFAULT_TITLE),
        "title_en": str(config.get("title_en") or DEFAULT_TITLE_EN),
        "message": str(config.get("message") or DEFAULT_MESSAGE),
        "since": config.get("since"),
    }


def _is_stock_row(item: Any) -> bool:
    return isinstance(item, dict) and any(
        isinstance(item.get(key), str) and STOCK_CODE.match(item[key].strip()) for key in STOCK_ID_KEYS
    )


def _is_code_list(items: list[Any]) -> bool:
    """["002863.SZ", ...] or a list made only of bare A-share codes (YYYYMM etc. start with 2)."""
    strings = [item for item in items if isinstance(item, str)]
    if not strings:
        return False
    if any(SUFFIXED_CODE_IN_TEXT.fullmatch(item.strip()) for item in strings):
        return True
    return len(strings) == len(items) and all(BARE_A_SHARE_CODE.match(item.strip()) for item in strings)


def _collect_names(obj: Any, names: set[str]) -> None:
    """Top-level name fields of withheld stock rows only (not nested factor/strategy names)."""
    rows = obj if isinstance(obj, list) else [obj]
    for row in rows:
        if _is_stock_row(row):
            names.update(row[key].strip() for key in STOCK_NAME_KEYS if isinstance(row.get(key), str))


def scrub(obj: Any, names: set[str] | None = None) -> tuple[Any, int]:
    """Withhold stock-level rows. Returns (scrubbed copy, number of rows/keys withheld).

    Names of withheld stocks are added to ``names`` so free text can be redacted.
    """
    names = set() if names is None else names
    if isinstance(obj, list):
        if any(_is_stock_row(item) for item in obj) or _is_code_list(obj):
            _collect_names(obj, names)
            return [], len(obj)
        out, removed = [], 0
        for item in obj:
            value, count = scrub(item, names)
            out.append(value)
            removed += count
        return out, removed
    if isinstance(obj, dict):
        out, removed = {}, 0
        for key, value in obj.items():
            if isinstance(key, str) and (STOCK_CODE.match(key) or STOCK_FIELD.search(key)):
                _collect_names(value, names)
                if isinstance(value, str) and STOCK_FIELD.search(key) and "name" in key:
                    names.add(value.strip())
                removed += 1
                continue
            value, count = scrub(value, names)
            out[key] = value
            removed += count
        return out, removed
    return obj, 0


def redact_names(obj: Any, names: set[str]) -> tuple[Any, int]:
    """Replace any text that mentions a withheld stock by the neutral notice."""
    if isinstance(obj, list):
        out, hits = [], 0
        for item in obj:
            value, count = redact_names(item, names)
            out.append(value)
            hits += count
        return out, hits
    if isinstance(obj, dict):
        out, hits = {}, 0
        for key, value in obj.items():
            value, count = redact_names(value, names)
            out[key] = value
            hits += count
        return out, hits
    if isinstance(obj, str) and names and any(name in obj for name in names):
        return REDACTED_TEXT, 1
    return obj, 0


def _usable_names(names: set[str]) -> set[str]:
    return {name for name in names if len(name) >= 2 and not STOCK_CODE.match(name)}


def residual_stock_refs(obj: Any) -> int:
    """Stock identifiers still present after scrubbing (structured or embedded in text)."""
    if isinstance(obj, list):
        return sum(residual_stock_refs(item) for item in obj)
    if isinstance(obj, dict):
        count = sum(1 for key in obj if isinstance(key, str) and STOCK_CODE.match(key))
        for key, value in obj.items():
            if key in STOCK_ID_KEYS and isinstance(value, str) and STOCK_CODE.match(value.strip()):
                count += 1
            count += residual_stock_refs(value)
        return count
    if isinstance(obj, str):
        return len(SUFFIXED_CODE_IN_TEXT.findall(obj))
    return 0


def publishable_data_files(repo: Path) -> list[str]:
    """Files under data/ that the publisher's ``git add data/`` would commit."""
    def ls(*args: str) -> list[str]:
        proc = subprocess.run(["git", "-C", str(repo), "ls-files", *args, "--", "data"],
                              capture_output=True, text=True, check=True)
        return [line for line in proc.stdout.splitlines() if line]
    tracked = ls()
    untracked = ls("--others", "--exclude-standard")
    return sorted({rel for rel in tracked + untracked if (repo / rel).is_file()})


def _write_json_like(path: Path, payload: Any, original_text: str) -> None:
    indent = 2 if original_text.lstrip().startswith(("{\n", "[\n")) or "\n  " in original_text[:200] else None
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    if original_text.endswith("\n"):
        text += "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def zero_pick_counts(obj: Any) -> Any:
    """Zero the counters that describe withheld pick lists so pages do not show "20 观察"."""
    if isinstance(obj, list):
        return [zero_pick_counts(item) for item in obj]
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in PICK_COUNT_KEYS and _is_number(value):
                out[key] = 0
            elif key in PICK_COUNT_MAPS and isinstance(value, dict):
                out[key] = {k: (0 if _is_number(v) else v) for k, v in value.items()}
            else:
                out[key] = zero_pick_counts(value)
        return out
    return obj


def _stamp(rel: str, payload: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    payload["maintenance"] = dict(marker)
    if rel.endswith("system_verdict.json"):
        action = payload.get("final_action")
        if isinstance(action, dict):
            action = dict(action)
            if action.get("action") in EXECUTING_ACTIONS:
                action["action"] = "observe_only"
            action["maintenance"] = True
            action["label"] = marker["title"]
            payload["final_action"] = action
    if rel in BANNER_FILES:
        payload["honesty_banner"] = f"{marker['title']}：{marker['message']}"
    return payload


def apply(repo: Path, state: dict[str, Any], *, applied_at: str | None = None) -> dict[str, Any]:
    """Withhold stock-level data in ``repo`` according to ``state``."""
    summary: dict[str, Any] = {"enabled": bool(state.get("enabled")), "source": state.get("source")}
    if not state.get("enabled"):
        return summary
    marker = {
        "enabled": True,
        "status": "calibrating",
        "title": state["title"],
        "title_en": state["title_en"],
        "message": state["message"],
        "since": state.get("since"),
        "applied_at": applied_at or now_iso(),
        "withheld": "stock_level_lists",
    }
    residual: dict[str, int] = {}
    names: set[str] = set()
    docs: dict[str, tuple[str, Any, Any]] = {}
    withheld_total = 0
    # Pass 1: withhold stock rows everywhere and learn the withheld names.
    for rel in publishable_data_files(repo):
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        if not rel.endswith(".json"):
            count = len(SUFFIXED_CODE_IN_TEXT.findall(text))
            if count:
                residual[rel] = count  # cannot scrub non-JSON safely
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MaintenanceScrubError(f"{rel} is not valid JSON: {exc}") from exc
        scrubbed, withheld = scrub(payload, names)
        withheld_total += withheld
        docs[rel] = (text, payload, scrubbed)

    # Pass 2: redact free text naming a withheld stock (AI advice etc.), stamp markers, verify, write.
    names = _usable_names(names)
    final: dict[str, tuple[str, Any]] = {}
    redacted_total = 0
    for rel, (text, payload, scrubbed) in docs.items():
        scrubbed, redacted = redact_names(scrubbed, names)
        redacted_total += redacted
        scrubbed = zero_pick_counts(scrubbed)
        if rel in MARKED_FILES and isinstance(scrubbed, dict):
            scrubbed = _stamp(rel, scrubbed, marker)
        left = residual_stock_refs(scrubbed)
        if left:
            residual[rel] = left
        if scrubbed != payload:
            final[rel] = (text, scrubbed)
    summary.update({"files_changed": sorted(final), "rows_withheld": withheld_total, "texts_redacted": redacted_total,
                    "names_known": len(names), "residual": residual, "applied_at": marker["applied_at"]})
    if residual:  # nothing written: the publisher aborts on the untouched batch
        raise MaintenanceScrubError(f"stock references remain after maintenance scrub: {residual}")
    for rel, (text, scrubbed) in final.items():
        _write_json_like(repo / rel, scrubbed, text)
    return summary


def is_active(doc: dict[str, Any] | None) -> bool:
    maintenance = (doc or {}).get("maintenance")
    return isinstance(maintenance, dict) and maintenance.get("enabled") is True


def _write_flag(flag_file: Path, payload: dict[str, Any]) -> None:
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    flag_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--flag-file", type=Path, default=DEFAULT_FLAG_FILE)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    enable = sub.add_parser("enable")
    enable.add_argument("--title")
    enable.add_argument("--message")
    sub.add_parser("disable")
    run = sub.add_parser("apply")
    run.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "enable":
            payload = {"enabled": True, "since": now_iso()}
            if args.title:
                payload["title"] = args.title
            if args.message:
                payload["message"] = args.message
            _write_flag(args.flag_file, payload)
        elif args.cmd == "disable":
            _write_flag(args.flag_file, {"enabled": False, "disabled_at": now_iso()})
        state = resolve_state(flag_file=args.flag_file)
        if args.cmd == "apply":
            print(json.dumps(apply(args.repo, state), ensure_ascii=False))
        else:
            print(json.dumps(state, ensure_ascii=False))
        return 0
    except MaintenanceConfigError as exc:
        print(f"MAINTENANCE_CONFIG_ERROR: {exc}", file=sys.stderr)
        return 2
    except MaintenanceScrubError as exc:
        print(f"MAINTENANCE_SCRUB_ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
