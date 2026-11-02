from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.reports.persistence import (
    ReportPermissionDenied,
    ReportExportRecord,
    ReportRecord,
    ReportRepository,
)
from apps.api.app.quality.persistence import TestArtifactRecord as _ArtifactRecord
from apps.api.app.runs.persistence import TestRunRecord as RunRecord
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


NOW = datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc)
ACTOR_ID = UUID("13000000-0000-4000-8000-000000000001")
ORG_ID = UUID("13000000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("13000000-0000-4000-8000-000000000001")
RUN_ID = UUID("13000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("13000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("13000000-0000-4000-8000-000000000002")


@pytest.fixture
def export_repository():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AuthBase.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE outbox_events"))
        connection.execute(text(
            "CREATE TABLE outbox_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_type TEXT NOT NULL, "
            "aggregate_id CHAR(32) NOT NULL, event_type TEXT NOT NULL, payload JSON NOT NULL, "
            "status TEXT NOT NULL, attempts INTEGER NOT NULL, available_at DATETIME NOT NULL, "
            "created_at DATETIME NOT NULL, last_error TEXT, claimed_by TEXT, "
            "claimed_until DATETIME, published_at DATETIME)"
        ))
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(UserRecord(
            id=ACTOR_ID, email="report-exporter@example.invalid", password_hash="hash",
            display_name="Report Exporter", platform_role="user", status="active",
            created_at=NOW,
        ))
        session.add(OrganizationRecord(
            id=ORG_ID, name="Report Export Org", slug="report-export-org", created_at=NOW,
        ))
        session.add(OrganizationMembershipRecord(
            organization_id=ORG_ID, user_id=ACTOR_ID, role="org_admin", created_at=NOW,
        ))
        session.add(ProjectRecord(
            id=PROJECT_ID, organization_id=ORG_ID, name="Report Export Project",
            slug="report-export-project", created_by=ACTOR_ID, created_at=NOW,
        ))
        session.add(RunRecord(
            id=RUN_ID, project_id=PROJECT_ID, plan_id=None, environment_id=UUID(int=20),
            environment_snapshot_id=None, parent_run_id=None, requested_by=ACTOR_ID,
            request_intent=None, branch="main", commit_sha="abc1234", event_replay_floor=0,
            status="PASSED", trigger_source="manual", browser_matrix=["chromium"],
            idempotency_key="export-run", policy={}, lease_owner=None,
            lease_expires_at=None, heartbeat_at=None, claimed_at=None, attempt_token=None,
            started_at=NOW, finished_at=NOW, created_at=NOW,
        ))
        session.add(ReportRecord(
            id=REPORT_ID, run_id=RUN_ID,
            summary={"run": {"id": str(RUN_ID), "status": "PASSED"}, "artifacts": []},
            generated_at=NOW,
        ))
    repository = ReportRepository(sessions)
    yield repository, sessions
    engine.dispose()


def _request(repository, *, key="report-export-key-001", format="pdf"):
    return repository.request_export(
        ACTOR_ID, REPORT_ID, format=format, idempotency_key=key, now=NOW,
    )


def test_same_idempotency_key_returns_same_export_and_one_outbox_event(export_repository):
    repository, sessions = export_repository

    first = _request(repository)
    second = _request(repository)

    assert first["id"] == second["id"]
    assert first["status"] == second["status"] == "QUEUED"
    with sessions() as session:
        assert len(session.scalars(select(ReportExportRecord)).all()) == 1
        events = session.scalars(select(OutboxRecord).where(
            OutboxRecord.event_type == "REPORT_EXPORT_REQUESTED",
        )).all()
        assert len(events) == 1
        assert events[0].aggregate_id == first["id"]


def test_missing_artifact_is_labeled_expired_without_hiding_its_report(export_repository):
    repository, sessions = export_repository
    missing_artifact_id = uuid4()
    with sessions.begin() as session:
        report = session.get(ReportRecord, REPORT_ID)
        report.summary = {
            **report.summary,
            "artifacts": [{
                "id": str(missing_artifact_id), "artifact_type": "trace",
                "status": "AVAILABLE",
            }],
        }

    result = repository.get_report(ACTOR_ID, RUN_ID, limit=30, cursor=None)

    assert result is not None
    assert result["artifact_references"] == [{
        "id": str(missing_artifact_id), "artifact_type": "trace",
        "status": "EXPIRED_ARTIFACT",
    }]


def test_reusing_idempotency_key_with_different_format_conflicts(export_repository):
    repository, _ = export_repository
    _request(repository, key="report-export-key-002", format="pdf")

    with pytest.raises(Exception) as caught:
        _request(repository, key="report-export-key-002", format="html")
    assert caught.value.__class__.__name__ == "ReportIdempotencyConflict"


def test_idempotent_replay_rechecks_current_export_write_access(export_repository):
    repository, sessions = export_repository
    _request(repository, key="report-export-key-003")
    with sessions.begin() as session:
        membership = session.get(OrganizationMembershipRecord, (ORG_ID, ACTOR_ID))
        membership.role = "member"
        session.add(ProjectMembershipRecord(
            project_id=PROJECT_ID, user_id=ACTOR_ID, role="viewer", created_at=NOW,
        ))

    with pytest.raises(ReportPermissionDenied):
        _request(repository, key="report-export-key-003")


def test_expired_export_lease_is_reclaimed(export_repository):
    repository, _ = export_repository
    export = _request(repository)

    first = repository.claim_export(export["id"], worker_id="worker-a", now=NOW)
    reclaimed = repository.claim_export(
        export["id"], worker_id="worker-b", now=NOW + timedelta(seconds=61),
    )

    assert first["lease_owner"] == "worker-a"
    assert reclaimed["lease_owner"] == "worker-b"
    assert reclaimed["attempts"] == 2


def test_expired_export_lease_is_republished_after_original_outbox_delivery(export_repository):
    repository, sessions = export_repository
    export = _request(repository)
    with sessions.begin() as session:
        event = session.scalar(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == export["id"],
            OutboxRecord.event_type == "REPORT_EXPORT_REQUESTED",
        ))
        event.status = "PUBLISHED"
        event.published_at = NOW
    repository.claim_export(export["id"], worker_id="crashed-worker", now=NOW)

    requeued = repository.requeue_due_exports(now=NOW + timedelta(seconds=61))

    assert requeued == 1
    with sessions() as session:
        events = session.scalars(select(OutboxRecord).where(
            OutboxRecord.aggregate_id == export["id"],
            OutboxRecord.event_type == "REPORT_EXPORT_REQUESTED",
        )).all()
        assert [event.status for event in events].count("PENDING") == 1
        assert [event.status for event in events].count("PUBLISHED") == 1


def test_stale_worker_cannot_finish_export_after_reclaim(export_repository):
    repository, _ = export_repository
    export = _request(repository)
    repository.claim_export(export["id"], worker_id="worker-a", now=NOW)
    repository.claim_export(
        export["id"], worker_id="worker-b", now=NOW + timedelta(seconds=61),
    )

    assert not repository.finish_export(
        export["id"], worker_id="worker-a", artifact_id=ARTIFACT_ID,
        now=NOW + timedelta(seconds=62),
    )
    assert repository.finish_export(
        export["id"], worker_id="worker-b", artifact_id=ARTIFACT_ID,
        now=NOW + timedelta(seconds=63),
    )


def test_successful_export_persists_private_artifact_with_the_terminal_update(export_repository):
    from apps.api.app.reports.storage import build_report_object_key

    repository, sessions = export_repository
    export = _request(repository, key="report-export-key-005")
    repository.claim_export(export["id"], worker_id="renderer", now=NOW)
    data = b"%PDF-rendered-report"
    storage_key = build_report_object_key(PROJECT_ID, RUN_ID, export["id"], "pdf")

    assert repository.finish_export(
        export["id"], worker_id="renderer", artifact_id=ARTIFACT_ID,
        now=NOW + timedelta(seconds=1), artifact_metadata={
            "storage_key": storage_key,
            "content_type": "application/pdf",
            "sha256": sha256(data).hexdigest(),
            "size_bytes": len(data),
        },
    )

    with sessions() as session:
        stored_export = session.get(ReportExportRecord, export["id"])
        stored_artifact = session.get(_ArtifactRecord, ARTIFACT_ID)
        assert stored_export.status == "SUCCEEDED"
        assert stored_export.artifact_id == ARTIFACT_ID
        assert stored_artifact.artifact_type == "report"
        assert stored_artifact.storage_key == storage_key
        assert stored_artifact.content_type == "application/pdf"


def test_export_retry_exhaustion_becomes_failed(export_repository):
    repository, sessions = export_repository
    export = _request(repository)
    current = NOW

    for attempt in range(8):
        claim = repository.claim_export(export["id"], worker_id=f"worker-{attempt}", now=current)
        assert claim is not None
        assert repository.fail_export(
            export["id"], worker_id=f"worker-{attempt}",
            error_code="REPORT_STORAGE_UNAVAILABLE", now=current, max_attempts=8,
        )
        current += timedelta(seconds=301)

    with sessions() as session:
        stored = session.get(ReportExportRecord, export["id"])
        assert stored.status == "FAILED"
        assert stored.last_error_code == "REPORT_STORAGE_UNAVAILABLE"


def test_report_export_outbox_is_claimable_and_has_a_beat_dispatcher(
    export_repository, monkeypatch,
):
    monkeypatch.setenv("TESTPILOT_CELERY_BROKER_URL", "memory://")
    from apps.api.app.cases import worker

    repository, sessions = export_repository
    export = _request(repository)
    publisher = worker.GenerationOutboxPublisher(
        sessions, worker_id="report-publisher",
        event_types=("REPORT_EXPORT_REQUESTED",),
    )

    events = publisher.claim(now=NOW)

    assert len(events) == 1
    assert events[0]["event_type"] == "REPORT_EXPORT_REQUESTED"
    assert events[0]["payload"]["export_id"] == str(export["id"])
    assert worker.celery_app.conf.beat_schedule["publish-report-export-outbox"] == {
        "task": "testpilot.dispatch_report_export_outbox", "schedule": 2.0,
    }
    assert worker.celery_app.conf.beat_schedule["recover-expired-report-exports"] == {
        "task": "testpilot.recover_report_exports", "schedule": 15.0,
    }


def test_report_export_dispatch_enqueues_renderer_task(export_repository, monkeypatch):
    monkeypatch.setenv("TESTPILOT_CELERY_BROKER_URL", "memory://")
    from apps.api.app.cases import worker

    repository, sessions = export_repository
    export = repository.request_export(
        ACTOR_ID, REPORT_ID, format="pdf", idempotency_key="report-export-key-004",
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    sent = []
    monkeypatch.setattr(worker, "session_factory", lambda: sessions)
    monkeypatch.setattr(
        worker.celery_app,
        "send_task",
        lambda name, *, kwargs, task_id: sent.append((name, kwargs, task_id)),
    )

    result = worker.dispatch_report_export_outbox()

    assert result == {"claimed": 1, "published": 1}
    assert sent[0][0] == "testpilot.render_report_export"
    assert sent[0][1] == {"export_id": str(export["id"])}


def test_report_export_worker_renders_and_commits_artifact_metadata(monkeypatch):
    monkeypatch.setenv("TESTPILOT_CELERY_BROKER_URL", "memory://")
    from apps.api.app.cases import worker
    from apps.api.app.reports import persistence, storage

    export_id, run_id, project_id = uuid4(), uuid4(), uuid4()
    snapshot = {
        "schema_version": 1,
        "run": {
            "id": str(run_id), "project_id": str(project_id),
            "branch": "main", "commit_sha": "abc1234", "status": "PASSED",
        },
        "summary": {"counts": {"total": 0}},
        "cases": [], "artifacts": [], "usage": [], "snapshot_hash": "a" * 64,
    }
    calls = {}

    class RepositoryStub:
        def __init__(self, sessions):
            assert sessions == "report-sessions"

        def claim_export(self, requested_id, *, worker_id, now):
            assert requested_id == export_id
            return {
                "id": export_id, "run_id": run_id, "project_id": project_id,
                "format": "json", "attempts": 1, "snapshot": snapshot,
            }

        def finish_export(self, requested_id, **kwargs):
            calls["finish"] = (requested_id, kwargs)
            return True

        def fail_export(self, *args, **kwargs):
            pytest.fail("a successful renderer must not fail the export")

    class StorageStub:
        def put(self, key, data, *, content_type):
            calls["put"] = (key, data, content_type)
            return sha256(data).hexdigest()

    monkeypatch.setattr(worker, "session_factory", lambda: "report-sessions")
    monkeypatch.setattr(persistence, "ReportRepository", RepositoryStub)
    monkeypatch.setattr(storage.S3ReportStorage, "from_environment", lambda: StorageStub())

    result = worker.render_report_export(str(export_id))

    assert result["status"] == "SUCCEEDED"
    key, data, content_type = calls["put"]
    assert key == f"projects/{project_id}/runs/{run_id}/reports/{export_id}.json"
    assert content_type == "application/json"
    assert json.loads(data) == snapshot
    finish_kwargs = calls["finish"][1]
    assert finish_kwargs["artifact_metadata"]["storage_key"] == key
    assert finish_kwargs["artifact_metadata"]["sha256"] == sha256(data).hexdigest()


def test_report_export_worker_fails_closed_when_storage_is_missing(monkeypatch):
    monkeypatch.setenv("TESTPILOT_CELERY_BROKER_URL", "memory://")
    from apps.api.app.cases import worker
    from apps.api.app.reports import persistence, storage

    export_id, run_id = uuid4(), uuid4()
    calls = []

    class RepositoryStub:
        def __init__(self, _sessions):
            pass

        def claim_export(self, *_args, **_kwargs):
            return {
                "id": export_id, "run_id": run_id, "project_id": uuid4(),
                "format": "json", "attempts": 1,
                "snapshot": {"run": {}, "summary": {}, "cases": [], "artifacts": [], "usage": []},
            }

        def fail_export(self, requested_id, **kwargs):
            calls.append((requested_id, kwargs))
            return True

    monkeypatch.setattr(worker, "session_factory", lambda: "report-sessions")
    monkeypatch.setattr(persistence, "ReportRepository", RepositoryStub)
    monkeypatch.setattr(storage.S3ReportStorage, "from_environment", lambda: None)

    result = worker.render_report_export(str(export_id))

    assert result == {
        "claimed": True, "status": "QUEUED", "error_code": "REPORT_STORAGE_UNAVAILABLE",
    }
    assert calls[0][0] == export_id
    assert calls[0][1]["error_code"] == "REPORT_STORAGE_UNAVAILABLE"
