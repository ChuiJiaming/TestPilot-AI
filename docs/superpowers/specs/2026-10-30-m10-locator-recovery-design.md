# M10 Locator Recovery Design

Date: 2026-10-30  
Status: Proposed for owner review  
Scope: First Phase 4 vertical slice (M10 only)

## Goal

Recover a Run from a confirmed locator-resolution failure without changing the pinned case version or its assertions. Recovery is bounded, approval-gated, persisted, and fenced from stale workers. A successful recovery creates a new immutable case version for separate M05 review.

M11 defect analysis and M12 visual baselines are outside this slice. Failed screenshots are also deferred until the shared artifact-storage boundary exists; M10 must not put screenshot bytes in `healing_records.evidence`. This slice stores a bounded, redacted accessibility observation and locator-verification evidence. Screenshot capture/storage remains an M10 completion item.

## Approaches considered

1. **Human approval before every recovery replay (selected).** Safest while locator semantics and confidence calibration are new. It adds review latency but keeps automatic application behind a current, auditable decision.
2. **Automatically replay verified SAFE candidates.** Faster, but current evidence and policy are not calibrated enough to support silent locator changes.
3. **Review candidates without replay.** Lowest execution risk, but does not complete the recovery path.

## Runtime flow

1. The executor enters Recovery only for a Worker result that proves locator resolution failed before dispatch. The current Worker reports `LOCATOR_NOT_UNIQUE` before calling click/fill. The Worker result will make this explicit with the failure phase, match count, and `action_dispatched=false`. Timeouts, transport errors, and uncertain outcomes do not enter automatic replay.
2. Recovery captures the failed action ID, original locator, error result, and bounded redacted accessibility observation. A budgeted recovery provider may propose a locator and must return its source, confidence, and verification evidence. It can propose only a locator for the failed action; it cannot change action order, action type, values, or assertions.
3. A deterministic policy gate checks that the proposed locator is valid under the frozen DSL v2 locator schema and resolves to exactly one visible target in the current page. The Worker returns that target's bounded role/name/label/placeholder descriptor. The proposal must include the descriptor as semantic evidence, and the human approver must confirm that it is the intended business control. A CSS or test-id candidate without an observable semantic descriptor is not replayable. Confidence ranks proposals but does not satisfy the gate. If uniqueness, visibility, or semantics cannot be verified, the proposal is not replayable and the Run remains for manual handling.
4. Persist a `healing_records` row and a project-scoped `approvals` row (`target_kind='healing'`, `target_id=healing_id`, `run_id`, and a pending correlation key) in the same transaction that parks the Run as `WAITING_APPROVAL`. Store the requester, evidence, and expiry. The requester cannot decide their own request; the decision handler rechecks current project write access, target scope, pending status, and expiry under row locks.
5. Preserve the declared `POST /api/v1/healing-records/{id}/decision` contract with `APPROVE`/`REJECT` and a required reason. Extend the scoped GET response so the reviewer can read the source, confidence, bounded verification evidence, and decision state. The existing generic approval endpoint remains for Run and case-version approvals; `apply` is an internal transition after replay succeeds, never a public approval decision.
6. Rejection or expiry marks the healing request `REJECTED` or `EXPIRED`, fails the waiting Run, and creates no replay work. Approval issues a new Run attempt token and a single-use healing session-handoff grant. The Gateway may ask the same Worker to rebind the existing session only when the signed grant matches the Run, RunCase, browser, old attempt, new attempt, and Worker instance. The Worker atomically replaces the old session claims. Old attempt tokens then fail scope checks. If the original Worker session has expired or cannot be rebound, the Run moves to manual reconciliation; it never reconstructs page state from a checkpoint or opens a fresh page and guesses.
7. Replay uses a recovery-specific action ID so Worker action deduplication for the original attempt cannot return the cached locator failure. Worker deduplication is scoped by attempt token and action ID, preserving retry safety for duplicate delivery within an attempt. Only the original failed action is rerun, and only after the pre-dispatch locator failure is proven. At most two candidate/replay attempts are allowed, bounded by the existing run-wide model budget and timeout. Other `SAFE`, `FIXTURE_ONLY`, and `NEVER` rules remain unchanged; an uncertain side effect never replays.
8. Re-evaluate the original immutable assertions. If they pass, clone the pinned DSL into a new immutable version, changing only the healed locator. Record that version as `new_version_id`, set `applied_at`, and mark the healing record `APPLIED` only after both replay and original assertions succeed. The new version remains `PENDING_APPROVAL` under the existing M05 case-version flow and is not published by the healing decision. If replay or assertions fail, retain the defect and candidate evidence; do not publish or mark the healing `APPLIED`.

## Components and ownership

- **Run graph / executor:** classify eligible failures, stop the normal action path, persist the recovery request, resume at the failed action after approval, and recheck original assertions.
- **Recovery provider / policy gate:** produce a locator-only proposal using bounded observations; perform deterministic uniqueness, visibility, semantic, and budget checks.
- **Run repository / approval handling:** own transactional state changes, requester/decider separation, expiry, audit/events, and outbox resume intent. Approval remains the source of truth; graph checkpoint state is not authoritative.
- **Gateway / Browser Worker:** implement approval-granted session handoff, explicit pre-dispatch failure evidence, candidate locator verification, and attempt-scoped action deduplication. No unapproved route may rotate session claims.
- **Healing API / Run Console:** read a scoped proposal and its evidence, submit `APPROVE` or `REJECT` with a reason, and surface the pending recovery with the Run. The first valid decision wins; every later decision against a terminal approval returns a conflict and cannot enqueue another replay.
- **Case repository:** create the post-recovery immutable version using the existing pending-review and separation-of-duties rules.

## Persistence and state rules

- Reuse `healing_records` for old/new locators, bounded JSON evidence, confidence, status, resulting version, approver, and applied time.
- Reuse the project-aware `approvals` target model introduced by migration 003. No locator or assertion update is written into the already-published version.
- Store replay-step linkage and bounded verification details in healing evidence. Do not store raw page HTML, secrets, full form values, or screenshot bytes.
- Approval expiry is 30 minutes, matching the existing Run Approval policy. An expiry worker must transition both the healing record and waiting Run; a late decision remains a conflict and cannot enqueue replay.
- Healing status transitions are `PROPOSED -> PENDING_APPROVAL -> APPROVED -> APPLIED`; the decision branches from `PENDING_APPROVAL` to `REJECTED` or `EXPIRED`. An approved replay that cannot complete moves to `CANCELLED`. Run, approval, and healing transitions are written in the owning transaction.
- Run status, healing status, approval decision, audit entry, durable events, and resume Outbox intent must commit atomically. Database rows are authoritative over LangGraph checkpoints.

## Failure handling

- No candidate, invalid locator, non-unique or hidden target, missing observable semantic descriptor, unconfirmed business-control identity, exhausted budget, or exceeded retry count: retain the original failure and do not mutate the case.
- `ACTION_TIMEOUT`, browser disconnect, lost Worker, action already dispatched, or unavailable session handoff: no automatic replay; use manual reconciliation or fail the Run with a durable reason.
- Approval rejection, expiry, cancellation, stale attempt token, duplicate decision, or revoked project permission: no action is sent to the Worker.
- A replay that fails or succeeds while an original assertion fails remains a failed Run; mark that candidate `CANCELLED`, retain the reason, and do not mark it `APPLIED`.

## Acceptance evidence

- Only a pre-dispatch locator-resolution failure can create a recovery request; unrelated failures cannot.
- Candidate evidence is bounded and redacted; the original case version and assertions remain byte-for-byte unchanged.
- A missing, hidden, ambiguous, or semantically unverified candidate cannot replay; the reviewer sees the observed control descriptor and must confirm its business meaning.
- A requester cannot approve their own recovery; permission, expiry, and status are rechecked at decision time.
- Rejected, expired, cancelled, or stale approvals never rebind a session or apply a candidate.
- A valid approval rebinds only the same live Worker session under a new attempt token; stale tokens are rejected. A lost session cannot trigger a new guessed page state.
- The failed action is executed at most once per recovery attempt despite duplicate delivery; the original assertions are rerun.
- A successful recovery creates a new pending case version; a failed assertion or replay does not publish a version or set `APPLIED`.
- Retry and model-call limits terminate in a durable failed/manual state without an infinite loop.
- Phase 1–3 PostgreSQL and real mTLS/egress gates remain separate release gates. M10 is not production-ready until those are run alongside M10 integration acceptance.

## Known completion gap

M10 requires failed screenshot evidence, but the repository has no shared object-storage service yet. This slice will preserve a redacted accessibility observation instead of embedding image bytes in JSONB. Add screenshot artifact storage and bind its artifact ID into healing evidence before declaring the full M10 evidence requirement complete.
