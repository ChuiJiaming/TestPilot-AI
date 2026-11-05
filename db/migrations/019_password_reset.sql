-- Sureshot 019: password reset tokens reuse the verification outbox transport.
--
-- Why a `purpose` column instead of a second table: the delivery path (lease,
-- retry, dead-letter, encryption) is already implemented and tested for
-- verification mail. Duplicating it for reset mail would duplicate its bugs.
-- Existing rows default to VERIFY, so this migration is backward compatible.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

ALTER TABLE user_email_verifications
  ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'VERIFY';

ALTER TABLE user_email_verifications
  DROP CONSTRAINT IF EXISTS user_email_verifications_purpose_check;
ALTER TABLE user_email_verifications
  ADD CONSTRAINT user_email_verifications_purpose_check
  CHECK (purpose IN ('VERIFY','RESET'));

ALTER TABLE verification_email_outbox
  ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'VERIFY';

ALTER TABLE verification_email_outbox
  DROP CONSTRAINT IF EXISTS verification_email_outbox_purpose_check;
ALTER TABLE verification_email_outbox
  ADD CONSTRAINT verification_email_outbox_purpose_check
  CHECK (purpose IN ('VERIFY','RESET'));

-- Reset lookups always filter on (user_id, purpose, consumed_at IS NULL).
CREATE INDEX IF NOT EXISTS ix_email_verifications_user_purpose
  ON user_email_verifications(user_id, purpose)
  WHERE consumed_at IS NULL;

COMMIT;
