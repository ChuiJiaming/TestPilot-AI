"""Versioned deterministic DSL parsing and fenced browser action execution."""

from uuid import NAMESPACE_URL, UUID, uuid5

from apps.api.app.cases.dsl import DslValidationFailed, validate_dsl
from apps.api.app.runs.repository import RunRepository, StaleRunAttempt


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
        persisted = self.repository.start_step(
            case_id, action_id=step["action_id"], sequence=index + 1,
            action_type=step["action"], locator=step["locator"], expected=None,
            replay_policy=step["replay_policy"], attempt_token=self.attempt_token,
            worker_id=self.worker_id, now=self._now(),
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
            self.repository.finish_step(
                persisted["step_id"], status="PASSED" if succeeded else "FAILED",
                result=safe_result, attempt_token=self.attempt_token,
                worker_id=self.worker_id, now=self._now(),
            )
            if not succeeded:
                return {"case_status": "FAILED"}
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