from datetime import datetime, timezone
from uuid import uuid4

import pytest

from apps.api.app.cases.planning import process_generation_job
from apps.api.app.operations.quotas import QuotaExceeded
from apps.api.app.runs.agent import build_run_graph
from apps.api.app.runs.planner import PlanProposal, RunAgentProvider
from langgraph.checkpoint.memory import InMemorySaver


NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
ORG_ID = uuid4()
RUN_ID = uuid4()
GENERATION_ID = uuid4()
ATTEMPT_TOKEN = uuid4()


class _QuotaSpy:
    def __init__(self, events, *, deny=False):
        self.events = events
        self.deny = deny

    def reserve(self, organization_id, *, call_id, run_id, requested_tokens, now):
        self.events.append(("reserve", organization_id, call_id, run_id, requested_tokens))
        if self.deny:
            raise QuotaExceeded

    def settle(self, call_id, *, consumed_tokens, usage_source="provider_metadata", now):
        self.events.append(("settle", call_id, consumed_tokens, usage_source))

    def release(self, call_id, *, now):
        self.events.append(("release", call_id))
        return True


class _GenerationRepository:
    def __init__(self):
        self.failed = None
        self.completed = None

    def claim_job(self, job_id, *, now):
        return {
            "id": job_id,
            "organization_id": ORG_ID,
            "requirement": "A sign-in page accepts valid credentials",
            "model_name": "test-model",
            "attempt_token": ATTEMPT_TOKEN,
        }

    def fail_job(self, job_id, *, attempt_token, error_code, now):
        self.failed = error_code
        return {"id": job_id, "status": "FAILED", "error_code": error_code}

    def complete_job(self, job_id, *, attempt_token, output, proposals, now):
        self.completed = output
        return {"id": job_id, "status": "SUCCEEDED"}


class _Planner:
    def __init__(self, events, *, error=None, output=None):
        self.events = events
        self.error = error
        self.output = output

    def generate(self, **_kwargs):
        self.events.append(("provider",))
        if self.error:
            raise self.error
        if self.output is not None:
            return self.output
        return {
            "model_version": "test-model-v1",
            "cost": "0.001",
            "input_tokens": 12,
            "output_tokens": 8,
            "proposals": [{
                "title": "Sign in",
                "priority": "P1",
                "steps": [{"action": "navigate", "url": "https://example.test/login"}],
                "assertions": [{"type": "url", "expected": "/login", "match": "contains"}],
            }],
        }


class _RawResponse:
    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata or {}
        self.response_metadata = {}


class _StructuredModel:
    def __init__(self, events, *, usage_metadata=None, error=None):
        self.events = events
        self.usage_metadata = usage_metadata
        self.error = error

    def invoke(self, _messages):
        self.events.append(("provider",))
        if self.error:
            raise self.error
        return {
            "raw": _RawResponse(self.usage_metadata),
            "parsed": PlanProposal(action_ids=["action-1"], assertion_ids=["assertion-1"]),
            "parsing_error": None,
        }


def _run_agent_provider(events, *, usage_metadata=None, error=None, deny=False):
    provider = RunAgentProvider.__new__(RunAgentProvider)
    provider._quota_service = _QuotaSpy(events, deny=deny)
    provider._organization_id = ORG_ID
    provider._run_id = RUN_ID
    provider._attempt_token = ATTEMPT_TOKEN
    provider._max_tokens = 512
    provider.planner_model = _StructuredModel(
        events, usage_metadata=usage_metadata, error=error,
    )
    return provider


def _invoke_plan(provider, run_case_id=None):
    case = {"run_case_id": str(run_case_id or uuid4()), "schema_version": 2}
    return provider.plan(
        {"run_id": str(RUN_ID), "run_case_id": case["run_case_id"], "budget_left": 4},
        case,
        {"action_count": 1, "assertion_count": 1},
        ["action-1"],
        ["assertion-1"],
    )


def test_generation_quota_denial_does_not_call_provider():
    events = []
    repository = _GenerationRepository()

    result = process_generation_job(
        repository,
        _Planner(events),
        GENERATION_ID,
        now_factory=lambda: NOW,
        quota_service=_QuotaSpy(events, deny=True),
    )

    assert result["error_code"] == "QUOTA_EXCEEDED"
    assert repository.failed == "QUOTA_EXCEEDED"
    assert len(events) == 1
    assert events[0][0] == "reserve"
    assert events[0][1] == ORG_ID
    assert events[0][3:] == (None, 4096)


def test_generation_provider_failure_releases_reservation():
    events = []
    repository = _GenerationRepository()

    process_generation_job(
        repository,
        _Planner(events, error=RuntimeError("provider unavailable")),
        GENERATION_ID,
        now_factory=lambda: NOW,
        quota_service=_QuotaSpy(events),
    )

    assert [item[0] for item in events] == ["reserve", "provider", "release"]


def test_generation_provider_usage_settles_actual_tokens_once():
    events = []
    repository = _GenerationRepository()

    process_generation_job(
        repository,
        _Planner(events),
        GENERATION_ID,
        now_factory=lambda: NOW,
        quota_service=_QuotaSpy(events),
    )

    settlements = [event for event in events if event[0] == "settle"]
    assert len(settlements) == 1
    assert settlements[0][2:] == (20, "provider_metadata")


def test_generation_missing_usage_settles_reserved_cap():
    events = []
    repository = _GenerationRepository()
    output = {
        "model_version": "test-model-v1",
        "cost": "0",
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_source": "reserved_cap_fallback",
        "proposals": [{
            "title": "Sign in",
            "priority": "P1",
            "steps": [{"action": "navigate", "url": "https://example.test/login"}],
            "assertions": [{"type": "url", "expected": "/login", "match": "contains"}],
        }],
    }

    process_generation_job(
        repository,
        _Planner(events, output=output),
        GENERATION_ID,
        now_factory=lambda: NOW,
        quota_service=_QuotaSpy(events),
    )

    settlement = next(event for event in events if event[0] == "settle")
    assert settlement[2:] == (4096, "reserved_cap_fallback")


def test_run_agent_reserves_before_provider_request():
    events = []
    result = _invoke_plan(_run_agent_provider(
        events, usage_metadata={"input_tokens": 7, "output_tokens": 5},
    ))

    assert result == {"action_ids": ["action-1"], "assertion_ids": ["assertion-1"]}
    assert [item[0] for item in events] == ["reserve", "provider", "settle"]


def test_provider_failure_releases_reservation():
    events = []
    provider = _run_agent_provider(events, error=RuntimeError("provider unavailable"))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _invoke_plan(provider)

    assert [item[0] for item in events] == ["reserve", "provider", "release"]


def test_provider_usage_settles_actual_tokens_once():
    events = []
    _invoke_plan(_run_agent_provider(
        events, usage_metadata={"input_tokens": 7, "output_tokens": 5},
    ))

    settlements = [event for event in events if event[0] == "settle"]
    assert len(settlements) == 1
    assert settlements[0][2:] == (12, "provider_metadata")


def test_replayed_run_agent_invocation_reuses_call_id_and_new_attempt_gets_a_new_id():
    events = []
    provider = _run_agent_provider(
        events, usage_metadata={"input_tokens": 7, "output_tokens": 5},
    )
    run_case_id = uuid4()

    _invoke_plan(provider, run_case_id)
    first_call_id = next(event[2] for event in events if event[0] == "reserve")
    _invoke_plan(provider, run_case_id)
    second_call_id = [event[2] for event in events if event[0] == "reserve"][1]
    provider._attempt_token = uuid4()
    _invoke_plan(provider, run_case_id)
    third_call_id = [event[2] for event in events if event[0] == "reserve"][2]

    assert first_call_id == second_call_id
    assert third_call_id != first_call_id


def test_missing_provider_usage_settles_reserved_cap():
    events = []
    _invoke_plan(_run_agent_provider(events))

    settlement = next(event for event in events if event[0] == "settle")
    assert settlement[2:] == (512, "reserved_cap_fallback")


def test_run_agent_quota_denial_does_not_call_provider():
    events = []
    provider = _run_agent_provider(
        events, usage_metadata={"input_tokens": 7, "output_tokens": 5}, deny=True,
    )

    with pytest.raises(QuotaExceeded):
        _invoke_plan(provider)

    assert [item[0] for item in events] == ["reserve"]


@pytest.mark.parametrize("denied_operation", ["plan", "explore"])
def test_agent_quota_denial_keeps_deterministic_case_status(denied_operation):
    case_id = str(uuid4())
    action_id = str(uuid4())
    assertion_id = str(uuid4())

    class Executor:
        current_case = {"schema_version": 2, "run_case_id": case_id}
        case_plans = {case_id: (
            None,
            [{"action_id": action_id, "action": "click", "locator": None}],
            [{"assert_id": assertion_id}],
        )}

        def prepare_case(self, _case):
            return {"action_count": 1, "assertion_count": 1}

        def consume_agent_budget(self):
            return True

        def observe_page(self):
            return {"page": "bounded"}

        def execute_step_node(self, _state):
            return {"current_step": 1, "case_status": "PASSED"}

        def assertion_node(self, _state):
            return {"case_status": "PASSED"}

    def planner(*_args):
        if denied_operation == "plan":
            raise QuotaExceeded
        return {"action_ids": [action_id], "assertion_ids": [assertion_id]}

    def explorer(*_args):
        raise QuotaExceeded

    executor = Executor()
    graph = build_run_graph(
        InMemorySaver(), executor, planner=planner, explorer=explorer,
    )
    result = graph.invoke({
        "schema_version": 2,
        "run_id": str(RUN_ID),
        "run_case_id": case_id,
        "current_step": 0,
        "budget_left": 5,
    }, config={"configurable": {
        "thread_id": str(uuid4()),
        "checkpoint_ns": f"m15-quota-{denied_operation}",
    }})

    assert result["case_status"] == "PASSED"
