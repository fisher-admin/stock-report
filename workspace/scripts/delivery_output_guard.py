#!/usr/bin/env python3
"""Validate user-visible task output is sendable.

This is a lightweight shared rule checker for cron/task outputs.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

OUT = Path('/Users/fisher/.openclaw/workspace/stock_data/03-working/health/delivery_output_guard.json')

FORBIDDEN_PREFIXES = [
    '[已接收]',
    '等待审批',
    '系统将自动推送',
    '请审批后',
]


def now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def main() -> int:
    text = sys.stdin.read()
    payload = {
        'generated_at': now_str(),
        'ok': True,
        'length': len(text),
        'issues': [],
        'preview': text[:200],
    }
    stripped = text.strip()
    if not stripped:
        payload['issues'].append('EMPTY_OUTPUT')
    for prefix in FORBIDDEN_PREFIXES:
        if stripped.startswith(prefix):
            payload['issues'].append(f'FORBIDDEN_PREFIX:{prefix}')
    if len(stripped) < 12:
        payload['issues'].append('TOO_SHORT')
    payload['ok'] = len(payload['issues']) == 0
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(OUT))
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
