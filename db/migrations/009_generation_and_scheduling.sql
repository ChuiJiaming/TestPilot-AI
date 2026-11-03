BEGIN;

ALTER TABLE test_suites ADD COLUMN IF NOT EXISTS description text;

-- Migration 002 already created generation_jobs with the V10.3 baseline shape
-- (requester_id NOT NULL, object-only result, no cost/lease columns). This
-- migration reconciles that frozen table with the Phase 2 record shape instead
-- of declaring a second, incompatible definition of the same table.
ALTER TABLE generation_jobs
  ADD COLUMN IF NOT EXISTS created_by uuid REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS model_version text,
  ADD COLUMN IF NOT EXISTS cost numeric(18,6) NOT NULL DEFAULT 0 CHECK (cost >= 0),
  ADD COLUMN IF NOT EXISTS input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  ADD COLUMN IF NOT EXISTS output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  ADD COLUMN IF NOT EXISTS automatic_retries integer NOT NULL DEFAULT 0
    CHECK (automatic_retries BETWEEN 0 AND 3),
  ADD COLUMN IF NOT EXISTS cancel_requested boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS attempt_token uuid,
  ADD COLUMN IF NOT EXISTS started_at timestamptz;

-- Legacy rows keep their original requester in created_by. New Phase 2 rows
-- write created_by only, so the legacy NOT NULL requirement must be relaxed.
UPDATE generation_jobs SET created_by = requester_id WHERE created_by IS NULL;
ALTER TABLE generation_jobs ALTER COLUMN requester_id DROP NOT NULL;

-- Phase 2 proposals may persist a bare JSON array; the baseline constraint
-- allowed objects only.
ALTER TABLE generation_jobs DROP CONSTRAINT IF EXISTS generation_jobs_result_check;
ALTER TABLE generation_jobs ADD CONSTRAINT generation_jobs_result_check
  CHECK (result IS NULL OR jsonb_typeof(result) IN ('object','array'));

-- Status values were already widened by migration 003.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'uq_generation_jobs_id_project'
  ) THEN
    ALTER TABLE generation_jobs
      ADD CONSTRAINT uq_generation_jobs_id_project UNIQUE (id, project_id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_generation_jobs_queue ON generation_jobs(status, created_at, id);
CREATE INDEX IF NOT EXISTS ix_generation_jobs_project_history
  ON generation_jobs(project_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS generation_acceptances (
  generation_job_id uuid NOT NULL REFERENCES generation_jobs(id) ON DELETE RESTRICT,
  proposal_id uuid NOT NULL,
  case_id uuid NOT NULL REFERENCES test_cases(id) ON DELETE RESTRICT,
  case_version_id uuid NOT NULL REFERENCES test_case_versions(id) ON DELETE RESTRICT,
  accepted_by uuid REFERENCES users(id),
  accepted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (generation_job_id, proposal_id),
  UNIQUE (case_version_id)
);

CREATE TABLE IF NOT EXISTS schedule_trigger_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id uuid NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  idempotency_key text NOT NULL,
  request_intent text NOT NULL,
  status text NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING','ENQUEUED','COMPLETED','FAILED')),
  outbox_event_id bigint REFERENCES outbox_events(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (schedule_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_schedule_trigger_pending ON schedule_trigger_requests(status, created_at);

CREATE TABLE IF NOT EXISTS schedule_state_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id uuid NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  actor_user_id uuid REFERENCES users(id),
  event_type text NOT NULL CHECK (event_type IN ('ENABLED','DISABLED')),
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_schedule_state_events ON schedule_state_events(schedule_id, created_at);

COMMIT;
