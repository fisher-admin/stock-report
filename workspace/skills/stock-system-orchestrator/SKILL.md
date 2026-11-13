---
name: stock-system-orchestrator
description: Orchestrate a resilient A-share stock selection pipeline with staged execution, point-in-time data controls, an immutable production control, observation-only shadow portfolios, AI evidence checks, downgrade rules, validation, and publishing. Use when creating, refactoring, operating, or scheduling the OpenClaw stock pipeline.
---

# Stock System Orchestrator

## Overview

Use this skill to structure a stock-selection system as a **4-stage pipeline** with clear dependencies, fallback chains, and downgrade behavior. Prefer this skill when the current design is a single heavy cron and needs to become maintainable, restartable, and resilient.

## Pipeline Stages

Split the system into these four stages. Do not merge them back into one giant cron unless explicitly required.

### Stage 1: Data Preparation

Goal: make the current trade date usable and mark the dataset as ready.

Responsibilities:
- Resolve the effective trade date.
- Pull or refresh base quote data.
- Pull or refresh technical-factor data.
- Pull or refresh chip-factor data, with explicit provenance.
- Validate date consistency across required caches.
- Produce a machine-readable readiness result.

Typical outputs:
- `daily_YYYYMMDD.parquet`
- `stk_factor_YYYYMMDD.parquet`
- `cyq_perf_YYYYMMDD.parquet`
- `health/data_ready_YYYYMMDD.json`

Blocking rules:
- If trade date is unknown, stop.
- If base quotes are missing, stop.
- If real chip factors are missing, stop chip-dependent ranking and mark the run failed/degraded.
- Never allow locally generated or proxy chip values into ranking, backtests, or factor evaluation.

### Stage 2: Core Stock Selection

Goal: generate the day’s main stock-selection result.

Responsibilities:
- Run the core pipeline on the effective trade date.
- Scan the market universe.
- Score and rank candidates.
- Save top selections for next-day verification.
- Generate a lightweight summary.
- Optionally sync to tracker/database.

Typical outputs:
- `selection_history/YYYYMMDD.json`
- `verify_history/YYYYMMDD.json`
- `latest_summary.txt`
- base report JSON

Blocking rules:
- Require Stage 1 success.
- If Stage 1 is degraded, continue only with allowed factor downgrades clearly marked.

### Stage 3: Strategy Suite / Backtests

Goal: refresh the single retained production control and the three observation-only short-track books.

Active production source:
- `prebreakout_v41` is a legacy source alias only.
- Its immutable strategy identity is `prebreakout_v43_control`, version `4.3+8a5054a13fc32f0e`.

Forward short-track books (all observation-only):
- `prebreakout_v43_control`: exact production Top20 control.
- `prebreakout_v43_top15`: Top15 with no more than three stocks per industry.
- `prebreakout_v44_balanced`: five equal-weight non-chip factor groups.

Archived historical controls must not generate new daily signals: O2C, T1, S3,
WTS, auction chasing, simple PEAD ranking, and intraday reversal.

Responsibilities:
- Rebuild the retained production control.
- Write immutable candidate snapshots for the three short-track books.
- Maintain a five-sleeve overlapping portfolio ledger with T+1-open entry,
  adjusted prices, 0.30% base / 0.50% stress cost, and daily all-A benchmark.
- Keep process health and investment effectiveness in separate fields.

Typical outputs:
- `data/strategy_backtests.json` (`artifact_kind=candidate_snapshot`, no empty curve)
- `strategy_research/short_track/{daily,ledger,portfolio_daily}`

Blocking rules:
- Require Stage 1 success.
- Do not publish “full success” if `latest_trade_date` lags the required trade date.
- A healthy run is not evidence that a strategy is effective.

### Stage 4: AI Enrichment and Publish

Goal: enrich selected outputs with AI analysis, validate quality, and publish.

Responsibilities:
- Enrich only the retained control candidate set.
- AI may explain evidence and flag risk, but must keep `rank_change=0`; it may
  not add, remove, or reorder candidates.
- Apply primary model, then fallback models if quality gates fail.
- Validate field completeness, labels, enum values, and text quality.
- Publish report artifacts.
- Push to Git remote only after validation passes.

Typical outputs:
- enriched `data/strategy_backtests.json`
- `data.json`
- publish logs / validation logs

Blocking rules:
- If AI enrichment fails, choose one of:
  - retry with fallback model
  - publish degraded/basic version
  - fail closed
- Decide this from configuration, not ad hoc judgment.

## Recommended Cron Design

Prefer four smaller crons over one large cron.

Suggested schedule pattern:
- After close: Stages 1–3 and short-track snapshot generation.
- After the quantitative list is frozen: Stage 4 AI explanation/publish.
- Before the next open: run `event_quality_drift_v1` for yesterday's new
  announcements (including weekend announcement dates).
- Daily/weekly research jobs for archived strategies must remain disabled.

Adjust times to actual market close and provider latency.

## Fallback Design

Implement fallback by **layer**, not by a single global switch.

### Data-provider fallback

Use provider priority lists by data type.

Recommended layers:
- Trade calendar
- Base quotes / daily bars
- Technical factors
- Chip factors
- News / event / industry enrichment

Preferred behavior:
1. Try primary provider.
2. Try ordered fallbacks.
3. If no provider satisfies required quality, apply downgrade rules or stop.

### Model fallback

Configure different model chains for different tasks.

Recommended separation:
- Orchestrator model chain
- AI enrichment model chain
- Summary model chain

Preferred behavior:
1. Run primary model.
2. Validate output quality.
3. If validation fails, retry with next fallback model.
4. If all fail, degrade output or fail according to policy.

## Downgrade Policy

Support these output levels.

### Level A: Full
- Full data available
- Retained production control and observation books available
- AI enrichment passed
- Publish full report

### Level B: Enhanced Degraded
- Base data available
- Non-chip strategy outputs available
- AI enrichment partial or fallback-generated
- Publish with degraded marker

### Level C: Basic Safe Output
- Base selection available
- No deep AI enrichment
- Publish compact report only

### Level D: Fail Closed
- Trade date inconsistent
- Core data missing
- Validation fails on hard gates
- Do not publish official result

## Medium-Term Event Track

`event_quality_drift_v1` is the only active medium-term research strategy.

- Process only announcements newly visible on their announcement date.
- Enter at the next exchange open; use 20 trading days as primary holding and
  40 days as auxiliary evidence.
- Score announcement change (35%), improvement/turnaround (25%), quality
  (20%), and valuation protection (20%) after industry/size neutralization.
- Limit the persistent book to 20 stocks, 7.5% per stock, and 25% per industry.
- Missing historical revision chains force `auxiliary_only`; they can never be
  used for promotion.
- Research replay covers 2021–2024, frozen validation is 2025, and final
  historical testing is 2026. Replaying the same input must reproduce the same
  immutable snapshots.

## Recommendation Contract

Every recommendation must carry immutable strategy identity/version, signal
cutoff, planned entry, holding period, data sources/proxy flag/completeness,
cost and benchmark, settlement status (`pending`, `settled`, `data_missing`),
nullable return fields, and AI evidence time/risk/explanation with
`rank_change=0`. Future-dated AI backfills are permanently excluded.

## Health Gates

Before each stage, evaluate machine-readable health.

Minimum gate checks:
- effective trade date resolved
- required files exist
- all required files match expected date
- provider source used for each dataset
- degraded mode flags
- prior stage success marker present

Prefer storing a JSON status file per stage.

## Validation Rules

At minimum, validate:
- `latest_trade_date`
- retained strategy count
- each retained strategy `top20` length
- required AI fields
- explicit label presence if using structured investment cards
- enum legality
- numeric range legality
- git push evidence if publication is required

Hard-fail examples:
- trade date mismatch
- `top20 != 20` when 20 is required
- missing mandatory AI fields in full mode
- publish requested but git push failed

## Operational Troubleshooting

When an operator reports “the cron ran successfully” but the day’s result is missing, do not stop at scheduler status.

Use this triage order:
1. Check scheduler status (`last_run_at`, `last_status`) to confirm the cron fired.
2. Check the pipeline/business-layer success marker such as `orchestrator_run.json` or stage status JSON. This is the real source of truth for whether a full run closed successfully.
3. Compare the latest successful `trade_date` in the business-layer marker against today’s expected trade date.
4. Inspect Stage 1 required artifacts for that date:
   - `daily_YYYYMMDD.parquet`
   - `stk_factor_YYYYMMDD.parquet`
   - `cyq_perf_YYYYMMDD.parquet`
   - `health/data_ready_YYYYMMDD.json`
5. If only some artifacts exist, treat the run as incomplete even if cron says `ok`.

Important diagnostic rule:
- “cron last_status = ok” only means the scheduler command returned without crashing.
- It does NOT prove the stock-selection business flow reached publishable state.
- The publishability gate is the stage outputs plus the latest successful run marker.

Common failure pattern worth checking first:
- `daily_YYYYMMDD.parquet` exists
- `stk_factor_YYYYMMDD.parquet` exists
- `cyq_perf_YYYYMMDD.parquet` is missing
- Stage 1 then blocks downstream stages, so the latest complete run remains on the previous trade date.

In that situation, report the issue as:
- not “cron failed”
- but “business pipeline did not close for today because Stage 1 artifact(s) were incomplete”

## Implementation Notes

Prefer the following file layout when operationalizing this skill:
- scripts for deterministic stage entrypoints
- references for config schema and validation rules
- explicit JSON logs for stage status

When asked to implement this system:
1. Read `references/config-schema.md` first.
2. Map the current pipeline into the 4-stage model.
3. Externalize provider/model priority into config.
4. Add stage-level status outputs.
5. Add validation before publish.
6. Only then rewrite cron jobs.

## Resources

### references/
- `config-schema.md`: suggested config structure for providers, models, schedules, and downgrade rules.
- `stage-contracts.md`: inputs, outputs, and success criteria for each stage.

If building the system for real execution, read both files before editing cron or pipeline code.
