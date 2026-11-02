"""Transactional organization token reservations and idempotent settlement."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from apps.api.app.auth.persistence import UserRecord
from apps.api.app.core.audit import append_audit_entry
from apps.api.app.operations.persistence import QuotaPolicyRecord, QuotaReservationRecord
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
)


class QuotaExceeded(Exception):
    """The organization's monthly token budget cannot cover the reservation."""


class QuotaOrganizationMissing(Exception):
    pass


class QuotaReservationNotFound(Exception):
    pass


class QuotaIdempotencyConflict(Exception):
    pass


class QuotaSettlementConflict(Exception):
    pass


class QuotaCallAmbiguous(Exception):
    pass


class QuotaPolicyForbidden(Exception):
    pass


class QuotaPolicyNotFound(Exception):
    pass


@dataclass(frozen=True)
class QuotaReservationResult:
    reservation_id: UUID
    call_id: UUID
    organization_id: UUID
    run_id: UUID | None
    tokens_reserved: int
    status: str
    period_month: date


@dataclass(frozen=True)
class SettlementResult:
    reservation_id: UUID
    call_id: UUID
    organization_id: UUID
    run_id: UUID | None
    tokens_consumed: int
    status: str
    settled_at: datetime


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _validate_token_count(value: int, *, allow_zero: bool) -> None:
    if type(value) is not int or value < (0 if allow_zero else 1):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"token count must be a {qualifier} integer")


class QuotaPolicyService:
    DEFAULT_MAX_CONCURRENT_RUNS = 3
    DEFAULT_MONTHLY_TOKEN_LIMIT = 1_000_000

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def get_policy(
        self, actor_id: UUID, organization_id: UUID, *, now: datetime | None = None,
    ) -> dict:
        timestamp = _utc_datetime(now or datetime.now(timezone.utc))
        with self.session_factory() as session:
            organization = self._authorize(session, actor_id, organization_id, write=False)
            policy = session.scalar(select(QuotaPolicyRecord).where(
                QuotaPolicyRecord.organization_id == organization.id,
            ))
            return self._policy_dto(session, policy, organization_id, timestamp)

    def update_policy(
        self,
        actor_id: UUID,
        organization_id: UUID,
        *,
        max_concurrent_runs: int,
        monthly_token_limit: int,
        now: datetime,
    ) -> dict:
        if type(max_concurrent_runs) is not int or not 1 <= max_concurrent_runs <= 1000:
            raise ValueError("max_concurrent_runs must be between 1 and 1000")
        _validate_token_count(monthly_token_limit, allow_zero=True)
        if monthly_token_limit > 2**63 - 1:
            raise ValueError("monthly_token_limit exceeds the supported database range")
        timestamp = _utc_datetime(now)
        with self.session_factory.begin() as session:
            organization = self._authorize(session, actor_id, organization_id, write=True)
            policy = session.scalar(select(QuotaPolicyRecord).where(
                QuotaPolicyRecord.organization_id == organization.id,
            ).with_for_update())
            before = self._policy_dto(session, policy, organization_id, timestamp)
            if policy is None:
                policy = QuotaPolicyRecord(
                    id=uuid4(), organization_id=organization_id,
                    max_concurrent_runs=max_concurrent_runs,
                    monthly_token_limit=monthly_token_limit, updated_at=timestamp,
                )
                session.add(policy)
            else:
                policy.max_concurrent_runs = max_concurrent_runs
                policy.monthly_token_limit = monthly_token_limit
                policy.updated_at = timestamp
            session.flush()
            after = self._policy_dto(session, policy, organization_id, timestamp)
            append_audit_entry(
                session, organization_id=organization_id, actor_user_id=actor_id,
                action="quota_policy.updated", resource_type="quota_policy",
                resource_id=policy.id,
                metadata={
                    "before_hash": self._digest(before),
                    "after_hash": self._digest(after),
                },
                created_at=timestamp,
            )
            return after

    def list_usage(
        self,
        actor_id: UUID,
        organization_id: UUID,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict:
        if date_from is not None:
            date_from = _utc_datetime(date_from)
        if date_to is not None:
            date_to = _utc_datetime(date_to)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("usage date range is invalid")
        with self.session_factory() as session:
            from apps.api.app.cases.planning import UsageMetricRecord

            self._authorize(session, actor_id, organization_id, write=False)
            statement = select(
                UsageMetricRecord.metric_name,
                UsageMetricRecord.unit,
                func.sum(UsageMetricRecord.value),
            ).where(UsageMetricRecord.organization_id == organization_id)
            if date_from is not None:
                statement = statement.where(UsageMetricRecord.created_at >= date_from)
            if date_to is not None:
                statement = statement.where(UsageMetricRecord.created_at <= date_to)
            rows = session.execute(statement.group_by(
                UsageMetricRecord.metric_name, UsageMetricRecord.unit,
            ).order_by(UsageMetricRecord.metric_name, UsageMetricRecord.unit)).all()
            return {"items": [{
                "organization_id": organization_id,
                "metric_name": metric_name,
                "value": Decimal(value or 0),
                "unit": unit,
            } for metric_name, unit, value in rows]}

    @staticmethod
    def _authorize(session, actor_id, organization_id, *, write):
        organization = session.get(OrganizationRecord, organization_id)
        user = session.get(UserRecord, actor_id)
        if organization is None or user is None:
            raise QuotaOrganizationMissing
        if user.platform_role == "platform_admin":
            return organization
        membership = session.get(
            OrganizationMembershipRecord, (organization_id, actor_id),
        )
        if membership is None or (write and membership.role != "org_admin"):
            raise QuotaPolicyForbidden
        return organization

    @classmethod
    def _policy_dto(cls, session, policy, organization_id, now):
        month_start = date(now.year, now.month, 1)
        committed = session.scalar(select(func.coalesce(
            func.sum(QuotaReservationRecord.tokens_consumed), 0,
        )).where(
            QuotaReservationRecord.organization_id == organization_id,
            QuotaReservationRecord.period_month == month_start,
            QuotaReservationRecord.status == "COMMITTED",
        ))
        reserved = session.scalar(select(func.coalesce(
            func.sum(QuotaReservationRecord.tokens_reserved), 0,
        )).where(
            QuotaReservationRecord.organization_id == organization_id,
            QuotaReservationRecord.period_month == month_start,
            QuotaReservationRecord.status == "RESERVED",
        ))
        limit = (
            policy.monthly_token_limit if policy is not None
            else cls.DEFAULT_MONTHLY_TOKEN_LIMIT
        )
        committed = int(committed or 0)
        reserved = int(reserved or 0)
        return {
            "organization_id": organization_id,
            "max_concurrent_runs": (
                policy.max_concurrent_runs if policy is not None else cls.DEFAULT_MAX_CONCURRENT_RUNS
            ),
            "monthly_token_limit": limit,
            "monthly_tokens_committed": committed,
            "monthly_tokens_reserved": reserved,
            "monthly_tokens_remaining": max(0, int(limit) - committed - reserved),
            "quota_exhausted": committed + reserved >= int(limit),
            "period_month": month_start.isoformat(),
            "updated_at": policy.updated_at if policy is not None else None,
        }

    @staticmethod
    def _digest(value) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(canonical.encode("utf-8")).hexdigest()


class QuotaService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def reserve(
        self,
        organization_id: UUID,
        *,
        call_id: UUID,
        run_id: UUID | None,
        requested_tokens: int,
        now: datetime,
    ) -> QuotaReservationResult:
        _validate_token_count(requested_tokens, allow_zero=False)
        if not isinstance(call_id, UUID):
            raise ValueError("call_id must be a UUID")
        if run_id is not None and not isinstance(run_id, UUID):
            raise ValueError("run_id must be a UUID or None")
        timestamp = _utc_datetime(now)
        month_start = date(timestamp.year, timestamp.month, 1)
        call_key = str(call_id)

        try:
            with self.session_factory.begin() as session:
                organization = session.scalar(
                    select(OrganizationRecord)
                    .where(OrganizationRecord.id == organization_id)
                    .with_for_update()
                )
                if organization is None:
                    raise QuotaOrganizationMissing

                existing = session.scalar(
                    select(QuotaReservationRecord)
                    .where(
                        QuotaReservationRecord.organization_id == organization_id,
                        QuotaReservationRecord.call_id == call_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    self._ensure_same_reservation(
                        existing,
                        organization_id=organization_id,
                        run_id=run_id,
                        requested_tokens=requested_tokens,
                        period_month=month_start,
                    )
                    return self._reservation_result(existing, call_id)

                policy = session.scalar(
                    select(QuotaPolicyRecord)
                    .where(QuotaPolicyRecord.organization_id == organization_id)
                    .with_for_update()
                )
                if policy is None:
                    policy = QuotaPolicyRecord(
                        id=uuid4(),
                        organization_id=organization_id,
                        max_concurrent_runs=3,
                        monthly_token_limit=1_000_000,
                        updated_at=timestamp,
                    )
                    session.add(policy)
                    session.flush()

                committed = session.scalar(
                    select(func.coalesce(func.sum(QuotaReservationRecord.tokens_consumed), 0)).where(
                        QuotaReservationRecord.organization_id == organization_id,
                        QuotaReservationRecord.period_month == month_start,
                        QuotaReservationRecord.status == "COMMITTED",
                    )
                )
                reserved = session.scalar(
                    select(func.coalesce(func.sum(QuotaReservationRecord.tokens_reserved), 0)).where(
                        QuotaReservationRecord.organization_id == organization_id,
                        QuotaReservationRecord.period_month == month_start,
                        QuotaReservationRecord.status == "RESERVED",
                    )
                )
                if int(committed or 0) + int(reserved or 0) + requested_tokens > policy.monthly_token_limit:
                    raise QuotaExceeded

                row = QuotaReservationRecord(
                    id=uuid4(),
                    organization_id=organization_id,
                    run_id=run_id,
                    call_id=call_key,
                    tokens_reserved=requested_tokens,
                    tokens_consumed=0,
                    status="RESERVED",
                    period_month=month_start,
                    created_at=timestamp,
                    closed_at=None,
                    settled_at=None,
                    actual_cost=None,
                )
                session.add(row)
                session.flush()
                return self._reservation_result(row, call_id)
        except IntegrityError:
            # A unique call-ID race can happen across workers; return only an exact replay.
            with self.session_factory() as session:
                existing = session.scalar(
                    select(QuotaReservationRecord).where(
                        QuotaReservationRecord.organization_id == organization_id,
                        QuotaReservationRecord.call_id == call_key,
                    )
                )
                if existing is None:
                    raise
                self._ensure_same_reservation(
                    existing,
                    organization_id=organization_id,
                    run_id=run_id,
                    requested_tokens=requested_tokens,
                    period_month=month_start,
                )
                return self._reservation_result(existing, call_id)

    def settle(
        self,
        call_id: UUID,
        *,
        consumed_tokens: int,
        now: datetime,
        usage_source: str = "provider_metadata",
    ) -> SettlementResult:
        _validate_token_count(consumed_tokens, allow_zero=True)
        if usage_source not in {"provider_metadata", "reserved_cap_fallback"}:
            raise ValueError("unsupported quota usage source")
        if not isinstance(call_id, UUID):
            raise ValueError("call_id must be a UUID")
        timestamp = _utc_datetime(now)
        row = self._lock_reservation_for_call(call_id)
        if row is None:
            raise QuotaReservationNotFound
        with self.session_factory.begin() as session:
            # Use one lock order for reserve, settle, and release so concurrent accounting
            # never sees a reservation disappear between the committed and reserved sums.
            session.scalar(
                select(OrganizationRecord)
                .where(OrganizationRecord.id == row.organization_id)
                .with_for_update()
            )
            reservation = session.scalar(
                select(QuotaReservationRecord)
                .where(
                    QuotaReservationRecord.organization_id == row.organization_id,
                    QuotaReservationRecord.call_id == str(call_id),
                )
                .with_for_update()
            )
            if reservation is None:
                raise QuotaReservationNotFound
            if reservation.status == "COMMITTED":
                if reservation.tokens_consumed != consumed_tokens:
                    raise QuotaSettlementConflict
                return self._settlement_result(reservation, call_id)
            if reservation.status != "RESERVED":
                raise QuotaSettlementConflict

            reservation.status = "COMMITTED"
            reservation.tokens_consumed = consumed_tokens
            reservation.closed_at = timestamp
            reservation.settled_at = timestamp
            from apps.api.app.cases.planning import UsageMetricRecord

            metric_name = (
                "provider_tokens"
                if usage_source == "provider_metadata"
                else "provider_tokens_reserved_cap_fallback"
            )
            session.add(UsageMetricRecord(
                organization_id=reservation.organization_id,
                run_id=reservation.run_id,
                metric_name=metric_name,
                value=Decimal(consumed_tokens),
                unit="token",
                created_at=timestamp,
            ))
            session.flush()
            return self._settlement_result(reservation, call_id)

    def release(self, call_id: UUID, *, now: datetime) -> bool:
        if not isinstance(call_id, UUID):
            raise ValueError("call_id must be a UUID")
        timestamp = _utc_datetime(now)
        row = self._lock_reservation_for_call(call_id)
        if row is None:
            return False
        with self.session_factory.begin() as session:
            session.scalar(
                select(OrganizationRecord)
                .where(OrganizationRecord.id == row.organization_id)
                .with_for_update()
            )
            reservation = session.scalar(
                select(QuotaReservationRecord)
                .where(
                    QuotaReservationRecord.organization_id == row.organization_id,
                    QuotaReservationRecord.call_id == str(call_id),
                )
                .with_for_update()
            )
            if reservation is None or reservation.status != "RESERVED":
                return False
            reservation.status = "RELEASED"
            reservation.closed_at = timestamp
            session.flush()
            return True

    def _lock_reservation_for_call(self, call_id: UUID) -> QuotaReservationRecord | None:
        with self.session_factory() as session:
            rows = session.scalars(
                select(QuotaReservationRecord).where(
                    QuotaReservationRecord.call_id == str(call_id)
                )
            ).all()
            if len(rows) > 1:
                raise QuotaCallAmbiguous
            if not rows:
                return None
            # Return a detached snapshot; callers lock the organization first and then
            # re-read the reservation under that lock.
            session.expunge(rows[0])
            return rows[0]

    @staticmethod
    def _ensure_same_reservation(
        row: QuotaReservationRecord,
        *,
        organization_id: UUID,
        run_id: UUID | None,
        requested_tokens: int,
        period_month: date,
    ) -> None:
        if (
            row.organization_id != organization_id
            or row.run_id != run_id
            or row.tokens_reserved != requested_tokens
            or row.period_month != period_month
        ):
            raise QuotaIdempotencyConflict

    @staticmethod
    def _reservation_result(row: QuotaReservationRecord, call_id: UUID) -> QuotaReservationResult:
        return QuotaReservationResult(
            reservation_id=row.id,
            call_id=call_id,
            organization_id=row.organization_id,
            run_id=row.run_id,
            tokens_reserved=row.tokens_reserved,
            status=row.status,
            period_month=row.period_month,
        )

    @staticmethod
    def _settlement_result(row: QuotaReservationRecord, call_id: UUID) -> SettlementResult:
        settled_at = row.settled_at
        if settled_at.tzinfo is None:
            settled_at = settled_at.replace(tzinfo=timezone.utc)
        else:
            settled_at = settled_at.astimezone(timezone.utc)
        return SettlementResult(
            reservation_id=row.id,
            call_id=call_id,
            organization_id=row.organization_id,
            run_id=row.run_id,
            tokens_consumed=row.tokens_consumed,
            status=row.status,
            settled_at=settled_at,
        )
