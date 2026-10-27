from datetime import datetime, timezone
import unittest
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import apps.api.app.environments.persistence as environments
from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.environments.persistence import EnvironmentRecord
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
    ProjectRecord,
)


class EnvironmentRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.user_id = UUID("11111111-1111-4111-8111-111111111111")
        self.organization_id = UUID("22222222-2222-4222-8222-222222222222")
        self.project_id = UUID("33333333-3333-4333-8333-333333333333")
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=self.user_id, email="owner@example.com", password_hash="hash",
                display_name="Owner", platform_role="user", status="active",
                created_at=self.now, email_verified_at=self.now,
            ))
            session.add(OrganizationRecord(
                id=self.organization_id, name="Acme", slug="acme", created_at=self.now
            ))
            session.add(OrganizationMembershipRecord(
                organization_id=self.organization_id, user_id=self.user_id,
                role="org_admin", created_at=self.now,
            ))
            session.add(ProjectRecord(
                id=self.project_id, organization_id=self.organization_id,
                name="Web", slug="web", created_by=self.user_id, created_at=self.now,
            ))

    def tearDown(self):
        self.engine.dispose()

    def test_environment_repository_is_available(self):
        self.assertIsNotNone(getattr(environments, "EnvironmentRepository", None))

    def test_environment_crud_is_project_scoped_and_cursor_paged(self):
        repository = environments.EnvironmentRepository(self.sessions)
        first = repository.create(
            self.user_id, self.project_id, name="staging", base_url="https://staging.example.com",
            network_policy={"allowed_hosts": ["staging.example.com"]}, secret_ref_id=None, now=self.now,
        )
        repository.create(
            self.user_id, self.project_id, name="production", base_url="https://prod.example.com",
            network_policy={}, secret_ref_id=None, now=self.now,
        )
        page = repository.list_for_project(self.user_id, self.project_id, limit=1)
        self.assertEqual(page["items"][0]["name"], "production")
        self.assertIsNotNone(page["next_cursor"])
        updated = repository.update(
            self.user_id, first["id"], changes={"base_url": "https://new-staging.example.com"}, now=self.now
        )
        self.assertEqual(updated["base_url"], "https://new-staging.example.com")
        second = repository.list_for_project(
            self.user_id, self.project_id, limit=1, cursor=page["next_cursor"]
        )
        self.assertEqual(second["items"][0]["name"], "staging")
        self.assertIsNone(second["next_cursor"])


if __name__ == "__main__":
    unittest.main()
