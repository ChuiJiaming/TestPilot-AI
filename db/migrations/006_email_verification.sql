-- TestPilot AI Phase 1: email verification for new self-service registrations.
-- Status: DESIGN CANDIDATE until verified against PostgreSQL 15+ and upgrade data.
-- Existing active accounts remain active; the migration does not claim historical email verification.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

ALTER TABLE users
  ADD COLUMN email_verified_at timestamptz;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_status_check;
ALTER TABLE users ADD CONSTRAINT users_status_check
  CHECK (status IN ('pending_email','active','disabled'));

CREATE TABLE user_email_verifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_email_verifications_user_created
  ON user_email_verifications(user_id,created_at DESC);
CREATE INDEX ix_email_verifications_expiry
  ON user_email_verifications(expires_at) WHERE consumed_at IS NULL;

COMMIT;
