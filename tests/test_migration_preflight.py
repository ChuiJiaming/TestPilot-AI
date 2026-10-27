from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"


class MigrationPreflightTests(unittest.TestCase):
    def test_005_preflight_checks_every_documented_scope_link(self):
        sql = (MIGRATIONS / "005_preflight_and_validate.sql").read_text(encoding="utf-8").lower()
        for mismatch in (
            "case_versions_wrong_project",
            "plans_wrong_suite_project",
            "run_cases_wrong_run_project",
            "run_cases_wrong_version_project",
            "visual_baselines_wrong_version_project",
        ):
            with self.subTest(mismatch=mismatch):
                self.assertIn(mismatch, sql)

    def test_005_preflight_is_read_only(self):
        sql = (MIGRATIONS / "005_preflight_and_validate.sql").read_text(encoding="utf-8").lower()
        self.assertNotRegex(sql, r"\b(insert|update|delete|alter|drop|create)\b")


if __name__ == "__main__":
    unittest.main()
