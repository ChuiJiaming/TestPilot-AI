from datetime import datetime, timedelta, timezone
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.planning import (
    OutboxRecord, PlanningRepository, ScheduleRecord, ScheduleTriggerRecord,
    TestPlanRecord as PlanRecord,
)
from apps.api.app.runs.persistence import (
    AgentEventRecord, ProcessedEventRecord, RunAdmissionRepository,
    TestRunRecord as RunRecord,
)
from apps.api.app.runs.repository import RunRepository
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord, OrganizationRecord,
    ProjectMembershipRecord, ProjectRecord,
)


@pytest.fixture
def execution_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
    owner_id, organization_id, project_id = uuid4(), uuid4(), uuid4()
    plan_id, environment_id, schedule_id = uuid4(), uuid4(), uuid4()
    run_ids = [uuid4(), uuid4()]
    counters = {model: count(1) for model in (AgentEventRecord, OutboxRecord)}

    def assign_bigint_id(_mapper, _connection, target):
        if target.id is None:
            target.id = next(counters[type(target)])

    for model in counters:
        event.listen(model, "before_insert", assign_bigint_id)

    with sessions.begin() as session:
        session.add(UserRecord(
            id=owner_id, email=f"{owner_id.hex}@example.invalid", password_hash="hash",
            display_name="Owner", platform_role="user", status="active", created_at=now,
        ))
        session.add(OrganizationRecord(
            id=organization_id, name="Execution", slug=f"exec-{project_id.hex[:10]}",
            created_at=now,
        ))
        session.add(ProjectRecord(
            id=project_id, organization_id=organization_id, name="Execution",
            slug=f"exec-{project_id.hex[:10]}", created_by=owner_id, created_at=now,
        ))
        session.add(OrganizationMembershipRecord(
            organization_id=organization_id, user_id=owner_id,
            role="org_member", created_at=now,
        ))
        session.add(ProjectMembershipRecord(
            project_id=project_id, user_id=owner_id, role="tester", created_at=now,
        ))
        session.add(PlanRecord(
            id=plan_id, project_id=project_id, name="Execution plan", suite_id=None,
            policy={"concurrency_mode": "serial", "max_concurrent_runs": 1},
            created_at=now,
        ))
        for index, (run_id, status) in enumerate(zip(run_ids, ("RUNNING", "QUEUED"))):
            session.add(RunRecord(
                id=run_id, project_id=project_id, plan_id=plan_id,
                environment_id=environment_id, environment_snapshot_id=None,
                parent_run_id=None, requested_by=owner_id, request_intent=None,
                branch=None, commit_sha=None, event_replay_floor=0, status=status,
                trigger_source="manual", browser_matrix=["chromium"],
                idempotency_key=f"execution-{index}", policy={}, lease_owner=None,
                lease_expires_at=None, heartbeat_at=None, claimed_at=None,
                attempt_token=None, started_at=None, finished_at=None, created_at=now,
            ))
        session.add(ScheduleRecord(
            id=schedule_id, plan_id=plan_id, environment_id=environment_id,
            cron_expression="0 9 * * 1-5", timezone="UTC", enabled=True,
            next_fire_at=now + timedelta(days=1), last_fire_at=None, created_at=now,
        ))

    yield {
        "sessions": sessions, "now": now, "owner_id": owner_id,
        "project_id": project_id, "plan_id": plan_id,
        "run_ids": run_ids, "schedule_id": schedule_id,
    }

    for model in counters:
        event.remove(model, "before_insert", assign_bigint_id)
    engine.dispose()


def test_plan_concurrency_cap_keeps_extra_run_queued(execution_db):
    admission = RunAdmissionRepository(execution_db["sessions"])

    result = admission.claim(
        execution_db["run_ids"][1], worker_id="worker-a", now=execution_db["now"],
    )

    assert result == {
        "claimed": False, "reason": "PLAN_CONCURRENCY_LIMIT", "active_runs": 1,
    }
    with execution_db["sessions"]() as session:
        assert session.get(RunRecord, execution_db["run_ids"][1]).status == "QUEUED"


def test_run_requested_event_admits_once_and_duplicate_delivery_is_inert(execution_db):
    event_id = 1
    with execution_db["sessions"].begin() as session:
        session.add(OutboxRecord(
            id=event_id, aggregate_type="run", aggregate_id=execution_db["run_ids"][1],
            event_type="RUN_REQUESTED", payload={"run_id": str(execution_db["run_ids"][1])},
            status="PENDING", attempts=0, available_at=execution_db["now"],
            created_at=execution_db["now"],
        ))
        plan = session.get(PlanRecord, execution_db["plan_id"])
        plan.policy = {"concurrency_mode": "bounded", "max_concurrent_runs": 2}

    repository = RunRepository(execution_db["sessions"])
    first = repository.consume_run_requested(
        event_id, worker_id="worker-a", now=execution_db["now"],
    )
    duplicate = repository.consume_run_requested(
        event_id, worker_id="worker-b", now=execution_db["now"] + timedelta(seconds=1),
    )

    assert first["claimed"] is True
    assert duplicate == {
        "claimed": False, "duplicate": True, "reason": "ALREADY_PROCESSED",
    }
    with execution_db["sessions"]() as session:
        run = session.get(RunRecord, execution_db["run_ids"][1])
        assert run.status == "PREPARING"
        assert run.lease_owner == "worker-a"
        assert session.scalar(select(func.count()).select_from(AgentEventRecord).where(
            AgentEventRecord.run_id == run.id,
            AgentEventRecord.event_type == "run.started",
        )) == 1
        assert session.get(ProcessedEventRecord, {
            "consumer_name": "run-admission-v1", "event_id": event_id,
        }) is not None


def test_manual_schedule_trigger_is_idempotent_and_creates_one_outbox_event(execution_db):
    repository = PlanningRepository(execution_db["sessions"])

    repository.trigger_schedule(
        execution_db["owner_id"], execution_db["schedule_id"],
        idempotency_key="manual-trigger-1", request_intent="Run the plan",
        now=execution_db["now"],
    )
    repository.trigger_schedule(
        execution_db["owner_id"], execution_db["schedule_id"],
        idempotency_key="manual-trigger-1", request_intent="Duplicate delivery",
        now=execution_db["now"] + timedelta(seconds=1),
    )

    with execution_db["sessions"]() as session:
        triggers = list(session.scalars(select(ScheduleTriggerRecord).where(
            ScheduleTriggerRecord.schedule_id == execution_db["schedule_id"]
        )).all())
        events = list(session.scalars(select(OutboxRecord).where(
            OutboxRecord.aggregate_type == "schedule",
            OutboxRecord.aggregate_id == execution_db["schedule_id"],
            OutboxRecord.event_type == "SCHEDULE_TRIGGER_REQUESTED",
        )).all())
        assert len(triggers) == len(events) == 1
        assert triggers[0].status == "ENQUEUED"
        assert triggers[0].outbox_event_id == events[0].id
