# M11 M12 Quality Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement Phase 4 M11 defect analysis and M12 approved visual regression, including the shared private screenshot artifact service required by both modules and by M10 evidence.

**Architecture:** Add a quality-analysis API/repository boundary over the existing SQLAlchemy session factory. A private S3-compatible artifact adapter stores PNGs, while the mTLS Browser Gateway transports bounded, form-masked screenshots from the Worker. The Run graph persists redacted defect hypotheses and visual assertion results; the React quality workspace exposes defect triage, baseline approval and visual diff review.

**Tech Stack:** FastAPI, Pydantic 2, SQLAlchemy, PostgreSQL, boto3 S3 client, Pillow, Playwright Worker, React 18, TypeScript, Vitest/pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-m11-m12-quality-analysis-design.md`; binding requirements: `docs/TestPilot_V10.3.docx` Chapter 4 M11/M12 and `docs/PLAN.md` Phase 4.

## Global Constraints

- Preserve all 101 baseline HTTP operation IDs; additive routes are separately inventoried.
- Model hypotheses never determine Run status or confirm root cause.
- `INSUFFICIENT_EVIDENCE`, `NEED_BASELINE`, and non-PASS context mismatch must remain explicit.
- Project authorization is checked before every defect, baseline, occurrence and artifact read/write.
- Baseline dimensions are project, case version, environment, browser, action, viewport, DPR, color scheme, mask and comparison profile.
- Never store screenshot bytes or public storage URLs in PostgreSQL JSONB.

## Review Focus

- Screenshot transport exceeding configured limits is rejected before allocating an unbounded buffer.
- A missing artifact store or missing/corrupt screenshot fails visual evaluation closed and does not fail unrelated deterministic assertions.
- Concurrent approval of two baselines for the same dimensions leaves exactly one APPROVED.
- A cross-project artifact ID, case-version ID, environment ID or baseline ID is concealed as not found.
- Recovered action screenshots remain linked to the published source action, not the one-time replay action ID.

---

### Task 1: Private screenshot artifact boundary

**Files:**
- Create: `apps/api/app/quality/artifacts.py`
- Create: `apps/api/app/quality/persistence.py`
- Create: `apps/api/app/quality/routes.py`
- Create: `db/migrations/016_m11_m12_quality_analysis_visual.sql`
- Test: `tests/test_quality_artifacts.py`
- Modify: `apps/api/app/main.py`, `apps/api/app/auth/bootstrap.py`, `apps/api/app/runs/agent.py`, `apps/api/app/runs/executor.py`, `apps/api/app/runs/browser.py`, `apps/browser_gateway/browser_gateway_sessions.mjs`, `apps/browser_worker/browser_sessions.mjs`, relevant Worker/Gateway tests, and `apps/api/pyproject.toml`.

**Interfaces:**
- Produces `ArtifactStorage.put(key, data, content_type)`, `get(key)`, and `delete(key)`; `QualityRepository.save_screenshot(...) -> artifact DTO`; authenticated download and project artifact listing.
- Worker screenshot response contains base64 PNG plus viewport, DPR, color scheme and bounded byte count; request remains scoped by the existing job token and worker affinity.

- [x] Write tests for private object-key generation, content/hash limits, project-scoped download, bounded Worker screenshot, and migration source chain.
- [x] Run the focused artifact and Worker tests to observe expected failures.
- [x] Implement S3-compatible storage, artifact metadata migration/repository, authenticated API reads, and redacted screenshot transport.
- [x] Run the focused artifact and Worker tests; verify expected object metadata and authorization behavior.

### Task 2: M11 failure analysis and defect triage

**Files:**
- Create: `apps/api/app/quality/analyzer.py`
- Create: `tests/test_m11_analyzer.py`, `tests/test_m11_quality_routes.py`, `tests/test_m11_quality_persistence.py`
- Modify: `apps/api/app/quality/persistence.py`, `apps/api/app/quality/routes.py`, `apps/api/app/runs/planner.py`, `apps/api/app/runs/agent.py`, `apps/api/app/runs/executor.py`, `api/openapi.yaml`.

**Interfaces:**
- `FailureAnalysis` returns a validated category, title, hypothesis, confidence, evidence IDs and `evidence_status`; root cause remains unconfirmed until human triage.
- `QualityRepository.record_failure(...)` returns the deduplicated defect and occurrence; `patch_defect(...)` appends hash-chained audit metadata transactionally.
- Existing list/detail/occurrence/patch operation IDs remain unchanged.

- [x] Write tests for all seven classes, invalid model evidence, insufficient evidence, multi-RunCase fingerprint grouping, triage history, status transitions and project isolation.
- [x] Run the focused M11 tests to observe expected failures.
- [x] Add bounded Analyzer integration to the Run graph, deterministic fingerprinting, occurrence persistence, scoped APIs and audit history.
- [x] Run the focused M11 tests; confirm human edits are auditable and AI output cannot set `CONFIRMED`.

### Task 3: M12 versioned visual baselines and diffs

**Files:**
- Create: `apps/api/app/quality/visual.py`
- Create: `tests/test_m12_visual_diff.py`, `tests/test_m12_visual_routes.py`, `tests/test_m12_visual_persistence.py`
- Modify: `apps/api/app/cases/dsl.py`, `apps/api/app/runs/executor.py`, `apps/api/app/quality/persistence.py`, `apps/api/app/quality/routes.py`, `apps/api/pyproject.toml`, `api/openapi.yaml`.

**Interfaces:**
- `compare_pngs(expected, actual, *, masks, channel_tolerance) -> {pixel_ratio, changed_pixels, compared_pixels, diff_png}`.
- Visual assertion references an optional `baseline_id`; missing/unapproved baseline is stored as `NEED_BASELINE` and never passes.
- Proposal, decision, project baseline listing and diff detail/list return scoped DTOs with artifact IDs, threshold/mask/profile and approval state.

- [x] Write deterministic image comparison, mask, dimension mismatch, baseline approval/unique context, separation-of-duties, fail-closed assertion and project-scope tests.
- [x] Run the focused M12 tests to observe expected failures.
- [x] Implement versioned threshold/mask fields, immutable approved baseline behavior, diff PNG artifacts and deterministic Run assertion handling.
- [x] Run the focused M12 tests and verify the same inputs yield the same pixel ratio and diff bytes.

### Task 4: Quality workspace and Phase 4 status

**Files:**
- Create: `apps/web/src/QualityPage.tsx`, `apps/web/src/quality.css`, `apps/web/src/QualityPage.test.tsx`
- Modify: `apps/web/src/App.tsx`, `apps/web/src/api.ts`, `docs/PLAN.md`, `docs/traceability-matrix.md`, `docs/api-runtime-coverage.md`, `docs/api-source-coverage.md`, `apps/api/README.md`.

- [x] Write UI tests for defect list/triage, pending baseline decision, artifact-backed before/diff display, empty/error/loading states and project switching.
- [x] Run the focused UI test to observe expected failures.
- [x] Add the project-scoped quality workspace, authenticated artifact image loading, triage controls, baseline proposal/approval and diff review.
- [x] Run focused UI tests, typecheck, lint and build; regenerate API coverage and update Phase 4 source/validation status without claiming unrun external acceptance.

