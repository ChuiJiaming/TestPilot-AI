-- TestPilot AI Phase 1: explicit multi-device session lifecycle.
-- Status: DESIGN CANDIDATE until exercised on PostgreSQL 15+ with an upgrade fixture.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TABLE user_sessions (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_label text NOT NULL,
  created_at timestamptz NOT NULL,
  last_used_at timestamptz NOT NULL,
  revoked_at timestamptz
);

-- Preserve existing refresh-token families as legacy sessions.
INSERT INTO user_sessions(id,user_id,device_label,created_at,last_used_at,revoked_at)
SELECT token_family,
       user_id,
       'Legacy device',
       min(created_at),
       max(coalesce(rotated_at,created_at)),
       max(revoked_at)
FROM refresh_tokens
GROUP BY token_family,user_id;

ALTER TABLE refresh_tokens
  ADD CONSTRAINT fk_refresh_tokens_session
  FOREIGN KEY(token_family) REFERENCES user_sessions(id) ON DELETE CASCADE;

CREATE INDEX ix_user_sessions_user_active
  ON user_sessions(user_id,last_used_at DESC) WHERE revoked_at IS NULL;
COMMIT;
