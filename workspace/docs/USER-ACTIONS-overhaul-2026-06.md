# 整改执行 — 需要 fisher 在 Mac 上完成的事项

> 由 2026-06-12 全量整改会话生成。代码侧改造已由 Claude 完成；以下事项涉及外部账号凭证或需在 Mac 本机执行，必须人工完成。按优先级排序。

## 🔴 立即（凭证轮换）

### 1. 轮换 GitHub PAT（旧 token 已暴露在 git 配置中数月）
现状：旧 PAT（`ghp_9hYc...`）已从两个仓库的 remote URL 移除，迁入 `~/.openclaw/workspace/.secrets/git-credentials`（600 权限），推送功能不受影响。但旧 token 仍应作废：
```bash
# 1) 在 GitHub → Settings → Developer settings → Tokens 新建 fine-grained token
#    仓库限定 fisher-admin/stock-report，权限只给 Contents: Read and write
# 2) 更新凭证文件（替换 <NEW_TOKEN>）：
echo 'https://fisher-admin:<NEW_TOKEN>@github.com' > ~/.openclaw/workspace/.secrets/git-credentials
chmod 600 ~/.openclaw/workspace/.secrets/git-credentials
# 3) 验证推送通道：
cd ~/.openclaw/workspace/stock-report && git fetch origin
# 4) 回 GitHub 撤销旧 token
```

### 2. 轮换 Tushare token（曾以明文散布于代码/文档，且经 HTTP 明文传输）
现状：代码中全部明文已清除，统一从 `~/.openclaw/workspace/.secrets/tushare_token` 加载。
```bash
# 1) tushare.pro 个人中心重置 token（若 token 来自代理服务商 jiaoch.site，则联系服务商轮换）
# 2) echo '<NEW_TOKEN>' > ~/.openclaw/workspace/.secrets/tushare_token && chmod 600 同文件
# 3) 验证：~/.openclaw/venv/bin/python -c "import sys; sys.path.insert(0,'/Users/fisher/.openclaw/workspace/skills/stock-analyzer'); import pipeline; print(pipeline.init_tushare().daily(trade_date='20260611').shape)"
```
建议（非阻塞）：与 jiaoch.site 服务商确认是否支持 HTTPS；支持则更新 `.secrets/tushare_api_url` 为 `https://...`，消除明文传输。

### 3. 修复 AI 分析 venv（jiter 损坏，AI 链路断裂的根因）
```bash
bash ~/.openclaw/workspace/ops/repair_ai_venv.sh
```
（脚本由本次整改创建，含重建 venv + 依赖安装 + LLM 冒烟测试。）

## 🟡 一周内

### 4. workspace 本地 git 历史含旧 token（低风险：该仓无远端）
`skills/stock-analyzer/pipeline.py` 等文件的历史提交中仍有旧 token。该 git 仓是纯本地仓（无 remote），泄露面有限。如需彻底清除：轮换 token 后即可不处理；或 `git filter-repo --replace-text` 清洗。

### 5. stock-report 公开仓 git 历史瘦身（249MB → 约 10MB）
旧 PAT 轮换后执行（脚本由本次整改创建，含备份与确认提示）：
```bash
bash ~/.openclaw/workspace/ops/slim_stock_report_git.sh
```

### 6. OpenClaw cron「市场晨判-主频道-0800」最近一次投递失败（Message failed）
需在 OpenClaw 的 cron 配置中检查该任务的投递目标（频道 ID/webhook）是否有效。代码侧无法修复投递凭证。

## 🟢 建议尽快（闭环与验证）

### 7. 新增两条 cron（验证闭环 + 备份，整改已建好脚本）
在 OpenClaw cron（或 crontab）添加：
```
20:45 工作日   bash ~/.openclaw/workspace/ops/run_daily_closure.sh    # T+1验证回填+治理评估+健康告警
21:30 每日     bash ~/.openclaw/workspace/ops/backup_databases.sh     # 三库在线备份(保留14天)
```
可选：把 Discord webhook 写入 `~/.openclaw/workspace/.secrets/discord_webhook`，健康告警将主动推送（否则仅打印日志）。

### 8. 因子热修真实数据验证（今晚 19:30 管线运行前后各看一次）
```bash
cd ~/.openclaw/workspace/skills/stock-analyzer && ~/.openclaw/venv/bin/python validate_hotfix.py --days 30
```
输出新旧 TOP20 对比、行业集中度 HHI、滚动30日次日收益对照。预期：银行股 ≤5 只、行业 HHI 显著下降。
若需紧急回退热修：`export OPENCLAW_DISABLE_HIST_FEATURES=1`（一键恢复旧打分行为）。

### 9. 前端 v2 已切换（随今晚管线推送上线）
- 9 个页面已指向 v2 模块（按页加载，首屏 6.6MB→~200KB）；旧版文件保留未删
- 上线后浏览器检查各页；若有问题一键回退：`bash ~/.openclaw/workspace/ops/rollback_frontend_v2.sh`

## 记录
- `.secrets/` 已加入 workspace `.gitignore`；目录 700、文件 600。
- 两个发布仓 remote URL 已无凭证；credential.helper 指向 `.secrets/git-credentials`。
- 全部代码/文档明文 token、旧代理 IP（<REDACTED_IP>）已清除（除本方案文档中的描述性引用）。
