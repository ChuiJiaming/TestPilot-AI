# Candidate PostgreSQL migrations

Target: PostgreSQL 15+. The numbered files preserve the current V10.3 candidate chain:

1. `001_schema.sql` — extracted from Appendix A of `docs/TestPilot_V10.3.docx`.
2. `002_v5_extensions.sql` — extracted from Appendix B, including the adjacent V6 state/integration extension block because the document names no separate version for that block. This is a provenance ambiguity to resolve before freezing migration history.
3. `003_v10_repairs.sql` — extracted from Appendix G.
4. `004_v102_integrity.sql` — extracted from Chapter 33.3.
5. `005_scope_linkage.sql` — copied from `docs/TestPilot_AI_V10.3_数据库迁移_005.sql`.
6. `006_email_verification.sql` — Phase 1 addition for the confirmed pending-email activation flow.
7. `007_auth_sessions.sql` — Phase 1 multi-device session metadata and refresh-family linkage; existing families are backfilled as `Legacy device`.
8. `008_case_version_review.sql` — Phase 2 candidate fields for immutable DSL snapshots, review state, published-version pinning and case-version approval targets. Not yet applied or database-validated.
9. `009_generation_and_scheduling.sql` — Phase 2 candidate generation-job envelope, ordered suite metadata, idempotent manual schedule-trigger intents and schedule state reasons. Not yet applied or database-validated.
10. `010_run_browser_and_environment_pins.sql` — Phase 2/M09 candidate RunCase browser expansion, environment snapshot FK, and schedule-trigger reason/source/error fields. Legacy RunCase rows retain `NULL` browser names; no browser is inferred. Not yet applied or database-validated.
11. `011_verification_email_outbox.sql` — Phase 1 M01 transactional email outbox with encrypted verification-token payload, leases, retry, dead-letter, and cancellation states. Requires `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS`; never applied or database-validated.

These are source candidates, not database-validated migrations. The original Word document is the source of record for 001–004; compare future edits against those sections. Do not rerun a numbered migration against a database where it has already committed. Before applying, establish a schema migration ledger and determine the database's applied version. A failed transaction may be retried only after confirming PostgreSQL rolled back the entire migration.

`005_preflight_and_validate.sql` is read-only and reports the five legacy project-scope mismatch counts described in Chapter 43. It does not apply or validate constraints. Run it after 005 in a disposable or reviewed staging database; require all counts to be zero before a separately reviewed `VALIDATE CONSTRAINT` operation.

Use `python -m apps.api.scripts.migrate --check` for read-only ledger and checksum verification. The default mode never applies SQL. `--apply` serializes writers with a PostgreSQL advisory lock, refuses checksum changes or history gaps, strips source transaction wrappers, and commits each migration body together with its SHA-256 ledger row. It excludes `005_preflight_and_validate.sql`. On databases already populated outside this runner, seed/verify the ledger only from independently recorded migration evidence; do not infer applied status from table presence.

Validation record (2026-10-26): migrations 001–007 were applied sequentially to a fresh disposable PostgreSQL 18.3 cluster and separately to the user-authorized local `testpilot` PostgreSQL 18.3 database. A `public.schema_migrations` ledger stores each applied filename and SHA-256. The temporary clusters were removed after validation; the local database is persistent. The read-only 005 preflight returned five zero counts on both disposable runs. The new runner's `--apply` then `--check` passed on a fresh disposable cluster, and `--check` verified the seven local ledger checksums without applying SQL. Targeted API repository integration passes against the local database. This is not a PostgreSQL 15 compatibility, populated upgrade, rollback, concurrency, authenticated HTTP, or production-readiness claim. Migrations 008–011 remain unapplied candidates.
