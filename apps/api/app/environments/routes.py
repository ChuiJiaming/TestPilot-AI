from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError

from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.core.errors import ProblemDetails
from apps.api.app.environments.persistence import (
    EnvironmentPermissionDenied,
    EnvironmentRepository,
    EnvironmentScopeError,
    EnvironmentSnapshotRepository,
)
from apps.api.app.environments.health_check import (
    BrowserGatewayHealthClient,
    HealthCheckGatewayUnavailable,
    HealthCheckPolicyError,
    HealthCheckRequest,
    HealthCheckResult,
    validate_health_check_target,
)
from apps.api.app.tenancy.persistence import (
    MissingResource,
    OrganizationProjectRepository,
)


class EnvironmentSnapshotView(BaseModel):
    id: UUID
    environment_id: UUID
    project_id: UUID
    source_updated_at: datetime | None
    base_url: str
    network_policy: dict[str, object]
    secret_ref_version_id: UUID | None
    snapshot_hash: str
    created_at: datetime


class EnvironmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    base_url: str = Field(min_length=8)
    network_policy: dict[str, object] = Field(default_factory=dict)
    secret_ref_id: UUID | None = None

    @field_validator("base_url")
    @classmethod
    def require_http_url_without_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        return value


class EnvironmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None, min_length=8)
    network_policy: dict[str, object] | None = None
    secret_ref_id: UUID | None = None

    @field_validator("base_url")
    @classmethod
    def require_http_url_without_credentials(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        return value


class EnvironmentView(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    base_url: str
    secret_ref_id: UUID | None
    network_policy: dict[str, object]
    created_at: datetime


class EnvironmentPage(BaseModel):
    items: list[EnvironmentView]
    next_cursor: str | None


def create_environment_router(
    *,
    environments: EnvironmentRepository | None = None,
    snapshots: EnvironmentSnapshotRepository | None,
    tenancy: OrganizationProjectRepository | None,
    auth_sessions: AuthSessionService | None,
    health_check_gateway: BrowserGatewayHealthClient | None = None,
):
    router = APIRouter(prefix="/api/v1", tags=["Projects & Environments"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def require_actor(credentials, *, permission: str, project_id: UUID | None = None,
                      environment_id: UUID | None = None):
        if tenancy is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Environment service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if principal.get("credential_type") == "api_token":
            scoped_project = project_id
            if environment_id is not None and environments is not None:
                scoped_project = environments.project_id_for_environment(environment_id)
            organization_id = tenancy.organization_id_for_project(scoped_project) if scoped_project else None
            try:
                require_permission(principal, permission, organization_id=organization_id)
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        return UUID(principal["id"])

    @router.post(
        "/projects/{id}/environments",
        response_model=EnvironmentView,
        operation_id="post__api_v1_projects__id__environments",
        responses={
            401: {"description": "Authentication required"},
            403: {"description": "Project write role is required"},
            404: {"description": "Project or SecretRef not found or inaccessible"},
            409: {"description": "Environment name already exists in project"},
            503: {"description": "Environment service is not configured", "model": ProblemDetails},
        },
    )
    def create_environment(
        id: UUID,
        payload: EnvironmentCreate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if environments is None:
            raise HTTPException(status_code=503, detail="Environment service is not configured")
        actor_id = require_actor(credentials, permission="resource:write", project_id=id)
        try:
            return environments.create(
                actor_id, id, name=payload.name, base_url=payload.base_url,
                network_policy=payload.network_policy, secret_ref_id=payload.secret_ref_id,
                now=datetime.now(timezone.utc),
            )
        except EnvironmentPermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write role is required") from exc
        except EnvironmentScopeError as exc:
            raise HTTPException(status_code=404, detail="Project or SecretRef not found or inaccessible") from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Environment name already exists in project") from exc

    @router.get(
        "/projects/{id}/environments",
        response_model=EnvironmentPage,
        operation_id="get__api_v1_projects__id__environments",
        responses={
            401: {"description": "Authentication required"},
            404: {"description": "Project not found or inaccessible"},
            503: {"description": "Environment service is not configured", "model": ProblemDetails},
        },
    )
    def list_environments(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if environments is None:
            raise HTTPException(status_code=503, detail="Environment service is not configured")
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        actor_id = require_actor(credentials, permission="resource:read", project_id=id)
        try:
            return environments.list_for_project(actor_id, id, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid environment cursor") from exc
        except EnvironmentScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.patch(
        "/environments/{id}",
        response_model=EnvironmentView,
        operation_id="patch__api_v1_environments__id",
        responses={
            401: {"description": "Authentication required"},
            403: {"description": "Project write role is required"},
            404: {"description": "Environment or SecretRef not found or inaccessible"},
            409: {"description": "Environment name already exists in project"},
            503: {"description": "Environment service is not configured", "model": ProblemDetails},
        },
    )
    def patch_environment(
        id: UUID,
        payload: EnvironmentPatch,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if environments is None:
            raise HTTPException(status_code=503, detail="Environment service is not configured")
        changes = payload.model_dump(exclude_unset=True)
        if not changes or any(value is None for key, value in changes.items() if key != "secret_ref_id"):
            raise HTTPException(status_code=422, detail="At least one non-null field is required")
        actor_id = require_actor(credentials, permission="resource:write", environment_id=id)
        try:
            return environments.update(
                actor_id, id, changes=changes, now=datetime.now(timezone.utc)
            )
        except EnvironmentPermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project write role is required") from exc
        except EnvironmentScopeError as exc:
            raise HTTPException(status_code=404, detail="Environment or SecretRef not found or inaccessible") from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Environment name already exists in project") from exc

    @router.get(
        "/environments/{id}/snapshots",
        response_model=list[EnvironmentSnapshotView],
        operation_id="get_api_v1_environments_id_snapshots",
        responses={
            401: {"description": "Authentication required"},
            404: {"description": "Environment not found or inaccessible"},
            503: {"description": "Environment service is not configured", "model": ProblemDetails},
        },
    )
    def list_snapshots(
        id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if snapshots is None:
            raise HTTPException(status_code=503, detail="Environment service is not configured")
        actor_id = require_actor(credentials, permission="resource:read", environment_id=id)
        project_id = snapshots.project_id_for_environment(id)
        if project_id is None:
            raise HTTPException(status_code=404, detail="Environment not found or inaccessible")
        try:
            tenancy.get_project(actor_id, project_id)
            return snapshots.list_for_environment(environment_id=id, project_id=project_id)
        except (MissingResource, EnvironmentScopeError) as exc:
            raise HTTPException(status_code=404, detail="Environment not found or inaccessible") from exc

    @router.post(
        "/environments/{id}/health-check",
        response_model=HealthCheckResult,
        operation_id="post__api_v1_environments__id__health_check",
        responses={
            403: {"description": "Project role or domain allowlist denied"},
            404: {"description": "Environment not found or inaccessible"},
            422: {"description": "Invalid network policy or request"},
            503: {"description": "Isolated Browser Gateway is unavailable", "model": ProblemDetails},
        },
    )
    def health_check(
        id: UUID,
        payload: HealthCheckRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = require_actor(credentials, permission="resource:write", environment_id=id)
        if environments is None:
            raise HTTPException(status_code=503, detail="Environment service is not configured")
        try:
            context = environments.health_check_context(actor_id, id)
        except EnvironmentPermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project administrator or tester role required") from exc
        except EnvironmentScopeError as exc:
            raise HTTPException(status_code=404, detail="Environment not found or inaccessible") from exc
        try:
            _, allowed_domains = validate_health_check_target(
                context["base_url"], context["network_policy"]
            )
        except HealthCheckPolicyError as exc:
            status = 403 if str(exc) == "DOMAIN_NOT_ALLOWLISTED" else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        if health_check_gateway is None:
            raise HTTPException(status_code=503, detail="Isolated Browser Gateway is not configured")
        try:
            return health_check_gateway.check(
                environment_id=context["id"], base_url=context["base_url"],
                network_policy=context["network_policy"], allowed_domains=allowed_domains,
                secret_ref_id=context["secret_ref_id"],
                secret_ref_version_id=context["secret_ref_version_id"],
                check_name=payload.name,
            )
        except HealthCheckGatewayUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router
