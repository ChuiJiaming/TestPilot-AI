"""OpenAI-compatible LangChain adapter used by the isolated Agent Worker."""

import json
import os
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
        base_url = os.getenv("TESTPILOT_LLM_BASE_URL", "https://api.openai.com/v1")
        api_key = os.getenv("TESTPILOT_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        raw_pricing = os.getenv("TESTPILOT_LLM_PRICING_JSON", "")
        if not raw_pricing:
            raise ValueError("TESTPILOT_LLM_PRICING_JSON must define provider rates per model")
        try:
            parsed = json.loads(raw_pricing)
            if not isinstance(parsed, dict):
                raise ValueError("pricing JSON must be an object")
            pricing = {
                model: {
                    "input_usd_per_million": Decimal(str(rate["input_usd_per_million"])),
                    "output_usd_per_million": Decimal(str(rate["output_usd_per_million"])),
                }
                for model, rate in parsed.items()
            }
            if any(not value.is_finite() or value < 0
                   for rates in pricing.values() for value in rates.values()):
                raise ValueError("model pricing rates must be finite and non-negative")
        except (json.JSONDecodeError, KeyError, TypeError, ArithmeticError) as exc:
            raise ValueError("TESTPILOT_LLM_PRICING_JSON has an invalid model rate map") from exc
        if not pricing:
            raise ValueError("TESTPILOT_LLM_PRICING_JSON must contain at least one model")
        return cls(base_url=base_url, api_key=api_key, pricing=pricing)

    def generate(self, *, requirement: str, model_name: str) -> PlannerOutput:
        from langchain_openai import ChatOpenAI

        rates = self.pricing.get(model_name)
        if rates is None:
            raise ValueError("no cost rates are configured for the requested model")
        model = ChatOpenAI(model=model_name, api_key=self.api_key, base_url=self.base_url,
                           timeout=90, max_retries=0)
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
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise ValueError("provider response omitted reliable token usage; cost cannot be recorded")

        cost = (
            Decimal(input_tokens) * rates["input_usd_per_million"]
            + Decimal(output_tokens) * rates["output_usd_per_million"]
        ) / Decimal(1_000_000)
        metadata = getattr(raw, "response_metadata", None) or {}
        actual_model = metadata.get("model_name") or metadata.get("model") or model_name
        return PlannerOutput(model_version=str(actual_model), cost=cost,
            input_tokens=input_tokens, output_tokens=output_tokens,
            proposals=parsed.proposals)
