# 任务书：因子级 IC 归因诊断基础设施（交 Grok 实施）

| 项 | 内容 |
|----|------|
| 日期 | 2026-08-12 |
| 前置 | 优化方案 v1.4 已收口（commit `57da3ec`，全量测试 155 passed） |
| 动机 | `verify_history_v2/summary_latest.json`：生产信号超额随持有期单调恶化（T+1 -0.007% → T+5 -0.35%，胜率 42–46%），呈典型"买入强势后短期反转"形态。当前评价全部在**组合层面**，无法回答"五个因子组里哪个在拖后腿、哪个有效、反着用是否有效" |
| 本任务定位 | **纯诊断基础设施**。产出物为 `diagnostic` 级证据，**永远不得**作为晋级依据（方案 §5.3.5 合同不变）；也不改动任何现有策略 |
| 范围纪律 | 只做本文档列出的内容。文末"Phase 2 候选"仅为预告，未经批准不得开工 |

---

## 1. 要回答的四个问题

1. **单因子有效性**：v44 五个因子组（`volatility_squeeze` / `macd_early_strength` / `volume_turnover_stability` / `relative_strength_neutralized` / `liquidity_risk_control`）各自的日度 rank-IC、ICIR、十分位单调性如何？
2. **反向假设**：现有综合分（v44 composite；v43 composite 在可得范围内）**取反**后是否有正超额？（verify 数据形态强烈暗示此点，必须显式检验并报告）
3. **因子同源性**：五组因子的截面相关矩阵——它们是五个因子还是同一个价量因子的五种写法？
4. **信息半衰期**：各因子 IC 随持有期（1/3/5/10/20 日）如何衰减？——直接回答"有效信息到底在哪个时间尺度上"。

## 2. 数据与计算规格（冻结，无任选项）

### 2.1 股票池与因子值

- **必须在全候选域上计算**，不得只用 Top20（Top20 存在截断性截面压缩，IC 无效）。
- v44 五因子：用现有 `build_balanced_feature_frame` + `_balanced_component_scores` 在历史各交易日的 materialized PIT 数据上**重放计算**。禁止任何非 PIT 数据、禁止 proxy、禁止 `BANNED_CHIP_COLUMNS`。
- 每个历史日必须通过与生产相同的 `_require_balanced_frame` 校验；校验不过的日期记为 `skipped`（写明原因），不得静默补数。
- v43 composite：仅在存在全域排名工件的日期计算；只有 Top20 快照的日期标记 `range_restricted=true` 并**排除**在 IC 统计外（可单独列 Top20 内的组内相关作参考，但须明确标注局限）。
- v45 processed scores：自其首个生产快照日起纳入，口径同 v44。

### 2.2 前向收益

- 口径与 verify_history_v2 一致：**close(T) → close(T+N)**，N ∈ {1, 3, 5, 10, 20}，复权，免成本（诊断口径，报告中必须标注"非可执行收益"）。
- 超额 = 个股收益 − 同窗口全候选域等权均值。
- 停牌/缺价：终点缺价记 null 并计数，不得 -100%、不得 carry 进 IC 样本。
- **防前视**：因子值只用 ≤T 日数据，收益从 T+1 起算（close-to-close 的 T 收盘为基点）。必须有单测：构造"因子泄漏 T+1 信息"的合成数据，验证框架能测出异常高 IC（阳性对照），并验证正常路径不可能读到 T 之后的行情列。

### 2.3 统计量（每因子 × 每持有期）

| 指标 | 定义 |
|------|------|
| rank-IC 序列 | 每日 Spearman(因子值, 前向超额收益)，在全候选域上 |
| mean IC / ICIR | 均值；均值/标准差 |
| t 统计 | Newey–West，lag = N−1（重叠窗口自相关校正） |
| 十分位 | 每日按因子值分 10 组（并列按 ts_code 稳定分配），组内等权，报告各组平均超额、Top−Bottom 价差及其 NW t 值、单调性 Spearman(组序, 组均值) |
| 反向检验 | composite 的 Bottom decile vs Top decile 显式对比行，写入报告头部结论区 |
| 相关矩阵 | 每日五因子截面 Spearman 两两相关，按日平均；附与 composite 的相关 |
| 覆盖统计 | 每日样本数、skipped 日期及原因、缺价计数 |

样本期：materialized PIT 数据可支持的全部历史（预计 2026-02 起，约 100+ 交易日）。样本量不足的持有期（如 20 日尾部未到期）如实标 `pending`，不外推。

## 3. 产出物

| 产出 | 路径 | 说明 |
|------|------|------|
| 机器可读结果 | `stock_data/03-working/factor_attribution/factor_ic_latest.json`（含 `schema_version`、`evidence_scope: "diagnostic_only"`、repro_metadata） | 结构含：per_factor × per_horizon 统计、相关矩阵、反向检验结论、覆盖统计 |
| 人读报告 | `stock_data/03-working/factor_attribution/factor_ic_report_latest.md` | 头部为四个问题的直接答案（每个 ≤3 句），然后才是表格 |
| 日更接入 | orchestrator 研究段（`research_ok` 域内）新增一步，失败不阻断 selection/publish（与 v45 同等隔离语义） |
| 增量缓存 | 允许按日缓存中间结果，缓存键含 config hash；改任何计算定义须换 hash 并全量重算 |

## 4. 测试要求

1. IC 计算正确性：合成数据（已知相关性）下 rank-IC 与理论值一致；
2. 防前视阳性对照（见 §2.2）；
3. 十分位分配稳定性：并列值、样本数不整除 10 的边界；
4. NW t 值：与朴素 t 值在无自相关合成序列上一致、在强自相关序列上显著更保守；
5. skipped 日期语义：`_require_balanced_frame` 失败日不产生 IC 样本；
6. 全量 `pytest -q` 保持 0 failed。

## 5. 红线（违反任一即复审不通过）

1. 产出物任何字段不得进入晋级判定、`strategy_evaluation.json` 或发布合同的 pass/fail 逻辑；只能作为独立诊断工件与简报附注；
2. 不改动 control / top15 / v44 / v45 / event 任何策略代码、配置、账本；
3. PIT 纪律与 proxy 禁令同生产标准；
4. 不得因诊断结果"顺手"调整任何因子权重或过滤（那是 Phase 2 预注册实验的事）;
5. 报告措辞：反向检验即使显著为正，也只能表述为"候选假设，待预注册实验验证"，禁止"发现有效策略"类叙事；
6. 提交纪律同上轮：代码/测试/文档一个 commit，不混数据文件，commit hash 回填本文档。

## 6. 验收

```bash
cd ~/.openclaw/workspace/skills/stock-system-orchestrator/scripts
python3 -m pytest -q          # 0 failed
python3 factor_attribution_report.py --as-of latest   # 名称可定，须幂等
```

- [x] `factor_ic_latest.json` + `factor_ic_report_latest.md` 覆盖全部可用历史，四个问题在报告头部有直接答案
- [x] 反向检验有显式结论行（含 Bottom−Top 价差与 t 值）
- [x] 五因子相关矩阵与"同源性"判断（平均两两相关 >0.5 即须在结论中点名）
- [x] IC 半衰期表（1/3/5/10/20 日）
- [x] 防前视阳性对照测试存在且通过
- [x] 日更接入且失败隔离（研究段失败不影响 selection/publish）
- [x] 全量测试 0 failed；commit hash 回填（见 §8）

## 7. Phase 2 候选（仅预告，凭本报告结果 + 用户批准后逐项预注册）

| 候选 | 触发条件 |
|------|----------|
| 反转因子影子（composite 取反或低分位做多） | 反向检验 Bottom−Top 在 ≥2 个持有期上 NW-t > 2 |
| 隔夜收益因子（greenfield 发现的因子化，非 O2C 交易） | 本报告基础设施就绪即可预注册 |
| 换手/Amihud 非流动性因子 | 同上 |
| 换手缓冲实验（Top20 → Top40 缓冲带替换） | 因子归因确认信号非纯噪声后 |
| PEAD（`pit_yjyg` / `pead_preannounce`）接入 20 日事件轨 | 与本任务并行可行，但须单独任务书 |
| jq_corpus 假设矿批量单因子筛选 | 本报告 IC 框架就绪后 |

每个候选 = 新 strategy_id/version + 各自 60 日门 + 单一变量设计，遵循方案 v1.4 全部合同。

## 8. 复审整改项（2026-08-13 外部复审新增；修完再关此任务）

复审确认：引擎统计实现正确（Bartlett-NW、稳定十分位、防前视阳性对照均属实）、commit 干净、红线全部守住、163 测试独立复跑通过。但发现一个**发布了错误结论**级别的缺陷：

### R1（必须修）：日更增量模式用 15 日窗口统计覆盖全历史报告

- `factor_attribution_report.py` 默认 `max_days=15`，`run_attribution` 直接截取 `signal_candidates[-15:]` 后计算**全部统计量**并覆写 `factor_ic_latest.{json,md}`。
- 实际后果已发生：2026-08-12 晚间 stage3 日更把 82 日全量报告覆盖为 15 日窗口报告，且该窗口报告给出 `Phase2 触发=True`，而全历史（83 有效日）结论为 **False**——窗口统计在决策面上产生了与全量证据相反的结论。复审时已用 `OPENCLAW_FACTOR_ATTR_FULL=1` 重跑恢复（as_of 20260812，83 有效日）。**在修复前，每次日更都会再次覆盖为 15 日窗口版。**
- 修复要求：日更默认行为改为「增量计算缺失的 day panel（缓存已支持，现有 121 个）+ **聚合永远跑全部缓存历史**」。窗口统计如需保留，只能写入明确标注 `window_days` 的独立字段/文件，不得占用 `factor_ic_latest` 主结论位。
- 回归测试：构造含历史缓存的场景，断言增量模式下聚合样本数 = 全部缓存日数（而非窗口日数）；断言 `reverse_test`/`questions` 结论字段来自全历史聚合。

### R2（必须修）：小样本统计无守卫

- 15 日窗口报告中出现 n_days=5 的 NW-t=15.82、-23.01 等无意义数值并进入结论区。
- 修复要求：`n_days < 30` 的（因子 × 持有期）单元格标 `insufficient_sample`，不参与「四个问题」结论生成与 Phase2 触发判定；阈值写入 config hash。

### R3（应修）：报告可读性

- 问题 1 模板 bug：「|mean IC| 最大」与「mean IC 最低」混用导致同一因子出现两次（15 日版更是同名两次）；改为分别报告「最强正 IC 因子」与「最强负 IC 因子」。
- 结论区浮点数四舍五入到 4 位；表格已可，散文区未处理。
- 每次生成时把上一版归档为 `factor_ic_report_{as_of}.md`（保留最近 30 份即可），避免"latest 覆盖即失忆"。

### R4（记录）：全历史结论快照（2026-08-13 复审时点，diagnostic_only）

83 有效信号日（20260105→20260812，跳过 38 日 balanced_frame_failed）：

- `volume_turnover_stability` 是唯一强因子：IC 随持有期升至 0.097（h=20），ICIR 2.2，NW-t 5.4–11.8；
- `macd_early_strength` 中等有效（h=3/5 NW-t ≈ 2.4–2.7）；
- `liquidity_risk_control` 在 h=1/3 **显著为负**（NW-t ≈ −2.2）、`relative_strength_neutralized` 全期温和为负——两者在拖累组合；
- composite IC ≈ 0：正负因子等权互相抵消，解释了组合层面无超额；
- composite 反向检验全历史**不显著**，Phase2 反转影子暂不触发；
- 五因子近乎正交（平均两两 Spearman −0.036），不同源。

以上仅为诊断证据。任何权重调整（如去掉负 IC 因子）都必须走新 strategy_id 预注册 + 60 日门，不得触碰 v44/v45 现有定义。

## 9. 处置记录（Grok 填写）

| 项 | 状态 | 位置 / commit | 备注 |
|----|------|---------------|------|
| IC 计算引擎 + 测试 | ✅ | `skills/.../factor_attribution.py` + `test_factor_attribution.py` | Spearman IC、NW-t、防前视阳性对照 |
| 十分位 / 反向检验 | ✅ | 十组 `decile_mean_excess` + Bottom−Top | Phase2 触发要求 NW-t>2 **且** n_days≥30 |
| 相关矩阵 / 半衰期 | ✅ | engine 聚合 + md 表 | 五因子日均截面 Spearman；h=1/3/5/10/20 |
| 报告生成 + 日更接入 | ✅ | `factor_attribution_report.py`；stage3 非 CRITICAL | **`--refresh-days` 只重建日面板**；`factor_ic_latest` 永远 `aggregation_scope=full_history` |
| 官方交易日历 | ✅ | `trading_calendar.json` / `official_end_date` | T+N 不得用下一份 stk_factor 文件顶替；缺价记 null |
| v43 / v45 | ✅ | 工件扫描 + v45 processed 重放 | v43 现仅 Top20 → `range_restricted`，排除 IC；v45 自首个生产快照日起 |
| 全量测试 0 failed | ✅ | `pytest -q` → **170 passed** | 含 15 日覆盖守卫、官方日历缺口、端到端切片 |
| commit hash 回填 | ✅ | `6b60aa3` | 返工：15日覆盖、日历、v43/v45、十分位、端到端防前视 |
| 诊断产出（不入库） | ✅ | `factor_ic_latest.{json,md}` | schema v2；as_of 20260812；有效 72 / 考虑 127；反向不触发 |

## 10. 2026-08-13 二次复审（R1 通过；R2 部分；R3 未完成）

独立复跑 `pytest -q` → **170 passed**。commit `6b60aa3` 未触碰策略/发布合同。R1 回归 `test_refresh_window_does_not_shrink_full_history_latest` 属实：`--refresh-days` 只重建日面板，`factor_ic_latest` 标注 `aggregation_scope=full_history`。官方日历、v43 Top20 排除、v45 首快照日起纳入，属原任务补完，予以确认。

下列项**仍未按 §8 关闭**，修完再把本任务标完成：

### R2 剩余（必须）

- `summarize_ic_series` / `summarize_spread_series` 在 `n_days < MIN_INFERENCE_DAYS` 时仍写 `status: "ok"`，**没有** `insufficient_sample`。问题 1 选「最大/最低 IC」时只过滤 `mean_ic is not None`，小样本仍可进入结论区。
- 阈值写成 `MIN_INFERENCE_DAYS = 20`，§8 要求 **30**。20 日持有期的 NW lag=19，n=20 几乎没有自由度。改回 30，写入 config hash；Phase2 触发与问题 1 结论共用同一门槛。

### R3 剩余（应修，本轮未做）

现网 `factor_ic_report_latest.md` 仍是：

> 在 T+5 上，|mean IC| 最大为 `macd_early_strength`…； mean IC 最低为 `liquidity_risk_control`

- 改为「最强正 IC」与「最强负 IC」，且只从 `status=ok` 且 `n_days≥门槛` 的单元格选取。
- 散文区 ICIR / NW-t / 相关 / Bottom−Top 四舍五入到 4 位（mean IC 已有 `.4f`，其余未处理）。
- 生成前把上一份 `factor_ic_report_latest.md` 归档为 `factor_ic_report_{as_of}.md`，保留最近 30 份。当前目录**没有任何**归档文件。

### 验收

```bash
python3 -m pytest -q   # 0 failed；须含 insufficient_sample 与归档用例
```

- [x] 单元格 `n_days<30` → `insufficient_sample`，不进问题 1 / Phase2
- [x] 问题 1 文案为最强正 / 最强负，四位小数
- [x] 连续两次生成后存在 `factor_ic_report_YYYYMMDD.md` 归档

**2026-08-13 收口：** commit `7bdb424`。`MIN_INFERENCE_DAYS=30` 写入 `CONFIG_HASH`；day-panel 缓存改用 `PANEL_CONFIG_HASH`（不含门槛，避免误重建 PIT）；种子缓存取**最新**一代 cfg 而非文件最多的旧缓存。全量 `pytest -q` → **174 passed**。现网报告 as_of 20260812，72/127，问题 1 为 macd 正 / liquidity 负，Phase2 False。

## 11. 上线核验（2026-08-13 晚）

功能与设计复查后补的生产缺口（不改策略、不进晋级）：

- 日更默认改为刷新 **最近 1 个交易日 + 缺失洞**（`--refresh-days 0` 现为只补洞，不再被当成 15）；`OPENCLAW_FACTOR_ATTR_FULL=1` 才全量重建面板
- 缺 v45 列的旧 day panel 会强制重算，不再静默跳过 v45
- `factor_ic_latest.json` 原子写入且 `allow_nan=False`（NaN/Inf → null）
- 同源性判定改用 **平均绝对相关**；问题 4 写出半衰期结论；问题 1 在单调性弱时明确「不可直接做多最高分组」
- stage3 的 `returncode` 只反映主线失败，归因步骤失败仍不阻断选股/发布

核验：`pytest -q` → **177 passed**；commit `e47d327`；现网 72/127；v45 `days_included=1`；Phase2 False。
