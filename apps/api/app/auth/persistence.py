"""SQLAlchemy persistence for the candidate email-verification schema."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid, select, text, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class AuthBase(DeclarativeBase):
    pass


class UserRecord(AuthBase):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    platform_role: Mapped[str] = mapped_column(String, nullable=False, default="user")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_email")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerificationRecord(AuthBase):
    __tablename__ = "user_email_verifications"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VerificationEmailOutboxRecord(AuthBase):
    __tablename__ = "verification_email_outbox"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    verification_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_email_verifications.id", ondelete="CASCADE"), nullable=False
    )
    recipient_email: Mapped[str] = mapped_column(String, nullable=False)
    token_ciphertext: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSessionRecord(AuthBase):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_label: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshTokenRecord(AuthBase):
    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    token_family: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyVerificationRepository:
    """Repository methods own their transactions and never persist raw tokens."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def issue(self, email, password_hash, display_name, token_hash, token_ciphertext, expires_at, now):
        try:
            with self.session_factory.begin() as session:
                user = session.scalar(
                    select(UserRecord).where(UserRecord.email == email).with_for_update()
                )
                if user is not None:
                    # Repeated registration does not overwrite an account; resend is explicit.
                    return None
                user = UserRecord(
                    id=uuid4(),
                    email=email,
                    password_hash=password_hash,
                    display_name=display_name,
                    platform_role="user",
                    status="pending_email",
                    created_at=now,
                    email_verified_at=None,
                )
                session.add(user)
                session.flush()
                self._replace_token(session, user.id, email, token_hash, token_ciphertext, expires_at, now)
                return user.id
        except IntegrityError:
            # Concurrent registrations for the same unique email have the same public result.
            return None

    def resend(self, email, token_hash, token_ciphertext, expires_at, now):
        with self.session_factory.begin() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.email == email).with_for_update()
            )
            if user is None or user.status != "pending_email":
                return None
            self._replace_token(session, user.id, email, token_hash, token_ciphertext, expires_at, now)
            return user.id

    def claim_verification_emails(self, *, worker_id, now, limit=25, lease_seconds=60):
        with self.session_factory.begin() as session:
            session.execute(
                update(VerificationEmailOutboxRecord)
                .where(
                    VerificationEmailOutboxRecord.status.in_(("PENDING", "RETRY", "PROCESSING")),
                    VerificationEmailOutboxRecord.expires_at <= now,
                )
                .values(status="CANCELLED", token_ciphertext="", last_error="TOKEN_EXPIRED",
                        lease_owner=None, lease_until=None)
            )
            rows = session.scalars(
                select(VerificationEmailOutboxRecord)
                .where(
                    or_(
                        (VerificationEmailOutboxRecord.status.in_(("PENDING", "RETRY")))
                        & (VerificationEmailOutboxRecord.available_at <= now),
                        (VerificationEmailOutboxRecord.status == "PROCESSING")
                        & (VerificationEmailOutboxRecord.lease_until <= now),
                    ),
                    VerificationEmailOutboxRecord.expires_at > now,
                )
                .order_by(VerificationEmailOutboxRecord.available_at, VerificationEmailOutboxRecord.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            claimed = []
            for row in rows:
                row.status = "PROCESSING"
                row.lease_owner = worker_id
                row.lease_until = now + timedelta(seconds=lease_seconds)
                row.attempts += 1
                claimed.append({
                    "id": row.id,
                    "recipient_email": row.recipient_email,
                    "token_ciphertext": row.token_ciphertext,
                    "expires_at": row.expires_at,
                    "attempts": row.attempts,
                })
            return claimed

    def mark_verification_email_sent(self, outbox_id, *, worker_id, now):
        with self.session_factory.begin() as session:
            row = session.get(VerificationEmailOutboxRecord, outbox_id, with_for_update=True)
            if row is None or row.status != "PROCESSING" or row.lease_owner != worker_id:
                return False
            row.status = "SENT"
            row.sent_at = now
            row.token_ciphertext = ""
            row.last_error = None
            row.lease_owner = None
            row.lease_until = None
            return True

    def retry_verification_email(self, outbox_id, *, worker_id, now, error_code, max_attempts=8):
        with self.session_factory.begin() as session:
            row = session.get(VerificationEmailOutboxRecord, outbox_id, with_for_update=True)
            if row is None or row.status != "PROCESSING" or row.lease_owner != worker_id:
                return False
            row.last_error = error_code[:120]
            row.lease_owner = None
            row.lease_until = None
            if row.attempts >= max_attempts or row.expires_at <= now:
                row.status = "DEAD" if row.expires_at > now else "CANCELLED"
                row.token_ciphertext = ""
            else:
                row.status = "RETRY"
                row.available_at = now + timedelta(seconds=min(3600, 2 ** min(row.attempts, 12)))
            return True

    def list_dead_verification_emails(self, actor_id, *, limit=50):
        with self.session_factory() as session:
            user = session.get(UserRecord, actor_id)
            if user is None or user.platform_role != "platform_admin":
                raise PermissionError("platform administrator access required")
            rows = session.scalars(
                select(VerificationEmailOutboxRecord)
                .where(VerificationEmailOutboxRecord.status == "DEAD")
                .order_by(VerificationEmailOutboxRecord.created_at.desc(), VerificationEmailOutboxRecord.id.desc())
                .limit(limit)
            ).all()
            return [{
                "id": row.id,
                "recipient_email": row.recipient_email,
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
            } for row in rows]

    def consume(self, token_hash, now):
        with self.session_factory.begin() as session:
            verification = session.scalar(
                select(EmailVerificationRecord)
                .where(EmailVerificationRecord.token_hash == token_hash)
                .with_for_update()
            )
            if (
                verification is None
                or verification.consumed_at is not None
                or self._as_utc(verification.expires_at) <= self._as_utc(now)
            ):
                return False
            activated = session.execute(
                update(UserRecord)
                .where(
                    UserRecord.id == verification.user_id,
                    UserRecord.status == "pending_email",
                )
                .values(status="active", email_verified_at=now)
            )
            if activated.rowcount != 1:
                return False
            verification.consumed_at = now
            session.execute(
                update(VerificationEmailOutboxRecord)
                .where(
                    VerificationEmailOutboxRecord.verification_id == verification.id,
                    VerificationEmailOutboxRecord.status.in_(("PENDING", "RETRY", "PROCESSING")),
                )
                .values(status="CANCELLED", token_ciphertext="", lease_owner=None, lease_until=None)
            )
            return True

    def create_session(self, email, refresh_hash, refresh_expires_at, device_label, now):
        with self.session_factory.begin() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.email == email).with_for_update()
            )
            if user is None or user.status != "active":
                return None
            session_id = uuid4()
            session.add(
                UserSessionRecord(
                    id=session_id,
                    user_id=user.id,
                    device_label=device_label[:255],
                    created_at=now,
                    last_used_at=now,
                )
            )
            # PostgreSQL enforces refresh_tokens.token_family immediately. The ORM
            # has no relationship metadata to order these inserts for us.
            session.flush()
            session.add(
                RefreshTokenRecord(
                    id=uuid4(),
                    user_id=user.id,
                    token_hash=refresh_hash,
                    token_family=session_id,
                    expires_at=refresh_expires_at,
                    revoked_at=None,
                    rotated_at=None,
                    created_at=now,
                )
            )
            return self._user_dict(user, session_id)

    def get_login_user(self, email):
        with self.session_factory() as session:
            user = session.scalar(select(UserRecord).where(UserRecord.email == email))
            if user is None:
                return None
            return {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "status": user.status,
                "email_verified_at": user.email_verified_at,
                "password_hash": user.password_hash,
            }

    def get_user_by_id(self, user_id):
        with self.session_factory() as session:
            user = session.get(UserRecord, user_id)
            if user is None:
                return None
            return {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "status": user.status,
            }

    def authenticate_api_token(self, plaintext: str, now: datetime):
        """Resolve an active opaque API token without loading or returning its hash."""
        if not plaintext or len(plaintext) > 256 or not plaintext.startswith("tp_"):
            return None
        digest = sha256(plaintext.encode("utf-8")).hexdigest()
        with self.session_factory() as session:
            row = session.execute(text("""
                SELECT t.user_id, t.organization_id, t.scopes
                FROM api_tokens AS t
                JOIN users AS u ON u.id = t.user_id
                WHERE t.token_hash = :digest
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > :now)
                  AND u.status = 'active'
            """), {"digest": digest, "now": now}).mappings().first()
            if row is None:
                return None
            scopes = row["scopes"] or []
            if isinstance(scopes, str):
                scopes = json.loads(scopes)
            return {
                "id": str(UUID(str(row["user_id"]))),
                "credential_type": "api_token",
                "organization_id": str(UUID(str(row["organization_id"]))),
                "scopes": list(scopes),
            }

    def rotate_refresh(self, token_hash, next_hash, next_expires_at, now):
        with self.session_factory.begin() as session:
            token = session.scalar(
                select(RefreshTokenRecord)
                .where(RefreshTokenRecord.token_hash == token_hash)
                .with_for_update()
            )
            if token is None:
                return None
            user_session = session.get(UserSessionRecord, token.token_family, with_for_update=True)
            if user_session is None:
                return None
            if token.rotated_at is not None or token.revoked_at is not None:
                user_session.revoked_at = user_session.revoked_at or now
                session.execute(
                    update(RefreshTokenRecord)
                    .where(RefreshTokenRecord.token_family == token.token_family)
                    .values(revoked_at=now)
                )
                return None
            if self._as_utc(token.expires_at) <= self._as_utc(now):
                token.revoked_at = now
                user_session.revoked_at = now
                return None
            user = session.get(UserRecord, token.user_id)
            if user is None or user.status != "active":
                token.revoked_at = now
                user_session.revoked_at = now
                return None

            token.rotated_at = now
            user_session.last_used_at = now
            session.add(
                RefreshTokenRecord(
                    id=uuid4(),
                    user_id=user.id,
                    token_hash=next_hash,
                    token_family=user_session.id,
                    expires_at=next_expires_at,
                    revoked_at=None,
                    rotated_at=None,
                    created_at=now,
                )
            )
            return self._user_dict(user, user_session.id)

    def session_is_active(self, user_id, session_id, now):
        with self.session_factory() as session:
            row = session.scalar(
                select(UserSessionRecord)
                .join(UserRecord, UserRecord.id == UserSessionRecord.user_id)
                .where(
                    UserSessionRecord.id == session_id,
                    UserSessionRecord.user_id == user_id,
                    UserSessionRecord.revoked_at.is_(None),
                    UserRecord.status == "active",
                )
            )
            return row is not None

    def list_sessions(self, user_id):
        with self.session_factory() as session:
            rows = session.scalars(
                select(UserSessionRecord)
                .where(UserSessionRecord.user_id == user_id)
                .order_by(UserSessionRecord.last_used_at.desc())
            ).all()
            return [
                {
                    "id": row.id,
                    "device_label": row.device_label,
                    "created_at": row.created_at,
                    "last_used_at": row.last_used_at,
                    "revoked_at": row.revoked_at,
                }
                for row in rows
            ]

    def revoke_session(self, user_id, session_id, now):
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(UserSessionRecord)
                .where(
                    UserSessionRecord.id == session_id,
                    UserSessionRecord.user_id == user_id,
                    UserSessionRecord.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return False
            row.revoked_at = now
            session.execute(
                update(RefreshTokenRecord)
                .where(RefreshTokenRecord.token_family == session_id)
                .values(revoked_at=now)
            )
            return True

    def revoke_refresh(self, user_id, token_hash, now):
        with self.session_factory.begin() as session:
            token = session.scalar(
                select(RefreshTokenRecord)
                .where(RefreshTokenRecord.token_hash == token_hash)
                .with_for_update()
            )
            if token is None:
                return None
            if user_id is not None and token.user_id != user_id:
                return None
            session.execute(
                update(UserSessionRecord)
                .where(UserSessionRecord.id == token.token_family)
                .values(revoked_at=now)
            )
            session.execute(
                update(RefreshTokenRecord)
                .where(RefreshTokenRecord.token_family == token.token_family)
                .values(revoked_at=now)
            )
            return token.user_id

    def revoke_all_sessions(self, user_id, now):
        with self.session_factory.begin() as session:
            session.execute(
                update(UserSessionRecord)
                .where(UserSessionRecord.user_id == user_id, UserSessionRecord.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            session.execute(
                update(RefreshTokenRecord)
                .where(RefreshTokenRecord.user_id == user_id, RefreshTokenRecord.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    @staticmethod
    def _user_dict(user: UserRecord, session_id):
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "session_id": session_id,
        }

    @staticmethod
    def _replace_token(session: Session, user_id, email, token_hash, token_ciphertext, expires_at, now):
        session.execute(
            update(EmailVerificationRecord)
            .where(
                EmailVerificationRecord.user_id == user_id,
                EmailVerificationRecord.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        session.execute(
            update(VerificationEmailOutboxRecord)
            .where(
                VerificationEmailOutboxRecord.user_id == user_id,
                    VerificationEmailOutboxRecord.status.in_(("PENDING", "RETRY", "PROCESSING")),
            )
            .values(status="CANCELLED", token_ciphertext="", lease_owner=None, lease_until=None)
        )
        verification = EmailVerificationRecord(
                id=uuid4(),
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
                consumed_at=None,
                created_at=now,
            )
        session.add(verification)
        session.flush()
        session.add(VerificationEmailOutboxRecord(
            id=uuid4(), user_id=user_id, verification_id=verification.id,
            recipient_email=email, token_ciphertext=token_ciphertext, expires_at=expires_at,
            status="PENDING", attempts=0, available_at=now,
            lease_owner=None, lease_until=None, last_error=None, created_at=now, sent_at=None,
        ))

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
