from datetime import datetime, timezone
from hashlib import sha256
import unittest
from uuid import UUID

from apps.api.app.auth.verification import EmailVerificationService


class MemoryRepository:
    def __init__(self):
        self.users = {}
        self.verification = None

    def issue(self, email, password_hash, display_name, token_hash, expires_at, now):
        user = self.users.get(email)
        if user is not None and user["status"] != "pending_email":
            return None
        if user is None:
            user = {
                "id": UUID("11111111-1111-4111-8111-111111111111"),
                "email": email,
                "password_hash": password_hash,
                "display_name": display_name,
                "status": "pending_email",
                "email_verified_at": None,
            }
            self.users[email] = user
        self.verification = {
            "user_id": user["id"],
            "token_hash": token_hash,
            "expires_at": expires_at,
            "consumed_at": None,
        }
        return user["id"]

    def resend(self, email, token_hash, expires_at, now):
        user = self.users.get(email)
        if user is None or user["status"] != "pending_email":
            return None
        self.verification = {
            "user_id": user["id"],
            "token_hash": token_hash,
            "expires_at": expires_at,
            "consumed_at": None,
        }
        return user["id"]

    def consume(self, token_hash, now):
        token = self.verification
        if token is None or token["token_hash"] != token_hash:
            return False
        if token["consumed_at"] is not None or token["expires_at"] <= now:
            return False
        user = next(user for user in self.users.values() if user["id"] == token["user_id"])
        if user["status"] != "pending_email":
            return False
        token["consumed_at"] = now
        user["status"] = "active"
        user["email_verified_at"] = now
        return True


class RecordingMailer:
    def __init__(self):
        self.messages = []

    def send_verification(self, email, token, expires_at):
        self.messages.append((email, token, expires_at))


class FixedLimiter:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def allow(self, email_key, client_ip):
        return self.allowed


class EmailVerificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.repository = MemoryRepository()
        self.mailer = RecordingMailer()
        self.limiter = FixedLimiter()
        self.service = EmailVerificationService(
            repository=self.repository,
            mailer=self.mailer,
            limiter=self.limiter,
            token_ttl_seconds=600,
            token_factory=lambda: "A" * 43,
        )

    def test_registration_creates_pending_user_and_delivers_only_hashed_token_to_storage(self):
        result = self.service.register(
            email=" User@Example.com ",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )

        user = self.repository.users["user@example.com"]
        self.assertEqual(result.status, "accepted")
        self.assertEqual(user["status"], "pending_email")
        self.assertIsNone(user["email_verified_at"])
        self.assertEqual(self.mailer.messages[0][0], "user@example.com")
        self.assertEqual(
            self.repository.verification["token_hash"],
            sha256(self.mailer.messages[0][1].encode()).hexdigest(),
        )
        self.assertNotIn(self.mailer.messages[0][1], result.model_dump_json())

    def test_verification_activates_user_once(self):
        self.service.register(
            email="user@example.com",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )
        token = self.mailer.messages[0][1]

        self.assertEqual(self.service.verify(token, now=self.now), "verified")
        self.assertEqual(self.repository.users["user@example.com"]["status"], "active")
        self.assertFalse(self.service.verify(token, now=self.now))

    def test_rate_limited_registration_returns_same_public_response_without_sending_mail(self):
        self.limiter.allowed = False

        result = self.service.register(
            email="user@example.com",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )

        self.assertEqual(result.status, "accepted")
        self.assertEqual(self.mailer.messages, [])
        self.assertEqual(self.repository.users, {})

    def test_rate_limited_registration_defers_password_hashing(self):
        self.limiter.allowed = False
        calls = []

        self.service.register(
            email="user@example.com",
            password_hash=lambda: calls.append("hashed") or "password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )

        self.assertEqual(calls, [])

    def test_resend_for_unknown_email_does_not_create_an_account(self):
        result = self.service.resend(
            email="unknown@example.com",
            client_ip="192.0.2.1",
            now=self.now,
        )

        self.assertEqual(result.status, "accepted")
        self.assertEqual(self.repository.users, {})
        self.assertEqual(self.mailer.messages, [])


if __name__ == "__main__":
    unittest.main()
