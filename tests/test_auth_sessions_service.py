from datetime import datetime, timezone
import unittest
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.passwords import ScryptPasswordHasher
from apps.api.app.auth.persistence import AuthBase, SqlAlchemyVerificationRepository, UserRecord
from apps.api.app.auth.sessions import AuthSessionService


class AllowLimiter:
    def allow(self, email_key, client_ip):
        return True


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.repo = SqlAlchemyVerificationRepository(self.sessions)
        self.hasher = ScryptPasswordHasher(n=1024)
        self.password_hash = self.hasher.hash("correct horse battery")
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=UUID("11111111-1111-4111-8111-111111111111"),
                email="user@example.com",
                password_hash=self.password_hash,
                display_name="Test User",
                platform_role="user",
                status="active",
                created_at=self.now,
                email_verified_at=self.now,
            ))
        self.service = AuthSessionService(
            repository=self.repo,
            limiter=AllowLimiter(),
            password_verifier=self.hasher.verify,
            jwt_signing_key="k" * 40,
            access_ttl_seconds=900,
            refresh_ttl_seconds=30 * 24 * 60 * 60,
        )

    def tearDown(self):
        self.engine.dispose()

    def test_login_refresh_rotates_cookie_credential_and_replay_revokes_family(self):
        login = self.service.login(
            email="user@example.com", password="correct horse battery",
            device_label="Chrome on Linux", client_ip="192.0.2.1", now=self.now,
        )
        self.assertEqual(login.user["display_name"], "Test User")
        self.assertEqual(login.device_label, "Chrome on Linux")
        session_id = UUID(login.session_id)
        self.assertEqual(len(self.repo.list_sessions(UUID(login.user["id"]))), 1)

        refreshed = self.service.refresh(login.refresh_token, now=self.now)
        self.assertIsNotNone(refreshed)
        self.assertNotEqual(login.refresh_token, refreshed.refresh_token)
        self.assertIsNone(self.service.refresh(login.refresh_token, now=self.now))
        self.assertFalse(self.repo.session_is_active(UUID(login.user["id"]), session_id, self.now))

    def test_bearer_access_token_is_bound_to_active_session(self):
        login = self.service.login(
            email="user@example.com", password="correct horse battery",
            device_label="CLI", client_ip=None, now=self.now,
        )
        claims = self.service.authenticate(login.access_token, now=self.now)
        self.assertEqual(claims["id"], login.user["id"])
        self.repo.revoke_session(UUID(login.user["id"]), UUID(login.session_id), self.now)
        self.assertIsNone(self.service.authenticate(login.access_token, now=self.now))

    def test_invalid_password_does_not_create_a_device_session(self):
        result = self.service.login(
            email="user@example.com", password="wrong password",
            device_label="Browser", client_ip=None, now=self.now,
        )
        self.assertIsNone(result)
        self.assertEqual(self.repo.list_sessions(UUID("11111111-1111-4111-8111-111111111111")), [])


if __name__ == "__main__":
    unittest.main()
