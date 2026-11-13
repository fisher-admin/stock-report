# Hermes Stock System Hardening Implementation Plan

> **For Codex:** Execute this plan task by task. Use test-driven development for code changes and run verification commands before reporting completion.

**Goal:** Make the Hermes stock-selection system publish truthful public state, finish post-publish research within its deadline, notify failures promptly, and remain reproducible and recoverable from a clean checkout.

**Architecture:** Keep source artifacts conservative (`published=false`) and stamp only the deployable Pages artifact as published. Replace repeated DataFrame index lookups in short-track settlement with a direct price map and cache repeated benchmark returns. Deliver post-publish failures through the existing Hermes notification route, version all runtime dependencies, and restart gateways only after an idle check.

**Tech Stack:** Python 3, pytest/unittest, pandas, Bash, GitHub Actions/Pages, Hermes cron and SQLite execution ledger.

---

## Task 1: Record the public-artifact publication contract

**Files:**
- Modify: `stock-report/tests/test_pages_artifact.py`
- Modify: `stock-report/scripts/build_pages_artifact.py`
- Modify: `stock-report/.github/workflows/pages-cd.yml`
- Modify: `stock-report/docs/ARCHITECTURE.md`

1. Add a regression test which builds with `published_artifact=True` and expects the output manifest to contain `published: true`, `publish_state: published`, and both system-verdict publication flags to be true.
2. Run the focused test and confirm it fails because the flag is not implemented.
3. Add the optional function argument and `--published-artifact` CLI flag. Stamp only the copied output manifest before verdict reconciliation.
4. Pass the flag only in the Pages deployment workflow; leave the public-CI and local default unchanged.
5. Update the architecture document and run all public-boundary tests.

## Task 2: Make the runtime bundle self-contained

**Files:**
- Modify: `skills/stock-system-orchestrator/scripts/test_sync_public_orchestrator.py`
- Modify: `skills/stock-system-orchestrator/scripts/sync_public_orchestrator.py`
- Add: `skills/stock-system-orchestrator/scripts/qfq_price_fallback.py`
- Add: `skills/stock-system-orchestrator/scripts/production_release_gate.py`
- Add: the associated five regression test files identified in the audit
- Add/modify synchronized copies under `stock-report/system/src/orchestrator/`

1. Add a test requiring `qfq_price_fallback.py` in the public synchronization allowlist and output.
2. Run it and confirm failure.
3. Add the dependency to the allowlist and run the synchronization script.
4. Run import and parity tests from a clean temporary copy of the tracked file set.

## Task 3: Remove the measured short-track bottleneck

**Files:**
- Modify: `skills/stock-system-orchestrator/scripts/test_shadow_portfolio_evaluator.py`
- Modify: `skills/stock-system-orchestrator/scripts/shadow_portfolio_evaluator.py`
- Modify: `skills/stock-system-orchestrator/scripts/test_short_track_shadow_runner.py`
- Modify: `skills/stock-system-orchestrator/scripts/test_post_publish_research_gate.py`
- Modify: `skills/stock-system-orchestrator/scripts/run_post_publish_research.py`

1. Add a failing test asserting normalized prices are a direct `(code, date)` lookup and retain duplicate validation.
2. Add a failing test asserting identical benchmark windows are calculated once per settlement cohort.
3. Implement the price lookup and per-cohort benchmark cache without changing return calculations.
4. Add a failing orchestration test showing the no-op `shortTrackEvaluation` stage must not exist, then remove that stage.
5. Run focused tests, the full evaluator suite, and a timed real-data run for trade date `20260904`; require completion under 600 seconds with successful artifacts.
6. Run the full post-publish research gate for the same date and verify every stage passes.

## Task 4: Make research failures visible

**Files:**
- Modify: `scripts/stock/post_publish_research.sh`
- Modify: the Hermes cron job through its supported CLI/configuration interface
- Add or modify: focused wrapper/cron tests where available

1. Inspect Hermes delivery behavior and capture the current job definition and notification destination.
2. Add a wrapper test: success must produce no delivery text; failure must preserve a concise actionable error and nonzero exit.
3. Implement the quiet-success wrapper behavior.
4. Route the post-publish job to the existing stock notification channel and change event-quality drift to weekdays only.
5. Run a harmless injected-failure exercise and confirm one failure notification is generated; confirm a success dry run is silent.

## Task 5: Finalize Hermes execution recovery

**Files:**
- Modify: `/Users/fisher/.hermes/hermes-agent/cron/executions.py`
- Modify: `/Users/fisher/.hermes/hermes-agent/tests/cron/test_execution_ledger.py`

1. Run the required repository test wrapper against the execution-ledger tests.
2. Inspect active executions and postpone restarts if any relevant job is running.
3. Commit the exact ledger files.
4. Restart the stale news gateway and any other confirmed stale Hermes service through their normal service commands.
5. Verify process start times, gateway health, ledger schema, and a safe recovery probe.

## Task 6: Commit and deploy only the intended changes

**Files:**
- Commit the exact stock-system source and test files changed by this repair.
- Commit and push the exact `stock-report` workflow, build, documentation, test, and synchronized-source files.
- Preserve unrelated dirty files, especially generated stock data and `stock-report/data/latest/prebreakout_factory_watch.json`.

1. Review each intended diff and check for secrets, generated data, and unrelated changes.
2. Run the complete stock script suite, end-to-end suite, public-boundary suite, and Hermes canonical tests.
3. Create narrowly scoped commits using explicit path lists.
4. Push the public-site commit, wait for Pages deployment, and read the live manifest and verdict back over HTTPS.
5. Ask for an independent code review and address any material findings.

## Task 7: Trading-day observation

1. Observe the next two trading-day main runs, Pages deployments, and post-publish research runs.
2. Record duration, publication state, research result, and notification behavior in the task diary.
3. If both days pass, archive the task and report completion. If either fails, continue diagnosis and repair in this same task.
