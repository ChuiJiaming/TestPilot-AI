"""mTLS client and short-lived scoped job tokens for Browser Gateway actions."""

from datetime import datetime, timedelta, timezone
import base64
import binascii
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

    def screenshot(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
                   browser: str, attempt_token: UUID, token: str):
        """Fetch one bounded, redacted PNG through the worker-affine Gateway route."""
        if session_id not in self.session_context:
            raise BrowserGatewayUnavailable("BROWSER_SESSION_SCOPE_MISMATCH")
        fresh_token = self._refresh_token(
            session_id, run_id, run_case_id, browser, attempt_token, token)
        result = self._post(f"/internal/v1/browser-sessions/{session_id}/screenshot", {
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser, "attempt_token": str(attempt_token),
            "job_token": fresh_token,
            "worker_instance": self.session_context[session_id]["worker_instance"],
        })
        encoded = result.get("image_base64")
        size_bytes = result.get("size_bytes")
        viewport = result.get("viewport")
        dpr = result.get("device_scale_factor")
        color_scheme = result.get("color_scheme")
        if (result.get("content_type") != "image/png" or result.get("redacted") is not True
                or not isinstance(encoded, str) or len(encoded) > 11_184_812
                or type(size_bytes) is not int or not 1 <= size_bytes <= 8 * 1024 * 1024
                or not isinstance(viewport, dict)
                or type(viewport.get("width")) is not int
                or type(viewport.get("height")) is not int
                or viewport["width"] < 1 or viewport["height"] < 1
                or viewport["width"] * viewport["height"] > 32_000_000
                or type(dpr) not in {int, float} or not 0.5 <= dpr <= 4
                or color_scheme not in {"light", "dark"}):
            raise BrowserGatewayUnavailable("BROWSER_WORKER_INVALID_SCREENSHOT")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BrowserGatewayUnavailable("BROWSER_WORKER_INVALID_SCREENSHOT") from exc
        if (len(image) != size_bytes or len(image) > 8 * 1024 * 1024
                or not image.startswith(b"\x89PNG\r\n\x1a\n")):
            raise BrowserGatewayUnavailable("BROWSER_WORKER_INVALID_SCREENSHOT")
        return {
            "image": image,
            "viewport": viewport,
            "device_scale_factor": float(dpr),
            "color_scheme": color_scheme,
            "redacted": True,
        }

    def verify_locator(self, *, session_id: UUID, run_id: UUID, run_case_id: UUID,
                       browser: str, attempt_token: UUID, token: str, locator: dict):
        if session_id not in self.session_context:
            raise BrowserGatewayUnavailable("BROWSER_SESSION_SCOPE_MISMATCH")
        fresh_token = self._refresh_token(
            session_id, run_id, run_case_id, browser, attempt_token, token)
        result = self._post(
            f"/internal/v1/browser-sessions/{session_id}/locators/verify", {
                "run_id": str(run_id), "run_case_id": str(run_case_id),
                "browser": browser, "attempt_token": str(attempt_token),
                "job_token": fresh_token,
                "worker_instance": self.session_context[session_id]["worker_instance"],
                "locator": locator,
            },
        )
        if (result.get("status") not in {"VERIFIED", "UNVERIFIED"}
                or type(result.get("match_count")) is not int
                or type(result.get("visible")) is not bool
                or not isinstance(result.get("descriptor"), dict)):
            raise BrowserGatewayUnavailable("BROWSER_WORKER_INVALID_RESPONSE")
        return result

    def rebind_session(self, *, healing_id: UUID, previous_attempt_token: UUID,
                       attempt_token: UUID, session_id: UUID, run_id: UUID,
                       run_case_id: UUID, browser: str, snapshot: dict,
                       worker_instance: str):
        """Consume an approved one-time grant to continue the original Worker page."""
        if (not worker_instance or len(worker_instance) > 128
                or previous_attempt_token == attempt_token):
            raise BrowserGatewayUnavailable("BROWSER_SESSION_SCOPE_MISMATCH")
        grant = {
            "healing_id": str(healing_id), "session_id": str(session_id),
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser,
            "previous_attempt_token": str(previous_attempt_token),
            "attempt_token": str(attempt_token),
            "worker_instance": worker_instance,
        }
        job_token = self._job_token(
            run_id=run_id, run_case_id=run_case_id, browser=browser,
            attempt_token=attempt_token, snapshot=snapshot,
            worker_instance=worker_instance, recovery_handoff=grant,
        )
        result = self._post(f"/internal/v1/browser-sessions/{session_id}/rebind", {
            "run_id": str(run_id), "run_case_id": str(run_case_id),
            "browser": browser, "attempt_token": str(attempt_token),
            "job_token": job_token, "worker_instance": worker_instance,
        })
        if result.get("status") != "READY":
            raise BrowserGatewayUnavailable(
                result.get("error_code", "SESSION_NOT_RESTORABLE"))
        if (result.get("session_id") != str(session_id)
                or result.get("worker_instance") != worker_instance
                or result.get("rebound") is not True):
            raise BrowserGatewayUnavailable("BROWSER_WORKER_INVALID_RESPONSE")
        self.session_context[session_id] = {
            "run_id": run_id, "run_case_id": run_case_id,
            "browser": browser, "attempt_token": attempt_token,
            "snapshot": snapshot, "worker_instance": worker_instance,
        }
        return {"job_token": job_token, "worker_instance": worker_instance,
                "session_id": session_id, "status": "READY"}

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
                   worker_instance: str | None = None,
                   recovery_handoff: dict | None = None):
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
        if recovery_handoff is not None:
            claims["recovery_handoff"] = recovery_handoff
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
