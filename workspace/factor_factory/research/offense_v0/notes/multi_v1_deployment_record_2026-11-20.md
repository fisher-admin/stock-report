# forward_shadow_multi_v1 部署记录（2026-11-20，opus-5-5；fable-5-2 额度不可用）

**合同不动**：`factor_factory/contracts/forward_shadow_multi_v1.json`，spec_hash `87929c95aeb9820c`（11-17 重签版）。代码、合同与本地 commit 3c24f9c 一致，原样同步到 VPS（这些文件此前在 VPS 上都不存在）。

## 零付费条件下各账本的真实口径（必读）

| 账本 | 判定 | 本次部署的口径 |
|---|---|---|
| `ew6_retain_15_30` | judged | 与 forward_shadow_v1 同源。11-19 起 daily_basic 来自自愈层（来源 `selfheal_*`），评估按 11-19 前后分段报告 |
| `rebal_15` | judged | 同上 |
| `aegis_micro5p_10_30` | judged | **Micro5P 无 cgo 4 支柱口径（Micro5P-4P）**。`cyq_perf` 自 11-17 后无来源（Tushare 筹码数据，零付费下永久缺失）。按合同 `cyq_perf ffill(limit=5)`，前 5 个交易日用陈旧 cgo，之后 cgo 支柱缺失，`min_valid=4` 用其余 4 个支柱（neg_roc_60, rpo_clip_60, vpd_20, ivol_60）。**它不是回测验证过的 5 支柱 Micro5P，结论只代表 Micro5P-4P，不得冒充 5 支柱版**。降级天数和降级模式记录在每条决策的 `scorer.degraded_mode` 里 |
| `aegis_core7p_10_30` | judged，但**永久非正式** | qarp/acc 支柱依赖 x_jump_ms_v1 的 Tushare VIP 公告表，`ann_fetch.ok` 永远达不到。每条决策会写一行 `gaps.jsonl`（core7p_ann_stale），这是合同预期，不是故障。它的零付费继任者在 forward_shadow_v2（B1）里用东财字段重建 |
| `aegis_core7p_top30_h90_cap4` | 描述性 | 同上，永久非正式 |

- 价格：`stk_factor` 已无来源，按合同用 `daily_<d>`（tdx-api）补齐全部代码；K 线闸门照常执行。
- `primary_1`（Core7P − EW6 续持）在零付费下无法形成正式样本。`primary_2`（续持 vs 再平衡）和 `secondary`（Micro5P-4P vs EW6 续持）照常累积。

## 部署动作（VPS）

- 文件：`forward_shadow_multi.py`、`aegis_scorer.py`、`kline_quality_gate.py`、合同 json、`tests/test_forward_shadow_multi.py`。`factor_factory/.gitignore` 补上 `data/forward_shadow/*/test/`，原文件备份在 `archive/zero-paid-a1-20261120-backup/`。
- 包装脚本：`ops/hermes/stock/forward_shadow_multi.sh`。合同没有变；只改了消息分支：除 Core7P 外全部正式时，发"📗 已正式入账（Core7P 零付费下按合同非正式）"，避免每晚误报。另加 3 个模式入口 `forward_shadow_multi_{evening,premarket,cutoff}.sh`，因为 Hermes cron 不支持给脚本传参。
- Hermes 任务（工作日）：
  - 23:50 晚间 `7fb4020cb16a`
  - 07:40 盘前 `3bc38329a2c7`
  - 08:55 盘前 `59b3fde220bc`
  - 09:35 开盘兜底 `cb95e7403300`
- 上线前在 VPS 用 `--test --date 20261119` 跑了一次，只写 `multi_v1/test/`：
  - K 线闸门 ok，覆盖 1.0；
  - Micro5P/Core7P 覆盖 0.9991（2249/2251）；
  - 降级模式 `cyq_ffill_2d`；
  - `official=false` 符合 test 模式。
- Hermes 任务另加 09:10 盘前兜底 `9748a2ff6e0a`（11-20 追加，见下节）。
- 首个正式信号日：2026-11-20，入场截止 11-23 09:30；按下节时序，实际在 11-23 08:55 落盘。`#/today` 的 burn-in 需要 15 个有效正式交易日。

## 时序更正（2026-11-20 17:20，fisher 指出）

合同规定："x_jump_ms_v1 公告抓取未成功时，在截止前 60 分钟（`ANN_WAIT`）之前不写决策。"零付费下 x_jump_ms_v1 不运行，`ann_fetch_ok` 恒为 false，所以每个信号日：
- 23:50 晚间：`waiting_for_x_jump_ms_ann_fetch`，不落盘（静默）；
- 07:40 盘前：距 09:30 截止还有 110 分钟，超过 60 分钟，同样等待；
- **08:55 盘前：首次落盘（正式）**；
- 09:10 盘前兜底（新增）：08:55 若因偶发错误没写成，在截止前再试一次；已决策时直接返回 `already_decided`；
- 09:35 开盘兜底：过了截止，只能记缺口或写非正式的连续记录。

裁定：**合同不动**。等待条件属于冻结合同 multi_v1 的 `official` 条款，收窄它就得重签合同；而现状在 09:30 前仍有 08:55 和 09:10 两次落盘机会，正式性不受影响。代价是晚间那一轮缓冲用不上；如果 08:55 和 09:10 都失败，09:35 会按合同记缺口，不回补。
