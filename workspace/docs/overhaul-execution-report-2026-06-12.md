# 整改/优化/升级 执行报告（2026-06-12）

按《stock-system-overhaul-plan-2026-06.md》全量推进。本报告记录已落地的全部改动、真实数据验证结果与遗留事项。基本数据（parquet/数据库/历史快照）全程只读，未做任何改写；清理一律归档至 `trash/`。

## 一、阶段0 止血 — 已完成

### 安全
- Tushare token、API 地址明文从 8 处代码/文档清除（pipeline.py、stock_analyzer.py、fix_missing.py、data_adapter.py、TOOLS.md、SKILL.md、TASK.md、旧仓 3 个脚本），统一经新建的 `skills/stock-analyzer/credentials.py` 从 `.secrets/` 或环境变量加载；TOOLS.md 中两处 Brave key 明文一并清除
- GitHub PAT 移出两个仓库的 remote URL，迁入 `.secrets/git-credentials`（600），credential.helper 已配置，推送链路不中断；`.secrets/` 已加入 .gitignore
- **待用户执行**：PAT 与 Tushare token 轮换（见 USER-ACTIONS 第 1、2 条）

### 选股因子热修（核心）
- `backtest.py score_from_bulk()`：动量=当日涨幅、量比=1.0、波动=2.0 三处写死 → 改为从本地日线缓存计算**真实 5/20 日动量、前5日均量量比、20日波动率、量稳定性**（滚动面板缓存，特征覆盖率写入日志与 selection_meta；`OPENCLAW_DISABLE_HIST_FEATURES=1` 一键回退）
- 启动前夕硬过滤 `max_5d_change=15%` 此前实际比较的是**单日**涨幅，现在拿到真实 5 日动量后首次真正生效
- 权重 12% 的 turnover_stability 此前恒为常数（默认 -0.5），现接入真实值；顺带修复 `or -0.5` 把 0.0 误判为缺失的边界 bug（pipeline.py 两处）
- 新增**行业暴露上限**：TOP20 单一行业 ≤5 只（`OPENCLAW_INDUSTRY_CAP` 可配），行业映射经 tushare stock_basic 拉取并缓存 30 天，失败自动跳过并记录
- 新增 ST/退市名称过滤（批量打分路径此前未过滤）
- **权重单源化**：run_strategy_suite 的策略权重改为直接取自 pipeline.py 配置，消除"展示 v4.1 权重 ≠ 实际 v4.3 权重"漂移
- 验证：合成数据测试 5 组全过（`tests/test_factor_hotfix.py`）；Mac 真实数据对照脚本已备（`validate_hotfix.py`）

### AI 链路
- 根因（venv `jiter` 损坏）修复脚本 `ops/repair_ai_venv.sh`（重建+冒烟测试，**待用户执行**）
- `.env` 配置 `LITELLM_FALLBACK_MODELS=openai/gpt-5.5`，消除单模型单点
- 失败污染核查：现役副本上游已修复（失败/解析降级均 success=False 且不落库）；旧库 33.75% 的 score=50 污染记录属休眠副本历史数据，已在其 DEPRECATED.md 中标注
- fisher_bridge 陈旧的 'Gemini 2.5 Flash' 标签改为动态读取实际模型

### 诚实化与 fail-closed
- pipeline.py 硬编码回测数（+15.89%/Sharpe1.71，2月旧数）删除 → 实读 backtest_result.json，缺失则明示
- stage4 `build_backtest` 不再格式化 "+0.00%" 伪数字 → 指标缺失时输出 `status: unavailable`；有真值时附方法学局限说明
- T1 零分名单 fail-closed：发布层两处拦截（全 0 分 → 不发布 + status=no_valid_output）
- 公开 JSON 的 LLM 报错原文：发布层 merge 链路统一清洗（`sanitize_ai_text_fields`）
- O2C 影子盘病态统计（-100%/胜率0.08%）：加合理性守卫，自动打 `invalid_data_suspected` 标记，禁止误读为真实绩效

## 二、阶段1 后端加固 — 已完成（本阶段范围）

- 两个现役 SQLite 开启 WAL + busy_timeout；`ops/backup_databases.sh`（每日在线备份保留14天 + 月度 VACUUM INTO）
- 死代码归档至 `trash/cleanup-20260612/`（55MB）：2 个 .bak 巨型文件、4 个 tmp_stage 脚本、Users/ 嵌套 bug 目录、5 个 phase 一次性改版脚本、硬编码日期的兜底脚本、根目录海报/临时文件
- 旧休眠仓 `~/daily_stock_analysis` 标注 DEPRECATED.md（含数据质量警示）
- `fisher-quant/` 目标骨架建立（datahub / backtest_v2 / verify / ops / tests / config，1700+ 行），作为渐进迁移的承接结构
- 已知未尽事项：workspace 本地 git 992MB（追踪了 6128 个 parquet）——本地仓无远端、风险低，建议后续 `git rm -r --cached stock_data` + filter-repo，未在本轮动

## 三、阶段2 前端 v2 — 已完成并切换

- 视图层重写为原生 ES 模块（assets/scripts/v2/，18 个模块，每个 ≤200 行，渲染层为纯函数可在 Node 离线测试）；重复卡片渲染合并；死函数不再移植；0 内联硬编码色值（并入 token）
- **按页数据清单**：每页只拉所需 JSON（首屏 6.6MB×禁缓存 → 164-396KB×可缓存，run_id 版本化）；13 个硬依赖降为 2 个，任一数据缺失显示占位+原因，不再白屏
- 热力大文件（3.9MB）出 `_latest.json` 摘要（91K/6K，已生成），管线侧生成器 `generate_view_summaries.py` 就位
- 孤儿页与 sentiment 首次进入导航；sentiment.html 移除无用 Vue/Tailwind CDN
- 45 个死资产归档 + git rm（两仓一致）；`data/history/` 经核实仍被 stage4 读写，保留
- 测试：31/31 通过（真实数据 fixture 渲染 + 降级模式 9 页无白屏 + HTTP 端到端）；旧版文件保留，`ops/rollback_frontend_v2.sh` 一键回退
- 改动已在两个克隆本地提交（发布仓 `97c73b7`），随今晚 Mac 管线推送上线
- git 瘦身脚本 `ops/slim_stock_report_git.sh` 就绪（249MB→约10MB，需 PAT 轮换后人工执行）

## 四、阶段3 回测 v2 + 验证闭环 — 已完成并实跑

引擎（fisher-quant/，16/16 单测通过）：次日**开盘价**入场、一字/近涨停开盘放弃（分板块阈值 9.8/19.8/29.8%）、停牌跳过、双边费用+滑点 0.26%、T+1/3/5/10 持有矩阵、全市场等权基准对照、剔银行对照组；指标全部实算，代码零硬编码收益数字。

**真实数据结果（63 个选股日全量回填，验证闭环自 3/25 停摆后复活）：**

| 口径 | T+1 | T+5 |
|---|---|---|
| 信号层（收盘到收盘，无摩擦） | 均值 -0.08%，胜率 42.8%，超额 +0.08% | 均值 -1.02%，胜率 37.0%，超额 -0.34% |
| **真实执行（次日开盘+成本）** | 单笔均 -0.42%，胜率 38.2%，组合 -13.34% vs 市场 -9.33%，**超额 -4.02%** | 单笔均 -1.43%，胜率 33.0%，**超额 -4.32%** |
| 剔除银行后 | 更差（T+1 超额 -6.29%） | 更差（-7.51%） |

**诚实结论**：热修前的"启动前夕"在信号层接近市场中性，但经真实执行摩擦后显著跑输；银行股反而是当时组合的缓冲垫而非问题本身——印证了"因子退化 + 回测口径乐观"的研判。该结果已作为治理基线：

- 策略治理：`fisher-quant/config/strategy_governance.json` 晋级/降级门 + `run_governance_eval.py` 实跑判定——prebreakout_v41 → **active_under_review**（超额与胜率双触发）、t1_factor_v1 → research_no_valid_output、greenfield_o2c_v1 → shadow
- 主动告警：`health_alert.py` 聚合健康合同+治理+验证新鲜度+特征覆盖+行业集中度，异常推 Discord（webhook 可选）；当前实跑已正确捕获 base_only 降级等 4 项
- 每日闭环入口：`ops/run_daily_closure.sh`（建议 cron 20:45）

## 五、验证总览

| 项 | 结果 |
|---|---|
| 全部触碰 Python py_compile | ✅ 通过（20+ 文件） |
| 因子热修合成数据测试 | ✅ 5/5 |
| 回测 v2 单元测试 | ✅ 16/16 |
| 前端 v2 渲染测试（真实数据） | ✅ 31/31，降级模式无白屏 |
| 验证回填 / 治理评估 / 健康告警实跑 | ✅ 真实数据全链路 |
| 旧→新打分真实数据对照 | ⏳ 需 Mac 执行 validate_hotfix.py（沙盒无 parquet 引擎） |
| AI venv 修复 | ⏳ 需 Mac 执行 repair_ai_venv.sh |

## 六、遗留与下一步（按优先级）

1. **用户行动**：见 `USER-ACTIONS-overhaul-2026-06.md`（PAT/token 轮换、venv 修复、2 条 cron、validate_hotfix、git 瘦身）
2. 热修后新证据积累 20 个交易日 → 治理复评 prebreakout（用 run_governance_eval 自动判）
3. 阶段1 深水区（后续会话）：ai_engine 从 daily_stock_analysis 抽取瘦身、配置中心 pydantic 化、巨型文件拆分迁入 fisher-quant、stage4 stale-AI 复用加最大时效
4. O2C 影子盘统计口径重建（迁移到 backtest_v2）；晨判 cron 投递修复（OpenClaw 配置层）
5. 周复盘报告自动化（verify_history_v2 已有数据基础）
