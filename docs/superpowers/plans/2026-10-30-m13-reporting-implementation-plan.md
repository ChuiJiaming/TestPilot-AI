# M13 Reporting and Exports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide immutable Run reports and private, asynchronous HTML/PDF/JSON/JUnit exports that match durable Run facts and reauthorize every read/download.

**Architecture:** Build a deterministic report snapshot in the same database transaction that appends `run.finished`. Serve it through scoped FastAPI routes. Create export jobs transactionally with an Outbox event, render them in the existing Celery worker, and store output as a private report artifact. Add a Reports page that reads and downloads through the authenticated API.

**Tech Stack:** Python 3.11+, FastAPI 0.115+, Pydantic 2.9+, SQLAlchemy 2.0+, PostgreSQL 15+, Celery 5.6+, boto3/S3, ReportLab Platypus, React 18, TypeScript 5.7, Vite 5.4, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-30-phase-5-reporting-integrations-operations-design.md` (M13); `docs/TestPilot_V10.3.docx` Chapter 4 M13; `docs/PLAN.md` Phase 5.

## Global Constraints

- Preserve every baseline operation ID; M13 owns four baseline operations and adds the V10.3 nested export route.
- Run/RunCase/Step database facts determine report status and counts. Model summaries remain labeled hypotheses and never determine PASS/FAIL.
- Snapshot generation is idempotent and shares the `run.finished` transaction. Outbox and worker delivery are at least once.
- Do not include SecretRef values, environment credentials, raw locators, or unredacted form values in report snapshots.
- Export objects remain private, use server-generated keys, are capped at 8 MiB, and are downloaded only after current project authorization.
- Do not edit migrations 001–016 or apply any migration to a persistent database. M13 schema additions go in candidate migration 017.
- Preserve the existing PNG storage limits and behavior for M10–M12 screenshots.

## Review Focus

1. A missing Run or current project access loss returns 404/403 without exposing report or artifact existence — pin with route scope tests.
2. Duplicate `run.finished` handling creates one snapshot and never rewrites it — pin with snapshot and terminal-transition tests.
3. HTML/PDF report text containing markup, control characters, or Chinese text remains escaped and renderable — pin with renderer tests.
4. Missing or expired evidence is labeled `EXPIRED_ARTIFACT`; it does not turn the report page into a 404 — pin with artifact lifecycle tests.
5. A crashed export worker is reclaimed after lease expiry; repeated delivery never creates duplicate artifacts — pin with repository worker-lease tests.

---

### Task 1: Immutable report snapshot

**Files:**

- Create: `apps/api/app/reports/__init__.py`
- Create: `apps/api/app/reports/snapshot.py`
- Create: `apps/api/app/reports/persistence.py`
- Modify: `apps/api/app/runs/repository.py` at `_append_event`
- Test: `tests/test_m13_report_snapshot.py`

**Interfaces:**

- Produces `ensure_report_snapshot(session, run_id: UUID, *, now: datetime) -> ReportRecord`.
- The snapshot contains `schema_version`, `run_id`, `project_id`, `environment_id`, `branch`, `commit_sha`, `browser_matrix`, `trigger_source`, terminal status/timestamps, ordered RunCase and Step summaries, deterministic counts/duration, and artifact IDs/statuses. It excludes secrets, raw locators, and raw expected values.

- [x] **Step 1: Write failing tests**

Add `test_run_finished_event_creates_snapshot_in_same_transaction`, `test_snapshot_is_immutable_on_duplicate_finished_event`, and `test_snapshot_excludes_secret_values_and_orders_results`. Assert stable case/step order, summary counts, and a stable SHA-256 over canonical JSON.

- [x] **Step 2: Run the tests and confirm they fail**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_snapshot.py -q`  
Expected: FAIL because the report model/snapshot hook is not implemented.

- [x] **Step 3: Implement the report model and snapshot builder**

Map existing `reports` table in `ReportRecord`. Build the snapshot from `TestRunRecord`, `RunCaseRecord`, `TestStepRecord`, immutable case-version metadata, and artifact metadata. In `_append_event`, call `ensure_report_snapshot` when `event_type == "run.finished"`; keep creation in the caller's transaction. Return an existing record unchanged if a duplicate finish event is observed.

- [x] **Step 4: Run the tests and confirm they pass**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_snapshot.py -q`  
Expected: all three tests PASS.

### Task 2: Scoped report and baseline API operations

**Files:**

- Create: `apps/api/app/reports/routes.py`
- Modify: `apps/api/app/reports/persistence.py`
- Modify: `apps/api/app/main.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m13_report_routes.py`
- Test: `tests/test_openapi_inventory.py`

**Interfaces:**

- Produces `create_report_router(repository, auth_sessions)`.
- `ReportRepository.get_report(actor_id, run_id) -> dict | None` and `get_export(actor_id, export_id) -> dict | None` enforce current organization/project membership.

- [x] **Step 1: Write failing route and contract tests**

Cover all four baseline IDs: `get__api_v1_test_runs__id__report`, `get__api_v1_test_runs__id__report_export`, `post__api_v1_report_exports`, and `get__api_v1_report_exports__id`. Assert cross-project access is hidden, exports return 202, and the V10.3 `POST /api/v1/reports/{id}/exports` operation is additive. Keep the legacy `GET /test-runs/{id}/report/export` response as the Report DTO and accept its declared `limit`/`cursor` query shape for artifact-reference pagination.

- [x] **Step 2: Run the tests and confirm they fail**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_routes.py tests/test_openapi_inventory.py -q`  
Expected: FAIL because the router and nested export route are absent.

- [x] **Step 3: Implement scoped routes and DTOs**

Add `GET /api/v1/test-runs/{id}/report`, compatibility `GET /api/v1/test-runs/{id}/report/export`, baseline `POST /api/v1/report-exports`, baseline `GET /api/v1/report-exports/{id}`, and additive `POST /api/v1/reports/{id}/exports` (`post__api_v1_reports__id__exports`). Both export-create routes require the `Idempotency-Key` header. Require `run:read` or project write permission as appropriate; do not return a storage key.

- [x] **Step 4: Run the tests and confirm they pass**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_routes.py tests/test_openapi_inventory.py -q`  
Expected: all route and operation-ID assertions PASS. Use an ASGI-capable Linux runner for route tests if the Windows asyncio `socketpair` startup hang recurs.

### Task 3: Durable export jobs and migration 017

**Files:**

- Create: `db/migrations/017_m13_report_exports.sql`
- Modify: `apps/api/app/reports/persistence.py`
- Modify: `apps/api/app/cases/worker.py`
- Modify: `api/openapi.yaml`
- Test: `tests/test_m13_report_exports.py`
- Test: `tests/test_migration_runner.py`
- Modify: `db/migrations/README.md`

**Interfaces:**

- `ReportRepository.request_export(actor_id, report_id, *, format, idempotency_key, now) -> dict`.
- `ReportRepository.claim_export(export_id, *, worker_id, now, lease_seconds=60) -> dict | None`.
- `ReportRepository.finish_export(export_id, *, worker_id, artifact_id, now) -> bool` and `fail_export(export_id, *, worker_id, error_code, now, max_attempts=8) -> bool` are lease-fenced.
- Export statuses are `QUEUED`, `RUNNING`, `SUCCEEDED`, and `FAILED`.

- [x] **Step 1: Write failing idempotency, retry, and fencing tests**

Add `test_same_idempotency_key_returns_same_export`, `test_reusing_key_with_different_format_conflicts`, `test_expired_export_lease_is_reclaimed`, `test_stale_worker_cannot_finish_export`, and `test_export_retry_exhaustion_becomes_failed`. Assert one export and one `REPORT_EXPORT_REQUESTED` Outbox event for a repeated key.

- [x] **Step 2: Run the tests and confirm they fail**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_exports.py -q`  
Expected: FAIL because export persistence and lease fields are absent.

- [x] **Step 3: Add migration 017 and the export repository**

Add nullable historical-safe `requester_id` and `idempotency_key`, unique `(requester_id, idempotency_key)` where both are non-null, `attempts`, `available_at`, `lease_owner`, `lease_expires_at`, and `last_error_code` to `report_exports`. Add `snapshot_hash` to `reports`. Insert the Outbox event in the same transaction as a new queued export. Extend the current Outbox publisher and Beat schedule with `REPORT_EXPORT_REQUESTED`; worker claims are at least once and fenced.

- [x] **Step 4: Run tests and migration-source checks**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_exports.py tests/test_migration_runner.py -q`  
Expected: PASS. Do not apply migration 017 without a disposable PostgreSQL database.

### Task 4: Four renderers and private artifact delivery

**Files:**

- Create: `apps/api/app/reports/rendering.py`
- Create: `apps/api/app/reports/storage.py`
- Modify: `apps/api/app/quality/persistence.py`
- Modify: `apps/api/app/quality/routes.py`
- Modify: `apps/api/app/main.py`
- Modify: `apps/api/pyproject.toml`
- Test: `tests/test_m13_report_rendering.py`
- Test: `tests/test_m13_report_storage.py`
- Test: `tests/test_quality_artifacts.py`

**Interfaces:**

- `ReportRenderer.render(snapshot: dict, format: Literal["html", "pdf", "json", "junit"]) -> RenderedReport`.
- `S3ReportStorage.put(key: str, data: bytes, *, content_type: str) -> str` and `get(key: str, *, max_bytes=8*1024*1024) -> bytes` accept only the four export content types and server-generated report keys.

- [x] **Step 1: Write failing renderer and storage tests**

Add `test_json_and_junit_match_snapshot_facts`, `test_html_escapes_untrusted_run_text`, `test_pdf_uses_simplified_chinese_cid_font`, `test_unknown_format_is_rejected`, `test_report_storage_rejects_wrong_key_type_and_oversize`, and `test_artifact_download_reauthorizes_report_artifact`. Assert no public or long-lived signed URL is emitted.

- [x] **Step 2: Run the tests and confirm they fail**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_rendering.py tests/test_m13_report_storage.py -q`  
Expected: FAIL because renderer/storage support does not exist.

- [x] **Step 3: Implement renderers and private storage**

Add `reportlab>=4,<5` to the API dependencies. Use `SimpleDocTemplate`, `Paragraph`, and `Table`; escape all report-derived text before passing it to ReportLab markup. Register `UnicodeCIDFont("STSong-Light")` for Simplified Chinese. JSON is canonicalized; JUnit uses `xml.etree.ElementTree`; HTML text is escaped. Store with AES256 server-side encryption and keys under `projects/{project_id}/runs/{run_id}/reports/{export_id}.{format}`. Keep PNG validation unchanged; authorize through the existing artifact metadata route and return bytes with a safe content type/disposition.

- [x] **Step 4: Run focused tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_rendering.py tests/test_m13_report_storage.py tests/test_quality_artifacts.py -q`  
Expected: all renderer, storage, and artifact regression tests PASS.

- [x] **Step 5: Connect the Celery task**

Add `testpilot.render_report_export(export_id)` to the current Celery entry point. Render only a claimed export, write its artifact, and commit `SUCCEEDED` only after artifact metadata is persisted. Storage failures retry up to eight attempts; the final state is `FAILED`, and the Run remains terminal.

- [x] **Step 6: Run export worker tests**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_exports.py tests/test_m13_report_storage.py -q`  
Expected: retries, lease recovery, and success/failure status tests PASS.

### Task 5: Reports UI and end-to-end flow

**Files:**

- Create: `apps/web/src/ReportsPage.tsx`
- Create: `apps/web/src/ReportsPage.test.tsx`
- Create: `apps/web/src/reports.css`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/api.ts` only if a typed report API helper is needed
- Create: `apps/web/e2e/reports.spec.ts`

- [x] **Step 1: Write failing page tests**

Cover report loading, four-format selection, queued/running/succeeded/failed states, expired artifact notice, project switch, 403 response, retry, and authenticated blob download/revocation.

- [ ] **Step 2: Run the page tests and confirm they fail**

Run: `npm --prefix apps/web test -- ReportsPage.test.tsx`  
Expected: FAIL because the page and navigation are absent.

- [x] **Step 3: Implement the Reports page**

Add a Reports navigation entry. Fetch report and export status through the existing `api()` helper. Download only through `/api/v1/artifacts/{id}/download` with the user's current access token; create and revoke an object URL. Render loading, empty, denied, expired, and retry states.

- [x] **Step 4: Run frontend checks**

Run: `npm --prefix apps/web test -- ReportsPage.test.tsx`, `npm --prefix apps/web run typecheck`, and `npm --prefix apps/web run lint`  
Expected: tests, typecheck, and lint PASS.

- [ ] **Step 5: Add and run the report browser flow**

`apps/web/e2e/reports.spec.ts` verifies report details, async export polling, artifact download authorization, and an expired-artifact state.

Run: `npm --prefix apps/web run test:e2e -- e2e/reports.spec.ts`  
Expected: the report/export/download flow passes against configured disposable API/storage services.

### Task 6: M13 gate and runtime inventory

**Files:**

- Modify: `api/openapi.yaml`
- Modify: `docs/api-runtime-coverage.md`
- Modify: `docs/traceability-matrix.md`
- Modify: `docs/PLAN.md`
- Test: `tests/test_openapi_inventory.py`

- [x] **Step 1: Assert baseline IDs and additive routes**

Verify the four M13 baseline operation IDs remain present and registered; verify the additive nested export and retry route IDs are recorded separately.

- [x] **Step 2: Regenerate and verify inventory**

Run: `apps/api/.venv/Scripts/python.exe -m apps.api.scripts.report_runtime_api_coverage`  
Expected: all four M13 baseline operations show `Registered`; do not claim all 101 until M14 and M15 are complete.

- [x] **Step 3: Run M13 acceptance checks**

Run: `apps/api/.venv/Scripts/python.exe -m pytest tests/test_m13_report_snapshot.py tests/test_m13_report_routes.py tests/test_m13_report_exports.py tests/test_m13_report_rendering.py tests/test_m13_report_storage.py -q`; then `npm --prefix apps/web run typecheck`, `lint`, `test`, and `build`.  
Expected: all focused tests and frontend checks PASS. PostgreSQL and real S3/MinIO remain external acceptance gates.
