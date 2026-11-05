"""Password reset: request a one-time token, then rotate the credential.

Security properties that the tests pin down:

* Requesting a reset for an unknown or disabled address returns exactly the same
  response as a known one, and never sends mail. Account existence is not
  observable through this endpoint.
* The raw token is only ever stored encrypted (for retryable delivery) and as a
  SHA-256 hash (for lookup). A database reader cannot reconstruct it.
* Consuming a token rotates the password *and* revokes every session and refresh
  token for that account in the same transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from typing import Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from apps.api.app.auth.verification import (
    VerificationRateLimiter,
    VerificationRepository,
    VerificationTokenEncryptor,
)


class ResetAccepted(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    message: str


class PasswordResetService:
    def __init__(
        self,
        *,
        repository: VerificationRepository,
        token_encryptor: VerificationTokenEncryptor,
        limiter: VerificationRateLimiter,
        token_ttl_seconds: int,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if token_ttl_seconds <= 0:
            raise ValueError("token_ttl_seconds must be positive")
        self.repository = repository
        self.token_encryptor = token_encryptor
        self.limiter = limiter
        self.token_ttl_seconds = token_ttl_seconds
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def request(
        self,
        *,
        email: str,
        client_ip: str | None,
        now: datetime | None = None,
    ) -> ResetAccepted:
        normalized_email = email.strip().casefold()
        current_time = now or datetime.now(timezone.utc)
        email_key = sha256(normalized_email.encode("utf-8")).hexdigest()
        if not self.limiter.allow(email_key, client_ip):
            return self._accepted()

        token = self.token_factory()
        self.repository.issue_password_reset(
            email=normalized_email,
            token_hash=sha256(token.encode("utf-8")).hexdigest(),
            token_ciphertext=self.token_encryptor.encrypt(token),
            expires_at=current_time + timedelta(seconds=self.token_ttl_seconds),
            now=current_time,
        )
        return self._accepted()

    def confirm(
        self,
        token: str,
        *,
        password_hash: str | Callable[[], str],
        now: datetime | None = None,
    ) -> bool:
        if not token or len(token) > 256:
            return False
        encoded = password_hash() if callable(password_hash) else password_hash
        current_time = now or datetime.now(timezone.utc)
        return self.repository.consume_password_reset(
            sha256(token.encode("utf-8")).hexdigest(),
            encoded,
            current_time,
        )

    @staticmethod
    def _accepted() -> ResetAccepted:
        return ResetAccepted(
            status="accepted",
            message="If the address belongs to an active account, reset instructions may be sent.",
        )
