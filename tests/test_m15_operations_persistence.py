from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.operations.persistence import (
    NotificationPreferenceRecord,
    NotificationRecord,
    QuotaPolicyRecord,
    QuotaReservationRecord,
    SystemSettingChangeRecord,
    SystemSettingRecord,
)
from apps.api.app.runs.persistence import TestRunRecord as RunRecord
from apps.api.app.tenancy.persistence import OrganizationRecord


@pytest.fixture
def operations_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ = RunRecord.__table__
    AuthBase.metadata.create_all(engine, tables=[
        UserRecord.__table__, OrganizationRecord.__table__, QuotaPolicyRecord.__table__,
        QuotaReservationRecord.__table__, NotificationRecord.__table__,
        NotificationPreferenceRecord.__table__, SystemSettingRecord.__table__,
        SystemSettingChangeRecord.__table__,
    ])
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    user_id, second_user_id, organization_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        for user_id_value in (user_id, second_user_id):
            session.add(UserRecord(
                id=user_id_value, email=f"{user_id_value}@example.test",
                password_hash="hash", display_name="Operations User",
                platform_role="platform_admin", status="active", created_at=now,
                email_verified_at=now,
            ))
        session.add(OrganizationRecord(
            id=organization_id, name="Operations Org", slug=f"operations-{user_id.hex[:12]}",
            created_at=now,
        ))
    yield {
        "engine": engine, "sessions": sessions, "now": now,
        "user_id": user_id, "second_user_id": second_user_id,
        "organization_id": organization_id,
    }
    engine.dispose()


def test_call_id_is_idempotent_per_org_and_mismatched_reuse_conflicts(operations_db):
    with operations_db["sessions"].begin() as session:
        session.add(QuotaReservationRecord(
            id=uuid4(), organization_id=operations_db["organization_id"], run_id=None,
            call_id="call-1", tokens_reserved=120, tokens_consumed=0,
            status="RESERVED", created_at=operations_db["now"], closed_at=None,
            settled_at=None, actual_cost=None, period_month=operations_db["now"].date().replace(day=1),
        ))

    with pytest.raises(IntegrityError):
        with operations_db["sessions"].begin() as session:
            session.add(QuotaReservationRecord(
                id=uuid4(), organization_id=operations_db["organization_id"], run_id=None,
                call_id="call-1", tokens_reserved=999, tokens_consumed=0,
                status="RESERVED", created_at=operations_db["now"], closed_at=None,
                settled_at=None, actual_cost=None, period_month=operations_db["now"].date().replace(day=1),
            ))


def test_quota_reservation_call_id_is_non_null_in_runtime_model():
    assert QuotaReservationRecord.__table__.c.call_id.nullable is False


def test_notification_dedupe_key_is_unique_per_user(operations_db):
    with operations_db["sessions"].begin() as session:
        session.add(NotificationRecord(
            id=uuid4(), user_id=operations_db["user_id"], kind="run.failed",
            payload={"run_id": "run-1"}, read_at=None, created_at=operations_db["now"],
            dedupe_key="run.failed:run-1",
        ))

    with pytest.raises(IntegrityError):
        with operations_db["sessions"].begin() as session:
            session.add(NotificationRecord(
                id=uuid4(), user_id=operations_db["user_id"], kind="run.failed",
                payload={"run_id": "run-1"}, read_at=None, created_at=operations_db["now"],
                dedupe_key="run.failed:run-1",
            ))

    with operations_db["sessions"].begin() as session:
        session.add(NotificationRecord(
            id=uuid4(), user_id=operations_db["second_user_id"], kind="run.failed",
            payload={"run_id": "run-1"}, read_at=None, created_at=operations_db["now"],
            dedupe_key="run.failed:run-1",
        ))


def test_one_pending_setting_change_per_key(operations_db):
    with operations_db["sessions"].begin() as session:
        session.add(SystemSettingChangeRecord(
            id=uuid4(), key="retention_days", proposed_value=90,
            previous_value_hash="0" * 64, proposed_value_hash="1" * 64,
            requested_by=operations_db["user_id"], approved_by=None,
            status="PENDING", reason="Increase retention", created_at=operations_db["now"],
            decided_at=None,
        ))

    with pytest.raises(IntegrityError):
        with operations_db["sessions"].begin() as session:
            session.add(SystemSettingChangeRecord(
                id=uuid4(), key="retention_days", proposed_value=120,
                previous_value_hash="0" * 64, proposed_value_hash="2" * 64,
                requested_by=operations_db["second_user_id"], approved_by=None,
                status="PENDING", reason="Increase retention again", created_at=operations_db["now"],
                decided_at=None,
            ))


def test_direct_apply_records_the_same_user_as_requester_and_applier(operations_db):
    """平台管理员保存即生效：申请人与生效人可以是同一人，但 approved_by 不能为空。"""
    with operations_db["sessions"].begin() as session:
        session.add(SystemSettingChangeRecord(
            id=uuid4(), key="retention_days", proposed_value=90,
            previous_value_hash="0" * 64, proposed_value_hash="1" * 64,
            requested_by=operations_db["user_id"], approved_by=operations_db["user_id"],
            status="APPROVED", reason="Direct apply", created_at=operations_db["now"],
            decided_at=operations_db["now"],
        ))

    with pytest.raises(IntegrityError):
        with operations_db["sessions"].begin() as session:
            session.add(SystemSettingChangeRecord(
                id=uuid4(), key="max_concurrent_runs", proposed_value=5,
                previous_value_hash="0" * 64, proposed_value_hash="1" * 64,
                requested_by=operations_db["user_id"], approved_by=None,
                status="APPROVED", reason="Missing applier", created_at=operations_db["now"],
                decided_at=operations_db["now"],
            ))


def test_pending_setting_change_does_not_replace_current_value(operations_db):
    with operations_db["sessions"].begin() as session:
        session.add(SystemSettingRecord(
            id=uuid4(), key="retention_days", value=30, updated_at=operations_db["now"],
        ))
        session.add(SystemSettingChangeRecord(
            id=uuid4(), key="retention_days", proposed_value=90,
            previous_value_hash="0" * 64, proposed_value_hash="1" * 64,
            requested_by=operations_db["user_id"], approved_by=None,
            status="PENDING", reason="Increase retention", created_at=operations_db["now"],
            decided_at=None,
        ))

    with operations_db["sessions"]() as session:
        current = session.query(SystemSettingRecord).filter_by(key="retention_days").one()
        pending = session.query(SystemSettingChangeRecord).filter_by(key="retention_days").one()
        assert current.value == 30
        assert pending.proposed_value == 90
        assert pending.status == "PENDING"
