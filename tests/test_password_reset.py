"""Password reset: enumeration safety, token lifecycle, session revocation."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.adapters import VerificationTokenCipher
from apps.api.app.auth.password_reset import PasswordResetService
from apps.api.app.auth.persistence import (
    AuthBase,
    EmailVerificationRecord,
    RefreshTokenRecord,
    SqlAlchemyVerificationRepository,
    UserRecord,
    UserSessionRecord,
    VerificationEmailOutboxRecord,
)
from apps.api.app.main import create_app


class AllowAllLimiter:
    def allow(self, email_key, client_ip):
        return True


class DenyAllLimiter:
    def allow(self, email_key, client_ip):
        return False


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _active_user(sessions, email="reset@example.test", password_hash="old-hash"):
    user_id = uuid4()
    now = datetime.now(timezone.utc)
    with sessions.begin() as session:
        session.add(UserRecord(
            id=user_id,
            email=email,
            password_hash=password_hash,
            display_name="Reset User",
            platform_role="user",
            status="active",
            created_at=now,
            email_verified_at=now,
        ))
    return user_id


def _service(sessions, *, limiter=None, token="reset-token-" + "x" * 32, ttl=3600):
    return PasswordResetService(
        repository=SqlAlchemyVerificationRepository(sessions),
        token_encryptor=VerificationTokenCipher(Fernet.generate_key().decode()),
        limiter=limiter or AllowAllLimiter(),
        token_ttl_seconds=ttl,
        token_factory=lambda: token,
    ), token


def test_request_for_unknown_address_is_indistinguishable_and_sends_nothing():
    sessions = _factory()
    service, _ = _service(sessions)
    result = service.request(email="nobody@example.test", client_ip="1.2.3.4")
    assert result.status == "accepted"
    with sessions() as session:
        assert session.scalars(select(EmailVerificationRecord)).all() == []
        assert session.scalars(select(VerificationEmailOutboxRecord)).all() == []


def test_request_enqueues_an_encrypted_reset_token():
    sessions = _factory()
    _active_user(sessions)
    token = "reset-token-" + "y" * 32
    service, _ = _service(sessions, token=token)

    assert service.request(email="reset@example.test", client_ip=None).status == "accepted"

    with sessions() as session:
        record = session.scalars(select(EmailVerificationRecord)).one()
        outbox = session.scalars(select(VerificationEmailOutboxRecord)).one()
    assert record.purpose == "RESET"
    assert record.token_hash != token  # never store the raw token
    assert outbox.purpose == "RESET"
    assert token not in outbox.token_ciphertext


def test_rate_limited_request_still_returns_accepted_but_issues_nothing():
    sessions = _factory()
    _active_user(sessions)
    service, _ = _service(sessions, limiter=DenyAllLimiter())
    assert service.request(email="reset@example.test", client_ip=None).status == "accepted"
    with sessions() as session:
        assert session.scalars(select(EmailVerificationRecord)).all() == []


def test_confirm_rotates_password_and_revokes_sessions_and_refresh_tokens():
    sessions = _factory()
    user_id = _active_user(sessions)
    service, token = _service(sessions)
    service.request(email="reset@example.test", client_ip=None)

    now = datetime.now(timezone.utc)
    session_id = uuid4()
    with sessions.begin() as session:
        session.add(UserSessionRecord(
            id=session_id, user_id=user_id, device_label="laptop",
            created_at=now, last_used_at=now, revoked_at=None,
        ))
        session.add(RefreshTokenRecord(
            id=uuid4(), user_id=user_id, token_hash="refresh-hash",
            token_family=session_id, expires_at=now + timedelta(days=1),
            revoked_at=None, rotated_at=None, created_at=now,
        ))

    assert service.confirm(token, password_hash="new-hash") is True

    with sessions() as session:
        user = session.get(UserRecord, user_id)
        assert user.password_hash == "new-hash"
        assert session.scalars(select(UserSessionRecord)).one().revoked_at is not None
        assert session.scalars(select(RefreshTokenRecord)).one().revoked_at is not None

    # A token is single-use.
    assert service.confirm(token, password_hash="another-hash") is False
    with sessions() as session:
        assert session.get(UserRecord, user_id).password_hash == "new-hash"


def test_expired_or_unknown_tokens_are_rejected():
    sessions = _factory()
    _active_user(sessions)
    service, token = _service(sessions)
    service.request(email="reset@example.test", client_ip=None)

    assert service.confirm("a" * 40, password_hash="x") is False
    assert service.confirm(token, password_hash="x", now=datetime.now(timezone.utc) + timedelta(hours=2)) is False


def test_reset_does_not_cancel_a_pending_email_verification():
    sessions = _factory()
    user_id = _active_user(sessions)
    now = datetime.now(timezone.utc)
    with sessions.begin() as session:
        session.add(EmailVerificationRecord(
            id=uuid4(), user_id=user_id, token_hash="verify-hash",
            expires_at=now + timedelta(days=1), consumed_at=None,
            created_at=now, purpose="VERIFY",
        ))

    service, _ = _service(sessions)
    service.request(email="reset@example.test", client_ip=None)

    with sessions() as session:
        pending = session.scalar(
            select(EmailVerificationRecord).where(EmailVerificationRecord.token_hash == "verify-hash")
        )
    assert pending.consumed_at is None


def test_http_endpoints_are_wired_and_keep_the_same_shape_for_unknown_addresses():
    sessions = _factory()
    _active_user(sessions)
    service, token = _service(sessions)
    app = create_app(
        password_reset_service=service,
        password_hasher=lambda value: f"scrypt:{value}",
    )
    client = TestClient(app)

    known = client.post("/api/v1/auth/password-reset/request", json={"email": "reset@example.test"})
    unknown = client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.test"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()

    assert client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "not-the-token-" + "z" * 30, "new_password": "a-perfectly-long-password"},
    ).status_code == 400

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "a-perfectly-long-password"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "reset"}


def test_short_password_is_rejected_by_the_schema():
    service, _ = _service(_factory())
    client = TestClient(create_app(password_reset_service=service, password_hasher=lambda v: v))
    response = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "x" * 32, "new_password": "short"},
    )
    assert response.status_code == 422
