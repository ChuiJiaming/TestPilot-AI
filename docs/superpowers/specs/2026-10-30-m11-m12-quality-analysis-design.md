# M11 M12 Quality Analysis Design

## Goal

Complete Phase 4 modules M11 and M12 and provide the shared screenshot artifact boundary still missing from M10. M11 groups failed RunCases into auditable defects without treating an AI hypothesis as a confirmed root cause. M12 stores versioned visual baselines, requires a human decision before use, and compares screenshots with a deterministic pixel algorithm.

## Authority and scope

The binding behavior comes from `docs/TestPilot_V10.3.docx`, Chapter 4, M11 and M12, together with the Phase 4 acceptance criteria in `docs/PLAN.md`. Existing operation IDs are preserved. Additive routes are recorded in `api/openapi.yaml` and the runtime coverage inventory.

Included:

- Private, project-scoped screenshot and diff artifacts, with authenticated downloads.
- Redacted screenshots linked to Run, RunCase, action and Step, including failed-action evidence for M10.
- M11 failure classification, deterministic fingerprint grouping, occurrence history, evidence references, human triage and chained audit entries.
- M12 visual DSL assertions, baseline proposals and approval, deterministic masked pixel diffs, and a review UI.
- API and UI contract updates, migration source, and focused acceptance coverage.

Not included: applying migrations to a persistent database, deploying or configuring production S3/MinIO credentials, production browser-network acceptance, or claiming Phase 1–3 production readiness.

## Design decisions

### Artifact boundary

Use one artifact service for screenshots and generated diffs. The service stores content in a private S3-compatible bucket and only stores server-generated object keys, SHA-256, content type, size and scoped metadata in PostgreSQL. No caller supplies a storage key and no public or long-lived signed URL is persisted. Downloads first authorize the requesting user against the artifact's project. Browser screenshots travel over the existing mTLS Gateway and job-token-scoped session, are bounded PNG responses, and mask form controls before leaving the Worker.

### M11 analyzer

The Run graph gives the Analyzer only bounded, redacted failure metadata and evidence IDs. A structured model result may suggest one of `LOCATOR`, `ASSERTION`, `ENVIRONMENT`, `INFRA`, `SECURITY`, `DATA`, or `FLAKY`; deterministic evidence validation rejects unsupported categories or evidence IDs. When there is no admissible evidence, the persisted analysis is `INSUFFICIENT_EVIDENCE`. Model output is always a hypothesis. Only an explicit human triage patch can confirm a root cause. Fingerprints hash a normalized failure signature and never store raw expected values or page text. One defect can link many independent RunCase occurrences.

### M12 visual comparison

Visual assertions reference an optional baseline ID. The engine selects the captured screenshot for the baseline action, requires an `APPROVED` baseline and an exact match for project, case version, environment, browser, viewport, device scale factor, color scheme and comparison profile. Missing or unapproved baselines produce `NEED_BASELINE`; mismatched dimensions produce a non-PASS result. The approved baseline versions its normalized mask rectangles, per-channel tolerance and maximum changed-pixel ratio. Pillow's RGB pixel comparison emits a deterministic ratio and a diff PNG. Only the server-side comparison result can pass the assertion.

### Approvals and audit

Baseline approval rechecks project scope and current state inside a database transaction, enforces separation of duties, and relies on the database's unique approved-dimensions constraint for concurrent requests. Defect changes append the existing organization-scoped hash-chained audit record in the same transaction. Artifact writes use a server-generated object key; a failed database insert triggers best-effort object deletion, and an unavailable store never turns a visual assertion into PASS.

## Acceptance

- A defect groups multiple RunCases while each occurrence retains its own browser/environment/run/step and evidence links.
- Automated analysis cannot confirm a root cause; missing evidence is explicitly `INSUFFICIENT_EVIDENCE`; human changes retain actor, reason and before/after history.
- An unauthorized project member cannot list, triage or download evidence from another project.
- A baseline is not usable until approved by a different project-authorized user.
- At most one approved baseline exists for an exact visual context; mask, threshold and comparison profile are immutable baseline configuration.
- Pixel ratio and diff artifact are reproducible; masked regions do not count toward the denominator.
- Missing, rejected, pending or context-mismatched baselines cannot produce PASS.
- Screenshot or diff bytes are not stored in JSONB or exposed by public object URLs.
