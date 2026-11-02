# Phase 5 Reporting, Integrations, and Operations Design

Status: draft for review. No Phase 5 product code is included in this draft.

## Intent

Implement M13–M15 in the order M13 reports, M14 CI and Webhooks, then M15 notifications, quotas, audit, and operations. The goal is to turn durable Run facts into authorized reports and exports, accept secure/idempotent CI and webhook activity, and enforce quotas and visible delivery/audit operations.

## Current state and authority

- `docs/PLAN.md` requires all 101 baseline OpenAPI operation IDs to remain, while the V10.3 chapters 41–50 supplement takes precedence over conflicting candidate details.
- Runtime inventory currently registers 82/101 baseline operations. The 19 missing baseline operations are all in M13–M15; 10 additive operations are registered.
- The V10.3 M13–M15 chapters and `api/openapi.yaml` differ in paths and operation shape. Examples include nested report-export creation, CI trigger and gate operations, webhook delivery operations, and quota-policy paths. The design preserves every baseline operation ID and adds the V10.3 chapter routes where they are absent. Compatibility operations must retain their existing IDs and authorization semantics; they must not silently change meaning.
- No Phase 5 API domain or UI page is implemented yet. Existing private S3 storage in `apps/api/app/quality/artifacts.py` and `append_audit_entry` in `apps/api/app/core/audit.py` are reusable foundations.
- M03 keeps SecretRefs metadata-only and does not resolve provider secrets in the API. M14 will store only SecretRef references and use a deployment-injected resolver; absent configuration fails closed. Phase 5 will not add a secret-value store or return provider secrets to the browser.
- Candidate schema already exists in migrations 001–002 for reports, integrations, webhook deliveries, notifications, audit logs, usage metrics, quota policies, exports, notification preferences, delivery attempts, quota reservations, and webhook nonce/signature fields. Do not edit historical migrations. Add a new numbered migration only if schema review identifies a missing invariant.
- The Phase 1–4 local evidence and its environment limits are recorded in `docs/phase-1-4-unified-validation.md`.

## Approaches

1. **Treat the static OpenAPI paths as the only implementation contract.** This minimizes route additions, but leaves required V10.3 M13–M15 workflows and routes absent. It conflicts with the project rule that chapters 41–50 take precedence.
2. **Implement V10.3 behavior additively and preserve all 101 baseline operation IDs as compatibility operations. Recommended.** This follows both project rules, supports the documented workflows, and keeps older contract consumers identifiable. Add exact chapter routes when the baseline lacks them; preserve operation IDs and behavior for existing baseline paths.
3. **Build shared database and worker foundations for all three modules, then add APIs and UI later.** This delays usable vertical slices and makes it harder to validate authorization, idempotency, and failure recovery as users encounter each workflow.

The recommendation is approach 2, delivered as vertical slices in M13 → M14 → M15 order.

## Design

### M13 — immutable Run reports and exports

- Create one immutable report snapshot from authoritative Run, RunCase, Step, artifact, and usage facts when a Run reaches a terminal state. Include environment, branch, commit, browser matrix, trigger source, deterministic result counts, and authorized artifact references. Redact secret values and never use model text as the source of pass/fail or counts.
- Serve `GET /test-runs/{id}/report`, `POST /reports/{id}/exports`, and `GET /report-exports/{id}` from V10.3. Keep the baseline report and export operation IDs registered with their documented behavior; retain the baseline `POST /report-exports` operation as a compatibility entry point.
- Queue exports durably. Track `QUEUED`, `RUNNING`, `SUCCEEDED`, and `FAILED`; export retries never change the Run's terminal state. Support HTML, PDF, JSON, and JUnit.
- Store generated files through the private artifact boundary. Downloads reauthorize against current project membership before returning bytes or a short-lived URL. An expired Trace reference is represented as `EXPIRED_ARTIFACT`, not a broken report page.
- Use a deterministic renderer interface; select a PDF implementation during the implementation plan after checking the existing build/runtime constraints. Do not introduce a public bucket or persist long-lived signed URLs.

### M14 — scoped integrations, CI gates, and signed Webhooks

- Store integration credentials as SecretRef references and enforce organization/project scopes on every management, trigger, and delivery operation.
- Implement the V10.3 `/integrations`, `/webhooks/{id}/deliveries`, `/ci/trigger`, and `/test-runs/{id}/gate` resources. Keep the baseline `/organizations/{id}/integrations`, `/integrations/{id}/test`, and `/webhooks/{provider}` operation IDs and semantics.
- CI trigger idempotency includes organization, repository, commit SHA, plan, environment, sorted browser matrix, trigger source, and request intent. A duplicate returns the original Run; a different environment or browser matrix creates a distinct Run.
- Implement machine-readable Run gates from durable assertion/test results and configured blocking severity. AI summaries cannot override gate status.
- Validate Webhook HMAC in constant time over the raw body, timestamp, and nonce; reject invalid signatures, stale timestamps, and nonce replay before enqueueing work. Keep received-event processing idempotent.
- Preserve the existing provider webhook operation for inbound events; expose the V10.3 delivery resource for scoped delivery history and recovery actions.
- Use durable Outbox/worker delivery with bounded exponential retry, per-attempt evidence, and a visible `DEAD` state. Preserve the existing static provider webhook operation while adding the V10.3 delivery operations.

### M15 — hard quotas, notifications, audit, and operations

- Reserve token budget transactionally before a model/tool call. Enforce the organization limit before provider invocation; settle once by stable call ID, and release reservations after failed calls. Duplicate settlement has no second billing effect.
- Generate notifications with a stable dedupe key and user/event preference checks. Use bounded retry/backoff and expose exhausted deliveries as dead letters.
- Reuse the organization-locked audit hash chain for immutable application history. System-setting writes record actor, reason, prior-value digest, and resulting-value digest. Expose scoped audit filtering/export and a chain-verification operation.
- Require a distinct platform administrator to approve system-setting changes, then persist the approver identity with the before/after digests. The owner selected this separation-of-duties rule on 2026-10-30.
- Keep operational claims precise: a database hash chain detects alteration, while independent WORM/append-only storage and daily production verification require deployment infrastructure and remain Phase 6 acceptance.
- Provide accessible report, integration/CI delivery, notification, quota/usage, and audit/operations UI entry points consistent with the current React/Vite application.

Implement the V10.3 `/notifications/preferences`, `/audit-logs`, `/audit-logs/export`, `/quota-policies`, `/usage`, and `/system-settings` resources while preserving the baseline organization quota, audit, usage, notification, and admin settings operations.

## Acceptance

- M13: HTML/PDF/JSON/JUnit exports contain the Run commit and agree with durable database facts; unauthorized users cannot read/download; export failure leaves Run state unchanged.
- M14: modified HMAC, stale timestamp, and repeated nonce are rejected; CI retries deduplicate only when all selected idempotency dimensions match; the gate returns machine-readable deterministic results; failed deliveries reach a visible `DEAD` state.
- M15: quota exhaustion prevents a provider call; duplicate settlement/notification delivery has one business effect; unauthorized users cannot mutate audit or system settings; audit-chain verification detects altered entries.
- Preserve all 101 baseline operation IDs and record all additive routes in OpenAPI/runtime inventory.
- Add focused repository, API authorization/idempotency, worker retry, and frontend coverage for each vertical slice.

## Non-goals and limits

- Applying migrations to a persistent database, configuring real provider secrets, production SMTP/Webhook endpoints, deploying WORM storage, or claiming Phase 6 readiness.
- Replacing baseline API operation IDs or broadening existing token scopes.
- Changing Phase 1–4 policies already approved by the owner.

## Remaining implementation choices

- The exact additive OpenAPI operation IDs for V10.3-only routes.
- PDF renderer and worker task wiring, selected against existing runtime/deployment constraints.
- Whether existing columns fully enforce nonce, call-ID settlement, notification dedupe, and immutable report snapshot invariants; use a new migration after schema review if required.
