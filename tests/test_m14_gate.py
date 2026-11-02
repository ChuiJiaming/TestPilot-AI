from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.persistence import (
    TestCaseRecord as CaseRecord,
    TestCaseVersionRecord as CaseVersionRecord,
)
from apps.api.app.environments.persistence import EnvironmentRecord
from apps.api.app.integrations.ci import GateEvaluator
from apps.api.app.runs.persistence import (
    RunCaseRecord,
    TestRunRecord as RunRecord,
    TestStepRecord as StepRecord,
)
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


class GateFixture:
    def __init__(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        self.actor_id, self.organization_id, self.project_id = uuid4(), uuid4(), uuid4()
        self.environment_id, self.run_id = uuid4(), uuid4()
        with self.sessions.begin() as session:
            session.add(UserRecord(
                id=self.actor_id, email=f"{self.actor_id.hex}@gate.example.test",
                password_hash="hash", display_name="Gate Actor", platform_role="user",
                status="active", created_at=self.now, email_verified_at=self.now,
            ))
            session.add(OrganizationRecord(
                id=self.organization_id, name="Gate Org", slug=f"gate-{self.project_id.hex[:12]}",
                created_at=self.now,
            ))
            session.add(ProjectRecord(
                id=self.project_id, organization_id=self.organization_id, name="Gate Project",
                slug=f"gate-{self.project_id.hex[:12]}", created_by=self.actor_id,
                created_at=self.now,
            ))
            session.add(OrganizationMembershipRecord(
                organization_id=self.organization_id, user_id=self.actor_id,
                role="org_admin", created_at=self.now,
            ))
            session.add(ProjectMembershipRecord(
                project_id=self.project_id, user_id=self.actor_id, role="tester",
                created_at=self.now,
            ))
            session.add(EnvironmentRecord(
                id=self.environment_id, project_id=self.project_id, name="Gate Env",
                base_url="https://gate.example.test", secret_ref_id=None,
                network_policy={}, created_at=self.now,
            ))

    def add_case(self, *, priority, assertion_status, result_kind="ASSERTION", run_status="FAILED",
                 extra_step=None):
        case_id, version_id, run_case_id, assertion_id = uuid4(), uuid4(), uuid4(), uuid4()
        with self.sessions.begin() as session:
            session.add(CaseRecord(
                id=case_id, project_id=self.project_id, title="Gate case", priority=priority,
                status="active", published_version_id=version_id, created_at=self.now,
            ))
            session.add(CaseVersionRecord(
                id=version_id, case_id=case_id, project_id=self.project_id,
                version=1, dsl_schema_version=3,
                dsl_title="Gate case", version_name="v1", steps=[],
                assertions=[{
                    "assert_id": str(assertion_id), "type": "url_contains",
                    "expected": "/ok", "severity": "BLOCKER",
                }],
                changelog="initial", review_status="APPROVED", review_reason=None,
                created_by=self.actor_id, created_at=self.now,
                approved_by=self.actor_id, approved_at=self.now,
            ))
            if session.get(RunRecord, self.run_id) is None:
                session.add(RunRecord(
                    id=self.run_id, project_id=self.project_id, plan_id=None,
                    environment_id=self.environment_id, environment_snapshot_id=None,
                    parent_run_id=None, requested_by=self.actor_id, request_intent=None,
                    branch=None, commit_sha=None, event_replay_floor=0,
                    status=run_status, trigger_source="ci", browser_matrix=["chromium"],
                    idempotency_key="gate-run", policy={}, lease_owner=None,
                    lease_expires_at=None, heartbeat_at=None, claimed_at=None,
                    attempt_token=None, started_at=None,
                    finished_at=self.now if run_status in {"PASSED", "FAILED", "ERROR", "CANCELLED"} else None,
                    created_at=self.now,
                ))
            else:
                session.get(RunRecord, self.run_id).status = run_status
            session.add(RunCaseRecord(
                id=run_case_id, run_id=self.run_id, case_version_id=version_id,
                browser_name="chromium", status=run_status, lease_owner=None,
                lease_expires_at=None, heartbeat_at=None, attempt_token=None,
                started_at=self.now, finished_at=self.now,
            ))
            if assertion_status is not None:
                session.add(StepRecord(
                    id=uuid4(), run_case_id=run_case_id, action_id=assertion_id,
                    sequence=1, action_type="assert:url", locator=None,
                    expected={"severity": "BLOCKER"},
                    result_json={"matched": assertion_status == "PASSED", "result_kind": result_kind},
                    status=assertion_status, replay_policy="SAFE", recovery_healing_id=None,
                    idempotency_key=None, attempt_token=None, started_at=self.now,
                    finished_at=self.now,
                ))
            if extra_step is not None:
                session.add(StepRecord(
                    id=uuid4(), run_case_id=run_case_id, action_id=uuid4(),
                    sequence=2, action_type="click", locator=None, expected=None,
                    result_json=extra_step, status="FAILED", replay_policy="NEVER",
                    recovery_healing_id=None, idempotency_key=None, attempt_token=None,
                    started_at=self.now, finished_at=self.now,
                ))
        return assertion_id

    def close(self):
        self.engine.dispose()


def test_gate_is_pending_until_terminal():
    db = GateFixture()
    try:
        db.add_case(priority="P1", assertion_status="PASSED", run_status="RUNNING")
        evaluator = GateEvaluator(db.sessions)

        pending = evaluator.evaluate(db.run_id, blocking_severity="HIGH")
        with db.sessions.begin() as session:
            run = session.get(RunRecord, db.run_id)
            run.status = "PASSED"
            run.finished_at = db.now
        passed = evaluator.evaluate(db.run_id, blocking_severity="HIGH")

        assert pending["status"] == "PENDING"
        assert passed["status"] == "PASS"
    finally:
        db.close()


def test_gate_uses_case_priority_and_assertion_status_only():
    db = GateFixture()
    try:
        db.add_case(priority="P1", assertion_status="FAILED")
        db.add_case(priority="P3", assertion_status="FAILED")

        result = GateEvaluator(db.sessions).evaluate(
            db.run_id, blocking_severity="HIGH"
        )

        assert result["status"] == "FAIL"
        assert result["counts"]["assertions_failed"] == 2
        assert result["counts"]["blocking_failures"] == 1
    finally:
        db.close()


def test_gate_ignores_ai_hypothesis_severity():
    db = GateFixture()
    try:
        db.add_case(
            priority="P1", assertion_status="PASSED", run_status="PASSED",
            extra_step={"ai_hypothesis_severity": "CRITICAL"},
        )

        result = GateEvaluator(db.sessions).evaluate(
            db.run_id, blocking_severity="HIGH"
        )

        assert result["status"] == "PASS"
        assert result["counts"]["blocking_failures"] == 0
    finally:
        db.close()


def test_terminal_gate_stays_pending_until_all_expected_assertions_are_recorded():
    db = GateFixture()
    try:
        db.add_case(priority="P0", assertion_status=None, run_status="FAILED")

        result = GateEvaluator(db.sessions).evaluate(
            db.run_id, blocking_severity="HIGH"
        )

        assert result["status"] == "PENDING"
        assert result["counts"]["assertions_pending"] == 1
        assert result["counts"]["blocking_failures"] == 0
    finally:
        db.close()
