# Phase 3 Execution Backbone Implementation Plan

> **For agentic workers:** Implement this plan task by task. Unified tests, migration execution, builds, and deployment checks are explicitly deferred by the owner.

**Goal:** Complete the M07–M09 execution path so a queued Run is consumed idempotently, admitted under its plan limit, executed through deterministic assertions and an isolated Browser Worker, and observable through durable Run APIs and SSE.

**Architecture:** PostgreSQL remains authoritative for Run state, leases, events, Outbox, and processed-event deduplication. Celery delivery is at least once; each consumer commits its business effect and `processed_events` row atomically, and every Worker write is fenced by `attempt_token`. LangGraph checkpoints contain resumable metadata only, while Browser Gateway and the separately deployed Playwright Worker own browser actions and enforce scoped sessions and egress boundaries.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Celery, LangGraph/Postgres checkpointer, Node.js, Playwright, React, TypeScript, SSE.

**Spec:** `docs/PLAN.md`, `docs/TestPilot_V10.3.docx` chapters 41–50, `docs/traceability-matrix.md`, and `docs/decisions/phase-2-open-issues.md`.

## Global Constraints

- Preserve all 101 baseline operationIds; Run APIs retain their registered paths and DTOs.
- PostgreSQL transactions own state transitions, leases, durable events, Outbox, and consumer deduplication.
- Delivery is at least once; duplicate messages must not repeat a committed business effect.
- Only deterministic assertions can produce PASS/FAIL; Agents cannot rewrite expected assertions.
- A stale lease or `attempt_token` cannot write steps, events, or Run state.
- Never automatically replay an unknown non-idempotent action; move it to manual reconciliation.
- Plan admission enforces the persisted owner-approved `serial/1` default or configured bounded cap.
- Browser sessions are unique by `(run_case_id, browser, attempt_token)` and Worker traffic is mTLS plus a short-lived job scope.
- Do not execute tests, migrations, image builds, or deployments in this implementation pass; record them for unified validation.
- Execute both the owner-approved DSL v2 and the V10.3 chapter 44 DSL v3 by schema version; never rewrite published assertions.

## Review Focus

- Duplicate `RUN_REQUESTED` delivery: one committed Run attempt and one `processed_events` row.
- Plan concurrency race: concurrent admission claims never exceed the configured cap.
- Lease expiry during a non-idempotent action: old tokens are rejected and the action is not replayed automatically.
- SSE cursor below retention: return `409 SNAPSHOT_REQUIRED`; valid cursors replay in increasing database event order.
- Browser session/action scope and blocked private, loopback, link-local, metadata, IPv6, redirect, and DNS-rebinding destinations.

---

### Task 1: Durable Run lifecycle, event log, and admission consumer

**Files:**
- Modify: `apps/api/app/runs/persistence.py`
- Modify: apps/api/app/cases/worker.py (reuse the existing Celery Outbox dispatcher and Beat process)
- Modify: `apps/api/app/cases/worker.py`
- Modify or create candidate migration: `db/migrations/012_phase3_execution.sql`

**Interfaces:**
- `RunRepository.transition(run_id, expected_status, new_status, attempt_token, payload, now)` performs a conditional state update and appends an `agent_events` record in the same transaction.
- `RunOutboxConsumer.consume(event_id, now)` performs `RUN_REQUESTED` deduplication and admission in one PostgreSQL transaction; `RunAdmissionRepository.claim` accepts that transaction's SQLAlchemy session.
- Capacity-blocked Runs stay `QUEUED` and are reconsidered by a bounded queue poll; admitted Runs are dispatched with their current fencing token.

- [x] Candidate source: append-only Run state/event operations and attempt-token-fenced heartbeats/completion; unified validation pending.
- [x] Candidate source: RUN_REQUESTED consumer dispatch with atomic processed_events deduplication and at-least-once recovery; unified validation pending.
- [x] Candidate source: reconcile expired leases, invalidate old tokens, and route unsafe unknown actions to manual reconciliation; unified validation pending.

### Task 2: Run API and authorized event replay

**Files:**
- Create: `apps/api/app/runs/routes.py`
- Modify: `apps/api/app/main.py`
- Modify: `apps/api/app/runs/persistence.py`
- Modify: `api/openapi.yaml` only if an implementation/schema parity correction is required by V10.3.

**Interfaces:**
- `create_run_router(repository, auth_sessions)` provides registered Run create/read/list/control/steps/events/SSE-ticket operations.
- Run access resolves project scope before exposing existence or event data.
- SSE ticket issuance stores only a hash, is short-lived and single-use, and binds to one Run/user.
- Event replay uses `agent_events.id`, emits `text/event-stream`, and returns `409 SNAPSHOT_REQUIRED` for an expired cursor.

- [x] Candidate source: authorized Run create/get/list and pause/resume/cancel/retry with persisted transitions; HTTP validation pending.
- [x] Candidate source: event/step APIs and single-use SSE tickets with sensitive payload filtering; HTTP/SSE validation pending.
- [x] Candidate source: server Run snapshots remain authoritative and event IDs provide ordered progress; browser recovery validation pending.

### Task 3: Versioned deterministic DSL executor and M07 graph boundary

**Files:**
- Create: `apps/api/app/runs/executor.py`
- Create: `apps/api/app/runs/agent.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/api/app/runs/worker.py`

**Interfaces:**
- `DeterministicExecutor.execute_step(run_case, step, original_assertions, gateway, attempt_token)` returns a typed action result and deterministic assertion result.
- `build_run_graph(checkpointer, executor, planner, explorer)` uses bounded state with `schema_version`, `last_completed_action_id`, `attempt_token`, and `budget_left`.
- Planner/Explorer outputs are validated structured proposals and cannot mutate original expected assertions.

- [x] Candidate source: dispatch DSL v2/v3 by stored schema version and use deterministic assertions; contract validation pending.
- [x] Candidate source: use LangGraph Postgres checkpoints with metadata-only state; recovery validation pending.
- [x] Candidate source: wire budget-gated structured Planner/Explorer calls to the configured model and bounded Worker page observations; a Run-wide call allowance is transactionally consumed and survives lease recovery. Proposal validation prevents changing published action/assertion IDs and locators. Unified validation pending.
- Runtime settings: `TESTPILOT_RUN_AGENT_MODEL`, `TESTPILOT_LLM_API_KEY` (or `OPENAI_API_KEY`), and optional `TESTPILOT_LLM_BASE_URL`; model calls are disabled when the plan's `agent_budget` is zero.

### Task 4: Scoped Browser Gateway and independent Playwright Worker

**Files:**
- Modify: `apps/browser_gateway/server.mjs`, `apps/browser_gateway/package.json`, `apps/browser_gateway/README.md`
- Modify: `apps/browser_worker/server.mjs`, `apps/browser_worker/package.json`, `apps/browser_worker/README.md`
- Create: `apps/browser_worker/worker_dns.mjs`
- Modify: `apps/browser_worker/compose.yaml`
- Create: `apps/browser_worker/configure-egress-acl.sh`
- Create: `apps/browser_worker/Dockerfile`
- Modify: `apps/api/app/runs/agent.py`

**Interfaces:**
- Gateway accepts validated action envelopes and verifies the session/run-case/browser/attempt-token scope before forwarding over mTLS.
- Worker owns Playwright contexts and action-id result records; session identity is `(run_case_id, browser, attempt_token)`.
- Worker registration, readiness, drain, session creation/action/close, and evidence references remain independent from the API process.

- [x] Candidate source: independent Playwright browser sessions and action lifecycle; health probing remains separate.
- [x] Candidate source: request/job scope, action timeouts, bounded sessions, TTL, drain, cleanup, mTLS Worker registration/heartbeats to every configured Gateway origin, readiness discovery, least-loaded dispatch, and worker-ID session affinity; lifecycle validation pending.
- [x] Candidate configuration: Compose CPU, memory, PID, read-only filesystem, tmpfs, dropped capabilities, and no-new-privileges limits; image/runtime validation pending.
- [x] Candidate configuration: resolve target hosts through an explicitly configured IPv4 DNS server; Compose requires the same server for the Worker network.
- [x] Candidate configuration: add a Docker `DOCKER-USER` ACL script for the dedicated Worker subnet, approved DNS resolver, same-subnet Gateway registration, public HTTP/HTTPS, and private/special-use destination blocks. Its managed chain can be rebuilt without removing the active jump. It has not been applied.
- [ ] Provision and rotate the Gateway/Worker certificates; deployment and security validation pending.

### Task 5: Run Console and SSE recovery

**Files:**
- Create: `apps/web/src/RunConsolePage.tsx`
- Create: `apps/web/src/run-console.css`
- Modify: `apps/web/src/App.tsx`, `apps/web/src/api.ts`, and `apps/web/src/styles.css`

**Interfaces:**
- The console reads an authoritative Run snapshot, then de-duplicates ordered SSE events by database ID and filters by Run/attempt token.
- On reconnect or `SNAPSHOT_REQUIRED`, it refreshes the snapshot and obtains a new ticket before resuming.

- [x] Candidate source: Run list/detail, controls, step/browser progress, errors, connection state, and event replay; frontend validation pending.
- [x] Candidate source: snapshot-authoritative Run state and no reconnect after terminal status; browser validation pending.

### Task 6: Traceability and unified-validation handoff

**Files:**
- Modify: `docs/PLAN.md`
- Modify: `docs/traceability-matrix.md`
- Modify: `docs/TestPilot_AI_V10.3_验证记录.txt`
- Modify: `docs/decisions/phase-2-open-issues.md`

- [x] Update implementation status to distinguish source code from verified acceptance evidence.
- [x] Record unrun tests, migration, build, security, and deployment checks under unified validation; no unobserved PASS is claimed.

## Execution ledger — 2026-10-27

Ruling: Reuse the existing apps/api/app/cases/worker.py Celery app for Run event consumption, queue admission, execution dispatch, and lease recovery instead of introducing a second broker/Beat entry point. This keeps M04–M06 and M09 Outbox publication under one configured worker. Cost if wrong: Run traffic shares the existing worker pool until deployment scaling is split.

Ruling: TDD test creation and execution, migration execution/validation, builds, image builds, browser flows, and deployment checks are deferred to the owner-directed unified validation phase. No test files were added or run in this pass.

Status: Candidate source and deployment configuration are present for Tasks 1–6, including Planner/Explorer provider/page-observation wiring, multi-Gateway Worker registration, session affinity, explicit IPv4 target DNS, container limits, and a host egress ACL script. Certificate provisioning, applying the ACL, and all acceptance evidence remain open.
