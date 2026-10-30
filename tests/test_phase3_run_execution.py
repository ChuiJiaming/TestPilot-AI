from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.api.app.runs.agent import build_run_graph
from apps.api.app.runs.executor import (
    DeterministicExecutor,
    ExecutionContractError,
    normalize_case,
)
from apps.api.app.runs.repository import StaleRunAttempt


def _v2_case(*, run_case_id=None):
    case_id = uuid4()
    return {
        "schema_version": 2,
        "case_id": str(case_id),
        "case_version_id": str(uuid4()),
        "run_case_id": run_case_id or uuid4(),
        "browser": "chromium",
        "title": "Submit an order",
        "steps": [{
            "action": "click",
            "locator": {"by": "role", "value": "button", "name": "Submit"},
        }],
        "assertions": [{
            "type": "url",
            "expected": "/confirmation",
            "match": "contains",
        }],
    }


class _ExecutionRepository:
    def __init__(self, start_result=None, error=None):
        self.start_result = start_result
        self.error = error

    def execution_status(self, *_args, **_kwargs):
        return "RUNNING"

    def start_step(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.start_result


class _Gateway:
    def __init__(self):
        self.actions = []

    def action(self, **kwargs):
        self.actions.append(kwargs)
        return {"status": "SUCCEEDED"}


def test_v2_click_is_never_replayed_and_action_ids_are_stable():
    case = _v2_case()

    first_steps, first_assertions = normalize_case(case, fixtures={})
    second_steps, second_assertions = normalize_case(case, fixtures={})

    assert first_steps[0]["replay_policy"] == "NEVER"
    assert first_steps[0]["action_id"] == second_steps[0]["action_id"]
    assert first_assertions[0]["assert_id"] == second_assertions[0]["assert_id"]


def test_v3_secret_reference_fails_closed_without_a_secret_provider():
    case = {
        "schema_version": 3,
        "steps": [{
            "action_id": str(uuid4()),
            "action": "fill",
            "locator": {"strategy": "testid", "value": "email"},
            "value_ref": "secret:credentials.email",
            "replay_policy": "SAFE",
        }],
        "assertions": [{
            "assert_id": str(uuid4()),
            "type": "url_contains",
            "expected": "/home",
            "severity": "BLOCKER",
        }],
    }

    with pytest.raises(ExecutionContractError, match="SECRET_PROVIDER_UNAVAILABLE"):
        normalize_case(case, fixtures={})


def test_executor_does_not_dispatch_a_step_awaiting_manual_reconciliation():
    case = _v2_case()
    attempt_token = uuid4()
    session_id = uuid4()
    gateway = _Gateway()
    repository = _ExecutionRepository({
        "execute": False,
        "reason": "MANUAL_RECONCILIATION",
    })
    executor = DeterministicExecutor(
        repository, gateway, run_id=uuid4(), attempt_token=attempt_token,
        worker_id="agent-1", snapshot={"base_url": "https://example.com"}, policy={},
    )
    executor.prepare_case(case)
    executor.current_case = case
    executor.bind_browser_session(session_id, "short-lived-job-token")

    result = executor.execute_step_node({
        "run_case_id": str(case["run_case_id"]),
        "current_step": 0,
    })

    assert result == {"case_status": "WAITING_APPROVAL"}
    assert gateway.actions == []


def test_executor_does_not_dispatch_after_the_repository_rejects_a_stale_attempt():
    case = _v2_case()
    gateway = _Gateway()
    executor = DeterministicExecutor(
        _ExecutionRepository(error=StaleRunAttempt()), gateway,
        run_id=uuid4(), attempt_token=uuid4(), worker_id="old-agent",
        snapshot={"base_url": "https://example.com"}, policy={},
    )
    executor.prepare_case(case)

    with pytest.raises(StaleRunAttempt):
        executor.execute_step_node({
            "run_case_id": str(case["run_case_id"]),
            "current_step": 0,
        })

    assert gateway.actions == []


def test_run_graph_skips_planner_and_explorer_when_the_budget_is_exhausted():
    calls = {"execute": 0, "assert": 0}

    class Executor:
        current_case = {"schema_version": 2, "run_case_id": str(uuid4())}

        def prepare_case(self, _case):
            return {"action_count": 1, "assertion_count": 1}

        def consume_agent_budget(self):
            raise AssertionError("a zero graph budget must not consume persistent budget")

        def observe_page(self):
            raise AssertionError("a zero graph budget must not observe the page")

        def execute_step_node(self, _state):
            calls["execute"] += 1
            return {"current_step": 1}

        def assertion_node(self, _state):
            calls["assert"] += 1
            return {"case_status": "PASSED"}

    def unexpected_model_call(*_args, **_kwargs):
        raise AssertionError("model call made with no budget")

    executor = Executor()
    graph = build_run_graph(
        InMemorySaver(), executor, planner=unexpected_model_call,
        explorer=unexpected_model_call,
    )
    result = graph.invoke({
        "schema_version": 2,
        "run_id": str(uuid4()),
        "run_case_id": executor.current_case["run_case_id"],
        "current_step": 0,
        "budget_left": 0,
    }, config={"configurable": {
        "thread_id": str(uuid4()),
        "checkpoint_ns": "phase3-budget-test",
    }})

    assert result["planner_status"] == "BUDGET_EXHAUSTED"
    assert result["exploration_status"] == "BUDGET_EXHAUSTED"
    assert result["case_status"] == "PASSED"
    assert calls == {"execute": 1, "assert": 1}
