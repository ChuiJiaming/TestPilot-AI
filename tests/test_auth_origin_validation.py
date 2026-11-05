"""Origin validation for the auth composition root."""

import unittest

from apps.api.app.auth.bootstrap import EmailVerificationSettings


def make_settings(allowed_origins) -> EmailVerificationSettings:
    return EmailVerificationSettings(
        database_url="postgresql://postgres:postgres@127.0.0.1:5432/testpilot",
        token_ttl_seconds=3600,
        rate_limit_max_requests=5,
        rate_limit_window_seconds=900,
        smtp_host="127.0.0.1",
        smtp_port=1025,
        smtp_sender="Dev <dev@testpilot.local>",
        verification_url="http://localhost:5173/verify",
        email_outbox_encryption_keys="ivsI9GggMRf5Ke2V08y_6IluJt_NGWZK0lq3DqH8r7M=",
        jwt_signing_key="unit-test-signing-key-with-enough-bytes-0123456789",
        allowed_origins=allowed_origins,
    )


class AllowedOriginValidationTests(unittest.TestCase):
    def test_https_origins_are_accepted(self):
        make_settings(("https://app.example.com", "https://app.example.com:8443"))

    def test_loopback_http_origins_are_accepted_for_local_development(self):
        make_settings(("http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:5173"))

    def test_plain_http_on_a_public_host_is_rejected(self):
        with self.assertRaises(ValueError):
            make_settings(("http://app.example.com",))

    def test_origins_with_a_path_query_or_fragment_are_rejected(self):
        for bad in ("https://app.example.com/", "https://app.example.com?x=1", "https://app.example.com#f"):
            with self.subTest(origin=bad):
                with self.assertRaises(ValueError):
                    make_settings((bad,))

    def test_other_schemes_are_rejected(self):
        for bad in ("ftp://app.example.com", "app.example.com", ""):
            with self.subTest(origin=bad):
                with self.assertRaises(ValueError):
                    make_settings((bad,))


if __name__ == "__main__":
    unittest.main()
