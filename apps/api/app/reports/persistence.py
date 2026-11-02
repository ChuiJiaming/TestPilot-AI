"""Persistence mapping for immutable Run report snapshots."""

from datetime import datetime, timedelta, timezone
import re
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Uuid, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.runs.persistence import TestRunRecord
from apps.api.app.runs.repository import RunRepository, RunScopeDenied
from apps.api.app.tenancy.persistence import ProjectRecord


class ReportRecord(AuthBase):
    __tablename__ = "reports"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    summary: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict,
    )
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_artifacts.id"),
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReportExportRecord(AuthBase):
    __tablename__ = "report_exports"
    __table_args__ = (
        Index(
            "uq_report_exports_requester_idempotency",
            "requester_id", "idempotency_key", unique=True,
            postgresql_where=text("requester_id IS NOT NULL AND idempotency_key IS NOT NULL"),
            sqlite_where=text("requester_id IS NOT NULL AND idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False,
    )
    format: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="QUEUED")
    artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_artifacts.id"))
    requester_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportPermissionDenied(Exception):
    """The actor can see the report's project but cannot create an export."""


class ReportIdempotencyConflict(Exception):
    """An idempotency key was reused for a different export request."""


class ReportRepository:
    """Tenant-scoped persistence for immutable reports and export requests."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self._run_scope = RunRepository(session_factory)

    def organization_id_for_run(self, run_id: UUID) -> UUID | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ProjectRecord.organization_id)
                .join(TestRunRecord, TestRunRecord.project_id == ProjectRecord.id)
                .where(TestRunRecord.id == run_id)
            )

    def organization_id_for_report(self, report_id: UUID) -> UUID | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ProjectRecord.organization_id)
                .join(TestRunRecord, TestRunRecord.project_id == ProjectRecord.id)
                .join(ReportRecord, ReportRecord.run_id == TestRunRecord.id)
                .where(ReportRecord.id == report_id)
            )

    def organization_id_for_export(self, export_id: UUID) -> UUID | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ProjectRecord.organization_id)
                .join(TestRunRecord, TestRunRecord.project_id == ProjectRecord.id)
                .join(ReportExportRecord, ReportExportRecord.run_id == TestRunRecord.id)
                .where(ReportExportRecord.id == export_id)
            )

    def get_report(
        self, actor_id: UUID, run_id: UUID, *, limit: int = 30, cursor: str | None = None,
    ) -> dict | None:
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None or not self._visible(session, actor_id, run.project_id):
                return None
            report = session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))
            if report is None:
                return None
            snapshot = report.summary if isinstance(report.summary, dict) else {}
            references = snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), list) else []
            artifact_ids = []
            for reference in references:
                if isinstance(reference, dict):
                    try:
                        artifact_ids.append(UUID(str(reference.get("id"))))
                    except (TypeError, ValueError):
                        continue
            from apps.api.app.quality.persistence import TestArtifactRecord

            current_artifacts = session.scalars(select(TestArtifactRecord).where(
                TestArtifactRecord.id.in_(artifact_ids),
            )).all() if artifact_ids else []
            current_status = {
                str(artifact.id): self._artifact_status(artifact.metadata_json)
                for artifact in current_artifacts
            }
            return self._report_view(
                report, limit=limit, cursor=cursor, artifact_status=current_status,
            )

    def get_export(self, actor_id: UUID, export_id: UUID) -> dict | None:
        with self.session_factory() as session:
            export = session.get(ReportExportRecord, export_id)
            if export is None:
                return None
            run = session.get(TestRunRecord, export.run_id)
            if run is None or not self._visible(session, actor_id, run.project_id):
                return None
            return self._export_view(export)

    def report_run_id(self, actor_id: UUID, report_id: UUID) -> UUID | None:
        with self.session_factory() as session:
            report = session.get(ReportRecord, report_id)
            if report is None:
                return None
            run = session.get(TestRunRecord, report.run_id)
            if run is None or not self._visible(session, actor_id, run.project_id):
                return None
            return run.id

    def report_id_for_run(self, actor_id: UUID, run_id: UUID) -> UUID | None:
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None or not self._visible(session, actor_id, run.project_id):
                return None
            return session.scalar(select(ReportRecord.id).where(ReportRecord.run_id == run_id))

    def create_export(
        self, actor_id: UUID, *, run_id: UUID, format: str,
        idempotency_key: str, now: datetime,
    ) -> dict | None:
        report_id = self.report_id_for_run(actor_id, run_id)
        if report_id is None:
            return None
        return self.request_export(
            actor_id, report_id, format=format, idempotency_key=idempotency_key, now=now,
        )

    def request_export(
        self, actor_id: UUID, report_id: UUID, *, format: str,
        idempotency_key: str, now: datetime,
    ) -> dict | None:
        if format not in {"html", "pdf", "json", "junit"}:
            raise ValueError("unsupported report export format")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 160:
            raise ValueError("invalid Idempotency-Key")
        try:
            with self.session_factory.begin() as session:
                report = session.get(ReportRecord, report_id)
                if report is None:
                    return None
                run = session.get(TestRunRecord, report.run_id)
                if run is None or not self._visible(session, actor_id, run.project_id):
                    return None
                try:
                    self._run_scope._visible_project(
                        session, actor_id, run.project_id, for_write=True,
                    )
                except RunScopeDenied as exc:
                    raise ReportPermissionDenied from exc
                existing = session.scalar(select(ReportExportRecord).where(
                    ReportExportRecord.requester_id == actor_id,
                    ReportExportRecord.idempotency_key == idempotency_key,
                ).with_for_update())
                if existing is not None:
                    if existing.run_id != report.run_id or existing.format != format:
                        raise ReportIdempotencyConflict
                    return self._export_view(existing)
                export = ReportExportRecord(
                    id=uuid4(), run_id=run.id, format=format, status="QUEUED",
                    requester_id=actor_id, idempotency_key=idempotency_key,
                    attempts=0, available_at=now, created_at=now,
                )
                session.add(export)
                session.flush()
                session.add(OutboxRecord(
                    aggregate_type="report_export", aggregate_id=export.id,
                    event_type="REPORT_EXPORT_REQUESTED",
                    payload={
                        "export_id": str(export.id), "report_id": str(report.id),
                        "run_id": str(run.id),
                    },
                    status="PENDING", attempts=0, available_at=now, created_at=now,
                ))
                session.flush()
                return self._export_view(export)
        except IntegrityError:
            # A concurrent request may win the partial unique idempotency index.
            with self.session_factory() as session:
                existing = session.scalar(select(ReportExportRecord).where(
                    ReportExportRecord.requester_id == actor_id,
                    ReportExportRecord.idempotency_key == idempotency_key,
                ))
                report = session.get(ReportRecord, report_id)
                if existing is None:
                    raise
                if report is None or existing.run_id != report.run_id or existing.format != format:
                    raise ReportIdempotencyConflict
                run = session.get(TestRunRecord, existing.run_id)
                if run is None or not self._visible(session, actor_id, run.project_id):
                    return None
                return self._export_view(existing)

    def claim_export(
        self, export_id: UUID, *, worker_id: str, now: datetime, lease_seconds: int = 60,
        max_attempts: int = 8,
    ) -> dict | None:
        if not worker_id or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid export lease")
        now = _aware(now)
        with self.session_factory.begin() as session:
            export = session.get(ReportExportRecord, export_id, with_for_update=True)
            if export is None or export.status in {"SUCCEEDED", "FAILED"}:
                return None
            if export.status == "QUEUED" and _aware(export.available_at) > now:
                return None
            if (export.status == "RUNNING" and export.lease_expires_at is not None
                    and _aware(export.lease_expires_at) > now):
                return None
            if export.attempts >= max_attempts:
                export.status = "FAILED"
                export.finished_at = now
                export.last_error_code = "ATTEMPTS_EXHAUSTED"
                export.lease_owner = None
                export.lease_expires_at = None
                return None
            export.status = "RUNNING"
            export.attempts += 1
            export.lease_owner = worker_id
            export.lease_expires_at = now + timedelta(seconds=lease_seconds)
            report = session.scalar(select(ReportRecord).where(ReportRecord.run_id == export.run_id))
            result = self._export_view(export)
            result.update({
                "attempts": export.attempts,
                "lease_owner": export.lease_owner,
                "lease_expires_at": export.lease_expires_at,
                "snapshot": report.summary if report is not None else None,
                "project_id": run.project_id if (run := session.get(TestRunRecord, export.run_id)) else None,
            })
            return result

    def requeue_due_exports(self, *, now: datetime, limit: int = 100) -> int:
        """Create a fresh Outbox delivery for due work or an expired worker lease."""
        if not 1 <= limit <= 500:
            raise ValueError("invalid report export recovery limit")
        now = _aware(now)
        with self.session_factory.begin() as session:
            exports = session.scalars(
                select(ReportExportRecord).where(
                    ((ReportExportRecord.status == "QUEUED")
                     & (ReportExportRecord.available_at <= now))
                    | ((ReportExportRecord.status == "RUNNING")
                       & (ReportExportRecord.lease_expires_at <= now)),
                ).order_by(ReportExportRecord.available_at, ReportExportRecord.id)
                .limit(limit).with_for_update(skip_locked=True)
            ).all()
            requeued = 0
            for export in exports:
                active_event = session.scalar(select(OutboxRecord.id).where(
                    OutboxRecord.aggregate_type == "report_export",
                    OutboxRecord.aggregate_id == export.id,
                    OutboxRecord.event_type == "REPORT_EXPORT_REQUESTED",
                    OutboxRecord.status.in_(("PENDING", "PUBLISHING")),
                ).limit(1))
                if active_event is not None:
                    continue
                report_id = session.scalar(select(ReportRecord.id).where(
                    ReportRecord.run_id == export.run_id,
                ))
                if report_id is None:
                    export.status = "FAILED"
                    export.finished_at = now
                    export.last_error_code = "REPORT_SNAPSHOT_MISSING"
                    export.lease_owner = None
                    export.lease_expires_at = None
                    continue
                session.add(OutboxRecord(
                    aggregate_type="report_export", aggregate_id=export.id,
                    event_type="REPORT_EXPORT_REQUESTED",
                    payload={
                        "export_id": str(export.id), "report_id": str(report_id),
                        "run_id": str(export.run_id),
                    },
                    status="PENDING", attempts=0, available_at=now, created_at=now,
                ))
                requeued += 1
            return requeued

    def finish_export(
        self, export_id: UUID, *, worker_id: str, artifact_id: UUID, now: datetime,
        artifact_metadata: dict | None = None,
    ) -> bool:
        now = _aware(now)
        with self.session_factory.begin() as session:
            export = session.get(ReportExportRecord, export_id, with_for_update=True)
            if not self._owns_live_lease(export, worker_id, now):
                return False
            if artifact_metadata is not None:
                from apps.api.app.quality.persistence import TestArtifactRecord
                from apps.api.app.reports.storage import (
                    MAX_REPORT_BYTES, REPORT_CONTENT_TYPES, build_report_object_key,
                )

                run = session.get(TestRunRecord, export.run_id)
                content_type = artifact_metadata.get("content_type")
                storage_key = artifact_metadata.get("storage_key")
                size_bytes = artifact_metadata.get("size_bytes")
                digest = artifact_metadata.get("sha256")
                if run is None:
                    return False
                if (content_type != REPORT_CONTENT_TYPES.get(export.format)
                        or storage_key != build_report_object_key(
                            run.project_id, run.id, export.id, export.format,
                        )
                        or type(size_bytes) is not int or not 1 <= size_bytes <= MAX_REPORT_BYTES
                        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                    raise ValueError("invalid rendered report artifact metadata")
                report = session.scalar(select(ReportRecord).where(ReportRecord.run_id == run.id))
                session.add(TestArtifactRecord(
                    id=artifact_id, project_id=run.project_id, run_id=run.id,
                    run_case_id=None, step_id=None, action_id=None,
                    environment_id=run.environment_id, case_version_id=None, browser=None,
                    artifact_type="report", content_type=content_type,
                    storage_key=storage_key, sha256=digest, size_bytes=size_bytes,
                    redacted=True, viewport=None, device_scale_factor=None,
                    color_scheme=None,
                    metadata_json={
                        "report_export_id": str(export.id),
                        "snapshot_hash": report.snapshot_hash if report is not None else None,
                    },
                    created_at=now,
                ))
                session.flush()
            export.status = "SUCCEEDED"
            export.artifact_id = artifact_id
            export.finished_at = now
            export.lease_owner = None
            export.lease_expires_at = None
            export.last_error_code = None
            return True

    def fail_export(
        self, export_id: UUID, *, worker_id: str, error_code: str, now: datetime,
        max_attempts: int = 8,
    ) -> bool:
        now = _aware(now)
        with self.session_factory.begin() as session:
            export = session.get(ReportExportRecord, export_id, with_for_update=True)
            if not self._owns_live_lease(export, worker_id, now):
                return False
            code = error_code if isinstance(error_code, str) and re.fullmatch(
                r"[A-Z][A-Z0-9_.:-]{0,79}", error_code,
            ) else "REPORT_EXPORT_FAILED"
            export.last_error_code = code
            export.lease_owner = None
            export.lease_expires_at = None
            if export.attempts >= max_attempts:
                export.status = "FAILED"
                export.finished_at = now
            else:
                export.status = "QUEUED"
                export.available_at = now + timedelta(seconds=min(300, 2 ** export.attempts))
            return True

    @staticmethod
    def _owns_live_lease(export, worker_id: str, now: datetime) -> bool:
        return bool(
            export is not None and export.status == "RUNNING"
            and export.lease_owner == worker_id
            and export.lease_expires_at is not None
            and _aware(export.lease_expires_at) > now
        )

    def _visible(self, session, actor_id: UUID, project_id: UUID) -> bool:
        try:
            self._run_scope._visible_project(session, actor_id, project_id)
        except RunScopeDenied:
            return False
        return True

    @staticmethod
    def _report_view(
        report: ReportRecord, *, limit: int, cursor: str | None,
        artifact_status: dict[str, str],
    ) -> dict:
        from base64 import urlsafe_b64decode, urlsafe_b64encode
        from copy import deepcopy

        snapshot = deepcopy(report.summary) if isinstance(report.summary, dict) else {}
        artifacts = snapshot.pop("artifacts", [])
        if not isinstance(artifacts, list):
            artifacts = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact["status"] = artifact_status.get(
                str(artifact.get("id")), "EXPIRED_ARTIFACT",
            )
        offset = 0
        if cursor is not None:
            try:
                raw_cursor = cursor + "=" * (-len(cursor) % 4)
                last_id = urlsafe_b64decode(raw_cursor.encode("ascii")).decode("ascii")
                offset = next(index + 1 for index, item in enumerate(artifacts)
                              if isinstance(item, dict) and item.get("id") == last_id)
            except (ValueError, UnicodeError, StopIteration) as exc:
                raise ValueError("invalid report artifact cursor") from exc
        page = artifacts[offset:offset + limit]
        next_cursor = None
        if offset + limit < len(artifacts) and page:
            next_cursor = urlsafe_b64encode(str(page[-1]["id"]).encode("ascii")).decode("ascii").rstrip("=")
        run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
        return {
            "id": report.id,
            "run_id": report.run_id,
            "summary": snapshot,
            "branch": run.get("branch"),
            "commit_sha": run.get("commit_sha"),
            "artifact_references": page,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _artifact_status(metadata: dict | None) -> str:
        from datetime import datetime

        metadata = metadata if isinstance(metadata, dict) else {}
        status = metadata.get("status")
        if status in ("EXPIRED", "DELETED", "EXPIRED_ARTIFACT") or metadata.get("deleted_at"):
            return "EXPIRED_ARTIFACT"
        expires_at = metadata.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                expires_at = None
        if isinstance(expires_at, datetime):
            expires = _aware(expires_at)
            if expires <= datetime.now(timezone.utc):
                return "EXPIRED_ARTIFACT"
        return "AVAILABLE"

    @staticmethod
    def _export_view(export: ReportExportRecord) -> dict:
        return {
            "id": export.id,
            "run_id": export.run_id,
            "format": export.format,
            "status": export.status,
            "artifact_id": export.artifact_id,
        }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
