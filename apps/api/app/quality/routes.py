"""Authenticated project-scoped access to quality evidence."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.cases.persistence import CasePermissionDenied, CaseScopeError
from apps.api.app.core.errors import ProblemDetails
from apps.api.app.quality.artifacts import ArtifactStorageError
from apps.api.app.quality.persistence import QualityConflict, QualityRepository


class ArtifactView(BaseModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    run_case_id: UUID | None
    step_id: UUID | None
    action_id: UUID | None
    environment_id: UUID | None
    case_version_id: UUID | None
    browser: str | None
    artifact_type: str
    content_type: str
    size_bytes: int | None
    redacted: bool
    viewport: dict | None
    device_scale_factor: float | None
    color_scheme: str | None
    created_at: datetime


class ArtifactPage(BaseModel):
    items: list[ArtifactView]
    next_cursor: str | None


class DefectView(BaseModel):
    id: UUID
    project_id: UUID
    fingerprint: str
    title: str
    status: Literal["OPEN", "TRIAGED", "IN_PROGRESS", "RESOLVED", "WONT_FIX"]
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    classification: Literal[
        "LOCATOR", "ASSERTION", "ENVIRONMENT", "INFRA", "SECURITY", "DATA", "FLAKY",
    ]
    root_cause_status: Literal["INSUFFICIENT_EVIDENCE", "SUGGESTED", "CONFIRMED"]
    root_cause_confirmed_by: UUID | None = None
    root_cause_confirmed_at: datetime | None = None
    triaged_by: UUID | None = None
    triaged_at: datetime | None = None
    analysis: dict
    created_at: datetime
    occurrence_count: int | None = None


class DefectOccurrenceView(BaseModel):
    id: UUID
    defect_id: UUID
    run_id: UUID
    run_case_id: UUID
    case_version_id: UUID
    environment_id: UUID
    browser: str | None
    step_id: UUID | None
    action_id: UUID | None
    classification: str
    evidence_status: Literal["INSUFFICIENT_EVIDENCE", "AVAILABLE"]
    error_code: str | None
    evidence: dict
    observed_at: datetime


class QualityPage(BaseModel):
    items: list[dict]
    next_cursor: str | None


class DefectPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["OPEN", "TRIAGED", "IN_PROGRESS", "RESOLVED", "WONT_FIX"] | None = None
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None
    classification: Literal[
        "LOCATOR", "ASSERTION", "ENVIRONMENT", "INFRA", "SECURITY", "DATA", "FLAKY",
    ] | None = None
    root_cause_status: Literal["INSUFFICIENT_EVIDENCE", "SUGGESTED", "CONFIRMED"] | None = None
    reason: str = Field(min_length=1, max_length=2000)


class MaskRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class VisualBaselineProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    case_version_id: UUID
    action_id: UUID
    environment_id: UUID
    browser: Literal["chromium", "firefox", "webkit"]
    artifact_id: UUID
    viewport: dict
    mask_regions: list[MaskRegion] = Field(default_factory=list, max_length=100)
    max_pixel_ratio: float = Field(default=0.01, ge=0, le=1)
    pixel_channel_tolerance: int = Field(default=0, ge=0, le=255)


class VisualBaselineDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=1, max_length=2000)


class VisualBaselineView(BaseModel):
    id: UUID
    project_id: UUID
    case_version_id: UUID
    action_id: UUID | None
    environment_id: UUID | None
    browser: str
    viewport: dict
    artifact_id: UUID
    device_scale_factor: float
    color_scheme: str
    status: str
    approved_by: UUID | None
    approved_at: datetime | None
    created_by: UUID | None
    decided_by: UUID | None
    decision_reason: str | None
    decided_at: datetime | None
    mask_regions: list[dict]
    max_pixel_ratio: float
    pixel_channel_tolerance: int
    mask_hash: str
    comparison_profile: str
    created_at: datetime


class VisualDiffView(BaseModel):
    id: UUID
    baseline_id: UUID | None
    run_case_id: UUID
    actual_artifact_id: UUID | None
    diff_artifact_id: UUID | None
    pixel_ratio: float | None
    changed_pixels: int | None
    compared_pixels: int | None
    max_pixel_ratio: float | None
    pixel_channel_tolerance: int | None
    mask_hash: str | None
    comparison_profile: str | None
    status: str
    error_code: str | None
    created_at: datetime


def create_quality_router(*, repository: QualityRepository | None,
                          auth_sessions: AuthSessionService | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Reports & Defects"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def actor_for(credentials, *, project_id=None, artifact_id=None,
                  defect_id=None, baseline_id=None, diff_id=None,
                  permission="resource:read"):
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Quality service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if artifact_id is not None:
            project_id = repository.project_id_for_artifact(artifact_id)
            if project_id is None:
                raise HTTPException(status_code=404, detail="Artifact not found or inaccessible")
        if defect_id is not None:
            project_id = repository.project_id_for_defect(defect_id)
            if project_id is None:
                raise HTTPException(status_code=404, detail="Defect not found or inaccessible")
        if baseline_id is not None:
            project_id = repository.project_id_for_visual_baseline(baseline_id)
            if project_id is None:
                raise HTTPException(status_code=404, detail="Visual baseline not found or inaccessible")
        if diff_id is not None:
            project_id = repository.project_id_for_visual_diff(diff_id)
            if project_id is None:
                raise HTTPException(status_code=404, detail="Visual diff not found or inaccessible")
        organization_id = repository.organization_id_for_project(project_id)
        if organization_id is None:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible")
        try:
            require_permission(principal, permission, organization_id=organization_id)
        except MissingPermission as exc:
            raise HTTPException(
                status_code=403,
                detail="API token scope or organization boundary denied",
            ) from exc
        return UUID(principal["id"])

    @router.get(
        "/projects/{project_id}/defects",
        response_model=QualityPage,
        operation_id="get__api_v1_projects__id__defects",
        tags=["Reports & Defects"],
    )
    def list_project_defects(
        project_id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, project_id=project_id)
        try:
            return repository.list_defects(actor_id, project_id, limit=limit, cursor=cursor)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.get(
        "/defects/{id}", response_model=DefectView,
        operation_id="get__api_v1_defects__id", tags=["Reports & Defects"],
    )
    def get_defect(
        id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, defect_id=id)
        try:
            result = repository.get_defect(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Defect not found or inaccessible") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Defect not found or inaccessible")
        return result

    @router.patch(
        "/defects/{id}", response_model=DefectView,
        operation_id="patch__api_v1_defects__id", tags=["Reports & Defects"],
    )
    def patch_defect(
        id: UUID,
        payload: DefectPatchRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(
            credentials, defect_id=id, permission="resource:write")
        body = payload.model_dump(exclude_none=True)
        reason = body.pop("reason")
        try:
            result = repository.patch_defect(
                actor_id, id, changes=body, reason=reason,
                now=datetime.now(timezone.utc),
            )
        except QualityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Defect not found or inaccessible") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write permission denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid defect triage request") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Defect not found or inaccessible")
        return result

    @router.get(
        "/defects/{id}/occurrences", response_model=QualityPage,
        operation_id="get__api_v1_defects__id__occurrences", tags=["Reports & Defects"],
    )
    def list_defect_occurrences(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, defect_id=id)
        try:
            result = repository.list_defect_occurrences(
                actor_id, id, limit=limit, cursor=cursor)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Defect not found or inaccessible") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Defect not found or inaccessible")
        return result

    @router.get(
        "/projects/{project_id}/visual-baselines", response_model=QualityPage,
        operation_id="get__api_v1_projects__id__visual_baselines",
        tags=["Reports & Defects"],
    )
    def list_visual_baselines(
        project_id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, project_id=project_id)
        try:
            return repository.list_visual_baselines(
                actor_id, project_id, limit=limit, cursor=cursor)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.post(
        "/visual-baselines/proposals", response_model=VisualBaselineView,
        status_code=status.HTTP_201_CREATED,
        operation_id="post__api_v1_visual_baselines_proposals",
        tags=["Reports & Defects"],
    )
    def propose_visual_baseline(
        payload: VisualBaselineProposal,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(
            credentials, project_id=payload.project_id, permission="resource:write")
        try:
            result = repository.propose_visual_baseline(
                actor_id, payload=payload.model_dump(mode="json"),
                now=datetime.now(timezone.utc),
            )
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write permission denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid visual baseline proposal") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Screenshot artifact not found or inaccessible")
        return result

    @router.post(
        "/visual-baselines/{id}/decision", response_model=VisualBaselineView,
        operation_id="post__api_v1_visual_baselines__id__decision",
        tags=["Reports & Defects"],
    )
    def decide_visual_baseline(
        id: UUID,
        payload: VisualBaselineDecisionRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(
            credentials, baseline_id=id, permission="approval:decide")
        try:
            result = repository.decide_visual_baseline(
                actor_id, id, decision=payload.decision, reason=payload.reason,
                now=datetime.now(timezone.utc),
            )
        except QualityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Visual baseline not found or inaccessible") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project decision permission denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid visual baseline decision") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Visual baseline not found or inaccessible")
        return result

    @router.get(
        "/visual-diffs", response_model=QualityPage,
        operation_id="get_api_v1_visual_diffs", tags=["Reports & Defects"],
    )
    def list_visual_diffs(
        project_id: UUID,
        run_case_id: UUID | None = None,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, project_id=project_id)
        try:
            return repository.list_visual_diffs(
                actor_id, project_id, limit=limit, cursor=cursor,
                run_case_id=run_case_id,
            )
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.get(
        "/visual-diffs/{id}", response_model=VisualDiffView,
        operation_id="get__api_v1_visual_diffs__id", tags=["Reports & Defects"],
    )
    def get_visual_diff(
        id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, diff_id=id)
        try:
            result = repository.get_visual_diff(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Visual diff not found or inaccessible") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Visual diff not found or inaccessible")
        return result

    @router.get(
        "/projects/{project_id}/artifacts",
        response_model=ArtifactPage,
        operation_id="get__api_v1_projects__id__artifacts",
        responses={
            401: {"description": "Authentication required"},
            403: {"description": "Project permission denied"},
            404: {"description": "Project not found or inaccessible"},
            503: {"description": "Quality service is not configured", "model": ProblemDetails},
        },
    )
    def list_project_artifacts(
        project_id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        artifact_type: Literal["screenshot", "video", "trace", "har", "console", "report", "diff"] | None = None,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, project_id=project_id)
        try:
            return repository.list_artifacts(
                actor_id, project_id, limit=limit, cursor=cursor,
                artifact_type=artifact_type,
            )
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project permission denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid artifact cursor") from exc

    @router.get(
        "/artifacts/{id}/download",
        operation_id="get__api_v1_artifacts__id__download",
        responses={
            200: {"description": "Authorized private artifact bytes"},
            401: {"description": "Authentication required"},
            403: {"description": "Project permission denied"},
            404: {"description": "Artifact not found or inaccessible"},
            503: {"description": "Object storage is unavailable", "model": ProblemDetails},
        },
    )
    def download_artifact(
        id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, artifact_id=id)
        try:
            result = repository.download_artifact(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found or inaccessible") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project permission denied") from exc
        except ArtifactStorageError as exc:
            raise HTTPException(status_code=503, detail="Artifact storage is unavailable") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Artifact not found or inaccessible")
        artifact, data = result
        content_type = artifact.get("content_type")
        report_types = {
            "text/html; charset=utf-8": ("html", "text/html; charset=utf-8"),
            "application/pdf": ("pdf", "application/pdf"),
            "application/json": ("json", "application/json"),
            "application/xml": ("junit", "application/xml"),
        }
        if artifact.get("artifact_type") == "report" and content_type in report_types:
            extension, media_type = report_types[content_type]
            disposition = f'attachment; filename="{id}.{extension}"'
        elif content_type == "image/png":
            media_type = "image/png"
            disposition = f'inline; filename="{id}.png"'
        else:
            media_type = "application/octet-stream"
            disposition = "attachment"
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": disposition,
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
