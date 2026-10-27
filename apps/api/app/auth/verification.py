from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from typing import Callable, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class VerificationRepository(Protocol):
    def issue(
        self,
        email: str,
        password_hash: str,
        display_name: str,
        token_hash: str,
        token_ciphertext: str,
        expires_at: datetime,
        now: datetime,
    ) -> UUID | None: ...

    def resend(
        self,
        email: str,
        token_hash: str,
        token_ciphertext: str,
        expires_at: datetime,
        now: datetime,
    ) -> UUID | None: ...

    def consume(self, token_hash: str, now: datetime) -> bool: ...


class VerificationTokenEncryptor(Protocol):
    def encrypt(self, token: str) -> str: ...


class VerificationRateLimiter(Protocol):
    def allow(self, email_key: str, client_ip: str | None) -> bool: ...


class RegistrationAccepted(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    message: str
    verification_required: bool


class EmailVerificationService:
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

    def register(
        self,
        *,
        email: str,
        password_hash: str | Callable[[], str],
        display_name: str,
        client_ip: str | None,
        now: datetime | None = None,
    ) -> RegistrationAccepted:
        normalized_email = email.strip().casefold()
        current_time = now or datetime.now(timezone.utc)
        email_key = sha256(normalized_email.encode("utf-8")).hexdigest()
        if not self.limiter.allow(email_key, client_ip):
            return self._accepted()

        encoded_password = password_hash() if callable(password_hash) else password_hash
        token = self.token_factory()
        expires_at = current_time + timedelta(seconds=self.token_ttl_seconds)
        user_id = self.repository.issue(
            email=normalized_email,
            password_hash=encoded_password,
            display_name=display_name,
            token_hash=sha256(token.encode("utf-8")).hexdigest(),
            token_ciphertext=self.token_encryptor.encrypt(token),
            expires_at=expires_at,
            now=current_time,
        )
        if user_id is None:
            return self._accepted()

        return self._accepted()

    def resend(
        self,
        *,
        email: str,
        client_ip: str | None,
        now: datetime | None = None,
    ) -> RegistrationAccepted:
        normalized_email = email.strip().casefold()
        current_time = now or datetime.now(timezone.utc)
        email_key = sha256(normalized_email.encode("utf-8")).hexdigest()
        if not self.limiter.allow(email_key, client_ip):
            return self._accepted()

        token = self.token_factory()
        expires_at = current_time + timedelta(seconds=self.token_ttl_seconds)
        user_id = self.repository.resend(
            email=normalized_email,
            token_hash=sha256(token.encode("utf-8")).hexdigest(),
            token_ciphertext=self.token_encryptor.encrypt(token),
            expires_at=expires_at,
            now=current_time,
        )
        if user_id is None:
            return self._accepted()
        return self._accepted()

    @staticmethod
    def _accepted() -> RegistrationAccepted:
        return RegistrationAccepted(
            status="accepted",
            message="If the address is eligible, verification instructions may be sent.",
            verification_required=True,
        )

    def verify(self, token: str, *, now: datetime | None = None) -> str | None:
        if not token or len(token) > 256:
            return None
        current_time = now or datetime.now(timezone.utc)
        token_hash = sha256(token.encode("utf-8")).hexdigest()
        return "verified" if self.repository.consume(token_hash, current_time) else None

    def list_dead_email_messages(self, actor_id: UUID, *, limit: int = 50):
        return self.repository.list_dead_verification_emails(actor_id, limit=limit)
