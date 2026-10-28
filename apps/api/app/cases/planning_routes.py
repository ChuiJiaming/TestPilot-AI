from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.cases.persistence import CaseScopeError
from apps.api.app.cases.planning import GenerationRepository, PlanningRepository, PlanningConflict
from apps.api.app.cases.scheduling import validate_cron_expression
from apps.api.app.core.errors import ProblemDetails


class GenerationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = Field(min_length=5, max_length=12000)
    model_name: str


class SuiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None


class SuiteCaseAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: UUID
    sequence: int = Field(default=0, ge=0)


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    suite_id: UUID | None = None
    policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_concurrency_policy(self):
        policy = dict(self.policy)
        mode = policy.get("concurrency_mode", "serial")
        maximum = policy.get("max_concurrent_runs", 1)
        if mode not in {"serial", "bounded"}:
            raise ValueError("concurrency_mode must be serial or bounded")
        if type(maximum) is not int or maximum < 1:
            raise ValueError("max_concurrent_runs must be a positive integer")
        if mode == "serial" and maximum != 1:
            raise ValueError("serial concurrency requires max_concurrent_runs=1")
        policy["concurrency_mode"] = mode
        policy["max_concurrent_runs"] = maximum
        self.policy = policy
        return self


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron_expression: str = Field(min_length=1, max_length=255)
    timezone: str = "UTC"
    environment_id: UUID

    @field_validator("cron_expression")
    @classmethod
    def valid_cron(cls, value: str):
        return validate_cron_expression(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an installed IANA timezone") from exc
        return value


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron_expression: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = None
    enabled: bool | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("cron_expression")
    @classmethod
    def valid_cron(cls, value: str | None):
        return None if value is None else validate_cron_expression(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None):
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an installed IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def reject_null_or_empty_patch(self):
        if not self.model_fields_set:
            raise ValueError("at least one schedule field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("schedule fields cannot be null")
        if self.model_fields_set == {"reason"}:
            raise ValueError("reason must accompany a schedule change")
        return self


class ScheduleReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class ScheduleTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_intent: str = Field(min_length=1, max_length=4000)
    reason: str | None = Field(default=None, max_length=2000)


class GenerationAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_ids: list[UUID] = Field(min_length=1)


class DeadGenerationOutboxItem(BaseModel):
    id: int
    aggregate_id: UUID
    event_type: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    available_at: datetime


class DeadGenerationOutboxPage(BaseModel):
    items: list[DeadGenerationOutboxItem]
    next_cursor: str | None


def create_planning_router(
    *, planning: PlanningRepository | None, generations: GenerationRepository | None,
    auth_sessions: AuthSessionService | None,
):
    router = APIRouter(prefix="/api/v1", tags=["Cases & Plans"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def principal_for(credentials):
        if planning is None or generations is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Planning service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return principal

    def actor(credentials, *, permission: str, project_id: UUID | None = None):
        principal = principal_for(credentials)
        if principal.get("credential_type") == "api_token":
            try:
                require_permission(principal, permission, organization_id=_organization_id(project_id))
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        try:
            return UUID(principal["id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid authenticated principal") from exc

    def _organization_id(project_id):
        if project_id is None:
            return None
        # The planning repositories share the authenticated DB session factory.
        with planning.session_factory() as session:
            from sqlalchemy import select
            from apps.api.app.tenancy.persistence import ProjectRecord
            return session.scalar(select(ProjectRecord.organization_id).where(ProjectRecord.id == project_id))

    def project_actor(credentials, project_id, permission):
        actor_id = actor(credentials, permission=permission, project_id=project_id)
        return actor_id

    def scoped_actor(credentials, project_resolver: Callable[[], UUID | None], permission):
        principal = principal_for(credentials)
        project_id = project_resolver()
        if project_id is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if principal.get("credential_type") == "api_token":
            try:
                require_permission(principal, permission, organization_id=_organization_id(project_id))
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope or organization boundary denied") from exc
        return UUID(principal["id"])

    @router.post("/projects/{id}/generations", status_code=202,
                 operation_id="post__api_v1_projects__id__generations",
                 responses={503: {"description": "Generation service unavailable", "model": ProblemDetails}})
    def create_generation(id: UUID, payload: GenerationCreate,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = project_actor(credentials, id, "resource:write")
        try:
            return generations.create_job(actor_id, id, requirement=payload.requirement,
                                          model_name=payload.model_name, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.get("/projects/{id}/generations", operation_id="get_api_v1_projects_id_generations")
    def list_generations(id: UUID, limit: int = Query(default=30, ge=1, le=100),
                         cursor: str | None = Query(default=None, max_length=512),
                         credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = project_actor(credentials, id, "resource:read")
        try:
            return generations.list_jobs(actor_id, id, limit=limit, cursor=cursor)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid generation cursor") from exc

    @router.post("/projects/{id}/test-cases/generate", status_code=202, deprecated=True,
                 operation_id="post_api_v1_projects_id_test_cases_generate")
    def create_generation_alias(id: UUID, payload: GenerationCreate,
                                credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return create_generation(id, payload, credentials)

    @router.get("/generations/{id}", operation_id="get__api_v1_generations__id")
    def get_generation(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: generations.project_id_for_job(id) if generations else None, "resource:read")
        try:
            return generations.get_job(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Generation job not found") from exc

    @router.post("/generations/{id}/cancel", operation_id="post_api_v1_generations_id_cancel")
    def cancel_generation(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: generations.project_id_for_job(id) if generations else None, "resource:write")
        try:
            return generations.cancel_job(actor_id, id, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Generation job not found") from exc
        except PlanningConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/generations/{id}/retry", operation_id="post_api_v1_generations_id_retry")
    def retry_generation(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: generations.project_id_for_job(id) if generations else None, "resource:write")
        try:
            return generations.retry_job(actor_id, id, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Generation job not found") from exc
        except PlanningConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/generations/{id}/accept", operation_id="post_api_v1_generations_id_accept")
    def accept_generation(id: UUID, payload: GenerationAccept,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: generations.project_id_for_job(id) if generations else None, "resource:write")
        try:
            return generations.accept_proposals(actor_id, id, proposal_ids=payload.proposal_ids,
                                                now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Generation job not found") from exc
        except PlanningConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/projects/{id}/suites", operation_id="post__api_v1_projects__id__suites")
    def create_suite(id: UUID, payload: SuiteCreate,
                     credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = project_actor(credentials, id, "resource:write")
        try:
            return planning.create_suite(actor_id, id, name=payload.name, description=payload.description,
                                         now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.get("/projects/{id}/suites", operation_id="get_api_v1_projects_id_suites")
    def list_suites(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = project_actor(credentials, id, "resource:read")
        try:
            return planning.list_suites(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.post("/suites/{id}/cases", operation_id="post__api_v1_suites__id__cases")
    def add_case_to_suite(id: UUID, payload: SuiteCaseAdd,
                          credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_suite(id) if planning else None, "resource:write")
        try:
            return planning.add_case(actor_id, id, case_id=payload.case_id, sequence=payload.sequence)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Suite or project case not found") from exc

    @router.get("/suites/{id}/cases", operation_id="get_api_v1_suites_id_cases")
    def list_suite_cases(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials,
            lambda: planning.project_id_for_suite(id) if planning else None, "resource:read")
        try:
            return planning.list_suite_cases(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Suite not found") from exc

    @router.get("/projects/{id}/plans", operation_id="get_api_v1_projects_id_plans")
    def list_plans(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = project_actor(credentials, id, "resource:read")
        try:
            return planning.list_plans(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project not found or inaccessible") from exc

    @router.post("/projects/{id}/plans", operation_id="post__api_v1_projects__id__plans")
    def create_plan(id: UUID, payload: PlanCreate,
                    credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = project_actor(credentials, id, "resource:write")
        try:
            return planning.create_plan(actor_id, id, name=payload.name, suite_id=payload.suite_id,
                                        policy=payload.policy, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Project or suite not found") from exc

    @router.get("/plans/{id}/schedules", operation_id="get_api_v1_plans_id_schedules")
    def list_schedules(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_plan(id) if planning else None, "resource:read")
        try:
            return planning.list_schedules(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Plan not found") from exc

    @router.post("/plans/{id}/schedules", operation_id="post__api_v1_plans__id__schedules")
    def create_schedule(id: UUID, payload: ScheduleCreate,
                        credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_plan(id) if planning else None, "resource:write")
        try:
            return planning.create_schedule(actor_id, id, environment_id=payload.environment_id,
                cron_expression=payload.cron_expression, timezone=payload.timezone,
                now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Plan or same-project environment not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/schedules/{id}", operation_id="get_api_v1_schedules_id")
    def get_schedule(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_schedule(id) if planning else None, "resource:read")
        try:
            return planning.get_schedule(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch("/schedules/{id}", operation_id="patch__api_v1_schedules__id")
    def patch_schedule(id: UUID, payload: SchedulePatch,
                       credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_schedule(id) if planning else None, "resource:write")
        changes = payload.model_dump(exclude_unset=True)
        reason = changes.pop("reason", None)
        try:
            return planning.update_schedule(actor_id, id, changes=changes, reason=reason,
                now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/schedules/{id}", operation_id="delete__api_v1_schedules__id")
    def delete_schedule(id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_schedule(id) if planning else None, "resource:write")
        try:
            return planning.delete_schedule(actor_id, id)
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def set_enabled(schedule_id, credentials, *, enabled, reason):
        actor_id = scoped_actor(credentials,
            lambda: planning.project_id_for_schedule(schedule_id) if planning else None, "resource:write")
        try:
            return planning.set_schedule_enabled(actor_id, schedule_id, enabled=enabled,
                reason=reason, now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/schedules/{id}/enable", operation_id="post__api_v1_schedules__id__enable")
    def enable_schedule(id: UUID, payload: ScheduleReason,
                        credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return set_enabled(id, credentials, enabled=True, reason=payload.reason)

    @router.post("/schedules/{id}/disable", operation_id="post__api_v1_schedules__id__disable")
    def disable_schedule(id: UUID, payload: ScheduleReason,
                         credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return set_enabled(id, credentials, enabled=False, reason=payload.reason)

    @router.post("/schedules/{id}/pause", operation_id="post_api_v1_schedules_id_pause")
    def pause_schedule(id: UUID, payload: ScheduleReason,
                       credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return set_enabled(id, credentials, enabled=False, reason=payload.reason)

    @router.post("/schedules/{id}/resume", operation_id="post_api_v1_schedules_id_resume")
    def resume_schedule(id: UUID, payload: ScheduleReason,
                        credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        return set_enabled(id, credentials, enabled=True, reason=payload.reason)

    @router.post("/schedules/{id}/trigger", operation_id="post__api_v1_schedules__id__trigger")
    def trigger_schedule(id: UUID, payload: ScheduleTrigger,
                         idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=160),
                         credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        actor_id = scoped_actor(credentials, lambda: planning.project_id_for_schedule(id) if planning else None, "resource:write")
        try:
            return planning.trigger_schedule(actor_id, id, idempotency_key=idempotency_key,
                request_intent=payload.request_intent, reason=payload.reason,
                now=datetime.now(timezone.utc))
        except CaseScopeError as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc

    @router.get(
        "/admin/generation-outbox/dead", response_model=DeadGenerationOutboxPage,
        operation_id="get_api_v1_admin_generation_outbox_dead",
    )
    def list_dead_generation_outbox(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if generations is None:
            raise HTTPException(status_code=503, detail="Generation service is not configured")
        principal = principal_for(credentials)
        if principal.get("credential_type") == "api_token":
            raise HTTPException(status_code=403, detail="Platform administrator session required")
        try:
            actor_id = UUID(principal["id"])
            return generations.list_dead_outbox_events(actor_id, limit=limit, cursor=cursor)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Platform administrator session required") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc

    return router
