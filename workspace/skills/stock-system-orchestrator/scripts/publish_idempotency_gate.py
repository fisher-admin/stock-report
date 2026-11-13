#!/usr/bin/env python3
"""发布幂等闸（publish idempotency gate）。

同 trade_date + 同内容指纹 → 拒绝重复推送；不同内容 → 放行。

设计要点：
- 指纹 = 对"语义稳定文件集"的 sha256，**排除波动字段**（generated_at、updated_at、
  run_id、commit sha、时间戳类键），否则每次重跑都会产生不同指纹，闸失效。
- 状态文件：stock_data/03-working/health/publish_fingerprint.json
  结构：{"trade_date": "20260821", "fingerprint": "<sha256>", "recorded_at": ..., "pushed": true}
- 判定逻辑：
    state.trade_date == 当前 trade_date 且 state.fingerprint == 当前指纹 且 state.pushed
        → 幂等命中，跳过推送
    其余情况（新交易日 / 内容有变化 / 上次未成功推送）→ 放行

用法（在 publish_stock_report_v2.sh 的 git add 之前调用）：

    python publish_idempotency_gate.py --check --trade-date "$TRADE_DATE"
    # exit 0 = 需要推送; exit 7 = 幂等命中应跳过; 其他非零 = 内部错误(放行, 不阻断主流程由调用方决定)

    python publish_idempotency_gate.py --mark-pushed --trade-date "$TRADE_DATE"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", Path.home() / ".openclaw/workspace"))
DEFAULT_LATEST_DIR = WORKSPACE / "stock-report" / "data" / "latest"
DEFAULT_STATE_FILE = WORKSPACE / "stock_data" / "03-working" / "health" / "publish_fingerprint.json"

# 参与指纹的合同文件：决定线上页面实际内容的语义集合。
FINGERPRINT_FILES = [
    "system_verdict.json",
    "run_manifest.json",
    "candidate_state.json",
    "review_track_latest.json",
    "industry_heatmap.json",
    "dual_track_observation.json",
]

# 这些键是纯时间戳/元数据，重跑必变，必须从指纹中剔除。
VOLATILE_KEYS = {
    "generated_at",
    "updated_at",
    "recorded_at",
    "timestamp",
    "last_updated",
    "run_started_at",
    "generated_at_utc",
    "run_id",
}


def strip_volatile(obj: Any) -> Any:
    """递归剔除波动字段，只留语义内容。"""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in sorted(obj.items()) if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(x) for x in obj]
    if isinstance(obj, str) and len(obj) == 40 and all(c in "0123456789abcdef" for c in obj.lower()):
        # 40 位 hex 大概率是 commit sha —— 波动，剔除
        return "<sha-stripped>"
    return obj


def compute_fingerprint(latest_dir: Path) -> str | None:
    """计算当前 latest 合同的稳定内容指纹。任一核心文件缺失返回 None。"""
    h = hashlib.sha256()
    found_any = False
    for name in sorted(FINGERPRINT_FILES):
        fp = latest_dir / name
        h.update(name.encode())
        if fp.exists():
            found_any = True
            try:
                doc = json.loads(fp.read_text())
            except Exception:
                doc = {"_unparseable": fp.read_text(errors="replace")[:2000]}
            canonical = json.dumps(strip_volatile(doc), ensure_ascii=False, sort_keys=True)
            h.update(canonical.encode())
        else:
            h.update(b"<missing>")
    return h.hexdigest() if found_any else None


def load_state(state_file: Path) -> dict[str, Any]:
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def check(trade_date: str, fingerprint: str, state_file: Path) -> tuple[bool, str]:
    """True=需要推送(放行), False=幂等命中(跳过)。"""
    st = load_state(state_file)
    if not st:
        return True, "no previous fingerprint state"
    if st.get("trade_date") != trade_date:
        return True, f"new trade_date {trade_date} (state has {st.get('trade_date')})"
    if not st.get("pushed"):
        return True, "previous attempt never completed push"
    if st.get("fingerprint") != fingerprint:
        return True, "content changed since last push"
    return False, (
        f"duplicate publish skipped: trade_date={trade_date} fingerprint={fingerprint[:12]} "
        f"already pushed at {st.get('recorded_at')}"
    )


def mark_pushed(trade_date: str, fingerprint: str, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "trade_date": trade_date,
        "fingerprint": fingerprint,
        "pushed": True,
        "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
    }, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish idempotency gate")
    parser.add_argument("--check", action="store_true", help="check whether push should proceed")
    parser.add_argument("--mark-pushed", action="store_true", help="record successful push")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--latest-dir", default=str(DEFAULT_LATEST_DIR))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    args = parser.parse_args()

    latest_dir = Path(args.latest_dir)
    state_file = Path(args.state_file)

    if args.mark_pushed:
        fp = compute_fingerprint(latest_dir)
        if not fp:
            print("WARN: cannot compute fingerprint for mark-pushed; skipping record", file=sys.stderr)
            return 0
        mark_pushed(args.trade_date, fp, state_file)
        print(f"fingerprint recorded: {args.trade_date} {fp[:12]}")
        return 0

    if args.check:
        fp = compute_fingerprint(latest_dir)
        if not fp:
            # 无法计算指纹（目录为空等）→ 放行，不阻断
            print("proceed (no fingerprint computable)")
            return 0
        proceed, reason = check(args.trade_date, fp, state_file)
        if proceed:
            print(f"proceed: {reason}")
            return 0
        print(reason)
        return 7

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
