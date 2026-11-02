from datetime import datetime, timezone
from itertools import count
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.persistence import (
    AuthBase,
    SqlAlchemyVerificationRepository,
    UserRecord,
)
from apps.api.app.cases.persistence import (
    TestCaseRecord as CaseRecord,
    TestCaseVersionRecord as CaseVersionRecord,
)
from apps.api.app.cases.planning import (
    OutboxRecord,
    SuiteCaseRecord,
    TestPlanRecord as PlanRecord,
    TestSuiteRecord as SuiteRecord,
)
from apps.api.app.environments.persistence import EnvironmentRecord, EnvironmentSnapshotRepository
from apps.api.app.integrations.ci import CITriggerService
from apps.api.app.runs.persistence import (
    AgentEventRecord,
    ScheduledRunRepository,
    TestRunRecord as RunRecord,
)
from apps.api.app.runs.repository import RunRepository
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


@pytest.fixture
def ci_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    actor_id, organization_id, project_id = uuid4(), uuid4(), uuid4()
    plan_id, suite_id, case_id, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    environment_ids = [uuid4(), uuid4()]
    counters = {model: count(1) for model in (AgentEventRecord, OutboxRecord)}

    def assign_id(_mapper, _connection, target):
        if target.id is None:
            target.id = next(counters[type(target)])

    for model in counters:
        event.listen(model, "before_insert", assign_id)

    with sessions.begin() as session:
        session.add(UserRecord(
            id=actor_id, email=f"{actor_id.hex}@ci.example.test", password_hash="hash",
            display_name="CI Actor", platform_role="user", status="active", created_at=now,
            email_verified_at=now,
        ))
        session.add(OrganizationRecord(
            id=organization_id, name="CI Org", slug=f"ci-{project_id.hex[:12]}", created_at=now,
        ))
        session.add(ProjectRecord(
            id=project_id, organization_id=organization_id, name="CI Project",
            slug=f"ci-{project_id.hex[:12]}", created_by=actor_id, created_at=now,
        ))
        session.add(OrganizationMembershipRecord(
            organization_id=organization_id, user_id=actor_id, role="org_admin", created_at=now,
        ))
        session.add(ProjectMembershipRecord(
            project_id=project_id, user_id=actor_id, role="tester", created_at=now,
        ))
        session.add(SuiteRecord(
            id=suite_id, project_id=project_id, name="CI suite", description=None, created_at=now,
        ))
        session.add(CaseRecord(
            id=case_id, project_id=project_id, title="CI case", priority="P1",
            status="active", published_version_id=version_id, created_at=now,
        ))
        session.add(CaseVersionRecord(
            id=version_id, case_id=case_id, project_id=project_id, version=1, dsl_schema_version=3,
            dsl_title="CI case", version_name="v1", steps=[{"action": "navigate"}],
            assertions=[{"assert_id": str(uuid4()), "severity": "BLOCKER"}],
            changelog="initial", review_status="APPROVED", review_reason=None,
            created_by=actor_id, created_at=now, approved_by=actor_id, approved_at=now,
        ))
        session.add(SuiteCaseRecord(suite_id=suite_id, case_id=case_id, sequence=1))
        session.add(PlanRecord(
            id=plan_id, project_id=project_id, name="CI plan", suite_id=suite_id,
            policy={"concurrency_mode": "serial", "max_concurrent_runs": 1}, created_at=now,
        ))
        for index, environment_id in enumerate(environment_ids, start=1):
            session.add(EnvironmentRecord(
                id=environment_id, project_id=project_id, name=f"CI env {index}",
                base_url=f"https://ci-{index}.example.test", secret_ref_id=None,
                network_policy={"allowed_hosts": [f"ci-{index}.example.test"]}, created_at=now,
            ))

    yield {
        "engine": engine,
        "sessions": sessions,
        "now": now,
        "actor_id": actor_id,
        "organization_id": organization_id,
        "project_id": project_id,
        "plan_id": plan_id,
        "environment_ids": environment_ids,
    }

    for model in counters:
        event.remove(model, "before_insert", assign_id)
    engine.dispose()


def _key(db, *, environment_id=None, browsers=("chromium", "firefox"), request_intent="nightly"):
    return CITriggerService.idempotency_key(
        organization_id=db["organization_id"],
        repository="Acme/Service",
        commit_sha="ABCDEF0123456789",
        plan_id=db["plan_id"],
        environment_id=environment_id or db["environment_ids"][0],
        browser_matrix=list(browsers),
        trigger_source="ci",
        request_intent=request_intent,
    )


def _repository(db):
    return ScheduledRunRepository(
        db["sessions"], EnvironmentSnapshotRepository(db["sessions"])
    )


def test_ci_retry_returns_same_run_and_outbox(ci_db):
    repository = _repository(ci_db)
    key = _key(ci_db)
    kwargs = {
        "project_id": ci_db["project_id"],
        "plan_id": ci_db["plan_id"],
        "environment_id": ci_db["environment_ids"][0],
        "repository": "acme/service",
        "commit_sha": "abcdef0123456789",
        "branch": "main",
        "browser_matrix": ["chromium", "firefox"],
        "request_intent": "nightly",
        "idempotency_key": key,
    }

    first = repository.create_ci_run(
        ci_db["actor_id"], **kwargs, now=ci_db["now"]
    )
    retry = repository.create_ci_run(
        ci_db["actor_id"], **kwargs, now=ci_db["now"].replace(second=1)
    )

    assert first["id"] == retry["id"]
    with ci_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxRecord).where(
            OutboxRecord.event_type == "RUN_REQUESTED",
            OutboxRecord.aggregate_id == first["id"],
        )) == 1


def test_ci_different_environment_or_matrix_is_not_deduplicated(ci_db):
    repository = _repository(ci_db)
    base = {
        "project_id": ci_db["project_id"],
        "plan_id": ci_db["plan_id"],
        "repository": "acme/service",
        "commit_sha": "abcdef0123456789",
        "branch": "main",
        "request_intent": "nightly",
    }
    requests = [
        (ci_db["environment_ids"][0], ["chromium"]),
        (ci_db["environment_ids"][1], ["chromium"]),
        (ci_db["environment_ids"][0], ["firefox"]),
    ]
    runs = [repository.create_ci_run(
        ci_db["actor_id"],
        **base,
        environment_id=environment_id,
        browser_matrix=browsers,
        idempotency_key=_key(ci_db, environment_id=environment_id, browsers=browsers),
        now=ci_db["now"],
    ) for environment_id, browsers in requests]

    assert len({run["id"] for run in runs}) == 3
    with ci_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRecord).where(
            OutboxRecord.event_type == "RUN_REQUESTED"
        )) == 3


def test_ci_browser_matrix_is_sorted_before_hashing(ci_db):
    first = _key(ci_db, browsers=("chromium", "firefox"))
    second = _key(ci_db, browsers=("firefox", "chromium"))
    assert first == second


def test_ci_idempotency_key_includes_every_approved_dimension(ci_db):
    base = {
        "organization_id": ci_db["organization_id"],
        "repository": "Acme/Service",
        "commit_sha": "ABCDEF0123456789",
        "plan_id": ci_db["plan_id"],
        "environment_id": ci_db["environment_ids"][0],
        "browser_matrix": ["chromium"],
        "trigger_source": "ci",
        "request_intent": "nightly",
    }
    baseline = CITriggerService.idempotency_key(**base)
    variants = [
        {**base, "organization_id": uuid4()},
        {**base, "repository": "acme/other"},
        {**base, "commit_sha": "1234567890abcdef"},
        {**base, "plan_id": uuid4()},
        {**base, "environment_id": ci_db["environment_ids"][1]},
        {**base, "browser_matrix": ["firefox"]},
        {**base, "trigger_source": "manual"},
        {**base, "request_intent": "release"},
    ]

    assert all(CITriggerService.idempotency_key(**variant) != baseline for variant in variants)


def test_ci_trigger_route_requires_run_create_scope(ci_db):
    lifecycle = LifecycleRepository(ci_db["sessions"])
    token = lifecycle.create_api_token(
        ci_db["actor_id"], ci_db["organization_id"], name="read only",
        scopes=["run:read"], expires_at=None, now=ci_db["now"],
    )
    auth_repository = SqlAlchemyVerificationRepository(ci_db["sessions"])

    class Sessions:
        def authenticate(self, value):
            if value == "owner":
                return {"id": str(ci_db["actor_id"]), "credential_type": "user"}
            return auth_repository.authenticate_api_token(value, ci_db["now"])

    from apps.api.app.runs.routes import CITriggerRequest, create_run_router

    router = create_run_router(
        repository=RunRepository(ci_db["sessions"]), auth_sessions=Sessions()
    )
    endpoint = next(route.endpoint for route in router.routes
                    if isinstance(route, APIRoute) and route.operation_id == "post__api_v1_ci_trigger")
    with pytest.raises(HTTPException) as caught:
        endpoint(
            payload=CITriggerRequest(
                project_id=ci_db["project_id"], plan_id=ci_db["plan_id"],
                environment_id=ci_db["environment_ids"][0], repository="acme/service",
                commit_sha="abcdef0123456789", branch="main", browser_matrix=["chromium"],
                request_intent="nightly",
            ),
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token["token"]
            ),
        )
    assert caught.value.status_code == 403


def test_ci_route_creates_run_and_gate_route_returns_pending_for_queued_run(ci_db):
    class Sessions:
        def authenticate(self, value):
            if value == "owner":
                return {"id": str(ci_db["actor_id"]), "credential_type": "user"}
            return None

    from apps.api.app.runs.routes import CITriggerRequest, create_run_router

    router = create_run_router(
        repository=RunRepository(ci_db["sessions"]), auth_sessions=Sessions()
    )
    operations = {
        route.operation_id: route.endpoint
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    payload = CITriggerRequest(
        project_id=ci_db["project_id"], plan_id=ci_db["plan_id"],
        environment_id=ci_db["environment_ids"][0], repository="acme/service",
        commit_sha="abcdef0123456789", branch="main", browser_matrix=["chromium"],
        request_intent="nightly",
    )

    created = operations["post__api_v1_ci_trigger"](
        payload=payload,
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="owner"),
    )
    gate = operations["get__api_v1_test_runs__id__gate"](
        id=created["id"], blocking_severity="HIGH",
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="owner"),
    )

    assert created["trigger_source"] == "ci"
    assert created["commit_sha"] == "abcdef0123456789"
    assert gate["run_id"] == created["id"]
    assert gate["status"] == "PENDING"
    with ci_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxRecord).where(
            OutboxRecord.event_type == "RUN_REQUESTED"
        )) == 1
