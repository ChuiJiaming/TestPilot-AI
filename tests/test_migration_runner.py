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
        self.assertEqual(len(migrations), 7)
        self.assertEqual(migrations[0].name, "001_schema.sql")
        self.assertEqual(migrations[-1].name, "007_auth_sessions.sql")
        self.assertNotIn("005_preflight_and_validate.sql", {path.name for path in migrations})

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
