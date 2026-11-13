#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Factor Factory - Phase 1 bootstrapper

作用：
1. 初始化 factor_factory/ 目录结构
2. 写入 Phase 1 所需 YAML 配置文件
3. 保证可重复执行（幂等）

默认约束：
- Universe: 剔除 ST / 停牌 / 上市未满一年 / 日均成交额不在前 50%
- Backtest Window: 2021-01-01 ~ 2026-04-01
- Base Factor: prebreakout_v41 固定 40%
- Friction: 双边 0.2%（默认单边 0.1%）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from textwrap import dedent


DEFAULT_ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")


DIRS = [
    "config",
    "data/raw",
    "data/prepared",
    "data/universe",
    "data/factors",
    "data/regimes",
    "data/audit_cache",
    "scripts",
    "modules/universe",
    "modules/features",
    "modules/audit",
    "modules/regime",
    "modules/composer",
    "modules/report",
    "outputs/factor_registry",
    "outputs/candidate_lists",
    "outputs/audit_reports",
    "outputs/regime_maps",
    "outputs/weight_maps",
    "outputs/attribution_reports",
    "notebooks/exploratory",
    "logs",
]


README_MD = dedent(
    """
    # Factor Factory

    面向【启动前夕】固定基准因子的辅助因子发现、审计、配权与自愈系统。

    ## Phase 1 目标
    - 构建活跃股票池
    - 生成首批候选因子面板
    - 完成相关性、走步回测、极端样本剔除、摩擦成本后的审计闭环

    ## 关键约束
    - `prebreakout_v41` 固定 40%
    - 辅助因子与 base 因子低相关（|rho| < 0.3）
    - 回测窗口：2021-01-01 ~ 2026-04-01
    - 双边摩擦成本：0.2%
    """
).strip() + "\n"


PHASE1_YAML = dedent(
    """
    system:
      name: factor-factory
      version: phase1
      timezone: Asia/Shanghai

    paths:
      workspace: /Users/fisher/.openclaw/workspace
      stock_data_root: /Users/fisher/.openclaw/workspace/stock_data
      base_strategy_repo: /Users/fisher/.openclaw/workspace/stock_data/03-working/stock-report-repo
      output_root: /Users/fisher/.openclaw/workspace/factor_factory

    execution:
      silent: true
      random_seed: 42
      max_workers: 8
      use_cache: true
      overwrite_outputs: true

    phase1:
      start_date: 2021-01-01
      end_date: 2026-04-01
      rebalance_freq: D
      friction_model: cost_v1
      factor_panel_format: parquet
      universe_snapshot_format: parquet
      audit_report_format: json

    base_factor:
      id: prebreakout_v41
      locked: true
      black_box: true
      fixed_weight: 0.40
      description: 启动前夕固定基准因子，不允许修改内部逻辑

    auxiliary_factor_pool:
      min_factors: 30
      max_factors: 80
      orthogonal_threshold: 0.30
      candidate_groups:
        - price_volume_momentum
        - volatility_compression
        - liquidity_turnover_distribution
        - chip_structure_delta
        - fundamentals_event_filters

    walk_forward:
      train_months: 24
      validation_months: 6
      test_months: 6
      step_months: 3
      min_train_samples: 252

    monte_carlo:
      enabled: true
      n_sims: 500
      block_size: 5
      bootstrap_method: stationary

    outlier_filter:
      enabled: true
      trim_top_pct: 5.0
      trim_by: next_return

    quality_gate:
      min_rank_ic_abs: 0.01
      min_win_rate_lift: 0.01
      min_sharpe_lift: 0.05
      max_turnover: 1.0
      max_drawdown_deterioration: 0.05

    reporting:
      write_registry: true
      write_whitelist: true
      write_coldlist: true
      write_attribution: true
    """
).strip() + "\n"


UNIVERSE_RULES_YAML = dedent(
    """
    universe:
      market: A-share
      include_boards:
        - SH_main
        - SZ_main
        - ChiNext
      exclude_boards:
        - STAR
        - BSE

      filters:
        exclude_st: true
        exclude_suspended: true
        minimum_listing_days: 252
        avg_amount_percentile_threshold: 0.50
        avg_amount_lookback_days: 60
        min_price: 2.0
        min_daily_trading_days_ratio: 0.90

      st_name_patterns:
        - ST
        - *ST
        - 退

      suspended_fields_priority:
        - is_suspended_effective
        - is_suspended_proxy
        - susp_is_suspended_tushare
        - is_suspended

      liquidity_metric:
        field: amount
        unit: yuan
        transform: none

      outputs:
        snapshot_prefix: universe_
        summary_file: universe_summary.json
    """
).strip() + "\n"


FACTOR_REGISTRY_SEED_YAML = dedent(
    """
    base_factor:
      id: prebreakout_v41
      family: fixed_base
      locked: true
      black_box: true
      weight: 0.40
      inputs:
        - price
        - volume
        - chip_structure
      outputs:
        - score
        - rank
        - top20

    candidate_groups:
      price_volume_momentum:
        target_count: 18
        enabled: true
        examples:
          - ret_5_slope
          - ret_10_slope
          - momentum_second_derivative_20
          - breakout_volume_coupling
      volatility_compression:
        target_count: 10
        enabled: true
        examples:
          - atr_compression_10
          - boll_width_delta_20
          - realized_vol_quantile_20
          - low_vol_breakout_ratio
      liquidity_turnover_distribution:
        target_count: 10
        enabled: true
        examples:
          - turnover_skew_20
          - turnover_kurtosis_20
          - amount_stability_20
          - liquidity_regime_switch
      chip_structure_delta:
        target_count: 10
        enabled: true
        examples:
          - winner_rate_delta_5
          - weight_avg_distance_speed
          - cost_band_spread_delta
          - chip_concentration_second_diff
      fundamentals_event_filters:
        target_count: 8
        enabled: true
        examples:
          - earnings_surprise_dummy
          - cashflow_profit_divergence
          - valuation_compression_state
          - event_density_proxy

    common_constraints:
      orthogonal_to_base_abs_rho_lt: 0.30
      max_family_weight: 0.30
      max_single_factor_weight: 0.20
      audit_required: true
    """
).strip() + "\n"


REGIME_SCHEMA_YAML = dedent(
    """
    regimes:
      - id: volume_uptrend
        label: 放量上涨
        description: 指数/行业广度走强且成交额同步放大
      - id: volume_downtrend
        label: 放量下跌
        description: 跌势放量扩散，风险偏好明显收缩
      - id: shrink_uptrend
        label: 缩量上涨
        description: 上涨但量能不足，偏存量博弈
      - id: shrink_downtrend
        label: 缩量阴跌
        description: 低成交弱势回落，确认度不足
      - id: high_vol_chop
        label: 高波动震荡
        description: 振幅大、方向弱，适合控制仓位
      - id: small_cap_rotation
        label: 风格切换至小盘
        description: 小盘/高弹性方向占优
      - id: large_cap_rotation
        label: 风格切换至权重
        description: 权重/防御方向占优
      - id: sector_concentration
        label: 主线集中
        description: 热点行业集中，主线清晰
      - id: sector_dispersion
        label: 主线发散
        description: 题材轮动杂乱，持续性偏弱

    phase1_engine:
      mode: rule_based
      outputs:
        - regime_id
        - regime_confidence
        - regime_components
      confidence_range:
        min: 0.0
        max: 1.0
    """
).strip() + "\n"


BACKTEST_COSTS_YAML = dedent(
    """
    friction:
      model_id: cost_v1
      enabled: true
      round_trip_cost: 0.0020
      buy_side_cost: 0.0010
      sell_side_cost: 0.0010
      components:
        commission: 0.0003
        slippage: 0.0012
        tax_and_fees: 0.0005

    portfolio_rules:
      equal_weight: true
      max_position_count: 20
      min_position_count: 10
      cash_buffer: 0.00
      rebalance_at_close: true
      execution_price: next_open_proxy

    performance_metrics:
      - total_return
      - annualized_return
      - sharpe
      - max_drawdown
      - calmar
      - win_rate
      - turnover
      - avg_holding_days
    """
).strip() + "\n"


CONFIG_FILES = {
    "README.md": README_MD,
    "config/phase1.yaml": PHASE1_YAML,
    "config/universe_rules.yaml": UNIVERSE_RULES_YAML,
    "config/factor_registry_seed.yaml": FACTOR_REGISTRY_SEED_YAML,
    "config/regime_schema.yaml": REGIME_SCHEMA_YAML,
    "config/backtest_costs.yaml": BACKTEST_COSTS_YAML,
}


def ensure_dirs(root: Path) -> list[str]:
    created = []
    for rel in DIRS:
        path = root / rel
        path.mkdir(parents=True, exist_ok=True)
        created.append(str(path))
    return created


def write_files(root: Path, force: bool = False) -> list[str]:
    written = []
    for rel, content in CONFIG_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not force:
            continue
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap factor_factory Phase 1 project structure")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Target factor_factory root directory")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config files")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    created_dirs = ensure_dirs(root)
    written_files = write_files(root, force=args.force)

    summary = {
        "root": str(root),
        "created_dirs": created_dirs,
        "written_files": written_files,
        "force": bool(args.force),
        "status": "ok",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
