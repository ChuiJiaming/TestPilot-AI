BEGIN;

-- Phase 2 candidate: preserve the V10.3 immutable case-version workflow.
-- Existing versions retain unknown review/title state; they are never silently
-- relabeled as drafts or published from the legacy tables.
ALTER TABLE test_case_versions
  ADD COLUMN dsl_title text,
  ADD COLUMN version_name text,
  ADD COLUMN changelog text NOT NULL DEFAULT '',
  ADD COLUMN review_status text
    CHECK (review_status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','SUPERSEDED')),
  ADD COLUMN review_reason text,
  ADD COLUMN approved_by uuid REFERENCES users(id),
  ADD COLUMN approved_at timestamptz,
  ADD CONSTRAINT uq_case_version_id_case UNIQUE (id, case_id);

ALTER TABLE test_cases
  ADD COLUMN published_version_id uuid,
  ADD CONSTRAINT fk_test_cases_published_version_case
    FOREIGN KEY (published_version_id, id)
    REFERENCES test_case_versions(id, case_id)
    ON DELETE RESTRICT;

-- Keep the V10.3 generic target model from migration 003. Legacy linkage
-- columns remain populated only for their matching target kinds.
ALTER TABLE approvals
  ALTER COLUMN run_id DROP NOT NULL,
  ALTER COLUMN expires_at DROP NOT NULL,
  ADD COLUMN case_version_id uuid REFERENCES test_case_versions(id) ON DELETE RESTRICT;

UPDATE approvals a
SET project_id = r.project_id,
    target_kind = COALESCE(a.target_kind, 'run'),
    target_id = COALESCE(a.target_id, a.run_id)
FROM test_runs r
WHERE a.run_id = r.id
  AND (a.project_id IS NULL OR a.target_kind IS NULL OR a.target_id IS NULL);

ALTER TABLE approvals
  DROP CONSTRAINT IF EXISTS ck_approval_single_target,
  ADD CONSTRAINT ck_approval_target_binding CHECK (
    target_kind IS NOT NULL AND target_id IS NOT NULL AND (
      (target_kind = 'run' AND run_id IS NOT NULL
        AND target_id = run_id AND case_version_id IS NULL)
      OR (target_kind = 'case_version' AND run_id IS NULL
        AND case_version_id IS NOT NULL AND target_id = case_version_id)
      OR (target_kind IN ('healing','visual_baseline','risky_action')
        AND run_id IS NULL AND case_version_id IS NULL)
    )
  );

CREATE INDEX ix_case_versions_review ON test_case_versions(case_id, review_status, version);
CREATE INDEX ix_approvals_case_version ON approvals(case_version_id) WHERE case_version_id IS NOT NULL;
CREATE UNIQUE INDEX uq_approval_case_version ON approvals(case_version_id) WHERE case_version_id IS NOT NULL;

COMMIT;
