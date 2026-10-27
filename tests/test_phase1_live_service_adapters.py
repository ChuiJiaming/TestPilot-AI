"""Opt-in integration checks against isolated Redis and Mailpit services.

Set TESTPILOT_TEST_REDIS_URL, TESTPILOT_TEST_SMTP_HOST,
TESTPILOT_TEST_SMTP_PORT and TESTPILOT_TEST_MAILPIT_API_URL to run these tests.
They are skipped in ordinary unit-test runs when those dependencies are absent.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import socket
import time
import unittest
from urllib.request import urlopen
from urllib.parse import urlsplit
from uuid import uuid4

from apps.api.app.auth.adapters import RedisVerificationRateLimiter, SmtpVerificationMailer


LIVE_REDIS = os.getenv("TESTPILOT_TEST_REDIS_URL")
SMTP_HOST = os.getenv("TESTPILOT_TEST_SMTP_HOST")
SMTP_PORT = os.getenv("TESTPILOT_TEST_SMTP_PORT")
MAILPIT_API_URL = os.getenv("TESTPILOT_TEST_MAILPIT_API_URL")


class RedisRespClient:
    """Small RESP2 client for exercising the limiter against real Redis."""

    def __init__(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "redis" or not parsed.hostname:
            raise ValueError("TESTPILOT_TEST_REDIS_URL must be a redis:// URL")
        self.host = parsed.hostname
        self.port = parsed.port or 6379
        self.database = int(parsed.path.lstrip("/") or "0")

    def _command(self, *parts):
        encoded = [str(part).encode("utf-8") for part in parts]
        payload = b"*" + str(len(encoded)).encode() + b"\r\n"
        for part in encoded:
            payload += b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
        with socket.create_connection((self.host, self.port), timeout=3) as connection:
            reader = connection.makefile("rb")
            if self.database:
                self._write(connection, "SELECT", self.database)
                if not reader.readline().startswith(b"+OK"):
                    raise RuntimeError("Redis SELECT failed")
            connection.sendall(payload)
            prefix = reader.read(1)
            line = reader.readline().rstrip(b"\r\n")
            if prefix == b":":
                return int(line)
            if prefix == b"+":
                return line.decode("utf-8")
            if prefix == b"-":
                raise RuntimeError(line.decode("utf-8"))
            if prefix == b"$":
                length = int(line)
                if length < 0:
                    return None
                value = reader.read(length)
                reader.read(2)
                return value.decode("utf-8")
            raise RuntimeError("Unsupported Redis response")

    @staticmethod
    def _write(connection, *parts):
        encoded = [str(part).encode("utf-8") for part in parts]
        payload = b"*" + str(len(encoded)).encode() + b"\r\n"
        for part in encoded:
            payload += b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
        connection.sendall(payload)

    def ping(self):
        return self._command("PING") == "PONG"

    def eval(self, script, numkeys, key, expiry):
        return self._command("EVAL", script, numkeys, key, expiry)

    def ttl(self, key):
        return self._command("TTL", key)

    def delete(self, *keys):
        return self._command("DEL", *keys) if keys else 0

    def close(self):
        pass


@unittest.skipUnless(LIVE_REDIS, "set TESTPILOT_TEST_REDIS_URL for live Redis checks")
class RedisRateLimiterLiveTests(unittest.TestCase):
    def test_fixed_window_limits_email_and_client_ip_using_redis(self):
        client = RedisRespClient(LIVE_REDIS)
        client.ping()
        identity = uuid4().hex
        client_ip = "192.0.2.42"
        ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
        keys = [f"auth:verify:email:{identity}", f"auth:verify:ip:{ip_hash}"]
        client.delete(*keys)
        limiter = RedisVerificationRateLimiter(client, max_requests=2, window_seconds=20)
        try:
            self.assertTrue(limiter.allow(identity, client_ip))
            self.assertTrue(limiter.allow(identity, client_ip))
            self.assertFalse(limiter.allow(identity, client_ip))
            self.assertGreater(client.ttl(keys[0]), 0)
            self.assertGreater(client.ttl(keys[1]), 0)
        finally:
            client.delete(*keys)
            client.close()


@unittest.skipUnless(
    SMTP_HOST and SMTP_PORT and MAILPIT_API_URL,
    "set TESTPILOT_TEST_SMTP_HOST/PORT and TESTPILOT_TEST_MAILPIT_API_URL for live SMTP checks",
)
class SmtpMailerLiveTests(unittest.TestCase):
    def test_verification_mail_is_delivered_through_smtp(self):
        email = f"smtp-{uuid4().hex}@example.invalid"
        mailer = SmtpVerificationMailer(
            host=SMTP_HOST,
            port=int(SMTP_PORT),
            username=os.getenv("TESTPILOT_TEST_SMTP_USERNAME"),
            password=os.getenv("TESTPILOT_TEST_SMTP_PASSWORD"),
            sender=os.getenv("TESTPILOT_TEST_SMTP_SENDER", "TestPilot AI <noreply@example.invalid>"),
            verify_url="https://testpilot.example.invalid/verify-email",
            starttls=os.getenv("TESTPILOT_TEST_SMTP_STARTTLS", "false").lower() == "true",
        )
        mailer.send_verification(
            email,
            "test-only-verification-token",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with urlopen(f"{MAILPIT_API_URL.rstrip('/')}/api/v1/messages?limit=50", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if email.lower() in json.dumps(payload).lower():
                return
            time.sleep(0.2)
        self.fail("Mailpit did not expose the verification email delivered over SMTP")


if __name__ == "__main__":
    unittest.main()
