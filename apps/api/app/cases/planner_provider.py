"""OpenAI-compatible LangChain adapter used by the isolated Agent Worker."""

from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.cases.planning import PlannerOutput, ScenarioDraft


class PlannerCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposals: list[ScenarioDraft] = Field(min_length=1)


class OpenAICompatibleScenarioPlanner:
    def __init__(self, *, base_url: str, api_key: str, pricing: dict[str, dict[str, Decimal]]):
        parsed_url = urlsplit(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname
            or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment
            or (parsed_url.scheme != "https" and parsed_url.hostname not in local_hosts)):
            raise ValueError("model base URL must use HTTPS except for loopback development endpoints")
        if not api_key:
            raise ValueError("model API key is required")
        self.base_url = base_url
        self.api_key = api_key
        self.pricing = pricing

    @classmethod
    def from_environment(cls):
        from apps.api.app.operations.model_config import resolve_model_config

        return cls.from_model_config(resolve_model_config())

    @classmethod
    def from_model_config(cls, config):
        """Build the planner from the resolved platform model configuration."""
        if not config.pricing:
            raise ValueError("TESTPILOT_LLM_PRICING_JSON must define provider rates per model")
        return cls(base_url=config.base_url, api_key=config.api_key, pricing=config.pricing)

    def generate(self, *, requirement: str, model_name: str) -> PlannerOutput:
        from langchain_openai import ChatOpenAI

        rates = self.pricing.get(model_name)
        if rates is None:
            raise ValueError("no cost rates are configured for the requested model")
        model = ChatOpenAI(model=model_name, api_key=self.api_key, base_url=self.base_url,
                           timeout=90, max_retries=0, max_tokens=4096)
        structured_model = model.with_structured_output(PlannerCompletion, include_raw=True)
        response = structured_model.invoke([
            ("system", "Create proposed browser test scenarios. Use only the supported DSL action and assertion schema. Never claim a test passed; test outcomes are determined by deterministic assertions, not by the model. Return only structured proposals."),
            ("human", requirement),
        ])
        raw = response.get("raw") if isinstance(response, dict) else None
        parsed = response.get("parsed") if isinstance(response, dict) else None
        parsing_error = response.get("parsing_error") if isinstance(response, dict) else None
        if parsing_error is not None:
            raise ValueError("model response did not conform to the scenario schema") from parsing_error
        if not isinstance(parsed, PlannerCompletion) or raw is None:
            raise ValueError("model response did not include structured output and usage metadata")

        usage: Any = getattr(raw, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if input_tokens is None or output_tokens is None:
            details = getattr(raw, "response_metadata", None) or {}
            token_usage = details.get("token_usage") or details.get("usage") or {}
            input_tokens = token_usage.get("prompt_tokens", token_usage.get("input_tokens"))
            output_tokens = token_usage.get("completion_tokens", token_usage.get("output_tokens"))
        usage_source = "provider_metadata"
        if (type(input_tokens) is not int or input_tokens < 0
                or type(output_tokens) is not int or output_tokens < 0):
            input_tokens = 0
            output_tokens = 0
            cost = Decimal(0)
            usage_source = "reserved_cap_fallback"
        else:
            cost = (
                Decimal(input_tokens) * rates["input_usd_per_million"]
                + Decimal(output_tokens) * rates["output_usd_per_million"]
            ) / Decimal(1_000_000)
        metadata = getattr(raw, "response_metadata", None) or {}
        actual_model = metadata.get("model_name") or metadata.get("model") or model_name
        return PlannerOutput(model_version=str(actual_model), cost=cost,
            input_tokens=input_tokens, output_tokens=output_tokens,
            usage_source=usage_source, proposals=parsed.proposals)
