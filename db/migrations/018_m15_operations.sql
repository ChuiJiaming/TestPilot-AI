-- Source: M15 operations schema review for TestPilot V10.3.
-- Status: DESIGN CANDIDATE; do not apply to a persistent database without a separately approved migration gate.
-- Prerequisite: 001_schema.sql through 017_m13_report_exports.sql.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- Existing call_id and its unique index were introduced in migration 003;
-- migration 004 added the required-call check as NOT VALID. Backfill only
-- missing values and derive a UTC calendar month before validating that check.
ALTER TABLE quota_reservations
ADD COLUMN IF NOT EXISTS period_month date;
UPDATE quota_reservations
SET call_id = gen_random_uuid()::text
WHERE call_id IS NULL;
UPDATE quota_reservations
SET period_month = date_trunc('month', created_at AT TIME ZONE 'UTC')::date
WHERE period_month IS NULL;
ALTER TABLE quota_reservations
ALTER COLUMN period_month SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_quota_reservations_org_period_status
ON quota_reservations(organization_id, period_month, status);
ALTER TABLE quota_reservations
VALIDATE CONSTRAINT ck_quota_call_required_v102;

-- Keep existing in-app rows while assigning each a stable unique key.
ALTER TABLE notifications
ADD COLUMN IF NOT EXISTS dedupe_key text;
UPDATE notifications
SET dedupe_key = 'legacy:' || id::text
WHERE dedupe_key IS NULL;
ALTER TABLE notifications
ALTER COLUMN dedupe_key SET NOT NULL;
ALTER TABLE notifications
ADD CONSTRAINT uq_notifications_user_dedupe UNIQUE(user_id, dedupe_key);
CREATE INDEX IF NOT EXISTS ix_notifications_user_created
ON notifications(user_id, created_at DESC, id DESC);

CREATE TABLE system_setting_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key text NOT NULL,
    proposed_value jsonb NOT NULL,
    previous_value_hash text NOT NULL CHECK (previous_value_hash ~ '^[0-9a-f]{64}$'),
    proposed_value_hash text NOT NULL CHECK (proposed_value_hash ~ '^[0-9a-f]{64}$'),
    requested_by uuid NOT NULL REFERENCES users(id),
    approved_by uuid REFERENCES users(id),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'STALE')),
    reason text NOT NULL CHECK (length(trim(reason)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    CONSTRAINT ck_system_setting_change_approval_separation
        CHECK (status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_by <> requested_by)),
    CONSTRAINT ck_system_setting_change_decision_time
        CHECK ((status = 'PENDING' AND decided_at IS NULL) OR (status <> 'PENDING' AND decided_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_system_setting_one_pending_per_key
ON system_setting_changes(key)
WHERE status = 'PENDING';
CREATE INDEX ix_system_setting_changes_status_created
ON system_setting_changes(status, created_at DESC, id DESC);

COMMIT;
