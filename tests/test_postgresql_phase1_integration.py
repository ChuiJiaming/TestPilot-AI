from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier, Lock
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import (
    SqlAlchemyVerificationRepository,
    UserRecord,
)
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.auth.lifecycle import (
    ApiTokenRecord, InvitationRecord, LifecycleRepository, SecretRefRecord,
    SecretRefVersionRecord,
)
from apps.api.app.environments.persistence import (
    EnvironmentRecord,
    EnvironmentSnapshotRecord,
    EnvironmentRepository,
    EnvironmentSnapshotRepository,
)
from apps.api.app.tenancy.persistence import (
    MissingResource,
    OrganizationMembershipRecord,
    OrganizationProjectRepository,
    ProjectRecord,
)


class PhaseOnePostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.getenv("TESTPILOT_TEST_DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest("TESTPILOT_TEST_DATABASE_URL is not set")
        cls.engine = create_engine(database_url, pool_pre_ping=True)
        database = cls.engine.url.database
        host = cls.engine.url.host
        if database != "testpilot" or host not in {"127.0.0.1", "localhost", "::1"}:
            cls.engine.dispose()
            raise RuntimeError("integration test is restricted to local database testpilot")
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def test_auth_tenancy_and_snapshot_repositories_against_postgresql(self):
        now = datetime.now(timezone.utc)
        run_id = uuid4()
        owner_id = None
        member_id = None
        outsider_id = uuid4()
        organization_id = None
        project_id = None
        environment_id = uuid4()
        owner_email = f"phase1-owner-{run_id.hex}@example.invalid"
        member_email = f"phase1-member-{run_id.hex}@example.invalid"
        owner_token = f"verify-owner-{run_id.hex}"
        member_token = f"verify-member-{run_id.hex}"
        owner_refresh = f"refresh-{run_id.hex}"
        next_refresh = f"refresh-next-{run_id.hex}"

        auth = SqlAlchemyVerificationRepository(self.sessions)
        tenancy = OrganizationProjectRepository(self.sessions)
        environment_repository = EnvironmentRepository(self.sessions)
        snapshots = EnvironmentSnapshotRepository(self.sessions)
        lifecycle = LifecycleRepository(self.sessions)
        secret_ref_id = None

        try:
            owner_id = auth.issue(
                owner_email, "integration-hash", "Phase 1 Owner", owner_token,
                now + timedelta(minutes=10), now
            )
            self.assertIsNotNone(owner_id)
            self.assertTrue(auth.consume(owner_token, now))
            member_id = auth.issue(
                member_email, "integration-hash", "Phase 1 Member", member_token,
                now + timedelta(minutes=10), now
            )
            self.assertIsNotNone(member_id)
            self.assertTrue(auth.consume(member_token, now))
            session = auth.create_session(
                owner_email, owner_refresh, now + timedelta(days=30), "PostgreSQL integration", now
            )
            self.assertIsNotNone(session)
            self.assertIsNotNone(
                auth.rotate_refresh(owner_refresh, next_refresh, now + timedelta(days=30), now)
            )
            self.assertIsNone(
                auth.rotate_refresh(owner_refresh, f"replay-{owner_id.hex}",
                                    now + timedelta(days=30), now + timedelta(seconds=1))
            )
            self.assertFalse(auth.session_is_active(owner_id, session["session_id"], now))

            organization = tenancy.create_organization(
                owner_id, name="Phase 1 PostgreSQL", slug=f"phase1-{owner_id.hex}", now=now
            )
            organization_id = organization["id"]
            project = tenancy.create_project(
                owner_id, organization_id=organization_id, name="Integration", slug="integration", now=now
            )
            project_id = project["id"]
            with self.sessions.begin() as db:
                db.add(OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=member_id,
                    role="org_member",
                    created_at=now,
                ))
            tenancy.set_project_member(owner_id, project_id, member_id, role="tester", now=now)
            invitation = lifecycle.create_invitation(
                owner_id, organization_id, project_id=project_id, email=member_email,
                role="developer", expires_at=now + timedelta(days=1), now=now,
            )
            self.assertEqual(
                lifecycle.accept_invitation(member_id, invitation["token"], now=now)["role"],
                "developer",
            )
            api_token = lifecycle.create_api_token(
                owner_id, organization_id, name="integration", scopes=["run:create"],
                expires_at=now + timedelta(days=1), now=now,
            )
            self.assertNotIn("token", lifecycle.list_api_tokens(owner_id)[0])
            self.assertNotEqual(api_token["token"], api_token["id"])
            self.assertEqual(auth.authenticate_api_token(api_token["token"], now)["scopes"], ["run:create"])
            lifecycle.revoke_api_token(owner_id, api_token["id"], now=now + timedelta(seconds=2))
            self.assertIsNone(auth.authenticate_api_token(api_token["token"], now + timedelta(seconds=3)))
            secret_ref = lifecycle.create_secret_ref(
                owner_id, organization_id, provider="vault", external_key=f"phase1/{run_id.hex}",
                provider_version=f"v1-{run_id.hex}", now=now,
            )
            secret_ref_id = secret_ref["id"]
            rotated_ref = lifecycle.rotate_secret_ref(
                owner_id, secret_ref_id, provider_version=f"v2-{run_id.hex}",
                now=now + timedelta(seconds=1),
            )
            self.assertEqual(rotated_ref["active_version"], 2)
            members = tenancy.list_organization_members(owner_id, organization_id, limit=10)
            self.assertEqual({member["user_id"] for member in members["items"]}, {owner_id, member_id})
            with self.assertRaises(MissingResource):
                tenancy.list_organization_members(outsider_id, organization_id, limit=10)

            created_environment = environment_repository.create(
                owner_id, project_id, name="integration", base_url="https://example.invalid",
                network_policy={"allow_domains": ["example.invalid"]}, secret_ref_id=secret_ref_id, now=now,
            )
            environment_id = created_environment["id"]
            listed_environments = environment_repository.list_for_project(owner_id, project_id, limit=10)
            self.assertEqual(listed_environments["items"][0]["id"], environment_id)
            first = snapshots.capture(environment_id=environment_id, project_id=project_id, now=now)
            environment_repository.update(
                owner_id, environment_id,
                changes={"base_url": "https://changed.example.invalid"},
                now=now + timedelta(seconds=1),
            )
            second = snapshots.capture(
                environment_id=environment_id, project_id=project_id, now=now + timedelta(seconds=1)
            )
            self.assertIsNotNone(second["secret_ref_version_id"])
            persisted = snapshots.list_for_environment(
                environment_id=environment_id, project_id=project_id
            )
            self.assertNotEqual(first["snapshot_hash"], second["snapshot_hash"])
            self.assertIn("https://example.invalid", {row["base_url"] for row in persisted})
            self.assertTrue(all("secret_value" not in row for row in persisted))
        finally:
            with self.sessions.begin() as db:
                db.execute(
                    delete(EnvironmentSnapshotRecord).where(
                        EnvironmentSnapshotRecord.environment_id == environment_id
                    )
                )
                db.execute(delete(EnvironmentRecord).where(EnvironmentRecord.id == environment_id))
                if secret_ref_id is not None:
                    db.execute(delete(SecretRefVersionRecord).where(SecretRefVersionRecord.secret_ref_id == secret_ref_id))
                    db.execute(delete(SecretRefRecord).where(SecretRefRecord.id == secret_ref_id))
                if project_id is not None:
                    db.execute(delete(ProjectRecord).where(ProjectRecord.id == project_id))
                if organization_id is not None:
                    db.execute(delete(InvitationRecord).where(InvitationRecord.organization_id == organization_id))
                    db.execute(delete(ApiTokenRecord).where(ApiTokenRecord.organization_id == organization_id))
                    db.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id})
                db.execute(text("""
                    DELETE FROM refresh_tokens
                    WHERE user_id IN (SELECT id FROM users WHERE email IN (:owner, :member))
                """), {"owner": owner_email, "member": member_email})
                db.execute(text("""
                    DELETE FROM user_sessions
                    WHERE user_id IN (SELECT id FROM users WHERE email IN (:owner, :member))
                """), {"owner": owner_email, "member": member_email})
                db.execute(text("""
                    DELETE FROM user_email_verifications
                    WHERE user_id IN (SELECT id FROM users WHERE email IN (:owner, :member))
                """), {"owner": owner_email, "member": member_email})
                db.execute(delete(UserRecord).where(UserRecord.email.in_([owner_email, member_email])))

    def test_concurrent_refresh_replay_revokes_the_session_family(self):
        now = datetime.now(timezone.utc)
        identity = uuid4().hex
        email = f"phase1-concurrent-{identity}@example.invalid"
        raw_refresh = f"refresh-concurrent-{identity}"
        next_tokens = iter((f"next-a-{identity}", f"next-b-{identity}"))
        next_tokens_lock = Lock()
        start = Barrier(2)
        auth = SqlAlchemyVerificationRepository(self.sessions)
        user_id = uuid4()
        session_id = None

        class AllowAll:
            def allow(self, email_key, client_ip):
                return True

        def token_factory():
            with next_tokens_lock:
                return next(next_tokens)

        service = AuthSessionService(
            repository=auth,
            limiter=AllowAll(),
            password_verifier=lambda password, encoded: True,
            jwt_signing_key="phase1-postgresql-concurrency-test-signing-key",
            access_ttl_seconds=900,
            refresh_ttl_seconds=2_592_000,
            token_factory=token_factory,
        )
        try:
            with self.sessions.begin() as db:
                db.add(UserRecord(
                    id=user_id,
                    email=email,
                    password_hash="integration-hash",
                    display_name="Concurrent Refresh",
                    platform_role="user",
                    status="active",
                    email_verified_at=now,
                    created_at=now,
                ))
            created = auth.create_session(
                email,
                service._token_hash(raw_refresh),
                now + timedelta(days=30),
                "Concurrent test browser",
                now,
            )
            session_id = created["session_id"]

            def refresh_at_same_time():
                start.wait(timeout=5)
                return service.refresh(raw_refresh, now=now)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: refresh_at_same_time(), range(2)))

            successful = [result for result in results if result is not None]
            self.assertEqual(len(successful), 1)
            self.assertFalse(auth.session_is_active(user_id, session_id, now))
            self.assertIsNone(service.authenticate(successful[0].access_token, now=now))
        finally:
            with self.sessions.begin() as db:
                db.execute(text("DELETE FROM refresh_tokens WHERE user_id = :id"), {"id": user_id})
                db.execute(text("DELETE FROM user_sessions WHERE user_id = :id"), {"id": user_id})
                db.execute(delete(UserRecord).where(UserRecord.id == user_id))


if __name__ == "__main__":
    unittest.main()
