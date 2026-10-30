from datetime import datetime, timezone
from hashlib import sha256
import unittest
from uuid import UUID

from apps.api.app.auth.verification import EmailVerificationService


class MemoryRepository:
    def __init__(self):
        self.users = {}
        self.verification = None
        self.outbox = []

    def issue(self, email, password_hash, display_name, token_hash, token_ciphertext, expires_at, now):
        user = self.users.get(email)
        if user is not None:
            return None
        user = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "email": email,
            "password_hash": password_hash,
            "display_name": display_name,
            "status": "pending_email",
            "email_verified_at": None,
        }
        self.users[email] = user
        self._save_verification_and_message(
            user["id"], token_hash, token_ciphertext, expires_at
        )
        return user["id"]

    def resend(self, email, token_hash, token_ciphertext, expires_at, now):
        user = self.users.get(email)
        if user is None or user["status"] != "pending_email":
            return None
        self._cancel_pending_messages(user["id"])
        self._save_verification_and_message(
            user["id"], token_hash, token_ciphertext, expires_at
        )
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
        self._cancel_pending_messages(user["id"])
        return True

    def _save_verification_and_message(self, user_id, token_hash, token_ciphertext, expires_at):
        self.verification = {
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": expires_at,
            "consumed_at": None,
        }
        self.outbox.append({
            "user_id": user_id,
            "token_hash": token_hash,
            "token_ciphertext": token_ciphertext,
            "expires_at": expires_at,
            "status": "PENDING",
        })

    def _cancel_pending_messages(self, user_id):
        for message in self.outbox:
            if message["user_id"] == user_id and message["status"] == "PENDING":
                message["status"] = "CANCELLED"
                message["token_ciphertext"] = ""


class RecordingEncryptor:
    def __init__(self):
        self.tokens = []

    def encrypt(self, token):
        self.tokens.append(token)
        return f"encrypted-{len(self.tokens)}"


class FixedLimiter:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def allow(self, email_key, client_ip):
        return self.allowed


class EmailVerificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.repository = MemoryRepository()
        self.encryptor = RecordingEncryptor()
        self.limiter = FixedLimiter()
        self.service = EmailVerificationService(
            repository=self.repository,
            token_encryptor=self.encryptor,
            limiter=self.limiter,
            token_ttl_seconds=600,
            token_factory=lambda: "A" * 43,
        )

    def test_registration_creates_pending_user_and_encrypted_outbox_message(self):
        result = self.service.register(
            email=" User@Example.com ",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )

        user = self.repository.users["user@example.com"]
        message = self.repository.outbox[0]
        raw_token = self.encryptor.tokens[0]
        self.assertEqual(result.status, "accepted")
        self.assertEqual(user["status"], "pending_email")
        self.assertIsNone(user["email_verified_at"])
        self.assertEqual(message["token_hash"], sha256(raw_token.encode()).hexdigest())
        self.assertEqual(message["token_ciphertext"], "encrypted-1")
        self.assertNotIn(raw_token, str(message))
        self.assertNotIn(raw_token, result.model_dump_json())

    def test_verification_activates_user_once_and_clears_pending_ciphertext(self):
        self.service.register(
            email="user@example.com",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )
        token = self.encryptor.tokens[0]

        self.assertEqual(self.service.verify(token, now=self.now), "verified")
        self.assertEqual(self.repository.users["user@example.com"]["status"], "active")
        self.assertFalse(self.service.verify(token, now=self.now))
        self.assertEqual(self.repository.outbox[0]["status"], "CANCELLED")
        self.assertEqual(self.repository.outbox[0]["token_ciphertext"], "")

    def test_pending_resend_replaces_old_encrypted_message(self):
        self.service.register(
            email="user@example.com",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )
        self.service.resend(email="user@example.com", client_ip="192.0.2.1", now=self.now)

        old_message, new_message = self.repository.outbox
        self.assertEqual(old_message["status"], "CANCELLED")
        self.assertEqual(old_message["token_ciphertext"], "")
        self.assertEqual(new_message["status"], "PENDING")
        self.assertEqual(new_message["token_ciphertext"], "encrypted-2")
        self.assertEqual(
            new_message["token_hash"],
            sha256(self.encryptor.tokens[1].encode()).hexdigest(),
        )

    def test_rate_limited_registration_returns_same_public_response_without_persisting(self):
        self.limiter.allowed = False

        result = self.service.register(
            email="user@example.com",
            password_hash="password-hash",
            display_name="User",
            client_ip="192.0.2.1",
            now=self.now,
        )

        self.assertEqual(result.status, "accepted")
        self.assertEqual(self.repository.outbox, [])
        self.assertEqual(self.repository.users, {})
        self.assertEqual(self.encryptor.tokens, [])

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

    def test_resend_for_unknown_email_does_not_create_an_account_or_outbox_message(self):
        result = self.service.resend(
            email="unknown@example.com",
            client_ip="192.0.2.1",
            now=self.now,
        )

        self.assertEqual(result.status, "accepted")
        self.assertEqual(self.repository.users, {})
        self.assertEqual(self.repository.outbox, [])


if __name__ == "__main__":
    unittest.main()
