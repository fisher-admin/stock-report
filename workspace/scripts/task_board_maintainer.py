#!/usr/bin/env python3
"""Maintain ACTIVE/WAITING/ARCHIVE task boards.

Rules:
- completed/done cards leave ACTIVE and move to ARCHIVE
- waiting cards leave ACTIVE and move to WAITING
- keep ACTIVE focused on real in-progress work
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

WORKSPACE = Path("/Users/fisher/.openclaw/workspace")
CORE = WORKSPACE / "memory/core"
ACTIVE = CORE / "ACTIVE_TASKS.md"
WAITING = CORE / "WAITING_TASKS.md"
ARCHIVE = CORE / "TASK_ARCHIVE.md"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M Asia/Shanghai")


def split_header_sections(text: str) -> tuple[str, str]:
    marker = "## Active\n"
    if marker in text:
        head, tail = text.split(marker, 1)
        return head + marker, tail
    return text, ""


def parse_cards(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^###\s+.+?\n.*?(?=^###\s+|\Z)", text, flags=re.M | re.S))
    return [(m.group(0), m.group(0)) for m in matches]


def card_id(card: str) -> str:
    m = re.search(r"^###\s+(.+)$", card, flags=re.M)
    return m.group(1).strip() if m else "UNKNOWN"


def card_status(card: str) -> str:
    m = re.search(r"^- 状态：(.*)$", card, flags=re.M)
    return (m.group(1).strip().lower() if m else "")


def normalize_board(path: Path, title_line: str, section_name: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"{title_line}\n\n## {section_name}\n"


def append_cards(board_text: str, cards: list[str], section_name: str) -> str:
    if not cards:
        return board_text
    marker = f"## {section_name}\n"
    if marker not in board_text:
        board_text = board_text.rstrip() + f"\n\n{marker}"
    head, tail = board_text.split(marker, 1)
    tail = tail.rstrip() + "\n\n" + "\n\n".join(card.strip() for card in cards) + "\n"
    return head + marker + tail


def dedupe_cards(board_text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    last = 0
    for m in re.finditer(r"^###\s+.+?\n.*?(?=^###\s+|\Z)", board_text, flags=re.M | re.S):
        chunk = m.group(0)
        cid = card_id(chunk)
        prefix = board_text[last:m.start()]
        if cid not in seen:
            out.append(prefix)
            out.append(chunk.rstrip() + "\n")
            seen.add(cid)
        last = m.end()
    out.append(board_text[last:])
    return "".join(out)


def main() -> int:
    active_text = normalize_board(ACTIVE, "# ACTIVE_TASKS.md - 主 Agent 在办任务板", "Active")
    waiting_text = normalize_board(WAITING, "# WAITING_TASKS.md - 主 Agent 等待队列", "Waiting")
    archive_text = normalize_board(ARCHIVE, "# TASK_ARCHIVE.md - 主 Agent 任务归档", "Recent Done")

    head, active_tail = split_header_sections(active_text)
    cards = [c for _, c in parse_cards(active_tail)]
    keep_active: list[str] = []
    move_waiting: list[str] = []
    move_archive: list[str] = []

    for card in cards:
        status = card_status(card)
        if status.startswith("completed") or status.startswith("done"):
            if "最近更新时间" in card and "Asia/Shanghai" not in card:
                pass
            move_archive.append(card)
        elif status.startswith("waiting") or "暂停" in status or "等待" in status:
            move_waiting.append(card)
        else:
            keep_active.append(card)

    new_active = head + ("\n".join(card.strip() for card in keep_active) + "\n" if keep_active else "")
    waiting_text = append_cards(waiting_text, move_waiting, "Waiting")
    archive_text = append_cards(archive_text, move_archive, "Recent Done")

    ACTIVE.write_text(dedupe_cards(new_active), encoding="utf-8")
    WAITING.write_text(dedupe_cards(waiting_text), encoding="utf-8")
    ARCHIVE.write_text(dedupe_cards(archive_text), encoding="utf-8")

    print(str(ACTIVE))
    print(str(WAITING))
    print(str(ARCHIVE))
    print(f"moved_waiting={len(move_waiting)} moved_archive={len(move_archive)} kept_active={len(keep_active)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
