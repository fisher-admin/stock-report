# SPIKE_REPORT — Spike-1 · 上游迁移: fork housekeeping + PhaseContext backport

- 规格: PHASE2_TIERA_REPORT.md §Spike-1; 输入: deep/upstream_diff.md §1 判决表+housekeeping A-D+§4.1-4.3
- 执行: 2026-07-11 (周六, 非交易日), spike 建造者
- **结论: 通过**（6 条验收 5 条 PASS 实测、1 条拆半: 测试套件 PASS + AI 链 2 交易日绿灯 PENDING 观察），并产出 1 条规格修订建议（接入点②按 §4.3 字面移植会弱化盘前护栏，见 §F-1）。

---

## 0. 交付物清单

| 位置 | 内容 |
|---|---|
| fork `main` @ `7c463d2` | housekeeping 提交（9 工作区改动固化 + 去重 + FREEZE.md + patch/archive 归档） |
| fork 分支 `spike1-phasecontext` @ `8fb63a0` | `src/core/phase_context.py`（backport 模块）+ `spike_harness/mini_pipeline.py`（4 接入点演练）+ `spike_harness/PROPOSED_PATCHES.md` + `tests/test_phase_context.py`（28 单测） |
| 本目录 `code_mirror/` | 上述 4 文件原样镜像（分支删了也不丢） |
| 本目录 `run_acceptance.py` + `fixtures/calendar_202607.json` | 验收 2-5 机械复跑器（21 项断言） |
| 本目录 `outputs/` | acceptance_runs/（各场景 manifest+alerts 落盘）、live_akshare_smoke.txt、full_suite_branch.txt |

fork 状态收尾: 已切回 `main`（生产姿态），`spike1-phasecontext` 分支保留未合并。fork 从不 push（origin=上游只读，FREEZE.md 已声明）。

---

## A. fork housekeeping — 验收 1: **PASS**

按 upstream_diff.md §1 规格 A-D 执行：

- **A(提交固化)**: 9 个工作区修改 + FREEZE.md + 归档文件合为一个本地 commit `7c463d2`，message=`local-freeze: honesty-guardrail + py39-compat + truncation-widen @base-650a1c3`（body 注明 spike-1 housekeeping）。
- **去重**: analyzer.py "## 分析要求"块 3→1。实测 3 处中 2 处是 `LEGACY_DEFAULT_SYSTEM_PROMPT` 内的背靠背双重插入（L806/L814），1 处在生产用 `SYSTEM_PROMPT`（L972）；base@650a1c3 全文 grep=0。删 LEGACY 两处（该区域恢复为 base 原文），保 SYSTEM_PROMPT 一处。
- **B(.bak+归档)**: 删 `src/analyzer.py.bak.202605031208`、`src/notification.py.bak.202605031212`；`ai_analysis_with_retry.py`/`bridge_selection_to_analysis.py` 移入 `patch/archive/`（tracked, 可原样移回）。`fisher_bridge.py` 留根未跟踪（待 stock_pipeline_gate 退役判定，判决表 #12）。
- **C(FREEZE.md)**: 仓根，含 base=650a1c3、冻结四理由、判决表链接、禁 pull/merge/rebase 声明、本地资产清单、活依赖(venv+.env)。
- **提交前测试基线**: 去重后的工作树全量离线套件 **1657 passed, 2 deselected**（防止固化提交打包坏状态）。

复跑命令与实际输出:
```
$ git -C /Users/fisher/.openclaw/workspace/daily_stock_analysis status --short
?? fisher_bridge.py
$ grep -c "## 分析要求" /Users/fisher/.openclaw/workspace/daily_stock_analysis/src/analyzer.py
1
$ ls /Users/fisher/.openclaw/workspace/daily_stock_analysis/FREEZE.md
FREEZE.md   (git ls-files 确认已 tracked)
$ git -C ... log --oneline -2
7c463d2 local-freeze: honesty-guardrail + py39-compat + truncation-widen @base-650a1c3
c7059b5 fix: normalize GPT-5 temperature for LiteLLM
```
判定: 验收 1 三个子项（status 仅剩 fisher_bridge.py / FREEZE.md 在根 / grep=1）全满足 → **PASS**。

---

## B. PhaseContext backport — 验收 2-6

模块设计（对照 §4.3 逐项）: cn 单市场✓、无 unknown 档✓（`Phase.__members__` 无 UNKNOWN，单测锁定）、akshare 日历源+本地 JSON cache 兜底✓、不 import exchange-calendars✓（收盘 15:00/竞价窗 14:57-15:00 硬编码）、**时钟可注入**（`now_fn`）+日历可注入（`calendar_provider`）✓。§4.2 排雷表 7 处 fail-open 全部反转或不适用：日历不可得/日历过期/空日历/推断域外时刻一律 raise `PhaseContextError`（无自然日兜底、无"当开市"兜底）。

接入点实现方式（**解释性决定，如实声明**）: 隔离铁律禁改 skills/ 生产脚本，故 4 个接入点的"各一行判断"落在 fork 分支 `spike_harness/mini_pipeline.py`（逐行 tag ①-④），并在 `PROPOSED_PATCHES.md` 给出 4 个真实生产落点的精确 diff（run_pipeline.py ~L443 / L433-440、greenfield_multifactor_lib.py ~L95 原子写、stock_pipeline_alert.py L54-88）。Wave1 落地时以该文档为施工图。

### 验收 2: 14:59/15:01 两时点 — **PASS**（注入时钟实测）
```
$ /Users/fisher/.openclaw/venv/bin/python run_acceptance.py   （摘录）
[PASS] A2a manifest phase=closing_auction — phase=closing_auction
[PASS] A2a effective_trade_date=20260709 (prev session)
[PASS] A2b manifest phase=postmarket — phase=postmarket
[PASS] A2b effective_trade_date=20260710 (today)
```
14:59 → closing_auction + 前一交易日 + is_partial_bar=True（且裸跑被拒 exit=5）; 15:01 → postmarket + 当日 + 全管线跑通 exit=0。边界格另有 11 组参数化单测（09:29/09:30/11:30/13:00/14:56/14:57/15:00 整点等）。

### 验收 3: 节假日 → non_trading + 拒进取数 stage — **PASS**
```
[PASS] A3 exit=2 (non_trading refusal)
[PASS] A3 manifest phase=non_trading
[PASS] A3 fetch stage NOT entered — stages=['stage0']
```
周日(2026-07-12)与合成周中假日(剔除 0708 的日历)两种都测（后者在单测）。effective_trade_date=上一交易日。

### 验收 4: 日历取不到 → 退出码≠0 + 告警 — **PASS**（模拟断网实测，告警走 stub 缝）
```
[PASS] A4 exit=3 (fail-closed, non-zero)
[PASS] A4 alert emitted (Discord seam stub) — alerts=['phase_context_unavailable']
[PASS] A4 no phase_context in manifest (no guessed context)
```
诚实声明: Discord 告警在 spike 内为 **stub**（写 alerts.jsonl + stderr `DISCORD_ALERT_STUB`），缝位与生产 pipeline_alert webhook 相同——spike 真发 Discord 消息属生产副作用，刻意不发。补充: 断网+有 cache → calendar_source=cached + warning 降级续跑（S7 PASS）; cache 损坏 → 仍 raise（单测）。

### 验收 5: 0710 型盘中缓存污染回放 — **PASS**
构造工作日盘中时刻(2026-07-10T11:00, 注入时钟)带 --trade-date 请求缓存写入:
```
[PASS] A5 exit=4 (partial-bar cache write blocked)
[PASS] A5 block alert emitted — alerts=['partial_bar_cache_write_blocked']
[PASS] A5 sandbox cache empty (nothing written)
```
写入前断言 `guard_backtest_cache_write` 拦截，沙箱缓存目录零文件。补充 S6: 真实 0710 事故时刻(07:47 盘前裸跑)回放 → 放行但 effective_trade_date=20260709，缓存文件名 cyq_perf_20260709——事故的实际坏法(盘前数据标为当日)被 effective 日期语义结构性消除。

### 验收 6a: 测试套件全绿 — **PASS**
- 分支全量: **1685 passed, 2 deselected**（现有 1657 + 新增 28, outputs/full_suite_branch.txt）
- main(housekeeping 后)全量: 1657 passed, 2 deselected
- 验收器: 21/21 checks passed
- 附加实测: 真实 akshare 默认链路 smoke（真时钟 2026-07-11 周六）→ `phase=non_trading, effective=20260710, calendar_source=akshare`（outputs/live_akshare_smoke.txt）✓

### 验收 6b: AI 链(19:30 管线)连续 2 交易日绿灯 — **PENDING**（日历绑定, 无法提前实测）
- 观察计划: 下两个交易日 **2026-07-13(一)、07-14(二)** 19:30 管线后，查 aiBackfill stage 绿灯（run_manifest / Discord pipeline alert 无 critical）。
- 风险评估: housekeeping 仅固化提交+归档，唯一内容变化是 analyzer.py LEGACY 提示词区去重（生产 AI 链 2026-07-08 起直连 generate_prebreakout_ai.py，不经 fork src/；fork 活依赖仅 venv+.env，两者未动）→ 预期绿灯。若红灯，回滚=`git -C daily_stock_analysis revert 7c463d2`（或从 patch/archive 移回）。

---

## F. 发现与规格修订建议

- **F-1（规格修订建议, 唯一实质发现）**: upstream_diff.md §4.3 接入点②原文"盘中裸跑护栏改读 is_partial_bar"**按字面单独落地会弱化现有护栏**——premarket 的 is_partial_bar=False（上游语义，本模块保留），07:47 型盘前裸跑将放行；现生产护栏(run_pipeline.py L433-440)覆盖 00:00-15:05 全窗。放行仅在①(stage1 改由 effective_trade_date 驱动)+④同时落地时安全（S6 已证：盘前跑数据自动标为前一完成日）。**处置二选一**: ②写成 `is_partial_bar or phase is PREMARKET`（PROPOSED_PATCHES.md 采用），或①②④绑定同一 commit。已写入 PROPOSED_PATCHES.md。
- F-2: 上游 test_trading_calendar.py 边界断言未读（confession 建议项）——本 spike 的 cn 裁剪语义与上游多市场语义分歧过大（无 unknown/无市场参数/固定收盘时刻），直接以 §4.3 冻结契约+自建 28 单测为验收基准；Wave1 若移植 provider 骨架仍应先读上游对应测试。
- F-3: "跑管线"验收在 spike 内以 mini_pipeline 演练替代真 19:30 管线（隔离铁律禁改生产）；PhaseContext 端语义已全部实测，真接入的验证归 Wave1（施工图=PROPOSED_PATCHES.md）。

## 回滚
- housekeeping: `git revert 7c463d2`（归档文件在 patch/archive/ 可原样移回；.bak 已删但内容=git 历史中的 base+diff，无信息损失）。
- backport: 分支未合并 main，删分支即零残留；factor_factory 侧无任何生产文件改动。

---

## 隔离核查（铁律要求）

`git -C /Users/fisher/.openclaw/workspace status --short` 共 1110 行，与会话起始快照对比：本 spike 新增的唯一条目是
```
?? factor_factory/research/2026-07-github-stock-systems/spikes/
```
（additive, 本 spike 全部产出所在）。其余 M/D 条目均为会话开始前已存在的工作区状态（起始快照可查）。未触碰: skills/ 生产脚本、panel_full、冻结 json、recommendations.db、backtest_cache 既有文件、stock-report 克隆。daily_stock_analysis 为独立嵌套 git 仓（不在 workspace 仓 status 内），其改动即规格授权的唯一例外（housekeeping 提交+spike 分支）。缓存写入测试全部落在 outputs/acceptance_runs/*/backtest_cache_sandbox/（沙箱）。

## 复跑命令汇总
```bash
# 验收1 (fork main)
git -C /Users/fisher/.openclaw/workspace/daily_stock_analysis status --short        # 期望: ?? fisher_bridge.py
grep -c "## 分析要求" /Users/fisher/.openclaw/workspace/daily_stock_analysis/src/analyzer.py   # 期望: 1

# 验收2-5 (需先切分支; 跑完切回 main)
git -C /Users/fisher/.openclaw/workspace/daily_stock_analysis checkout spike1-phasecontext
/Users/fisher/.openclaw/venv/bin/python /Users/fisher/.openclaw/workspace/factor_factory/research/2026-07-github-stock-systems/spikes/spike1_upstream/run_acceptance.py   # 期望: 21/21, exit 0

# 验收6a (pytest 装到任意 --target 目录, 不动 fork venv)
/Users/fisher/.openclaw/venv/bin/python -m pip install --target=/tmp/pytest_pkgs pytest
cd /Users/fisher/.openclaw/workspace/daily_stock_analysis && PYTHONPATH=/tmp/pytest_pkgs venv/bin/python -m pytest -m "not network" -q -p no:cacheprovider   # 期望: 1685 passed(分支)/1657(main)
git -C /Users/fisher/.openclaw/workspace/daily_stock_analysis checkout main
```
