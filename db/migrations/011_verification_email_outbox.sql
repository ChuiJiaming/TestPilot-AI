BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TABLE verification_email_outbox (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  verification_id uuid NOT NULL REFERENCES user_email_verifications(id) ON DELETE CASCADE,
  recipient_email text NOT NULL,
  token_ciphertext text NOT NULL,
  expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING','PROCESSING','RETRY','SENT','DEAD','CANCELLED')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  available_at timestamptz NOT NULL,
  lease_owner text,
  lease_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  CHECK (
    (status = 'PROCESSING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
    OR (status <> 'PROCESSING' AND lease_owner IS NULL AND lease_until IS NULL)
  )
);

CREATE INDEX ix_verification_email_outbox_queue
  ON verification_email_outbox(status, available_at, id)
  WHERE status IN ('PENDING','RETRY','PROCESSING');
CREATE INDEX ix_verification_email_outbox_dead
  ON verification_email_outbox(created_at DESC, id DESC)
  WHERE status = 'DEAD';
CREATE INDEX ix_verification_email_outbox_user
  ON verification_email_outbox(user_id, created_at DESC);

COMMIT;
