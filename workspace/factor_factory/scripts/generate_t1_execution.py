#!/usr/bin/env python3
"""Generate T1 execution data for publication layer."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", "/Users/fisher/.openclaw/workspace"))
OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "t1_alpha191"
PUBLISHED_REPO = Path(os.environ.get("OPENCLAW_PUBLISHED_REPO", str(WORKSPACE / "stock-report")))
CACHE_DIR = WORKSPACE / "stock_data" / "03-working" / "backtest_cache"
STOCK_LIST = WORKSPACE / "stock_data" / "01-root" / "stock_list.csv"


def load_t1_data() -> dict:
    """Load T1 research state."""
    with open(OUTPUT_DIR / "research_state_t1.json") as f:
        return json.load(f)


def load_portfolio_spec() -> dict:
    """Load portfolio spec."""
    with open(OUTPUT_DIR / "portfolio_spec.json") as f:
        return json.load(f)


def load_stock_name_map(trade_date: str) -> dict[str, str]:
    names: dict[str, str] = {}
    daily_path = CACHE_DIR / f"daily_{trade_date}.parquet"
    if daily_path.exists():
        daily = pd.read_parquet(daily_path)
        if "ts_code" in daily.columns and "name" in daily.columns:
            for _, row in daily[["ts_code", "name"]].iterrows():
                code = str(row.get("ts_code") or "")
                name = str(row.get("name") or "")
                if code and name and name != "nan":
                    names[code] = name
                    names[code.split(".")[0]] = name
    if STOCK_LIST.exists():
        stock_list = pd.read_csv(STOCK_LIST)
        for _, row in stock_list.iterrows():
            code = str(row.get("ts_code") or "")
            name = str(row.get("name") or "")
            if code and name and name != "nan":
                names.setdefault(code, name)
                names.setdefault(code.split(".")[0], name)
    return names


def generate_execution_data(t1_data: dict, portfolio_spec: dict) -> list[dict]:
    """Generate execution data for T1 strategy."""
    top20 = t1_data.get("top20", [])
    trade_date = str(t1_data.get("latest_trade_date") or t1_data.get("trade_date") or "")
    names = load_stock_name_map(trade_date)
    
    execution_rows = []
    for item in top20:
        ts_code = item.get("ts_code", "")
        score = float(item.get("score") or 0)
        
        # Normalize code (remove .SZ/.SH suffix)
        stock_code = ts_code.split(".")[0] if "." in ts_code else ts_code
        stock_name = item.get("name") or item.get("stock_name") or names.get(ts_code) or names.get(stock_code) or ""
        
        # Determine action based on score
        if score > 0.15:
            raw_action = "main"
        elif score > 0.10:
            raw_action = "watch"
        else:
            raw_action = "watch"
        
        # 诚实化（任务B真退役，2026-07）：T1 无真实 AI 评分，ai_score 一律 null，
        # 不再用 t1_ai_score() 的 45-88 随机模板分伪造评分。
        ai_summary = (
            f"{stock_name or stock_code} 进入 T1 Alpha191 短线胜率模型 top20，"
            f"综合因子分数 {score:.4f}。T1 已退役（无可交易边际），本记录仅为冻结静态快照的因子研究参考，"
            "不构成任何买卖建议。"
        )
        execution_rows.append({
            "stock_code": stock_code,
            "ts_code": ts_code,
            "code": stock_code,
            "stock_name": stock_name,
            "name": stock_name,
            "strategy_source": "t1_factor",
            "raw_action": raw_action,
            "adjusted_action": raw_action,  # Will be adjusted by publication layer
            "adjustment_reason": "",
            "position_tier": 2,
            "buy_zone": f"T1 已退役：不提供买入区间（研究观察档案，历史参考分数{score:.4f}）",
            "invalidation": "T1 已退役：不提供失效条件（研究观察档案）",
            "next_day_handling": "T1 已退役：不提供操作指引（研究观察档案）",
            "ai_view": ai_summary,
            "ai_advice": "观察",
            "ai_score": None,
            "ai_summary": ai_summary,
            "ai_source_kind": "retired_template_note",
            "ai_source_name": "T1 已退役·冻结静态快照（非深度AI）",
            "ai_source_date": trade_date,
            "recommend_date": trade_date,
        })
    
    return execution_rows


def save_execution_data(execution_rows: list[dict], latest_date: str | None = None) -> None:
    """Save execution data to publication layer."""
    # Save to T1 analytics directory
    analytics_dir = PUBLISHED_REPO / "data" / "recommendation_analytics"
    analytics_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = analytics_dir / "t1_factor_recommendations.json"
    with open(output_file, "w") as f:
        latest_date = latest_date or (execution_rows[0].get("recommend_date") if execution_rows else None)
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "trade_date": latest_date,
            "latest_recommend_date": latest_date,
            "strategy_id": "t1_factor_v1",
            "strategy_name": "T+1胜率因子",
            "rows": execution_rows
        }, f, ensure_ascii=False, indent=2)
    
    print(f"Saved {len(execution_rows)} T1 execution rows to {output_file}")


def main() -> int:
    # T1已退役（任务B真退役，2026-07）：本脚本此前是每日 cron（daily_t1_alpha191.sh，已摘除）
    # 生成 t1_factor_recommendations.json 的唯一途径，且用 t1_ai_score() 伪造 45-88 分。
    # 现默认直接退出、不再每日重算；仅当显式传 --force-archive 才重建静态档案（用于修复/迁移场景）。
    if "--force-archive" not in sys.argv:
        print("T1 retired, frozen archive only")
        return 0

    t1_data = load_t1_data()
    portfolio_spec = load_portfolio_spec()

    execution_rows = generate_execution_data(t1_data, portfolio_spec)
    save_execution_data(execution_rows, t1_data.get("latest_trade_date") or t1_data.get("trade_date"))

    print(f"Generated T1 execution data for {len(execution_rows)} stocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
