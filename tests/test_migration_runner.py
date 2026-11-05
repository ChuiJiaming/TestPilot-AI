from pathlib import Path
import tempfile
import unittest

from apps.api.scripts.migrate import (
    MigrationError,
    migration_source,
    repository_migrations,
    validate_ledger_order,
)


class MigrationRunnerTests(unittest.TestCase):
    def test_discovers_only_numbered_migrations_and_excludes_read_only_preflight(self):
        migrations = repository_migrations(
            Path(__file__).resolve().parents[1] / "db" / "migrations"
        )
        names = [path.name for path in migrations]
        self.assertEqual(migrations[0].name, "001_schema.sql")
        # Ordered, gap-free, and every applied migration is discoverable. The
        # count is intentionally not pinned: pinning it turns every new
        # migration into a failing test that says nothing about correctness.
        self.assertEqual(names, sorted(names))
        self.assertEqual(names, [f"{index:03d}_" + name.split("_", 1)[1]
                                  for index, name in enumerate(names, start=1)])
        self.assertIn("018_m15_operations.sql", names)
        self.assertIn("019_password_reset.sql", names)
        self.assertIn("020_setting_direct_apply.sql", names)
        self.assertNotIn("005_preflight_and_validate.sql", set(names))

    def test_m13_migration_adds_export_idempotency_and_lease_invariants(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "db" / "migrations" / "017_m13_report_exports.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("uq_report_exports_requester_idempotency", migration)
        self.assertIn("lease_expires_at", migration)
        self.assertIn("REPORT_EXPORT_REQUESTED", migration)
        self.assertIn("snapshot_hash", migration)

    def test_normalizes_transaction_wrappers_and_keeps_content_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "002_candidate.sql"
            source = "BEGIN;\nSELECT 1;\nCOMMIT;\nBEGIN;\nSELECT 2;\nCOMMIT;\n"
            path.write_text(source, encoding="utf-8")
            normalized, digest = migration_source(path)
        self.assertEqual(normalized, "SELECT 1;\nSELECT 2;\n")
        self.assertEqual(len(digest), 64)

    def test_rejects_unbalanced_transaction_wrappers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "001_candidate.sql"
            path.write_text("BEGIN;\nSELECT 1;\n", encoding="utf-8")
            with self.assertRaises(MigrationError):
                migration_source(path)

    def test_rejects_history_gaps_before_applying_any_pending_migration(self):
        with self.assertRaises(MigrationError):
            validate_ledger_order(
                ["001_schema.sql", "002_v5_extensions.sql", "003_v10_repairs.sql"],
                {"001_schema.sql", "003_v10_repairs.sql"},
            )


if __name__ == "__main__":
    unittest.main()
