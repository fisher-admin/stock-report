# 前端 v5 设计说明（2026-10-10，分支 `feat/modern-ui-redesign`）

## 0. 原则
1. **Top-20 展厅是首页的灵魂。** 每日 Top-20 候选 + 逐股 AI 深度分析是首页主体；维护模式只是覆盖层（横幅 + 展厅占位遮罩），永不替换布局。
2. **只读公开合同。** 只请求 `config/public-result-allowlist.txt` 内的文件；`prebreakout_factory_watch` / `review_state_unified` / `factor_attribution_state` 永不读取（测试强制）。
3. **诚实优先。** 执行权限 `observe_only_no_auto_order` 常驻裁决条；风险分标注口径（收盘实测 / 盘前）；每个面板带源日期，超过 7 天自动标「陈旧」；AI 明示「不改排名」。
4. **零构建、零外部依赖。** 原生 ES Module + 现代 CSS（变量、cascade layers、container queries、color-mix）；图表为手写 SVG。

## 1. 结构
```
index.html                      SPA 入口（body[data-route=today]）
<11 个旧入口>.html              location.replace('./index.html' + search + '#/<route>') + meta refresh 回退
assets/styles/v5.css            设计令牌 + 组件
assets/scripts/v5/
  app.js     唯一接触 DOM：哈希路由、取数、抽屉、排序、主题/涨跌配色、演示开关
  data.js    SOURCES（白名单路径）· ROUTE_DEPS · createLoader · 纯函数模型（top20Model / reviewSeries / maintenanceOf / ageDays）
  ui.js      esc / 数字格式 / 徽章 / 面板 / 计量条 / SVG bars·line·spark
  shell.js   顶栏 · 全局裁决条（四闸 + 市况/仓位/风险分口径 + 执行权限）· 维护横幅 · 页脚
  top20.js   Top-20 展厅（卡片网格 / 因子矩阵）· 维护占位遮罩 · 逐股 AI 抽屉
  views.js   today / candidates / market / evidence / lab / system
preview/demo-20261008/          评审用演示快照（20261008 已公开发布，bd241cdc）；Pages 构建不复制
```
旧 v2（`assets/scripts/v2/`、`assets/styles/app.css`）原样保留，回滚 = 把 13 个页面壳恢复为 main 版本并把 `REQUIRED_SITE_FILES` 改回 v2。

## 2. 路由
| 路由 | 内容 | 依赖 |
|---|---|---|
| `#/today` | Top-20 展厅（主列）+ 市场脉搏 / 决策闸门 / 策略健康 / 数据血缘（侧栏） | 核心 + recommendation_state, strategy_backtests, market_state, review_track_latest, strategy_evaluation |
| `#/candidates` | **v4.4 挑战者竞技场**（2026-10-10）：冠军 vs 挑战者头部 · 三栏差异条（共同入选 / 新策略独享 / 被否决或排序未入选）· 挑战者 Top-20（纯化核心 / VPD / SMF 分量条，卡片 ↔ 矩阵）· 挑战者抽屉 · 否决门与持仓簿 · v4.4 因子权重 | 核心 + recommendation_state, strategy_backtests, v44_challenger_state |
| `#/market` | 指数快照 · 行业热力 · 行业广度/午盘观点 · 领涨/领跌 · 行业动作 | 核心 + market_state, market_industry_heatmap_latest |
| `#/evidence` | 战绩（累计/逐日/命中率，复利计算）· 前瞻验证轨道（x/60）· 结算完整性 · AI 证据剔除 · 方法论 | 核心 + review_track_latest, strategy_evaluation, dual_track_state |
| `#/lab` | 双轨影子 · S3（前瞻 x/60、中位 vs p75 成本）· 剧本引擎 · 因子进化 · AI 观点分布 · 已归档策略 | 核心 + s3_watchlist, setup_engine_status, dual_track_state, factor_evolution_state, sentiment_state |
| `#/system` | 流水线 · 日期合同 · 发布守卫 · 系统健康 · 策略档案 | 核心 + publish_guard_state, system_health, strategy_registry, strategy_run_state |

核心 = run_manifest, system_verdict, decision_state, market_context, candidate_state。只有 run_manifest + system_verdict 都缺失时才整页报错；其余缺失只降级对应面板。

查询参数（写在哈希里）：`?demo=live` 切到演示快照；`?s=000651` 打开该股 AI 抽屉（可分享、可后退关闭）。

## 3. Top-20 数据
来源优先级：`recommendation_state.final_recommendations` → `strategy_backtests.strategies[].top20` → `candidate_state.candidates`，按代码合并 candidate_state 的市场动作。
卡片：排名、名称、6 位代码、行业、综合分环、AI 分、动作徽章、价格涨跌、四个核心因子条（波动收敛 / 量稳定性 / 动量 / 温和放量）、AI 结论两行摘要。
抽屉：投资逻辑（ai_conclusion）· 分项诊断（ai_points 按「标题：正文」解析）· 催化 / 风险 · 全部因子子分 · 执行参考（buy_zone / invalidation / next_day_handling）· 规则约束 · 证据时间。

## 4. 视觉
深色默认 + 浅色；`data-updown="cn|intl"` 切换红涨绿跌 / 绿涨红跌（价格色 `--up/--down` 与状态色 pass/warn/block 分离，因子热度用强调色，不借用涨跌色）。
数字统一等宽 tabular-nums。断点：≥2200 宽侧栏、≤1280 侧栏下沉、≤900 单列、≤760 底部标签栏 + 抽屉变底部面板。偏好存 localStorage（try/catch，失败不影响渲染）。

## 5. 验证
`node tests/v5-render.test.mjs`：白名单约束、演示快照 20 卡 + 抽屉全字段、维护模式 20 占位 + 遮罩、三种数据形态 × 6 视图干净渲染、复利、风险分口径、XSS 转义、取数器、页面壳委托。

## 6. v4.4 挑战者（2026-10-10）
`data/latest/v44_challenger_state.json` 由 workspace `export_v44_challenger.py`（发布器 step2e，非阻断）生成：读取本机
`selection_state/prebreakout_v44_latest.json`（run_strategy_suite 落盘）与已发布的 v4.3 `recommendation_state`，
输出挑战者 Top-20（v4.4 因子 + 挑战者分量 z/分位）与差异桶。缺失时写 `status=unavailable`（前端降级，不 404）；
维护模式由 maintenance_mode.apply 统一撤下。v4.4 为影子策略：无执行权限、不改 v4.3 名单、未完成前瞻验证；
仅与冠军共同入选的股票带有 v4.3 同日 AI 分析。
