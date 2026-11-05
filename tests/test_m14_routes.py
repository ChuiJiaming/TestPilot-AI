from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.persistence import (
    AuthBase,
    SqlAlchemyVerificationRepository,
    UserRecord,
)
from apps.api.app.integrations.persistence import IntegrationRepository
from apps.api.app.integrations.routes import (
    IntegrationCreate,
    IntegrationUpdate,
    LegacyIntegrationCreate,
    create_integration_router,
)
from apps.api.app.main import create_app
from apps.api.app.tenancy.persistence import OrganizationProjectRepository


class RouteSessions:
    def __init__(self, users, token_authenticator, now):
        self.users = users
        self.token_authenticator = token_authenticator
        self.now = now

    def authenticate(self, token):
        if token in self.users:
            return {"id": str(self.users[token]), "credential_type": "user"}
        return self.token_authenticator(token, self.now)


class TestIntegrationRoutes:
    def setup_method(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime.now(timezone.utc)
        self.actor_id = uuid4()
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=self.actor_id,
                email="integration-api@example.test",
                password_hash="hash",
                display_name="Integration API",
                platform_role="user",
                status="active",
                created_at=self.now,
                email_verified_at=self.now,
            ))
        self.tenancy = OrganizationProjectRepository(self.sessions)
        self.organization = self.tenancy.create_organization(
            self.actor_id,
            name="API Org",
            slug=f"api-{uuid4().hex[:12]}",
            now=self.now,
        )
        self.lifecycle = LifecycleRepository(self.sessions)
        self.auth_repository = SqlAlchemyVerificationRepository(self.sessions)
        self.auth_sessions = RouteSessions(
            {"owner": self.actor_id},
            self.auth_repository.authenticate_api_token,
            self.now,
        )
        self.repository = IntegrationRepository(self.sessions)
        self.router = create_integration_router(
            integration_repository=self.repository,
            auth_sessions=self.auth_sessions,
            secret_ref_resolver=None,
        )
        self.app = create_app(
            lifecycle_repository=self.lifecycle,
            tenancy_repository=self.tenancy,
            auth_sessions=self.auth_sessions,
            integration_repository=self.repository,
        )

    def teardown_method(self):
        self.engine.dispose()

    def endpoint(self, operation_id):
        return next(
            route.endpoint
            for route in self.router.routes
            if isinstance(route, APIRoute) and route.operation_id == operation_id
        )

    @staticmethod
    def credentials(token="owner"):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    def test_integration_dto_never_contains_secret_value(self):
        secret_ref = self.lifecycle.create_secret_ref(
            self.actor_id,
            self.organization["id"],
            provider="vault",
            external_key="ci/github",
            provider_version="provider-version-secret",
            now=self.now,
        )
        del secret_ref
        result = self.endpoint("post__api_v1_organizations__id__integrations")(
            id=self.organization["id"],
            payload=LegacyIntegrationCreate(
                kind="github", config={"repository": "acme/service"}
            ),
            credentials=self.credentials(),
        )
        assert result["kind"] == "github"
        assert "secret_ref_id" not in result
        assert "provider_version" not in result

    def test_secret_provider_missing_fails_closed(self):
        secret_ref = self.lifecycle.create_secret_ref(
            self.actor_id,
            self.organization["id"],
            provider="vault",
            external_key="ci/github",
            provider_version="v1",
            now=self.now,
        )
        created = self.endpoint("post__api_v1_integrations")(
            payload=IntegrationCreate(
                organization_id=self.organization["id"],
                kind="github",
                config={"repository": "acme/service"},
                secret_ref_id=secret_ref["id"],
            ),
            credentials=self.credentials(),
        )
        with pytest.raises(HTTPException) as caught:
            self.endpoint("post__api_v1_integrations__id__test")(
                id=created["id"], payload=None, credentials=self.credentials()
            )
        assert caught.value.status_code == 503
        assert caught.value.detail == "SECRET_PROVIDER_UNAVAILABLE"

    def test_integration_list_is_organization_scoped(self):
        outsider_id = uuid4()
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=outsider_id,
                email="integration-other@example.test",
                password_hash="hash",
                display_name="Other",
                platform_role="user",
                status="active",
                created_at=self.now,
                email_verified_at=self.now,
            ))
        other_org = self.tenancy.create_organization(
            outsider_id,
            name="Other Org",
            slug=f"other-{uuid4().hex[:12]}",
            now=self.now,
        )
        with pytest.raises(HTTPException) as caught:
            self.endpoint("get__api_v1_organizations__id__integrations")(
                id=other_org["id"], limit=30, cursor=None,
                credentials=self.credentials(),
            )
        assert caught.value.status_code == 404

    def test_integration_create_requires_write_scope(self):
        token = self.lifecycle.create_api_token(
            self.actor_id,
            self.organization["id"],
            name="read integrations",
            scopes=["organization:read"],
            expires_at=None,
            now=self.now,
        )
        with pytest.raises(HTTPException) as caught:
            self.endpoint("post__api_v1_organizations__id__integrations")(
                id=self.organization["id"],
                payload=LegacyIntegrationCreate(kind="github", config={}),
                credentials=self.credentials(token["token"]),
            )
        assert caught.value.status_code == 403

    def test_v10_integration_aliases_list_create_and_update(self):
        created = self.endpoint("post__api_v1_integrations")(
            payload=IntegrationCreate(
                organization_id=self.organization["id"],
                kind="github",
                config={"repository": "acme/service"},
            ),
            credentials=self.credentials(),
        )
        page = self.endpoint("get__api_v1_integrations")(
            organization_id=self.organization["id"], limit=30, cursor=None,
            credentials=self.credentials(),
        )
        updated = self.endpoint("patch__api_v1_integrations__id")(
            id=created["id"], payload=IntegrationUpdate(enabled=False),
            credentials=self.credentials(),
        )

        assert [item["id"] for item in page["items"]] == [created["id"]]
        assert updated["enabled"] is False

    def test_baseline_integration_operation_ids_are_registered(self):
        # FastAPI >= 0.160 keeps include_router results as lazy `_IncludedRouter`
        # entries on app.routes, so reflecting over app.routes silently sees only
        # the routes declared directly on the app. The OpenAPI document is the
        # stable, version-independent view of what is actually served.
        operation_ids = {
            operation.get("operationId")
            for path_item in self.app.openapi()["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and operation.get("operationId")
        }
        assert {
            "get__api_v1_organizations__id__integrations",
            "post__api_v1_organizations__id__integrations",
            "post__api_v1_integrations__id__test",
            "post__api_v1_webhooks__provider",
        } <= operation_ids


if __name__ == "__main__":
    import unittest

    unittest.main()
