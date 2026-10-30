"""Budget-gated, structured Planner and Explorer provider for Run execution."""

import json
import os
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.runs.recovery import RecoveryProposal


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_ids: list[str] = Field(max_length=500)
    assertion_ids: list[str] = Field(max_length=500)


class ExplorerProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    locator: dict | None


class RunAgentProvider:
    def __init__(self, *, model_name: str, api_key: str, base_url: str):
        from langchain_openai import ChatOpenAI

        parsed = urlsplit(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and parsed.hostname not in local_hosts)):
            raise ValueError("model base URL must use HTTPS except for loopback development endpoints")
        if not model_name or not api_key:
            raise ValueError("Run Agent model and API key are required")
        base_model = ChatOpenAI(
            model=model_name, api_key=api_key, base_url=base_url,
            temperature=0, timeout=30, max_retries=0, max_tokens=512,
        )
        self.planner_model = base_model.with_structured_output(PlanProposal)
        self.explorer_model = base_model.with_structured_output(ExplorerProposal)
        self.recovery_model = base_model.with_structured_output(RecoveryProposal)

    @classmethod
    def from_environment(cls):
        model_name = (os.getenv("TESTPILOT_RUN_AGENT_MODEL") or "").strip()
        api_key = os.getenv("TESTPILOT_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        if not model_name or not api_key:
            return None
        return cls(
            model_name=model_name, api_key=api_key,
            base_url=os.getenv("TESTPILOT_LLM_BASE_URL", "https://api.openai.com/v1"),
        )

    def plan(self, state, case, summary, action_ids, assertion_ids):
        proposal = self.planner_model.invoke([
            ("system", "Validate the published Run plan. Return exactly the supplied action_ids and assertion_ids in the same order. Never add, remove, reorder, or rewrite any action or assertion. The Run engine remains the only authority for execution and outcomes."),
            ("human", json.dumps({
                "schema_version": case.get("schema_version"),
                "action_count": summary["action_count"],
                "assertion_count": summary["assertion_count"],
                "action_ids": action_ids,
                "assertion_ids": assertion_ids,
            }, ensure_ascii=False)),
        ])
        return proposal.model_dump()

    def explore(self, state, case, step, observation):
        proposal = self.explorer_model.invoke([
            ("system", "Inspect the bounded page observation. Return the exact supplied action_id and locator only when they remain the published next action. Do not invent, replace, or edit actions, locators, values, or assertions."),
            ("human", json.dumps({
                "schema_version": case.get("schema_version"),
                "published_next_action": step,
                "page_observation": observation,
            }, ensure_ascii=False)),
        ])
        return proposal.model_dump()

    def recover(self, state, case, failed_step, observation) -> RecoveryProposal:
        """Suggest a locator for the failed published action, without editing its DSL."""
        proposal = self.recovery_model.invoke([
            ("system", "Propose only a replacement locator for the one supplied failed action. "
             "Return its exact action_id, a DSL v2 locator, source, confidence from 0 to 1, "
             "and a semantic_descriptor copied from the observed target. Never change the "
             "action, its value, action order, or any assertion. The deterministic Worker "
             "verification and a separate human approval remain mandatory; confidence "
             "does not authorize a replay."),
            ("human", json.dumps({
                "schema_version": case.get("schema_version"),
                "budget_left": state.get("budget_left", 0),
                "failed_action": {
                    "action_id": str(failed_step["action_id"]),
                    "action": failed_step.get("action"),
                    "locator": failed_step.get("locator"),
                },
                "page_observation": observation,
            }, ensure_ascii=False)),
        ])
        result = RecoveryProposal.model_validate(proposal)
        if str(result.action_id) != str(failed_step["action_id"]):
            raise ValueError("Recovery proposal must target the failed published action")
        return result
