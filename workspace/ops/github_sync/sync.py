#!/usr/bin/env python3
"""把 VPS 上的选股系统源码与研究代码单向镜像到 GitHub 公开仓库 stock-report 的 vps-source 分支(脱敏后)。

流程: rsync 白名单 -> 暂存目录(镜像仓库的工作区) -> 体积闸门 -> 凭据扫描 -> git commit -> git push。
只推源码: 数据、模型产物、日志、环境文件、记忆与个人文件都不在白名单内。
凭据扫描命中时整次不提交不推送(宁可不同步, 不可泄漏), 退出码 2, 报告写到 state 目录。
只用标准库; 以 ubuntu 用户运行; 推送凭据是只对该仓库有效的 deploy key。

用法: sync.py [--dry-run] [--no-push]
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HSYNC_ROOT", "/srv/hermes-stock"))
MIRROR = Path(os.environ.get("HSYNC_MIRROR", ROOT / "git-mirror/stock-report-vps-source"))
STATE = Path(os.environ.get("HSYNC_STATE", ROOT / "git-mirror/state"))
REMOTE = os.environ.get("HSYNC_REMOTE", "git@github-stock-report:fisher-admin/stock-report.git")
BRANCH = os.environ.get("HSYNC_BRANCH", "vps-source")   # 独立分支: 不触发 main 上的 Pages 部署与 CI, 不与每日发布抢推送
MAX_BYTES = int(os.environ.get("HSYNC_MAX_BYTES", 1_000_000))
# (仓库内目标, 源, rsync 额外参数)
SOURCES = [
    ("workspace/", ROOT / "workspace/", ["--filter", f"merge {HERE / 'workspace.filter'}"]),
    # /srv/hermes-stock/deploy(备份脚本、服务单元、环境样例)属机器专属运维内容, 不公开
    ("tools/", ROOT / "tools/", ["--include", "/colab_publish_receiver.py", "--include", "/copilot-sdk/", "--include", "/copilot-sdk/copilot_analyze.mjs",
                                   "--include", "/copilot-sdk/package-lock.json", "--exclude", "*"]),
]
SINGLE = []   # CLAUDE.md 是 VPS 运维交接文档(含机器地址与操作红线), 不公开
PATTERNS = [(n, re.compile(p)) for n, p in [
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("openai_style_key", r"\bsk-[A-Za-z0-9_\-]{24,}"),
    ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("google_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    ("discord_webhook", r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+"),
    ("discord_bot_token", r"\b[MN][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("url_credentials", r"[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:[^/\s:@'\"{}$<>]{6,}@"),
    ("assigned_secret", r"(?i)\b[\w\-]*(?:api[_\-]?key|secret|token|passwd|password|access[_\-]?key)[\w\-]*[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_\-+/=.]{20,})[\"']"),
    ("long_hex_assigned", r"(?i)(?:token|key|secret)[\w\-]*[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32,}\b"),
]]
IPV4 = re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DOC_EXT = {".md", ".txt", ".rst"}

def public_ip(ip):
    a, b = (int(x) for x in ip.split(".")[:2])
    return not (a in (0, 10, 127) or a >= 224 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31) or (a == 169 and b == 254) or a < 11)

def redact():
    """公开前脱敏: 文档里的公网 IP 与邮箱地址替换为占位符。代码/配置文件不改内容, 留给扫描拦截。"""
    n = 0
    for p in MIRROR.rglob("*"):
        if ".git" in p.relative_to(MIRROR).parts or not p.is_file() or p.suffix.lower() not in DOC_EXT: continue
        try: t = p.read_text()
        except (OSError, UnicodeDecodeError): continue
        u = IPV4.sub(lambda m: "<REDACTED_IP>" if public_ip(m.group(0)) else m.group(0), t)
        u = EMAIL.sub(lambda m: m.group(0) if "noreply" in m.group(0) or m.group(0).endswith(("example.com", ".png", ".jpg")) else "<REDACTED_EMAIL>", u)
        if u != t: p.write_text(u); n += 1
    return n

PLACEHOLDER = re.compile(r"(?i)(example|placeholder|your[_\-]|xxxx|\*\*\*|<[a-z_ ]+>|changeme|dummy|redacted|os\.environ|getenv|\$\{)")

def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)

def git(*a, check=True):
    r = run(["git", "-C", str(MIRROR), *a])
    if check and r.returncode: sys.exit(f"git {' '.join(a)} failed: {r.stderr.strip()[:500]}")
    return r.stdout

def load_allow():
    f = HERE / "secret_scan_allow.txt"
    if not f.exists(): return set()
    return {l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")}

def scan(files, allow):
    hits = []
    for rel in files:
        p = MIRROR / rel
        try: text = p.read_text(errors="ignore")
        except OSError: continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 4000: line = line[:4000]
            for name, rx in PATTERNS:
                m = rx.search(line)
                if not m or PLACEHOLDER.search(m.group(0)): continue
                key = f"{rel}:{name}"
                if key in allow or f"{rel}:{i}:{name}" in allow: continue
                hits.append({"file": rel, "line": i, "rule": name})   # 不记录命中内容本身
            for name, rx, ok in (("public_ip", IPV4, lambda v: not public_ip(v)), ("email", EMAIL, lambda v: "noreply" in v or v.endswith("example.com"))):
                for m in rx.finditer(line):
                    if ok(m.group(0)) or f"{rel}:{name}" in allow: continue
                    hits.append({"file": rel, "line": i, "rule": name}); break
    return hits

def main():
    dry = "--dry-run" in sys.argv; nopush = dry or "--no-push" in sys.argv
    STATE.mkdir(parents=True, exist_ok=True); MIRROR.mkdir(parents=True, exist_ok=True)
    if not (MIRROR / ".git").exists():
        run(["git", "init", "-q", "-b", "main", str(MIRROR)])
        git("config", "user.name", "hermes-stock-sync"); git("config", "user.email", "hermes-stock-sync@users.noreply.github.com")
    for dst, src, extra in SOURCES:
        (MIRROR / dst).mkdir(parents=True, exist_ok=True)
        r = run(["rsync", "-a", "--delete", "--delete-excluded", "--prune-empty-dirs", "--no-links", *extra, f"{src}/", f"{MIRROR / dst}/"])
        if r.returncode not in (0, 24): sys.exit(f"rsync {src} failed rc={r.returncode}: {r.stderr.strip()[:500]}")
    for dst, src in SINGLE:
        if src.exists(): (MIRROR / dst).write_bytes(src.read_bytes())
    # 体积闸门: 超限文件不入库, 记入报告
    skipped = []
    for p in MIRROR.rglob("*"):
        if ".git" in p.relative_to(MIRROR).parts or not p.is_file(): continue
        if p.stat().st_size > MAX_BYTES:
            skipped.append([str(p.relative_to(MIRROR)), p.stat().st_size]); p.unlink()
    redacted = redact()
    (MIRROR / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (MIRROR / "README.md").write_text("# 选股系统源码镜像(vps-source 分支)\n\n本分支由服务器定时任务自动生成, 是选股系统与研究代码的只读镜像; 不含行情数据、模型产物、日志、凭据与机器专属的运维配置, "
                                      "文档中的服务器地址与邮箱已替换为占位符。发布页面与公开结果在 main 分支。本仓库是研究与观察系统, 不含自动下单功能, 内容不构成投资建议。\n")
    git("add", "-A")
    changed = [l[3:].strip('"') for l in git("status", "--porcelain", "-z").split("\0") if l and l[0] != "D" and l[1] != "D"]
    changed = [c for c in changed if (MIRROR / c).is_file()]
    hits = scan(changed, load_allow())
    rep = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "changed_files": len(changed), "skipped_large": skipped, "redacted_docs": redacted, "secret_hits": hits, "pushed": False, "commit": None}
    if hits:
        git("reset", "-q", check=False)
        rep["status"] = "blocked_secret_scan"; (STATE / "last_run.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        print(f"GitHub 同步已拦截：凭据扫描命中 {len(hits)} 处，未提交未推送。", file=sys.stderr); sys.exit(2)
    if dry:
        rep["status"] = "dry_run"; rep["files"] = sorted(changed); git("reset", "-q", check=False)
        (STATE / "dry_run.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1)); print("dry-run", len(changed), "files; skipped", len(skipped)); return
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", f"sync: {time.strftime('%Y-%m-%d %H:%M %Z')} ({len(changed)} files)")
    rep["commit"] = git("rev-parse", "--short", "HEAD", check=False).strip() or None
    if not nopush and rep["commit"]:
        if "origin" not in git("remote"): git("remote", "add", "origin", REMOTE)
        r = run(["git", "-C", str(MIRROR), "push", "-q", "origin", f"HEAD:{BRANCH}"])
        if r.returncode:
            rep["status"] = "push_failed"; rep["error"] = r.stderr.strip()[:300]
            (STATE / "last_run.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1)); print("GitHub 同步推送失败。", file=sys.stderr); sys.exit(1)
        rep["pushed"] = True
    rep["status"] = "ok"; (STATE / "last_run.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    print("ok", rep["commit"], "changed", len(changed), "pushed", rep["pushed"])

if __name__ == "__main__":
    main()
