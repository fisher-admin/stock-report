# OpenClaw — Autonomous AI Agent Framework for Quantitative Research and Workflow Automation

> **stock-report** is the public visualization layer of the OpenClaw ecosystem — an autonomous AI agent and quantitative framework designed for end-to-end market research, multi-strategy orchestration, and self-optimizing workflow automation.

这是 Fisher 选股系统的公开展示层，不再是单页“选股报告”，而是一套分层操作系统：

- 首页：`index.html` → 系统总控台
- 市场层：`market-overview.html` → 市场驾驶舱
- 策略层：`strategy-vs-market.html` → 策略中心
- 个股层：`decision-candidates.html` → 候选股决策中心
- 复盘研究层：`research-lab.html` / `recommendation-review.html`

## 当前设计原则

1. 公开页统一读取 `data/latest/*.json`
2. 不再让每个页面各自拼接松散 JSON
3. 页面职责固定：市场 / 策略 / 个股 / 复盘研究 分层
4. 所有公开结果必须能追溯到 `trade_date / run_id / generated_at`

## 关键数据文件

位于 `data/latest/`：

- `run_manifest.json`：本轮运行总状态
- `market_state.json`：市场层状态
- `strategy_state.json`：策略层状态
- `candidate_state.json`：个股决策卡
- `review_state.json`：复盘摘要
- `research_state.json`：研究摘要

原始分析产物仍保留在 `data/recommendation_analytics/`，用于生成 latest 层。

## 目标

这套站点的目标不是“展示很多页面”，而是回答一条完整链路：

**今天市场能不能做 → 哪条策略该上 → 哪些个股值得看 → 事后验证做得对不对 → 下一轮该怎么优化**

---

## Roadmap

### ✅ Current (Stable)
- Multi-layer architecture: market / strategy / candidate / review pipeline
- Unified `data/latest/*.json` data contract with full traceability (`trade_date`, `run_id`, `generated_at`)
- Static-site deployment via GitHub Actions (`deploy.sh` + `generate_github_pages.py`)
- AI-generated market context, factor research, and candidate scoring

### 🚀 Codex-Driven Evolution (Upcoming)
- [ ] **Phase 1: Architecture Migration & Refactoring**
  - Transition core agent memory structures and multi-model routing systems into a native OpenAI Codex ecosystem setup to enhance deep contextual intelligence.
- [ ] **Phase 2: Codex-Powered Automated Maintenance**
  - Integrate Codex into GitHub Actions for autonomous PR code reviews, continuous incremental refactoring, and AI-driven issue diagnostics.
- [ ] **Phase 3: Automated Secondary Development Ecosystem**
  - Build self-generating developer documentation and API reference tools leveraging Codex's advanced code comprehension.