from datetime import datetime
import base64
import binascii
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, select
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase, UserRecord


class OrganizationRecord(AuthBase):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrganizationMembershipRecord(AuthBase):
    __tablename__ = "organization_memberships"

    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectRecord(AuthBase):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_project_org_slug"),
        UniqueConstraint("id", "organization_id", name="uq_project_id_org"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectMembershipRecord(AuthBase):
    __tablename__ = "project_memberships"

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ForbiddenScope(Exception):
    pass


class MissingResource(Exception):
    pass


class OrganizationProjectRepository:
    """Tenant-aware organization/project reads and writes."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_organization(self, actor_id, *, name, slug, now):
        with self.session_factory.begin() as session:
            organization = OrganizationRecord(
                id=uuid4(), name=name, slug=slug, created_at=now
            )
            session.add(organization)
            session.flush()
            session.add(
                OrganizationMembershipRecord(
                    organization_id=organization.id,
                    user_id=actor_id,
                    role="org_admin",
                    created_at=now,
                )
            )
            return self._org_dict(organization, "org_admin")

    def list_organizations(self, actor_id, *, limit=30, cursor=None, scoped_organization_id=None):
        cursor_key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            statement = (
                select(OrganizationRecord, OrganizationMembershipRecord.role)
                .join(
                    OrganizationMembershipRecord,
                    OrganizationMembershipRecord.organization_id == OrganizationRecord.id,
                )
                .where(OrganizationMembershipRecord.user_id == actor_id)
            )
            if scoped_organization_id is not None:
                statement = statement.where(OrganizationRecord.id == scoped_organization_id)
            if cursor_key:
                last_name, last_id = cursor_key
                statement = statement.where(
                    (OrganizationRecord.name > last_name)
                    | ((OrganizationRecord.name == last_name) & (OrganizationRecord.id > last_id))
                )
            rows = session.execute(
                statement.order_by(OrganizationRecord.name, OrganizationRecord.id).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            next_cursor = self._encode_cursor(items[-1][0]) if has_more and items else None
            return {
                "items": [self._org_dict(org, role) for org, role in items],
                "next_cursor": next_cursor,
            }

    def create_project(self, actor_id, *, organization_id, name, slug, now):
        with self.session_factory.begin() as session:
            membership = session.get(OrganizationMembershipRecord, (organization_id, actor_id))
            if membership is None:
                raise ForbiddenScope
            project = ProjectRecord(
                id=uuid4(),
                organization_id=organization_id,
                name=name,
                slug=slug,
                created_by=actor_id,
                created_at=now,
            )
            session.add(project)
            session.flush()
            session.add(
                ProjectMembershipRecord(
                    project_id=project.id,
                    user_id=actor_id,
                    role="project_admin",
                    created_at=now,
                )
            )
            return self._project_dict(project, "project_admin")

    def list_projects(self, actor_id, *, scoped_organization_id=None):
        with self.session_factory() as session:
            rows = session.execute(
                select(ProjectRecord, OrganizationMembershipRecord.role, ProjectMembershipRecord.role)
                .join(
                    OrganizationMembershipRecord,
                    OrganizationMembershipRecord.organization_id == ProjectRecord.organization_id,
                )
                .outerjoin(
                    ProjectMembershipRecord,
                    (ProjectMembershipRecord.project_id == ProjectRecord.id)
                    & (ProjectMembershipRecord.user_id == actor_id),
                )
                .where(
                    OrganizationMembershipRecord.user_id == actor_id,
                    (OrganizationMembershipRecord.role == "org_admin")
                    | ProjectMembershipRecord.user_id.is_not(None),
                )
                .where(
                    True if scoped_organization_id is None
                    else ProjectRecord.organization_id == scoped_organization_id
                )
                .order_by(ProjectRecord.name)
            ).all()
            return [
                self._project_dict(project, role or ("org_admin" if org_role == "org_admin" else None))
                for project, org_role, role in rows
            ]

    def get_project(self, actor_id, project_id):
        with self.session_factory() as session:
            row = session.execute(
                select(ProjectRecord, OrganizationMembershipRecord.role, ProjectMembershipRecord.role)
                .join(
                    OrganizationMembershipRecord,
                    OrganizationMembershipRecord.organization_id == ProjectRecord.organization_id,
                )
                .outerjoin(
                    ProjectMembershipRecord,
                    (ProjectMembershipRecord.project_id == ProjectRecord.id)
                    & (ProjectMembershipRecord.user_id == actor_id),
                )
                .where(
                    ProjectRecord.id == project_id,
                    OrganizationMembershipRecord.user_id == actor_id,
                    (OrganizationMembershipRecord.role == "org_admin")
                    | ProjectMembershipRecord.user_id.is_not(None),
                )
            ).first()
            if row is None:
                raise MissingResource
            project, org_role, project_role = row
            return self._project_dict(project, project_role or ("org_admin" if org_role == "org_admin" else None))

    def organization_id_for_project(self, project_id):
        with self.session_factory() as session:
            return session.scalar(
                select(ProjectRecord.organization_id).where(ProjectRecord.id == project_id)
            )

    def update_project(self, actor_id, project_id, *, name, now):
        with self.session_factory.begin() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise MissingResource
            if not self._can_manage(session, actor_id, project):
                raise ForbiddenScope
            if name is not None:
                project.name = name
            return self._project_dict(project, self._role_for(session, actor_id, project))

    def list_organization_members(self, actor_id, organization_id, *, limit=30, cursor=None):
        cursor_key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            membership = session.get(OrganizationMembershipRecord, (organization_id, actor_id))
            if membership is None:
                raise MissingResource
            statement = (
                select(UserRecord, OrganizationMembershipRecord.role, OrganizationMembershipRecord.created_at)
                .join(
                    OrganizationMembershipRecord,
                    OrganizationMembershipRecord.user_id == UserRecord.id,
                )
                .where(OrganizationMembershipRecord.organization_id == organization_id)
            )
            if cursor_key:
                last_name, last_id = cursor_key
                statement = statement.where(
                    (UserRecord.display_name > last_name)
                    | ((UserRecord.display_name == last_name) & (UserRecord.id > last_id))
                )
            rows = session.execute(
                statement.order_by(UserRecord.display_name, UserRecord.id).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            next_cursor = self._encode_cursor(items[-1][0]) if has_more and items else None
            return {
                "items": [
                    {
                        "user_id": user.id,
                        "organization_id": organization_id,
                        "email": user.email,
                        "display_name": user.display_name,
                        "role": role,
                        "joined_at": joined_at,
                    }
                    for user, role, joined_at in items
                ],
                "next_cursor": next_cursor,
            }

    def list_project_members(self, actor_id, project_id, *, limit=30, cursor=None):
        cursor_key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            project = self._get_visible_project(session, actor_id, project_id)
            statement = (
                select(UserRecord, ProjectMembershipRecord.role)
                .join(ProjectMembershipRecord, ProjectMembershipRecord.user_id == UserRecord.id)
                .where(ProjectMembershipRecord.project_id == project.id)
            )
            if cursor_key:
                last_name, last_id = cursor_key
                statement = statement.where(
                    (UserRecord.display_name > last_name)
                    | ((UserRecord.display_name == last_name) & (UserRecord.id > last_id))
                )
            rows = session.execute(
                statement.order_by(UserRecord.display_name, UserRecord.id).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            next_cursor = self._encode_cursor(items[-1][0]) if has_more and items else None
            return {
                "items": [
                    {
                        "user_id": user.id,
                        "project_id": project.id,
                        "email": user.email,
                        "display_name": user.display_name,
                        "role": role,
                    }
                    for user, role in items
                ],
                "next_cursor": next_cursor,
            }

    def set_project_member(self, actor_id, project_id, user_id, *, role, now):
        with self.session_factory.begin() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise MissingResource
            if not self._can_manage(session, actor_id, project):
                raise ForbiddenScope
            org_member = session.get(OrganizationMembershipRecord, (project.organization_id, user_id))
            if org_member is None:
                raise ForbiddenScope
            membership = session.get(ProjectMembershipRecord, (project_id, user_id))
            if membership is None:
                membership = ProjectMembershipRecord(
                    project_id=project_id, user_id=user_id, role=role, created_at=now
                )
                session.add(membership)
            else:
                membership.role = role
            user = session.get(UserRecord, user_id)
            if user is None:
                raise MissingResource
            return {
                "user_id": user.id,
                "project_id": project_id,
                "email": user.email,
                "display_name": user.display_name,
                "role": role,
            }

    def remove_project_member(self, actor_id, project_id, user_id):
        with self.session_factory.begin() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise MissingResource
            if not self._can_manage(session, actor_id, project):
                raise ForbiddenScope
            membership = session.get(ProjectMembershipRecord, (project_id, user_id))
            if membership is None:
                raise MissingResource
            session.delete(membership)

    @staticmethod
    def _get_visible_project(session, actor_id, project_id):
        row = session.execute(
            select(ProjectRecord, OrganizationMembershipRecord.role, ProjectMembershipRecord.role)
            .join(
                OrganizationMembershipRecord,
                OrganizationMembershipRecord.organization_id == ProjectRecord.organization_id,
            )
            .outerjoin(
                ProjectMembershipRecord,
                (ProjectMembershipRecord.project_id == ProjectRecord.id)
                & (ProjectMembershipRecord.user_id == actor_id),
            )
            .where(
                ProjectRecord.id == project_id,
                OrganizationMembershipRecord.user_id == actor_id,
                (OrganizationMembershipRecord.role == "org_admin")
                | ProjectMembershipRecord.user_id.is_not(None),
            )
        ).first()
        if row is None:
            raise MissingResource
        return row[0]

    @classmethod
    def _can_manage(cls, session, actor_id, project):
        org = session.get(OrganizationMembershipRecord, (project.organization_id, actor_id))
        return bool(
            org is not None
            and (
                org.role == "org_admin"
                or cls._role_for(session, actor_id, project) == "project_admin"
            )
        )

    @staticmethod
    def _role_for(session, actor_id, project):
        role = session.get(ProjectMembershipRecord, (project.id, actor_id))
        return role.role if role is not None else None

    @staticmethod
    def _org_dict(organization, role):
        return {
            "id": organization.id,
            "name": organization.name,
            "slug": organization.slug,
            "role": role,
        }

    @staticmethod
    def _project_dict(project, role):
        return {
            "id": project.id,
            "organization_id": project.organization_id,
            "name": project.name,
            "slug": project.slug,
            "created_by": project.created_by,
            "role": role,
        }

    @staticmethod
    def _encode_cursor(organization):
        label = getattr(organization, "name", None) or organization.display_name
        value = f"{label}\0{organization.id}".encode("utf-8")
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor):
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
            name, raw_id = raw.split("\0", 1)
            return name, UUID(raw_id)
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ValueError("Invalid organization cursor") from exc
