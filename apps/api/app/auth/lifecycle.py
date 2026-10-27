"""Persistence and services for invitations, API tokens, and SecretRef metadata.

Secret values are owned by an external provider. This module stores only opaque
provider-version references and never accepts or returns secret material.
"""

from datetime import datetime, timezone
from hashlib import sha256
import secrets
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Uuid, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


PERMISSION_REGISTRY = frozenset({
    "approval:decide", "organization:create", "organization:read",
    "organization:write", "platform:admin", "resource:read",
    "resource:write", "run:create", "run:read", "user:self",
})
ORGANIZATION_TOKEN_UNSCOPED_PERMISSIONS = frozenset({"organization:create", "platform:admin"})


class InvitationRecord(AuthBase):
    __tablename__ = "invitations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiTokenRecord(AuthBase):
    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    scopes: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecretRefRecord(AuthBase):
    __tablename__ = "secret_refs"
    __table_args__ = (UniqueConstraint("organization_id", "provider", "external_key", name="uq_secret_ref_scope"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    external_key: Mapped[str] = mapped_column(String, nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecretRefVersionRecord(AuthBase):
    __tablename__ = "secret_ref_versions"
    __table_args__ = (UniqueConstraint("secret_ref_id", "version", name="uq_secret_ref_version"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    secret_ref_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("secret_refs.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_version: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LifecycleNotFound(Exception):
    pass


class LifecycleForbidden(Exception):
    pass


class InvalidInvitation(Exception):
    pass


class LifecycleRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def token_hash(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        # SQLite drops timezone metadata for DateTime(timezone=True); production PostgreSQL does not.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    def create_invitation(self, actor_id, organization_id, *, project_id, email, role, expires_at, now):
        normalized_email = email.strip().casefold()
        token = secrets.token_urlsafe(32)
        with self.session_factory.begin() as session:
            org = session.get(OrganizationRecord, organization_id)
            membership = session.get(OrganizationMembershipRecord, (organization_id, actor_id))
            project = session.get(ProjectRecord, project_id) if project_id else None
            project_membership = session.get(ProjectMembershipRecord, (project_id, actor_id)) if project_id else None
            if org is None or membership is None or (membership.role != "org_admin" and
                    (project_id is None or project is None or project.organization_id != organization_id
                     or project_membership is None or project_membership.role != "project_admin")):
                raise LifecycleNotFound
            if (project_id is None and role not in {"org_admin", "org_member"}) or (
                    project_id is not None and role not in {"project_admin", "tester", "developer", "viewer"}):
                raise ValueError("role does not match invitation scope")
            if expires_at <= now:
                raise ValueError("expires_at must be in the future")
            record = InvitationRecord(
                id=uuid4(), organization_id=organization_id, project_id=project_id,
                email=normalized_email, role=role, token_hash=self.token_hash(token),
                expires_at=expires_at, accepted_at=None, created_at=now,
            )
            session.add(record)
            session.flush()
            return {"id": record.id, "organization_id": organization_id, "project_id": project_id,
                    "email": normalized_email, "role": role, "expires_at": expires_at,
                    "created_at": now, "token": token}

    def accept_invitation(self, actor_id, token, *, now):
        with self.session_factory.begin() as session:
            invitation = session.scalar(select(InvitationRecord).where(
                InvitationRecord.token_hash == self.token_hash(token)
            ).with_for_update())
            user = session.get(UserRecord, actor_id)
            if (invitation is None or user is None or invitation.accepted_at is not None
                    or self._utc(invitation.expires_at) <= self._utc(now)
                    or user.email.strip().casefold() != invitation.email):
                raise InvalidInvitation
            org_membership = session.get(OrganizationMembershipRecord, (invitation.organization_id, actor_id))
            if org_membership is None:
                session.add(OrganizationMembershipRecord(
                    organization_id=invitation.organization_id, user_id=actor_id,
                    role=invitation.role if invitation.project_id is None else "org_member", created_at=now,
                ))
            elif invitation.project_id is None:
                org_membership.role = invitation.role
            if invitation.project_id is not None:
                project = session.get(ProjectRecord, invitation.project_id)
                if project is None or project.organization_id != invitation.organization_id:
                    raise InvalidInvitation
                member = session.get(ProjectMembershipRecord, (invitation.project_id, actor_id))
                if member is None:
                    session.add(ProjectMembershipRecord(
                        project_id=invitation.project_id, user_id=actor_id,
                        role=invitation.role, created_at=now,
                    ))
                else:
                    member.role = invitation.role
            invitation.accepted_at = now
            return {"status": "accepted", "organization_id": invitation.organization_id,
                    "project_id": invitation.project_id, "role": invitation.role}

    def create_api_token(self, actor_id, organization_id, *, name, scopes, expires_at, now):
        if not scopes or not set(scopes) <= PERMISSION_REGISTRY:
            raise ValueError("scopes must be non-empty registered permissions")
        if set(scopes) & ORGANIZATION_TOKEN_UNSCOPED_PERMISSIONS:
            raise ValueError("organization-bound tokens cannot grant global permissions")
        token = "tp_" + secrets.token_urlsafe(36)
        with self.session_factory.begin() as session:
            membership = session.get(OrganizationMembershipRecord, (organization_id, actor_id))
            if membership is None or membership.role != "org_admin":
                raise LifecycleNotFound
            record = ApiTokenRecord(
                id=uuid4(), organization_id=organization_id, user_id=actor_id,
                name=name, token_hash=self.token_hash(token), scopes=sorted(set(scopes)),
                expires_at=expires_at, revoked_at=None, created_at=now,
            )
            session.add(record)
            session.flush()
            return {**self._api_token_dict(record), "token": token}

    def list_api_tokens(self, actor_id, *, organization_id=None):
        with self.session_factory() as session:
            statement = select(ApiTokenRecord).where(ApiTokenRecord.user_id == actor_id)
            if organization_id:
                statement = statement.where(ApiTokenRecord.organization_id == organization_id)
            return [self._api_token_dict(row) for row in session.scalars(statement.order_by(ApiTokenRecord.created_at.desc())).all()]

    def revoke_api_token(self, actor_id, token_id, *, now):
        with self.session_factory.begin() as session:
            token = session.get(ApiTokenRecord, token_id, with_for_update=True)
            if token is None:
                raise LifecycleNotFound
            membership = session.get(OrganizationMembershipRecord, (token.organization_id, actor_id))
            if membership is None or membership.role != "org_admin":
                raise LifecycleNotFound
            if token.revoked_at is None:
                token.revoked_at = now
            return self._api_token_dict(token)

    def rotate_api_token(self, actor_id, token_id, *, now):
        with self.session_factory.begin() as session:
            token = session.get(ApiTokenRecord, token_id, with_for_update=True)
            if token is None:
                raise LifecycleNotFound
            membership = session.get(OrganizationMembershipRecord, (token.organization_id, actor_id))
            if membership is None or membership.role != "org_admin":
                raise LifecycleNotFound
            if token.revoked_at is not None:
                raise ValueError("revoked token cannot be rotated")
            plaintext = "tp_" + secrets.token_urlsafe(36)
            token.token_hash = self.token_hash(plaintext)
            return {**self._api_token_dict(token), "token": plaintext}

    def list_secret_refs(self, actor_id, *, organization_id=None):
        with self.session_factory() as session:
            query = select(SecretRefRecord).join(
                OrganizationMembershipRecord,
                OrganizationMembershipRecord.organization_id == SecretRefRecord.organization_id,
            ).where(OrganizationMembershipRecord.user_id == actor_id)
            if organization_id:
                query = query.where(SecretRefRecord.organization_id == organization_id)
            rows = session.scalars(query.order_by(SecretRefRecord.created_at.desc())).all()
            result = []
            for row in rows:
                version = session.scalar(select(SecretRefVersionRecord).where(
                    SecretRefVersionRecord.secret_ref_id == row.id,
                    SecretRefVersionRecord.retired_at.is_(None),
                ).order_by(SecretRefVersionRecord.version.desc()).limit(1))
                result.append(self._secret_dict(row, version=version))
            return result

    def create_secret_ref(self, actor_id, organization_id, *, provider, external_key, provider_version, now):
        with self.session_factory.begin() as session:
            membership = session.get(OrganizationMembershipRecord, (organization_id, actor_id))
            if membership is None or membership.role != "org_admin":
                raise LifecycleNotFound
            ref = SecretRefRecord(id=uuid4(), organization_id=organization_id,
                provider=provider, external_key=external_key, rotated_at=None, created_at=now)
            session.add(ref)
            session.flush()
            version = SecretRefVersionRecord(id=uuid4(), secret_ref_id=ref.id,
                version=1, provider_version=provider_version, created_by=actor_id,
                created_at=now, retired_at=None)
            session.add(version)
            session.flush()
            return self._secret_dict(ref, version=version)

    def rotate_secret_ref(self, actor_id, secret_ref_id, *, provider_version, now):
        with self.session_factory.begin() as session:
            ref = session.get(SecretRefRecord, secret_ref_id, with_for_update=True)
            if ref is None:
                raise LifecycleNotFound
            membership = session.get(OrganizationMembershipRecord, (ref.organization_id, actor_id))
            if membership is None or membership.role != "org_admin":
                raise LifecycleNotFound
            latest = session.scalar(select(SecretRefVersionRecord).where(
                SecretRefVersionRecord.secret_ref_id == ref.id
            ).order_by(SecretRefVersionRecord.version.desc()).limit(1))
            if latest is not None and latest.retired_at is None:
                latest.retired_at = now
            version = SecretRefVersionRecord(id=uuid4(), secret_ref_id=ref.id,
                version=(latest.version + 1 if latest else 1), provider_version=provider_version,
                created_by=actor_id, created_at=now, retired_at=None)
            session.add(version)
            ref.rotated_at = now
            session.flush()
            return self._secret_dict(ref, version=version)

    @staticmethod
    def _api_token_dict(row):
        return {"id": row.id, "organization_id": row.organization_id, "name": row.name,
                "scopes": row.scopes, "expires_at": row.expires_at, "revoked_at": row.revoked_at,
                "created_at": row.created_at}

    @staticmethod
    def _secret_dict(row, *, version=None):
        return {"id": row.id, "organization_id": row.organization_id, "provider": row.provider,
                "external_key": row.external_key, "rotated_at": row.rotated_at,
                "created_at": row.created_at, "active_version": version.version if version else None}
