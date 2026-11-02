from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_quality_migration_adds_scoped_evidence_and_fail_closed_visual_state():
    migration = (ROOT / "db" / "migrations" / "016_m11_m12_quality_analysis_visual.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE test_artifacts" in migration
    assert "ADD COLUMN project_id uuid REFERENCES projects(id)" in migration
    assert "ALTER TABLE test_artifacts ALTER COLUMN project_id SET NOT NULL" in migration
    assert "ADD COLUMN result_json jsonb NOT NULL DEFAULT '{}'::jsonb" in migration
    assert "INSUFFICIENT_EVIDENCE" in migration
    assert "NEED_BASELINE" in migration
    assert "actual_artifact_id" in migration


def test_approved_baseline_unique_index_covers_action_mask_and_display_context():
    migration = (ROOT / "db" / "migrations" / "003_v10_repairs.sql").read_text(encoding="utf-8")
    match = re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_approved_dimensions_v10\s+"
        r"ON visual_baselines\s*\((.*?)\)\s+NULLS NOT DISTINCT\s+WHERE status = 'APPROVED'",
        migration, re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    indexed_columns = {column.strip() for column in match.group(1).split(",")}
    assert {
        "project_id", "case_version_id", "environment_id", "browser", "action_id",
        "viewport", "device_scale_factor", "color_scheme", "mask_hash",
        "comparison_profile",
    } <= indexed_columns
