#!/usr/bin/env python3
"""启动前夕（prebreakout_v41）AI 分析生成器——稳定直连 litellm，替换易卡死的 daily_stock_analysis bridge。

背景（2026-06-28）：bridge_selection_to_analysis 走 LLMToolAdapter 的 Router 包装，在交互/部分环境
会卡死（0625/0626 实测 10+ 分钟 0 产出 → AI 覆盖 0/20）。本生成器复用 generate_o2c_ai 已验证稳定的
Copilot SDK 直调路径（2026-11-11 起：Node 子进程调 @github/copilot-sdk），从启动前夕策略库的 recommendation_snapshot.evidence_json 取真实因子证据，逐股产出
结构化中文分析，写 ai_analysis/{date}.json（strategy_source=traditional），供发布层 merge_strategy_ai 接入。

诚实化：只依据给出的真实因子分/价格/筹码分析，不编造消息面/目标价；ai_advice 受 final_action 约束
（市场降级为 watch 时不得「买入」）。ai_score 为模型对该股的 0-100 信心分（启动前夕本就用 AI 信心分，
非伪造；与历史口径一致）。断点续跑 + 并发 + 单只重试 + 失败隔离。

用法：
  daily_stock_analysis/venv/bin/python3 generate_prebreakout_ai.py [--trade-date YYYYMMDD] [--force] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
PRE_DB = WORKSPACE / "stock_data" / "03-working" / "strategy_research" / "prebreakout_v41" / "prebreakout_v41.db"
AI_ANALYSIS_DIR = WORKSPACE / "stock_data" / "03-working" / "ai_analysis"

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 复用 O2C 生成器已验证稳定的 LLM 直连工具（.env 解析 / 模型链 / JSON 抽取 / 调用）。
from generate_o2c_ai import (  # noqa: E402
    ENV_PATH,
    effective_model_chain,
    extract_json,
    has_model_credentials,
    llm_complete,
    load_env,
    model_chain,
    norm_code,
    prepare_runtime_env,
)

STRATEGY_ID = "prebreakout_v41"
PROMPT_VERSION = "prebreakout_ai_v1"
SOURCE_KIND = "traditional_deep_analysis"
ADVICE_ALLOWED = {"买入", "观察", "回避"}


def load_picks(trade_date: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{PRE_DB}?mode=ro&immutable=1", uri=True)
    rows = conn.execute(
        "SELECT rank_no, stock_code, ts_code, stock_name, sector_name, adjusted_action, evidence_json "
        "FROM recommendation_snapshot WHERE strategy_id=? AND recommend_date=? "
        "AND created_at=(SELECT MAX(created_at) FROM recommendation_snapshot "
        "WHERE strategy_id=? AND recommend_date=?) ORDER BY rank_no LIMIT ?",
        (STRATEGY_ID, trade_date, STRATEGY_ID, trade_date, limit),
    ).fetchall()
    conn.close()
    out = []
    for rank_no, code, ts_code, name, sector, action, ev in rows:
        try:
            e = json.loads(ev) if ev else {}
        except (json.JSONDecodeError, TypeError):
            e = {}
        out.append({
            "rank": rank_no, "code": norm_code(code or ts_code), "ts_code": ts_code or code,
            "name": name or code, "sector": sector or e.get("sector_name") or "未标注",
            "final_action": e.get("final_action") or action or "watch",
            "close": e.get("close"), "change": e.get("change"),
            "chip_conc": e.get("chip_conc"), "winner_rate": e.get("winner_rate"),
            "score": e.get("score"), "buy_zone": e.get("buy_zone"),
            "factor_scores": e.get("factor_scores") or {},
        })
    return out


def build_prompt(s: dict[str, Any]) -> str:
    fs = s.get("factor_scores") or {}
    flines = "\n".join(f"- {k}：{v}" for k, v in list(fs.items())[:12]) or "（无因子明细）"
    research = s.get("final_action") != "main"
    if research:
        advice_rule = "该股当前最终动作为「观察」（受市场环境/门槛约束），你的 ai_advice 只能是「观察」或「回避」，不得「买入」。"
        options = "观察 | 回避"
    else:
        advice_rule = "ai_advice 取值：买入 | 观察 | 回避。"
        options = "买入 | 观察 | 回避"
    price = s.get("close")
    chg = s.get("change")
    head = (
        f"股票：{s['name']} {s['ts_code']} | 行业：{s['sector']} | 策略排名：第{s['rank']}/20\n"
        f"现价：{price if price is not None else '—'} | 当日涨跌：{chg if chg is not None else '—'}% | "
        f"量化综合分：{s.get('score') if s.get('score') is not None else '—'} | "
        f"筹码集中度：{s.get('chip_conc')} | 获利盘：{s.get('winner_rate')}%"
    )
    return f"""你是A股「启动前夕」策略量化分析师。该股来自基于均线/动量/量价/筹码的多因子启动前夕模型，目标是判断其短线启动概率、关键位与风险。

{head}
策略状态：{advice_rule}
触发参考：{s.get('buy_zone') or '—'}

因子打分（0-100，越高越强）：
{flines}

要求：
1) 只依据上面给出的真实因子分、价格与筹码数据分析，不要编造消息面、财报或目标价。
2) ai_points 依次覆盖：技术形态/启动条件 / 核心因子贡献 / 量价与筹码 / 风险点 / 执行参考。
3) ai_risks 给 2 条最关键风险。
4) ai_score 给 0-100 的整数信心分（越高越看好短线启动），与上面动作/风险自洽。

严格只输出如下 JSON（无多余文字、无 markdown）：
{{"ai_summary":"一句话总体判断（<=60字）","ai_advice":"{options}","ai_score":<0-100整数>,"ai_points":["技术形态/启动条件：...","核心因子贡献：...","量价与筹码：...","风险点：...","执行参考：..."],"ai_risks":["...","..."]}}"""


def clamp(obj: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
    advice = str(obj.get("ai_advice") or "").strip()
    if advice not in ADVICE_ALLOWED:
        advice = next((a for a in ("买入", "观察", "回避") if a in advice), "观察")
    if s.get("final_action") != "main" and advice == "买入":
        advice = "观察"
    try:
        score = int(round(float(obj.get("ai_score"))))
        score = max(0, min(100, score))
    except (TypeError, ValueError):
        score = None
    points = [str(p).strip() for p in (obj.get("ai_points") or []) if str(p).strip()]
    risks = [str(r).strip() for r in (obj.get("ai_risks") or []) if str(r).strip()]
    return {"ai_advice": advice, "ai_score": score, "ai_points": points, "ai_risks": risks,
            "ai_summary": str(obj.get("ai_summary") or "").strip()}


def gen_one(s: dict[str, Any], env, models, *, attempts: int, max_tokens: int, timeout: int) -> dict[str, Any]:
    prompt = build_prompt(s)
    last = ""
    used = ""
    for i in range(1, attempts + 1):
        res = llm_complete(prompt, env, models, max_tokens=max_tokens, timeout=timeout)
        used = res.get("model") or used
        if res.get("ok"):
            obj = extract_json(res.get("content") or "")
            if obj and str(obj.get("ai_summary") or "").strip() and len([p for p in (obj.get("ai_points") or []) if str(p).strip()]) >= 3:
                c = clamp(obj, s)
                if c["ai_score"] is not None:
                    return {"status": "success", "model": used, **c, **s}
                last = "no_score"
            else:
                last = "parse_or_validate"
        else:
            last = res.get("error") or "llm_failed"
        if i < attempts:
            time.sleep(min(6.0, 1.5 * i))
    return {"status": "failed", "model": used, "error": last, **s}


def to_record(r: dict[str, Any], trade_date: str, now: str) -> dict[str, Any]:
    return {
        "code": r["code"], "ts_code": r["ts_code"], "name": r["name"], "sector_name": r["sector"],
        "strategy_source": "traditional", "strategy_name": "启动前夕",
        "ai_source_kind": SOURCE_KIND, "ai_status": "ready", "ai_provenance": "fresh_same_day",
        "ai_summary": r["ai_summary"], "analysis_summary": r["ai_summary"],
        "ai_advice": r["ai_advice"], "operation_advice": r["ai_advice"],
        "ai_points": r["ai_points"], "ai_risks": r["ai_risks"],
        "ai_score": r["ai_score"], "sentiment_score": r["ai_score"], "success": True,
        "ai_source_date": trade_date, "ai_analysis_date": trade_date, "trade_date": trade_date,
        "model_name": r.get("model"), "prompt_version": PROMPT_VERSION,
        "generated_at": now, "ai_evidence_time": now, "rank_change": 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade-date", default=os.environ.get("OPENCLAW_TARGET_TRADE_DATE"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-workers", type=int, default=4)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=4500)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()
    trade_date = str(args.trade_date or "")
    if not trade_date:
        print(json.dumps({"ok": False, "reason": "no_trade_date"})); return 1

    picks = load_picks(trade_date, args.limit)
    if not picks:
        print(json.dumps({"ok": False, "reason": "no_picks", "trade_date": trade_date})); return 1

    out_path = AI_ANALYSIS_DIR / f"{trade_date}.json"
    existing: dict[str, dict[str, Any]] = {}
    if out_path.exists() and not args.force:
        try:
            for it in json.loads(out_path.read_text(encoding="utf-8")) or []:
                c = norm_code((it or {}).get("code"))
                if c:
                    existing[c] = it
        except Exception:
            existing = {}
    # 20260710修: 老桥接失败壳的 analysis_summary 是错误文案("分析过程出错: All LLM models
    # failed...")非空——按"非空即完成"会把失败股跳过, 缺口永远补不上。带失败标记的记录视为未完成。
    fail_markers = ("分析过程出错", "Analysis failed", "All LLM models failed", "AI分析暂缺")

    def _real_summary(it: dict[str, Any]) -> bool:
        text = str(it.get("analysis_summary") or it.get("ai_summary") or "").strip()
        return bool(text) and not any(m in text for m in fail_markers)

    todo = [p for p in picks if args.force or not (
        existing.get(p["code"]) and _real_summary(existing[p["code"]])
        and (existing[p["code"]].get("strategy_source") in (None, "traditional"))
    )]

    try:
        env = prepare_runtime_env(load_env(ENV_PATH))
        # 2026-11-10 免费模型切换：把 .env 里的 key（如 OPENROUTER_API_KEY）注入进程环境，
        # 供 LiteLLM 按 provider 自行取用；已有环境变量优先，不覆盖。
        for _k, _v in env.items():
            if _v and _k not in os.environ:
                os.environ[_k] = _v
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "reason": "xai_auth_unavailable", "error": str(exc)[:300]}, ensure_ascii=False))
        return 1
    models = effective_model_chain(env)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if not has_model_credentials(env, models):
        print(json.dumps({"ok": False, "reason": "missing_key"})); return 1
    print(f"[pre-ai] trade_date={trade_date} picks={len(picks)} todo={len(todo)} models={models}")

    results: list[dict[str, Any]] = []
    if todo:
        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futs = {ex.submit(gen_one, s, env, models, attempts=args.attempts, max_tokens=args.max_tokens, timeout=args.timeout): s for s in todo}
            for fut in as_completed(futs):
                r = fut.result(); results.append(r)
                tag = "OK " if r["status"] == "success" else "FAIL"
                print(f"  [{tag}] {r['code']} {r['name']} {'score='+str(r.get('ai_score'))+' '+str(r.get('ai_advice')) if r['status']=='success' else 'err='+str(r.get('error'))}")

    merged = dict(existing)
    for r in results:
        if r["status"] == "success":
            merged[r["code"]] = to_record(r, trade_date, now)
    AI_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2), encoding="utf-8")

    succ = sum(1 for r in results if r["status"] == "success")
    have = sum(1 for p in picks if str((merged.get(p["code"]) or {}).get("analysis_summary") or "").strip())
    summary = {"ok": True, "trade_date": trade_date, "generated": succ, "coverage": f"{have}/{len(picks)}",
               "failed": [r["code"] for r in results if r["status"] == "failed"], "path": str(out_path)}
    print("[pre-ai] " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
