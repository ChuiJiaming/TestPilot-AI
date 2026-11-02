from datetime import datetime, timezone
import hashlib
import hmac
from itertools import count
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import event, func, select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from apps.api.app.auth.lifecycle import SecretRefRecord
from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.cases.planning import OutboxRecord
from apps.api.app.integrations.persistence import (
    IntegrationRepository,
    WebhookDeliveryRecord,
)
from apps.api.app.integrations.routes import create_integration_router
from apps.api.app.runs.persistence import TestRunRecord as RunRecord
from apps.api.app.tenancy.persistence import OrganizationMembershipRecord, OrganizationRecord


class StaticResolver:
    def __init__(self, secret):
        self.secret = secret

    def resolve(self, secret_ref_id, organization_id):
        return self.secret


@pytest.fixture
def webhook_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ = RunRecord.__table__
    AuthBase.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    actor_id, organization_id, integration_id, secret_ref_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    outbox_ids = count(1)

    def assign_outbox_id(_mapper, _connection, target):
        if target.id is None:
            target.id = next(outbox_ids)

    event.listen(OutboxRecord, "before_insert", assign_outbox_id)
    with sessions.begin() as session:
        session.add(UserRecord(
            id=actor_id, email=f"{actor_id.hex}@webhook.example.test", password_hash="hash",
            display_name="Webhook Owner", platform_role="user", status="active",
            created_at=now, email_verified_at=now,
        ))
        session.add(OrganizationRecord(
            id=organization_id, name="Webhook Org", slug=f"webhook-{actor_id.hex[:12]}",
            created_at=now,
        ))
        session.add(OrganizationMembershipRecord(
            organization_id=organization_id, user_id=actor_id, role="org_admin",
            created_at=now,
        ))
        session.add(SecretRefRecord(
            id=secret_ref_id, organization_id=organization_id, provider="vault",
            external_key="integrations/github", rotated_at=None, created_at=now,
        ))

    integration = IntegrationRepository(sessions).create(
        actor_id, organization_id, kind="github", config={},
        secret_ref_id=secret_ref_id, enabled=True, now=now,
    )
    data = {
        "engine": engine, "sessions": sessions, "now": now,
        "actor_id": actor_id, "organization_id": organization_id,
        "integration_id": integration["id"], "secret_ref_id": secret_ref_id,
        "secret": b"vault-managed-webhook-secret",
        "repository": IntegrationRepository(sessions),
    }
    yield data
    event.remove(OutboxRecord, "before_insert", assign_outbox_id)
    engine.dispose()


def _request(body):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": "/api/v1/webhooks/github", "raw_path": b"/api/v1/webhooks/github",
        "query_string": b"", "headers": [], "server": ("test", 443),
        "client": ("127.0.0.1", 12345), "root_path": "",
    }, receive)


def _signed_headers(db, body, *, nonce="f" * 24):
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body
    signature = "sha256=" + hmac.new(db["secret"], message, hashlib.sha256).hexdigest()
    return timestamp, nonce, signature


def _endpoint(db, resolver=None):
    router = create_integration_router(
        integration_repository=db["repository"], auth_sessions=None,
        secret_ref_resolver=resolver,
    )
    return next(
        route.endpoint for route in router.routes
        if isinstance(route, APIRoute)
        and route.operation_id == "post__api_v1_webhooks__provider"
    )


def _call(endpoint, db, body, *, nonce="f" * 24, use_integration_id=True):
    timestamp, nonce, signature = _signed_headers(db, body, nonce=nonce)
    coroutine = endpoint(
        provider="github", request=_request(body),
        x_testpilot_timestamp=timestamp, x_testpilot_nonce=nonce,
        x_testpilot_signature=signature,
        x_testpilot_integration_id=db["integration_id"] if use_integration_id else None,
    )
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("Webhook endpoint yielded; this direct route test expects in-memory awaits only")


def test_webhook_route_persists_only_normalized_event_and_body_digest(webhook_db):
    body = json.dumps({
        "id": "evt-201", "type": "push", "token": "sensitive-provider-token",
        "ref": "refs/heads/main",
    }, separators=(",", ":")).encode()

    receipt = _call(_endpoint(webhook_db, StaticResolver(webhook_db["secret"])), webhook_db, body)

    assert receipt["status"] == "accepted"
    assert receipt["duplicate"] is False
    with webhook_db["sessions"]() as session:
        delivery = session.scalar(select(WebhookDeliveryRecord))
        outbox = session.scalar(select(OutboxRecord))
        serialized = json.dumps(outbox.payload)
        assert delivery.request_nonce == "f" * 24
        assert delivery.signed_at is not None
        assert outbox.payload["body_sha256"] == hashlib.sha256(body).hexdigest()
        assert outbox.payload["event"]["payload"] == {
            "id": "evt-201", "type": "push", "ref": "refs/heads/main"
        }
        assert "sensitive-provider-token" not in serialized
        assert body.decode() not in serialized


def test_replayed_nonce_is_rejected_without_a_second_outbox_event(webhook_db):
    body = b'{"id":"evt-202","type":"push"}'
    endpoint = _endpoint(webhook_db, StaticResolver(webhook_db["secret"]))
    _call(endpoint, webhook_db, body)

    with pytest.raises(HTTPException) as caught:
        _call(endpoint, webhook_db, body)

    assert caught.value.status_code == 409
    with webhook_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRecord)) == 1


def test_webhook_signature_failure_is_rejected_before_persistence(webhook_db):
    body = b'{"id":"evt-203"}'
    endpoint = _endpoint(webhook_db, StaticResolver(b"wrong-key"))

    with pytest.raises(HTTPException) as caught:
        _call(endpoint, webhook_db, body)

    assert caught.value.status_code == 401
    with webhook_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(WebhookDeliveryRecord)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxRecord)) == 0


def test_missing_secret_provider_fails_closed(webhook_db):
    with pytest.raises(HTTPException) as caught:
        _call(_endpoint(webhook_db), webhook_db, b'{"id":"evt-204"}')

    assert caught.value.status_code == 503


def test_webhook_route_rejects_oversize_body_before_lookup(webhook_db):
    with pytest.raises(HTTPException) as caught:
        _call(
            _endpoint(webhook_db, StaticResolver(webhook_db["secret"])),
            webhook_db, b"x" * (1024 * 1024 + 1),
        )

    assert caught.value.status_code == 413
    with webhook_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(WebhookDeliveryRecord)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxRecord)) == 0


def test_duplicate_provider_integrations_require_explicit_integration_id(webhook_db):
    other_org_id, other_secret_id = uuid4(), uuid4()
    with webhook_db["sessions"].begin() as session:
        session.add(OrganizationRecord(
            id=other_org_id, name="Second Webhook Org", slug=f"other-{other_org_id.hex[:12]}",
            created_at=webhook_db["now"],
        ))
        session.add(OrganizationMembershipRecord(
            organization_id=other_org_id, user_id=webhook_db["actor_id"],
            role="org_admin", created_at=webhook_db["now"],
        ))
        session.add(SecretRefRecord(
            id=other_secret_id, organization_id=other_org_id, provider="vault",
            external_key="integrations/github-second", rotated_at=None,
            created_at=webhook_db["now"],
        ))
    webhook_db["repository"].create(
        webhook_db["actor_id"], other_org_id, kind="github", config={},
        secret_ref_id=other_secret_id, enabled=True, now=webhook_db["now"],
    )

    with pytest.raises(HTTPException) as caught:
        _call(
            _endpoint(webhook_db, StaticResolver(webhook_db["secret"])),
            webhook_db, b'{"id":"evt-205"}', use_integration_id=False,
        )

    assert caught.value.status_code == 409
    with webhook_db["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRecord)) == 0


def test_webhook_operation_id_is_registered(webhook_db):
    router = create_integration_router(
        integration_repository=webhook_db["repository"], auth_sessions=None,
        secret_ref_resolver=StaticResolver(webhook_db["secret"]),
    )

    assert any(
        isinstance(route, APIRoute)
        and route.operation_id == "post__api_v1_webhooks__provider"
        for route in router.routes
    )
