BEGIN;

-- M09 candidate required for browser-matrix expansion and immutable environment pinning.
-- Legacy RunCases keep NULL browser_name; no browser is inferred for historical rows.
ALTER TABLE run_cases
  ADD COLUMN browser_name text,
  DROP CONSTRAINT run_cases_run_id_case_version_id_key,
  ADD CONSTRAINT ck_run_cases_browser_name
    CHECK (browser_name IS NULL OR browser_name IN ('chromium', 'firefox', 'webkit')),
  ADD CONSTRAINT uq_run_case_version_browser
    UNIQUE (run_id, case_version_id, browser_name);

ALTER TABLE test_runs
  ADD COLUMN environment_snapshot_id uuid
    REFERENCES environment_snapshots(id) ON DELETE RESTRICT;

ALTER TABLE schedule_trigger_requests
  ADD COLUMN error_code text,
  ADD COLUMN reason text,
  ADD COLUMN trigger_source text NOT NULL DEFAULT 'manual'
    CHECK (trigger_source IN ('manual', 'schedule'));

COMMIT;
