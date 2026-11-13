# 本系统能力盘点(Phase 0 冻结, 2026-07-11)

目的: **防重复建设硬门**——任何"从头部项目借鉴"的提案, 先对照本表; 已有能力只能提"升级差距", 不得提"新建"。

## 1. 数据层
| 能力 | 现状 | 已知差距(升级候选) |
|---|---|---|
| 行情数据 | tushare(第三方代理, 唯一合法通道)+akshare(新浪日历/行情兜底); backtest_cache/*.parquet 按日落盘(daily/stk_factor/cyq_perf) | 无 Raw 不可变层; 缓存文件可被盘中裸跑污染(0710事故); 无 revision/双时间 |
| 研究面板 | panel_full 1298日全历史(冻结保护, 5-6月缺口已补) | 无 PIT as-of API; 更新靠重建 |
| 分钟/竞价 | B0 全量竞价(2022→今)+分钟首批; VWAP单位错位有物理边界自愈护栏 | 分钟覆盖非全量 |
| 名称/universe | stock_list.csv(4925)+panel_full 双源; S3 冻结人群=ST/退/次新/摘牌排除 | universe 语义分散在各消费点, 无统一 universe 服务 |
| 数据源单点教训 | Tushare 日历 SPOF 已去(akshare 主用); GREENFIELD 死源复活事故(0710) | **provider 候选列表无新鲜度契约——Phase4 provider contract 正治此** |

## 2. 策略与因子层
- 三策略: prebreakout_v41(可交易, AI 20/20)、greenfield_o2c_v1(research_only, review_gate 6条件)、t1_factor_v1(退役为永久研究观察, 模板注记)。
- 因子闯关机(factor_gauntlet): 五道闸+gate6 holdout+日聚合去市场均值 placebo(行级恒等已证伪并替换)。
- setup_registry.json 预注册(S1-S8 已判决, REJECTED 定义只读); necromap.json 防重测地图(14死签名+77词条聚宽图谱)。
- 已证伪结论(红线, 勿再投入): T1 无样本外边际; 短线启动三段分解(可成交段太薄); 尾盘隔夜开口两向关闭; 价量降级为闸门。
- 全市场因子评估引擎(rank IC/分位净收益/多重比较校正)+时点快照捕获(PIT, 杜绝前视)。

## 3. 执行与合同层
- 发布合同 v2: validate_publication_contract.py --strict, 17 族检查(含收盘一致性/指数 exact_close/O2C 硬校验/final_action 禁买入级)。pre-push 守卫装在所有克隆。
- run_manifest + run_lineage(core_aligned 单一血统门, 拒绝拼装绿灯)+reconcile 竞态兜底。
- 复盘: 真指数基准(沪深300/中证500)超额+IR; review_gate 升级门槛; O2C/三策略统一复盘。
- S3 前瞻账本: 冻结 4 规则, 60笔裁判(按笔——**已知未解决: 应按日, 用户拍板中**), regime>70% 护栏; 结算-污染重置铁律。

## 4. 调度与运维层
- Gateway cron 唯一调度源(OS crontab 已去重): 19:30 管线(9 stage)+20:00 报告(已改造去老桥接)+20:45 收盘闭环+21:05 S3+21:30 备份。
- 硬告警: pipeline_alert(覆盖/落后/回填, 收盘感知基线)+STAGE_TIMEOUT+Discord 通道。
- 磁盘水位: KEEP_DAYS=5, <8G 裁剪, <4G 拒备份。**已知: recommendations.db 全量日备 1.4G/天过重(Wave1 候选: 增量/压缩备份)**。
- 护栏: 交易日收盘前禁裸跑; git subprocess errors=replace 通治; 生成器失败标记黑名单。
- AI 链: 直连 litellm 三件套(mimo 推理模型 max_tokens≈4500), 老桥接全线退役; AI 永不改冻结量化分数与 verdict。

## 5. 发布层
- stock-report(GitHub Pages, v2 合同): 5 页客户向+研究诚实性规范(honesty banner); 唯一推送路径 publish_stock_report_v2.sh; 旧工作克隆已对齐 origin(仅作 strategy_backtests.json 工作区, 待退役)。

## 6. 上游关系
- daily_stock_analysis = ZhuLinsen/daily_stock_analysis 的本地深改克隆(HEAD c7059b5, 9 改+5 自有, 上游~309 提交领先)——**独立迁移项目**(Phase2 差异报告→Phase3 worktree 逐切片 backport)。
