# A股选股操作系统（stock-report）

这是 Fisher 选股系统的公开代码与结果仓库。它包含系统架构、核心选股与验证代码、测试，以及 GitHub Pages 展示层。

- 首页：`index.html` → 系统总控台
- 市场层：`market-overview.html` → 市场驾驶舱
- 策略层：`strategy-vs-market.html` → 策略中心
- 个股层：`decision-candidates.html` → 候选股决策中心
- 复盘研究层：`research-lab.html` / `recommendation-review.html`

## 本机与 GitHub 的边界

1. 行情、财务数据、数据库、历史逐股推荐、原始 AI 分析和缓存只在本机保存与计算。
2. GitHub 仓库公开系统架构、核心源代码、测试、说明和页面代码。
3. GitHub Pages 只读取明确列入白名单的结果摘要；不提供原始数据下载或全量历史明细。
4. 每次发布先做合同校验、隐私清理和白名单审计，任何一步失败都不推送。
5. 所有公开结果可追溯到交易日、运行编号和生成时间；策略未通过门槛时保持“观察”，不接自动下单。

## 仓库结构

- `system/`：本机选股、事件策略、评价与发布闸门的核心源代码快照
- `docs/ARCHITECTURE.md`：系统结构、数据流和公开边界
- `assets/` 与页面文件：GitHub Pages 展示层
- `data/latest/`：当前允许公开的结果合同
- `config/public-result-allowlist.txt`：唯一允许发布的数据文件清单
- `scripts/enforce_public_boundary.py`：发布前清理和审计
- `tests/`：页面与公开边界回归测试

关键公开结果包括：

- `run_manifest.json`：本轮运行总状态
- `market_state.json`：市场层状态
- `strategy_state.json`：策略层状态
- `candidate_state.json`：当前候选结果
- `review_track_latest.json`：组合级复盘摘要，不含逐股历史明细
- `research_state.json`：研究摘要

## 目标

这套站点的目标不是“展示很多页面”，而是回答一条完整链路：

**本机完成数据计算与 AI 核对 → 规则闸门验证 → 生成公开结果摘要 → GitHub Pages 展示**

完整设计见 [系统架构](docs/ARCHITECTURE.md)。
