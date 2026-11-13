#!/bin/bash
# 保护三层闸 · 影子重放常态化 wrapper (B-4决议: 影子常态化一周, 7/18后拍板接线第18族)
#
# 调 spike4 影子重放器 (shadow_replay.run_shadow), 对历史推荐台账重放 ProtectionEngine,
# 回答"如果当时有保护会挡掉什么"。MARKET_STRESS 采用已拍板的 5% 档 (SPIKE_REPORT §MARKET_STRESS)。
# 产出 JSON: 锁生效日/被挡推荐/被挡后验超额 三列 + 5%档触发日清单 + 落锁清单。
#
# 铁律: 纯 additive/只读。影子重放器对生产 recommendations.db 走 mode=ro 只读连接,
#       MemLockStore 纯内存零残留 (SPIKE_REPORT 验收3), 不触碰任何冻结资产/panel_full/发布链。
#       输出仅落 stock_data/03-working/health/ 下的影子产物。
set -uo pipefail

WS="$HOME/.openclaw/workspace"
PY="$HOME/.openclaw/venv/bin/python"
[ -x "$PY" ] || PY="python3"

SPIKE_DIR="$WS/factor_factory/research/2026-07-github-stock-systems/spikes/spike4_protection"
HEALTH_DIR="$WS/stock_data/03-working/health"
ARCHIVE_DIR="$HEALTH_DIR/protection_shadow_archive"
STRESS_THRESHOLD="${MARKET_STRESS:-0.05}"   # 已拍板 5% 档; 可经环境变量覆盖用于调参

mkdir -p "$ARCHIVE_DIR"

DAY="$(date '+%Y%m%d')"
LATEST="$HEALTH_DIR/protection_shadow_latest.json"
ARCHIVE="$ARCHIVE_DIR/protection_shadow_${DAY}.json"

MARKET_STRESS="$STRESS_THRESHOLD" SPIKE_DIR="$SPIKE_DIR" \
LATEST_OUT="$LATEST" ARCHIVE_OUT="$ARCHIVE" RUN_DAY="$DAY" \
"$PY" - <<'PYEOF'
import json, os, sys, datetime

spike_dir = os.environ["SPIKE_DIR"]
sys.path.insert(0, spike_dir)
import shadow_replay as sr

th = float(os.environ["MARKET_STRESS"])

res = sr.run_shadow(None)  # 只跑引擎不落 spike 目录文件 (零残留)

# --- 三列报告: 锁生效日 / 被挡推荐 / 被挡后验超额5日 ---
blocked = [
    {
        "lock_effective_date": b["lock_effective_date"],
        "blocked_rec": b["blocked_rec"],
        "posthoc_excess_5d": b["posthoc_excess_5d"],
        "reason_code": b["reason_code"],
        "created_by": b["created_by"],
        "lock_id": b["lock_id"],
        "lock_end_date": b["lock_end_date"],
    }
    for b in res["blocked"]
]

# --- 5% 档 MARKET_STRESS 触发日清单 (share >= 阈值) ---
stress_days = [
    {"day": s["day"], "n_holdings": s["n_holdings"],
     "n_blocked": s["n_blocked"], "share": round(s["share"], 6)}
    for s in res["stress"] if s["share"] >= th
]

# --- 落锁清单 ---
locks = [
    {k: lk[k] for k in ("day", "lock_id", "scope", "scope_key",
                        "reason_code", "lock_end_date", "unlock_at_phase",
                        "created_by")}
    for lk in res["locks"]
]

# --- 被挡后验超额汇总 (有值的) ---
excess_vals = [float(b["posthoc_excess_5d"]) for b in blocked
               if b["posthoc_excess_5d"] != "NA"]
excess_summary = None
if excess_vals:
    srt = sorted(excess_vals)
    n = len(srt)
    excess_summary = {
        "n_with_value": n,
        "mean": round(sum(srt) / n, 6),
        "median": round(srt[n // 2], 6),
        "neg_share": round(sum(1 for v in srt if v < 0) / n, 6),
    }

out = {
    "generated_at": datetime.datetime.now().astimezone().isoformat(),
    "run_day": os.environ["RUN_DAY"],
    "mode": "shadow_readonly",
    "market_stress_threshold": th,
    "replay_window": {
        "n_trading_days": len(res["replay_days"]),
        "first": res["replay_days"][0],
        "last": res["replay_days"][-1],
    },
    "n_recommendations": res["n_recs"],
    "n_locks_fired": len(locks),
    "n_blocked": len(blocked),
    "blocked_posthoc_excess_summary": excess_summary,
    "market_stress_trigger_days": stress_days,
    "locks_fired": locks,
    "blocked_recommendations": blocked,
    "note": "B-4决议影子常态化一周; 参数为 spike 演示口径非生产定稿; 7/18后拍板接线第18族",
}

for path in (os.environ["LATEST_OUT"], os.environ["ARCHIVE_OUT"]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

print(f"protection shadow: locks={len(locks)} blocked={len(blocked)} "
      f"stress_days(>={th:.0%})={len(stress_days)} -> {os.environ['LATEST_OUT']}")
PYEOF
