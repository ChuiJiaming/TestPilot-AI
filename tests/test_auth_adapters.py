import unittest

from apps.api.app.auth.adapters import validate_verification_url
from apps.api.app.auth.passwords import ScryptPasswordHasher


class AuthAdapterTests(unittest.TestCase):
    def test_scrypt_hash_is_salted_and_verifies_password(self):
        hasher = ScryptPasswordHasher(n=1024)
        first = hasher.hash("a-long-test-password")
        second = hasher.hash("a-long-test-password")
        self.assertNotEqual(first, second)
        self.assertTrue(hasher.verify("a-long-test-password", first))
        self.assertFalse(hasher.verify("wrong-password", first))

    def test_verification_url_requires_https_origin(self):
        self.assertEqual(validate_verification_url("https://example.com/verify"), "https://example.com/verify")
        with self.assertRaises(ValueError):
            validate_verification_url("http://example.com/verify")


if __name__ == "__main__":
    unittest.main()
