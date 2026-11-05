"""Effective model configuration: approved platform settings first, env fallback.

The API key itself is never stored. A setting may only name the environment
variable that holds it, so an approved configuration change can be audited
without ever moving a secret through the database, the API or the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from apps.api.app.auth.persistence import UserRecord
from apps.api.app.operations.persistence import SystemSettingRecord

MODEL_CONFIG_KEY = "llm_config"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "TESTPILOT_LLM_API_KEY"

_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,64}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CONFIG_KEYS = {"base_url", "api_key_env", "planner_model", "run_agent_model", "pricing"}
_RATE_KEYS = {"input_usd_per_million", "output_usd_per_million"}
_MAX_MODELS = 50


class ModelConfigError(ValueError):
    """Raised when a model configuration would be unusable at call time."""


def _validate_base_url(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ModelConfigError("base_url must be a non-empty URL")
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ModelConfigError("base_url must be an http(s) URL without credentials, query or fragment")
    if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
        raise ModelConfigError("base_url must use HTTPS except for loopback endpoints")
    return candidate


def _validate_api_key_env(value) -> str:
    if not isinstance(value, str) or not _ENV_NAME_PATTERN.match(value.strip()):
        raise ModelConfigError(
            "api_key_env must be an upper-case environment variable name, never the key itself")
    return value.strip()


def _validate_model_name(value, *, label: str) -> str:
    if not isinstance(value, str) or not _MODEL_NAME_PATTERN.match(value.strip()):
        raise ModelConfigError(f"{label} must be a provider model identifier")
    return value.strip()


def _validate_rate(value, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelConfigError(f"{label} must be a number")
    rate = float(value)
    if not math.isfinite(rate) or rate < 0:
        raise ModelConfigError(f"{label} must be finite and non-negative")
    return rate


def validate_pricing(value) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict) or not value:
        raise ModelConfigError("pricing must map at least one model to its rates")
    if len(value) > _MAX_MODELS:
        raise ModelConfigError(f"pricing may define at most {_MAX_MODELS} models")
    pricing: dict[str, dict[str, float]] = {}
    for model, rates in value.items():
        name = _validate_model_name(model, label="pricing model")
        if not isinstance(rates, dict) or set(rates) != _RATE_KEYS:
            raise ModelConfigError(
                "each pricing entry needs exactly input_usd_per_million and output_usd_per_million")
        pricing[name] = {
            "input_usd_per_million": _validate_rate(
                rates["input_usd_per_million"], label="input_usd_per_million"),
            "output_usd_per_million": _validate_rate(
                rates["output_usd_per_million"], label="output_usd_per_million"),
        }
    return pricing


def parse_model_config(value) -> dict:
    """Validate an ``llm_config`` setting value and return its normalized form."""
    if not isinstance(value, dict):
        raise ModelConfigError("llm_config must be an object")
    if set(value) != _CONFIG_KEYS:
        raise ModelConfigError(
            "llm_config must contain exactly base_url, api_key_env, planner_model, "
            "run_agent_model and pricing")
    pricing = validate_pricing(value["pricing"])
    planner_model = _validate_model_name(value["planner_model"], label="planner_model")
    run_agent_model = _validate_model_name(value["run_agent_model"], label="run_agent_model")
    for model in (planner_model, run_agent_model):
        if model not in pricing:
            raise ModelConfigError("every configured model must have a pricing entry")
    return {
        "base_url": _validate_base_url(value["base_url"]),
        "api_key_env": _validate_api_key_env(value["api_key_env"]),
        "planner_model": planner_model,
        "run_agent_model": run_agent_model,
        "pricing": pricing,
    }


def _json_pricing(raw: str | None) -> dict[str, dict[str, float]]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ModelConfigError("pricing JSON must be an object")
        return {
            str(model): {
                "input_usd_per_million": _validate_rate(
                    rate.get("input_usd_per_million") if isinstance(rate, dict) else None,
                    label="input_usd_per_million"),
                "output_usd_per_million": _validate_rate(
                    rate.get("output_usd_per_million") if isinstance(rate, dict) else None,
                    label="output_usd_per_million"),
            }
            for model, rate in parsed.items()
        }
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise ModelConfigError("pricing JSON has an invalid model rate map") from exc


@dataclass
class ResolvedModelConfig:
    """The configuration a worker will actually use, plus why it is not ready."""

    base_url: str
    api_key_env: str
    api_key: str
    planner_model: str
    run_agent_model: str
    pricing: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    raw_pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    source: str = "default"
    updated_at: datetime | None = None
    issues: list[dict[str, str]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.issues

    def rates_for(self, model: str) -> dict[str, Decimal]:
        rates = self.pricing.get(model)
        if rates is None:
            raise ModelConfigError("no cost rates are configured for the requested model")
        return rates

    def to_view(self) -> dict:
        return {
            "source": self.source,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(self.api_key),
            "planner_model": self.planner_model,
            "run_agent_model": self.run_agent_model,
            "pricing": [
                {"model": model, **rates}
                for model, rates in sorted(self.raw_pricing.items())
            ],
            "ready": self.ready,
            "issues": list(self.issues),
            "updated_at": self.updated_at,
        }


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _decimal_pricing(raw: dict[str, dict[str, float]]) -> dict[str, dict[str, Decimal]]:
    return {
        model: {
            "input_usd_per_million": Decimal(str(rates["input_usd_per_million"])),
            "output_usd_per_million": Decimal(str(rates["output_usd_per_million"])),
        }
        for model, rates in raw.items()
    }


def _from_stored(value) -> dict | None:
    try:
        return parse_model_config(value)
    except ModelConfigError:
        return None


def load_stored_config(session_factory) -> tuple[dict | None, datetime | None]:
    """Read the approved ``llm_config`` row, ignoring values that no longer validate."""
    if session_factory is None:
        return None, None
    try:
        with session_factory() as session:
            row = session.scalar(select(SystemSettingRecord).where(
                SystemSettingRecord.key == MODEL_CONFIG_KEY))
    except Exception:  # a missing table must not break worker startup
        return None, None
    if row is None:
        return None, None
    return _from_stored(row.value), row.updated_at


def resolve_model_config(session_factory=None, *, environ=None) -> ResolvedModelConfig:
    """Resolve the effective model configuration for a worker call."""
    env = os.environ if environ is None else environ
    stored, updated_at = load_stored_config(session_factory)
    issues: list[dict[str, str]] = []

    if stored is not None:
        source = "database"
        base_url = stored["base_url"]
        api_key_env = stored["api_key_env"]
        planner_model = stored["planner_model"]
        run_agent_model = stored["run_agent_model"]
        raw_pricing = stored["pricing"]
    else:
        source = "environment" if (
            env.get("TESTPILOT_LLM_PRICING_JSON") or env.get("TESTPILOT_RUN_AGENT_MODEL")
        ) else "default"
        base_url = (env.get("TESTPILOT_LLM_BASE_URL") or DEFAULT_BASE_URL).strip()
        api_key_env = (env.get("TESTPILOT_LLM_API_KEY_ENV") or DEFAULT_API_KEY_ENV).strip()
        planner_model = (env.get("TESTPILOT_PLANNER_MODEL") or "").strip()
        run_agent_model = (env.get("TESTPILOT_RUN_AGENT_MODEL") or "").strip()
        try:
            raw_pricing = _json_pricing(env.get("TESTPILOT_LLM_PRICING_JSON"))
        except ModelConfigError:
            raw_pricing = {}
            issues.append(_issue(
                "PRICING_INVALID",
                "TESTPILOT_LLM_PRICING_JSON 无法解析，请改为在平台设置中保存模型定价。",
            ))

    try:
        base_url = _validate_base_url(base_url)
    except ModelConfigError as exc:
        issues.append(_issue("BASE_URL_INVALID", str(exc)))
    try:
        api_key_env = _validate_api_key_env(api_key_env)
    except ModelConfigError as exc:
        issues.append(_issue("API_KEY_ENV_INVALID", str(exc)))
        api_key_env = DEFAULT_API_KEY_ENV

    api_key = env.get(api_key_env) or (env.get("OPENAI_API_KEY") if api_key_env == DEFAULT_API_KEY_ENV else "")
    if not api_key:
        issues.append(_issue(
            "API_KEY_MISSING",
            f"环境变量 {api_key_env} 未设置，模型调用会被拒绝。",
        ))
    if not raw_pricing:
        issues.append(_issue("PRICING_MISSING", "尚未配置任何模型定价。"))
    if not run_agent_model:
        issues.append(_issue("RUN_AGENT_MODEL_MISSING", "尚未指定 Run Agent 使用的模型。"))
    elif raw_pricing and run_agent_model not in raw_pricing:
        issues.append(_issue(
            "RUN_AGENT_MODEL_UNPRICED",
            f"Run Agent 模型 {run_agent_model} 不在定价表中。",
        ))
    if planner_model and raw_pricing and planner_model not in raw_pricing:
        issues.append(_issue(
            "PLANNER_MODEL_UNPRICED",
            f"场景生成模型 {planner_model} 不在定价表中。",
        ))

    return ResolvedModelConfig(
        base_url=base_url,
        api_key_env=api_key_env,
        api_key=api_key or "",
        planner_model=planner_model,
        run_agent_model=run_agent_model,
        pricing=_decimal_pricing(raw_pricing),
        raw_pricing=raw_pricing,
        source=source,
        updated_at=updated_at,
        issues=issues,
    )


def model_options(config: ResolvedModelConfig) -> dict:
    """Public model list for the generation form; rates and endpoint stay private."""
    names = sorted(config.raw_pricing)
    default = config.planner_model or (names[0] if names else "")
    return {"items": names, "default": default}


def probe_model_config(config: ResolvedModelConfig, *, model: str | None = None,
                       timeout: float = 12.0) -> dict:
    """Send one bounded chat completion to prove the endpoint, key and model work."""
    import time
    from urllib.parse import urljoin

    import httpx

    target = (model or config.run_agent_model or config.planner_model or "").strip()
    if not config.api_key:
        return {"ok": False, "model": target, "latency_ms": 0,
                "error_code": "API_KEY_MISSING", "message": "API Key 环境变量未设置。"}
    if not target:
        return {"ok": False, "model": target, "latency_ms": 0,
                "error_code": "MODEL_NOT_CONFIGURED", "message": "尚未指定要测试的模型。"}
    url = urljoin(config.base_url.rstrip("/") + "/", "chat/completions")
    payload = {"model": target, "max_tokens": 8, "temperature": 0,
               "messages": [{"role": "user", "content": "ping"}]}
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {config.api_key}"},
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "model": target,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error_code": "PROVIDER_UNREACHABLE",
                "message": type(exc).__name__}
    latency = int((time.monotonic() - started) * 1000)
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = str((body.get("error") or {}).get("message") or "")[:200]
        except (ValueError, AttributeError):
            detail = ""
        return {"ok": False, "model": target, "latency_ms": latency,
                "error_code": f"PROVIDER_HTTP_{response.status_code}",
                "message": detail or "上游返回错误状态。"}
    try:
        body = response.json()
    except ValueError as exc:
        raise ModelConfigError("provider response was not JSON") from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        return {"ok": False, "model": target, "latency_ms": latency,
                "error_code": "PROVIDER_RESPONSE_INVALID",
                "message": "上游响应缺少 choices。"}
    return {"ok": True, "model": target, "latency_ms": latency,
            "error_code": "", "message": "连接成功。"}


class ModelConfigService:
    """Read and probe the effective model configuration for platform administrators."""

    def __init__(self, session_factory, *, environ=None):
        self.session_factory = session_factory
        self.environ = environ

    def _require_platform_admin(self, actor_id) -> None:
        from apps.api.app.operations.settings import SettingForbidden

        if self.session_factory is None:
            raise SettingForbidden("platform administrator access required")
        with self.session_factory() as session:
            user = session.get(UserRecord, actor_id)
        if user is None or user.platform_role != "platform_admin":
            raise SettingForbidden("platform administrator access required")

    def view(self, actor_id) -> dict:
        self._require_platform_admin(actor_id)
        return resolve_model_config(self.session_factory, environ=self.environ).to_view()

    def options(self) -> dict:
        """Model names only; no endpoint, rates or key material."""
        return model_options(resolve_model_config(self.session_factory, environ=self.environ))

    def probe(self, actor_id, *, model: str | None = None) -> dict:
        self._require_platform_admin(actor_id)
        config = resolve_model_config(self.session_factory, environ=self.environ)
        return probe_model_config(config, model=model)
