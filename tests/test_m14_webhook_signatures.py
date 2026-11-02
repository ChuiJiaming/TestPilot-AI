from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

import pytest

from apps.api.app.integrations.webhooks import (
    WebhookVerificationError,
    verify_inbound_webhook,
)


def _signature(body, timestamp, nonce, secret):
    signing_bytes = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body
    return "sha256=" + hmac.new(secret, signing_bytes, hashlib.sha256).hexdigest()


def _body(**payload):
    return json.dumps(payload, separators=(",", ":")).encode()


def test_valid_signature_accepts_exact_raw_body_and_redacts_secret_fields():
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    timestamp = str(int(now.timestamp()))
    nonce = "a" * 24
    secret = b"deployment-webhook-key"
    raw_body = _body(id="evt-101", type="push", token="must-not-be-stored", ref="main")

    verified = verify_inbound_webhook(
        raw_body, timestamp=timestamp, nonce=nonce,
        signature=_signature(raw_body, timestamp, nonce, secret),
        secret=secret, now=now,
    )

    assert verified.nonce == nonce
    assert verified.event_id == "evt-101"
    assert verified.body_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert verified.payload == {"id": "evt-101", "type": "push", "ref": "main"}


def test_body_change_invalidates_signature():
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    timestamp, nonce, secret = str(int(now.timestamp())), "b" * 24, b"key"
    original = _body(id="evt-102", ref="main")

    with pytest.raises(WebhookVerificationError, match="SIGNATURE"):
        verify_inbound_webhook(
            original + b" ", timestamp=timestamp, nonce=nonce,
            signature=_signature(original, timestamp, nonce, secret),
            secret=secret, now=now,
        )


@pytest.mark.parametrize("offset", [-301, 301])
def test_stale_and_future_timestamps_are_rejected(offset):
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    timestamp = str(int((now + timedelta(seconds=offset)).timestamp()))
    nonce, secret, raw_body = "c" * 24, b"key", _body(id="evt-103")

    with pytest.raises(WebhookVerificationError, match="TIMESTAMP"):
        verify_inbound_webhook(
            raw_body, timestamp=timestamp, nonce=nonce,
            signature=_signature(raw_body, timestamp, nonce, secret),
            secret=secret, now=now,
        )


def test_malformed_signature_is_rejected():
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(WebhookVerificationError, match="SIGNATURE"):
        verify_inbound_webhook(
            _body(id="evt-104"), timestamp=str(int(now.timestamp())),
            nonce="d" * 24, signature="sha256=xyz", secret=b"key", now=now,
        )


@pytest.mark.parametrize("raw_body", [
    pytest.param(b"{", id="malformed"),
    pytest.param(b"x" * (1024 * 1024 + 1), id="oversize"),
])
def test_oversize_or_malformed_payload_is_rejected(raw_body):
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    timestamp, nonce, secret = str(int(now.timestamp())), "e" * 24, b"key"
    with pytest.raises(WebhookVerificationError):
        verify_inbound_webhook(
            raw_body, timestamp=timestamp, nonce=nonce,
            signature=_signature(raw_body, timestamp, nonce, secret),
            secret=secret, now=now,
        )
