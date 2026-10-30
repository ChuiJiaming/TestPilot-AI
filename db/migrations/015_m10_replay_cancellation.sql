BEGIN;

ALTER TABLE healing_records DROP CONSTRAINT IF EXISTS healing_records_status_check;
ALTER TABLE healing_records ADD CONSTRAINT healing_records_status_check
CHECK (status IN ('PROPOSED','PENDING_APPROVAL','APPROVED','REJECTED','APPLIED','EXPIRED','CANCELLED'));

ALTER TABLE test_steps
ADD COLUMN IF NOT EXISTS recovery_healing_id uuid REFERENCES healing_records(id);

COMMIT;
