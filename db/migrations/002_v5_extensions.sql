-- Source: docs/TestPilot_V10.3.docx
-- Status: DESIGN CANDIDATE; extracted for review, not runtime validated.
-- V10.3 Appendix B, V5 and V6 extension blocks; candidate, version ordered, not safely rerunnable.

-- V5 full-feature extensions: additive migration, idempotent only on fresh V4 schema.
BEGIN;
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS parent_run_id uuid REFERENCES test_runs(id);
ALTER TABLE run_cases ADD COLUMN IF NOT EXISTS browser text NOT NULL DEFAULT 'chromium' CHECK(browser IN ('chromium','firefox','webkit'));
ALTER TABLE run_cases DROP CONSTRAINT IF EXISTS run_cases_run_id_case_version_id_key;
ALTER TABLE run_cases ADD CONSTRAINT uq_run_case_browser UNIQUE(run_id,case_version_id,browser);
ALTER TABLE test_steps DROP CONSTRAINT IF EXISTS test_steps_status_check;
ALTER TABLE test_steps ADD CONSTRAINT test_steps_status_check CHECK(status IN ('PENDING','RUNNING','PASSED','FAILED','ERROR','SKIPPED','CANCELLED'));
CREATE TABLE IF NOT EXISTS generation_jobs (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
project_id uuid NOT NULL REFERENCES projects(id),
requester_id uuid NOT NULL REFERENCES users(id),
requirement text NOT NULL,
model_name text NOT NULL,
status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
result jsonb CHECK(result IS NULL OR jsonb_typeof(result)='object'),
error_code text,
created_at timestamptz NOT NULL DEFAULT now(),
finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS browser_sessions (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_case_id uuid NOT NULL REFERENCES run_cases(id) ON DELETE CASCADE,
browser text NOT NULL CHECK(browser IN ('chromium','firefox','webkit')),
worker_instance text NOT NULL,
attempt_token uuid NOT NULL,
status text NOT NULL DEFAULT 'ALLOCATING' CHECK(status IN ('ALLOCATING','READY','BUSY','CLOSING','CLOSED','LOST')),
lease_expires_at timestamptz NOT NULL,
heartbeat_at timestamptz NOT NULL DEFAULT now(),
created_at timestamptz NOT NULL DEFAULT now(),
closed_at timestamptz,
UNIQUE(run_case_id,browser,attempt_token)
);
CREATE INDEX IF NOT EXISTS ix_browser_session_expire ON browser_sessions(lease_expires_at) WHERE status IN ('ALLOCATING','READY','BUSY');
CREATE TABLE IF NOT EXISTS processed_events (
consumer_name text NOT NULL,
event_id bigint NOT NULL REFERENCES outbox_events(id),
processed_at timestamptz NOT NULL DEFAULT now(),
PRIMARY KEY(consumer_name,event_id)
);
CREATE TABLE IF NOT EXISTS agent_checkpoints (
thread_id text NOT NULL,
checkpoint_id text NOT NULL,
parent_checkpoint_id text,
checkpoint jsonb NOT NULL,
metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
created_at timestamptz NOT NULL DEFAULT now(),
PRIMARY KEY(thread_id,checkpoint_id)
);
CREATE INDEX IF NOT EXISTS ix_checkpoints_thread_time ON agent_checkpoints(thread_id,created_at DESC);
CREATE TABLE IF NOT EXISTS report_exports (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_id uuid NOT NULL REFERENCES test_runs(id),
format text NOT NULL CHECK(format IN ('html','pdf','json','junit')),
status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
artifact_id uuid REFERENCES test_artifacts(id),
created_at timestamptz NOT NULL DEFAULT now(),
finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS notification_preferences (
user_id uuid NOT NULL REFERENCES users(id),
channel text NOT NULL CHECK(channel IN ('in_app','email','webhook')),
event_type text NOT NULL,
enabled boolean NOT NULL DEFAULT true,
PRIMARY KEY(user_id,channel,event_type)
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
id bigserial PRIMARY KEY,
delivery_type text NOT NULL CHECK(delivery_type IN ('webhook','email','notification')),
delivery_id uuid NOT NULL,
attempt integer NOT NULL CHECK(attempt>0),
status text NOT NULL CHECK(status IN ('SUCCESS','RETRY','DEAD')),
response_status integer,
error_text text,
created_at timestamptz NOT NULL DEFAULT now(),
UNIQUE(delivery_type,delivery_id,attempt)
);
CREATE TABLE IF NOT EXISTS quota_reservations (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
organization_id uuid NOT NULL REFERENCES organizations(id),
run_id uuid REFERENCES test_runs(id),
tokens_reserved bigint NOT NULL CHECK(tokens_reserved>=0),
tokens_consumed bigint NOT NULL DEFAULT 0 CHECK(tokens_consumed>=0),
status text NOT NULL DEFAULT 'RESERVED' CHECK(status IN ('RESERVED','COMMITTED','RELEASED')),
created_at timestamptz NOT NULL DEFAULT now(),
closed_at timestamptz
);
ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS request_nonce text;
ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS signed_at timestamptz;
ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS signature_version integer NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_nonce ON webhook_deliveries(integration_id,request_nonce) WHERE request_nonce IS NOT NULL;
ALTER TABLE visual_baselines ADD COLUMN IF NOT EXISTS environment_id uuid REFERENCES environments(id);
ALTER TABLE visual_baselines ADD COLUMN IF NOT EXISTS device_scale_factor numeric(5,2) NOT NULL DEFAULT 1;
ALTER TABLE visual_baselines ADD COLUMN IF NOT EXISTS color_scheme text NOT NULL DEFAULT 'light';
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_approved_dimensions ON visual_baselines(project_id,case_version_id,browser,environment_id,md5(viewport::text),device_scale_factor,color_scheme) WHERE status='APPROVED';
CREATE INDEX IF NOT EXISTS ix_generation_project ON generation_jobs(project_id,created_at DESC);
CREATE INDEX IF NOT EXISTS ix_run_parent ON test_runs(parent_run_id) WHERE parent_run_id IS NOT NULL;
-- Outbox event is at-least-once: do not assume publish status and queue send are atomic.
COMMIT;
-- V6: state consistency and integration evidence. PostgreSQL authoritative run leases live in worker_leases.
BEGIN;
ALTER TABLE healing_records DROP CONSTRAINT IF EXISTS healing_records_status_check;
ALTER TABLE healing_records ADD CONSTRAINT healing_records_status_check CHECK (status IN ('PROPOSED','PENDING_APPROVAL','APPROVED','REJECTED','APPLIED','EXPIRED','CANCELLED'));
ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS source_repository text;
CREATE INDEX IF NOT EXISTS ix_outbox_claim_recovery ON outbox_events(claimed_until,id) WHERE status = 'PUBLISHING';
CREATE INDEX IF NOT EXISTS ix_session_run_case_browser ON browser_sessions(run_case_id,browser,attempt_token);
CREATE TABLE IF NOT EXISTS sse_tickets (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
run_id uuid NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
user_id uuid NOT NULL REFERENCES users(id),
token_hash text NOT NULL UNIQUE,
expires_at timestamptz NOT NULL,
used_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sse_ticket_valid ON sse_tickets(run_id,expires_at) WHERE used_at IS NULL;
COMMIT;
