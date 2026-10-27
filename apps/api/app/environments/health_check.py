"""Safe, gateway-mediated environment health checks.

The API validates tenant scope and the configured domain allowlist, then sends
only environment metadata to the internal mTLS Browser Gateway. It never opens
a connection to the environment URL itself.
"""

from ipaddress import ip_address
from datetime import datetime
import os
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthCheckStatus:
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    values = {HEALTHY, UNHEALTHY, BLOCKED, ERROR}


class HealthCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: str
    checked_at: datetime
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = Field(default=None, max_length=80)

    @field_validator("status")
    @classmethod
    def supported_status(cls, value: str) -> str:
        if value not in HealthCheckStatus.values:
            raise ValueError("unsupported health-check status")
        return value


class HealthCheckRequest(BaseModel):
    """Keep the V10.3 optional name field as a display label only."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)


class HealthCheckGatewayUnavailable(Exception):
    pass


class HealthCheckPolicyError(Exception):
    pass


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_health_check_target(base_url: str, network_policy: dict) -> tuple[str, list[str]]:
    """Require exact FQDN allowlisting; wildcard and IP allowlist entries are denied."""
    try:
        parsed = urlsplit(base_url)
        hostname_raw = parsed.hostname
        hostname = hostname_raw.rstrip(".").encode("idna").decode("ascii").lower() if hostname_raw else ""
    except (ValueError, UnicodeError) as exc:
        raise HealthCheckPolicyError("INVALID_ENVIRONMENT_URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.fragment:
        raise HealthCheckPolicyError("INVALID_ENVIRONMENT_URL")
    try:
        expected_port = 443 if parsed.scheme == "https" else 80
        if parsed.port is not None and parsed.port != expected_port:
            raise HealthCheckPolicyError("NON_DEFAULT_PORT_BLOCKED")
    except ValueError as exc:
        raise HealthCheckPolicyError("INVALID_ENVIRONMENT_URL") from exc
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        raise HealthCheckPolicyError("IP_LITERAL_TARGET_BLOCKED")

    raw_domains = network_policy.get("allowed_domains")
    if not isinstance(raw_domains, list) or not raw_domains:
        raise HealthCheckPolicyError("DOMAIN_ALLOWLIST_REQUIRED")

    allowed: list[str] = []
    for candidate in raw_domains:
        if not isinstance(candidate, str):
            raise HealthCheckPolicyError("INVALID_DOMAIN_ALLOWLIST")
        try:
            domain = candidate.strip().rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise HealthCheckPolicyError("INVALID_DOMAIN_ALLOWLIST") from exc
        if ("*" in domain or ":" in domain or "/" in domain or "@" in domain
                or not domain or any(not _DOMAIN_LABEL.fullmatch(label) for label in domain.split("."))):
            raise HealthCheckPolicyError("INVALID_DOMAIN_ALLOWLIST")
        try:
            ip_address(domain)
        except ValueError:
            pass
        else:
            raise HealthCheckPolicyError("IP_LITERAL_ALLOWLIST_BLOCKED")
        allowed.append(domain)

    allowed = sorted(set(allowed))
    if hostname not in allowed:
        raise HealthCheckPolicyError("DOMAIN_NOT_ALLOWLISTED")
    return hostname, allowed


class BrowserGatewayHealthClient:
    """mTLS client for the internal Browser Gateway health-check endpoint."""

    def __init__(self, *, base_url: str, client_cert: str, client_key: str,
                 ca_bundle: str, timeout_seconds: float = 8.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Browser Gateway URL must be an HTTPS origin")
        self.endpoint = base_url.rstrip("/") + "/internal/v1/health-checks"
        self.client_cert = client_cert
        self.client_key = client_key
        self.ca_bundle = ca_bundle
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls):
        names = (
            "TESTPILOT_BROWSER_GATEWAY_URL",
            "TESTPILOT_BROWSER_GATEWAY_CLIENT_CERT",
            "TESTPILOT_BROWSER_GATEWAY_CLIENT_KEY",
            "TESTPILOT_BROWSER_GATEWAY_CA_CERT",
        )
        values = {name: os.getenv(name) for name in names}
        if not all(values.values()):
            return None
        return cls(
            base_url=values[names[0]], client_cert=values[names[1]],
            client_key=values[names[2]], ca_bundle=values[names[3]],
            timeout_seconds=float(os.getenv("TESTPILOT_BROWSER_GATEWAY_TIMEOUT_SECONDS", "8")),
        )

    def check(self, *, environment_id: UUID, base_url: str, network_policy: dict,
              allowed_domains: list[str], secret_ref_id: UUID | None,
              secret_ref_version_id: UUID | None, check_name: str | None) -> HealthCheckResult:
        payload = {
            "environment_id": str(environment_id),
            "base_url": base_url,
            "network_policy": network_policy,
            "allowed_domains": allowed_domains,
            "secret_ref_id": str(secret_ref_id) if secret_ref_id else None,
            "secret_ref_version_id": str(secret_ref_version_id) if secret_ref_version_id else None,
            "check_name": check_name,
            "redirect_policy": "deny",
            "resolve_and_pin_dns": True,
        }
        try:
            response = httpx.post(
                self.endpoint, json=payload,
                cert=(self.client_cert, self.client_key), verify=self.ca_bundle,
                timeout=self.timeout_seconds, follow_redirects=False,
            )
            if response.status_code != 200:
                raise HealthCheckGatewayUnavailable("Browser Gateway rejected or failed the health check")
            result = HealthCheckResult.model_validate(response.json())
            if result.id != environment_id:
                raise HealthCheckGatewayUnavailable("Browser Gateway returned a mismatched environment ID")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise HealthCheckGatewayUnavailable("Browser Gateway is unavailable or returned an invalid result") from exc
