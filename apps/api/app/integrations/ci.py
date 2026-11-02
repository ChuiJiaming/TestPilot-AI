"""Canonical CI idempotency and deterministic Run gates."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from sqlalchemy import select

from apps.api.app.cases.persistence import TestCaseRecord, TestCaseVersionRecord
from apps.api.app.runs.persistence import RunCaseRecord, TestRunRecord, TestStepRecord


BlockingSeverity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]


class CITriggerService:
    """Create stable idempotency digests for equivalent CI requests."""

    @staticmethod
    def idempotency_key(
        *,
        organization_id: UUID,
        repository: str,
        commit_sha: str,
        plan_id: UUID,
        environment_id: UUID,
        browser_matrix: list[str],
        trigger_source: str,
        request_intent: str,
    ) -> str:
        dimensions = {
            "organization_id": str(organization_id),
            "repository": repository.strip().strip("/").casefold(),
            "commit_sha": commit_sha.strip().casefold(),
            "plan_id": str(plan_id),
            "environment_id": str(environment_id),
            "browser_matrix": sorted(browser.strip().casefold() for browser in browser_matrix),
            "trigger_source": trigger_source.strip().casefold(),
            "request_intent": request_intent.strip(),
        }
        canonical = json.dumps(
            dimensions, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class GateEvaluator:
    """Evaluate a Run from persisted assertion steps and published case priority."""

    _TERMINAL_RUN_STATUSES = {"PASSED", "FAILED", "ERROR", "CANCELLED"}
    _PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    _SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def evaluate(self, run_id: UUID, *, blocking_severity: BlockingSeverity) -> dict | None:
        severity_rank = self._SEVERITY_RANK.get(blocking_severity)
        if severity_rank is None:
            raise ValueError("unsupported blocking severity")

        with self.session_factory() as session:
            run = session.get(TestRunRecord, run_id)
            if run is None:
                return None

            rows = session.execute(
                select(RunCaseRecord, TestCaseVersionRecord, TestCaseRecord)
                .join(
                    TestCaseVersionRecord,
                    TestCaseVersionRecord.id == RunCaseRecord.case_version_id,
                )
                .join(TestCaseRecord, TestCaseRecord.id == TestCaseVersionRecord.case_id)
                .where(RunCaseRecord.run_id == run_id)
                .order_by(RunCaseRecord.id)
            ).all()
            run_cases = [row[0] for row in rows]
            steps_by_case: dict[UUID, list[TestStepRecord]] = {item.id: [] for item in run_cases}
            if run_cases:
                steps = session.scalars(
                    select(TestStepRecord)
                    .where(TestStepRecord.run_case_id.in_(list(steps_by_case)))
                    .order_by(TestStepRecord.sequence)
                ).all()
                for step in steps:
                    if step.action_type.startswith("assert:"):
                        steps_by_case[step.run_case_id].append(step)

            assertions_total = 0
            assertions_evaluated = 0
            assertions_passed = 0
            assertions_failed = 0
            assertions_pending = 0
            blocking_failures = 0
            cases_without_assertions = 0
            cases_with_unknown_priority = 0

            for run_case, version, case in rows:
                expected_assertions = version.assertions or []
                if not expected_assertions:
                    cases_without_assertions += 1
                    continue

                priority_rank = self._PRIORITY_RANK.get(str(case.priority).upper())
                if priority_rank is None:
                    cases_with_unknown_priority += 1

                recorded_by_action = {
                    str(step.action_id): step for step in steps_by_case[run_case.id]
                }
                assertions_total += len(expected_assertions)
                for expected in expected_assertions:
                    action_id = expected.get("assert_id") if isinstance(expected, dict) else None
                    step = recorded_by_action.get(str(action_id)) if action_id else None
                    if step is None or step.status not in {"PASSED", "FAILED"}:
                        assertions_pending += 1
                        continue
                    assertions_evaluated += 1
                    if step.status == "PASSED":
                        assertions_passed += 1
                    else:
                        assertions_failed += 1
                        if priority_rank is not None and priority_rank <= severity_rank:
                            blocking_failures += 1

        is_terminal = run.status in self._TERMINAL_RUN_STATUSES
        is_complete = (
            is_terminal
            and bool(run_cases)
            and assertions_pending == 0
            and cases_without_assertions == 0
            and cases_with_unknown_priority == 0
        )
        status = "PENDING" if not is_complete else "FAIL" if blocking_failures else "PASS"
        return {
            "run_id": run_id,
            "status": status,
            "blocking_severity": blocking_severity,
            "counts": {
                "run_cases": len(run_cases),
                "assertions_total": assertions_total,
                "assertions_evaluated": assertions_evaluated,
                "assertions_passed": assertions_passed,
                "assertions_failed": assertions_failed,
                "assertions_pending": assertions_pending,
                "blocking_failures": blocking_failures,
                "cases_without_assertions": cases_without_assertions,
                "cases_with_unknown_priority": cases_with_unknown_priority,
            },
        }
