"""Budget-gated, structured Planner and Explorer provider for Run execution."""

import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.quality.analyzer import FailureAnalysisProposal
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
    MAX_PROVIDER_TOKENS = 512

    def __init__(self, *, model_name: str, api_key: str, base_url: str,
                 quota_service=None, organization_id: UUID | None = None,
                 run_id: UUID | None = None, attempt_token: UUID | None = None):
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
        self.planner_model = base_model.with_structured_output(PlanProposal, include_raw=True)
        self.explorer_model = base_model.with_structured_output(ExplorerProposal, include_raw=True)
        self.recovery_model = base_model.with_structured_output(RecoveryProposal, include_raw=True)
        self.analysis_model = base_model.with_structured_output(FailureAnalysisProposal, include_raw=True)
        self._quota_service = quota_service
        self._organization_id = organization_id
        self._run_id = run_id
        self._attempt_token = attempt_token
        self._max_tokens = self.MAX_PROVIDER_TOKENS

    @classmethod
    def from_environment(cls, *, quota_service=None, organization_id=None,
                         run_id=None, attempt_token=None):
        from apps.api.app.operations.model_config import resolve_model_config

        return cls.from_model_config(
            resolve_model_config(), quota_service=quota_service,
            organization_id=organization_id, run_id=run_id,
            attempt_token=attempt_token,
        )

    @classmethod
    def from_model_config(cls, config, *, quota_service=None, organization_id=None,
                          run_id=None, attempt_token=None):
        model_name = (config.run_agent_model or "").strip()
        api_key = config.api_key
        if not model_name or not api_key:
            return None
        return cls(
            model_name=model_name, api_key=api_key, base_url=config.base_url,
            quota_service=quota_service, organization_id=organization_id,
            run_id=run_id, attempt_token=attempt_token,
        )

    @staticmethod
    def _usage_tokens(response):
        raw = response.get("raw") if isinstance(response, dict) and "raw" in response else None
        if raw is None:
            return None
        usage = getattr(raw, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
        if input_tokens is None or output_tokens is None:
            metadata = getattr(raw, "response_metadata", None) or {}
            token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
            input_tokens = token_usage.get("prompt_tokens", token_usage.get("input_tokens"))
            output_tokens = token_usage.get("completion_tokens", token_usage.get("output_tokens"))
        if (type(input_tokens) is not int or input_tokens < 0
                or type(output_tokens) is not int or output_tokens < 0):
            return None
        return input_tokens + output_tokens

    def _call_id(self, operation, state, run_case_id, discriminator):
        run_id = getattr(self, "_run_id", None) or state.get("run_id")
        attempt_token = getattr(self, "_attempt_token", None)
        if run_id is None or attempt_token is None:
            raise RuntimeError("RUN_AGENT_QUOTA_SCOPE_MISSING")
        detail = json.dumps(discriminator, sort_keys=True, separators=(",", ":"), default=str)
        return uuid5(NAMESPACE_URL, ":".join((
            "testpilot-run-agent", str(run_id), str(attempt_token), str(run_case_id),
            operation, str(state.get("budget_left", "none")), detail,
        )))

    def _invoke_structured(self, operation, model, messages, *, schema, state,
                           run_case_id, discriminator=None):
        service = getattr(self, "_quota_service", None)
        call_id = None
        max_tokens = getattr(self, "_max_tokens", self.MAX_PROVIDER_TOKENS)
        if service is not None:
            organization_id = getattr(self, "_organization_id", None)
            run_id = getattr(self, "_run_id", None)
            if organization_id is None or run_id is None or run_case_id is None:
                raise RuntimeError("RUN_AGENT_QUOTA_SCOPE_MISSING")
            call_id = self._call_id(operation, state, run_case_id, discriminator)
            service.reserve(
                organization_id, call_id=call_id, run_id=run_id,
                requested_tokens=max_tokens, now=datetime.now(timezone.utc),
            )
        try:
            response = model.invoke(messages)
            if isinstance(response, dict) and "raw" in response:
                parsing_error = response.get("parsing_error")
                if parsing_error is not None:
                    raise ValueError("model response did not conform to the structured schema") from parsing_error
                parsed = response.get("parsed")
            else:
                parsed = response
            proposal = schema.model_validate(parsed)
            usage_tokens = self._usage_tokens(response)
        except Exception:
            if service is not None:
                service.release(call_id, now=datetime.now(timezone.utc))
            raise
        if service is not None:
            usage_source = "provider_metadata" if usage_tokens is not None else "reserved_cap_fallback"
            service.settle(
                call_id,
                consumed_tokens=usage_tokens if usage_tokens is not None else max_tokens,
                usage_source=usage_source,
                now=datetime.now(timezone.utc),
            )
        return proposal

    def plan(self, state, case, summary, action_ids, assertion_ids):
        proposal = self._invoke_structured("plan", self.planner_model, [
            ("system", "Validate the published Run plan. Return exactly the supplied action_ids and assertion_ids in the same order. Never add, remove, reorder, or rewrite any action or assertion. The Run engine remains the only authority for execution and outcomes."),
            ("human", json.dumps({
                "schema_version": case.get("schema_version"),
                "action_count": summary["action_count"],
                "assertion_count": summary["assertion_count"],
                "action_ids": action_ids,
                "assertion_ids": assertion_ids,
            }, ensure_ascii=False)),
        ], schema=PlanProposal, state=state, run_case_id=case["run_case_id"],
            discriminator={"action_ids": action_ids, "assertion_ids": assertion_ids})
        return proposal.model_dump()

    def explore(self, state, case, step, observation):
        proposal = self._invoke_structured("explore", self.explorer_model, [
            ("system", "Inspect the bounded page observation. Return the exact supplied action_id and locator only when they remain the published next action. Do not invent, replace, or edit actions, locators, values, or assertions."),
            ("human", json.dumps({
                "schema_version": case.get("schema_version"),
                "published_next_action": step,
                "page_observation": observation,
            }, ensure_ascii=False)),
        ], schema=ExplorerProposal, state=state, run_case_id=case["run_case_id"],
            discriminator={"action_id": step.get("action_id"),
                           "current_step": state.get("current_step")})
        return proposal.model_dump()

    def recover(self, state, case, failed_step, observation) -> RecoveryProposal:
        """Suggest a locator for the failed published action, without editing its DSL."""
        proposal = self._invoke_structured("recover", self.recovery_model, [
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
        ], schema=RecoveryProposal, state=state, run_case_id=case.get("run_case_id") or state.get("run_case_id"),
            discriminator={"action_id": failed_step.get("action_id"),
                           "healing_id": state.get("healing_id")})
        result = proposal
        if str(result.action_id) != str(failed_step["action_id"]):
            raise ValueError("Recovery proposal must target the failed published action")
        return result

    def analyze_failure(self, state, failure, evidence_ids):
        """Classify bounded execution metadata without page text or secret values."""
        proposal = self._invoke_structured("analyze", self.analysis_model, [
            ("system", "Classify this failed web test using only the supplied deterministic "
             "failure fields and evidence IDs. Return one supported classification, a short "
             "redacted title, a tentative hypothesis, confidence, and only evidence IDs from "
             "the supplied list. Never claim or set a confirmed root cause. Do not infer from "
             "page text, form values, credentials, or screenshots."),
            ("human", json.dumps({
                "run_case_id": str(state.get("run_case_id", "")),
                "failure": {
                    "action_type": failure.get("action_type"),
                    "failure_phase": failure.get("failure_phase"),
                    "error_code": failure.get("error_code"),
                    "browser": failure.get("browser"),
                },
                "allowed_evidence_ids": list(evidence_ids),
            }, ensure_ascii=True)),
        ], schema=FailureAnalysisProposal, state=state,
            run_case_id=state.get("run_case_id"),
            discriminator={"evidence_ids": list(evidence_ids)})
        return proposal.model_dump()
