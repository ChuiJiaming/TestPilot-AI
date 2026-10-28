"""Append organization-scoped, SHA-256 chained audit entries."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.tenancy.persistence import OrganizationRecord


class AuditLogRecord(AuthBase):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id"))
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSON().with_variant(JSONB, "postgresql"), nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String)
    entry_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def append_audit_entry(
    session,
    *,
    organization_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    metadata: dict,
    created_at: datetime,
) -> AuditLogRecord:
    """Append one entry; the organization row serializes concurrent chain writes."""
    locked_org_id = session.scalar(
        select(OrganizationRecord.id)
        .where(OrganizationRecord.id == organization_id)
        .with_for_update()
    )
    if locked_org_id is None:
        raise ValueError("audit organization does not exist")

    previous_hash = session.scalar(
        select(AuditLogRecord.entry_hash)
        .where(AuditLogRecord.organization_id == organization_id)
        .order_by(AuditLogRecord.id.desc())
        .limit(1)
    )
    timestamp = created_at
    if timestamp.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    timestamp_text = timestamp.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    envelope = {
        "version": 1,
        "organization_id": str(organization_id),
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id else None,
        "metadata": metadata,
        "created_at": timestamp_text,
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    entry_hash = sha256(canonical.encode("utf-8")).hexdigest()
    entry = AuditLogRecord(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata_json=metadata,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
        created_at=created_at,
    )
    session.add(entry)
    session.flush()
    return entry
