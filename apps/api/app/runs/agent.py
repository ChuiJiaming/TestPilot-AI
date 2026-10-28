"""LangGraph run graph and fenced Celery execution orchestration."""

from datetime import datetime, timezone
from threading import Event, Thread
from typing import TypedDict
from uuid import UUID
import os

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, StateGraph

from apps.api.app.runs.browser import (
    BrowserGatewayExecutionClient,
    BrowserGatewayUnavailable,
)
from apps.api.app.runs.executor import DeterministicExecutor
from apps.api.app.runs.planner import RunAgentProvider
from apps.api.app.runs.repository import RunRepository, StaleRunAttempt


class RunGraphState(TypedDict, total=False):
    schema_version: int
    run_id: str
    run_case_id: str
    environment_snapshot_id: str
    current_step: int
    retry_count: int
    budget_left: int
    last_completed_action_id: str | None
    attempt_token: str
    action_count: int
    assertion_count: int
    exploration_status: str
    case_status: str
    planner_status: str


def build_run_graph(checkpointer, executor, planner=None, explorer=None):
    """Compile a checkpointed graph whose only executable actions come from published DSL."""

    def plan_node(state: RunGraphState):
        case = executor.current_case
        summary = executor.prepare_case(case)
        if planner is None:
            return {**summary, "schema_version": case.get("schema_version", 1),
                    "planner_status": "NOT_CONFIGURED"}
        budget_left = state.get("budget_left", 0)
        if budget_left <= 0:
            return {**summary, "schema_version": case.get("schema_version", 1),
                    "budget_left": 0, "planner_status": "BUDGET_EXHAUSTED"}
        _, steps, assertions = executor.case_plans[case["run_case_id"]]
        expected_actions = [str(item["action_id"]) for item in steps]
        expected_assertions = [str(item["assert_id"]) for item in assertions]
        if not executor.consume_agent_budget():
            return {**summary, "budget_left": 0, "planner_status": "BUDGET_EXHAUSTED"}
        proposal = planner(state, case, summary, expected_actions, expected_assertions)
        if (not isinstance(proposal, dict) or set(proposal) != {"action_ids", "assertion_ids"}
                or proposal.get("action_ids") != expected_actions
                or proposal.get("assertion_ids") != expected_assertions):
            raise ValueError("Planner proposal must preserve the published action and assertion order")
        return {**summary, "schema_version": case.get("schema_version", 1),
                "budget_left": budget_left - 1, "planner_status": "VALIDATED"}

    def explore_node(state: RunGraphState):
        if explorer is None:
            return executor.explorer_node(state)
        budget_left = state.get("budget_left", 0)
        if budget_left <= 0:
            return {"exploration_status": "BUDGET_EXHAUSTED", "budget_left": 0}
        case = executor.current_case
        _, steps, _ = executor.case_plans[case["run_case_id"]]
        index = state.get("current_step", 0)
        if index >= len(steps):
            return {"exploration_status": "COMPLETE"}
        step = steps[index]
        published_step = {"action_id": str(step["action_id"]),
                          "action": step["action"], "locator": step["locator"]}
        observation = executor.observe_page()
        if not executor.consume_agent_budget():
            return {"exploration_status": "BUDGET_EXHAUSTED", "budget_left": 0}
        proposal = explorer(state, case, published_step, observation)
        if (not isinstance(proposal, dict) or set(proposal) != {"action_id", "locator"}
                or proposal.get("action_id") != str(step["action_id"])
                or proposal.get("locator") != step["locator"]):
            raise ValueError("Explorer proposal must match the next published action and locator")
        return {"exploration_status": "VALIDATED", "budget_left": budget_left - 1}

    def execute_node(state: RunGraphState):
        return executor.execute_step_node(state)

    def assertion_node(state: RunGraphState):
        if state.get("case_status") in {
            "PAUSED", "CANCELLED", "WAITING_APPROVAL", "ERROR",
        }:
            return {}
        return executor.assertion_node(state)

    def route(state: RunGraphState):
        status = state.get("case_status")
        if status in {"PAUSED", "CANCELLED", "WAITING_APPROVAL", "FAILED", "ERROR"}:
            return "end"
        if state.get("current_step", 0) < state.get("action_count", 0):
            return "execute"
        return "assertions"

    graph = StateGraph(RunGraphState)
    graph.add_node("planner", plan_node)
    graph.add_node("explorer", explore_node)
    graph.add_node("execute_step", execute_node)
    graph.add_node("assertions", assertion_node)
    graph.set_entry_point("planner")
    graph.add_edge("planner", "explorer")
    graph.add_edge("explorer", "execute_step")
    graph.add_conditional_edges(
        "execute_step", route,
        {"execute": "explorer", "assertions": "assertions", "end": END},
    )
    graph.add_edge("assertions", END)
    return graph.compile(checkpointer=checkpointer)


class _RunHeartbeat:
    def __init__(self, repository: RunRepository, run_id: UUID,
                 attempt_token: UUID, worker_id: str):
        self.repository = repository
        self.run_id = run_id
        self.attempt_token = attempt_token
        self.worker_id = worker_id
        self.stop_event = Event()
        self.lost_event = Event()
        self.thread = Thread(target=self._loop, name=f"run-heartbeat-{run_id}", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _loop(self):
        while not self.stop_event.wait(20):
            try:
                alive = self.repository.heartbeat(
                    self.run_id, self.attempt_token, worker_id=self.worker_id,
                    now=datetime.now(timezone.utc), lease_seconds=60,
                )
            except Exception:
                alive = False
            if not alive:
                self.lost_event.set()
                return


def _checkpoint_conninfo():
    value = os.getenv("TESTPILOT_DATABASE_URL")
    if not value:
        raise RuntimeError("Set TESTPILOT_DATABASE_URL for the PostgreSQL LangGraph checkpointer")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def execute_admitted_run(session_factory, *, run_id: UUID,
                         attempt_token: UUID, worker_id: str):
    repository = RunRepository(session_factory)
    bundle = repository.load_execution_bundle(run_id, attempt_token, worker_id=worker_id)
    status = repository.execution_status(run_id, attempt_token, worker_id=worker_id)
    if status == "PREPARING":
        repository.transition(
            run_id, "RUNNING", expected_status="PREPARING",
            now=datetime.now(timezone.utc), attempt_token=attempt_token,
            worker_id=worker_id,
        )
    elif status != "RUNNING":
        return {"run_id": run_id, "status": status}

    gateway = BrowserGatewayExecutionClient.from_environment()
    if gateway is None:
        raise RuntimeError("BROWSER_GATEWAY_NOT_CONFIGURED")

    configured_budget = bundle["policy"].get("agent_budget", 0)
    configured_budget = min(1000, max(0, configured_budget if type(configured_budget) is int else 0))
    remaining_budget = bundle["policy"].get("agent_budget_remaining", configured_budget)
    remaining_budget = min(configured_budget, max(0, remaining_budget if type(remaining_budget) is int else 0))
    agent_provider = RunAgentProvider.from_environment() if remaining_budget else None
    if remaining_budget and agent_provider is None:
        raise RuntimeError("RUN_AGENT_PROVIDER_NOT_CONFIGURED")
    heartbeat = _RunHeartbeat(repository, run_id, attempt_token, worker_id)
    heartbeat.start()
    case_results = []
    executor = DeterministicExecutor(
        repository, gateway, run_id=run_id, attempt_token=attempt_token,
        worker_id=worker_id, snapshot=bundle["snapshot"], policy=bundle["policy"],
    )
    try:
        with PostgresSaver.from_conn_string(_checkpoint_conninfo()) as checkpointer:
            checkpointer.setup()
            graph = build_run_graph(
                checkpointer, executor,
                planner=agent_provider.plan if agent_provider else None,
                explorer=agent_provider.explore if agent_provider else None,
            )
            for case in bundle["cases"]:
                if case["status"] in {"PASSED", "FAILED", "ERROR", "CANCELLED"}:
                    case_results.append(case["status"])
                    continue
                if heartbeat.lost_event.is_set():
                    raise StaleRunAttempt
                current_status = repository.execution_status(
                    run_id, attempt_token, worker_id=worker_id)
                if current_status in {"CANCELLING", "PAUSING"}:
                    target = "CANCELLED" if current_status == "CANCELLING" else "PAUSED"
                    repository.transition(
                        run_id, target, expected_status=current_status,
                        now=datetime.now(timezone.utc), attempt_token=attempt_token,
                        worker_id=worker_id,
                    )
                    return {"run_id": run_id, "status": target}
                repository.set_run_case_state(
                    case["run_case_id"], "RUNNING", attempt_token=attempt_token,
                    worker_id=worker_id, now=datetime.now(timezone.utc),
                )
                session_claim = repository.create_browser_session(
                    case["run_case_id"], browser=case["browser"],
                    attempt_token=attempt_token, worker_id=worker_id,
                    now=datetime.now(timezone.utc),
                )
                session_id = session_claim["id"]
                token = None
                case_status = None
                try:
                    previous_worker = session_claim.get("worker_instance")
                    resume_only = bool(session_claim["existing"]
                                       and previous_worker not in {None, "", "pending"})
                    token, browser_worker_instance = gateway.create_session(
                        session_id=session_id, run_id=run_id,
                        run_case_id=case["run_case_id"], browser=case["browser"],
                        attempt_token=attempt_token, snapshot=bundle["snapshot"],
                        resume_only=resume_only,
                        worker_instance=previous_worker if resume_only else None,
                    )
                    repository.set_browser_session_status(
                        session_id, "READY", attempt_token=attempt_token,
                        worker_id=worker_id, now=datetime.now(timezone.utc),
                        worker_instance=browser_worker_instance,
                    )
                    executor.bind_browser_session(session_id, token)
                    executor.current_case = case
                    budget_left = remaining_budget
                    initial: RunGraphState = {
                        "schema_version": case.get("schema_version", 1),
                        "run_id": str(run_id),
                        "run_case_id": str(case["run_case_id"]),
                        "environment_snapshot_id": str(bundle["snapshot"]["id"]),
                        "current_step": 0,
                        "retry_count": 0,
                        "budget_left": budget_left,
                        "last_completed_action_id": None,
                        "attempt_token": str(attempt_token),
                        "case_status": "RUNNING",
                    }
                    config = {"configurable": {
                        "thread_id": str(run_id),
                        "checkpoint_ns": f"run-case:{case['run_case_id']}:attempt:{attempt_token}",
                    }}
                    result = graph.invoke(initial, config=config)
                    remaining_budget = min(
                        remaining_budget,
                        max(0, result.get("budget_left", remaining_budget)),
                    )
                    case_status = result.get("case_status", "ERROR")
                except BrowserGatewayUnavailable as exc:
                    repository.set_browser_session_status(
                        session_id, "LOST", attempt_token=attempt_token,
                        worker_id=worker_id, now=datetime.now(timezone.utc),
                    )
                    if exc.code != "SESSION_NOT_RESTORABLE":
                        raise
                    repository.transition(
                        run_id, "WAITING_APPROVAL",
                        expected_status={"PREPARING", "RUNNING"},
                        now=datetime.now(timezone.utc),
                        payload={"reason_code": "BROWSER_SESSION_NOT_RESTORABLE"},
                        attempt_token=attempt_token, worker_id=worker_id,
                    )
                    return {"run_id": run_id, "status": "WAITING_APPROVAL"}
                finally:
                    if token is not None and case_status != "PAUSED":
                        try:
                            repository.set_browser_session_status(
                                session_id, "CLOSING", attempt_token=attempt_token,
                                worker_id=worker_id, now=datetime.now(timezone.utc),
                            )
                            gateway.close_session(
                                session_id=session_id, run_id=run_id,
                                run_case_id=case["run_case_id"], browser=case["browser"],
                                attempt_token=attempt_token, token=token,
                            )
                            repository.set_browser_session_status(
                                session_id, "CLOSED", attempt_token=attempt_token,
                                worker_id=worker_id, now=datetime.now(timezone.utc),
                            )
                        except Exception:
                            try:
                                repository.set_browser_session_status(
                                    session_id, "LOST", attempt_token=attempt_token,
                                    worker_id=worker_id, now=datetime.now(timezone.utc),
                                )
                            except Exception:
                                pass

                if case_status in {"PAUSED", "CANCELLED", "WAITING_APPROVAL"}:
                    return {"run_id": run_id, "status": case_status}
                if case_status not in {"PASSED", "FAILED", "ERROR"}:
                    case_status = "ERROR"
                repository.set_run_case_state(
                    case["run_case_id"], case_status, attempt_token=attempt_token,
                    worker_id=worker_id, now=datetime.now(timezone.utc),
                )
                case_results.append(case_status)
                current_status = repository.execution_status(
                    run_id, attempt_token, worker_id=worker_id)
                if current_status == "PAUSING":
                    repository.transition(
                        run_id, "PAUSED", expected_status="PAUSING",
                        now=datetime.now(timezone.utc), attempt_token=attempt_token,
                        worker_id=worker_id,
                    )
                    return {"run_id": run_id, "status": "PAUSED"}
                if current_status == "CANCELLING":
                    repository.transition(
                        run_id, "CANCELLED", expected_status="CANCELLING",
                        now=datetime.now(timezone.utc), attempt_token=attempt_token,
                        worker_id=worker_id,
                    )
                    return {"run_id": run_id, "status": "CANCELLED"}
                if case_status == "ERROR":
                    break

        if heartbeat.lost_event.is_set():
            raise StaleRunAttempt
        final_status = (
            "ERROR" if "ERROR" in case_results
            else "FAILED" if "FAILED" in case_results
            else "PASSED"
        )
        repository.transition(
            run_id, "FINALIZING", expected_status="RUNNING",
            now=datetime.now(timezone.utc), attempt_token=attempt_token,
            worker_id=worker_id,
            payload={"run_case_count": len(case_results)},
        )
        repository.transition(
            run_id, final_status, expected_status="FINALIZING",
            now=datetime.now(timezone.utc), attempt_token=attempt_token,
            worker_id=worker_id,
            payload={"passed_run_cases": case_results.count("PASSED"),
                     "failed_run_cases": case_results.count("FAILED"),
                     "error_run_cases": case_results.count("ERROR")},
        )
        return {"run_id": run_id, "status": final_status, "run_cases": case_results}
    finally:
        heartbeat.stop()