BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- Run API inputs and SSE replay boundary; the application owns durable events.
ALTER TABLE test_runs
  ADD COLUMN IF NOT EXISTS requested_by uuid REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS request_intent text,
  ADD COLUMN IF NOT EXISTS event_replay_floor bigint NOT NULL DEFAULT 0
    CHECK (event_replay_floor >= 0);

CREATE INDEX IF NOT EXISTS ix_runs_project_created
  ON test_runs(project_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_outbox_run_requested
  ON outbox_events(available_at, id)
  WHERE event_type = 'RUN_REQUESTED' AND status IN ('PENDING','PUBLISHING');

COMMIT;
