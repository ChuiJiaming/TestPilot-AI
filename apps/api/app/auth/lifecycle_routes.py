from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError

from apps.api.app.auth.lifecycle import (
    InvalidInvitation, LifecycleForbidden, LifecycleNotFound,
    LifecycleRepository, PERMISSION_REGISTRY,
)
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.auth.permissions import MissingPermission, require_permission


class InvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role: Literal["org_admin", "org_member", "project_admin", "tester", "developer", "viewer"]
    project_id: UUID | None = None
    # Expiry is supplied explicitly because the product contract has not chosen a default TTL.
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expiration_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class InvitationCreated(BaseModel):
    id: UUID
    organization_id: UUID
    project_id: UUID | None
    email: str
    role: str
    expires_at: datetime
    created_at: datetime
    token: str


class InvitationAccepted(BaseModel):
    status: Literal["accepted"]
    organization_id: UUID
    project_id: UUID | None
    role: str


class InvitationAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=256)


class ApiTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class ApiTokenView(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    scopes: list[str]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    token: str | None = None


class SecretRefCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=80)
    external_key: str = Field(min_length=1, max_length=512)
    provider_version: str = Field(min_length=1, max_length=512)


class SecretRefAliasCreate(SecretRefCreate):
    organization_id: UUID


class SecretRefRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_version: str = Field(min_length=1, max_length=512)


class SecretRefView(BaseModel):
    id: UUID
    organization_id: UUID
    provider: str
    external_key: str
    rotated_at: datetime | None
    created_at: datetime
    active_version: int | None = None


def create_lifecycle_router(*, repository: LifecycleRepository | None,
                            auth_sessions: AuthSessionService | None):
    router = APIRouter(prefix="/api/v1", tags=["Auth & Orgs", "Projects & Environments"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def actor(credentials: HTTPAuthorizationCredentials | None, *, permission: str | None = None,
              organization_id: UUID | None = None, allow_api_token: bool = True) -> dict:
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Lifecycle service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if principal.get("credential_type") == "api_token":
            if not allow_api_token or permission is None:
                raise HTTPException(status_code=403, detail="This action requires a user session")
            try:
                require_permission(principal, permission,
                    organization_id=organization_id or principal.get("organization_id"))
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        return principal

    @router.post("/organizations/{org_id}/invitations", response_model=InvitationCreated,
                 operation_id="post__api_v1_organizations__org_id__invitations", status_code=201)
    def create_invitation(org_id: UUID, payload: InvitationCreate,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="organization:write", organization_id=org_id)
            return repository.create_invitation(UUID(principal["id"]), org_id,
                project_id=payload.project_id, email=payload.email, role=payload.role,
                expires_at=payload.expires_at, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Organization/project not found or inaccessible") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="An equivalent invitation already exists") from exc

    @router.post("/invitations/accept", response_model=InvitationAccepted,
                 operation_id="post__api_v1_invitations_accept")
    def accept_invitation(payload: InvitationAccept,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, allow_api_token=False)
            return repository.accept_invitation(UUID(principal["id"]), payload.token, now=datetime.now(timezone.utc))
        except InvalidInvitation as exc:
            raise HTTPException(status_code=400, detail="Invitation is invalid, expired, or already accepted") from exc

    @router.post("/organizations/{org_id}/api-tokens", response_model=ApiTokenView, status_code=201,
                 operation_id="post__api_v1_organizations__org_id__api_tokens")
    def create_org_token(org_id: UUID, payload: ApiTokenCreate,
                         credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if any(scope not in PERMISSION_REGISTRY for scope in payload.scopes):
            raise HTTPException(status_code=422, detail="Unknown permission scope")
        try:
            principal = actor(credentials, permission="organization:write", organization_id=org_id)
            if principal.get("credential_type") == "api_token" and not set(payload.scopes) <= set(principal.get("scopes", ())):
                raise HTTPException(status_code=403, detail="An API token cannot grant scopes it does not hold")
            return repository.create_api_token(UUID(principal["id"]), org_id, name=payload.name,
                scopes=payload.scopes, expires_at=payload.expires_at, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Organization not found or inaccessible") from exc

    @router.post("/api-tokens", response_model=ApiTokenView, status_code=201,
                 operation_id="post__api_v1_api_tokens")
    def create_token(payload: ApiTokenCreate,
                     credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if payload.organization_id is None:
            raise HTTPException(status_code=422, detail="organization_id is required")
        if any(scope not in PERMISSION_REGISTRY for scope in payload.scopes):
            raise HTTPException(status_code=422, detail="Unknown permission scope")
        try:
            principal = actor(credentials, permission="resource:write", organization_id=payload.organization_id)
            if principal.get("credential_type") == "api_token" and not set(payload.scopes) <= set(principal.get("scopes", ())):
                raise HTTPException(status_code=403, detail="An API token cannot grant scopes it does not hold")
            return repository.create_api_token(UUID(principal["id"]), payload.organization_id,
                name=payload.name, scopes=payload.scopes, expires_at=payload.expires_at,
                now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Organization not found or inaccessible") from exc

    @router.get("/api-tokens", response_model=list[ApiTokenView], operation_id="get__api_v1_api_tokens")
    def list_tokens(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        principal = actor(credentials, permission="resource:read")
        return repository.list_api_tokens(UUID(principal["id"]),
            organization_id=principal.get("organization_id") if principal.get("credential_type") == "api_token" else None)

    @router.delete("/api-tokens/{id}", response_model=ApiTokenView,
                   operation_id="delete__api_v1_api_tokens__id")
    def revoke_token(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="resource:write")
            if principal.get("credential_type") == "api_token" and not any(
                item["id"] == id for item in repository.list_api_tokens(UUID(principal["id"]), organization_id=UUID(principal["organization_id"]))
            ):
                raise LifecycleNotFound
            return repository.revoke_api_token(UUID(principal["id"]), id, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Token not found or inaccessible") from exc

    @router.post("/api-tokens/{id}/rotate", response_model=ApiTokenView,
                 operation_id="post__api_v1_api_tokens__id__rotate")
    def rotate_token(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="resource:write")
            if principal.get("credential_type") == "api_token" and not any(
                item["id"] == id for item in repository.list_api_tokens(UUID(principal["id"]), organization_id=UUID(principal["organization_id"]))
            ):
                raise LifecycleNotFound
            return repository.rotate_api_token(UUID(principal["id"]), id, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Token not found or inaccessible") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/organizations/{org_id}/secret-refs", response_model=SecretRefView, status_code=201,
                 operation_id="post__api_v1_organizations__org_id__secret_refs")
    def create_secret_ref(org_id: UUID, payload: SecretRefCreate,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="resource:write", organization_id=org_id)
            return repository.create_secret_ref(UUID(principal["id"]), org_id,
                provider=payload.provider, external_key=payload.external_key,
                provider_version=payload.provider_version, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Organization not found or inaccessible") from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="SecretRef already exists") from exc

    @router.post("/secret-refs", response_model=SecretRefView,
                 operation_id="post__api_v1_secret_refs")
    def create_secret_ref_compat(payload: SecretRefAliasCreate,
                                 credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="resource:write", organization_id=payload.organization_id)
            return repository.create_secret_ref(UUID(principal["id"]), payload.organization_id,
                provider=payload.provider, external_key=payload.external_key,
                provider_version=payload.provider_version, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="Organization not found or inaccessible") from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="SecretRef already exists") from exc

    @router.get("/secret-refs", response_model=list[SecretRefView], operation_id="get__api_v1_secret_refs")
    def list_secret_refs(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        principal = actor(credentials, permission="resource:read")
        return repository.list_secret_refs(UUID(principal["id"]),
            organization_id=principal.get("organization_id") if principal.get("credential_type") == "api_token" else None)

    @router.post("/organizations/{org_id}/secret-refs/{secret_id}/rotate",
                 response_model=SecretRefView,
                 operation_id="post_api_v1_organizations_org_id_secret_refs_secret_id_rotate")
    def rotate_secret_ref(org_id: UUID, secret_id: UUID, payload: SecretRefRotate,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="resource:write", organization_id=org_id)
            refs = repository.list_secret_refs(UUID(principal["id"]), organization_id=org_id)
            if not any(ref["id"] == secret_id for ref in refs):
                raise LifecycleNotFound
            return repository.rotate_secret_ref(UUID(principal["id"]), secret_id,
                provider_version=payload.provider_version, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="SecretRef not found or inaccessible") from exc

    @router.post("/secret-refs/{id}/rotate", response_model=SecretRefView,
                 operation_id="post__api_v1_secret_refs__id__rotate")
    def rotate_secret_ref_compat(id: UUID, payload: SecretRefRotate,
                                 credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            principal = actor(credentials, permission="resource:write")
            actor_id = UUID(principal["id"])
            visible = repository.list_secret_refs(actor_id,
                organization_id=(UUID(principal["organization_id"])
                    if principal.get("credential_type") == "api_token" else None))
            if not any(item["id"] == id for item in visible):
                raise LifecycleNotFound
            return repository.rotate_secret_ref(actor_id, id,
                provider_version=payload.provider_version, now=datetime.now(timezone.utc))
        except LifecycleNotFound as exc:
            raise HTTPException(status_code=404, detail="SecretRef not found or inaccessible") from exc

    @router.delete("/secret-refs/{id}", status_code=409,
                   operation_id="delete__api_v1_secret_refs__id")
    def delete_secret_ref_disabled(id: UUID,
                                   credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor(credentials, permission="resource:write")
        raise HTTPException(status_code=409, detail="SecretRef deletion is disabled by the current retention policy")

    return router
