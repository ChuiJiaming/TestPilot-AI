"""Tenant-scoped immutable report and export request APIs."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.reports.persistence import ReportPermissionDenied, ReportRepository


class ReportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    summary: dict
    branch: str | None = None
    commit_sha: str | None = None
    artifact_references: list[dict] = []
    next_cursor: str | None = None


class ReportExportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    format: Literal["html", "pdf", "json", "junit"]
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]
    artifact_id: UUID | None = None


class ReportExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    format: Literal["html", "pdf", "json", "junit"]


class NestedReportExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["html", "pdf", "json", "junit"]


def create_report_router(
    *, repository: ReportRepository | None, auth_sessions: AuthSessionService | None,
):
    router = APIRouter(prefix="/api/v1", tags=["Reports & Defects"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def principal_for(credentials):
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Report service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            actor_id = UUID(principal["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid authenticated principal") from exc
        return principal, actor_id

    def enforce_permission(principal, permission, organization_id):
        if organization_id is None:
            raise HTTPException(status_code=404, detail="Report resource not found or inaccessible")
        try:
            require_permission(principal, permission, organization_id=organization_id)
        except MissingPermission as exc:
            raise HTTPException(
                status_code=403, detail="API token scope or organization boundary denied",
            ) from exc

    def actor_for_run(credentials, run_id, permission):
        principal, actor_id = principal_for(credentials)
        organization_id = repository.organization_id_for_run(run_id)
        enforce_permission(principal, permission, organization_id)
        return actor_id

    def actor_for_report(credentials, report_id, permission):
        principal, actor_id = principal_for(credentials)
        organization_id = repository.organization_id_for_report(report_id)
        enforce_permission(principal, permission, organization_id)
        run_id = repository.report_run_id(actor_id, report_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="Report not found or inaccessible")
        return actor_id, run_id

    def read_report(actor_id, run_id, *, limit, cursor):
        try:
            result = repository.get_report(actor_id, run_id, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid report artifact cursor") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Report not found or inaccessible")
        return result

    def request_export(actor_id, run_id, export_format, idempotency_key):
        try:
            result = repository.create_export(
                actor_id, run_id=run_id, format=export_format,
                idempotency_key=idempotency_key, now=datetime.now(timezone.utc),
            )
        except ReportPermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project permission denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid report export request") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Report not found or inaccessible")
        return result

    @router.get(
        "/test-runs/{id}/report",
        response_model=ReportView,
        operation_id="get__api_v1_test_runs__id__report",
    )
    def get_run_report(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for_run(credentials, id, "run:read")
        return read_report(actor_id, id, limit=limit, cursor=cursor)

    @router.get(
        "/test-runs/{id}/report/export",
        response_model=ReportView,
        operation_id="get__api_v1_test_runs__id__report_export",
    )
    def get_legacy_run_report_export(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        # The baseline operation remains a Report DTO read with run:read scope.
        actor_id = actor_for_run(credentials, id, "run:read")
        return read_report(actor_id, id, limit=limit, cursor=cursor)

    @router.post(
        "/report-exports",
        response_model=ReportExportView,
        status_code=202,
        operation_id="post__api_v1_report_exports",
    )
    def create_legacy_report_export(
        payload: ReportExportRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=160),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for_run(credentials, payload.run_id, "resource:write")
        return request_export(actor_id, payload.run_id, payload.format, idempotency_key)

    @router.post(
        "/reports/{id}/exports",
        response_model=ReportExportView,
        status_code=202,
        operation_id="post__api_v1_reports__id__exports",
    )
    def create_report_export(
        id: UUID,
        payload: NestedReportExportRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=160),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id, run_id = actor_for_report(credentials, id, "resource:write")
        return request_export(actor_id, run_id, payload.format, idempotency_key)

    @router.get(
        "/report-exports/{id}",
        response_model=ReportExportView,
        operation_id="get__api_v1_report_exports__id",
    )
    def get_report_export(
        id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal, actor_id = principal_for(credentials)
        organization_id = repository.organization_id_for_export(id)
        enforce_permission(principal, "resource:read", organization_id)
        result = repository.get_export(actor_id, id)
        if result is None:
            raise HTTPException(status_code=404, detail="Report export not found or inaccessible")
        return result

    return router
