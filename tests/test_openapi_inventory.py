from pathlib import Path
import unittest

import yaml
from apps.api.app.auth.lifecycle import PERMISSION_REGISTRY
from apps.api.app.main import create_app


ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "api" / "openapi.yaml"


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(
                    f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class OpenApiInventoryTests(unittest.TestCase):
    def test_candidate_contract_preserves_the_101_operations_and_adds_verification(self):
        document = yaml.load(OPENAPI.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        operations = [
            operation
            for path_item in document["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
        ]
        operation_ids = [operation["operationId"] for operation in operations]

        original_ids = set(
            (ROOT / "contracts" / "openapi_v10_3_original_operation_ids.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(len(original_ids), 101)
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertTrue(original_ids.issubset(set(operation_ids)))

    def test_run_creation_remains_idempotent(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        run_create = document["paths"]["/api/v1/test-runs"]["post"]

        self.assertIn(
            "Idempotency-Key",
            {parameter["name"] for parameter in run_create["parameters"]},
        )

    def test_phase3_run_detail_and_sse_ticket_keep_baseline_operation_ids(self):
        runtime = create_app().openapi()
        operation_ids = {
            operation["operationId"]
            for path_item in runtime["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
            and isinstance(operation, dict)
            and "operationId" in operation
        }

        self.assertIn("get__api_v1_test_runs__id", operation_ids)
        self.assertIn("post_api_v1_sse_tickets", operation_ids)

    def test_m13_report_operations_are_registered_at_runtime(self):
        runtime = create_app().openapi()
        operation_ids = {
            operation["operationId"]
            for path_item in runtime["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
            and isinstance(operation, dict)
            and "operationId" in operation
        }
        self.assertTrue({
            "get__api_v1_test_runs__id__report",
            "get__api_v1_test_runs__id__report_export",
            "post__api_v1_report_exports",
            "get__api_v1_report_exports__id",
            "post__api_v1_reports__id__exports",
        }.issubset(operation_ids))

    def test_m14_baseline_operations_keep_runtime_registration_and_webhook_hmac_contract(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        runtime = create_app().openapi()
        baseline_ids = {
            "get__api_v1_organizations__id__integrations",
            "post__api_v1_organizations__id__integrations",
            "post__api_v1_integrations__id__test",
            "post__api_v1_webhooks__provider",
        }
        runtime_ids = {
            operation["operationId"]
            for path_item in runtime["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
            and isinstance(operation, dict)
            and "operationId" in operation
        }
        static_operations = {
            operation["operationId"]: operation
            for path_item in document["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
        }

        self.assertTrue(baseline_ids.issubset(runtime_ids))
        self.assertTrue(baseline_ids.issubset(static_operations))
        webhook = static_operations["post__api_v1_webhooks__provider"]
        self.assertEqual(webhook.get("security"), [])
        self.assertEqual(webhook.get("x-auth-policy"), "PUBLIC_ROUTE_WITH_REQUIRED_VERIFIED_HMAC")
        self.assertEqual(webhook.get("x-webhook-signing"), "HMAC-SHA256(timestamp + newline + nonce + newline + exact raw body)")
        for operation_id in baseline_ids - {"post__api_v1_webhooks__provider"}:
            self.assertIn("BearerAuth", str(static_operations[operation_id].get("security", [])))

    def test_m15_notification_operations_keep_runtime_and_static_contracts(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        runtime = create_app().openapi()
        expected_ids = {
            "get__api_v1_notifications",
            "post__api_v1_notifications__id__read",
            "get__api_v1_notifications_preferences",
            "put__api_v1_notifications_preferences",
            "get__api_v1_notification_deliveries",
        }
        runtime_ids = {
            operation["operationId"]
            for path_item in runtime["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
            and isinstance(operation, dict)
            and "operationId" in operation
        }
        static_operations = {
            operation["operationId"]: operation
            for path_item in document["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
        }

        self.assertTrue(expected_ids.issubset(runtime_ids))
        self.assertTrue(expected_ids.issubset(static_operations))
        self.assertEqual(
            static_operations["get__api_v1_notification_deliveries"].get("x-contract-status"),
            "V10.3_IMPLEMENTED",
        )

    def test_m15_baseline_and_additive_operations_are_registered_in_both_contracts(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        runtime = create_app().openapi()
        baseline_ids = {
            "get__api_v1_admin_system_settings",
            "put__api_v1_admin_system_settings__key",
            "get__api_v1_notifications",
            "get__api_v1_notifications_preferences",
            "get__api_v1_organizations__id__audit_logs",
            "get__api_v1_organizations__id__quotas",
            "get__api_v1_organizations__id__usage",
            "post__api_v1_audit_logs_export",
            "post__api_v1_notifications__id__read",
            "put__api_v1_notifications_preferences",
            "put__api_v1_organizations__id__quotas",
        }
        additive_ids = {
            "get__api_v1_notification_deliveries",
            "get__api_v1_audit_logs",
            "get__api_v1_audit_logs_verify",
            "get__api_v1_quota_policies",
            "put__api_v1_quota_policies",
            "get__api_v1_usage",
            "get__api_v1_system_settings",
            "put__api_v1_system_settings",
            "post__api_v1_admin_system_setting_changes__id__decision",
        }
        runtime_ids = {
            operation["operationId"]
            for path_item in runtime["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
            and isinstance(operation, dict)
            and "operationId" in operation
        }
        static_ids = {
            operation["operationId"]
            for path_item in document["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
        }

        self.assertTrue(baseline_ids <= runtime_ids)
        self.assertTrue(baseline_ids <= static_ids)
        self.assertTrue(additive_ids <= runtime_ids)
        self.assertTrue(additive_ids <= static_ids)

    def test_phase5_additive_operation_ids_are_inventoried_separately(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        runtime = create_app().openapi()
        original_ids = set(
            (ROOT / "contracts" / "openapi_v10_3_original_operation_ids.txt")
            .read_text(encoding="utf-8").splitlines()
        )
        additive_path = ROOT / "contracts" / "openapi_v10_3_phase5_additive_operation_ids.txt"
        additive_ids = set(additive_path.read_text(encoding="utf-8").splitlines())
        static_ids = {
            operation["operationId"]
            for path_item in document["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
        }
        runtime_ids = {
            operation["operationId"]
            for path_item in runtime["paths"].values()
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
            and isinstance(operation, dict)
            and "operationId" in operation
        }

        self.assertEqual(len(original_ids), 101)
        self.assertTrue(original_ids <= static_ids)
        self.assertTrue(original_ids <= runtime_ids)
        self.assertFalse(original_ids & additive_ids)
        self.assertEqual(static_ids - original_ids, additive_ids)
        self.assertTrue(additive_ids <= runtime_ids)

    def test_quota_contract_includes_monthly_accounting_summary(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        quota = document["components"]["schemas"]["Quota"]
        required = set(quota["required"])

        self.assertTrue({
            "monthly_tokens_committed", "monthly_tokens_reserved",
            "monthly_tokens_remaining", "quota_exhausted", "period_month",
        } <= required)

    def test_api_token_permission_registry_matches_openapi_permissions(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        declared = {
            permission
            for path_item in document["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict)
            for permission in operation.get("x-required-permissions", [])
        }
        self.assertEqual(PERMISSION_REGISTRY, declared)
        schema_scopes = set(document["components"]["schemas"]["ApiToken"]["properties"]["scopes"]["items"]["enum"])
        self.assertEqual(schema_scopes, PERMISSION_REGISTRY)


if __name__ == "__main__":
    unittest.main()
