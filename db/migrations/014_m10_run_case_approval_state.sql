BEGIN;

-- M10 parks one RunCase while a Healing candidate awaits human approval.
ALTER TABLE run_cases DROP CONSTRAINT IF EXISTS run_cases_status_check;
ALTER TABLE run_cases ADD CONSTRAINT run_cases_status_check
CHECK (status IN ('QUEUED','RUNNING','WAITING_APPROVAL','PASSED','FAILED','ERROR','SKIPPED','CANCELLED'));

COMMIT;
