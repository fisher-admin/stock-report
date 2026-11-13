#!/usr/bin/env python3
"""Deploy published artifacts and notify Discord."""

from __future__ import annotations

from deployment_receipt import DeploymentReceipt, now_iso

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from managed_process import run_managed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from orchestrator_common import (
    HEALTH_DIR,
    PUBLISHED_REPO,
    WORKSPACE,
    current_run_id,
    current_run_started_at,
    detect_local_http_proxy,
    git_network_env,
    now_str,
    write_health_snapshot,
)

LATEST_DIR = PUBLISHED_REPO / "data" / "latest"
SYSTEM_VERDICT_PATH = LATEST_DIR / "system_verdict.json"
CANDIDATE_STATE_PATH = LATEST_DIR / "candidate_state.json"
STAGE6_REPORT_PATH = HEALTH_DIR / "stage6_deploy_and_notify.json"
V2_PUBLISH_SCRIPT = Path(__file__).resolve().parent / "publish_stock_report_v2.sh"

DISCORD_WEBHOOK_ENV_KEYS = [
    "OPENCLAW_DISCORD_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
    "STOCK_SYSTEM_DISCORD_WEBHOOK_URL",
    "OMX_DISCORD_WEBHOOK_URL",
]
# 环境变量都没设时，从该文件兜底读取 webhook URL（一行 URL，或 KEY=URL）。
# 与 data_freshness_watchdog.py 共用同一文件：用户建一个 Discord webhook、写一处，管线与看门狗都推送。
DISCORD_WEBHOOK_FILE = Path.home() / ".openclaw" / "credentials" / "discord-webhook"
# Bot 直发模式（复用 openclaw 的 Discord bot 发到指定频道；优先于 webhook，与看门狗同一套配置）。
DISCORD_BOT_TOKEN_ENV_KEYS = ["DISCORD_BOT_TOKEN", "OPENCLAW_DISCORD_BOT_TOKEN", "DISCORD_TOKEN"]
DISCORD_ALERT_CONFIG = Path.home() / ".openclaw" / "credentials" / "discord-alert.json"
OPENCLAW_JSON = Path.home() / ".openclaw" / "openclaw.json"

COLOR_FATAL = 16711680
COLOR_WARNING = 16776960
COLOR_SUCCESS = 65280

DEFAULT_BRANCH = os.environ.get("OPENCLAW_PUBLISHED_BRANCH", "main").strip() or "main"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def first_non_empty_env(keys: list[str]) -> str:
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def resolve_discord_webhook() -> str:
    """Discord webhook URL：先环境变量，再 DISCORD_WEBHOOK_FILE 兜底（launchd/cron 无 env 时也可用）。"""
    value = first_non_empty_env(DISCORD_WEBHOOK_ENV_KEYS)
    if value:
        return value
    try:
        for line in DISCORD_WEBHOOK_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line and not line.startswith("http"):
                line = line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("http"):
                return line
    except OSError:
        pass
    return ""


def resolve_discord_bot_token() -> str:
    """Bot token：先环境变量，再复用 openclaw.json 的 channels.discord.token。"""
    value = first_non_empty_env(DISCORD_BOT_TOKEN_ENV_KEYS)
    if value:
        return value
    try:
        doc = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
        token = ((doc.get("channels", {}) or {}).get("discord", {}) or {}).get("token", "")
        return token.strip() if token else ""
    except Exception:  # noqa: BLE001
        return ""


def resolve_discord_channel_id() -> str:
    value = str(os.environ.get("DISCORD_ALERT_CHANNEL_ID") or "").strip()
    if value:
        return value
    try:
        return str(json.loads(DISCORD_ALERT_CONFIG.read_text(encoding="utf-8")).get("channel_id") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def truncate(text: str, limit: int = 1000) -> str:
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def embed_field(name: str, value: str, inline: bool = False) -> dict[str, Any]:
    return {
        "name": truncate(name, 256),
        "value": truncate(value or "—", 1024),
        "inline": inline,
    }


def build_embed(
    *,
    title: str,
    description: str,
    color: int,
    fields: list[dict[str, Any]],
    footer: str,
) -> dict[str, Any]:
    return {
        "title": truncate(title, 256),
        "description": truncate(description, 4096),
        "color": color,
        "fields": fields[:20],
        "footer": {"text": truncate(footer, 2048)},
        "timestamp": now_utc_iso(),
    }


def post_discord_embed(embed: dict[str, Any]) -> dict[str, Any]:
    # 优先 bot 直发指定频道（复用 openclaw Discord bot）；否则退回 incoming webhook。
    bot_token = resolve_discord_bot_token()
    channel_id = resolve_discord_channel_id()
    use_bot = bool(bot_token and channel_id)
    webhook_url = resolve_discord_webhook()
    payload = {
        "attempted": False,
        "ok": False,
        "webhook_configured": bool(webhook_url) or use_bot,
        "transport": "bot" if use_bot else ("webhook" if webhook_url else None),
        "http_status": None,
    }
    if not use_bot and not webhook_url:
        payload["error"] = "discord webhook/bot missing"
        return payload

    opener = None
    proxy_url = detect_local_http_proxy()
    if proxy_url:
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        payload["proxy_used"] = True
    else:
        opener = build_opener()
        payload["proxy_used"] = False

    if use_bot:
        target_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        body = {"embeds": [embed]}
        headers = {"Content-Type": "application/json", "Authorization": f"Bot {bot_token}",
                   "User-Agent": "openclaw-stock/1.0"}
    else:
        target_url = webhook_url
        body = {"username": "OpenClaw Stock System", "embeds": [embed]}
        headers = {"Content-Type": "application/json"}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(target_url, data=data, headers=headers, method="POST")
    payload["attempted"] = True
    try:
        with opener.open(request, timeout=20) as response:
            payload["http_status"] = getattr(response, "status", None)
            payload["response_reason"] = getattr(response, "reason", "")
            payload["ok"] = payload["http_status"] is not None and 200 <= payload["http_status"] < 300
    except HTTPError as exc:
        payload["http_status"] = exc.code
        payload["error"] = f"HTTPError {exc.code}: {exc.reason}"
    except URLError as exc:
        payload["error"] = f"URLError: {exc.reason}"
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


def load_verdict() -> dict[str, Any]:
    return load_json(SYSTEM_VERDICT_PATH)


def load_candidate_state() -> dict[str, Any]:
    return load_json(CANDIDATE_STATE_PATH)


def verdict_trade_date(verdict: dict[str, Any]) -> str:
    return str(((verdict.get("dates") or {}).get("decision_trade_date")) or verdict.get("trade_date") or "")


def verdict_label(verdict: dict[str, Any]) -> str:
    return str(((verdict.get("final_action") or {}).get("label")) or ((verdict.get("final_action") or {}).get("action")) or "未知")


def verdict_action(verdict: dict[str, Any]) -> str:
    return str(((verdict.get("final_action") or {}).get("action")) or "")


def verdict_blockers(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = list(((verdict.get("date_contract") or {}).get("blockers")) or [])
    if blockers:
        return blockers
    gates = verdict.get("gates") or {}
    for gate in gates.values():
        gate_blockers = gate.get("blockers") or []
        if gate_blockers:
            blockers.extend(gate_blockers)
    return blockers


def main_count_from_verdict(verdict: dict[str, Any]) -> int:
    summary = ((verdict.get("candidate_execution") or {}).get("summary")) or {}
    try:
        return int(summary.get("main") or 0)
    except Exception:
        return 0


def git_cmd(cmd: list[str], cwd: Path, timeout_seconds: int = 180) -> subprocess.CompletedProcess[str]:
    env = git_network_env()
    # errors="replace": 同orchestrator_common.run_git——push经代理输出偶含非UTF-8字节
    # (20260710夜实测0xbc), 严格解码抛UnicodeDecodeError会把git操作本身记成失败。
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_seconds,
    )


def git_head_short(repo_dir: Path) -> str:
    proc = git_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def notify_warning(
    *,
    run_id: str,
    trade_date: str,
    attempt: int,
    blocker_codes: list[str],
    remediation_groups: list[str],
) -> dict[str, Any]:
    embed = build_embed(
        title="🟠 自愈警告",
        description="流水线检测到系统裁决阻断，已进入自愈重试环。",
        color=COLOR_WARNING,
        fields=[
            embed_field("运行ID", run_id, True),
            embed_field("交易日", trade_date or "—", True),
            embed_field("重试轮次", str(attempt), True),
            embed_field("捕获 Blocker", "、".join(blocker_codes) or "—"),
            embed_field("修复组", " -> ".join(remediation_groups) or "—"),
        ],
        footer="OpenClaw Self-Healing Warning",
    )
    return post_discord_embed(embed)


def notify_fatal(
    *,
    run_id: str,
    trade_date: str,
    error_code: str,
    reason: str,
    blockers: list[dict[str, Any]] | None = None,
    stderr_tail: str = "",
) -> dict[str, Any]:
    blocker_lines = []
    for blocker in (blockers or [])[:6]:
        code = blocker.get("code") or blocker.get("field") or "BLOCK"
        msg = blocker.get("message") or blocker.get("reason") or ""
        blocker_lines.append(f"- {code}: {msg}")
    embed = build_embed(
        title="🔴 致命异常",
        description="流水线未能闭环，系统已停止发布。",
        color=COLOR_FATAL,
        fields=[
            embed_field("运行ID", run_id, True),
            embed_field("交易日", trade_date or "—", True),
            embed_field("错误码", error_code, True),
            embed_field("失败原因", reason or "—"),
            embed_field("阻断原因", "\n".join(blocker_lines) or "—"),
            embed_field("Git / 终端错误", stderr_tail or "—"),
        ],
        footer="OpenClaw Fatal Notification",
    )
    return post_discord_embed(embed)


def notify_success(
    *,
    run_id: str,
    trade_date: str,
    final_action_label: str,
    main_count: int,
    commit_sha: str,
    branch: str,
) -> dict[str, Any]:
    embed = build_embed(
        title="🟢 发布成功",
        description="系统裁决已完成发布并推送到 GitHub。",
        color=COLOR_SUCCESS,
        fields=[
            embed_field("运行ID", run_id, True),
            embed_field("交易日", trade_date or "—", True),
            embed_field("最终动作", final_action_label or "—", True),
            embed_field("主攻股票数", str(main_count), True),
            embed_field("分支", branch, True),
            embed_field("Commit", commit_sha or "—", True),
        ],
        footer="OpenClaw Publish Success",
    )
    return post_discord_embed(embed)


def deploy_published_repo(
    verdict: dict[str, Any],
    *,
    branch: str = DEFAULT_BRANCH,
    run_id: str | None = None,
    trade_date: str | None = None,
) -> dict[str, Any]:
    action = verdict_action(verdict)
    action_label = verdict_label(verdict)
    effective_trade_date = (trade_date or "").strip() or verdict_trade_date(verdict)
    effective_run_id = (run_id or "").strip() or str(verdict.get("run_id") or "").strip() or current_run_id()

    payload: dict[str, Any] = {
        "ok": False,
        "branch": branch,
        "action": action,
        "final_action": action,
        "final_action_label": action_label,
        "proxy_used": bool(detect_local_http_proxy()),
    }

    if not PUBLISHED_REPO.exists():
        payload["error"] = f"published repo missing: {PUBLISHED_REPO}"
        return payload

    if action == "halt":
        payload["error"] = "system verdict halted deployment"
        return payload

    # Exclusive remote-publish path: rebuild v2 contracts, strict-validate, then push.
    # Do not git add . or bare-push from this function — that bypassed the pre-push hook
    # contract and previously shipped incomplete dual-track JSON.
    if not V2_PUBLISH_SCRIPT.exists():
        payload["error"] = f"v2 publisher missing: {V2_PUBLISH_SCRIPT}"
        return payload

    env = git_network_env()
    if effective_run_id:
        env["RUN_ID"] = effective_run_id
    if effective_trade_date:
        env["TRADE_DATE"] = effective_trade_date
        # 2026-10-03: the publisher's generators prefer OPENCLAW_TARGET_TRADE_DATE.
        # The 19:30 gate exports the *requested* date into this process; Stage 1
        # may lock an earlier session.  Always hand the locked batch date down,
        # otherwise 20260930 data was published labelled 20261002 (Oct 2 run).
        env["OPENCLAW_TARGET_TRADE_DATE"] = effective_trade_date

    try:
        proc = run_managed(
            ["bash", str(V2_PUBLISH_SCRIPT)],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        payload["error"] = "v2 publisher timed out"
        payload["publisher"] = str(V2_PUBLISH_SCRIPT)
        for field, output in (("publisher_stdout_tail", exc.stdout), ("publisher_stderr_tail", exc.stderr)):
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            payload[field] = "\n".join(str(output or "").splitlines()[-20:])
        return payload
    except (RuntimeError, OSError, ValueError) as exc:
        payload['error'] = f'publisher process cleanup unconfirmed: {type(exc).__name__}'
        payload['cleanup_unconfirmed'] = True
        return payload
    payload["publisher"] = "publish_stock_report_v2.sh"
    payload["publisher_returncode"] = proc.returncode
    payload["publisher_stdout_tail"] = "\n".join((proc.stdout or "").splitlines()[-20:])
    payload["publisher_stderr_tail"] = "\n".join((proc.stderr or "").splitlines()[-20:])
    if proc.returncode != 0:
        payload["error"] = "v2 publisher failed"
        return payload

    # Verify receipt produced by v2 publisher: MUST strictly match expected run_id & trade_date (never self-infer)
    receipt_file = HEALTH_DIR / "deployment_receipt.json"
    receipt = DeploymentReceipt.load(receipt_file)
    if not receipt:
        payload["ok"] = False
        payload["error"] = "Deployment failed: deployment_receipt.json not found"
        return payload

    if not effective_run_id or not effective_trade_date:
        payload["ok"] = False
        payload["error"] = f"Deployment failed: missing effective run_id ({effective_run_id}) or trade_date ({effective_trade_date})"
        return payload

    if not receipt.matches(effective_run_id, effective_trade_date):
        payload["ok"] = False
        payload["error"] = (
            f"Deployment failed: receipt mismatch (receipt={receipt.to_dict()}, "
            f"expected run_id={effective_run_id}, trade_date={effective_trade_date})"
        )
        return payload

    if not receipt.remote_confirmed or not receipt.local_sha or receipt.local_sha != receipt.remote_sha:
        payload["ok"] = False
        payload["error"] = f"Deployment failed: receipt remote confirmation invalid (receipt={receipt.to_dict()})"
        return payload

    payload["ok"] = True
    payload["remote_push"] = "v2_publisher"
    commit_sha = git_head_short(PUBLISHED_REPO)
    payload["commit_sha"] = commit_sha
    payload["commit_message"] = f"publish(v2): {trade_date or 'unknown'}"
    payload["deployment_receipt"] = receipt.to_dict()
    return payload


def deploy_and_notify(
    *,
    branch: str = DEFAULT_BRANCH,
    run_id: str | None = None,
    trade_date: str | None = None,
) -> dict[str, Any]:
    verdict = load_verdict()
    candidate_state = load_candidate_state()
    effective_run_id = (run_id or "").strip() or current_run_id()
    effective_trade_date = (trade_date or "").strip() or verdict_trade_date(verdict)
    main_count = main_count_from_verdict(verdict)

    payload: dict[str, Any] = {
        "stage": "deployAndNotify",
        "generated_at": now_str(),
        "run": {
            "run_id": effective_run_id,
            "started_at": current_run_started_at(),
            "trade_date": effective_trade_date,
        },
        "verdict_present": bool(verdict),
        "verdict_action": verdict_action(verdict),
        "verdict_label": verdict_label(verdict),
        "main_count": main_count,
        "branch": branch,
        "ok": False,
        "notification_required": bool(resolve_discord_webhook()),
        "candidate_count": len(candidate_state.get("candidates") or []),
    }

    if not verdict:
        notify_result = notify_fatal(
            run_id=effective_run_id,
            trade_date=effective_trade_date,
            error_code="MISSING_SYSTEM_VERDICT",
            reason="stage6 无法读取 system_verdict.json",
            blockers=[],
        )
        payload["notification"] = notify_result
        write_health_snapshot(STAGE6_REPORT_PATH, payload, trade_date=effective_trade_date or None)
        return payload

    if verdict_action(verdict) == "halt":
        notify_result = notify_fatal(
            run_id=effective_run_id,
            trade_date=effective_trade_date,
            error_code="VERDICT_HALT",
            reason=(verdict.get("final_action") or {}).get("summary") or "最终裁决为 halt",
            blockers=verdict_blockers(verdict),
        )
        payload["notification"] = notify_result
        write_health_snapshot(STAGE6_REPORT_PATH, payload, trade_date=effective_trade_date or None)
        return payload

    deploy_result = deploy_published_repo(
        verdict,
        branch=branch,
        run_id=effective_run_id,
        trade_date=effective_trade_date,
    )
    payload["deploy"] = deploy_result
    if not deploy_result.get("ok"):
        notify_result = notify_fatal(
            run_id=effective_run_id,
            trade_date=effective_trade_date,
            error_code="GIT_PUSH_FAILED" if deploy_result.get("git_push_returncode") else "DEPLOY_FAILED",
            reason=deploy_result.get("error") or "deploy failed",
            blockers=verdict_blockers(verdict),
            stderr_tail=deploy_result.get("git_push_stderr_tail") or deploy_result.get("git_commit_tail") or deploy_result.get("git_add_stderr_tail") or "",
        )
        payload["notification"] = notify_result
        write_health_snapshot(STAGE6_REPORT_PATH, payload, trade_date=effective_trade_date or None)
        return payload

    notify_result = notify_success(
        run_id=effective_run_id,
        trade_date=effective_trade_date,
        final_action_label=verdict_label(verdict),
        main_count=main_count,
        commit_sha=deploy_result.get("commit_sha") or "",
        branch=branch,
    )
    payload["notification"] = notify_result
    # Do not block a successful deploy if webhook is missing or fails.
    payload["ok"] = True
    write_health_snapshot(STAGE6_REPORT_PATH, payload, trade_date=effective_trade_date or None)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy published repo and send Discord notifications")
    parser.add_argument("--event", choices=["deploy", "warning", "fatal"], default="deploy")
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--error-code", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--blocker", action="append", default=[])
    parser.add_argument("--remediation-group", action="append", default=[])
    args = parser.parse_args()

    run_id = args.run_id.strip() or current_run_id()
    trade_date = args.trade_date.strip()

    if args.event == "warning":
        result = notify_warning(
            run_id=run_id,
            trade_date=trade_date,
            attempt=args.attempt,
            blocker_codes=args.blocker,
            remediation_groups=args.remediation_group,
        )
        print(json.dumps({"ok": result.get("ok"), "notification": result}, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.event == "fatal":
        blocker_objs = [{"code": code, "message": ""} for code in args.blocker]
        result = notify_fatal(
            run_id=run_id,
            trade_date=trade_date,
            error_code=args.error_code or "FATAL",
            reason=args.reason or "fatal",
            blockers=blocker_objs,
        )
        print(json.dumps({"ok": result.get("ok"), "notification": result}, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    result = deploy_and_notify(branch=args.branch, run_id=run_id, trade_date=trade_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
