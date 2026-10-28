"""mTLS client and short-lived scoped job tokens for Browser Gateway actions."""

from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import jwt

from apps.api.app.environments.health_check import (
    HealthCheckPolicyError,
    validate_health_check_target,
)


class BrowserGatewayUnavailable(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BrowserGatewayExecutionClient:
    def __init__(self, *, base_url: str, client_cert: str, client_key: str,
                 ca_bundle: str, token_secret: str, timeout_seconds: float = 120.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Browser Gateway URL must be an HTTPS origin")
        self.base_url = base_url.rstrip("/")
        self.client_cert = client_cert
        self.client_key = client_key
        self.ca_bundle = ca_bundle
        self.token_secret = token_secret
        self.timeout_seconds = timeout_seconds
        self.session_context: dict[UUID, dict] = {}

    @classmethod
    def from_environment(cls):
        names = (
            "TESTPILOT_BROWSER_GATEWAY_URL",
            "TESTPILOT_BROWSER_GATEWAY_CLIENT_CERT",
            "TESTPILOT_BROWSER_GATEWAY_CLIENT_KEY",
            "TESTPILOT_BROWSER_GATEWAY_CA_CERT",
            "TESTPILOT_BROWSER_JOB_TOKEN_SECRET",
        )
        values = {name: os.getenv(name) for name in names}
        if not all(values.values()):
            return None
        return cls(
            base_url=values[names[0]], client_cert=values[names[1]],
            client_key=values[names[2]], ca_bundle=values[names[3]],
            token_secret=values[names[4]],
            timeout_seconds=float(os.getenv("TESTPILOT_BROWSER_GATEWAY_TIMEOUT_SECONDS", "120")),
        )

    def create_session(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
                       browser: str, attempt_token: UUID, snapshot: dict,
                       resume_only: bool = False,
                       worker_instance: str | None = None):
        token = self._job_token(
            run_id=run_id, run_case_id=run_case_id, browser=browser,
            attempt_token=attempt_token, snapshot=snapshot,
            worker_instance=worker_instance,
        )
        result = self._post("/internal/v1/browser-sessions/create", {
            "session_id": str(session_id), "run_id": str(run_id),
            "run_case_id": str(run_case_id), "browser": browser,
            "attempt_token": str(attempt_token), "job_token": token,
            "resume_only": resume_only,
            **({"worker_instance": worker_instance} if worker_instance else {}),
        })
        if result.get("status") != "READY":
            raise BrowserGatewayUnavailable(result.get("error_code", "BROWSER_WORKER_SESSION_FAILED"))
        if (result.get("session_id") != str(session_id)
                or not isinstance(result.get("worker_instance"), str)
                or not result["worker_instance"] or len(result["worker_instance"]) > 128):
            raise BrowserGatewayUnavailable("Browser Worker returned an invalid session response")
        self.session_context[session_id] = {
            "run_id": run_id, "run_case_id": run_case_id,
            "browser": browser, "attempt_token": attempt_token,
            "snapshot": snapshot, "worker_instance": result["worker_instance"],
        }
        return token, result["worker_instance"]

    def action(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
               browser: str, attempt_token: UUID, token: str, action: dict):
        fresh_token = self._refresh_token(
            session_id, run_id, run_case_id, browser, attempt_token, token)
        return self._post(f"/internal/v1/browser-sessions/{session_id}/actions", {
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser, "attempt_token": str(attempt_token),
            "job_token": fresh_token, "worker_instance": self.session_context[session_id]["worker_instance"],
            "action": action,
        })

    def assertion(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
                  browser: str, attempt_token: UUID, token: str, assertion: dict):
        fresh_token = self._refresh_token(
            session_id, run_id, run_case_id, browser, attempt_token, token)
        return self._post(f"/internal/v1/browser-sessions/{session_id}/assertions", {
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser, "attempt_token": str(attempt_token),
            "job_token": fresh_token, "worker_instance": self.session_context[session_id]["worker_instance"],
            "assertion": assertion,
        })

    def observe(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
                browser: str, attempt_token: UUID, token: str):
        fresh_token = self._refresh_token(
            session_id, run_id, run_case_id, browser, attempt_token, token)
        return self._post(f"/internal/v1/browser-sessions/{session_id}/observe", {
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser, "attempt_token": str(attempt_token),
            "job_token": fresh_token,
            "worker_instance": self.session_context[session_id]["worker_instance"],
        })

    def close_session(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
                      browser: str, attempt_token: UUID, token: str):
        fresh_token = self._refresh_token(
            session_id, run_id, run_case_id, browser, attempt_token, token)
        result = self._post(f"/internal/v1/browser-sessions/{session_id}/close", {
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser, "attempt_token": str(attempt_token),
            "job_token": fresh_token,
            "worker_instance": self.session_context[session_id]["worker_instance"],
        })
        self.session_context.pop(session_id, None)
        return result

    def _refresh_token(self, session_id, run_id, run_case_id, browser, attempt_token, fallback):
        context = self.session_context.get(session_id)
        if context is None:
            return fallback
        if (context["run_id"] != run_id or context["run_case_id"] != run_case_id
                or context["browser"] != browser or context["attempt_token"] != attempt_token):
            raise BrowserGatewayUnavailable("BROWSER_SESSION_SCOPE_MISMATCH")
        return self._job_token(
            run_id=run_id, run_case_id=run_case_id, browser=browser,
            attempt_token=attempt_token, snapshot=context["snapshot"],
            worker_instance=context["worker_instance"],
        )

    def _job_token(self, *, run_id, run_case_id, browser, attempt_token, snapshot,
                   worker_instance: str | None = None):
        if snapshot.get("secret_ref_version_id") is not None:
            raise BrowserGatewayUnavailable("SECRET_PROVIDER_UNAVAILABLE")
        base_url = snapshot.get("base_url")
        parsed = urlsplit(base_url or "")
        if parsed.username or parsed.password or parsed.fragment:
            raise BrowserGatewayUnavailable("ENVIRONMENT_NETWORK_POLICY_BLOCKED")
        network_policy = snapshot.get("network_policy") or {}
        try:
            _, allowed_domains = validate_health_check_target(base_url, network_policy)
        except (HealthCheckPolicyError, TypeError) as exc:
            raise BrowserGatewayUnavailable("ENVIRONMENT_NETWORK_POLICY_BLOCKED") from exc
        now = datetime.now(timezone.utc)
        claims = {
            "iss": "testpilot-agent-worker",
            "aud": "testpilot-browser-gateway",
            "sub": str(run_id),
            "run_id": str(run_id),
            "run_case_id": str(run_case_id),
            "browser": browser,
            "attempt_token": str(attempt_token),
            "base_url": base_url,
            "allowed_domains": allowed_domains,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": str(uuid4()),
        }
        if worker_instance:
            claims["worker_instance"] = worker_instance
        return jwt.encode(claims, self.token_secret, algorithm="HS256")

    def _post(self, path: str, payload: dict):
        try:
            response = httpx.post(
                self.base_url + path, json=payload,
                cert=(self.client_cert, self.client_key), verify=self.ca_bundle,
                timeout=self.timeout_seconds, follow_redirects=False,
            )
            if response.status_code != 200:
                code = "BROWSER_WORKER_REJECTED"
                try:
                    body = response.json()
                    if isinstance(body, dict) and isinstance(body.get("error"), str):
                        code = body["error"][:80]
                except ValueError:
                    pass
                raise BrowserGatewayUnavailable(code)
            result = response.json()
            if not isinstance(result, dict):
                raise BrowserGatewayUnavailable("BROWSER_WORKER_INVALID_RESPONSE")
            return result
        except BrowserGatewayUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserGatewayUnavailable("BROWSER_GATEWAY_UNAVAILABLE") from exc