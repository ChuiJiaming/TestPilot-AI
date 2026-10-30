"""Run lifecycle repository with tenant scope, durable events, and fenced leases."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from uuid import UUID, uuid4
import secrets

from apps.api.app.cases.persistence import (
    CaseApprovalRecord, CaseRepository, TestCaseRecord, TestCaseVersionRecord,
)
from apps.api.app.cases.planning import (
    OutboxRecord, SuiteCaseRecord, TestPlanRecord, TestSuiteRecord,
)
from apps.api.app.core.audit import append_audit_entry
from apps.api.app.environments.persistence import (
    EnvironmentRecord, EnvironmentScopeError, EnvironmentSnapshotRecord, EnvironmentSnapshotRepository,
)
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord, ProjectMembershipRecord, ProjectRecord,
)
from apps.api.app.runs.persistence import (
    AgentEventRecord, BrowserSessionRecord, HealingRecord, ProcessedEventRecord,
    RunAdmissionRepository, RunCaseRecord, RunCreationBlocked, SseTicketRecord, TestRunRecord,
    TestStepRecord, WorkerLeaseRecord,
)
from apps.api.app.runs.recovery import RecoveryProposal, evaluate_recovery_candidate


ACTIVE_STATUSES = {
    "PREPARING", "RUNNING", "WAITING_APPROVAL", "PAUSING", "PAUSED",
    "FINALIZING", "CANCELLING",
}


class RunScopeDenied(Exception):
    pass


class RunStateConflict(Exception):
    def __init__(self, current_status, requested_status):
        super().__init__("RUN_STATE_CONFLICT")
        self.current_status = current_status
        self.requested_status = requested_status


class RunApprovalConflict(Exception):
    pass


class RunApprovalPermissionDenied(Exception):
    pass


class EventCursorExpired(Exception):
    pass


RUN_TRANSITIONS = {
    "QUEUED": {"PREPARING", "CANCELLING"},
    "PREPARING": {"RUNNING", "WAITING_APPROVAL", "ERROR", "CANCELLING"},
    "RUNNING": {"WAITING_APPROVAL", "PAUSING", "FINALIZING", "ERROR", "CANCELLING"},
    "WAITING_APPROVAL": {"ERROR", "CANCELLING"},
    "PAUSING": {"PAUSED", "ERROR", "CANCELLING"},
    "PAUSED": {"RUNNING", "CANCELLING"},
    "FINALIZING": {"PASSED", "FAILED", "ERROR"},
    "CANCELLING": {"CANCELLED", "ERROR"},
    "PASSED": set(), "FAILED": set(), "ERROR": set(), "CANCELLED": set(),
}
TERMINAL_STATUSES = {"PASSED", "FAILED", "ERROR", "CANCELLED"}
RUN_APPROVAL_TTL = timedelta(minutes=30)
HEALING_APPROVAL_TTL = timedelta(minutes=30)
HEALING_EVIDENCE_MAX_BYTES = 16_384
SENSITIVE_EVENT_KEYS = {
    "prompt", "password", "cookie", "secret", "authorization", "access_token",
    "refresh_token", "job_token", "page_content", "html", "body",
}


class StaleRunAttempt(Exception):
    pass


class RunRepository:
    def __init__(self, session_factory, snapshots: EnvironmentSnapshotRepository | None = None):
        self.session_factory = session_factory
        self.snapshots = snapshots or EnvironmentSnapshotRepository(session_factory)
        self.admission = RunAdmissionRepository(session_factory)
        self.cases = CaseRepository(session_factory)

    def create_run(self, actor_id: UUID, *, project_id: UUID, environment_id: UUID,
                   plan_id: UUID | None, browser_matrix: list[str], trigger_source: str,
                   idempotency_key: str, request_intent: str | None = None,
                   branch: str | None = None, commit_sha: str | None = None,
                   now: datetime):
        if not plan_id:
            raise RunCreationBlocked("PLAN_REQUIRED")
        if (not browser_matrix or len(browser_matrix) > 3
                or len(set(browser_matrix)) != len(browser_matrix)
                or any(name not in {"chromium", "firefox", "webkit"} for name in browser_matrix)):
            raise RunCreationBlocked("INVALID_BROWSER_MATRIX")
        if trigger_source not in {"manual", "ci", "schedule", "api"}:
            raise RunCreationBlocked("INVALID_TRIGGER_SOURCE")
        try:
            with self.session_factory.begin() as session:
                project = self._visible_project(session, actor_id, project_id, for_write=True)
                existing = session.scalar(select(TestRunRecord).where(
                    TestRunRecord.project_id == project_id,
                    TestRunRecord.idempotency_key == idempotency_key,
                ))
                if existing is not None:
                    return self._run_dict(existing)
                plan = session.get(TestPlanRecord, plan_id)
                if plan is None or plan.project_id != project.id or plan.suite_id is None:
                    raise RunCreationBlocked("PLAN_NOT_FOUND")
                suite = session.get(TestSuiteRecord, plan.suite_id)
                environment = session.get(EnvironmentRecord, environment_id)
                if suite is None or suite.project_id != project.id:
                    raise RunCreationBlocked("SUITE_NOT_FOUND")
                if environment is None or environment.project_id != project.id:
                    raise RunCreationBlocked("ENVIRONMENT_SCOPE_MISMATCH")
                policy = dict(plan.policy or {})
                configured_budget = policy.get("agent_budget", 0)
                policy["agent_budget_remaining"] = min(
                    1000, max(0, configured_budget if type(configured_budget) is int else 0))
                mode = policy.get("concurrency_mode", "serial")
                maximum = policy.get("max_concurrent_runs", 1)
                if (mode not in {"serial", "bounded"} or type(maximum) is not int or maximum < 1
                        or (mode == "serial" and maximum != 1)):
                    raise RunCreationBlocked("INVALID_PLAN_CONCURRENCY_POLICY")
                associations = session.execute(
                    select(SuiteCaseRecord, TestCaseRecord)
                    .join(TestCaseRecord, TestCaseRecord.id == SuiteCaseRecord.case_id)
                    .where(SuiteCaseRecord.suite_id == suite.id)
                    .order_by(SuiteCaseRecord.sequence, SuiteCaseRecord.case_id)
                ).all()
                if not associations:
                    raise RunCreationBlocked("SUITE_HAS_NO_CASES")
                version_ids = []
                for _, case in associations:
                    if case.status != "active" or case.published_version_id is None:
                        raise RunCreationBlocked("SUITE_CASE_NOT_PUBLISHED")
                    version = session.get(TestCaseVersionRecord, case.published_version_id)
                    if version is None or version.case_id != case.id:
                        raise RunCreationBlocked("PUBLISHED_VERSION_NOT_FOUND")
                    version_ids.append(version.id)
                snapshot = self.snapshots.capture(
                    environment_id=environment.id, project_id=project.id,
                    now=now, session=session)
                run_id = uuid4()
                run = TestRunRecord(
                    id=run_id, project_id=project.id, plan_id=plan.id,
                    environment_id=environment.id, environment_snapshot_id=snapshot["id"],
                    requested_by=actor_id, request_intent=request_intent,
                    branch=branch, commit_sha=commit_sha, status="QUEUED",
                    trigger_source=trigger_source, browser_matrix=list(browser_matrix),
                    idempotency_key=idempotency_key, policy=policy,
                    event_replay_floor=0, created_at=now,
                )
                session.add(run)
                session.flush()
                for version_id in version_ids:
                    for browser in browser_matrix:
                        session.add(RunCaseRecord(
                            id=uuid4(), run_id=run_id, case_version_id=version_id,
                            browser_name=browser, status="QUEUED",
                        ))
                self._append_event(session, run_id, "run.queued", {
                    "trigger_source": trigger_source,
                    "browser_matrix": list(browser_matrix),
                    "run_case_count": len(version_ids) * len(browser_matrix),
                }, now)
                session.add(OutboxRecord(
                    aggregate_type="run", aggregate_id=run_id,
                    event_type="RUN_REQUESTED",
                    payload={"run_id": str(run_id), "project_id": str(project_id),
                             "trigger_source": trigger_source},
                    status="PENDING", attempts=0, available_at=now, created_at=now,
                ))
                return self._run_dict(run)
        except EnvironmentScopeError as exc:
            raise RunCreationBlocked("ENVIRONMENT_SNAPSHOT_FAILED") from exc
        except IntegrityError:
            with self.session_factory() as session:
                existing = session.scalar(select(TestRunRecord).where(
                    TestRunRecord.project_id == project_id,
                    TestRunRecord.idempotency_key == idempotency_key))
                if existing is not None:
                    return self._run_dict(existing)
            raise

    def consume_run_requested(self, event_id: int, *, worker_id: str, now: datetime):
        with self.session_factory.begin() as session:
            event = session.scalar(select(OutboxRecord).where(
                OutboxRecord.id == event_id).with_for_update())
            if event is None or event.event_type != "RUN_REQUESTED":
                return {"claimed": False, "reason": "RUN_EVENT_NOT_FOUND"}
            processed = session.get(ProcessedEventRecord, {
                "consumer_name": "run-admission-v1", "event_id": event_id,
            })
            if processed is not None:
                return {"claimed": False, "duplicate": True, "reason": "ALREADY_PROCESSED"}
            try:
                run_id = UUID(str(event.payload["run_id"]))
            except (KeyError, ValueError, TypeError):
                session.add(ProcessedEventRecord(
                    consumer_name="run-admission-v1", event_id=event_id, processed_at=now))
                return {"claimed": False, "reason": "INVALID_RUN_EVENT"}
            claim = self.admission.claim(
                run_id, worker_id=worker_id, now=now, session=session)
            session.add(ProcessedEventRecord(
                consumer_name="run-admission-v1", event_id=event_id, processed_at=now))
            return claim

    def claim_queued_runs(self, *, worker_id: str, now: datetime, limit: int = 50):
        # Scan without row locks, then let each admission transaction take locks in
        # the single canonical order: plan first, Run second. Keeping one transaction
        # per candidate avoids cross-plan lock-order cycles in concurrent pollers.
        with self.session_factory() as session:
            run_ids = list(session.scalars(select(TestRunRecord.id).where(
                TestRunRecord.status == "QUEUED"
            ).order_by(TestRunRecord.created_at, TestRunRecord.id).limit(limit)).all())
        claimed = []
        for run_id in run_ids:
            result = self.admission.claim(run_id, worker_id=worker_id, now=now)
            if result.get("claimed"):
                claimed.append(result)
        return claimed

    def consume_agent_budget(self, run_id: UUID, attempt_token: UUID, *,
                             worker_id: str, now: datetime) -> bool:
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id):
                raise StaleRunAttempt
            if run.status in {"PAUSING", "CANCELLING"}:
                return False
            if run.status not in {"PREPARING", "RUNNING"}:
                raise StaleRunAttempt
            policy = dict(run.policy or {})
            configured = policy.get("agent_budget", 0)
            limit = min(1000, max(0, configured if type(configured) is int else 0))
            remaining = policy.get("agent_budget_remaining", limit)
            if type(remaining) is not int or remaining < 0:
                remaining = 0
            if remaining <= 0:
                return False
            remaining -= 1
            policy["agent_budget_remaining"] = remaining
            run.policy = policy
            self._append_event(session, run_id, "run.agent_budget_consumed", {
                "budget_left": remaining,
            }, now)
            return True

    def heartbeat(self, run_id: UUID, attempt_token: UUID, *, worker_id: str,
                  now: datetime, lease_seconds: int = 60):
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id or run.status not in ACTIVE_STATUSES):
                return False
            lease = session.scalar(select(WorkerLeaseRecord).where(
                WorkerLeaseRecord.resource_type == "run",
                WorkerLeaseRecord.resource_id == run_id,
            ).with_for_update())
            if lease is None or lease.attempt_token != attempt_token or lease.lease_owner != worker_id:
                return False
            expires = now + timedelta(seconds=lease_seconds)
            run.heartbeat_at = now
            run.lease_expires_at = expires
            lease.heartbeat_at = now
            lease.lease_expires_at = expires
            return True

    def transition(self, run_id: UUID, new_status: str, *, expected_status,
                   now: datetime, payload: dict | None = None,
                   attempt_token: UUID | None = None, worker_id: str | None = None,
                   actor_user_id: UUID | None = None):
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if run is None:
                return None
            expected = ({expected_status} if isinstance(expected_status, str)
                        else set(expected_status or ()))
            if expected and run.status not in expected:
                raise RunStateConflict(run.status, new_status)
            if new_status not in RUN_TRANSITIONS.get(run.status, set()):
                raise RunStateConflict(run.status, new_status)
            if attempt_token is not None and run.attempt_token != attempt_token:
                raise StaleRunAttempt
            if worker_id is not None and run.lease_owner != worker_id:
                raise StaleRunAttempt
            previous = run.status
            if previous == "WAITING_APPROVAL" and new_status == "RUNNING":
                raise RunApprovalConflict("Run approval must use the approval decision path")

            effective_status = new_status
            cancel_without_worker = new_status == "CANCELLING" and run.lease_owner is None
            if cancel_without_worker:
                effective_status = "CANCELLED"
            run.status = effective_status

            if previous == "WAITING_APPROVAL":
                self._cancel_pending_run_approvals(
                    session, run, now=now, actor_user_id=actor_user_id,
                    reason_code="RUN_CANCELLED" if effective_status == "CANCELLED" else "RUN_LEFT_WAITING_APPROVAL",
                )

            approval = None
            if effective_status == "WAITING_APPROVAL":
                approval = self._ensure_run_approval(
                    session, run, now=now, request_context=payload or {},
                    actor_user_id=actor_user_id,
                )

            if effective_status in TERMINAL_STATUSES | {"PAUSED", "WAITING_APPROVAL"}:
                if effective_status in TERMINAL_STATUSES:
                    run.finished_at = now
                lease = session.scalar(select(WorkerLeaseRecord).where(
                    WorkerLeaseRecord.resource_type == "run",
                    WorkerLeaseRecord.resource_id == run_id,
                ).with_for_update())
                if lease is not None and (attempt_token is None or lease.attempt_token == attempt_token
                                          or cancel_without_worker):
                    session.delete(lease)
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = now
            if cancel_without_worker:
                self._cancel_unfinished_run_work(session, run_id, now=now)
                self._append_event(session, run_id, "run.status_changed", {
                    "from_status": previous, "status": "CANCELLING",
                    "reason_code": (payload or {}).get("reason_code", "USER_REQUESTED"),
                }, now)
                self._append_event(session, run_id, "run.finished", {
                    "from_status": "CANCELLING", "status": "CANCELLED",
                    "reason_code": "CANCELLED_WITHOUT_ACTIVE_WORKER",
                }, now)
            else:
                data = {**(payload or {}), "from_status": previous, "status": effective_status}
                event_name = "run.finished" if effective_status in TERMINAL_STATUSES else "run.status_changed"
                self._append_event(session, run_id, event_name, data, now)
            if approval is not None:
                self._append_run_approval_events(session, run, approval, now=now)
            if previous == "PAUSED" and new_status == "RUNNING":
                session.add(OutboxRecord(
                    aggregate_type="run", aggregate_id=run_id,
                    event_type="RUN_RESUME_REQUESTED", payload={"run_id": str(run_id)},
                    status="PENDING", attempts=0, available_at=now, created_at=now,
                ))
            return self._run_dict(run)

    def retry(self, actor_id: UUID, run_id: UUID, *, reason: str, now: datetime):
        with self.session_factory.begin() as session:
            parent = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if parent is None:
                return None
            self._visible_project(session, actor_id, parent.project_id, for_write=True)
            if parent.status not in {"FAILED", "ERROR", "CANCELLED"}:
                raise RunStateConflict(parent.status, "QUEUED")
            cases = session.scalars(select(RunCaseRecord).where(
                RunCaseRecord.run_id == run_id).order_by(RunCaseRecord.id)).all()
            if not cases:
                raise RunCreationBlocked("RUN_HAS_NO_CASES")
            new_id = uuid4()
            retry_policy = dict(parent.policy or {})
            configured_budget = retry_policy.get("agent_budget", 0)
            retry_policy["agent_budget_remaining"] = min(
                1000, max(0, configured_budget if type(configured_budget) is int else 0))
            run = TestRunRecord(
                id=new_id, project_id=parent.project_id, plan_id=parent.plan_id,
                environment_id=parent.environment_id,
                environment_snapshot_id=parent.environment_snapshot_id,
                parent_run_id=parent.id, requested_by=actor_id, request_intent=reason,
                branch=parent.branch, commit_sha=parent.commit_sha, status="QUEUED",
                trigger_source=parent.trigger_source, browser_matrix=list(parent.browser_matrix),
                idempotency_key=f"retry:{parent.id}:{new_id}",
                policy=retry_policy, event_replay_floor=0, created_at=now,
            )
            session.add(run)
            session.flush()
            for old_case in cases:
                session.add(RunCaseRecord(
                    id=uuid4(), run_id=new_id, case_version_id=old_case.case_version_id,
                    browser_name=old_case.browser_name, status="QUEUED",
                ))
            self._append_event(session, new_id, "run.queued", {
                "trigger_source": run.trigger_source, "parent_run_id": str(parent.id),
            }, now)
            session.add(OutboxRecord(
                aggregate_type="run", aggregate_id=new_id, event_type="RUN_REQUESTED",
                payload={"run_id": str(new_id), "project_id": str(run.project_id),
                         "trigger_source": run.trigger_source},
                status="PENDING", attempts=0, available_at=now, created_at=now,
            ))
            return {"run_id": new_id, "parent_run_id": parent.id, "status": "QUEUED"}

    def expired_run_ids(self, *, now: datetime, limit: int = 100):
        with self.session_factory() as session:
            return list(session.scalars(select(TestRunRecord.id).where(
                TestRunRecord.status.in_(("PREPARING", "RUNNING", "PAUSING", "FINALIZING", "CANCELLING")),
                TestRunRecord.lease_expires_at <= now,
            ).order_by(TestRunRecord.lease_expires_at, TestRunRecord.id).limit(limit)).all())

    def reconcile_expired_run(self, run_id: UUID, *, worker_id: str,
                              now: datetime, lease_seconds: int = 60):
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if (run is None or run.status not in {"PREPARING", "RUNNING", "PAUSING", "FINALIZING", "CANCELLING"}
                    or run.lease_expires_at is None or run.lease_expires_at > now):
                return {"recovered": False, "reason": "LEASE_NOT_EXPIRED"}
            old_token = run.attempt_token
            lease = session.scalar(select(WorkerLeaseRecord).where(
                WorkerLeaseRecord.resource_type == "run",
                WorkerLeaseRecord.resource_id == run_id,
            ).with_for_update())
            if run.status in {"FINALIZING", "CANCELLING"}:
                final_status = "ERROR" if run.status == "FINALIZING" else "CANCELLED"
                run.status = final_status
                run.finished_at = now
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = now
                if lease is not None:
                    session.delete(lease)
                self._append_event(session, run_id, "run.finished", {
                    "status": final_status,
                    "reason_code": "WORKER_LOST_DURING_FINALIZATION" if final_status == "ERROR" else "CANCELLED_AFTER_LEASE_EXPIRY",
                }, now)
                return {"recovered": True, "terminal": True,
                        "manual_reconciliation": False, "run_id": run_id}
            unsafe = session.scalars(select(TestStepRecord).join(
                RunCaseRecord, RunCaseRecord.id == TestStepRecord.run_case_id
            ).where(
                RunCaseRecord.run_id == run_id,
                TestStepRecord.status.in_(("RUNNING", "PASSED")),
                TestStepRecord.replay_policy.in_(("NEVER", "FIXTURE_ONLY")),
                TestStepRecord.attempt_token == old_token,
            )).all()
            new_token = uuid4()
            if unsafe:
                for step in unsafe:
                    if step.status == "RUNNING":
                        step.status = "WAITING_APPROVAL"
                        step.finished_at = now
                previous_status = run.status
                run.status = "WAITING_APPROVAL"
                run.attempt_token = new_token
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = now
                if lease is not None:
                    session.delete(lease)
                approval = self._ensure_run_approval(
                    session, run, now=now,
                    request_context={"reason_code": "UNKNOWN_SIDE_EFFECT",
                                     "step_ids": [str(step.id) for step in unsafe]},
                    actor_user_id=None,
                )
                self._append_event(session, run_id, "run.status_changed", {
                    "from_status": previous_status, "status": "WAITING_APPROVAL",
                    "reason_code": "UNKNOWN_SIDE_EFFECT",
                }, now)
                self._append_run_approval_events(session, run, approval, now=now)
                return {"recovered": True, "manual_reconciliation": True,
                        "run_id": run_id, "attempt_token": new_token,
                        "approval_id": approval.id, "expires_at": approval.expires_at}
            if run.status == "PAUSING":
                run.status = "PAUSED"
                run.attempt_token = new_token
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = now
                if lease is not None:
                    session.delete(lease)
                self._append_event(session, run_id, "run.status_changed", {
                    "from_status": "PAUSING", "status": "PAUSED",
                    "reason_code": "WORKER_LOST_DURING_PAUSE",
                }, now)
                return {"recovered": True, "paused": True,
                        "manual_reconciliation": False, "run_id": run_id,
                        "attempt_token": new_token}
            expires = now + timedelta(seconds=lease_seconds)
            run.attempt_token = new_token
            run.lease_owner = worker_id
            run.lease_expires_at = expires
            run.heartbeat_at = now
            run.claimed_at = now
            if lease is None:
                session.add(WorkerLeaseRecord(
                    id=uuid4(), resource_type="run", resource_id=run_id,
                    lease_owner=worker_id, attempt_token=new_token,
                    lease_expires_at=expires, heartbeat_at=now, claimed_at=now,
                ))
            else:
                lease.lease_owner = worker_id
                lease.attempt_token = new_token
                lease.lease_expires_at = expires
                lease.heartbeat_at = now
                lease.claimed_at = now
            self._append_event(session, run_id, "run.lease_recovered", {
                "previous_attempt_invalidated": True,
            }, now)
            return {"recovered": True, "manual_reconciliation": False,
                    "run_id": run_id, "attempt_token": new_token}

    def consume_run_resume_requested(self, event_id: int, *, worker_id: str, now: datetime):
        with self.session_factory.begin() as session:
            event = session.scalar(select(OutboxRecord).where(
                OutboxRecord.id == event_id).with_for_update())
            if event is None or event.event_type != "RUN_RESUME_REQUESTED":
                return {"claimed": False, "reason": "RESUME_EVENT_NOT_FOUND"}
            processed = session.get(ProcessedEventRecord, {
                "consumer_name": "run-resume-v1", "event_id": event_id,
            })
            if processed is not None:
                return {"claimed": False, "duplicate": True, "reason": "ALREADY_PROCESSED"}
            try:
                run_id = UUID(str(event.payload["run_id"]))
            except (KeyError, ValueError, TypeError):
                session.add(ProcessedEventRecord(
                    consumer_name="run-resume-v1", event_id=event_id, processed_at=now))
                return {"claimed": False, "reason": "INVALID_RESUME_EVENT"}
            plan_id = session.scalar(select(TestRunRecord.plan_id).where(
                TestRunRecord.id == run_id))
            if plan_id is not None:
                session.scalar(select(TestPlanRecord).where(
                    TestPlanRecord.id == plan_id).with_for_update())
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if run is None or run.status != "RUNNING" or run.lease_owner is not None:
                session.add(ProcessedEventRecord(
                    consumer_name="run-resume-v1", event_id=event_id, processed_at=now))
                return {"claimed": False, "reason": "RUN_NOT_RESUMABLE"}
            attempt_token = run.attempt_token or uuid4()
            expires = now + timedelta(seconds=60)
            lease = session.scalar(select(WorkerLeaseRecord).where(
                WorkerLeaseRecord.resource_type == "run",
                WorkerLeaseRecord.resource_id == run_id,
            ).with_for_update())
            if lease is None:
                session.add(WorkerLeaseRecord(
                    id=uuid4(), resource_type="run", resource_id=run_id,
                    lease_owner=worker_id, attempt_token=attempt_token,
                    lease_expires_at=expires, heartbeat_at=now, claimed_at=now,
                ))
            else:
                lease.lease_owner = worker_id
                lease.attempt_token = attempt_token
                lease.lease_expires_at = expires
                lease.heartbeat_at = now
                lease.claimed_at = now
            run.attempt_token = attempt_token
            run.lease_owner = worker_id
            run.lease_expires_at = expires
            run.heartbeat_at = now
            run.claimed_at = now
            run.finished_at = None
            self._append_event(session, run_id, "run.resume_claimed", {
                "attempt_token": str(attempt_token),
            }, now)
            session.add(ProcessedEventRecord(
                consumer_name="run-resume-v1", event_id=event_id, processed_at=now))
            return {"claimed": True, "run_id": run_id,
                    "attempt_token": attempt_token, "worker_id": worker_id}

    def finalize_unclaimed_cancellations(self, *, now: datetime, limit: int = 100):
        finalized = []
        with self.session_factory.begin() as session:
            rows = session.scalars(select(TestRunRecord).where(
                TestRunRecord.status == "CANCELLING",
                TestRunRecord.lease_owner.is_(None),
            ).order_by(TestRunRecord.created_at, TestRunRecord.id)
             .limit(limit).with_for_update(skip_locked=True)).all()
            for run in rows:
                run.status = "CANCELLED"
                run.finished_at = now
                self._append_event(session, run.id, "run.finished", {
                    "status": "CANCELLED", "reason_code": "CANCELLED_BEFORE_ADMISSION",
                }, now)
                finalized.append(run.id)
        return finalized
    def load_execution_bundle(self, run_id: UUID, attempt_token: UUID, *, worker_id: str):
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id
                    or run.status not in {"PREPARING", "RUNNING"}):
                raise StaleRunAttempt
            snapshot = session.get(EnvironmentSnapshotRecord, run.environment_snapshot_id)
            if snapshot is None:
                raise RunCreationBlocked("ENVIRONMENT_SNAPSHOT_NOT_FOUND")
            rows = session.execute(
                select(RunCaseRecord, TestCaseVersionRecord)
                .join(TestCaseVersionRecord, TestCaseVersionRecord.id == RunCaseRecord.case_version_id)
                .where(RunCaseRecord.run_id == run_id)
                .order_by(RunCaseRecord.browser_name, RunCaseRecord.id)
            ).all()
            return {
                "run": {**self._run_dict(run), "requested_by": run.requested_by},
                "policy": dict(run.policy or {}),
                "snapshot": {
                    "id": snapshot.id, "base_url": snapshot.base_url,
                    "network_policy": dict(snapshot.network_policy or {}),
                    "secret_ref_version_id": snapshot.secret_ref_version_id,
                    "snapshot_hash": snapshot.snapshot_hash,
                },
                "cases": [{
                    "run_case_id": run_case.id,
                    "case_version_id": version.id,
                    "case_id": version.case_id,
                    "schema_version": version.dsl_schema_version,
                    "browser": run_case.browser_name,
                    "status": run_case.status,
                    "steps": list(version.steps or []),
                    "assertions": list(version.assertions or []),
                } for run_case, version in rows],
            }

    def execution_status(self, run_id: UUID, attempt_token: UUID, *, worker_id: str):
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id):
                raise StaleRunAttempt
            return run.status
    def set_run_case_state(self, run_case_id: UUID, status: str, *, attempt_token: UUID,
                           worker_id: str, now: datetime):
        if status not in {"RUNNING", "PASSED", "FAILED", "ERROR", "CANCELLED"}:
            raise ValueError("unsupported RunCase status")
        with self.session_factory.begin() as session:
            run_case = session.get(RunCaseRecord, run_case_id)
            if run_case is None:
                return False
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_case.run_id).with_for_update())
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id
                    or run.status not in {"PREPARING", "RUNNING", "PAUSING", "CANCELLING"}):
                raise StaleRunAttempt
            run_case = session.scalar(select(RunCaseRecord).where(
                RunCaseRecord.id == run_case_id).with_for_update())
            if run_case is None:
                return False
            run_case.status = status
            run_case.attempt_token = attempt_token
            run_case.lease_owner = worker_id
            run_case.heartbeat_at = now
            run_case.lease_expires_at = run.lease_expires_at
            if status == "RUNNING":
                run_case.started_at = run_case.started_at or now
            else:
                run_case.finished_at = now
            self._append_event(session, run.id, "run.case_status_changed", {
                "run_case_id": str(run_case_id), "status": status,
                "browser": run_case.browser_name,
            }, now)
            return True

    def start_step(self, run_case_id: UUID, *, action_id: UUID, sequence: int,
                   action_type: str, locator: dict | None, expected: dict | None,
                   replay_policy: str, attempt_token: UUID, worker_id: str,
                   now: datetime, recovery_healing_id: UUID | None = None):
        if replay_policy not in {"SAFE", "FIXTURE_ONLY", "NEVER"}:
            raise ValueError("unsupported replay policy")
        with self.session_factory.begin() as session:
            run_case = session.get(RunCaseRecord, run_case_id)
            if run_case is None:
                return {"execute": False, "reason": "RUN_CASE_NOT_FOUND"}
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_case.run_id).with_for_update())
            if run is None or run.attempt_token != attempt_token or run.lease_owner != worker_id:
                raise StaleRunAttempt
            if run.status == "PAUSING":
                return {"execute": False, "reason": "RUN_PAUSING"}
            if run.status == "CANCELLING":
                return {"execute": False, "reason": "RUN_CANCELLING"}
            if run.status not in {"PREPARING", "RUNNING"}:
                raise StaleRunAttempt
            run_case = session.scalar(select(RunCaseRecord).where(
                RunCaseRecord.id == run_case_id).with_for_update())
            step = session.scalar(select(TestStepRecord).where(
                TestStepRecord.run_case_id == run_case_id,
                TestStepRecord.action_id == action_id,
            ).with_for_update())
            if step is not None and step.status == "PASSED":
                if step.attempt_token == attempt_token:
                    return {"execute": False, "reason": "STEP_ALREADY_PASSED", "step_id": step.id}
                if step.replay_policy != "SAFE":
                    previous_status = run.status
                    run.status = "WAITING_APPROVAL"
                    run.lease_owner = None
                    run.lease_expires_at = None
                    lease = session.scalar(select(WorkerLeaseRecord).where(
                        WorkerLeaseRecord.resource_type == "run",
                        WorkerLeaseRecord.resource_id == run.id,
                    ).with_for_update())
                    if lease is not None:
                        session.delete(lease)
                    approval = self._ensure_run_approval(
                        session, run, now=now,
                        request_context={"reason_code": "BROWSER_SESSION_NOT_RESTORABLE",
                                         "step_ids": [str(step.id)]},
                        actor_user_id=None,
                    )
                    self._append_event(session, run.id, "run.status_changed", {
                        "from_status": previous_status, "status": "WAITING_APPROVAL",
                        "reason_code": "BROWSER_SESSION_NOT_RESTORABLE",
                    }, now)
                    self._append_run_approval_events(session, run, approval, now=now)
                    return {"execute": False, "reason": "MANUAL_RECONCILIATION",
                            "approval_id": approval.id}
            if (step is not None and step.status == "RUNNING"
                    and step.replay_policy in {"NEVER", "FIXTURE_ONLY"}):
                previous_status = run.status
                run.status = "WAITING_APPROVAL"
                step.status = "WAITING_APPROVAL"
                step.finished_at = now
                run.lease_owner = None
                run.lease_expires_at = None
                lease = session.scalar(select(WorkerLeaseRecord).where(
                    WorkerLeaseRecord.resource_type == "run",
                    WorkerLeaseRecord.resource_id == run.id,
                ).with_for_update())
                if lease is not None:
                    session.delete(lease)
                approval = self._ensure_run_approval(
                    session, run, now=now,
                    request_context={"reason_code": "UNKNOWN_SIDE_EFFECT",
                                     "step_ids": [str(step.id)]},
                    actor_user_id=None,
                )
                self._append_event(session, run.id, "run.status_changed", {
                    "from_status": previous_status, "status": "WAITING_APPROVAL",
                    "reason_code": "UNKNOWN_SIDE_EFFECT",
                }, now)
                self._append_run_approval_events(session, run, approval, now=now)
                return {"execute": False, "reason": "MANUAL_RECONCILIATION",
                        "approval_id": approval.id}
            if step is None:
                step = TestStepRecord(
                    id=uuid4(), run_case_id=run_case_id, action_id=action_id,
                    sequence=sequence, action_type=action_type, locator=locator,
                    expected=expected, status="PENDING", replay_policy=replay_policy,
                    recovery_healing_id=recovery_healing_id,
                )
                session.add(step)
                session.flush()
            step.action_type = action_type
            step.locator = locator
            step.expected = expected
            step.replay_policy = replay_policy
            step.recovery_healing_id = recovery_healing_id
            step.status = "RUNNING"
            step.attempt_token = attempt_token
            step.started_at = now
            step.finished_at = None
            step.idempotency_key = f"{run_case_id}:{action_id}:{attempt_token}"
            self._append_event(session, run.id, "run.step_started", {
                "run_case_id": str(run_case_id), "step_id": str(step.id),
                "action_id": str(action_id), "sequence": sequence,
                "action_type": action_type,
                **({"recovery_healing_id": str(recovery_healing_id)}
                   if recovery_healing_id is not None else {}),
            }, now)
            return {"execute": True, "step_id": step.id}

    def finish_step(self, step_id: UUID, *, status: str, result: dict,
                    attempt_token: UUID, worker_id: str, now: datetime):
        if status not in {"PASSED", "FAILED", "ERROR"}:
            raise ValueError("unsupported step result")
        with self.session_factory.begin() as session:
            step = session.scalar(select(TestStepRecord).where(
                TestStepRecord.id == step_id).with_for_update())
            if step is None:
                return False
            run_case = session.get(RunCaseRecord, step.run_case_id)
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_case.run_id).with_for_update())
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id or step.attempt_token != attempt_token
                    or run.status not in {"PREPARING", "RUNNING", "PAUSING", "CANCELLING"}):
                raise StaleRunAttempt
            step.status = status
            step.finished_at = now
            self._append_event(session, run.id, "run.step_finished", {
                "run_case_id": str(run_case.id), "step_id": str(step.id),
                "action_id": str(step.action_id), "status": status,
                "result": result,
            }, now)
            return True

    def create_browser_session(self, run_case_id: UUID, *, browser: str,
                               attempt_token: UUID, worker_id: str, now: datetime,
                               lease_seconds: int = 90):
        with self.session_factory.begin() as session:
            run_case = session.get(RunCaseRecord, run_case_id)
            if run_case is None:
                raise StaleRunAttempt
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_case.run_id).with_for_update())
            if (run is None or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id or run.status not in {"PREPARING", "RUNNING", "PAUSING", "CANCELLING"}):
                raise StaleRunAttempt
            record = session.scalar(select(BrowserSessionRecord).where(
                BrowserSessionRecord.run_case_id == run_case_id,
                BrowserSessionRecord.browser == browser,
                BrowserSessionRecord.attempt_token == attempt_token,
            ).with_for_update())
            if record is not None:
                previous_worker = record.worker_instance
                record.status = "ALLOCATING"
                record.lease_expires_at = now + timedelta(seconds=lease_seconds)
                record.heartbeat_at = now
                return {"id": record.id, "existing": True,
                        "worker_instance": previous_worker}
            record = BrowserSessionRecord(
                id=uuid4(), run_case_id=run_case_id, browser=browser,
                worker_instance="pending", attempt_token=attempt_token,
                status="ALLOCATING", lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now, created_at=now,
            )
            session.add(record)
            session.flush()
            return {"id": record.id, "existing": False}

    def set_browser_session_status(self, session_id: UUID, status: str, *,
                                   attempt_token: UUID, worker_id: str,
                                   now: datetime, worker_instance: str | None = None):
        allowed = {"READY", "BUSY", "CLOSING", "CLOSED", "LOST"}
        if status not in allowed:
            raise ValueError("unsupported browser session status")
        with self.session_factory.begin() as session:
            record = session.scalar(select(BrowserSessionRecord).where(
                BrowserSessionRecord.id == session_id).with_for_update())
            if record is None or record.attempt_token != attempt_token:
                raise StaleRunAttempt
            run_case = session.get(RunCaseRecord, record.run_case_id)
            run = session.get(TestRunRecord, run_case.run_id) if run_case else None
            closing_after_release = (
                status in {"CLOSING", "CLOSED", "LOST"} and run is not None
                and run.lease_owner is None
                and run.status in TERMINAL_STATUSES | {"PAUSED", "WAITING_APPROVAL"}
            )
            if (run is None or run.attempt_token != attempt_token
                    or (run.lease_owner != worker_id and not closing_after_release)):
                raise StaleRunAttempt
            if worker_instance:
                record.worker_instance = worker_instance
            record.status = status
            record.heartbeat_at = now
            record.lease_expires_at = now + timedelta(seconds=90)
            if status in {"CLOSED", "LOST"}:
                record.closed_at = now
            return True

    def rebind_browser_session(self, session_id: UUID, run_case_id: UUID, *,
                               browser: str, previous_attempt_token: UUID,
                               attempt_token: UUID, worker_id: str,
                               worker_instance: str, now: datetime):
        """Rotate the stored session fence after the Gateway confirms Worker rebind."""
        if (not worker_instance or len(worker_instance) > 128
                or previous_attempt_token == attempt_token):
            raise ValueError("invalid browser session handoff scope")
        with self.session_factory.begin() as session:
            run_case = session.get(RunCaseRecord, run_case_id)
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_case.run_id if run_case else False,
            ).with_for_update())
            record = session.scalar(select(BrowserSessionRecord).where(
                BrowserSessionRecord.id == session_id,
            ).with_for_update())
            if (run_case is None or run is None or record is None
                    or run_case.run_id != run.id
                    or run_case.browser_name != browser
                    or run_case.attempt_token != attempt_token
                    or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id or run.status != "RUNNING"
                    or record.run_case_id != run_case_id or record.browser != browser
                    or record.attempt_token != previous_attempt_token
                    or record.worker_instance != worker_instance
                    or record.status not in {"READY", "BUSY"}):
                raise StaleRunAttempt
            record.attempt_token = attempt_token
            record.status = "READY"
            record.heartbeat_at = now
            record.lease_expires_at = now + timedelta(seconds=90)
            self._append_event(session, run.id, "run.browser_session_rebound", {
                "run_case_id": str(run_case_id), "session_id": str(session_id),
                "browser": browser, "worker_instance": worker_instance,
            }, now)
            return True
    def get_run(self, actor_id: UUID, run_id: UUID, *, for_write: bool = False):
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None
            self._visible_project(session, actor_id, run.project_id, for_write=for_write)
            result = self._run_dict(run)
            result["latest_event_id"] = session.scalar(select(func.max(
                AgentEventRecord.id
            )).where(AgentEventRecord.run_id == run_id)) or 0
            result["event_replay_floor"] = run.event_replay_floor
            return result

    def project_id_for_run(self, run_id: UUID):
        with self.session_factory() as session:
            return session.scalar(select(TestRunRecord.project_id).where(TestRunRecord.id == run_id))

    def propose_healing(self, run_id: UUID, run_case_id: UUID, failed_step_id: UUID, *,
                        old_locator: dict, proposal: RecoveryProposal | dict,
                        evidence: dict, requester_id: UUID, now: datetime) -> dict:
        """Persist one verified locator candidate and park its owning Run atomically."""
        candidate = (proposal if isinstance(proposal, RecoveryProposal)
                     else RecoveryProposal.model_validate(proposal))
        if not isinstance(old_locator, dict):
            raise ValueError("old locator is required")
        decision = evaluate_recovery_candidate(
            proposal=candidate,
            verification=evidence.get("verification") if isinstance(evidence, dict) else None,
        )
        if not decision.accepted or decision.proposal is None:
            raise RunApprovalConflict(f"Recovery candidate rejected: {decision.reason_code}")
        candidate = decision.proposal
        bounded_evidence = self._bounded_healing_evidence(evidence)
        bounded_evidence["source"] = candidate.source
        bounded_evidence["semantic_descriptor"] = self._bounded_healing_evidence({
            "descriptor": candidate.semantic_descriptor,
        }).get("descriptor", {})
        if len(json.dumps(bounded_evidence, ensure_ascii=False).encode("utf-8")) > HEALING_EVIDENCE_MAX_BYTES:
            raise ValueError("healing evidence exceeds its storage limit")

        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id,
            ).with_for_update())
            if run is None or run.requested_by != requester_id:
                raise RunApprovalConflict("Run is no longer eligible for Healing")
            if run.status == "WAITING_APPROVAL":
                pending = session.scalar(select(HealingRecord).join(
                    TestStepRecord, TestStepRecord.id == HealingRecord.step_id,
                ).where(
                    TestStepRecord.id == failed_step_id,
                    HealingRecord.status == "PENDING_APPROVAL",
                ).with_for_update())
                if pending is not None and pending.new_locator == candidate.locator:
                    approval = session.scalar(select(CaseApprovalRecord).where(
                        CaseApprovalRecord.target_kind == "healing",
                        CaseApprovalRecord.target_id == pending.id,
                        CaseApprovalRecord.project_id == run.project_id,
                    ).with_for_update())
                    if approval is not None:
                        return {"healing_id": pending.id, "approval_id": approval.id,
                                "expires_at": approval.expires_at}
                raise RunApprovalConflict("Run is no longer eligible for Healing")
            if run.status != "RUNNING":
                raise RunApprovalConflict("Run is no longer eligible for Healing")
            run_case = session.get(RunCaseRecord, run_case_id)
            if run_case is None or run_case.run_id != run.id or run_case.status != "RUNNING":
                raise RunScopeDenied
            step = session.get(TestStepRecord, failed_step_id)
            if (step is None or step.run_case_id != run_case.id
                    or step.action_id != candidate.action_id or step.locator != old_locator
                    or step.status not in {"FAILED", "ERROR"}):
                raise RunApprovalConflict("failed step no longer matches the Recovery proposal")

            existing = session.scalar(select(HealingRecord).where(
                HealingRecord.step_id == step.id,
                HealingRecord.status == "PENDING_APPROVAL",
            ).order_by(HealingRecord.created_at.desc()).with_for_update())
            if existing is not None:
                if existing.new_locator != candidate.locator:
                    raise RunApprovalConflict("a different Healing proposal is already pending")
                approval = session.scalar(select(CaseApprovalRecord).where(
                    CaseApprovalRecord.target_kind == "healing",
                    CaseApprovalRecord.target_id == existing.id,
                    CaseApprovalRecord.project_id == run.project_id,
                ).with_for_update())
                if approval is None:
                    raise RunApprovalConflict("pending Healing approval is missing")
                return {"healing_id": existing.id, "approval_id": approval.id,
                        "expires_at": approval.expires_at}

            prior_attempts = session.scalar(select(func.count()).select_from(HealingRecord).where(
                HealingRecord.step_id == step.id,
            )) or 0
            if prior_attempts >= 2:
                raise RunApprovalConflict("Healing candidate limit reached")

            healing_id = uuid4()
            expires_at = now + HEALING_APPROVAL_TTL
            healing = HealingRecord(
                id=healing_id, step_id=step.id, old_locator=old_locator,
                new_locator=candidate.locator, evidence=bounded_evidence,
                confidence=Decimal(str(candidate.confidence)),
                status="PENDING_APPROVAL", new_version_id=None,
                approved_by=None, approved_at=None, applied_at=None, created_at=now,
            )
            correlation_key = f"healing:{healing_id}"
            approval = CaseApprovalRecord(
                id=uuid4(), project_id=run.project_id,
                target_kind="healing", target_id=healing_id, run_id=None,
                case_version_id=None, requested_by=requester_id, decided_by=None,
                kind="HEALING", status="PENDING", reason=None, decided_at=None,
                expires_at=expires_at,
                payload={"step_id": str(step.id), "action_id": str(step.action_id)},
                correlation_key=correlation_key, created_at=now,
            )
            session.add_all([healing, approval])
            session.flush()

            previous_status = run.status
            run.status = "WAITING_APPROVAL"
            run.lease_owner = None
            run.lease_expires_at = None
            run.heartbeat_at = now
            run_case.status = "WAITING_APPROVAL"
            run_case.lease_owner = None
            run_case.lease_expires_at = None
            run_case.heartbeat_at = now
            step.status = "WAITING_APPROVAL"
            step.finished_at = now
            lease = session.scalar(select(WorkerLeaseRecord).where(
                WorkerLeaseRecord.resource_type == "run",
                WorkerLeaseRecord.resource_id == run.id,
            ).with_for_update())
            if lease is not None:
                session.delete(lease)

            self._audit_approval(
                session, approval, actor_user_id=requester_id,
                action="approval.requested", now=now, reason_code="HEALING_PROPOSED",
            )
            self._append_event(session, run.id, "approval.requested", {
                "approval_id": str(approval.id), "target_kind": "healing",
                "target_id": str(healing.id), "expires_at": expires_at.isoformat(),
            }, now)
            self._append_event(session, run.id, "run.healing_requested", {
                "healing_id": str(healing.id), "approval_id": str(approval.id),
                "run_case_id": str(run_case.id), "step_id": str(step.id),
                "action_id": str(step.action_id), "expires_at": expires_at.isoformat(),
                "status": healing.status,
            }, now)
            self._append_event(session, run.id, "run.status_changed", {
                "from_status": previous_status, "status": "WAITING_APPROVAL",
                "reason_code": "HEALING_PROPOSED",
            }, now)
            return {"healing_id": healing.id, "approval_id": approval.id,
                    "expires_at": expires_at}

    def project_id_for_healing(self, healing_id: UUID):
        with self.session_factory() as session:
            return session.scalar(
                select(TestRunRecord.project_id)
                .join(RunCaseRecord, RunCaseRecord.run_id == TestRunRecord.id)
                .join(TestStepRecord, TestStepRecord.run_case_id == RunCaseRecord.id)
                .join(HealingRecord, HealingRecord.step_id == TestStepRecord.id)
                .where(HealingRecord.id == healing_id)
            )

    def get_healing(self, actor_id: UUID, healing_id: UUID) -> dict | None:
        with self.session_factory() as session:
            healing = session.get(HealingRecord, healing_id)
            if healing is None:
                return None
            step = session.get(TestStepRecord, healing.step_id)
            run_case = session.get(RunCaseRecord, step.run_case_id) if step else None
            run = session.get(TestRunRecord, run_case.run_id) if run_case else None
            if step is None or run_case is None or run is None:
                return None
            self._visible_project(session, actor_id, run.project_id)
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.target_kind == "healing",
                CaseApprovalRecord.target_id == healing.id,
                CaseApprovalRecord.project_id == run.project_id,
            ))
            if approval is None:
                return None
            return self._healing_review_dict(healing, approval, run, run_case, step)

    def decide_healing(self, actor_id: UUID, healing_id: UUID, *,
                       decision: str, reason: str, now: datetime) -> dict:
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("unsupported Healing decision")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("a non-empty decision reason is required")
        with self.session_factory() as lookup:
            run_id = lookup.scalar(
                select(RunCaseRecord.run_id)
                .join(TestStepRecord, TestStepRecord.run_case_id == RunCaseRecord.id)
                .join(HealingRecord, HealingRecord.step_id == TestStepRecord.id)
                .where(HealingRecord.id == healing_id)
            )
        if run_id is None:
            raise RunScopeDenied

        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id,
            ).with_for_update())
            if run is None:
                raise RunScopeDenied
            self._visible_project(session, actor_id, run.project_id, for_write=True)
            healing = session.scalar(select(HealingRecord).where(
                HealingRecord.id == healing_id,
            ).with_for_update())
            if healing is None:
                raise RunScopeDenied
            step = session.get(TestStepRecord, healing.step_id)
            run_case = session.get(RunCaseRecord, step.run_case_id) if step else None
            if (step is None or run_case is None or run_case.run_id != run.id):
                raise RunScopeDenied
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.target_kind == "healing",
                CaseApprovalRecord.target_id == healing.id,
                CaseApprovalRecord.project_id == run.project_id,
            ).with_for_update())
            if approval is None:
                raise RunScopeDenied
            if (healing.status != "PENDING_APPROVAL" or approval.status != "PENDING"
                    or run.status != "WAITING_APPROVAL"
                    or run_case.status != "WAITING_APPROVAL"):
                raise RunApprovalConflict("Healing is no longer awaiting a decision")
            if approval.expires_at is None:
                raise RunApprovalConflict("Healing approval has no expiry")
            if self._timestamp_expired(approval.expires_at, now):
                self._finish_healing_decision(
                    session, run, run_case, step, healing, approval,
                    status="EXPIRED", actor_user_id=None, reason="HEALING_APPROVAL_EXPIRED",
                    now=now,
                )
                return {"healing_status": "EXPIRED", "approval_status": "EXPIRED",
                        "expired": True}
            if approval.requested_by == actor_id:
                raise RunApprovalPermissionDenied

            previous_status = run.status
            previous_attempt_token = run.attempt_token
            if decision == "REJECT":
                self._finish_healing_decision(
                    session, run, run_case, step, healing, approval,
                    status="REJECTED", actor_user_id=actor_id,
                    reason=reason.strip(), now=now,
                )
                return {"healing_status": "REJECTED", "approval_status": "REJECTED",
                        "expired": False}

            if previous_attempt_token is None:
                raise RunApprovalConflict("Healing Run has no fenced attempt")
            attempt_token = uuid4()
            healing.status = "APPROVED"
            healing.approved_by = actor_id
            healing.approved_at = now
            approval.status = "APPROVED"
            approval.decided_by = actor_id
            approval.reason = reason.strip()
            approval.decided_at = now
            approval.payload = {
                **(approval.payload if isinstance(approval.payload, dict) else {}),
                "previous_attempt_token": str(previous_attempt_token),
                "attempt_token": str(attempt_token),
            }
            run.status = "RUNNING"
            run.attempt_token = attempt_token
            run.lease_owner = None
            run.lease_expires_at = None
            run.heartbeat_at = now
            run.finished_at = None
            run_case.status = "RUNNING"
            run_case.attempt_token = attempt_token
            run_case.lease_owner = None
            run_case.lease_expires_at = None
            run_case.heartbeat_at = now
            browser_session = session.scalar(select(BrowserSessionRecord).where(
                BrowserSessionRecord.run_case_id == run_case.id,
                BrowserSessionRecord.browser == run_case.browser_name,
                BrowserSessionRecord.attempt_token == previous_attempt_token,
            ).order_by(BrowserSessionRecord.created_at.desc()))
            if browser_session is not None:
                approval.payload = {
                    **approval.payload,
                    "session_id": str(browser_session.id),
                    "worker_instance": browser_session.worker_instance,
                }

            self._audit_approval(
                session, approval, actor_user_id=actor_id, action="approval.decided",
                now=now, reason_code="HEALING_APPROVED",
            )
            self._append_event(session, run.id, "approval.decided", {
                "approval_id": str(approval.id), "target_kind": "healing",
                "target_id": str(healing.id), "status": "APPROVED",
                "decision_actor_id": str(actor_id),
            }, now)
            self._append_event(session, run.id, "run.healing_approved", {
                "healing_id": str(healing.id), "run_case_id": str(run_case.id),
                "step_id": str(step.id), "previous_attempt_token": str(previous_attempt_token),
                "attempt_token": str(attempt_token),
            }, now)
            self._append_event(session, run.id, "run.status_changed", {
                "from_status": previous_status, "status": "RUNNING",
                "reason_code": "HEALING_APPROVED",
            }, now)
            session.add(OutboxRecord(
                aggregate_type="run", aggregate_id=run.id,
                event_type="RUN_RESUME_REQUESTED",
                payload={"run_id": str(run.id), "healing_id": str(healing.id),
                         "previous_attempt_token": str(previous_attempt_token),
                         "attempt_token": str(attempt_token)},
                status="PENDING", attempts=0, available_at=now, created_at=now,
            ))
            return {"healing_status": "APPROVED", "approval_status": "APPROVED",
                    "expired": False, "previous_attempt_token": previous_attempt_token,
                    "attempt_token": attempt_token}

    def load_approved_healing(self, run_id: UUID, run_case_id: UUID,
                              attempt_token: UUID, *, worker_id: str) -> dict | None:
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            run_case = session.get(RunCaseRecord, run_case_id)
            if (run is None or run_case is None or run_case.run_id != run_id
                    or run.status != "RUNNING" or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id or run_case.attempt_token != attempt_token):
                return None
            approval_row = session.execute(
                select(HealingRecord, CaseApprovalRecord, TestStepRecord)
                .join(TestStepRecord, TestStepRecord.id == HealingRecord.step_id)
                .join(CaseApprovalRecord,
                      (CaseApprovalRecord.target_kind == "healing")
                      & (CaseApprovalRecord.target_id == HealingRecord.id)
                      & (CaseApprovalRecord.project_id == run.project_id))
                .where(TestStepRecord.run_case_id == run_case_id,
                       HealingRecord.status == "APPROVED",
                       CaseApprovalRecord.status == "APPROVED")
                .order_by(HealingRecord.approved_at.desc(), HealingRecord.id)
            ).first()
            if approval_row is None:
                return None
            healing, approval, step = approval_row
            payload = approval.payload if isinstance(approval.payload, dict) else {}
            attempt_matches = payload.get("attempt_token") == str(attempt_token)
            previous_attempt = payload.get("previous_attempt_token")
            try:
                previous_attempt_token = UUID(str(previous_attempt))
            except (TypeError, ValueError, AttributeError):
                previous_attempt_token = None
            browser_session = session.scalar(select(BrowserSessionRecord).where(
                BrowserSessionRecord.run_case_id == run_case_id,
                BrowserSessionRecord.browser == run_case.browser_name,
                BrowserSessionRecord.attempt_token == previous_attempt_token
                if previous_attempt_token is not None else False,
            ).order_by(BrowserSessionRecord.created_at.desc()))
            if browser_session is None and payload.get("session_id"):
                try:
                    payload_session_id = UUID(str(payload["session_id"]))
                except (TypeError, ValueError, AttributeError):
                    payload_session_id = None
                if payload_session_id is not None:
                    candidate_session = session.get(BrowserSessionRecord, payload_session_id)
                    if (candidate_session is not None
                            and candidate_session.run_case_id == run_case_id
                            and candidate_session.browser == run_case.browser_name):
                        browser_session = candidate_session
            healing_ids = list(session.scalars(select(HealingRecord.id).where(
                HealingRecord.step_id == step.id,
            ).order_by(HealingRecord.created_at, HealingRecord.id)))
            candidate_number = healing_ids.index(healing.id) + 1
            return {
                "healing_id": healing.id,
                "approval_id": approval.id,
                "run_id": run.id,
                "run_case_id": run_case.id,
                "step_id": step.id,
                "action_id": step.action_id,
                "sequence": step.sequence,
                "browser": run_case.browser_name,
                "old_locator": healing.old_locator,
                "new_locator": healing.new_locator,
                "evidence": healing.evidence,
                "previous_attempt_token": previous_attempt_token,
                "attempt_token": attempt_token,
                "session_id": browser_session.id if browser_session else None,
                "worker_instance": browser_session.worker_instance if browser_session else None,
                "session_status": browser_session.status if browser_session else "MISSING",
                "candidate_number": candidate_number,
                "attempt_matches": attempt_matches,
            }

    def complete_healing(self, healing_id: UUID, run_id: UUID, run_case_id: UUID,
                         attempt_token: UUID, *, worker_id: str, now: datetime) -> dict:
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id,
            ).with_for_update())
            if run is None:
                raise RunScopeDenied
            healing = session.scalar(select(HealingRecord).where(
                HealingRecord.id == healing_id,
            ).with_for_update())
            if healing is None:
                raise RunScopeDenied
            step = session.get(TestStepRecord, healing.step_id)
            run_case = session.get(RunCaseRecord, run_case_id)
            if (step is None or run_case is None or step.run_case_id != run_case_id
                    or run_case.run_id != run.id or run.id != run_id
                    or run.status != "RUNNING" or run.attempt_token != attempt_token
                    or run.lease_owner != worker_id or run_case.attempt_token != attempt_token
                    or healing.status != "APPROVED"):
                raise StaleRunAttempt
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.target_kind == "healing",
                CaseApprovalRecord.target_id == healing.id,
                CaseApprovalRecord.project_id == run.project_id,
            ).with_for_update())
            if (approval is None or approval.status != "APPROVED"
                    or (approval.payload or {}).get("attempt_token") != str(attempt_token)):
                raise RunApprovalConflict("approved Healing no longer matches the active attempt")
            version = self.cases.create_recovered_version_in_session(
                session, source_version_id=run_case.case_version_id,
                action_id=step.action_id, new_locator=healing.new_locator,
                version_author_id=run.requested_by, now=now,
            )
            healing.new_version_id = version.id
            healing.status = "APPLIED"
            healing.applied_at = now
            self._append_event(session, run.id, "healing.applied", {
                "healing_id": str(healing.id), "run_case_id": str(run_case.id),
                "new_version_id": str(version.id), "status": "APPLIED",
            }, now)
            return {"healing_id": healing.id, "new_version_id": version.id,
                    "attempt_token": attempt_token, "version_status": version.review_status}

    def cancel_healing(self, healing_id: UUID, run_id: UUID, run_case_id: UUID,
                       attempt_token: UUID, *, worker_id: str, reason: str,
                       continue_retry: bool, now: datetime,
                       manual_reconciliation: bool = False) -> dict:
        """Cancel an unusable candidate and optionally establish the next candidate's base locator."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 160:
            raise ValueError("a bounded Healing cancellation reason is required")
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id,
            ).with_for_update())
            healing = session.scalar(select(HealingRecord).where(
                HealingRecord.id == healing_id,
            ).with_for_update())
            if run is None or healing is None:
                raise RunScopeDenied
            step = session.get(TestStepRecord, healing.step_id)
            run_case = session.get(RunCaseRecord, run_case_id)
            if (step is None or run_case is None or step.run_case_id != run_case_id
                    or run_case.run_id != run_id or run.id != run_id
                    or run.status not in {"PREPARING", "RUNNING", "PAUSING", "CANCELLING"}
                    or run.attempt_token != attempt_token or run.lease_owner != worker_id
                    or run_case.attempt_token != attempt_token):
                raise StaleRunAttempt
            if healing.status == "CANCELLED":
                return {"healing_id": healing.id, "status": healing.status}
            if healing.status not in {"APPROVED", "PENDING_APPROVAL"}:
                raise RunApprovalConflict("Healing is no longer active")
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.target_kind == "healing",
                CaseApprovalRecord.target_id == healing.id,
                CaseApprovalRecord.project_id == run.project_id,
            ).with_for_update())
            if approval is None:
                raise RunScopeDenied
            healing.status = "CANCELLED"
            if approval.status in {"PENDING", "APPROVED"}:
                approval.status = "CANCELLED"
                approval.decided_at = now
                approval.reason = reason.strip()
            step.status = "FAILED"
            step.finished_at = now
            if continue_retry:
                step.locator = healing.new_locator
            self._audit_approval(
                session, approval, actor_user_id=None,
                action="approval.cancelled", now=now,
                reason_code=reason.strip(),
            )
            self._append_event(session, run.id, "run.healing_cancelled", {
                "healing_id": str(healing.id), "run_case_id": str(run_case.id),
                "step_id": str(step.id), "reason_code": reason.strip(),
                "continue_retry": continue_retry,
            }, now)
            if manual_reconciliation:
                self._append_event(session, run.id, "run.manual_reconciliation_required", {
                    "healing_id": str(healing.id),
                    "run_case_id": str(run_case.id), "step_id": str(step.id),
                    "reason_code": reason.strip(),
                }, now)
            return {"healing_id": healing.id, "status": healing.status,
                    "manual_reconciliation": manual_reconciliation}

    def expire_healing_approval(self, approval_id: UUID, *, now: datetime) -> bool:
        with self.session_factory() as lookup:
            run_id = lookup.scalar(
                select(RunCaseRecord.run_id)
                .join(TestStepRecord, TestStepRecord.run_case_id == RunCaseRecord.id)
                .join(HealingRecord, HealingRecord.step_id == TestStepRecord.id)
                .join(CaseApprovalRecord,
                      (CaseApprovalRecord.target_kind == "healing")
                      & (CaseApprovalRecord.target_id == HealingRecord.id))
                .where(CaseApprovalRecord.id == approval_id)
            )
        if run_id is None:
            return False
        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id,
            ).with_for_update())
            # Lock Healing after Run, then its Approval, matching decision lock order.
            healing_id = session.scalar(select(CaseApprovalRecord.target_id).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "healing",
            ))
            healing = session.scalar(select(HealingRecord).where(
                HealingRecord.id == healing_id,
            ).with_for_update()) if healing_id else None
            if run is None or healing is None:
                return False
            step = session.get(TestStepRecord, healing.step_id)
            run_case = session.get(RunCaseRecord, step.run_case_id) if step else None
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "healing",
                CaseApprovalRecord.target_id == healing.id,
                CaseApprovalRecord.project_id == run.project_id,
            ).with_for_update())
            if (step is None or run_case is None or approval is None
                    or healing.status != "PENDING_APPROVAL" or approval.status != "PENDING"
                    or approval.expires_at is None
                    or not self._timestamp_expired(approval.expires_at, now)):
                return False
            self._finish_healing_decision(
                session, run, run_case, step, healing, approval,
                status="EXPIRED", actor_user_id=None,
                reason="HEALING_APPROVAL_EXPIRED", now=now,
            )
            return True

    def expire_due_healing_approvals(self, *, now: datetime, limit: int = 100) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("Healing approval expiry batch must be between 1 and 500")
        with self.session_factory() as session:
            approval_ids = list(session.scalars(
                select(CaseApprovalRecord.id)
                .join(HealingRecord,
                      (CaseApprovalRecord.target_kind == "healing")
                      & (CaseApprovalRecord.target_id == HealingRecord.id))
                .where(CaseApprovalRecord.status == "PENDING",
                       CaseApprovalRecord.expires_at <= now,
                       HealingRecord.status == "PENDING_APPROVAL")
                .order_by(CaseApprovalRecord.expires_at, CaseApprovalRecord.id)
                .limit(limit)
            ).all())
        return sum(self.expire_healing_approval(item, now=now) for item in approval_ids)

    @staticmethod
    def _bounded_healing_evidence(value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("healing evidence must be an object")
        excluded = {
            "password", "cookie", "secret", "authorization", "access_token",
            "refresh_token", "job_token", "token", "html", "raw_html",
            "page_content", "screenshot", "screenshot_bytes", "image_data",
            "form_values", "input_value", "text_value", "value", "text",
        }

        def clean(item, *, depth=0):
            if depth > 8:
                return None
            if isinstance(item, dict):
                result = {}
                for key, child in list(item.items())[:100]:
                    if not isinstance(key, str) or key.casefold() in excluded:
                        continue
                    cleaned = clean(child, depth=depth + 1)
                    if cleaned is not None:
                        result[key[:80]] = cleaned
                return result
            if isinstance(item, list):
                return [clean(child, depth=depth + 1) for child in item[:100]]
            if isinstance(item, str):
                return item[:512]
            if item is None or type(item) in {bool, int, float}:
                return item
            raise ValueError("healing evidence contains an unsupported value")

        cleaned = clean(value)
        encoded = json.dumps(cleaned, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > HEALING_EVIDENCE_MAX_BYTES:
            raise ValueError("healing evidence exceeds its storage limit")
        return cleaned

    @staticmethod
    def _healing_review_dict(healing, approval, run, run_case, step):
        evidence = healing.evidence if isinstance(healing.evidence, dict) else {}
        return {
            "id": healing.id,
            "run_id": run.id,
            "run_case_id": run_case.id,
            "step_id": step.id,
            "action_id": step.action_id,
            "sequence": step.sequence,
            "action_type": step.action_type,
            "browser": run_case.browser_name,
            "old_locator": healing.old_locator,
            "new_locator": healing.new_locator,
            "source": evidence.get("source"),
            "confidence": float(healing.confidence),
            "evidence": evidence,
            "status": healing.status,
            "approval": {
                "id": approval.id,
                "status": approval.status,
                "requested_by": approval.requested_by,
                "decided_by": approval.decided_by,
                "reason": approval.reason,
                "expires_at": approval.expires_at,
            },
            "expires_at": approval.expires_at,
            "new_version_id": healing.new_version_id,
            "applied_at": healing.applied_at,
            "created_at": healing.created_at,
        }

    def _finish_healing_decision(self, session, run, run_case, step, healing, approval,
                                 *, status, actor_user_id, reason, now):
        if status not in {"REJECTED", "EXPIRED"}:
            raise ValueError("unsupported terminal Healing decision")
        healing.status = status
        approval.status = status
        approval.decided_by = actor_user_id
        approval.reason = reason
        approval.decided_at = now
        self._audit_approval(
            session, approval, actor_user_id=actor_user_id,
            action="approval.expired" if status == "EXPIRED" else "approval.decided",
            now=now, reason_code=f"HEALING_{status}",
        )
        self._append_event(session, run.id, "approval.decided", {
            "approval_id": str(approval.id), "target_kind": "healing",
            "target_id": str(healing.id), "status": status,
            "reason_code": f"HEALING_{status}",
        }, now)
        self._append_event(session, run.id, "run.healing_decided", {
            "healing_id": str(healing.id), "run_case_id": str(run_case.id),
            "step_id": str(step.id), "status": status,
        }, now)
        self._finish_run_for_approval(
            session, run, approval, now=now,
            reason_code=f"HEALING_{status}",
        )

    def list_approvals(self, actor_id: UUID, run_id: UUID, *, limit: int = 30,
                       cursor: str | None = None):
        key = self._decode_cursor(cursor) if cursor is not None else None
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None
            self._visible_project(session, actor_id, run.project_id)
            statement = select(CaseApprovalRecord).where(
                CaseApprovalRecord.project_id == run.project_id,
                CaseApprovalRecord.target_kind == "run",
                CaseApprovalRecord.target_id == run_id,
                CaseApprovalRecord.run_id == run_id,
            )
            if key is not None:
                created_at, approval_id = key
                statement = statement.where(or_(
                    CaseApprovalRecord.created_at < created_at,
                    and_(CaseApprovalRecord.created_at == created_at,
                         CaseApprovalRecord.id < approval_id),
                ))
            rows = session.scalars(statement.order_by(
                CaseApprovalRecord.created_at.desc(), CaseApprovalRecord.id.desc()
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            return {
                "items": [self._approval_dict(row) for row in items],
                "next_cursor": self._encode_cursor(items[-1]) if has_more and items else None,
            }

    def approval_context(self, approval_id: UUID):
        with self.session_factory() as session:
            approval = session.get(CaseApprovalRecord, approval_id)
            if approval is None:
                return None
            return {
                "id": approval.id,
                "project_id": approval.project_id,
                "target_kind": approval.target_kind,
                "target_id": approval.target_id,
            }

    def authorize_approval_project(self, actor_id: UUID, project_id: UUID):
        with self.session_factory() as session:
            self._visible_project(session, actor_id, project_id, for_write=True)

    def decide_run_approval(self, actor_id: UUID, approval_id: UUID, *,
                            decision: str, reason: str | None, now: datetime):
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("unsupported approval decision")
        with self.session_factory() as lookup:
            run_id = lookup.scalar(select(CaseApprovalRecord.run_id).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "run",
            ))
        if run_id is None:
            raise RunScopeDenied

        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            if run is None:
                raise RunScopeDenied
            self._visible_project(session, actor_id, run.project_id, for_write=True)
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "run",
                CaseApprovalRecord.target_id == run.id,
                CaseApprovalRecord.run_id == run.id,
                CaseApprovalRecord.project_id == run.project_id,
            ).with_for_update())
            if approval is None:
                raise RunScopeDenied
            if approval.status != "PENDING":
                raise RunApprovalConflict("approval is no longer pending")
            if run.status != "WAITING_APPROVAL":
                self._cancel_pending_run_approvals(
                    session, run, now=now, actor_user_id=actor_id,
                    reason_code="RUN_NOT_WAITING_FOR_APPROVAL",
                )
                return {"conflict": True}
            if approval.requested_by == actor_id:
                raise RunApprovalPermissionDenied
            if approval.expires_at is None:
                raise RunApprovalConflict("Run approval has no expiry")
            if self._timestamp_expired(approval.expires_at, now):
                approval.status = "EXPIRED"
                approval.decided_at = now
                approval.reason = "APPROVAL_EXPIRED"
                self._finish_run_for_approval(
                    session, run, approval, now=now, reason_code="APPROVAL_EXPIRED",
                )
                self._audit_approval(
                    session, approval, actor_user_id=None, action="approval.expired",
                    now=now, reason_code="APPROVAL_EXPIRED",
                )
                self._append_event(session, run.id, "approval.decided", {
                    "approval_id": str(approval.id), "status": "EXPIRED",
                    "reason_code": "APPROVAL_EXPIRED",
                }, now)
                return {"expired": True, "approval": self._approval_dict(approval)}

            approval.status = decision
            approval.decided_by = actor_id
            approval.reason = reason
            approval.decided_at = now
            self._audit_approval(
                session, approval, actor_user_id=actor_id, action="approval.decided",
                now=now, reason_code="APPROVAL_" + decision,
            )
            self._append_event(session, run.id, "approval.decided", {
                "approval_id": str(approval.id), "status": decision,
                "decision_actor_id": str(actor_id),
            }, now)

            if decision == "REJECTED":
                self._finish_run_for_approval(
                    session, run, approval, now=now, reason_code="APPROVAL_REJECTED",
                )
                return {"expired": False, "approval": self._approval_dict(approval)}

            previous_status = run.status
            attempt_token = uuid4()
            run.status = "RUNNING"
            run.attempt_token = attempt_token
            run.lease_owner = None
            run.lease_expires_at = None
            run.heartbeat_at = now
            run.finished_at = None
            self._mark_approved_manual_reconciliation(
                session, run, approval, attempt_token=attempt_token, now=now,
            )
            self._append_event(session, run.id, "run.status_changed", {
                "from_status": previous_status, "status": "RUNNING",
                "approval_id": str(approval.id), "reason_code": "APPROVAL_APPROVED",
            }, now)
            session.add(OutboxRecord(
                aggregate_type="run", aggregate_id=run.id,
                event_type="RUN_RESUME_REQUESTED",
                payload={"run_id": str(run.id), "approval_id": str(approval.id)},
                status="PENDING", attempts=0, available_at=now, created_at=now,
            ))
            return {"expired": False, "approval": self._approval_dict(approval)}

    def expire_run_approval(self, approval_id: UUID, *, now: datetime):
        with self.session_factory() as lookup:
            run_id = lookup.scalar(select(CaseApprovalRecord.run_id).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "run",
            ))
        if run_id is None:
            return {"expired": False, "reason": "APPROVAL_NOT_FOUND"}

        with self.session_factory.begin() as session:
            run = session.scalar(select(TestRunRecord).where(
                TestRunRecord.id == run_id).with_for_update())
            approval = session.scalar(select(CaseApprovalRecord).where(
                CaseApprovalRecord.id == approval_id,
                CaseApprovalRecord.target_kind == "run",
                CaseApprovalRecord.target_id == run_id,
                CaseApprovalRecord.run_id == run_id,
            ).with_for_update())
            if run is None or approval is None or approval.status != "PENDING":
                return {"expired": False, "reason": "APPROVAL_NOT_PENDING"}
            if run.status != "WAITING_APPROVAL":
                self._cancel_pending_run_approvals(
                    session, run, now=now, actor_user_id=None,
                    reason_code="RUN_NOT_WAITING_FOR_APPROVAL",
                )
                return {"expired": False, "cancelled": True}
            if approval.expires_at is None or not self._timestamp_expired(approval.expires_at, now):
                return {"expired": False, "reason": "APPROVAL_NOT_DUE"}

            approval.status = "EXPIRED"
            approval.decided_at = now
            approval.reason = "APPROVAL_EXPIRED"
            self._finish_run_for_approval(
                session, run, approval, now=now, reason_code="APPROVAL_EXPIRED",
            )
            self._audit_approval(
                session, approval, actor_user_id=None, action="approval.expired",
                now=now, reason_code="APPROVAL_EXPIRED",
            )
            self._append_event(session, run.id, "approval.decided", {
                "approval_id": str(approval.id), "status": "EXPIRED",
                "reason_code": "APPROVAL_EXPIRED",
            }, now)
            return {"expired": True, "approval": self._approval_dict(approval)}

    def expire_due_run_approvals(self, *, now: datetime, limit: int = 100):
        if not 1 <= limit <= 500:
            raise ValueError("approval expiry batch must be between 1 and 500")
        with self.session_factory() as session:
            approval_ids = list(session.scalars(select(CaseApprovalRecord.id).where(
                CaseApprovalRecord.target_kind == "run",
                CaseApprovalRecord.status == "PENDING",
                CaseApprovalRecord.expires_at <= now,
            ).order_by(CaseApprovalRecord.expires_at, CaseApprovalRecord.id).limit(limit)).all())
        outcomes = [self.expire_run_approval(approval_id, now=now) for approval_id in approval_ids]
        return {
            "scanned": len(approval_ids),
            "expired": sum(bool(item.get("expired")) for item in outcomes),
            "cancelled": sum(bool(item.get("cancelled")) for item in outcomes),
        }

    def list_runs(self, actor_id: UUID, project_id: UUID, *, limit: int = 30,
                  cursor: str | None = None):
        key = self._decode_cursor(cursor) if cursor else None
        with self.session_factory() as session:
            self._visible_project(session, actor_id, project_id)
            statement = select(TestRunRecord).where(TestRunRecord.project_id == project_id)
            if key:
                created_at, run_id = key
                statement = statement.where(or_(
                    TestRunRecord.created_at < created_at,
                    and_(TestRunRecord.created_at == created_at, TestRunRecord.id < run_id),
                ))
            rows = session.scalars(statement.order_by(
                TestRunRecord.created_at.desc(), TestRunRecord.id.desc()
            ).limit(limit + 1)).all()
            items = rows[:limit]
            return {"items": [self._run_dict(row) for row in items],
                    "next_cursor": self._encode_cursor(items[-1]) if len(rows) > limit and items else None}

    def list_steps(self, actor_id: UUID, run_id: UUID):
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None
            self._visible_project(session, actor_id, run.project_id)
            rows = session.execute(
                select(TestStepRecord, RunCaseRecord.browser_name, RunCaseRecord.attempt_token)
                .join(RunCaseRecord, RunCaseRecord.id == TestStepRecord.run_case_id)
                .where(RunCaseRecord.run_id == run_id)
                .order_by(RunCaseRecord.browser_name, TestStepRecord.sequence, TestStepRecord.id)
            ).all()
            return {"items": [{
                "id": step.id, "run_case_id": step.run_case_id, "action_id": step.action_id,
                "sequence": step.sequence, "action_type": step.action_type,
                "status": step.status, "replay_policy": step.replay_policy,
                "browser": browser, "attempt_token": str(token) if token else None,
                "started_at": step.started_at, "finished_at": step.finished_at,
            } for step, browser, token in rows]}

    def list_events(self, actor_id: UUID, run_id: UUID, *, after_id: int = 0,
                    limit: int = 500):
        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None
            self._visible_project(session, actor_id, run.project_id)
            if after_id < run.event_replay_floor:
                raise EventCursorExpired
            rows = session.scalars(select(AgentEventRecord).where(
                AgentEventRecord.run_id == run_id,
                AgentEventRecord.id > after_id,
            ).order_by(AgentEventRecord.id).limit(limit + 1)).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "items": [self._event_dict(item) for item in page],
                "next_cursor": page[-1].id if has_more and page else None,
                "latest_id": page[-1].id if page else after_id,
            }

    def issue_sse_ticket(self, actor_id: UUID, run_id: UUID, *, now: datetime,
                         ttl_seconds: int = 60):
        token = secrets.token_urlsafe(32)
        with self.session_factory.begin() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None
            self._visible_project(session, actor_id, run.project_id)
            session.add(SseTicketRecord(
                id=uuid4(), run_id=run_id, user_id=actor_id,
                token_hash=sha256(token.encode("utf-8")).hexdigest(),
                expires_at=now + timedelta(seconds=ttl_seconds), created_at=now,
            ))
        return token

    def consume_sse_ticket(self, run_id: UUID, token: str, *, now: datetime):
        if not token:
            return None
        digest = sha256(token.encode("utf-8")).hexdigest()
        with self.session_factory.begin() as session:
            ticket = session.scalar(select(SseTicketRecord).where(
                SseTicketRecord.run_id == run_id,
                SseTicketRecord.token_hash == digest,
            ).with_for_update())
            if ticket is None or ticket.used_at is not None or ticket.expires_at <= now:
                return None
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None
            try:
                self._visible_project(session, ticket.user_id, run.project_id)
            except RunScopeDenied:
                return None
            ticket.used_at = now
            return ticket.user_id

    @staticmethod
    def _timestamp_expired(expires_at: datetime, now: datetime) -> bool:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return expires_at <= now

    def _ensure_run_approval(self, session, run, *, now, request_context, actor_user_id):
        approval = session.scalar(select(CaseApprovalRecord).where(
            CaseApprovalRecord.target_kind == "run",
            CaseApprovalRecord.target_id == run.id,
            CaseApprovalRecord.run_id == run.id,
            CaseApprovalRecord.status == "PENDING",
        ).with_for_update())
        if approval is not None:
            return approval

        context = request_context if isinstance(request_context, dict) else {}
        raw_reason_code = context.get("reason_code")
        reason_code = raw_reason_code if isinstance(raw_reason_code, str) else "MANUAL_RECONCILIATION"
        reason_code = "".join(char for char in reason_code.upper() if char.isalnum() or char == "_")[:100]
        if not reason_code:
            reason_code = "MANUAL_RECONCILIATION"
        raw_step_ids = list(context.get("step_ids") or [])
        if context.get("step_id") is not None:
            raw_step_ids.append(context["step_id"])
        step_ids = []
        for raw_id in raw_step_ids:
            try:
                normalized_id = str(UUID(str(raw_id)))
            except (TypeError, ValueError, AttributeError):
                continue
            if normalized_id not in step_ids:
                step_ids.append(normalized_id)

        expires_at = now + RUN_APPROVAL_TTL
        approval = CaseApprovalRecord(
            id=uuid4(), project_id=run.project_id,
            target_kind="run", target_id=run.id, run_id=run.id,
            case_version_id=None, requested_by=run.requested_by or actor_user_id,
            decided_by=None, kind="RUN_MANUAL_RECONCILIATION", status="PENDING",
            reason=None, decided_at=None, expires_at=expires_at,
            payload={"reason_code": reason_code, "step_ids": step_ids,
                     "resume_mode": "manual_reconciliation"},
            created_at=now,
        )
        session.add(approval)
        session.flush()
        self._audit_approval(
            session, approval, actor_user_id=approval.requested_by,
            action="approval.requested", now=now, reason_code=reason_code,
        )
        return approval

    def _append_run_approval_events(self, session, run, approval, *, now):
        context = approval.payload if isinstance(approval.payload, dict) else {}
        expires_at = approval.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_text = (expires_at.astimezone(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z") if expires_at else None)
        common = {
            "approval_id": str(approval.id), "kind": approval.kind,
            "expires_at": expires_text,
        }
        self._append_event(session, run.id, "approval.requested", common, now)
        self._append_event(session, run.id, "run.waiting_approval", {
            "run_id": str(run.id), **common,
            "reason_code": context.get("reason_code", "MANUAL_RECONCILIATION"),
            "required_role": "project_admin_or_tester",
            "step_ids": context.get("step_ids", []),
        }, now)

    def _cancel_pending_run_approvals(self, session, run, *, now, actor_user_id, reason_code):
        approvals = session.scalars(select(CaseApprovalRecord).where(
            CaseApprovalRecord.target_kind == "run",
            CaseApprovalRecord.target_id == run.id,
            CaseApprovalRecord.run_id == run.id,
            CaseApprovalRecord.status == "PENDING",
        ).order_by(CaseApprovalRecord.id).with_for_update()).all()
        for approval in approvals:
            approval.status = "CANCELLED"
            approval.decided_by = None
            approval.reason = reason_code
            approval.decided_at = now
            self._audit_approval(
                session, approval, actor_user_id=actor_user_id,
                action="approval.cancelled", now=now, reason_code=reason_code,
            )
            self._append_event(session, run.id, "approval.decided", {
                "approval_id": str(approval.id), "status": "CANCELLED",
                "reason_code": reason_code,
            }, now)
            step_ids = self._approval_step_ids(approval)
            if step_ids:
                steps = session.scalars(select(TestStepRecord).where(
                    TestStepRecord.id.in_(step_ids),
                    TestStepRecord.status == "WAITING_APPROVAL",
                ).with_for_update()).all()
                for step in steps:
                    step.status = "CANCELLED" if reason_code == "RUN_CANCELLED" else "ERROR"
                    step.finished_at = now

    def _cancel_unfinished_run_work(self, session, run_id, *, now):
        run_cases = session.scalars(select(RunCaseRecord).where(
            RunCaseRecord.run_id == run_id,
            RunCaseRecord.status.in_(("QUEUED", "RUNNING", "WAITING_APPROVAL")),
        ).with_for_update()).all()
        for run_case in run_cases:
            run_case.status = "CANCELLED"
            run_case.finished_at = now
        steps = session.scalars(select(TestStepRecord).join(
            RunCaseRecord, RunCaseRecord.id == TestStepRecord.run_case_id,
        ).where(
            RunCaseRecord.run_id == run_id,
            TestStepRecord.status.in_(("PENDING", "RUNNING", "WAITING_APPROVAL")),
        ).with_for_update()).all()
        for step in steps:
            step.status = "SKIPPED" if step.status == "PENDING" else "CANCELLED"
            step.finished_at = now

    def _mark_approved_manual_reconciliation(self, session, run, approval, *, attempt_token, now):
        step_ids = self._approval_step_ids(approval)
        if not step_ids:
            return
        steps = session.scalars(select(TestStepRecord).join(
            RunCaseRecord, RunCaseRecord.id == TestStepRecord.run_case_id,
        ).where(
            RunCaseRecord.run_id == run.id,
            TestStepRecord.id.in_(step_ids),
        ).order_by(TestStepRecord.id).with_for_update()).all()
        found = {str(step.id) for step in steps}
        if found != set(step_ids) or any(step.status not in {"WAITING_APPROVAL", "PASSED"} for step in steps):
            raise RunApprovalConflict("manual reconciliation steps changed")
        for step in steps:
            if step.status == "WAITING_APPROVAL":
                step.status = "PASSED"
                step.finished_at = now
            step.attempt_token = attempt_token
            self._append_event(session, run.id, "run.step_reconciled", {
                "step_id": str(step.id), "approval_id": str(approval.id),
                "resolution": "APPROVED_AS_COMPLETED",
            }, now)

    def _finish_run_for_approval(self, session, run, approval, *, now, reason_code):
        run.status = "FAILED"
        run.finished_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = now
        lease = session.scalar(select(WorkerLeaseRecord).where(
            WorkerLeaseRecord.resource_type == "run",
            WorkerLeaseRecord.resource_id == run.id,
        ).with_for_update())
        if lease is not None:
            session.delete(lease)
        steps = session.scalars(select(TestStepRecord).join(
            RunCaseRecord, RunCaseRecord.id == TestStepRecord.run_case_id,
        ).where(
            RunCaseRecord.run_id == run.id,
            TestStepRecord.status.in_(("PENDING", "RUNNING", "WAITING_APPROVAL")),
        ).with_for_update()).all()
        for step in steps:
            step.status = "SKIPPED" if step.status == "PENDING" else "FAILED"
            step.finished_at = now
        run_cases = session.scalars(select(RunCaseRecord).where(
            RunCaseRecord.run_id == run.id,
            RunCaseRecord.status.in_(("QUEUED", "RUNNING", "WAITING_APPROVAL")),
        ).with_for_update()).all()
        for run_case in run_cases:
            run_case.status = "FAILED" if run_case.status in {"RUNNING", "WAITING_APPROVAL"} else "SKIPPED"
            run_case.finished_at = now
        self._append_event(session, run.id, "run.finished", {
            "from_status": "WAITING_APPROVAL", "status": "FAILED",
            "approval_id": str(approval.id), "reason_code": reason_code,
        }, now)

    def _audit_approval(self, session, approval, *, actor_user_id, action, now, reason_code):
        project = session.get(ProjectRecord, approval.project_id)
        if project is None:
            raise RunScopeDenied
        append_audit_entry(
            session, organization_id=project.organization_id,
            actor_user_id=actor_user_id, action=action,
            resource_type=approval.target_kind, resource_id=approval.target_id,
            metadata={"approval_id": str(approval.id),
                      "target_kind": approval.target_kind,
                      "target_id": str(approval.target_id),
                      "status": approval.status, "reason_code": reason_code},
            created_at=now,
        )

    @staticmethod
    def _approval_step_ids(approval):
        payload = approval.payload if isinstance(approval.payload, dict) else {}
        values = payload.get("step_ids", [])
        if not isinstance(values, list):
            return []
        normalized = []
        for value in values:
            try:
                step_id = str(UUID(str(value)))
            except (TypeError, ValueError, AttributeError):
                continue
            if step_id not in normalized:
                normalized.append(step_id)
        return normalized

    def _visible_project(self, session, actor_id, project_id, *, for_write=False):
        row = session.execute(
            select(ProjectRecord, OrganizationMembershipRecord.role,
                   ProjectMembershipRecord.role)
            .join(OrganizationMembershipRecord,
                  OrganizationMembershipRecord.organization_id == ProjectRecord.organization_id)
            .outerjoin(ProjectMembershipRecord,
                       (ProjectMembershipRecord.project_id == ProjectRecord.id)
                       & (ProjectMembershipRecord.user_id == actor_id))
            .where(
                ProjectRecord.id == project_id,
                OrganizationMembershipRecord.user_id == actor_id,
                (OrganizationMembershipRecord.role == "org_admin")
                | ProjectMembershipRecord.user_id.is_not(None),
            )
        ).first()
        if row is None:
            raise RunScopeDenied
        project, org_role, project_role = row
        if for_write and org_role != "org_admin" and project_role not in {"project_admin", "tester"}:
            raise RunScopeDenied
        return project

    @staticmethod
    def _append_event(session, run_id, event_type, payload, now):
        session.add(AgentEventRecord(
            run_id=run_id, event_type=event_type,
            payload=RunRepository._sanitize_payload(payload or {}),
            created_at=now,
        ))

    @staticmethod
    def _sanitize_payload(value):
        if isinstance(value, dict):
            return {key: RunRepository._sanitize_payload(item)
                    for key, item in value.items()
                    if key.casefold() not in SENSITIVE_EVENT_KEYS}
        if isinstance(value, list):
            return [RunRepository._sanitize_payload(item) for item in value]
        if isinstance(value, str):
            return value[:2000]
        return value

    @staticmethod
    def _run_dict(row):
        return {
            "id": row.id, "project_id": row.project_id, "environment_id": row.environment_id,
            "environment_snapshot_id": row.environment_snapshot_id,
            "parent_run_id": row.parent_run_id, "status": row.status,
            "branch": row.branch, "commit_sha": row.commit_sha,
            "trigger_source": row.trigger_source, "browser_matrix": row.browser_matrix,
            "created_at": row.created_at, "started_at": row.started_at,
            "finished_at": row.finished_at,
        }

    @staticmethod
    def _approval_dict(row):
        return {
            "id": row.id,
            "project_id": row.project_id,
            "target_kind": row.target_kind,
            "target_id": row.target_id,
            "status": row.status,
            "requested_by": row.requested_by,
            "kind": row.kind,
            "expires_at": row.expires_at,
        }

    @staticmethod
    def _event_dict(row):
        return {"id": row.id, "event_type": row.event_type,
                "payload": row.payload, "timestamp": row.created_at}

    @staticmethod
    def _encode_cursor(row):
        raw = f"{row.created_at.isoformat()}|{row.id}".encode("utf-8")
        return urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor):
        if not cursor or len(cursor) > 512:
            raise ValueError("invalid cursor")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            created_at, run_id = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").split("|", 1)
            return datetime.fromisoformat(created_at), UUID(run_id)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid cursor") from exc
