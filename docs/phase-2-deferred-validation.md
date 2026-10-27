# Phase 2 deferred validation and contract register

Status: implementation is in progress. Per owner direction, implementation proceeds before this validation pass. Nothing listed as deferred below is verified by this document. Candidate migrations 008–011 remain unapplied; no Phase 2 test, build, migration, browser, or deployment command has been run.

## Unified validation is deferred until M04–M06 and dependent Run execution are implemented

Do not mark Phase 2 accepted until every applicable command below has actually run and its output/evidence is attached. A missing external service must be recorded as blocked, never as passed.

The remaining implementation dependencies include M09 execution/consumption of `RUN_REQUESTED` and active-Run concurrency enforcement, plus M04 provider key/pricing configuration. M05 Diff and duplicate route behavior, and M06 alias/audit/policy handling now have candidate code and matching OpenAPI/UI contracts; their agreement with V10.3 remains a validation task. Run the unified validation below only after dependent code paths are implemented; the project verification log `docs/TestPilot_AI_V10.3_验证记录.txt` tracks the backlog alongside Phase 1 evidence.

### Contract and migration validation

- Reconcile all M04–M06 OpenAPI operationIds and request/response/status/auth/idempotency details against `docs/TestPilot_V10.3.docx`; preserve all baseline operationIds.
- Validate the OpenAPI document and compare runtime registration with the baseline inventory: `python -m openapi_spec_validator api/openapi.yaml` and `python -m apps.api.scripts.report_runtime_api_coverage`.
- Regenerate `docs/api-runtime-coverage.md`; its current count snapshot predates the M04–M06 route additions and must not be used as current coverage evidence.
- Review migration provenance/order and run read-only migration checks: `python -m apps.api.scripts.migrate --check`.
- On disposable PostgreSQL 15 and 18 databases, apply the complete migration chain with the migration runner, inspect schema constraints/indexes, exercise legacy populated upgrade, and rehearse rollback/recovery. Do not use the persistent `testpilot` database for destructive fixtures.
- Specifically validate candidates `008_case_version_review.sql`, `009_generation_and_scheduling.sql`, `010_run_browser_and_environment_pins.sql`, and `011_verification_email_outbox.sql` against empty databases and populated fixtures with historical case versions, approvals, Runs/RunCases, and active/pending users; verify old RunCase references remain unchanged, historical browser values stay unknown/NULL, and mail tokens are encrypted at rest.

### Backend/API and deterministic behavior

- Add and run repository, HTTP contract, authorization, and concurrency coverage for M04 generation lifecycle; M05 version/approval/publish/rollback/archive/diff; M06 suite/plan/schedule lifecycle.
- Run `python -m compileall -q apps/api tests` and the focused Phase 2 API suite after it is added. Run the complete backend suite before acceptance.
- Verify deterministic schema validation rejects malformed DSL and never marks a draft runnable; an Agent/provider cannot determine PASS/FAIL.
- Verify immutable version history, rollback pinning, approval separation-of-duties as specified, tenant isolation, idempotent schedule triggers, duplicate Beat delivery, DST behavior, 60-second lateness boundary, Run/browser-matrix expansion, and atomic environment-snapshot/Run persistence using the frozen policies.
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

- M04 route aliases and accept/cancel/retry operation semantics; configured Worker/provider credentials and per-model pricing, prompt/schema versioning, cost source, cancellation races, and queue/worker metrics. Three automatic retries with 2/4/8-second backoff, retry counters/lease fencing, and `UNCLASSIFIED` human-review tags are implemented candidates but unverified. Worker/provider code exists but has not run.
- M05 validate the owner-approved strict DSL v2 against V10.3 and verify API/runtime parity (including required `title`, `dsl`, suite `name`, and publish `name` as `version_name`); canonical approve/publish/rollback/archive behavior and Diff query parameter are represented by current runtime/OpenAPI/UI candidates but still need source-document and HTTP validation.
- M06 pause/resume and enable/disable aliases, PATCH audit-reason behavior, plan policy schema (`serial`/`bounded`, default 1), five-field cron, DST, missed-fire and lateness-boundary behavior are implemented candidates, pending validation. Trigger idempotency scope/retention, Run/Outbox idempotency and failure/retry recovery, migration 010 compatibility for historical rows, exact suite/browser-matrix expansion, and pinned-version behavior remain to validate. M09 must add the active-Run concurrency enforcement; M06 currently persists and propagates the limit but does not yet gate Run execution. The trigger request/response fields have been reconciled to the V10.3 inventory (Schedule response; required `request_intent`; optional `reason`), pending HTTP contract validation.

Do not invent behavior to close these items. Keep their runtime paths explicitly unavailable or candidate-only until the V10.3 contract is reconciled.
