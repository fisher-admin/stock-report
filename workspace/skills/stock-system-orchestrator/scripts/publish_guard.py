#!/usr/bin/env python3
"""发布防回退监控（阶段6）：每天检查线上是否被旧任务/旧合同覆盖、数据是否落后。

检查项：① decision_state.gates 存在(v2合同) ② recommendation 策略级 strategy_gate 存在
③ 页面 JS 含手风琴逻辑 ④ 数据日期是否落后 ⑤ origin/main 是否被旧提交覆盖(回退)
⑥ 是否有非授权 clone 在推送。输出 data/latest/publish_guard_state.json，回退则 ok=false。

用法：python3 publish_guard.py [--fetch]   （--fetch 时先 git fetch 对比 origin/main）
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator_common import PUBLISHED_REPO  # noqa: E402

LATEST = PUBLISHED_REPO / "data" / "latest"
APP_JS = PUBLISHED_REPO / "assets" / "scripts" / "v2" / "app.js"


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(PUBLISHED_REPO), *args],
                              capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return ""


def main() -> int:
    checks: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []

    def chk(name: str, ok: bool, detail: str, *, warn: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            (warnings if warn else failures).append(f"{name}: {detail}")

    # ① decision_state.gates
    dec = {}
    try:
        dec = json.loads((LATEST / "decision_state.json").read_text("utf-8"))
        g = dec.get("gates") or {}
        ok = all(k in g for k in ("freshness_gate", "market_gate", "strategy_gate", "candidate_gate"))
        chk("contract_gates", ok, "四闸齐全" if ok else f"gates 缺失或不全（线上疑似回退到旧合同）：{list(g.keys())}")
    except Exception as e:
        chk("contract_gates", False, f"decision_state 读失败: {e}")

    # ② recommendation 策略级 strategy_gate
    try:
        rec = json.loads((LATEST / "recommendation_state.json").read_text("utf-8"))
        strats = rec.get("strategies") or {}
        declared_active = rec.get("active_strategy_ids")
        archived = rec.get("archived_strategies") or {}
        active = (
            [str(strategy_id) for strategy_id in declared_active if str(strategy_id)]
            if isinstance(declared_active, list)
            else []
        )
        scope_ok = bool(active) and len(active) == len(set(active))
        scope_ok = scope_ok and set(active) == set(strats)
        scope_ok = scope_ok and isinstance(archived, dict) and not (set(active) & set(archived))
        chk(
            "strategy_lifecycle_scope",
            scope_ok,
            (
                f"活跃 {len(active)} 个、归档 {len(archived)} 个，集合互斥"
                if scope_ok
                else "active_strategy_ids、strategies 与 archived_strategies 不一致"
            ),
        )
        miss = [strategy_id for strategy_id in active if "strategy_gate" not in (strats.get(strategy_id) or {})]
        chk(
            "strategy_gate_present",
            bool(active) and not miss,
            f"{len(active)} 个活跃策略均有 strategy_gate" if active and not miss else f"活跃策略缺 strategy_gate: {miss}",
        )
    except Exception as e:
        chk("strategy_lifecycle_scope", False, f"recommendation_state 读失败: {e}")
        chk("strategy_gate_present", False, f"recommendation_state 读失败: {e}")

    # ③ 页面 JS 手风琴逻辑
    try:
        js = APP_JS.read_text("utf-8")
        ok = "mountAiToggleHandlers" in js
        chk("frontend_accordion", ok, "app.js 含手风琴" if ok else "app.js 缺 mountAiToggleHandlers（前端疑似回退）")
    except Exception as e:
        chk("frontend_accordion", False, f"app.js 读失败: {e}")

    # ④ 数据新鲜度
    td = str(dec.get("trade_date") or "")
    latest_trade_date = td
    if len(td) == 8:
        try:
            d = datetime.strptime(td, "%Y%m%d")
            days = (datetime.now() - d).days
            # >4 天（含周末）落后告警
            chk("data_freshness", days <= 4, f"数据日 {td}，落后 {days} 天", warn=True)
        except Exception:
            chk("data_freshness", False, f"trade_date 非法: {td}", warn=True)
    else:
        chk("data_freshness", False, "decision_state 无 trade_date", warn=True)

    # ⑤ origin/main 回退检测
    local = _git("rev-parse", "HEAD")
    latest_commit = local
    if "--fetch" in sys.argv:
        _git("fetch", "origin")
        remote = _git("rev-parse", "origin/main")
        # 远端 decision_state 是否还有 gates（回退即线上无 gates）
        remote_dec = _git("show", "origin/main:data/latest/decision_state.json")
        remote_v2 = False
        try:
            remote_v2 = "gates" in json.loads(remote_dec)
        except Exception:
            pass
        chk("origin_not_rolled_back", remote_v2, "origin/main 仍是 v2 合同" if remote_v2 else "origin/main 疑似被旧合同覆盖（无 gates）")
        latest_commit = remote or local
        # 本地领先远端很多 = 远端可能被旧 clone 覆盖到旧点
        behind = _git("rev-list", "--count", "HEAD..origin/main")
        if behind and behind.isdigit() and int(behind) > 0:
            chk("origin_ahead_check", False, f"origin/main 领先本地 {behind}（可能旧 clone 推送），需核对", warn=True)

    contract_version = "v2" if (dec.get("gates")) else "unknown"
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": len(failures) == 0,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "latest_commit": latest_commit,
        "latest_trade_date": latest_trade_date,
        "contract_version": contract_version,
    }
    out = LATEST / "publish_guard_state.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"publish_guard: ok={payload['ok']} | {len(failures)} fail, {len(warnings)} warn → {out}")
    for f in failures:
        print("  FAIL:", f)
    for w in warnings:
        print("  WARN:", w)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
