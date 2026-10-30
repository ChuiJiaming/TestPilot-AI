from datetime import datetime, timedelta, timezone
from itertools import count
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.persistence import (
    CaseApprovalRecord,
    CasePermissionDenied,
    CaseRepository,
    TestCaseRecord as CaseRecord,
    TestCaseVersionRecord as CaseVersionRecord,
)
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.core.audit import AuditLogRecord
from apps.api.app.environments.persistence import EnvironmentRecord
from apps.api.app.runs.persistence import (
    AgentEventRecord,
    BrowserSessionRecord,
    HealingRecord,
    RunCaseRecord,
    TestRunRecord as RunRecord,
    TestStepRecord as StepRecord,
    WorkerLeaseRecord,
)
from apps.api.app.runs.repository import (
    RunApprovalConflict,
    RunApprovalPermissionDenied,
    RunRepository,
    RunScopeDenied,
)
from apps.api.app.runs.recovery import RecoveryProposal
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


@pytest.fixture
def recovery_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repo = RunRepository(sessions)
    now = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
    requester_id, reviewer_id, outsider_id = uuid4(), uuid4(), uuid4()
    organization_id, project_id = uuid4(), uuid4()
    environment_id, case_id, version_id = uuid4(), uuid4(), uuid4()
    run_id, run_case_id, step_id = uuid4(), uuid4(), uuid4()
    action_id = uuid5(NAMESPACE_URL, f"testpilot:v2:{version_id}:1")
    attempt_token = uuid4()
    session_id = uuid4()
    source_steps = [{
        "action": "click",
        "locator": {"by": "role", "value": "button", "name": "Continue"},
    }]
    source_assertions = [{"type": "url", "expected": "/done", "match": "contains"}]

    with sessions.begin() as session:
        for user_id, label in (
            (requester_id, "Requester"),
            (reviewer_id, "Reviewer"),
            (outsider_id, "Outsider"),
        ):
            session.add(UserRecord(
                id=user_id, email=f"{user_id.hex}@example.invalid", password_hash="hash",
                display_name=label, platform_role="user", status="active", created_at=now,
            ))
        session.add(OrganizationRecord(
            id=organization_id, name="Recovery", slug=f"recovery-{project_id.hex[:10]}",
            created_at=now,
        ))
        session.add(ProjectRecord(
            id=project_id, organization_id=organization_id, name="Recovery",
            slug=f"recovery-{project_id.hex[:10]}", created_by=requester_id, created_at=now,
        ))
        for user_id in (requester_id, reviewer_id):
            session.add(OrganizationMembershipRecord(
                organization_id=organization_id, user_id=user_id,
                role="org_member", created_at=now,
            ))
            session.add(ProjectMembershipRecord(
                project_id=project_id, user_id=user_id, role="tester", created_at=now,
            ))
        session.add(EnvironmentRecord(
            id=environment_id, project_id=project_id, name="test",
            base_url="https://example.invalid", network_policy={}, created_at=now,
        ))
        session.add(CaseRecord(
            id=case_id, project_id=project_id, title="Recovery case", priority="P1",
            status="active", published_version_id=version_id, created_at=now,
        ))
        session.add(CaseVersionRecord(
            id=version_id, case_id=case_id, version=1, dsl_schema_version=2,
            dsl_title="Recovery case", version_name="v1", steps=source_steps,
            assertions=source_assertions, changelog="initial", review_status="APPROVED",
            review_reason=None, created_by=requester_id, created_at=now - timedelta(days=1),
            approved_by=reviewer_id, approved_at=now - timedelta(days=1),
        ))
        session.add(RunRecord(
            id=run_id, project_id=project_id, plan_id=None, environment_id=environment_id,
            environment_snapshot_id=None, parent_run_id=None, requested_by=requester_id,
            request_intent=None, branch=None, commit_sha=None, event_replay_floor=0,
            status="RUNNING", trigger_source="manual", browser_matrix=["chromium"],
            idempotency_key=f"recovery-{run_id}", policy={}, lease_owner="worker-1",
            lease_expires_at=now + timedelta(seconds=60), heartbeat_at=now,
            claimed_at=now, attempt_token=attempt_token, started_at=now, finished_at=None,
            created_at=now,
        ))
        session.add(RunCaseRecord(
            id=run_case_id, run_id=run_id, case_version_id=version_id,
            browser_name="chromium", status="RUNNING", lease_owner="worker-1",
            lease_expires_at=now + timedelta(seconds=60), heartbeat_at=now,
            attempt_token=attempt_token, started_at=now, finished_at=None,
        ))
        session.add(StepRecord(
            id=step_id, run_case_id=run_case_id, action_id=action_id, sequence=1,
            action_type="click", locator=source_steps[0]["locator"], expected=None,
            status="FAILED", replay_policy="NEVER", idempotency_key="initial-action",
            attempt_token=attempt_token, started_at=now, finished_at=now,
        ))
        session.add(BrowserSessionRecord(
            id=session_id, run_case_id=run_case_id, browser="chromium",
            worker_instance="browser-worker-a", attempt_token=attempt_token,
            status="READY", lease_expires_at=now + timedelta(minutes=5),
            heartbeat_at=now, created_at=now, closed_at=None,
        ))
        session.add(WorkerLeaseRecord(
            id=uuid4(), resource_type="run", resource_id=run_id,
            lease_owner="worker-1", attempt_token=attempt_token,
            lease_expires_at=now + timedelta(seconds=60), heartbeat_at=now, claimed_at=now,
        ))

    # PostgreSQL bigserial columns need explicit IDs in this SQLite contract fixture.
    counters = {model: count(1) for model in (AgentEventRecord, AuditLogRecord, OutboxRecord)}
    listeners = []
    for model, sequence in counters.items():
        def assign_id(_mapper, _connection, target, sequence=sequence):
            if target.id is None:
                target.id = next(sequence)
        event.listen(model, "before_insert", assign_id)
        listeners.append((model, assign_id))
    yield {
        "engine": engine, "sessions": sessions, "repo": repo, "now": now,
        "requester_id": requester_id, "reviewer_id": reviewer_id,
        "outsider_id": outsider_id, "organization_id": organization_id,
        "project_id": project_id, "case_id": case_id, "version_id": version_id,
        "run_id": run_id, "run_case_id": run_case_id, "step_id": step_id,
        "action_id": action_id, "attempt_token": attempt_token,
        "session_id": session_id, "source_steps": source_steps,
        "source_assertions": source_assertions,
    }
    for model, listener in listeners:
        event.remove(model, "before_insert", listener)
    engine.dispose()


@pytest.fixture
def db(recovery_db):
    return recovery_db


def _proposal(action_id):
    return RecoveryProposal(
        action_id=action_id,
        locator={"by": "role", "value": "button", "name": "Continue order"},
        source="a11y_tree", confidence=0.74,
        semantic_descriptor={"role": "button", "name": "Continue order"},
    )


def _propose(db):
    return db["repo"].propose_healing(
        db["run_id"], db["run_case_id"], db["step_id"],
        old_locator=db["source_steps"][0]["locator"],
        proposal=_proposal(db["action_id"]),
        evidence={
            "verification": {
                "match_count": 1, "visible": True,
                "descriptor": {"role": "button", "name": "Continue order"},
            },
            "observation": {
                "tree": [{"role": "button", "name": "Continue order"}],
                "text": "secret form contents",
            },
        },
        requester_id=db["requester_id"], now=db["now"],
    )


def test_propose_healing_atomically_parks_run_and_creates_scoped_review(db):
    result = _propose(db)
    retried = _propose(db)

    assert result["expires_at"] == db["now"] + timedelta(minutes=30)
    assert result["healing_id"]
    assert result["approval_id"]
    assert retried["healing_id"] == result["healing_id"]
    assert retried["approval_id"] == result["approval_id"]
    assert retried["expires_at"].replace(tzinfo=timezone.utc) == result["expires_at"]
    with db["sessions"]() as session:
        run = session.get(RunRecord, db["run_id"])
        run_case = session.get(RunCaseRecord, db["run_case_id"])
        step = session.get(StepRecord, db["step_id"])
        healing = session.get(HealingRecord, result["healing_id"])
        approval = session.get(CaseApprovalRecord, result["approval_id"])
        events = session.scalars(select(AgentEventRecord).where(
            AgentEventRecord.run_id == db["run_id"]
        )).all()
        audit = session.scalars(select(AuditLogRecord).where(
            AuditLogRecord.resource_id == result["healing_id"]
        )).all()
        outbox = session.scalars(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == db["run_id"],
            OutboxRecord.event_type == "RUN_RESUME_REQUESTED",
        )).all()

        assert run.status == "WAITING_APPROVAL"
        assert run.lease_owner is None
        assert run_case.status == "WAITING_APPROVAL"
        assert step.status == "WAITING_APPROVAL"
        assert healing.status == "PENDING_APPROVAL"
        assert healing.old_locator == db["source_steps"][0]["locator"]
        assert healing.new_locator == _proposal(db["action_id"]).locator
        assert "text" not in healing.evidence["observation"]
        assert approval.target_kind == "healing"
        assert approval.target_id == healing.id
        assert approval.project_id == db["project_id"]
        assert approval.run_id is None
        assert approval.correlation_key == f"healing:{healing.id}"
        assert approval.expires_at.replace(tzinfo=timezone.utc) == result["expires_at"]
        assert any(item.event_type == "run.healing_requested" for item in events)
        assert len([item for item in events if item.event_type == "run.healing_requested"]) == 1
        assert len(audit) == 1
        assert outbox == []


def test_repository_rechecks_worker_verification_before_persisting_candidate(db):
    with pytest.raises(RunApprovalConflict, match="MATCH_COUNT_NOT_ONE"):
        db["repo"].propose_healing(
            db["run_id"], db["run_case_id"], db["step_id"],
            old_locator=db["source_steps"][0]["locator"],
            proposal=_proposal(db["action_id"]),
            evidence={"verification": {"match_count": 2, "visible": True,
                                        "descriptor": {"role": "button", "name": "Continue order"}}},
            requester_id=db["requester_id"], now=db["now"],
        )
    with db["sessions"]() as session:
        assert session.scalar(select(HealingRecord.id)) is None
        assert session.get(RunRecord, db["run_id"]).status == "RUNNING"


def test_healing_decision_rechecks_project_write_access_and_requester_separation(db):
    request = _propose(db)

    with pytest.raises(RunScopeDenied):
        db["repo"].get_healing(db["outsider_id"], request["healing_id"])
    with pytest.raises(RunScopeDenied):
        db["repo"].decide_healing(
            db["outsider_id"], request["healing_id"],
            decision="APPROVE", reason="Looks right", now=db["now"],
        )
    with pytest.raises(RunApprovalPermissionDenied):
        db["repo"].decide_healing(
            db["requester_id"], request["healing_id"],
            decision="APPROVE", reason="Self review", now=db["now"],
        )

    review = db["repo"].get_healing(db["reviewer_id"], request["healing_id"])
    assert review["id"] == request["healing_id"]
    assert review["source"] == "a11y_tree"
    assert review["evidence"]["verification"]["match_count"] == 1


def test_approval_rotates_attempt_and_emits_exactly_one_resume_intent(db):
    request = _propose(db)
    decision = db["repo"].decide_healing(
        db["reviewer_id"], request["healing_id"], decision="APPROVE",
        reason="Verified the intended Continue control", now=db["now"] + timedelta(minutes=1),
    )

    assert decision["healing_status"] == "APPROVED"
    assert decision["approval_status"] == "APPROVED"
    assert decision["previous_attempt_token"] == db["attempt_token"]
    assert decision["attempt_token"] != db["attempt_token"]
    with db["sessions"]() as session:
        outbox = session.scalars(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == db["run_id"],
            OutboxRecord.event_type == "RUN_RESUME_REQUESTED",
        )).all()
        assert len(outbox) == 1
        assert outbox[0].payload == {
            "run_id": str(db["run_id"]),
            "healing_id": str(request["healing_id"]),
            "previous_attempt_token": str(db["attempt_token"]),
            "attempt_token": str(decision["attempt_token"]),
        }
        run = session.get(RunRecord, db["run_id"])
        assert run.status == "RUNNING"
        assert run.attempt_token == decision["attempt_token"]
        assert run.lease_owner is None
        run_case = session.get(RunCaseRecord, db["run_case_id"])
        assert run_case.attempt_token == decision["attempt_token"]
        assert run_case.status == "RUNNING"

    worker_id = "worker-resumed"
    with db["sessions"].begin() as session:
        run = session.get(RunRecord, db["run_id"])
        run.lease_owner = worker_id
        run.lease_expires_at = db["now"] + timedelta(minutes=2)
        run_case = session.get(RunCaseRecord, db["run_case_id"])
        run_case.lease_owner = worker_id
        run_case.lease_expires_at = run.lease_expires_at
    loaded = db["repo"].load_approved_healing(
        db["run_id"], db["run_case_id"], decision["attempt_token"], worker_id=worker_id,
    )
    assert loaded["healing_id"] == request["healing_id"]
    assert loaded["previous_attempt_token"] == db["attempt_token"]
    assert loaded["session_id"] == db["session_id"]
    assert loaded["worker_instance"] == "browser-worker-a"
    assert db["repo"].load_approved_healing(
        db["run_id"], db["run_case_id"], db["attempt_token"], worker_id=worker_id,
    ) is None

    with pytest.raises(RunApprovalConflict):
        db["repo"].decide_healing(
            db["reviewer_id"], request["healing_id"], decision="REJECT",
            reason="Late conflicting decision", now=db["now"] + timedelta(minutes=2),
        )
    with db["sessions"]() as session:
        assert len(session.scalars(select(OutboxRecord).where(
            OutboxRecord.event_type == "RUN_RESUME_REQUESTED",
        )).all()) == 1


def test_rejection_fails_run_without_enqueuing_replay(db):
    rejected = _propose(db)
    result = db["repo"].decide_healing(
        db["reviewer_id"], rejected["healing_id"], decision="REJECT",
        reason="The target is not the intended control", now=db["now"] + timedelta(minutes=1),
    )
    assert result["healing_status"] == "REJECTED"
    with db["sessions"]() as session:
        assert session.get(RunRecord, db["run_id"]).status == "FAILED"
        assert session.get(HealingRecord, rejected["healing_id"]).status == "REJECTED"
        assert session.scalars(select(OutboxRecord).where(
            OutboxRecord.event_type == "RUN_RESUME_REQUESTED",
        )).all() == []

def test_due_expiry_is_atomic_idempotent_and_does_not_resume(db):
    request = _propose(db)
    expired_at = request["expires_at"] + timedelta(seconds=1)

    assert db["repo"].expire_due_healing_approvals(now=expired_at, limit=100) == 1
    assert db["repo"].expire_due_healing_approvals(now=expired_at, limit=100) == 0
    with db["sessions"]() as session:
        healing = session.get(HealingRecord, request["healing_id"])
        approval = session.get(CaseApprovalRecord, request["approval_id"])
        run = session.get(RunRecord, db["run_id"])
        assert healing.status == "EXPIRED"
        assert approval.status == "EXPIRED"
        assert run.status == "FAILED"
        assert session.scalars(select(OutboxRecord).where(
            OutboxRecord.event_type == "RUN_RESUME_REQUESTED",
        )).all() == []


def test_completion_creates_pending_immutable_case_version_for_original_requester(db):
    request = _propose(db)
    approval_time = db["now"] + timedelta(minutes=1)
    decision = db["repo"].decide_healing(
        db["reviewer_id"], request["healing_id"], decision="APPROVE",
        reason="Verified", now=approval_time,
    )
    worker_id = "worker-resumed"
    with db["sessions"].begin() as session:
        run = session.get(RunRecord, db["run_id"])
        run.lease_owner = worker_id

    result = db["repo"].complete_healing(
        request["healing_id"], db["run_id"], db["run_case_id"],
        decision["attempt_token"], worker_id=worker_id,
        now=approval_time + timedelta(seconds=10),
    )

    with db["sessions"]() as session:
        source = session.get(CaseVersionRecord, db["version_id"])
        recovered = session.get(CaseVersionRecord, result["new_version_id"])
        case = session.get(CaseRecord, db["case_id"])
        approval = session.scalar(select(CaseApprovalRecord).where(
            CaseApprovalRecord.target_kind == "case_version",
            CaseApprovalRecord.target_id == result["new_version_id"],
        ))
        healing = session.get(HealingRecord, request["healing_id"])
        action_index = 0
        expected_action_id = uuid5(
            NAMESPACE_URL, f"testpilot:v2:{db['version_id']}:{action_index + 1}"
        )
        assert expected_action_id == db["action_id"]
        assert recovered.version == 2
        assert recovered.review_status == "PENDING_APPROVAL"
        assert recovered.created_by == db["requester_id"]
        assert recovered.steps == [{
            **db["source_steps"][0],
            "locator": {"by": "role", "value": "button",
                        "name": "Continue order", "exact": False},
        }]
        assert recovered.assertions == db["source_assertions"]
        assert source.steps == db["source_steps"]
        assert source.assertions == db["source_assertions"]
        assert case.published_version_id == db["version_id"]
        assert approval.requested_by == db["requester_id"]
        assert approval.status == "PENDING"
        assert healing.status == "APPLIED"
        assert healing.new_version_id == recovered.id
        assert healing.applied_at is not None
        assert decision["attempt_token"] == result["attempt_token"]

    with pytest.raises(CasePermissionDenied):
        CaseRepository(db["sessions"]).decide_version(
            db["requester_id"], result["new_version_id"],
            decision="APPROVED", reason="Self review", now=approval_time,
        )
