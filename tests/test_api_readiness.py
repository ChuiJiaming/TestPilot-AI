import json
import unittest

from apps.api.app.main import create_app


class ApiReadinessTests(unittest.TestCase):
    def test_readiness_does_not_claim_ready_without_configured_dependencies(self):
        app = create_app()
        route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/health/ready")
        response = route.endpoint()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "not_ready")
        self.assertIn("postgresql", payload["checks"])
        self.assertIn("redis", payload["checks"])
        self.assertIn("object_storage", payload["checks"])
        self.assertNotEqual(payload["checks"]["postgresql"], "ready")


if __name__ == "__main__":
    unittest.main()
