-- TestPilot AI V10.3: cross-project links still missing after 004.
-- This is a DESIGN CANDIDATE. Validate against actual migrated PG15+ database.
-- Requires 001 -> 002 -> 003 -> 004 and reviewed remediation preflight.
-- NOT VALID permits existing dirty rows temporarily, but checks new/updated rows.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- Allow composite references to enforce that the version belongs to its case project.
ALTER TABLE test_case_versions ADD COLUMN IF NOT EXISTS project_id uuid;
UPDATE test_case_versions v SET project_id = c.project_id
  FROM test_cases c WHERE v.case_id = c.id AND v.project_id IS NULL;
ALTER TABLE test_case_versions
  ADD CONSTRAINT uq_case_versions_id_project_v103 UNIQUE (id, project_id);
ALTER TABLE test_case_versions
  ADD CONSTRAINT fk_case_versions_case_scope_v103
  FOREIGN KEY (case_id, project_id) REFERENCES test_cases (id, project_id) NOT VALID;
ALTER TABLE test_case_versions
  ADD CONSTRAINT ck_case_version_project_not_null_v103 CHECK (project_id IS NOT NULL) NOT VALID;

-- The plan's suite must belong to the same project as the plan.
ALTER TABLE test_plans
  ADD CONSTRAINT fk_plan_suite_scope_v103
  FOREIGN KEY (suite_id, project_id) REFERENCES test_suites (id, project_id) NOT VALID;

-- Each run_case must match the run project and the referenced case version project.
ALTER TABLE run_cases ADD COLUMN IF NOT EXISTS project_id uuid;
UPDATE run_cases rc SET project_id = r.project_id
  FROM test_runs r WHERE rc.run_id = r.id AND rc.project_id IS NULL;
ALTER TABLE run_cases
  ADD CONSTRAINT uq_run_cases_id_project_v103 UNIQUE (id, project_id);
ALTER TABLE run_cases
  ADD CONSTRAINT fk_run_cases_run_scope_v103
  FOREIGN KEY (run_id, project_id) REFERENCES test_runs (id, project_id) NOT VALID;
ALTER TABLE run_cases
  ADD CONSTRAINT fk_run_cases_version_scope_v103
  FOREIGN KEY (case_version_id, project_id)
  REFERENCES test_case_versions (id, project_id) NOT VALID;
ALTER TABLE run_cases
  ADD CONSTRAINT ck_run_cases_project_not_null_v103 CHECK (project_id IS NOT NULL) NOT VALID;

-- The visual baseline's chosen case version belongs to the project.
ALTER TABLE visual_baselines
  ADD CONSTRAINT fk_visual_case_scope_v103
  FOREIGN KEY (case_version_id, project_id)
  REFERENCES test_case_versions (id, project_id) NOT VALID;

CREATE INDEX IF NOT EXISTS ix_run_cases_project_status_v103
  ON run_cases(project_id,status);
CREATE INDEX IF NOT EXISTS ix_case_versions_project_v103
  ON test_case_versions(project_id,case_id,version DESC);
COMMIT;
