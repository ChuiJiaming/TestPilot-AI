"""Organization-scoped integration metadata persistence."""

from datetime import datetime
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Uuid,
    UniqueConstraint, func, select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.lifecycle import SecretRefRecord
from apps.api.app.auth.persistence import AuthBase
from apps.api.app.tenancy.persistence import OrganizationMembershipRecord


class IntegrationRecord(AuthBase):
    __tablename__ = "integrations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    secret_ref_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("secret_refs.id")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookDeliveryRecord(AuthBase):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("integration_id", "event_id", name="uq_webhook_event"),
        Index("uq_webhook_nonce", "integration_id", "request_nonce", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    integration_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("integrations.id"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    response_code: Mapped[int | None] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_nonce: Mapped[str | None] = mapped_column(String)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signature_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class IntegrationNotFound(Exception):
    """Raised for a missing integration or an organization outside actor scope."""


class IntegrationForbidden(Exception):
    """Raised when a member without the required organization role writes."""


class WebhookNonceReplay(Exception):
    """Raised when an integration nonce has already been accepted."""


class WebhookIntegrationAmbiguous(Exception):
    """Raised when a provider name cannot identify one integration safely."""


_SECRET_CONFIG_FRAGMENTS = (
    "secret", "password", "passwd", "token", "credential", "authorization", "auth",
    "apikey", "accesskey",
)
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_CONFIG_DEPTH = 8
_MAX_CONFIG_ITEMS = 512


def _safe_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    try:
        encoded = json.dumps(config, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("config must contain JSON values only") from exc
    if len(encoded.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise ValueError("config exceeds the 16 KiB limit")

    visited = 0

    def validate(value, depth):
        nonlocal visited
        visited += 1
        if visited > _MAX_CONFIG_ITEMS or depth > _MAX_CONFIG_DEPTH:
            raise ValueError("config exceeds the supported complexity limit")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("config object keys must be strings")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(fragment in normalized for fragment in _SECRET_CONFIG_FRAGMENTS):
                    raise ValueError("config cannot contain credentials; use a SecretRef reference")
                validate(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                validate(item, depth + 1)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("config must contain JSON values only")

    validate(config, 0)
    return json.loads(encoded)


class IntegrationRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create(self, actor_id, organization_id, *, kind, config, secret_ref_id, enabled, now):
        safe_config = _safe_config(config)
        with self.session_factory.begin() as session:
            self._membership(session, actor_id, organization_id, write=True)
            self._validate_secret_ref(session, organization_id, secret_ref_id)
            record = IntegrationRecord(
                id=uuid4(), organization_id=organization_id, kind=kind,
                config=safe_config, secret_ref_id=secret_ref_id,
                enabled=enabled, created_at=now,
            )
            session.add(record)
            session.flush()
            return self._dto(record)

    def list(self, actor_id, *, organization_id, limit=30, cursor=None):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        cursor_id = self._cursor_id(cursor)
        with self.session_factory() as session:
            self._membership(session, actor_id, organization_id, write=False)
            statement = select(IntegrationRecord).where(
                IntegrationRecord.organization_id == organization_id
            )
            if cursor_id is not None:
                statement = statement.where(IntegrationRecord.id > cursor_id)
            rows = session.scalars(
                statement.order_by(IntegrationRecord.id).limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "items": [self._dto(row) for row in rows],
                "next_cursor": str(rows[-1].id) if has_more and rows else None,
            }

    def get(self, actor_id, integration_id, *, require_admin=False):
        with self.session_factory() as session:
            record = session.get(IntegrationRecord, integration_id)
            if record is None:
                raise IntegrationNotFound
            self._membership(
                session, actor_id, record.organization_id, write=require_admin
            )
            return self._dto(record)

    def update(self, actor_id, integration_id, *, changes, now):
        unknown = set(changes) - {"config", "secret_ref_id", "enabled"}
        if unknown or not changes:
            raise ValueError("one or more supported integration fields are required")
        if "config" in changes:
            changes = {**changes, "config": _safe_config(changes["config"])}
        with self.session_factory.begin() as session:
            record = session.get(IntegrationRecord, integration_id, with_for_update=True)
            if record is None:
                raise IntegrationNotFound
            self._membership(session, actor_id, record.organization_id, write=True)
            if "secret_ref_id" in changes:
                self._validate_secret_ref(
                    session, record.organization_id, changes["secret_ref_id"]
                )
            for key, value in changes.items():
                setattr(record, key, value)
            session.flush()
            return self._dto(record)

    def organization_id_for_integration(self, integration_id):
        with self.session_factory() as session:
            return session.scalar(select(IntegrationRecord.organization_id).where(
                IntegrationRecord.id == integration_id
            ))

    def webhook_target(self, provider: str, *, integration_id: UUID | None = None):
        with self.session_factory() as session:
            statement = select(IntegrationRecord).where(
                func.lower(IntegrationRecord.kind) == provider.strip().casefold(),
                IntegrationRecord.enabled.is_(True),
                IntegrationRecord.secret_ref_id.is_not(None),
            ).join(
                SecretRefRecord,
                (SecretRefRecord.id == IntegrationRecord.secret_ref_id)
                & (SecretRefRecord.organization_id == IntegrationRecord.organization_id),
            )
            if integration_id is not None:
                statement = statement.where(IntegrationRecord.id == integration_id)
            rows = session.scalars(statement.order_by(IntegrationRecord.id).limit(2)).all()
            if not rows:
                raise IntegrationNotFound
            if len(rows) > 1:
                raise WebhookIntegrationAmbiguous
            record = rows[0]
            return {
                "id": record.id,
                "organization_id": record.organization_id,
                "secret_ref_id": record.secret_ref_id,
            }

    def receive_webhook(
        self,
        integration_id: UUID,
        *,
        event_id: str,
        event_type: str,
        nonce: str,
        signed_at: datetime,
        payload: dict,
        body_sha256: str,
        now: datetime,
    ) -> dict:
        from sqlalchemy.exc import IntegrityError

        from apps.api.app.cases.planning import OutboxRecord

        try:
            with self.session_factory.begin() as session:
                integration = session.get(
                    IntegrationRecord, integration_id, with_for_update=True
                )
                if (integration is None or not integration.enabled
                        or integration.secret_ref_id is None):
                    raise IntegrationNotFound
                secret_ref = session.get(SecretRefRecord, integration.secret_ref_id)
                if (secret_ref is None
                        or secret_ref.organization_id != integration.organization_id):
                    raise IntegrationNotFound
                replay = session.scalar(select(WebhookDeliveryRecord.id).where(
                    WebhookDeliveryRecord.integration_id == integration_id,
                    WebhookDeliveryRecord.request_nonce == nonce,
                ))
                if replay is not None:
                    raise WebhookNonceReplay
                duplicate = session.scalar(select(WebhookDeliveryRecord).where(
                    WebhookDeliveryRecord.integration_id == integration_id,
                    WebhookDeliveryRecord.event_id == event_id,
                ))
                if duplicate is not None:
                    return {"receipt_id": duplicate.id, "duplicate": True}

                receipt = WebhookDeliveryRecord(
                    id=uuid4(), integration_id=integration_id, event_id=event_id,
                    attempts=0, status="PENDING", response_code=None,
                    next_attempt_at=None, created_at=now, request_nonce=nonce,
                    signed_at=signed_at, signature_version=1,
                )
                session.add(receipt)
                session.flush()
                session.add(OutboxRecord(
                    aggregate_type="webhook_inbound", aggregate_id=receipt.id,
                    event_type="INBOUND_WEBHOOK_RECEIVED",
                    payload={
                        "receipt_id": str(receipt.id),
                        "integration_id": str(integration_id),
                        "body_sha256": body_sha256,
                        "event": {
                            "schema_version": 1,
                            "event_id": event_id,
                            "event_type": event_type,
                            "payload": payload,
                        },
                    },
                    status="PENDING", attempts=0, available_at=now, created_at=now,
                ))
                return {"receipt_id": receipt.id, "duplicate": False}
        except IntegrityError:
            with self.session_factory() as session:
                replay = session.scalar(select(WebhookDeliveryRecord.id).where(
                    WebhookDeliveryRecord.integration_id == integration_id,
                    WebhookDeliveryRecord.request_nonce == nonce,
                ))
                if replay is not None:
                    raise WebhookNonceReplay
                duplicate = session.scalar(select(WebhookDeliveryRecord).where(
                    WebhookDeliveryRecord.integration_id == integration_id,
                    WebhookDeliveryRecord.event_id == event_id,
                ))
                if duplicate is not None:
                    return {"receipt_id": duplicate.id, "duplicate": True}
            raise

    @staticmethod
    def _membership(session, actor_id, organization_id, *, write):
        membership = session.get(
            OrganizationMembershipRecord, (organization_id, actor_id)
        )
        if membership is None:
            raise IntegrationNotFound
        if write and membership.role != "org_admin":
            raise IntegrationForbidden
        if membership.role not in {"org_admin", "org_member"}:
            raise IntegrationNotFound
        return membership

    @staticmethod
    def _validate_secret_ref(session, organization_id, secret_ref_id):
        if secret_ref_id is None:
            return
        reference = session.get(SecretRefRecord, secret_ref_id)
        if reference is None or reference.organization_id != organization_id:
            raise IntegrationNotFound

    @staticmethod
    def _cursor_id(cursor):
        if cursor is None:
            return None
        try:
            return UUID(cursor)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid integration cursor") from exc

    @staticmethod
    def _dto(record):
        return {
            "id": record.id,
            "organization_id": record.organization_id,
            "kind": record.kind,
            "config": _safe_config(record.config),
            "secret_ref_id": record.secret_ref_id,
            "enabled": record.enabled,
            "created_at": record.created_at,
        }
