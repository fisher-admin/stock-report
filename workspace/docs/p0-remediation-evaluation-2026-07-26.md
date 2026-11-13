# 7/26 体检+优化工作执行评估（当晚复核）

> 生成：2026-07-26 22:15 · 方法：6 轨并行审计 + 逐轨对抗复核（12 agent，363 次工具调用）
> 评估对象：docs/stock-system-monthly-review-2026-07-26.md 开出的 P0 清单（6 项）+ 当晚 5 个提交（54a5e3f…bd50a3f）
> 所有 critical/high 主张均由第二个独立 agent 重新取证；下文标注 [已复核] 者为双 agent 一致。

## 总评

**P0 六项：4 项高质量完成、1 项半完成、1 项（恰是最有时限的那项）完全没做——而且实际情况比报告写的更糟。**

| P0 项 | 状态 | 评级 |
|---|---|---|
| P0-1 调度验证+投递烟测 | **未做，且调度器此刻已死** | ❌ critical |
| P0-2 O2C 合同不匹配修复 | 修复正确 + 护栏已加 + 生产实证 | ✅ excellent |
| P0-3 0724 尾账补跑 | 撕裂消除、全链对齐，3 处低危副作用 | ✅ good |
| P0-4 摘除 ~/.openclaw 泄密引信 | origin 已移除 | ✅ done |
| P0-5 PEAD 摄取恢复 | 抢救 2960 行 + 已挂常态调度 | ✅ excellent |
| P0-6 收编+异地副本 | 收编质量好；bundle 已做但未离机；**凭证历史未清洗** | ⚠️ partial |

## 一、最紧急：周一开盘链路将静默不启动 [已复核]

体检报告说"周一链路未经验证"，实测是**已确定不能跑**：

- 主机今天 **14:53:05 重启**，真正执行 sqlite cron store（含全部 6 个股票 cron）的 openclaw 网关（:18789）**未被 launchd 拉起**：`launchctl print gui/501/ai.openclaw.gateway` → "Could not find service"（plist 存在于 `~/Library/LaunchAgents/ai.openclaw.gateway.plist`，RunAtLoad+KeepAlive，未被 disable，纯粹是重启后未 bootstrap）。当前无任何进程持有 openclaw.sqlite，18789 无监听。
- **已实证漏跑**：今晚 19:30（每日热榜-晚报）、21:30（每日数据库备份三库）两个到点任务均未触发，next_run_at 停在过去时刻。
- **体检报告有两处错误**：① "调度本体仍在跑（备份/compact 正常）"在报告生成时（20:34）就已过时——调度器 14:51 起已死；② "cron 已迁到 hermes gateway"前提不成立——hermes(8642) 跑的是独立 store `~/.hermes/cron/jobs.json`（9 任务），其中 A 股晨判/午判/选股**自 4/24 起就是 paused**，与 sqlite store 零关联。6 个股票 cron 一直住在 openclaw.sqlite 里，执行者就是死掉的 openclaw 网关。
- 第二关同样未验证：Discord 出站 7/25 19:32 起 12/12 次投递全失败（TLS 断连），outbound 队列积压 161 条，重启后从未复测。

**修复动作（周一 08:00 前必须）**：
1. `launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.openclaw.gateway.plist`
2. 确认 `lsof -i :18789` 有监听、sqlite cron_run_logs 恢复写入（漏跑的备份任务会否补跑需观察）
3. 做一次真实投递烟测（Discord 出站全天故障，网关活了≠投递通了）

**叠加风险**：P0-3 补账把 0724 判决从 halt 改写为"谨慎执行"——若周一晨链没起来，线上将带着一个过期的可执行判决进入交易时段。调度器修复因此更加不可拖。

## 二、做得好的部分（全部一手证据坐实）

**P0-2 O2C 修复 [已复核，evidence 链完整]**
- 方案正确：完成判定 `== 'o2c_factor_llm_analysis'` → `in O2C_LLM_SOURCE_KINDS={'o2c_ai_analysis','o2c_factor_llm_analysis'}`，两种 kind 全覆盖、无误杀；复核 agent 专门穿透了三个读取入口（{date}.json / _o2c_greenfield / _factor_combined），确认失败/未生成股票不可能被误判为完成。
- 报告建议的"连续 N 轮 pending 集不变即提前退出"护栏**已加**（默认 3 轮，env 可调，下限钳 2，写 `stalled_no_progress` 并 rc=1）。
- 生产实证：今晚补跑 aiBackfill **rc=0、attempt=1、6 秒**（对比修复前 0722/0723 夜烧满 2700s、13 轮空转）；发布仓 push 从每晚 31/34 个降到 4 个。
- 附带改进合理：O2C 走专用生成器 + 独立 2400s 超时，避免与 bridge 默认 1500s 抢时被杀。

**P0-3 0724 尾账 [已复核]**
- run_manifest 与 system_verdict 双双 =20260724，checked_fields 7 个日期字段全对齐；recommendations.db 已有 0724；VALIDATION_STALE 清除（validation_report.checks.run_lineage 5 个 run_id 全部指向补跑 run）；stock-report main==origin 已推公网；周日补跑未污染 trade_date。
- 三处低危副作用留档：① 0724 判决 halt→cautious_execute（见上文叠加风险）；② 发布的 system_verdict.run.run_id 仍指旧 0723 run（血缘时序缺陷）；③ recommendation_fact 0724 首次出现 40 行（历史每日 20 行，greenfield_o2c_v1 首次入仓），假设每日 20 行的下游可能误读。晨判层仍停 0723（warning 级，未回填）。

**P0-5 PEAD [已复核]**
- 今晚 20:47 实跑：PIT 店 61464 行、今日新入 2960 行，与提交文案精确吻合。
- 不是一次性抢救：已挂入 `ops/run_daily_closure.sh` 且由 enabled cron（工作日 20:45）常态执行——**但该 cron 也在死掉的 openclaw 调度器里，一切以 P0-1 修复为前提**。
- 切读确认未做（符合计划，无隐性 PIT 污染）。残余：摄取步骤是 `|| echo` 非致命包裹 + freshness watchdog 零覆盖 PEAD/PIT，未来静默失败会无告警地重新丢 vintage。

**收编提交质量**
- 5 个提交消息与内容相符；bd50a3f 声明的排除清单（backups/recommendation_warehouse/qlib_lab 等）实测 1100 个文件零违例；diff 正文按 sk-/AKIA/ghp_/Bearer 等模式扫描零命中，未带入新明文秘钥。

## 三、安全侧：引信只拆了一半 [已复核]

- ✅ `~/.openclaw` 配置仓 origin 已摘（P0-4 完成）。
- ❌ **workspace 自己的 git 历史含真实凭证**：`HEAD:x-monitor/credentials.json` 可直读出 118 字符 bearer_token（入库于 10b77c8），今晚 5 个提交未处理，worktree 删除还停在未暂存状态。
- ⚠️ 异地副本半成品：当晚 21:17 已生成完整 bundle（`~/.openclaw/backups/workspace-20260726.bundle`，786MB，verify 通过）——但与仓库**同在唯一内置磁盘**上，防得住仓库损坏、防不住整机丢失；且 **bundle 含完整历史 = 含凭证**，历史未清洗前不可外发、不可加 remote push。
- .git 已从 712M 涨到 760M（HEAD 跟踪 parquet 达 6269 个）。**正确顺序：先撤销 token → 历史瘦身+凭证清洗（parquet 移出 git 可同批做）→ 再加私有 remote/外发副本。**

## 四、体检报告本身的准确性

抽查 3 条最有分量的主张，3/3 实质吻合：
- WTS kill 线：独立重算逐位一致（1085 笔 / 均值净 -1.2431%/笔 / 胜率 34.19%；R2=159 笔 -1.226%、R3=382 笔 -1.310%、R4=516 笔 -1.037%，均过 60 笔阈值且为负）——**kill 线触发成立，待拍板执行**。
- strategy_evaluation.json 的 -100%/nav=0 口径 bug 属实且仍在（今晚 21:13 补跑重新生成后依旧；按排期属 P1-12，未修不算失分）。小瑕疵：报告的"日均亏 -12.19%"实为亏损日均值（avg_loss_pct）而非日均收益。
- "发布仓 chore 提交 375 个/21 日"在其本意口径（publish stock report）下精确成立，峰值 7/17=89 也精确；全量 chore 实为 456 个（表述略窄）。

失分处集中在调度轨道：如上文，"调度本体仍在跑"写下时已过时、"cron 迁入 hermes"前提错误。其余部分证据密度与准确率都很高。

## 五、遗留清单（按优先级）

1. **今晚/周一 08:00 前**：bootstrap openclaw 网关 + 投递烟测（上文命令）。给该服务加开机自愈核查（launchd 已配 RunAtLoad+KeepAlive，本次是重启后未 bootstrap——查清为何未自动加载，否则下次重启复发）。
2. **本周**：撤销/轮换 x-monitor bearer_token → git 历史清洗（连同 parquet 移出 git）→ bundle 复制到外接盘/私有远端；Discord 出站修复 + 第二投递渠道（P1-7）；晨判层 0723 残留回填。
3. **注意疑似漏收编**：`scripts/`（含 cron_delivery_audit.py、delivery_output_guard.py、stock_pipeline_gate.py 等生产脚本）、`services/`、factor_factory 入口与 15 个 config yaml 仍 untracked——与 234be6b "生产脚本全量纳管"的声称冲突，下轮收编补上。
4. 根目录 112 个调试残渣（z*.txt/comp_*.txt/c1.* 等）可清理。
5. run_daily_closure.sh 两个新增块周一 20:45 首次在链内执行（非致命包裹，最坏损失是墙钟+日志噪音）——周一晚看一眼日志确认。

## 附录：审计证据
- 完整逐轨 JSON（含全部 evidence 路径与对抗复核 verdict）：`~/.claude/projects/-Users-fisher--openclaw-workspace/e38fe9bb-aef7-495e-8eb6-d248a26fc6e9/subagents/workflows/wf_4f7a428d-bad/journal.jsonl`
