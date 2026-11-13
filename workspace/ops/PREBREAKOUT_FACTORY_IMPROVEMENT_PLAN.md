# 启动前夕加工厂 · 系统优化提升方案

**依据**：Codex 会话「审查工厂 runbook」（2026-07-16）对运行手册、脚本与当日产物的只读审查  
**范围**：影子研究层（日淘 / 工厂 / 观察页 / 运维体检）  
**原则**：生产 PREBREAKOUT 继续冻结；先纠正「假确定 + 假健康」，再谈收益提升  
**状态**：P0 + 部分 P1 已落地（2026-07-16）；P2 待续

---

## 0. 总判断（与 Codex 对齐）

| 维度 | 结论 |
|------|------|
| 方向 | **正确**：影子隔离、非阻塞、允许弱市空仓、禁止自动下单 |
| 可信度 | **不足**：冠军与回测数字不可直接当决策依据 |
| 运维 | **偏乐观**：`ok=true` / cron Status=ok / `production_frozen` 易假绿 |
| 用途（整改前） | **仅研究展示 + 纸面观察**，不晋级生产、不下单 |
| 目标（整改后） | 可审计的研究工厂：搜索=执行、真留出、真对照、真完成态 |

**一句话**：不是推倒重来，而是把「看起来很强的研究玩具」收成「可证伪、可运维的研究系统」。

---

## 1. Codex 八大问题 → 系统对策总表

| # | 严重度 | Codex 问题 | 根因（代码侧） | 对策（本方案） | 优先级 |
|---|--------|------------|----------------|----------------|--------|
| 1 | 严重 | 留出参与选优，无真 OOS | 日淘 `make_objective` 用 train+hold；`score_row` 用 train/test/hold 综合挑 Top | 真 holdout 禁搜 + 纸面晋级门槛 | **P0** |
| 2 | 严重 | 搜索 ≠ 出厂执行 | v2 `apply_mult` 后 confirm；工厂 `_confirm_hits` 用原始 sub_scores | 统一 `evaluate_recipe()` 单入口 + 一致性断言 | **P0** |
| 3 | 严重 | 「战胜生产」不成立 | 对照=生产权重+实验门控；真实 top20 可空仍显示 edge | 真生产名单对照；缺对照标 `control_unavailable` | **P0** |
| 4 | 严重 | 历史日期跑错 | `resolve_trade_date` 不读管线 env；ops 只把日期传工厂 | 全链 `TARGET_TRADE_DATE` + 日期一致性闸门 | **P0** |
| 5 | 高 | cron 假绿 / 无互斥 | 手册看 Status=ok；19:30/20:00/20:45 无锁 | run_id + 完成指纹 + flock | **P1** |
| 6 | 高 | 安全标志表面化 | `production_frozen` 写死；先 ok 后写 watch；失败不改旧 ok | 配置指纹、原子写、末态提交 | **P1** |
| 7 | 中 | 手册体检误判 | 自然日、字段名错、无 exit code | 正式 `check_prebreakout_factory.py` | **P1** |
| 8 | 中 | 回测过乐观 | 固定 0.26%、无涨停/停牌/容量；mom 代理≠沪深300 | 成交约束 + 指标诚实命名 | **P2** |

---

## 2. 目标架构（整改后）

```text
                    ┌─────────────────────────────────────┐
                    │  TARGET_TRADE_DATE + RUN_ID（唯一）   │
                    └─────────────────┬───────────────────┘
                                      │
     ┌────────────────────────────────┼────────────────────────────────┐
     ▼                                ▼                                ▼
 日淘搜索                         工厂出厂                          观察/发布
 (仅 train/test 窗)              evaluate_recipe()                 atomic write
  holdout 只评分不选参  ────────►  同一函数  ◄────────  一致性校验
     │                                │                                │
     ▼                                ▼                                ▼
 search_report                    factory_latest                   watch.json
 + status(ok 仅当产物齐)          + config_fingerprint             + contract check
                                      │
                                      ▼
                            真生产名单对照（可 unavailable）
                                      │
                                      ▼
                            纸面 ledger（T+1 实结算，晋级门槛）
```

**核心契约**

1. **One date**：日淘 / 工厂 / watch 的 `trade_date` 必须一致，否则拒绝落盘。  
2. **One evaluator**：搜索、回测、当日选股同一函数。  
3. **One completion**：全部产物原子写成功后才 `ok=true`；失败写 `ok=false` 并覆盖旧绿。  
4. **Honest labels**：禁止在缺真对照时写「战胜生产」；holdout 未过不得称 champion 可晋级。

---

## 3. 分阶段实施计划

### Phase A — P0 可信度底座（建议 1–2 个工作日）

#### A1. 统一计算入口 `evaluate_recipe`

**改动文件**

- 新建：`scripts/prebreakout_recipe_engine.py`（或抽到 `prebreakout_factory.py` 可复用模块）
- 修改：`prebreakout_factory_search_v2.py`、`prebreakout_factory_search_daily.py`、`prebreakout_factory.py`

**行为**

```text
evaluate_recipe(
  panel_or_scored_rows,
  weights, multipliers, gates,
  *,
  confirm_on: "raw" | "multiplied"   # 默认 raw，与出厂一致；搜索必须跟出厂相同
) -> {picks, meta, daily_stats...}
```

**硬约束**

- 搜索路径 **禁止** 再走「乘子后 confirm、出厂 raw confirm」的分叉。  
- 增加单测 / 冒烟：同一 yaml + 同一日，`search.evaluate` 与 `factory.apply_experiment_selection` 的 `picks` 代码集合与 `avg_daily_pct`（同窗）**完全一致**（浮点容差 ≤ 1e-9 或四舍五入到 bp 一致）。

**验收**

```bash
python -m pytest ... -k recipe_parity   # 或独立 smoke
# 对当日 champion：search_report 全窗 edge == factory_latest edge
```

---

#### A2. 真 Holdout（禁止偷看）

**改动**：`prebreakout_factory_search_daily.py`（及 v2/v3 若日路径复用）

| 窗口 | 用途 |
|------|------|
| train | DE / 优化目标 **唯一** 用它（或 train 内交叉） |
| test | 报告用，**不进** 最终排序主分（或仅作 tie-break 且权重极低并标注） |
| **holdout 末 20–30 日** | **完全不参与** 参数搜索与门控网格 |

**选优规则（建议）**

1. 在 train 上搜出候选池 K（如 30）。  
2. 在 **未参与搜索的 holdout** 上评分，按 holdout 指标选 Top3。  
3. 报告必须分列：`train_edge` / `holdout_edge` / `full_window_edge`（full 仅展示，禁止当选优分）。  
4. 若 `holdout` 交易日 < 15：降级为 `status=degraded`，不标「真金」。

**晋级（纸面）规则（写入工厂，不自动改生产）**

- 连续 N 个交易日（建议 10–15）纸面 ledger 结算后，`holdout` 与纸面同向且空仓日占比可接受 → 才允许标记 `promotion_candidate=true`。  
- 观察页冠军默认仍是「可交易观察位」，**≠ 晋级候选**，两者字段分离。

---

#### A3. 真实生产对照

**改动**：`prebreakout_factory.py` + 观察页字段

| 现态 | 目标态 |
|------|--------|
| 生产权重 + **各实验自己的门控** 当对照 | 固定读 `selection_history/{trade_date}.json`（或发布合同中的真实 prebreakout top） |
| 缺失仍算 edge、ok=true | `production_control.status = available \| unavailable` |
| UI「战胜生产」 | 仅 `available` 时显示；否则 **「对照不可用」**，禁止正负 edge 文案 |

**对照结算**：与影子同一成本与成交假设（后续 P2 再加强）。  
**基线实验**：保留 `exp_baseline_gates` 作为「生产权重 + 统一研究门控」的 **研究基线**，命名改为 `baseline_prod_weights`，**不得**再叫 production_control。

---

#### A4. 目标交易日全链贯通

**改动**

- `resolve_trade_date()`：读取（优先级从高到低）  
  1. CLI `--trade-date`  
  2. env `TARGET_TRADE_DATE` / `OPENCLAW_TRADE_DATE` / 管线已有变量  
  3. 缓存最新交易日（仅默认）
- `run_prebreakout_shadow.sh`：  
  - 解析 `"$@"` / env 得到 `TD`  
  - 日淘与工厂都传 `--trade-date "$TD"`  
- `run_pipeline` stage 调用：显式注入与 coreSelection 相同的 trade_date  
- **闸门**：日淘 status / 工厂 status / watch 三者 `trade_date` 不一致 → 全部 `ok=false`，不覆盖 watch（或写 `watch.stale=true`）

**验收**：`TARGET_TRADE_DATE=20260715` 强制补跑 → 所有产物 `trade_date=20260715`，不得落到 20260716。

---

### Phase B — P1 运维真实性（建议 1 个工作日）

#### B1. 运行身份与完成态

每个 stage 写入：

```json
{
  "ok": false,
  "run_id": "uuid",
  "stage": "prebreakoutDailyPan|prebreakoutFactory",
  "started_at": "...",
  "finished_at": "...",
  "trade_date": "YYYYMMDD",
  "duration_sec": 0,
  "pid": 0,
  "git_head": "optional",
  "inputs_fingerprint": "...",
  "outputs": ["path1", "path2"],
  "error": null
}
```

- 开始时：`ok=null` 或 `running`  
- 结束成功：产物校验通过后才 `ok=true`  
- 结束失败 / 异常退出：`ok=false`（用 `try/finally` 或 atexit 钩子），**覆盖**旧绿  
- 手册 / 检查器：**禁止**只信 `openclaw cron Status=ok`；以 `finished_at` + 产物 `trade_date` + 可选 `run_manifest` 为准

#### B2. 单实例锁

```bash
flock -n /tmp/prebreakout_factory.lock -c '...'
```

覆盖：`prebreakout_factory_search_daily.py`、`prebreakout_factory.py`、`run_prebreakout_shadow.sh`。  
拿不到锁：写 status `ok=false, error=lock_busy`，exit 0（非阻塞）或 75，避免双写 watch / git 脏。

#### B3. 原子写入 + 配置指纹

- 所有 JSON：`*.tmp` → `os.replace`  
- `production_frozen`：**运行前**对 `pipeline.PREBREAKOUT_CONFIG` 做 hash，写入 status；与基线指纹比对，漂移则 `ok=false` 并告警（不是写死 true）  
- watch 写失败 → 工厂 status **不得** 保持 true  

#### B4. 发布合同纳入观察页

- `validate_publication_contract` / `validate_run`：可选检查 `prebreakout_shadow_watch.json`  
  - 存在、可解析、`trade_date` 与主 run 一致或标明 shadow 独立  
  - `production_frozen` 与指纹字段存在  
- 观察页失败 **不阻断** 生产发布（保持非阻塞），但写入 `system_health` 警告位，Discord/日志可见  

#### B5. 正式检查器替换手册内嵌脚本

新建：`ops/check_prebreakout_factory.py`（或 scripts 下）

| 检查 | 通过条件 |
|------|----------|
| 交易日 | 用交易日历，非 `datetime.now()` 自然日 |
| 日淘 / 工厂 | `ok is True` 且 `trade_date==expected` |
| 新鲜度 | `finished_at` 在期望窗口内（交易日 19:30–21:30） |
| champion 字段 | 兼容 `observation_champion` / `observation_champion_id` / `champion` |
| 配方 | 读 status 的 `experiment_files` 或 `active/` 目录，而非裸 `ls` 混归档 |
| 一致性 | search edge 与 factory edge 差 < 阈值（P0 后应为 0） |
| 对照 | `production_control.status` |
| exit | 0 通过 / 1 失败 / 2 警告（弱市空仓等） |

同步改写 `PREBREAKOUT_FACTORY_RUNBOOK.md` §2/§10 指向该检查器。

---

### Phase C — P2 研究质量与诚实展示（持续）

#### C1. 成交与成本

- 涨停/跌停无法买：当日收益记 miss 或 0，计入 `fill_rate`  
- 停牌：剔除  
- 成本：保留 0.26% 为 baseline，增加 `cost_stress`（0.4% / 0.6%）敏感性，只展示不选优  
- 报告字段：`paper_return` vs `fill_adjusted_return`

#### C2. 市场门命名

- `market_mom20` 来源已是成交额前 30% 等权 → 字段改名 `liquidity_proxy_mom20`，文案禁止写「沪深300」  
- 可选：并行接入真 HS300 作第二门，默认仍 proxy，页面双显  

#### C3. 搜索预算与过拟合控制

日更轻量保持，但加：

- 参数维数 vs 样本日数警告（如 params > days/5 → `overfit_risk=high`）  
- Top3 **权重余弦多样性** 硬阈值（已有弱约束则收紧）  
- 周度深淘（v3）与日淘产物隔离目录：`experiments/daily/` vs `experiments/deep/`  

#### C4. 观察页诚实 UI

- Banner 分级：`research_only` / `degraded` / `promotion_candidate`  
- 隐藏或灰显「战胜生产」当 control unavailable  
- 分列 train / holdout / paper 曲线，默认突出 **holdout + paper**  
- 空仓：明确 `empty_book_reason`（risk_off / 无过门 / 数据缺）

---

## 4. 实施顺序与里程碑

```text
Week 0（立即）
  └─ 产品口径：观察页与对外说明改为「研究展示，冠军未经验证」
  └─ 运行手册增加 Codex 红牌摘要（见 §6）

Week 1 — Phase A（P0）
  A1 统一 evaluate_recipe + 一致性测试
  A4 交易日贯通 + ops 传参
  A3 真生产对照 + 文案
  A2 真 holdout 选优
  里程碑：同配方 search≡factory；补历史日期正确；不再假「战胜生产」

Week 2 — Phase B（P1）
  B1–B5 状态机、锁、原子写、指纹、检查器、手册
  里程碑：假绿消失；check 脚本 exit code 可信

Week 3+ — Phase C（P2）
  成交约束、命名、过拟合治理、UI
  里程碑：纸面 ledger 可支撑 promotion_candidate
```

---

## 5. 明确不做什么（防范围膨胀）

| 不做 | 原因 |
|------|------|
| 自动把工厂冠军写回 `PREBREAKOUT_CONFIG` | 需人工晋级 + 纸面门槛 |
| 为抬收益加大日淘 DE 维度 | 在真 holdout 前只会加重过拟合 |
| 阻塞主选股管线 | 保持 NON_BLOCKING |
| 用手册「感觉正常」代替检查器 | Codex 已证明手册会误判 |

---

## 6. 整改前临时运维口径（立刻生效）

在 `PREBREAKOUT_FACTORY_RUNBOOK.md` 顶部应增加（实施 PR 时写入）：

> **红牌（Codex 2026-07-16）**  
> 1. 不得依据冠军 edge /「战胜生产」做实盘或改生产。  
> 2. `ok=true` 与 cron Status=ok **不足以**证明完成。  
> 3. 弱市空仓可能正常；`production_top20=0` 时对照数字无效。  
> 4. 补历史日期前确认 env/CLI 已贯通（P0 完成前尽量别补历史）。

---

## 7. 验收清单（Definition of Done）

### P0 Done

- [ ] 单测/冒烟：同一实验 yaml，搜索与工厂 picks 一致、全窗 edge 一致  
- [ ] `TARGET_TRADE_DATE=历史日` 全链产物日期一致  
- [ ] holdout 日不进入 DE/门控搜索目标  
- [ ] 报告分列 train / holdout；选优仅用 holdout（或 train 搜 + holdout 选）  
- [ ] 真生产名单对照；unavailable 时 UI/JSON 无「战胜生产」  
- [ ] 观察页诚实 banner 已更新  

### P1 Done

- [ ] status 含 run_id / started / finished；失败覆盖旧 ok  
- [ ] flock 单实例；并发第二次不污染  
- [ ] JSON 原子写；watch 失败则 factory ok=false  
- [ ] PREBREAKOUT 配置指纹写入 status  
- [ ] `check_prebreakout_factory.py` 替代手册内嵌脚本；runbook 已改  

### P2 Done

- [ ] fill_rate / 涨停不可买进入回测  
- [ ] 市场门命名纠正  
- [ ] promotion_candidate 仅来自纸面 ledger 规则  

---

## 8. 建议的首批代码任务拆分（可直接派工）

| ID | 任务 | 预估 | 依赖 |
|----|------|------|------|
| T1 | 抽取 `evaluate_recipe` + 工厂/搜索改接 | 0.5–1d | — |
| T2 | `recipe_parity` 冒烟测试 | 0.25d | T1 |
| T3 | `resolve_trade_date` + pipeline/ops 传参 | 0.25d | — |
| T4 | 真生产对照 + JSON/UI 字段 | 0.5d | T1 |
| T5 | 日淘真 holdout 选优与报告字段 | 0.5d | T1 |
| T6 | status 状态机 + 原子写 + 指纹 | 0.5d | T3 |
| T7 | flock 接入 ops/脚本 | 0.25d | T6 |
| T8 | `check_prebreakout_factory.py` + 改 runbook | 0.5d | T6 |
| T9 | 成交约束与命名（P2） | 0.5–1d | T1 |

---

## 9. 成功后的系统画像

| 之前 | 之后 |
|------|------|
| 淘沙数字好看、难以证伪 | 分窗披露，holdout/纸面说了算 |
| 搜索与出厂两套逻辑 | 单引擎，一致性门禁 |
| ok/cron 假绿 | run 完成态 + 检查器 exit code |
| 「战胜生产」在空对照下仍亮 | 对照可用性显式化 |
| 手册命令易误判 | 正式检查器 + 红牌口径 |
| 运维靠感觉 | 锁、指纹、原子写、日期闸门 |

---

## 10. 下一步（需你拍板）

1. **是否按 P0→P1→P2 开工？** 建议先做 **T1+T2+T3**（统一引擎 + 日期），当日即可消掉 Codex #2/#4。  
2. **holdout 长度**：20 还是 30 个交易日？（样本紧时用 20 + degraded）  
3. **确认规则默认**：统一为 **raw subscore confirm**（与当前工厂一致）是否确认？  
4. 本方案是否同时改观察页文案（强烈建议与 P0 同发）。

---

## 修订记录

| 日期 | 说明 |
|------|------|
| 2026-07-16 | 基于 Codex「审查工厂 runbook」结论与代码复核，形成优化方案 |
