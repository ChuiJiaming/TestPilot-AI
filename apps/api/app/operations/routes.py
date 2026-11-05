"""Authenticated notification and delivery operations endpoints."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.operations.notifications import (
    NotificationForbidden,
    NotificationNotFound,
    NotificationService,
)
from apps.api.app.operations.audit import AuditForbidden, AuditRepository
from apps.api.app.operations.model_config import ModelConfigService
from apps.api.app.operations.quotas import QuotaPolicyForbidden, QuotaPolicyService
from apps.api.app.operations.settings import (
    SettingConflict,
    SettingForbidden,
    SettingInvalid,
    SettingNotFound,
    SettingStale,
    SystemSettingService,
)


class NotificationView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    kind: str
    payload: dict
    read_at: datetime | None
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationView]
    next_cursor: str | None = None


class NotificationPreferenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["in_app", "email", "webhook"]
    event_type: str = Field(min_length=1, max_length=80)
    enabled: bool


class NotificationPreferencePage(BaseModel):
    items: list[NotificationPreferenceView]


class NotificationPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[NotificationPreferenceView] = Field(max_length=500)


class MarkNotificationReadView(BaseModel):
    id: UUID
    read: bool


class DeadNotificationDeliveryView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: UUID
    notification_id: UUID
    channel: Literal["email", "webhook"]
    status: Literal["DEAD"]
    attempts: int = Field(ge=0)
    error_code: str
    created_at: datetime


class DeadNotificationDeliveryPage(BaseModel):
    items: list[DeadNotificationDeliveryView]
    next_cursor: str | None = None


class AuditEntryView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    organization_id: UUID
    actor_user_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID | None
    metadata: dict[str, Any]
    previous_hash: str | None
    entry_hash: str
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEntryView]
    next_cursor: str | None = None


class AuditExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: UUID
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=100, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=64)


class AuditExportView(BaseModel):
    filename: str
    media_type: str
    content: str
    next_cursor: str | None = None


class AuditVerificationView(BaseModel):
    valid: bool
    entries_checked: int = Field(ge=0)
    first_invalid_entry_id: int | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None


class QuotaPolicyView(BaseModel):
    organization_id: UUID
    max_concurrent_runs: int = Field(ge=1)
    monthly_token_limit: int = Field(ge=0)
    monthly_tokens_committed: int = Field(ge=0)
    monthly_tokens_reserved: int = Field(ge=0)
    monthly_tokens_remaining: int = Field(ge=0)
    quota_exhausted: bool
    period_month: str
    updated_at: datetime | None = None


class QuotaPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent_runs: int = Field(ge=1, le=1000)
    monthly_token_limit: int = Field(ge=0, le=2**63 - 1)


class UsageMetricView(BaseModel):
    organization_id: UUID
    metric_name: str
    value: float
    unit: str


class UsagePage(BaseModel):
    items: list[UsageMetricView]


class SystemSettingView(BaseModel):
    key: str
    value_hash: str
    updated_at: datetime


class SystemSettingChangeView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    key: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "STALE"]
    previous_value_hash: str
    proposed_value_hash: str
    requested_by: UUID
    approved_by: UUID | None
    reason: str
    created_at: datetime
    decided_at: datetime | None


class SystemSettingsPage(BaseModel):
    items: list[SystemSettingView]
    pending_changes: list[SystemSettingChangeView] = Field(default_factory=list)
    recent_changes: list[SystemSettingChangeView] = Field(default_factory=list)


class SystemSettingChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    value: Any
    reason: str = Field(min_length=1, max_length=500)


class SystemSettingPathChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    reason: str = Field(min_length=1, max_length=500)


class SystemSettingDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=500)


class ModelPricingView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=120)
    input_usd_per_million: float = Field(ge=0)
    output_usd_per_million: float = Field(ge=0)


class ModelConfigIssueView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str


class ModelConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["database", "environment", "default"]
    base_url: str
    api_key_env: str
    api_key_configured: bool
    planner_model: str
    run_agent_model: str
    pricing: list[ModelPricingView]
    ready: bool
    issues: list[ModelConfigIssueView]
    updated_at: datetime | None = None


class ModelConfigProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=120)


class ModelConfigProbeView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    model: str
    latency_ms: int = Field(ge=0)
    error_code: str
    message: str


def create_operations_router(
    *,
    notification_service: NotificationService | None,
    auth_sessions: AuthSessionService | None,
    audit_repository: AuditRepository | None = None,
    quota_policy_service: QuotaPolicyService | None = None,
    system_setting_service: SystemSettingService | None = None,
    model_config_service: "ModelConfigService | None" = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Operations & Audit"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)

    def actor_for(credentials, permission: str, *, organization_id: UUID | None = None) -> UUID:
        if notification_service is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Operations service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if principal.get("credential_type") == "api_token":
            try:
                require_permission(
                    principal, permission,
                    organization_id=(
                        organization_id
                        if organization_id is not None
                        else principal.get("organization_id")
                    ),
                )
            except MissingPermission as exc:
                raise HTTPException(status_code=403, detail="API token scope denied") from exc
        try:
            return UUID(principal["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid authenticated principal") from exc

    def token_organization(credentials, requested: UUID | None = None) -> UUID | None:
        if requested is not None or credentials is None or auth_sessions is None:
            return requested
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None or principal.get("credential_type") != "api_token":
            return None
        try:
            return UUID(str(principal["organization_id"]))
        except (KeyError, TypeError, ValueError):
            return None

    @router.get(
        "/notifications",
        response_model=NotificationPage,
        operation_id="get__api_v1_notifications",
    )
    def list_notifications(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "resource:read")
        try:
            return notification_service.list_notifications(actor_id, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid notification cursor or limit") from exc

    @router.post(
        "/notifications/{id}/read",
        response_model=MarkNotificationReadView,
        operation_id="post__api_v1_notifications__id__read",
    )
    def mark_notification_read(
        id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "resource:write")
        try:
            notification_service.mark_read(actor_id, id, now=datetime.now(timezone.utc))
        except NotificationNotFound as exc:
            raise HTTPException(status_code=404, detail="Notification not found") from exc
        except NotificationForbidden as exc:
            raise HTTPException(status_code=403, detail="Notification belongs to another user") from exc
        return {"id": id, "read": True}

    @router.get(
        "/notifications/preferences",
        response_model=NotificationPreferencePage,
        operation_id="get__api_v1_notifications_preferences",
    )
    def get_notification_preferences(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "resource:read")
        return notification_service.list_preferences(actor_id)

    @router.put(
        "/notifications/preferences",
        response_model=NotificationPreferencePage,
        operation_id="put__api_v1_notifications_preferences",
    )
    def update_notification_preferences(
        payload: NotificationPreferenceUpdate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "resource:write")
        try:
            return notification_service.update_preferences(
                actor_id, [item.model_dump() for item in payload.items],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid notification preferences") from exc

    @router.get(
        "/notification-deliveries",
        response_model=DeadNotificationDeliveryPage,
        operation_id="get__api_v1_notification_deliveries",
    )
    def list_notification_deliveries(
        status: Literal["DEAD"] = Query(default="DEAD"),
        organization_id: UUID | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "resource:read", organization_id=organization_id)
        try:
            return notification_service.list_dead_deliveries(
                actor_id, organization_id=token_organization(credentials, organization_id), limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid delivery list limit") from exc

    def audit_list(actor_id, organization_id, *, filters, limit, cursor):
        if audit_repository is None:
            raise HTTPException(status_code=503, detail="Audit service is not configured")
        try:
            return audit_repository.list(
                actor_id, organization_id, filters=filters, limit=limit, cursor=cursor,
            )
        except AuditForbidden as exc:
            raise HTTPException(status_code=403, detail="Organization audit scope denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid audit query") from exc

    def quota_policy(actor_id, organization_id, *, write, payload=None):
        if quota_policy_service is None:
            raise HTTPException(status_code=503, detail="Quota policy service is not configured")
        try:
            if write:
                return quota_policy_service.update_policy(
                    actor_id, organization_id,
                    max_concurrent_runs=payload.max_concurrent_runs,
                    monthly_token_limit=payload.monthly_token_limit,
                    now=datetime.now(timezone.utc),
                )
            return quota_policy_service.get_policy(
                actor_id, organization_id, now=datetime.now(timezone.utc),
            )
        except QuotaPolicyForbidden as exc:
            raise HTTPException(status_code=403, detail="Organization quota scope denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid quota policy") from exc

    def usage_page(actor_id, organization_id, *, date_from, date_to):
        if quota_policy_service is None:
            raise HTTPException(status_code=503, detail="Usage service is not configured")
        try:
            return quota_policy_service.list_usage(
                actor_id, organization_id, date_from=date_from, date_to=date_to,
            )
        except QuotaPolicyForbidden as exc:
            raise HTTPException(status_code=403, detail="Organization usage scope denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid usage date range") from exc

    def platform_settings(actor_id):
        if system_setting_service is None:
            raise HTTPException(status_code=503, detail="System settings service is not configured")
        try:
            return system_setting_service.list_settings(actor_id)
        except SettingForbidden as exc:
            raise HTTPException(status_code=403, detail="Platform administrator access required") from exc

    def request_setting(actor_id, key, payload):
        if system_setting_service is None:
            raise HTTPException(status_code=503, detail="System settings service is not configured")
        try:
            return system_setting_service.request_change(
                actor_id, key, value=payload.value, reason=payload.reason,
                now=datetime.now(timezone.utc),
            )
        except SettingForbidden as exc:
            raise HTTPException(status_code=403, detail="Platform administrator access required") from exc
        except SettingConflict as exc:
            raise HTTPException(status_code=409, detail="A change for this key is already pending") from exc
        except SettingInvalid as exc:
            raise HTTPException(status_code=422, detail="Invalid system setting") from exc

    @router.get(
        "/organizations/{id}/audit-logs",
        response_model=AuditPage,
        operation_id="get__api_v1_organizations__id__audit_logs",
    )
    def list_organization_audit_logs(
        id: UUID,
        action: str | None = Query(default=None, max_length=160),
        resource_type: str | None = Query(default=None, max_length=160),
        actor_user_id: UUID | None = Query(default=None),
        resource_id: UUID | None = Query(default=None),
        created_at_from: datetime | None = Query(default=None),
        created_at_to: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=64),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:read", organization_id=id)
        return audit_list(actor_id, id, filters={
            "action": action, "resource_type": resource_type,
            "actor_user_id": actor_user_id, "resource_id": resource_id,
            "created_at_from": created_at_from, "created_at_to": created_at_to,
        }, limit=limit, cursor=cursor)

    @router.get(
        "/audit-logs",
        response_model=AuditPage,
        operation_id="get__api_v1_audit_logs",
    )
    def list_audit_logs(
        organization_id: UUID = Query(...),
        action: str | None = Query(default=None, max_length=160),
        resource_type: str | None = Query(default=None, max_length=160),
        actor_user_id: UUID | None = Query(default=None),
        resource_id: UUID | None = Query(default=None),
        created_at_from: datetime | None = Query(default=None),
        created_at_to: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=64),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:read", organization_id=organization_id)
        return audit_list(actor_id, organization_id, filters={
            "action": action, "resource_type": resource_type,
            "actor_user_id": actor_user_id, "resource_id": resource_id,
            "created_at_from": created_at_from, "created_at_to": created_at_to,
        }, limit=limit, cursor=cursor)

    @router.get(
        "/audit-logs/verify",
        response_model=AuditVerificationView,
        operation_id="get__api_v1_audit_logs_verify",
    )
    def verify_audit_chain(
        organization_id: UUID = Query(...),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if audit_repository is None:
            raise HTTPException(status_code=503, detail="Audit service is not configured")
        actor_id = actor_for(credentials, "organization:read", organization_id=organization_id)
        try:
            result = audit_repository.verify_chain(organization_id, actor_id=actor_id)
            return result.__dict__
        except AuditForbidden as exc:
            raise HTTPException(status_code=403, detail="Organization audit scope denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Organization scope is required") from exc

    @router.post(
        "/audit-logs/export",
        response_model=AuditExportView,
        operation_id="post__api_v1_audit_logs_export",
    )
    def export_audit_logs(
        payload: AuditExportRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if audit_repository is None:
            raise HTTPException(status_code=503, detail="Audit service is not configured")
        actor_id = actor_for(credentials, "resource:write", organization_id=payload.organization_id)
        try:
            return audit_repository.export(
                actor_id, payload.organization_id, filters=payload.filters,
                limit=payload.limit, cursor=payload.cursor,
            )
        except AuditForbidden as exc:
            raise HTTPException(status_code=403, detail="Organization audit scope denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid audit export query") from exc

    @router.get(
        "/organizations/{id}/quotas",
        response_model=QuotaPolicyView,
        operation_id="get__api_v1_organizations__id__quotas",
    )
    def get_organization_quotas(
        id: UUID, credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:read", organization_id=id)
        return quota_policy(actor_id, id, write=False)

    @router.put(
        "/organizations/{id}/quotas",
        response_model=QuotaPolicyView,
        operation_id="put__api_v1_organizations__id__quotas",
    )
    def put_organization_quotas(
        id: UUID, payload: QuotaPolicyUpdate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:write", organization_id=id)
        return quota_policy(actor_id, id, write=True, payload=payload)

    @router.get(
        "/quota-policies",
        response_model=QuotaPolicyView,
        operation_id="get__api_v1_quota_policies",
    )
    def get_quota_policies(
        organization_id: UUID = Query(...),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:read", organization_id=organization_id)
        return quota_policy(actor_id, organization_id, write=False)

    @router.put(
        "/quota-policies",
        response_model=QuotaPolicyView,
        operation_id="put__api_v1_quota_policies",
    )
    def put_quota_policies(
        payload: QuotaPolicyUpdate,
        organization_id: UUID = Query(...),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:write", organization_id=organization_id)
        return quota_policy(actor_id, organization_id, write=True, payload=payload)

    @router.get(
        "/organizations/{id}/usage",
        response_model=UsagePage,
        operation_id="get__api_v1_organizations__id__usage",
    )
    def get_organization_usage(
        id: UUID,
        date_from: datetime | None = Query(default=None),
        date_to: datetime | None = Query(default=None),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:read", organization_id=id)
        return usage_page(actor_id, id, date_from=date_from, date_to=date_to)

    @router.get(
        "/usage",
        response_model=UsagePage,
        operation_id="get__api_v1_usage",
    )
    def get_usage(
        organization_id: UUID = Query(...),
        date_from: datetime | None = Query(default=None),
        date_to: datetime | None = Query(default=None),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "organization:read", organization_id=organization_id)
        return usage_page(actor_id, organization_id, date_from=date_from, date_to=date_to)

    @router.get(
        "/admin/system-settings",
        response_model=SystemSettingsPage,
        operation_id="get__api_v1_admin_system_settings",
    )
    def get_admin_system_settings(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "platform:admin")
        return platform_settings(actor_id)

    @router.get(
        "/system-settings",
        response_model=SystemSettingsPage,
        operation_id="get__api_v1_system_settings",
    )
    def get_system_settings(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "platform:admin")
        return platform_settings(actor_id)

    @router.put(
        "/admin/system-settings/{key}",
        response_model=SystemSettingChangeView,
        operation_id="put__api_v1_admin_system_settings__key",
    )
    def put_admin_system_setting(
        key: str, payload: SystemSettingPathChangeRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "platform:admin")
        return request_setting(actor_id, key, payload)

    @router.put(
        "/system-settings",
        response_model=SystemSettingChangeView,
        operation_id="put__api_v1_system_settings",
    )
    def put_system_setting(
        payload: SystemSettingChangeRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "platform:admin")
        return request_setting(actor_id, payload.key, payload)

    @router.post(
        "/admin/system-setting-changes/{id}/decision",
        response_model=SystemSettingChangeView,
        operation_id="post__api_v1_admin_system_setting_changes__id__decision",
    )
    def decide_admin_system_setting_change(
        id: UUID, payload: SystemSettingDecisionRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        if system_setting_service is None:
            raise HTTPException(status_code=503, detail="System settings service is not configured")
        actor_id = actor_for(credentials, "platform:admin")
        try:
            return system_setting_service.decide_change(
                actor_id, id, decision=payload.decision, reason=payload.reason,
                now=datetime.now(timezone.utc),
            )
        except SettingForbidden as exc:
            raise HTTPException(status_code=403, detail="A different platform administrator must decide") from exc
        except SettingNotFound as exc:
            raise HTTPException(status_code=404, detail="Setting change not found") from exc
        except SettingConflict as exc:
            raise HTTPException(status_code=409, detail="Setting change is no longer pending") from exc
        except SettingStale as exc:
            raise HTTPException(status_code=409, detail="Current setting changed; proposal is stale") from exc
        except SettingInvalid as exc:
            raise HTTPException(status_code=422, detail="Invalid system setting decision") from exc

    @router.get(
        "/admin/model-config",
        response_model=ModelConfigView,
        operation_id="get__api_v1_admin_model_config",
    )
    def get_admin_model_config(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "platform:admin")
        if model_config_service is None:
            raise HTTPException(status_code=503, detail="Model configuration service is not configured")
        try:
            return model_config_service.view(actor_id)
        except SettingForbidden as exc:
            raise HTTPException(status_code=403, detail="Platform administrator access required") from exc

    @router.post(
        "/admin/model-config/test",
        response_model=ModelConfigProbeView,
        operation_id="post__api_v1_admin_model_config_test",
    )
    def test_admin_model_config(
        payload: ModelConfigProbeRequest,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = actor_for(credentials, "platform:admin")
        if model_config_service is None:
            raise HTTPException(status_code=503, detail="Model configuration service is not configured")
        try:
            return model_config_service.probe(actor_id, model=payload.model)
        except SettingForbidden as exc:
            raise HTTPException(status_code=403, detail="Platform administrator access required") from exc

    return router
