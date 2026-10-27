from pathlib import Path
import unittest

import yaml

from apps.api.app.main import create_app


class EnvironmentSnapshotRouteContractTests(unittest.TestCase):
    def test_environment_crud_routes_match_typed_candidate_contract(self):
        runtime = create_app().openapi()
        candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text(encoding="utf-8")
        )
        documentation_candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "docs" / "TestPilot_AI_V10.3_OpenAPI_修订候选.yaml").read_text(encoding="utf-8")
        )
        path = "/api/v1/projects/{id}/environments"
        for method, operation_id in (
            ("get", "get__api_v1_projects__id__environments"),
            ("post", "post__api_v1_projects__id__environments"),
        ):
            with self.subTest(method=method):
                runtime_operation = runtime["paths"][path][method]
                candidate_operation = candidate["paths"][path][method]
                documentation_operation = documentation_candidate["paths"][path][method]
                self.assertEqual(runtime_operation["operationId"], operation_id)
                self.assertEqual(candidate_operation["operationId"], operation_id)
                self.assertEqual(documentation_operation["operationId"], operation_id)
                self.assertIn("503", candidate_operation["responses"])
                self.assertIn("503", documentation_operation["responses"])
        runtime_page_ref = runtime["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        candidate_page_ref = candidate["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        documentation_page_ref = documentation_candidate["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(runtime_page_ref.rsplit("/", 1)[1], "EnvironmentPage")
        self.assertEqual(candidate_page_ref.rsplit("/", 1)[1], "EnvironmentPageV10")
        self.assertEqual(documentation_page_ref.rsplit("/", 1)[1], "EnvironmentPageV10")
        runtime_create = runtime["components"]["schemas"]["EnvironmentCreate"]["required"]
        candidate_create = candidate["components"]["schemas"]["V102PostApiV1ProjectsIdEnvironmentsInput"]["required"]
        self.assertEqual(runtime_create, candidate_create)
        self.assertEqual(set(runtime_create), {"name", "base_url"})
        documentation_create = documentation_candidate["components"]["schemas"]["V102PostApiV1ProjectsIdEnvironmentsInput"]["required"]
        self.assertEqual(documentation_create, runtime_create)
        runtime_patch = runtime["paths"]["/api/v1/environments/{id}"]["patch"]
        candidate_patch = candidate["paths"]["/api/v1/environments/{id}"]["patch"]
        documentation_patch = documentation_candidate["paths"]["/api/v1/environments/{id}"]["patch"]
        self.assertEqual(runtime_patch["operationId"], candidate_patch["operationId"])
        self.assertEqual(runtime_patch["operationId"], documentation_patch["operationId"])
        self.assertIn("503", candidate_patch["responses"])
        self.assertIn("503", documentation_patch["responses"])

    def test_snapshot_list_route_matches_candidate_contract(self):
        runtime = create_app().openapi()
        candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text(encoding="utf-8")
        )
        path = "/api/v1/environments/{id}/snapshots"
        runtime_operation = runtime["paths"][path]["get"]
        candidate_operation = candidate["paths"][path]["get"]

        self.assertEqual(runtime_operation["operationId"], "get_api_v1_environments_id_snapshots")
        self.assertEqual(candidate_operation["operationId"], runtime_operation["operationId"])
        self.assertEqual(runtime_operation["responses"]["200"]["content"]["application/json"]["schema"]["type"], "array")
        self.assertEqual(candidate_operation["responses"]["200"]["content"]["application/json"]["schema"]["type"], "array")
        self.assertEqual(runtime_operation["security"], candidate_operation["security"])
        for status in ("401", "404", "503"):
            self.assertIn(status, runtime_operation["responses"])
            self.assertIn(status, candidate_operation["responses"])


if __name__ == "__main__":
    unittest.main()
