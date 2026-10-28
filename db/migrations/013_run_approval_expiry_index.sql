BEGIN;

-- Supports the bounded 60-second sweep for 30-minute Run approvals.
CREATE INDEX ix_approvals_pending_run_expiry
  ON approvals (expires_at, id)
  WHERE target_kind = 'run'
    AND status = 'PENDING'
    AND expires_at IS NOT NULL;

COMMIT;
