"""Persistence mappings for M15 quotas, notifications, and setting approvals."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase


JsonValue = JSON().with_variant(JSONB, "postgresql")


class QuotaPolicyRecord(AuthBase):
    __tablename__ = "quota_policies"
    __table_args__ = (
        CheckConstraint("max_concurrent_runs > 0", name="ck_quota_policy_max_concurrent_runs"),
        CheckConstraint("monthly_token_limit >= 0", name="ck_quota_policy_monthly_token_limit"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False, unique=True,
    )
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    monthly_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1_000_000)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuotaReservationRecord(AuthBase):
    __tablename__ = "quota_reservations"
    __table_args__ = (
        CheckConstraint("tokens_reserved >= 0", name="ck_quota_reservation_reserved_nonnegative"),
        CheckConstraint("tokens_consumed >= 0", name="ck_quota_reservation_consumed_nonnegative"),
        CheckConstraint("status IN ('RESERVED','COMMITTED','RELEASED')", name="ck_quota_reservation_status"),
        Index(
            "uq_quota_call_per_org", "organization_id", "call_id", unique=True,
            postgresql_where=text("call_id IS NOT NULL"),
            sqlite_where=text("call_id IS NOT NULL"),
        ),
        Index("ix_quota_reservations_org_period_status", "organization_id", "period_month", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False,
    )
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_runs.id"))
    call_id: Mapped[str] = mapped_column(String, nullable=False)
    tokens_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tokens_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="RESERVED")
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))


class NotificationRecord(AuthBase):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_dedupe"),
        Index("ix_notifications_user_created", "user_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonValue, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationPreferenceRecord(AuthBase):
    __tablename__ = "notification_preferences"
    __table_args__ = (
        CheckConstraint("channel IN ('in_app','email','webhook')", name="ck_notification_preference_channel"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), primary_key=True,
    )
    channel: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SystemSettingRecord(AuthBase):
    __tablename__ = "system_settings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    value: Mapped[object] = mapped_column(JsonValue, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SystemSettingChangeRecord(AuthBase):
    __tablename__ = "system_setting_changes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','STALE')",
            name="ck_system_setting_change_status",
        ),
        CheckConstraint(
            "status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_by <> requested_by)",
            name="ck_system_setting_change_approval_separation",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND decided_at IS NULL) OR (status <> 'PENDING' AND decided_at IS NOT NULL)",
            name="ck_system_setting_change_decision_time",
        ),
        Index(
            "uq_system_setting_one_pending_per_key", "key", unique=True,
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
        Index("ix_system_setting_changes_status_created", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String, nullable=False)
    proposed_value: Mapped[object] = mapped_column(JsonValue, nullable=False)
    previous_value_hash: Mapped[str] = mapped_column(String, nullable=False)
    proposed_value_hash: Mapped[str] = mapped_column(String, nullable=False)
    requested_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    approved_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"),
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
