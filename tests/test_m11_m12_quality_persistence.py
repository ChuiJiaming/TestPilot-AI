from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import count
from uuid import uuid4

from PIL import Image
import pytest
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.persistence import CaseScopeError
from apps.api.app.cases.persistence import TestCaseRecord as CaseRecord
from apps.api.app.cases.persistence import TestCaseVersionRecord as CaseVersionRecord
from apps.api.app.core.audit import AuditLogRecord
from apps.api.app.environments.persistence import EnvironmentRecord
from apps.api.app.quality.analyzer import insufficient_analysis
from apps.api.app.quality.artifacts import build_artifact_object_key
from apps.api.app.quality.persistence import (
    DefectOccurrenceRecord,
    QualityConflict,
    QualityRepository,
    TestArtifactRecord as ArtifactRecord,
    VisualBaselineRecord,
    VisualDiffRecord,
)
from apps.api.app.runs.persistence import HealingRecord, RunCaseRecord
from apps.api.app.runs.persistence import TestRunRecord as RunRecord
from apps.api.app.runs.persistence import TestStepRecord as StepRecord
from apps.api.app.runs.repository import StaleRunAttempt
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord, OrganizationRecord,
    ProjectMembershipRecord, ProjectRecord,
)


class MemoryStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key, data, *, content_type):
        assert content_type == "image/png"
        self.objects[key] = data
        from hashlib import sha256
        return sha256(data).hexdigest()

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


def make_png(color: tuple[int, int, int]) -> bytes:
    from io import BytesIO
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


@pytest.fixture
def quality_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repo = QualityRepository(sessions, storage=MemoryStorage())
    now = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
    creator_id, reviewer_id, outsider_id = uuid4(), uuid4(), uuid4()
    organization_id, project_id = uuid4(), uuid4()
    environment_id, case_id, version_id = uuid4(), uuid4(), uuid4()
    run_id, run_case_id, step_id = uuid4(), uuid4(), uuid4()
    action_id, attempt_token = uuid4(), uuid4()
    worker_id = "worker-quality-1"

    with sessions.begin() as session:
        for user_id, label in (
            (creator_id, "Creator"), (reviewer_id, "Reviewer"), (outsider_id, "Outsider"),
        ):
            session.add(UserRecord(
                id=user_id, email=f"{user_id.hex}@example.invalid", password_hash="hash",
                display_name=label, platform_role="user", status="active", created_at=now,
            ))
        session.add(OrganizationRecord(
            id=organization_id, name="Quality", slug=f"quality-{project_id.hex[:10]}",
            created_at=now,
        ))
        session.add(ProjectRecord(
            id=project_id, organization_id=organization_id, name="Quality",
            slug=f"quality-{project_id.hex[:10]}", created_by=creator_id, created_at=now,
        ))
        for user_id in (creator_id, reviewer_id):
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
            id=case_id, project_id=project_id, title="Visual case", priority="P1",
            status="active", published_version_id=version_id, created_at=now,
        ))
        session.add(CaseVersionRecord(
            id=version_id, case_id=case_id, project_id=project_id, version=1,
            dsl_schema_version=2, dsl_title="Visual case", version_name="v1",
            steps=[{"action": "click"}], assertions=[], changelog="initial",
            review_status="APPROVED", created_by=creator_id,
            created_at=now - timedelta(days=1), approved_by=reviewer_id,
            approved_at=now - timedelta(days=1),
        ))
        session.add(RunRecord(
            id=run_id, project_id=project_id, plan_id=None,
            environment_id=environment_id, environment_snapshot_id=None,
            parent_run_id=None, requested_by=creator_id, request_intent=None,
            branch=None, commit_sha=None, event_replay_floor=0, status="RUNNING",
            trigger_source="manual", browser_matrix=["chromium"], policy={},
            idempotency_key=f"quality-{run_id}", lease_owner=worker_id,
            lease_expires_at=now + timedelta(minutes=1), heartbeat_at=now,
            claimed_at=now, attempt_token=attempt_token, started_at=now,
            finished_at=None, created_at=now,
        ))
        session.add(RunCaseRecord(
            id=run_case_id, run_id=run_id, case_version_id=version_id,
            browser_name="chromium", status="RUNNING", lease_owner=worker_id,
            lease_expires_at=now + timedelta(minutes=1), heartbeat_at=now,
            attempt_token=attempt_token, started_at=now, finished_at=None,
        ))
        session.add(StepRecord(
            id=step_id, run_case_id=run_case_id, action_id=action_id, sequence=1,
            action_type="click", locator={"by": "role", "value": "button"},
            expected=None, result_json={
                "failure_phase": "locator_resolution", "error_code": "LOCATOR_NOT_UNIQUE",
            }, status="FAILED", replay_policy="NEVER",
            idempotency_key=f"quality:{step_id}", attempt_token=attempt_token,
            started_at=now, finished_at=now,
        ))

    sequence = count(1)
    def assign_audit_id(_mapper, _connection, target):
        if target.id is None:
            target.id = next(sequence)
    event.listen(AuditLogRecord, "before_insert", assign_audit_id)
    yield {
        "engine": engine, "sessions": sessions, "repo": repo, "now": now,
        "creator_id": creator_id, "reviewer_id": reviewer_id,
        "outsider_id": outsider_id, "organization_id": organization_id,
        "project_id": project_id, "environment_id": environment_id,
        "case_id": case_id, "version_id": version_id, "run_id": run_id,
        "run_case_id": run_case_id, "step_id": step_id, "action_id": action_id,
        "attempt_token": attempt_token, "worker_id": worker_id,
        "storage": repo.storage,
    }
    event.remove(AuditLogRecord, "before_insert", assign_audit_id)
    engine.dispose()


def test_m11_fingerprint_groups_occurrences_and_audits_human_triage(quality_db):
    repo = quality_db["repo"]
    context = repo.failure_context(quality_db["run_case_id"])
    analysis = insufficient_analysis(context["failure"], context["evidence_ids"])
    first = repo.record_failure(
        run_case_id=quality_db["run_case_id"], context=context, analysis=analysis,
        attempt_token=quality_db["attempt_token"], worker_id=quality_db["worker_id"],
        now=quality_db["now"],
    )
    duplicate = repo.record_failure(
        run_case_id=quality_db["run_case_id"], context=context, analysis=analysis,
        attempt_token=quality_db["attempt_token"], worker_id=quality_db["worker_id"],
        now=quality_db["now"] + timedelta(seconds=1),
    )

    assert duplicate["id"] == first["id"]
    assert repo.get_defect(quality_db["creator_id"], first["id"])["occurrence_count"] == 1
    updated = repo.patch_defect(
        quality_db["creator_id"], first["id"],
        changes={"status": "OPEN", "root_cause_status": "CONFIRMED"},
        reason="人工核对了失败步骤和证据截图", now=quality_db["now"] + timedelta(minutes=1),
    )
    assert updated["status"] == "OPEN"
    assert updated["root_cause_status"] == "CONFIRMED"
    assert updated["root_cause_confirmed_by"] == quality_db["creator_id"]

    with quality_db["sessions"]() as session:
        audit = session.scalar(select(AuditLogRecord).where(
            AuditLogRecord.resource_id == first["id"]))
        assert audit.action == "defect.triaged"
        assert audit.previous_hash is None
        assert audit.entry_hash
        assert session.scalar(select(func.count()).select_from(
            DefectOccurrenceRecord).where(DefectOccurrenceRecord.defect_id == first["id"])) == 1


def test_m11_stale_worker_cannot_persist_an_occurrence(quality_db):
    repo = quality_db["repo"]
    context = repo.failure_context(quality_db["run_case_id"])
    analysis = insufficient_analysis(context["failure"], context["evidence_ids"])
    with pytest.raises(StaleRunAttempt):
        repo.record_failure(
            run_case_id=quality_db["run_case_id"], context=context, analysis=analysis,
            attempt_token=uuid4(), worker_id=quality_db["worker_id"], now=quality_db["now"],
        )
    with quality_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(DefectOccurrenceRecord)) == 0


def test_m10_replay_screenshot_keeps_the_published_source_action_link(quality_db):
    replay_step_id, healing_id, replay_action_id = uuid4(), uuid4(), uuid4()
    with quality_db["sessions"].begin() as session:
        session.add(HealingRecord(
            id=healing_id, step_id=quality_db["step_id"],
            old_locator={"by": "role", "value": "button"},
            new_locator={"by": "role", "value": "button", "name": "Continue"},
            evidence={}, confidence=0.8, status="APPROVED", created_at=quality_db["now"],
        ))
        session.add(StepRecord(
            id=replay_step_id, run_case_id=quality_db["run_case_id"],
            action_id=replay_action_id, sequence=2, action_type="click",
            locator={"by": "role", "value": "button", "name": "Continue"},
            expected=None, result_json={}, status="RUNNING", replay_policy="SAFE",
            recovery_healing_id=healing_id, idempotency_key="replay-action",
            attempt_token=quality_db["attempt_token"], started_at=quality_db["now"],
        ))
    artifact = quality_db["repo"].save_screenshot(
        run_id=quality_db["run_id"], run_case_id=quality_db["run_case_id"],
        step_id=replay_step_id, attempt_token=quality_db["attempt_token"],
        worker_id=quality_db["worker_id"], image=make_png((255, 255, 255)),
        viewport={"width": 2, "height": 2}, device_scale_factor=1,
        color_scheme="light", now=quality_db["now"] + timedelta(seconds=1),
    )

    assert artifact["action_id"] == quality_db["action_id"]
    assert artifact["step_id"] == replay_step_id
    assert artifact["redacted"] is True
    with quality_db["sessions"]() as session:
        stored = session.get(ArtifactRecord, artifact["id"])
        assert stored.metadata_json["capture_profile"] == "playwright-masked-v1"


def test_m11_insufficient_evidence_cannot_be_human_confirmed(quality_db):
    repo = quality_db["repo"]
    run_case_id = uuid4()
    with quality_db["sessions"].begin() as session:
            session.add(RunCaseRecord(
            id=run_case_id, run_id=quality_db["run_id"],
            case_version_id=quality_db["version_id"], browser_name="webkit",
            status="RUNNING", lease_owner=quality_db["worker_id"],
            attempt_token=quality_db["attempt_token"], started_at=quality_db["now"],
            finished_at=quality_db["now"],
        ))
    context = repo.failure_context(run_case_id)
    analysis = insufficient_analysis(context["failure"], context["evidence_ids"])
    assert analysis.evidence_status == "INSUFFICIENT_EVIDENCE"
    defect = repo.record_failure(
        run_case_id=run_case_id, context=context, analysis=analysis,
        attempt_token=quality_db["attempt_token"], worker_id=quality_db["worker_id"],
        now=quality_db["now"],
    )
    with pytest.raises(QualityConflict, match="INSUFFICIENT_EVIDENCE"):
        repo.patch_defect(
            quality_db["creator_id"], defect["id"],
            changes={"root_cause_status": "CONFIRMED"}, reason="人工复核",
            now=quality_db["now"] + timedelta(minutes=1),
        )


def _seed_screenshot(db, *, artifact_id, created_at, image, step_id=None,
                     action_id=None):
    storage_key = build_artifact_object_key(
        db["project_id"], db["run_id"], artifact_id)
    digest = db["storage"].put(storage_key, image, content_type="image/png")
    with db["sessions"].begin() as session:
        session.add(ArtifactRecord(
            id=artifact_id, project_id=db["project_id"], run_id=db["run_id"],
            run_case_id=db["run_case_id"], step_id=step_id,
            action_id=action_id or db["action_id"],
            environment_id=db["environment_id"],
            case_version_id=db["version_id"], browser="chromium",
            artifact_type="screenshot", content_type="image/png",
            storage_key=storage_key, sha256=digest, size_bytes=len(image),
            redacted=True, viewport={"width": 2, "height": 2},
            device_scale_factor=1, color_scheme="light", metadata_json={},
            created_at=created_at,
        ))
    return artifact_id


def test_m12_baseline_requires_a_different_reviewer_and_visual_diff_is_deterministic(quality_db):
    repo = quality_db["repo"]
    reference = make_png((255, 255, 255))
    from io import BytesIO
    actual_image = Image.new("RGB", (2, 2), (255, 255, 255))
    actual_image.putpixel((0, 0), (250, 250, 250))
    actual_buffer = BytesIO()
    actual_image.save(actual_buffer, format="PNG", optimize=False)
    actual = actual_buffer.getvalue()
    baseline_artifact_id = _seed_screenshot(
        quality_db, artifact_id=uuid4(), created_at=quality_db["now"],
        image=reference, step_id=quality_db["step_id"],
    )
    actual_artifact_id = _seed_screenshot(
        quality_db, artifact_id=uuid4(), created_at=quality_db["now"] + timedelta(seconds=1),
        image=actual, step_id=quality_db["step_id"],
    )
    proposal = repo.propose_visual_baseline(
        quality_db["creator_id"], payload={
            "project_id": str(quality_db["project_id"]),
            "case_version_id": str(quality_db["version_id"]),
            "action_id": str(quality_db["action_id"]),
            "environment_id": str(quality_db["environment_id"]),
            "browser": "chromium", "artifact_id": str(baseline_artifact_id),
            "viewport": {"width": 2, "height": 2},
            "mask_regions": [], "max_pixel_ratio": 0.25,
            "pixel_channel_tolerance": 0,
        }, now=quality_db["now"],
    )
    with pytest.raises(QualityConflict, match="SEPARATION_OF_DUTIES"):
        repo.decide_visual_baseline(
            quality_db["creator_id"], proposal["id"], decision="APPROVE",
            reason="同一人不得批准", now=quality_db["now"] + timedelta(seconds=1),
        )
    approved = repo.decide_visual_baseline(
        quality_db["reviewer_id"], proposal["id"], decision="APPROVE",
        reason="截图和上下文已核对", now=quality_db["now"] + timedelta(seconds=2),
    )
    assertion_step_id = uuid4()
    with quality_db["sessions"].begin() as session:
        session.add(StepRecord(
            id=assertion_step_id, run_case_id=quality_db["run_case_id"],
            action_id=uuid4(), sequence=2, action_type="assert:visual",
            locator=None, expected={"severity": "BLOCKER"}, result_json={},
            status="RUNNING", replay_policy="SAFE", idempotency_key="visual-assertion",
            attempt_token=quality_db["attempt_token"], started_at=quality_db["now"],
        ))

    result = repo.evaluate_visual_assertion(
        run_id=quality_db["run_id"], run_case_id=quality_db["run_case_id"],
        attempt_token=quality_db["attempt_token"], worker_id=quality_db["worker_id"],
        case_version_id=quality_db["version_id"], environment_id=quality_db["environment_id"],
        browser="chromium", action_id=quality_db["action_id"],
        baseline_id=approved["id"], assertion_step_id=assertion_step_id,
        now=quality_db["now"] + timedelta(seconds=3),
    )
    assert result["status"] == "PASS"
    assert result["pixel_ratio"] == 0.25
    assert result["actual_artifact_id"] == actual_artifact_id
    with quality_db["sessions"]() as session:
        diff = session.get(VisualDiffRecord, result["diff_id"])
        diff_artifact = session.get(ArtifactRecord, diff.diff_artifact_id)
        diff_storage_key = diff_artifact.storage_key
    diff_bytes = quality_db["storage"].get(diff_storage_key)
    assert diff_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    with quality_db["sessions"]() as session:
        diff = session.get(VisualDiffRecord, result["diff_id"])
        assert diff.status == "PASS"
        assert diff.baseline_id == approved["id"]


def test_m12_missing_approved_baseline_never_passes(quality_db):
    repo = quality_db["repo"]
    _seed_screenshot(
        quality_db, artifact_id=uuid4(), created_at=quality_db["now"],
        image=make_png((255, 255, 255)), step_id=quality_db["step_id"],
    )
    assertion_step_id = uuid4()
    with quality_db["sessions"].begin() as session:
        session.add(StepRecord(
            id=assertion_step_id, run_case_id=quality_db["run_case_id"],
            action_id=uuid4(), sequence=2, action_type="assert:visual",
            locator=None, expected={"severity": "BLOCKER"}, result_json={},
            status="RUNNING", replay_policy="SAFE", idempotency_key="visual-assertion",
            attempt_token=quality_db["attempt_token"], started_at=quality_db["now"],
        ))
    result = repo.evaluate_visual_assertion(
        run_id=quality_db["run_id"], run_case_id=quality_db["run_case_id"],
        attempt_token=quality_db["attempt_token"], worker_id=quality_db["worker_id"],
        case_version_id=quality_db["version_id"], environment_id=quality_db["environment_id"],
        browser="chromium", action_id=quality_db["action_id"], baseline_id=None,
        assertion_step_id=assertion_step_id, now=quality_db["now"] + timedelta(seconds=1),
    )
    assert result["status"] == "NEED_BASELINE"
    assert result["error_code"] == "BASELINE_NOT_FOUND"


def test_m12_explicit_baseline_for_another_action_never_passes(quality_db):
    repo = quality_db["repo"]
    other_action_id = uuid4()
    reference_id = _seed_screenshot(
        quality_db, artifact_id=uuid4(), created_at=quality_db["now"],
        image=make_png((255, 255, 255)), step_id=quality_db["step_id"],
        action_id=other_action_id,
    )
    _seed_screenshot(
        quality_db, artifact_id=uuid4(), created_at=quality_db["now"] + timedelta(seconds=1),
        image=make_png((255, 255, 255)), step_id=quality_db["step_id"],
    )
    proposal = repo.propose_visual_baseline(
        quality_db["creator_id"], payload={
            "project_id": str(quality_db["project_id"]),
            "case_version_id": str(quality_db["version_id"]),
            "action_id": str(other_action_id),
            "environment_id": str(quality_db["environment_id"]),
            "browser": "chromium", "artifact_id": str(reference_id),
            "viewport": {"width": 2, "height": 2},
            "mask_regions": [], "max_pixel_ratio": 0.01,
            "pixel_channel_tolerance": 0,
        }, now=quality_db["now"],
    )
    approved = repo.decide_visual_baseline(
        quality_db["reviewer_id"], proposal["id"], decision="APPROVE",
        reason="审批另一动作的候选", now=quality_db["now"] + timedelta(seconds=1),
    )
    assertion_step_id = uuid4()
    with quality_db["sessions"].begin() as session:
        session.add(StepRecord(
            id=assertion_step_id, run_case_id=quality_db["run_case_id"],
            action_id=uuid4(), sequence=2, action_type="assert:visual",
            locator=None, expected={"severity": "BLOCKER"}, result_json={},
            status="RUNNING", replay_policy="SAFE", idempotency_key="visual-context-assertion",
            attempt_token=quality_db["attempt_token"], started_at=quality_db["now"],
        ))

    result = repo.evaluate_visual_assertion(
        run_id=quality_db["run_id"], run_case_id=quality_db["run_case_id"],
        attempt_token=quality_db["attempt_token"], worker_id=quality_db["worker_id"],
        case_version_id=quality_db["version_id"], environment_id=quality_db["environment_id"],
        browser="chromium", action_id=quality_db["action_id"],
        baseline_id=approved["id"], assertion_step_id=assertion_step_id,
        now=quality_db["now"] + timedelta(seconds=2),
    )

    assert result["status"] == "NEED_BASELINE"
    assert result["error_code"] == "BASELINE_CONTEXT_MISMATCH"
    with quality_db["sessions"]() as session:
        assert session.get(VisualBaselineRecord, approved["id"]).action_id == other_action_id


def test_report_artifact_download_rechecks_project_membership_before_storage(quality_db):
    data = b"%PDF-private-report"
    artifact_id = uuid4()
    storage_key = (
        f"projects/{quality_db['project_id']}/runs/{quality_db['run_id']}"
        f"/reports/{uuid4()}.pdf"
    )

    class ReportStorage:
        def __init__(self):
            self.get_calls = []

        def get(self, key):
            self.get_calls.append(key)
            return data

    report_storage = ReportStorage()
    quality_db["repo"].report_storage = report_storage
    with quality_db["sessions"].begin() as session:
        session.add(ArtifactRecord(
            id=artifact_id, project_id=quality_db["project_id"],
            run_id=quality_db["run_id"], run_case_id=None, step_id=None,
            action_id=None, environment_id=quality_db["environment_id"],
            case_version_id=None, browser=None, artifact_type="report",
            content_type="application/pdf", storage_key=storage_key,
            sha256=sha256(data).hexdigest(), size_bytes=len(data), redacted=True,
            viewport=None, device_scale_factor=None, color_scheme=None,
            metadata_json={"report_export_id": str(uuid4())}, created_at=quality_db["now"],
        ))

    artifact, retrieved = quality_db["repo"].download_artifact(
        quality_db["creator_id"], artifact_id,
    )

    assert retrieved == data
    assert artifact["artifact_type"] == "report"
    assert "storage_key" not in artifact
    assert report_storage.get_calls == [storage_key]
    with pytest.raises(CaseScopeError):
        quality_db["repo"].download_artifact(quality_db["outsider_id"], artifact_id)
    assert report_storage.get_calls == [storage_key]
