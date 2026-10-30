# M10 Locator Recovery Implementation Plan

> **For agentic workers:** After owner review, use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover a Run from a confirmed locator-resolution failure through a human-approved, bounded single-action replay, then create a new pending-review case version without changing the pinned source version or its assertions.

**Architecture:** Keep Recovery inside the existing Run execution and persistence boundary. The Recovery provider proposes only a locator; a Worker-side verifier supplies concrete match and semantic evidence; a project-scoped Healing approval authorizes a single-use browser-session handoff to a new fenced attempt. Database state drives resume, and a successful replay plus original assertions creates a separate M05-pending immutable version.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL JSONB, LangGraph, Celery/Outbox, Node.js, Playwright, React/TypeScript, OpenAPI.

**Spec:** `docs/superpowers/specs/2026-10-30-m10-locator-recovery-design.md`

## Global Constraints

- Enter Recovery only on a Worker-confirmed pre-dispatch locator-resolution failure (`failure_phase='locator_resolution'`, `action_dispatched=false`).
- Every candidate requires a different project writer's approval; confidence ranks candidates and never replaces deterministic verification or approval.
- Verify exactly one visible target and provide a bounded role/name/label/placeholder descriptor; the reviewer must confirm it is the intended business control.
- An approved handoff may rebind only the same live Worker session, Run, RunCase, browser, old attempt, and Worker instance to a new attempt token.
- Replay only the failed action, at most twice, using an attempt-scoped action ID and the run-wide model budget/timeout; never replay an uncertain side effect.
- Recheck the original immutable assertions. Do not update the pinned case version; a successful recovery creates a new `PENDING_APPROVAL` case version.
- Expire Healing approvals after 30 minutes. Rejected, expired, cancelled, stale, or unauthorized decisions never rebind a session or enqueue a replay.
- Store only bounded redacted observation/evidence in JSONB. Screenshot artifact storage remains a required M10 completion item.
- PostgreSQL/migration and real mTLS/egress Phase 1–3 acceptance gates remain outstanding and are not satisfied by local unit tests.

## Review Focus

1. **Expired or missing Worker session at approval time:** reject handoff and use manual reconciliation; never open a fresh page and infer prior state. Verify in Task 1 and Task 4.
2. **Replayed handoff grant or stale attempt token:** reject before changing session claims or forwarding an action. Verify in Task 1.
3. **Zero, multiple, hidden, or semantically unobservable locator matches:** do not create a replayable candidate. Verify in Tasks 1 and 2.
4. **Concurrent approve/reject/expiry decisions:** allow one terminal transition and at most one resume Outbox event. Verify in Task 3.
5. **Replay succeeds but an original assertion fails:** keep the Run failed, mark the healing `CANCELLED`, and create no applied/published version. Verify in Task 4.

---

## File Map

- Modify `apps/browser_worker/browser_sessions.mjs`: locator verification, pre-dispatch result metadata, approved session rebind, attempt-scoped action deduplication.
- Modify `apps/browser_gateway/browser_gateway_sessions.mjs` and `apps/browser_gateway/server.mjs`: validate and route verification/rebind operations to the session-affined Worker.
- Modify `apps/api/app/runs/browser.py`: expose typed `verify_locator` and `rebind_session` Gateway calls.
- Modify `apps/api/app/runs/planner.py`; create `apps/api/app/runs/recovery.py`: structured locator-only proposal and deterministic evidence policy.
- Modify `apps/api/app/runs/persistence.py` and `apps/api/app/runs/repository.py`: healing ORM projection and transactional candidate, approval, replay, expiry, and completion operations. Reuse `CaseApprovalRecord` and the target columns from migration 003; do not add a schema migration unless the implementation finds a required field absent from the existing schema.
- Modify `apps/api/app/runs/executor.py` and `apps/api/app/runs/agent.py`: failure classification, Recovery graph branch, approval resume from DB state, bounded replay, and original assertion recheck.
- Modify `apps/api/app/cases/persistence.py`: add an in-session helper to create an immutable recovered version and its existing M05 Approval.
- Modify `apps/api/app/runs/routes.py`, `api/openapi.yaml`, and `docs/api-runtime-coverage.md`: scoped Healing GET/decision contracts and runtime coverage.
- Modify `apps/api/app/cases/worker.py`: 60-second Healing approval expiry sweep.
- Modify `apps/web/src/RunConsolePage.tsx`: show the pending Healing evidence and submit an explained approval/rejection.
- Create `apps/browser_worker/browser_healing_sessions.test.mjs`, `apps/browser_gateway/browser_gateway_healing_sessions.test.mjs`, `tests/test_m10_recovery_policy.py`, `tests/test_m10_healing_persistence.py`, `tests/test_m10_run_recovery.py`, and `tests/test_m10_healing_routes.py`; modify `tests/test_openapi_inventory.py`. Keep the already-uncommitted Phase 3 Worker/Gateway tests out of M10 commits.
- Modify `docs/traceability-matrix.md` and `docs/PLAN.md` to close Q-010 and record M10's source implementation plus the screenshot-storage completion gap.

## Task 1: Prove locator failures and add fenced Worker session handoff

**Files:** `apps/browser_worker/browser_sessions.mjs`, create `apps/browser_worker/browser_healing_sessions.test.mjs`, `apps/browser_gateway/browser_gateway_sessions.mjs`, create `apps/browser_gateway/browser_gateway_healing_sessions.test.mjs`; inspect `apps/browser_gateway/server.mjs` to preserve its existing worker-affinity rule.

**Interfaces:**

- Worker locator verification returns `{match_count: number, visible: boolean, descriptor: {role, name, label, placeholder}}` for one session-scoped locator.
- The Worker exposes `POST /internal/v1/browser-sessions/{session_id}/locators/verify` and `POST /internal/v1/browser-sessions/{session_id}/rebind`; Gateway paths use the same suffixes and forward only to the session-affined Worker.
- A rebind request carries a signed, single-use `recovery_handoff` grant containing `healing_id`, `session_id`, `run_id`, `run_case_id`, `browser`, `previous_attempt_token`, `attempt_token`, and `worker_instance`.
- Rebind succeeds only when the in-memory session exists and its current claims match every old-scope field; it atomically installs the new attempt claims. A missing session returns `SESSION_NOT_RESTORABLE`.
- Action deduplication key is `(attempt_token, action_id)`.

- [x] **Step 1: Add failing Worker contract tests** in `browser_healing_sessions.test.mjs` for pre-dispatch count evidence (0 and multiple matches), hidden candidate, semantic descriptor, valid rebind, old-token rejection, consumed/replayed grant rejection, missing-session rejection, and duplicate delivery within one attempt.
- [x] **Step 2: Run `node --test apps/browser_worker/browser_healing_sessions.test.mjs`** and confirm the new cases fail for the expected missing fields/routes.
- [x] **Step 3: Implement Worker behavior** in `browser_sessions.mjs`; do not expose `rebind` through ordinary user action routes. Rebind must compare old session claims before replacing them.
- [x] **Step 4: Add failing Gateway tests** in `browser_gateway_healing_sessions.test.mjs` that assert rebind/locator verification require the signed token scope and route to the assigned Worker instance.
- [x] **Step 5: Run `node --test apps/browser_gateway/browser_gateway_healing_sessions.test.mjs`** and confirm the new route/scope cases fail before implementation.
- [x] **Step 6: Implement Gateway forwarding** in `browser_gateway_sessions.mjs`. Keep `server.mjs` unchanged if its existing `canSelectAnother` rule and signed `worker_instance` already prevent rebind reassignment; assert the signed Worker identity in the Gateway contract test.
- [x] **Step 7: Run `node --test apps/browser_worker/browser_healing_sessions.test.mjs apps/browser_gateway/browser_gateway_healing_sessions.test.mjs`** and require all session, scope, and replay cases to pass.
- [x] **Step 8: Commit only Task 1 files** with message `feat: fence browser healing session handoff`.

## Task 2: Define the Recovery proposal and evidence policy

**Files:** create `apps/api/app/runs/recovery.py`; modify `apps/api/app/runs/planner.py`; create `tests/test_m10_recovery_policy.py`.

**Interfaces:**

- `RecoveryProposal`: `action_id: UUID`, `locator: dict`, `source: Literal['a11y_tree','dom','action_history']`, `confidence: float` constrained to `[0,1]`, and `semantic_descriptor: dict`.
- `RunAgentProvider.recover(state, case, failed_step, observation) -> RecoveryProposal` consumes the existing run-wide model budget and cannot return action/value/assertion edits.
- `evaluate_recovery_candidate(*, proposal, verification) -> RecoveryDecision` accepts only a DSL v2 locator, `match_count == 1`, `visible is True`, and a non-empty observed semantic descriptor; it returns an explicit reason code for every refusal.

- [x] **Step 1: Add failing policy tests** for accepted unique/visible candidates, invalid DSL v2 locators, zero/multiple matches, hidden targets, empty descriptors, invalid confidence, and provider output that attempts to change action/value/assertions.
- [x] **Step 2: Run `python -m pytest tests/test_m10_recovery_policy.py -q`** and confirm the new tests fail because the Recovery schema and policy do not exist.
- [x] **Step 3: Implement the typed proposal and pure policy evaluator** in `runs/recovery.py`; add the structured-output method to `RunAgentProvider` in `planner.py` and reject extra fields.
- [x] **Step 4: Rerun `python -m pytest tests/test_m10_recovery_policy.py -q`** and require all exact-match and refusal cases to pass.
- [x] **Step 5: Commit Task 2 files** with message `feat: add bounded locator recovery proposals`.

## Task 3: Persist Healing requests and atomic decisions

**Files:** `apps/api/app/runs/persistence.py`, `apps/api/app/runs/repository.py`, `apps/api/app/cases/persistence.py`, create `tests/test_m10_healing_persistence.py`.

**Interfaces:**

- `RunRepository.propose_healing(run_id, run_case_id, failed_step_id, *, old_locator, proposal, evidence, requester_id, now) -> dict` atomically creates a `PENDING_APPROVAL` healing row, a project-scoped `CaseApprovalRecord(target_kind='healing')`, Run/RunCase `WAITING_APPROVAL`, durable events, and audit; it returns `healing_id`, `approval_id`, and `expires_at`. It does not enqueue replay work.
- `RunRepository.get_healing(actor_id, healing_id) -> dict` derives project scope through healing step -> RunCase -> Run and returns the bounded review DTO.
- `RunRepository.decide_healing(actor_id, healing_id, *, decision, reason, now) -> dict` returns the approval/healing states and, on approval, `previous_attempt_token` plus the new `attempt_token`; it rotates the attempt and writes one resume Outbox row with those tokens. On rejection it fails the waiting Run and creates no resume row.
- `RunRepository.load_approved_healing(run_id, run_case_id, attempt_token, *, worker_id) -> dict | None` loads only the approved recovery bound to the active fenced Run attempt.
- `RunRepository.complete_healing(healing_id, run_id, run_case_id, attempt_token, *, worker_id, now) -> dict` creates the recovered case version and marks the healing `APPLIED` in one transaction after the original assertions pass.
- `RunRepository.expire_due_healing_approvals(*, now, limit=100) -> int` locks due healing approvals, expires the healing and Run, and creates no resume row.
- `CaseRepository.create_recovered_version_in_session(session, *, source_version_id, action_id, new_locator, version_author_id, now) -> TestCaseVersionRecord` clones the pinned DSL, changes only the matching locator, and creates the existing M05 `case_version` Approval in the caller's transaction. `complete_healing` passes the original Run requester as `version_author_id`; a different project writer must review it.

- [x] **Step 1: Add failing repository tests** for atomic proposal creation, requester/decider separation, current project authorization, stale/duplicate decisions, concurrent decision serialization, expiry, one resume intent, and the recovered version preserving all source fields except the one locator.
- [x] **Step 2: Run `python -m pytest tests/test_m10_healing_persistence.py -q`** and confirm failures on the absent repository methods.
- [x] **Step 3: Add the HealingRecord ORM projection** and transactional methods using the existing `healing_records` columns and `CaseApprovalRecord`; preserve stable lock order Run -> Healing -> Approval.
- [x] **Step 4: Add the in-session version helper** to `CaseRepository`; do not publish the new version or rewrite the source version.
- [x] **Step 5: Rerun the repository tests** and require terminal-state, Outbox, expiry, and immutable-version assertions to pass.
- [x] **Step 6: Commit only Task 3 files** with message `feat: persist healing approvals and recovery versions`.

## Task 4: Connect Recovery to the fenced Run graph

**Files:** `apps/api/app/runs/browser.py`, `apps/api/app/runs/executor.py`, `apps/api/app/runs/agent.py`, `apps/api/app/runs/repository.py`, `apps/api/app/runs/persistence.py`, `db/migrations/015_m10_replay_cancellation.sql`, create `tests/test_m10_run_recovery.py`, extend `tests/test_m10_healing_persistence.py` and `tests/test_migration_runner.py`.

**Interfaces:**

- `BrowserGatewayExecutionClient.verify_locator(..., locator) -> {match_count, visible, descriptor}` uses the current attempt-scoped session.
- `BrowserGatewayExecutionClient.rebind_session(..., healing_id, previous_attempt_token, attempt_token, session_id, worker_instance) -> dict` sends the single-use grant only after the repository returns an approved healing for that current attempt.
- The executor emits a Recovery-eligible result only for `error_code == 'LOCATOR_NOT_UNIQUE'`, `failure_phase == 'locator_resolution'`, and `action_dispatched is False`.
- `RunGraphState` carries persisted `healing_id`, failed action index, and retry count. On approval, the initial cursor comes from the DB record and points to the failed action; completed earlier steps are not replayed.

- [x] **Step 1: Add failing graph/executor tests** proving only confirmed pre-dispatch locator failures enter Recovery; timeouts and transport failures do not; proposal verification and persistence park the Run; approved resume rebinds the same session and starts at the failed action; lost session enters manual reconciliation; attempt count stops at two.
- [x] **Step 2: Run `python -m pytest tests/test_m10_run_recovery.py -q`** and confirm the Recovery route and resume behavior fail before implementation.
- [x] **Step 3: Extend the Gateway client** with verification and approved rebind methods, passing current Run/RunCase/browser/worker scopes.
- [x] **Step 4: Add failure classification and the Recovery graph branch**; persist before yielding `WAITING_APPROVAL`; read approved recovery input from repository state on resume, not from checkpoint-only data.
- [x] **Step 5: Rerun only the failed action** with a UUID derived from `(failed_action_id, healing_id, replay_index)`; persist its step result and candidate linkage. On further confirmed locator failure, cancel that candidate and permit one more approved candidate; otherwise terminate without replay on uncertainty.
- [x] **Step 6: Recheck the original immutable assertions** and call `create_recovered_version_in_session` only after all pass. If replay or any original assertion fails, mark the candidate `CANCELLED`, retain evidence, fail the Run, and create no applied version.
- [x] **Step 7: Rerun the graph tests** and require both approved-success and every fail-closed branch to pass.
- [x] **Step 8: Commit only Task 4 files** with message `feat: recover confirmed locator failures in runs`.

## Task 5: Expose Healing review and decision contracts

**Files:** `apps/api/app/runs/routes.py`, `api/openapi.yaml`, `docs/api-runtime-coverage.md`, `docs/traceability-matrix.md`, create `tests/test_m10_healing_routes.py`.

**Interfaces:**

- `GET /api/v1/healing-records/{healing_id}` calls `get_healing` and returns locator pair, source, confidence, bounded evidence/descriptor, approval state, expiry, and optional resulting version ID. It returns the same scoped 404 behavior used by current tenant routes.
- `POST /api/v1/healing-records/{id}/decision` accepts the existing `APPROVE`/`REJECT` plus required `reason` schema and calls `decide_healing`.
- Every repeated decision after terminal state returns HTTP 409; it cannot write another Outbox event.

- [x] **Step 1: Add failing HTTP tests** for scoped GET, evidence response, valid approve/reject, empty reason, missing project permission, requester self-approval, expired request, and duplicate decision conflict.
- [x] **Step 2: Run `python -m pytest tests/test_m10_healing_routes.py tests/test_openapi_inventory.py -q`** and confirm route/schema coverage failures.
- [x] **Step 3: Implement the two routes** using existing authentication/session dependencies and map repository outcomes to existing problem-detail responses.
- [x] **Step 4: Extend `HealingRecord` in OpenAPI** with evidence, source, approval/expiry, `new_version_id`, and `applied_at`; preserve operation IDs and the decision body.
- [x] **Step 5: Update runtime coverage** to show both healing operations are implemented; resolve Q-010 as `decision` with internal apply semantics.
- [x] **Step 6: Rerun route and inventory tests** and require runtime/OpenAPI operation IDs and response schemas to agree.
- [x] **Step 7: Commit only Task 5 files** with message `feat: expose scoped healing review endpoints`.

## Task 6: Surface pending Healing in the Run Console

**Files:** `apps/web/src/RunConsolePage.tsx`, `apps/web/src/RunConsolePage.test.tsx` (new), and `apps/web/src/run-console.css` if presentation needs local styling.

**Interfaces:**

- A Run event with `healing_id` opens a scoped Healing review card loaded from the GET route.
- The card displays failed action, old/proposed locator, verified target descriptor, source/confidence/evidence, and expiry. Approve/reject requires a non-empty reason and calls the decision route once.
- Terminal decisions disable both actions and render the resulting approval state.

- [x] **Step 1: Add failing tests** in `RunConsolePage.test.tsx` for a `run.healing_requested` event loading the review DTO, non-empty decision reason, POST body shape, terminal state disabling both actions, and HTTP 409 display.
- [x] **Step 2: Run `pnpm --dir apps/web exec vitest run src/RunConsolePage.test.tsx`** and confirm the new review cases fail before the UI exists.
- [x] **Step 3: Add `run.healing_requested` handling** and implement the review card using current Run Console event/reload and `api()` patterns; do not add a separate design system or broad navigation surface.
- [x] **Step 4: Run `pnpm --dir apps/web exec vitest run src/RunConsolePage.test.tsx`** and require the interaction tests to pass.
- [x] **Step 5: Run `pnpm --dir apps/web build`** and require TypeScript/Vite build success.
- [x] **Step 6: Commit only Task 6 files** with message `feat: review locator healing in run console`.

## Task 7: Expire approvals and record M10 coverage

**Files:** `apps/api/app/cases/worker.py`, `apps/api/app/runs/repository.py`, `tests/test_m10_healing_persistence.py`, `docs/traceability-matrix.md`, `docs/PLAN.md`.

- [x] **Step 1: Add `test_due_healing_approval_expiry_is_atomic_and_never_resumes`** proving a due Healing Approval expires with its Healing record and Run and emits no resume event.
- [x] **Step 2: Run `python -m pytest tests/test_m10_healing_persistence.py -k due_healing_approval_expiry -q`** and confirm the periodic task is absent.
- [x] **Step 3: Add a Beat entry every 60 seconds** and a bounded task invoking `expire_due_healing_approvals(now, limit=100)`; keep each decision/expiry transaction idempotent.
- [x] **Step 4: Update traceability and Phase 4 status**: close Q-010, list implemented API/Worker/Run/UI surfaces, and leave screenshot artifact storage plus real PostgreSQL/mTLS acceptance open.
- [x] **Step 5: Run the focused persistence/expiry tests** and require no late approval or duplicate resume path to pass.
- [x] **Step 6: Commit only Task 7 files** with message `feat: expire pending healing approvals`.

## Task 8: Unified M10 source validation

- [x] **Step 1: Run `python -m pytest tests/test_m10_recovery_policy.py tests/test_m10_healing_persistence.py tests/test_m10_run_recovery.py tests/test_m10_healing_routes.py tests/test_openapi_inventory.py -q`**; require all tests to pass.
- [x] **Step 2: Run `node --test apps/browser_worker/browser_sessions.test.mjs apps/browser_worker/browser_healing_sessions.test.mjs apps/browser_gateway/browser_gateway_sessions.test.mjs apps/browser_gateway/browser_gateway_healing_sessions.test.mjs`**; require all Phase 3 and M10 Worker/Gateway boundary cases to pass.
- [x] **Step 3: Run the frontend build specified in Task 6** and confirm no generated API/type mismatch.
- [x] **Step 4: Review `git diff` and the M10 acceptance evidence**; confirm the source DSL/assertions are unchanged and only the new version is pending approval.
- [x] **Step 5: Report the screenshot-store gap and the separate Phase 1–3 PostgreSQL/mTLS gates without claiming M10 production readiness.**

## Execution notes

- Add numbered migrations only when the persisted contract requires a capability absent from the current schema. Migration 014 permits `RunCase.WAITING_APPROVAL`; migration 015 permits `Healing.CANCELLED` and links replay steps to their Healing record. `tests/test_migration_runner.py` verifies the numbered migration inventory.
- The user chose direct changes in the current workspace and approved native inline execution. Keep commits scoped to each task because the workspace already contains unrelated, uncommitted Phase 1–3 acceptance changes.
- Browser integration, database migration execution, and deployment checks require the Docker-backed environment and remain outside local source validation until available.

## Review follow-up (2026-10-30)

- Rejection and expiry now include `WAITING_APPROVAL` RunCases in terminalization, so the RunCase reaches `FAILED` with `finished_at` alongside the Run.
- The Run Console refreshes the scoped Healing record from the actual `approval.decided` payload for Healing targets and from `healing.applied`, so background expiry and replay completion reach the open review.
- Post-review verification: M10 API/persistence/recovery/OpenAPI suite 61 passed; Worker/Gateway suite 21 passed; Run Console review suite 5 passed; Web production build passed.
