from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.planning import UsageMetricRecord
from apps.api.app.core.audit import AuditLogRecord, append_audit_entry
from apps.api.app.runs.persistence import TestRunRecord as RunModelRecord
from apps.api.app.operations.audit import AuditForbidden, AuditRepository
from apps.api.app.operations.persistence import (
    QuotaPolicyRecord,
    QuotaReservationRecord,
    SystemSettingChangeRecord,
    SystemSettingRecord,
)
from apps.api.app.operations.quotas import (
    QuotaPolicyForbidden,
    QuotaPolicyService,
)
from apps.api.app.operations.settings import (
    SettingForbidden,
    SettingInvalid,
    SettingStale,
    SystemSettingService,
)
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
)


@pytest.fixture
def operations_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine, tables=[
        UserRecord.__table__, OrganizationRecord.__table__,
        OrganizationMembershipRecord.__table__, AuditLogRecord.__table__,
        QuotaPolicyRecord.__table__, QuotaReservationRecord.__table__,
        UsageMetricRecord.__table__, RunModelRecord.__table__,
        SystemSettingRecord.__table__, SystemSettingChangeRecord.__table__,
    ])
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    requester_id, approver_id, member_id, outsider_id, organization_id = (
        uuid4() for _ in range(5)
    )
    with sessions.begin() as session:
        for user_id, role in (
            (requester_id, "platform_admin"),
            (approver_id, "platform_admin"),
            (member_id, "user"),
            (outsider_id, "user"),
        ):
            session.add(UserRecord(
                id=user_id, email=f"{user_id}@example.test", password_hash="hash",
                display_name="Operations User", platform_role=role, status="active",
                created_at=now, email_verified_at=now,
            ))
        session.add(OrganizationRecord(
            id=organization_id, name="Operations Org", slug=f"ops-{organization_id.hex[:12]}",
            created_at=now,
        ))
        session.add_all([
            OrganizationMembershipRecord(
                organization_id=organization_id, user_id=member_id,
                role="org_member", created_at=now,
            ),
            OrganizationMembershipRecord(
                organization_id=organization_id, user_id=requester_id,
                role="org_admin", created_at=now,
            ),
        ])
        session.add(SystemSettingRecord(
            id=uuid4(), key="maintenance_mode", value=False, updated_at=now,
        ))
    yield {
        "engine": engine, "sessions": sessions, "now": now,
        "requester_id": requester_id, "approver_id": approver_id,
        "member_id": member_id, "outsider_id": outsider_id,
        "organization_id": organization_id,
    }
    engine.dispose()


def _append(operations_db, *, actor_id=None, index=0):
    with operations_db["sessions"].begin() as session:
        return append_audit_entry(
            session,
            organization_id=operations_db["organization_id"],
            actor_user_id=actor_id or operations_db["member_id"],
            action=f"case.updated.{index}",
            resource_type="test_case",
            resource_id=uuid4(),
            metadata={"sequence": index},
            created_at=operations_db["now"] + timedelta(seconds=index),
        )


def test_audit_scope_and_keyset_pagination(operations_db):
    entries = [_append(operations_db, index=index) for index in range(3)]
    repository = AuditRepository(operations_db["sessions"])

    first = repository.list(
        operations_db["member_id"], operations_db["organization_id"],
        filters={}, limit=2, cursor=None,
    )
    second = repository.list(
        operations_db["member_id"], operations_db["organization_id"],
        filters={}, limit=2, cursor=first["next_cursor"],
    )

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {entry.id for entry in entries}
    with pytest.raises(AuditForbidden):
        repository.list(
            operations_db["outsider_id"], operations_db["organization_id"],
            filters={}, limit=20, cursor=None,
        )


def test_audit_chain_verifier_detects_modified_entry(operations_db):
    _append(operations_db, index=0)
    entry = _append(operations_db, index=1)
    with operations_db["sessions"].begin() as session:
        stored = session.get(AuditLogRecord, entry.id)
        stored.metadata_json = {"sequence": "tampered"}

    result = AuditRepository(operations_db["sessions"]).verify_chain(
        operations_db["organization_id"],
    )

    assert result.valid is False
    assert result.first_invalid_entry_id == entry.id


def test_audit_verification_requires_organization_scope(operations_db):
    repository = AuditRepository(operations_db["sessions"])

    with pytest.raises(ValueError):
        repository.verify_chain(None)
    with pytest.raises(AuditForbidden):
        repository.list(
            operations_db["outsider_id"], operations_db["organization_id"],
            filters={}, limit=20, cursor=None,
        )


def test_setting_request_does_not_change_current_value(operations_db):
    service = SystemSettingService(operations_db["sessions"])

    proposal = service.request_change(
        operations_db["requester_id"], "maintenance_mode", value=True,
        reason="Enable maintenance window", now=operations_db["now"],
    )

    assert proposal["status"] == "PENDING"
    assert "proposed_value" not in proposal
    with operations_db["sessions"]() as session:
        current = session.scalar(select(SystemSettingRecord).where(
            SystemSettingRecord.key == "maintenance_mode",
        ))
        assert current.value is False


def test_requester_cannot_approve_own_change(operations_db):
    service = SystemSettingService(operations_db["sessions"])
    proposal = service.request_change(
        operations_db["requester_id"], "maintenance_mode", value=True,
        reason="Enable maintenance window", now=operations_db["now"],
    )

    with pytest.raises(SettingForbidden):
        service.decide_change(
            operations_db["requester_id"], proposal["id"], decision="approve",
            reason="Self approval", now=operations_db["now"] + timedelta(minutes=1),
        )


def test_approval_and_audit_commit_atomically(operations_db):
    service = SystemSettingService(operations_db["sessions"])
    proposal = service.request_change(
        operations_db["requester_id"], "maintenance_mode", value=True,
        reason="Enable maintenance window", now=operations_db["now"],
    )

    result = service.decide_change(
        operations_db["approver_id"], proposal["id"], decision="approve",
        reason="Reviewed maintenance window", now=operations_db["now"] + timedelta(minutes=1),
    )

    with operations_db["sessions"]() as session:
        current = session.scalar(select(SystemSettingRecord).where(
            SystemSettingRecord.key == "maintenance_mode",
        ))
        change = session.get(SystemSettingChangeRecord, proposal["id"])
        audit = session.scalar(select(AuditLogRecord).where(
            AuditLogRecord.action == "system_setting.approved",
        ))
        assert current.value is True
        assert change.status == "APPROVED"
        assert change.approved_by == operations_db["approver_id"]
        assert audit is not None
        assert audit.metadata_json["requester_id"] == str(operations_db["requester_id"])
        assert audit.metadata_json["approver_id"] == str(operations_db["approver_id"])
        assert "proposed_value" not in audit.metadata_json
        assert "value" not in audit.metadata_json
    assert result["status"] == "APPROVED"


def test_stale_proposal_conflicts_and_is_recorded(operations_db):
    service = SystemSettingService(operations_db["sessions"])
    proposal = service.request_change(
        operations_db["requester_id"], "maintenance_mode", value=True,
        reason="Enable maintenance window", now=operations_db["now"],
    )
    with operations_db["sessions"].begin() as session:
        current = session.scalar(select(SystemSettingRecord).where(
            SystemSettingRecord.key == "maintenance_mode",
        ))
        current.value = True

    with pytest.raises(SettingStale):
        service.decide_change(
            operations_db["approver_id"], proposal["id"], decision="approve",
            reason="Current setting changed", now=operations_db["now"] + timedelta(minutes=1),
        )

    with operations_db["sessions"]() as session:
        assert session.get(SystemSettingChangeRecord, proposal["id"]).status == "STALE"


def test_secrets_are_not_written_to_audit_metadata(operations_db):
    service = SystemSettingService(operations_db["sessions"])

    with pytest.raises(SettingInvalid):
        service.request_change(
            operations_db["requester_id"], "provider_api_key", value="sk-live-secret",
            reason="Rotate provider key", now=operations_db["now"],
        )

    with operations_db["sessions"]() as session:
        assert session.scalar(select(SystemSettingChangeRecord)) is None
        assert session.scalar(select(AuditLogRecord)) is None


def test_quota_policy_and_usage_are_organization_scoped(operations_db):
    service = QuotaPolicyService(operations_db["sessions"])
    with operations_db["sessions"].begin() as session:
        session.add(UsageMetricRecord(
            organization_id=operations_db["organization_id"], run_id=None,
            metric_name="provider_tokens", value=Decimal("42"), unit="token",
            created_at=operations_db["now"],
        ))

    policy = service.get_policy(operations_db["member_id"], operations_db["organization_id"])
    updated = service.update_policy(
        operations_db["requester_id"], operations_db["organization_id"],
        max_concurrent_runs=7, monthly_token_limit=12345, now=operations_db["now"],
    )
    usage = service.list_usage(operations_db["member_id"], operations_db["organization_id"])

    assert policy["monthly_token_limit"] == 1_000_000
    assert updated["max_concurrent_runs"] == 7
    assert usage["items"][0]["value"] == 42
    with pytest.raises(QuotaPolicyForbidden):
        service.update_policy(
            operations_db["outsider_id"], operations_db["organization_id"],
            max_concurrent_runs=1, monthly_token_limit=0, now=operations_db["now"],
        )


def test_quota_policy_exposes_committed_reserved_and_remaining_monthly_tokens(operations_db):
    with operations_db["sessions"].begin() as session:
        session.add(QuotaPolicyRecord(
            id=uuid4(), organization_id=operations_db["organization_id"],
            max_concurrent_runs=3, monthly_token_limit=100, updated_at=operations_db["now"],
        ))
        session.add_all([
            QuotaReservationRecord(
                id=uuid4(), organization_id=operations_db["organization_id"], run_id=None,
                call_id=str(uuid4()), tokens_reserved=42, tokens_consumed=42,
                status="COMMITTED", period_month=date(2026, 11, 1),
                created_at=operations_db["now"], closed_at=operations_db["now"],
                settled_at=operations_db["now"], actual_cost=None,
            ),
            QuotaReservationRecord(
                id=uuid4(), organization_id=operations_db["organization_id"], run_id=None,
                call_id=str(uuid4()), tokens_reserved=58, tokens_consumed=0,
                status="RESERVED", period_month=date(2026, 11, 1),
                created_at=operations_db["now"], closed_at=None, settled_at=None,
                actual_cost=None,
            ),
        ])

    policy = QuotaPolicyService(operations_db["sessions"]).get_policy(
        operations_db["member_id"], operations_db["organization_id"],
        now=operations_db["now"],
    )

    assert policy["monthly_tokens_committed"] == 42
    assert policy["monthly_tokens_reserved"] == 58
    assert policy["monthly_tokens_remaining"] == 0
    assert policy["quota_exhausted"] is True
