BEGIN;

-- M09 candidate required for browser-matrix expansion and immutable environment pinning.
-- Legacy RunCases keep NULL browser_name; no browser is inferred for historical rows.
--
-- Reconciliation notes against the frozen 001-007 chain:
--   * run_cases already carries a legacy `browser` column and `uq_run_case_browser`
--     from migration 002. Phase 2 code reads `browser_name`, so both columns exist
--     during the transition; `browser` is legacy and must not be dropped here.
--   * test_runs.environment_snapshot_id already exists from migration 003.
--   * the constraint dropped below does not exist in the 002 baseline, so the drop
--     must be guarded.
ALTER TABLE run_cases
  ADD COLUMN IF NOT EXISTS browser_name text;

ALTER TABLE run_cases
  DROP CONSTRAINT IF EXISTS run_cases_run_id_case_version_id_key;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_run_cases_browser_name') THEN
    ALTER TABLE run_cases
      ADD CONSTRAINT ck_run_cases_browser_name
      CHECK (browser_name IS NULL OR browser_name IN ('chromium', 'firefox', 'webkit'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_run_case_version_browser') THEN
    ALTER TABLE run_cases
      ADD CONSTRAINT uq_run_case_version_browser
      UNIQUE (run_id, case_version_id, browser_name);
  END IF;
END $$;

ALTER TABLE test_runs
  ADD COLUMN IF NOT EXISTS environment_snapshot_id uuid
    REFERENCES environment_snapshots(id) ON DELETE RESTRICT;

ALTER TABLE schedule_trigger_requests
  ADD COLUMN IF NOT EXISTS error_code text,
  ADD COLUMN IF NOT EXISTS reason text,
  ADD COLUMN IF NOT EXISTS trigger_source text NOT NULL DEFAULT 'manual'
    CHECK (trigger_source IN ('manual', 'schedule'));

COMMIT;
