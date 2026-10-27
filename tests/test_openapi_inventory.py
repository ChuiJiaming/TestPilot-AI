from pathlib import Path
import unittest

import yaml
from apps.api.app.auth.lifecycle import PERMISSION_REGISTRY


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
        self.assertEqual(len(operations), 105)
        self.assertEqual(len(set(operation_ids)), 105)
        self.assertTrue(original_ids.issubset(set(operation_ids)))

    def test_run_creation_remains_idempotent(self):
        document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        run_create = document["paths"]["/api/v1/test-runs"]["post"]

        self.assertIn(
            "Idempotency-Key",
            {parameter["name"] for parameter in run_create["parameters"]},
        )

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
