# Public Publication Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the 20260825 stock report to GitHub Pages and prevent future runs from being blocked by pre-deployment status reconciliation or stale compatibility data.

**Architecture:** Keep `run_manifest.published` as the post-deployment fact. The Pages artifact builder will reconcile the public status flags to that fact without treating `published=false` as a build failure. The local stock publisher will generate `market_state` before the compatibility payload so every derived document reads the same morning snapshot.

**Tech Stack:** Python `unittest`, Node.js render tests, JSON publication contracts, GitHub Actions Pages artifact/deploy workflow.

---

### Task 1: Add regression coverage for pre-deployment artifact status

**Files:**
- Modify: `tests/test_pages_artifact.py`
- Test: `tests/test_pages_artifact.py`

- [x] **Step 1: Assert the artifact status follows `run_manifest.published`**

The artifact build test must accept a valid pre-deployment manifest where `published` is false and the reconciled public status is also false. It must still assert that the artifact build succeeds and contains the allowlisted files.

- [x] **Step 2: Run the focused test and observe the current failure**

Run: `python3 -m unittest tests.test_pages_artifact.PagesArtifactTests.test_build_contains_only_sanitized_allowlisted_results -v`

Expected before the fix: failure because the current test demands `publish_ok=True` even though the manifest says `published=false`.

### Task 2: Add regression coverage for generator ordering

**Files:**
- Create or modify: `/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/test_publish_market_overview.py`
- Test: `/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/test_publish_market_overview.py`

- [x] **Step 1: Assert latest-state generation precedes unified-payload generation**

The test must import `publish_market_overview.SCRIPTS` and assert that `generate_latest_states.py` appears before `generate_unified_decision_payload.py`. This catches the stale `morning_brief` ordering regression without running live market data.

- [x] **Step 2: Run the focused test and observe the current failure**

Run: `python3 scripts/test_publish_market_overview.py`

Expected before the fix: failure because the current list runs `generate_unified_decision_payload.py` before `generate_latest_states.py`.

### Task 3: Implement the two minimal fixes

**Files:**
- Modify: `tests/test_pages_artifact.py`
- Modify: `/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/publish_market_overview.py`
- Create or modify: `/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/test_publish_market_overview.py`

- [x] **Step 1: Make the artifact test compare against the manifest**

Read `published` from the copied `run_manifest.json` and assert both public `publish_ok` fields equal that boolean. Do not set `published=true` before GitHub Pages actually deploys.

- [x] **Step 2: Reorder the publisher generator list**

Run `generate_latest_states.py` before `generate_unified_decision_payload.py`, so the compatibility payload reads the newly generated `market_state.morning`.

- [x] **Step 3: Keep the unified payload date aligned**

The generator-order test should also verify that the current morning brief and compatibility payload use the same `trade_date` when run against a temporary fixture, if the existing test harness supports it without live credentials.

### Task 4: Verify locally

**Files:**
- No additional production files.

- [x] **Step 1: Run the focused Python regression tests**

Run: `python3 -m unittest tests.test_pages_artifact -v` and `python3 scripts/test_publish_market_overview.py`.

- [x] **Step 2: Run the full Python public-boundary suite**

Run: `python3 -m unittest discover tests -p 'test_*.py' -v`.

- [x] **Step 3: Run the Node Pages rendering suite**

Run: `node tests/render.test.mjs && node tests/dual-track-render.test.mjs`.

- [x] **Step 4: Build the sanitized artifact**

Run: `python3 scripts/build_pages_artifact.py --output /tmp/stock-report-pages-site` and verify exit code 0 plus `data/latest/run_manifest.json` with `trade_date=20260825`.

### Task 5: Publish and verify online

**Files:**
- Generated publication JSON under `data/latest/` and `data/recommendation_analytics/`.

- [x] **Step 1: Commit the repository fix and generated 20260825 publication data**
- [x] **Step 2: Push `main` and monitor both `public-ci` and `pages-cd`**
- [x] **Step 3: Verify the public `run_manifest.json`, `system_verdict.json`, and morning brief report `20260825`**
- [x] **Step 4: Verify the public homepage returns the new data instead of the 20260824 snapshot**
