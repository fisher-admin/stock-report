# spike-1 · PhaseContext 四接入点生产落点（提案，spike 期间不改生产）

模块目标家: `factor_factory/data/phase_context.py`（从本分支 `src/core/phase_context.py` 原样拷贝）。
以下四处各加一行判断（Wave1 实施时应用；行号为 2026-07-11 现状）:

## ① 19:30 管线 stage0 → run_manifest
文件: `skills/stock-system-orchestrator/scripts/run_pipeline.py` main() ~L443（`run_env` 构建后）
```python
ctx = build_phase_context(cache_path=CAL_CACHE)          # stage0 一次构建（fail-closed，PhaseContextError 直接非零退出）
payload["phase_context"] = ctx.to_dict()                  # ← 一行：写入 run_manifest
```

## ② 盘中裸跑护栏（替换散装 weekday+"15:05" 字符串判断）
文件: `skills/stock-system-orchestrator/scripts/run_pipeline.py` L433-440
```python
if refuse_bare_run_if_partial(ctx, bool(target_trade_date)):  # ← 一行判断（R-1 语义封装在模块内）
    return 1  # 原 FATAL 文案保留
```
**R-1 已实施（本分支 commit）**: upstream_diff.md §4.3 原文"改读 is_partial_bar"按字面执行会**弱化**现有护栏——
premarket 的 is_partial_bar=False（上游语义），07:47 型盘前裸跑将放行；现生产护栏覆盖 00:00-15:05 全窗。
处置采用 R-1 选项(a): 复合条件 `is_partial_bar or phase is PREMARKET` 已封装进
`refuse_bare_run_if_partial`（`BARE_RUN_REFUSAL_PHASES = PARTIAL_BAR_PHASES ∪ {PREMARKET}`，
单一真源），生产一行判断直接调用该 helper，任何调用点都不可能再引入弱化版。
**纪律保留（belt-and-suspenders）**: 选项(a)落地后 ②即使单独存在也不弱于现护栏；但①②④仍应绑定
同一 commit 落地（S6 已证盘前跑数据自动标为前一完成日，联动落地保证 effective 日期语义完整）。

## ③ backtest_cache 写入前断言
文件: `factor_factory/scripts/greenfield_multifactor_lib.py` 原子写函数（~L95 `_atomic_to_parquet`，
backtest_cache 的 cyq_perf_/daily_/stk_factor_ 写入均经此路径）
```python
guard_backtest_cache_write(ctx, explicit_intraday_flag=cfg.explicit_intraday)  # ← 一行：写前断言
```

## ④ pipeline_alert 落后判定
文件: `skills/stock-system-orchestrator/scripts/stock_pipeline_alert.py` `_ak_latest_trade_date`（L54-88）
现函数手工实现"15:05 收盘感知"（a6698e8）。改为:
```python
real_date = build_phase_context(cache_path=CAL_CACHE).effective_trade_date  # ← 一行：收盘感知有效交易日
```
其"19:45 后仍落后才告警"的调度语义保留在调用点不变。

## 回滚
PhaseContext 为新增独立模块，四接入点各一行——revert 单个 commit 即全退（spike 规格原文）。
