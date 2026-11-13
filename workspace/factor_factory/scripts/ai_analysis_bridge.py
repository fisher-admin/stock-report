#!/usr/bin/env python3
"""
AI深度分析桥接：把 O2C / T1 Alpha191 选股结果接入 daily_stock_analysis AI流水线
==========================================================================

用法：
    python ai_analysis_bridge.py                    # 分析 O2C + T1 全部
    python ai_analysis_bridge.py --source o2c       # 只分析 O2C
    python ai_analysis_bridge.py --source t1        # 只分析 T1
    python ai_analysis_bridge.py --dry-run          # 只打印股票列表
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 路径配置
WORKSPACE = Path.home() / ".openclaw" / "workspace"
FACTOR_OUTPUTS = WORKSPACE / "factor_factory" / "outputs"
DAILY_ANALYSIS_ROOT = WORKSPACE / "daily_stock_analysis"
AI_ANALYSIS_DIR = WORKSPACE / "stock_data" / "03-working" / "ai_analysis"
RECOMMENDATION_DIR = WORKSPACE / "stock-report" / "data" / "recommendation_analytics"

# O2C 和 T1 的选股结果路径
O2C_SELECTION_PATH = FACTOR_OUTPUTS / "greenfield_multifactor" / "current_selection_snapshot.json"
T1_SELECTION_PATH = FACTOR_OUTPUTS / "t1_alpha191" / "research_state_t1.json"
T1_LEGACY_SELECTION_PATH = FACTOR_OUTPUTS / "t1_alpha191" / "latest_recommendation.json"


def load_o2c_stocks(top_n: int = 20) -> list[dict]:
    """加载 O2C 因子选股结果"""
    if not O2C_SELECTION_PATH.exists():
        print(f"❌ O2C 选股文件不存在: {O2C_SELECTION_PATH}")
        return []
    
    with open(O2C_SELECTION_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    stocks = []
    for item in data.get("top20", [])[:top_n]:
        ts_code = item.get("ts_code", "")
        code = ts_code.split(".")[0] if "." in ts_code else ts_code
        stocks.append({
            "code": code,
            "ts_code": ts_code,
            "name": item.get("name", ""),
            "industry": item.get("industry", ""),
            "score": item.get("composite_score", 0),
            "source": "o2c_greenfield",
            "trade_date": data.get("trade_date", ""),
            "factors": {
                "factor_values": item.get("factor_values", {}),
                "factor_zscores": item.get("factor_zscores", {}),
            },
        })
    
    print(f"📊 O2C 加载 {len(stocks)} 只 (日期: {data.get('trade_date', 'N/A')})")
    return stocks


def load_t1_stocks(top_n: int = 20) -> list[dict]:
    """加载 T1 Alpha191 选股结果。优先使用当日研究状态，避免读取滞后的 legacy latest_recommendation。"""
    selection_path = T1_SELECTION_PATH if T1_SELECTION_PATH.exists() else T1_LEGACY_SELECTION_PATH
    if not selection_path.exists():
        print(f"❌ T1 选股文件不存在: {T1_SELECTION_PATH} 或 {T1_LEGACY_SELECTION_PATH}")
        return []
    
    with open(selection_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    trade_date = data.get("latest_trade_date") or data.get("trade_date", "")
    
    stocks = []
    for item in data.get("top20", [])[:top_n]:
        ts_code = item.get("ts_code", "")
        code = ts_code.split(".")[0] if "." in ts_code else ts_code
        stocks.append({
            "code": code,
            "ts_code": ts_code,
            "name": item.get("name", ""),
            "industry": "",
            "score": item.get("score", 0),
            "source": "t1_alpha191",
            "trade_date": trade_date,
            "factors": item.get("factors", {}),
        })
    
    print(f"📊 T1 Alpha191 加载 {len(stocks)} 只 (日期: {trade_date})")
    return stocks


def run_ai_analysis(codes: list[str]) -> list[dict]:
    """调用 daily_stock_analysis AI 分析流水线"""
    os.chdir(DAILY_ANALYSIS_ROOT)
    
    # 确保 daily_stock_analysis 在 Python 路径中
    dsa_str = str(DAILY_ANALYSIS_ROOT)
    if dsa_str not in sys.path:
        sys.path.insert(0, dsa_str)
    
    os.environ["NO_PROXY"] = "token-plan-cn.xiaomimimo.com,localhost,127.0.0.1"
    os.environ["no_proxy"] = "token-plan-cn.xiaomimimo.com,localhost,127.0.0.1"
    os.environ.setdefault("DOTENV_PATH", str(DAILY_ANALYSIS_ROOT / ".env"))
    
    from src.config import get_config, setup_env
    setup_env()
    config = get_config()
    
    print(f"\n🔍 模型: {config.openai_model}")
    print(f"📡 API: {config.openai_base_url}")
    print(f"🎯 分析 {len(codes)} 只股票\n")
    
    # 优先用 Agent 模式（决策仪表盘）
    try:
        from src.agent.executor import build_agent_executor
        from src.config import get_config as _gc
        
        results = []
        for i, code in enumerate(codes, 1):
            print(f"  [{i}/{len(codes)}] 分析 {code}...", end=" ", flush=True)
            try:
                executor = build_agent_executor(config)
                result = executor.run(f"请分析股票 {code}，给出决策仪表盘")
                dashboard = {}
                if hasattr(result, "dashboard"):
                    dashboard = result.dashboard or {}
                elif isinstance(result, dict):
                    dashboard = result.get("dashboard", {})
                
                content = ""
                if hasattr(result, "content"):
                    content = result.content or ""
                elif isinstance(result, dict):
                    content = result.get("content", "")
                
                success = True
                if hasattr(result, "success"):
                    success = result.success
                elif isinstance(result, dict):
                    success = result.get("success", True)
                
                results.append({
                    "code": code,
                    "success": success,
                    "dashboard": dashboard,
                    "content": content,
                })
                print("✅" if success else "⚠️")
            except Exception as e:
                print(f"❌ {e}")
                results.append({"code": code, "success": False, "error": str(e)})
        
        return results
    except ImportError:
        print("⚠️ Agent 模式不可用，回退到 Legacy 模式")
    
    # Legacy 模式回退
    try:
        from src.core.pipeline import StockAnalysisPipeline
        pipeline = StockAnalysisPipeline(config)
        raw_results = pipeline.run(stock_codes=codes)
        results = []
        for r in raw_results:
            if isinstance(r, dict):
                results.append(r)
            elif hasattr(r, "__dict__"):
                results.append(r.__dict__)
            else:
                results.append({"success": False, "code": "unknown"})
        return results
    except Exception as e:
        print(f"❌ Legacy 模式也失败: {e}")
        return []


def save_results(all_stocks: list[dict], ai_results: list[dict], sources: list[str]):
    """保存 AI 分析结果到标准位置"""
    # 建立 AI 结果索引
    ai_map = {}
    for r in ai_results:
        code = r.get("code", "")
        if "." in code:
            code = code.split(".")[0]
        ai_map[code] = r
    
    trade_date = datetime.now().strftime("%Y%m%d")
    
    # 1. 保存到 ai_analysis 目录
    AI_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    
    # 按 source 分别保存
    for source in sources:
        source_stocks = [s for s in all_stocks if s["source"] == source]
        source_results = []
        for stock in source_stocks:
            ai = ai_map.get(stock["code"], {})
            merged = {**stock, **ai}
            source_results.append(merged)
        
        out_path = AI_ANALYSIS_DIR / f"{trade_date}_{source}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(source_results, f, ensure_ascii=False, indent=2)
        print(f"💾 {source} 结果已保存: {out_path}")
    
    # 2. 保存合并结果
    merged_all = []
    for stock in all_stocks:
        ai = ai_map.get(stock["code"], {})
        merged = {**stock, **ai}
        merged_all.append(merged)
    
    merged_path = AI_ANALYSIS_DIR / f"{trade_date}_factor_combined.json"
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged_all, f, ensure_ascii=False, indent=2)
    print(f"💾 合并结果已保存: {merged_path}")
    
    # 3. 更新 recommendation_analytics 中的文件
    RECOMMENDATION_DIR.mkdir(parents=True, exist_ok=True)
    
    for source in sources:
        source_stocks = [s for s in all_stocks if s["source"] == source]
        ai_enriched = []
        for stock in source_stocks:
            ai = ai_map.get(stock["code"], {})
            entry = {
                "ts_code": stock["ts_code"],
                "code": stock["code"],
                "name": stock.get("name", ""),
                "score": stock.get("score", 0),
                "ai_score": ai.get("dashboard", {}).get("core_conclusion", {}).get("sentiment_score", 
                           ai.get("sentiment_score", 0)),
                "ai_summary": ai.get("dashboard", {}).get("core_conclusion", {}).get("one_sentence", 
                             ai.get("analysis_summary", "")),
                "ai_decision": ai.get("dashboard", {}).get("core_conclusion", {}).get("signal_type",
                              ai.get("decision_type", "")),
                "ai_advice": ai.get("operation_advice", ""),
                "ai_analysis_date": trade_date,
            }
            ai_enriched.append(entry)
        
        rec_path = RECOMMENDATION_DIR / f"{source}_ai_analysis.json"
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump({
                "source": source,
                "trade_date": trade_date,
                "analyzed_at": datetime.now().isoformat(),
                "count": len(ai_enriched),
                "stocks": ai_enriched,
            }, f, ensure_ascii=False, indent=2)
        print(f"💾 {source} 推荐数据已更新: {rec_path}")


def main():
    parser = argparse.ArgumentParser(description="O2C/T1 Alpha191 AI深度分析桥接")
    parser.add_argument("--source", choices=["o2c", "t1", "all"], default="all",
                        help="选择分析来源 (默认: all)")
    parser.add_argument("--top", type=int, default=20, help="每个来源取前N只 (默认20)")
    parser.add_argument("--dry-run", action="store_true", help="只打印股票列表")
    args = parser.parse_args()
    
    # 加载选股结果
    all_stocks = []
    sources = []
    
    if args.source in ("o2c", "all"):
        o2c = load_o2c_stocks(args.top)
        all_stocks.extend(o2c)
        sources.append("o2c_greenfield")
    
    if args.source in ("t1", "all"):
        t1 = load_t1_stocks(args.top)
        all_stocks.extend(t1)
        sources.append("t1_alpha191")
    
    if not all_stocks:
        print("❌ 没有加载到任何选股结果")
        return 1
    
    # 去重
    seen = set()
    unique_stocks = []
    for s in all_stocks:
        if s["code"] not in seen:
            seen.add(s["code"])
            unique_stocks.append(s)
    
    codes = [s["code"] for s in unique_stocks]
    
    print(f"\n{'='*50}")
    print(f"📋 共 {len(all_stocks)} 只 (去重后 {len(codes)} 只)")
    print(f"📋 来源: {', '.join(sources)}")
    print(f"{'='*50}")
    
    for i, s in enumerate(unique_stocks, 1):
        label = f"{s['name']}({s['code']})" if s['name'] else s['code']
        print(f"  {i:2d}. {label} [{s['source']}] 得分:{s['score']:.3f}")
    
    if args.dry_run:
        print("\n🔍 dry-run 模式，跳过分析")
        return 0
    
    # 运行 AI 分析
    print(f"\n🚀 开始 AI 深度分析...")
    ai_results = run_ai_analysis(codes)
    
    success_count = sum(1 for r in ai_results if r.get("success", False))
    fail_count = len(ai_results) - success_count
    
    print(f"\n{'='*50}")
    print(f"✅ AI 分析完成: 成功 {success_count}, 失败 {fail_count}")
    print(f"{'='*50}")
    
    # 保存结果
    save_results(unique_stocks, ai_results, sources)
    
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
