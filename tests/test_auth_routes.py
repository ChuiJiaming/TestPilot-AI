import unittest
from pathlib import Path

from fastapi import APIRouter, Request, Response
from apps.api.app.main import create_app
from apps.api.app.auth.routes import LoginRequest, create_auth_router
from apps.api.app.auth.sessions import AuthSessionTokens
import yaml


class AuthRouteContractTests(unittest.TestCase):
    def test_email_verification_routes_have_stable_operation_ids_and_unconfigured_503(self):
        document = create_app().openapi()
        expected = {
            "/api/v1/auth/register": ("post", "post__api_v1_auth_register"),
            "/api/v1/auth/verify-email": ("post", "post__api_v1_auth_verify_email"),
            "/api/v1/auth/verification/resend": ("post", "post__api_v1_auth_verification_resend"),
            "/api/v1/auth/login": ("post", "post__api_v1_auth_login"),
            "/api/v1/auth/refresh": ("post", "post__api_v1_auth_refresh"),
            "/api/v1/auth/logout": ("post", "post__api_v1_auth_logout"),
            "/api/v1/auth/sessions": ("get", "get__api_v1_auth_sessions"),
        }
        for path, (method, operation_id) in expected.items():
            operation = document["paths"][path][method]
            self.assertEqual(operation["operationId"], operation_id)
            self.assertIn("503", operation["responses"])
        revoke = document["paths"]["/api/v1/auth/sessions/{session_id}"]["delete"]
        self.assertEqual(revoke["operationId"], "delete__api_v1_auth_sessions__session_id")
        self.assertIn("Set-Cookie", document["paths"]["/api/v1/auth/login"]["post"]["responses"]["200"]["headers"])
        self.assertIn("__Host-testpilot_refresh", str(document["paths"]["/api/v1/auth/refresh"]["post"]["parameters"]))

        candidate = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text(encoding="utf-8")
        )
        for path, (method, operation_id) in expected.items():
            self.assertEqual(candidate["paths"][path][method]["operationId"], operation_id)
        self.assertEqual(
            candidate["paths"]["/api/v1/auth/sessions/{session_id}"]["delete"]["operationId"],
            "delete__api_v1_auth_sessions__session_id",
        )
        self.assertEqual(candidate["paths"]["/api/v1/auth/logout"]["post"]["responses"]["204"]["description"],
                         "Session or all caller sessions revoked; refresh cookie cleared")

    def test_login_and_refresh_set_secure_http_only_cookie_without_body_token(self):
        tokens = AuthSessionTokens(
            access_token="access",
            refresh_token="refresh-secret",
            expires_in=900,
            user={
                "id": "11111111-1111-4111-8111-111111111111",
                "email": "user@example.com",
                "display_name": "User",
                "status": "active",
            },
            session_id="22222222-2222-4222-8222-222222222222",
            device_label="Test Browser",
        )

        class FakeSessions:
            refresh_ttl_seconds = 2_592_000

            def login(self, **kwargs):
                return tokens

            def refresh(self, refresh_token):
                self.last_refresh_token = refresh_token
                return tokens

        router = create_auth_router(
            service=None,
            password_hash=lambda value: "password-hash",
            auth_sessions=FakeSessions(),
            allowed_origins=("https://app.example.test",),
        )
        login_route = next(route for route in router.routes if route.path == "/api/v1/auth/login")
        request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/auth/login",
            "headers": [(b"user-agent", b"Test Browser")], "query_string": b"",
            "client": ("192.0.2.10", 1234), "server": ("test", 443), "scheme": "https",
            "http_version": "1.1", "root_path": "",
        })
        response = Response()
        body = login_route.endpoint(LoginRequest(email="user@example.com", password="secret-password"), request, response)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("__host-testpilot_refresh=refresh-secret", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertNotIn("refresh-secret", body.model_dump_json())

        refresh_route = next(route for route in router.routes if route.path == "/api/v1/auth/refresh")
        refresh_response = Response()
        result = refresh_route.endpoint(
            request=request,
            response=refresh_response,
            origin="https://app.example.test",
            refresh_cookie="old-refresh",
        )
        self.assertEqual(result.access_token, "access")
        self.assertIn("__host-testpilot_refresh=refresh-secret", refresh_response.headers["set-cookie"].lower())


if __name__ == "__main__":
    unittest.main()
