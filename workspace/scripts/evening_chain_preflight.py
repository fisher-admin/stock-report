#!/usr/bin/env python3
"""Afternoon preflight for the 19:30 stock chain (Hermes job, weekdays 16:30).

The 19:30 orchestrator only discovers a broken environment when it starts, and
one failed evening cascades into AI publish, research, watchdog, next-morning
brief, midday and event drift (10-20..22: a deleted interpreter cost three
trading days). This runs the same preflight checks hours earlier, plus the
failure modes seen since 2026-08, and prints nothing when all is well.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(os.environ.get("STOCK_WORKSPACE") or "/Users/fisher/.openclaw/workspace")
ORCHESTRATOR_SCRIPTS = WORKSPACE / "skills/stock-system-orchestrator/scripts"
HEALTH_DIR = WORKSPACE / "stock_data/03-working/health"
OUT = HEALTH_DIR / "evening_chain_preflight.json"
AI_PYTHON = WORKSPACE / "daily_stock_analysis/venv/bin/python3"
HERMES_AGENT = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser() / "hermes-agent"
GIT_REPOS = (WORKSPACE, WORKSPACE / "stock-report", WORKSPACE / "stock_data/03-working/stock-report-repo")
PIPELINE_LOCK = HEALTH_DIR / "stock_pipeline_gate.lockdir"
MIN_FREE_GB = float(os.environ.get("STOCK_PREFLIGHT_MIN_FREE_GB", "5"))
STALE_LOCK_SECONDS = 30 * 60
INTERPRETER_FIX = "UV_PYTHON_INSTALL_DIR=~/.openclaw/runtimes/python uv python install 3.11.14"
HERMES_DEPLOY = WORKSPACE / "ops/hermes/deploy.py"

COPILOT_PROBE = f"""
import sys
sys.path.insert(0, {str(HERMES_AGENT)!r})
from hermes_cli.copilot_auth import resolve_copilot_token
token, _ = resolve_copilot_token()
print("token" if token else "no_token")
"""


def _pid_alive(pid: int) -> bool:
    """Portable liveness probe (no external `kill` binary in slim Linux images)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_environments() -> list[str]:
    sys.path.insert(0, str(ORCHESTRATOR_SCRIPTS))
    import run_pipeline  # noqa: PLC0415 - same probe the 19:30 preflight runs

    issues: list[str] = []
    probe = run_pipeline.run_env_drift_probe(os.environ.copy())
    report = probe.get("report") or {}
    for name, env in sorted((report.get("locked_environments") or {}).items()):
        if not env.get("ok"):
            hint = f"；修复：{INTERPRETER_FIX}" if name == "ai" else ""
            issues.append(f"Python 环境 {name} 不可用：{env.get('error')}（{env.get('path')}）{hint}")
    for name, module in sorted((report.get("modules") or {}).items()):
        if not module.get("ok"):
            issues.append(f"主环境缺模块 {name}：{module.get('error')}；修复：~/.openclaw/venv/bin/pip install {name}")
    if not probe.get("ok") and not issues:
        issues.append(f"环境探针失败：{probe.get('stderr_tail') or probe.get('reason') or 'unknown'}")

    git = run_pipeline.git_status(run_pipeline.WORKING_REPO)
    if git.get("behind") or not (git.get("clean") or git.get("allowed_output_changes_only")):
        issues.append(
            f"发布工作仓库状态会被 19:30 拦截（behind={git.get('behind')}，非产出改动={git.get('other') or git.get('modified')}）；"
            f"检查：git -C {run_pipeline.WORKING_REPO} status"
        )
    return issues


def _ai_transport() -> str:
    """STOCK_AI_TRANSPORT from the environment or daily_stock_analysis/.env (default copilot_sdk)."""
    value = os.environ.get("STOCK_AI_TRANSPORT", "")
    env_file = WORKSPACE / "daily_stock_analysis/.env"
    if not value and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("STOCK_AI_TRANSPORT="):
                value = line.split("=", 1)[1].strip().strip("'\"")
    return (value or "copilot_sdk").lower()


def check_copilot() -> list[str]:
    # 2026-11-11: AI 分析默认走 Copilot SDK 直调（tools/copilot-sdk），不经 Hermes copilot_auth；
    # 只有 STOCK_AI_TRANSPORT=litellm 时才需要 Hermes 返回令牌。
    if _ai_transport() != "litellm":
        runner = WORKSPACE.parent / "tools/copilot-sdk/copilot_analyze.mjs"
        if not runner.is_file():
            return [f"Copilot SDK 入口缺失（{runner}），AI 分析会失败"]
        if not shutil.which("node") and not Path("/usr/bin/node").exists():
            return ["找不到 node，Copilot SDK 直调会失败"]
        if not (Path.home() / ".copilot/config.json").is_file() and not os.environ.get("COPILOT_GITHUB_TOKEN"):
            return ["Copilot CLI 未登录（~/.copilot/config.json 不存在），AI 分析会失败"]
        return []
    if not AI_PYTHON.exists():
        return []  # already reported by the environment probe
    try:
        proc = subprocess.run([str(AI_PYTHON), "-c", COPILOT_PROBE], capture_output=True, text=True,
                              timeout=90, cwd=str(HERMES_AGENT) if HERMES_AGENT.is_dir() else None)
    except subprocess.TimeoutExpired:
        return ["AI 模型凭据检查超时（Hermes copilot_auth 90 秒无响应）"]
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown"]
        return [f"AI venv 无法导入 Hermes copilot_auth（AI 分析会报 missing_key、覆盖 0/20）：{tail[0][:200]}；"
                "Hermes 升级引入新依赖时，在 AI venv 中 pip install 缺失模块"]
    if "no_token" in proc.stdout:
        return ["Hermes 未返回 Copilot 令牌（AI 分析会报 missing_key）；检查 hermes auth 登录状态"]
    return []


def _git_running() -> bool:
    return subprocess.run(["pgrep", "-x", "git"], capture_output=True).returncode == 0


def check_locks(now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    issues: list[str] = []
    git_busy = None
    for repo in GIT_REPOS:
        lock = repo / ".git/index.lock"
        if lock.exists() and now - lock.stat().st_mtime > STALE_LOCK_SECONDS:
            git_busy = _git_running() if git_busy is None else git_busy
            if not git_busy:
                age = int((now - lock.stat().st_mtime) / 60)
                issues.append(f"残留 git 锁 {lock}（{age} 分钟、无 git 进程），会阻断提交/发布；确认后 trash 该文件")
    owner = PIPELINE_LOCK / "owner.txt"
    if owner.exists():
        pid = owner.read_text().split("|", 1)[0].strip()
        alive = pid.isdigit() and _pid_alive(int(pid))
        if not alive:
            issues.append(f"主管线锁 {PIPELINE_LOCK} 的持有进程 {pid} 已不存在（19:30 会按陈旧锁处理，若失败请手动清理）")
    return issues


def check_disk() -> list[str]:
    free_gb = shutil.disk_usage(WORKSPACE).free / 1024 ** 3
    if free_gb < MIN_FREE_GB:
        return [f"磁盘剩余 {free_gb:.1f} GB，低于 {MIN_FREE_GB:g} GB；清理时不要删 ~/.openclaw/ 与 ~/.local/share/uv"]
    return []


def check_wrappers() -> list[str]:
    """Hermes wrappers must match ops/hermes/stock (edits in ~/.hermes are untracked drift)."""
    sys.path.insert(0, str(HERMES_DEPLOY.parent))
    import deploy  # noqa: PLC0415

    items = deploy.drift()
    if not items:
        return []
    names = "、".join(f"{name}（{'缺失' if kind == 'missing' else '与仓库不一致'}）" for name, kind in items)
    return [f"Hermes 包装脚本漂移：{names}；以仓库为准：python3 {HERMES_DEPLOY} deploy（改动请先提交到 ops/hermes/stock/）"]


CHECKS = (("environment", check_environments), ("copilot", check_copilot),
          ("locks", check_locks), ("disk", check_disk), ("wrappers", check_wrappers))


def run_checks(checks=CHECKS) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    for name, check in checks:
        try:
            results[name] = check()
        except Exception as exc:  # a broken check is itself a finding
            results[name] = [f"预检项 {name} 自身出错：{type(exc).__name__}: {exc}"]
    return results


def render(results: dict[str, list[str]]) -> str:
    issues = [issue for items in results.values() for issue in items]
    if not issues:
        return ""
    lines = [f"⚠️ 【晚间链预检】发现 {len(issues)} 个问题，19:30 主选股管线可能失败，请在开跑前处理："]
    lines += [f"- {issue}" for issue in issues]
    return "\n".join(lines)


def main() -> int:
    results = run_checks()
    message = render(results)
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "ok": not message, "results": results}, ensure_ascii=False, indent=2))
    if message:
        print(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
