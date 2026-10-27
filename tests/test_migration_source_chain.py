from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"


class MigrationSourceChainTests(unittest.TestCase):
    def test_candidate_migrations_are_available_in_version_order(self):
        names = [
            "001_schema.sql",
            "002_v5_extensions.sql",
            "003_v10_repairs.sql",
            "004_v102_integrity.sql",
            "005_scope_linkage.sql",
            "006_email_verification.sql",
            "007_auth_sessions.sql",
        ]
        for name in names:
            with self.subTest(migration=name):
                source = MIGRATIONS / name
                self.assertTrue(source.is_file(), f"missing migration source: {source}")
                self.assertGreater(source.stat().st_size, 0)

    def test_migration_004_defines_composite_target_used_by_005(self):
        migration_004 = (MIGRATIONS / "004_v102_integrity.sql").read_text(encoding="utf-8")
        migration_005 = (MIGRATIONS / "005_scope_linkage.sql").read_text(encoding="utf-8")

        self.assertIn("uq_test_suites_id_project UNIQUE (id, project_id)", migration_004)
        self.assertIn("REFERENCES test_suites (id, project_id)", migration_005)

    def test_migration_005_scopes_case_versions_and_run_cases(self):
        migration_005 = (MIGRATIONS / "005_scope_linkage.sql").read_text(encoding="utf-8")

        self.assertIn("fk_case_versions_case_scope_v103", migration_005)
        self.assertIn("fk_run_cases_run_scope_v103", migration_005)
        self.assertIn("fk_run_cases_version_scope_v103", migration_005)
        self.assertIn("fk_visual_case_scope_v103", migration_005)


if __name__ == "__main__":
    unittest.main()
