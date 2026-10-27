"""Versioned stdlib scrypt password hashes for M01 credentials."""

import base64
import hashlib
import hmac
import secrets


class ScryptPasswordHasher:
    def __init__(self, *, n=16384, r=8, p=1, salt_bytes=16, key_bytes=32):
        if n <= 1 or n & (n - 1) or min(r, p, salt_bytes, key_bytes) <= 0:
            raise ValueError("invalid scrypt parameters")
        self.n = n
        self.r = r
        self.p = p
        self.salt_bytes = salt_bytes
        self.key_bytes = key_bytes

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(self.salt_bytes)
        key = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=self.n, r=self.r, p=self.p,
            dklen=self.key_bytes,
        )
        return "scrypt${}${}${}${}${}${}".format(
            self.n,
            self.r,
            self.p,
            self._encode(salt),
            self._encode(key),
            self.key_bytes,
        )

    @classmethod
    def verify(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt_text, key_text, key_bytes = encoded.split("$", 6)
            if algorithm != "scrypt":
                return False
            expected = cls._decode(key_text)
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=cls._decode(salt_text),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=int(key_bytes),
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, MemoryError):
            return False

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
