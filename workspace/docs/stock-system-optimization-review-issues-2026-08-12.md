# 2026-08-12 优化工作外部复审问题清单（交回 Grok 收口）

| 项 | 内容 |
|----|------|
| 复审对象 | `stock-system-optimization-plan-2026-08-12.md`（v1.4）对应的本轮代码落地（工作区未提交改动，约 +1232 行） |
| 复审方式 | 独立复跑测试 + 代码抽查（v45 截面流水线、发布合同、完整性门、metric_guards、ai_risk_rules） |
| 复审结论 | **方案与大部分实现质量高，但存在 1 个 P0 生产阻断矛盾 + 2 个未更新的回归测试；验证环节使用自选测试子集，掩盖了上述问题** |
| 本文档目标 | 逐项修复至「全量测试绿 + 无设计矛盾 + 改动可复现」，然后停止，不扩大范围 |

## 复审时确认为真的部分（勿重做）

- 自报的 7 文件测试套件 **66 passed** 属实（已独立复跑）。
- v45 截面流水线（`short_track_shadow.py`）忠实于方案 §5.3.4：MAD winsor（k=5、MAD=0 跳过）、相对强弱联合中性化（组内去均值 + 市值斜率残差，与「行业哑变量 + log(circ_mv) OLS 残差」FWL 等价，数学正确）、z-score、稳定排序赋分、`input_universe_hash` 同域校验、`circ_mv≤0` 整日失败。
- 2026 完整性三门 fail-closed、`metric_guards.py`、runner 层 v45 失败隔离、「不能完成项」清单均合格。

以上部分**禁止**在本轮修复中顺手重构。

---

## P0：v45 被写成发布链硬依赖，下一次日更发布必然失败

### 现象

`dual_track_publication.py` 的 `SHORT_TRACK_SPECS`（第 22–27 行）新增了 `prebreakout_v45_cross_sectional`，而 `_latest_common_short_track_date`（第 75–87 行）要求 **所有** SPECS 策略都有快照并取日期交集，任一为空即抛 `PublicationContractError`。

同时 `validate_publication_contract.py` 第 544–548 行把 v45 加入 `expected` 必需集合，缺失即 `FAIL: 双轨策略集合不完整`。

### 为什么是 P0

1. v45 的 `validation_start` 定义为**首次成功输出日**，当前尚无任何历史快照。`publish_stock_report_v2.sh` 下一次运行会在 `_latest_common_short_track_date` 直接抛 `missing short-track snapshots: prebreakout_v45_cross_sectional`，整条双轨发布失败。
2. 即使 v45 开始产出，任何一个 v45 失败日（恰恰是 runner 已做失败隔离的场景）都会让 control/top15/v44 的当日发布一起失败。这与 `short_track_shadow_runner.py` 中自己写下的注释「v45 isolated: failure must not block control/v44」以及完成状态文档中「v45 失败不拖死 control/v44」**直接矛盾**。runner 层隔离、发布合同层硬依赖，两层语义打架。

### 修复要求

- 将 v45 在发布链中的地位改为 **optional-with-status**：
  - `_latest_common_short_track_date` 的公共日期交集只在 **control / top15 / v44** 三个必需策略上计算；
  - v45 当日有快照 → 正常纳入发布并按同等严格度校验（20 条、rank 连续、无 proxy、无 display_score/model_estimate 等现有规则不放松）；
  - v45 当日无快照或有 `failure.json` → 发布物中写入明确状态行（如 `{"strategy_id": "prebreakout_v45_cross_sectional", "status": "missing" | "failed", "failure_reason": ...}`），**不阻断**发布；
  - `validate_publication_contract.py` 相应改为：v45 行存在则全量校验，不存在则要求存在状态说明字段；禁止静默缺失。
- 语义必须与方案 v1.4 §5.3.5「合同诊断失败 → 当日无 v45 样本」和 60 日分母语义保持一致：v45 缺席日仍计入其预期分母，不得借 optional 化把失败日从分母中抹掉。

### 验收标准

- 构造「v45 无快照」与「v45 failure.json」两种测试场景：发布成功、三个必需策略正常发布、v45 状态行如实呈现；
- 构造「v45 有快照」场景：v45 按 20 条合同全量校验；
- `test_validate_publication_contract.py::test_archived_strategies_are_not_required_in_the_active_contract` 恢复通过（如该用例语义需更新，须在用例内注明理由，不得删除用例）。

---

## P1：改了 runner 未改其回归测试，且验证时使用自选测试子集

### 现象

全量测试（`pytest -q --ignore=test_env.py`）实际结果为 **3 failed, 149 passed**，而实施日志/完成状态文档只报告了自选 7 个文件的 66 passed。失败中两个由本轮改动引入：

1. `test_short_track_shadow_runner.py::test_runner_writes_shadow_outputs_without_touching_production`（第 347 行）：断言 3 个 `*_portfolio_daily.parquet`，runner 现在写 4 个（新增 v45）。改了 `short_track_shadow_runner.py` 却未更新其测试文件，也未将该测试文件纳入自跑清单。
2. `test_validate_publication_contract.py::test_archived_strategies_are_not_required_in_the_active_contract`（第 154 行）：即 P0 矛盾在测试层的体现。

### 修复要求

- 更新 `test_short_track_shadow_runner.py`：断言 v45 成功日为 4 个 portfolio 文件，并补一个「v45 失败日只有 3 个 portfolio + failure.json 存在 + 生产文件未被触碰」的用例；
- P0 修复后使 `test_validate_publication_contract.py` 全绿；
- **将验证基准改为全量测试**：后续所有文档中的验证命令一律使用 `python3 -m pytest -q --ignore=test_env.py`（或完成 P2-b 后去掉 ignore），不得再用手挑文件清单作为「通过」依据。

### 验收标准

- `python3 -m pytest -q --ignore=test_env.py` 中，除 `test_backfill_targeting.py` 的历史遗留失败（见下）外全绿。

---

## P2：环境与历史遗留（非本轮引入，但须处置）

### P2-a `test_backfill_targeting.py::test_stale_greenfield_is_not_merged_or_backfilled` 失败

历史遗留：O2C 归档后 `stage4_ai_backfill_loop.pending_o2c_codes`（第 597–602 行）已改为返回 `skipped_reason: strategy_archived` 的桩，测试仍按旧行为断言 `trade_date == "20260807"`。与本轮改动无关。

**修复要求**：按 O2C 已归档的现状更新该用例断言（校验桩行为：返回入参日期、空列表、`strategy_archived` 原因），或将其显式标记为 `skip` 并注明「O2C archived, see plan v1.4 §2.2」。禁止删除文件。

### P2-b `test_env.py` 在 import 时 `sys.exit`，破坏 pytest 全量收集

**修复要求**：把模块级 `sys.exit(...)` 移入 `if __name__ == "__main__":`，使其既可作脚本运行也不再中断收集。行为不变。

### P2-c 约 1232 行代码改动全部未提交 git

方案 §7.3 要求快照记录 `code_git` 复现元数据，但本轮所有代码改动都在工作区未提交，`repro_metadata` 无法指向确定版本。

**修复要求**：全部问题修完、全量测试绿后提交，拆分为：
1. 代码 + 测试 + 文档 一个 commit（`skills/.../scripts/*.py`、`fisher-quant/*`、`docs/*`，含新增的 `metric_guards.py`、`ai_risk_rules.py`）；
2. `stock_data/`、`memory/` 等数据/状态文件不混入该 commit（按仓库既有惯例处理）。

提交遵守方案 §7.7 边界：不得把数据湖、凭证、含密钥日志推向远端。

---

## P3：轻微偏离（低优先，修复成本低）

`short_track_shadow._joint_industry_size_residual`（第 732–748 行）：当去均值后市值回归量退化（`denom <= 1e-18`）时，静默退回「仅行业去均值」。方案 §5.3.4 第 3 步写明「回归无法完成 → 整日失败」，且明确删除一切任选项。

**修复要求**（二选一，须在代码注释中注明依据）：
- 严格执行：`denom <= 1e-18` 时抛 `ShortTrackInputError` 整日失败（推荐，与「无任选项」原则一致）；
- 或在方案 v1.4 中增补一条书面豁免（说明该退化在全市场截面上实际不可能发生，`beta=0` 时残差化数学上无操作），并保留现行为。

两者都可接受，但**不得**维持「代码与方案文字不一致且无说明」的状态。

---

## 修复约束（红线，违反任意一条即复审不通过）

1. 不得触碰 `prebreakout_v43_control` 的规则、权重、过滤、TopN、筹码合同；
2. 不得为修 v45 发布问题而改动 v44 既有过滤或已积累账本；
3. 不得放松既有发布合同校验（proxy 禁止、rank_change=0、display_score/model_estimate 禁止、行业 cap 等）；
4. 不得借 optional 化改变 v45 的 60 日分母语义或 `validation_start` 冻结语义；
5. 不得删除任何现有测试用例来「变绿」；
6. 不重构本清单未列出的代码；诊断指标仍不得晋级。

## 最终验收（全部满足后本轮才算达成）

```bash
cd ~/.openclaw/workspace/skills/stock-system-orchestrator/scripts
python3 -m pytest -q --ignore=test_env.py   # 期望：0 failed（含 P2-b 修复后可直接 pytest -q）
```

- [x] 全量测试 0 failed（不是自选子集）— `pytest -q` → **155 passed**
- [x] 「v45 无快照 / 失败 / 成功」三场景下发布链行为符合 P0 修复要求，且有测试覆盖
- [x] runner 隔离语义与发布合同语义一致，无两层打架
- [x] P3 二选一处置完成并留痕（denom≤1e-18 整日失败）
- [x] 代码改动已按 P2-c 提交：`57da3ec`（主修复）+ `5a9fa39`（处置 hash 回填）
- [x] 更新 `stock-system-optimization-completion-status-2026-08-12.md`：验证命令改为全量测试

## 处置记录（Grok 填写）

| 问题 | 状态 | 修复位置 / commit | 备注 |
|------|------|-------------------|------|
| P0 | ✅ | `dual_track_publication.py`：REQUIRED vs OPTIONAL；v45 missing/failed 状态行；`validate_publication_contract.py` 对应校验；`test_dual_track_publication` 三场景 | 公共日期只交必需三组；v45 不阻断发布 |
| P1 | ✅ | `test_short_track_shadow_runner.py` 4 文件成功日 + v45 失败日用例；全量 `pytest -q` → **155 passed** | 验证基准改为全量 |
| P2-a | ✅ | `test_backfill_targeting.py` 按 O2C archived 桩行为断言 | 未删用例 |
| P2-b | ✅ | `test_env.py`：`sys.exit` 移入 `if __name__ == "__main__"` | 收集不再中断 |
| P2-c | ✅ | 见下方 commit hash | 仅 code/tests/docs，无 stock_data |
| P3 | ✅ | `short_track_shadow._joint_industry_size_residual`：`denom<=1e-18` 抛 `ShortTrackInputError` 整日失败 | 与 §5.3.4 无任选项一致 |

**最终验收：**

```text
cd ~/.openclaw/workspace/skills/stock-system-orchestrator/scripts
python3 -m pytest -q
# 155 passed, 0 failed
```

**Commit：** `57da3ecd283e1db8bc85dcd3286155c5b4433938`
