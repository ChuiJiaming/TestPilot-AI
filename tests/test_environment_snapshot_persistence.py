from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.environments.persistence import (
    EnvironmentRecord,
    EnvironmentScopeError,
    EnvironmentSnapshotRecord,
    EnvironmentSnapshotRepository,
    SecretRefRecord,
    SecretRefVersionRecord,
)
from apps.api.app.tenancy.persistence import OrganizationRecord, ProjectRecord


class EnvironmentSnapshotRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.repository = EnvironmentSnapshotRepository(self.sessions)
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.user_id = UUID("11111111-1111-4111-8111-111111111111")
        self.organization_id = UUID("22222222-2222-4222-8222-222222222222")
        self.project_id = UUID("33333333-3333-4333-8333-333333333333")
        self.environment_id = UUID("44444444-4444-4444-8444-444444444444")
        self.secret_ref_id = UUID("55555555-5555-4555-8555-555555555555")
        self.version_id = UUID("66666666-6666-4666-8666-666666666666")
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=self.user_id, email="user@example.com", password_hash="hash",
                display_name="User", platform_role="user", status="active",
                created_at=self.now, email_verified_at=self.now,
            ))
            session.add(OrganizationRecord(
                id=self.organization_id, name="Acme", slug="acme", created_at=self.now
            ))
            session.add(ProjectRecord(
                id=self.project_id, organization_id=self.organization_id,
                name="Web", slug="web", created_by=self.user_id, created_at=self.now,
            ))
            session.add(SecretRefRecord(
                id=self.secret_ref_id, organization_id=self.organization_id,
                provider="vault", external_key="acme/staging", created_at=self.now,
            ))
            session.add(SecretRefVersionRecord(
                id=self.version_id, secret_ref_id=self.secret_ref_id, version=1,
                provider_version="v1", created_by=self.user_id, created_at=self.now,
            ))
            session.add(EnvironmentRecord(
                id=self.environment_id, project_id=self.project_id, name="staging",
                base_url="https://staging.example.com", secret_ref_id=self.secret_ref_id,
                network_policy={"allowed_hosts": ["staging.example.com"]}, created_at=self.now,
            ))

    def tearDown(self):
        self.engine.dispose()

    def test_snapshots_are_immutable_idempotent_and_pin_secret_version(self):
        first = self.repository.capture(
            environment_id=self.environment_id, project_id=self.project_id, now=self.now
        )
        same = self.repository.capture(
            environment_id=self.environment_id, project_id=self.project_id, now=self.now
        )
        self.assertEqual(first["id"], same["id"])
        self.assertEqual(first["secret_ref_version_id"], self.version_id)

        with self.sessions.begin() as session:
            environment = session.get(EnvironmentRecord, self.environment_id)
            environment.base_url = "https://new-staging.example.com"
            environment.network_policy = {"allowed_hosts": ["new-staging.example.com"]}
        second = self.repository.capture(
            environment_id=self.environment_id,
            project_id=self.project_id,
            now=self.now + timedelta(seconds=1),
        )
        self.assertNotEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual(first["base_url"], "https://staging.example.com")
        self.assertEqual(
            self.repository.list_for_environment(
                environment_id=self.environment_id, project_id=self.project_id
            )[0]["id"],
            second["id"],
        )
        with self.sessions() as session:
            persisted = session.get(EnvironmentSnapshotRecord, first["id"])
            self.assertEqual(persisted.base_url, "https://staging.example.com")

    def test_snapshot_rejects_cross_project_environment_and_cross_org_secret(self):
        with self.assertRaises(EnvironmentScopeError):
            self.repository.capture(
                environment_id=self.environment_id, project_id=uuid4(), now=self.now
            )

        other_org_id = UUID("77777777-7777-4777-8777-777777777777")
        other_secret_id = UUID("88888888-8888-4888-8888-888888888888")
        with self.sessions.begin() as session:
            session.add(OrganizationRecord(
                id=other_org_id, name="Other", slug="other", created_at=self.now
            ))
            session.add(SecretRefRecord(
                id=other_secret_id, organization_id=other_org_id,
                provider="vault", external_key="other/key", created_at=self.now,
            ))
            environment = session.get(EnvironmentRecord, self.environment_id)
            environment.secret_ref_id = other_secret_id
        with self.assertRaises(EnvironmentScopeError):
            self.repository.capture(
                environment_id=self.environment_id, project_id=self.project_id, now=self.now
            )


if __name__ == "__main__":
    unittest.main()
