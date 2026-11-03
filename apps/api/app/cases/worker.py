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

# The worker process imports a much smaller module graph than the API. Without this,
# SQLAlchemy cannot resolve cross-module foreign keys (e.g. report_exports.artifact_id
# -> test_artifacts.id) and mapper configuration fails at runtime.
from apps.api.app import orm_models

orm_models.load_all_models()

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
            "publish-report-export-outbox": {
                "task": "testpilot.dispatch_report_export_outbox",
                "schedule": 2.0,
            },
            "publish-webhook-delivery-outbox": {
                "task": "testpilot.dispatch_webhook_outbox",
                "schedule": 2.0,
            },
            "publish-notification-delivery-outbox": {
                "task": "testpilot.dispatch_notification_outbox",
                "schedule": 2.0,
            },
            "recover-expired-report-exports": {
                "task": "testpilot.recover_report_exports",
                "schedule": 15.0,
            },
            "recover-stuck-webhook-deliveries": {
                "task": "testpilot.recover_webhook_deliveries",
                "schedule": 15.0,
            },
            "scan-due-schedules": {
                "task": "testpilot.scan_due_schedules",
                "schedule": 15.0,
            },
            "recover-expired-generations": {
                "task": "testpilot.recover_expired_generations",
                "schedule": 15.0,
            },
            "admit-queued-runs": {
                "task": "testpilot.admit_queued_runs",
                "schedule": 5.0,
            },
            "recover-expired-run-leases": {
                "task": "testpilot.recover_expired_run_leases",
                "schedule": 15.0,
            },
            "expire-run-approvals": {
                "task": "testpilot.expire_run_approvals",
                "schedule": 60.0,
            },
            "expire-healing-approvals": {
                "task": "testpilot.expire_healing_approvals",
                "schedule": 60.0,
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
                 event_types=("GENERATION_REQUESTED", "SCHEDULE_TRIGGER_REQUESTED", "RUN_REQUESTED", "RUN_RESUME_REQUESTED", "REPORT_EXPORT_REQUESTED", "WEBHOOK_DELIVERY_REQUESTED")):
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
    return _dispatch_outbox(
        event_types=("GENERATION_REQUESTED", "SCHEDULE_TRIGGER_REQUESTED", "RUN_REQUESTED", "RUN_RESUME_REQUESTED"),
    )


@celery_app.task(name="testpilot.dispatch_report_export_outbox")
def dispatch_report_export_outbox():
    return _dispatch_outbox(event_types=("REPORT_EXPORT_REQUESTED",))


@celery_app.task(name="testpilot.dispatch_webhook_outbox")
def dispatch_webhook_outbox():
    return _dispatch_outbox(event_types=("WEBHOOK_DELIVERY_REQUESTED",))


@celery_app.task(name="testpilot.dispatch_notification_outbox")
def dispatch_notification_outbox():
    return _dispatch_outbox(event_types=("NOTIFICATION_DELIVERY_REQUESTED",))


_webhook_sender_factory = None
_notification_sender_factory = None


def configure_webhook_sender_factory(factory):
    """Install the deployment-owned sender factory in this worker process."""
    if not callable(factory):
        raise TypeError("webhook sender factory must be callable")
    global _webhook_sender_factory
    _webhook_sender_factory = factory


def configure_notification_sender_factory(factory):
    """Install deployment-owned notification channel adapters in this worker."""
    if not callable(factory):
        raise TypeError("notification sender factory must be callable")
    global _notification_sender_factory
    _notification_sender_factory = factory


@celery_app.task(name="testpilot.deliver_notification")
def deliver_notification(delivery_id: str):
    from apps.api.app.operations.notifications import NotificationDispatcher

    sender = (
        _notification_sender_factory()
        if _notification_sender_factory is not None
        else None
    )
    return NotificationDispatcher(session_factory()).dispatch_delivery_id(
        UUID(str(delivery_id)),
        sender=sender,
        worker_id=f"notification-delivery:{uuid4()}",
        now=datetime.now(timezone.utc),
    )


@celery_app.task(name="testpilot.deliver_webhook")
def deliver_webhook(delivery_id: str):
    from apps.api.app.integrations.delivery import (
        DeliveryRepository,
        FixedHostWebhookSender,
        process_webhook_delivery,
    )
    from apps.api.app.integrations.secrets import UnavailableSecretRefResolver

    sender = (
        _webhook_sender_factory()
        if _webhook_sender_factory is not None
        else FixedHostWebhookSender(UnavailableSecretRefResolver())
    )
    return process_webhook_delivery(
        DeliveryRepository(session_factory()),
        sender,
        UUID(str(delivery_id)),
        worker_id=f"webhook-delivery:{uuid4()}",
        now=datetime.now(timezone.utc),
    )


@celery_app.task(name="testpilot.recover_report_exports")
def recover_report_exports():
    from apps.api.app.reports.persistence import ReportRepository

    repository = ReportRepository(session_factory())
    return repository.requeue_due_exports(now=datetime.now(timezone.utc), limit=100)


@celery_app.task(name="testpilot.recover_webhook_deliveries")
def recover_webhook_deliveries():
    from apps.api.app.integrations.delivery import DeliveryRepository

    repository = DeliveryRepository(session_factory())
    return repository.requeue_due_deliveries(
        now=datetime.now(timezone.utc), limit=100, recovery_after_seconds=60,
    )


def _dispatch_outbox(*, event_types):
    now = datetime.now(timezone.utc)
    publisher = GenerationOutboxPublisher(
        session_factory(), worker_id=f"outbox:{uuid4()}", event_types=event_types,
    )
    events = publisher.claim(now=now)
    published = 0
    for event in events:
        try:
            if event["event_type"] == "GENERATION_REQUESTED":
                task_name = "testpilot.generate_scenario"
                task_kwargs = {"generation_id": str(UUID(event["payload"]["generation_id"]))}
            elif event["event_type"] == "SCHEDULE_TRIGGER_REQUESTED":
                task_name = "testpilot.create_scheduled_run"
                task_kwargs = {"trigger_id": str(UUID(event["payload"]["trigger_id"]))}
            elif event["event_type"] == "RUN_REQUESTED":
                task_name = "testpilot.consume_run_requested"
                task_kwargs = {"event_id": event["id"]}
            elif event["event_type"] == "REPORT_EXPORT_REQUESTED":
                task_name = "testpilot.render_report_export"
                task_kwargs = {"export_id": str(UUID(event["payload"]["export_id"]))}
            elif event["event_type"] == "WEBHOOK_DELIVERY_REQUESTED":
                task_name = "testpilot.deliver_webhook"
                task_kwargs = {"delivery_id": str(UUID(event["payload"]["delivery_id"]))}
            elif event["event_type"] == "NOTIFICATION_DELIVERY_REQUESTED":
                task_name = "testpilot.deliver_notification"
                task_kwargs = {"delivery_id": str(UUID(event["payload"]["delivery_id"]))}
            else:
                task_name = "testpilot.consume_run_resume_requested"
                task_kwargs = {"event_id": event["id"]}
            celery_app.send_task(
                task_name,
                kwargs=task_kwargs,
                task_id=f"outbox-{event['id']}",
            )
            published += int(publisher.published(event["id"], now=datetime.now(timezone.utc)))
        except Exception as exc:
            publisher.failed(event["id"], now=datetime.now(timezone.utc),
                             error_code=f"dispatch:{type(exc).__name__}")
    return {"claimed": len(events), "published": published}


@celery_app.task(name="testpilot.render_report_export")
def render_report_export(export_id: str):
    from apps.api.app.reports.persistence import ReportRepository
    from apps.api.app.reports.rendering import ReportRenderer
    from apps.api.app.reports.storage import (
        ReportStorageError, S3ReportStorage, build_report_object_key,
    )

    now = datetime.now(timezone.utc)
    export_uuid = UUID(str(export_id))
    worker_id = f"report-render:{uuid4()}"
    repository = ReportRepository(session_factory())
    claim = repository.claim_export(export_uuid, worker_id=worker_id, now=now)
    if claim is None:
        return {"claimed": False, "status": "UNCHANGED"}

    try:
        storage = S3ReportStorage.from_environment()
        if storage is None:
            raise ReportStorageError("Report object storage is not configured")
        snapshot = claim.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ReportStorageError("Report snapshot is unavailable")
        rendered = ReportRenderer().render(snapshot, format=claim["format"])
        object_key = build_report_object_key(
            claim["project_id"], claim["run_id"], claim["id"], claim["format"],
        )
        digest = storage.put(object_key, rendered.data, content_type=rendered.content_type)
        artifact_id = uuid4()
        finished = repository.finish_export(
            export_uuid, worker_id=worker_id, artifact_id=artifact_id,
            now=datetime.now(timezone.utc),
            artifact_metadata={
                "storage_key": object_key,
                "content_type": rendered.content_type,
                "sha256": digest,
                "size_bytes": len(rendered.data),
            },
        )
        if not finished:
            return {"claimed": True, "status": "STALE"}
        return {"claimed": True, "status": "SUCCEEDED", "artifact_id": str(artifact_id)}
    except Exception as exc:
        error_code = (
            "REPORT_STORAGE_UNAVAILABLE" if isinstance(exc, ReportStorageError)
            else "REPORT_RENDER_FAILED"
        )
        failed = repository.fail_export(
            export_uuid, worker_id=worker_id, error_code=error_code,
            now=datetime.now(timezone.utc), max_attempts=8,
        )
        if not failed:
            return {"claimed": True, "status": "STALE"}
        return {
            "claimed": True,
            "status": "FAILED" if claim["attempts"] >= 8 else "QUEUED",
            "error_code": error_code,
        }



@celery_app.task(name="testpilot.consume_run_requested")
def consume_run_requested(event_id: int):
    from apps.api.app.environments.persistence import EnvironmentSnapshotRepository
    from apps.api.app.runs.repository import RunRepository

    sessions = session_factory()
    repository = RunRepository(sessions, EnvironmentSnapshotRepository(sessions))
    worker_id = f"run-admission:{uuid4()}"
    result = repository.consume_run_requested(
        int(event_id), worker_id=worker_id, now=datetime.now(timezone.utc))
    if result.get("claimed"):
        celery_app.send_task(
            "testpilot.execute_admitted_run",
            kwargs={
                "run_id": str(result["run_id"]),
                "attempt_token": str(result["attempt_token"]),
                "worker_id": worker_id,
            },
            task_id=f"run-attempt-{result['run_id']}-{result['attempt_token']}",
        )
    return result


@celery_app.task(name="testpilot.admit_queued_runs")
def admit_queued_runs():
    from apps.api.app.environments.persistence import EnvironmentSnapshotRepository
    from apps.api.app.runs.repository import RunRepository

    sessions = session_factory()
    repository = RunRepository(sessions, EnvironmentSnapshotRepository(sessions))
    worker_id = f"run-admission:{uuid4()}"
    claims = repository.claim_queued_runs(
        worker_id=worker_id, now=datetime.now(timezone.utc), limit=50)
    for claim in claims:
        celery_app.send_task(
            "testpilot.execute_admitted_run",
            kwargs={
                "run_id": str(claim["run_id"]),
                "attempt_token": str(claim["attempt_token"]),
                "worker_id": worker_id,
            },
            task_id=f"run-attempt-{claim['run_id']}-{claim['attempt_token']}",
        )
    finalized = repository.finalize_unclaimed_cancellations(
        now=datetime.now(timezone.utc), limit=100)
    return {"claimed": len(claims), "cancelled": len(finalized)}


@celery_app.task(name="testpilot.consume_run_resume_requested")
def consume_run_resume_requested(event_id: int):
    from apps.api.app.runs.repository import RunRepository

    repository = RunRepository(session_factory())
    worker_id = f"run-resume:{uuid4()}"
    result = repository.consume_run_resume_requested(
        int(event_id), worker_id=worker_id, now=datetime.now(timezone.utc))
    if result.get("claimed"):
        celery_app.send_task(
            "testpilot.execute_admitted_run",
            kwargs={
                "run_id": str(result["run_id"]),
                "attempt_token": str(result["attempt_token"]),
                "worker_id": worker_id,
            },
            task_id=f"run-resume-{result['run_id']}-{result['attempt_token']}",
        )
    return result


@celery_app.task(name="testpilot.recover_expired_run_leases")
def recover_expired_run_leases():
    from apps.api.app.environments.persistence import EnvironmentSnapshotRepository
    from apps.api.app.runs.repository import RunRepository

    sessions = session_factory()
    repository = RunRepository(sessions, EnvironmentSnapshotRepository(sessions))
    now = datetime.now(timezone.utc)
    worker_id = f"run-recovery:{uuid4()}"
    recovered = []
    for run_id in repository.expired_run_ids(now=now, limit=100):
        result = repository.reconcile_expired_run(
            run_id, worker_id=worker_id, now=now)
        if (result.get("recovered") and not result.get("manual_reconciliation")
                and not result.get("terminal") and not result.get("paused")):
            recovered.append(result)
            celery_app.send_task(
                "testpilot.execute_admitted_run",
                kwargs={
                    "run_id": str(result["run_id"]),
                    "attempt_token": str(result["attempt_token"]),
                    "worker_id": worker_id,
                },
                task_id=f"run-recovery-{result['run_id']}-{result['attempt_token']}",
            )
    return {"recovered": len(recovered)}


@celery_app.task(name="testpilot.expire_run_approvals")
def expire_run_approvals():
    from apps.api.app.runs.repository import RunRepository

    repository = RunRepository(session_factory())
    return repository.expire_due_run_approvals(now=datetime.now(timezone.utc), limit=100)


@celery_app.task(name="testpilot.expire_healing_approvals")
def expire_healing_approvals():
    from apps.api.app.runs.repository import RunRepository

    repository = RunRepository(session_factory())
    return repository.expire_due_healing_approvals(
        now=datetime.now(timezone.utc), limit=100)


@celery_app.task(name="testpilot.execute_admitted_run")
def execute_admitted_run(run_id: str, attempt_token: str, worker_id: str):
    from apps.api.app.runs.agent import execute_admitted_run as execute_run
    from apps.api.app.quality.artifacts import S3ArtifactStorage
    from apps.api.app.quality.persistence import QualityRepository

    try:
        sessions = session_factory()
        quality_repository = QualityRepository(
            sessions, storage=S3ArtifactStorage.from_environment())
        return execute_run(
            sessions,
            run_id=UUID(run_id),
            attempt_token=UUID(attempt_token),
            worker_id=worker_id,
            quality_repository=quality_repository,
        )
    except Exception as exc:
        from apps.api.app.runs.repository import RunRepository, RunStateConflict, StaleRunAttempt

        repository = RunRepository(session_factory())
        try:
            repository.transition(
                UUID(run_id), "ERROR", expected_status={"PREPARING", "RUNNING"},
                now=datetime.now(timezone.utc),
                payload={"reason_code": f"WORKER_{type(exc).__name__.upper()}"},
                attempt_token=UUID(attempt_token), worker_id=worker_id,
            )
        except (StaleRunAttempt, RunStateConflict):
            pass
        raise

@celery_app.task(name="testpilot.generate_scenario")
def generate_scenario(generation_id: str):
    repository = GenerationRepository(session_factory())
    from apps.api.app.operations.quotas import QuotaService

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
        quota_service=QuotaService(session_factory()),
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
