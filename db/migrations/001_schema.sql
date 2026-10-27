-- Source: docs/TestPilot_V10.3.docx
-- Status: DESIGN CANDIDATE; extracted for review, not runtime validated.
-- V10.3 Appendix A, candidate initial schema; not executed on PostgreSQL.

-- TestPilot AI V4.0 PostgreSQL 15+ schema. UUID business identifiers;
-- bigint append-only streams and outbox IDs are deliberate exceptions.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE users (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
email text NOT NULL UNIQUE,
password_hash text NOT NULL,
display_name text NOT NULL,
platform_role text NOT NULL DEFAULT 'user' CHECK(platform_role IN ('user','platform_admin')),
status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE organizations (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
name text NOT NULL,
slug text NOT NULL UNIQUE,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE organization_memberships (
organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
role text NOT NULL CHECK(role IN ('org_admin','org_member')),
created_at timestamptz NOT NULL DEFAULT now(),
PRIMARY KEY(organization_id,user_id)
);
CREATE TABLE invitations (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL REFERENCES organizations(id),
project_id uuid,
email text NOT NULL,
role text NOT NULL CHECK(role IN ('org_admin','org_member','project_admin','tester','developer','viewer')),
token_hash text NOT NULL UNIQUE,
expires_at timestamptz NOT NULL,
accepted_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE refresh_tokens (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
user_id uuid NOT NULL REFERENCES users(id),
token_hash text NOT NULL UNIQUE,
token_family uuid NOT NULL,
expires_at timestamptz NOT NULL,
revoked_at timestamptz,
rotated_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_refresh_user ON refresh_tokens(user_id,expires_at);
CREATE TABLE api_tokens (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL REFERENCES organizations(id),
user_id uuid NOT NULL REFERENCES users(id),
name text NOT NULL,
token_hash text NOT NULL UNIQUE,
scopes jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(scopes)='array'),
expires_at timestamptz,
revoked_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE projects (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL REFERENCES organizations(id),
name text NOT NULL,
slug text NOT NULL,
created_by uuid NOT NULL REFERENCES users(id),
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(organization_id,slug),
UNIQUE(id,organization_id)
);
ALTER TABLE invitations ADD CONSTRAINT fk_invitation_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE;
CREATE TABLE project_memberships (
project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
role text NOT NULL CHECK(role IN ('project_admin','tester','developer','viewer')),
created_at timestamptz NOT NULL DEFAULT now(),
PRIMARY KEY(project_id,user_id)
);
CREATE TABLE secret_refs (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL REFERENCES organizations(id),
provider text NOT NULL,
external_key text NOT NULL,
rotated_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(organization_id,provider,external_key)
);
CREATE TABLE environments (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
name text NOT NULL,
base_url text NOT NULL,
secret_ref_id uuid REFERENCES secret_refs(id),
network_policy jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(network_policy)='object'),
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(project_id,name),
UNIQUE(id,project_id)
);
CREATE TABLE test_cases (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
title text NOT NULL,
priority text NOT NULL DEFAULT 'P1' CHECK(priority IN ('P0','P1','P2','P3')),
status text NOT NULL DEFAULT 'active' CHECK(status IN ('draft','active','archived')),
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(id,project_id)
);
CREATE TABLE test_case_versions (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
case_id uuid NOT NULL REFERENCES test_cases(id),
version integer NOT NULL CHECK(version>0),
dsl_schema_version integer NOT NULL DEFAULT 1,
steps jsonb NOT NULL CHECK(jsonb_typeof(steps)='array'),
assertions jsonb NOT NULL CHECK(jsonb_typeof(assertions)='array'),
created_by uuid REFERENCES users(id),
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(case_id,version)
);
CREATE TABLE test_suites (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
name text NOT NULL,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE suite_cases (
suite_id uuid NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
case_id uuid NOT NULL REFERENCES test_cases(id),
sequence integer NOT NULL DEFAULT 0,
PRIMARY KEY(suite_id,case_id)
);
CREATE TABLE test_plans (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
name text NOT NULL,
suite_id uuid REFERENCES test_suites(id),
policy jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(policy)='object'),
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE schedules (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
plan_id uuid NOT NULL REFERENCES test_plans(id),
environment_id uuid NOT NULL REFERENCES environments(id),
cron_expression text NOT NULL,
timezone text NOT NULL DEFAULT 'UTC',
enabled boolean NOT NULL DEFAULT true,
next_fire_at timestamptz,
last_fire_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE test_runs (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
plan_id uuid REFERENCES test_plans(id),
environment_id uuid NOT NULL REFERENCES environments(id),
status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','PREPARING','RUNNING','WAITING_APPROVAL','PAUSING','PAUSED','FINALIZING','PASSED','FAILED','ERROR','CANCELLING','CANCELLED')),
trigger_source text NOT NULL CHECK(trigger_source IN ('manual','schedule','ci','api')),
branch text,
commit_sha text CHECK(commit_sha IS NULL OR commit_sha ~ '^[0-9a-fA-F]{7,64}$'),
browser_matrix jsonb NOT NULL DEFAULT '["chromium"]'::jsonb CHECK(jsonb_typeof(browser_matrix)='array'),
idempotency_key text,
policy jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(policy)='object'),
lease_owner text,
lease_expires_at timestamptz,
heartbeat_at timestamptz,
claimed_at timestamptz,
attempt_token uuid,
started_at timestamptz,
finished_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(project_id,idempotency_key),
UNIQUE(id,project_id)
);
CREATE INDEX ix_runs_queue ON test_runs(status,created_at);
CREATE INDEX ix_runs_lease ON test_runs(lease_expires_at) WHERE status IN ('PREPARING','RUNNING');
CREATE TABLE run_cases (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_id uuid NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
case_version_id uuid NOT NULL REFERENCES test_case_versions(id),
status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','RUNNING','PASSED','FAILED','ERROR','SKIPPED','CANCELLED')),
lease_owner text,
lease_expires_at timestamptz,
heartbeat_at timestamptz,
attempt_token uuid,
started_at timestamptz,
finished_at timestamptz,
UNIQUE(run_id,case_version_id)
);
CREATE TABLE test_steps (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_case_id uuid NOT NULL REFERENCES run_cases(id) ON DELETE CASCADE,
action_id uuid NOT NULL,
sequence integer NOT NULL,
action_type text NOT NULL,
locator jsonb,
expected jsonb,
status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','PASSED','FAILED','ERROR','SKIPPED')),
replay_policy text NOT NULL DEFAULT 'SAFE' CHECK(replay_policy IN ('SAFE','FIXTURE_ONLY','NEVER')),
idempotency_key text,
attempt_token uuid,
started_at timestamptz,
finished_at timestamptz,
UNIQUE(run_case_id,sequence),
UNIQUE(run_case_id,action_id)
);
CREATE TABLE agent_events (
id bigserial PRIMARY KEY,
run_id uuid NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
event_type text NOT NULL,
payload jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(payload)='object'),
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_events_run_cursor ON agent_events(run_id,id);
CREATE TABLE outbox_events (
id bigserial PRIMARY KEY,
aggregate_type text NOT NULL,
aggregate_id uuid NOT NULL,
event_type text NOT NULL,
payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','PUBLISHING','PUBLISHED','DEAD')),
attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
available_at timestamptz NOT NULL DEFAULT now(),
claimed_by text,
claimed_until timestamptz,
published_at timestamptz,
last_error text,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_dispatch ON outbox_events(status,available_at,id);
CREATE TABLE worker_leases (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
resource_type text NOT NULL,
resource_id uuid NOT NULL,
lease_owner text NOT NULL,
attempt_token uuid NOT NULL,
lease_expires_at timestamptz NOT NULL,
heartbeat_at timestamptz NOT NULL DEFAULT now(),
claimed_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(resource_type,resource_id)
);
CREATE TABLE test_artifacts (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
step_id uuid REFERENCES test_steps(id),
run_id uuid NOT NULL REFERENCES test_runs(id),
artifact_type text NOT NULL CHECK(artifact_type IN ('screenshot','video','trace','har','console','report','diff')),
storage_key text NOT NULL UNIQUE,
sha256 text,
redacted boolean NOT NULL DEFAULT false,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE healing_records (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
step_id uuid NOT NULL REFERENCES test_steps(id),
old_locator jsonb NOT NULL,
new_locator jsonb NOT NULL,
evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(evidence)='object'),
confidence numeric(5,4) NOT NULL CHECK(confidence BETWEEN 0 AND 1),
status text NOT NULL DEFAULT 'PROPOSED' CHECK(status IN ('PROPOSED','PENDING_APPROVAL','APPROVED','REJECTED','APPLIED','EXPIRED')),
new_version_id uuid REFERENCES test_case_versions(id),
approved_by uuid REFERENCES users(id),
approved_at timestamptz,
applied_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE approvals (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_id uuid NOT NULL REFERENCES test_runs(id),
requested_by uuid REFERENCES users(id),
decided_by uuid REFERENCES users(id),
kind text NOT NULL,
status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED')),
reason text,
decided_at timestamptz,
expires_at timestamptz NOT NULL,
created_at timestamptz NOT NULL DEFAULT now(),
CHECK(decided_by IS NULL OR requested_by IS DISTINCT FROM decided_by)
);
CREATE TABLE defects (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
fingerprint text NOT NULL,
status text NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','TRIAGED','IN_PROGRESS','RESOLVED','WONT_FIX')),
severity text NOT NULL CHECK(severity IN ('CRITICAL','HIGH','MEDIUM','LOW')),
title text NOT NULL,
analysis_json jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(analysis_json)='object'),
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(project_id,fingerprint)
);
CREATE TABLE defect_occurrences (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
defect_id uuid NOT NULL REFERENCES defects(id) ON DELETE CASCADE,
run_case_id uuid NOT NULL REFERENCES run_cases(id) ON DELETE CASCADE,
step_id uuid REFERENCES test_steps(id),
observed_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(defect_id,run_case_id,step_id)
);
CREATE TABLE visual_baselines (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
case_version_id uuid NOT NULL REFERENCES test_case_versions(id),
browser text NOT NULL,
viewport jsonb NOT NULL,
artifact_id uuid NOT NULL REFERENCES test_artifacts(id),
status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
approved_by uuid REFERENCES users(id),
approved_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE visual_diffs (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
baseline_id uuid NOT NULL REFERENCES visual_baselines(id),
run_case_id uuid NOT NULL REFERENCES run_cases(id),
diff_artifact_id uuid REFERENCES test_artifacts(id),
pixel_ratio numeric(7,6) CHECK(pixel_ratio BETWEEN 0 AND 1),
status text NOT NULL CHECK(status IN ('PASS','FAIL','REVIEW')),
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE reports (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_id uuid NOT NULL UNIQUE REFERENCES test_runs(id),
summary jsonb NOT NULL DEFAULT '{}'::jsonb,
artifact_id uuid REFERENCES test_artifacts(id),
generated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE integrations (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL REFERENCES organizations(id),
kind text NOT NULL,
config jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(config)='object'),
secret_ref_id uuid REFERENCES secret_refs(id),
enabled boolean NOT NULL DEFAULT true,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE webhook_deliveries (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
integration_id uuid NOT NULL REFERENCES integrations(id),
event_id text NOT NULL,
attempts integer NOT NULL DEFAULT 0,
status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','SUCCEEDED','FAILED','DEAD')),
response_code integer,
next_attempt_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(integration_id,event_id)
);
CREATE TABLE notifications (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
user_id uuid NOT NULL REFERENCES users(id),
kind text NOT NULL,
payload jsonb NOT NULL DEFAULT '{}'::jsonb,
read_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE audit_logs (
id bigserial PRIMARY KEY,
organization_id uuid REFERENCES organizations(id),
actor_user_id uuid REFERENCES users(id),
action text NOT NULL,
resource_type text NOT NULL,
resource_id uuid,
metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
previous_hash text,
entry_hash text NOT NULL,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_org_time ON audit_logs(organization_id,id);
CREATE TABLE usage_metrics (
id bigserial PRIMARY KEY,
organization_id uuid NOT NULL REFERENCES organizations(id),
run_id uuid REFERENCES test_runs(id),
metric_name text NOT NULL,
value numeric(18,6) NOT NULL CHECK(value>=0),
unit text NOT NULL,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE quota_policies (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL UNIQUE REFERENCES organizations(id),
max_concurrent_runs integer NOT NULL DEFAULT 3 CHECK(max_concurrent_runs>0),
monthly_token_limit bigint NOT NULL DEFAULT 1000000 CHECK(monthly_token_limit>=0),
updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE system_settings (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
key text NOT NULL UNIQUE,
value jsonb NOT NULL,
updated_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;
