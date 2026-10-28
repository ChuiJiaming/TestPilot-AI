"""Tenant-scoped Run APIs and one-use SSE event streaming."""

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.cases.persistence import (
    CasePermissionDenied, CaseRepository, CaseScopeError, CaseVersionConflict,
)
from apps.api.app.core.errors import ProblemDetails
from apps.api.app.runs.persistence import RunCreationBlocked
from apps.api.app.runs.repository import (
    EventCursorExpired, RunApprovalConflict, RunApprovalPermissionDenied,
    RunRepository, RunScopeDenied, RunStateConflict,
)
from apps.api.app.tenancy.persistence import ProjectRecord


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    environment_id: UUID
    plan_id: UUID | None = None
    browser_matrix: list[Literal["chromium", "firefox", "webkit"]] = Field(min_length=1, max_length=3)
    trigger_source: Literal["manual", "ci", "schedule", "api"]
    branch: str | None = Field(default=None, max_length=255)
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,64}$")
    request_intent: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def distinct_browsers(self):
        if len(set(self.browser_matrix)) != len(self.browser_matrix):
            raise ValueError("browser_matrix values must be unique")
        return self


class RunControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class SseTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID


class ApprovalView(BaseModel):
    id: UUID
    project_id: UUID
    target_kind: Literal["run", "healing", "case_version", "visual_baseline", "risky_action"]
    target_id: UUID
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]
    requested_by: UUID | None
    kind: str | None = None
    expires_at: datetime | None = None


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "REJECT"]
    reason: str


class ApprovalPage(BaseModel):
    items: list[ApprovalView]
    next_cursor: str | None


def create_run_router(*, repository: RunRepository | None,
                      case_repository: CaseRepository | None = None,
                      auth_sessions: AuthSessionService | None,
                      allowed_origins: tuple[str, ...] = ()):
    router = APIRouter(prefix="/api/v1", tags=["Runs & Approvals", "SSE"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def principal_for(credentials):
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Run service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return principal

    def organization_id_for_project(project_id):
        with repository.session_factory() as session:
            return session.scalar(select(ProjectRecord.organization_id).where(
                ProjectRecord.id == project_id))

    def actor(credentials, *, project_id: UUID, permission: str):
        principal = principal_for(credentials)
        if principal.get("credential_type") == "api_token":
            organization_id = organization_id_for_project(project_id)
            if organization_id is None:
                raise HTTPException(status_code=404, detail="Project not found")
            try:
                require_permission(principal, permission, organization_id=organization_id)
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        try:
            return UUID(principal["id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid authenticated principal") from exc

    def project_for_run(run_id):
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Run service is not configured")
        project_id = repository.project_id_for_run(run_id)
        if project_id is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return project_id

    def project_for_approval(approval_id):
        if repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Approval service is not configured")
        context = repository.approval_context(approval_id)
        if context is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        return context

    def run_actor(credentials, run_id, permission):
        project_id = project_for_run(run_id)
        actor_id = actor(credentials, project_id=project_id, permission=permission)
        return actor_id, project_id

    def check_origin(request: Request):
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="Origin header required")
        parsed = urlsplit(origin)
        allowed = set(allowed_origins)
        forwarded_host = request.headers.get("x-forwarded-host")
        request_host = forwarded_host or request.headers.get("host", "")
        forwarded_proto = request.headers.get("x-forwarded-proto")
        request_scheme = forwarded_proto or request.url.scheme
        same_origin = parsed.netloc.casefold() == request_host.casefold() and parsed.scheme == request_scheme
        if (allowed and origin not in allowed) or (not allowed and not same_origin):
            raise HTTPException(status_code=403, detail="Origin is not allowed")
        if allowed and not same_origin and origin not in allowed:
            raise HTTPException(status_code=403, detail="Origin is not allowed")

    def map_repo_error(exc):
        if isinstance(exc, RunScopeDenied):
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if isinstance(exc, RunStateConflict):
            raise HTTPException(status_code=409, detail="Run state conflict") from exc
        if isinstance(exc, RunCreationBlocked):
            code = exc.error_code
            status = 404 if code in {"PLAN_NOT_FOUND", "SUITE_NOT_FOUND"} else 409 if code in {
                "SUITE_CASE_NOT_PUBLISHED", "PUBLISHED_VERSION_NOT_FOUND",
            } else 422
            raise HTTPException(status_code=status, detail=code) from exc

    @router.post("/test-runs", operation_id="post__api_v1_test_runs",
                 responses={400: {"model": ProblemDetails}, 409: {"model": ProblemDetails},
                            422: {"model": ProblemDetails}})
    def create_run(payload: RunCreate,
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
                   credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        actor_id = actor(credentials, project_id=payload.project_id, permission="run:create")
        scoped_key = sha256(
            f"{payload.project_id}:{payload.trigger_source}:{idempotency_key.strip()}".encode("utf-8")
        ).hexdigest()
        try:
            return repository.create_run(
                actor_id, project_id=payload.project_id,
                environment_id=payload.environment_id, plan_id=payload.plan_id,
                browser_matrix=payload.browser_matrix, trigger_source=payload.trigger_source,
                idempotency_key=f"api:{scoped_key}", request_intent=payload.request_intent,
                branch=payload.branch, commit_sha=payload.commit_sha,
                now=datetime.now(timezone.utc),
            )
        except (RunCreationBlocked, RunScopeDenied) as exc:
            map_repo_error(exc)

    @router.get("/test-runs/{id}", operation_id="get_api_v1_test_runs_id",
                responses={404: {"model": ProblemDetails}})
    def get_run(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id, _ = run_actor(credentials, id, "run:read")
        try:
            value = repository.get_run(actor_id, id)
        except RunScopeDenied as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if value is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return value

    @router.get(
        "/test-runs/{id}/approvals",
        response_model=ApprovalPage,
        operation_id="get__api_v1_test_runs__id__approvals",
        responses={400: {"description": "Invalid cursor", "model": ProblemDetails}},
    )
    def list_run_approvals(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id, _ = run_actor(credentials, id, "run:read")
        try:
            page = repository.list_approvals(actor_id, id, limit=limit, cursor=cursor)
        except RunScopeDenied as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc
        if page is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return page

    @router.post(
        "/approvals/{id}/decide",
        response_model=ApprovalView,
        operation_id="post__api_v1_approvals__id__decide",
        responses={403: {"model": ProblemDetails}, 404: {"model": ProblemDetails},
                   409: {"model": ProblemDetails}},
    )
    def decide_approval(
        id: UUID,
        payload: ApprovalDecision,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        context = project_for_approval(id)
        actor_id = actor(credentials, project_id=context["project_id"], permission="approval:decide")
        try:
            repository.authorize_approval_project(actor_id, context["project_id"])
            decision = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}[payload.decision]
            if context["target_kind"] == "run":
                result = repository.decide_run_approval(
                    actor_id, id, decision=decision, reason=payload.reason,
                    now=datetime.now(timezone.utc),
                )
                if result.get("expired"):
                    raise HTTPException(status_code=409, detail="Approval expired")
                if result.get("conflict"):
                    raise HTTPException(status_code=409, detail="Run is no longer waiting for approval")
                return result["approval"]
            if context["target_kind"] == "case_version":
                if case_repository is None:
                    raise HTTPException(status_code=503, detail="Case approval service is not configured")
                return case_repository.decide_approval(
                    actor_id, id, decision=decision,
                    reason=payload.reason, now=datetime.now(timezone.utc),
                )
            raise HTTPException(status_code=409, detail="Approval target is not supported yet")
        except RunApprovalPermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Approval requester cannot decide their own request") from exc
        except (RunApprovalConflict, CaseVersionConflict) as exc:
            raise HTTPException(status_code=409, detail="Approval state conflict") from exc
        except (RunScopeDenied, CaseScopeError) as exc:
            raise HTTPException(status_code=404, detail="Approval not found") from exc
        except CasePermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Project approval permission denied") from exc

    @router.get("/projects/{id}/test-runs",
                operation_id="get__api_v1_projects__id__test_runs")
    def list_runs(id: UUID, limit: int = Query(default=30, ge=1, le=100),
                  cursor: str | None = Query(default=None, max_length=512),
                  credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = actor(credentials, project_id=id, permission="resource:read")
        try:
            return repository.list_runs(actor_id, id, limit=limit, cursor=cursor)
        except RunScopeDenied as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc

    def change_run(id, payload, credentials, target, allowed):
        actor_id, _ = run_actor(credentials, id, "resource:write")
        try:
            return repository.transition(
                id, target, expected_status=set(allowed), now=datetime.now(timezone.utc),
                payload={"reason_code": "USER_REQUESTED"},
                actor_user_id=actor_id,
            )
        except (RunScopeDenied, RunStateConflict) as exc:
            map_repo_error(exc)

    @router.post("/test-runs/{id}/pause", operation_id="post__api_v1_test_runs__id__pause")
    def pause_run(id: UUID, payload: RunControl,
                  credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return change_run(id, payload, credentials, "PAUSING", {"RUNNING"})

    @router.post("/test-runs/{id}/resume", operation_id="post__api_v1_test_runs__id__resume")
    def resume_run(id: UUID, payload: RunControl,
                   credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return change_run(id, payload, credentials, "RUNNING", {"PAUSED"})

    @router.post("/test-runs/{id}/cancel", operation_id="post__api_v1_test_runs__id__cancel")
    def cancel_run(id: UUID, payload: RunControl,
                   credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return change_run(
            id, payload, credentials, "CANCELLING",
            {"QUEUED", "PREPARING", "RUNNING", "WAITING_APPROVAL", "PAUSING", "PAUSED"},
        )

    @router.post("/test-runs/{id}/retry", operation_id="post__api_v1_test_runs__id__retry",
                 status_code=201)
    def retry_run(id: UUID, payload: RunControl,
                  credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id, _ = run_actor(credentials, id, "resource:write")
        try:
            return repository.retry(actor_id, id, reason=payload.reason,
                                    now=datetime.now(timezone.utc))
        except (RunScopeDenied, RunStateConflict, RunCreationBlocked) as exc:
            map_repo_error(exc)

    @router.get("/test-runs/{id}/steps", operation_id="get__api_v1_test_runs__id__steps")
    def list_steps(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id, _ = run_actor(credentials, id, "run:read")
        try:
            result = repository.list_steps(actor_id, id)
        except RunScopeDenied as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result

    @router.post("/sse-tickets", operation_id="post__api_v1_sse_tickets",
                 responses={403: {"model": ProblemDetails}, 404: {"model": ProblemDetails}})
    def create_sse_ticket(payload: SseTicketRequest, request: Request, response: Response,
                           credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        check_origin(request)
        actor_id, _ = run_actor(credentials, payload.run_id, "run:read")
        try:
            token = repository.issue_sse_ticket(
                actor_id, payload.run_id, now=datetime.now(timezone.utc))
        except RunScopeDenied as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if token is None:
            raise HTTPException(status_code=404, detail="Run not found")
        path = f"/api/v1/test-runs/{payload.run_id}/events"
        response.set_cookie(
            "testpilot_sse", token, max_age=60, httponly=True, secure=True,
            samesite="strict", path=path,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"run_id": payload.run_id, "expires_in": 60}

    @router.get("/test-runs/{id}/events", operation_id="get__api_v1_test_runs__id__events",
                responses={409: {"model": ProblemDetails}})
    async def stream_events(
        id: UUID, request: Request,
        after_id: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        ticket: str | None = Cookie(default=None, alias="testpilot_sse"),
    ):
        if repository is None:
            raise HTTPException(status_code=503, detail="Run service is not configured")
        check_origin(request)
        user_id = repository.consume_sse_ticket(
            id, ticket or "", now=datetime.now(timezone.utc))
        if user_id is None:
            raise HTTPException(status_code=401, detail="Valid one-use SSE ticket required")
        if after_id is not None:
            cursor = after_id
        elif last_event_id:
            try:
                cursor = int(last_event_id)
                if cursor < 0:
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc
        else:
            cursor = 0
        try:
            initial = repository.list_events(UUID(str(user_id)), id, after_id=cursor, limit=500)
        except EventCursorExpired as exc:
            raise HTTPException(status_code=409, detail="SNAPSHOT_REQUIRED") from exc
        except RunScopeDenied as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if initial is None:
            raise HTTPException(status_code=404, detail="Run not found")

        async def generate():
            nonlocal cursor
            last_heartbeat = datetime.now(timezone.utc)
            while not await request.is_disconnected():
                try:
                    page = repository.list_events(
                        UUID(str(user_id)), id, after_id=cursor, limit=500)
                except (EventCursorExpired, RunScopeDenied):
                    yield "event: error\ndata: {\"code\":\"SNAPSHOT_REQUIRED\"}\n\n"
                    return
                for item in page["items"]:
                    cursor = item["id"]
                    envelope = {
                        "schema_version": 1, "run_id": str(id),
                        "event_type": item["event_type"], "payload": item["payload"],
                        "timestamp": item["timestamp"].isoformat(),
                    }
                    yield (f"id: {cursor}\nevent: {item['event_type']}\n"
                           f"data: {json.dumps(envelope, separators=(',', ':'))}\n\n")
                if page["items"]:
                    continue
                run = repository.get_run(UUID(str(user_id)), id)
                if run is None or run["status"] in {"PASSED", "FAILED", "ERROR", "CANCELLED"}:
                    return
                now = datetime.now(timezone.utc)
                if (now - last_heartbeat).total_seconds() >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )

    return router
