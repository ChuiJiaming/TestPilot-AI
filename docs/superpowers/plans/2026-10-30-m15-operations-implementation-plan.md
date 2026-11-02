# M15 Quotas, Notifications, Audit, and Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce organization token quotas before provider calls, settle each call once, provide deduplicated notification delivery with visible dead letters, and expose scoped audit/usage/operations views with two-person system-setting approval.

**Architecture:** Add a transactionally locked quota reservation service used by M04 and M07 provider calls. Persist in-app notifications and channel deliveries with a stable dedupe key and bounded worker retries. Reuse the organization-locked audit hash chain. System settings use a pending-change record and a second platform administrator's decision before the setting changes.

**Tech Stack:** Python 3.11+, FastAPI 0.115+, Pydantic 2.9+, SQLAlchemy 2.0+, PostgreSQL 15+, Celery 5.6+, Redis, React 18, TypeScript 5.7, Vite 5.4, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-30-phase-5-reporting-integrations-operations-design.md` (M15); `docs/TestPilot_V10.3.docx` Chapter 4 M15; `docs/PLAN.md` Phase 5.

## Global Constraints

- Preserve all 101 baseline operation IDs; M15 owns eleven baseline notification, quota, audit, usage, and settings operations, plus additive V10.3 aliases/actions.
- A quota reservation is committed before an external model call. Exhaustion prevents the call. Settlement is idempotent by `call_id`; failed calls release reservations.
- A monthly quota period is the UTC calendar month. Count consumed usage plus active reservations before approving a new reservation.
- Never persist secret values in settings, notifications, audit metadata, or browser storage; use SecretRef references or deployment configuration.
- Notification delivery is deduplicated and at least once. Retry/backoff is bounded, and exhausted work becomes visibly `DEAD`.
- Audit writes are append-only in the application and hash chained. This detects tampering but does not claim WORM/independent immutable storage; that remains a deployment requirement.
- System-setting changes remain pending until approved by a different platform administrator. Approval, committed setting, and hash-chained audit entry are one database transaction.
- Do not edit migrations 001–017 or apply a new migration to a persistent database. M15 schema changes go in candidate migration 018.

## Review Focus

1. Two simultaneous reservations cannot exceed the monthly token cap — pin with a PostgreSQL concurrency test and a deterministic repository test.
2. Provider calls are not made after reservation denial, and failures release reserved tokens — pin with injected fake model clients.
3. Duplicate usage settlement or notification events have one business effect — pin with call-ID and dedupe-key tests.
4. A setting remains unchanged until a distinct administrator approves, and a stale proposal cannot overwrite a newer value — pin with settings CAS tests.
5. Tampered audit entries are detected while audit metadata excludes secret values — pin with hash-chain verification and redaction tests.

---

### Task 1: Migration 018 and Operations persistence models

**Files:**

- Create: `db/migrations/018_m15_operations.sql`
- Create: `apps/api/app/operations/__init__.py`
- Create: `apps/api/app/operations/persistence.py`
- Modify: `db/migrations/README.md`
- Test: `tests/test_m15_operations_migrations.py`
- Test: `tests/test_m15_operations_persistence.py`

**Interfaces:**

- Models: `QuotaPolicyRecord`, `QuotaReservationRecord`, `NotificationRecord`, `NotificationPreferenceRecord`, `SystemSettingChangeRecord`; reuse `UsageMetricRecord`/`AuditLogRecord` where existing schema already provides them.
- `QuotaReservationRecord.call_id` is stored as text to match the existing schema/index; new service calls use canonical UUID strings. `period_month` is the first day of the UTC month.
- Notification unique key is `(user_id, dedupe_key)`.
- `SystemSettingChangeRecord` fields are `id`, `key`, `proposed_value`, `previous_value_hash`, `proposed_value_hash`, `requested_by`, `approved_by`, `status`, `reason`, `created_at`, and `decided_at`.

- [x] **Step 1: Write failing schema/persistence tests**

Add `test_legacy_call_ids_are_backfilled_and_existing_unique_index_is_preserved`, `test_call_id_is_idempotent_and_mismatched_reuse_conflicts`, `test_notification_dedupe_key_is_unique_per_user`, `test_one_pending_setting_change_per_key`, `test_setting_approval_cannot_equal_requester`, and `test_settings_change_does_not_replace_current_value_when_pending`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_operations_migrations.py tests/test_m15_operations_persistence.py -q`  
Expected: FAIL because the models/constraints are absent.

- [x] **Step 3: Add migration 018 and map the models**

Migration 018 must account for the existing `quota_reservations.call_id text`, `settled_at`, `actual_cost`, and `uq_quota_call_per_org` added by migration 003, plus the unvalidated non-null check in migration 004. Backfill only null legacy call IDs with unique UUID strings, derive `period_month` from `created_at` in UTC, add a period/status lookup index, and validate the existing call-ID check after backfill; do not add a duplicate call-ID column or unique index. Add `dedupe_key` to notifications and backfill existing rows with unique legacy values; create the partial unique pending-setting index and `system_setting_changes` table. Add database checks that approved changes have a non-null, distinct approver. Keep proposed setting values restricted to non-secret settings; credentials remain deployment/SecretRef managed.

- [x] **Step 4: Run focused model and migration-source tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_operations_migrations.py tests/test_m15_operations_persistence.py -q`  
Expected: tests PASS. PostgreSQL 15/18 fresh/upgrade/rollback remains a separate external gate; do not apply migration 018 here.

### Task 2: Hard quota reservation and settlement service

**Files:**

- Create: `apps/api/app/operations/quotas.py`
- Modify: `apps/api/app/operations/persistence.py`
- Test: `tests/test_m15_quotas.py`

**Interfaces:**

- `QuotaService.reserve(organization_id: UUID, *, call_id: UUID, run_id: UUID | None, requested_tokens: int, now: datetime) -> QuotaReservationResult`; persist `str(call_id)` in the existing text column.
- `QuotaService.settle(call_id: UUID, *, consumed_tokens: int, now: datetime) -> SettlementResult`.
- `QuotaService.release(call_id: UUID, *, now: datetime) -> bool`.
- `QuotaExceeded` is raised before a provider call when committed usage plus reserved capacity would exceed `monthly_token_limit`.

- [x] **Step 1: Write failing quota tests**

Add `test_reservation_denied_at_monthly_limit`, `test_active_reservations_count_against_limit`, `test_same_call_id_returns_same_reservation`, `test_same_call_id_with_different_dimensions_conflicts`, `test_success_settles_once_by_call_id`, `test_failed_call_releases_reservation`, `test_month_boundary_uses_utc_calendar_month`, and `test_negative_or_boolean_token_counts_are_rejected`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_quotas.py -q`  
Expected: FAIL because reservation/settlement APIs are absent.

- [x] **Step 3: Implement transactional reservation accounting**

Lock the organization row (including first policy creation) and quota policy row. Sum committed usage and live `RESERVED` capacity for the current UTC month. Insert one reservation per call ID only when the requested token cap fits. Settlement locks the reservation and transitions `RESERVED → COMMITTED` once, writes the usage metric in the same transaction, and returns the prior result on duplicate settlement. Failure transitions `RESERVED → RELEASED`.

- [x] **Step 4: Run quota tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_quotas.py -q`  
Expected: all cap, period, settlement, and release tests PASS.

### Task 3: Enforce quotas before M04 and M07 provider calls

**Files:**

- Modify: `apps/api/app/cases/planning.py`
- Modify: `apps/api/app/cases/planner_provider.py`
- Modify: `apps/api/app/runs/agent.py`
- Modify: `apps/api/app/runs/planner.py`
- Modify: `apps/api/app/cases/worker.py`
- Test: `tests/test_m15_provider_quotas.py`
- Test: `tests/test_phase3_run_execution.py`

- [x] **Step 1: Write failing provider-boundary tests**

Add `test_generation_quota_denial_does_not_call_provider`, `test_run_agent_reserves_before_provider_request`, `test_provider_failure_releases_reservation`, `test_provider_usage_settles_actual_tokens_once`, and `test_missing_provider_usage_settles_reserved_cap`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_provider_quotas.py -q`  
Expected: FAIL because M04/M07 do not use organization reservations.

- [x] **Step 3: Wire reservations around each provider request**

Generate a stable UUID `call_id` for each billable provider invocation and persist the reservation before network I/O. A retried logical job gets a new call ID for a new provider invocation; replay of the same invocation reuses the prior ID and reservation. Estimate/request a bounded maximum token count, release on a definite provider failure, and settle from provider usage metadata on success; if metadata is absent, settle the reserved amount with an explicit fallback usage source. Do not change M04/M07 retry semantics or allow an Agent result to affect test status.

- [x] **Step 4: Run provider and Phase 3 regression tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_provider_quotas.py tests/test_phase3_run_execution.py tests/test_phase3_recovery_persistence.py -q`  
Expected: all focused tests PASS; PostgreSQL multi-transaction quota races remain unverified until disposable PostgreSQL is available.

### Task 4: Deduplicated notifications and visible delivery retries

**Files:**

- Create: `apps/api/app/operations/notifications.py`
- Create: `apps/api/app/operations/routes.py`
- Modify: `apps/api/app/cases/worker.py`
- Modify: `apps/api/app/main.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m15_notifications.py`
- Test: `tests/test_m15_notification_routes.py`

**Interfaces:**

- `NotificationService.enqueue(user_id, *, event_type, dedupe_key, payload, now) -> NotificationRecord`.
- `NotificationDispatcher.dispatch(notification_id, *, channel, sender, worker_id, now)` records every attempt and uses the configured channel adapter.
- User routes are `GET /notifications`, `POST /notifications/{id}/read`, `GET /notifications/preferences`, and `PUT /notifications/preferences`. Add scoped `GET /notification-deliveries?status=DEAD` (`get__api_v1_notification_deliveries`) so exhausted deliveries shown in Operations have a real API source.

- [x] **Step 1: Write failing notification tests**

Add `test_duplicate_event_creates_one_notification`, `test_preference_suppresses_disabled_channel`, `test_user_can_only_mark_own_notification_read`, `test_transient_failure_retries_with_backoff`, `test_retry_exhaustion_is_visible_as_dead`, and `test_dead_delivery_list_is_user_and_organization_scoped`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_notifications.py tests/test_m15_notification_routes.py -q`  
Expected: FAIL because notification persistence/routes are absent.

- [x] **Step 3: Implement notification service, worker, and routes**

Enqueue with the `(user_id, dedupe_key)` unique invariant and apply `notification_preferences` before delivery. `in_app` notifications are queryable/readable by their owner. Email and Webhook channels use injected adapters, eight bounded attempts with `min(300, 2**attempt)` second backoff, and one `delivery_attempts` row per attempt. Unknown channel/provider configuration fails closed and is surfaced as `DEAD`; never persist raw credentials or response bodies. The dead-delivery listing is scoped to the owning user or an authorized organization administrator and returns safe error codes only.

- [x] **Step 4: Run focused notification tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_notifications.py tests/test_m15_notification_routes.py -q`  
Expected: dedupe, preference, ownership, retry, and dead-letter tests PASS.

### Task 5: Audit/usage APIs and two-person system-setting changes

**Files:**

- Create: `apps/api/app/operations/audit.py`
- Create: `apps/api/app/operations/settings.py`
- Modify: `apps/api/app/core/audit.py`
- Modify: `apps/api/app/operations/routes.py`
- Modify: `apps/api/app/main.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m15_audit_settings.py`
- Test: `tests/test_m15_operations_routes.py`

**Interfaces:**

- `AuditRepository.list(actor_id, organization_id, *, filters, limit, cursor)`, `export(...)`, and `verify_chain(organization_id) -> AuditVerificationResult`.
- `SystemSettingService.request_change(actor_id, key, *, value, reason, now) -> dict` creates `PENDING` only.
- `SystemSettingService.decide_change(actor_id, change_id, *, decision, reason, now) -> dict` requires a different platform administrator and compares the current-value hash before committing.

- [x] **Step 1: Write failing audit/settings tests**

Add `test_audit_scope_and_keyset_pagination`, `test_audit_chain_verifier_detects_modified_entry`, `test_audit_verification_requires_organization_scope`, `test_setting_request_does_not_change_current_value`, `test_requester_cannot_approve_own_change`, `test_approval_and_audit_commit_atomically`, `test_stale_proposal_conflicts`, and `test_secrets_are_not_written_to_audit_metadata`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_audit_settings.py tests/test_m15_operations_routes.py -q`  
Expected: FAIL because operations routes and settings workflow are absent.

- [x] **Step 3: Implement scoped operations APIs**

Preserve baseline IDs `get__api_v1_admin_system_settings`, `put__api_v1_admin_system_settings__key`, `get__api_v1_notifications`, `get__api_v1_notifications_preferences`, `get__api_v1_organizations__id__audit_logs`, `get__api_v1_organizations__id__quotas`, `get__api_v1_organizations__id__usage`, `post__api_v1_audit_logs_export`, `post__api_v1_notifications__id__read`, `put__api_v1_notifications_preferences`, and `put__api_v1_organizations__id__quotas`. Add V10.3 `GET /audit-logs` (`get__api_v1_audit_logs`), `GET /audit-logs/verify` (`get__api_v1_audit_logs_verify`), `GET/PUT /quota-policies` (`get__api_v1_quota_policies`, `put__api_v1_quota_policies`), `GET /usage` (`get__api_v1_usage`), and `GET/PUT /system-settings` (`get__api_v1_system_settings`, `put__api_v1_system_settings`) routes, plus `POST /admin/system-setting-changes/{id}/decision` (`post__api_v1_admin_system_setting_changes__id__decision`) and `GET /notification-deliveries` (`get__api_v1_notification_deliveries`). The V10.3 export route keeps the baseline ID `post__api_v1_audit_logs_export`. Record all additive IDs separately. The existing PUT setting operation creates a pending change; the setting value changes only after a second platform administrator approves. Require a reason and audit only before/after digests and identities. Reject secret values in system settings; credentials remain external.

- [x] **Step 4: Run audit and settings tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_audit_settings.py tests/test_m15_operations_routes.py -q`  
Expected: all scope, hash verification, separation-of-duties, stale-write, and transaction tests PASS.

### Task 6: Operations UI

**Files:**

- Create: `apps/web/src/OperationsPage.tsx`
- Create: `apps/web/src/OperationsPage.test.tsx`
- Create: `apps/web/src/operations.css`
- Modify: `apps/web/src/App.tsx`
- Create: `apps/web/e2e/operations.spec.ts`

- [x] **Step 1: Write failing Operations page tests**

Cover organization quota/usage, provider-call quota denial, notification list/preferences/read, audit filters/export/verification result, pending setting approval by a second admin, stale/denied approval, and visible dead deliveries.

- [x] **Step 2: Implement the Operations page**

Add a role-aware Operations navigation entry. Keep organization-scoped usage/quota and user-scoped notification views separate from platform-admin system settings. Show setting requester/approver and old/new hashes without values. Render `DEAD` deliveries with attempt count and last safe error code.

- [x] **Step 3: Run frontend checks**

Run: `npm --prefix apps/web run typecheck`, `npm --prefix apps/web run lint`, and the Vitest suite.  
Evidence: typecheck and lint passed; the full frontend suite passed 31 tests and the Operations page suite passed 7 tests. Vite's default test and build commands hit Windows `EPERM` on asynchronous `realpath`; the same checks passed with temporary configs setting `resolve.preserveSymlinks: true`. Those temporary configs are not part of the project.

- [x] **Step 4: Add the browser flow**

`apps/web/e2e/operations.spec.ts` verifies quota exhaustion display, notification preferences, audit filtering, and a two-admin system-setting approval.

- [ ] **Step 5: Run the browser flow**

Run: `npm --prefix apps/web run test:e2e -- e2e/operations.spec.ts` against configured disposable API/database services. The `TESTPILOT_E2E_*` variables are not configured in this workspace, so browser acceptance remains an external gate.

### Task 7: M15 operation inventory and release checks

**Files:**

- Modify: `api/openapi.yaml`
- Modify: `docs/api-runtime-coverage.md`
- Modify: `docs/traceability-matrix.md`
- Modify: `docs/PLAN.md`
- Test: `tests/test_openapi_inventory.py`

- [x] **Step 1: Verify all eleven M15 baseline IDs**

Assert all IDs listed in Task 5 remain in static OpenAPI and runtime registration; verify additive aliases/decision route IDs are recorded separately.

- [x] **Step 2: Regenerate runtime inventory**

Run: `apps/api/.venv/Scripts/python.exe -m apps.api.scripts.report_runtime_api_coverage`  
Evidence: `docs/api-runtime-coverage.md` reports 101/101 baseline operations and 27/27 additive operations registered; `contracts/openapi_v10_3_phase5_additive_operation_ids.txt` separately inventories all additive operation IDs. This remains route inventory, not end-to-end acceptance.

- [x] **Step 3: Run Phase 5 focused acceptance and build checks**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m15_operations_migrations.py tests/test_m15_operations_persistence.py tests/test_m15_quotas.py tests/test_m15_provider_quotas.py tests/test_m15_notifications.py tests/test_m15_notification_routes.py tests/test_m15_audit_settings.py tests/test_m15_operations_routes.py -q`; then `npm --prefix apps/web run typecheck`, `lint`, `test`, and `build`.  
Evidence: the focused M15 backend group passed 61 tests; OpenAPI inventory passed 10 tests; the frontend suite passed 31 tests; typecheck and lint passed; production build passed with the temporary `preserveSymlinks` config described in Task 6. Disposable PostgreSQL concurrency, mail/provider delivery, WORM storage, deployment, and browser E2E checks remain external gates.
