"""Budget-gated, structured Planner and Explorer provider for Run execution."""

import json
import os
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field


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
