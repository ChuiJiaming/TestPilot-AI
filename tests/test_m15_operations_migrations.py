from pathlib import Path
import unittest

from apps.api.scripts.migrate import repository_migrations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db" / "migrations" / "018_m15_operations.sql"


class M15OperationsMigrationTests(unittest.TestCase):
    def test_legacy_call_ids_and_utc_months_are_backfilled_before_validation(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("update quota_reservations", sql)
        self.assertIn("call_id is null", sql)
        self.assertIn("period_month", sql)
        self.assertIn("created_at at time zone 'utc'", sql)
        self.assertIn("validate constraint ck_quota_call_required_v102", sql)
        self.assertLess(sql.index("update quota_reservations"), sql.index("validate constraint"))
        self.assertNotIn("add column if not exists call_id", sql)
        self.assertNotIn("create unique index uq_quota_call_per_org", sql)

    def test_notifications_get_deterministic_legacy_dedupe_keys_and_unique_scope(self):
        sql = " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())
        self.assertIn("alter table notifications add column if not exists dedupe_key", sql)
        self.assertIn("legacy:", sql)
        self.assertRegex(sql, r"unique\s*\(\s*user_id\s*,\s*dedupe_key\s*\)")

    def test_pending_setting_changes_are_unique_and_approved_changes_require_a_distinct_admin(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("system_setting_changes", sql)
        self.assertIn("where status = 'pending'", sql)
        self.assertRegex(sql, r"approved_by\s*<>\s*requested_by")
        self.assertIn("approved_by is not null", sql)

    def test_migration_runner_discovers_candidate_018_without_editing_history(self):
        migrations = repository_migrations(ROOT / "db" / "migrations")
        names = [path.name for path in migrations]
        # 018 must stay discoverable in order; later migrations are appended, so
        # asserting "018 is last" would fail for reasons unrelated to M15.
        self.assertIn("018_m15_operations.sql", names)
        self.assertLess(names.index("017_m13_report_exports.sql"), names.index("018_m15_operations.sql"))
        self.assertEqual(MIGRATION.name, "018_m15_operations.sql")


if __name__ == "__main__":
    unittest.main()
