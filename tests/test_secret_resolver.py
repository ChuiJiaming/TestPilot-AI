"""SecretRef resolution: tenant scope, fail-closed behaviour, mounted rotation."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.lifecycle import SecretRefRecord
from apps.api.app.auth.persistence import AuthBase
from apps.api.app.integrations.secrets import (
    DatabaseSecretRefResolver,
    EnvironmentSecretBackend,
    SecretProviderUnavailable,
    UnavailableSecretRefResolver,
    from_environment,
)


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuthBase.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _insert_ref(session_factory, *, organization_id, provider="vault", external_key="ci/github"):
    secret_ref_id = uuid4()
    with session_factory.begin() as session:
        session.add(
            SecretRefRecord(
                id=secret_ref_id,
                organization_id=organization_id,
                provider=provider,
                external_key=external_key,
                rotated_at=None,
                created_at=datetime.now(timezone.utc),
            )
        )
    return secret_ref_id


def test_backend_reads_inline_json_values():
    backend = EnvironmentSecretBackend(inline_json='{"vault:ci/github": "s3cret"}')
    assert backend.get("vault", "ci/github") == b"s3cret"


def test_backend_accepts_wrapped_value_objects():
    backend = EnvironmentSecretBackend(inline_json='{"vault:key": {"value": "wrapped"}}')
    assert backend.get("vault", "key") == b"wrapped"


def test_backend_fails_closed_on_missing_or_malformed_material():
    with pytest.raises(SecretProviderUnavailable):
        EnvironmentSecretBackend(inline_json="{}").get("vault", "absent")
    with pytest.raises(SecretProviderUnavailable):
        EnvironmentSecretBackend(inline_json="{not json").get("vault", "any")
    with pytest.raises(SecretProviderUnavailable):
        EnvironmentSecretBackend(inline_json='{"vault:blank": ""}').get("vault", "blank")
    with pytest.raises(SecretProviderUnavailable):
        EnvironmentSecretBackend(inline_json='{"vault:num": 3}').get("vault", "num")


def test_backend_rereads_a_mounted_file_so_rotation_needs_no_restart(tmp_path):
    path = tmp_path / "secretrefs.json"
    path.write_text('{"vault:rotating": "v1"}', encoding="utf-8")
    backend = EnvironmentSecretBackend(file_path=str(path))
    assert backend.get("vault", "rotating") == b"v1"
    path.write_text('{"vault:rotating": "v2"}', encoding="utf-8")
    assert backend.get("vault", "rotating") == b"v2"


def test_resolver_enforces_tenant_scope_not_just_id_presence():
    sessions = _session_factory()
    organization_id = uuid4()
    secret_ref_id = _insert_ref(sessions, organization_id=organization_id)
    resolver = DatabaseSecretRefResolver(
        sessions, EnvironmentSecretBackend(inline_json='{"vault:ci/github": "scoped"}')
    )

    assert resolver.resolve(secret_ref_id, organization_id) == b"scoped"

    # Same secret ref id, different tenant: must not leak.
    with pytest.raises(SecretProviderUnavailable):
        resolver.resolve(secret_ref_id, uuid4())


def test_resolver_reports_unknown_ids_without_leaking_which_part_failed():
    sessions = _session_factory()
    resolver = DatabaseSecretRefResolver(
        sessions, EnvironmentSecretBackend(inline_json='{"vault:ci/github": "scoped"}')
    )
    with pytest.raises(SecretProviderUnavailable):
        resolver.resolve(uuid4(), uuid4())


def test_composition_falls_back_to_fail_closed_without_a_backend(monkeypatch):
    monkeypatch.delenv("TESTPILOT_SECRETREFS_JSON", raising=False)
    monkeypatch.delenv("TESTPILOT_SECRETREFS_FILE", raising=False)
    resolver = from_environment(_session_factory())
    assert isinstance(resolver, UnavailableSecretRefResolver)
    with pytest.raises(SecretProviderUnavailable):
        resolver.resolve(uuid4(), uuid4())


def test_composition_builds_a_real_resolver_when_a_backend_is_configured(monkeypatch):
    monkeypatch.setenv("TESTPILOT_SECRETREFS_JSON", '{"vault:ci/github": "from-env"}')
    sessions = _session_factory()
    organization_id = uuid4()
    secret_ref_id = _insert_ref(sessions, organization_id=organization_id)
    resolver = from_environment(sessions)
    assert isinstance(resolver, DatabaseSecretRefResolver)
    assert resolver.resolve(secret_ref_id, organization_id) == b"from-env"
