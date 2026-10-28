# Phase 2 deferred validation and contract register

Status: implementation is in progress. Per owner direction, implementation proceeds before this validation pass. Nothing listed as deferred below is verified by this document. Candidate migrations 008–013 remain unapplied; no Phase 2/3 test, build, migration, browser, security, or deployment command has been run for the current source follow-up.

## Unified validation remains deferred while implementation candidates and Phase 3 gaps are completed

Do not mark Phase 2 accepted until every applicable command below has actually run and its output/evidence is attached. A missing external service must be recorded as blocked, never as passed.

Phase 3 now has candidate code for `RUN_REQUESTED` consumption/admission, Run approval request/decision/expiry, budget-gated M07 Planner/Explorer calls with bounded page observations, and M08 mTLS Worker registration/discovery, session affinity, explicit IPv4 target resolution, and resource-limited Compose settings. None has end-to-end evidence yet. Remaining external/source configuration includes M04 provider key/pricing, M07 run-agent model selection, and M08 certificate provisioning plus applying the candidate host egress ACL. M05 DSL v2 remains an explicit divergence from V10.3 §44.2 DSL v3; HTTP contract checks for M05 and M06 remain deferred. Keep the unified validation below deferred until source configuration is complete; the project verification log `docs/TestPilot_AI_V10.3_验证记录.txt` tracks this backlog alongside Phase 1 evidence.

### Contract and migration validation

- Reconcile all M04–M06 OpenAPI operationIds and request/response/status/auth/idempotency details against `docs/TestPilot_V10.3.docx`; preserve all baseline operationIds.
- Validate the OpenAPI document and compare runtime registration with the baseline inventory: `python -m openapi_spec_validator api/openapi.yaml` and `python -m apps.api.scripts.report_runtime_api_coverage`.
- Regenerate `docs/api-runtime-coverage.md`; its current count snapshot predates the M04–M06 route additions and must not be used as current coverage evidence.
- Review migration provenance/order and run read-only migration checks: `python -m apps.api.scripts.migrate --check`.
- On disposable PostgreSQL 15 and 18 databases, apply the complete migration chain with the migration runner, inspect schema constraints/indexes, exercise legacy populated upgrade, and rehearse rollback/recovery. Do not use the persistent `testpilot` database for destructive fixtures.
- Specifically validate candidates `008_case_version_review.sql`, `009_generation_and_scheduling.sql`, `010_run_browser_and_environment_pins.sql`, `011_verification_email_outbox.sql`, `012_phase3_execution.sql`, and `013_run_approval_expiry_index.sql` against empty databases and populated fixtures with historical case versions, approvals, Runs/RunCases, and active/pending users; verify old RunCase references remain unchanged, historical browser values stay unknown/NULL, and mail tokens are encrypted at rest.

### Backend/API and deterministic behavior

- Add and run repository, HTTP contract, authorization, and concurrency coverage for M04 generation lifecycle; M05 version/approval/publish/rollback/archive/diff; M06 suite/plan/schedule lifecycle.
- Run `python -m compileall -q apps/api tests` and the focused Phase 2 API suite after it is added. Run the complete backend suite before acceptance.
- Verify deterministic schema validation rejects malformed DSL and never marks a draft runnable; an Agent/provider cannot determine PASS/FAIL.
- Verify immutable version history, direct-to-`PENDING_APPROVAL` creation and rollback, approval separation-of-duties, generic approval CAS and wire values, 30-minute Run approval expiry at the `expires_at <= now` boundary, 60-second/100-item expiry sweep, chained audit hashes, manual reconciliation without replay of `NEVER` actions, re-evaluation of original assertions, tenant isolation, idempotent schedule triggers, duplicate Beat delivery, DST behavior, 60-second lateness boundary, Run/browser-matrix expansion, and atomic environment-snapshot/Run persistence using the frozen policies.
- Verify Schedule PATCH requires an audit reason when `enabled` changes, records one state event atomically, and recalculates `next_fire_at` for Cron/timezone edits.
- Verify the M01 encrypted verification-email Outbox commits with registration/resend, lease expiry is recoverable, stale/consumed verification payloads are cleared, retry exhaustion is visible to platform administrators, and encryption-key rotation can decrypt outstanding messages.
- Verify generation Outbox dead-letter events are visible only to platform-admin sessions, never expose requirement text or secrets, and retain attempt/error metadata after exhausted dispatch retries.

### Frontend and browser behavior

- Run `npm --prefix apps/web run lint`, `npm --prefix apps/web run typecheck`, `npm --prefix apps/web test`, and `npm --prefix apps/web run build`.
- Run browser E2E for case creation/edit/version history/review, generation review and rejection, suite/plan editing, schedule pause/resume/trigger, and all loading/empty/permission/error/retry states.
- Capture actual browser results; source inspection or component tests alone do not satisfy browser-flow acceptance.

### Phase 2 release gates

- M04 acceptance: 100% DSL schema validity before version persistence; cancel/failure visible; actual model cost accounting; high-risk output waits for human approval.
- M05 acceptance: runs remain pinned to the original immutable version; rollback does not rewrite history; invalid DSL cannot be approved.
- M06 acceptance: duplicate Beat delivery creates one Run; every selected browser creates a distinct RunCase; the environment snapshot and published case versions are pinned atomically; DST, late-window, missed-fire, and pause/resume behavior match the approved policy.
- No deployment or production claim until the later deployment phase is complete.

## Contract items that remain open while implementation proceeds

- M04 the canonical `/projects/{id}/generations` route and deprecated `/projects/{id}/test-cases/generate` alias now share one handler; the alias, response status, and operation IDs still need V10.3 HTTP validation. Both platform-admin dead-letter APIs now have stable cursor pages. Configured Worker/provider credentials and per-model pricing, prompt/schema versioning, cost source, cancellation races, and queue/worker metrics remain open. Three automatic retries with 2/4/8-second backoff, retry counters/lease fencing, and `UNCLASSIFIED` human-review tags are implemented candidates but unverified. Worker/provider code exists but has not run.
- M05 owner decision: keep strict DSL v2 and record its divergence from V10.3 chapter 44.2 DSL v3; do not claim conformance. Source now creates versions and rollback copies directly in `PENDING_APPROVAL` with a same-transaction approval row and request audit. Validate API/runtime parity (including required `title`, `dsl`, suite `name`, publish `name` as `version_name`, bodyless version-id approval, and reasoned version-number decision); Diff parameters and other response details still need source-document/HTTP validation. Migration 008 remains unapplied.
- M06 pause/resume and enable/disable aliases, PATCH audit-reason behavior, plan policy schema (serial/bounded, default 1), five-field cron, DST, missed-fire and lateness-boundary behavior are implemented candidates, pending validation. Trigger idempotency scope/retention, Run/Outbox idempotency and failure/retry recovery, migration 010 compatibility for historical rows, exact suite/browser-matrix expansion, and pinned-version behavior remain to validate. The active-Run concurrency cap is now wired to the RUN_REQUESTED consumer and bounded queue admission scan, but must be verified end to end in unified validation. The trigger request/response fields have been reconciled to the V10.3 inventory (Schedule response; required request_intent; optional reason), pending HTTP contract validation.
- M09 approval path is present as a source candidate: Run `WAITING_APPROVAL` transitions persist generic Approval rows; the decision handler enforces project write access and requester/decider separation, writes organization-scoped chained audit entries and durable events, and queues resume through Outbox. Run approval TTL is 30 minutes with expiry at `expires_at <= now` and a 60-second, 100-item sweep; rejection/expiry fail the Run. The approval decision wire values are `APPROVE`/`REJECT`, with a required reason. Verify all behavior and the hash algorithm below during unified validation; migrations 008, 012 and 013 remain unapplied.

The owner has selected the DSL, submission, and Run approval-expiry decisions above. Keep their source marked candidate-only until unified validation; retain the DSL v2/V10.3 §44.2 divergence in release notes.

## Phase 3 unified validation handoff

- No Phase 3 tests, migration execution, build, browser flow, security check, or deployment check has been run.
- Add migrations 012–013 to disposable PostgreSQL 15/18 fresh, upgrade, checksum, constraint, and rollback validation.
- Run API/repository/outbox/concurrency/lease and DSL/checkpoint recovery coverage, including duplicate RUN_REQUESTED delivery and configured plan capacity.
- Run web lint/typecheck/unit/build and Run Console pause/resume/cancel/retry, SSE reconnect, ticket renewal, cursor expiration, permissions, and terminal-state flows.
- Build the independent Browser Worker image and exercise mTLS, scoped action/session tokens, worker drain/capacity/TTL, the configured IPv4 resolver, public and blocked network destinations, redirects, DNS rebinding, and worker-loss recovery.
- Verify Worker registration/discovery, multi-worker session affinity, runtime resource limits, and production egress ACLs; source-only controls do not establish deployment acceptance.
- Record actual commands, outputs, test counts, database versions, security limitations, and deployment evidence here and in the V10.3 validation record. Do not mark a check PASS without its observed result.
