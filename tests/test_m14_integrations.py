import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.integrations.persistence import (
    IntegrationRecord,
    IntegrationNotFound,
    IntegrationRepository,
)
from apps.api.app.tenancy.persistence import OrganizationProjectRepository


class TestIntegrationRepository:
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
                email="integration-owner@example.test",
                password_hash="hash",
                display_name="Integration Owner",
                platform_role="user",
                status="active",
                created_at=self.now,
                email_verified_at=self.now,
            ))
        self.tenancy = OrganizationProjectRepository(self.sessions)
        self.organization = self.tenancy.create_organization(
            self.actor_id,
            name="Integration Org",
            slug=f"integration-{uuid4().hex[:12]}",
            now=self.now,
        )
        self.repository = IntegrationRepository(self.sessions)
        self.lifecycle = LifecycleRepository(self.sessions)

    def teardown_method(self):
        self.engine.dispose()

    def test_integration_dto_never_contains_secret_value(self):
        secret = "provider-material-must-never-leak"
        secret_ref = self.lifecycle.create_secret_ref(
            self.actor_id,
            self.organization["id"],
            provider="vault",
            external_key="integrations/github",
            provider_version="vault-version-1",
            now=self.now,
        )
        record = self.repository.create(
            self.actor_id,
            self.organization["id"],
            kind="github",
            config={"repository": "acme/service"},
            secret_ref_id=secret_ref["id"],
            enabled=True,
            now=self.now,
        )
        serialized = json.dumps(record, default=str)

        assert str(secret_ref["id"]) in serialized
        assert secret not in serialized
        assert "provider_version" not in serialized
        assert "secret_value" not in serialized

    def test_secret_like_config_fields_are_rejected(self):
        with pytest.raises(ValueError, match="SecretRef"):
            self.repository.create(
                self.actor_id,
                self.organization["id"],
                kind="github",
                config={"auth": {"access_token": "raw-provider-token"}},
                secret_ref_id=None,
                enabled=True,
                now=self.now,
            )

    def test_preexisting_secret_like_config_fails_closed_on_read(self):
        record = self.repository.create(
            self.actor_id,
            self.organization["id"],
            kind="github",
            config={"repository": "acme/service"},
            secret_ref_id=None,
            enabled=True,
            now=self.now,
        )
        with self.sessions.begin() as session:
            stored = session.get(IntegrationRecord, record["id"])
            stored.config = {"token": "legacy-raw-token"}

        with pytest.raises(ValueError, match="SecretRef"):
            self.repository.get(self.actor_id, record["id"])

    def test_integration_list_is_organization_scoped(self):
        record = self.repository.create(
            self.actor_id,
            self.organization["id"],
            kind="github",
            config={"repository": "acme/service"},
            secret_ref_id=None,
            enabled=True,
            now=self.now,
        )
        outsider_id = uuid4()
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=outsider_id,
                email="integration-outsider@example.test",
                password_hash="hash",
                display_name="Outsider",
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
        self.repository.create(
            outsider_id,
            other_org["id"],
            kind="gitlab",
            config={"repository": "other/service"},
            secret_ref_id=None,
            enabled=True,
            now=self.now,
        )

        page = self.repository.list(self.actor_id, organization_id=self.organization["id"])

        assert [item["id"] for item in page["items"]] == [record["id"]]
        with pytest.raises(IntegrationNotFound):
            self.repository.list(self.actor_id, organization_id=other_org["id"])

    def test_secret_ref_must_belong_to_the_integration_organization(self):
        outsider_id = uuid4()
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=outsider_id,
                email="secret-owner@example.test",
                password_hash="hash",
                display_name="Secret Owner",
                platform_role="user",
                status="active",
                created_at=self.now,
                email_verified_at=self.now,
            ))
        other_org = self.tenancy.create_organization(
            outsider_id,
            name="Secret Org",
            slug=f"secret-{uuid4().hex[:12]}",
            now=self.now,
        )
        secret_ref = self.lifecycle.create_secret_ref(
            outsider_id,
            other_org["id"],
            provider="vault",
            external_key="outside/scope",
            provider_version="vault-version-1",
            now=self.now,
        )

        with pytest.raises(IntegrationNotFound):
            self.repository.create(
                self.actor_id,
                self.organization["id"],
                kind="github",
                config={},
                secret_ref_id=secret_ref["id"],
                enabled=True,
                now=self.now,
            )
