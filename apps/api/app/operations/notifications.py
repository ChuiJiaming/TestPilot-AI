"""Deduplicated in-app notifications and bounded external delivery retries."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from apps.api.app.auth.persistence import UserRecord
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.integrations.delivery import DeliveryAttemptRecord
from apps.api.app.operations.persistence import (
    NotificationPreferenceRecord,
    NotificationRecord,
)
from apps.api.app.tenancy.persistence import OrganizationMembershipRecord


_CHANNELS = {"in_app", "email", "webhook"}
_EXTERNAL_CHANNELS = ("email", "webhook")
_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,80}$")
_SENSITIVE_PAYLOAD_FRAGMENTS = (
    "secret", "password", "passwd", "token", "cookie", "authorization",
    "credential", "apikey", "accesskey", "pagecontent", "html", "responsebody",
)


class NotificationForbidden(Exception):
    pass


class NotificationNotFound(Exception):
    pass


class NotificationChannelUnavailable(Exception):
    pass


@dataclass(frozen=True)
class NotificationDeliveryResponse:
    status_code: int | None
    error_code: str | None = None


def _safe_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("notification payload must be an object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("notification payload must contain JSON values only") from exc
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("notification payload exceeds the supported limit")

    def validate(value, depth=0):
        if depth > 8:
            raise ValueError("notification payload exceeds the supported complexity limit")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("notification payload keys must be strings")
                normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
                if any(fragment in normalized for fragment in _SENSITIVE_PAYLOAD_FRAGMENTS):
                    raise ValueError("notification payload cannot include secrets or response bodies")
                validate(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 256:
                raise ValueError("notification payload exceeds the supported item limit")
            for item in value:
                validate(item, depth + 1)
        elif isinstance(value, str) and len(value) > 4000:
            raise ValueError("notification payload string exceeds the supported limit")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("notification payload must contain JSON values only")

    validate(payload)
    return json.loads(encoded)


def _safe_error_code(value: str | None) -> str:
    if isinstance(value, str) and _ERROR_CODE.fullmatch(value):
        return value
    return "DELIVERY_FAILED"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class NotificationService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def enqueue(
        self,
        user_id: UUID,
        *,
        event_type: str,
        dedupe_key: str,
        payload: dict,
        now: datetime,
        organization_id: UUID | None = None,
    ) -> NotificationRecord:
        if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type):
            raise ValueError("event_type must be a short stable identifier")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip() or len(dedupe_key) > 255:
            raise ValueError("dedupe_key must be non-empty and at most 255 characters")
        safe_payload = _safe_payload(payload)
        try:
            with self.session_factory.begin() as session:
                user = session.get(UserRecord, user_id)
                if user is None:
                    raise NotificationNotFound
                existing = session.scalar(select(NotificationRecord).where(
                    NotificationRecord.user_id == user_id,
                    NotificationRecord.dedupe_key == dedupe_key,
                ).with_for_update())
                if existing is not None:
                    return existing

                notification = NotificationRecord(
                    id=uuid5(NAMESPACE_URL, f"notification:{user_id}:{dedupe_key}"),
                    user_id=user_id,
                    kind=event_type,
                    payload=safe_payload,
                    dedupe_key=dedupe_key,
                    read_at=None,
                    created_at=now,
                )
                session.add(notification)
                session.flush()
                for channel in _EXTERNAL_CHANNELS:
                    preference = session.get(
                        NotificationPreferenceRecord, (user_id, channel, event_type),
                    )
                    if preference is not None and not preference.enabled:
                        continue
                    delivery_id = uuid5(
                        NAMESPACE_URL,
                        f"notification-delivery:{user_id}:{dedupe_key}:{channel}",
                    )
                    event_payload = {
                        "delivery_id": str(delivery_id),
                        "notification_id": str(notification.id),
                        "user_id": str(user_id),
                        "organization_id": str(organization_id) if organization_id else None,
                        "channel": channel,
                        "event_type": event_type,
                    }
                    session.add(OutboxRecord(
                        aggregate_type="notification_delivery",
                        aggregate_id=delivery_id,
                        event_type="NOTIFICATION_DELIVERY_REQUESTED",
                        payload=event_payload,
                        status="PENDING",
                        attempts=0,
                        available_at=now,
                        created_at=now,
                        last_error=None,
                        claimed_by=None,
                        claimed_until=None,
                        published_at=None,
                    ))
                session.flush()
                return notification
        except IntegrityError:
            # A concurrent enqueue with the same user/key commits only one notification and outbox.
            with self.session_factory() as session:
                existing = session.scalar(select(NotificationRecord).where(
                    NotificationRecord.user_id == user_id,
                    NotificationRecord.dedupe_key == dedupe_key,
                ))
                if existing is None:
                    raise
                return existing

    def list_notifications(self, actor_id: UUID, *, limit: int = 50, cursor: str | None = None):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        cursor_id = None
        if cursor is not None:
            try:
                cursor_id = UUID(cursor)
            except (TypeError, ValueError) as exc:
                raise ValueError("notification cursor is invalid") from exc
        with self.session_factory() as session:
            disabled = session.scalars(select(NotificationPreferenceRecord.event_type).where(
                NotificationPreferenceRecord.user_id == actor_id,
                NotificationPreferenceRecord.channel == "in_app",
                NotificationPreferenceRecord.enabled.is_(False),
            )).all()
            statement = select(NotificationRecord).where(NotificationRecord.user_id == actor_id)
            if disabled:
                statement = statement.where(NotificationRecord.kind.not_in(disabled))
            if cursor_id is not None:
                cursor_row = session.scalar(select(NotificationRecord).where(
                    NotificationRecord.id == cursor_id,
                    NotificationRecord.user_id == actor_id,
                ))
                if cursor_row is None:
                    raise ValueError("notification cursor is invalid")
                statement = statement.where(or_(
                    NotificationRecord.created_at < cursor_row.created_at,
                    and_(
                        NotificationRecord.created_at == cursor_row.created_at,
                        NotificationRecord.id < cursor_row.id,
                    ),
                ))
            rows = session.scalars(statement.order_by(
                NotificationRecord.created_at.desc(), NotificationRecord.id.desc(),
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            return {
                "items": [self._notification_dto(row) for row in items],
                "next_cursor": str(items[-1].id) if has_more and items else None,
            }

    def mark_read(self, actor_id: UUID, notification_id: UUID, *, now: datetime) -> bool:
        with self.session_factory.begin() as session:
            row = session.get(NotificationRecord, notification_id, with_for_update=True)
            if row is None:
                raise NotificationNotFound
            if row.user_id != actor_id:
                raise NotificationForbidden
            row.read_at = row.read_at or now
            return True

    def list_preferences(self, actor_id: UUID) -> dict:
        with self.session_factory() as session:
            rows = session.scalars(select(NotificationPreferenceRecord).where(
                NotificationPreferenceRecord.user_id == actor_id,
            ).order_by(
                NotificationPreferenceRecord.event_type,
                NotificationPreferenceRecord.channel,
            )).all()
            return {"items": [self._preference_dto(row) for row in rows]}

    def update_preferences(self, actor_id: UUID, values: list[dict]) -> dict:
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError("preference items must be a list of at most 500 entries")
        normalized = []
        seen = set()
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("preference items must be objects")
            channel = value.get("channel")
            event_type = value.get("event_type")
            enabled = value.get("enabled")
            if (channel not in _CHANNELS or not isinstance(event_type, str)
                    or not _EVENT_TYPE.fullmatch(event_type) or type(enabled) is not bool):
                raise ValueError("preference item is invalid")
            key = (channel, event_type)
            if key in seen:
                raise ValueError("preference items must be unique per channel and event type")
            seen.add(key)
            normalized.append({"channel": channel, "event_type": event_type, "enabled": enabled})
        with self.session_factory.begin() as session:
            if session.get(UserRecord, actor_id) is None:
                raise NotificationNotFound
            for item in normalized:
                key = (actor_id, item["channel"], item["event_type"])
                row = session.get(NotificationPreferenceRecord, key, with_for_update=True)
                if row is None:
                    row = NotificationPreferenceRecord(user_id=actor_id, **item)
                    session.add(row)
                else:
                    row.enabled = item["enabled"]
            session.flush()
            return {"items": normalized}

    def list_dead_deliveries(
        self, actor_id: UUID, *, organization_id: UUID | None = None, limit: int = 100,
    ) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.session_factory() as session:
            admin_orgs = set(session.scalars(select(
                OrganizationMembershipRecord.organization_id,
            ).where(
                OrganizationMembershipRecord.user_id == actor_id,
                OrganizationMembershipRecord.role == "org_admin",
            )).all())
            rows = session.scalars(select(OutboxRecord).where(
                OutboxRecord.event_type == "NOTIFICATION_DELIVERY_REQUESTED",
                OutboxRecord.status == "DEAD",
            ).order_by(OutboxRecord.created_at.desc(), OutboxRecord.id.desc()).limit(1000)).all()
            items = []
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else {}
                try:
                    notification_id = UUID(str(payload.get("notification_id")))
                    owner_id = UUID(str(payload.get("user_id")))
                    row_organization_id = UUID(str(payload["organization_id"])) if payload.get("organization_id") else None
                except (TypeError, ValueError, KeyError):
                    continue
                if organization_id is not None and row_organization_id != organization_id:
                    continue
                if owner_id != actor_id and row_organization_id not in admin_orgs:
                    continue
                attempt = session.scalar(select(func.max(DeliveryAttemptRecord.attempt)).where(
                    DeliveryAttemptRecord.delivery_type == "notification",
                    DeliveryAttemptRecord.delivery_id == row.aggregate_id,
                )) or 0
                items.append({
                    "delivery_id": row.aggregate_id,
                    "notification_id": notification_id,
                    "channel": payload.get("channel"),
                    "status": "DEAD",
                    "attempts": int(attempt),
                    "error_code": _safe_error_code(row.last_error),
                    "created_at": row.created_at,
                })
                if len(items) >= limit:
                    break
            return {"items": items, "next_cursor": None}

    @staticmethod
    def _notification_dto(row: NotificationRecord) -> dict:
        return {
            "id": row.id,
            "kind": row.kind,
            "payload": dict(row.payload or {}),
            "read_at": row.read_at,
            "created_at": row.created_at,
        }

    @staticmethod
    def _preference_dto(row: NotificationPreferenceRecord) -> dict:
        return {"channel": row.channel, "event_type": row.event_type, "enabled": row.enabled}


class NotificationDispatcher:
    MAX_ATTEMPTS = 8
    LEASE_SECONDS = 60

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def dispatch_delivery_id(
        self, delivery_id: UUID, *, sender, worker_id: str, now: datetime,
    ) -> dict:
        with self.session_factory() as session:
            outbox = session.scalar(select(OutboxRecord).where(
                OutboxRecord.aggregate_type == "notification_delivery",
                OutboxRecord.aggregate_id == delivery_id,
                OutboxRecord.event_type == "NOTIFICATION_DELIVERY_REQUESTED",
            ))
            if outbox is None or not isinstance(outbox.payload, dict):
                return {"claimed": False, "status": "UNCHANGED"}
            payload = dict(outbox.payload)
        try:
            notification_id = UUID(str(payload["notification_id"]))
            channel = payload["channel"]
        except (KeyError, TypeError, ValueError):
            return {"claimed": False, "status": "INVALID_DELIVERY"}
        return self.dispatch(
            notification_id,
            channel=channel,
            delivery_id=delivery_id,
            sender=sender,
            worker_id=worker_id,
            now=now,
        )

    def dispatch(
        self,
        notification_id: UUID,
        *,
        channel: str,
        delivery_id: UUID,
        sender,
        worker_id: str,
        now: datetime,
    ) -> dict:
        if channel not in _EXTERNAL_CHANNELS or not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("notification channel and worker ID are required")
        claim = self._claim(
            notification_id, channel=channel, delivery_id=delivery_id,
            worker_id=worker_id, now=now,
        )
        if claim is None:
            return {"claimed": False, "status": "UNCHANGED"}

        if sender is None or not (callable(sender) or callable(getattr(sender, "send", None))):
            return self._finish(
                delivery_id, worker_id=worker_id,
                response=NotificationDeliveryResponse(None, "CHANNEL_NOT_CONFIGURED"),
                now=now, force_dead=True,
            )
        try:
            send = sender.send if callable(getattr(sender, "send", None)) else sender
            response = send(
                notification_id=notification_id,
                channel=channel,
                event_type=claim["event_type"],
                payload=claim["payload"],
                recipient_email=claim["recipient_email"],
            )
            if isinstance(response, NotificationDeliveryResponse):
                normalized = response
            elif isinstance(response, int):
                normalized = NotificationDeliveryResponse(response)
            else:
                normalized = NotificationDeliveryResponse(None, "INVALID_CHANNEL_RESPONSE")
        except NotificationChannelUnavailable:
            normalized = NotificationDeliveryResponse(None, "CHANNEL_NOT_CONFIGURED")
            return self._finish(
                delivery_id, worker_id=worker_id, response=normalized, now=now, force_dead=True,
            )
        except Exception:
            normalized = NotificationDeliveryResponse(None, "CHANNEL_SEND_FAILED")
        return self._finish(delivery_id, worker_id=worker_id, response=normalized, now=now)

    def _claim(self, notification_id, *, channel, delivery_id, worker_id, now):
        with self.session_factory.begin() as session:
            outbox = session.scalar(select(OutboxRecord).where(
                OutboxRecord.aggregate_type == "notification_delivery",
                OutboxRecord.aggregate_id == delivery_id,
                OutboxRecord.event_type == "NOTIFICATION_DELIVERY_REQUESTED",
            ).with_for_update())
            if outbox is None or not isinstance(outbox.payload, dict):
                return None
            payload = outbox.payload
            if (payload.get("notification_id") != str(notification_id)
                    or payload.get("channel") != channel
                    or outbox.last_error == "DELIVERED"
                    or outbox.status == "DEAD"):
                return None
            if outbox.status == "PENDING" and _as_utc(outbox.available_at) > _as_utc(now):
                return None
            if outbox.status == "PUBLISHING":
                lease_until = outbox.claimed_until
                publisher_claim = isinstance(outbox.claimed_by, str) and outbox.claimed_by.startswith("outbox:")
                if (not publisher_claim and lease_until is not None
                        and _as_utc(lease_until) > _as_utc(now)):
                    return None
            elif outbox.status not in {"PENDING", "PUBLISHED"}:
                return None
            notification = session.get(NotificationRecord, notification_id)
            user = session.get(UserRecord, notification.user_id) if notification is not None else None
            if notification is None or user is None:
                outbox.status = "DEAD"
                outbox.last_error = "NOTIFICATION_NOT_FOUND"
                outbox.claimed_by = None
                outbox.claimed_until = None
                return None
            outbox.status = "PUBLISHING"
            outbox.claimed_by = worker_id
            outbox.claimed_until = now + timedelta(seconds=self.LEASE_SECONDS)
            return {
                "event_type": notification.kind,
                "payload": dict(notification.payload or {}),
                "recipient_email": user.email,
            }

    def _finish(self, delivery_id, *, worker_id, response, now, force_dead=False):
        status_code = response.status_code
        if type(status_code) is not int or not 100 <= status_code <= 599:
            status_code = None
        error_code = _safe_error_code(response.error_code)
        with self.session_factory.begin() as session:
            outbox = session.scalar(select(OutboxRecord).where(
                OutboxRecord.aggregate_type == "notification_delivery",
                OutboxRecord.aggregate_id == delivery_id,
                OutboxRecord.event_type == "NOTIFICATION_DELIVERY_REQUESTED",
            ).with_for_update())
            if (outbox is None or outbox.status != "PUBLISHING"
                    or outbox.claimed_by != worker_id or outbox.claimed_until is None
                    or _as_utc(outbox.claimed_until) <= _as_utc(now)):
                return {"claimed": True, "status": "STALE"}
            previous_attempt = session.scalar(select(func.max(DeliveryAttemptRecord.attempt)).where(
                DeliveryAttemptRecord.delivery_type == "notification",
                DeliveryAttemptRecord.delivery_id == delivery_id,
            )) or 0
            attempt = int(previous_attempt) + 1
            if status_code is not None and 200 <= status_code < 300:
                attempt_status = "SUCCESS"
                delivery_status = "SUCCEEDED"
                outbox.status = "PUBLISHED"
                outbox.last_error = "DELIVERED"
                outbox.published_at = now
            elif (force_dead or (status_code is not None and status_code not in {429} and status_code < 500)):
                attempt_status = "DEAD"
                delivery_status = "DEAD"
                outbox.status = "DEAD"
                outbox.last_error = error_code
            elif attempt >= self.MAX_ATTEMPTS:
                attempt_status = "DEAD"
                delivery_status = "DEAD"
                outbox.status = "DEAD"
                outbox.last_error = error_code
            else:
                attempt_status = "RETRY"
                delivery_status = "RETRY"
                delay = min(300, 2 ** attempt)
                outbox.status = "PENDING"
                outbox.available_at = now + timedelta(seconds=delay)
                outbox.last_error = error_code
            session.add(DeliveryAttemptRecord(
                id=None,
                delivery_type="notification",
                delivery_id=delivery_id,
                attempt=attempt,
                status=attempt_status,
                response_status=status_code,
                error_text=None if attempt_status == "SUCCESS" else error_code,
                created_at=now,
            ))
            outbox.claimed_by = None
            outbox.claimed_until = None
            session.flush()
            return {
                "claimed": True,
                "status": delivery_status,
                "attempt": attempt,
                "error_code": None if attempt_status == "SUCCESS" else error_code,
                "next_attempt_at": outbox.available_at if attempt_status == "RETRY" else None,
            }
