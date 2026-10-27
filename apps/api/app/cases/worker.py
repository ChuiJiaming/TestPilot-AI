"""Celery entry point for M04 generation and the durable Outbox publisher.

Run in a separate process with ``celery -A apps.api.app.cases.worker:celery_app``.
The API process does not import this module and never executes browser actions.
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import os
from uuid import UUID, uuid4

from celery import Celery
from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from apps.api.app.cases.planning import (
    GenerationRepository,
    OutboxRecord,
    PlanningRepository,
    process_generation_job,
)


def create_celery_app() -> Celery:
    broker = os.getenv("TESTPILOT_CELERY_BROKER_URL") or os.getenv("TESTPILOT_REDIS_URL")
    if not broker:
        raise RuntimeError("Set TESTPILOT_CELERY_BROKER_URL or TESTPILOT_REDIS_URL for the Agent Worker")
    app = Celery("testpilot_agent_worker", broker=broker, include=[__name__])
    app.conf.update(
        accept_content=["json"],
        task_serializer="json",
        result_serializer="json",
        result_backend=None,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"visibility_timeout": 3600},
        timezone="UTC",
        beat_schedule={
            "publish-generation-outbox": {
                "task": "testpilot.dispatch_generation_outbox",
                "schedule": 2.0,
            },
            "scan-due-schedules": {
                "task": "testpilot.scan_due_schedules",
                "schedule": 15.0,
            },
            "recover-expired-generations": {
                "task": "testpilot.recover_expired_generations",
                "schedule": 15.0,
            },
            "dispatch-verification-email-outbox": {
                "task": "testpilot.dispatch_verification_email_outbox",
                "schedule": 5.0,
            },
        },
    )
    return app


celery_app = create_celery_app()


@lru_cache(maxsize=1)
def session_factory():
    database_url = os.getenv("TESTPILOT_DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set TESTPILOT_DATABASE_URL for the Agent Worker")
    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)


class GenerationOutboxPublisher:
    def __init__(self, sessions, *, worker_id: str,
                 event_types=("GENERATION_REQUESTED", "SCHEDULE_TRIGGER_REQUESTED")):
        self.sessions = sessions
        self.worker_id = worker_id
        self.event_types = event_types

    def claim(self, *, now, limit=50, lease_seconds=30):
        with self.sessions.begin() as session:
            statement = select(OutboxRecord).where(
                OutboxRecord.event_type.in_(self.event_types),
                or_(
                    (OutboxRecord.status == "PENDING") & (OutboxRecord.available_at <= now),
                    (OutboxRecord.status == "PUBLISHING") & (
                        OutboxRecord.claimed_until.is_(None) | (OutboxRecord.claimed_until <= now)
                    ),
                ),
            ).order_by(OutboxRecord.available_at, OutboxRecord.id).limit(limit).with_for_update(skip_locked=True)
            rows = session.scalars(statement).all()
            claimed = []
            for row in rows:
                row.status = "PUBLISHING"
                row.claimed_by = self.worker_id
                row.claimed_until = now + timedelta(seconds=lease_seconds)
                row.attempts += 1
                claimed.append({"id": row.id, "event_type": row.event_type,
                                "payload": row.payload, "attempts": row.attempts})
            return claimed

    def published(self, event_id, *, now):
        with self.sessions.begin() as session:
            row = session.get(OutboxRecord, event_id, with_for_update=True)
            if row is None or row.status != "PUBLISHING" or row.claimed_by != self.worker_id:
                return False
            row.status = "PUBLISHED"
            row.published_at = now
            row.claimed_by = None
            row.claimed_until = None
            row.last_error = None
            return True

    def failed(self, event_id, *, now, error_code, max_attempts=8):
        with self.sessions.begin() as session:
            row = session.get(OutboxRecord, event_id, with_for_update=True)
            if row is None or row.status != "PUBLISHING" or row.claimed_by != self.worker_id:
                return False
            row.last_error = error_code
            row.claimed_by = None
            row.claimed_until = None
            if row.attempts >= max_attempts:
                row.status = "DEAD"
            else:
                row.status = "PENDING"
                row.available_at = now + timedelta(seconds=min(300, 2 ** row.attempts))
            return True


@celery_app.task(name="testpilot.dispatch_generation_outbox")
def dispatch_generation_outbox():
    now = datetime.now(timezone.utc)
    publisher = GenerationOutboxPublisher(session_factory(), worker_id=f"outbox:{uuid4()}")
    events = publisher.claim(now=now)
    published = 0
    for event in events:
        try:
            if event["event_type"] == "GENERATION_REQUESTED":
                task_name = "testpilot.generate_scenario"
                task_kwargs = {"generation_id": str(UUID(event["payload"]["generation_id"]))}
            else:
                task_name = "testpilot.create_scheduled_run"
                task_kwargs = {"trigger_id": str(UUID(event["payload"]["trigger_id"]))}
            celery_app.send_task(
                task_name,
                kwargs=task_kwargs,
                task_id=f"generation-outbox-{event['id']}",
            )
            published += int(publisher.published(event["id"], now=datetime.now(timezone.utc)))
        except Exception as exc:
            publisher.failed(event["id"], now=datetime.now(timezone.utc),
                             error_code=f"dispatch:{type(exc).__name__}")
    return {"claimed": len(events), "published": published}


@celery_app.task(name="testpilot.generate_scenario")
def generate_scenario(generation_id: str):
    repository = GenerationRepository(session_factory())

    class ConfiguredPlanner:
        def generate(self, *, requirement: str, model_name: str):
            from apps.api.app.cases.planner_provider import OpenAICompatibleScenarioPlanner
            return OpenAICompatibleScenarioPlanner.from_environment().generate(
                requirement=requirement, model_name=model_name)

    return process_generation_job(
        repository,
        ConfiguredPlanner(),
        UUID(generation_id),
        now_factory=lambda: datetime.now(timezone.utc),
    )


@celery_app.task(name="testpilot.scan_due_schedules")
def scan_due_schedules():
    repository = PlanningRepository(session_factory())
    return repository.scan_due_schedules(now=datetime.now(timezone.utc),
                                         max_lateness_seconds=60)


@celery_app.task(name="testpilot.recover_expired_generations")
def recover_expired_generations():
    repository = GenerationRepository(session_factory())
    return repository.recover_expired_jobs(now=datetime.now(timezone.utc))


@celery_app.task(name="testpilot.create_scheduled_run")
def create_scheduled_run(trigger_id: str):
    from apps.api.app.environments.persistence import EnvironmentSnapshotRepository
    from apps.api.app.runs.persistence import ScheduledRunRepository

    sessions = session_factory()
    repository = ScheduledRunRepository(
        sessions, EnvironmentSnapshotRepository(sessions))
    return repository.create_from_trigger(UUID(trigger_id), now=datetime.now(timezone.utc))


@celery_app.task(name="testpilot.dispatch_verification_email_outbox")
def dispatch_verification_email_outbox():
    from apps.api.app.auth.adapters import SmtpVerificationMailer, VerificationTokenCipher
    from apps.api.app.auth.persistence import SqlAlchemyVerificationRepository

    encryption_keys = os.getenv("TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS")
    if not encryption_keys:
        raise RuntimeError("Set TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS for the verification mail dispatcher")
    cipher = VerificationTokenCipher(encryption_keys)
    mailer = SmtpVerificationMailer(
        host=os.environ["TESTPILOT_SMTP_HOST"],
        port=int(os.environ["TESTPILOT_SMTP_PORT"]),
        username=os.getenv("TESTPILOT_SMTP_USERNAME"),
        password=os.getenv("TESTPILOT_SMTP_PASSWORD"),
        sender=os.environ["TESTPILOT_SMTP_SENDER"],
        verify_url=os.environ["TESTPILOT_EMAIL_VERIFY_URL"],
        starttls=os.getenv("TESTPILOT_SMTP_STARTTLS", "true").casefold() == "true",
    )
    repository = SqlAlchemyVerificationRepository(session_factory())
    worker_id = f"verification-mail:{uuid4()}"
    messages = repository.claim_verification_emails(
        worker_id=worker_id, now=datetime.now(timezone.utc))
    sent = 0
    for message in messages:
        try:
            token = cipher.decrypt(message["token_ciphertext"])
            mailer.send_verification(message["recipient_email"], token, message["expires_at"])
            sent += int(repository.mark_verification_email_sent(
                message["id"], worker_id=worker_id, now=datetime.now(timezone.utc)))
        except Exception as exc:
            repository.retry_verification_email(
                message["id"], worker_id=worker_id, now=datetime.now(timezone.utc),
                error_code=f"{type(exc).__name__}")
    return {"claimed": len(messages), "sent": sent}
