from pathlib import Path
import unittest

import yaml

from apps.api.app.main import create_app


class TenancyRouteContractTests(unittest.TestCase):
    def test_organization_project_member_routes_keep_candidate_operation_ids(self):
        runtime = create_app().openapi()
        candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text(encoding="utf-8")
        )
        expected = {
            ("/api/v1/organizations", "get"): "get__api_v1_organizations",
            ("/api/v1/organizations", "post"): "post__api_v1_organizations",
            ("/api/v1/organizations/{org_id}/members", "get"): "get__api_v1_organizations__org_id__members",
            ("/api/v1/projects", "get"): "get__api_v1_projects",
            ("/api/v1/projects", "post"): "post__api_v1_projects",
            ("/api/v1/projects/{id}", "get"): "get__api_v1_projects__id",
            ("/api/v1/projects/{id}", "patch"): "patch__api_v1_projects__id",
            ("/api/v1/projects/{id}/members", "get"): "get__api_v1_projects__id__members",
            ("/api/v1/projects/{id}/members/{user_id}", "put"): "put__api_v1_projects__id__members__user_id",
            ("/api/v1/projects/{project_id}/members/{user_id}", "delete"): "delete_api_v1_projects_project_id_members_user_id",
        }
        for (path, method), operation_id in expected.items():
            with self.subTest(path=path, method=method):
                runtime_operation = runtime["paths"][path][method]
                candidate_operation = candidate["paths"][path][method]
                self.assertEqual(runtime_operation["operationId"], operation_id)
                self.assertEqual(candidate_operation["operationId"], operation_id)
                self.assertIn("503", runtime_operation["responses"])
                self.assertIn("503", candidate_operation["responses"])
                runtime_schema = runtime_operation["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema")
                candidate_schema = candidate_operation["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema")
                self.assertIsNotNone(runtime_schema)
                self.assertIsNotNone(candidate_schema)

    def test_organization_member_list_is_a_typed_scoped_page(self):
        runtime = create_app().openapi()
        candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text(encoding="utf-8")
        )
        documentation_candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "docs" / "TestPilot_AI_V10.3_OpenAPI_修订候选.yaml").read_text(encoding="utf-8")
        )
        path = "/api/v1/organizations/{org_id}/members"
        runtime_operation = runtime["paths"][path]["get"]
        candidate_operation = candidate["paths"][path]["get"]
        documentation_operation = documentation_candidate["paths"][path]["get"]
        self.assertEqual(runtime_operation["operationId"], candidate_operation["operationId"])
        self.assertEqual(runtime_operation["security"], candidate_operation["security"])
        self.assertIn("503", runtime_operation["responses"])
        self.assertIn("503", candidate_operation["responses"])
        self.assertIn("503", documentation_operation["responses"])
        runtime_ref = runtime_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        candidate_ref = candidate_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        runtime_page = runtime["components"]["schemas"][runtime_ref.rsplit("/", 1)[1]]
        candidate_page = candidate["components"]["schemas"][candidate_ref.rsplit("/", 1)[1]]
        self.assertEqual(runtime_page["type"], "object")
        self.assertEqual(candidate_page["type"], "object")
        self.assertEqual(runtime_page["required"], candidate_page["required"])
        runtime_items_ref = runtime_page["properties"]["items"]["items"]["$ref"]
        candidate_items_ref = candidate_page["properties"]["items"]["items"]["$ref"]
        runtime_member = runtime["components"]["schemas"][runtime_items_ref.rsplit("/", 1)[1]]
        candidate_member = candidate["components"]["schemas"][candidate_items_ref.rsplit("/", 1)[1]]
        documentation_ref = documentation_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        documentation_page = documentation_candidate["components"]["schemas"][documentation_ref.rsplit("/", 1)[1]]
        documentation_items_ref = documentation_page["properties"]["items"]["items"]["$ref"]
        documentation_member = documentation_candidate["components"]["schemas"][documentation_items_ref.rsplit("/", 1)[1]]
        self.assertEqual(runtime_member["required"], candidate_member["required"])
        self.assertEqual(set(runtime_member["properties"]), set(candidate_member["properties"]))
        self.assertEqual(runtime_member["required"], documentation_member["required"])
        self.assertEqual(set(runtime_member["properties"]), set(documentation_member["properties"]))

if __name__ == "__main__":
    unittest.main()
