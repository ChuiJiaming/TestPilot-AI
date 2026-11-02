"""Raw-body HMAC verification for inbound provider Webhooks."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re


MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
_SECRET_KEY_PARTS = (
    "secret", "password", "passwd", "token", "credential", "apikey",
    "accesskey", "privatekey", "signingkey",
)
_SECRET_KEY_NAMES = {"auth", "authentication", "authorization"}


class WebhookVerificationError(ValueError):
    def __init__(self, code: str, status_code: int = 401):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class VerifiedWebhook:
    nonce: str
    signed_at: datetime
    event_id: str
    event_type: str
    payload: dict
    body_sha256: str


def _invalid_json_constant(value):
    raise ValueError(f"unsupported JSON constant: {value}")


def _normalized_payload(value, *, depth=0):
    if depth > 32:
        raise WebhookVerificationError("WEBHOOK_PAYLOAD_TOO_DEEP", 400)
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise WebhookVerificationError("WEBHOOK_PAYLOAD_INVALID", 400)
            compact_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if (compact_key in _SECRET_KEY_NAMES
                    or any(part in compact_key for part in _SECRET_KEY_PARTS)):
                continue
            normalized[key[:256]] = _normalized_payload(item, depth=depth + 1)
        return normalized
    if isinstance(value, list):
        return [_normalized_payload(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise WebhookVerificationError("WEBHOOK_PAYLOAD_INVALID", 400)


def verify_inbound_webhook(
    raw_body: bytes,
    *,
    timestamp: str,
    nonce: str,
    signature: str,
    secret: bytes,
    now: datetime,
    max_skew_seconds: int = 300,
) -> VerifiedWebhook:
    if not isinstance(raw_body, bytes):
        raise WebhookVerificationError("WEBHOOK_BODY_INVALID", 400)
    if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
        raise WebhookVerificationError("WEBHOOK_BODY_TOO_LARGE", 413)
    if not nonce or not 16 <= len(nonce) <= 256 or not nonce.isprintable():
        raise WebhookVerificationError("WEBHOOK_NONCE_INVALID", 401)
    if not isinstance(secret, bytes) or not secret:
        raise WebhookVerificationError("SECRET_PROVIDER_UNAVAILABLE", 503)
    if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{10}", timestamp):
        raise WebhookVerificationError("WEBHOOK_TIMESTAMP_INVALID", 401)
    try:
        signed_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise WebhookVerificationError("WEBHOOK_TIMESTAMP_INVALID", 401) from exc
    if now.tzinfo is None or abs((now - signed_at).total_seconds()) > max_skew_seconds:
        raise WebhookVerificationError("WEBHOOK_TIMESTAMP_STALE", 401)
    if not isinstance(signature, str) or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        raise WebhookVerificationError("WEBHOOK_SIGNATURE_INVALID", 401)

    signing_bytes = timestamp.encode("utf-8") + b"\n" + nonce.encode("utf-8") + b"\n" + raw_body
    expected = "sha256=" + hmac.new(secret, signing_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("WEBHOOK_SIGNATURE_INVALID", 401)

    try:
        decoded = json.loads(raw_body.decode("utf-8"), parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WebhookVerificationError("WEBHOOK_PAYLOAD_INVALID", 400) from exc
    if not isinstance(decoded, dict):
        raise WebhookVerificationError("WEBHOOK_PAYLOAD_INVALID", 400)
    payload = _normalized_payload(decoded)
    body_sha256 = hashlib.sha256(raw_body).hexdigest()
    raw_event_id = payload.get("event_id", payload.get("id"))
    if isinstance(raw_event_id, str) and raw_event_id.strip() and len(raw_event_id.strip()) <= 255:
        event_id = raw_event_id.strip()
    else:
        event_id = f"sha256:{body_sha256}"
    raw_event_type = payload.get("event_type", payload.get("type", "unknown"))
    event_type = raw_event_type.strip()[:120] if isinstance(raw_event_type, str) else "unknown"
    if not event_type:
        event_type = "unknown"
    return VerifiedWebhook(
        nonce=nonce,
        signed_at=signed_at,
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        body_sha256=body_sha256,
    )
