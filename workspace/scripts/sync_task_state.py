#!/usr/bin/env python3
"""Generate STATE_CARD.md from ACTIVE/WAITING/ARCHIVE task boards.

Goal: keep the startup context short and aligned with the real task board.
This is intentionally conservative: it only extracts structured task cards
from markdown and rewrites STATE_CARD.md in a compact format.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

WORKSPACE = Path("/Users/fisher/.openclaw/workspace")
CORE = WORKSPACE / "memory/core"
ACTIVE = CORE / "ACTIVE_TASKS.md"
WAITING = CORE / "WAITING_TASKS.md"
ARCHIVE = CORE / "TASK_ARCHIVE.md"
STATE = CORE / "STATE_CARD.md"

SH_TZ = "+0800"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_cards(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^###\s+(?P<id>.+?)\n(?P<body>.*?)(?=^###\s+|\Z)", text, flags=re.M | re.S))
    cards: list[dict[str, str]] = []
    for m in matches:
        task_id = m.group("id").strip()
        body = m.group("body")
        fields: dict[str, str] = {"taskId": task_id}
        for key in ["目标", "状态", "下一条具体动作", "证据目标", "最近更新时间"]:
            fm = re.search(rf"^- {re.escape(key)}：(.*)$", body, flags=re.M)
            if fm:
                fields[key] = fm.group(1).strip()
        cards.append(fields)
    return cards


def classify_focus(cards: list[dict[str, str]]) -> list[dict[str, str]]:
    real_active = []
    for card in cards:
        status = card.get("状态", "")
        if status.startswith("completed") or status.startswith("done"):
            continue
        real_active.append(card)
    return real_active


def short_line(card: dict[str, str]) -> str:
    return f"- {card['taskId']}: {card.get('目标', '（未写目标）')}\n  - 下一步：{card.get('下一条具体动作', '（未写下一步）')}"


def render() -> str:
    active_cards = extract_cards(ACTIVE)
    waiting_cards = extract_cards(WAITING)
    archive_cards = extract_cards(ARCHIVE)
    focus_cards = classify_focus(active_cards)

    by_prefix = Counter(card["taskId"].split("-", 1)[0] for card in focus_cards)
    stable_facts = [
        "- 主 workspace：`~/.openclaw/workspace`",
        "- 主 agent：`main / fisherbot`",
        "- 主模型链路：`openai-codex-proxy/gpt-5.4 -> openai-codex/gpt-5.4 -> minimax/MiniMax-M2.5`",
        "- 任务板以 `ACTIVE_TASKS.md / WAITING_TASKS.md / TASK_ARCHIVE.md` 为准",
    ]

    risk_lines = []
    if by_prefix.get("CFG"):
        risk_lines.append("- 模型/配置切换类任务仍在 active，需防止临时配置和现网口径漂移。")
    if any("投递" in card.get("目标", "") or "Discord" in card.get("目标", "") for card in focus_cards):
        risk_lines.append("- cron/Discord 投递仍需持续观测，避免 not-delivered / 目标歧义复发。")
    if any("选股" in card.get("目标", "") or "A股" in card.get("目标", "") for card in focus_cards):
        risk_lines.append("- 选股系统需持续区分数据链、AI 链、发布链、消息链，避免失败口径混杂。")
    if not risk_lines:
        risk_lines.append("- 当前无额外自动提炼风险；详见任务板。")

    next_steps = []
    for card in focus_cards[:4]:
        nxt = card.get("下一条具体动作")
        if nxt:
            next_steps.append(f"- {card['taskId']}: {nxt}")
    if not next_steps:
        next_steps.append("- 当前无真实 active 任务；可回收状态卡为待机状态。")

    lines = [
        "# STATE_CARD.md - OpenClaw 当前状态卡",
        "",
        f"更新时间：{now_str()}",
        "",
        "## 当前焦点",
    ]
    if focus_cards:
        lines.extend(short_line(card) for card in focus_cards[:6])
    else:
        lines.append("- 当前无真实 active 任务。")

    lines.extend([
        "",
        "## 等待中",
    ])
    if waiting_cards:
        lines.extend(f"- {card['taskId']}: {card.get('目标', '（未写目标）')}" for card in waiting_cards[:6])
    else:
        lines.append("- 无等待任务")

    lines.extend([
        "",
        "## 稳定约束",
        *stable_facts,
        "",
        "## 风险与下一步",
        *risk_lines,
        *next_steps,
        "",
        "## 归档提示",
        f"- active 卡片数：{len(active_cards)}（真实 active 约 {len(focus_cards)}）",
        f"- waiting 卡片数：{len(waiting_cards)}",
        f"- archive 卡片数：{len(archive_cards)}",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    STATE.write_text(render(), encoding="utf-8")
    print(str(STATE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
