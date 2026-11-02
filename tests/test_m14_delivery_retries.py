from datetime import datetime, timedelta, timezone
from itertools import count
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import event, func, select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.lifecycle import SecretRefRecord
from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.integrations.delivery import (
    DeliveryAttemptRecord,
    DeliveryRepository,
    DeliveryResponse,
    FixedHostWebhookSender,
    WebhookDestinationRejected,
    process_webhook_delivery,
)
from apps.api.app.integrations.persistence import (
    IntegrationRepository,
    WebhookDeliveryRecord,
)
from apps.api.app.integrations.routes import create_integration_router
from apps.api.app.runs.persistence import TestRunRecord as RunRecord
from apps.api.app.tenancy.persistence import OrganizationMembershipRecord, OrganizationRecord


class StaticResolver:
    def __init__(self, value):
        self.value = value

    def resolve(self, secret_ref_id, organization_id):
        return self.value


class DeliverySessions:
    def __init__(self, owner_id, outsider_id):
        self.users = {
            "owner": owner_id,
            "outsider": outsider_id,
        }

    def authenticate(self, token):
        user_id = self.users.get(token)
        return {"id": str(user_id), "credential_type": "user"} if user_id else None


@pytest.fixture
def delivery_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ = RunRecord.__table__
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    owner_id, outsider_id, organization_id, secret_ref_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    integration_repository = IntegrationRepository(sessions)
    counters = {OutboxRecord: count(1), DeliveryAttemptRecord: count(1)}

    def assign_bigint(_mapper, _connection, target):
        if target.id is None:
            target.id = next(counters[type(target)])

    for model in counters:
        event.listen(model, "before_insert", assign_bigint)
    with sessions.begin() as session:
        for user_id, email in (
            (owner_id, "delivery-owner@example.test"),
            (outsider_id, "delivery-outsider@example.test"),
        ):
            session.add(UserRecord(
                id=user_id, email=email, password_hash="hash", display_name=email,
                platform_role="user", status="active", created_at=now,
                email_verified_at=now,
            ))
        session.add(OrganizationRecord(
            id=organization_id, name="Delivery Org", slug=f"delivery-{owner_id.hex[:12]}",
            created_at=now,
        ))
        session.add(OrganizationMembershipRecord(
            organization_id=organization_id, user_id=owner_id, role="org_admin",
            created_at=now,
        ))
        session.add(SecretRefRecord(
            id=secret_ref_id, organization_id=organization_id, provider="vault",
            external_key="delivery/slack", rotated_at=None, created_at=now,
        ))
    integration = integration_repository.create(
        owner_id, organization_id, kind="slack", config={"channel": "qa"},
        secret_ref_id=secret_ref_id, enabled=True, now=now,
    )
    repository = DeliveryRepository(sessions)
    data = {
        "engine": engine, "sessions": sessions, "now": now,
        "owner_id": owner_id, "outsider_id": outsider_id,
        "organization_id": organization_id, "integration_id": integration["id"],
        "secret_ref_id": secret_ref_id, "integrations": integration_repository,
        "repository": repository, "sender_sessions": DeliverySessions(owner_id, outsider_id),
        "http_events": [],
    }
    yield data
    for model in counters:
        event.remove(model, "before_insert", assign_bigint)
    engine.dispose()


def enqueue(db, event_id="evt-out-1"):
    return db["repository"].enqueue_delivery(
        db["integration_id"], event_id=event_id,
        event={"event_type": "run.completed", "payload": {"run_id": "run-1"}},
        now=db["now"],
    )


def _attempt(db, delivery_id, *, status_code, offset=0, error_code=None):
    now = db["now"] + timedelta(seconds=offset)
    claim = db["repository"].claim(
        delivery_id, worker_id="delivery-worker", now=now,
    )
    assert claim is not None
    return db["repository"].record_attempt(
        delivery_id, worker_id="delivery-worker", response_code=status_code,
        error_code=error_code, now=now,
    )


@pytest.mark.parametrize("status_code", [429, 503])
def test_429_and_5xx_retry_with_bounded_exponential_backoff(delivery_db, status_code):
    delivery = enqueue(delivery_db)

    result = _attempt(
        delivery_db, delivery["id"], status_code=status_code,
        error_code=f"HTTP_{status_code}",
    )

    assert result["status"] == "PENDING"
    assert result["attempts"] == 1
    assert result["next_attempt_at"] == delivery_db["now"] + timedelta(seconds=2)


def test_permanent_4xx_is_not_retried(delivery_db):
    delivery = enqueue(delivery_db)

    result = _attempt(
        delivery_db, delivery["id"], status_code=400, error_code="HTTP_400"
    )

    assert result["status"] == "FAILED"
    assert result["attempts"] == 1
    assert result["next_attempt_at"] is None


def test_eighth_retryable_failure_becomes_dead_with_attempt_history(delivery_db):
    delivery = enqueue(delivery_db)
    next_offset = 0
    result = None

    for attempt in range(8):
        result = _attempt(
            delivery_db, delivery["id"], status_code=503, offset=next_offset,
            error_code="HTTP_503",
        )
        if attempt < 7:
            next_offset = int((result["next_attempt_at"] - delivery_db["now"]).total_seconds()) + 1

    assert result["status"] == "DEAD"
    assert result["attempts"] == 8
    with delivery_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(DeliveryAttemptRecord).where(
            DeliveryAttemptRecord.delivery_id == delivery["id"]
        )) == 8


def test_duplicate_outbox_delivery_has_one_business_effect(delivery_db):
    first = enqueue(delivery_db)
    duplicate = enqueue(delivery_db)

    assert duplicate["id"] == first["id"]
    with delivery_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRecord).where(
            OutboxRecord.aggregate_id == first["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        )) == 1


def test_manual_retry_reuses_delivery_and_keeps_attempt_history(delivery_db):
    delivery = enqueue(delivery_db)
    next_offset = 0
    for _ in range(8):
        failed = _attempt(
            delivery_db, delivery["id"], status_code=503, offset=next_offset,
            error_code="HTTP_503",
        )
        if failed["status"] == "PENDING":
            next_offset = int((failed["next_attempt_at"] - delivery_db["now"]).total_seconds()) + 1
    retry_time = delivery_db["now"] + timedelta(days=1)

    first = delivery_db["repository"].retry_delivery(
        delivery_db["owner_id"], delivery_db["integration_id"],
        delivery["id"], now=retry_time,
    )
    second = delivery_db["repository"].retry_delivery(
        delivery_db["owner_id"], delivery_db["integration_id"],
        delivery["id"], now=retry_time + timedelta(seconds=1),
    )
    page = delivery_db["repository"].list_deliveries(
        delivery_db["owner_id"], delivery_db["integration_id"],
    )

    assert first["queued"] is True
    assert second["id"] == first["id"]
    assert second["attempts"] == 0
    assert page["items"][0]["attempt_history"] and len(page["items"][0]["attempt_history"]) == 8
    with delivery_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRecord).where(
            OutboxRecord.aggregate_id == delivery["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        )) == 1


def test_injected_sender_completes_a_claimed_delivery(delivery_db):
    delivery = enqueue(delivery_db)

    class Sender:
        def send(self, integration, event):
            assert integration["id"] == delivery_db["integration_id"]
            assert event["event_type"] == "run.completed"
            return DeliveryResponse(status_code=204)

    result = process_webhook_delivery(
        delivery_db["repository"], Sender(), delivery["id"],
        worker_id="delivery-worker", now=delivery_db["now"],
    )

    assert result == {"claimed": True, "status": "SUCCEEDED"}


def test_dispatched_outbox_delivery_can_be_claimed(delivery_db):
    delivery = enqueue(delivery_db)
    with delivery_db["sessions"].begin() as session:
        outbox = session.scalar(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == delivery["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        ))
        outbox.status = "PUBLISHED"
        outbox.published_at = delivery_db["now"]

    class Sender:
        def send(self, integration, event):
            return DeliveryResponse(status_code=204)

    result = process_webhook_delivery(
        delivery_db["repository"], Sender(), delivery["id"],
        worker_id="delivery-worker", now=delivery_db["now"],
    )

    assert result == {"claimed": True, "status": "SUCCEEDED"}


@pytest.mark.parametrize("stale_status", ["PUBLISHED", "PUBLISHING"])
def test_stale_dispatch_or_worker_lease_is_requeued(delivery_db, stale_status):
    delivery = enqueue(delivery_db)
    stale_time = delivery_db["now"] - timedelta(minutes=5)
    with delivery_db["sessions"].begin() as session:
        outbox = session.scalar(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == delivery["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        ))
        outbox.status = stale_status
        outbox.published_at = stale_time if stale_status == "PUBLISHED" else None
        outbox.claimed_by = "abandoned-worker" if stale_status == "PUBLISHING" else None
        outbox.claimed_until = stale_time if stale_status == "PUBLISHING" else None

    recovered = delivery_db["repository"].requeue_due_deliveries(
        now=delivery_db["now"], recovery_after_seconds=60,
    )

    assert recovered == 1
    with delivery_db["sessions"]() as session:
        outbox = session.scalar(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == delivery["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        ))
        assert outbox.status == "PENDING"
        assert outbox.available_at.replace(tzinfo=timezone.utc) == delivery_db["now"]
        assert outbox.claimed_by is None
        assert outbox.claimed_until is None


def test_recovery_does_not_requeue_active_or_completed_delivery(delivery_db):
    active = enqueue(delivery_db, event_id="evt-active")
    completed = enqueue(delivery_db, event_id="evt-completed")
    old = delivery_db["now"] - timedelta(minutes=5)
    with delivery_db["sessions"].begin() as session:
        active_outbox = session.scalar(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == active["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        ))
        active_outbox.status = "PUBLISHING"
        active_outbox.claimed_by = "live-worker"
        active_outbox.claimed_until = delivery_db["now"] + timedelta(seconds=30)
        completed_outbox = session.scalar(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == completed["id"],
            OutboxRecord.event_type == "WEBHOOK_DELIVERY_REQUESTED",
        ))
        completed_outbox.status = "PUBLISHED"
        completed_outbox.published_at = old
        completed_record = session.get(WebhookDeliveryRecord, completed["id"])
        completed_record.status = "SUCCEEDED"

    recovered = delivery_db["repository"].requeue_due_deliveries(
        now=delivery_db["now"], recovery_after_seconds=60,
    )

    assert recovered == 0


def test_redirect_is_not_followed_and_arbitrary_provider_host_is_rejected(delivery_db):
    delivery_db["secret"] = b"https://hooks.slack.com/services/T123/B123/secret-path"
    delivery_db["http_events"].clear()

    def respond(request):
        delivery_db["http_events"].append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    sender = FixedHostWebhookSender(
        StaticResolver(delivery_db["secret"]),
        http_transport=httpx.MockTransport(respond),
    )
    integration = {
        "id": delivery_db["integration_id"], "organization_id": delivery_db["organization_id"],
        "kind": "slack", "config": {"channel": "qa"},
        "secret_ref_id": delivery_db["secret_ref_id"],
    }
    response = sender.send(integration, {"event_type": "run.completed"})

    assert response == DeliveryResponse(status_code=302, error_code="REDIRECT_REJECTED")
    assert len(delivery_db["http_events"]) == 1

    untrusted_sender = FixedHostWebhookSender(
        StaticResolver(b"https://evil.example/hooks/secret"),
        http_transport=httpx.MockTransport(respond),
    )
    with pytest.raises(WebhookDestinationRejected):
        untrusted_sender.send(integration, {"event_type": "run.completed"})
    assert len(delivery_db["http_events"]) == 1


def test_outsider_cannot_list_or_retry_delivery(delivery_db):
    delivery = enqueue(delivery_db)
    router = create_integration_router(
        integration_repository=delivery_db["integrations"],
        auth_sessions=delivery_db["sender_sessions"],
        secret_ref_resolver=None,
        delivery_repository=delivery_db["repository"],
    )
    endpoints = {
        route.operation_id: route.endpoint
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    outsider = HTTPAuthorizationCredentials(scheme="Bearer", credentials="outsider")

    with pytest.raises(HTTPException) as list_error:
        endpoints["get__api_v1_webhooks__id__deliveries"](
            id=delivery_db["integration_id"], limit=30, cursor=None,
            credentials=outsider,
        )
    with pytest.raises(HTTPException) as retry_error:
        endpoints["post__api_v1_webhooks__id__deliveries__delivery_id__retry"](
            id=delivery_db["integration_id"], delivery_id=delivery["id"],
            credentials=outsider,
        )

    assert list_error.value.status_code == 404
    assert retry_error.value.status_code == 404
