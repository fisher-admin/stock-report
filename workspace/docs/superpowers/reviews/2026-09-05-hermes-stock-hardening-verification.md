# Hermes stock-system repair verification

Status: September 7 follow-up repairs and full historical replay verified; two clean actual trading-day runs remain OPEN. Historical verification does not establish production acceptance.
Last updated: 2026-09-08 08:16 UTC+08:00.

## Outcome

September8 update: follow-up repair8971563 fixes an overlooked no-op factor-attribution entrypoint and repeated factory scoring. Complete four-stage historical replay passed871.709s with real artifact checks, unchanged frozen selections and preserved original scheduled failure. Details: `2026-09-07-hermes-stock-acceptance.md`. September7 did not pass actual scheduled research; now awaiting two clean trading days, expected September8/9. The following September5 results remain historical evidence, not a claim that all then-running stages were substantively validated.

The reviewed repair is installed in the existing runtime and deployed to the existing public site. It does not change frozen selections, authorize orders, or establish investment effectiveness.

The latest real-data short-track replay completed in 4.282 seconds (previously exceeded its 600-second deadline). All four strategies produced healthy tracking reports and nonempty portfolios; immutable candidate snapshot hashes were unchanged. The event-research replay completed in 14.718 seconds; a read-only stack sample of the earlier run, still consuming a CPU after more than six minutes, showed repeated pandas index construction. Its valid outcome was no eligible new announcement candidates, with existing portfolios updated and promotion gates remaining closed.

The complete four-stage historical post-publication research replay succeeded in 993.29 seconds. That replay used the matching September 4 release with a historical gate clock explicitly confined to the verification harness. It did not weaken the production current-day release gate or replace the production run summary. Final price-trust changes were additionally verified by the separate real short-track and event replays.

## Repairs and evidence

| Requirement | Verified result |
| --- | --- |
| Truthful public publication state | Only the deployable Pages copy is stamped published. Live manifest and both verdict flags are true; the source manifest stays false. |
| Same-run readiness | Public readiness includes run ID, date, mode and AI completeness from the original local readiness record. Missing or mismatched identity blocks deployment. The current public report received only the verified missing lineage metadata. |
| Reliable research runtime | Direct price lookup and per-cohort benchmark caches replace repeated DataFrame indexing in both research evaluators. No-op standalone evaluation stage removed. |
| Safe frozen-day resume | Candidate contracts, counts, configuration, duplicate rows, PIT copies and SHA256 integrity receipts checked. Partial or modified frozen artifacts fail closed. |
| Honest price basis | Raw daily prices require official factors and a valid official qfq anchor. Local technical fallback, invalid factors, unproven daily qfq and duplicate price keys are rejected. Provenance retained. |
| Honest failure state | Evaluation errors produce degraded tracking and nonzero runner exit. Stage failures retain useful error tails. |
| Failure visibility | Deployed and versioned research wrappers match; successful output is silent and injected failure preserves exit code 9 and error text. Hermes delivery tests verify failed jobs deliver once and silent successes do not deliver. No false alert was sent to the real channel during testing. |
| Scheduling | Event research: weekdays 08:15. Post-publication research: weekdays 22:15. Existing Discord stock destination retained. |
| Hermes ownership recovery | Stable process identity fix committed, canonical ledger tests pass; stale news gateway restarted and supervised by launchd, service definition matches installed code. |
| Recoverability | Exact tracked/index copies independently imported and tested; required previously untracked runtime dependencies and restored accounting tests are committed. Unrelated working changes preserved. |

## Verification runs

- Stock orchestrator scripts: 333 tests passed after the final changes.
- Stock end-to-end failure scenarios: 20 passed.
- Stock analyzer affected tests: 8 passed.
- Public Python boundary/build tests: 17 passed.
- Frontend: 77 assertions plus dual-track rendering passed.
- Hermes canonical test runner: 28 execution-ledger tests plus 11 selected scheduler/notification tests passed.
- Clean runtime copy: 174 tracked source files, 6 key modules imported without untracked dependencies, 44 focused tests passed.
- Clean public release copy: 223 tracked files, tests and final artifact build passed; 40 allowlisted public data files, no artifact audit violations.
- Source mirror parity: all 8 files match sanitized runtime source. Public source Python compiles.
- Existing unfiltered repository contents are not equivalent to the Pages artifact. Its whole-tree audit still lists pre-existing internal/result files; they were not deleted or added by this repair. The deployed allowlisted artifact passed the boundary audit. This work did not perform a repository-history privacy cleanup.

## Versions and deployment

- Runtime main repair: `db7abe1`.
- Runtime source sanitization: `137c25c`.
- Runtime final price/readiness trust fix: `4815b3b`.
- Runtime event performance fix / current repair HEAD: `13812df`.
- Hermes execution ledger: `8348e0aac18c2e7c0e90720b14499f3e48c5acdc`.
- Public repair: `d4f13b5c`, followed by portable entrypoint correction `103954143b59337c560fa1c7f979fabb65d36346`.
- Public CI: https://github.com/fisher-admin/stock-report/actions/runs/33975997278 — success.
- Pages deployment: https://github.com/fisher-admin/stock-report/actions/runs/33975997304 — success, matching `10395414`.
- Live site: https://fisher-admin.github.io/stock-report/.
- HTTPS readback at 23:51:47 confirmed run `stock-20260904T113002Z`, date `20260904`, published/publish_ok/ai_complete true, matching readiness identity.

## Reproducible evidence files

Local audit directory: `/Users/fisher/.hermes/audits/stock-hardening-20260905/`.

- `research-replay-result.json`: complete four-stage historical replay.
- `short-track-final-result.json`: final short-track timing, health, portfolio counts and unchanged snapshot hashes.
- `event-final-result.json`: final event timing, healthy-empty-result contract and portfolio count.
- `event-replay-sample.txt`: original event price-index CPU bottleneck.
- `live-release-result.json`: actual HTTPS publication verification.
- `verify_clean_runtime.py`, `verify_public_index.py`: isolated tracked-source verification.
- `verify_short_track_run.py`, `verify_event_run.py`, `verify_live_release.py`: repeatable real-data/live checks.

## Remaining acceptance criteria

September 7 follow-up: see `2026-09-07-hermes-stock-acceptance.md`. Main production and actual Pages deployment passed, but factor attribution was a no-op entrypoint and daily research exceeded its stage deadline. September 7 is NOT a clean acceptance day. Two actual clean trading-day runs are still required after the corrective changes; neither the old replay nor an isolated manual supplement counts.

The local exchange calendar confirms September 7 and 8 as the next two open days. Actual scheduled production, AI completion, Pages publication, event research, post-publication research and alert behavior must be checked for those days. Historical replays cannot satisfy this requirement.

Codex heartbeat automation `hermes` is ACTIVE in this task at 23:15 on weekdays, with instructions to remain quiet unless there is a meaningful failure, required action, or completion. It must pause itself after both days pass and only then evaluate the full goal for completion. The goal remains active; no investment-performance or automatic-trading authorization is implied.
