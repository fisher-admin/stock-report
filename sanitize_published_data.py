#!/usr/bin/env python3
"""sanitize_published_data.py — 发布前脱敏：清除公开 JSON 中的本机绝对路径。

GitHub Pages 公开仓库的 data/ 下多个 JSON（review_state、strategy_registry、
system_verdict 等）由管线写入时携带了 /Users/<name>/... 形式的本机绝对路径
（db_path、source_database 等字段），向公网暴露本机目录结构。

本脚本递归遍历 data/latest/ 与 data/recommendation_analytics/ 的全部 JSON，
把字符串值中的家目录前缀替换为 "~"，仅在内容有变化时回写（保持 mtime 稳定）。
由 stage6_deploy_and_notify.py 在 git add 之前调用；也可手动运行。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
TARGET_DIRS = [
    REPO / "data" / "latest",
    REPO / "data" / "recommendation_analytics",
]
# 兼容历史上可能出现过的其他本机用户名前缀，统一脱敏。
HOME_PATTERN = re.compile(r"/Users/[A-Za-z0-9_.-]+")


def sanitize_value(value):
    if isinstance(value, str):
        return HOME_PATTERN.sub("~", value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def main() -> int:
    changed_files = 0
    for target_dir in TARGET_DIRS:
        if not target_dir.is_dir():
            continue
        for path in sorted(target_dir.glob("*.json")):
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"[skip] {path.name}: 读取失败 {exc}", file=sys.stderr)
                continue
            if "/Users/" not in raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[skip] {path.name}: 非法 JSON {exc}", file=sys.stderr)
                continue
            sanitized = sanitize_value(data)
            new_raw = json.dumps(sanitized, ensure_ascii=False, indent=2)
            if new_raw != json.dumps(data, ensure_ascii=False, indent=2):
                path.write_text(new_raw + "\n", encoding="utf-8")
                changed_files += 1
                print(f"[ok] 脱敏 {path.relative_to(REPO)}")
    print(f"完成：{changed_files} 个文件已脱敏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
