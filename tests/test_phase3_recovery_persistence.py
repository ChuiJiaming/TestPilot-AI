from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.persistence import (
    CaseApprovalRecord,
    TestCaseRecord as CaseRecord,
    TestCaseVersionRecord as CaseVersionRecord,
)
from apps.api.app.environments.persistence import EnvironmentRecord, EnvironmentSnapshotRecord
from apps.api.app.runs.persistence import (
    RunCaseRecord,
    TestRunRecord as RunRecord,
    TestStepRecord as StepRecord,
    WorkerLeaseRecord,
)
from apps.api.app.runs.repository import RunRepository, StaleRunAttempt
from apps.api.app.tenancy.persistence import OrganizationRecord, ProjectRecord


def test_expired_run_with_unknown_non_idempotent_action_requires_reconciliation():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = RunRepository(sessions)
    now = datetime(2026, 10, 30, 8, 0, 0)
    old_attempt = uuid4()
    user_id = uuid4()
    organization_id = uuid4()
    project_id = uuid4()
    environment_id = uuid4()
    snapshot_id = uuid4()
    case_id = uuid4()
    version_id = uuid4()
    run_id = uuid4()
    run_case_id = uuid4()
    step_id = uuid4()
    action_id = uuid4()

    try:
        with sessions.begin() as session:
            session.add(UserRecord(
                id=user_id, email=f"{user_id.hex}@example.invalid", password_hash="hash",
                display_name="Recovery Test", platform_role="user", status="active",
                created_at=now,
            ))
            session.add(OrganizationRecord(
                id=organization_id, name="Recovery Test", slug=f"recovery-{user_id.hex[:10]}",
                created_at=now,
            ))
            session.add(ProjectRecord(
                id=project_id, organization_id=organization_id, name="Recovery Test",
                slug=f"recovery-{user_id.hex[:10]}", created_by=user_id, created_at=now,
            ))
            session.add(EnvironmentRecord(
                id=environment_id, project_id=project_id, name="test",
                base_url="https://example.com", network_policy={"allowed_domains": ["example.com"]},
                created_at=now,
            ))
            session.add(EnvironmentSnapshotRecord(
                id=snapshot_id, environment_id=environment_id, project_id=project_id,
                base_url="https://example.com", network_policy={"allowed_domains": ["example.com"]},
                snapshot_hash=uuid4().hex, created_at=now,
            ))
            session.add(CaseRecord(
                id=case_id, project_id=project_id, title="Recovery case", priority="P1",
                status="active", created_at=now,
            ))
            session.add(CaseVersionRecord(
                id=version_id, case_id=case_id, version=1, dsl_schema_version=2,
                steps=[], assertions=[], changelog="test", created_at=now,
            ))
            session.add(RunRecord(
                id=run_id, project_id=project_id, plan_id=None, environment_id=environment_id,
                environment_snapshot_id=snapshot_id, parent_run_id=None, requested_by=user_id,
                request_intent=None, branch=None, commit_sha=None, event_replay_floor=0,
                status="RUNNING", trigger_source="manual", browser_matrix=["chromium"],
                idempotency_key=f"recovery-{run_id}", policy={}, lease_owner="agent-old",
                lease_expires_at=now - timedelta(seconds=1), heartbeat_at=now - timedelta(minutes=2),
                claimed_at=now - timedelta(minutes=3), attempt_token=old_attempt,
                started_at=now - timedelta(minutes=4), created_at=now - timedelta(minutes=5),
            ))
            session.add(RunCaseRecord(
                id=run_case_id, run_id=run_id, case_version_id=version_id,
                browser_name="chromium", status="RUNNING",
            ))
            session.add(StepRecord(
                id=step_id, run_case_id=run_case_id, action_id=action_id, sequence=1,
                action_type="click", locator={"by": "role", "value": "button"},
                expected=None, status="RUNNING", replay_policy="NEVER",
                idempotency_key=f"{run_case_id}:{action_id}:{old_attempt}",
                attempt_token=old_attempt, started_at=now - timedelta(minutes=1),
            ))
            session.add(WorkerLeaseRecord(
                id=uuid4(), resource_type="run", resource_id=run_id,
                lease_owner="agent-old", attempt_token=old_attempt,
                lease_expires_at=now - timedelta(seconds=1),
                heartbeat_at=now - timedelta(minutes=2), claimed_at=now - timedelta(minutes=3),
            ))

        # SQLite cannot model PostgreSQL row locks; this regression targets the recovery
        # transaction's persisted lease, action state, and approval decisions.
        with (
            patch.object(RunRepository, "_append_event", staticmethod(lambda *_a, **_k: None)),
            patch.object(RunRepository, "_audit_approval", staticmethod(lambda *_a, **_k: None)),
        ):
            result = repository.reconcile_expired_run(
                run_id, worker_id="agent-recovery", now=now,
            )

        assert result["recovered"] is True
        assert result["manual_reconciliation"] is True
        assert result["attempt_token"] != old_attempt
        with sessions() as session:
            run = session.get(RunRecord, run_id)
            step = session.get(StepRecord, step_id)
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.run_id == run_id,
                CaseApprovalRecord.status == "PENDING",
            ))
            lease = session.scalar(select(WorkerLeaseRecord).where(
                WorkerLeaseRecord.resource_type == "run",
                WorkerLeaseRecord.resource_id == run_id,
            ))
            assert run.status == "WAITING_APPROVAL"
            assert run.attempt_token == result["attempt_token"]
            assert run.lease_owner is None
            assert step.status == "WAITING_APPROVAL"
            assert approval is not None
            assert step.id in [UUID(value) for value in approval.payload["step_ids"]]
            assert lease is None

        with pytest.raises(StaleRunAttempt):
            repository.start_step(
                run_case_id, action_id=action_id, sequence=1, action_type="click",
                locator=None, expected=None, replay_policy="NEVER",
                attempt_token=old_attempt, worker_id="agent-old", now=now,
            )
    finally:
        engine.dispose()
