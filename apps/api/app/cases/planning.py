"""Persistence for generation job envelopes and test suites/plans/schedules.

This layer records durable intent; it does not fabricate model output or
execute browser runs.
"""

import base64
import binascii
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, Numeric, String, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal, Protocol
from decimal import Decimal

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.persistence import CaseRepository, CaseScopeError
from apps.api.app.cases.persistence import TestCaseRecord, TestCaseVersionRecord
from apps.api.app.cases.dsl import Assertion, DslValidationFailed, Step, validate_dsl
from apps.api.app.cases.scheduling import next_fire_at
from apps.api.app.environments.persistence import EnvironmentRecord
from apps.api.app.tenancy.persistence import ProjectRecord

GENERATION_MAX_AUTOMATIC_RETRIES = 3
GENERATION_RETRY_BASE_SECONDS = 2


class TestSuiteRecord(AuthBase):
    __tablename__ = "test_suites"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SuiteCaseRecord(AuthBase):
    __tablename__ = "suite_cases"
    suite_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_suites.id", ondelete="CASCADE"), primary_key=True)
    case_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_cases.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class TestPlanRecord(AuthBase):
    __tablename__ = "test_plans"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    suite_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_suites.id"))
    policy: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScheduleRecord(AuthBase):
    __tablename__ = "schedules"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_plans.id"), nullable=False)
    environment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("environments.id"), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxRecord(AuthBase):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String)
    claimed_by: Mapped[str | None] = mapped_column(String)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleTriggerRecord(AuthBase):
    __tablename__ = "schedule_trigger_requests"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    schedule_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_intent: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    trigger_source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String, nullable=False)
    outbox_event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("outbox_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)


class ScheduleStateEventRecord(AuthBase):
    __tablename__ = "schedule_state_events"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    schedule_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GenerationJobRecord(AuthBase):
    __tablename__ = "generation_jobs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    requirement: Mapped[str] = mapped_column(String, nullable=False)
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    model_version: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[dict | list | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    error_code: Mapped[str | None] = mapped_column(String)
    cost: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    automatic_retries: Mapped[int] = mapped_column(Integer, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class GenerationAcceptanceRecord(AuthBase):
    __tablename__ = "generation_acceptances"
    generation_job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("generation_jobs.id"), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    case_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_cases.id"), nullable=False)
    case_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_case_versions.id"), unique=True)
    accepted_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScenarioDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    priority: Literal["P0", "P1", "P2", "P3"]
    steps: list[Step] = Field(min_length=1)
    assertions: list[Assertion] = Field(min_length=1)


class GeneratedScenario(ScenarioDraft):
    proposal_id: UUID
    requires_human_review: Literal[True] = True
    risk_tags: list[str] = Field(default_factory=lambda: ["UNCLASSIFIED"])


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version: str
    cost: Decimal = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    proposals: list[ScenarioDraft] = Field(min_length=1)


class ScenarioPlanner(Protocol):
    def generate(self, *, requirement: str, model_name: str) -> PlannerOutput | dict: ...


class PlanningConflict(Exception):
    pass


class UsageMetricRecord(AuthBase):
    __tablename__ = "usage_metrics"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("test_runs.id"))
    metric_name: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlanningRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_suite(self, actor_id, project_id, *, name, description, now):
        with self.session_factory.begin() as session:
            CaseRepository._visible_project(session, actor_id, project_id, for_write=True)
            row = TestSuiteRecord(id=uuid4(), project_id=project_id, name=name, description=description, created_at=now)
            session.add(row)
            session.flush()
            return self._suite_dict(row)

    def list_suites(self, actor_id, project_id):
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            rows = session.scalars(select(TestSuiteRecord).where(TestSuiteRecord.project_id == project_id)
                                   .order_by(TestSuiteRecord.created_at, TestSuiteRecord.id)).all()
            return {"items": [self._suite_dict(row) for row in rows], "next_cursor": None}

    def add_case(self, actor_id, suite_id, *, case_id, sequence):
        from apps.api.app.cases.persistence import TestCaseRecord
        with self.session_factory.begin() as session:
            suite = session.get(TestSuiteRecord, suite_id, with_for_update=True)
            case = session.get(TestCaseRecord, case_id)
            if suite is None or case is None or suite.project_id != case.project_id:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, suite.project_id, for_write=True)
            link = session.get(SuiteCaseRecord, (suite_id, case_id), with_for_update=True)
            if link is None:
                link = SuiteCaseRecord(suite_id=suite_id, case_id=case_id, sequence=sequence)
                session.add(link)
            else:
                link.sequence = sequence
            session.flush()
            links = session.execute(select(SuiteCaseRecord, TestCaseRecord)
                                    .join(TestCaseRecord, TestCaseRecord.id == SuiteCaseRecord.case_id)
                                    .where(SuiteCaseRecord.suite_id == suite_id)
                                    .order_by(SuiteCaseRecord.sequence, SuiteCaseRecord.case_id)).all()
            result = self._suite_dict(suite)
            result["cases"] = [{"case_id": c.id, "title": c.title, "sequence": link.sequence} for link, c in links]
            return result

    def list_suite_cases(self, actor_id, suite_id):
        with self.session_factory() as session:
            suite = session.get(TestSuiteRecord, suite_id)
            if suite is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, suite.project_id)
            from apps.api.app.cases.persistence import TestCaseRecord
            links = session.execute(select(SuiteCaseRecord, TestCaseRecord)
                .join(TestCaseRecord, TestCaseRecord.id == SuiteCaseRecord.case_id)
                .where(SuiteCaseRecord.suite_id == suite_id)
                .order_by(SuiteCaseRecord.sequence, SuiteCaseRecord.case_id)).all()
            result = self._suite_dict(suite)
            result["cases"] = [{"case_id": case.id, "title": case.title,
                                "sequence": link.sequence} for link, case in links]
            return result

    def create_plan(self, actor_id, project_id, *, name, suite_id, policy, now):
        with self.session_factory.begin() as session:
            CaseRepository._visible_project(session, actor_id, project_id, for_write=True)
            if suite_id is not None:
                suite = session.get(TestSuiteRecord, suite_id)
                if suite is None or suite.project_id != project_id:
                    raise CaseScopeError
            plan_policy = dict(policy or {})
            plan_policy.setdefault("concurrency_mode", "serial")
            plan_policy.setdefault("max_concurrent_runs", 1)
            row = TestPlanRecord(id=uuid4(), project_id=project_id, name=name, suite_id=suite_id,
                                 policy=plan_policy, created_at=now)
            session.add(row)
            session.flush()
            return self._plan_dict(row)

    def list_plans(self, actor_id, project_id):
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            rows = session.scalars(select(TestPlanRecord).where(TestPlanRecord.project_id == project_id)
                                   .order_by(TestPlanRecord.created_at, TestPlanRecord.id)).all()
            return {"items": [self._plan_dict(row) for row in rows], "next_cursor": None}

    def create_schedule(self, actor_id, plan_id, *, environment_id, cron_expression, timezone, now):
        with self.session_factory.begin() as session:
            plan = session.get(TestPlanRecord, plan_id)
            env = session.get(EnvironmentRecord, environment_id)
            if plan is None or env is None or plan.project_id != env.project_id:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, plan.project_id, for_write=True)
            row = ScheduleRecord(id=uuid4(), plan_id=plan_id, environment_id=environment_id,
                                 cron_expression=cron_expression, timezone=timezone, enabled=True,
                                 next_fire_at=next_fire_at(cron_expression, timezone, now),
                                 last_fire_at=None, created_at=now)
            session.add(row)
            session.flush()
            return self._schedule_dict(row)

    def list_schedules(self, actor_id, plan_id):
        with self.session_factory() as session:
            plan = session.get(TestPlanRecord, plan_id)
            if plan is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, plan.project_id)
            rows = session.scalars(select(ScheduleRecord).where(ScheduleRecord.plan_id == plan_id)
                                   .order_by(ScheduleRecord.created_at, ScheduleRecord.id)).all()
            return {"items": [self._schedule_dict(row) for row in rows], "next_cursor": None}

    def _schedule_for_write(self, session, actor_id, schedule_id):
        row = session.get(ScheduleRecord, schedule_id, with_for_update=True)
        if row is None:
            raise CaseScopeError
        project_id = session.scalar(select(TestPlanRecord.project_id).where(TestPlanRecord.id == row.plan_id))
        CaseRepository._visible_project(session, actor_id, project_id, for_write=True)
        return row

    def get_schedule(self, actor_id, schedule_id):
        with self.session_factory() as session:
            row = session.get(ScheduleRecord, schedule_id)
            if row is None:
                raise CaseScopeError
            project_id = session.scalar(select(TestPlanRecord.project_id).where(TestPlanRecord.id == row.plan_id))
            CaseRepository._visible_project(session, actor_id, project_id)
            return self._schedule_dict(row)

    def project_id_for_schedule(self, schedule_id):
        with self.session_factory() as session:
            return session.scalar(select(TestPlanRecord.project_id)
                                  .join(ScheduleRecord, ScheduleRecord.plan_id == TestPlanRecord.id)
                                  .where(ScheduleRecord.id == schedule_id))

    def project_id_for_suite(self, suite_id):
        with self.session_factory() as session:
            return session.scalar(select(TestSuiteRecord.project_id).where(TestSuiteRecord.id == suite_id))

    def project_id_for_plan(self, plan_id):
        with self.session_factory() as session:
            return session.scalar(select(TestPlanRecord.project_id).where(TestPlanRecord.id == plan_id))

    def set_schedule_enabled(self, actor_id, schedule_id, *, enabled, reason, now):
        with self.session_factory.begin() as session:
            row = self._schedule_for_write(session, actor_id, schedule_id)
            row.enabled = enabled
            row.next_fire_at = (next_fire_at(row.cron_expression, row.timezone, now)
                                if enabled else None)
            session.add(ScheduleStateEventRecord(id=uuid4(), schedule_id=schedule_id,
                actor_user_id=actor_id, event_type="ENABLED" if enabled else "DISABLED",
                reason=reason, created_at=now))
            session.flush()
            return self._schedule_dict(row)

    def update_schedule(self, actor_id, schedule_id, *, changes, reason=None, now):
        with self.session_factory.begin() as session:
            row = self._schedule_for_write(session, actor_id, schedule_id)
            enabled_changed = "enabled" in changes and changes["enabled"] != row.enabled
            if enabled_changed and not reason:
                raise ValueError("a non-empty reason is required when changing schedule enabled state")
            for field in ("cron_expression", "timezone", "enabled"):
                if field in changes:
                    setattr(row, field, changes[field])
            if enabled_changed:
                session.add(ScheduleStateEventRecord(id=uuid4(), schedule_id=schedule_id,
                    actor_user_id=actor_id,
                    event_type="ENABLED" if row.enabled else "DISABLED",
                    reason=reason, created_at=now))
            row.next_fire_at = (next_fire_at(row.cron_expression, row.timezone, now)
                                if row.enabled else None)
            session.flush()
            return self._schedule_dict(row)

    def delete_schedule(self, actor_id, schedule_id):
        with self.session_factory.begin() as session:
            row = self._schedule_for_write(session, actor_id, schedule_id)
            result = self._schedule_dict(row)
            session.delete(row)
            return result

    def trigger_schedule(self, actor_id, schedule_id, *, idempotency_key, request_intent, reason=None, now):
        try:
            with self.session_factory.begin() as session:
                schedule = self._schedule_for_write(session, actor_id, schedule_id)
                existing = session.scalar(select(ScheduleTriggerRecord).where(
                    ScheduleTriggerRecord.schedule_id == schedule_id,
                    ScheduleTriggerRecord.idempotency_key == idempotency_key))
                if existing is not None:
                    return self._schedule_dict(schedule)
                trigger = ScheduleTriggerRecord(id=uuid4(), schedule_id=schedule_id,
                    idempotency_key=idempotency_key, request_intent=request_intent,
                    reason=reason, status="PENDING",
                    trigger_source="manual", outbox_event_id=None, created_at=now)
                session.add(trigger)
                session.flush()
                event = OutboxRecord(aggregate_type="schedule", aggregate_id=schedule_id,
                    event_type="SCHEDULE_TRIGGER_REQUESTED",
                    payload={"trigger_id": str(trigger.id), "plan_id": str(schedule.plan_id),
                             "environment_id": str(schedule.environment_id), "request_intent": request_intent,
                             "reason": reason, "trigger_source": "manual"},
                    status="PENDING", attempts=0, available_at=now, created_at=now)
                session.add(event)
                session.flush()
                trigger.outbox_event_id = event.id
                trigger.status = "ENQUEUED"
                session.flush()
                return self._schedule_dict(schedule)
        except IntegrityError:
            with self.session_factory() as session:
                existing = session.scalar(select(ScheduleTriggerRecord).where(
                    ScheduleTriggerRecord.schedule_id == schedule_id,
                    ScheduleTriggerRecord.idempotency_key == idempotency_key))
                if existing is None:
                    raise
                schedule = session.get(ScheduleRecord, schedule_id)
                if schedule is None:
                    raise
                return self._schedule_dict(schedule)

    def scan_due_schedules(self, *, now, max_lateness_seconds=60, limit=100):
        """Create idempotent Outbox intents, skipping occurrences over the approved lateness window."""
        due_count = 0
        skipped_count = 0
        with self.session_factory.begin() as session:
            rows = session.scalars(select(ScheduleRecord).where(
                ScheduleRecord.enabled.is_(True), ScheduleRecord.next_fire_at <= now
            ).order_by(ScheduleRecord.next_fire_at, ScheduleRecord.id)
             .limit(limit).with_for_update(skip_locked=True)).all()
            for schedule in rows:
                scheduled_for = schedule.next_fire_at
                lateness = (now - scheduled_for).total_seconds()
                if lateness > max_lateness_seconds:
                    schedule.next_fire_at = next_fire_at(
                        schedule.cron_expression, schedule.timezone, now)
                    skipped_count += 1
                    continue

                scheduled_key = "cron:" + scheduled_for.astimezone(timezone.utc).isoformat(timespec="seconds")
                existing = session.scalar(select(ScheduleTriggerRecord).where(
                    ScheduleTriggerRecord.schedule_id == schedule.id,
                    ScheduleTriggerRecord.idempotency_key == scheduled_key))
                if existing is None:
                    trigger = ScheduleTriggerRecord(id=uuid4(), schedule_id=schedule.id,
                        idempotency_key=scheduled_key, request_intent="Scheduled occurrence",
                        trigger_source="schedule", status="PENDING", outbox_event_id=None, created_at=now)
                    session.add(trigger)
                    session.flush()
                    event = OutboxRecord(aggregate_type="schedule", aggregate_id=schedule.id,
                        event_type="SCHEDULE_TRIGGER_REQUESTED",
                        payload={"trigger_id": str(trigger.id), "schedule_id": str(schedule.id),
                            "plan_id": str(schedule.plan_id), "environment_id": str(schedule.environment_id),
                            "request_intent": trigger.request_intent, "trigger_source": "schedule",
                            "scheduled_for": scheduled_for.astimezone(timezone.utc).isoformat()},
                        status="PENDING", attempts=0, available_at=now, created_at=now)
                    session.add(event)
                    session.flush()
                    trigger.outbox_event_id = event.id
                    trigger.status = "ENQUEUED"
                schedule.last_fire_at = scheduled_for
                schedule.next_fire_at = next_fire_at(
                    schedule.cron_expression, schedule.timezone, scheduled_for)
                due_count += 1
        return {"enqueued": due_count, "skipped_missed": skipped_count}

    def create_generation_job(self, actor_id, project_id, *, requirement, model_name, now):
        with self.session_factory.begin() as session:
            CaseRepository._visible_project(session, actor_id, project_id, for_write=True)
            job = GenerationJobRecord(id=uuid4(), project_id=project_id, created_by=actor_id,
                requirement=requirement, model_name=model_name, model_version=None, status="QUEUED",
                result=None, error_code=None, cost=0, attempts=0, automatic_retries=0,
                cancel_requested=False,
                created_at=now, started_at=None, finished_at=None)
            session.add(job)
            session.flush()
            session.add(OutboxRecord(aggregate_type="generation", aggregate_id=job.id,
                event_type="GENERATION_REQUESTED",
                payload={"generation_id": str(job.id), "project_id": str(project_id)},
                status="PENDING", attempts=0, available_at=now, created_at=now))
            session.flush()
            return self._job_dict(job)

    def get_generation_job(self, actor_id, job_id):
        with self.session_factory() as session:
            job = session.get(GenerationJobRecord, job_id)
            if job is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, job.project_id)
            return self._job_dict(job)

    @staticmethod
    def _suite_dict(row):
        return {"id": row.id, "project_id": row.project_id, "name": row.name,
                "description": row.description, "created_at": row.created_at}

    @staticmethod
    def _plan_dict(row):
        return {"id": row.id, "project_id": row.project_id, "name": row.name,
                "suite_id": row.suite_id, "policy": row.policy, "created_at": row.created_at}

    @staticmethod
    def _schedule_dict(row):
        return {"id": row.id, "plan_id": row.plan_id, "environment_id": row.environment_id,
                "cron_expression": row.cron_expression, "timezone": row.timezone,
                "enabled": row.enabled, "next_fire_at": row.next_fire_at,
                "last_fire_at": row.last_fire_at, "created_at": row.created_at}

    @staticmethod
    def _job_dict(row):
        return {"id": row.id, "project_id": row.project_id, "status": row.status,
                "model_name": row.model_name, "model_version": row.model_version,
                "result": row.result, "error_code": row.error_code, "cost": row.cost,
                "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
                "attempts": row.attempts, "automatic_retries": row.automatic_retries,
                "created_at": row.created_at, "started_at": row.started_at, "finished_at": row.finished_at}


class GenerationRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def list_dead_outbox_events(self, actor_id, *, limit=50):
        with self.session_factory() as session:
            user = session.get(UserRecord, actor_id)
            if user is None or user.platform_role != "platform_admin":
                raise PermissionError("platform administrator access required")
            rows = session.scalars(
                select(OutboxRecord)
                .where(OutboxRecord.status == "DEAD",
                       OutboxRecord.event_type == "GENERATION_REQUESTED")
                .order_by(OutboxRecord.created_at.desc(), OutboxRecord.id.desc())
                .limit(limit)
            ).all()
            return [{
                "id": row.id,
                "aggregate_id": row.aggregate_id,
                "event_type": row.event_type,
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "created_at": row.created_at,
                "available_at": row.available_at,
            } for row in rows]

    def create_job(self, actor_id, project_id, *, requirement, model_name, now):
        with self.session_factory.begin() as session:
            CaseRepository._visible_project(session, actor_id, project_id, for_write=True)
            job = GenerationJobRecord(
                id=uuid4(), project_id=project_id, created_by=actor_id,
                requirement=requirement, model_name=model_name, model_version=None,
                status="QUEUED", result=None, error_code=None, cost=0,
                input_tokens=0, output_tokens=0, attempts=0, automatic_retries=0,
                cancel_requested=False, created_at=now, started_at=None, finished_at=None,
            )
            session.add(job)
            session.flush()
            event = OutboxRecord(
                aggregate_type="generation", aggregate_id=job.id,
                event_type="GENERATION_REQUESTED",
                payload={"generation_id": str(job.id), "project_id": str(project_id)},
                status="PENDING", attempts=0, available_at=now, created_at=now,
            )
            session.add(event)
            session.flush()
            return PlanningRepository._job_dict(job)

    def get_job(self, actor_id, job_id):
        with self.session_factory() as session:
            job = session.get(GenerationJobRecord, job_id)
            if job is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, job.project_id)
            return PlanningRepository._job_dict(job)

    def list_jobs(self, actor_id, project_id, *, limit=30, cursor=None):
        cursor_key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            CaseRepository._visible_project(session, actor_id, project_id)
            statement = select(GenerationJobRecord).where(
                GenerationJobRecord.project_id == project_id)
            if cursor_key is not None:
                created_at, job_id = cursor_key
                statement = statement.where(
                    (GenerationJobRecord.created_at < created_at)
                    | ((GenerationJobRecord.created_at == created_at)
                       & (GenerationJobRecord.id < job_id)))
            rows = session.scalars(statement.order_by(
                GenerationJobRecord.created_at.desc(), GenerationJobRecord.id.desc()
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            next_cursor = self._encode_cursor(items[-1]) if has_more and items else None
            return {"items": [PlanningRepository._job_dict(row) for row in items],
                    "next_cursor": next_cursor}

    @staticmethod
    def _encode_cursor(row):
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        value = f"{created_at.astimezone(timezone.utc).isoformat()}\0{row.id}".encode()
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor):
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
            raw_timestamp, raw_id = raw.split("\0", 1)
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None:
                raise ValueError("cursor timestamp must include a timezone")
            return timestamp.astimezone(timezone.utc), UUID(raw_id)
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ValueError("Invalid generation cursor") from exc

    def claim_job(self, job_id, *, now, lease_seconds=600):
        with self.session_factory.begin() as session:
            job = session.get(GenerationJobRecord, job_id, with_for_update=True)
            if job is None:
                return None
            if job.cancel_requested:
                if job.status == "QUEUED":
                    job.status = "CANCELLED"
                    job.finished_at = now
                    job.lease_expires_at = None
                    job.attempt_token = None
                    session.flush()
                return None
            # Expired attempts are retried only by recover_expired_jobs, which
            # first invalidates the old fencing token and applies retry limits.
            if job.status != "QUEUED":
                return None
            job.status = "RUNNING"
            job.started_at = now
            job.finished_at = None
            job.error_code = None
            job.attempts += 1
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.attempt_token = uuid4()
            session.flush()
            return {"id": job.id, "requirement": job.requirement,
                    "model_name": job.model_name, "attempt_token": job.attempt_token}

    def recover_expired_jobs(self, *, now, limit=100):
        """Requeue jobs whose worker lease expired; stale attempt tokens cannot commit results."""
        recovered = 0
        cancelled = 0
        failed = 0
        with self.session_factory.begin() as session:
            jobs = session.scalars(
                select(GenerationJobRecord)
                .where(
                    GenerationJobRecord.status == "RUNNING",
                    GenerationJobRecord.lease_expires_at <= now,
                )
                .order_by(GenerationJobRecord.lease_expires_at, GenerationJobRecord.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for job in jobs:
                job.lease_expires_at = None
                job.attempt_token = None
                if job.cancel_requested:
                    job.status = "CANCELLED"
                    job.finished_at = now
                    cancelled += 1
                    continue
                if job.automatic_retries >= GENERATION_MAX_AUTOMATIC_RETRIES:
                    job.status = "FAILED"
                    job.error_code = "WORKER_LEASE_EXPIRED_RETRIES_EXHAUSTED"
                    job.finished_at = now
                    failed += 1
                    continue
                job.automatic_retries += 1
                retry_delay = GENERATION_RETRY_BASE_SECONDS ** job.automatic_retries
                job.status = "QUEUED"
                job.started_at = None
                job.finished_at = None
                job.error_code = "WORKER_LEASE_EXPIRED"
                session.add(OutboxRecord(
                    aggregate_type="generation",
                    aggregate_id=job.id,
                    event_type="GENERATION_REQUESTED",
                    payload={"generation_id": str(job.id), "project_id": str(job.project_id),
                             "recovered_attempt": job.attempts + 1,
                             "automatic_retry": job.automatic_retries},
                    status="PENDING",
                    attempts=0,
                    available_at=now + timedelta(seconds=retry_delay),
                    created_at=now,
                ))
                recovered += 1
            session.flush()
        return {"recovered": recovered, "cancelled": cancelled, "failed": failed}

    def complete_job(self, job_id, *, attempt_token, output: PlannerOutput,
                     proposals: list[GeneratedScenario], now):
        with self.session_factory.begin() as session:
            job = session.get(GenerationJobRecord, job_id, with_for_update=True)
            if (job is None or job.status != "RUNNING" or job.attempt_token != attempt_token
                    or job.lease_expires_at is None or job.lease_expires_at <= now):
                return None
            if job.cancel_requested:
                job.status = "CANCELLED"
                job.result = None
                job.finished_at = now
            else:
                job.status = "SUCCEEDED"
                job.result = {"proposals": [item.model_dump(mode="json") for item in proposals]}
                job.model_version = output.model_version
                job.cost = output.cost
                job.input_tokens = output.input_tokens
                job.output_tokens = output.output_tokens
                job.finished_at = now
                project = session.get(ProjectRecord, job.project_id)
                if output.cost > 0:
                    session.add(UsageMetricRecord(organization_id=project.organization_id, run_id=None,
                        metric_name="generation_cost", value=output.cost, unit="USD", created_at=now))
                for metric_name, value in (
                    ("generation_input_tokens", output.input_tokens),
                    ("generation_output_tokens", output.output_tokens),
                ):
                    if value:
                        session.add(UsageMetricRecord(organization_id=project.organization_id, run_id=None,
                            metric_name=metric_name, value=value, unit="token", created_at=now))
            job.lease_expires_at = None
            job.attempt_token = None
            session.flush()
            return PlanningRepository._job_dict(job)

    def fail_job(self, job_id, *, attempt_token, error_code, now):
        with self.session_factory.begin() as session:
            job = session.get(GenerationJobRecord, job_id, with_for_update=True)
            if (job is None or job.status != "RUNNING" or job.attempt_token != attempt_token
                    or job.lease_expires_at is None or job.lease_expires_at <= now):
                return None
            job.lease_expires_at = None
            job.attempt_token = None
            if job.cancel_requested:
                job.status = "CANCELLED"
                job.error_code = None
                job.finished_at = now
            elif job.automatic_retries < GENERATION_MAX_AUTOMATIC_RETRIES:
                job.automatic_retries += 1
                retry_delay = GENERATION_RETRY_BASE_SECONDS ** job.automatic_retries
                job.status = "QUEUED"
                job.error_code = error_code
                job.started_at = None
                job.finished_at = None
                session.add(OutboxRecord(
                    aggregate_type="generation", aggregate_id=job.id,
                    event_type="GENERATION_REQUESTED",
                    payload={"generation_id": str(job.id), "project_id": str(job.project_id),
                             "automatic_retry": job.automatic_retries},
                    status="PENDING", attempts=0,
                    available_at=now + timedelta(seconds=retry_delay), created_at=now,
                ))
            else:
                job.status = "FAILED"
                job.error_code = error_code
                job.finished_at = now
            session.flush()
            return PlanningRepository._job_dict(job)

    def cancel_job(self, actor_id, job_id, *, now):
        with self.session_factory.begin() as session:
            job = session.get(GenerationJobRecord, job_id, with_for_update=True)
            if job is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, job.project_id, for_write=True)
            if job.status not in {"QUEUED", "RUNNING"}:
                raise PlanningConflict("generation job is no longer cancellable")
            job.cancel_requested = True
            if job.status == "QUEUED":
                job.status = "CANCELLED"
                job.finished_at = now
            events = session.scalars(select(OutboxRecord).where(
                OutboxRecord.aggregate_type == "generation",
                OutboxRecord.aggregate_id == job.id,
                OutboxRecord.status == "PENDING",
            ).with_for_update()).all()
            for event in events:
                event.status = "DEAD"
                event.last_error = "generation_cancelled"
            session.flush()
            return PlanningRepository._job_dict(job)

    def retry_job(self, actor_id, job_id, *, now):
        with self.session_factory.begin() as session:
            job = session.get(GenerationJobRecord, job_id, with_for_update=True)
            if job is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, job.project_id, for_write=True)
            if job.status != "FAILED":
                raise PlanningConflict("only failed generation jobs can be retried")
            job.status = "QUEUED"
            job.result = None
            job.error_code = None
            job.cancel_requested = False
            job.started_at = None
            job.finished_at = None
            job.lease_expires_at = None
            job.attempt_token = None
            session.flush()
            session.add(OutboxRecord(
                aggregate_type="generation", aggregate_id=job.id,
                event_type="GENERATION_REQUESTED",
                payload={"generation_id": str(job.id), "project_id": str(job.project_id),
                         "retry_attempt": job.attempts + 1},
                status="PENDING", attempts=0, available_at=now, created_at=now,
            ))
            session.flush()
            return PlanningRepository._job_dict(job)

    def accept_proposals(self, actor_id, job_id, *, proposal_ids, now):
        with self.session_factory.begin() as session:
            job = session.get(GenerationJobRecord, job_id, with_for_update=True)
            if job is None:
                raise CaseScopeError
            CaseRepository._visible_project(session, actor_id, job.project_id, for_write=True)
            if job.status != "SUCCEEDED" or not isinstance(job.result, dict):
                raise PlanningConflict("generation job has no successful result")
            proposals = {UUID(item["proposal_id"]): item for item in job.result.get("proposals", [])}
            if not proposal_ids or len(set(proposal_ids)) != len(proposal_ids):
                raise PlanningConflict("proposal_ids must be a non-empty unique list")
            if any(proposal_id not in proposals for proposal_id in proposal_ids):
                raise PlanningConflict("proposal not found in this generation result")
            accepted = []
            for proposal_id in proposal_ids:
                prior = session.get(GenerationAcceptanceRecord, (job_id, proposal_id))
                if prior is not None:
                    accepted.append({"proposal_id": prior.proposal_id, "case_id": prior.case_id,
                                     "case_version_id": prior.case_version_id})
                    continue
                proposal = proposals[proposal_id]
                case_id = uuid4()
                dsl = {"schema_version": 2, "case_id": str(case_id), "title": proposal["title"],
                       "steps": proposal["steps"], "assertions": proposal["assertions"]}
                try:
                    validated = validate_dsl(dsl, expected_case_id=case_id)
                except DslValidationFailed as exc:
                    raise PlanningConflict("generation output failed DSL validation") from exc
                case = TestCaseRecord(id=case_id, project_id=job.project_id,
                    title=proposal["title"], priority=proposal["priority"], status="draft",
                    published_version_id=None, created_at=now)
                version_id = uuid4()
                version = TestCaseVersionRecord(id=version_id, case_id=case_id, version=1,
                    dsl_schema_version=validated.schema_version, dsl_title=validated.title,
                    version_name=validated.title,
                    steps=[step.model_dump(mode="json") for step in validated.steps],
                    assertions=[item.model_dump(mode="json") for item in validated.assertions],
                    changelog=f"Accepted from generation {job.id}", review_status="DRAFT",
                    review_reason=None, created_by=actor_id, created_at=now,
                    approved_by=None, approved_at=None)
                acceptance = GenerationAcceptanceRecord(generation_job_id=job.id,
                    proposal_id=proposal_id, case_id=case_id, case_version_id=version_id,
                    accepted_by=actor_id, accepted_at=now)
                session.add_all([case, version, acceptance])
                session.flush()
                accepted.append({"proposal_id": proposal_id, "case_id": case_id,
                                 "case_version_id": version_id})
            return {"generation_id": job.id, "accepted": accepted}

    def project_id_for_job(self, job_id):
        with self.session_factory() as session:
            return session.scalar(select(GenerationJobRecord.project_id)
                                  .where(GenerationJobRecord.id == job_id))


def process_generation_job(repository: GenerationRepository, planner: ScenarioPlanner, job_id: UUID,
                           *, now_factory):
    """Process a claimed outbox job; callers must supply an explicitly configured model adapter."""
    claimed = repository.claim_job(job_id, now=now_factory())
    if claimed is None:
        return None
    try:
        output = PlannerOutput.model_validate(
            planner.generate(requirement=claimed["requirement"], model_name=claimed["model_name"])
        )
        proposals = [GeneratedScenario(proposal_id=uuid4(), **item.model_dump()) for item in output.proposals]
        return repository.complete_job(job_id, attempt_token=claimed["attempt_token"],
            output=output, proposals=proposals, now=now_factory())
    except ValidationError:
        return repository.fail_job(job_id, attempt_token=claimed["attempt_token"],
            error_code="DSL_VALIDATION_FAILED", now=now_factory())
    except Exception:
        return repository.fail_job(job_id, attempt_token=claimed["attempt_token"],
            error_code="MODEL_PROVIDER_FAILED", now=now_factory())
