from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import (
    AuthBase,
    EmailVerificationRecord,
    SqlAlchemyVerificationRepository,
    UserRecord,
)


class SqlAlchemyVerificationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.repository = SqlAlchemyVerificationRepository(self.sessions)
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)

    def tearDown(self):
        self.engine.dispose()

    def test_issue_resend_and_consume_store_only_hash_and_activate_once(self):
        user_id = self.repository.issue(
            "user@example.com", "scrypt-hash", "User", "hash-1", self.now + timedelta(minutes=10), self.now
        )
        self.assertIsNotNone(user_id)
        self.assertIsNone(self.repository.issue(
            "user@example.com", "different-hash", "Changed", "hash-2", self.now + timedelta(minutes=10), self.now
        ))
        self.assertEqual(
            self.repository.resend(
                "user@example.com", "hash-2", self.now + timedelta(minutes=10), self.now
            ),
            user_id,
        )
        with self.sessions() as session:
            user = session.get(UserRecord, user_id)
            tokens = session.scalars(select(EmailVerificationRecord)).all()
            self.assertEqual(user.password_hash, "scrypt-hash")
            self.assertEqual(user.display_name, "User")
            self.assertEqual(user.status, "pending_email")
            self.assertEqual(len(tokens), 2)
            self.assertEqual(tokens[0].consumed_at.replace(tzinfo=timezone.utc), self.now)
            self.assertEqual(tokens[1].token_hash, "hash-2")

        self.assertTrue(self.repository.consume("hash-2", self.now))
        self.assertFalse(self.repository.consume("hash-2", self.now))
        with self.sessions() as session:
            user = session.get(UserRecord, user_id)
            self.assertEqual(user.status, "active")
            self.assertEqual(user.email_verified_at.replace(tzinfo=timezone.utc), self.now)

    def test_unknown_and_expired_tokens_do_not_activate_accounts(self):
        self.assertFalse(self.repository.consume("missing", self.now))
        user_id = self.repository.issue(
            "user@example.com", "hash", "User", "expired", self.now, self.now - timedelta(minutes=2)
        )
        self.assertIsNotNone(user_id)
        self.assertFalse(self.repository.consume("expired", self.now))


if __name__ == "__main__":
    unittest.main()
