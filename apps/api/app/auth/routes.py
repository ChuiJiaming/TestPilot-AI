from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.auth.verification import EmailVerificationService, RegistrationAccepted
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.core.errors import ProblemDetails


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=256)


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=1, max_length=128)
    device_label: str | None = Field(default=None, max_length=80)


class AuthUser(BaseModel):
    id: UUID
    email: str
    display_name: str
    status: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    user: AuthUser


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_sessions: bool = False


class AuthSessionView(BaseModel):
    id: UUID
    device_label: str
    created_at: datetime
    last_used_at: datetime
    revoked_at: datetime | None


class AuthSessionList(BaseModel):
    sessions: list[AuthSessionView]


class AuthSessionRevoked(BaseModel):
    status: Literal["revoked"]


class DeadVerificationEmail(BaseModel):
    id: UUID
    recipient_email: str
    status: Literal["DEAD"]
    attempts: int
    last_error: str | None
    created_at: datetime
    expires_at: datetime


class DeadVerificationEmailPage(BaseModel):
    items: list[DeadVerificationEmail]


class EmailVerificationResult(BaseModel):
    status: str


PasswordHash = Callable[[str], str]


def create_auth_router(
    *,
    service: EmailVerificationService | None,
    password_hash: PasswordHash | None,
    auth_sessions: AuthSessionService | None = None,
    allowed_origins: Sequence[str] = (),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["Auth & Orgs"])
    bearer_scheme = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def configured():
        if service is None or password_hash is None:
            raise HTTPException(status_code=503, detail="Email verification is not configured")
        return service

    def configured_sessions():
        if auth_sessions is None:
            raise HTTPException(status_code=503, detail="Authentication sessions are not configured")
        return auth_sessions

    def require_origin(origin: str | None) -> None:
        if not origin or origin not in allowed_origins:
            raise HTTPException(status_code=403, detail="Request origin is not allowed")

    def current_user(credentials: HTTPAuthorizationCredentials | None) -> dict[str, str]:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        user = configured_sessions().authenticate(credentials.credentials)
        if user is None or user.get("credential_type") == "api_token":
            raise HTTPException(status_code=401, detail="Authentication required")
        return user

    @router.post(
        "/register", status_code=202, response_model=RegistrationAccepted,
        operation_id="post__api_v1_auth_register",
        responses={503: {"description": "Authentication delivery is not configured", "model": ProblemDetails}},
    )
    def register(payload: RegisterRequest, request: Request):
        auth = configured()
        return auth.register(
            email=payload.email,
            password_hash=lambda: password_hash(payload.password),
            display_name=payload.display_name,
            client_ip=request.client.host if request.client else None,
        )

    @router.post(
        "/verify-email", response_model=EmailVerificationResult,
        operation_id="post__api_v1_auth_verify_email",
        responses={503: {"description": "Authentication storage is not configured", "model": ProblemDetails}},
    )
    def verify(payload: VerifyEmailRequest):
        result = configured().verify(payload.token)
        if result is None:
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")
        return EmailVerificationResult(status=result)

    @router.post(
        "/verification/resend", status_code=202, response_model=RegistrationAccepted,
        operation_id="post__api_v1_auth_verification_resend",
        responses={503: {"description": "Authentication delivery is not configured", "model": ProblemDetails}},
    )
    def resend(payload: ResendVerificationRequest, request: Request):
        return configured().resend(
            email=payload.email,
            client_ip=request.client.host if request.client else None,
        )

    @router.post(
        "/login", response_model=AuthResponse,
        operation_id="post__api_v1_auth_login",
        responses={
            200: {"description": "Refresh credential set in a Secure HttpOnly cookie", "headers": {
                "Set-Cookie": {"description": "__Host-testpilot_refresh; Secure; HttpOnly; SameSite=Lax; Path=/", "schema": {"type": "string"}}
            }},
            401: {"description": "Invalid credentials or unverified account"},
            503: {"description": "Authentication is not configured", "model": ProblemDetails},
        },
    )
    def login(payload: LoginRequest, request: Request, response: Response):
        if auth_sessions is None:
            raise HTTPException(status_code=503, detail="Authentication sessions are not configured")
        label = payload.device_label or request.headers.get("user-agent", "Unknown device")
        tokens = auth_sessions.login(
            email=payload.email,
            password=payload.password,
            device_label=label,
            client_ip=request.client.host if request.client else None,
        )
        if tokens is None:
            raise HTTPException(status_code=401, detail="Invalid credentials or unverified account")
        response.set_cookie(
            key="__Host-testpilot_refresh",
            value=tokens.refresh_token,
            max_age=auth_sessions.refresh_ttl_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return AuthResponse(
            access_token=tokens.access_token,
            expires_in=tokens.expires_in,
            user=tokens.user,
        )

    @router.post(
        "/refresh", response_model=AuthResponse,
        operation_id="post__api_v1_auth_refresh",
        responses={
            200: {"description": "Refresh cookie rotated", "headers": {
                "Set-Cookie": {"description": "Rotated __Host-testpilot_refresh cookie", "schema": {"type": "string"}}
            }},
            401: {"description": "Refresh session expired, revoked, or replayed"},
            403: {"description": "Origin is not allowed"},
            503: {"description": "Authentication is not configured", "model": ProblemDetails},
        },
    )
    def refresh(
        request: Request,
        response: Response,
        origin: str = Header(..., alias="Origin"),
        refresh_cookie: str | None = Cookie(default=None, alias="__Host-testpilot_refresh"),
    ):
        require_origin(origin)
        auth = configured_sessions()
        refresh_token = refresh_cookie
        tokens = auth.refresh(refresh_token) if refresh_token else None
        if tokens is None:
            response.delete_cookie("__Host-testpilot_refresh", path="/", secure=True, httponly=True, samesite="lax")
            raise HTTPException(status_code=401, detail="Refresh session is invalid")
        response.set_cookie(
            key="__Host-testpilot_refresh",
            value=tokens.refresh_token,
            max_age=auth.refresh_ttl_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return AuthResponse(access_token=tokens.access_token, expires_in=tokens.expires_in, user=tokens.user)

    @router.post(
        "/logout", status_code=204,
        operation_id="post__api_v1_auth_logout",
        responses={
            401: {"description": "Authentication or refresh session required"},
            403: {"description": "Origin is not allowed"},
            503: {"description": "Authentication is not configured", "model": ProblemDetails},
        },
    )
    def logout(
        request: Request,
        response: Response,
        origin: str = Header(..., alias="Origin"),
        refresh_cookie: str | None = Cookie(default=None, alias="__Host-testpilot_refresh"),
        payload: LogoutRequest | None = None,
    ):
        require_origin(origin)
        auth = configured_sessions()
        is_all_sessions = bool(payload and payload.all_sessions)
        user = None
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() == "bearer" and token:
            user = auth.authenticate(token)
        if is_all_sessions:
            if user is None:
                raise HTTPException(status_code=401, detail="Authentication required to revoke all sessions")
            auth.repository.revoke_all_sessions(UUID(user["id"]), datetime.now(timezone.utc))
        else:
            refresh_token = refresh_cookie
            if not refresh_token:
                raise HTTPException(status_code=401, detail="Refresh session required")
            user_id = UUID(user["id"]) if user else None
            auth.repository.revoke_refresh(
                user_id,
                sha256(refresh_token.encode("utf-8")).hexdigest(),
                datetime.now(timezone.utc),
            )
        response.delete_cookie("__Host-testpilot_refresh", path="/", secure=True, httponly=True, samesite="lax")
        response.status_code = 204
        return response

    @router.get(
        "/sessions", response_model=AuthSessionList,
        operation_id="get__api_v1_auth_sessions",
        responses={401: {"description": "Authentication required"}, 503: {"description": "Authentication is not configured", "model": ProblemDetails}},
    )
    def list_sessions(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        user = current_user(credentials)
        return {"sessions": configured_sessions().repository.list_sessions(UUID(user["id"]))}

    @router.delete(
        "/sessions/{session_id}", response_model=AuthSessionRevoked,
        operation_id="delete__api_v1_auth_sessions__session_id",
        responses={401: {"description": "Authentication required"}, 404: {"description": "Session not found"}, 503: {"description": "Authentication is not configured", "model": ProblemDetails}},
    )
    def revoke_session(
        session_id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        user = current_user(credentials)
        if not configured_sessions().repository.revoke_session(
            UUID(user["id"]), session_id, datetime.now(timezone.utc)
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        return AuthSessionRevoked(status="revoked")

    @router.get(
        "/admin/verification-email-outbox/dead",
        response_model=DeadVerificationEmailPage,
        operation_id="get_api_v1_admin_verification_email_outbox_dead",
    )
    def list_dead_verification_emails(
        limit: int = Query(default=50, ge=1, le=100),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        user = current_user(credentials)
        if user.get("credential_type") == "api_token":
            raise HTTPException(status_code=403, detail="Platform administrator session required")
        try:
            return {"items": configured().list_dead_email_messages(UUID(user["id"]), limit=limit)}
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Platform administrator session required") from exc

    return router


def create_me_router(*, auth_sessions: AuthSessionService | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Auth & Orgs"])
    bearer_scheme = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    @router.get(
        "/me", response_model=AuthUser, operation_id="get__api_v1_me",
        responses={401: {"description": "Authentication required"},
                   404: {"description": "Authenticated user not found"}},
    )
    def me(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)):
        if auth_sessions is None:
            raise HTTPException(status_code=503, detail="Authentication sessions are not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if principal.get("credential_type") == "api_token":
            from apps.api.app.auth.permissions import MissingPermission, require_permission
            try:
                require_permission(principal, "user:self", organization_id=principal.get("organization_id"))
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope denied") from exc
        user = auth_sessions.repository.get_user_by_id(UUID(principal["id"]))
        if user is None or user["status"] != "active":
            raise HTTPException(status_code=404, detail="Authenticated user not found")
        return user

    return router
