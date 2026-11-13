# Wave1 批次1 收账报告 (TRANCHE1)

- 收账日期: 2026-07-11(周六) · 收账人: 批次收账 agent
- 输入: 本目录五份轨道报告 — `B-5.md` / `B-6.md` / `R-1修订接入分支.md` / `契约层休眠落库.md` + `W1-3-切换设计.md` / `影子常态化挂闭环.md`
- **总判决: 五轨全过 (4 pass + 1 pass_after_redo), 零红灯。全部改动对周一 7/13 19:30 生产管线为休眠/只读/纯 additive/非致命四类之一, 无生产行为变更。**

---

## 1. 五轨判决表

| 轨道 | 报告 | 判决 | 核心证据 | 备注 |
|---|---|---|---|---|
| t1 · B-6 回补31日洞 | B-6.md | **pass_after_redo** | 184/184 交易日零洞; 31 文件全新增(31/31 `??`, exists 门控 SKIP=0); 行数 5424–5497 全过 >4000 闸 | 两处重做后过: ① 连续性初检用跨 4 月洞的假相邻日产生 ~200 假失配, 二次以真前一交易日复核 → ≤4/300(除权噪声级); ② 上一版误称 backtest_cache 整体 untracked, 勘误为该目录有 193 tracked 文件——但本轨 31 文件逐一核验均 untracked, 安全结论不变 |
| t2 · B-5 PEAD双写PIT店 | B-5.md | **pass** | pit_store 33/33 单测 + spike2 七条验收 7/7 在生产店重跑 PASS; 真实摄取 58,504 行入店, R-2.1 下界守卫首跑活捉 2 行 -481 天污染样本入隔离表; 二遍幂等 appended=0; 主输出 93 行纯插入 0 删改, 对账旧 29,652 键零丢失; 双写强制 raise 沙盒实证主输出照写、exit 0 | R-2 四条收紧全落地; 该脚本不在 19:30 管线内(run_pipeline.py 零引用) |
| t3 · R-1 修订接入分支 | R-1修订接入分支.md | **pass** | fork `spike1-phasecontext` 分支 @2186923 落 R-1 选项(a)复合条件(canonical helper `refuse_bare_run_if_partial`); 07:47 型盘前裸跑函数级+管线级单测均拦(exit=5+零写入); 分支全量单测 1697 passed; 验收器升级 24/24 | fork main 未动(@7c463d2), 完工已切回 main 生产姿态; 合并等 P-1 双绿灯(见 §3.1) |
| t4 · 契约层休眠落库 | 契约层休眠落库.md + W1-3-切换设计.md | **pass** | `factor_factory/contracts/` 落库含 R-3 四条(pre_close 能力表 fail-closed / yjyg 27 键规范 / 健康度-合规分离 / 名称归一化 79→0 残差); 单测 54/54; 离线+live 自检双 exit 0; 全工作区 grep 零生产 import | 休眠零调用点; W1-3 三态开关(off/shadow/enforce, 每消费点独立 env, 禁全局单开关)设计稿交付待批次2实施 |
| t5 · 影子常态化挂闭环 | 影子常态化挂闭环.md | **pass** | wrapper 实跑 locks=49 blocked=660 stress_days(5%档)=5, 与 SPIKE_REPORT 验收4逐项一致; 三列 JSON(锁生效日/被挡推荐/后验超额5日)落 health/ 双写(latest+按日存档); `bash -n` 收盘链语法过 | DB 只读(mode=ro)+MemLockStore 纯内存; 收盘链仅末尾非致命追加(`\|\| echo non-fatal`) |

---

## 2. 生产改动全景 (git status 汇总 + 逐文件安全定性)

收账时 `git status --short` 总量: **112 M + 71 D + 982 ??**。分三类定性:

### 2.1 Wave1 五轨产出 — 生产可触文件 (仅 2 个 tracked 修改)

| 文件 | 轨 | 状态 | 安全定性 |
|---|---|---|---|
| `skills/stock-system-orchestrator/scripts/ingest_pead_preannounce.py` | t2 | M | **安全**: 93 行纯插入 0 删改(git diff --stat 佐证), 主路径 byte 级不变; 双写段整体 try/except 异常隔离(沙盒强制 raise 实证不改退出码); 不在 19:30 管线(run_pipeline.py 零引用), 唯一读方 phase_b_pead_backtest.py 只读 schema 未变的主输出; 无定时器, 仅人工运行时生效 |
| `ops/run_daily_closure.sh` | t5 | M | **安全**: 仅末尾追加非致命步骤(失败 `\|\| echo non-fatal` 不中断收盘 1-4 步); 既有行零改动; 新步骤对生产 DB 只读+纯内存重放, 只写 health/ 影子产物 |

### 2.2 Wave1 五轨产出 — 纯 additive 新增 (untracked, 删除即净回滚)

| 条目 | 轨 | 安全定性 |
|---|---|---|
| `?? factor_factory/pit/` (pit_store.py+单测+生产验收器) | t2 | 新模块, 无现有代码 import |
| `?? stock_data/03-working/fundamental_cache/pit/` (主表+隔离表 2 parquet) | t2 | 新数据目录, 非发布源非 panel; 隔离表现有 2 行待人工裁决 |
| `?? factor_factory/contracts/` (9 源文件+contracts.yaml+54 断言单测) | t4 | 零生产调用点(全工作区 grep 佐证); 运行时写入限 git-ignored `.selfcheck/` |
| `?? ops/run_protection_shadow.sh` | t5 | 全新 wrapper, 只读 DB+内存重放 |
| `?? stock_data/03-working/health/protection_shadow_latest.json` + `protection_shadow_archive/` | t5 | 影子产物, 非生产资产非发布链 |
| `?? backtest_cache/daily_{31日}.parquet` ×31 | t1 | 31/31 逐一核验 untracked 新增, 零 tracked 文件被改/删; 回测缓存补洞, 对下游严格信息增量 |
| `?? factor_factory/research/.../wave1/` (五报告+本报告+data/跑前备份) | 全 | 研究产物; 含 pead 主输出跑前备份(sha256 828e1a69...) |

### 2.3 Wave1 研究侧 tracked 修改 (非生产, 15 文件)

`M factor_factory/research/.../spikes/spike1_upstream/` 下: `run_acceptance.py`(S6→S6a/S6b 对齐 R-1 语义) + `code_mirror/` 4 文件(只读镜像刷新, 无 import 方) + `outputs/acceptance_runs/` 10 文件(验收器沙箱输出重建)。全部仅手动运行/只读镜像, 无生产调用方。t3 的代码改动本体在 fork 仓分支, workspace 仓零生产资产改动。

### 2.4 非本批次遗留 (与 Wave1 无关, 不在本次验收范围)

- `backtest_cache/` 64 D(2025-11/12 月 cyq_perf+stk_factor) + 64 M(2026 年同型) = 128 条: 前序会话滚动缓存行为遗留, 开工前快照已存在。
- 其余 38 条 tracked M/D: AGENTS/MEMORY/SOUL/IDENTITY 等 memory 体系文件、skills 下前序修改(stock_analyzer/long-term-alpha 等)、alpha_mining 输出、`D BOOTSTRAP.md` 等——均为开工前已存在的工作区状态。
- 982 条 `??` 大部为历史积累(docs/、configs/、daily_stock_analysis fork 目录等), Wave1 新增部分已在 §2.2 列全。

### 2.5 铁律核查汇总

五轨报告一致声明且交叉无矛盾: 未触碰 setup_registry / panel_full / 发布链 / recommendations.db(只读除外) / run_pipeline.py / 任何 cron·launchd 注册; tushare 全程走代理; 冻结资产零改动。fork 仓完工态: main @7c463d2, status 仅 `?? fisher_bridge.py`。

---

## 3. 周一观察清单 (7/13 起)

### 3.1 P-1: 两晚管线绿灯 → 合并 t3 分支
- **7/13(一)、7/14(二) 两晚 19:30 管线后**核: aiBackfill stage 绿灯 = run_manifest 无 critical + Discord pipeline alert 无 critical, 连续两日。
- **双绿 → 执行 R-1 报告 §3 合并检查单**: fork main 确认干净@7c463d2 → 复验分支(验收器 24/24 + 单测 1697)→ `merge --no-ff spike1-phasecontext` → 合并后 import smoke + main 全量单测 + status 恢复生产姿态。全程 fork 本地永不 push。
- **红灯 → 检查单作废**: 先 `revert 7c463d2` 处置 P-1, 分支保留待 rebase 重评。
- 注: 合并 ≠ 生产接线; 四接入点接线是独立后续条目(W1-8), 非交易时段实施。

### 3.2 影子首跑闭环 (7/13 ~20:45)
- 收盘链 `run_daily_closure.sh` 跑完后核: ① `health/protection_shadow_latest.json` 时间戳刷新; ② `protection_shadow_archive/protection_shadow_20260713.json` 生成; ③ 收盘链 1-4 步不受新步骤影响(即便影子步报错也应 non-fatal 走完)。
- 首跑数字与 7/11 基线(locks=49/blocked=660/stress5)对照, 增量应来自新交易日。

### 3.3 PEAD 双写首个交易日核对 (7/13)
- 人工跑 `ingest_pead_preannounce.py --start 2021` 一遍: ① 主输出行数/schema 正常刷新; ② PIT 店 appended≥0 且 quarantined 不异常膨胀(现有 2 行基线); ③ 新公告日 vintage 入链。
- 提醒: 东财 yjyg 为 latest-only 源, **每晚一天少一天 vintage** — 建议本周内将 ingest 纳入例行(定频决定可留到切读 commit 一并做, 但摄取本身宜尽早日跑)。

### 3.4 P-2: EM 通道对账
- 按 PHASE3 观察计划继续跑 spike3 目录 `run_em_recon.py` 落盘, 观察至约 7/16。
- 结论落定: EM 活 → 恢复 `--em` 可选通道; EM 死 → sina 正名为对账主源(contracts.yaml 无需改动, sina 契约已冻结)。W1-3 shadow 期一律禁用 EM。

---

## 4. 批次2 议程草案

| # | 条目 | 前置闸 | 内容 |
|---|---|---|---|
| 1 | **W1-3 消费端切换开工** | 契约层休眠稳定(已达) | 新增 `contracts/switch.py`(resolve_mode+shadow_compare); 首消费点=PEAD 摄取(与 PIT 双写周并轨), 按 6 步验证走: 离线绿→live 绿→shadow≥3 交易日 0 breach+拒绝率人工分类→enforce 单 commit 周五后→2 交易日观察→健康度接 Discord。次序: PEAD→名称快照→面板回补→19:30 管线最后(需 P-2 落定+前三点 enforce ≥1 周) |
| 2 | **W2-1 parity 切换** | spike5 表达式 parity 验收已过(Phase3) | 表达式引擎 parity 从 spike 态推进切换, 同 W1-3 纪律: shadow 先行、enforce 单 commit |
| 3 | **W2-2 tradability** | — | 可交易性(停牌/涨跌停/流动性)闸模块推进 |
| 4 | **切读 PEAD** | 双写稳定一周(**≥7/18**) | 消费端(phase_b_pead_backtest.py 等)改读 PIT 店 `pit_asof(mode='as_first_reported')`, 单独 commit; 顺带消除主路径 `contains("净利润")` 混扣非 + `keep='first'` 压链两处口径旧疾; 同 commit 定 ingest 例行频率 |
| 5 | **18族接线** | 影子常态化满一周(**7/18 后拍板**, B-4 决议) | 核一周影子 JSON(被挡后验超额分布/5%档触发日合理性)→ 拍板保护三层闸接线第 18 族; MARKET_STRESS 档位按影子分布定(现拍板 5%, wrapper 支持 env 覆盖调参) |
| 6 | (条件项) **W1-8 PhaseContext 生产接线** | t3 分支已合并(§3.1) | 模块拷贝→按 PROPOSED_PATCHES.md 施工, ①②④绑定同一 commit(R-1 纪律), 非交易时段实施, 次一交易日绿灯验收 |

---

## 5. 轨道状态

```json
[{"track":"t1_backfill","status":"pass_after_redo"},{"track":"t2_pead_dualwrite","status":"pass"},{"track":"t3_r1_branch","status":"pass"},{"track":"t4_contract_module","status":"pass"},{"track":"t5_shadow_daily","status":"pass"}]
```
