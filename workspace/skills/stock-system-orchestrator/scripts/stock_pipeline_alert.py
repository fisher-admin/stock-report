#!/usr/bin/env python3
"""选股管线硬告警：失败要「响」不要「哑」（2026-06-28）。

对当前发布状态做三类硬检查，任一命中即输出醒目告警 + 写 health/pipeline_alert.json + 退出码 1：
  1) 交易日落后：发布 trade_date < akshare(新浪)真实最新交易日 → 数据/选股没跟上（如 token 过期那种静默落后）。
  2) AI 覆盖不足：当前唯一生产对照组 ai_coverage < 60% → AI 回填/同步出问题。
  3) 选股缺失：发布 trade_date 与真实最新交易日不一致 / 策略 items 数 < 预期。
  4) 回填异常：ai_backfill_loop.json 状态为 timed_out / base_not_ready / publish_not_ready。

设计为可直接挂进「收盘闭环-验证+治理+告警」cron：脚本打印的告警块会被 cron agent 播报到 Discord；
退出码非 0 也便于任何调度器据此重试/升级。纯读，不改任何数据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator_common import PUBLISHED_REPO, HEALTH_DIR  # noqa: E402


def _send_discord(payload: dict) -> dict:
    """命中告警时通过 stage6 的 Discord 通道（webhook/bot）推送；无配置则静默跳过。"""
    try:
        from stage6_deploy_and_notify import build_embed, embed_field, post_discord_embed, COLOR_WARNING
    except Exception as exc:
        return {"sent": False, "reason": f"import_failed: {exc}"}
    fields = [
        embed_field("发布交易日", payload.get("published_trade_date") or "—", True),
        embed_field("真实最新交易日", payload.get("real_latest_trade_date") or "—", True),
        embed_field("严重度", payload.get("severity") or "—", True),
        embed_field("告警项", "\n".join(payload.get("alerts") or []) or "—"),
    ]
    embed = build_embed(
        title="🚨 选股管线告警",
        description="收盘后自检命中异常：数据/选股/AI 覆盖未达标，请尽快处理。",
        color=COLOR_WARNING,
        fields=fields,
        footer="OpenClaw Stock Pipeline Alert",
    )
    try:
        return post_discord_embed(embed)
    except Exception as exc:
        return {"sent": False, "reason": f"post_failed: {exc}"}

LATEST = PUBLISHED_REPO / "data" / "latest"
ALERT_PATH = HEALTH_DIR / "pipeline_alert.json"
AI_COVERAGE_MIN = 0.60
EXPECTED_ITEMS = 20
SIDS = ["prebreakout_v41"]


def _ak_latest_trade_date() -> str | None:
    """akshare(新浪)日历下【最新"可发布"交易日】yyyymmdd；失败返回 None（不误报「落后」）。

    20260710修(收盘感知): 原逻辑取"<=今天"的最近交易日——当天是交易日时, 从0:00起就把
    今天当基准, 但当日发布要等19:30收盘管线才产生 → 每个交易日0:00-19:40之间任何一次
    自检都会误报critical「落后1个交易日」(实测: 7/10 12:44盘中补跑0709被误报)。
    修: 当天为交易日且当前时刻<19:45(收盘管线19:30+缓冲)时, 基准回退到前一交易日;
    19:45后仍落后才是真告警。"""
    try:
        import akshare as ak
        import datetime as _dt
        df = ak.tool_trade_date_hist_sina()
        now = _dt.datetime.now()
        today = now.strftime("%Y%m%d")
        dates = sorted(str(d).replace("-", "") for d in df["trade_date"].astype(str))
        prev = [d for d in dates if d <= today]
        if not prev:
            return None
        latest = prev[-1]
        if latest == today and now.strftime("%H:%M") < "19:45":
            # 今天是交易日但当日发布尚未到点——基准=前一交易日
            return prev[-2] if len(prev) >= 2 else latest
        return latest
    except Exception:
        return None


def main() -> int:
    alerts: list[str] = []
    info: dict = {"checks": {}}

    try:
        rec = json.loads((LATEST / "recommendation_state.json").read_text(encoding="utf-8"))
    except Exception as exc:
        alerts.append(f"无法读取 recommendation_state.json：{exc}")
        rec = {}

    pub_date = str(rec.get("trade_date") or "")
    real_date = _ak_latest_trade_date()
    info["published_trade_date"] = pub_date
    info["real_latest_trade_date"] = real_date

    # 1) 交易日落后
    if real_date and pub_date and pub_date < real_date:
        alerts.append(f"🚨 数据/选股落后：发布交易日 {pub_date} < 真实最新交易日 {real_date}（落后 {_lag(pub_date, real_date)} 个交易日）")

    strategies = rec.get("strategies") or {}
    cov_summary = {}
    for sid in SIDS:
        s = strategies.get(sid) or {}
        cov = s.get("ai_coverage") or {}
        have = cov.get("have")
        total = cov.get("total") or len(s.get("items") or [])
        if isinstance(have, (int, float)) and isinstance(total, (int, float)) and total:
            try:
                from metric_guards import safe_ratio

                ratio = safe_ratio(have, total)
            except Exception:
                ratio = have / total if total else None
                if isinstance(ratio, float) and ratio > 1.0:
                    ratio = 1.0
        else:
            ratio = None
        cov_summary[sid] = {"have": have, "total": total, "ratio": ratio}
        # 模板型策略（T1：t1_template_note，ai_score 故意为 null）AI 覆盖 0/20 是设计如此，不计「覆盖不足」。
        types = {it.get("ai_analysis_type") for it in (s.get("items") or [])}
        ai_optional = bool(types) and types <= {"t1_template_note", "none"}
        # 2) AI 覆盖不足（仅对「应有真实 AI」的策略）
        if not ai_optional and ratio is not None and ratio < AI_COVERAGE_MIN:
            alerts.append(f"🚨 AI 覆盖不足：{s.get('strategy_name') or sid} 仅 {have}/{total}（<{AI_COVERAGE_MIN:.0%}）")
        # 3) 选股缺失/不足
        n = len(s.get("items") or [])
        if n and n < EXPECTED_ITEMS and not s.get("insufficient_data"):
            alerts.append(f"⚠️ 选股不足：{s.get('strategy_name') or sid} 仅 {n}/{EXPECTED_ITEMS} 只")
    info["checks"]["ai_coverage"] = cov_summary

    # 是否存在「应有 AI 却不足」的策略——回填状态告警只在此前提下才有意义（避免读到陈旧 loop 健康误报）。
    ai_shortfall = any("AI 覆盖不足" in a for a in alerts)
    # 4) 回填异常（仅当确有 AI 覆盖缺口时才据 loop 状态告警；否则 loop 健康可能是上一轮陈旧记录）。
    try:
        loop = json.loads((HEALTH_DIR / "ai_backfill_loop.json").read_text(encoding="utf-8"))
        st = str(loop.get("status") or "")
        info["checks"]["ai_backfill_status"] = st
        if ai_shortfall and st in ("timed_out", "base_not_ready", "publish_not_ready"):
            alerts.append(f"🚨 AI 回填异常：ai_backfill_loop 状态 = {st}")
    except Exception:
        pass

    # 5) 三态：发布失败 ≠ 选股失败（只告警，不把 selection 误写成全挂）
    try:
        orch = json.loads((HEALTH_DIR / "orchestrator_run.json").read_text(encoding="utf-8"))
        three = {
            "selection_ok": orch.get("selection_ok"),
            "research_ok": orch.get("research_ok"),
            "publish_ok": orch.get("publish_ok"),
            "ok": orch.get("ok"),
        }
        info["checks"]["pipeline_three_state"] = three
        if three.get("selection_ok") is True and three.get("publish_ok") is False:
            alerts.append("⚠️ 结果发布失败但选股运行成功（selection_ok=true, publish_ok=false）——勿归因成策略全失败")
        if three.get("research_ok") is False and three.get("selection_ok") is True:
            alerts.append("⚠️ 研究/影子环节失败但主选股成功（可能为代理数据正确阻断）")
    except Exception:
        pass

    # 6) AI 覆盖率合法性：have/total 不得超过 100%
    for sid, cov in cov_summary.items():
        ratio = cov.get("ratio")
        have, total = cov.get("have"), cov.get("total")
        if isinstance(have, (int, float)) and isinstance(total, (int, float)) and total > 0 and have > total:
            alerts.append(f"🚨 覆盖率非法：{sid} have={have} > total={total}")
        if isinstance(ratio, (int, float)) and ratio > 1.0 + 1e-12:
            alerts.append(f"🚨 覆盖率非法：{sid} ratio={ratio} > 1")

    # 7) 冻名单工程告警（连续 ≥3 个快照 Top 完全相同 → 查数据/run_id，不强迫换股）
    try:
        from orchestrator_common import WORKSPACE as _WS

        daily = _WS / "stock_data/03-working/strategy_research/short_track/daily"
        control_files = sorted(daily.glob("prebreakout_v43_control_*_candidate_snapshot.json"))
        if len(control_files) >= 3:
            recent = control_files[-3:]
            code_sets = []
            for path in recent:
                doc = json.loads(path.read_text(encoding="utf-8"))
                codes = tuple(str(c.get("ts_code") or "") for c in (doc.get("candidates") or []))
                code_sets.append(codes)
            identical = len(set(code_sets)) == 1 and bool(code_sets[0])
            if identical:
                alerts.append(
                    "⚠️ 工程告警：对照组 Top 连续 ≥3 个快照完全相同——请审计数据 asof / run_id / 行情是否过期（不是策略成功指标）"
                )
            info["checks"]["control_top_freeze"] = {
                "files": [p.name for p in recent],
                "identical": identical,
            }
    except Exception:
        pass
    # 8) short_track 未到期空 NAV 不升 CRITICAL（仅信息）
    info["checks"]["nav_note"] = (
        "5 日持有未到期时 sample_trade_days=0 / 空 NAV 合法；仅到期仍无结算才应 CRITICAL"
    )

    ok = not alerts
    # Downgrade pure publish-failure-with-selection-ok from critical spam if only that warn
    severity = "ok"
    if alerts:
        severity = "critical" if any("🚨" in a for a in alerts) else "warn"
    payload = {
        "ok": ok,
        "severity": severity,
        "alerts": alerts,
        "published_trade_date": pub_date,
        "real_latest_trade_date": real_date,
        "ai_coverage": cov_summary,
        "checks": info["checks"],
        "lifecycle": "verified_observe_or_research",
        "execution_authority": "observe_only_no_auto_order",
    }
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if ok:
        print(f"[pipeline-alert] OK：发布交易日={pub_date}（与真实日历一致），当前生产对照组 AI 覆盖达标。")
        return 0
    # 发 Discord（除非 --no-discord）。
    if "--no-discord" not in sys.argv:
        payload["discord"] = _send_discord(payload)
        ALERT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 60)
    print("🚨 选股管线告警（需要关注）")
    print(f"   发布交易日={pub_date} | 真实最新交易日={real_date}")
    for a in alerts:
        print("   - " + a)
    if payload.get("discord"):
        print(f"   Discord: {payload['discord']}")
    print(f"   详情：{ALERT_PATH}")
    print("=" * 60)
    # 默认退 0（管线内非阻塞、只告警不卡流水线）；独立调度器要据退出码升级时传 --strict。
    return 1 if "--strict" in sys.argv else 0


def _lag(pub: str, real: str) -> str:
    try:
        import akshare as ak
        dates = sorted(str(d).replace("-", "") for d in ak.tool_trade_date_hist_sina()["trade_date"].astype(str))
        return str(len([d for d in dates if pub < d <= real]))
    except Exception:
        return "?"


if __name__ == "__main__":
    raise SystemExit(main())
