"""Login, rotating refresh sessions, and short-lived bearer token handling."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from typing import Callable
from uuid import UUID

import jwt

from apps.api.app.auth.verification import VerificationRateLimiter


@dataclass(frozen=True)
class AuthSessionTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    user: dict[str, str]
    session_id: str
    device_label: str


class AuthSessionService:
    def __init__(
        self,
        *,
        repository,
        limiter: VerificationRateLimiter,
        password_verifier: Callable[[str, str], bool],
        jwt_signing_key: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
        token_factory: Callable[[], str] | None = None,
    ):
        if len(jwt_signing_key.encode("utf-8")) < 32:
            raise ValueError("JWT signing key must be at least 32 bytes")
        if access_ttl_seconds != 900 or refresh_ttl_seconds != 2_592_000:
            raise ValueError("Phase 1 token lifetimes are 900 seconds and 2,592,000 seconds")
        self.repository = repository
        self.limiter = limiter
        self.password_verifier = password_verifier
        self.jwt_signing_key = jwt_signing_key
        self.access_ttl_seconds = access_ttl_seconds
        self.refresh_ttl_seconds = refresh_ttl_seconds
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(48))

    def login(self, *, email, password, device_label, client_ip, now=None):
        current_time = now or datetime.now(timezone.utc)
        normalized_email = email.strip().casefold()
        email_key = sha256(normalized_email.encode("utf-8")).hexdigest()
        if not self.limiter.allow(email_key, client_ip):
            return None
        credentials = self.repository.get_login_user(normalized_email)
        if credentials is None or not self.password_verifier(password, credentials["password_hash"]):
            return None
        label = (device_label or "Unknown device").strip()[:255] or "Unknown device"
        refresh_token = self.token_factory()
        user = self.repository.create_session(
            normalized_email,
            self._token_hash(refresh_token),
            current_time + timedelta(seconds=self.refresh_ttl_seconds),
            label,
            current_time,
        )
        if user is None:
            return None
        return self._tokens(user, refresh_token, current_time, label)

    def refresh(self, refresh_token: str, *, now=None) -> AuthSessionTokens | None:
        if not refresh_token or len(refresh_token) > 256:
            return None
        current_time = now or datetime.now(timezone.utc)
        next_token = self.token_factory()
        user = self.repository.rotate_refresh(
            self._token_hash(refresh_token),
            self._token_hash(next_token),
            current_time + timedelta(seconds=self.refresh_ttl_seconds),
            current_time,
        )
        if user is None:
            return None
        sessions = self.repository.list_sessions(user["id"])
        matching = next((item for item in sessions if item["id"] == user["session_id"]), None)
        label = matching["device_label"] if matching else "Unknown device"
        return self._tokens(user, next_token, current_time, label)

    def authenticate(self, access_token: str, *, now=None) -> dict[str, str] | None:
        current_time = now or datetime.now(timezone.utc)
        try:
            claims = jwt.decode(
                access_token,
                self.jwt_signing_key,
                algorithms=["HS256"],
                options={
                    "require": ["exp", "iat", "sub", "sid", "jti"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            if int(claims["exp"]) <= int(current_time.timestamp()):
                return None
            if int(claims["iat"]) > int(current_time.timestamp()):
                return None
            user_id = UUID(claims["sub"])
            session_id = UUID(claims["sid"])
        except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
            token_authenticator = getattr(self.repository, "authenticate_api_token", None)
            return token_authenticator(access_token, current_time) if token_authenticator else None
        if not self.repository.session_is_active(user_id, session_id, current_time):
            return None
        return {"id": str(user_id), "session_id": str(session_id)}

    @staticmethod
    def _token_hash(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()

    def _tokens(self, user, refresh_token, now, device_label):
        user_id = str(user["id"])
        session_id = str(user["session_id"])
        issued_at = int(now.timestamp())
        access_token = jwt.encode(
            {
                "sub": user_id,
                "sid": session_id,
                "email": user["email"],
                "iat": issued_at,
                "exp": issued_at + self.access_ttl_seconds,
                "jti": secrets.token_urlsafe(16),
            },
            self.jwt_signing_key,
            algorithm="HS256",
        )
        return AuthSessionTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.access_ttl_seconds,
            user={
                "id": user_id,
                "email": user["email"],
                "display_name": user["display_name"],
                "status": user["status"],
            },
            session_id=session_id,
            device_label=device_label,
        )
