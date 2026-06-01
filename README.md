# FisherQuant · stock-report

**An autonomous AI agent system for A-share quantitative research — from daily market judgment to candidate scoring, strategy tracking, and retrospective optimization, all in one self-updating public dashboard.**

[![Last Commit](https://img.shields.io/github/last-commit/fisher-admin/stock-report?style=flat-square)](https://github.com/fisher-admin/stock-report/commits/main)
[![GitHub Stars](https://img.shields.io/github/stars/fisher-admin/stock-report?style=flat-square)](https://github.com/fisher-admin/stock-report/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/fisher-admin/stock-report?style=flat-square)](https://github.com/fisher-admin/stock-report/forks)
[![Deploy Stock Report](https://github.com/fisher-admin/stock-report/actions/workflows/deploy.yml/badge.svg)](https://github.com/fisher-admin/stock-report/actions/workflows/deploy.yml)
[![Live Demo](https://img.shields.io/badge/live%20demo-GitHub%20Pages-2ea44f?style=flat-square&logo=github)](https://fisher-admin.github.io/stock-report/)
[![Data Updated Daily](https://img.shields.io/badge/data-updated%20daily-brightgreen?style=flat-square)](#architecture)

> 这不是一份静态选股报告，而是一套每日自动运行的 AI 量化研究操作系统。  
> This is not a static stock report — it is a daily self-running AI quantitative research operating system.

---

## What It Does

Most quant dashboards answer *one* question. FisherQuant answers *five*, in sequence, every single trading day:

```
① Can we trade today?      →  Market regime classification + risk scoring
② Which strategy to run?   →  Multi-strategy activation & consensus scoring
③ Which stocks to watch?   →  AI-scored candidate cards with entry/exit conditions
④ Did we get it right?     →  Automated retrospective vs. actual price movement
⑤ How to improve?          →  Factor evolution tracking + strategy parameter tuning
```

All five layers are publicly visible, fully traceable to `run_id / trade_date / generated_at`, and rebuilt automatically on each trading day.

---

## Live Dashboard

| Layer | Page | Description |
|---|---|---|
| 🏠 System Console | `index.html` | Run status, system verdict, overall health |
| 📊 Market | `market-overview.html` | Morning brief, midday analysis, industry heatmap |
| 🎯 Strategy | `strategy-vs-market.html` | Active / watch / degraded strategy states |
| 📋 Candidates | `decision-candidates.html` | Per-stock AI decision cards |
| 🔬 Research Lab | `research-lab.html` | Alpha191 factor research, T1 model analysis |
| 🔁 Review | `recommendation-review.html` | Post-hoc validation against real market data |

### Screenshots

| System Console | Market Overview |
|---|---|
| ![System Console](verification-index.png) | ![Market Overview](verification-market-overview.png) |

| Candidate Decisions | Strategy vs Market |
|---|---|
| ![Candidates](verification-decision-candidates.png) | ![Strategy](verification-strategy-vs-market.png) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                 FisherQuant Agent Pipeline               │
│                                                         │
│  Market Data ──▶ Market Agent ──▶ market_state.json     │
│                       │                                 │
│                       ▼                                 │
│             Strategy Orchestrator ──▶ strategy_state    │
│              (PreBreakout v4.1 / T1 / O2C)              │
│                       │                                 │
│                       ▼                                 │
│          AI Factor Research (Alpha191)                  │
│                       │                                 │
│                       ▼                                 │
│         Candidate Scoring ──▶ candidate_state.json      │
│                       │                                 │
│                       ▼                                 │
│       system_verdict.json  ◀──  Review & Validation     │
│                       │                                 │
│                       ▼                                 │
│        data/latest/*.json  ──▶  GitHub Pages            │
└─────────────────────────────────────────────────────────┘
```

The entire pipeline is orchestrated by a Python agent backend and published to this repository via **GitHub Actions** every trading day. The public site reads exclusively from `data/latest/*.json` — a strict versioned data contract ensuring every displayed result is reproducible and auditable.

---

## Data Contract

All public pages consume a single source of truth: `data/latest/`.

| File | Purpose |
|---|---|
| `run_manifest.json` | Run identity: `run_id`, `trade_date`, validation gates |
| `market_state.json` | Market regime, risk score, morning & midday context |
| `strategy_state.json` | Strategy activation states and consensus scores |
| `candidate_state.json` | Per-stock decision cards with AI reasoning |
| `review_state.json` | Retrospective summary vs. actual outcomes |
| `research_state.json` | Factor research and alpha signal analysis |
| `system_verdict.json` | Final system-level go/no-go verdict with gate checks |

Every artifact is tagged with `trade_date`, `run_id`, and `generated_at` — enabling full audit trails across runs.

---

## Key Design Decisions

- **Single data contract**: pages never parse raw analytics directly; all display logic reads from `data/latest/` only
- **Layered responsibility**: market / strategy / candidate / review are strictly separated — no layer skips upstream validation
- **Hard gates**: `system_verdict.json` enforces freshness, date consistency, and pipeline completion before anything is published
- **Zero client-side secrets**: the public site is fully static; all AI inference and data processing happens in the private agent backend

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Backend | Python (multi-model: T1, O2C, Alpha191 pipeline) |
| Frontend | Vanilla JS + Chart.js, semantic HTML5 |
| Styling | Custom CSS design system (`assets/styles/`) |
| Deployment | GitHub Actions → GitHub Pages |
| Data Format | JSON (versioned contracts) + CSV (factor outputs) |

---

## Roadmap

### ✅ Stable

- Multi-layer pipeline: market → strategy → candidate → review
- Unified `data/latest/*.json` data contract with full traceability
- Daily auto-deploy via GitHub Actions
- AI-generated market context, Alpha191 factor research, multi-strategy scoring
- System verdict with hard gate validation

### 🚀 Codex-Driven Evolution

- [ ] **Phase 1 — Architecture Migration** ([#1](../../issues/1))  
  Transition agent memory structures and multi-model routing into a native OpenAI Codex ecosystem for deeper contextual reasoning across runs.

- [ ] **Phase 2 — Automated Maintenance** ([#2](../../issues/2))  
  Integrate Codex into GitHub Actions for autonomous PR review, incremental refactoring, and AI-driven issue triage.

- [ ] **Phase 3 — Developer Ecosystem** ([#3](../../issues/3))  
  Self-generating API documentation and contributor tooling powered by Codex code comprehension.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. Contributions welcome in:

- **Frontend**: improving the dashboard UI/UX (`*.html`, `assets/`)
- **Data schema**: proposing extensions to the `data/latest/` contract
- **Tooling**: `generate_github_pages.py`, `deploy.sh` improvements

Please open an issue before submitting a PR for non-trivial changes.

---

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability reporting policy.

---

## License

MIT — data outputs and frontend code are freely reusable. The private agent backend and proprietary strategy logic are not included in this repository.
