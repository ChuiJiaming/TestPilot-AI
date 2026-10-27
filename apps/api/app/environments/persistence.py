from datetime import datetime
import base64
import binascii
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.auth.lifecycle import SecretRefRecord, SecretRefVersionRecord
from apps.api.app.environments.snapshots import build_environment_snapshot
from apps.api.app.tenancy.persistence import ProjectRecord
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationProjectRepository,
    ProjectMembershipRecord,
)


class EnvironmentRecord(AuthBase):
    __tablename__ = "environments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    secret_ref_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("secret_refs.id")
    )
    network_policy: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EnvironmentSnapshotRecord(AuthBase):
    __tablename__ = "environment_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "snapshot_hash", name="uq_environment_snapshot_project_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    environment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("environments.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    network_policy: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    secret_ref_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("secret_ref_versions.id", ondelete="RESTRICT")
    )
    snapshot_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EnvironmentScopeError(Exception):
    pass


class EnvironmentPermissionDenied(Exception):
    pass


class EnvironmentRepository:
    """Tenant-scoped environment CRUD; this repository never performs network probes."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def list_for_project(self, actor_id, project_id, *, limit=30, cursor=None):
        cursor_key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            project = self._visible_project(session, actor_id, project_id)
            statement = select(EnvironmentRecord).where(EnvironmentRecord.project_id == project.id)
            if cursor_key:
                last_name, last_id = cursor_key
                statement = statement.where(
                    (EnvironmentRecord.name > last_name)
                    | ((EnvironmentRecord.name == last_name) & (EnvironmentRecord.id > last_id))
                )
            rows = session.scalars(
                statement.order_by(EnvironmentRecord.name, EnvironmentRecord.id).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            return {
                "items": [self._environment_dict(row) for row in items],
                "next_cursor": self._encode_cursor(items[-1]) if has_more and items else None,
            }

    def create(
        self, actor_id, project_id, *, name, base_url, network_policy, secret_ref_id, now
    ):
        with self.session_factory.begin() as session:
            project = self._visible_project(session, actor_id, project_id, for_write=True)
            if network_policy:
                self._require_policy_admin(session, actor_id, project)
            self._validate_secret_ref(session, project, secret_ref_id)
            environment = EnvironmentRecord(
                id=uuid4(), project_id=project.id, name=name, base_url=base_url,
                network_policy=network_policy, secret_ref_id=secret_ref_id, created_at=now,
            )
            session.add(environment)
            session.flush()
            return self._environment_dict(environment)

    def update(self, actor_id, environment_id, *, changes, now):
        with self.session_factory.begin() as session:
            environment = session.get(EnvironmentRecord, environment_id, with_for_update=True)
            if environment is None:
                raise EnvironmentScopeError("environment not found")
            project = self._visible_project(
                session, actor_id, environment.project_id, for_write=True
            )
            if "network_policy" in changes:
                self._require_policy_admin(session, actor_id, project)
            if "secret_ref_id" in changes:
                self._validate_secret_ref(session, project, changes["secret_ref_id"])
            for field in ("name", "base_url", "network_policy", "secret_ref_id"):
                if field in changes:
                    setattr(environment, field, changes[field])
            session.flush()
            return self._environment_dict(environment)

    def project_id_for_environment(self, environment_id):
        with self.session_factory() as session:
            return session.scalar(
                select(EnvironmentRecord.project_id).where(EnvironmentRecord.id == environment_id)
            )

    def health_check_context(self, actor_id, environment_id):
        """Load an authorized environment and active SecretRef version metadata only."""
        with self.session_factory() as session:
            environment = session.get(EnvironmentRecord, environment_id)
            if environment is None:
                raise EnvironmentScopeError("environment not found")
            project = self._visible_project(
                session, actor_id, environment.project_id, for_write=True
            )
            secret_version_id = None
            if environment.secret_ref_id is not None:
                secret_ref = session.get(SecretRefRecord, environment.secret_ref_id)
                if secret_ref is None or secret_ref.organization_id != project.organization_id:
                    raise EnvironmentScopeError("secret reference is outside project organization")
                version = session.scalar(
                    select(SecretRefVersionRecord)
                    .where(
                        SecretRefVersionRecord.secret_ref_id == environment.secret_ref_id,
                        SecretRefVersionRecord.retired_at.is_(None),
                    )
                    .order_by(SecretRefVersionRecord.version.desc())
                    .limit(1)
                )
                if version is None:
                    raise EnvironmentScopeError("secret reference has no active version")
                secret_version_id = version.id
            return {
                "id": environment.id,
                "project_id": environment.project_id,
                "base_url": environment.base_url,
                "network_policy": environment.network_policy,
                "secret_ref_id": environment.secret_ref_id,
                "secret_ref_version_id": secret_version_id,
            }

    @staticmethod
    def _visible_project(session, actor_id, project_id, *, for_write=False):
        project = OrganizationProjectRepository._get_visible_project(session, actor_id, project_id)
        if for_write:
            org_membership = session.get(
                OrganizationMembershipRecord, (project.organization_id, actor_id)
            )
            project_membership = session.get(ProjectMembershipRecord, (project.id, actor_id))
            allowed = (
                org_membership is not None and org_membership.role == "org_admin"
            ) or (
                project_membership is not None
                and project_membership.role in {"project_admin", "tester"}
            )
            if not allowed:
                raise EnvironmentPermissionDenied("environment write scope denied")
        return project

    @staticmethod
    def _require_policy_admin(session, actor_id, project):
        org_membership = session.get(
            OrganizationMembershipRecord, (project.organization_id, actor_id)
        )
        project_membership = session.get(ProjectMembershipRecord, (project.id, actor_id))
        if not (
            org_membership is not None and org_membership.role == "org_admin"
        ) and not (
            project_membership is not None and project_membership.role == "project_admin"
        ):
            raise EnvironmentPermissionDenied("network policy changes require an organization or project admin")

    @staticmethod
    def _validate_secret_ref(session, project, secret_ref_id):
        if secret_ref_id is None:
            return
        secret_ref = session.get(SecretRefRecord, secret_ref_id)
        if secret_ref is None or secret_ref.organization_id != project.organization_id:
            raise EnvironmentScopeError("secret reference is outside project organization")

    @staticmethod
    def _environment_dict(environment):
        return {
            "id": environment.id,
            "project_id": environment.project_id,
            "name": environment.name,
            "base_url": environment.base_url,
            "secret_ref_id": environment.secret_ref_id,
            "network_policy": environment.network_policy,
            "created_at": environment.created_at,
        }

    @staticmethod
    def _encode_cursor(environment):
        value = f"{environment.name}\0{environment.id}".encode("utf-8")
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor):
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
            name, raw_id = raw.split("\0", 1)
            return name, UUID(raw_id)
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ValueError("Invalid environment cursor") from exc


class EnvironmentSnapshotRepository:
    """Persists immutable snapshots from scoped environment and secret metadata."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def capture(self, *, environment_id, project_id, now, session=None):
        if session is not None:
            return self._capture_in_session(
                session, environment_id=environment_id, project_id=project_id, now=now
            )
        with self.session_factory.begin() as owned_session:
            return self._capture_in_session(
                owned_session, environment_id=environment_id, project_id=project_id, now=now
            )

    def _capture_in_session(self, session, *, environment_id, project_id, now):
        environment = session.scalar(
            select(EnvironmentRecord)
            .where(
                EnvironmentRecord.id == environment_id,
                EnvironmentRecord.project_id == project_id,
            )
            .with_for_update()
        )
        if environment is None:
            raise EnvironmentScopeError("environment not found in project")

        secret_version_id = None
        if environment.secret_ref_id is not None:
            project = session.get(ProjectRecord, project_id)
            secret_ref = session.get(SecretRefRecord, environment.secret_ref_id)
            if (
                project is None
                or secret_ref is None
                or secret_ref.organization_id != project.organization_id
            ):
                raise EnvironmentScopeError("secret reference is outside project organization")
            version = session.scalar(
                select(SecretRefVersionRecord)
                .where(
                    SecretRefVersionRecord.secret_ref_id == environment.secret_ref_id,
                    SecretRefVersionRecord.retired_at.is_(None),
                )
                .order_by(SecretRefVersionRecord.version.desc())
                .limit(1)
            )
            if version is None:
                raise EnvironmentScopeError("secret reference has no active version")
            secret_version_id = version.id

        snapshot = build_environment_snapshot(
            environment_id=environment.id,
            project_id=environment.project_id,
            base_url=environment.base_url,
            network_policy=environment.network_policy,
            secret_ref_version_id=secret_version_id,
        )
        existing = session.scalar(
            select(EnvironmentSnapshotRecord).where(
                EnvironmentSnapshotRecord.project_id == project_id,
                EnvironmentSnapshotRecord.snapshot_hash == snapshot.snapshot_hash,
            )
        )
        if existing is not None:
            return self._snapshot_dict(existing)
        record = EnvironmentSnapshotRecord(
            id=uuid4(),
            environment_id=environment.id,
            project_id=environment.project_id,
            source_updated_at=None,
            base_url=snapshot.base_url,
            network_policy=snapshot.network_policy,
            secret_ref_version_id=secret_version_id,
            snapshot_hash=snapshot.snapshot_hash,
            created_at=now,
        )
        session.add(record)
        session.flush()
        return self._snapshot_dict(record)

    def list_for_environment(self, *, environment_id, project_id):
        with self.session_factory() as session:
            environment = session.scalar(
                select(EnvironmentRecord).where(
                    EnvironmentRecord.id == environment_id,
                    EnvironmentRecord.project_id == project_id,
                )
            )
            if environment is None:
                raise EnvironmentScopeError("environment not found in project")
            records = session.scalars(
                select(EnvironmentSnapshotRecord)
                .where(
                    EnvironmentSnapshotRecord.environment_id == environment_id,
                    EnvironmentSnapshotRecord.project_id == project_id,
                )
                .order_by(EnvironmentSnapshotRecord.created_at.desc())
            ).all()
            return [self._snapshot_dict(record) for record in records]

    def project_id_for_environment(self, environment_id):
        with self.session_factory() as session:
            environment = session.get(EnvironmentRecord, environment_id)
            return environment.project_id if environment is not None else None

    @staticmethod
    def _snapshot_dict(record):
        return {
            "id": record.id,
            "environment_id": record.environment_id,
            "project_id": record.project_id,
            "source_updated_at": record.source_updated_at,
            "base_url": record.base_url,
            "network_policy": record.network_policy,
            "secret_ref_version_id": record.secret_ref_version_id,
            "snapshot_hash": record.snapshot_hash,
            "created_at": record.created_at,
        }
