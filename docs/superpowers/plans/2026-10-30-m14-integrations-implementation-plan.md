# M14 Integrations, CI, and Webhooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add scoped integrations, idempotent CI Run creation, deterministic machine-readable gates, replay-resistant signed webhook ingestion, and retry/dead-letter visibility.

**Architecture:** Store integration metadata and SecretRef IDs only. CI calls the existing Run creation/admission path with a canonical idempotency digest. Inbound Webhooks verify the raw request before parsing and enqueue a normalized event. Outbound delivery uses an injected provider transport with strict host rules, a durable Outbox, bounded retries, and delivery history.

**Tech Stack:** Python 3.11+, FastAPI 0.115+, Pydantic 2.9+, SQLAlchemy 2.0+, PostgreSQL 15+, Celery 5.6+, Redis, httpx, React 18, TypeScript 5.7, Vite 5.4, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-30-phase-5-reporting-integrations-operations-design.md` (M14); `docs/TestPilot_V10.3.docx` Chapter 4 M14; `docs/PLAN.md` Phase 5.

## Global Constraints

- Preserve the 101 baseline operation IDs; M14 owns the four current baseline integration/Webhook IDs and adds V10.3 CI/gate/delivery routes.
- CI idempotency is scoped by organization, repository, commit SHA, plan, environment, sorted browser matrix, trigger source, and request intent.
- A Run gate uses persisted assertion and case-priority facts. AI summaries, classifications, and hypotheses cannot override the gate.
- Integration records store only `secret_ref_id`, never secret material. Resolve credentials through an injected deployment provider; unavailable secrets fail closed.
- Verify HMAC in constant time over the exact raw request body, timestamp, and nonce. Reject stale timestamps and replayed nonces before enqueueing.
- Never fetch an arbitrary user-supplied URL. Outbound transport is provider-specific, host-restricted, timeout-bounded, and does not follow redirects.
- Outbox/Celery processing is at least once; consumers and retries are idempotent. Do not modify migrations 001–017.
- Use current project/organization authorization for all management and delivery-history reads; redact credentials and raw Webhook bodies from responses/logs.

## Review Focus

1. Replayed, stale, malformed, or tampered HMAC input is rejected before persistence — pin with signature tests.
2. Idempotency with a changed environment or browser matrix creates a distinct Run — pin with CI repository tests.
3. A token with no `run:create` or no project access cannot trigger CI — pin with route authorization tests.
4. An absent SecretRef provider, non-allowlisted endpoint, redirect, timeout, 429, 4xx, or 5xx fails according to the explicit closed/retry policy — pin with injected fake-provider tests.
5. Duplicate delivery, expired worker lease, and a repeated manual retry cannot cause duplicate business effects — pin with Outbox/delivery-state tests.

---

### Task 1: Integration persistence, SecretRef boundary, and baseline routes

**Files:**

- Create: `apps/api/app/integrations/__init__.py`
- Create: `apps/api/app/integrations/persistence.py`
- Create: `apps/api/app/integrations/routes.py`
- Create: `apps/api/app/integrations/secrets.py`
- Modify: `apps/api/app/main.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m14_integrations.py`
- Test: `tests/test_m14_routes.py`

**Interfaces:**

- `SecretRefResolver.resolve(secret_ref_id: UUID, organization_id: UUID) -> bytes` raises `SecretProviderUnavailable` if missing; it never returns provider material to a response DTO.
- `IntegrationRepository.create/list/get/update` accepts an actor ID and validates organization scope before returning a record.
- Baseline IDs: `get__api_v1_organizations__id__integrations`, `post__api_v1_organizations__id__integrations`, and `post__api_v1_integrations__id__test`.

- [x] **Step 1: Write failing persistence and route tests**

Add `test_integration_dto_never_contains_secret_value`, `test_secret_provider_missing_fails_closed`, `test_integration_list_is_organization_scoped`, `test_integration_create_requires_write_scope`, and `test_baseline_integration_operation_ids_are_registered`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_integrations.py tests/test_m14_routes.py -q`  
Expected: FAIL because the integration domain/router is absent.

- [x] **Step 3: Implement the persistence/service boundary**

Map existing `integrations` and `secret_refs` tables. Add V10.3 `GET/POST /api/v1/integrations` aliases and `PATCH /api/v1/integrations/{id}` for enabled/config changes; keep baseline organization-scoped create/list and integration test operation IDs and semantics. Accept only `organization_id`, `kind`, non-secret `config`, `secret_ref_id`, and `enabled`; never accept or return raw credential values. Mount the router through `create_app` dependency injection.

- [x] **Step 4: Run focused tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_integrations.py tests/test_m14_routes.py -q`  
Expected: all persistence, scope, fail-closed, and baseline ID tests PASS.

### Task 2: Idempotent CI trigger and deterministic gate

**Files:**

- Create: `apps/api/app/integrations/ci.py`
- Modify: `apps/api/app/runs/persistence.py`
- Modify: `apps/api/app/runs/routes.py`
- Modify: `apps/api/app/main.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m14_ci.py`
- Test: `tests/test_m14_gate.py`

**Interfaces:**

- `CITriggerService.idempotency_key(*, organization_id, repository, commit_sha, plan_id, environment_id, browser_matrix, trigger_source, request_intent) -> str` returns SHA-256 of canonical JSON.
- `ScheduledRunRepository.create_ci_run(actor_id, *, project_id, plan_id, environment_id, repository, commit_sha, branch, browser_matrix, request_intent, idempotency_key, now) -> dict` creates or returns the same Run and writes one `RUN_REQUESTED` event.
- `GateEvaluator.evaluate(run_id: UUID, *, blocking_severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]) -> dict` returns `PENDING`, `PASS`, or `FAIL` with deterministic counts.

- [x] **Step 1: Write failing CI and gate tests**

Add `test_ci_retry_returns_same_run_and_outbox`, `test_ci_different_environment_or_matrix_is_not_deduplicated`, `test_ci_browser_matrix_is_sorted_before_hashing`, `test_gate_is_pending_until_terminal`, `test_gate_uses_case_priority_and_assertion_status_only`, and `test_gate_ignores_ai_hypothesis_severity`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_ci.py tests/test_m14_gate.py -q`  
Expected: FAIL because CI/gate services and routes are absent.

- [x] **Step 3: Implement CI Run creation and gate evaluation**

Canonicalize the complete idempotency tuple as sorted-key compact UTF-8 JSON and SHA-256 it. Normalize and sort the browser matrix before hashing. Reuse `ScheduledRunRepository` environment snapshot, published-case pinning, RunCase expansion, and `RUN_REQUESTED` transaction; store `trigger_source="ci"`, branch, commit SHA, and request intent. Add `POST /api/v1/ci/trigger` (`post__api_v1_ci_trigger`) and `GET /api/v1/test-runs/{id}/gate` (`get__api_v1_test_runs__id__gate`) with `run:create`/`run:read` checks. Map `CRITICAL/HIGH/MEDIUM/LOW` to P0/P1/P2/P3 case priority; gate failure counts come only from deterministic failed assertions.

- [x] **Step 4: Run CI/gate tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_ci.py tests/test_m14_gate.py -q`  
Expected: all idempotency, pinning, authorization, and deterministic gate tests PASS.

### Task 3: HMAC, timestamp, and nonce validation for inbound Webhooks

**Files:**

- Create: `apps/api/app/integrations/webhooks.py`
- Modify: `apps/api/app/integrations/routes.py`
- Modify: `apps/api/app/integrations/persistence.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m14_webhook_signatures.py`
- Test: `tests/test_m14_webhook_routes.py`

**Interfaces:**

- `verify_inbound_webhook(raw_body: bytes, *, timestamp: str, nonce: str, signature: str, secret: bytes, now: datetime, max_skew_seconds=300) -> VerifiedWebhook`.
- Signing bytes are `timestamp UTF-8 + b"\n" + nonce UTF-8 + b"\n" + raw_body`; signature is `sha256=<lowercase HMAC-SHA256 hex>`.
- Inbound headers are `X-TestPilot-Timestamp`, `X-TestPilot-Nonce`, and `X-TestPilot-Signature`.

- [x] **Step 1: Write failing verifier tests**

Add `test_valid_signature_accepts_exact_raw_body`, `test_body_change_invalidates_signature`, `test_stale_and_future_timestamps_are_rejected`, `test_nonce_reuse_is_rejected_per_integration`, `test_malformed_signature_is_rejected`, and `test_oversize_or_malformed_payload_is_rejected`. Unit tests pin valid/invalid outcomes; verify the implementation uses `hmac.compare_digest` during code review rather than relying on timing assertions.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_webhook_signatures.py tests/test_m14_webhook_routes.py -q`  
Expected: FAIL because verification and ingestion routes are absent.

- [x] **Step 3: Implement bounded, fail-closed ingestion**

Read at most 1 MiB of raw body, check timestamp within ±300 seconds, validate nonce length 16–256, resolve the integration's secret through the injected resolver, compare with `hmac.compare_digest`, then parse JSON. Keep baseline `POST /api/v1/webhooks/{provider}` operation ID `post__api_v1_webhooks__provider` public-or-verified-HMAC; add signature headers and a receipt response. Persist nonce/signed time and only the normalized event envelope plus body SHA-256 in Outbox; never persist raw request bytes. Reject unavailable credentials with 503, invalid HMAC with 401, and replay with 409.

- [x] **Step 4: Run signature and route tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_webhook_signatures.py tests/test_m14_webhook_routes.py -q`  
Expected: all signature, replay, size, and authorization tests PASS.

### Task 4: Outbound delivery retries and visible dead letters

**Files:**

- Modify: `apps/api/app/integrations/persistence.py`
- Modify: `apps/api/app/integrations/routes.py`
- Modify: `apps/api/app/cases/worker.py`
- Modify: `apps/api/app/main.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m14_delivery_retries.py`
- Test: `tests/test_m14_routes.py`

**Interfaces:**

- `WebhookSender.send(integration: dict, event: dict) -> DeliveryResponse` is injected; production adapters use fixed provider hosts, bounded timeouts, and redirects disabled.
- `DeliveryRepository.claim(delivery_id, *, worker_id, now, lease_seconds=30)`, `record_attempt(...)`, `retry_or_dead(...)`, and `list_deliveries(actor_id, integration_id, ...)` are scoped and lease-fenced.

- [x] **Step 1: Write failing delivery tests**

Add `test_5xx_and_429_retry_with_bounded_backoff`, `test_permanent_4xx_is_not_retried`, `test_eighth_failure_becomes_dead`, `test_duplicate_outbox_delivery_has_one_business_effect`, `test_redirect_and_arbitrary_host_are_rejected`, and `test_outsider_cannot_list_or_retry_delivery`.

- [x] **Step 2: Run tests and confirm failure**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_delivery_retries.py -q`  
Expected: FAIL because the sender and retry state machine are absent.

- [x] **Step 3: Implement delivery state and routes**

Reuse `webhook_deliveries`, `delivery_attempts`, and Outbox. Retry 429/5xx/timeouts up to eight attempts with `min(300, 2**attempt)` seconds; treat other 4xx as permanent. Add `GET /api/v1/webhooks/{id}/deliveries` (`get__api_v1_webhooks__id__deliveries`) and `POST /api/v1/webhooks/{id}/deliveries/{delivery_id}/retry` (`post__api_v1_webhooks__id__deliveries__delivery_id__retry`). Store bounded error codes, never raw provider response bodies or credentials. The worker must reject redirects and hosts outside the provider adapter's fixed allowlist.

- [x] **Step 4: Run delivery tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_delivery_retries.py tests/test_m14_routes.py -q`  
Expected: retries, dead-letter state, idempotency, and tenant scope tests PASS.

### Task 5: Integrations and CI UI

**Files:**

- Create: `apps/web/src/IntegrationsPage.tsx`
- Create: `apps/web/src/IntegrationsPage.test.tsx`
- Create: `apps/web/src/integrations.css`
- Modify: `apps/web/src/App.tsx`
- Create: `apps/web/e2e/integrations-ci.spec.ts`

- [x] **Step 1: Write failing page tests**

Cover scoped integration list/create/enable, secret reference labels without secret values, CI Run trigger with environment/browser matrix, gate `PENDING/PASS/FAIL`, delivery history, retry, and `DEAD` display.

- [x] **Step 2: Implement the page**

Add an Integrations navigation entry and typed loading/empty/denied/retry states. Never render or persist credential material in browser storage; accept only SecretRef IDs.

- [ ] **Step 3: Run frontend tests and browser flow**

Run: `npm --prefix apps/web test -- IntegrationsPage.test.tsx` and `npm --prefix apps/web run test:e2e -- e2e/integrations-ci.spec.ts`  
Expected: page tests PASS; browser flow passes against configured disposable API services and fake providers.

> Focused page tests, typecheck, and lint pass. The Playwright browser flow is implemented but could not run because `TESTPILOT_E2E_EMAIL`, `TESTPILOT_E2E_PASSWORD`, and a configured E2E service are absent in this environment.

### Task 6: M14 contract and acceptance gate

**Files:**

- Modify: `api/openapi.yaml`
- Modify: `docs/api-runtime-coverage.md`
- Modify: `docs/traceability-matrix.md`
- Modify: `docs/PLAN.md`
- Test: `tests/test_openapi_inventory.py`

- [x] **Step 1: Assert all four M14 baseline IDs remain**

Verify `get__api_v1_organizations__id__integrations`, `post__api_v1_organizations__id__integrations`, `post__api_v1_integrations__id__test`, and `post__api_v1_webhooks__provider` remain registered with their documented security contracts. Verify additive route IDs separately.

- [x] **Step 2: Regenerate route inventory**

Run: `apps/api/.venv/Scripts/python.exe -m apps.api.scripts.report_runtime_api_coverage`  
Expected: all four M14 baseline operations show `Registered`; report counts update without deleting operation IDs.

- [x] **Step 3: Run focused M14 and frontend checks**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m14_integrations.py tests/test_m14_routes.py tests/test_m14_ci.py tests/test_m14_gate.py tests/test_m14_webhook_signatures.py tests/test_m14_webhook_routes.py tests/test_m14_delivery_retries.py -q`; then `npm --prefix apps/web run typecheck`, `lint`, `test`, and `build`.  
Expected: focused tests and frontend checks PASS. Real provider credentials, PostgreSQL concurrency, and mTLS/deployment remain external gates.
