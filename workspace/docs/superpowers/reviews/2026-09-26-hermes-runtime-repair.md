# Hermes 数据源与运行修复验收

更新时间：2026-09-26 15:30 Asia/Shanghai。当前故障已完成修复与人工全链路复验；整体加固任务及两个真实定时交易日验收仍未完成。

## 根因与实际修复

- RDS 不支持 stock_st，原路径返回 HTTP400；Promax 支持但有间歇503。该接口明确走 Promax，成功的收盘日全量响应经过字段、日期、主键和截断校验后缓存，失败不缓存，不拿旧交易日冒充新日。
- 研究重复下载21日数据，因子多股票请求过大时会超时。请求分批最多200只；研究缓存按端点、日期窗口、采集日及查询形状隔离。实测新旧复权因子不等价，禁止混用旧复权缓存。
- stock_basic 默认响应缺少退市日期，造成339条退市状态错误。改为显式请求必需字段并拒绝无日期的退市记录；修正材料写入 PIT v2，旧文件保留。旧339条均未进入可交易集合。
- 用户确认全部 Hermes 定时任务使用北京时间：默认、news、ai、discordlite 四份配置设为 Asia/Shanghai；九个实际股票任务包装器固定 TZ。默认和新闻网关、面板已重新加载；不调整系统时区。
- 研究每次有独立身份，旧终态归档，人工补跑明确标记 manual_recovery / historical_reconstruction；未开启自动恢复或放宽发布检查。

## 最新正式入口复验

本次人工执行使用最近有效交易日20260924，不计作9月26日市场数据或真实定时成功日。

- 主入口：`/Users/fisher/.hermes/scripts/stock/stock_pipeline_gate.sh --trade-date 20260924`，退出0。
- 主批次：`stock-20260926T071307Z`；北京时间15:13:07至15:15:48，161秒；selection_ok=true、publish_ok=true。
- 数据准备：ok/quality_ok/same_day_ready=true，degraded=false，refresh_errors/quality_errors为空。本轮使用已验证的RDS同交易日缓存：daily5557、stk_factor5556、cyq_perf5557行；不把热缓存复验当作重新全量联网取数。
- 研究入口：`post_publish_research.sh`，STOCK_RESEARCH_RECOVERY=1、STOCK_RESEARCH_ALERTS=0，退出0；未发送额外研究测试通知。
- 研究批次：`research-b2cb22a3902f4095837c38bd637aac10`，关联上述主批次；15:15:49至15:27:21，四项全部通过。
  - shortTrackGeneration：9.590秒（冻结结果与热缓存复用）。
  - factorAttribution：211.568秒。
  - prebreakoutDailyPan：306.120秒。
  - prebreakoutShadowResearch：164.747秒。
- 前一轮实际四阶段也全部通过，耗时约866秒；但运行中的包装器被编辑导致最终shell退出2。该轮不能声称入口成功；本次在稳定文件下重新完整执行，已取得退出0。
- 本次等待研究时因跨北京时间日期正确被发布检查阻断；重新真实执行主管线后自然放行，没有改时间或伪造成功标志。

## 发布与线上核验

- 既有网站发布提交：`f3a4ffffa97437c931cd3d44fdee75001a16f690`。
- public-ci `36226246232`、pages-cd `36226246096` 均 completed/success，提交相同。
- HTTPS回读 `/stock-report/data/latest/run_manifest.json` 返回200，run_id与本次主批次一致，trade_date=20260924，validation_ok/ai_complete/publish_ready/published=true，publish_mode=full，Top20就绪20。
- 本次公开发布发生在研究复验之前；不能宣称15:27的研究结果已再次自动发布。研究完成后的自动二次发布仍未实现。

## 测试与保护

- 最新相关回归73 passed、3 subtests passed（2.63秒）；影子评估与看门狗另9 passed（0.48秒）。
- 统一QA入口66 passed（4.48秒）；上述计数分别记录，不宣称全部互不重复。
- 九个已配置股票入口 bash语法检查通过。四个profile在机器TZ=America/Los_Angeles环境下均解析为Asia/Shanghai。
- 调度计算：下次工作日晨判2026-09-28 08:00+08、选股19:30+08、研究22:15+08。现有定时任务记录保持原成功/失败，不把人工补跑写成cron成功。
- Sep21–24旧 PIT daily_basic/universe 共8文件SHA256与修复前一致。原失败研究摘要及首轮复验摘要已归档到 `stock_data/03-working/health/research_run_archive/`。
- 已有未提交生产改动保留；修复运行目录已生效，但未将混合工作树整体提交或宣称为干净版本发布。

## 尚不能保证或尚未完成

- 供应商仍可能503/403；必需接口没有同口径备用时诚实失败，不伪造可用数据。当天首次ST请求仍依赖Promax，不承诺完全消除外部故障。
- 人工全链路成功不替代后续两个真实定时交易日。9月28/29需逐日核查实际调度、全流程与公开结果。
- 9月10方案中的严格自动恢复、跨入口生命周期租约、所有研究不可变输入/输出隔离及研究后自动二次发布仍未全部实现，整体任务不结案。
- 旧历史55日/513条缺数不在本次验收中宣称已消除。
- 工具审批曾因workspace credits不足中断一次网页回读；用户继续后正常审批恢复并完成HTTPS核验，未绕过权限。
