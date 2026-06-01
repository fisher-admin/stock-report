# Changelog

All notable changes to FisherQuant are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.0.0] — 2026-05-29

### Added
- Stable public data contract: `data/latest/*.json` with full `run_id / trade_date / generated_at` traceability
- `system_verdict.json` hard gate validation (freshness gate, date contract gate, pipeline completion gate)
- Complete 5-layer pipeline: market judgment → strategy orchestration → candidate scoring → retrospective → factor research
- `CONTRIBUTING.md`, `SECURITY.md`: community contribution and security reporting infrastructure
- GitHub Actions workflow with daily automated deployment (weekdays 01:00 UTC)
- Issue templates: structured bug report and feature request forms
- PR template enforcing data contract and disclaimer verification
- 8 repository topics: `quantitative-finance`, `a-share`, `stock-analysis`, `ai-agent`, `alpha191`, `china-stock`, `automated-trading`, `github-actions`

### Changed
- Project rebranded to **FisherQuant** (formerly internal "OpenClaw" codename)
- README completely rewritten with architecture diagram, data contract table, screenshot gallery, and Codex integration roadmap

---

## [0.9.0] — 2026-05-21

### Added
- AI detail cards rendered inline for all three strategy tabs (PreBreakout, T1, O2C)
- `sentiment.html`: market sentiment analysis with overnight FX history chart
- `adjustment_log.json`: audit trail for manual overrides and system adjustments
- Scroll position persistence across page navigation (localStorage)
- Decision filter summaries and sort state memory

### Changed
- Full CSS design system unification across all 6 pages (consistent typography, spacing, CSS custom properties)
- `recommendation-review.html` rebuilt with unified retrospective view

---

## [0.7.0] — 2026-05-19

### Added
- AI deep analysis coverage expanded to 40 stocks per day
- `research-lab.html`: Alpha191 factor research and T1 model analysis viewer
- AI-generated markdown narrative cards embedded in candidate decision view
- `factor_evolution_state.json`: tracks factor signal drift across consecutive runs
- `t1_factor_research_state.json`: dedicated T1 Alpha191 research output

### Changed
- Candidate scoring upgraded: AI factor reasoning displayed alongside quantitative scores

---

## [0.5.0] — 2026-05-10

### Added
- Multi-strategy orchestration: PreBreakout v4.1, T1 Alpha191, and O2C strategies running in parallel
- Strategy consensus scoring: cross-strategy agreement surfaces higher-conviction candidates
- `strategy-vs-market.html`: active/watch/degraded strategy state dashboard
- `strategy_state.json` and `strategy_consensus_state.json` added to data contract
- `system_health.json`: pipeline component health tracking

### Changed
- `decision-candidates.html` updated to show per-strategy attribution for each candidate

---

## [0.3.0] — 2026-04-17

### Added
- `decision-candidates.html`: execution-first workbench with entry/exit condition cards
- `stock-workbench.js`: interactive filtering, sorting, and drill-down for candidate stocks
- `recommendation-review.html`: post-hoc validation against actual price movement
- `candidate_state.json` and `review_state.json` added to data contract

### Changed
- `index.html` redesigned as a system console showing run status and pipeline health
- Dashboard navigation unified across all pages

---

## [0.1.0] — 2026-02-12

### Added
- Initial public release: single-page A-share quantitative research report
- `market-overview.html`: morning brief and midday analysis viewer
- `market_state.json`: market regime classification and risk scoring
- `run_manifest.json`: run identity and validation gate results
- `generate_github_pages.py`: static site generator from JSON data contract
- GitHub Pages deployment via `deploy.sh`
- MIT License

---

[1.0.0]: https://github.com/fisher-admin/stock-report/compare/v0.9.0...v1.0.0
[0.9.0]: https://github.com/fisher-admin/stock-report/compare/v0.7.0...v0.9.0
[0.7.0]: https://github.com/fisher-admin/stock-report/compare/v0.5.0...v0.7.0
[0.5.0]: https://github.com/fisher-admin/stock-report/compare/v0.3.0...v0.5.0
[0.3.0]: https://github.com/fisher-admin/stock-report/compare/v0.1.0...v0.3.0
[0.1.0]: https://github.com/fisher-admin/stock-report/releases/tag/v0.1.0
