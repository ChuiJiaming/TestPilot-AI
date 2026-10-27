-- Source: docs/TestPilot_V10.3.docx
-- Status: DESIGN CANDIDATE; extracted for review, not runtime validated.
-- V10.3 Appendix G, candidate repair migration; not executed on PostgreSQL.

-- TestPilot AI V10 incremental schema repair. Target PostgreSQL 15+.
-- Prerequisite: apply 001_schema.sql, then 002_v5_extensions.sql once, in order.
-- Preflight: backup, check pending approvals, affected rows and lock windows.
-- This migration is intentionally not claimed PostgreSQL-runtime tested.
BEGIN;
-- M05: an approved/published case version must be distinguishable from a draft.
ALTER TABLE test_case_versions
ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'DRAFT',
ADD COLUMN IF NOT EXISTS approved_by uuid REFERENCES users(id),
ADD COLUMN IF NOT EXISTS approved_at timestamptz,
ADD COLUMN IF NOT EXISTS published_at timestamptz,
ADD COLUMN IF NOT EXISTS supersedes_version_id uuid REFERENCES test_case_versions(id);
ALTER TABLE test_case_versions DROP CONSTRAINT IF EXISTS test_case_versions_review_status_check;
ALTER TABLE test_case_versions ADD CONSTRAINT test_case_versions_review_status_check
CHECK (review_status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','SUPERSEDED'));
CREATE INDEX IF NOT EXISTS ix_case_versions_review ON test_case_versions(case_id,review_status,version DESC);
-- M05/M10/M12: approvals may target entities outside a Test Run.
ALTER TABLE approvals ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE approvals
ADD COLUMN IF NOT EXISTS project_id uuid REFERENCES projects(id),
ADD COLUMN IF NOT EXISTS target_kind text,
ADD COLUMN IF NOT EXISTS target_id uuid,
ADD COLUMN IF NOT EXISTS correlation_key text;
UPDATE approvals SET target_kind = 'run', target_id = run_id
WHERE target_kind IS NULL AND run_id IS NOT NULL;
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_target_kind_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_target_kind_check
CHECK (target_kind IN ('run','healing','case_version','visual_baseline','risky_action'));
ALTER TABLE approvals ADD CONSTRAINT approvals_target_pair_check
CHECK ((target_kind IS NULL AND target_id IS NULL) OR (target_kind IS NOT NULL AND target_id IS NOT NULL));
CREATE INDEX IF NOT EXISTS ix_approvals_target ON approvals(target_kind,target_id,status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approval_correlation ON approvals(correlation_key)
WHERE correlation_key IS NOT NULL AND status = 'PENDING';
-- M04/M09: persist states documented in the state machine.
ALTER TABLE generation_jobs DROP CONSTRAINT IF EXISTS generation_jobs_status_check;
ALTER TABLE generation_jobs ADD CONSTRAINT generation_jobs_status_check
CHECK(status IN ('QUEUED','RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED'));
ALTER TABLE test_steps DROP CONSTRAINT IF EXISTS test_steps_status_check;
ALTER TABLE test_steps ADD CONSTRAINT test_steps_status_check
CHECK(status IN ('PENDING','RUNNING','WAITING_APPROVAL','PASSED','FAILED','ERROR','SKIPPED','CANCELLED'));
-- M03: versioned secret references and immutable runtime environment snapshots.
CREATE TABLE IF NOT EXISTS secret_ref_versions (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
secret_ref_id uuid NOT NULL REFERENCES secret_refs(id) ON DELETE RESTRICT,
version integer NOT NULL CHECK(version > 0),
provider_version text NOT NULL,
created_by uuid REFERENCES users(id),
created_at timestamptz NOT NULL DEFAULT now(),
retired_at timestamptz,
UNIQUE(secret_ref_id,version)
);
CREATE TABLE IF NOT EXISTS environment_snapshots (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE RESTRICT,
project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
source_updated_at timestamptz,
base_url text NOT NULL,
network_policy jsonb NOT NULL CHECK(jsonb_typeof(network_policy)='object'),
secret_ref_version_id uuid REFERENCES secret_ref_versions(id) ON DELETE RESTRICT,
snapshot_hash text NOT NULL,
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(project_id,snapshot_hash)
);
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS environment_snapshot_id uuid REFERENCES environment_snapshots(id);
CREATE INDEX IF NOT EXISTS ix_runs_environment_snapshot ON test_runs(environment_snapshot_id);
-- For existing live rows, backfill environment_snapshot_id before enforcing NOT NULL.
-- A fresh production create-Run transaction MUST write this FK and its immutable snapshot.
-- M15: one reservation per billable call and atomic settlement policy.
ALTER TABLE quota_reservations
ADD COLUMN IF NOT EXISTS call_id text,
ADD COLUMN IF NOT EXISTS settled_at timestamptz,
ADD COLUMN IF NOT EXISTS actual_cost numeric(18,8) CHECK(actual_cost IS NULL OR actual_cost>=0);
CREATE UNIQUE INDEX IF NOT EXISTS uq_quota_call_per_org ON quota_reservations(organization_id,call_id)
WHERE call_id IS NOT NULL;
-- M12: uniquely approved baseline per action + environment + browser + display context.
ALTER TABLE visual_baselines
ADD COLUMN IF NOT EXISTS action_id uuid,
ADD COLUMN IF NOT EXISTS mask_hash text NOT NULL DEFAULT '',
ADD COLUMN IF NOT EXISTS comparison_profile text NOT NULL DEFAULT 'pixel-v1';
DROP INDEX IF EXISTS uq_visual_approved_dimensions;
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_approved_dimensions_v10
ON visual_baselines (project_id,case_version_id,environment_id,browser,action_id,viewport,
device_scale_factor,color_scheme,mask_hash,comparison_profile)
NULLS NOT DISTINCT WHERE status = 'APPROVED';
-- For legacy baseline rows with action_id=NULL, block new approval until reviewed and backfilled.
-- Agent durability: graph_threads records ownership; actual LangGraph checkpoint
-- tables are created by the chosen official PostgreSQL checkpointer setup().
CREATE TABLE IF NOT EXISTS graph_threads (
run_id uuid PRIMARY KEY REFERENCES test_runs(id) ON DELETE CASCADE,
thread_id text NOT NULL UNIQUE,
checkpoint_backend text NOT NULL DEFAULT 'langgraph-postgres',
schema_version integer NOT NULL DEFAULT 1,
last_checkpoint_id text,
updated_at timestamptz NOT NULL DEFAULT now()
);
-- M14: correlate report exports and audit keys; the source Run already has branch + commit_sha.
CREATE INDEX IF NOT EXISTS ix_runs_commit_branch ON test_runs(source_repository,commit_sha,branch)
WHERE commit_sha IS NOT NULL;
COMMIT;
