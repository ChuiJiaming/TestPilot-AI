-- Source: docs/TestPilot_V10.3.docx
-- Status: DESIGN CANDIDATE; extracted for review, not runtime validated.
-- V10.2 Chapter 33.3, candidate integrity migration; not executed on PostgreSQL.

-- TestPilot AI V10.2 schema integrity hardening (PostgreSQL 15+)
-- Migration chain: 001_schema.sql -> 002_v5_extensions.sql ->
--                  003_v10_repairs.sql -> 004_v102_integrity.sql
-- STATUS: DESIGN CANDIDATE. MUST test on fresh DB and cloned pre-production DB.
-- Before applying: audit cross-project mismatches and fix existing dirty rows.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
-- Composite FK targets must expose the exact unique referenced column tuple.
ALTER TABLE test_plans ADD CONSTRAINT uq_test_plans_id_project UNIQUE (id, project_id);
ALTER TABLE test_suites ADD CONSTRAINT uq_test_suites_id_project UNIQUE (id, project_id);
ALTER TABLE test_runs ADD CONSTRAINT uq_test_runs_id_environment UNIQUE (id, environment_id);
ALTER TABLE environment_snapshots ADD CONSTRAINT uq_environment_snapshots_id_env_project UNIQUE (id, environment_id, project_id);
-- Always check for invalid legacy combinations before VALIDATE CONSTRAINT.
-- These NOT VALID constraints still protect *new and updated* rows.
ALTER TABLE test_runs ADD CONSTRAINT fk_test_runs_environment_scope_v102
FOREIGN KEY (environment_id, project_id) REFERENCES environments (id, project_id) NOT VALID;
ALTER TABLE test_runs ADD CONSTRAINT fk_test_runs_plan_scope_v102
FOREIGN KEY (plan_id, project_id) REFERENCES test_plans (id, project_id) NOT VALID;
ALTER TABLE test_runs ADD CONSTRAINT fk_test_runs_snapshot_scope_v102
FOREIGN KEY (environment_snapshot_id, environment_id, project_id)
REFERENCES environment_snapshots (id, environment_id, project_id) NOT VALID;
ALTER TABLE environment_snapshots ADD CONSTRAINT fk_snapshot_environment_scope_v102
FOREIGN KEY (environment_id, project_id) REFERENCES environments (id, project_id) NOT VALID;
-- Preserve project-level referential integrity for schedules and suite items.
ALTER TABLE schedules ADD COLUMN project_id uuid;
UPDATE schedules s SET project_id = p.project_id FROM test_plans p
WHERE s.plan_id = p.id AND s.project_id IS NULL;
ALTER TABLE schedules ADD CONSTRAINT fk_schedules_plan_scope_v102
FOREIGN KEY (plan_id, project_id) REFERENCES test_plans (id, project_id) NOT VALID;
ALTER TABLE schedules ADD CONSTRAINT fk_schedules_environment_scope_v102
FOREIGN KEY (environment_id, project_id) REFERENCES environments (id, project_id) NOT VALID;
ALTER TABLE suite_cases ADD COLUMN project_id uuid;
UPDATE suite_cases sc SET project_id = s.project_id FROM test_suites s
WHERE sc.suite_id = s.id AND sc.project_id IS NULL;
ALTER TABLE suite_cases ADD CONSTRAINT fk_suite_cases_suite_scope_v102
FOREIGN KEY (suite_id, project_id) REFERENCES test_suites (id, project_id) NOT VALID;
ALTER TABLE suite_cases ADD CONSTRAINT fk_suite_cases_case_scope_v102
FOREIGN KEY (case_id, project_id) REFERENCES test_cases (id, project_id) NOT VALID;
ALTER TABLE schedules ADD CONSTRAINT ck_schedules_project_required_v102
CHECK (project_id IS NOT NULL) NOT VALID;
ALTER TABLE suite_cases ADD CONSTRAINT ck_suite_cases_project_required_v102
CHECK (project_id IS NOT NULL) NOT VALID;
-- Live historical approval rows can be backfilled before validation.
ALTER TABLE approvals ADD CONSTRAINT ck_approvals_target_required_v102
CHECK (target_kind IS NOT NULL AND target_id IS NOT NULL) NOT VALID;
ALTER TABLE approvals ADD CONSTRAINT ck_approvals_project_required_v102
CHECK (project_id IS NOT NULL) NOT VALID;
ALTER TABLE approvals ADD CONSTRAINT fk_approvals_run_scope_v102
FOREIGN KEY (run_id, project_id) REFERENCES test_runs (id, project_id) NOT VALID;
ALTER TABLE test_runs ADD CONSTRAINT ck_run_snapshot_required_v102
CHECK (environment_snapshot_id IS NOT NULL) NOT VALID;
-- Per-call token reservations and approved baselines need non-null dimensions.
ALTER TABLE quota_reservations ADD CONSTRAINT ck_quota_call_required_v102
CHECK (call_id IS NOT NULL) NOT VALID;
ALTER TABLE visual_baselines ADD CONSTRAINT ck_visual_approved_dimensions_v102
CHECK (status <> 'APPROVED' OR (environment_id IS NOT NULL AND action_id IS NOT NULL)) NOT VALID;
ALTER TABLE visual_baselines ADD CONSTRAINT fk_visual_environment_scope_v102
FOREIGN KEY (environment_id, project_id) REFERENCES environments (id, project_id) NOT VALID;
-- Worker leases own execution claims. test_runs/run_cases lease columns are
-- denormalized snapshots (read-only projections); writers update worker_leases first.
CREATE INDEX IF NOT EXISTS ix_worker_leases_expire_v102 ON worker_leases (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_events_run_cursor_v102 ON agent_events (run_id, id);
CREATE INDEX IF NOT EXISTS ix_snapshots_env_time_v102 ON environment_snapshots (environment_id, created_at DESC);
COMMIT;
