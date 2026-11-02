BEGIN;

-- Shared private artifact metadata for screenshots and deterministic visual diffs.
ALTER TABLE test_artifacts
  ADD COLUMN project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
  ADD COLUMN run_case_id uuid REFERENCES run_cases(id) ON DELETE CASCADE,
  ADD COLUMN action_id uuid,
  ADD COLUMN environment_id uuid REFERENCES environments(id) ON DELETE RESTRICT,
  ADD COLUMN case_version_id uuid REFERENCES test_case_versions(id) ON DELETE RESTRICT,
  ADD COLUMN browser text,
  ADD COLUMN content_type text NOT NULL DEFAULT 'application/octet-stream',
  ADD COLUMN size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
  ADD COLUMN viewport jsonb CHECK (viewport IS NULL OR jsonb_typeof(viewport) = 'object'),
  ADD COLUMN device_scale_factor numeric(5,2)
    CHECK (device_scale_factor IS NULL OR device_scale_factor BETWEEN 0.5 AND 4),
  ADD COLUMN color_scheme text CHECK (color_scheme IS NULL OR color_scheme IN ('light','dark')),
  ADD COLUMN metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metadata_json) = 'object');

UPDATE test_artifacts AS artifact
SET project_id = run.project_id,
    environment_id = run.environment_id
FROM test_runs AS run
WHERE run.id = artifact.run_id;

UPDATE test_artifacts AS artifact
SET run_case_id = run_case.id,
    case_version_id = run_case.case_version_id,
    browser = run_case.browser_name,
    action_id = step.action_id
FROM test_steps AS step
JOIN run_cases AS run_case ON run_case.id = step.run_case_id
WHERE artifact.step_id = step.id
  AND run_case.run_id = artifact.run_id;

ALTER TABLE test_artifacts ALTER COLUMN project_id SET NOT NULL;
CREATE INDEX ix_test_artifacts_project_created
  ON test_artifacts(project_id, created_at DESC, id DESC);
CREATE INDEX ix_test_artifacts_run_action
  ON test_artifacts(run_case_id, action_id, created_at DESC)
  WHERE artifact_type = 'screenshot';

-- Step outcomes are compact deterministic metadata used by M11; never store page text.
ALTER TABLE test_steps
  ADD COLUMN result_json jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(result_json) = 'object');

-- Analyzer outputs are suggestions. Root cause is confirmed only by a human triage patch.
ALTER TABLE defects
  ADD COLUMN classification text NOT NULL DEFAULT 'ASSERTION'
    CHECK (classification IN ('LOCATOR','ASSERTION','ENVIRONMENT','INFRA','SECURITY','DATA','FLAKY')),
  ADD COLUMN root_cause_status text NOT NULL DEFAULT 'INSUFFICIENT_EVIDENCE'
    CHECK (root_cause_status IN ('INSUFFICIENT_EVIDENCE','SUGGESTED','CONFIRMED')),
  ADD COLUMN root_cause_confirmed_by uuid REFERENCES users(id),
  ADD COLUMN root_cause_confirmed_at timestamptz,
  ADD COLUMN triaged_by uuid REFERENCES users(id),
  ADD COLUMN triaged_at timestamptz,
  ADD CONSTRAINT ck_defect_confirmed_root_cause_actor
    CHECK (root_cause_status <> 'CONFIRMED'
      OR (root_cause_confirmed_by IS NOT NULL AND root_cause_confirmed_at IS NOT NULL));

ALTER TABLE defect_occurrences
  ADD COLUMN classification text NOT NULL DEFAULT 'ASSERTION'
    CHECK (classification IN ('LOCATOR','ASSERTION','ENVIRONMENT','INFRA','SECURITY','DATA','FLAKY')),
  ADD COLUMN evidence_status text NOT NULL DEFAULT 'INSUFFICIENT_EVIDENCE'
    CHECK (evidence_status IN ('INSUFFICIENT_EVIDENCE','AVAILABLE')),
  ADD COLUMN error_code text,
  ADD COLUMN evidence jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(evidence) = 'object');
CREATE INDEX ix_defect_occurrences_defect_time
  ON defect_occurrences(defect_id, observed_at DESC, id DESC);

-- Baseline configuration is versioned with the approval record and its mask hash.
ALTER TABLE visual_baselines
  ADD COLUMN created_by uuid REFERENCES users(id),
  ADD COLUMN decided_by uuid REFERENCES users(id),
  ADD COLUMN decision_reason text,
  ADD COLUMN decided_at timestamptz,
  ADD COLUMN mask_regions jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(mask_regions) = 'array'),
  ADD COLUMN max_pixel_ratio numeric(7,6) NOT NULL DEFAULT 0.010000
    CHECK (max_pixel_ratio BETWEEN 0 AND 1),
  ADD COLUMN pixel_channel_tolerance smallint NOT NULL DEFAULT 0
    CHECK (pixel_channel_tolerance BETWEEN 0 AND 255);

ALTER TABLE visual_diffs ALTER COLUMN baseline_id DROP NOT NULL;
ALTER TABLE visual_diffs
  ADD COLUMN actual_artifact_id uuid REFERENCES test_artifacts(id) ON DELETE RESTRICT,
  ADD COLUMN changed_pixels bigint CHECK (changed_pixels IS NULL OR changed_pixels >= 0),
  ADD COLUMN compared_pixels bigint CHECK (compared_pixels IS NULL OR compared_pixels >= 0),
  ADD COLUMN max_pixel_ratio numeric(7,6) CHECK (max_pixel_ratio IS NULL OR max_pixel_ratio BETWEEN 0 AND 1),
  ADD COLUMN pixel_channel_tolerance smallint
    CHECK (pixel_channel_tolerance IS NULL OR pixel_channel_tolerance BETWEEN 0 AND 255),
  ADD COLUMN mask_hash text,
  ADD COLUMN comparison_profile text,
  ADD COLUMN error_code text,
  ADD COLUMN metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metadata_json) = 'object');
ALTER TABLE visual_diffs DROP CONSTRAINT IF EXISTS visual_diffs_status_check;
ALTER TABLE visual_diffs ADD CONSTRAINT visual_diffs_status_check
  CHECK (status IN ('PASS','FAIL','REVIEW','NEED_BASELINE','ERROR'));
CREATE INDEX ix_visual_diffs_run_case_created
  ON visual_diffs(run_case_id, created_at DESC, id DESC);
CREATE INDEX ix_visual_diffs_baseline_created
  ON visual_diffs(baseline_id, created_at DESC, id DESC)
  WHERE baseline_id IS NOT NULL;

COMMIT;
