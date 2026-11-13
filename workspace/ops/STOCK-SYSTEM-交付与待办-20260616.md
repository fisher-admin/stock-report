# 荐股系统｜本轮交付与待办（2026-06-16）

> 一句话现状：**所有代码层面的修复都已完成并提交在本地；唯一卡住「上线 + 根除」的，是把 v4 推上
> origin 这一步——它是不可逆的 force-push，必须由你在自己终端执行（安全策略拦了我）。**

涉及两个 git 仓库：
- **发布仓** `~/.openclaw/workspace/stock-report`（→ GitHub `fisher-admin/stock-report` → GitHub Pages）
- **工作区仓** `~/.openclaw/workspace`（管线/编排脚本所在，本机个人仓）

---

## 一、本会话已完成的工作（均已提交）

### 1. 前端：客户向完全重构 + 视觉升级（发布仓 stock-report）
| 内容 | 提交 | 状态 |
|---|---|---|
| v3 客户向重构：10 页→5 页信息架构、诚实性规范（零硬编码业绩、AI 三态、负收益照实、免责声明） | `c11c726` | 已提交，测试绿 |
| 情绪因子页迁入 v3、退役孤儿 legacy 生成器 | `e21279e` | 已提交 |
| **v4 视觉重做**：Brokerage Pro 方向 + 铜金/香槟金 + 深浅双主题、八类 SVG 图表、抬升式卡片 | `56a6f01` | 已提交，逐页浏览器实测 |
| **历史战绩页层一升级**：进阶绩效指标（年化/夏普/卡玛/盈亏比/最大连亏）+ 分层归因（行业/AI观点/评分段）+ 回测数据产物（CSV 明细 + 评价 JSON） | `cdeb5d6` | 已提交，61 条测试绿 |
| **刻度盘字符重叠修复**：去掉穿心指针，改弧线末端值点 | `ccb4cca` | 已提交，浏览器确认 |

> 验证口径：`node tests/render.test.mjs` 61 全绿；诚实性 grep 干净；CSS 类 0 缺失；深/浅主题 + 移动端逐页截图确认。

### 2. 管线：数据停更根因修复（工作区仓）
| 内容 | 提交 | 状态 |
|---|---|---|
| **预检按输出目录前缀放行**（根治停更）：`data/latest/`、`data/recommendation_analytics/` 下任意文件（含未跟踪）都算合法输出，新增产物永不再漏登记白名单 | `cd51e39` | 已提交，用真实工作仓验证「预检会放行=True」 |
| role 语义修复（AI 缺失不再机械判 avoid）+ 部署前摘要/脱敏钩子 | `298713a` | 已提交 |

### 3. 告警体系：再也不会无声停更（工作区仓）
| 内容 | 提交 | 状态 |
|---|---|---|
| **数据新鲜度看门狗** `data_freshness_watchdog.py`：独立于主管线，查推荐库/行情/发布层 trade_date 滞后 + 最近一次运行是否通过 | `cd51e39` / `2ace73b` | 已提交 |
| 告警直发 **Discord fisher 频道**（复用 openclaw bot token，发到 `1468134011154202675`）；管线 stage6 致命告警同样发该频道 | `eb52045` / `8d49bd7` | 已提交，**端到端实测 http 200 发送成功** |
| **launchd 定时**：工作日 20:30 自动跑看门狗，已注册并手动触发跑通 | （系统任务） | 已加载 `ai.openclaw.stock.freshness-watchdog` |

告警优先级：bot 直发 fisher 频道 → webhook 兜底 → macOS 本机通知（始终可用）+ 日志 + 状态文件。

### 4. 仓库抢救
- 排查中发现发布仓被自动 `git pull` 搞成**游离 HEAD、工作树退回 v3**（你看到 v3 旧 gauge 的原因），已 `git checkout main` 恢复到 v4；旧状态备份在分支 `backup-main-prev-v4`。

---

## 二、当前未解决的核心问题（一个根源，三个后果）

**根源：v4 一直没推上 origin（force-push 被安全策略拦），导致本地 main(v4) 与 origin(v3 肥历史) 分叉。**
由此连锁出三件事：
1. **线上还是 v3** —— gauge 重叠没消失、v4 界面/层一升级用户看不到；
2. **发布仓反复被污染** —— 多个脚本对发布仓 `git pull`，分叉下每次都把仓库搞坏；
3. **新数据发不出去** —— 即便预检已修好、管线能重新产出数据，stage6 的 push 也会因分叉被拒。

> 重要澄清（我之前讲混了）：「数据**产出**」的停更根因（git 预检）**我已修好**，不依赖推送；
> 但「数据**上线发布** + 界面上线 + 根除污染」必须靠这次推送。

---

## 三、需要你做的下一步（按顺序）

### ▶ 第 1 步（总开关）：把 v4 推上 origin —— 在你自己的终端
先确认在 v4 的 main 上：
```bash
cd ~/.openclaw/workspace/stock-report && git log --oneline -1   # 应显示 ccb4cca
```
然后执行（脚本自带完整历史 bundle 备份，可恢复）：
```bash
bash ~/.openclaw/workspace/ops/clean_publish_history.sh
```
该脚本会：① 备份旧历史 → ② 把当前 v4 工作树压成单根提交、force-push 覆盖 origin（顺带把 251MB 肥历史压回几 MB）→ ③ 把两个本地克隆对齐新历史。
- origin 上多出的 `3548cb2 Update report 20260615` 只是 v3 血统的日常数据，被覆盖无妨。
- 若 force-push 被 GitHub 拒（认证/分支保护），把报错贴给我。

### ▶ 第 2 步：验证上线
- 浏览器开 GitHub Pages 线上地址，确认界面变成 v4（铜金主题、刻度盘无重叠）。
- 确认两个克隆已对齐：
```bash
cd ~/.openclaw/workspace/stock-report && git status -sb            # 应为 ## main...origin/main（无 ahead/behind）
cd ~/.openclaw/workspace/stock_data/03-working/stock-report-repo && git status -sb
```

### ▶ 第 3 步：确认数据恢复产出
- 等下一次主管线自动触发（预检现在会放行），或手动触发一次。
- 跑一次看门狗确认：
```bash
python3 ~/.openclaw/workspace/skills/stock-system-orchestrator/scripts/data_freshness_watchdog.py
```
  退出码 0 = 数据已追上；非 0 = 仍有滞后（会推 Discord）。

### ▶ 第 4 步（告警，已就绪，可选验证）
- Discord 告警已配好发到 fisher 频道，**无需你操作**。你已收到几条测试消息即代表通道就绪。

---

## 四、仍待处理 / 已知问题（不在本轮范围，列出供决策）

- **AI 回填慢性失败**：候选股 AI 分析长期为空，根因是外部 LLM 网关（小米 mimo-v2.5-pro）间歇 500 + fallback 没接上，在独立仓库 `daily_stock_analysis` 内。需确认网关稳定性 / 修 fallback 注入 / 查 Gemini env 泄漏。
- **多脚本对发布仓 git pull**：第 1 步消除分叉后这些 pull 即变无害；若想更稳，后续可把发布仓的 pull 全部去掉（发布仓只 push 不 pull）。
- **stage6 致命告警通道**：已接 Discord bot+频道（与看门狗共用）；若你换频道，改 `~/.openclaw/credentials/discord-alert.json` 的 channel_id 即可。

---

## 五、关键文件/路径速查

| 用途 | 路径 |
|---|---|
| 一键清理历史 + 部署 v4 | `~/.openclaw/workspace/ops/clean_publish_history.sh` |
| 数据新鲜度看门狗 | `~/.openclaw/workspace/skills/stock-system-orchestrator/scripts/data_freshness_watchdog.py` |
| 看门狗 launchd 任务 | `~/Library/LaunchAgents/ai.openclaw.stock.freshness-watchdog.plist` |
| 告警频道配置（fisher） | `~/.openclaw/credentials/discord-alert.json` |
| 告警日志 / 状态文件 | `~/.openclaw/logs/data-freshness-watchdog.log`，`stock_data/03-working/health/data_freshness_watchdog.json` |
| 前端设计规范 | `stock-report/DESIGN-V3.md`、`stock-report/DESIGN-V4.md` |
| 回测数据产物 | `stock-report/data/latest/recommendation_history.csv`、`strategy_evaluation.json` |
| 旧 main 备份分支 | `backup-main-prev-v4`（发布仓内） |
