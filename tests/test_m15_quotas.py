from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.cases.planning import UsageMetricRecord
from apps.api.app.operations.persistence import QuotaPolicyRecord, QuotaReservationRecord
from apps.api.app.operations.quotas import (
    QuotaExceeded,
    QuotaIdempotencyConflict,
    QuotaService,
)
from apps.api.app.runs.persistence import TestRunRecord as RunRecord
from apps.api.app.tenancy.persistence import OrganizationRecord


@pytest.fixture
def quota_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ = RunRecord.__table__
    AuthBase.metadata.create_all(engine, tables=[
        OrganizationRecord.__table__,
        QuotaPolicyRecord.__table__,
        QuotaReservationRecord.__table__,
        UsageMetricRecord.__table__,
    ])
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    organization_id = uuid4()
    with sessions.begin() as session:
        session.add(OrganizationRecord(
            id=organization_id,
            name="Quota Org",
            slug=f"quota-{organization_id.hex[:12]}",
            created_at=now,
        ))
        session.add(QuotaPolicyRecord(
            id=uuid4(),
            organization_id=organization_id,
            max_concurrent_runs=3,
            monthly_token_limit=1_000,
            updated_at=now,
        ))
    yield {"engine": engine, "sessions": sessions, "organization_id": organization_id, "now": now}
    engine.dispose()


def _add_reservation(db, *, tokens_reserved, tokens_consumed, status, period_month=None):
    row = QuotaReservationRecord(
        id=uuid4(),
        organization_id=db["organization_id"],
        run_id=None,
        call_id=str(uuid4()),
        tokens_reserved=tokens_reserved,
        tokens_consumed=tokens_consumed,
        status=status,
        period_month=period_month or db["now"].date().replace(day=1),
        created_at=db["now"],
        closed_at=db["now"] if status != "RESERVED" else None,
        settled_at=db["now"] if status == "COMMITTED" else None,
        actual_cost=None,
    )
    with db["sessions"].begin() as session:
        session.add(row)
    return row


def test_reservation_denied_at_monthly_limit(quota_db):
    with quota_db["sessions"].begin() as session:
        policy = session.scalar(select(QuotaPolicyRecord).where(
            QuotaPolicyRecord.organization_id == quota_db["organization_id"]
        ))
        policy.monthly_token_limit = 100
    _add_reservation(quota_db, tokens_reserved=90, tokens_consumed=90, status="COMMITTED")

    with pytest.raises(QuotaExceeded):
        QuotaService(quota_db["sessions"]).reserve(
            quota_db["organization_id"], call_id=uuid4(), run_id=None,
            requested_tokens=11, now=quota_db["now"],
        )


def test_active_reservations_count_against_limit(quota_db):
    with quota_db["sessions"].begin() as session:
        policy = session.scalar(select(QuotaPolicyRecord).where(
            QuotaPolicyRecord.organization_id == quota_db["organization_id"]
        ))
        policy.monthly_token_limit = 100
    _add_reservation(quota_db, tokens_reserved=80, tokens_consumed=0, status="RESERVED")

    with pytest.raises(QuotaExceeded):
        QuotaService(quota_db["sessions"]).reserve(
            quota_db["organization_id"], call_id=uuid4(), run_id=None,
            requested_tokens=30, now=quota_db["now"],
        )


def test_same_call_id_returns_same_reservation(quota_db):
    service = QuotaService(quota_db["sessions"])
    call_id = uuid4()
    first = service.reserve(
        quota_db["organization_id"], call_id=call_id, run_id=None,
        requested_tokens=100, now=quota_db["now"],
    )
    second = service.reserve(
        quota_db["organization_id"], call_id=call_id, run_id=None,
        requested_tokens=100, now=quota_db["now"],
    )

    assert second == first


def test_same_call_id_with_different_dimensions_conflicts(quota_db):
    service = QuotaService(quota_db["sessions"])
    call_id = uuid4()
    service.reserve(
        quota_db["organization_id"], call_id=call_id, run_id=None,
        requested_tokens=100, now=quota_db["now"],
    )

    with pytest.raises(QuotaIdempotencyConflict):
        service.reserve(
            quota_db["organization_id"], call_id=call_id, run_id=None,
            requested_tokens=101, now=quota_db["now"],
        )


def test_success_settles_once_by_call_id(quota_db):
    service = QuotaService(quota_db["sessions"])
    call_id = uuid4()
    service.reserve(
        quota_db["organization_id"], call_id=call_id, run_id=None,
        requested_tokens=100, now=quota_db["now"],
    )

    first = service.settle(call_id, consumed_tokens=43, now=quota_db["now"])
    second = service.settle(call_id, consumed_tokens=43, now=quota_db["now"] + timedelta(seconds=1))

    assert first.tokens_consumed == 43
    assert first.status == "COMMITTED"
    assert second == first
    with quota_db["sessions"]() as session:
        metric = session.scalars(select(UsageMetricRecord)).all()
        assert len(metric) == 1
        assert metric[0].metric_name == "provider_tokens"
        assert metric[0].value == Decimal("43.000000")


def test_reserved_cap_fallback_usage_source_is_persisted(quota_db):
    service = QuotaService(quota_db["sessions"])
    call_id = uuid4()
    service.reserve(
        quota_db["organization_id"], call_id=call_id, run_id=None,
        requested_tokens=100, now=quota_db["now"],
    )

    service.settle(
        call_id, consumed_tokens=100, usage_source="reserved_cap_fallback",
        now=quota_db["now"],
    )

    with quota_db["sessions"]() as session:
        metric = session.scalar(select(UsageMetricRecord))
        assert metric.metric_name == "provider_tokens_reserved_cap_fallback"


def test_failed_call_releases_reservation(quota_db):
    service = QuotaService(quota_db["sessions"])
    call_id = uuid4()
    result = service.reserve(
        quota_db["organization_id"], call_id=call_id, run_id=None,
        requested_tokens=100, now=quota_db["now"],
    )

    assert service.release(call_id, now=quota_db["now"])
    assert not service.release(call_id, now=quota_db["now"] + timedelta(seconds=1))
    with quota_db["sessions"]() as session:
        row = session.get(QuotaReservationRecord, result.reservation_id)
        assert row.status == "RELEASED"
        assert row.closed_at.replace(tzinfo=timezone.utc) == quota_db["now"]


def test_month_boundary_uses_utc_calendar_month(quota_db):
    local_time = datetime(2026, 10, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    result = QuotaService(quota_db["sessions"]).reserve(
        quota_db["organization_id"], call_id=uuid4(), run_id=None,
        requested_tokens=100, now=local_time,
    )

    assert result.period_month == date(2026, 11, 1)


@pytest.mark.parametrize("invalid", [-1, 0, True, 1.5, "4"])
def test_negative_or_boolean_token_counts_are_rejected(quota_db, invalid):
    with pytest.raises(ValueError):
        QuotaService(quota_db["sessions"]).reserve(
            quota_db["organization_id"], call_id=uuid4(), run_id=None,
            requested_tokens=invalid, now=quota_db["now"],
        )
