"""Idempotent scheduled Run creation from an M06 trigger intent."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, Uuid, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.app.auth.persistence import AuthBase
from apps.api.app.cases.persistence import TestCaseRecord
from apps.api.app.cases.planning import (
    OutboxRecord,
    ScheduleRecord,
    ScheduleTriggerRecord,
    SuiteCaseRecord,
    TestPlanRecord,
    TestSuiteRecord,
)
from apps.api.app.environments.persistence import (
    EnvironmentScopeError,
    EnvironmentRecord,
    EnvironmentSnapshotRepository,
)


class TestRunRecord(AuthBase):
    __tablename__ = "test_runs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    plan_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_plans.id"))
    environment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("environments.id"), nullable=False)
    environment_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("environment_snapshots.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String, nullable=False)
    trigger_source: Mapped[str] = mapped_column(String, nullable=False)
    browser_matrix: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String)
    policy: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunCaseRecord(AuthBase):
    __tablename__ = "run_cases"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False)
    case_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_case_versions.id"), nullable=False)
    browser_name: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunCreationBlocked(Exception):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class RunAdmissionRepository:
    """Atomically admit queued Runs under each plan's persisted concurrency policy."""

    ACTIVE_STATUSES = (
        "PREPARING", "RUNNING", "WAITING_APPROVAL", "PAUSING", "PAUSED",
        "FINALIZING", "CANCELLING",
    )

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def claim(self, run_id: UUID, *, worker_id: str, now: datetime, lease_seconds: int = 60):
        """Claim a queued Run only when its plan has an available execution slot.

        Locking the plan row serializes admission attempts from independent workers.
        Runs blocked by the cap remain QUEUED so a later worker poll can claim them.
        """
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker_id and a positive lease_seconds are required")
        with self.session_factory.begin() as session:
            plan_id = session.scalar(select(TestRunRecord.plan_id).where(TestRunRecord.id == run_id))
            if plan_id is None:
                run = session.get(TestRunRecord, run_id, with_for_update=True)
                if run is None or run.status != "QUEUED":
                    return {"claimed": False, "reason": "RUN_NOT_QUEUED"}
                policy = dict(run.policy or {})
            else:
                plan = session.scalar(select(TestPlanRecord).where(TestPlanRecord.id == plan_id)
                                      .with_for_update())
                run = session.get(TestRunRecord, run_id, with_for_update=True)
                if plan is None or run is None or run.status != "QUEUED":
                    return {"claimed": False, "reason": "RUN_NOT_QUEUED"}
                policy = {**dict(plan.policy or {}), **dict(run.policy or {})}

            mode = policy.get("concurrency_mode", "serial")
            maximum = policy.get("max_concurrent_runs", 1)
            if (mode not in {"serial", "bounded"} or type(maximum) is not int or maximum < 1
                    or (mode == "serial" and maximum != 1)):
                return {"claimed": False, "reason": "INVALID_PLAN_CONCURRENCY_POLICY"}

            if run.plan_id is not None:
                active_count = session.scalar(
                    select(func.count()).select_from(TestRunRecord).where(
                        TestRunRecord.plan_id == run.plan_id,
                        TestRunRecord.id != run.id,
                        TestRunRecord.status.in_(self.ACTIVE_STATUSES),
                    )
                ) or 0
                if active_count >= maximum:
                    return {"claimed": False, "reason": "PLAN_CONCURRENCY_LIMIT", "active_runs": active_count}

            attempt_token = uuid4()
            run.status = "PREPARING"
            run.lease_owner = worker_id
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            run.heartbeat_at = now
            run.claimed_at = now
            run.attempt_token = attempt_token
            run.started_at = run.started_at or now
            session.flush()
            return {
                "claimed": True,
                "run_id": run.id,
                "project_id": run.project_id,
                "plan_id": run.plan_id,
                "environment_id": run.environment_id,
                "environment_snapshot_id": run.environment_snapshot_id,
                "browser_matrix": run.browser_matrix,
                "policy": policy,
                "attempt_token": attempt_token,
                "lease_expires_at": run.lease_expires_at,
            }


class ScheduledRunRepository:
    def __init__(self, session_factory, snapshots: EnvironmentSnapshotRepository):
        self.session_factory = session_factory
        self.snapshots = snapshots

    def create_from_trigger(self, trigger_id: UUID, *, now: datetime):
        try:
            with self.session_factory.begin() as session:
                trigger = session.get(ScheduleTriggerRecord, trigger_id, with_for_update=True)
                if trigger is None:
                    raise RunCreationBlocked("SCHEDULE_TRIGGER_NOT_FOUND")
                schedule = session.get(ScheduleRecord, trigger.schedule_id, with_for_update=True)
                if schedule is None:
                    raise RunCreationBlocked("SCHEDULE_NOT_FOUND")
                plan = session.get(TestPlanRecord, schedule.plan_id)
                if plan is None or plan.suite_id is None:
                    raise RunCreationBlocked("PLAN_HAS_NO_SUITE")
                suite = session.get(TestSuiteRecord, plan.suite_id)
                if suite is None or suite.project_id != plan.project_id:
                    raise RunCreationBlocked("SUITE_NOT_FOUND")
                environment = session.get(EnvironmentRecord, schedule.environment_id)
                if environment is None or environment.project_id != plan.project_id:
                    raise RunCreationBlocked("ENVIRONMENT_SCOPE_MISMATCH")

                browsers = plan.policy.get("browser_matrix", ["chromium"])
                run_policy = dict(plan.policy or {})
                run_policy.setdefault("concurrency_mode", "serial")
                run_policy.setdefault("max_concurrent_runs", 1)
                mode = run_policy["concurrency_mode"]
                maximum = run_policy["max_concurrent_runs"]
                if (mode not in {"serial", "bounded"} or type(maximum) is not int or maximum < 1
                        or (mode == "serial" and maximum != 1)):
                    raise RunCreationBlocked("INVALID_PLAN_CONCURRENCY_POLICY")
                allowed_browsers = {"chromium", "firefox", "webkit"}
                if (not isinstance(browsers, list) or not browsers
                    or any(not isinstance(browser, str) for browser in browsers)
                    or len(set(browsers)) != len(browsers)
                    or any(browser not in allowed_browsers for browser in browsers)):
                    raise RunCreationBlocked("INVALID_BROWSER_MATRIX")

                associations = session.execute(select(SuiteCaseRecord, TestCaseRecord)
                    .join(TestCaseRecord, TestCaseRecord.id == SuiteCaseRecord.case_id)
                    .where(SuiteCaseRecord.suite_id == suite.id)
                    .order_by(SuiteCaseRecord.sequence, SuiteCaseRecord.case_id)).all()
                if not associations:
                    raise RunCreationBlocked("SUITE_HAS_NO_CASES")
                pinned_versions = []
                for _, case in associations:
                    if case.status != "active" or case.published_version_id is None:
                        raise RunCreationBlocked("SUITE_CASE_NOT_PUBLISHED")
                    pinned_versions.append(case.published_version_id)

                idempotency_key = f"schedule-trigger:{trigger.id}"
                existing = session.scalar(select(TestRunRecord).where(
                    TestRunRecord.project_id == plan.project_id,
                    TestRunRecord.idempotency_key == idempotency_key))
                if existing is not None:
                    trigger.status = "COMPLETED"
                    trigger.error_code = None
                    return self._run_dict(existing)

                snapshot = self.snapshots.capture(
                    environment_id=environment.id, project_id=plan.project_id, now=now,
                    session=session)
                run = TestRunRecord(
                    id=uuid4(), project_id=plan.project_id, plan_id=plan.id,
                    environment_id=environment.id, environment_snapshot_id=snapshot["id"],
                    status="QUEUED", trigger_source=trigger.trigger_source, browser_matrix=browsers,
                    idempotency_key=idempotency_key, policy=run_policy, created_at=now,
                )
                session.add(run)
                session.flush()
                for version_id in pinned_versions:
                    for browser in browsers:
                        session.add(RunCaseRecord(
                            id=uuid4(), run_id=run.id, case_version_id=version_id,
                            browser_name=browser, status="QUEUED", started_at=None, finished_at=None,
                        ))
                session.add(OutboxRecord(
                    aggregate_type="run", aggregate_id=run.id,
                    event_type="RUN_REQUESTED",
                    payload={"run_id": str(run.id), "project_id": str(run.project_id),
                             "trigger_source": trigger.trigger_source},
                    status="PENDING", attempts=0, available_at=now, created_at=now,
                ))
                trigger.status = "COMPLETED"
                trigger.error_code = None
                session.flush()
                return self._run_dict(run)
        except RunCreationBlocked as exc:
            self._mark_trigger_failed(trigger_id, error_code=exc.error_code)
            return {"trigger_id": trigger_id, "status": "FAILED", "error_code": exc.error_code}
        except EnvironmentScopeError:
            self._mark_trigger_failed(trigger_id, error_code="ENVIRONMENT_SNAPSHOT_FAILED")
            return {"trigger_id": trigger_id, "status": "FAILED",
                    "error_code": "ENVIRONMENT_SNAPSHOT_FAILED"}

    def _mark_trigger_failed(self, trigger_id: UUID, *, error_code: str):
        with self.session_factory.begin() as session:
            trigger = session.get(ScheduleTriggerRecord, trigger_id, with_for_update=True)
            if trigger is not None and trigger.status != "COMPLETED":
                trigger.status = "FAILED"
                trigger.error_code = error_code

    @staticmethod
    def _run_dict(row):
        return {"id": row.id, "project_id": row.project_id, "plan_id": row.plan_id,
                "environment_id": row.environment_id,
                "environment_snapshot_id": row.environment_snapshot_id,
                "status": row.status, "trigger_source": row.trigger_source,
                "browser_matrix": row.browser_matrix, "created_at": row.created_at}
