# TestPilot AI V10.3 Implementation Plan

> **For implementation:** Execute one phase at a time. Each module keeps its interface, database, frontend, tests, and acceptance evidence. Do not mark a module complete based on static checks alone.

**Goal:** Implement the complete M01–M15 enterprise AI Web automation testing platform defined by V10.3, preserving every one of the 101 baseline HTTP operations. Additive M01–M06 operations, including verification/session and operational dead-letter reads, are inventoried separately and must not replace baseline IDs.

**Architecture:** React + TypeScript + Vite + shadcn/ui is the web SPA. FastAPI owns tenant-scoped business APIs and PostgreSQL transactions. Celery and LangGraph perform bounded asynchronous orchestration; a Browser Gateway routes scoped actions to an independently isolated Playwright Worker. PostgreSQL is authoritative for business state, Outbox, leases, and events; Redis is the queue/broker; MinIO/S3 stores evidence.

**Tech Stack:** React, TypeScript, Vite, shadcn/ui, Python, FastAPI, LangGraph, LangChain, Playwright, PostgreSQL, Redis, Celery, MinIO/S3, Ubuntu Linux, PM2, Nginx.

**Spec:** `docs/TestPilot_V10.3.docx` (V10.3 chapters 41–50 supplement and take precedence over conflicting earlier candidate details, while preserving M01–M15 and all 101 baseline operationIds).

## Global Constraints

- Preserve every M01–M15 capability and all 101 baseline HTTP operations; new operations must be additive and recorded, as with four Phase 1 M01 operations.
- Treat SQL, OpenAPI DTOs, code examples, and static validation as candidates until verified against implementation and real integration environments.
- PostgreSQL transactions own Run state transitions and durable events; queue delivery is at least once and consumers must be idempotent.
- PASS/FAIL comes from deterministic assertions. Planner, Explorer, Recovery, and Analyzer may propose plans or explanations but cannot decide PASS or rewrite expected assertions.
- Browser Worker runs independently from the PM2 business host, with mTLS, short-lived job scope, resource limits, and enforced egress policy.
- PM2 manages API, Agent Worker, single active Beat, and Browser Gateway. Nginx provides TLS termination, reverse proxy, static assets, and SSE support.
- Secrets are references; never persist plaintext secrets in API responses, Agent state, events, logs, screenshots, or reports.
- Every module must deliver API, database, frontend, tests, and acceptance evidence.
- Never claim an unrun test, migration, image build, or deployment check passed.

## Review Focus

- Cross-organization and cross-project ID enumeration, SSE access, and artifact downloads.
- Concurrent token rotation, invitation consumption, and last-organization-admin changes.
- Illegal Run, Step, Approval, Healing, and BrowserSession transitions, including cancellation races.
- Outbox duplicate delivery, lease expiry, stale attempt-token writes, and Checkpoint recovery around non-idempotent browser actions.
- SSRF through redirects, subresources, DNS rebinding, IPv6, private ranges, and cloud metadata; secret leakage into evidence.

## Phase 0 — Contract and migration reconciliation

**Deliver:** approved route/DTO/permission inventory, one canonical state-transition table, migration provenance and ordering, issue log, ADRs, and traceability matrix.

**Acceptance:** all 101 operationIds have a module owner and an explicit request/response/status/auth/idempotency decision; route aliases and gaps are recorded without deleting operations; 001–005 dependencies and preflight/validation/rollback procedure are explicit.

**Verification:** `python -m openapi_spec_validator api/openapi.yaml`; `python -m pytest tests/contracts -q` (to be added); compare OpenAPI operationIds with the reviewed 101-operation inventory. These commands are future gates until dependencies and tests exist.

## Phase 1 — Platform foundation and M01–M03

**Deliver:** versioned database migrations and migration ledger; FastAPI application and shared error/auth/tenant-scope infrastructure; account registration/verification/login/refresh/logout/session/token APIs; organization/project/member RBAC; environment CRUD, snapshots, SecretRef metadata and network policy model; web shell and Auth, Project, and Environment screens; candidate M03 health-check API/UI delegated through the separate mTLS Gateway and HTTP probe Worker. The minimal probe Worker is not the full M08 Playwright Worker and does not establish production egress acceptance.

**Acceptance:** AT-M01-01..03, AT-M02-01..03, and AT-M03-01..03 pass; cross-tenant access is denied; refresh replay revokes the family; member removal invalidates access; secret values never appear in responses; environment changes do not alter a Run snapshot. M03 acceptance also requires the Gateway/Worker hop, exact allowlist enforcement, DNS pinning, redirect denial, and explicit behavior for SecretRef-backed checks. Production egress enforcement depends on M08 and remains a cross-phase release gate.

**Verification:** current unit gate includes `python -m unittest tests.test_lifecycle_repository tests.test_lifecycle_routes tests.test_api_readiness tests.test_auth_adapters tests.test_auth_routes tests.test_auth_sessions_service tests.test_email_verification_contract tests.test_email_verification_persistence tests.test_email_verification_service tests.test_environment_repository tests.test_environment_snapshot tests.test_environment_snapshot_persistence tests.test_environment_snapshot_routes tests.test_migration_preflight tests.test_migration_runner tests.test_migration_source_chain tests.test_openapi_inventory tests.test_problem_details tests.test_tenancy_repository tests.test_tenancy_routes -v`, plus `python -m compileall -q apps/api tests`. Frontend gates: `npm --prefix apps/web run lint`; `npm --prefix apps/web run typecheck`; `npm --prefix apps/web test`; `npm --prefix apps/web run build`. Migrations 001–007 ran on disposable PostgreSQL 18.3 clusters and, with user authorization, on local `testpilot`; 005 read-only preflight returned five zero counts. The runner's `--apply` then `--check` passed on a fresh disposable cluster, and `--check` verified all seven local hashes. The local app-persistence gate is `python -m unittest tests.test_postgresql_phase1_integration -v` with `TESTPILOT_TEST_DATABASE_URL` set only in the test process. PostgreSQL 15/populated upgrade, Redis 7/Mailpit, refresh replay concurrency, and browser-cookie rotation have additional evidence dated 2026-10-27 below. Full baseline-route authorization, Worker egress and production provider/deployment behavior remain open.

**Additional verification (2026-10-27):** PostgreSQL 15 fresh 001–007 migration and repository integration passed. A separate populated-upgrade fixture applied 001–005, inserted a legacy user and refresh-token family, applied 006–007, and verified that the account remained active and the old token family became a `Legacy device` session. `tests.test_postgresql_phase1_integration` also exercises concurrent refresh replay and family revocation. `tests.test_phase1_live_service_adapters` passed against a disposable Redis 7 instance and Mailpit SMTP sink. `apps/web/e2e/auth-cookie.spec.ts` passed in system Chrome for login, Secure/HttpOnly/SameSite=Lax cookie storage, browser-sent refresh, and cookie rotation. These are local development-service checks, not production SMTP or PostgreSQL rollback evidence. The runtime route inventory is generated by `python -m apps.api.scripts.report_runtime_api_coverage`; this snapshot has handlers for 31/101 baseline operations and 70 remain static-only.

## Phase 2 — Test assets and M04–M06

**Deliver:** generation jobs and review flow; DSL schema/editor; immutable case versions and approval/rollback; suites, plans, timezone-aware schedules, and browser matrices.

**Acceptance:** invalid model output cannot become a runnable version; history stays immutable; schedule triggers are idempotent; DST semantics are explicit and tested.

**Verification:** `python -m pytest tests/api/generations tests/api/cases tests/api/schedules -q`; `python -m pytest tests/contracts/dsl -q`; `pnpm exec playwright test tests/e2e/cases-schedules`.

**Current implementation status (2026-10-27):** M04 candidate generation persistence, project history API/UI, OpenAI-compatible LangChain adapter, Celery task, leased Outbox publisher, expired-worker-lease recovery, and platform-admin generation dead-letter view are present. Owner confirmed up to three automatic retries with 2/4/8-second backoff and `UNCLASSIFIED` human-review tags; code records retry counts, fences expired leases, and labels every proposal for human review. Provider API key and per-model pricing still require runtime configuration. M05 review lifecycle and strict owner-approved DSL v2 validation/API/UI are present with candidate migrations 008–011 unapplied. Publish `name` now persists as `version_name`, with backend, migration, OpenAPI, and UI changes. M06 suite/plan/schedule persistence/API/UI includes ordered suite-case readback, 5-field timezone-aware scheduling, the owner-approved DST rules, 15-second Beat scan, 60-second late window, and schedule Outbox consumption. The owner-approved default serial policy (`concurrency_mode=serial`, `max_concurrent_runs=1`) is validated and persisted across plans and queued Runs; M09 must enforce the cap at execution time. The accepted M09 schema candidate supports schedule-triggered Run creation: it binds an immutable environment snapshot, pins published case versions, expands every selected browser into a RunCase, and emits `RUN_REQUESTED`; browser matrix comes from `test_plans.policy.browser_matrix` and defaults to `['chromium']`. M09 Run execution, state/event APIs, SSE, and isolated Browser Worker are later work. No Phase 2 tests, migrations, builds, browser flows, or deployments were run per owner direction; consolidate validation after implementation is complete. See `docs/decisions/phase-2-open-issues.md` and `docs/phase-2-deferred-validation.md`.

## Phase 3 — Execution backbone and M07–M09

**Deliver:** Outbox, leases, Reconciler, durable events, LangGraph Planner/Explorer and Checkpoint integration, Browser Gateway, independently built Worker, deterministic executor/assertion engine, Run API and SSE console.

**Acceptance:** budget stops further model calls; resuming a Checkpoint does not repeat unknown non-idempotent actions; stale leases cannot write; assertion engine determines outcomes; browser matrix gets isolated sessions; SSE replays without loss/duplication; Worker SSRF cases are blocked.

**Verification:** `python -m pytest tests/agent tests/api/runs tests/integration/outbox_lease -q`; `pnpm exec playwright test tests/e2e/run-console`; build and run the Worker image with egress-negative tests.

## Phase 4 — Recovery and quality analysis, M10–M12

**Deliver:** bounded Locator Recovery and approval; Analyzer classifications, evidence and defect occurrences; visual baselines, approvals, deterministic diffs and masks.

**Acceptance:** original assertions are rechecked; rejection/expiry never applies healing; insufficient evidence is not a confirmed root cause; unapproved/missing baselines cannot yield PASS.

**Verification:** `python -m pytest tests/agent/recovery tests/api/defects tests/visual -q`; `pnpm exec playwright test tests/e2e/healing-visual`.

## Phase 5 — Reporting and integrations, M13–M15

**Deliver:** report snapshots and HTML/PDF/JSON/JUnit export; CI and signed Webhook processing; notifications, quota reservation/settlement, audit chain, usage and operations screens.

**Acceptance:** exported data matches durable Run facts; downloads reauthorize; CI idempotency includes environment/browser matrix; invalid/replayed HMAC is rejected; budget is hard; audit tampering is detectable; notification retries reach a visible dead-letter state.

**Verification:** `python -m pytest tests/reports tests/integrations tests/operations -q`; `pnpm exec playwright test tests/e2e/reports-integrations-admin`.

## Phase 6 — Linux deployment and security hardening

**Deliver:** PM2 ecosystem and startup instructions, Nginx TLS/SSE configuration, independent Worker deployment and egress policy, monitoring, backup and recovery runbooks.

**Acceptance:** PM2 restart/restore works; only one Beat leader is active; Nginx passes configuration validation; SSE reconnects; Worker is non-root and isolated; PostgreSQL and object storage restore together within the organization-approved RPO/RTO.

**Verification:** on a provisioned Ubuntu host run `pm2 start deploy/pm2/ecosystem.config.cjs`, `pm2 save`, `pm2 ls`, `sudo nginx -t`, `curl -N` against SSE, SSRF negative tests, and a DB/object-store restore drill. None is currently runnable in this repository.

## Phase 7 — Full release gate

**Deliver:** complete P0/P1 test evidence, performance and cost baseline, security findings, migration/rollback record, release checklist, and demo recording only after real features exist.

**Acceptance:** no open P0; all modules and operations are traceable; test/build/migration/security/recovery evidence is tied to the same revision; no STATIC-only result is presented as production acceptance.

**Verification:** `python -m pytest -q`; `npm --prefix apps/web run lint && npm --prefix apps/web run typecheck && npm --prefix apps/web test`; `npm --prefix apps/web exec playwright test`; run CI migration, security, load, chaos, and recovery gates.

## Phase 1 file map

Phase 1 code currently includes candidate migrations 001–011, M01 verification/session/invitation/API-token lifecycle, M02 organization/project tenancy, M03 environment CRUD, immutable snapshots, health-check UI/API and the separate mTLS Gateway/minimal HTTP probe Worker, plus the transactional encrypted verification-email Outbox and `/me` self-resource handler. The `/me` candidate correction is recorded because the baseline OpenAPI used an array response and unrelated pagination. The operation-coverage report is stale and must be regenerated after implementation; its historic route count is not current evidence. Owner confirmed SecretRefs remain metadata-only in this phase, so attached-SecretRef health checks return `SECRET_PROVIDER_UNAVAILABLE`; no provider resolver is a Phase 1 code gap. The full Playwright Worker belongs to M08. Remaining M03 questions concern result history/retention, final health-check response-contract reconciliation, explicit port policy and domain-allowlist semantics. Global-scope rejection and SecretRef no-delete are owner-confirmed. Preserve all 101 baseline IDs plus four additive auth operations.

### Phase 1 first implementation slice file map

- API: `apps/api/app/main.py`; `apps/api/app/auth/{adapters,bootstrap,passwords,persistence,routes,sessions,verification}.py`; `apps/api/app/tenancy/{persistence,routes}.py`; `apps/api/app/environments/{persistence,routes,snapshots}.py`; `apps/api/app/core/errors.py`.
- Database/API contracts: `db/migrations/006_email_verification.sql`, `db/migrations/007_auth_sessions.sql`, `api/openapi.yaml`, `docs/TestPilot_AI_V10.3_OpenAPI_修订候选.yaml`, `docs/decisions/phase-1-open-issues.md`.
- Tests: `tests/test_auth_*.py`, `tests/test_email_verification_*.py`, `tests/test_tenancy_*.py`, `tests/test_environment_snapshot*.py`, migration and OpenAPI contract tests.
- Remaining Phase 1 code: no further SecretRef provider integration is required under the owner's metadata-only decision. `/me` has a candidate handler and corrected User-object response; transactional email Outbox, retry/dead-letter, migration 011 and the M03 health-check candidate path are present. Remaining M03 contract questions and all runtime/browser/security checks stay in the unified validation or decision register. Remaining validation (deferred to unified validation): HTTP authorization/contract coverage, invitation/membership/API-token concurrency, SMTP/Redis behavior, PostgreSQL lock-timeout and rollback failure injection, and browser/production deployment evidence. The global-scope restriction and SecretRef no-delete rule are owner-confirmed.
