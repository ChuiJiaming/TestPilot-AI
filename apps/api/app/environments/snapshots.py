import hashlib
import json
from copy import deepcopy
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EnvironmentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment_id: UUID
    project_id: UUID
    base_url: str
    network_policy: dict[str, object]
    secret_ref_version_id: UUID | None
    snapshot_hash: str


def build_environment_snapshot(
    *,
    environment_id: UUID,
    project_id: UUID,
    base_url: str,
    network_policy: dict[str, object],
    secret_ref_version_id: UUID | None,
) -> EnvironmentSnapshot:
    """Build a stable snapshot identity using secret references, never secret values."""
    snapshot_fields = {
        "environment_id": str(environment_id),
        "project_id": str(project_id),
        "base_url": base_url,
        "network_policy": network_policy,
        "secret_ref_version_id": (
            str(secret_ref_version_id) if secret_ref_version_id is not None else None
        ),
    }
    canonical = json.dumps(
        snapshot_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return EnvironmentSnapshot(
        environment_id=environment_id,
        project_id=project_id,
        base_url=base_url,
        network_policy=deepcopy(network_policy),
        secret_ref_version_id=secret_ref_version_id,
        snapshot_hash=snapshot_hash,
    )
