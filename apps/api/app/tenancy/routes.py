from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.core.errors import ProblemDetails
from apps.api.app.tenancy.persistence import (
    ForbiddenScope,
    MissingResource,
    OrganizationProjectRepository,
)


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    slug: str = Field(min_length=1)


class OrganizationView(BaseModel):
    id: UUID
    name: str
    slug: str
    role: str


class OrganizationPage(BaseModel):
    items: list[OrganizationView]
    next_cursor: str | None


class OrganizationMemberView(BaseModel):
    user_id: UUID
    organization_id: UUID
    email: str
    display_name: str
    role: Literal["org_admin", "org_member"]
    joined_at: datetime


class OrganizationMemberPage(BaseModel):
    items: list[OrganizationMemberView]
    next_cursor: str | None


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9-]{2,64}$")


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default=None, min_length=1, max_length=120)


class ProjectView(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    slug: str
    created_by: UUID
    role: str | None


class ProjectMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["project_admin", "tester", "developer", "viewer"]


class ProjectMemberView(BaseModel):
    user_id: UUID
    project_id: UUID
    email: str
    display_name: str
    role: Literal["project_admin", "tester", "developer", "viewer"]


class ProjectMemberPage(BaseModel):
    items: list[ProjectMemberView]
    next_cursor: str | None


class ProjectMemberRemoved(BaseModel):
    status: Literal["removed"]


def create_tenancy_router(
    *,
    repository: OrganizationProjectRepository | None,
    auth_sessions: AuthSessionService | None,
):
    router = APIRouter(prefix="/api/v1", tags=["Organizations & Projects"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def dependencies(credentials: HTTPAuthorizationCredentials | None, *, permission="organization:read",
                    organization_id=None, project_id=None):
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Organization service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        scoped_org = organization_id
        if project_id is not None and principal.get("credential_type") == "api_token":
            scoped_org = repository.organization_id_for_project(project_id)
            if scoped_org is None:
                raise HTTPException(status_code=404, detail="Project not found or inaccessible")
        elif scoped_org is None and principal.get("credential_type") == "api_token":
            if permission == "organization:create":
                raise HTTPException(status_code=403, detail="API tokens cannot create organizations")
            scoped_org = principal.get("organization_id")
        try:
            require_permission(principal, permission, organization_id=scoped_org)
        except MissingPermission as exc:
            raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        return repository, UUID(principal["id"]), principal

    @router.get(
        "/organizations", response_model=OrganizationPage,
        operation_id="get__api_v1_organizations",
        responses={401: {"description": "Authentication required"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def list_organizations(
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        repo, actor_id, principal = dependencies(credentials, permission="organization:read")
        try:
            return repo.list_organizations(actor_id, limit=limit, cursor=cursor,
                scoped_organization_id=(UUID(principal["organization_id"])
                    if principal.get("credential_type") == "api_token" else None))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid organization cursor") from exc

    @router.post(
        "/organizations", response_model=OrganizationView,
        operation_id="post__api_v1_organizations",
        responses={401: {"description": "Authentication required"}, 409: {"description": "Organization slug already exists"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def create_organization(payload: OrganizationCreate, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        repo, actor_id, _ = dependencies(credentials, permission="organization:create")
        try:
            return repo.create_organization(
                actor_id, name=payload.name, slug=payload.slug, now=datetime.now(timezone.utc)
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Organization slug already exists") from exc

    @router.get(
        "/organizations/{org_id}/members", response_model=OrganizationMemberPage,
        operation_id="get__api_v1_organizations__org_id__members",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Organization not found or inaccessible"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def list_organization_members(
        org_id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        repo, actor_id, _ = dependencies(credentials, permission="organization:read", organization_id=org_id)
        try:
            return repo.list_organization_members(actor_id, org_id, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid organization member cursor") from exc
        except MissingResource as exc:
            raise HTTPException(status_code=404, detail="Organization not found or inaccessible") from exc

    @router.get(
        "/projects", response_model=list[ProjectView],
        operation_id="get__api_v1_projects",
        responses={401: {"description": "Authentication required"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def list_projects(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        repo, actor_id, principal = dependencies(credentials, permission="organization:read")
        return repo.list_projects(actor_id, scoped_organization_id=(UUID(principal["organization_id"])
            if principal.get("credential_type") == "api_token" else None))

    @router.post(
        "/projects", response_model=ProjectView,
        operation_id="post__api_v1_projects",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Organization not found or inaccessible"}, 409: {"description": "Project slug already exists in organization"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def create_project(payload: ProjectCreate, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        repo, actor_id, _ = dependencies(credentials, permission="organization:write", organization_id=payload.organization_id)
        try:
            return repo.create_project(
                actor_id,
                organization_id=payload.organization_id,
                name=payload.name,
                slug=payload.slug,
                now=datetime.now(timezone.utc),
            )
        except ForbiddenScope as exc:
            raise HTTPException(status_code=404, detail="Organization not found or inaccessible") from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Project slug already exists in organization") from exc

    @router.get(
        "/projects/{id}", response_model=ProjectView,
        operation_id="get__api_v1_projects__id",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Project not found or inaccessible"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def get_project(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        repo, actor_id, _ = dependencies(credentials, permission="organization:read", project_id=id)
        try:
            return repo.get_project(actor_id, id)
        except MissingResource as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.patch(
        "/projects/{id}", response_model=ProjectView,
        operation_id="patch__api_v1_projects__id",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Project not found or inaccessible"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def patch_project(id: UUID, payload: ProjectPatch, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not payload.model_fields_set or payload.name is None:
            raise HTTPException(status_code=422, detail="At least one field is required")
        repo, actor_id, _ = dependencies(credentials, permission="organization:write", project_id=id)
        try:
            return repo.update_project(
                actor_id, id, name=payload.name, now=datetime.now(timezone.utc)
            )
        except (MissingResource, ForbiddenScope) as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.get(
        "/projects/{id}/members", response_model=ProjectMemberPage,
        operation_id="get__api_v1_projects__id__members",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Project not found or inaccessible"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def list_project_members(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        repo, actor_id, _ = dependencies(credentials, permission="organization:read", project_id=id)
        try:
            return repo.list_project_members(actor_id, id, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid project member cursor") from exc
        except MissingResource as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.put(
        "/projects/{id}/members/{user_id}", response_model=ProjectMemberView,
        operation_id="put__api_v1_projects__id__members__user_id",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Project or organization member not found"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def set_project_member(
        id: UUID, user_id: UUID, payload: ProjectMemberUpdate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        repo, actor_id, _ = dependencies(credentials, permission="organization:write", project_id=id)
        try:
            return repo.set_project_member(
                actor_id, id, user_id, role=payload.role, now=datetime.now(timezone.utc)
            )
        except ForbiddenScope as exc:
            raise HTTPException(status_code=404, detail="Project or organization member not found") from exc
        except MissingResource as exc:
            raise HTTPException(status_code=404, detail="Project or organization member not found") from exc

    @router.delete(
        "/projects/{project_id}/members/{user_id}", response_model=ProjectMemberRemoved,
        operation_id="delete_api_v1_projects_project_id_members_user_id",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Project member not found"}, 503: {"description": "Organization service is not configured", "model": ProblemDetails}},
    )
    def remove_project_member(
        project_id: UUID, user_id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        repo, actor_id, _ = dependencies(credentials, permission="organization:write", project_id=project_id)
        try:
            repo.remove_project_member(actor_id, project_id, user_id)
        except (ForbiddenScope, MissingResource) as exc:
            raise HTTPException(status_code=404, detail="Project member not found") from exc
        return ProjectMemberRemoved(status="removed")

    return router
