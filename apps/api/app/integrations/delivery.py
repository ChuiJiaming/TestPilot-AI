"""Bounded, tenant-scoped outbound Webhook delivery and retry state."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Uuid, func, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.lifecycle import SecretRefRecord
from apps.api.app.auth.persistence import AuthBase
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.integrations.persistence import (
    IntegrationNotFound,
    IntegrationRecord,
    WebhookDeliveryRecord,
)
from apps.api.app.integrations.secrets import (
    SecretProviderUnavailable,
    SecretRefResolver,
)


class DeliveryAttemptRecord(AuthBase):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        Index(
            "uq_webhook_delivery_attempt",
            "delivery_type", "delivery_id", "attempt", unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    delivery_type: Mapped[str] = mapped_column(String, nullable=False)
    delivery_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    error_text: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class DeliveryResponse:
    status_code: int | None
    error_code: str | None = None


class WebhookDestinationRejected(ValueError):
    """Raised when a SecretRef destination does not match a fixed provider host."""


class DeliveryRepository:
    MAX_ATTEMPTS = 8

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def enqueue_delivery(self, integration_id: UUID, *, event_id: str, event: dict,
                         now: datetime) -> dict:
        from apps.api.app.integrations.webhooks import _normalized_payload

        normalized_event = _normalized_payload(event)
        if not isinstance(normalized_event, dict) or not event_id.strip():
            raise ValueError("a delivery event ID and object payload are required")
        outbound_event_id = f"outbound:{event_id.strip()}"[:255]
        with self.session_factory.begin() as session:
            integration = session.get(IntegrationRecord, integration_id, with_for_update=True)
            if (integration is None or not integration.enabled
                    or integration.secret_ref_id is None):
                raise IntegrationNotFound
            secret_ref = session.get(SecretRefRecord, integration.secret_ref_id)
            if secret_ref is None or secret_ref.organization_id != integration.organization_id:
                raise IntegrationNotFound

            existing = session.scalar(select(WebhookDeliveryRecord).where(
                WebhookDeliveryRecord.integration_id == integration_id,
                WebhookDeliveryRecord.event_id == outbound_event_id,
                WebhookDeliveryRecord.request_nonce.is_(None),
            ).with_for_update())
            if existing is not None:
                return self._dto(existing)

            delivery = WebhookDeliveryRecord(
                id=uuid4(), integration_id=integration_id, event_id=outbound_event_id,
                attempts=0, status="PENDING", response_code=None,
                next_attempt_at=now, created_at=now, request_nonce=None,
                signed_at=None, signature_version=1,
            )
            session.add(delivery)
            session.flush()
            session.add(OutboxRecord(
                aggregate_type="webhook_delivery", aggregate_id=delivery.id,
                event_type="WEBHOOK_DELIVERY_REQUESTED",
                payload={
                    "delivery_id": str(delivery.id),
                    "integration_id": str(integration_id),
                    "event": normalized_event,
                },
                status="PENDING", attempts=0, available_at=now, created_at=now,
            ))
            return self._dto(delivery)

    def claim(self, delivery_id: UUID, *, worker_id: str, now: datetime,
              lease_seconds: int = 30) -> dict | None:
        if not worker_id.strip() or not 1 <= lease_seconds <= 300:
            raise ValueError("worker ID and bounded lease are required")
        with self.session_factory.begin() as session:
            delivery = session.get(WebhookDeliveryRecord, delivery_id, with_for_update=True)
            if (delivery is None or delivery.request_nonce is not None
                    or delivery.status != "PENDING"
                    or (delivery.next_attempt_at is not None
                        and self._as_utc(delivery.next_attempt_at) > self._as_utc(now))):
                return None
            outbox = session.scalar(select(OutboxRecord).where(
                OutboxRecord.aggregate_id == delivery_id,
                OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
                OutboxRecord.aggregate_type == "webhook_delivery",
                ((OutboxRecord.status == "PENDING") & (OutboxRecord.available_at <= now))
                | (OutboxRecord.status == "PUBLISHED")
                | ((OutboxRecord.status == "PUBLISHING") & (
                    OutboxRecord.claimed_until.is_(None) | (OutboxRecord.claimed_until <= now)
                )),
            ).with_for_update(skip_locked=True))
            if outbox is None:
                return None
            integration = session.get(IntegrationRecord, delivery.integration_id)
            if integration is None:
                return None
            outbox.status = "PUBLISHING"
            outbox.claimed_by = worker_id
            outbox.claimed_until = now + timedelta(seconds=lease_seconds)
            outbox.attempts += 1
            return {
                "delivery_id": delivery.id,
                "integration": {
                    "id": integration.id,
                    "organization_id": integration.organization_id,
                    "kind": integration.kind,
                    "config": integration.config,
                    "secret_ref_id": integration.secret_ref_id,
                    "enabled": integration.enabled,
                },
                "event": outbox.payload.get("event", {}),
                "worker_id": worker_id,
                "lease_expires_at": outbox.claimed_until,
            }

    def requeue_due_deliveries(self, *, now: datetime, limit: int = 100,
                               recovery_after_seconds: int = 60) -> int:
        """Recover dispatched tasks that never claimed, plus expired worker leases."""
        if not 1 <= limit <= 1000 or not 1 <= recovery_after_seconds <= 3600:
            raise ValueError("recovery limit and age must be bounded")
        now_utc = self._as_utc(now)
        recovery_cutoff = now_utc - timedelta(seconds=recovery_after_seconds)
        with self.session_factory.begin() as session:
            rows = session.scalars(select(OutboxRecord).where(
                OutboxRecord.aggregate_type == "webhook_delivery",
                OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
                or_(
                    (OutboxRecord.status == "PUBLISHED")
                    & OutboxRecord.published_at.is_not(None)
                    & (OutboxRecord.published_at <= recovery_cutoff),
                    (OutboxRecord.status == "PUBLISHING")
                    & (OutboxRecord.claimed_until.is_(None)
                       | (OutboxRecord.claimed_until <= now_utc)),
                ),
            ).order_by(OutboxRecord.available_at, OutboxRecord.id)
              .limit(limit).with_for_update(skip_locked=True)).all()
            recovered = 0
            for outbox in rows:
                delivery = session.get(
                    WebhookDeliveryRecord, outbox.aggregate_id, with_for_update=True,
                )
                if (delivery is None or delivery.request_nonce is not None
                        or delivery.status != "PENDING"):
                    continue
                due_at = self._as_utc(delivery.next_attempt_at) if delivery.next_attempt_at else now_utc
                if due_at > now_utc:
                    continue
                outbox.status = "PENDING"
                outbox.available_at = now_utc
                outbox.claimed_by = None
                outbox.claimed_until = None
                outbox.published_at = None
                outbox.last_error = "DELIVERY_DISPATCH_RECOVERED"
                recovered += 1
            return recovered

    def record_attempt(self, delivery_id: UUID, *, worker_id: str,
                       response_code: int | None, error_code: str | None,
                       now: datetime) -> dict | None:
        with self.session_factory.begin() as session:
            delivery = session.get(WebhookDeliveryRecord, delivery_id, with_for_update=True)
            outbox = session.scalar(select(OutboxRecord).where(
                OutboxRecord.aggregate_id == delivery_id,
                OutboxRecord.aggregate_type == "webhook_delivery",
                OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
            ).with_for_update())
            if (delivery is None or outbox is None or outbox.status != "PUBLISHING"
                    or outbox.claimed_by != worker_id or outbox.claimed_until is None
                    or self._as_utc(outbox.claimed_until) <= self._as_utc(now)):
                return None

            delivery.attempts += 1
            delivery.response_code = response_code if (
                isinstance(response_code, int) and 100 <= response_code <= 599
            ) else None
            safe_error_code = self._safe_error_code(error_code)
            previous_attempt = session.scalar(select(func.max(
                DeliveryAttemptRecord.attempt
            )).where(
                DeliveryAttemptRecord.delivery_type == "webhook",
                DeliveryAttemptRecord.delivery_id == delivery_id,
            )) or 0
            if response_code is not None and 200 <= response_code < 300:
                delivery.status = "SUCCEEDED"
                delivery.next_attempt_at = None
                outbox.status = "PUBLISHED"
                outbox.published_at = now
                outbox.last_error = None
                attempt_status = "SUCCESS"
            else:
                if response_code is not None and 300 <= response_code < 400:
                    safe_error_code = safe_error_code or "REDIRECT_REJECTED"
                outcome = self.retry_or_dead(
                    session, delivery=delivery, outbox=outbox,
                    response_code=response_code, error_code=safe_error_code, now=now,
                )
                attempt_status = "RETRY" if outcome == "RETRY" else "DEAD"

            session.add(DeliveryAttemptRecord(
                id=None, delivery_type="webhook", delivery_id=delivery_id,
                attempt=previous_attempt + 1, status=attempt_status,
                response_status=delivery.response_code, error_text=safe_error_code,
                created_at=now,
            ))
            outbox.claimed_by = None
            outbox.claimed_until = None
            return self._dto(delivery)

    def retry_or_dead(self, session, *, delivery: WebhookDeliveryRecord,
                      outbox: OutboxRecord, response_code: int | None,
                      error_code: str | None, now: datetime) -> str:
        """Apply the automatic retry/dead-letter transition in the current transaction."""
        if response_code is None or response_code == 429 or response_code >= 500:
            if delivery.attempts >= self.MAX_ATTEMPTS:
                delivery.status = "DEAD"
                delivery.next_attempt_at = None
                outbox.status = "DEAD"
                outbox.last_error = self._safe_error_code(error_code)
                return "DEAD"
            delay = min(300, 2 ** delivery.attempts)
            delivery.status = "PENDING"
            delivery.next_attempt_at = now + timedelta(seconds=delay)
            outbox.status = "PENDING"
            outbox.available_at = delivery.next_attempt_at
            outbox.last_error = self._safe_error_code(error_code)
            return "RETRY"
        delivery.status = "FAILED"
        delivery.next_attempt_at = None
        outbox.status = "DEAD"
        outbox.last_error = self._safe_error_code(error_code)
        return "FAILED"

    def retry_delivery(self, actor_id: UUID, integration_id: UUID, delivery_id: UUID,
                       *, now: datetime) -> dict:
        from apps.api.app.integrations.persistence import IntegrationRepository

        with self.session_factory.begin() as session:
            integration = session.get(IntegrationRecord, integration_id, with_for_update=True)
            if integration is None:
                raise IntegrationNotFound
            IntegrationRepository._membership(
                session, actor_id, integration.organization_id, write=True
            )
            delivery = session.scalar(select(WebhookDeliveryRecord).where(
                WebhookDeliveryRecord.id == delivery_id,
                WebhookDeliveryRecord.integration_id == integration_id,
                WebhookDeliveryRecord.request_nonce.is_(None),
            ).with_for_update())
            if delivery is None:
                raise IntegrationNotFound
            if delivery.status == "PENDING":
                return {**self._dto(delivery), "queued": True}
            if delivery.status not in {"FAILED", "DEAD"}:
                return {**self._dto(delivery), "queued": False}
            outbox = session.scalar(select(OutboxRecord).where(
                OutboxRecord.aggregate_id == delivery_id,
                OutboxRecord.aggregate_type == "webhook_delivery",
                OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
            ).with_for_update())
            if outbox is None:
                raise IntegrationNotFound
            delivery.status = "PENDING"
            delivery.attempts = 0
            delivery.response_code = None
            delivery.next_attempt_at = now
            outbox.status = "PENDING"
            outbox.available_at = now
            outbox.attempts = 0
            outbox.claimed_by = None
            outbox.claimed_until = None
            outbox.published_at = None
            outbox.last_error = None
            return {**self._dto(delivery), "queued": True}

    def list_deliveries(self, actor_id: UUID, integration_id: UUID, *,
                        limit: int = 30, cursor: str | None = None) -> dict:
        from apps.api.app.integrations.persistence import IntegrationRepository

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        cursor_id = self._cursor_id(cursor)
        with self.session_factory() as session:
            integration = session.get(IntegrationRecord, integration_id)
            if integration is None:
                raise IntegrationNotFound
            IntegrationRepository._membership(
                session, actor_id, integration.organization_id, write=False
            )
            statement = select(WebhookDeliveryRecord).where(
                WebhookDeliveryRecord.integration_id == integration_id,
                WebhookDeliveryRecord.request_nonce.is_(None),
            )
            if cursor_id is not None:
                statement = statement.where(WebhookDeliveryRecord.id < cursor_id)
            rows = session.scalars(
                statement.order_by(
                    WebhookDeliveryRecord.created_at.desc(),
                    WebhookDeliveryRecord.id.desc(),
                ).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            ids = [row.id for row in rows]
            attempt_rows = session.scalars(select(DeliveryAttemptRecord).where(
                DeliveryAttemptRecord.delivery_type == "webhook",
                DeliveryAttemptRecord.delivery_id.in_(ids),
            ).order_by(DeliveryAttemptRecord.attempt)).all() if ids else []
            attempts_by_delivery: dict[UUID, list[dict]] = {item: [] for item in ids}
            for attempt in attempt_rows:
                attempts_by_delivery[attempt.delivery_id].append({
                    "attempt": attempt.attempt,
                    "status": attempt.status,
                    "response_status": attempt.response_status,
                    "error_code": attempt.error_text,
                    "created_at": attempt.created_at,
                })
            items = [
                {**self._dto(row), "attempt_history": attempts_by_delivery[row.id]}
                for row in rows
            ]
            return {
                "items": items,
                "next_cursor": str(rows[-1].id) if has_more and rows else None,
            }

    @staticmethod
    def _safe_error_code(value: str | None) -> str | None:
        if value is None:
            return None
        return value[:120] if re.fullmatch(r"[A-Z0-9_:-]{1,120}", value) else "DELIVERY_FAILED"

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @staticmethod
    def _cursor_id(cursor: str | None) -> UUID | None:
        if cursor is None:
            return None
        try:
            return UUID(cursor)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid delivery cursor") from exc

    @staticmethod
    def _dto(record: WebhookDeliveryRecord) -> dict:
        return {
            "id": record.id,
            "integration_id": record.integration_id,
            "event_id": record.event_id,
            "status": record.status,
            "attempts": record.attempts,
            "response_code": record.response_code,
            "next_attempt_at": record.next_attempt_at,
            "created_at": record.created_at,
        }


class FixedHostWebhookSender:
    _PROVIDERS = {
        "slack": ("hooks.slack.com", "/services/"),
        "discord": ("discord.com", "/api/webhooks/"),
    }

    def __init__(self, secret_resolver: SecretRefResolver, *,
                 http_transport: httpx.BaseTransport | None = None,
                 timeout_seconds: float = 5.0):
        if not 0.1 <= timeout_seconds <= 10:
            raise ValueError("webhook timeout must be between 0.1 and 10 seconds")
        self.secret_resolver = secret_resolver
        self.http_transport = http_transport
        self.timeout_seconds = timeout_seconds

    def send(self, integration: dict, event: dict) -> DeliveryResponse:
        secret_ref_id = integration.get("secret_ref_id")
        organization_id = integration.get("organization_id")
        if not isinstance(secret_ref_id, UUID) or not isinstance(organization_id, UUID):
            raise SecretProviderUnavailable("SECRET_PROVIDER_UNAVAILABLE")
        endpoint_bytes = self.secret_resolver.resolve(secret_ref_id, organization_id)
        if not isinstance(endpoint_bytes, bytes) or not endpoint_bytes:
            raise SecretProviderUnavailable("SECRET_PROVIDER_UNAVAILABLE")
        try:
            endpoint = endpoint_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebhookDestinationRejected("provider endpoint is invalid") from exc
        self._validate_endpoint(str(integration.get("kind", "")), endpoint)

        try:
            with httpx.Client(
                transport=self.http_transport,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(endpoint, json=event)
        except httpx.TimeoutException:
            return DeliveryResponse(status_code=None, error_code="NETWORK_TIMEOUT")
        except httpx.RequestError:
            return DeliveryResponse(status_code=None, error_code="NETWORK_ERROR")
        if 300 <= response.status_code < 400:
            return DeliveryResponse(
                status_code=response.status_code, error_code="REDIRECT_REJECTED"
            )
        if response.status_code >= 400:
            return DeliveryResponse(
                status_code=response.status_code,
                error_code=f"HTTP_{response.status_code}",
            )
        return DeliveryResponse(status_code=response.status_code)

    @classmethod
    def _validate_endpoint(cls, provider: str, endpoint: str) -> None:
        allowed = cls._PROVIDERS.get(provider.casefold())
        if allowed is None:
            raise WebhookDestinationRejected("provider is not supported")
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise WebhookDestinationRejected("provider endpoint is invalid") from exc
        allowed_host, required_path_prefix = allowed
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != allowed_host
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(required_path_prefix)
            or len(parsed.path) > 2048
        ):
            raise WebhookDestinationRejected("provider endpoint is not allowlisted")


def process_webhook_delivery(repository: DeliveryRepository, sender,
                             delivery_id: UUID, *, worker_id: str,
                             now: datetime) -> dict:
    claim = repository.claim(delivery_id, worker_id=worker_id, now=now)
    if claim is None:
        return {"claimed": False, "status": "UNCHANGED"}
    try:
        response = sender.send(claim["integration"], claim["event"])
    except WebhookDestinationRejected:
        response = DeliveryResponse(status_code=400, error_code="ENDPOINT_NOT_ALLOWED")
    except SecretProviderUnavailable:
        response = DeliveryResponse(status_code=503, error_code="SECRET_PROVIDER_UNAVAILABLE")
    except TimeoutError:
        response = DeliveryResponse(status_code=None, error_code="NETWORK_TIMEOUT")
    except Exception:
        response = DeliveryResponse(status_code=503, error_code="PROVIDER_TRANSPORT_ERROR")
    result = repository.record_attempt(
        delivery_id,
        worker_id=worker_id,
        response_code=response.status_code,
        error_code=response.error_code,
        now=now,
    )
    return {"claimed": result is not None, "status": result["status"] if result else "STALE"}
