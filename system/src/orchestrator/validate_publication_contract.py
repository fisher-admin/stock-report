#!/usr/bin/env python3
"""发布合同 v2 校验：纯读 stock-report/data/latest/*.json，校验 12 项（含每股链接化合同）。

接入发布流程：发布层 + generate_latest_states 跑完后运行；带 --strict 时校验失败非零退出、阻断 deploy。
不带 --strict 仅打印 FAIL/WARN（灰度期观测用）。
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator_common import PUBLISHED_REPO  # noqa: E402

# 校验目标 latest 目录：默认发布仓 data/latest；可用 --latest-dir <path> 或 OPENCLAW_VALIDATE_LATEST_DIR
# 覆盖，供任意克隆的 pre-push 钩子校验自身待推送内容（防旧合同从任一克隆推上线）。
def _resolve_latest() -> Path:
    for i, a in enumerate(sys.argv):
        if a == "--latest-dir" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1]).expanduser()
    env = os.environ.get("OPENCLAW_VALIDATE_LATEST_DIR")
    if env:
        return Path(env).expanduser()
    return PUBLISHED_REPO / "data" / "latest"


LATEST = _resolve_latest()
PREBREAKOUT_ID = "prebreakout_v41"
O2C_ID = "greenfield_o2c_v1"
T1_ID = "t1_factor_v1"
GATE_STATUSES = {"pass", "warn", "fail"}


def _load(name: str):
    return json.loads((LATEST / name).read_text(encoding="utf-8"))


def _is_return_field(name: object) -> bool:
    text = str(name or "").lower()
    return "return" in text or "收益" in text


def _impossible_json_returns(value: object, *, path: str = "", return_context: bool = False) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            findings.extend(
                _impossible_json_returns(
                    child,
                    path=child_path,
                    return_context=return_context or _is_return_field(key),
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(
                _impossible_json_returns(
                    child,
                    path=f"{path}[{index}]",
                    return_context=return_context,
                )
            )
    elif return_context and isinstance(value, (int, float)) and not isinstance(value, bool) and value <= -99.0:
        findings.append(f"{path}={value}")
    return findings


def _scan_public_return_artifacts(latest: Path) -> list[str]:
    findings: list[str] = []
    json_paths = [
        latest / name
        for name in (
            "review_state.json",
            "review_state_unified.json",
            "review_track_latest.json",
            "prebreakout_shadow_watch.json",
            "dual_track_state.json",
            "strategy_evaluation.json",
        )
    ]
    analytics = latest.parent / "recommendation_analytics"
    json_paths.extend(
        analytics / name
        for name in (
            "latest.json",
            "prebreakout_summary.json",
            "prebreakout_recommendations.json",
            "o2c_factor_summary.json",
            "o2c_factor_recommendations.json",
            "industry_heatmap.json",
            "industry_heatmap_latest.json",
        )
    )
    for path in json_paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        findings.extend(f"{path.name}:{item}" for item in _impossible_json_returns(payload))

    csv_paths = [
        latest / "recommendation_history.csv",
        analytics / "prebreakout_recommendations.csv",
    ]
    for path in csv_paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                return_fields = [field for field in (reader.fieldnames or []) if _is_return_field(field)]
                for row_number, row in enumerate(reader, start=2):
                    for field in return_fields:
                        raw = row.get(field)
                        if raw in (None, ""):
                            continue
                        try:
                            number = float(str(raw).strip().rstrip("%"))
                        except ValueError:
                            continue
                        if number <= -99.0:
                            findings.append(f"{path.name}:row{row_number}.{field}={number}")
        except OSError:
            continue
    return findings


def _active_strategy_scope(rec: dict, errs: list[str]) -> list[str]:
    strategies = rec.get("strategies") or {}
    declared = rec.get("active_strategy_ids")
    if not isinstance(declared, list) or not declared:
        errs.append("recommendation_state 缺非空 active_strategy_ids")
        return sorted(strategies)
    active = [str(strategy_id) for strategy_id in declared if str(strategy_id)]
    if len(active) != len(declared) or len(active) != len(set(active)):
        errs.append("recommendation_state.active_strategy_ids 含空值或重复项")
    if set(active) != set(strategies):
        errs.append("recommendation_state.strategies 必须与 active_strategy_ids 完全一致")
    archived = rec.get("archived_strategies") or {}
    if not isinstance(archived, dict):
        errs.append("recommendation_state.archived_strategies 必须为对象")
        archived = {}
    overlap = sorted(set(active) & set(archived))
    if overlap:
        errs.append(f"活跃与归档策略重叠: {overlap}")
    return active


def main() -> int:
    errs: list[str] = []
    warns: list[str] = []

    # 0) 所有 latest JSON 能正常解析
    review_contract_name = (
        "review_state_unified.json"
        if (LATEST / "review_state_unified.json").exists()
        else "review_track_latest.json"
    )
    files = {
        "decision_state.json": None, "recommendation_state.json": None,
        "strategy_run_state.json": None, review_contract_name: None,
        "adjustment_log.json": None, "system_health.json": None,
    }
    for name in files:
        try:
            files[name] = _load(name)
        except Exception as exc:
            errs.append(f"{name} 解析失败: {exc}")
    if errs:  # JSON 都解析不了，后续无意义
        for e in errs:
            print("FAIL:", e)
        print(f"contract v2: {len(errs)} fail, 0 warn")
        return 1 if "--strict" in sys.argv else 0

    dec, rec = files["decision_state.json"], files["recommendation_state.json"]
    run, rev, adj = files["strategy_run_state.json"], files[review_contract_name], files["adjustment_log.json"]

    # 1) decision.gates 四闸齐全、各有 status/summary/reasons、status 合法
    g = dec.get("gates") or {}
    for k in ("freshness_gate", "market_gate", "strategy_gate", "candidate_gate"):
        gg = g.get(k)
        if not isinstance(gg, dict):
            errs.append(f"decision_state.gates 缺 {k}")
            continue
        for f in ("status", "summary", "reasons"):
            if f not in gg:
                errs.append(f"decision_state.gates.{k} 缺 {f}")
        if gg.get("status") not in GATE_STATUSES:
            errs.append(f"decision_state.gates.{k}.status 非法: {gg.get('status')}")

    rec_strats = rec.get("strategies") or {}
    active_sids = _active_strategy_scope(rec, errs)
    # 2) recommendation 每策略扁平合同字段
    for sid in active_sids:
        s = rec_strats.get(sid) or {}
        for f in ("ai_coverage", "research_only", "strategy_gate", "data_freshness"):
            if f not in s:
                errs.append(f"recommendation_state.strategies.{sid} 缺 {f}")
        if (s.get("strategy_gate") or {}).get("status") not in GATE_STATUSES:
            errs.append(f"{sid}.strategy_gate.status 非法")

    # 3) T1 ai_score 全 null 且模板不计 AI 覆盖
    t1 = rec_strats.get(T1_ID) or {}
    if any(it.get("ai_score") is not None for it in (t1.get("items") or [])):
        errs.append("T1 存在非 null 的 ai_score（伪 AI 分）")
    if (t1.get("ai_coverage") or {}).get("have", 0) != 0:
        warns.append("T1 ai_coverage.have != 0（模板疑似计入 AI 覆盖）")

    # 4) 每股三段式 + ai_coverage_counted
    for sid in active_sids:
        for it in (rec_strats.get(sid) or {}).get("items") or []:
            missing = [f for f in ("raw_action", "gate_adjusted_action", "final_action", "ai_coverage_counted") if f not in it]
            if missing:
                errs.append(f"{sid} 个股缺字段 {missing}")
                break

    # 5) strategy_gate.status != pass → 该策略不得有 final_action == main
    for sid in active_sids:
        s = rec_strats.get(sid) or {}
        if (s.get("strategy_gate") or {}).get("status") != "pass":
            if any(it.get("final_action") == "main" for it in (s.get("items") or [])):
                errs.append(f"{sid} 未达门槛却有 final_action=main（研究观察策略不得出现买入）")

    # 6) 每策略推荐数 20（除非显式标记数据不足）
    for sid in active_sids:
        s = rec_strats.get(sid) or {}
        n = len(s.get("items") or [])
        if n != 20 and not s.get("insufficient_data") and (s.get("strategy_gate") or {}).get("verdict") != "research_only":
            warns.append(f"{sid} 推荐数={n}（非20且未标注数据不足）")

    # 7) strategy_run_state.strategies[] 存在且与 runs 并存
    if not isinstance(run.get("strategies"), list) or not run.get("strategies"):
        errs.append("strategy_run_state 缺 strategies[]")
    if not run.get("runs"):
        warns.append("strategy_run_state 缺 runs（旧兼容字段）")

    # 8) review 无空策略 + 默认日有效
    rev_strats = rev.get("strategies") or {}
    for sid in active_sids:
        st = rev_strats.get(sid)
        if not isinstance(st, dict) or not st.get("strategy_id") or not st.get("strategy_name"):
            errs.append(f"review_state_unified.strategies.{sid} 为 null/空（缺 strategy_id/name）")
    drd = rev.get("default_review_date")
    if drd is not None and (rev.get("available_dates") and drd not in rev["available_dates"]):
        warns.append("default_review_date 不在 available_dates 中")

    # 9) adjustment_log 每条三段式 + 无无信息原因 + 全逐股
    for r in adj.get("rows") or []:
        if "stock_code" not in r:
            errs.append("adjustment_log 混入非逐股条目（schema bug 残留）")
            break
        if "保持原策略动作。" in (r.get("reasons") or []):
            errs.append("adjustment_log 残留无信息原因「保持原策略动作」")
            break
        if not all(f in r for f in ("raw_action", "gate_adjusted_action", "final_action", "changed")):
            errs.append("adjustment_log 条目缺三段式/changed")
            break

    # 10) 跨文件 trade_date 一致
    if len({dec.get("trade_date"), rec.get("trade_date"), run.get("trade_date")}) > 1:
        errs.append("decision/recommendation/strategy_run trade_date 不一致")

    # 11) candidate_state.json 合同字段（阶段5）：研究观察个股不得 final_action=main（硬）；缺字段记 warn。
    try:
        cand = _load("candidate_state.json")
        cands = cand.get("candidates") or []
        if cands:
            missing_fields = [f for f in ("raw_action", "gate_adjusted_action", "final_action", "research_only", "ai_coverage_counted")
                              if f not in cands[0]]
            if missing_fields:
                warns.append(f"candidate_state 候选缺合同字段 {missing_fields}（待 generate_latest_states 重生成）")
            for c in cands:
                if c.get("research_only") and c.get("final_action") == "main":
                    errs.append("candidate_state 研究观察候选出现 final_action=main")
                    break
    except Exception:
        warns.append("candidate_state.json 不可读（非阻断）")

    # 12) 个股链接化合同（任务8）：每股链接/锚点/面板字段齐全、可点开、锚点全局唯一、O2C code 非空。
    allowed_ai_types = {"trading_ai", "o2c_ai", "t1_research_ai", "t1_template_note", "none"}
    seen_anchors: dict[str, str] = {}
    link_field_missing = 0
    for sid in active_sids:
        items = (rec_strats.get(sid) or {}).get("items") or []
        for idx, it in enumerate(items):
            # 12a) 链接/锚点/面板字段齐全
            for f in ("code", "name", "display_code", "analysis_anchor_id", "analysis_panel_id",
                      "analysis_link_href", "has_ai_analysis", "ai_analysis_type", "ai_coverage_counted"):
                if not it.get(f) and it.get(f) not in (False, 0):
                    errs.append(f"{sid}[{idx}] 缺个股链接合同字段 {f}")
                    link_field_missing += 1
                    break
            if link_field_missing:
                break
            anchor = str(it.get("analysis_anchor_id") or "")
            href = str(it.get("analysis_link_href") or "")
            panel = str(it.get("analysis_panel_id") or "")
            # 12b) 锚点/href/panel 命名规范
            if not anchor.startswith("stock-"):
                errs.append(f"{sid}[{idx}] analysis_anchor_id 不以 stock- 开头: {anchor}")
            if not href.startswith("#stock-"):
                errs.append(f"{sid}[{idx}] analysis_link_href 不以 #stock- 开头: {href}")
            if panel != f"{anchor}-analysis":
                errs.append(f"{sid}[{idx}] analysis_panel_id 与锚点不一致: {panel} != {anchor}-analysis")
            if href != f"#{anchor}":
                errs.append(f"{sid}[{idx}] analysis_link_href 与锚点不一致: {href} != #{anchor}")
            # 12c) 锚点全局唯一（三策略合计）
            if anchor in seen_anchors:
                errs.append(f"锚点重复: {anchor}（{seen_anchors[anchor]} 与 {sid}[{idx}]）")
            else:
                seen_anchors[anchor] = f"{sid}[{idx}]"
            # 12c2) 名称未解析（用代码占位）→ 透明告警，非阻断（诚实化，不编造名称）
            _it_name = str(it.get("name") or "")
            _it_dcode = str(it.get("display_code") or "")
            if it.get("name_resolved") is False or _it_name == _it_dcode:
                warns.append(f"{sid}[{idx}] 名称未解析，前端以代码 {it.get('display_code')} 展示（上游无名称，未编造）")
            # 12c3) 伪解析硬校验（001312.SZ 次新股事故护栏）：name_resolved=true 却 name==代码串
            #   → 自相矛盾（声称已解析实为代码占位），strict 下阻断。字典缺次新时上游伪名污染。
            _code_forms = {str(it.get(k) or "") for k in ("display_code", "ts_code", "code")}
            _code_forms.discard("")
            if it.get("name_resolved") is True and (_it_name == _it_dcode or _it_name in _code_forms):
                errs.append(f"{sid}[{idx}] 伪解析: name_resolved=true 但 name={_it_name} 等于代码串（自相矛盾，字典缺名却谎称已解析）")
            # 12d) ai_analysis_type 合法
            at = it.get("ai_analysis_type")
            if at not in allowed_ai_types:
                errs.append(f"{sid}[{idx}] ai_analysis_type 非法: {at}")
            # 12e) has_ai_analysis 与 type 自洽
            if bool(it.get("has_ai_analysis")) != (at != "none"):
                errs.append(f"{sid}[{idx}] has_ai_analysis 与 ai_analysis_type 不自洽")
            # 12f) 模板/研究/未覆盖三类都不得计入真实 AI 覆盖
            if at in ("t1_template_note", "none") and it.get("ai_coverage_counted"):
                errs.append(f"{sid}[{idx}] {at} 不应 ai_coverage_counted=true（伪 AI 覆盖）")
        if link_field_missing:
            break
    # 12g) O2C code 必须非空（合同要求，便于锚点与跳转）
    for idx, it in enumerate((rec_strats.get(O2C_ID) or {}).get("items") or []):
        c = str(it.get("code") or "").strip()
        if not c or c.lower() == "none":
            errs.append(f"greenfield_o2c_v1[{idx}] code 为空（合同要求 O2C code 非 null）")
            break
    # 12h) O2C 若无 ai_summary 必须显式为「未覆盖」(ai_analysis_type=none)，不得伪装成已覆盖
    for idx, it in enumerate((rec_strats.get(O2C_ID) or {}).get("items") or []):
        has_sum = bool(str(it.get("ai_summary") or "").strip())
        if not has_sum and it.get("ai_analysis_type") not in ("none", "o2c_ai"):
            warns.append(f"greenfield_o2c_v1[{idx}] 无 ai_summary 却 type={it.get('ai_analysis_type')}（应为 none）")
            break

    # 13) O2C 独立 AI 覆盖硬校验（任务VIII）：每只须有真实 o2c_ai；覆盖率自洽；未满覆盖必须显式 pending 否则 strict 失败。
    o2c_active = O2C_ID in active_sids
    o2c = rec_strats.get(O2C_ID) or {}
    if not o2c_active:
        o2c = {
            "items": [],
            "research_only": True,
            "review_gate": {
                "status": "n/a",
                "valid_review_days": 0,
                "required_review_days": 3,
            },
        }
    o2c_items = o2c.get("items") or []
    allow_pending = bool(o2c.get("allow_o2c_ai_pending")) or os.environ.get("OPENCLAW_ALLOW_O2C_AI_PENDING") in ("1", "true", "True")
    covered = 0
    for idx, it in enumerate(o2c_items):
        is_cov = (
            str(it.get("ai_source_kind") or "") == "o2c_ai_analysis"
            and it.get("ai_analysis_type") == "o2c_ai"
            and bool(it.get("has_ai_analysis"))
            and bool(str(it.get("ai_summary") or "").strip())
            and bool(str(it.get("ai_advice") or "").strip())
            and bool(it.get("ai_coverage_counted"))
        )
        if is_cov:
            covered += 1
            # 13a) 已覆盖个股字段必须齐全
            for f in ("code", "name", "ai_summary", "ai_advice"):
                if not str(it.get(f) or "").strip():
                    errs.append(f"o2c[{idx}] 标记 o2c_ai 但缺 {f}")
            if it.get("ai_score") is not None:
                errs.append(f"o2c[{idx}] o2c_ai 的 ai_score 必须为 null（不造假分），实为 {it.get('ai_score')}")
        else:
            # 未覆盖个股：必须显式 none，不得伪装
            if it.get("ai_analysis_type") == "o2c_ai" or it.get("has_ai_analysis"):
                errs.append(f"o2c[{idx}] 无完整 o2c_ai 却标记已覆盖（伪装）")
    total = len(o2c_items)
    cov_obj = o2c.get("ai_coverage") or {}
    # 13b) ai_coverage 自洽：have==covered，total==items，value==have/total
    if total:
        if cov_obj.get("have") != covered:
            errs.append(f"o2c ai_coverage.have={cov_obj.get('have')} 与真实覆盖数 {covered} 不一致")
        if cov_obj.get("total") != total:
            errs.append(f"o2c ai_coverage.total={cov_obj.get('total')} 与 items 数 {total} 不一致")
        exp_val = round(covered / total, 3)
        if cov_obj.get("value") is not None and abs(float(cov_obj.get("value")) - exp_val) > 0.011:
            errs.append(f"o2c ai_coverage.value={cov_obj.get('value')} 与 have/total={exp_val} 不一致")
        # 13c) 覆盖率<60% 不得显示 pass
        if covered / total < 0.6 and cov_obj.get("status") == "pass":
            errs.append("o2c ai_coverage<60% 却 status=pass（不允许伪 pass）")
        # 13d) 未满 20/20：必须显式 allow_o2c_ai_pending，否则 strict 失败（线上不许 silent pending）
        if covered < total:
            msg = f"o2c AI 覆盖 {covered}/{total}（未满）"
            if allow_pending:
                warns.append(msg + "，已显式 allow_o2c_ai_pending=true，放行")
            else:
                errs.append(msg + "，且未显式 allow_o2c_ai_pending → strict 拒绝 silent pending")

    # 14) O2C 复盘升级门槛（任务IX）：review_gate 必须存在且自洽；样本不足/未通过门槛必须 research_only；
    #     不允许样本不足却显示可买入。复盘页数据（review_state_o2c.date_stats）须与 valid_review_days 对齐。
    rg = o2c.get("review_gate")
    if not isinstance(rg, dict):
        errs.append("greenfield_o2c_v1 缺 review_gate（未跑 generate_o2c_review 或发布层未接入）")
    else:
        vrd = rg.get("valid_review_days")
        if not isinstance(vrd, (int, float)):
            errs.append(f"o2c review_gate.valid_review_days 非数字: {vrd}")
            vrd = 0
        req = rg.get("required_review_days") or 3
        ro = bool(o2c.get("research_only"))
        # 14a) 样本不足 → 必须 research_only
        if vrd < req and not ro:
            errs.append(f"o2c 有效复盘样本 {vrd}<{req} 却 research_only=false（样本不足不得升级）")
        # 14b) research_only=false（已升级）→ 必须同时满足 review_gate.pass + ai_coverage pass + data_freshness pass
        if not ro:
            if rg.get("status") != "pass":
                errs.append(f"o2c 已升级(research_only=false) 但 review_gate.status={rg.get('status')}（非 pass 不得升级）")
            if (o2c.get("ai_coverage") or {}).get("status") != "pass":
                errs.append("o2c 已升级但 ai_coverage 未 pass")
            if (o2c.get("data_freshness") or {}).get("status") != "pass":
                errs.append("o2c 已升级但 data_freshness 未 pass")
        # 14c) research_only=true → 所有 O2C 个股 final_action 只能 watch/avoid/research（不得 main 买入）
        if ro:
            bad = [it.get("code") for it in o2c_items if it.get("final_action") == "main"]
            if bad:
                errs.append(f"o2c research_only=true 却有 final_action=main: {bad[:5]}（样本不足/未达门槛不得显示买入）")
        # 14d) review_gate.status 合法
        if rg.get("status") not in ("pass", "fail", "n/a"):
            errs.append(f"o2c review_gate.status 非法: {rg.get('status')}")
    # 14e) 复盘页数据：review_state_o2c.date_stats 应与 valid_review_days 对齐（防页面显示空复盘）
    # 一致性检查升级为 errs（原仅 warns，7/2 体检发现旧快照未被拦截）：--strict 下阻断发布。
    if o2c_active:
        try:
            o2c_review = _load("review_state_o2c.json")
            ds = o2c_review.get("date_stats")
            rgate = o2c_review.get("review_gate") or {}
            if not isinstance(ds, list):
                errs.append("review_state_o2c.json 缺 date_stats 数组（复盘页将无 O2C 明细）")
            elif isinstance(rg, dict) and rg.get("valid_review_days") not in (None, len(ds)):
                errs.append(f"review_state_o2c.date_stats 数 {len(ds)} 与 review_gate.valid_review_days {rg.get('valid_review_days')} 不一致")
            if rgate.get("status") not in ("pass", "fail", "n/a", None):
                errs.append(f"review_state_o2c.review_gate.status 非法: {rgate.get('status')}")
        except Exception:
            warns.append("review_state_o2c.json 不可读（复盘页将缺 O2C 数据；建议先跑 generate_o2c_review）")

    # 15) 发布新鲜度硬校验（任务D）：数据源日期落后决策日却未带 data_stale 标记 → error（防止前端把旧快照当“最新”展示）。
    for sid in active_sids:
        s = rec_strats.get(sid) or {}
        s_source_date = str(s.get("source_date") or "")
        s_trade_date = str(rec.get("trade_date") or "")
        is_behind = bool(s_source_date and s_trade_date and s_source_date != s_trade_date)
        if is_behind and not s.get("data_stale"):
            errs.append(
                f"{sid} 数据源日期({s_source_date})落后决策日({s_trade_date})却未标记 data_stale=true（旧快照可能被当作最新展示）"
            )
        if s.get("data_stale") and not s.get("stale_source_date"):
            errs.append(f"{sid} data_stale=true 却缺 stale_source_date（无法定位具体旧快照日期）")

    # 16) prebreakout_v41 硬堵漏洞（合同层兜底，与任务A呼应）：该策略任何条目 final_action 不得为 main/买入级。
    pre_bad = [
        it.get("code") or it.get("stock_code")
        for it in (rec_strats.get(PREBREAKOUT_ID) or {}).get("items") or []
        if str(it.get("final_action") or "") in ("main", "买入", "主攻")
    ]
    if pre_bad:
        errs.append(f"prebreakout_v41 出现 final_action=main/买入级（该策略已冻结复盘IR-7.71无边际，禁止对外买入建议）: {pre_bad[:5]}")

    # 17) 收盘一致性（20260703两起"实时冒充收盘"事故的护栏）：定调/指数不得与当日涨跌面自相矛盾。
    try:
        ctx = _load("market_context.json")
        mstate = _load("market_state.json")
        # 17a) 涨跌面来源：优先 close_actuals.breadth，回退 market_summary.positive_sector_ratio
        breadth = None
        ca = ctx.get("close_actuals") or {}
        if isinstance(ca.get("breadth"), (int, float)):
            breadth = float(ca["breadth"])
        elif isinstance((mstate.get("market_summary") or {}).get("positive_sector_ratio"), (int, float)):
            breadth = float(mstate["market_summary"]["positive_sector_ratio"])
        gate = str(ctx.get("gate_signal") or "")
        # 17b) 普涨日(涨跌面>55%)判 risk_off = 语义矛盾 → error
        if gate == "risk_off" and breadth is not None and breadth > 0.55:
            errs.append(f"收盘定调矛盾: gate_signal=risk_off 但当日涨跌面 breadth={breadth:.2%}>55%(普涨) —— 疑似盘前情绪冒充收盘定调")
        # 17c) 三大指数涨跌方向须与涨跌面大体一致：>55%普涨日不得有指数收跌;<45%普跌日不得有指数收涨。
        # 20260710修: 方向背离本身不能证明数据脏——当晚实测真实分化日(全市场68.3%上涨/中位数+1.23%,
        # 但权重砸盘致创业板收-4.37%), 指数经tushare独立复核为当日结算收盘。规则意图是抓盘中/陈旧指数,
        # 故仅当涉事指数"不是当日exact_close"时才 error; 已核实当日收盘的真实分化降级 warn 留痕。
        ss = mstate.get("session_snapshot") or {}
        ctx_trade_date = str(ctx.get("trade_date") or "")
        idx = {k: (ss.get(k) or {}).get("change_pct") for k in ("shanghai", "shenzhen", "chinext")}
        idx = {k: v for k, v in idx.items() if isinstance(v, (int, float))}
        idx_all_settled = bool(idx) and all(
            str((ss.get(k) or {}).get("source_kind") or "") == "exact_close"
            and str((ss.get(k) or {}).get("as_of") or "") == ctx_trade_date
            for k in idx
        )
        if breadth is not None and idx:
            contradiction = None
            if breadth > 0.55 and any(v < -0.3 for v in idx.values()):
                contradiction = f"涨跌面{breadth:.0%}普涨但有指数收跌>0.3% {idx}"
            if breadth < 0.45 and any(v > 0.3 for v in idx.values()):
                contradiction = f"涨跌面{breadth:.0%}普跌但有指数收涨>0.3% {idx}"
            if contradiction:
                if idx_all_settled:
                    warns.append(f"指数与涨跌面方向背离(指数已核实为当日exact_close, 判定为真实分化行情): {contradiction}")
                else:
                    errs.append(f"指数与涨跌面矛盾: {contradiction} —— 疑似指数取到盘中/陈旧值")
        # 17d) 指数来源诚实性：收盘发布时三大指数不应是 intraday_spot(现价冒充收盘)
        spot_idx = [k for k in ("shanghai", "shenzhen", "chinext") if str((ss.get(k) or {}).get("source_kind") or "") == "intraday_spot"]
        if spot_idx:
            warns.append(f"指数来源为盘中现价(intraday_spot)而非结算收盘: {spot_idx} —— 收盘发布应走 tushare exact_close")
    except FileNotFoundError:
        warns.append("market_context.json / market_state.json 缺失，跳过收盘一致性校验")
    except Exception as _e:
        warns.append(f"收盘一致性校验异常(非阻断): {type(_e).__name__}: {_e}")

    # 18) 双轨观察与评价完整性：发布页必须展示三组短线+事件轨，并持续保持只观察。
    try:
        dual = _load("prebreakout_shadow_watch.json")
        if dual.get("contract_version") != "dual_track_v1":
            errs.append(f"双轨合同版本非法: {dual.get('contract_version')}")
        if str(dual.get("trade_date") or "") != str(rec.get("trade_date") or ""):
            errs.append("双轨合同 trade_date 与推荐合同不一致")
        if dual.get("flow_status") != "healthy":
            errs.append(f"双轨流程状态非 healthy: {dual.get('flow_status')}")
        if dual.get("execution_authority") != "observe_only_no_auto_order":
            errs.append("双轨总合同出现自动交易权限")
        expected_counts = {
            "prebreakout_v43_control": 20,
            "prebreakout_v43_top15": 15,
            "prebreakout_v44_balanced": 20,
        }
        strategy_rows = dual.get("short_track_strategies") or []
        actual_ids = [str(item.get("strategy_id") or "") for item in strategy_rows if isinstance(item, dict)]
        if set(actual_ids) != set(expected_counts) or len(actual_ids) != len(expected_counts):
            errs.append(f"双轨策略集合不完整: {actual_ids}")
        for strategy in strategy_rows:
            if not isinstance(strategy, dict):
                errs.append("双轨策略条目不是对象")
                continue
            sid = str(strategy.get("strategy_id") or "")
            if sid not in expected_counts:
                continue
            candidates = strategy.get("candidates") or []
            expected = expected_counts[sid]
            declared = strategy.get("candidate_count")
            if declared != expected or len(candidates) != expected:
                errs.append(f"{sid} 候选数必须为 {expected}（声明 {declared}，实际 {len(candidates)}）")
            if strategy.get("execution_authority") != "observe_only_no_auto_order":
                errs.append(f"{sid} 出现自动交易权限")
            for idx, candidate in enumerate(candidates, start=1):
                if candidate.get("used_proxy") is not False:
                    errs.append(f"{sid}[{idx}] 使用代理数据")
                    break
                if int(candidate.get("rank_change") or 0) != 0:
                    errs.append(f"{sid}[{idx}] rank_change 非 0")
                    break
                if int(candidate.get("rank") or 0) != idx:
                    errs.append(f"{sid}[{idx}] 排名不连续")
                    break
        event_track = dual.get("event_track") or {}
        if event_track.get("strategy_id") != "event_quality_drift_v1":
            errs.append("双轨合同缺 event_quality_drift_v1")
        if event_track.get("execution_authority") != "observe_only_no_auto_order":
            errs.append("event_quality_drift_v1 出现自动交易权限")
    except Exception as exc:
        errs.append(f"prebreakout_shadow_watch.json 双轨合同不可读: {exc}")

    try:
        evaluation = _load("strategy_evaluation.json")
        if evaluation.get("contract_version") != "evaluation_integrity_v2":
            errs.append(f"评价合同版本非法: {evaluation.get('contract_version')}")
        integrity = evaluation.get("integrity") or {}
        if int(integrity.get("fake_or_impossible_return_count") or 0) != 0:
            errs.append("评价库仍含伪造或不可能收益")
        if int(integrity.get("proxy_rows") or 0) != 0:
            errs.append("评价库仍含代理数据")
        if int(integrity.get("rank_changed_rows") or 0) != 0:
            errs.append("评价库仍含 AI/下游改排名记录")
    except Exception as exc:
        errs.append(f"strategy_evaluation.json 评价合同不可读: {exc}")

    # 19) 数据库审计通过后，公开副本也不得残留 -100% 缺价占位。
    public_impossible_returns = _scan_public_return_artifacts(LATEST)
    if public_impossible_returns:
        errs.append(
            "公开发布文件仍含不可能收益: "
            + "; ".join(public_impossible_returns[:10])
            + (f"（共 {len(public_impossible_returns)} 处）" if len(public_impossible_returns) > 10 else "")
        )

    for w in warns:
        print("WARN:", w)
    for e in errs:
        print("FAIL:", e)
    print(f"contract v2: {len(errs)} fail, {len(warns)} warn ({LATEST})")
    return 1 if (errs and "--strict" in sys.argv) else 0


if __name__ == "__main__":
    raise SystemExit(main())
