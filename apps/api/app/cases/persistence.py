"""Tenant-scoped persistence for immutable test-case versions."""

from datetime import datetime
import base64
import binascii
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Uuid, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.core.audit import append_audit_entry
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


class TestCaseRecord(AuthBase):
    __tablename__ = "test_cases"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    published_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TestCaseVersionRecord(AuthBase):
    __tablename__ = "test_case_versions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    case_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_cases.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    dsl_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dsl_title: Mapped[str | None] = mapped_column(String)
    version_name: Mapped[str | None] = mapped_column(String)
    steps: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    assertions: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    changelog: Mapped[str] = mapped_column(String, nullable=False)
    review_status: Mapped[str | None] = mapped_column(String)
    review_reason: Mapped[str | None] = mapped_column(String)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseApprovalRecord(AuthBase):
    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    target_kind: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_runs.id"))
    case_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_case_versions.id"))
    requested_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    decided_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CaseScopeError(Exception):
    pass


class CasePermissionDenied(Exception):
    pass


class CaseVersionConflict(Exception):
    pass


class CaseRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_case(self, actor_id: UUID, project_id: UUID, *, title: str, priority: str, now: datetime):
        with self.session_factory.begin() as session:
            self._visible_project(session, actor_id, project_id, for_write=True)
            record = TestCaseRecord(
                id=uuid4(), project_id=project_id, title=title, priority=priority,
                status="draft", published_version_id=None, created_at=now,
            )
            session.add(record)
            session.flush()
            return self._case_dict(record)

    def list_cases(self, actor_id: UUID, project_id: UUID, *, limit: int = 30, cursor: str | None = None):
        cursor_key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            self._visible_project(session, actor_id, project_id)
            statement = select(TestCaseRecord).where(TestCaseRecord.project_id == project_id)
            if cursor_key:
                last_title, last_id = cursor_key
                statement = statement.where(
                    (TestCaseRecord.title > last_title)
                    | ((TestCaseRecord.title == last_title) & (TestCaseRecord.id > last_id))
                )
            rows = session.scalars(statement.order_by(TestCaseRecord.title, TestCaseRecord.id).limit(limit + 1)).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            return {
                "items": [self._case_dict(row) for row in items],
                "next_cursor": self._encode_cursor(items[-1]) if has_more and items else None,
            }

    def get_case(self, actor_id: UUID, case_id: UUID):
        with self.session_factory() as session:
            row = session.get(TestCaseRecord, case_id)
            if row is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, row.project_id)
            return self._case_dict(row)

    def project_id_for_case(self, case_id: UUID):
        with self.session_factory() as session:
            return session.scalar(select(TestCaseRecord.project_id).where(TestCaseRecord.id == case_id))

    def case_id_for_version(self, version_id: UUID):
        with self.session_factory() as session:
            return session.scalar(select(TestCaseVersionRecord.case_id).where(TestCaseVersionRecord.id == version_id))

    def list_versions(self, actor_id: UUID, case_id: UUID):
        with self.session_factory() as session:
            case = session.get(TestCaseRecord, case_id)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id)
            records = session.scalars(
                select(TestCaseVersionRecord).where(TestCaseVersionRecord.case_id == case_id)
                .order_by(TestCaseVersionRecord.version.desc())
            ).all()
            return {"items": [self._version_dict(row) for row in records], "next_cursor": None}

    def create_version(self, actor_id: UUID, case_id: UUID, *, dsl, changelog: str, now: datetime):
        with self.session_factory.begin() as session:
            case = session.get(TestCaseRecord, case_id, with_for_update=True)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id, for_write=True)
            last_version = session.scalar(
                select(func.max(TestCaseVersionRecord.version)).where(TestCaseVersionRecord.case_id == case_id)
            ) or 0
            record = TestCaseVersionRecord(
                id=uuid4(), case_id=case_id, version=last_version + 1,
                dsl_schema_version=dsl.schema_version, dsl_title=dsl.title,
                version_name=dsl.title,
                steps=[step.model_dump(mode="json") for step in dsl.steps],
                assertions=[item.model_dump(mode="json") for item in dsl.assertions],
                changelog=changelog, review_status="PENDING_APPROVAL", created_by=actor_id,
                created_at=now, approved_by=None, approved_at=None,
            )
            session.add(record)
            session.flush()
            self._request_case_version_approval(session, case, record, actor_id=actor_id, now=now)
            return self._version_dict(record)

    def get_version(self, actor_id: UUID, case_id: UUID, version: int):
        with self.session_factory() as session:
            case = session.get(TestCaseRecord, case_id)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id)
            row = session.scalar(select(TestCaseVersionRecord).where(
                TestCaseVersionRecord.case_id == case_id,
                TestCaseVersionRecord.version == version,
            ))
            if row is None:
                raise CaseScopeError
            return self._version_dict(row)

    def decide_version(self, actor_id: UUID, version_id: UUID, *, decision: str,
                       reason: str | None, now: datetime):
        with self.session_factory.begin() as session:
            record = session.get(TestCaseVersionRecord, version_id, with_for_update=True)
            if record is None:
                raise CaseScopeError
            case = session.get(TestCaseRecord, record.case_id)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id, for_write=True)
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.target_kind == "case_version",
                CaseApprovalRecord.target_id == version_id,
                CaseApprovalRecord.case_version_id == version_id,
                CaseApprovalRecord.project_id == case.project_id,
            ).with_for_update())
            if record.review_status != "PENDING_APPROVAL" or approval is None or approval.status != "PENDING":
                raise CaseVersionConflict("case version is not awaiting a decision")
            if record.created_by == actor_id:
                raise CasePermissionDenied("author and reviewer must differ")
            self._apply_case_version_decision(
                session, case, record, approval, actor_id,
                decision=decision, reason=reason, now=now,
            )
            return self._version_dict(record)

    def decide_approval(self, actor_id: UUID, approval_id: UUID, *,
                        decision: str, reason: str | None, now: datetime):
        with self.session_factory.begin() as session:
            target = session.get(CaseApprovalRecord, approval_id)
            if (target is None or target.target_kind != "case_version"
                    or target.case_version_id is None or target.target_id != target.case_version_id):
                raise CaseScopeError
            record = session.scalar(select(TestCaseVersionRecord).where(
                TestCaseVersionRecord.id == target.case_version_id,
            ).with_for_update())
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "case_version",
            ).with_for_update())
            if record is None or approval is None:
                raise CaseScopeError
            case = session.get(TestCaseRecord, record.case_id)
            if case is None:
                raise CaseScopeError
            if approval.project_id != case.project_id:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id, for_write=True)
            if record.review_status != "PENDING_APPROVAL" or approval.status != "PENDING":
                raise CaseVersionConflict("approval is no longer pending")
            if approval.requested_by == actor_id:
                raise CasePermissionDenied("requester and decider must differ")
            self._apply_case_version_decision(
                session, case, record, approval, actor_id,
                decision=decision, reason=reason, now=now,
            )
            return self._approval_dict(approval)

    @staticmethod
    def _request_case_version_approval(session, case, record, *, actor_id, now):
        approval = CaseApprovalRecord(
            id=uuid4(), project_id=case.project_id,
            target_kind="case_version", target_id=record.id,
            run_id=None, case_version_id=record.id,
            requested_by=actor_id, decided_by=None,
            kind="CASE_VERSION", status="PENDING", reason=None,
            decided_at=None, expires_at=None,
            payload={"version": record.version}, created_at=now,
        )
        session.add(approval)
        session.flush()
        project = session.get(ProjectRecord, case.project_id)
        if project is None:
            raise CaseScopeError
        append_audit_entry(
            session, organization_id=project.organization_id,
            actor_user_id=actor_id, action="approval.requested",
            resource_type="case_version", resource_id=record.id,
            metadata={"approval_id": str(approval.id), "target_kind": "case_version"},
            created_at=now,
        )
        return approval

    @staticmethod
    def _apply_case_version_decision(session, case, record, approval, actor_id,
                                     *, decision, reason: str | None, now):
        if decision not in {"APPROVED", "REJECTED"}:
            raise CaseVersionConflict("unsupported approval decision")
        status = "APPROVED" if decision == "APPROVED" else "REJECTED"
        approval.status = status
        approval.decided_by = actor_id
        approval.reason = reason
        approval.decided_at = now
        record.review_status = status
        record.review_reason = reason
        if status == "APPROVED":
            record.approved_by = actor_id
            record.approved_at = now
        project = session.get(ProjectRecord, case.project_id)
        if project is None:
            raise CaseScopeError
        append_audit_entry(
            session, organization_id=project.organization_id,
            actor_user_id=actor_id, action="approval.decided",
            resource_type="case_version", resource_id=record.id,
            metadata={"approval_id": str(approval.id), "decision": status,
                      "target_kind": "case_version"},
            created_at=now,
        )
        session.flush()

    @staticmethod
    def _approval_dict(row):
        return {
            "id": row.id, "project_id": row.project_id,
            "target_kind": row.target_kind, "target_id": row.target_id,
            "status": row.status, "requested_by": row.requested_by,
            "kind": row.kind, "expires_at": row.expires_at,
        }

    def publish_version(self, actor_id: UUID, version_id: UUID, *, name: str | None, now: datetime):
        with self.session_factory.begin() as session:
            version = session.get(TestCaseVersionRecord, version_id, with_for_update=True)
            if version is None:
                raise CaseScopeError
            case = session.get(TestCaseRecord, version.case_id, with_for_update=True)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id, for_write=True)
            if version.review_status != "APPROVED":
                raise CaseVersionConflict("only an approved version can be published")
            if name is not None:
                version.version_name = name
            elif version.version_name is None:
                version.version_name = version.dsl_title
            if case.published_version_id and case.published_version_id != version.id:
                previous = session.get(TestCaseVersionRecord, case.published_version_id, with_for_update=True)
                if previous is not None and previous.review_status == "APPROVED":
                    previous.review_status = "SUPERSEDED"
            case.published_version_id = version.id
            case.status = "active"
            session.flush()
            return self._version_dict(version)

    def rollback_version(self, actor_id: UUID, case_id: UUID, *, target_version: int, reason: str, now: datetime):
        with self.session_factory.begin() as session:
            case = session.get(TestCaseRecord, case_id, with_for_update=True)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id, for_write=True)
            source = session.scalar(select(TestCaseVersionRecord).where(
                TestCaseVersionRecord.case_id == case_id,
                TestCaseVersionRecord.version == target_version,
            ))
            if source is None:
                raise CaseScopeError
            if source.dsl_schema_version != 2 or source.dsl_title is None:
                raise CaseVersionConflict("legacy DSL version cannot be copied until its schema is migrated")
            latest = session.scalar(select(func.max(TestCaseVersionRecord.version)).where(
                TestCaseVersionRecord.case_id == case_id
            )) or 0
            restored = TestCaseVersionRecord(
                id=uuid4(), case_id=case_id, version=latest + 1,
                dsl_schema_version=source.dsl_schema_version, dsl_title=source.dsl_title,
                version_name=source.version_name or source.dsl_title,
                steps=source.steps, assertions=source.assertions,
                changelog=f"Rollback from v{source.version}: {reason}",
                review_status="PENDING_APPROVAL", review_reason=None, created_by=actor_id,
                created_at=now, approved_by=None, approved_at=None,
            )
            session.add(restored)
            session.flush()
            self._request_case_version_approval(
                session, case, restored, actor_id=actor_id, now=now)
            return self._version_dict(restored)

    def archive_case(self, actor_id: UUID, case_id: UUID):
        with self.session_factory.begin() as session:
            case = session.get(TestCaseRecord, case_id, with_for_update=True)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id, for_write=True)
            case.status = "archived"
            session.flush()
            return self._case_dict(case)

    def diff_by_ids(self, actor_id: UUID, case_id: UUID, version_id: UUID, against_version_id: UUID):
        target = self._get_version_by_id(actor_id, case_id, version_id)
        base = self._get_version_by_id(actor_id, case_id, against_version_id)
        if base["dsl"].get("title") is None or target["dsl"].get("title") is None:
            raise CaseVersionConflict("legacy DSL title is unknown and cannot be reconstructed")
        fields = ("title", "steps", "assertions")
        return {
            "case_id": case_id, "base": base["dsl"], "target": target["dsl"],
            "changed_fields": [field for field in fields if base["dsl"].get(field) != target["dsl"].get(field)],
        }

    def _get_version_by_id(self, actor_id: UUID, case_id: UUID, version_id: UUID):
        with self.session_factory() as session:
            case = session.get(TestCaseRecord, case_id)
            if case is None:
                raise CaseScopeError
            self._visible_project(session, actor_id, case.project_id)
            record = session.scalar(select(TestCaseVersionRecord).where(
                TestCaseVersionRecord.id == version_id, TestCaseVersionRecord.case_id == case_id,
            ))
            if record is None:
                raise CaseScopeError
            return self._version_dict(record)

    def diff_versions(self, actor_id: UUID, case_id: UUID, version_a: int, version_b: int):
        before = self.get_version(actor_id, case_id, version_a)
        after = self.get_version(actor_id, case_id, version_b)
        return {
            "case_id": case_id,
            "from_version": version_a,
            "to_version": version_b,
            "before": before["dsl"],
            "after": after["dsl"],
        }

    @staticmethod
    def _visible_project(session, actor_id, project_id, *, for_write=False):
        row = session.execute(
            select(ProjectRecord, OrganizationMembershipRecord.role, ProjectMembershipRecord.role)
            .join(OrganizationMembershipRecord,
                  OrganizationMembershipRecord.organization_id == ProjectRecord.organization_id)
            .outerjoin(ProjectMembershipRecord,
                       (ProjectMembershipRecord.project_id == ProjectRecord.id)
                       & (ProjectMembershipRecord.user_id == actor_id))
            .where(
                ProjectRecord.id == project_id,
                OrganizationMembershipRecord.user_id == actor_id,
                (OrganizationMembershipRecord.role == "org_admin")
                | ProjectMembershipRecord.user_id.is_not(None),
            )
        ).first()
        if row is None:
            raise CaseScopeError
        project, org_role, project_role = row
        if for_write and org_role != "org_admin" and project_role not in {"project_admin", "tester"}:
            raise CasePermissionDenied
        return project

    @staticmethod
    def _case_dict(row):
        return {
            "id": row.id, "project_id": row.project_id, "title": row.title,
            "priority": row.priority, "status": row.status,
            "published_version_id": row.published_version_id, "created_at": row.created_at,
        }

    @staticmethod
    def _version_dict(row):
        return {
            "id": row.id, "case_id": row.case_id, "version": row.version,
            "version_name": row.version_name,
            "review_status": row.review_status,
            "review_reason": row.review_reason,
            "dsl": {
                "schema_version": row.dsl_schema_version,
                "case_id": row.case_id, "title": row.dsl_title,
                "steps": row.steps, "assertions": row.assertions,
            },
            "changelog": row.changelog, "created_by": row.created_by,
            "created_at": row.created_at, "approved_by": row.approved_by,
            "approved_at": row.approved_at,
        }

    @staticmethod
    def _encode_cursor(record):
        value = f"{record.title}\0{record.id}".encode("utf-8")
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor):
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True).decode("utf-8")
            title, raw_id = raw.split("\0", 1)
            return title, UUID(raw_id)
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ValueError("Invalid case cursor") from exc
