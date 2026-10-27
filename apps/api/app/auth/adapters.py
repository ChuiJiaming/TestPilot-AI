"""SMTP and Redis adapters; operational limits are required configuration."""

from email.message import EmailMessage
import smtplib
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SmtpVerificationMailer:
    def __init__(self, *, host, port, username, password, sender, verify_url, starttls=True):
        if not all((host, port, sender, verify_url)):
            raise ValueError("SMTP host, port, sender and verify_url are required")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.verify_url = validate_verification_url(verify_url)
        self.starttls = starttls

    def send_verification(self, email, token, expires_at):
        message = EmailMessage()
        message["Subject"] = "Verify your TestPilot AI account"
        message["From"] = self.sender
        message["To"] = email
        message.set_content(
            f"Verify your email using this one-time token: {token}\n"
            f"Verification link: {self.verify_url}?token={token}\n"
            f"Expires at: {expires_at.isoformat()}"
        )
        with smtplib.SMTP(self.host, self.port, timeout=10) as client:
            if self.starttls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password or "")
            client.send_message(message)


class VerificationTokenCipher:
    """Encrypts retryable email tokens so the durable outbox never stores plaintext."""

    def __init__(self, key: str):
        try:
            keys = [Fernet(part.strip().encode("ascii")) for part in key.split(",") if part.strip()]
            if not keys:
                raise ValueError("at least one encryption key is required")
            self._fernet = MultiFernet(keys)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("email outbox encryption keys must be Fernet URL-safe base64 keys") from exc

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError) as exc:
            raise ValueError("email outbox token cannot be decrypted") from exc


class RedisVerificationRateLimiter:
    """Fixed-window limiter; both limit and window are explicit deployment settings."""

    def __init__(self, redis_client, *, max_requests, window_seconds):
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        self.redis = redis_client
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def allow(self, email_key, client_ip):
        keys = [f"auth:verify:email:{email_key}"]
        if client_ip:
            # Avoid putting raw IP addresses into Redis keys.
            from hashlib import sha256

            ip_key = sha256(client_ip.encode("utf-8")).hexdigest()
            keys.append(f"auth:verify:ip:{ip_key}")
        for key in keys:
            count = int(self.redis.eval(
                "local n=redis.call('INCR',KEYS[1]); "
                "if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
                1,
                key,
                self.window_seconds,
            ))
            if count > self.max_requests:
                return False
        return True


def validate_verification_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("verification URL must be an absolute HTTPS URL")
    return url
