# Codex 交接文档：OpenClaw A股选股系统停更回补

**交接时间**：2026-08-09 15:16（Asia/Shanghai）  
**交接方**：Grok Build（当前会话）  
**接收方**：Codex  
**状态**：回补与明日运行准备已**完成**，相关回补进程已全部停止  

---

## 0. Codex 完成更新（2026-08-09）

本节覆盖后文的初始交接状态；第 1–11 节保留为故障现场记录，不应再按其中的“续跑”步骤重复执行。

- 9 个缺失交易日已通过 OpenClaw 正式入口串行回补完成：`20260728`、`20260729`、`20260730`、`20260731`、`20260803`、`20260804`、`20260805`、`20260806`、`20260807`。
- 每日最终状态均为 `rc=0`、`validation_ok=true`、`ai_complete=true`、`publish_ready=true`。
- 每日启动前夕 AI 分析均为 20/20，模型为 `xai/grok-4.5`；推荐数据库亦已同步。
- `stock-report/data/latest/run_manifest.json` 已推进到 `trade_date=20260807`，最终 `run_id=stock-20260809T114427Z`。
- AI 分析统一使用 `xai/grok-4.5` **中等推理**：生成脚本、OAuth 刷新调用和 6 个启用中的股票分析 cron 均已设为 `medium`。
- 19:30 主任务只调用正式入口 `scripts/stock_pipeline_gate.py`；已移除对退役 bridge 的依赖，并要求同一轮核验 validation、AI、数据库同步与发布状态。
- 下一交易日为 `20260810`；08:00、12:00、19:30、20:00、20:45、21:05 的股票任务均已启用并排期。
- 验证结果：xAI 中等推理真实连通成功；xAI 单元测试 3/3、历史回补定向测试 9/9、相关脚本编译检查全部通过。

---

## 1. 任务目标（用户原意）

用户反馈：OpenClaw 中的选股系统长时间数据未更新、系统未正常运行。要求：

1. 研判停更原因  
2. **补齐这段时间的数据和产出**（缺失交易日全量回补）  
3. 后续会话中已要求：使用与 Grok 相同的 **xAI OAuth 直连**（`xai/grok-4.5` medium），不要走 codex-proxy  

回补范围（交易日）：

```
20260728, 20260729, 20260730, 20260731,
20260803, 20260804, 20260805, 20260806, 20260807
```

（最后完整业务日原先停在 **20260727**；今日为周日 2026-08-09，最近交易日应为 **20260807**。）

---

## 2. 根因研判（已确认）

### 2.1 调度层（为什么 cron 连续失败）

主任务：

| 字段 | 值 |
|------|-----|
| 名称 | `A股选股+启动前夕深度分析` |
| Job ID | `a62ed078-9537-4de7-a26d-702aa0bf58a1` |
| 计划 | `cron 30 19 * * 1-5` `@ Asia/Shanghai` |
| 入口命令（agent 必须执行） | `~/.openclaw/venv/bin/python ~/.openclaw/workspace/scripts/stock_pipeline_gate.py` |

**自 2026-07-28 起连续 error**：

- 任务曾 **硬编码** `model: mimo/mimo-v2.5-pro`  
- mimo API key **401 Invalid**  
- fallback 到 `openai-codex-proxy` 也失败（EOF / unknown provider）  

**已修复（交接前）**：

```bash
openclaw cron edit a62ed078-9537-4de7-a26d-702aa0bf58a1 --model xai/grok-4.5 --thinking medium
```

相关主频道选股/晨判/闭环等 cron 也已改为 `xai/grok-4.5`。

### 2.2 数据层（为什么停在 20260727）

- 业务完整产物最后成功日：**20260727**  
- `stock-report/data/latest/run_manifest.json` 在暂停时仍显示 `trade_date=20260727`（尚未成功推进到最新）  
- Tushare 凭证：  
  - **有效组合**：`~/.openclaw/workspace/.secrets/tushare_token` + `tushare_api_url` → **`https://fast.xiaodefa.cn`**  
  - **无效**：同一 token 打官方默认 API；`~/daily_stock_analysis/.env` 里的 jiaoch.site token  

加载方式（代码约定）：

```python
from credentials import get_tushare_token, get_tushare_http_url  # skills/stock-analyzer
pro = ts.pro_api(get_tushare_token())
pro._DataApi__token = get_tushare_token()
pro._DataApi__http_url = get_tushare_http_url()
```

### 2.3 代码层（回补时踩过的坑）

1. **`stage1_data_prepare.py` 的 `same_day_ready`**  
   旧逻辑要求 target 必须是**全局最新**缓存日。回补历史日时若已有更新日（如 0807），会永远 not ready。  
   **已打补丁**：改为「目标日三表缓存齐全 + quality_ok」即可。  
   备份：`stage1_data_prepare.py.bak-backfill-20260809`

2. **并行 stage1 全脚本易限流**  
   4 路同时跑完整 `stage1` 时，`20260730` 出现 bulk 0 行 + 长时间 batch 重试。  
   **改用 v3 驱动**：专用 Tushare 拉数（3 worker + 0.4s 全局节流）+ 全流水线串行。

3. **盘前/周末 auto trade-date**  
   `stock_pipeline_gate.py` 在 15:00 前若不带 `--trade-date`，会回退到「最近已记录交易日」，可能误锁旧日。  
   **回补必须**：`--trade-date YYYYMMDD` 或 `OPENCLAW_TARGET_TRADE_DATE=YYYYMMDD`。

4. **AI 阶段当前失败点（20260728 已暴露）**  
   - LiteLLM / mimo：`No module named 'jiter.jiter'`  
   - SearXNG 搜索 403 / 限流  
   - 结果：`ai_complete=false`，`ready=0 pending=20`，validation `ANALYTICS_DATE_MISMATCH` / `AI_INCOMPLETE` / `PUBLISH_BLOCKED`  
   - `recommendation_db_sync` 对 20260728 曾显示 ok（stage5 可过），但 **publish 未完成**  

---

## 3. 暂停时精确进度（2026-08-09 15:16）

### 3.1 进程

**已全部停止**（驱动 + pipeline + prebreakout_factory）。  
暂停标记：`~/.openclaw/logs/backfill/PAUSED_AT`

### 3.2 Phase A：行情缓存（已完成 ✅）

路径：`~/.openclaw/workspace/stock_data/03-working/backtest_cache/`

| 交易日 | daily | stk_factor | cyq_perf | data_ready | degraded |
|--------|------:|---------:|---------:|:----------:|:--------:|
| 20260728 | 5524 | 5524 | 5524 | ok | **false** |
| 20260729 | 5524 | 5524 | 5524 | ok | false |
| 20260730 | 5528 | 5528 | 5528 | ok | false |
| 20260731 | 5528 | 5528 | 5528 | ok | false |
| 20260803 | 5529 | 5529 | 5529 | ok | false |
| 20260804 | 5529 | 5529 | 5529 | ok | false |
| 20260805 | 5532 | 5532 | 5532 | ok | false |
| 20260806 | 5533 | 5533 | 5533 | ok | false |
| 20260807 | 5535 | 5534 | 5535 | ok | false |

> 数据层**不必重拉**（除非强制刷新）。剩余工作是 **全流水线产出 + AI + 发布校验**。

### 3.3 Phase B：全流水线（进行中，已暂停）

驱动日志：`~/.openclaw/logs/backfill/driver.out.log`

| 交易日 | 流水线状态 | 说明 |
|--------|------------|------|
| **20260728** | **done rc=1**（约 52.6 分钟） | 策略/stage5 有进展；AI+validation 失败；`run_manifest` 未升到 0728 |
| **20260729** | **进行中被杀** | 已跑到 `prebreakout_factory.py`（CPU 高），**未完成** |
| 20260730–0807 | **未开始** | 排队 |

健康文件（暂停时快照）：

| 文件 | ok | trade_date | 备注 |
|------|----|------------|------|
| `orchestrator_run.json` | false | 20260728 | AI / validation 失败 |
| `validation_report.json` | false | 20260728 | `TRADE_DATE_CONTRACT_BROKEN` / AI incomplete |
| `ai_publish_readiness.json` | false | 20260728 | ready=0/20, `base_only` |
| `recommendation_db_sync.json` | true | 20260728 | stage5 成功 |
| `strategy_suite_run.json` | — | 20260729 | 中断前正在写 0729 |
| `stock-report/.../run_manifest.json` | — | **20260727** | **官方 latest 仍未推进** |

20260728 失败码（gate 汇总）：

```
ANALYTICS_DATE_MISMATCH
AI_INCOMPLETE
PUBLISH_BLOCKED
primary: ANALYTICS_DATE_MISMATCH
root_causes:
  - stage_failed: aiEnrichmentAndPublish
  - validation: analytics dates mismatch
  - validation: publish not completed
  - validation: ai incomplete
```

详细 JSON 输出：`~/.openclaw/logs/backfill/pipeline_20260728.log`

---

## 4. 关键路径地图

```
~/.openclaw/
├── openclaw.json                          # 主配置（agents 已切 xai/grok-4.5）
├── venv/bin/python                        # 流水线 Python
├── logs/backfill/                         # 回补日志与暂停标记
│   ├── driver.out.log
│   ├── pipeline_YYYYMMDD.log
│   ├── PAUSED_AT
│   └── driver.pid                         # 旧 PID，已无效
└── workspace/
    ├── scripts/stock_pipeline_gate.py     # 官方入口（cron 也用它）
    ├── skills/stock-system-orchestrator/scripts/
    │   ├── run_pipeline.py                # 编排器 --trade-date
    │   ├── stage1_data_prepare.py         # 已 patch same_day_ready
    │   ├── stage3_strategy_suite.py
    │   ├── stage4_ai_publish.py
    │   ├── stage4_ai_backfill_loop.py
    │   ├── stage5_recommendation_db_sync.py
    │   └── validate_run.py
    ├── skills/stock-analyzer/
    │   ├── credentials.py                 # Tushare secrets 读取
    │   ├── pipeline.py / backtest.py
    │   └── run_strategy_suite.py
    ├── stock_data/03-working/
    │   ├── backtest_cache/                # daily_*/stk_factor_*/cyq_perf_*.parquet
    │   ├── health/                        # data_ready_*, orchestrator_run, validation...
    │   ├── stock-report-repo/             # 工作仓库
    │   └── recommendation_warehouse/
    ├── stock-report/                      # 发布仓库（对外产物）
    │   └── data/latest/run_manifest.json
    └── daily_stock_analysis/              # AI bridge / LiteLLM（当前 jiter 问题）
        ├── .env
        └── venv/bin/python3

/tmp/stock_backfill_driver_v3.py           # 回补驱动（并行拉数+串行流水线）
```

运维文档（背景）：

- `~/.openclaw/workspace/docs/stock-system-operations.md`  
- Skill：`~/.openclaw/workspace/skills/stock-system-orchestrator/SKILL.md`  

---

## 5. 环境与鉴权（续跑前必查）

### 5.1 Tushare（已验证有效）

```bash
export TUSHARE_TOKEN=$(cat ~/.openclaw/workspace/.secrets/tushare_token)
export TUSHARE_API_URL=$(cat ~/.openclaw/workspace/.secrets/tushare_api_url)
# 应为 https://fast.xiaodefa.cn
```

### 5.2 OpenClaw 主模型

```bash
openclaw agents list
# main / secondary 应为 xai/grok-4.5
openclaw models status
# xai:default OAuth (<REDACTED_EMAIL>)
```

xAI OAuth 从 `~/.grok/auth.json` 导入过（与 Grok Build 同 client_id）。若过期：

```bash
openclaw models auth login --provider xai --method oauth
# 或 grok login 后再同步 token 到 openclaw auth store
```

### 5.3 AI 分析（当前阻塞点）

`daily_stock_analysis` 侧：

- 依赖 LiteLLM + 配置的模型（曾用 mimo，现报 `jiter.jiter` 缺失）  
- 情报搜索依赖 SearXNG 公共实例，大量 403/限流  

**Codex 优先修复建议**：

1. 在 `daily_stock_analysis` venv 安装/修复 `jiter`（或升级 litellm/openai 依赖）  
2. 将 AI 模型改为可用通道（xAI / 本地可用 proxy），不要继续挂死 mimo  
3. 回补阶段可考虑 **降级发布**（base_only / 允许 AI 不全）若业务接受；否则必须先修好 LLM  

---

## 6. Codex 接手续跑步骤（推荐）

### 6.1 确认进程干净

```bash
ps aux | grep -E 'stock_backfill|stock_pipeline_gate|run_pipeline|prebreakout_factory' | grep -v grep
# 应为空
```

### 6.2 先修 AI（强烈建议，否则每天都会 rc=1）

重点检查：

- `~/.openclaw/workspace/daily_stock_analysis/.env`（模型、base_url）  
- `~/.openclaw/workspace/daily_stock_analysis/venv` 的 litellm/openai/jiter  
- stage4 日志：`health/ai_publish_readiness.json`  

### 6.3 从中断日续跑 Phase B（数据已齐，勿盲目 purge）

**不要**默认 `FORCE_REFETCH=1`（会重拉 9 日 Tushare）。

单日重跑示例：

```bash
export TUSHARE_TOKEN=$(cat ~/.openclaw/workspace/.secrets/tushare_token)
export TUSHARE_API_URL=$(cat ~/.openclaw/workspace/.secrets/tushare_api_url)
export OPENCLAW_TARGET_TRADE_DATE=20260729
export PYTHONUNBUFFERED=1

# 推荐：官方 gate（含失败汇总 JSON）
~/.openclaw/venv/bin/python \
  ~/.openclaw/workspace/scripts/stock_pipeline_gate.py \
  --trade-date 20260729 \
  2>&1 | tee ~/.openclaw/logs/backfill/pipeline_20260729_resume.log
```

建议顺序（剩余）：

```
20260729 → 20260730 → 20260731 → 20260803 → 20260804 → 20260805 → 20260806 → 20260807
```

可选：先 **重跑 20260728**（AI 修好后），因当日 validation/publish 未过。

### 6.4 或继续用 v3 驱动（需小改）

文件：`/tmp/stock_backfill_driver_v3.py`

当前逻辑：Phase A skip 已有 good 缓存；Phase B 从列表头开始。  
若直接重跑 v3，会 **再次从 20260728 串行全跑**（每天 ~50min+）。  

建议 Codex 改驱动：

- 增加 `OPENCLAW_BACKFILL_PIPELINE_START=20260729`  
- 或检测「pipeline 已成功日」跳过  
- 成功判据不要只看 `rc==0`：以 `run_manifest.trade_date` + `validation_ok` / 业务可接受的 degraded 策略为准  

启动：

```bash
export PYTHONUNBUFFERED=1
export OPENCLAW_BACKFILL_STAGE1_WORKERS=3
export OPENCLAW_BACKFILL_THROTTLE_S=0.4
nohup ~/.openclaw/venv/bin/python /tmp/stock_backfill_driver_v3.py \
  > ~/.openclaw/logs/backfill/driver.out.log 2>&1 &
echo $! > ~/.openclaw/logs/backfill/driver.pid
```

### 6.5 验收清单（全部完成后）

```bash
# 1) latest 交易日
python3 -c "import json;print(json.load(open('$HOME/.openclaw/workspace/stock-report/data/latest/run_manifest.json'))['trade_date'])"
# 期望: 20260807

# 2) 健康
python3 - <<'PY'
import json
from pathlib import Path
h=Path.home()/'.openclaw/workspace/stock_data/03-working/health'
for f in ['orchestrator_run.json','validation_report.json','ai_publish_readiness.json','recommendation_db_sync.json']:
    d=json.loads((h/f).read_text())
    print(f, 'ok=', d.get('ok'), 'trade=', d.get('trade_date') or (d.get('run') or {}).get('trade_date'))
PY

# 3) 缓存末日
ls ~/.openclaw/workspace/stock_data/03-working/backtest_cache/daily_20260807.parquet

# 4) cron 不再钉死 mimo
openclaw cron get a62ed078-9537-4de7-a26d-702aa0bf58a1 | head
```

用户可接受的最低交付（若 AI 短期无法修）：

- [x] 9 日 Tushare 缓存齐  
- [x] 策略 top20 / strategy_backtests 更新到 20260807  
- [x] recommendation DB 同步到 20260807  
- [x] run_manifest.trade_date=20260807  
- [x] AI 状态为 complete  

理想交付另加：

- [x] validation_ok=true, ai_complete=true, publish_ready=true  
- [x] Discord 主频道可恢复正常日报  

---

## 7. 已知变更（Grok 已做，勿回滚除非有理由）

1. **OpenClaw agents** `main` / `secondary` → `xai/grok-4.5`；股票 AI 分析 thinking medium；启用 `plugins.entries.xai`  
2. **xAI OAuth** 写入 main/secondary auth store（来自 `~/.grok/auth.json`）  
3. **选股相关 cron model** → `xai/grok-4.5`  
4. **`stage1_data_prepare.py`** same_day_ready 补丁（支持历史回补）  
5. **回补驱动** `/tmp/stock_backfill_driver_v3.py`（并行拉数 + 串行流水线）  
6. **9 日 Tushare 非降级缓存** 已落盘  

配置备份示例：

- `~/.openclaw/openclaw.json.bak-before-xai-oauth-*`  
- `~/.openclaw/openclaw.json.bak-before-grok45-*`  

---

## 8. 不要做的事

1. **不要** 无 `--trade-date` 在工作日 15:05 前裸跑 `run_pipeline.py`（护栏会拒跑或锁旧日）  
2. **不要** 多日 **全流水线并行**（共享 `stock-report/data/latest/*`、health、易 OOM；历史 cron 已有 SIGKILL）  
3. **不要** 并行拉数超过 3–4 且无节流（Tushare 代理会 0 行/限流）  
4. **不要** 删除 9 日 parquet 后重来（除非校验确实坏了）  
5. **不要** 只看 cron `last_status=ok`；以 `orchestrator_run` + `validation_report` + `run_manifest` 为准  

---

## 9. 一键状态探测（给 Codex）

```bash
echo '=== PAUSED ===' && cat ~/.openclaw/logs/backfill/PAUSED_AT 2>/dev/null
echo '=== DRIVER LOG ===' && tail -30 ~/.openclaw/logs/backfill/driver.out.log
echo '=== MANIFEST ===' && python3 -c "import json;d=json.load(open('$HOME/.openclaw/workspace/stock-report/data/latest/run_manifest.json'));print(d.get('trade_date'),d.get('validation_ok'),d.get('ai_complete'),d.get('publish_ready'))"
echo '=== PROCS ===' && ps aux | grep -E 'stock_pipeline_gate|run_pipeline|backfill_driver' | grep -v grep || echo none
```

---

## 10. 建议的 Codex 工作顺序（历史，现已完成）

1. **修 AI 依赖**（`jiter` / 模型 endpoint），用 1 只股票试 `stage4` 或 bridge  
2. **重跑 20260728** gate，确认 validation/publish 可过（或明确 degraded 策略）  
3. **串行** 20260729→20260807  
4. 验收 `run_manifest.trade_date == 20260807`  
5. 抽查 cron 模型仍为 `xai/grok-4.5`，必要时 `openclaw daemon restart`  
6. 向用户汇报：补齐日列表、AI 是否完整、遗留风险  

---

## 11. 联系上下文

- 用户机器：macOS，`/Users/fisher`  
- OpenClaw 版本：2026.7.1  
- Gateway：`ai.openclaw.gateway`，port `18789`  
- 主频道 Discord：`channel:1468134011154202675`  
- 选股 cron 超时：`timeoutSeconds: 5400`  

---

**交接结论（已更新）**：  
9 个缺失交易日的数据、策略、AI、推荐数据库、验证和发布状态均已补齐到 **20260807**。明日交易日任务已恢复为 OpenClaw 正式链路，股票 AI 分析固定使用 `xai/grok-4.5` 中等推理；无需再次执行历史回补驱动。
