from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.lifecycle import (
    ApiTokenRecord,
    InvalidInvitation,
    LifecycleRepository,
    PERMISSION_REGISTRY,
    SecretRefVersionRecord,
)
from apps.api.app.auth.persistence import AuthBase, SqlAlchemyVerificationRepository, UserRecord
from apps.api.app.tenancy.persistence import OrganizationMembershipRecord


class LifecycleRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.repo = LifecycleRepository(self.sessions)
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.owner, self.member = uuid4(), uuid4()
        with self.sessions.begin() as session:
            for uid, email in ((self.owner, "owner@example.test"), (self.member, "member@example.test")):
                session.add(UserRecord(id=uid, email=email, password_hash="hash", display_name="Test",
                    platform_role="user", status="active", created_at=self.now, email_verified_at=self.now))
        org = self._create_org()
        self.org_id = org

    def tearDown(self):
        self.engine.dispose()

    def _create_org(self):
        from apps.api.app.tenancy.persistence import OrganizationProjectRepository
        return OrganizationProjectRepository(self.sessions).create_organization(
            self.owner, name="Lifecycle", slug=f"lifecycle-{uuid4().hex[:10]}", now=self.now
        )["id"]

    def test_invitation_is_email_bound_one_time_and_assigns_project_role(self):
        from apps.api.app.tenancy.persistence import OrganizationProjectRepository
        tenancy = OrganizationProjectRepository(self.sessions)
        project = tenancy.create_project(self.owner, organization_id=self.org_id,
            name="Web", slug="web", now=self.now)
        invitation = self.repo.create_invitation(self.owner, self.org_id,
            project_id=project["id"], email="MEMBER@example.test", role="tester",
            expires_at=self.now + timedelta(days=1), now=self.now)
        with self.assertRaises(InvalidInvitation):
            self.repo.accept_invitation(self.owner, invitation["token"], now=self.now)
        result = self.repo.accept_invitation(self.member, invitation["token"], now=self.now)
        self.assertEqual(result["role"], "tester")
        with self.assertRaises(InvalidInvitation):
            self.repo.accept_invitation(self.member, invitation["token"], now=self.now)

    def test_api_token_plaintext_is_one_time_and_only_hash_is_persisted(self):
        token = self.repo.create_api_token(self.owner, self.org_id, name="CI",
            scopes=["run:create"], expires_at=None, now=self.now)
        self.assertTrue(token["token"].startswith("tp_"))
        self.assertNotIn("token", self.repo.list_api_tokens(self.owner)[0])
        auth_repository = SqlAlchemyVerificationRepository(self.sessions)
        principal = auth_repository.authenticate_api_token(token["token"], self.now)
        self.assertEqual(principal["organization_id"], str(self.org_id))
        self.assertEqual(principal["scopes"], ["run:create"])
        with self.sessions() as session:
            record = session.get(ApiTokenRecord, token["id"])
            self.assertNotEqual(record.token_hash, token["token"])
        rotated = self.repo.rotate_api_token(self.owner, token["id"], now=self.now + timedelta(seconds=1))
        self.assertNotEqual(rotated["token"], token["token"])
        self.assertIsNone(auth_repository.authenticate_api_token(token["token"], self.now))
        self.assertIsNotNone(auth_repository.authenticate_api_token(rotated["token"], self.now))
        with self.assertRaises(ValueError):
            self.repo.create_api_token(self.owner, self.org_id, name="Bad",
                scopes=["admin:everything"], expires_at=None, now=self.now)
        with self.assertRaises(ValueError):
            self.repo.create_api_token(self.owner, self.org_id, name="Global",
                scopes=["platform:admin"], expires_at=None, now=self.now)
        self.repo.revoke_api_token(self.owner, token["id"], now=self.now + timedelta(seconds=2))
        self.assertIsNone(auth_repository.authenticate_api_token(rotated["token"], self.now + timedelta(seconds=3)))

    def test_secret_ref_rotation_keeps_metadata_only_and_versions_in_order(self):
        created = self.repo.create_secret_ref(self.owner, self.org_id, provider="vault",
            external_key="team/web", provider_version="vault-v1", now=self.now)
        rotated = self.repo.rotate_secret_ref(self.owner, created["id"],
            provider_version="vault-v2", now=self.now + timedelta(seconds=1))
        self.assertEqual(rotated["active_version"], 2)
        listed = self.repo.list_secret_refs(self.owner, organization_id=self.org_id)
        self.assertEqual(listed[0]["active_version"], 2)
        with self.sessions() as session:
            versions = session.scalars(select(SecretRefVersionRecord).order_by(
                SecretRefVersionRecord.version)).all()
        self.assertEqual([v.retired_at is None for v in versions], [False, True])
        self.assertTrue(PERMISSION_REGISTRY)
        self.assertNotIn("secret_value", rotated)


if __name__ == "__main__":
    unittest.main()
