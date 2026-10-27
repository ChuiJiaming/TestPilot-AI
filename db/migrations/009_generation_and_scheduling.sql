BEGIN;

ALTER TABLE test_suites ADD COLUMN description text;

CREATE TABLE generation_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  created_by uuid REFERENCES users(id),
  requirement text NOT NULL,
  model_name text NOT NULL,
  model_version text,
  status text NOT NULL DEFAULT 'QUEUED'
    CHECK (status IN ('QUEUED','RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED')),
  result jsonb CHECK (result IS NULL OR jsonb_typeof(result) IN ('object','array')),
  error_code text,
  cost numeric(18,6) NOT NULL DEFAULT 0 CHECK (cost >= 0),
  input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  automatic_retries integer NOT NULL DEFAULT 0 CHECK (automatic_retries BETWEEN 0 AND 3),
  cancel_requested boolean NOT NULL DEFAULT false,
  lease_expires_at timestamptz,
  attempt_token uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  UNIQUE (id, project_id)
);
CREATE INDEX ix_generation_jobs_queue ON generation_jobs(status, created_at, id);
CREATE INDEX ix_generation_jobs_project_history
  ON generation_jobs(project_id, created_at DESC, id DESC);

CREATE TABLE generation_acceptances (
  generation_job_id uuid NOT NULL REFERENCES generation_jobs(id) ON DELETE RESTRICT,
  proposal_id uuid NOT NULL,
  case_id uuid NOT NULL REFERENCES test_cases(id) ON DELETE RESTRICT,
  case_version_id uuid NOT NULL REFERENCES test_case_versions(id) ON DELETE RESTRICT,
  accepted_by uuid REFERENCES users(id),
  accepted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (generation_job_id, proposal_id),
  UNIQUE (case_version_id)
);

CREATE TABLE schedule_trigger_requests (
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
CREATE INDEX ix_schedule_trigger_pending ON schedule_trigger_requests(status, created_at);

CREATE TABLE schedule_state_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id uuid NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  actor_user_id uuid REFERENCES users(id),
  event_type text NOT NULL CHECK (event_type IN ('ENABLED','DISABLED')),
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_schedule_state_events ON schedule_state_events(schedule_id, created_at);

COMMIT;
