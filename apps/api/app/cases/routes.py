from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.cases.dsl import DslValidationFailed, validate_dsl
from apps.api.app.cases.persistence import (
    CasePermissionDenied,
    CaseRepository,
    CaseScopeError,
    CaseVersionConflict,
)
from apps.api.app.core.errors import ProblemDetails
from apps.api.app.tenancy.persistence import OrganizationProjectRepository


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Candidate OpenAPI marks these optional, while the database requires title.
    # Runtime enforces the storage invariant; contract reconciliation remains open.
    title: str
    priority: str = "P1"


class CaseVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsl: dict[str, Any]
    changelog: str = ""


class CaseVersionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]
    reason: str


class CaseRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_version: str
    reason: str


class CasePublish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=250)


def create_case_router(
    *, cases: CaseRepository | None, tenancy: OrganizationProjectRepository | None,
    auth_sessions: AuthSessionService | None,
):
    router = APIRouter(prefix="/api/v1", tags=["Cases & Plans"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def actor(credentials, *, permission: str, project_id: UUID | None = None, case_id: UUID | None = None):
        if cases is None or tenancy is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Case service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        organization_id = None
        if case_id is not None:
            project_id = cases.project_id_for_case(case_id)
            if project_id is None:
                raise HTTPException(status_code=404, detail="Case not found")
        if project_id is not None:
            organization_id = tenancy.organization_id_for_project(project_id)
        if principal.get("credential_type") == "api_token":
            try:
                require_permission(principal, permission, organization_id=organization_id)
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        try:
            return UUID(principal["id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid authenticated principal") from exc

    def require_login(credentials):
        if credentials is None or auth_sessions is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if auth_sessions.authenticate(credentials.credentials) is None:
            raise HTTPException(status_code=401, detail="Authentication required")

    @router.post(
        "/projects/{id}/test-cases", operation_id="post__api_v1_projects__id__test_cases",
        responses={401: {"description": "Authentication required"}, 403: {"description": "Project write role required"},
                   404: {"description": "Project not found or inaccessible"}, 422: {"description": "Invalid case fields"},
                   503: {"description": "Case service unavailable", "model": ProblemDetails}},
    )
    def create_case(id: UUID, payload: CaseCreate,
                    credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:write", project_id=id)
        if payload.priority not in {"P0", "P1", "P2", "P3"}:
            raise HTTPException(status_code=422, detail="priority must be P0, P1, P2, or P3")
        try:
            return cases.create_case(actor_id, id, title=payload.title, priority=payload.priority,
                                     now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write role required") from exc

    @router.get("/projects/{id}/test-cases", operation_id="get__api_v1_projects__id__test_cases")
    def list_cases(id: UUID, limit: int = Query(30, ge=1, le=100), cursor: str | None = None,
                   credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:read", project_id=id)
        try:
            return cases.list_cases(actor_id, id, limit=limit, cursor=cursor)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc

    @router.get("/test-cases/{id}", operation_id="get__api_v1_test_cases__id")
    def get_case(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:read", case_id=id)
        try:
            return cases.get_case(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc

    @router.post("/test-cases/{id}/versions", operation_id="post__api_v1_test_cases__id__versions")
    def create_version(id: UUID, payload: CaseVersionCreate,
                       credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:write", case_id=id)
        try:
            dsl = validate_dsl(payload.dsl, expected_case_id=id)
        except DslValidationFailed as exc:
            return JSONResponse(status_code=422, content={
                "code": "DSL_VALIDATION_FAILED", "message": "DSL validation failed",
                "trace_id": str(uuid4()), "details": {"errors": [
                    {"loc": list(error.get("loc", ())), "msg": error.get("msg", "Invalid value"),
                     "type": error.get("type", "value_error")}
                    for error in exc.errors
                ]},
            })
        try:
            return cases.create_version(actor_id, id, dsl=dsl, changelog=payload.changelog,
                                        now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write role required") from exc

    @router.get("/test-cases/{id}/versions", operation_id="get__api_v1_test_cases__id__versions")
    def list_versions(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:read", case_id=id)
        try:
            return cases.list_versions(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc

    def apply_decision(case_id: UUID, version_id: UUID, *, decision: str,
                       reason: str | None,
                       credentials: HTTPAuthorizationCredentials | None):
        actor_id = actor(credentials, permission="approval:decide", case_id=case_id)
        try:
            return cases.decide_version(actor_id, version_id, decision=decision,
                                        reason=reason, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case version not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CaseVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/test-case-versions/{version_id}/approve",
                 operation_id="post_api_v1_test_case_versions_version_id_approve")
    def decide_version_by_id(version_id: UUID,
                              credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        require_login(credentials)
        case_id = cases.case_id_for_version(version_id) if cases is not None else None
        if case_id is None:
            raise HTTPException(status_code=404, detail="Case version not found")
        return apply_decision(
            case_id, version_id, decision="APPROVED", reason=None,
            credentials=credentials,
        )

    @router.post("/test-cases/{id}/versions/{version}/approve",
                 operation_id="post__api_v1_test_cases__id__versions__version__approve")
    def decide_version_by_number(id: UUID, version: str, payload: CaseVersionDecision,
                                 credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            number = int(version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Case version not found") from exc
        actor_id = actor(credentials, permission="approval:decide", case_id=id)
        try:
            view = cases.get_version(actor_id, id, number)
            return cases.decide_version(actor_id, view["id"], decision=payload.decision,
                                        reason=payload.reason, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case version not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CaseVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/test-case-versions/{version_id}/publish",
                 operation_id="post_api_v1_test_case_versions_version_id_publish")
    def publish_version(version_id: UUID, payload: CasePublish,
                        credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        require_login(credentials)
        case_id = cases.case_id_for_version(version_id) if cases is not None else None
        if case_id is None:
            raise HTTPException(status_code=404, detail="Case version not found")
        actor_id = actor(credentials, permission="approval:decide", case_id=case_id)
        try:
            return cases.publish_version(actor_id, version_id, name=payload.name,
                                         now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case version not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project admin or tester role required") from exc
        except CaseVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/test-cases/{case_id}/archive", operation_id="post_api_v1_test_cases_case_id_archive")
    def archive_case(case_id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:write", case_id=case_id)
        try:
            return cases.archive_case(actor_id, case_id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write role required") from exc

    def rollback(case_id: UUID, payload: CaseRollback, credentials):
        actor_id = actor(credentials, permission="resource:write", case_id=case_id)
        try:
            target = int(payload.target_version)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="target_version must be a version number") from exc
        if target < 1:
            raise HTTPException(status_code=422, detail="target_version must be positive")
        try:
            return cases.rollback_version(actor_id, case_id, target_version=target,
                                          reason=payload.reason, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case or target version not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write role required") from exc
        except CaseVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/test-cases/{case_id}/rollback", operation_id="post_api_v1_test_cases_case_id_rollback")
    def rollback_case(case_id: UUID, payload: CaseRollback,
                      credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return rollback(case_id, payload, credentials)

    @router.post("/test-cases/{id}/versions/{version}/rollback",
                 operation_id="post__api_v1_test_cases__id__versions__version__rollback")
    def rollback_case_alias(id: UUID, version: str, payload: CaseRollback,
                            credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if version != payload.target_version:
            raise HTTPException(status_code=409, detail="Path version and target_version must match")
        return rollback(id, payload, credentials)

    @router.get("/projects/{id}/test-cases/{case_id}/versions/{version_id}/diff",
                operation_id="get_api_v1_projects_id_test_cases_case_id_versions_version_id_diff")
    def diff_versions(id: UUID, case_id: UUID, version_id: UUID, against_version_id: UUID,
                      credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, permission="resource:read", project_id=id)
        try:
            case = cases.get_case(actor_id, case_id)
            if case["project_id"] != id:
                raise CaseScopeError
            return cases.diff_by_ids(actor_id, case_id, version_id, against_version_id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Case or version not found") from exc
        except CaseVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
