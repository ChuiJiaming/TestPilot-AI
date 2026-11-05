"""Deployment-owned SecretRef resolution.

The API stores SecretRef metadata only (provider, external_key, versions). This
module is the other half: it turns a SecretRef ID into the actual bytes at the
moment of use, inside the owning organization.

Two rules are non-negotiable:

1. **Tenant scope is checked here, not by the caller.** A resolver that trusts
   the caller's organization ID would turn an IDOR into a cross-tenant secret
   read. The lookup always filters on both ``id`` and ``organization_id``.
2. **Fail closed.** A missing backend, a missing key, or an unknown provider
   raises ``SecretProviderUnavailable``. There is deliberately no "empty secret"
   fallback: an integration that cannot authenticate must 503, not silently
   call the provider with a blank credential.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from apps.api.app.auth.lifecycle import SecretRefRecord

logger = logging.getLogger("testpilot.secrets")


class SecretProviderUnavailable(RuntimeError):
    """Raised when the deployment cannot resolve a SecretRef safely."""


class SecretRefResolver(Protocol):
    def resolve(self, secret_ref_id: UUID, organization_id: UUID) -> bytes:
        """Resolve a SecretRef within the owning organization."""


class UnavailableSecretRefResolver:
    """Default when no backend is configured: every resolution fails closed."""

    def resolve(self, secret_ref_id: UUID, organization_id: UUID) -> bytes:
        raise SecretProviderUnavailable("SECRET_PROVIDER_UNAVAILABLE")


class EnvironmentSecretBackend:
    """Reads secret material from the process environment or a mounted JSON file.

    Two supported layouts, both keyed by ``"<provider>:<external_key>"``:

    * ``TESTPILOT_SECRETREFS_JSON`` — a JSON object in the environment.
    * ``TESTPILOT_SECRETREFS_FILE`` — an absolute path to a JSON file, intended
      for a read-only secret mount (Docker secret / Kubernetes volume). The file
      is re-read on every resolution so a rotated mount takes effect without a
      process restart.

    Values may be raw strings or ``{"value": "..."}`` objects. Anything else is
    rejected loudly rather than coerced.
    """

    def __init__(self, *, inline_json: str | None = None, file_path: str | None = None):
        if not inline_json and not file_path:
            raise ValueError(
                "a SecretRef backend needs TESTPILOT_SECRETREFS_JSON or TESTPILOT_SECRETREFS_FILE"
            )
        self.inline_json = inline_json
        self.file_path = file_path

    @classmethod
    def from_environment(cls) -> "EnvironmentSecretBackend | None":
        inline = os.getenv("TESTPILOT_SECRETREFS_JSON") or None
        file_path = os.getenv("TESTPILOT_SECRETREFS_FILE") or None
        if not inline and not file_path:
            return None
        return cls(inline_json=inline, file_path=file_path)

    def _load(self) -> dict:
        raw = None
        if self.file_path:
            try:
                raw = Path(self.file_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise SecretProviderUnavailable("SECRET_BACKEND_UNREADABLE") from exc
        elif self.inline_json:
            raw = self.inline_json
        if not raw:
            raise SecretProviderUnavailable("SECRET_BACKEND_EMPTY")
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretProviderUnavailable("SECRET_BACKEND_INVALID") from exc
        if not isinstance(values, dict):
            raise SecretProviderUnavailable("SECRET_BACKEND_INVALID")
        return values

    def get(self, provider: str, external_key: str) -> bytes:
        entry = self._load().get(f"{provider}:{external_key}")
        if entry is None:
            raise SecretProviderUnavailable("SECRET_NOT_FOUND")
        if isinstance(entry, dict):
            entry = entry.get("value")
        if not isinstance(entry, str) or not entry:
            raise SecretProviderUnavailable("SECRET_VALUE_INVALID")
        return entry.encode("utf-8")


class DatabaseSecretRefResolver:
    """Resolves SecretRef IDs against the secret_refs table, then the backend."""

    def __init__(self, session_factory, backend: EnvironmentSecretBackend):
        self.session_factory = session_factory
        self.backend = backend

    def resolve(self, secret_ref_id: UUID, organization_id: UUID) -> bytes:
        with self.session_factory() as session:
            ref = session.scalar(
                select(SecretRefRecord).where(
                    SecretRefRecord.id == secret_ref_id,
                    # Tenant scope is part of the lookup, not a post-check.
                    SecretRefRecord.organization_id == organization_id,
                )
            )
            if ref is None:
                raise SecretProviderUnavailable("SECRET_REF_NOT_FOUND")
            provider, external_key = ref.provider, ref.external_key
        try:
            return self.backend.get(provider, external_key)
        except SecretProviderUnavailable:
            logger.warning(
                "SecretRef resolution failed",
                extra={"provider": provider, "secret_ref_id": str(secret_ref_id)},
            )
            raise


def from_environment(session_factory) -> SecretRefResolver:
    """Composition helper: a real resolver when a backend exists, else fail closed."""
    backend = EnvironmentSecretBackend.from_environment()
    if backend is None or session_factory is None:
        return UnavailableSecretRefResolver()
    return DatabaseSecretRefResolver(session_factory, backend)
