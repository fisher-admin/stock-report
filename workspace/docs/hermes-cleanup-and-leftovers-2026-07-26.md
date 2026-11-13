# Hermes 退役 + 遗留问题处置执行报告（2026-07-26 深夜）

> 前置审计：4 agent 只读预检（117 次工具调用），确认生产链对 ~/.hermes 零功能依赖后才动手。
> 本报告承接 docs/p0-remediation-evaluation-2026-07-26.md 的遗留清单。

## 一、调度器复活 + 周一链路验证（原 P0-1，已闭环 ✅）

| 动作 | 结果 |
|---|---|
| `launchctl bootstrap gui/501 …ai.openclaw.gateway.plist` | node PID 9911 起，:18789 双栈监听，launchd state=running |
| 漏跑任务补跑 | 调度器自动把 19:30 热榜晚报/21:30 三库备份重排至 22:26/22:27，**双双 ok** |
| 投递烟测（活体） | 热榜晚报 22:27:48 `finished ok`，run 记录明示 deliveryStatus=**delivered**，"✅ 晚报已推送至 Discord"——对比 7/25-26 白天 12/12 次 DELIVERY_FAILED |
| openclaw CLI | 恢复正常（rc=0）。体检报告"CLI 诊断链路失效"实为网关死亡的下游症状 |
| 出站网络 | 网关 22:27 起对 mimo 云 API 全部 200；TLS 风暴确认是重启前旧病，未复发 |

**静默死亡根因（新发现）**：`sfltool dumpbtm` 显示 `ai.openclaw.gateway` 的 BTM Disposition = `[enabled, disallowed, notified]`——macOS"允许在后台运行"开关被关闭，所以开机 launchd 不拉起；而 `ai.hermes.gateway` 是 `allowed`，旧系统反而每次开机自动复活。手工 bootstrap 本次已生效，但**下次重启仍会复发**，根治需人工一步（见"待 Fisher"）。

## 二、Hermes 退役（全程零删除，可整体回滚）

**预检结论**（4 轨审计）：
- AI 生成链路零 hermes 依赖：litellm 是 Python 客户端库（`generate_o2c_ai.py:121` 延迟导入），端点是小米云 API（`daily_stock_analysis/.env` 的 `OPENAI_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1`），本机不存在 litellm 服务进程；系统早有反 hermes 路径断言护栏（test_env.py:44-49）
- openclaw sqlite 21 任务 payload 零 hermes 引用；shell 启动文件无 autostart
- ~/.hermes 9.3G 无独有生产数据（独有内容仅会话历史 state.db 1.5G + sessions 697M、mailsentry 独立项目 193M、4月因子进化残档）

**执行动作**：
1. **迁移 2 个独有任务入 openclaw cron**（此前全机唯一调度入口在 hermes）：
   - `research_preview_daily_run`（30 15 * * *，id ddbb4022）：research_preview_simulator + promotion_evaluator
   - `factor_health_weekly_check`（0 16 * * 5，id 256b3065）：factor_health_monitor
   - 均为纯 command 载荷（sh -lc，cwd=factor_factory，qlib_lab venv），no-deliver 对齐原 deliver=local
   - **通路实测**：手动触发周检 → `outputs/research_preview/factor_health_20260726.json` 22:40 落盘 ✅
2. **退役 7 个任务**：08:30 factor_factory_daily_selection（与 openclaw 19:30 管线 stage3 三步完全重复，无早间消费者；如需盘前刷新，一条 openclaw cron 即可恢复）；09:15 orchestrator_daily_pipeline（僵尸：跑 hermes 侧陈旧克隆，推荐库冻结 20260518，82 次纯烧 token 的失败运行）；5 个 paused 任务（自证"已迁回 OpenClaw"）
3. **停服务**：`launchctl bootout + disable` ai.hermes.gateway / ai.hermes.dashboard（KeepAlive 服务，直接 kill 会复活），实测进程清零、8642 释放
4. **归档**：3 个 plist → `~/.openclaw/backups/hermes-launchagents-20260726/`；整个 ~/.hermes → `~/.openclaw/backups/hermes-archived-20260726/`（同卷 mv 瞬时，9.3G 原样保留）
5. **拔活引用**：`publish_stock_report_v2.sh:14` LOG_DIR 从 `~/.hermes/logs` 改为 `~/.openclaw/logs/publish_v2`（提交 7f53245）——这是生产链最后一处会重建 ~/.hermes 的自愈路径

**留档不阻塞**：3 个休眠脚本仍硬编码 ~/.hermes 路径（compute_chip_factors.py:11、superpower_factor_autopilot.py:106、daily_t1_alpha191.sh:7），均无调用方/已退役，重跑前需改路径。

## 三、其余遗留处置

1. **x-monitor 凭证出 HEAD**（提交 ecb5598）：`git show HEAD:x-monitor/credentials.json` 已报不存在。历史（10b77c8）仍含 token，清洗排期见"待 Fisher"。
2. **收编补漏**（提交 51 文件 +5904 行）：scripts/ 12 个生产运维脚本、factor_factory 管线入口/tests/21 个 config yaml/memory 日志/qlib_lab 轻量证据、outlookemail README——全部经秘钥扫描零命中后入库。
3. **新雷排除**：`services/outlookemail-local/` 经扫描确认是**活体凭据仓库**（SECRETS.local.json 明文密码+64 位 SECRET_KEY、docker-compose.yml 内嵌同密、data/outlook_accounts.db 100 个邮箱账户密文凭据）——`.gitignore` 整目录拦截仅放行 README；`factor_factory/.gitignore` 补 `outputs/`（内含 109MB 单文件，超 GitHub 100MB 硬限）+`mlruns/`+config 备份副本。
4. **根目录清渣**：107 个调试残渣（z*/comp_*/hx_*/tmp_h* 等 7/12 hermes 调试遗物）移入 `~/.openclaw/backups/root-debris-20260726/`，根目录只剩正主。
5. **新 bundle**：`workspace-20260726-post-cleanup.bundle`（750M，verify 通过，含今晚 4 个新提交 ecb5598/7f53245/收编/护栏）。仍在本机同盘、仍含凭证历史——离机与外发前提不变。
6. 剩余 536 个未提交路径几乎全是刻意排除的数据类（backtest_cache parquet 315、selection_history 83、gauntlet_reports 74），代码/配置漏收编已清零。

## 四、待 Fisher（按紧急度）

1. **【一次点击，防复发】** 系统设置 → 通用 → 登录项与扩展 → "允许在后台"列表中找到 openclaw 网关条目（BTM 显示名可能是 **"sh"** / Unknown Developer，对应 ai.openclaw.gateway）→ 打开开关。不做的话**下次重启调度器再次静默死亡**（本次已手工救活，重启前有效）。
2. **【撤销两个 token】** ① x-monitor bearer_token（在 workspace git 历史 10b77c8）；② hermes 归档区 stock-report 克隆 remote URL 内嵌的 GitHub token（`~/.openclaw/backups/hermes-archived-20260726/stock-system/workspace/stock-report`，即 6/12 手册 T4a 一直没轮换的旧 PAT）。撤销后 git 历史清洗（可连 parquet 瘦身同批）才有意义。
3. **【周一晚观察】** 20:45 收盘链首次在链内跑新增的 PEAD 摄取块；15:30 迁移后的 research_preview 首次自动触发——各看一眼日志。
4. 悬而未决的拍板项不变：WTS kill 线执行、B-4 接线、P-1/P-2、outlookemail docker-compose 改 env_file+轮换后是否入库。
