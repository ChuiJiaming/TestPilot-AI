from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.persistence import (
    TestCaseRecord as CaseRecord,
    TestCaseVersionRecord as CaseVersionRecord,
)
from apps.api.app.cases.planning import UsageMetricRecord
from apps.api.app.environments.persistence import (
    EnvironmentRecord,
    EnvironmentSnapshotRecord,
    SecretRefRecord,
)
from apps.api.app.quality.persistence import TestArtifactRecord as ArtifactRecord
from apps.api.app.runs.persistence import (
    AgentEventRecord,
    RunCaseRecord,
    TestRunRecord as RunRecord,
    TestStepRecord as StepRecord,
)
from apps.api.app.runs.repository import RunRepository
from apps.api.app.tenancy.persistence import OrganizationRecord, ProjectRecord


NOW = datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc)
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
ORG_ID = UUID("20000000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
ENV_ID = UUID("40000000-0000-4000-8000-000000000001")
ENV_SNAPSHOT_ID = UUID("50000000-0000-4000-8000-000000000001")
SECRET_REF_ID = UUID("60000000-0000-4000-8000-000000000001")
RUN_ID = UUID("70000000-0000-4000-8000-000000000001")
CASE_A_ID = UUID("80000000-0000-4000-8000-000000000001")
CASE_B_ID = UUID("80000000-0000-4000-8000-000000000002")
VERSION_A_ID = UUID("90000000-0000-4000-8000-000000000001")
VERSION_B_ID = UUID("90000000-0000-4000-8000-000000000002")
RUN_CASE_A_ID = UUID("a0000000-0000-4000-8000-000000000001")
RUN_CASE_B_ID = UUID("a0000000-0000-4000-8000-000000000002")
STEP_A1_ID = UUID("b0000000-0000-4000-8000-000000000001")
STEP_A2_ID = UUID("b0000000-0000-4000-8000-000000000002")
ARTIFACT_ID = UUID("c0000000-0000-4000-8000-000000000001")


class RollBackSnapshot(Exception):
    pass


@pytest.fixture
def report_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AuthBase.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS reports ("
            "id CHAR(32) PRIMARY KEY, run_id CHAR(32) NOT NULL UNIQUE, "
            "summary JSON NOT NULL, snapshot_hash VARCHAR(64), "
            "artifact_id CHAR(32), generated_at DATETIME NOT NULL)"
        ))
        # SQLite does not autoincrement a BIGINT primary key like PostgreSQL does.
        connection.execute(text("DROP TABLE agent_events"))
        connection.execute(text(
            "CREATE TABLE agent_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id CHAR(32) NOT NULL, event_type TEXT NOT NULL, "
            "payload JSON NOT NULL, created_at DATETIME NOT NULL)"
        ))
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(UserRecord(
            id=USER_ID, email="report-owner@example.invalid", password_hash="hash",
            display_name="Report Owner", platform_role="user", status="active",
            created_at=NOW,
        ))
        session.add(OrganizationRecord(
            id=ORG_ID, name="Reports Org", slug="reports-org", created_at=NOW,
        ))
        session.add(ProjectRecord(
            id=PROJECT_ID, organization_id=ORG_ID, name="Reports Project",
            slug="reports-project", created_by=USER_ID, created_at=NOW,
        ))
        session.add(SecretRefRecord(
            id=SECRET_REF_ID, organization_id=ORG_ID, provider="vault",
            external_key="VERY_SECRET_EXTERNAL_KEY", created_at=NOW,
        ))
        session.add(EnvironmentRecord(
            id=ENV_ID, project_id=PROJECT_ID, name="staging",
            base_url="https://staging.example.invalid", secret_ref_id=SECRET_REF_ID,
            network_policy={"allowed_hosts": ["staging.example.invalid"]}, created_at=NOW,
        ))
        session.add(EnvironmentSnapshotRecord(
            id=ENV_SNAPSHOT_ID, environment_id=ENV_ID, project_id=PROJECT_ID,
            base_url="https://staging.example.invalid",
            network_policy={"allowed_hosts": ["staging.example.invalid"]},
            snapshot_hash="snapshot-hash", created_at=NOW,
        ))
        session.add_all([
            CaseRecord(
                id=CASE_A_ID, project_id=PROJECT_ID, title="Mutable case title A",
                priority="P1", status="active", created_at=NOW,
            ),
            CaseRecord(
                id=CASE_B_ID, project_id=PROJECT_ID, title="Mutable case title B",
                priority="P2", status="active", created_at=NOW,
            ),
            CaseVersionRecord(
                id=VERSION_A_ID, case_id=CASE_A_ID, project_id=PROJECT_ID, version=1,
                dsl_schema_version=2, dsl_title="Immutable case A", version_name="v1",
                steps=[], assertions=[], changelog="test", created_at=NOW,
            ),
            CaseVersionRecord(
                id=VERSION_B_ID, case_id=CASE_B_ID, project_id=PROJECT_ID, version=1,
                dsl_schema_version=2, dsl_title="Immutable case B", version_name="v1",
                steps=[], assertions=[], changelog="test", created_at=NOW,
            ),
            RunRecord(
                id=RUN_ID, project_id=PROJECT_ID, plan_id=None, environment_id=ENV_ID,
                environment_snapshot_id=ENV_SNAPSHOT_ID, parent_run_id=None,
                requested_by=USER_ID, request_intent="release-check", branch="release/1.2",
                commit_sha="abc123def456", event_replay_floor=0, status="RUNNING",
                trigger_source="ci", browser_matrix=["firefox", "chromium"],
                idempotency_key="report-run", policy={"api_key": "VERY_SECRET_POLICY_VALUE"},
                lease_owner=None, lease_expires_at=None, heartbeat_at=NOW,
                claimed_at=NOW, attempt_token=None, started_at=NOW, created_at=NOW,
            ),
            RunCaseRecord(
                id=RUN_CASE_B_ID, run_id=RUN_ID, case_version_id=VERSION_B_ID,
                browser_name="firefox", status="FAILED", started_at=NOW,
                finished_at=NOW,
            ),
            RunCaseRecord(
                id=RUN_CASE_A_ID, run_id=RUN_ID, case_version_id=VERSION_A_ID,
                browser_name="chromium", status="PASSED", started_at=NOW,
                finished_at=NOW,
            ),
            StepRecord(
                id=STEP_A2_ID, run_case_id=RUN_CASE_A_ID, action_id=UUID(int=202),
                sequence=2, action_type="assert", locator={"value": "RAW_LOCATOR_SECRET"},
                expected={"value": "RAW_EXPECTED_SECRET"},
                result_json={"error_code": "ASSERTION_FAILED", "message": "raw user data"},
                status="PASSED", replay_policy="SAFE", idempotency_key="step-a2",
                attempt_token=None, started_at=NOW, finished_at=NOW,
            ),
            StepRecord(
                id=STEP_A1_ID, run_case_id=RUN_CASE_A_ID, action_id=UUID(int=201),
                sequence=1, action_type="navigate", locator={"value": "RAW_LOCATOR_SECRET"},
                expected={"value": "RAW_EXPECTED_SECRET"}, result_json={},
                status="PASSED", replay_policy="SAFE", idempotency_key="step-a1",
                attempt_token=None, started_at=NOW, finished_at=NOW,
            ),
            ArtifactRecord(
                id=ARTIFACT_ID, project_id=PROJECT_ID, run_id=RUN_ID,
                run_case_id=RUN_CASE_A_ID, step_id=STEP_A1_ID, action_id=UUID(int=201),
                environment_id=ENV_ID, case_version_id=VERSION_A_ID, browser="chromium",
                artifact_type="trace", content_type="application/zip",
                storage_key="private/path/must-not-leak.zip", sha256="a" * 64,
                size_bytes=42, redacted=True, viewport=None, device_scale_factor=None,
                color_scheme=None, metadata_json={"capture_profile": "trace-v1"},
                created_at=NOW,
            ),
            UsageMetricRecord(
                id=1, organization_id=ORG_ID, run_id=RUN_ID, metric_name="input_tokens",
                value=12, unit="token", created_at=NOW,
            ),
        ])
    yield sessions
    engine.dispose()


def test_run_finished_event_creates_snapshot_in_same_transaction(report_db):
    repository = RunRepository(report_db)

    with pytest.raises(RollBackSnapshot):
        with report_db.begin() as session:
            run = session.get(RunRecord, RUN_ID)
            run.status = "PASSED"
            run.finished_at = NOW
            repository._append_event(session, RUN_ID, "run.finished", {"status": "PASSED"}, NOW)
            assert session.scalar(text("SELECT COUNT(*) FROM reports WHERE run_id = :run_id"),
                                  {"run_id": RUN_ID.hex}) == 1
            assert session.scalar(select(AgentEventRecord.id).where(
                AgentEventRecord.run_id == RUN_ID,
                AgentEventRecord.event_type == "run.finished",
            )) is not None
            raise RollBackSnapshot

    with report_db() as session:
        assert session.get(RunRecord, RUN_ID).status == "RUNNING"
        assert session.scalar(text("SELECT COUNT(*) FROM reports")) == 0
        assert session.scalar(select(AgentEventRecord.id).where(
            AgentEventRecord.run_id == RUN_ID,
            AgentEventRecord.event_type == "run.finished",
        )) is None


def test_snapshot_is_immutable_on_duplicate_finished_event(report_db):
    repository = RunRepository(report_db)
    repository.transition(RUN_ID, "FINALIZING", expected_status="RUNNING", now=NOW)
    repository.transition(RUN_ID, "PASSED", expected_status="FINALIZING", now=NOW)

    with report_db.begin() as session:
        original_json = session.scalar(text(
            "SELECT summary FROM reports WHERE run_id = :run_id"), {"run_id": RUN_ID.hex})
        assert original_json is not None, "terminal transition must create the report snapshot"
        original = json.loads(original_json)
        run = session.get(RunRecord, RUN_ID)
        run.commit_sha = "changed-after-finish"
        repository._append_event(session, RUN_ID, "run.finished", {"status": "PASSED"}, NOW)
        repeated_json = session.scalar(text(
            "SELECT summary FROM reports WHERE run_id = :run_id"), {"run_id": RUN_ID.hex})
        assert repeated_json is not None
        repeated = json.loads(repeated_json)

    assert repeated == original
    assert original["run"]["commit_sha"] == "abc123def456"


def test_snapshot_excludes_secret_values_and_orders_results(report_db):
    repository = RunRepository(report_db)
    repository.transition(RUN_ID, "FINALIZING", expected_status="RUNNING", now=NOW)
    repository.transition(RUN_ID, "PASSED", expected_status="FINALIZING", now=NOW)
    with report_db() as session:
        snapshot_json = session.scalar(text(
            "SELECT summary FROM reports WHERE run_id = :run_id"), {"run_id": RUN_ID.hex})
        assert snapshot_json is not None, "terminal transition must create the report snapshot"
        snapshot = json.loads(snapshot_json)

    serialized = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    for secret in (
        "VERY_SECRET_EXTERNAL_KEY", "VERY_SECRET_POLICY_VALUE", "RAW_LOCATOR_SECRET",
        "RAW_EXPECTED_SECRET", "private/path/must-not-leak.zip", "raw user data",
    ):
        assert secret not in serialized
    assert [case["id"] for case in snapshot["cases"]] == [str(RUN_CASE_A_ID), str(RUN_CASE_B_ID)]
    assert [step["sequence"] for step in snapshot["cases"][0]["steps"]] == [1, 2]
    assert snapshot["cases"][0]["title"] == "Immutable case A"
    assert snapshot["summary"]["counts"] == {
        "total": 2, "passed": 1, "failed": 1, "error": 0, "skipped": 0,
        "cancelled": 0, "queued": 0, "running": 0, "other": 0,
    }
    assert snapshot["artifacts"][0] == {
        "id": str(ARTIFACT_ID), "run_case_id": str(RUN_CASE_A_ID),
        "step_id": str(STEP_A1_ID), "artifact_type": "trace",
        "content_type": "application/zip", "sha256": "a" * 64,
        "size_bytes": 42, "status": "AVAILABLE", "redacted": True,
        "created_at": NOW.isoformat(),
    }
    assert snapshot["usage"] == [{
        "metric_name": "input_tokens", "value": "12.000000", "unit": "token",
        "created_at": NOW.isoformat(),
    }]
    content_hash = snapshot.pop("snapshot_hash")
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert content_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
