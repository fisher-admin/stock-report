# stock-report 发布源清单（2026-06-20 收口）

> 目的：确保**只有一条 v2 发布链路**能推送 GitHub Pages（`fisher-admin/stock-report` → https://fisher-admin.github.io/stock-report/），
> 杜绝旧 cron/旧脚本/旧克隆用无 `decision_state.gates` 的旧合同覆盖线上 v2 页面。

## 一、唯一允许的发布路径

**`skills/stock-system-orchestrator/scripts/publish_stock_report_v2.sh`**（从 `.openclaw/workspace/stock-report` 克隆推送）

流程：`strategy_publication_layer.py`（生成 v2 合同 latest JSON）→ `generate_latest_states.py` →
`validate_publication_contract.py --strict`（合同硬校验，**失败即拒推**）→ `git pull --rebase` + `git push origin main`。

合同校验任一项失败（缺 `decision_state.gates` / 策略缺 `strategy_gate`/`ai_coverage` / review 空策略 /
T1 出现真实 `ai_score` / 研究观察策略出现 `final_action=main` 等）→ 非零退出、不推送、记录原因。

## 二、所有 stock-report 克隆

| 路径 | remote | 状态 | 处置 |
|---|---|---|---|
| `.openclaw/workspace/stock-report` | fisher-admin/stock-report | **v2 权威克隆**（HEAD=线上） | 唯一发布源；已装 pre-push 守卫 |
| `.openclaw/workspace/stock_data/03-working/stock-report-repo` | fisher-admin/stock-report | working 克隆（旧 cron 曾从此推） | 已装 pre-push 守卫（旧合同会被拒推） |
| `.hermes/stock-system/workspace/stock-report` | fisher-admin/stock-report（**内嵌 GH token**） | 陈旧（ahead 778/behind 9，HEAD 20260518，origin 无其提交→未在推） | 已装 pre-push 守卫；**建议用户轮换该 token** |
| `.newmax/.../openclaw/workspace/stock-report` | fisher-admin/stock-report（**内嵌 GH token**） | 陈旧（origin 无其提交→未在推） | 已装 pre-push 守卫；**建议用户轮换该 token** |
| `.openclaw/stock-report` | fisher-admin/stock-report | 陈旧备份（HEAD 20260406） | 非活跃；未装钩子（非活跃 git 工作区） |
| `Documents/codex/.../work/stock-report` | fisher-admin/stock-report | codex 实验克隆（FisherQuant rebrand 分叉） | 与生产无关，不处理 |
| `Documents/codex/tmp/test-stock/.../stock-report` | 无 remote | 空仓 | 无害 |

## 三、自动任务（openclaw cron，经 Gateway 管理）

`openclaw cron list` 共 ~18 个任务。**唯一推送 stock-report 的是**：
- **`daily-ai-analysis-20260503131048`「AI每日分析+情绪因子」**（`0 20 * * 1-5`）——
  **已改造**：发布步骤从旧 `generate_github_pages.py + 裸 push（working 克隆）` 改为
  `bash publish_stock_report_v2.sh`（v2 合同 + 硬校验 + 仅校验通过才推）。
- 相邻任务 `A股选股+启动前夕深度分析`（19:30，仅选股）、`每日收盘闭环-验证+治理+告警`（20:45）—— **均不推送 stock-report**。
- launchd（`ai.openclaw.gateway` / `freshness-watchdog` 等）—— 不直接推送 stock-report。

## 四、防回退保障（config 无关）

所有 4 个能推 `fisher-admin/stock-report` 的活跃克隆已安装 **`.git/hooks/pre-push` 守卫**
（源：`skills/stock-system-orchestrator/scripts/stock_report_pre_push_hook.sh`）：推送前对该克隆 `data/latest`
跑 `validate_publication_contract.py --strict`，**不合规直接拒推**。即使将来又出现旧脚本/旧 cron，也无法把旧合同推上线。

`stage6_deploy_and_notify.py` 也在 deploy 前跑 `--strict` 校验，双重保险。

## 五、如何确认没有旧任务覆盖

- `openclaw cron list` + `openclaw cron get <id>`：确认无任务再用 `generate_github_pages.py` 推送。
- `git -C <clone> log -1`：线上最新提交应为 v2（`decision_state.gates` 存在）。
- 每日 `publish_guard_state.json`（防回退监控，见 research-lab 系统页）：线上回退到无 gates / 数据落后 / 非授权 clone 推送 → 失败/告警。
- **待用户处理**：轮换 `.hermes` 与 `.newmax` 两克隆内嵌的 `ghp_*` token（即便已装钩子，token 外泄仍是风险）。
