"""Deployment-owned SecretRef resolution.

The API stores SecretRef IDs only. Deployments may inject a provider adapter;
without one, any operation that needs provider material fails closed.
"""

from typing import Protocol
from uuid import UUID


class SecretProviderUnavailable(RuntimeError):
    """Raised when the deployment cannot resolve a SecretRef safely."""


class SecretRefResolver(Protocol):
    def resolve(self, secret_ref_id: UUID, organization_id: UUID) -> bytes:
        """Resolve a SecretRef within the owning organization."""


class UnavailableSecretRefResolver:
    def resolve(self, secret_ref_id: UUID, organization_id: UUID) -> bytes:
        raise SecretProviderUnavailable("SECRET_PROVIDER_UNAVAILABLE")
