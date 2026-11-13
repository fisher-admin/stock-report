#!/usr/bin/env python3
"""计算筹码结构因子并合并到因子面板。"""

from __future__ import annotations

import pandas as pd
import numpy as np
from pathlib import Path

FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"
CYQ_DIR = Path("/Users/fisher/.hermes/stock-system/workspace/stock_data/03-working/backtest_cache")


def load_cyq_data() -> pd.DataFrame:
    """加载所有筹码数据。"""
    cyq_files = sorted(CYQ_DIR.glob("cyq_perf_*.parquet"))
    if not cyq_files:
        raise FileNotFoundError("未找到筹码数据文件")
    
    dfs = []
    for f in cyq_files:
        df = pd.read_parquet(f)
        # 确保 trade_date 是字符串类型
        df["trade_date"] = df["trade_date"].astype(str)
        dfs.append(df)
    
    return pd.concat(dfs, ignore_index=True)


def compute_chip_factors(cyq_df: pd.DataFrame) -> pd.DataFrame:
    """计算筹码结构因子。"""
    df = cyq_df.copy()
    
    # 1. 筹码集中度 (cost_85pct - cost_15pct) / cost_50pct
    # 值越小表示筹码越集中
    df["chip_concentration"] = (df["cost_85pct"] - df["cost_15pct"]) / df["cost_50pct"].replace(0, np.nan)
    
    # 2. 获利盘比例 (winner_rate)
    # 已经是 0-100 的值，直接使用
    df["winner_rate_pct"] = df["winner_rate"]
    
    # 3. 成本偏离度 (当前价格 vs 加权平均成本)
    # 需要当前价格，暂时用 cost_50pct 近似
    df["cost_deviation"] = (df["cost_50pct"] - df["weight_avg"]) / df["weight_avg"].replace(0, np.nan)
    
    # 4. 筹码分位数范围 (95% - 5%)
    df["chip_range"] = df["cost_95pct"] - df["cost_5pct"]
    
    # 5. 下部筹码支撑 (cost_15pct / cost_50pct)
    df["chip_support"] = df["cost_15pct"] / df["cost_50pct"].replace(0, np.nan)
    
    # 6. 上部筹码压力 (cost_85pct / cost_50pct)
    df["chip_resistance"] = df["cost_85pct"] / df["cost_50pct"].replace(0, np.nan)
    
    return df


def build_chip_panel(cyq_df: pd.DataFrame) -> pd.DataFrame:
    """构建筹码因子面板。"""
    df = compute_chip_factors(cyq_df)
    
    # 选择需要的列
    chip_factors = [
        "ts_code", "trade_date",
        "chip_concentration", "winner_rate_pct", "cost_deviation",
        "chip_range", "chip_support", "chip_resistance"
    ]
    
    return df[chip_factors].dropna(subset=["ts_code", "trade_date"])


def main() -> int:
    """主函数。"""
    print("=== 计算筹码结构因子 ===")
    
    # 加载筹码数据
    print("加载筹码数据...")
    cyq_df = load_cyq_data()
    print(f"  加载 {len(cyq_df):,} 行数据")
    print(f"  交易日范围: {cyq_df['trade_date'].min()} - {cyq_df['trade_date'].max()}")
    
    # 计算筹码因子
    print("\n计算筹码因子...")
    chip_panel = build_chip_panel(cyq_df)
    print(f"  生成 {len(chip_panel):,} 行因子数据")
    print(f"  因子数量: {len([c for c in chip_panel.columns if c not in ['ts_code', 'trade_date']])}")
    
    # 加载现有因子面板
    print("\n加载现有因子面板...")
    panel_file = FACTORS_DIR / "gtja_alpha191_panel.parquet"
    if panel_file.exists():
        existing_panel = pd.read_parquet(panel_file)
        print(f"  现有面板: {len(existing_panel):,} 行")
        
        # 合并因子
        print("\n合并因子...")
        merged = existing_panel.merge(
            chip_panel,
            on=["ts_code", "trade_date"],
            how="left"
        )
        print(f"  合并后: {len(merged):,} 行")
        
        # 检查新增因子
        new_factors = [c for c in chip_panel.columns if c not in ["ts_code", "trade_date"]]
        for factor in new_factors:
            non_null = merged[factor].notna().sum()
            print(f"  {factor}: {non_null:,} 非空值 ({non_null/len(merged)*100:.1f}%)")
        
        # 保存
        print("\n保存因子面板...")
        merged.to_parquet(panel_file, index=False)
        print(f"  保存到: {panel_file}")
        
        # 更新因子列表
        all_factors = [c for c in merged.columns if c.startswith("alpha") or c in new_factors]
        print(f"\n=== 因子列表更新 ===")
        print(f"总因子数: {len(all_factors)}")
        print("新增筹码因子:")
        for factor in new_factors:
            print(f"  - {factor}")
    else:
        print("  现有因子面板不存在，创建新面板")
        chip_panel.to_parquet(panel_file, index=False)
        print(f"  保存到: {panel_file}")
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
