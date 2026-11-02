"""Tenant-scoped persistence for private Run evidence and quality records."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Integer, JSON, Numeric, String, Uuid,
    and_, func, or_, select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.cases.persistence import (
    CasePermissionDenied,
    CaseRepository,
    CaseScopeError,
    TestCaseVersionRecord,
)
from apps.api.app.core.audit import append_audit_entry
from apps.api.app.quality.artifacts import (
    ArtifactStorageError,
    MAX_ARTIFACT_BYTES,
    S3ArtifactStorage,
    build_artifact_object_key,
    validate_png_bytes,
)
from apps.api.app.quality.analyzer import (
    FailureAnalysis,
    fingerprint_failure,
    insufficient_analysis,
)
from apps.api.app.quality.visual import (
    VisualComparisonError,
    compare_pngs,
    normalize_mask_regions,
)
from apps.api.app.runs.persistence import (
    HealingRecord,
    RunCaseRecord,
    TestRunRecord,
    TestStepRecord,
)
from apps.api.app.tenancy.persistence import (
    OrganizationRecord,
    ProjectRecord,
)
from apps.api.app.environments.persistence import EnvironmentRecord


class TestArtifactRecord(AuthBase):
    __tablename__ = "test_artifacts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False)
    run_case_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("run_cases.id", ondelete="CASCADE"))
    step_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_steps.id", ondelete="SET NULL"))
    action_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    environment_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    case_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    browser: Mapped[str | None] = mapped_column(String)
    artifact_type: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    sha256: Mapped[str | None] = mapped_column(String)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    redacted: Mapped[bool] = mapped_column(nullable=False, default=False)
    viewport: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    device_scale_factor: Mapped[float | None] = mapped_column(Numeric(5, 2))
    color_scheme: Mapped[str | None] = mapped_column(String)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata_json", JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DefectRecord(AuthBase):
    __tablename__ = "defects"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    analysis_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    classification: Mapped[str] = mapped_column(String, nullable=False, default="ASSERTION")
    root_cause_status: Mapped[str] = mapped_column(String, nullable=False,
                                                   default="INSUFFICIENT_EVIDENCE")
    root_cause_confirmed_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"))
    root_cause_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triaged_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DefectOccurrenceRecord(AuthBase):
    __tablename__ = "defect_occurrences"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    defect_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("defects.id", ondelete="CASCADE"), nullable=False)
    run_case_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("run_cases.id", ondelete="CASCADE"), nullable=False)
    step_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_steps.id", ondelete="SET NULL"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    classification: Mapped[str] = mapped_column(String, nullable=False, default="ASSERTION")
    evidence_status: Mapped[str] = mapped_column(String, nullable=False,
                                                 default="INSUFFICIENT_EVIDENCE")
    error_code: Mapped[str | None] = mapped_column(String)
    evidence: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)


class VisualBaselineRecord(AuthBase):
    __tablename__ = "visual_baselines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    case_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_case_versions.id", ondelete="RESTRICT"), nullable=False)
    action_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    environment_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    browser: Mapped[str] = mapped_column(String, nullable=False)
    viewport: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_artifacts.id", ondelete="RESTRICT"), nullable=False)
    device_scale_factor: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=1)
    color_scheme: Mapped[str] = mapped_column(String, nullable=False, default="light")
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    decided_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    decision_reason: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mask_regions: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)
    max_pixel_ratio: Mapped[float] = mapped_column(Numeric(7, 6), nullable=False, default=0.01)
    pixel_channel_tolerance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mask_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    comparison_profile: Mapped[str] = mapped_column(String, nullable=False, default="pixel-v1")


class VisualDiffRecord(AuthBase):
    __tablename__ = "visual_diffs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    baseline_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("visual_baselines.id", ondelete="RESTRICT"))
    run_case_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("run_cases.id", ondelete="CASCADE"), nullable=False)
    diff_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_artifacts.id", ondelete="SET NULL"))
    actual_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("test_artifacts.id", ondelete="RESTRICT"))
    pixel_ratio: Mapped[float | None] = mapped_column(Numeric(7, 6))
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_pixels: Mapped[int | None] = mapped_column(BigInteger)
    compared_pixels: Mapped[int | None] = mapped_column(BigInteger)
    max_pixel_ratio: Mapped[float | None] = mapped_column(Numeric(7, 6))
    pixel_channel_tolerance: Mapped[int | None] = mapped_column(Integer)
    mask_hash: Mapped[str | None] = mapped_column(String)
    comparison_profile: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String)
    metadata_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)


class QualityConflict(Exception):
    """A human quality decision conflicts with the current resource state."""


class _QualityM11Mixin:
    def project_id_for_defect(self, defect_id: UUID):
        with self.session_factory() as session:
            return session.scalar(select(DefectRecord.project_id).where(
                DefectRecord.id == defect_id))

    def list_defects(self, actor_id: UUID, project_id: UUID, *, limit: int,
                     cursor: str | None = None):
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            statement = select(DefectRecord).where(DefectRecord.project_id == project_id)
            if cursor:
                cursor_time, cursor_id = self._decode_cursor(cursor)
                statement = statement.where(or_(
                    DefectRecord.created_at < cursor_time,
                    and_(DefectRecord.created_at == cursor_time,
                         DefectRecord.id < cursor_id),
                ))
            rows = session.scalars(statement.order_by(
                DefectRecord.created_at.desc(), DefectRecord.id.desc()
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            return {
                "items": [self._defect_dict(row) for row in items],
                "next_cursor": self._encode_cursor(items[-1]) if has_more and items else None,
            }

    def get_defect(self, actor_id: UUID, defect_id: UUID):
        with self.session_factory() as session:
            defect = session.get(DefectRecord, defect_id)
            if defect is None:
                return None
            CaseRepository._visible_project(session, actor_id, defect.project_id)
            result = self._defect_dict(defect)
            result["occurrence_count"] = session.scalar(select(func.count()).select_from(
                DefectOccurrenceRecord).where(DefectOccurrenceRecord.defect_id == defect_id)) or 0
            return result

    def list_defect_occurrences(self, actor_id: UUID, defect_id: UUID, *, limit: int,
                                cursor: str | None = None):
        with self.session_factory() as session:
            defect = session.get(DefectRecord, defect_id)
            if defect is None:
                return None
            CaseRepository._visible_project(session, actor_id, defect.project_id)
            statement = (
                select(DefectOccurrenceRecord, RunCaseRecord, TestRunRecord, TestStepRecord)
                .join(RunCaseRecord, RunCaseRecord.id == DefectOccurrenceRecord.run_case_id)
                .join(TestRunRecord, TestRunRecord.id == RunCaseRecord.run_id)
                .outerjoin(TestStepRecord, TestStepRecord.id == DefectOccurrenceRecord.step_id)
                .where(DefectOccurrenceRecord.defect_id == defect_id)
            )
            if cursor:
                cursor_time, cursor_id = self._decode_cursor(cursor)
                statement = statement.where(or_(
                    DefectOccurrenceRecord.observed_at < cursor_time,
                    and_(DefectOccurrenceRecord.observed_at == cursor_time,
                         DefectOccurrenceRecord.id < cursor_id),
                ))
            rows = session.execute(statement.order_by(
                DefectOccurrenceRecord.observed_at.desc(),
                DefectOccurrenceRecord.id.desc(),
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            items = []
            for occurrence, run_case, run, step in page:
                items.append({
                    "id": occurrence.id,
                    "defect_id": defect_id,
                    "run_id": run.id,
                    "run_case_id": run_case.id,
                    "case_version_id": run_case.case_version_id,
                    "environment_id": run.environment_id,
                    "browser": run_case.browser_name,
                    "step_id": occurrence.step_id,
                    "action_id": step.action_id if step is not None else None,
                    "classification": occurrence.classification,
                    "evidence_status": occurrence.evidence_status,
                    "error_code": occurrence.error_code,
                    "evidence": dict(occurrence.evidence or {}),
                    "observed_at": occurrence.observed_at,
                })
            return {
                "items": items,
                "next_cursor": self._encode_cursor(page[-1][0]) if has_more and page else None,
            }

    def failure_context(self, run_case_id: UUID):
        with self.session_factory() as session:
            run_case = session.get(RunCaseRecord, run_case_id)
            if run_case is None:
                return None
            run = session.get(TestRunRecord, run_case.run_id)
            if run is None:
                return None
            step = session.scalar(select(TestStepRecord).where(
                TestStepRecord.run_case_id == run_case_id,
                TestStepRecord.status.in_(("FAILED", "ERROR")),
            ).order_by(TestStepRecord.sequence.desc(), TestStepRecord.id.desc()).limit(1))
            artifact_ids = []
            if step is not None:
                artifact_ids = session.scalars(select(TestArtifactRecord.id).where(
                    TestArtifactRecord.run_case_id == run_case_id,
                    TestArtifactRecord.step_id == step.id,
                    TestArtifactRecord.artifact_type == "screenshot",
                    TestArtifactRecord.redacted.is_(True),
                ).order_by(TestArtifactRecord.created_at.desc(), TestArtifactRecord.id.desc())
                  .limit(5)).all()
            result = dict(step.result_json or {}) if step is not None else {}
            step_id = step.id if step is not None else None
            evidence_ids = ([str(step_id)] if step_id else []) + [str(item) for item in artifact_ids]
            return {
                "project_id": run.project_id,
                "run_id": run.id,
                "run_case_id": run_case.id,
                "case_version_id": run_case.case_version_id,
                "environment_id": run.environment_id,
                "browser": run_case.browser_name,
                "step_id": step_id,
                "action_id": step.action_id if step is not None else None,
                "artifact_ids": artifact_ids,
                "evidence_ids": evidence_ids,
                "failure": {
                    "action_type": step.action_type if step is not None else None,
                    "failure_phase": result.get("failure_phase"),
                    "error_code": result.get("error_code") or result.get("screenshot_error_code")
                    or ("RUN_CASE_FAILED" if step is None else None),
                    "browser": run_case.browser_name,
                    "status": step.status if step is not None else run_case.status,
                },
            }

    def record_failure(self, *, run_case_id: UUID, context: dict,
                       analysis: FailureAnalysis, attempt_token: UUID,
                       worker_id: str, now: datetime):
        if now.tzinfo is None:
            raise ValueError("failure timestamps must be timezone-aware")
        if not isinstance(analysis, FailureAnalysis):
            raise TypeError("analysis must be validated before persistence")
        failure = context.get("failure", {})
        fingerprint = fingerprint_failure(failure)
        severity = {
            "SECURITY": "CRITICAL", "INFRA": "HIGH", "DATA": "HIGH",
            "ENVIRONMENT": "MEDIUM", "LOCATOR": "MEDIUM",
            "ASSERTION": "MEDIUM", "FLAKY": "LOW",
        }[analysis.classification]
        from apps.api.app.runs.repository import StaleRunAttempt
        with self.session_factory.begin() as session:
            active = session.execute(
                select(TestRunRecord, RunCaseRecord)
                .join(RunCaseRecord, RunCaseRecord.run_id == TestRunRecord.id)
                .where(
                    TestRunRecord.id == context["run_id"],
                    TestRunRecord.status == "RUNNING",
                    TestRunRecord.attempt_token == attempt_token,
                    TestRunRecord.lease_owner == worker_id,
                    RunCaseRecord.id == run_case_id,
                    RunCaseRecord.run_id == context["run_id"],
                    RunCaseRecord.case_version_id == context["case_version_id"],
                    RunCaseRecord.status == "RUNNING",
                    RunCaseRecord.attempt_token == attempt_token,
                    RunCaseRecord.lease_owner == worker_id,
                ).with_for_update()
            ).first()
            if active is None:
                raise StaleRunAttempt
            project = session.scalar(select(ProjectRecord).where(
                ProjectRecord.id == context["project_id"]).with_for_update())
            if project is None:
                return None
            defect = session.scalar(select(DefectRecord).where(
                DefectRecord.project_id == project.id,
                DefectRecord.fingerprint == fingerprint,
            ).with_for_update())
            summary = analysis.model_dump(mode="json")
            if defect is None:
                defect = DefectRecord(
                    id=uuid4(), project_id=project.id, fingerprint=fingerprint,
                    status="OPEN", severity=severity, title=analysis.title,
                    analysis_json=summary, classification=analysis.classification,
                    root_cause_status=(
                        "SUGGESTED" if analysis.evidence_status == "AVAILABLE"
                        else "INSUFFICIENT_EVIDENCE"
                    ),
                    created_at=now,
                )
                session.add(defect)
                session.flush()
            else:
                if defect.status == "RESOLVED":
                    defect.status = "OPEN"
                if defect.status != "WONT_FIX":
                    defect.title = analysis.title
                    defect.severity = severity
                    defect.classification = analysis.classification
                    defect.analysis_json = summary
                    if defect.root_cause_status != "CONFIRMED":
                        defect.root_cause_status = (
                            "SUGGESTED" if analysis.evidence_status == "AVAILABLE"
                            else "INSUFFICIENT_EVIDENCE"
                        )
            query = select(DefectOccurrenceRecord).where(
                DefectOccurrenceRecord.defect_id == defect.id,
                DefectOccurrenceRecord.run_case_id == run_case_id,
            )
            if context.get("step_id") is None:
                query = query.where(DefectOccurrenceRecord.step_id.is_(None))
            else:
                query = query.where(DefectOccurrenceRecord.step_id == context["step_id"])
            occurrence = session.scalar(query.with_for_update())
            if occurrence is None:
                session.add(DefectOccurrenceRecord(
                    id=uuid4(), defect_id=defect.id, run_case_id=run_case_id,
                    step_id=context.get("step_id"), observed_at=now,
                    classification=analysis.classification,
                    evidence_status=analysis.evidence_status,
                    error_code=failure.get("error_code"),
                    evidence={
                        "step_id": str(context["step_id"]) if context.get("step_id") else None,
                        "artifact_ids": [str(value) for value in context.get("artifact_ids", [])],
                    },
                ))
            return self._defect_dict(defect)

    def patch_defect(self, actor_id: UUID, defect_id: UUID, *, changes: dict,
                     reason: str, now: datetime):
        allowed = {"name", "title", "status", "severity", "classification", "root_cause_status"}
        if (not isinstance(changes, dict) or not changes or set(changes) - allowed
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
                or now.tzinfo is None):
            raise ValueError("defect triage input is invalid")
        changes = dict(changes)
        if "name" in changes:
            if "title" in changes:
                raise ValueError("provide title or name, not both")
            changes["title"] = changes.pop("name")
        allowed_status = {"OPEN", "TRIAGED", "IN_PROGRESS", "RESOLVED", "WONT_FIX"}
        allowed_severity = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        from apps.api.app.quality.analyzer import FAILURE_CLASSES
        if (changes.get("status", "TRIAGED") not in allowed_status
                or changes.get("severity", "MEDIUM") not in allowed_severity
                or changes.get("classification", "ASSERTION") not in FAILURE_CLASSES
                or changes.get("root_cause_status", "SUGGESTED") not in {
                    "INSUFFICIENT_EVIDENCE", "SUGGESTED", "CONFIRMED",
                }):
            raise ValueError("defect triage value is invalid")
        if "title" in changes:
            from apps.api.app.quality.analyzer import sanitize_summary
            title = sanitize_summary(changes["title"], maximum=160)
            if not title:
                raise ValueError("defect title is invalid")
            changes["title"] = title

        with self.session_factory.begin() as session:
            defect = session.scalar(select(DefectRecord).where(
                DefectRecord.id == defect_id).with_for_update())
            if defect is None:
                return None
            CaseRepository._visible_project(
                session, actor_id, defect.project_id, for_write=True)
            before = self._defect_dict(defect)
            target_root = changes.get("root_cause_status")
            if (target_root == "CONFIRMED"
                    and (defect.analysis_json or {}).get("evidence_status") != "AVAILABLE"):
                raise QualityConflict("INSUFFICIENT_EVIDENCE_CANNOT_BE_CONFIRMED")
            for key in ("title", "status", "severity", "classification"):
                if key in changes:
                    setattr(defect, key, changes[key])
            if target_root is not None:
                defect.root_cause_status = target_root
                if target_root == "CONFIRMED":
                    defect.root_cause_confirmed_by = actor_id
                    defect.root_cause_confirmed_at = now
                else:
                    defect.root_cause_confirmed_by = None
                    defect.root_cause_confirmed_at = None
            if defect.status == "OPEN" and "status" not in changes:
                defect.status = "TRIAGED"
            defect.triaged_by = actor_id
            defect.triaged_at = now
            after = self._defect_dict(defect)
            organization_id = session.scalar(select(ProjectRecord.organization_id).where(
                ProjectRecord.id == defect.project_id))
            append_audit_entry(
                session, organization_id=organization_id, actor_user_id=actor_id,
                action="defect.triaged", resource_type="defect", resource_id=defect.id,
                metadata={
                    "reason": self._audit_summary(reason),
                    "before": self._audit_state(before),
                    "after": self._audit_state(after),
                }, created_at=now,
            )
            return after

    @staticmethod
    def _defect_dict(record):
        return {
            "id": record.id, "project_id": record.project_id,
            "fingerprint": record.fingerprint, "title": record.title,
            "status": record.status, "severity": record.severity,
            "classification": record.classification,
            "root_cause_status": record.root_cause_status,
            "root_cause_confirmed_by": record.root_cause_confirmed_by,
            "root_cause_confirmed_at": record.root_cause_confirmed_at,
            "triaged_by": record.triaged_by, "triaged_at": record.triaged_at,
            "analysis": dict(record.analysis_json or {}), "created_at": record.created_at,
        }

    @staticmethod
    def _audit_summary(value):
        from apps.api.app.quality.analyzer import sanitize_summary
        return sanitize_summary(value, maximum=500)

    @staticmethod
    def _audit_state(value):
        return {
            key: (str(item) if isinstance(item, UUID) else item)
            for key, item in value.items()
            if key in {"status", "severity", "classification", "root_cause_status", "title"}
        }


def _mask_digest(masks):
    canonical = json.dumps(masks, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _QualityM12Mixin:
    def project_id_for_visual_baseline(self, baseline_id: UUID):
        with self.session_factory() as session:
            return session.scalar(select(VisualBaselineRecord.project_id).where(
                VisualBaselineRecord.id == baseline_id))

    def list_visual_baselines(self, actor_id: UUID, project_id: UUID, *, limit: int,
                              cursor: str | None = None):
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            statement = select(VisualBaselineRecord).where(
                VisualBaselineRecord.project_id == project_id)
            if cursor:
                cursor_time, cursor_id = self._decode_cursor(cursor)
                statement = statement.where(or_(
                    VisualBaselineRecord.created_at < cursor_time,
                    and_(VisualBaselineRecord.created_at == cursor_time,
                         VisualBaselineRecord.id < cursor_id),
                ))
            rows = session.scalars(statement.order_by(
                VisualBaselineRecord.created_at.desc(), VisualBaselineRecord.id.desc()
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "items": [self._baseline_dict(item) for item in page],
                "next_cursor": self._encode_cursor(page[-1]) if has_more and page else None,
            }

    def propose_visual_baseline(self, actor_id: UUID, *, payload: dict, now: datetime):
        if now.tzinfo is None:
            raise ValueError("baseline timestamps must be timezone-aware")
        try:
            masks = normalize_mask_regions(payload.get("mask_regions", []))
            project_id = UUID(str(payload["project_id"]))
            case_version_id = UUID(str(payload["case_version_id"]))
            action_id = UUID(str(payload["action_id"]))
            environment_id = UUID(str(payload["environment_id"]))
            artifact_id = UUID(str(payload["artifact_id"]))
            browser = payload["browser"]
            viewport = payload["viewport"]
            max_pixel_ratio = float(payload.get("max_pixel_ratio", 0.01))
            tolerance = payload.get("pixel_channel_tolerance", 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("baseline proposal is invalid") from exc
        if (browser not in {"chromium", "firefox", "webkit"}
                or not isinstance(viewport, dict)
                or type(viewport.get("width")) is not int
                or type(viewport.get("height")) is not int
                or viewport["width"] < 1 or viewport["height"] < 1
                or not 0 <= max_pixel_ratio <= 1
                or type(tolerance) is not int or not 0 <= tolerance <= 255):
            raise ValueError("baseline proposal is invalid")
        with self.session_factory.begin() as session:
            project = CaseRepository._visible_project(
                session, actor_id, project_id, for_write=True)
            artifact = session.get(TestArtifactRecord, artifact_id)
            version = session.get(TestCaseVersionRecord, case_version_id)
            environment = session.get(EnvironmentRecord, environment_id)
            if (artifact is None or artifact.project_id != project.id
                    or artifact.artifact_type != "screenshot"
                    or artifact.content_type != "image/png" or artifact.redacted is not True
                    or artifact.case_version_id != case_version_id
                    or artifact.environment_id != environment_id
                    or artifact.action_id != action_id or artifact.browser != browser
                    or dict(artifact.viewport or {}) != viewport
                    or version is None or version.project_id != project.id
                    or environment is None or environment.project_id != project.id):
                return None
            baseline = VisualBaselineRecord(
                id=uuid4(), project_id=project.id,
                case_version_id=case_version_id, action_id=action_id,
                environment_id=environment_id, browser=browser,
                viewport=dict(artifact.viewport), artifact_id=artifact.id,
                device_scale_factor=float(artifact.device_scale_factor or 1),
                color_scheme=artifact.color_scheme or "light", status="PENDING",
                created_by=actor_id, created_at=now, mask_regions=masks,
                max_pixel_ratio=max_pixel_ratio,
                pixel_channel_tolerance=tolerance,
                mask_hash=_mask_digest(masks), comparison_profile="pixel-v1",
            )
            session.add(baseline)
            session.flush()
            organization_id = session.scalar(select(ProjectRecord.organization_id).where(
                ProjectRecord.id == project.id))
            append_audit_entry(
                session, organization_id=organization_id, actor_user_id=actor_id,
                action="visual_baseline.proposed", resource_type="visual_baseline",
                resource_id=baseline.id,
                metadata={
                    "status": "PENDING", "artifact_id": str(artifact.id),
                    "mask_hash": baseline.mask_hash,
                    "comparison_profile": baseline.comparison_profile,
                    "max_pixel_ratio": max_pixel_ratio,
                    "pixel_channel_tolerance": tolerance,
                }, created_at=now,
            )
            return self._baseline_dict(baseline)

    def decide_visual_baseline(self, actor_id: UUID, baseline_id: UUID, *,
                               decision: str, reason: str, now: datetime):
        from apps.api.app.quality.analyzer import sanitize_summary
        if (decision not in {"APPROVE", "REJECT"} or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 2000 or now.tzinfo is None):
            raise ValueError("baseline decision is invalid")
        try:
            with self.session_factory.begin() as session:
                baseline = session.scalar(select(VisualBaselineRecord).where(
                    VisualBaselineRecord.id == baseline_id).with_for_update())
                if baseline is None:
                    return None
                CaseRepository._visible_project(
                    session, actor_id, baseline.project_id, for_write=True)
                if baseline.status != "PENDING":
                    raise QualityConflict("BASELINE_ALREADY_DECIDED")
                if baseline.created_by is None:
                    raise QualityConflict("BASELINE_CREATOR_UNKNOWN")
                if baseline.created_by == actor_id:
                    raise QualityConflict("BASELINE_SEPARATION_OF_DUTIES_REQUIRED")
                baseline.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
                baseline.decided_by = actor_id
                baseline.decision_reason = sanitize_summary(reason, maximum=500)
                baseline.decided_at = now
                if decision == "APPROVE":
                    baseline.approved_by = actor_id
                    baseline.approved_at = now
                organization_id = session.scalar(select(ProjectRecord.organization_id).where(
                    ProjectRecord.id == baseline.project_id))
                append_audit_entry(
                    session, organization_id=organization_id, actor_user_id=actor_id,
                    action="visual_baseline.decided", resource_type="visual_baseline",
                    resource_id=baseline.id,
                    metadata={
                        "decision": decision, "reason": baseline.decision_reason,
                        "artifact_id": str(baseline.artifact_id),
                        "mask_hash": baseline.mask_hash,
                        "comparison_profile": baseline.comparison_profile,
                    }, created_at=now,
                )
                session.flush()
                return self._baseline_dict(baseline)
        except IntegrityError as exc:
            raise QualityConflict("BASELINE_CONTEXT_ALREADY_APPROVED") from exc

    def project_id_for_visual_diff(self, diff_id: UUID):
        with self.session_factory() as session:
            return session.scalar(
                select(TestRunRecord.project_id)
                .join(RunCaseRecord, RunCaseRecord.run_id == TestRunRecord.id)
                .join(VisualDiffRecord, VisualDiffRecord.run_case_id == RunCaseRecord.id)
                .where(VisualDiffRecord.id == diff_id)
            )

    def list_visual_diffs(self, actor_id: UUID, project_id: UUID, *, limit: int,
                          cursor: str | None = None, run_case_id: UUID | None = None):
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            statement = (
                select(VisualDiffRecord)
                .join(RunCaseRecord, RunCaseRecord.id == VisualDiffRecord.run_case_id)
                .join(TestRunRecord, TestRunRecord.id == RunCaseRecord.run_id)
                .where(TestRunRecord.project_id == project_id)
            )
            if run_case_id is not None:
                statement = statement.where(VisualDiffRecord.run_case_id == run_case_id)
            if cursor:
                cursor_time, cursor_id = self._decode_cursor(cursor)
                statement = statement.where(or_(
                    VisualDiffRecord.created_at < cursor_time,
                    and_(VisualDiffRecord.created_at == cursor_time,
                         VisualDiffRecord.id < cursor_id),
                ))
            rows = session.scalars(statement.order_by(
                VisualDiffRecord.created_at.desc(), VisualDiffRecord.id.desc()
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "items": [self._visual_diff_dict(item) for item in page],
                "next_cursor": self._encode_cursor(page[-1]) if has_more and page else None,
            }

    def get_visual_diff(self, actor_id: UUID, diff_id: UUID):
        project_id = self.project_id_for_visual_diff(diff_id)
        if project_id is None:
            return None
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            record = session.get(VisualDiffRecord, diff_id)
            return self._visual_diff_dict(record) if record is not None else None

    def evaluate_visual_assertion(self, *, run_id: UUID, run_case_id: UUID,
                                  attempt_token: UUID, worker_id: str,
                                  case_version_id: UUID, environment_id: UUID,
                                  browser: str, action_id: UUID,
                                  baseline_id: UUID | None, assertion_step_id: UUID,
                                  now: datetime):
        from apps.api.app.runs.repository import StaleRunAttempt
        if now.tzinfo is None:
            raise ValueError("visual diff timestamps must be timezone-aware")
        with self.session_factory() as session:
            context = self._active_visual_context(
                session, run_id, run_case_id, attempt_token, worker_id,
                case_version_id, environment_id, browser, assertion_step_id,
            )
            if context is None:
                raise StaleRunAttempt
            actual = session.scalar(select(TestArtifactRecord).where(
                TestArtifactRecord.run_case_id == run_case_id,
                TestArtifactRecord.action_id == action_id,
                TestArtifactRecord.artifact_type == "screenshot",
                TestArtifactRecord.redacted.is_(True),
            ).order_by(TestArtifactRecord.created_at.desc(), TestArtifactRecord.id.desc()).limit(1))
            if actual is not None:
                actual = self._artifact_dict(actual)
            if baseline_id is not None:
                baseline = session.get(VisualBaselineRecord, baseline_id)
                if baseline is not None and baseline.project_id != context["project_id"]:
                    baseline = None
                candidates = [baseline] if baseline is not None else []
            else:
                candidates = session.scalars(select(VisualBaselineRecord).where(
                    VisualBaselineRecord.project_id == context["project_id"],
                    VisualBaselineRecord.case_version_id == case_version_id,
                    VisualBaselineRecord.environment_id == environment_id,
                    VisualBaselineRecord.browser == browser,
                    VisualBaselineRecord.action_id == action_id,
                    VisualBaselineRecord.status == "APPROVED",
                    VisualBaselineRecord.comparison_profile == "pixel-v1",
                )).all()
            if actual is None:
                selected_baseline = None
                status, error_code = "ERROR", "SCREENSHOT_UNAVAILABLE"
            else:
                matching = [item for item in candidates if (
                    item.status == "APPROVED"
                    and item.project_id == context["project_id"]
                    and item.case_version_id == case_version_id
                    and item.environment_id == environment_id
                    and item.browser == browser
                    and item.action_id == action_id
                    and dict(item.viewport or {}) == dict(actual["viewport"] or {})
                    and float(item.device_scale_factor or 1)
                        == float(actual["device_scale_factor"] or 1)
                    and item.color_scheme == actual["color_scheme"]
                    and item.comparison_profile == "pixel-v1"
                )]
                if baseline_id is not None:
                    selected_baseline = matching[0] if matching else None
                    if selected_baseline is None:
                        same_project_approved = (
                            baseline is not None
                            and baseline.project_id == context["project_id"]
                            and baseline.status == "APPROVED"
                        )
                        status, error_code = (
                            "NEED_BASELINE",
                            "BASELINE_CONTEXT_MISMATCH" if same_project_approved
                            else "BASELINE_NOT_APPROVED",
                        )
                    else:
                        status, error_code = None, None
                elif len(matching) == 1:
                    selected_baseline = matching[0]
                    status, error_code = None, None
                else:
                    selected_baseline = None
                    status, error_code = (
                        "NEED_BASELINE",
                        "BASELINE_AMBIGUOUS" if matching else "BASELINE_NOT_FOUND",
                    )
            context = {
                **context, "action_id": action_id,
                "viewport": dict(actual["viewport"] or {}) if actual is not None else None,
                "device_scale_factor": actual["device_scale_factor"] if actual is not None else None,
                "color_scheme": actual["color_scheme"] if actual is not None else None,
            }
        if actual is None:
            return self._record_visual_outcome(
                context, actual=None, baseline=None, status=status, error_code=error_code,
                now=now,
            )
        if selected_baseline is None:
            return self._record_visual_outcome(
                context, actual=actual, baseline=None, status=status, error_code=error_code,
                now=now,
            )
        if selected_baseline.mask_hash != _mask_digest(selected_baseline.mask_regions or []):
            return self._record_visual_outcome(
                context, actual=actual, baseline=selected_baseline,
                status="ERROR", error_code="BASELINE_CONFIGURATION_INVALID", now=now,
            )
        if self.storage is None:
            return self._record_visual_outcome(
                context, actual=actual, baseline=selected_baseline,
                status="ERROR", error_code="ARTIFACT_STORAGE_UNAVAILABLE", now=now,
            )
        try:
            expected_bytes = self._load_artifact_bytes(selected_baseline.artifact_id)
            actual_bytes = self._load_artifact_bytes(actual["id"])
            comparison = compare_pngs(
                expected_bytes, actual_bytes,
                masks=list(selected_baseline.mask_regions or []),
                channel_tolerance=selected_baseline.pixel_channel_tolerance,
            )
        except VisualComparisonError as exc:
            return self._record_visual_outcome(
                context, actual=actual, baseline=selected_baseline,
                status="ERROR", error_code=str(exc), now=now,
            )
        except ArtifactStorageError:
            return self._record_visual_outcome(
                context, actual=actual, baseline=selected_baseline,
                status="ERROR", error_code="ARTIFACT_STORAGE_UNAVAILABLE", now=now,
            )
        status = "PASS" if comparison.pixel_ratio <= float(selected_baseline.max_pixel_ratio) else "FAIL"
        return self._record_visual_outcome(
            context, actual=actual, baseline=selected_baseline, status=status,
            error_code=None, comparison=comparison, now=now,
        )

    def _load_artifact_bytes(self, artifact_id):
        with self.session_factory() as session:
            record = session.get(TestArtifactRecord, artifact_id)
            if record is None or record.content_type != "image/png":
                record = None
            else:
                key, expected_hash = record.storage_key, record.sha256
        if record is None:
            raise ArtifactStorageError("Artifact metadata is missing")
        data = self.storage.get(key)
        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ArtifactStorageError("Stored artifact hash does not match its metadata")
        return data

    def _record_visual_outcome(self, context, *, actual, baseline, status,
                               error_code, now, comparison=None):
        from apps.api.app.runs.repository import StaleRunAttempt
        diff_id = uuid4()
        artifact_id = uuid4() if comparison is not None else None
        object_key = None
        digest = None
        if comparison is not None:
            if self.storage is None:
                status, error_code, comparison, artifact_id = (
                    "ERROR", "ARTIFACT_STORAGE_UNAVAILABLE", None, None)
            else:
                object_key = build_artifact_object_key(
                    context["project_id"], context["run_id"], artifact_id)
                try:
                    digest = self.storage.put(
                        object_key, comparison.diff_png, content_type="image/png")
                except ArtifactStorageError:
                    status, error_code, comparison, artifact_id = (
                        "ERROR", "ARTIFACT_STORAGE_UNAVAILABLE", None, None)
                    object_key = None
        try:
            with self.session_factory.begin() as session:
                active = self._active_visual_context(
                    session, context["run_id"], context["run_case_id"],
                    context["attempt_token"], context["worker_id"],
                    context["case_version_id"], context["environment_id"],
                    context["browser"], context["assertion_step_id"], lock_rows=True,
                )
                if active is None:
                    raise StaleRunAttempt
                if object_key is not None:
                    diff_artifact = TestArtifactRecord(
                        id=artifact_id, project_id=context["project_id"],
                        run_id=context["run_id"], run_case_id=context["run_case_id"],
                        step_id=context["assertion_step_id"], action_id=context["action_id"],
                        environment_id=context["environment_id"],
                        case_version_id=context["case_version_id"],
                        browser=context["browser"], artifact_type="diff",
                        content_type="image/png", storage_key=object_key,
                        sha256=digest, size_bytes=len(comparison.diff_png), redacted=True,
                        viewport=context["viewport"],
                        device_scale_factor=context["device_scale_factor"],
                        color_scheme=context["color_scheme"],
                        metadata_json={"comparison_profile": baseline.comparison_profile},
                        created_at=now,
                    )
                    session.add(diff_artifact)
                diff = VisualDiffRecord(
                    id=diff_id,
                    baseline_id=baseline.id if baseline is not None else None,
                    run_case_id=context["run_case_id"],
                    diff_artifact_id=artifact_id,
                    actual_artifact_id=actual["id"] if actual is not None else None,
                    pixel_ratio=comparison.pixel_ratio if comparison is not None else None,
                    status=status, created_at=now,
                    changed_pixels=comparison.changed_pixels if comparison is not None else None,
                    compared_pixels=comparison.compared_pixels if comparison is not None else None,
                    max_pixel_ratio=float(baseline.max_pixel_ratio) if baseline is not None else None,
                    pixel_channel_tolerance=baseline.pixel_channel_tolerance if baseline is not None else None,
                    mask_hash=baseline.mask_hash if baseline is not None else None,
                    comparison_profile=baseline.comparison_profile if baseline is not None else None,
                    error_code=error_code,
                    metadata_json={"action_id": str(context["action_id"])},
                )
                session.add(diff)
                session.flush()
        except Exception:
            if object_key is not None and self.storage is not None:
                try:
                    self.storage.delete(object_key)
                except Exception:
                    pass
            raise
        return {
            "status": status, "visual_status": status,
            "pixel_ratio": comparison.pixel_ratio if comparison is not None else None,
            "changed_pixels": comparison.changed_pixels if comparison is not None else None,
            "compared_pixels": comparison.compared_pixels if comparison is not None else None,
            "actual_artifact_id": actual["id"] if actual is not None else None,
            "baseline_id": baseline.id if baseline is not None else None,
            "diff_id": diff_id, "error_code": error_code,
        }

    @staticmethod
    def _active_visual_context(session, run_id, run_case_id, attempt_token,
                               worker_id, case_version_id, environment_id,
                               browser, assertion_step_id, *, lock_rows=False):
        statement = (
            select(TestRunRecord, RunCaseRecord, TestStepRecord)
            .join(RunCaseRecord, RunCaseRecord.run_id == TestRunRecord.id)
            .join(TestStepRecord, TestStepRecord.run_case_id == RunCaseRecord.id)
            .where(
                TestRunRecord.id == run_id,
                TestRunRecord.status == "RUNNING",
                TestRunRecord.attempt_token == attempt_token,
                TestRunRecord.lease_owner == worker_id,
                RunCaseRecord.id == run_case_id,
                RunCaseRecord.case_version_id == case_version_id,
                RunCaseRecord.browser_name == browser,
                RunCaseRecord.status == "RUNNING",
                RunCaseRecord.attempt_token == attempt_token,
                TestStepRecord.id == assertion_step_id,
                TestStepRecord.status == "RUNNING",
                TestStepRecord.attempt_token == attempt_token,
            )
        )
        if lock_rows:
            statement = statement.with_for_update()
        row = session.execute(statement).first()
        if row is None:
            return None
        run, run_case, step = row
        if (run.environment_id != environment_id
                or not step.action_type.startswith("assert:visual")):
            return None
        return {
            "project_id": run.project_id, "run_id": run.id,
            "run_case_id": run_case.id, "case_version_id": run_case.case_version_id,
            "environment_id": run.environment_id, "browser": run_case.browser_name,
            "attempt_token": run.attempt_token, "worker_id": run.lease_owner,
            "assertion_step_id": step.id,
        }

    @staticmethod
    def _baseline_dict(record):
        return {
            "id": record.id, "project_id": record.project_id,
            "case_version_id": record.case_version_id, "action_id": record.action_id,
            "environment_id": record.environment_id, "browser": record.browser,
            "viewport": dict(record.viewport or {}), "artifact_id": record.artifact_id,
            "device_scale_factor": float(record.device_scale_factor or 1),
            "color_scheme": record.color_scheme, "status": record.status,
            "approved_by": record.approved_by, "approved_at": record.approved_at,
            "created_by": record.created_by, "decided_by": record.decided_by,
            "decision_reason": record.decision_reason, "decided_at": record.decided_at,
            "mask_regions": list(record.mask_regions or []),
            "max_pixel_ratio": float(record.max_pixel_ratio),
            "pixel_channel_tolerance": record.pixel_channel_tolerance,
            "mask_hash": record.mask_hash,
            "comparison_profile": record.comparison_profile,
            "created_at": record.created_at,
        }

    @staticmethod
    def _visual_diff_dict(record):
        return {
            "id": record.id, "baseline_id": record.baseline_id,
            "run_case_id": record.run_case_id,
            "actual_artifact_id": record.actual_artifact_id,
            "diff_artifact_id": record.diff_artifact_id,
            "pixel_ratio": float(record.pixel_ratio) if record.pixel_ratio is not None else None,
            "changed_pixels": record.changed_pixels,
            "compared_pixels": record.compared_pixels,
            "max_pixel_ratio": float(record.max_pixel_ratio)
            if record.max_pixel_ratio is not None else None,
            "pixel_channel_tolerance": record.pixel_channel_tolerance,
            "mask_hash": record.mask_hash,
            "comparison_profile": record.comparison_profile,
            "status": record.status, "error_code": record.error_code,
            "created_at": record.created_at,
        }


class QualityRepository(_QualityM11Mixin, _QualityM12Mixin):
    def __init__(self, session_factory, storage: S3ArtifactStorage | None = None,
                 report_storage=None):
        self.session_factory = session_factory
        self.storage = storage
        self.report_storage = report_storage

    def organization_id_for_project(self, project_id: UUID):
        with self.session_factory() as session:
            return session.scalar(
                select(ProjectRecord.organization_id).where(ProjectRecord.id == project_id)
            )

    def project_id_for_artifact(self, artifact_id: UUID):
        with self.session_factory() as session:
            return session.scalar(
                select(TestArtifactRecord.project_id).where(TestArtifactRecord.id == artifact_id)
            )

    def list_artifacts(self, actor_id: UUID, project_id: UUID, *, limit: int,
                       cursor: str | None = None, artifact_type: str | None = None):
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            statement = select(TestArtifactRecord).where(
                TestArtifactRecord.project_id == project_id
            )
            if artifact_type is not None:
                statement = statement.where(TestArtifactRecord.artifact_type == artifact_type)
            if cursor:
                cursor_time, cursor_id = self._decode_cursor(cursor)
                statement = statement.where(or_(
                    TestArtifactRecord.created_at < cursor_time,
                    and_(TestArtifactRecord.created_at == cursor_time,
                         TestArtifactRecord.id < cursor_id),
                ))
            records = session.scalars(
                statement.order_by(TestArtifactRecord.created_at.desc(),
                                   TestArtifactRecord.id.desc()).limit(limit + 1)
            ).all()
            has_more = len(records) > limit
            items = records[:limit]
            next_cursor = self._encode_cursor(items[-1]) if has_more and items else None
            return {"items": [self._artifact_dict(item) for item in items],
                    "next_cursor": next_cursor}

    def download_artifact(self, actor_id: UUID, artifact_id: UUID):
        with self.session_factory() as session:
            record = session.get(TestArtifactRecord, artifact_id)
            if record is None:
                return None
            CaseRepository._visible_project(session, actor_id, record.project_id)
            key, expected_hash = record.storage_key, record.sha256
            artifact = self._artifact_dict(record)
            report_artifact = record.artifact_type == "report"
        storage = self.report_storage if report_artifact else self.storage
        if storage is None:
            raise ArtifactStorageError("Object storage is not configured")
        if report_artifact:
            data = storage.get(key)
        else:
            data = storage.get(key)
        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ArtifactStorageError("Stored artifact hash does not match its metadata")
        return artifact, data

    def save_screenshot(self, *, run_id: UUID, run_case_id: UUID, step_id: UUID,
                        attempt_token: UUID, worker_id: str, image: bytes,
                        viewport: dict, device_scale_factor: float,
                        color_scheme: str, now: datetime):
        if self.storage is None:
            raise ArtifactStorageError("Object storage is not configured")
        if now.tzinfo is None:
            raise ValueError("artifact timestamps must be timezone-aware")
        width, height = validate_png_bytes(image, max_bytes=MAX_ARTIFACT_BYTES)
        if (not isinstance(viewport, dict)
                or viewport.get("width") != width or viewport.get("height") != height
                or type(device_scale_factor) not in {int, float}
                or not 0.5 <= float(device_scale_factor) <= 4
                or color_scheme not in {"light", "dark"}):
            raise ArtifactStorageError("Screenshot display context is invalid")

        artifact_id = uuid4()
        try:
            with self.session_factory() as session:
                context = self._active_capture_context(
                    session, run_id, run_case_id, step_id, attempt_token, worker_id)
            if context is None:
                from apps.api.app.runs.repository import StaleRunAttempt
                raise StaleRunAttempt
            object_key = build_artifact_object_key(
                context["project_id"], run_id, artifact_id)
            digest = self.storage.put(object_key, image, content_type="image/png")
            with self.session_factory.begin() as session:
                context = self._active_capture_context(
                    session, run_id, run_case_id, step_id, attempt_token, worker_id,
                    lock_rows=True,
                )
                if context is None:
                    from apps.api.app.runs.repository import StaleRunAttempt
                    raise StaleRunAttempt
                record = TestArtifactRecord(
                    id=artifact_id,
                    project_id=context["project_id"],
                    run_id=run_id,
                    run_case_id=run_case_id,
                    step_id=step_id,
                    action_id=context["action_id"],
                    environment_id=context["environment_id"],
                    case_version_id=context["case_version_id"],
                    browser=context["browser"],
                    artifact_type="screenshot",
                    content_type="image/png",
                    storage_key=object_key,
                    sha256=digest,
                    size_bytes=len(image),
                    redacted=True,
                    viewport={"width": width, "height": height},
                    device_scale_factor=float(device_scale_factor),
                    color_scheme=color_scheme,
                    metadata_json={"capture_profile": "playwright-masked-v1"},
                    created_at=now,
                )
                session.add(record)
                session.flush()
                result = self._artifact_dict(record)
        except Exception:
            try:
                if "object_key" in locals():
                    self.storage.delete(object_key)
            except Exception as cleanup_error:
                import logging
                logging.getLogger(__name__).warning(
                    "Could not remove orphaned artifact object after metadata write failed: %s",
                    type(cleanup_error).__name__,
                )
            raise
        return result

    @staticmethod
    def _active_capture_context(session, run_id, run_case_id, step_id,
                               attempt_token, worker_id, *, lock_rows=False):
        statement = (
            select(TestRunRecord, RunCaseRecord, TestStepRecord)
            .join(RunCaseRecord, RunCaseRecord.run_id == TestRunRecord.id)
            .join(TestStepRecord, TestStepRecord.run_case_id == RunCaseRecord.id)
            .where(
                TestRunRecord.id == run_id,
                TestRunRecord.status == "RUNNING",
                TestRunRecord.lease_owner == worker_id,
                TestRunRecord.attempt_token == attempt_token,
                RunCaseRecord.id == run_case_id,
                RunCaseRecord.status == "RUNNING",
                RunCaseRecord.lease_owner == worker_id,
                RunCaseRecord.attempt_token == attempt_token,
                TestStepRecord.id == step_id,
                TestStepRecord.status == "RUNNING",
                TestStepRecord.attempt_token == attempt_token,
            )
        )
        if lock_rows:
            statement = statement.with_for_update()
        row = session.execute(statement).first()
        if row is None:
            return None
        run, run_case, step = row
        action_id = step.action_id
        if step.recovery_healing_id is not None:
            healing = session.get(HealingRecord, step.recovery_healing_id)
            if healing is not None:
                source_step = session.get(TestStepRecord, healing.step_id)
                if source_step is not None:
                    action_id = source_step.action_id
        return {
            "project_id": run.project_id,
            "environment_id": run.environment_id,
            "case_version_id": run_case.case_version_id,
            "browser": run_case.browser_name,
            "action_id": action_id,
        }

    @staticmethod
    def _artifact_dict(record: TestArtifactRecord):
        return {
            "id": record.id,
            "project_id": record.project_id,
            "run_id": record.run_id,
            "run_case_id": record.run_case_id,
            "step_id": record.step_id,
            "action_id": record.action_id,
            "environment_id": record.environment_id,
            "case_version_id": record.case_version_id,
            "browser": record.browser,
            "artifact_type": record.artifact_type,
            "content_type": record.content_type,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "redacted": record.redacted,
            "viewport": record.viewport,
            "device_scale_factor": float(record.device_scale_factor)
            if record.device_scale_factor is not None else None,
            "color_scheme": record.color_scheme,
            "created_at": record.created_at,
        }

    @staticmethod
    def _encode_cursor(record):
        value = json.dumps({
            "created_at": record.created_at.isoformat(), "id": str(record.id),
        }, separators=(",", ":")).encode("utf-8")
        return urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str):
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
            raise ValueError("invalid artifact cursor")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(urlsafe_b64decode(padded.encode("ascii")))
            created_at = datetime.fromisoformat(value["created_at"])
            record_id = UUID(value["id"])
            if created_at.tzinfo is None:
                raise ValueError
            return created_at, record_id
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid artifact cursor") from exc
