from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class EmailVerificationContractTests(unittest.TestCase):
    def test_migration_006_adds_pending_email_and_one_time_verification_tokens(self):
        sql = (ROOT / "db" / "migrations" / "006_email_verification.sql").read_text(
            encoding="utf-8"
        ).lower()

        self.assertIn("pending_email", sql)
        self.assertIn("email_verified_at", sql)
        self.assertIn("user_email_verifications", sql)
        self.assertIn("token_hash", sql)
        self.assertIn("expires_at", sql)
        self.assertIn("consumed_at", sql)

    def test_verification_routes_extend_without_removing_the_101_existing_operations(self):
        document = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
        operations = [
            operation
            for path_item in document["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
        ]

        operation_ids = {
            operation["operationId"]
            for operation in operations
            if isinstance(operation, dict) and "operationId" in operation
        }
        original_ids = set(
            (ROOT / "contracts" / "openapi_v10_3_original_operation_ids.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(len(original_ids), 101)
        self.assertTrue(original_ids.issubset(operation_ids))
        registration = document["paths"]["/api/v1/auth/register"]["post"]
        self.assertIn("202", registration["responses"])
        self.assertEqual(
            registration["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RegistrationAccepted",
        )
        self.assertNotIn("AuthResponse", str(registration["responses"]["202"]))
        self.assertIn("/api/v1/auth/verify-email", document["paths"])
        self.assertIn("/api/v1/auth/verification/resend", document["paths"])
        self.assertIn("/api/v1/auth/sessions", document["paths"])
        self.assertIn("/api/v1/auth/sessions/{session_id}", document["paths"])

    def test_migration_007_backfills_refresh_families_as_user_sessions(self):
        sql = (ROOT / "db" / "migrations" / "007_auth_sessions.sql").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("create table user_sessions", sql)
        self.assertIn("device_label", sql)
        self.assertIn("last_used_at", sql)
        self.assertIn("legacy device", sql)
        self.assertIn("fk_refresh_tokens_session", sql)


if __name__ == "__main__":
    unittest.main()
