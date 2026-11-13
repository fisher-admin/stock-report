# 研究红线(Phase 0 冻结)

任何 Phase/Wave 的提案与实施, 违反任一条即整体否决(硬门, 先于加权评分):

1. **不破冻结**: setup_registry 已判决条目/wts_frozen_rules/s3_frozen_cells/holdout 名单/REJECTED 定义/前瞻账本历史行 只读。
2. **无未来数据**: 任何借鉴机制落地必须过 PIT 验收(as-of 时点 available_at>as_of 记录=0); 新数据接口先过可得性+单位+历史可得性审计 spike(stk_auction_c 前科)。
3. **许可证**: GPL(freqtrade/backtrader)/LGPL(NautilusTrader) 只思想借鉴禁复制代码; OpenBB 许可为 NOASSERTION, 深读先核实条款再定; 研究卡强制 license 字段。
   - 勘误(2026-07-11, 用户批准): **OpenBB=AGPL-3.0**(Phase2 深读全文核验, GitHub NOASSERTION 系自定义序言所致), 按 GPL 同级红线执行: 禁复制代码/禁引入依赖。
4. **可回滚**: 每项实施必须有回滚剧本; 生产链不受扰——一切实验走 scratch worktree/shadow。
5. **不重复建设**: 提案先对照 local/system_inventory.md; 已有能力只提升级差距。
6. **开源项目=想法与工程范本, 非证据**: 策略想法一律过 necromap(说不清与死签名区别=死), 幸存者从 S9 起预注册五道闸; 任何 AI 环节不宣称 alpha、不改冻结量化分数。
7. **用户拍板清单**: 成本模型/阈值放宽、cron/发布行为变更、任何花钱项。
8. **磁盘纪律**: 研究产物≤2G; **禁全量克隆**(API/raw 快照优先); free<6G 时禁克隆类操作(当前 5G——本轮全程纯文本)。
9. **证据纪律(条令 v1.1)**: 文件:行(或 path#anchor)引证绑定冻结 SHA; 抽检不合格整卡退回; 覆盖率必须自白(coverage_confession), 禁假装全覆盖; 卡两次上报。
