from datetime import datetime, timedelta, timezone
import importlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.integrations.delivery import DeliveryAttemptRecord
from apps.api.app.operations.notifications import (
    NotificationDeliveryResponse,
    NotificationDispatcher,
    NotificationForbidden,
    NotificationService,
)
from apps.api.app.operations.persistence import NotificationPreferenceRecord, NotificationRecord
from apps.api.app.tenancy.persistence import OrganizationMembershipRecord, OrganizationRecord


@pytest.fixture
def notification_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine, tables=[
        UserRecord.__table__, OrganizationRecord.__table__, OrganizationMembershipRecord.__table__,
        NotificationRecord.__table__, NotificationPreferenceRecord.__table__,
        OutboxRecord.__table__, DeliveryAttemptRecord.__table__,
    ])
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    owner_id, member_id, admin_id, outsider_id, organization_id = (uuid4() for _ in range(5))
    with sessions.begin() as session:
        for user_id in (owner_id, member_id, admin_id, outsider_id):
            session.add(UserRecord(
                id=user_id, email=f"{user_id}@example.test", password_hash="hash",
                display_name="Notification User", platform_role="user", status="active",
                created_at=now, email_verified_at=now,
            ))
        session.add(OrganizationRecord(
            id=organization_id, name="Notifications Org", slug=f"notify-{organization_id.hex[:12]}",
            created_at=now,
        ))
        session.add_all([
            OrganizationMembershipRecord(
                organization_id=organization_id, user_id=owner_id, role="member", created_at=now,
            ),
            OrganizationMembershipRecord(
                organization_id=organization_id, user_id=member_id, role="member", created_at=now,
            ),
            OrganizationMembershipRecord(
                organization_id=organization_id, user_id=admin_id, role="org_admin", created_at=now,
            ),
        ])
    yield {
        "engine": engine, "sessions": sessions, "now": now,
        "owner_id": owner_id, "member_id": member_id, "admin_id": admin_id,
        "outsider_id": outsider_id, "organization_id": organization_id,
    }
    engine.dispose()


class _Sender:
    def __init__(self, status_code=204, error_code=None):
        self.status_code = status_code
        self.error_code = error_code
        self.calls = 0

    def send(self, **_kwargs):
        self.calls += 1
        return NotificationDeliveryResponse(
            status_code=self.status_code, error_code=self.error_code,
        )


def _enqueue(db, user_id=None, *, dedupe_key="run.failed:run-1", event_type="run.failed"):
    return NotificationService(db["sessions"]).enqueue(
        user_id or db["owner_id"],
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload={"run_id": "run-1", "status": "FAILED"},
        organization_id=db["organization_id"],
        now=db["now"],
    )


def _deliveries(db, notification_id):
    with db["sessions"]() as session:
        return session.scalars(select(OutboxRecord).where(
            OutboxRecord.event_type == "NOTIFICATION_DELIVERY_REQUESTED",
            OutboxRecord.payload["notification_id"].as_string() == str(notification_id),
        )).all()


def test_duplicate_event_creates_one_notification(notification_db):
    first = _enqueue(notification_db)
    duplicate = _enqueue(notification_db)

    assert duplicate.id == first.id
    with notification_db["sessions"]() as session:
        assert len(session.scalars(select(NotificationRecord)).all()) == 1
        assert len(session.scalars(select(OutboxRecord).where(
            OutboxRecord.event_type == "NOTIFICATION_DELIVERY_REQUESTED",
        )).all()) == 2


def test_preference_suppresses_disabled_channel(notification_db):
    with notification_db["sessions"].begin() as session:
        session.add(NotificationPreferenceRecord(
            user_id=notification_db["owner_id"], channel="email",
            event_type="run.failed", enabled=False,
        ))

    notification = _enqueue(notification_db)
    deliveries = _deliveries(notification_db, notification.id)

    assert {row.payload["channel"] for row in deliveries} == {"webhook"}


def test_user_can_only_mark_own_notification_read(notification_db):
    notification = _enqueue(notification_db)
    service = NotificationService(notification_db["sessions"])

    assert service.mark_read(notification_db["owner_id"], notification.id, now=notification_db["now"])
    with pytest.raises(NotificationForbidden):
        service.mark_read(notification_db["member_id"], notification.id, now=notification_db["now"])


def test_notification_list_cursor_returns_the_next_page(notification_db):
    notifications = [
        _enqueue(notification_db, dedupe_key=f"run.failed:page-{index}")
        for index in range(3)
    ]
    service = NotificationService(notification_db["sessions"])

    first_page = service.list_notifications(notification_db["owner_id"], limit=2)
    second_page = service.list_notifications(
        notification_db["owner_id"], limit=2, cursor=first_page["next_cursor"],
    )

    first_ids = {item["id"] for item in first_page["items"]}
    second_ids = {item["id"] for item in second_page["items"]}
    assert len(first_ids) == 2
    assert len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {item.id for item in notifications}
    assert second_page["next_cursor"] is None


def test_transient_failure_retries_with_backoff(notification_db):
    notification = _enqueue(notification_db)
    delivery = _deliveries(notification_db, notification.id)[0]
    response = NotificationDispatcher(notification_db["sessions"]).dispatch(
        notification.id,
        channel=delivery.payload["channel"],
        delivery_id=delivery.aggregate_id,
        sender=_Sender(503, "UPSTREAM_UNAVAILABLE"),
        worker_id="notification-worker-1",
        now=notification_db["now"],
    )

    assert response["status"] == "RETRY"
    assert response["attempt"] == 1
    with notification_db["sessions"]() as session:
        outbox = session.get(OutboxRecord, delivery.id)
        attempt = session.scalar(select(DeliveryAttemptRecord))
        assert outbox.status == "PENDING"
        assert outbox.available_at.replace(tzinfo=timezone.utc) == (
            notification_db["now"] + timedelta(seconds=2)
        )
        assert attempt.status == "RETRY"
        assert attempt.error_text == "UPSTREAM_UNAVAILABLE"


def test_retry_exhaustion_is_visible_as_dead(notification_db):
    notification = _enqueue(notification_db)
    delivery = _deliveries(notification_db, notification.id)[0]
    dispatcher = NotificationDispatcher(notification_db["sessions"])
    sender = _Sender(503, "UPSTREAM_UNAVAILABLE")
    result = None
    for attempt in range(8):
        result = dispatcher.dispatch(
            notification.id,
            channel=delivery.payload["channel"],
            delivery_id=delivery.aggregate_id,
            sender=sender,
            worker_id=f"notification-worker-{attempt}",
            now=notification_db["now"] + timedelta(seconds=600 * attempt),
        )

    assert result["status"] == "DEAD"
    with notification_db["sessions"]() as session:
        outbox = session.get(OutboxRecord, delivery.id)
        attempts = session.scalars(select(DeliveryAttemptRecord).where(
            DeliveryAttemptRecord.delivery_type == "notification",
            DeliveryAttemptRecord.delivery_id == delivery.aggregate_id,
        )).all()
        assert outbox.status == "DEAD"
        assert len(attempts) == 8
        assert attempts[-1].status == "DEAD"
        assert sender.calls == 8


def test_dead_delivery_list_is_user_and_organization_scoped(notification_db):
    service = NotificationService(notification_db["sessions"])
    dispatcher = NotificationDispatcher(notification_db["sessions"])
    owner_notification = _enqueue(notification_db)
    other_notification = _enqueue(notification_db, notification_db["member_id"], dedupe_key="run.failed:run-2")

    for notification in (owner_notification, other_notification):
        delivery = _deliveries(notification_db, notification.id)[0]
        sender = _Sender(503, "UPSTREAM_UNAVAILABLE")
        for attempt in range(8):
            dispatcher.dispatch(
                notification.id,
                channel=delivery.payload["channel"],
                delivery_id=delivery.aggregate_id,
                sender=sender,
                worker_id=f"dead-worker-{notification.id}-{attempt}",
                now=notification_db["now"] + timedelta(seconds=600 * attempt),
            )

    owner_items = service.list_dead_deliveries(notification_db["owner_id"])["items"]
    admin_items = service.list_dead_deliveries(notification_db["admin_id"])["items"]
    outsider_items = service.list_dead_deliveries(notification_db["outsider_id"])["items"]

    assert {item["notification_id"] for item in owner_items} == {owner_notification.id}
    assert {item["notification_id"] for item in admin_items} == {
        owner_notification.id, other_notification.id,
    }
    assert outsider_items == []


def test_notification_outbox_dispatches_to_the_delivery_worker(notification_db, monkeypatch):
    monkeypatch.setenv("TESTPILOT_REDIS_URL", "redis://127.0.0.1:6379/0")
    worker = importlib.import_module("apps.api.app.cases.worker")
    monkeypatch.setattr(worker, "session_factory", lambda: notification_db["sessions"])
    monkeypatch.setattr(worker, "_notification_sender_factory", None, raising=False)
    sent_tasks = []
    monkeypatch.setattr(
        worker.celery_app,
        "send_task",
        lambda task_name, **kwargs: sent_tasks.append((task_name, kwargs)),
    )
    notification = _enqueue(notification_db)

    published = worker.dispatch_notification_outbox()

    assert published == {"claimed": 2, "published": 2}
    assert {name for name, _kwargs in sent_tasks} == {"testpilot.deliver_notification"}
    assert len({kwargs["kwargs"]["delivery_id"] for _name, kwargs in sent_tasks}) == 2
    assert worker.celery_app.conf.beat_schedule["publish-notification-delivery-outbox"] == {
        "task": "testpilot.dispatch_notification_outbox", "schedule": 2.0,
    }

    delivery = _deliveries(notification_db, notification.id)[0]
    sender = _Sender(204)
    worker.configure_notification_sender_factory(lambda: sender)
    result = worker.deliver_notification(str(delivery.aggregate_id))

    assert result["status"] == "SUCCEEDED"
    assert sender.calls == 1
