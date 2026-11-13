#!/usr/bin/env python3
"""O2C 前瞻OOS跟踪月报——改造方案 A1。

背景：greenfield_o2c_v1 已锁"研究观察"，现实角色=候选池弱排序信号 + 前瞻OOS纸面跟踪装置，
非可部署策略。参数已按 factor_factory/config/o2c_frozen_manifest.json 冻结签名，冻结期内
禁止回看调参；累积不重叠有效复盘周期数 >= min_cycles_for_review(40) 且 IR >= ir_min(0.5)
时才允许人工发起复议。

数据源（只读，不改动任何现有文件/表）：
  stock-report/data/latest/review_state_o2c.json
    ↑ 由 skills/stock-system-orchestrator/scripts/generate_o2c_review.py 权威写入。
    date_stats：仅含 data_complete=1（真实后验行情齐全）的有效复盘日，逐条已是"不重叠周期"
    （O2C hold_days=0 → 每个 recommend_date 对应一个不跨窗口重叠的 T+1 开→收 周期）。
    review_gate：该脚本自带的展示级门槛（required_review_days=20, ir_min=0.5），
    与本报告使用的复议级门槛（min_cycles_for_review=40）是两个不同层级，不要混淆。

本脚本只做二次汇总与"距40周期还差多少"的进度估算，不重新计算复盘本身，不写回 review_state_o2c.json，
不碰 generate_o2c_review.py 使用的任何 DB/JSON。

输出（本任务新增，仅此两个文件）：
  stock_data/03-working/health/o2c_tracking_report.md
  stock_data/03-working/health/o2c_tracking_report.json

用法：
  python3 generate_o2c_tracking_report.py
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
MANIFEST_PATH = WORKSPACE / "factor_factory" / "config" / "o2c_frozen_manifest.json"
REVIEW_STATE_PATH = WORKSPACE / "stock-report" / "data" / "latest" / "review_state_o2c.json"
OUT_MD = WORKSPACE / "stock_data" / "03-working" / "health" / "o2c_tracking_report.md"
OUT_JSON = WORKSPACE / "stock_data" / "03-working" / "health" / "o2c_tracking_report.json"

DATE_FMT = "%Y%m%d"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_date(d: str) -> datetime | None:
    try:
        return datetime.strptime(d, DATE_FMT)
    except (ValueError, TypeError):
        return None


def compute_information_ratio(excesses: list[float]) -> float | None:
    """年化 IR = 日均超额 / 日超额标准差 × sqrt(252)。与 generate_o2c_review.py 口径一致（独立复算校验）。"""
    if len(excesses) < 2:
        return None
    mean = statistics.fmean(excesses)
    sd = statistics.stdev(excesses)
    if sd <= 1e-9:
        return None
    return round((mean / sd) * (252 ** 0.5), 4)


def estimate_pace_and_eta(dates_asc: list[str], cycles_so_far: int, target_cycles: int) -> dict[str, Any]:
    """按当前节奏（近期不重叠周期产出速率）估算到达 target_cycles 还需多久。"""
    remaining = max(0, target_cycles - cycles_so_far)
    if remaining == 0:
        return {"cycles_remaining": 0, "estimated_calendar_days_remaining": 0,
                "estimated_eta_date": None, "pace_note": "已达标周期数（仍需同时满足 IR 门槛）"}
    if len(dates_asc) < 2:
        return {"cycles_remaining": remaining, "estimated_calendar_days_remaining": None,
                "estimated_eta_date": None, "pace_note": "样本不足2个有效周期，无法估算节奏"}
    d0 = parse_date(dates_asc[0])
    d1 = parse_date(dates_asc[-1])
    if not d0 or not d1 or d1 <= d0:
        return {"cycles_remaining": remaining, "estimated_calendar_days_remaining": None,
                "estimated_eta_date": None, "pace_note": "日期跨度异常，无法估算节奏"}
    span_calendar_days = (d1 - d0).days
    n = len(dates_asc)
    # 平均每个有效周期跨越多少自然日（含休市/数据缺失导致的非有效日）。
    avg_calendar_days_per_cycle = span_calendar_days / max(1, n - 1)
    est_days_remaining = round(avg_calendar_days_per_cycle * remaining)
    eta = None
    try:
        from datetime import timedelta
        eta = (d1 + timedelta(days=est_days_remaining)).strftime(DATE_FMT)
    except Exception:
        pass
    return {
        "cycles_remaining": remaining,
        "estimated_calendar_days_remaining": est_days_remaining,
        "estimated_eta_date": eta,
        "pace_note": f"按最近 {n} 个有效周期跨度 {span_calendar_days} 自然日的平均节奏"
                     f"（{avg_calendar_days_per_cycle:.2f} 自然日/周期）线性外推，非保证到达日期",
    }


def build_report() -> dict[str, Any]:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest = load_json(MANIFEST_PATH) if MANIFEST_PATH.exists() else None
    review_state = load_json(REVIEW_STATE_PATH) if REVIEW_STATE_PATH.exists() else None

    if review_state is None:
        return {
            "ok": False, "generated_at": now,
            "reason": "missing_review_state", "path": str(REVIEW_STATE_PATH),
        }

    min_cycles_for_review = 40
    ir_min = 0.5
    if manifest:
        min_cycles_for_review = manifest.get("reconsideration_rule", {}).get("min_cycles_for_review", 40)
        ir_min = manifest.get("review_gate", {}).get("ir_min", 0.5)

    date_stats = review_state.get("date_stats", [])  # 已是 data_complete=1 的不重叠有效周期
    # date_stats 来自 review_state 时按 recommend_date DESC 排列；这里按 recommend_date 升序重排用于节奏估算。
    valid_sorted_asc = sorted(date_stats, key=lambda r: r.get("recommend_date") or "")
    dates_asc = [r["recommend_date"] for r in valid_sorted_asc if r.get("recommend_date")]

    cycles_so_far = len(date_stats)
    excesses = [r["avg_excess_return_pct"] for r in date_stats if isinstance(r.get("avg_excess_return_pct"), (int, float))]
    hit_rates = [r["next_day_hit_rate_pct"] for r in date_stats if isinstance(r.get("next_day_hit_rate_pct"), (int, float))]
    rets = [r["avg_next_day_return_pct"] for r in date_stats if isinstance(r.get("avg_next_day_return_pct"), (int, float))]

    cumulative_avg_excess = round(statistics.fmean(excesses), 4) if excesses else None
    cumulative_avg_hit_rate = round(statistics.fmean(hit_rates), 2) if hit_rates else None
    cumulative_avg_return = round(statistics.fmean(rets), 4) if rets else None
    information_ratio_recomputed = compute_information_ratio(excesses)
    # review_gate 里已有官方 IR（来自 generate_o2c_review.py 权威计算）；优先展示官方值，重算值仅作交叉校验。
    official_ir = (review_state.get("review_gate") or {}).get("information_ratio")

    pace = estimate_pace_and_eta(dates_asc, cycles_so_far, min_cycles_for_review)

    ir_for_gate = official_ir if official_ir is not None else information_ratio_recomputed
    cycles_ok = cycles_so_far >= min_cycles_for_review
    ir_ok = ir_for_gate is not None and ir_for_gate >= ir_min
    reconsideration_status = "eligible_for_human_review" if (cycles_ok and ir_ok) else "locked_frozen_tracking"

    review_gate_status = (review_state.get("review_gate") or {}).get("status")

    report: dict[str, Any] = {
        "ok": True,
        "generated_at": now,
        "schema_version": "2026-07-02.o2c_tracking_report.v1",
        "strategy_id": review_state.get("strategy_id"),
        "strategy_name": review_state.get("strategy_name"),
        "manifest_path": str(MANIFEST_PATH),
        "manifest_version": (manifest or {}).get("manifest_version"),
        "frozen_at_date": (manifest or {}).get("frozen_at_date"),
        "frozen_at_git_hash": (manifest or {}).get("frozen_at_git_hash"),
        "review_state_source": str(REVIEW_STATE_PATH),
        "review_state_generated_at": review_state.get("generated_at"),

        "cumulative_cycles": {
            "definition": "不重叠有效复盘周期数：O2C hold_days=0，每个 data_complete=1 的 recommend_date 即1个不重叠周期",
            "cycles_so_far": cycles_so_far,
            "min_cycles_for_review": min_cycles_for_review,
            "cycles_remaining": pace["cycles_remaining"],
            "estimated_calendar_days_remaining": pace["estimated_calendar_days_remaining"],
            "estimated_eta_date": pace["estimated_eta_date"],
            "pace_note": pace["pace_note"],
            "date_range": {"from": dates_asc[0] if dates_asc else None, "to": dates_asc[-1] if dates_asc else None},
        },

        "cumulative_performance": {
            "avg_excess_return_pct": cumulative_avg_excess,
            "avg_hit_rate_pct": cumulative_avg_hit_rate,
            "avg_next_day_return_pct": cumulative_avg_return,
            "information_ratio_official": official_ir,
            "information_ratio_recomputed_here": information_ratio_recomputed,
            "ir_recompute_note": "本报告用 date_stats 的 avg_excess_return_pct 序列独立重算 IR 作交叉校验；"
                                 "与 review_state_o2c.json::review_gate.information_ratio 口径一致(年化，×sqrt(252))，"
                                 "数值应基本一致，若有出入以官方值(information_ratio_official)为准。",
        },

        "review_gate_current": review_state.get("review_gate"),

        "reconsideration_gate": {
            "rule": "累积不重叠有效复盘周期数 >= min_cycles_for_review 且 IR >= ir_min 才允许人工复议；期间禁止回看调参。",
            "min_cycles_for_review": min_cycles_for_review,
            "ir_min": ir_min,
            "cycles_ok": cycles_ok,
            "ir_ok": ir_ok,
            "ir_used_for_gate": ir_for_gate,
            "status": reconsideration_status,
            "review_gate_display_status": review_gate_status,
            "note": "reconsideration_gate 是本报告独有的更高阈值（40周期复议门槛），"
                    "review_gate_current(20周期展示门槛)来自 generate_o2c_review.py，两者不同层级，均展示以便对照。",
        },
    }
    return report


def render_markdown(r: dict[str, Any]) -> str:
    if not r.get("ok"):
        return f"# O2C 前瞻OOS跟踪月报\n\n生成失败：{r.get('reason')}（{r.get('path')}）\n"

    cyc = r["cumulative_cycles"]
    perf = r["cumulative_performance"]
    gate = r["reconsideration_gate"]
    rg = r.get("review_gate_current") or {}

    lines: list[str] = []
    lines.append("# O2C 前瞻OOS跟踪月报")
    lines.append("")
    lines.append(f"生成时间：{r['generated_at']}")
    lines.append(f"策略：{r.get('strategy_name')}（{r.get('strategy_id')}）")
    lines.append(f"角色定位：候选池弱排序信号 + 前瞻OOS纸面跟踪装置（已锁研究观察，非可部署买入依据）")
    lines.append(f"参数冻结签名：manifest_version={r.get('manifest_version')}，冻结日={r.get('frozen_at_date')}，"
                 f"冻结时git hash={r.get('frozen_at_git_hash')}")
    lines.append(f"数据源：{r.get('review_state_source')}（generated_at={r.get('review_state_generated_at')}）")
    lines.append("")

    lines.append("## 1. 累积不重叠复盘周期")
    lines.append("")
    lines.append(f"- 周期定义：{cyc['definition']}")
    lines.append(f"- 已累积周期数：**{cyc['cycles_so_far']}** / 目标 {cyc['min_cycles_for_review']}")
    lines.append(f"- 还差：{cyc['cycles_remaining']} 个周期")
    if cyc.get("estimated_calendar_days_remaining") is not None:
        lines.append(f"- 按当前节奏预计还需：约 {cyc['estimated_calendar_days_remaining']} 自然日"
                     f"（预计到达日期：{cyc.get('estimated_eta_date')}）")
    lines.append(f"- 节奏估算说明：{cyc['pace_note']}")
    lines.append(f"- 覆盖区间：{cyc['date_range']['from']} ~ {cyc['date_range']['to']}")
    lines.append("")

    lines.append("## 2. 累计表现（跨全部有效周期均值）")
    lines.append("")
    lines.append(f"- 累计平均超额收益：{perf['avg_excess_return_pct']}%")
    lines.append(f"- 累计平均命中率：{perf['avg_hit_rate_pct']}%")
    lines.append(f"- 累计平均T+1日内收益（毛，未扣费）：{perf['avg_next_day_return_pct']}%")
    lines.append(f"- 信息比率IR（官方值，来自 generate_o2c_review.py）：{perf['information_ratio_official']}")
    lines.append(f"- 信息比率IR（本报告独立重算，交叉校验）：{perf['information_ratio_recomputed_here']}")
    lines.append("")

    lines.append("## 3. 复议门槛（本报告，40周期+IR≥0.5）")
    lines.append("")
    lines.append(f"- 规则：{gate['rule']}")
    lines.append(f"- 周期数达标：{'是' if gate['cycles_ok'] else '否'}（{cyc['cycles_so_far']}/{gate['min_cycles_for_review']}）")
    lines.append(f"- IR达标：{'是' if gate['ir_ok'] else '否'}（{gate['ir_used_for_gate']}/{gate['ir_min']}）")
    lines.append(f"- **当前状态：{gate['status']}**")
    lines.append(f"- 说明：{gate['note']}")
    lines.append("")

    lines.append("## 4. review_gate 现值（20周期展示门槛，来自 generate_o2c_review.py，供对照）")
    lines.append("")
    lines.append(f"- status: {rg.get('status')}")
    lines.append(f"- valid_review_days: {rg.get('valid_review_days')} / required {rg.get('required_review_days')}")
    lines.append(f"- information_ratio: {rg.get('information_ratio')} (ir_min={rg.get('ir_min')})")
    lines.append(f"- avg_excess_return_pct: {rg.get('avg_excess_return_pct')}%")
    lines.append(f"- hit_rate_pct: {rg.get('hit_rate_pct')}%（信息项，非硬门槛）")
    lines.append(f"- summary: {rg.get('summary')}")
    lines.append("")

    lines.append("## 5. 纪律提醒")
    lines.append("")
    lines.append("- 期间（未同时满足40周期+IR≥0.5）禁止基于回看结果调整因子集/权重/hold_days/成本假设/门槛阈值。")
    lines.append("- 任何参数变更必须新开 manifest_version，复盘周期计数从0重新累积，不得延续本版本已累积的周期数。")
    lines.append(f"- 冻结参数详见：{r.get('manifest_path')}")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    report = build_report()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "ok": report.get("ok"),
        "out_md": str(OUT_MD),
        "out_json": str(OUT_JSON),
        "cycles_so_far": report.get("cumulative_cycles", {}).get("cycles_so_far"),
        "min_cycles_for_review": report.get("cumulative_cycles", {}).get("min_cycles_for_review"),
        "information_ratio_official": report.get("cumulative_performance", {}).get("information_ratio_official"),
        "reconsideration_status": report.get("reconsideration_gate", {}).get("status"),
    }, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
