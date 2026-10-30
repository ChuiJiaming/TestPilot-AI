from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import jwt
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.api.app.runs.agent import (
    _resume_approved_healing,
    build_run_graph,
    execute_admitted_run,
)
from apps.api.app.runs.browser import (
    BrowserGatewayExecutionClient,
    BrowserGatewayUnavailable,
)
from apps.api.app.runs.executor import DeterministicExecutor


def _v2_case(*, run_case_id=None):
    case_id = uuid4()
    return {
        "schema_version": 2,
        "case_id": str(case_id),
        "case_version_id": str(uuid4()),
        "run_case_id": run_case_id or uuid4(),
        "browser": "chromium",
        "title": "Submit an order",
        "steps": [
            {"action": "navigate", "url": "https://example.com/orders/new"},
            {"action": "click", "locator": {
                "by": "role", "value": "button", "name": "Submit",
            }},
        ],
        "assertions": [{
            "type": "url", "expected": "/confirmation", "match": "contains",
        }],
    }


class _ExecutorRepository:
    def __init__(self):
        self.finished = []
        self.started = []
        self.proposals = []
        self.cancelled = []

    def execution_status(self, *_args, **_kwargs):
        return "RUNNING"

    def start_step(self, _run_case_id, **kwargs):
        self.started.append(kwargs)
        return {"execute": True, "step_id": uuid4(), **kwargs}

    def finish_step(self, step_id, **kwargs):
        self.finished.append((step_id, kwargs))

    def consume_agent_budget(self, *_args, **_kwargs):
        return True

    def propose_healing(self, *args, **kwargs):
        self.proposals.append((args, kwargs))
        return {"healing_id": uuid4(), "approval_id": uuid4()}

    def cancel_healing(self, *args, **kwargs):
        self.cancelled.append((args, kwargs))
        return {"status": "CANCELLED"}


class _Gateway:
    def __init__(self, result=None, error=None):
        self.result = result or {"status": "SUCCEEDED"}
        self.error = error
        self.actions = []
        self.assertions = []
        self.verifications = []

    def action(self, **kwargs):
        self.actions.append(kwargs)
        if self.error:
            raise self.error
        return self.result

    def observe(self, **_kwargs):
        return {"a11y_tree": [{"role": "button", "name": "Submit"}]}

    def verify_locator(self, **kwargs):
        self.verifications.append(kwargs)
        return {
            "status": "VERIFIED", "match_count": 1, "visible": True,
            "descriptor": {"role": "button", "name": "Submit"},
        }

    def assertion(self, **kwargs):
        self.assertions.append(kwargs)
        return {"matched": True}


def _executor(*, result=None, error=None):
    case = _v2_case()
    repository = _ExecutorRepository()
    gateway = _Gateway(result=result, error=error)
    executor = DeterministicExecutor(
        repository, gateway, run_id=uuid4(), attempt_token=uuid4(),
        worker_id="agent-1", snapshot={"base_url": "https://example.com"},
        policy={"agent_budget": 10},
    )
    executor.prepare_case(case)
    executor.current_case = case
    executor.bind_browser_session(uuid4(), "job-token")
    return case, repository, gateway, executor


@pytest.mark.parametrize(
    "result,eligible",
    [
        ({"status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
          "failure_phase": "locator_resolution", "action_dispatched": False,
          "match_count": 2}, True),
        ({"status": "FAILED", "error_code": "ACTION_TIMEOUT",
          "failure_phase": "action", "action_dispatched": True}, False),
        ({"status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
          "failure_phase": "action", "action_dispatched": False,
          "match_count": 2}, False),
        ({"status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
          "failure_phase": "locator_resolution", "action_dispatched": True,
          "match_count": 2}, False),
        ({"status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
          "failure_phase": "locator_resolution", "action_dispatched": False,
          "match_count": 1}, False),
    ],
)
def test_only_confirmed_pre_dispatch_locator_failures_are_recovery_eligible(result, eligible):
    case, repository, gateway, executor = _executor(result=result)

    state = executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]), "current_step": 1,
    })

    assert state.get("recovery_eligible", False) is eligible
    assert len(gateway.actions) == 1
    assert repository.finished


def test_transport_failure_never_enters_locator_recovery():
    case, _repository, gateway, executor = _executor(
        error=RuntimeError("gateway timeout"),
    )

    state = executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]), "current_step": 1,
    })

    assert state == {"case_status": "ERROR"}
    assert len(gateway.actions) == 1


def test_v3_locator_failure_does_not_enter_the_v2_only_recovery_policy():
    run_case_id = uuid4()
    case = {
        "schema_version": 3, "run_case_id": run_case_id,
        "case_id": str(uuid4()), "case_version_id": str(uuid4()),
        "browser": "chromium",
        "steps": [{
            "action_id": str(uuid4()), "action": "click",
            "locator": {"strategy": "testid", "value": "submit"},
            "replay_policy": "SAFE",
        }],
        "assertions": [{
            "assert_id": str(uuid4()), "type": "url_contains",
            "expected": "/confirmation", "severity": "BLOCKER",
        }],
    }
    repository = _ExecutorRepository()
    gateway = _Gateway(result={
        "status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
        "failure_phase": "locator_resolution", "action_dispatched": False,
        "match_count": 2,
    })
    executor = DeterministicExecutor(
        repository, gateway, run_id=uuid4(), attempt_token=uuid4(),
        worker_id="agent-1", snapshot={"base_url": "https://example.com"},
        policy={},
    )
    executor.prepare_case(case)

    state = executor.execute_step_node({
        "run_case_id": str(run_case_id), "current_step": 0,
    })

    assert state["recovery_eligible"] is False


def test_recovery_graph_persists_candidate_and_parks_before_any_replay():
    case, repository, gateway, executor = _executor(result={
        "status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
        "failure_phase": "locator_resolution", "action_dispatched": False,
        "match_count": 2,
    })
    failed = executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]), "current_step": 1,
    })
    proposal = {
        "action_id": str(executor.case_plans[case["run_case_id"]][1][1]["action_id"]),
        "locator": {"by": "role", "value": "button", "name": "Submit"},
        "source": "a11y_tree", "confidence": 0.99,
        "semantic_descriptor": {"role": "button", "name": "Submit"},
    }
    failed["run_id"] = str(executor.run_id)
    failed["retry_count"] = 0
    failed["failed_action_index"] = 1
    failed["failed_step_id"] = str(repository.finished[-1][0])
    failed["budget_left"] = 4
    failed["attempt_token"] = str(executor.attempt_token)
    failed["run_case_id"] = str(case["run_case_id"])
    result = executor.recovery_node(
        failed, lambda *_args: proposal, requester_id=uuid4(),
    )

    assert result["case_status"] == "WAITING_APPROVAL"
    assert result["healing_id"]
    assert executor.preserve_browser_session is True
    assert len(repository.proposals) == 1
    assert gateway.verifications[0]["locator"] == proposal["locator"]
    assert gateway.actions and len(gateway.actions) == 1


def test_real_run_graph_persists_only_verified_candidate_before_parking():
    case, repository, gateway, executor = _executor(result={
        "status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
        "failure_phase": "locator_resolution", "action_dispatched": False,
        "match_count": 0,
    })
    requester_id = uuid4()
    proposal = {
        "locator": {"by": "role", "value": "button", "name": "Submit"},
        "source": "a11y_tree", "confidence": 0.55,
        "semantic_descriptor": {"role": "button", "name": "Submit"},
    }

    def recoverer(_state, _case, failed_step, _observation):
        return {**proposal, "action_id": failed_step["action_id"]}

    graph = build_run_graph(
        InMemorySaver(), executor, recoverer=recoverer,
        requester_id=requester_id,
    )
    result = graph.invoke({
        "schema_version": 2, "run_id": str(executor.run_id),
        "run_case_id": str(case["run_case_id"]),
        "current_step": 1, "retry_count": 0, "budget_left": 4,
        "attempt_token": str(executor.attempt_token), "case_status": "RUNNING",
    }, config={"configurable": {
        "thread_id": str(uuid4()), "checkpoint_ns": "m10-real-recovery-branch",
    }})

    assert result["case_status"] == "WAITING_APPROVAL"
    assert result["healing_id"]
    assert len(gateway.actions) == 1
    assert len(gateway.verifications) == 1
    assert len(repository.proposals) == 1
    assert repository.proposals[0][1]["requester_id"] == requester_id
    assert executor.preserve_browser_session is True


def test_recovery_provider_is_not_called_when_run_budget_is_exhausted():
    case, repository, gateway, executor = _executor(result={
        "status": "FAILED", "error_code": "LOCATOR_NOT_UNIQUE",
        "failure_phase": "locator_resolution", "action_dispatched": False,
        "match_count": 2,
    })
    failed = executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]), "current_step": 1,
    })
    result = executor.recovery_node(
        {**failed, "run_case_id": str(case["run_case_id"]),
         "budget_left": 0},
        lambda *_args: pytest.fail("provider must not be called without budget"),
        requester_id=uuid4(),
    )

    assert result["case_status"] == "FAILED"
    assert repository.proposals == []
    assert gateway.verifications == []


def test_recovered_run_rechecks_original_assertions_after_the_replay():
    case, _repository, gateway, executor = _executor()
    steps, assertions = executor.case_plans[case["run_case_id"]][1:]
    executor.bind_approved_healing({
        "healing_id": uuid4(), "step_id": uuid4(),
        "action_id": steps[1]["action_id"],
        "new_locator": {"by": "role", "value": "button", "name": "Send"},
        "candidate_number": 1,
    }, failed_action_index=1)
    executor.active_healing["action_id"] = steps[1]["action_id"]
    # The cursor is past every published action after the approved replay succeeded.
    state = executor.assertion_node({
        "run_case_id": str(case["run_case_id"]), "current_step": len(steps),
    })

    assert state["case_status"] == "PASSED"
    assert len(gateway.assertions) == len(assertions) == 1
    assert gateway.assertions[0]["assertion"]["type"] == assertions[0]["assertion"]["type"]
    assert gateway.assertions[0]["assertion"]["expected"] == assertions[0]["assertion"]["expected"]


def test_second_candidate_is_not_requested_after_two_approved_attempts():
    case, repository, _gateway, executor = _executor()
    executor.active_healing = {
        "healing_id": uuid4(), "step_id": uuid4(),
        "action_id": executor.case_plans[case["run_case_id"]][1][1]["action_id"],
        "new_locator": {"by": "role", "value": "button", "name": "Submit"},
        "candidate_number": 2,
    }
    state = {
        "run_id": str(executor.run_id), "run_case_id": str(case["run_case_id"]),
        "attempt_token": str(executor.attempt_token), "failed_action_index": 1,
        "failed_step_id": str(uuid4()), "retry_count": 1,
        "recovery_eligible": True,
    }

    result = executor.recovery_node(state, lambda *_args: pytest.fail("third proposal"), requester_id=uuid4())

    assert result["case_status"] == "FAILED"
    assert repository.proposals == []
    assert repository.cancelled


def test_approved_candidate_replays_only_failed_action_with_stable_scoped_id():
    case, repository, gateway, executor = _executor()
    steps = executor.case_plans[case["run_case_id"]][1]
    original = steps[1]
    healing_id = uuid4()
    candidate = {"by": "role", "value": "button", "name": "Send"}
    executor.bind_approved_healing({
        "healing_id": healing_id, "step_id": uuid4(),
        "action_id": original["action_id"], "new_locator": candidate,
        "candidate_number": 1,
    }, failed_action_index=1)

    state = executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]), "current_step": 1,
    })

    expected_action_id = uuid5(
        NAMESPACE_URL, f"testpilot:m10:{original['action_id']}:{healing_id}:1",
    )
    assert state["current_step"] == 2
    assert gateway.actions[-1]["action"]["action_id"] == str(expected_action_id)
    assert gateway.actions[-1]["action"]["locator"] == candidate
    assert repository.started[-1]["sequence"] == len(steps) + 2
    assert repository.started[-1]["recovery_healing_id"] == healing_id


def test_fill_replay_changes_only_locator_and_preserves_published_value():
    case = _v2_case()
    case["steps"][1] = {
        "action": "fill",
        "locator": {"by": "role", "value": "textbox", "name": "Email"},
        "text": "fixed-test-value@example.invalid",
    }
    repository = _ExecutorRepository()
    gateway = _Gateway()
    executor = DeterministicExecutor(
        repository, gateway, run_id=uuid4(), attempt_token=uuid4(),
        worker_id="agent-1", snapshot={"base_url": "https://example.com"},
        policy={},
    )
    executor.prepare_case(case)
    original = executor.case_plans[case["run_case_id"]][1][1]
    candidate = {"by": "role", "value": "textbox", "name": "Work email"}
    executor.bind_approved_healing({
        "healing_id": uuid4(), "step_id": uuid4(),
        "action_id": original["action_id"], "new_locator": candidate,
        "candidate_number": 1,
    }, failed_action_index=1)

    executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]), "current_step": 1,
    })

    replayed = gateway.actions[0]["action"]
    assert replayed["action"] == "fill"
    assert replayed["locator"] == candidate
    assert replayed["value"] == "fixed-test-value@example.invalid"
    assert original["args"]["value"] == "fixed-test-value@example.invalid"


def test_graph_routes_confirmed_failure_through_recovery_and_parks():
    calls = {"recovery": 0, "assertions": 0}

    class Executor:
        current_case = {"schema_version": 2, "run_case_id": str(uuid4())}

        def prepare_case(self, _case):
            return {"action_count": 1, "assertion_count": 1}

        def explorer_node(self, _state):
            return {"exploration_status": "BOUNDED_TO_PUBLISHED_CASE"}

        def execute_step_node(self, _state):
            return {"case_status": "FAILED", "recovery_eligible": True}

        def recovery_node(self, state, _recoverer, **_kwargs):
            calls["recovery"] += 1
            return {"case_status": "WAITING_APPROVAL", "healing_id": str(uuid4())}

        def assertion_node(self, _state):
            calls["assertions"] += 1
            return {"case_status": "PASSED"}

    executor = Executor()
    graph = build_run_graph(
        InMemorySaver(), executor, recoverer=lambda *_args: None,
    )
    result = graph.invoke({
        "schema_version": 2, "run_id": str(uuid4()),
        "run_case_id": executor.current_case["run_case_id"],
        "current_step": 0, "action_count": 1, "budget_left": 0,
    }, config={"configurable": {
        "thread_id": str(uuid4()), "checkpoint_ns": "m10-recovery-route",
    }})

    assert result["case_status"] == "WAITING_APPROVAL"
    assert calls == {"recovery": 1, "assertions": 0}


def test_approved_resume_rebinds_same_session_and_restores_failed_cursor():
    run_id, run_case_id, attempt_token = uuid4(), uuid4(), uuid4()
    case = _v2_case(run_case_id=run_case_id)
    old_token, session_id = uuid4(), uuid4()
    executor = DeterministicExecutor(
        _ExecutorRepository(), _Gateway(), run_id=run_id,
        attempt_token=attempt_token, worker_id="agent-2",
        snapshot={"base_url": "https://example.com"}, policy={},
    )
    executor.prepare_case(case)
    executor.current_case = case

    class Repository:
        def load_approved_healing(self, *_args, **_kwargs):
            return {
                "healing_id": uuid4(), "run_id": run_id,
                "run_case_id": run_case_id, "step_id": uuid4(),
                "action_id": executor.case_plans[run_case_id][1][1]["action_id"],
                "sequence": 2, "browser": "chromium",
                "new_locator": {"by": "role", "value": "button", "name": "Send"},
                "previous_attempt_token": old_token, "attempt_token": attempt_token,
                "session_id": session_id, "worker_instance": "worker-a",
                "session_status": "READY", "candidate_number": 1,
                "attempt_matches": True,
            }

        def rebind_browser_session(self, *_args, **_kwargs):
            return True

    class Gateway:
        def __init__(self):
            self.rebinds = []

        def rebind_session(self, **kwargs):
            self.rebinds.append(kwargs)
            return {"job_token": "rebound-job-token", "worker_instance": "worker-a",
                    "session_id": session_id, "status": "READY"}

    repository, gateway = Repository(), Gateway()

    restored = _resume_approved_healing(
        repository, gateway, executor, run_id=run_id, run_case_id=run_case_id,
        case=case, attempt_token=attempt_token, worker_id="agent-2",
        snapshot={"base_url": "https://example.com"},
    )

    assert restored["current_step"] == 1
    assert restored["retry_count"] == 0
    assert gateway.rebinds[0]["session_id"] == session_id
    assert gateway.rebinds[0]["previous_attempt_token"] == old_token
    assert executor.current_session_id == session_id
    assert executor.current_job_token == "rebound-job-token"
    assert executor.active_healing["new_locator"]["name"] == "Send"


def test_lost_approved_session_fails_closed_without_rebind_or_new_page():
    run_id, run_case_id, attempt_token = uuid4(), uuid4(), uuid4()
    case = _v2_case(run_case_id=run_case_id)
    executor = DeterministicExecutor(
        _ExecutorRepository(), _Gateway(), run_id=run_id,
        attempt_token=attempt_token, worker_id="agent-2",
        snapshot={"base_url": "https://example.com"}, policy={},
    )
    executor.prepare_case(case)

    class Repository:
        def __init__(self):
            self.cancelled = []

        def load_approved_healing(self, *_args, **_kwargs):
            return {
                "healing_id": uuid4(), "step_id": uuid4(),
                "action_id": executor.case_plans[run_case_id][1][1]["action_id"],
                "sequence": 2, "browser": "chromium", "session_id": None,
                "worker_instance": None, "session_status": "MISSING",
                "candidate_number": 1, "attempt_matches": True,
            }

        def cancel_healing(self, *args, **kwargs):
            self.cancelled.append((args, kwargs))

    class Gateway:
        def rebind_session(self, **_kwargs):
            pytest.fail("a missing session must not be rebound")

        def create_session(self, **_kwargs):
            pytest.fail("recovery must not silently open a new page")

    repository, gateway = Repository(), Gateway()

    with pytest.raises(BrowserGatewayUnavailable, match="M10_SESSION_NOT_RESTORABLE"):
        _resume_approved_healing(
            repository, gateway, executor, run_id=run_id, run_case_id=run_case_id,
            case=case, attempt_token=attempt_token, worker_id="agent-2",
            snapshot={"base_url": "https://example.com"},
        )

    assert repository.cancelled


def test_admitted_run_never_creates_a_page_when_approved_healing_session_is_missing(monkeypatch):
    import apps.api.app.runs.agent as run_agent

    run_id, run_case_id, attempt_token = uuid4(), uuid4(), uuid4()
    case = _v2_case(run_case_id=run_case_id)
    case["status"] = "RUNNING"
    healing_id = uuid4()

    class Repository:
        def __init__(self):
            self.cancelled = []
            self.case_states = []
            self.transitions = []

        def load_execution_bundle(self, *_args, **_kwargs):
            return {
                "run": {"requested_by": uuid4()}, "policy": {"agent_budget": 0},
                "snapshot": {"id": uuid4(), "base_url": "https://example.com",
                             "network_policy": {"allowed_domains": ["example.com"]}},
                "cases": [case],
            }

        def execution_status(self, *_args, **_kwargs):
            return "RUNNING"

        def transition(self, *_args, **kwargs):
            self.transitions.append(kwargs)

        def set_run_case_state(self, _run_case_id, status, **_kwargs):
            self.case_states.append(status)

        def load_approved_healing(self, *_args, **_kwargs):
            return {
                "healing_id": healing_id, "step_id": uuid4(),
                "action_id": uuid5(NAMESPACE_URL,
                                   f"testpilot:v2:{case['case_version_id']}:2"),
                "previous_attempt_token": uuid4(),
                "attempt_matches": True, "candidate_number": 1,
                "session_id": None, "worker_instance": None,
                "session_status": "MISSING", "browser": "chromium",
                "new_locator": {"by": "role", "value": "button", "name": "Send"},
            }

        def cancel_healing(self, *args, **kwargs):
            self.cancelled.append((args, kwargs))

        def create_browser_session(self, *_args, **_kwargs):
            pytest.fail("missing M10 session must not allocate a replacement session")

    class Gateway:
        def __init__(self):
            self.created = []

        def create_session(self, **kwargs):
            self.created.append(kwargs)
            pytest.fail("missing M10 session must not create a page")

    class Heartbeat:
        def __init__(self, *_args, **_kwargs):
            self.lost_event = type("Event", (), {"is_set": lambda _self: False})()

        def start(self):
            pass

        def stop(self):
            pass

    class Checkpointer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setup(self):
            pass

        @classmethod
        def from_conn_string(cls, _value):
            return cls()

    repository, gateway = Repository(), Gateway()
    monkeypatch.setattr(run_agent, "RunRepository", lambda _factory: repository)
    monkeypatch.setattr(run_agent, "BrowserGatewayExecutionClient", type(
        "BrowserClientFactory", (), {"from_environment": staticmethod(lambda: gateway)},
    ))
    monkeypatch.setattr(run_agent, "_RunHeartbeat", Heartbeat)
    monkeypatch.setattr(run_agent, "PostgresSaver", Checkpointer)
    monkeypatch.setattr(run_agent, "_checkpoint_conninfo", lambda: "postgresql://local")
    monkeypatch.setattr(run_agent, "build_run_graph", lambda *_args, **_kwargs: None)

    result = execute_admitted_run(
        object(), run_id=run_id, attempt_token=attempt_token, worker_id="agent-2",
    )

    assert result["status"] == "ERROR"
    assert repository.case_states == ["RUNNING", "ERROR"]
    assert repository.cancelled[0][1]["manual_reconciliation"] is True
    assert gateway.created == []


def test_gateway_verifies_locator_and_rebinds_with_approved_scoped_grant(monkeypatch):
    attempt_token, next_token = uuid4(), uuid4()
    run_id, run_case_id, session_id, healing_id = uuid4(), uuid4(), uuid4(), uuid4()
    client = BrowserGatewayExecutionClient(
        base_url="https://gateway.example", client_cert="cert", client_key="key",
        ca_bundle="ca", token_secret="test-secret",
    )
    client.session_context[session_id] = {
        "run_id": run_id, "run_case_id": run_case_id,
        "browser": "chromium", "attempt_token": attempt_token,
        "snapshot": {
            "base_url": "https://example.com",
            "network_policy": {"allowed_domains": ["example.com"]},
        }, "worker_instance": "worker-a",
    }
    requests = []

    def fake_post(path, payload):
        requests.append((path, payload))
        if path.endswith("/verify"):
            return {"status": "VERIFIED", "match_count": 1, "visible": True,
                    "descriptor": {"role": "button", "name": "Send"}}
        return {"status": "READY", "session_id": str(session_id),
                "worker_instance": "worker-a", "rebound": True}

    monkeypatch.setattr(client, "_post", fake_post)

    verified = client.verify_locator(
        session_id=session_id, run_id=run_id, run_case_id=run_case_id,
        browser="chromium", attempt_token=attempt_token, token="old-token",
        locator={"by": "role", "value": "button", "name": "Send"},
    )
    rebound = client.rebind_session(
        healing_id=healing_id, previous_attempt_token=attempt_token,
        attempt_token=next_token, session_id=session_id, run_id=run_id,
        run_case_id=run_case_id, browser="chromium", snapshot={
            "base_url": "https://example.com",
            "network_policy": {"allowed_domains": ["example.com"]},
        }, worker_instance="worker-a",
    )

    assert verified["match_count"] == 1
    assert requests[0][0].endswith("/locators/verify")
    rebind_path, rebind_payload = requests[1]
    assert rebind_path.endswith("/rebind")
    claims = jwt.decode(rebound["job_token"], "test-secret", algorithms=["HS256"],
                        audience="testpilot-browser-gateway")
    assert claims["attempt_token"] == str(next_token)
    assert claims["worker_instance"] == "worker-a"
    assert claims["recovery_handoff"] == {
        "healing_id": str(healing_id), "session_id": str(session_id),
        "run_id": str(run_id), "run_case_id": str(run_case_id),
        "browser": "chromium", "previous_attempt_token": str(attempt_token),
        "attempt_token": str(next_token), "worker_instance": "worker-a",
    }
    assert rebind_payload["attempt_token"] == str(next_token)
    assert rebound["worker_instance"] == "worker-a"
    assert client.session_context[session_id]["attempt_token"] == next_token


def test_rebind_grant_cannot_be_built_without_a_worker_affinity():
    client = BrowserGatewayExecutionClient(
        base_url="https://gateway.example", client_cert="cert", client_key="key",
        ca_bundle="ca", token_secret="test-secret",
    )

    with pytest.raises(BrowserGatewayUnavailable, match="BROWSER_SESSION_SCOPE_MISMATCH"):
        client.rebind_session(
            healing_id=uuid4(), previous_attempt_token=uuid4(),
            attempt_token=uuid4(), session_id=uuid4(), run_id=uuid4(),
            run_case_id=uuid4(), browser="chromium",
            snapshot={"base_url": "https://example.com",
                      "network_policy": {"allowed_domains": ["example.com"]}},
            worker_instance="",
        )
