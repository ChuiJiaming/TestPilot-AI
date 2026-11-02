"""Baseline-compatible and V10.3 integration management routes."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.auth.permissions import MissingPermission, require_permission
from apps.api.app.core.errors import ProblemDetails
from apps.api.app.integrations.persistence import (
    IntegrationForbidden,
    IntegrationNotFound,
    IntegrationRepository,
    WebhookIntegrationAmbiguous,
    WebhookNonceReplay,
)
from apps.api.app.integrations.delivery import DeliveryRepository
from apps.api.app.integrations.secrets import (
    SecretProviderUnavailable,
    SecretRefResolver,
)
from apps.api.app.integrations.webhooks import (
    MAX_WEBHOOK_BODY_BYTES,
    WebhookVerificationError,
    verify_inbound_webhook,
)


class LegacyIntegrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)


class IntegrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: UUID
    kind: str = Field(min_length=1, max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref_id: UUID | None = None
    enabled: bool = True


class IntegrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any] | None = None
    secret_ref_id: UUID | None = None
    enabled: bool | None = None


class IntegrationCompatView(BaseModel):
    id: UUID
    organization_id: UUID
    kind: str
    enabled: bool


class IntegrationView(IntegrationCompatView):
    config: dict[str, Any]
    secret_ref_id: UUID | None
    created_at: datetime


class IntegrationPage(BaseModel):
    items: list[IntegrationView]
    next_cursor: str | None


class IntegrationTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=120)


class WebhookReceiptView(BaseModel):
    receipt_id: UUID
    status: Literal["accepted"]
    duplicate: bool


class DeliveryAttemptView(BaseModel):
    attempt: int
    status: Literal["SUCCESS", "RETRY", "DEAD"]
    response_status: int | None
    error_code: str | None
    created_at: datetime


class WebhookDeliveryView(BaseModel):
    id: UUID
    integration_id: UUID
    event_id: str
    status: Literal["PENDING", "SUCCEEDED", "FAILED", "DEAD"]
    attempts: int
    response_code: int | None
    next_attempt_at: datetime | None
    created_at: datetime
    attempt_history: list[DeliveryAttemptView] = Field(default_factory=list)


class WebhookDeliveryPage(BaseModel):
    items: list[WebhookDeliveryView]
    next_cursor: str | None


class WebhookRetryView(WebhookDeliveryView):
    queued: bool


def create_integration_router(
    *,
    integration_repository: IntegrationRepository | None,
    auth_sessions,
    secret_ref_resolver: SecretRefResolver | None = None,
    delivery_repository: DeliveryRepository | None = None,
):
    router = APIRouter(prefix="/api/v1", tags=["Integrations & Operations"])
    bearer = HTTPBearer(scheme_name="BearerAuth", auto_error=False)
    deliveries = delivery_repository or (
        DeliveryRepository(integration_repository.session_factory)
        if integration_repository is not None else None
    )

    def actor(credentials, *, permission: str, organization_id: UUID):
        if integration_repository is None or auth_sessions is None:
            raise HTTPException(status_code=503, detail="Integration service is not configured")
        if credentials is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        principal = auth_sessions.authenticate(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if principal.get("credential_type") == "api_token":
            try:
                require_permission(
                    principal, permission, organization_id=organization_id
                )
            except MissingPermission as exc:
                raise HTTPException(
                    status_code=403,
                    detail="API token scope or organization boundary denied",
                ) from exc
        return principal

    def call_repository(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except IntegrationNotFound as exc:
            raise HTTPException(status_code=404, detail="Integration not found or scoped out") from exc
        except IntegrationForbidden as exc:
            raise HTTPException(status_code=403, detail="Organization administrator role required") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def organization_for_integration(integration_id: UUID) -> UUID:
        if integration_repository is None:
            raise HTTPException(status_code=503, detail="Integration service is not configured")
        organization_id = integration_repository.organization_id_for_integration(integration_id)
        if organization_id is None:
            raise HTTPException(status_code=404, detail="Integration not found or scoped out")
        return organization_id

    def delivery_actor(credentials, integration_id: UUID, permission: str):
        if deliveries is None or integration_repository is None:
            raise HTTPException(status_code=503, detail="Delivery service is not configured")
        organization_id = organization_for_integration(integration_id)
        principal = actor(credentials, permission=permission, organization_id=organization_id)
        return UUID(principal["id"])

    def compatibility_view(record: dict):
        return {
            "id": record["id"],
            "organization_id": record["organization_id"],
            "kind": record["kind"],
            "enabled": record["enabled"],
        }

    def create_record(actor_id, organization_id, payload, *, status_compatible=False):
        record = call_repository(
            integration_repository.create,
            actor_id,
            organization_id,
            kind=payload.kind,
            config=payload.config,
            secret_ref_id=getattr(payload, "secret_ref_id", None),
            enabled=getattr(payload, "enabled", True),
            now=datetime.now(timezone.utc),
        )
        return compatibility_view(record) if status_compatible else record

    @router.post(
        "/organizations/{id}/integrations",
        response_model=IntegrationCompatView,
        operation_id="post__api_v1_organizations__id__integrations",
    )
    def create_organization_integration(
        id: UUID,
        payload: LegacyIntegrationCreate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal = actor(credentials, permission="organization:write", organization_id=id)
        return create_record(
            UUID(principal["id"]), id, payload, status_compatible=True
        )

    @router.get(
        "/organizations/{id}/integrations",
        response_model=IntegrationPage,
        operation_id="get__api_v1_organizations__id__integrations",
    )
    def list_organization_integrations(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = None,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal = actor(credentials, permission="organization:read", organization_id=id)
        return call_repository(
            integration_repository.list,
            UUID(principal["id"]), organization_id=id, limit=limit, cursor=cursor,
        )

    @router.get("/integrations", response_model=IntegrationPage,
                operation_id="get__api_v1_integrations")
    def list_integrations(
        organization_id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = None,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal = actor(
            credentials, permission="organization:read", organization_id=organization_id
        )
        return call_repository(
            integration_repository.list,
            UUID(principal["id"]), organization_id=organization_id,
            limit=limit, cursor=cursor,
        )

    @router.post("/integrations", response_model=IntegrationView, status_code=201,
                 operation_id="post__api_v1_integrations")
    def create_integration(
        payload: IntegrationCreate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        principal = actor(
            credentials, permission="organization:write",
            organization_id=payload.organization_id,
        )
        return create_record(UUID(principal["id"]), payload.organization_id, payload)

    @router.patch("/integrations/{id}", response_model=IntegrationView,
                  operation_id="patch__api_v1_integrations__id")
    def update_integration(
        id: UUID,
        payload: IntegrationUpdate,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        organization_id = organization_for_integration(id)
        principal = actor(
            credentials, permission="organization:write", organization_id=organization_id
        )
        changes = payload.model_dump(exclude_unset=True)
        return call_repository(
            integration_repository.update, UUID(principal["id"]), id,
            changes=changes, now=datetime.now(timezone.utc),
        )

    @router.post("/integrations/{id}/test", response_model=IntegrationCompatView,
                 operation_id="post__api_v1_integrations__id__test")
    def test_integration(
        id: UUID,
        payload: IntegrationTestRequest | None = Body(default=None),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        organization_id = organization_for_integration(id)
        principal = actor(
            credentials, permission="resource:write", organization_id=organization_id
        )
        record = call_repository(
            integration_repository.get,
            UUID(principal["id"]), id, require_admin=True,
        )
        if record["secret_ref_id"] is not None:
            if secret_ref_resolver is None:
                raise HTTPException(status_code=503, detail="SECRET_PROVIDER_UNAVAILABLE")
            try:
                secret = secret_ref_resolver.resolve(
                    record["secret_ref_id"], record["organization_id"]
                )
                if not isinstance(secret, bytes) or not secret:
                    raise SecretProviderUnavailable("SECRET_PROVIDER_UNAVAILABLE")
                del secret
            except SecretProviderUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="SECRET_PROVIDER_UNAVAILABLE"
                ) from exc
        return compatibility_view(record)

    @router.get(
        "/webhooks/{id}/deliveries",
        response_model=WebhookDeliveryPage,
        operation_id="get__api_v1_webhooks__id__deliveries",
        responses={400: {"model": ProblemDetails}, 403: {"model": ProblemDetails},
                   404: {"model": ProblemDetails}},
    )
    def list_webhook_deliveries(
        id: UUID,
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=128),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = delivery_actor(credentials, id, "organization:read")
        try:
            return deliveries.list_deliveries(
                actor_id, id, limit=limit, cursor=cursor
            )
        except IntegrationNotFound as exc:
            raise HTTPException(status_code=404, detail="Integration not found or scoped out") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid delivery cursor") from exc

    @router.post(
        "/webhooks/{id}/deliveries/{delivery_id}/retry",
        response_model=WebhookRetryView,
        status_code=202,
        operation_id="post__api_v1_webhooks__id__deliveries__delivery_id__retry",
        responses={403: {"model": ProblemDetails}, 404: {"model": ProblemDetails},
                   409: {"model": ProblemDetails}},
    )
    def retry_webhook_delivery(
        id: UUID,
        delivery_id: UUID,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        actor_id = delivery_actor(credentials, id, "organization:write")
        try:
            result = deliveries.retry_delivery(
                actor_id, id, delivery_id, now=datetime.now(timezone.utc)
            )
        except IntegrationNotFound as exc:
            raise HTTPException(status_code=404, detail="Delivery not found or scoped out") from exc
        if not result["queued"]:
            raise HTTPException(status_code=409, detail="Delivery is not retryable")
        return result

    @router.post(
        "/webhooks/{provider}",
        response_model=WebhookReceiptView,
        status_code=202,
        operation_id="post__api_v1_webhooks__provider",
        responses={400: {"description": "Malformed provider payload"},
                   401: {"description": "Signature, timestamp, or nonce invalid"},
                   404: {"description": "Provider integration not found"},
                   409: {"description": "Nonce replay or ambiguous provider"},
                   413: {"description": "Webhook body exceeds 1 MiB"},
                   503: {"description": "Secret provider unavailable"}},
    )
    async def receive_webhook(
        provider: str,
        request: Request,
        x_testpilot_timestamp: str | None = Header(default=None, alias="X-TestPilot-Timestamp"),
        x_testpilot_nonce: str | None = Header(default=None, alias="X-TestPilot-Nonce"),
        x_testpilot_signature: str | None = Header(default=None, alias="X-TestPilot-Signature"),
        x_testpilot_integration_id: UUID | None = Header(
            default=None, alias="X-TestPilot-Integration-Id"
        ),
    ):
        if integration_repository is None:
            raise HTTPException(status_code=503, detail="Integration service is not configured")
        if (not provider or len(provider) > 80
                or any(not (char.isalnum() or char in "-_") for char in provider)):
            raise HTTPException(status_code=404, detail="Provider integration not found")
        if not x_testpilot_timestamp or not x_testpilot_nonce or not x_testpilot_signature:
            raise HTTPException(status_code=401, detail="Webhook signature headers are required")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="WEBHOOK_BODY_TOO_LARGE")
            body.extend(chunk)
        try:
            target = integration_repository.webhook_target(
                provider, integration_id=x_testpilot_integration_id
            )
        except IntegrationNotFound as exc:
            raise HTTPException(status_code=404, detail="Provider integration not found") from exc
        except WebhookIntegrationAmbiguous as exc:
            raise HTTPException(status_code=409, detail="Integration ID is required for this provider") from exc

        if secret_ref_resolver is None:
            raise HTTPException(status_code=503, detail="SECRET_PROVIDER_UNAVAILABLE")
        try:
            secret = secret_ref_resolver.resolve(
                target["secret_ref_id"], target["organization_id"]
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="SECRET_PROVIDER_UNAVAILABLE") from exc
        if not isinstance(secret, bytes) or not secret:
            raise HTTPException(status_code=503, detail="SECRET_PROVIDER_UNAVAILABLE")

        now = datetime.now(timezone.utc)
        try:
            verified = verify_inbound_webhook(
                bytes(body), timestamp=x_testpilot_timestamp,
                nonce=x_testpilot_nonce, signature=x_testpilot_signature,
                secret=secret, now=now,
            )
        except WebhookVerificationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        finally:
            del secret

        try:
            receipt = integration_repository.receive_webhook(
                target["id"], event_id=verified.event_id,
                event_type=verified.event_type, nonce=verified.nonce,
                signed_at=verified.signed_at, payload=verified.payload,
                body_sha256=verified.body_sha256, now=now,
            )
        except WebhookNonceReplay as exc:
            raise HTTPException(status_code=409, detail="WEBHOOK_NONCE_REPLAY") from exc
        except IntegrationNotFound as exc:
            raise HTTPException(status_code=404, detail="Provider integration not found") from exc
        return {
            "receipt_id": receipt["receipt_id"],
            "status": "accepted",
            "duplicate": receipt["duplicate"],
        }

    return router
