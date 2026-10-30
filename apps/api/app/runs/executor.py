"""Versioned deterministic DSL parsing and fenced browser action execution."""

from uuid import NAMESPACE_URL, UUID, uuid5

from apps.api.app.cases.dsl import DslValidationFailed, validate_dsl
from apps.api.app.runs.repository import RunRepository, StaleRunAttempt
from apps.api.app.runs.recovery import evaluate_recovery_candidate


class ExecutionContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _locator_v3(value):
    if not isinstance(value, dict):
        raise ExecutionContractError("INVALID_DSL_LOCATOR")
    strategy = value.get("strategy")
    if strategy == "role":
        role = value.get("role")
        name = value.get("name")
        if not isinstance(role, str) or not role or (name is not None and not isinstance(name, str)):
            raise ExecutionContractError("INVALID_DSL_LOCATOR")
        return {"by": "role", "value": role, "name": name, "exact": value.get("exact", False)}
    if strategy == "testid":
        test_id = value.get("value")
        if not isinstance(test_id, str) or not test_id:
            raise ExecutionContractError("INVALID_DSL_LOCATOR")
        return {"by": "test_id", "value": test_id}
    raise ExecutionContractError("UNSUPPORTED_DSL_LOCATOR")


def _fixture_value(reference: str, fixtures: dict):
    if reference.startswith("secret:"):
        raise ExecutionContractError("SECRET_PROVIDER_UNAVAILABLE")
    if not reference.startswith("fixture:"):
        raise ExecutionContractError("INVALID_VALUE_REF")
    path = reference[len("fixture:"):].split(".")
    if not path or any(not part for part in path):
        raise ExecutionContractError("INVALID_VALUE_REF")
    value = fixtures
    for part in path:
        if not isinstance(value, dict) or part not in value:
            raise ExecutionContractError("FIXTURE_UNAVAILABLE")
        value = value[part]
    if not isinstance(value, str):
        raise ExecutionContractError("FIXTURE_VALUE_MUST_BE_TEXT")
    return value


def normalize_case(case: dict, *, fixtures: dict):
    version = case.get("schema_version")
    if version == 2:
        payload = {
            "schema_version": 2,
            "case_id": str(case["case_id"]),
            "title": case.get("title") or "Published case",
            "steps": case.get("steps") or [],
            "assertions": case.get("assertions") or [],
        }
        try:
            dsl = validate_dsl(payload, expected_case_id=UUID(str(case["case_id"])))
        except (DslValidationFailed, ValueError, TypeError) as exc:
            raise ExecutionContractError("INVALID_PUBLISHED_DSL_V2") from exc
        steps = []
        for index, item in enumerate(dsl.steps):
            raw = item.model_dump(mode="json")
            action_id = uuid5(
                NAMESPACE_URL,
                f"testpilot:v2:{case['case_version_id']}:{index + 1}",
            )
            action = raw["action"]
            if action == "navigate":
                replay = "SAFE"
                locator = None
                args = {"url": raw["url"]}
            elif action == "fill":
                replay = "SAFE"
                locator = raw["locator"]
                args = {"locator": locator, "value": raw["text"]}
            elif action == "click":
                replay = "NEVER"
                locator = raw["locator"]
                args = {"locator": locator}
            else:
                raise ExecutionContractError("UNSUPPORTED_DSL_V2_ACTION")
            steps.append({
                "action_id": action_id, "action": action, "args": args,
                "locator": locator, "replay_policy": replay,
            })
        assertions = []
        for index, item in enumerate(dsl.assertions):
            raw = item.model_dump(mode="json")
            assert_id = uuid5(
                NAMESPACE_URL,
                f"testpilot:v2-assertion:{case['case_version_id']}:{index + 1}",
            )
            if raw["type"] == "url":
                normalized = {"type": "url", "expected": raw["expected"],
                              "match": raw["match"]}
            else:
                normalized = {"type": "text", "expected": raw["expected"],
                              "match": raw["match"], "locator": raw["locator"]}
            assertions.append({
                "assert_id": assert_id, "assertion": normalized,
                "severity": "BLOCKER",
            })
        return steps, assertions

    if version != 3:
        raise ExecutionContractError("UNSUPPORTED_DSL_SCHEMA_VERSION")
    if not isinstance(case.get("steps"), list) or not case["steps"]:
        raise ExecutionContractError("INVALID_PUBLISHED_DSL_V3")
    if not isinstance(case.get("assertions"), list) or not case["assertions"]:
        raise ExecutionContractError("INVALID_PUBLISHED_DSL_V3")
    preconditions = case.get("preconditions", [])
    if not isinstance(preconditions, list):
        raise ExecutionContractError("INVALID_DSL_PRECONDITIONS")
    for precondition in preconditions:
        if not isinstance(precondition, dict) or not isinstance(precondition.get("fixture"), str):
            raise ExecutionContractError("INVALID_DSL_PRECONDITION")
        _fixture_value("fixture:" + precondition["fixture"], fixtures)

    steps = []
    for item in case["steps"]:
        if not isinstance(item, dict):
            raise ExecutionContractError("INVALID_DSL_STEP")
        try:
            action_id = UUID(str(item.get("action_id")))
        except (ValueError, TypeError) as exc:
            raise ExecutionContractError("INVALID_DSL_ACTION_ID") from exc
        action = item.get("action")
        replay = item.get("replay_policy")
        if replay not in {"SAFE", "FIXTURE_ONLY", "NEVER"}:
            raise ExecutionContractError("INVALID_DSL_REPLAY_POLICY")
        if action == "navigate":
            url = item.get("url")
            if not isinstance(url, str) or not url or len(url) > 2048:
                raise ExecutionContractError("INVALID_DSL_NAVIGATION")
            args = {"url": url}
            locator = None
        elif action == "click":
            locator = _locator_v3(item.get("locator"))
            args = {"locator": locator}
        elif action == "fill":
            locator = _locator_v3(item.get("locator"))
            reference = item.get("value_ref")
            if not isinstance(reference, str):
                raise ExecutionContractError("INVALID_VALUE_REF")
            args = {"locator": locator, "value": _fixture_value(reference, fixtures)}
        elif action == "upload":
            raise ExecutionContractError("UNSUPPORTED_DSL_UPLOAD")
        else:
            raise ExecutionContractError("UNSUPPORTED_DSL_V3_ACTION")
        steps.append({
            "action_id": action_id, "action": action, "args": args,
            "locator": locator, "replay_policy": replay,
        })

    assertions = []
    allowed_severity = {"BLOCKER", "MAJOR", "MINOR", "INFO"}
    for item in case["assertions"]:
        if not isinstance(item, dict) or item.get("severity") not in allowed_severity:
            raise ExecutionContractError("INVALID_DSL_ASSERTION_SEVERITY")
        try:
            assert_id = UUID(str(item.get("assert_id")))
        except (ValueError, TypeError) as exc:
            raise ExecutionContractError("INVALID_DSL_ASSERTION_ID") from exc
        kind = item.get("type")
        if kind == "url_contains":
            expected = item.get("expected")
            if not isinstance(expected, str):
                raise ExecutionContractError("INVALID_DSL_ASSERTION")
            assertion = {"type": "url", "expected": expected, "match": "contains"}
        elif kind == "element_visible":
            assertion = {"type": "visible", "locator": _locator_v3(item.get("locator"))}
        else:
            raise ExecutionContractError("UNSUPPORTED_DSL_V3_ASSERTION")
        assertions.append({
            "assert_id": assert_id, "assertion": assertion,
            "severity": item["severity"],
        })
    return steps, assertions


class DeterministicExecutor:
    """Executes immutable case actions and original assertions through the Gateway."""

    def __init__(self, repository: RunRepository, gateway, *, run_id: UUID,
                 attempt_token: UUID, worker_id: str, snapshot: dict, policy: dict):
        self.repository = repository
        self.gateway = gateway
        self.run_id = run_id
        self.attempt_token = attempt_token
        self.worker_id = worker_id
        self.snapshot = snapshot
        self.policy = policy
        self.case_plans: dict[UUID, tuple[dict, list, list]] = {}
        self.current_case: dict | None = None
        self.current_session_id: UUID | None = None
        self.current_job_token: str | None = None
        self.active_healing: dict | None = None
        self.preserve_browser_session = False
        fixtures = policy.get("fixtures", {})
        self.fixtures = fixtures if isinstance(fixtures, dict) else {}

    def bind_browser_session(self, session_id: UUID, job_token: str):
        self.current_session_id = session_id
        self.current_job_token = job_token
    def prepare_case(self, case: dict):
        try:
            steps, assertions = normalize_case(case, fixtures=self.fixtures)
        except ExecutionContractError:
            raise
        except Exception as exc:
            raise ExecutionContractError("INVALID_PUBLISHED_DSL") from exc
        self.case_plans[case["run_case_id"]] = (case, steps, assertions)
        return {"action_count": len(steps), "assertion_count": len(assertions)}

    def explorer_node(self, state: dict):
        # Fixed DSL runs do not grant an Explorer authority to invent Worker actions.
        return {"exploration_status": "BOUNDED_TO_PUBLISHED_CASE"}

    def consume_agent_budget(self):
        return self.repository.consume_agent_budget(
            self.run_id, self.attempt_token, worker_id=self.worker_id, now=self._now())

    def observe_page(self):
        if self.current_session_id is None or self.current_case is None:
            raise ExecutionContractError("BROWSER_SESSION_NOT_BOUND")
        return self.gateway.observe(
            session_id=self.current_session_id, run_id=self.run_id,
            run_case_id=UUID(str(self.current_case["run_case_id"])),
            browser=self.current_case["browser"], attempt_token=self.attempt_token,
            token=self.current_job_token,
        )

    def execute_step_node(self, state: dict):
        case_id = UUID(state["run_case_id"])
        case, steps, _ = self.case_plans[case_id]
        index = state["current_step"]
        if index >= len(steps):
            return {}
        status = self.repository.execution_status(
            self.run_id, self.attempt_token, worker_id=self.worker_id)
        if status == "PAUSING":
            self.repository.transition(
                self.run_id, "PAUSED", expected_status="PAUSING",
                now=self._now(), attempt_token=self.attempt_token,
                worker_id=self.worker_id,
            )
            return {"case_status": "PAUSED"}
        if status == "CANCELLING":
            self.repository.set_run_case_state(
                case_id, "CANCELLED", attempt_token=self.attempt_token,
                worker_id=self.worker_id, now=self._now(),
            )
            self.repository.transition(
                self.run_id, "CANCELLED", expected_status="CANCELLING",
                now=self._now(), attempt_token=self.attempt_token,
                worker_id=self.worker_id,
            )
            return {"case_status": "CANCELLED"}

        step = steps[index]
        replay_index = None
        recovery_healing_id = None
        if (self.active_healing is not None
                and index == self.active_healing.get("failed_action_index")):
            replay_index = self.active_healing["candidate_number"]
            recovery_healing_id = self.active_healing["healing_id"]
            replay_action_id = uuid5(
                NAMESPACE_URL,
                f"testpilot:m10:{step['action_id']}:{recovery_healing_id}:{replay_index}",
            )
            candidate_locator = self.active_healing["new_locator"]
            step = {
                **step,
                "action_id": replay_action_id,
                "locator": candidate_locator,
                "args": {**step["args"], "locator": candidate_locator},
            }
        sequence = (
            len(steps) + len(self.case_plans[case_id][2]) + replay_index
            if replay_index is not None else index + 1
        )
        persisted = self.repository.start_step(
            case_id, action_id=step["action_id"], sequence=sequence,
            action_type=step["action"], locator=step["locator"], expected=None,
            replay_policy=step["replay_policy"], attempt_token=self.attempt_token,
            worker_id=self.worker_id, now=self._now(),
            recovery_healing_id=recovery_healing_id,
        )
        if not persisted.get("execute"):
            if persisted.get("reason") == "STEP_ALREADY_PASSED":
                return {
                    "current_step": index + 1,
                    "last_completed_action_id": str(step["action_id"]),
                }
            if persisted.get("reason") == "RUN_PAUSING":
                self.repository.transition(
                    self.run_id, "PAUSED", expected_status="PAUSING",
                    now=self._now(), attempt_token=self.attempt_token,
                    worker_id=self.worker_id,
                )
                return {"case_status": "PAUSED"}
            if persisted.get("reason") == "RUN_CANCELLING":
                self.repository.set_run_case_state(
                    case_id, "CANCELLED", attempt_token=self.attempt_token,
                    worker_id=self.worker_id, now=self._now(),
                )
                self.repository.transition(
                    self.run_id, "CANCELLED", expected_status="CANCELLING",
                    now=self._now(), attempt_token=self.attempt_token,
                    worker_id=self.worker_id,
                )
                return {"case_status": "CANCELLED"}
            if persisted.get("reason") == "MANUAL_RECONCILIATION":
                return {"case_status": "WAITING_APPROVAL"}
            if persisted.get("reason") == "STEP_ALREADY_RUNNING":
                raise ExecutionContractError("ACTION_ATTEMPT_ALREADY_ACTIVE")
            raise ExecutionContractError(persisted.get("reason", "STEP_NOT_EXECUTABLE"))

        try:
            result = self.gateway.action(
                session_id=self.current_session_id, run_id=self.run_id,
                run_case_id=case_id, browser=case["browser"],
                attempt_token=self.attempt_token, token=self.current_job_token,
                action={
                    "action_id": str(step["action_id"]),
                    "action": step["action"],
                    **step["args"],
                },
            )
            succeeded = result.get("status") == "SUCCEEDED"
            safe_result = {"status": result.get("status"), "error_code": result.get("error_code")}
            if replay_index is not None:
                safe_result.update({
                    "recovery_healing_id": str(recovery_healing_id),
                    "replay_index": replay_index,
                    "source_action_id": str(self.active_healing["action_id"]),
                })
            if result.get("failure_phase") is not None:
                safe_result["failure_phase"] = result.get("failure_phase")
            if type(result.get("action_dispatched")) is bool:
                safe_result["action_dispatched"] = result["action_dispatched"]
            if type(result.get("match_count")) is int:
                safe_result["match_count"] = result["match_count"]
            self.repository.finish_step(
                persisted["step_id"], status="PASSED" if succeeded else "FAILED",
                result=safe_result, attempt_token=self.attempt_token,
                worker_id=self.worker_id, now=self._now(),
            )
            if not succeeded:
                eligible = (
                    result.get("status") == "FAILED"
                    and result.get("error_code") == "LOCATOR_NOT_UNIQUE"
                    and result.get("failure_phase") == "locator_resolution"
                    and result.get("action_dispatched") is False
                    and type(result.get("match_count")) is int
                    and result["match_count"] >= 0
                    and result["match_count"] != 1
                    and case.get("schema_version") == 2
                    and step["action"] in {"click", "fill"}
                    and isinstance(step.get("locator"), dict)
                )
                return {
                    "case_status": "FAILED",
                    "recovery_eligible": eligible,
                    **({
                        "failed_action_index": index,
                        "failed_step_id": str(persisted["step_id"]),
                        "failed_action_id": str(self.active_healing["action_id"])
                        if replay_index is not None
                        else str(step["action_id"]),
                    } if eligible else {}),
                }
        except StaleRunAttempt:
            raise
        except Exception as exc:
            self.repository.finish_step(
                persisted["step_id"], status="ERROR",
                result={"error_code": getattr(exc, "code", "BROWSER_ACTION_FAILED")},
                attempt_token=self.attempt_token, worker_id=self.worker_id,
                now=self._now(),
            )
            return {"case_status": "ERROR"}
        return {
            "current_step": index + 1,
            "last_completed_action_id": str(step["action_id"]),
        }

    def recovery_node(self, state: dict, recoverer, *, requester_id: UUID | None):
        """Propose a locator only after a confirmed pre-dispatch failure and live verification."""
        if state.get("recovery_eligible") is not True:
            return {"case_status": "FAILED", "recovery_eligible": False}
        case_id = UUID(str(state["run_case_id"]))
        case, steps, _ = self.case_plans[case_id]
        index = state.get("failed_action_index")
        if type(index) is not int or not 0 <= index < len(steps):
            return {"case_status": "FAILED", "recovery_eligible": False}
        source_step = steps[index]
        retry_count = state.get("retry_count", 0)
        if type(retry_count) is not int or retry_count < 0:
            return {"case_status": "ERROR", "recovery_eligible": False}

        previous = self.active_healing
        if retry_count >= 1:
            if previous is not None:
                self._cancel_active_healing(
                    run_case_id=case_id, reason="HEALING_REPLAY_LIMIT",
                    continue_retry=False,
                )
            return {"case_status": "FAILED", "recovery_eligible": False}

        old_locator = source_step["locator"]
        failed_step_id = state.get("failed_step_id")
        proposal_step_id = None
        if previous is not None:
            if (previous.get("action_id") != source_step["action_id"]
                    or previous.get("failed_action_index") != index):
                self._cancel_active_healing(
                    run_case_id=case_id, reason="HEALING_ACTION_SCOPE_MISMATCH",
                    continue_retry=False,
                )
                return {"case_status": "ERROR", "recovery_eligible": False}
            old_locator = previous["new_locator"]
            proposal_step_id = previous["step_id"]
            self._cancel_active_healing(
                run_case_id=case_id, reason="HEALING_CANDIDATE_FAILED",
                continue_retry=True,
            )
        else:
            try:
                proposal_step_id = UUID(str(failed_step_id))
            except (ValueError, TypeError, AttributeError):
                return {"case_status": "ERROR", "recovery_eligible": False}

        if recoverer is None or requester_id is None:
            return {"case_status": "FAILED", "recovery_eligible": False}
        graph_budget = state.get("budget_left", 0)
        if type(graph_budget) is not int or graph_budget <= 0:
            return {"case_status": "FAILED", "recovery_eligible": False,
                    "budget_left": 0}
        if not self.consume_agent_budget():
            return {"case_status": "FAILED", "recovery_eligible": False,
                    "budget_left": 0}

        failed_action = {
            "action_id": str(source_step["action_id"]),
            "action": source_step["action"],
            "locator": old_locator,
        }
        try:
            observation = self.observe_page()
            proposal = recoverer(state, case, failed_action, observation)
            if hasattr(proposal, "model_dump"):
                proposal = proposal.model_dump(mode="json")
            verification = self.gateway.verify_locator(
                session_id=self.current_session_id, run_id=self.run_id,
                run_case_id=case_id, browser=case["browser"],
                attempt_token=self.attempt_token, token=self.current_job_token,
                locator=proposal.get("locator") if isinstance(proposal, dict) else None,
            )
            decision = evaluate_recovery_candidate(
                proposal=proposal, verification=verification,
            )
            if (verification.get("status") != "VERIFIED"
                    or not decision.accepted or decision.proposal is None
                    or decision.proposal.action_id != source_step["action_id"]):
                return {"case_status": "FAILED", "recovery_eligible": False,
                        "budget_left": max(0, state.get("budget_left", 0) - 1)}
            healing = self.repository.propose_healing(
                self.run_id, case_id, proposal_step_id,
                old_locator=old_locator, proposal=decision.proposal,
                evidence={"verification": verification, "observation": observation},
                requester_id=requester_id, now=self._now(),
            )
        except StaleRunAttempt:
            raise
        except Exception:
            return {"case_status": "ERROR", "recovery_eligible": False,
                    "budget_left": max(0, state.get("budget_left", 0) - 1)}

        self.preserve_browser_session = True
        return {
            "case_status": "WAITING_APPROVAL",
            "healing_id": str(healing["healing_id"]),
            "recovery_eligible": False,
            "retry_count": retry_count + (1 if previous is not None else 0),
            "budget_left": max(0, state.get("budget_left", 0) - 1),
        }

    def bind_approved_healing(self, healing: dict, failed_action_index: int):
        self.active_healing = {
            **healing, "failed_action_index": failed_action_index,
        }

    def _cancel_active_healing(self, *, run_case_id: UUID, reason: str,
                               continue_retry: bool):
        if self.active_healing is None:
            return
        healing_id = self.active_healing["healing_id"]
        self.repository.cancel_healing(
            healing_id, self.run_id, run_case_id, self.attempt_token,
            worker_id=self.worker_id, reason=reason,
            continue_retry=continue_retry, now=self._now(),
        )
        self.active_healing = None

    def assertion_node(self, state: dict):
        case_id = UUID(state["run_case_id"])
        case, steps, assertions = self.case_plans[case_id]
        failed = False
        for index, item in enumerate(assertions):
            run_status = self.repository.execution_status(
                self.run_id, self.attempt_token, worker_id=self.worker_id)
            if run_status == "PAUSING":
                self.repository.transition(
                    self.run_id, "PAUSED", expected_status="PAUSING",
                    now=self._now(), attempt_token=self.attempt_token,
                    worker_id=self.worker_id,
                )
                return {"case_status": "PAUSED"}
            if run_status == "CANCELLING":
                self.repository.set_run_case_state(
                    case_id, "CANCELLED", attempt_token=self.attempt_token,
                    worker_id=self.worker_id, now=self._now(),
                )
                self.repository.transition(
                    self.run_id, "CANCELLED", expected_status="CANCELLING",
                    now=self._now(), attempt_token=self.attempt_token,
                    worker_id=self.worker_id,
                )
                return {"case_status": "CANCELLED"}
            assertion_id = item["assert_id"]
            stored = self.repository.start_step(
                case_id, action_id=assertion_id, sequence=len(steps) + index + 1,
                action_type=f"assert:{item['assertion']['type']}",
                locator=item["assertion"].get("locator"),
                expected={"severity": item["severity"]},
                replay_policy="SAFE", attempt_token=self.attempt_token,
                worker_id=self.worker_id, now=self._now(),
            )
            if not stored.get("execute"):
                if stored.get("reason") == "STEP_ALREADY_PASSED":
                    continue
                if stored.get("reason") == "RUN_PAUSING":
                    self.repository.transition(
                        self.run_id, "PAUSED", expected_status="PAUSING",
                        now=self._now(), attempt_token=self.attempt_token,
                        worker_id=self.worker_id,
                    )
                    return {"case_status": "PAUSED"}
                if stored.get("reason") == "RUN_CANCELLING":
                    self.repository.set_run_case_state(
                        case_id, "CANCELLED", attempt_token=self.attempt_token,
                        worker_id=self.worker_id, now=self._now(),
                    )
                    self.repository.transition(
                        self.run_id, "CANCELLED", expected_status="CANCELLING",
                        now=self._now(), attempt_token=self.attempt_token,
                        worker_id=self.worker_id,
                    )
                    return {"case_status": "CANCELLED"}
                if stored.get("reason") == "STEP_ALREADY_RUNNING":
                    raise ExecutionContractError("ASSERTION_ATTEMPT_ALREADY_ACTIVE")
                return {"case_status": "WAITING_APPROVAL"}
            try:
                result = self.gateway.assertion(
                    session_id=self.current_session_id, run_id=self.run_id,
                    run_case_id=case_id, browser=case["browser"],
                    attempt_token=self.attempt_token, token=self.current_job_token,
                    assertion={
                        "assert_id": str(assertion_id),
                        **item["assertion"],
                    },
                )
                matched = result.get("matched") is True
                raw_error = result.get("error_code")
                error_code = raw_error[:80] if isinstance(raw_error, str) else None
                result_kind = (
                    "PASS" if matched else "LOCATOR" if error_code == "LOCATOR_NOT_UNIQUE"
                    else "INFRA" if error_code else "ASSERTION"
                )
                step_status = (
                    "PASSED" if matched else "ERROR" if result_kind == "INFRA" else "FAILED"
                )
                self.repository.finish_step(
                    stored["step_id"], status=step_status,
                    result={"matched": matched, "result_kind": result_kind,
                            "error_code": error_code},
                    attempt_token=self.attempt_token, worker_id=self.worker_id,
                    now=self._now(),
                )
                if result_kind == "INFRA":
                    return {"case_status": "ERROR"}
                failed = failed or result_kind in {"LOCATOR", "ASSERTION"}
            except StaleRunAttempt:
                raise
            except Exception as exc:
                self.repository.finish_step(
                    stored["step_id"], status="ERROR",
                    result={"error_code": getattr(exc, "code", "ASSERTION_EVALUATION_FAILED")},
                    attempt_token=self.attempt_token, worker_id=self.worker_id,
                    now=self._now(),
                )
                return {"case_status": "ERROR"}
        return {"case_status": "FAILED" if failed else "PASSED"}

    @staticmethod
    def _now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)
