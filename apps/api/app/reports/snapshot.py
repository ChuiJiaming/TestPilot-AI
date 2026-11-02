"""Build and persist deterministic, privacy-bounded Run report snapshots."""

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.cases.persistence import TestCaseRecord, TestCaseVersionRecord
from apps.api.app.cases.planning import UsageMetricRecord
from apps.api.app.quality.persistence import TestArtifactRecord
from apps.api.app.reports.persistence import ReportRecord
from apps.api.app.runs.persistence import RunCaseRecord, TestRunRecord, TestStepRecord


_CASE_STATUSES = (
    "PASSED", "FAILED", "ERROR", "SKIPPED", "CANCELLED", "QUEUED", "RUNNING",
)
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_.:-]{0,79}$")


def ensure_report_snapshot(session: Session, run_id: UUID, *, now: datetime) -> ReportRecord:
    """Create one terminal Run snapshot in the caller's transaction."""
    existing = session.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))
    if existing is not None:
        return existing

    run = session.get(TestRunRecord, run_id)
    if run is None:
        raise LookupError("Run not found while creating report snapshot")
    if run.status not in {"PASSED", "FAILED", "ERROR", "CANCELLED"}:
        raise ValueError("report snapshots require a terminal Run")

    summary = build_report_snapshot(session, run, now=now)
    report = ReportRecord(
        run_id=run_id,
        summary=summary,
        snapshot_hash=summary["snapshot_hash"],
        generated_at=now,
    )
    session.add(report)
    # Flush in the same transaction so a caller can observe the snapshot before
    # commit and any uniqueness failure rolls back with the terminal event.
    session.flush()
    return report


def build_report_snapshot(session: Session, run: TestRunRecord, *, now: datetime) -> dict:
    """Return a canonical DTO made only from durable, allowlisted Run facts."""
    case_rows = session.execute(
        select(RunCaseRecord, TestCaseVersionRecord, TestCaseRecord)
        .join(TestCaseVersionRecord, TestCaseVersionRecord.id == RunCaseRecord.case_version_id)
        .join(TestCaseRecord, TestCaseRecord.id == TestCaseVersionRecord.case_id)
        .where(RunCaseRecord.run_id == run.id)
        .order_by(
            RunCaseRecord.case_version_id,
            RunCaseRecord.browser_name,
            RunCaseRecord.id,
        )
    ).all()
    run_cases = [row[0] for row in case_rows]
    run_case_ids = [row.id for row in run_cases]
    steps_by_run_case: dict[UUID, list[TestStepRecord]] = {row.id: [] for row in run_cases}
    if run_case_ids:
        steps = session.scalars(
            select(TestStepRecord)
            .where(TestStepRecord.run_case_id.in_(run_case_ids))
            .order_by(TestStepRecord.run_case_id, TestStepRecord.sequence, TestStepRecord.id)
        ).all()
        for step in steps:
            steps_by_run_case[step.run_case_id].append(step)

    cases = []
    for run_case, version, case in case_rows:
        title = version.dsl_title or version.version_name or case.title
        steps = [
            _step_summary(step)
            for step in steps_by_run_case[run_case.id]
        ]
        cases.append({
            "id": str(run_case.id),
            "case_version_id": str(run_case.case_version_id),
            "title": title,
            "version": version.version,
            "priority": case.priority,
            "browser": run_case.browser_name,
            "status": run_case.status,
            "started_at": _iso(run_case.started_at),
            "finished_at": _iso(run_case.finished_at),
            "duration_seconds": _duration_seconds(run_case.started_at, run_case.finished_at),
            "steps": steps,
        })

    artifacts = session.scalars(
        select(TestArtifactRecord)
        .where(TestArtifactRecord.run_id == run.id)
        .order_by(TestArtifactRecord.created_at, TestArtifactRecord.id)
    ).all()
    artifact_summaries = [
        {
            "id": str(artifact.id),
            "run_case_id": str(artifact.run_case_id) if artifact.run_case_id else None,
            "step_id": str(artifact.step_id) if artifact.step_id else None,
            "artifact_type": artifact.artifact_type,
            "content_type": artifact.content_type,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "status": "AVAILABLE",
            "redacted": artifact.redacted,
            "created_at": _iso(artifact.created_at),
        }
        for artifact in artifacts
    ]

    usage_metrics = session.scalars(
        select(UsageMetricRecord)
        .where(UsageMetricRecord.run_id == run.id)
        .order_by(
            UsageMetricRecord.metric_name,
            UsageMetricRecord.created_at,
            UsageMetricRecord.id,
        )
    ).all()
    usage = [
        {
            "metric_name": metric.metric_name,
            "value": _decimal_text(metric.value),
            "unit": metric.unit,
            "created_at": _iso(metric.created_at),
        }
        for metric in usage_metrics
    ]

    counts = Counter(row.status for row in run_cases)
    count_summary = {"total": len(run_cases)}
    count_summary.update({status.casefold(): counts.get(status, 0) for status in _CASE_STATUSES})
    count_summary["other"] = sum(
        count for status, count in counts.items() if status not in _CASE_STATUSES
    )
    finished_at = run.finished_at or now
    snapshot = {
        "schema_version": 1,
        "run": {
            "id": str(run.id),
            "project_id": str(run.project_id),
            "environment_id": str(run.environment_id),
            "environment_snapshot_id": (
                str(run.environment_snapshot_id) if run.environment_snapshot_id else None
            ),
            "branch": run.branch,
            "commit_sha": run.commit_sha,
            "browser_matrix": list(run.browser_matrix or []),
            "trigger_source": run.trigger_source,
            "status": run.status,
            "started_at": _iso(run.started_at),
            "finished_at": _iso(finished_at),
            "duration_seconds": _duration_seconds(run.started_at, finished_at),
        },
        "summary": {
            "counts": count_summary,
            "duration_seconds": _duration_seconds(run.started_at, finished_at),
        },
        "cases": cases,
        "artifacts": artifact_summaries,
        "usage": usage,
    }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    snapshot["snapshot_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return snapshot


def _step_summary(step: TestStepRecord) -> dict:
    result = step.result_json if isinstance(step.result_json, dict) else {}
    error_code = result.get("error_code")
    if not isinstance(error_code, str) or _SAFE_ERROR_CODE.fullmatch(error_code) is None:
        error_code = None
    return {
        "id": str(step.id),
        "sequence": step.sequence,
        "action_type": step.action_type,
        "status": step.status,
        "error_code": error_code,
        "started_at": _iso(step.started_at),
        "finished_at": _iso(step.finished_at),
        "duration_seconds": _duration_seconds(step.started_at, step.finished_at),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _duration_seconds(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    if started_at is None or finished_at is None:
        return None
    try:
        seconds = (finished_at - started_at).total_seconds()
    except TypeError:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        seconds = (finished_at - started_at).total_seconds()
    return round(max(0.0, seconds), 3)


def _decimal_text(value: Decimal | int | float) -> str:
    return format(Decimal(value), "f")
