from datetime import datetime, timezone
from uuid import uuid4
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.persistence import AuthBase, SqlAlchemyVerificationRepository, UserRecord
from apps.api.app.main import create_app
from apps.api.app.tenancy.persistence import OrganizationProjectRepository


class FakeSessions:
    def __init__(self, users, token_authenticator=None, now=None):
        self.users = users
        self.token_authenticator = token_authenticator
        self.now = now or datetime.now(timezone.utc)

    def authenticate(self, token):
        user_id = self.users.get(token)
        if user_id:
            return {"id": str(user_id), "session_id": str(uuid4())}
        return self.token_authenticator(token, self.now) if self.token_authenticator else None


class LifecycleRouteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime.now(timezone.utc)
        self.owner_id, self.member_id = uuid4(), uuid4()
        with self.sessions.begin() as session:
            for user_id, email in ((self.owner_id, "owner@example.test"), (self.member_id, "member@example.test")):
                session.add(UserRecord(id=user_id, email=email, password_hash="hash", display_name="Test",
                    platform_role="user", status="active", created_at=self.now, email_verified_at=self.now))
        tenancy = OrganizationProjectRepository(self.sessions)
        self.organization = tenancy.create_organization(self.owner_id, name="Test", slug=f"test-{uuid4().hex[:10]}", now=self.now)
        self.project = tenancy.create_project(self.owner_id, organization_id=self.organization["id"],
            name="Project", slug="project", now=self.now)
        self.lifecycle = LifecycleRepository(self.sessions)
        auth_repository = SqlAlchemyVerificationRepository(self.sessions)
        app = create_app(lifecycle_repository=self.lifecycle,
            tenancy_repository=OrganizationProjectRepository(self.sessions),
            auth_sessions=FakeSessions({"owner": self.owner_id, "member": self.member_id},
                token_authenticator=auth_repository.authenticate_api_token, now=self.now))
        self.client = TestClient(app)

    def tearDown(self):
        self.engine.dispose()

    def test_authenticated_invite_create_and_email_bound_acceptance(self):
        response = self.client.post(f"/api/v1/organizations/{self.organization['id']}/invitations",
            headers={"Authorization": "Bearer owner"}, json={
                "email": "member@example.test", "role": "tester", "project_id": str(self.project["id"]),
                "expires_at": "2030-01-01T00:00:00Z",
            })
        self.assertEqual(response.status_code, 201, response.text)
        token = response.json()["token"]
        accepted = self.client.post("/api/v1/invitations/accept",
            headers={"Authorization": "Bearer member"}, json={"token": token})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["role"], "tester")
        schema = self.client.get("/openapi.json").json()
        self.assertIn("post__api_v1_organizations__org_id__invitations", {
            operation["operationId"] for path in schema["paths"].values()
            for operation in path.values() if isinstance(operation, dict) and "operationId" in operation
        })

    def test_api_token_enforces_scopes_and_organization_boundary(self):
        token = self.lifecycle.create_api_token(self.owner_id, self.organization["id"],
            name="read-only", scopes=["organization:read"], expires_at=None, now=self.now)
        other = OrganizationProjectRepository(self.sessions).create_organization(
            self.owner_id, name="Other", slug=f"other-{uuid4().hex[:10]}", now=self.now)
        response = self.client.get("/api/v1/organizations",
            headers={"Authorization": f"Bearer {token['token']}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["items"]], [str(self.organization["id"])])
        denied = self.client.get(f"/api/v1/organizations/{other['id']}/members",
            headers={"Authorization": f"Bearer {token['token']}"})
        self.assertEqual(denied.status_code, 403)

    def test_legacy_secret_ref_paths_are_scoped_and_delete_is_disabled(self):
        created = self.client.post("/api/v1/secret-refs",
            headers={"Authorization": "Bearer owner"}, json={
                "organization_id": str(self.organization["id"]), "provider": "vault",
                "external_key": "compat/path", "provider_version": "v1",
            })
        self.assertEqual(created.status_code, 200, created.text)
        secret_id = created.json()["id"]
        rotated = self.client.post(f"/api/v1/secret-refs/{secret_id}/rotate",
            headers={"Authorization": "Bearer owner"}, json={"provider_version": "v2"})
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json()["active_version"], 2)
        denied = self.client.delete(f"/api/v1/secret-refs/{secret_id}",
            headers={"Authorization": "Bearer owner"})
        self.assertEqual(denied.status_code, 409)


if __name__ == "__main__":
    unittest.main()
