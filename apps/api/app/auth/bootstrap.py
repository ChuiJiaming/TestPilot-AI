"""Explicit composition root for M01 email verification dependencies."""

from dataclasses import dataclass
import os
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.adapters import RedisVerificationRateLimiter, VerificationTokenCipher
from apps.api.app.auth.passwords import ScryptPasswordHasher
from apps.api.app.auth.persistence import SqlAlchemyVerificationRepository
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.verification import EmailVerificationService
from apps.api.app.tenancy.persistence import OrganizationProjectRepository
from apps.api.app.environments.persistence import EnvironmentRepository, EnvironmentSnapshotRepository
from apps.api.app.cases.persistence import CaseRepository
from apps.api.app.cases.planning import GenerationRepository, PlanningRepository


@dataclass(frozen=True)
class EmailVerificationSettings:
    database_url: str
    token_ttl_seconds: int
    rate_limit_max_requests: int
    rate_limit_window_seconds: int
    smtp_host: str
    smtp_port: int
    smtp_sender: str
    verification_url: str
    email_outbox_encryption_keys: str
    jwt_signing_key: str
    allowed_origins: tuple[str, ...]
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True

    def __post_init__(self):
        if self.token_ttl_seconds <= 0:
            raise ValueError("token_ttl_seconds must be configured as positive")
        if self.rate_limit_max_requests <= 0 or self.rate_limit_window_seconds <= 0:
            raise ValueError("email verification rate limits must be configured as positive")
        if len(self.jwt_signing_key.encode("utf-8")) < 32:
            raise ValueError("JWT signing key must be at least 32 bytes")
        if not self.allowed_origins:
            raise ValueError("at least one allowed Origin is required")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("allowed origins must be HTTPS origins without a path")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")


@dataclass(frozen=True)
class AuthComponents:
    service: EmailVerificationService
    sessions: AuthSessionService
    password_hasher: ScryptPasswordHasher
    engine: object
    tenancy_repository: OrganizationProjectRepository
    environment_repository: EnvironmentRepository
    environment_snapshots: EnvironmentSnapshotRepository
    lifecycle_repository: LifecycleRepository
    case_repository: CaseRepository
    planning_repository: PlanningRepository
    generation_repository: GenerationRepository


def settings_from_environment() -> EmailVerificationSettings | None:
    """Return no settings when the deployment has not supplied the full mail/auth config."""
    required = (
        "TESTPILOT_DATABASE_URL",
        "TESTPILOT_REDIS_URL",
        "TESTPILOT_EMAIL_TOKEN_TTL_SECONDS",
        "TESTPILOT_EMAIL_RATE_LIMIT_MAX_REQUESTS",
        "TESTPILOT_EMAIL_RATE_LIMIT_WINDOW_SECONDS",
        "TESTPILOT_SMTP_HOST",
        "TESTPILOT_SMTP_PORT",
        "TESTPILOT_SMTP_SENDER",
        "TESTPILOT_EMAIL_VERIFY_URL",
        "TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS",
        "TESTPILOT_AUTH_JWT_SIGNING_KEY",
        "TESTPILOT_AUTH_ALLOWED_ORIGINS",
    )
    if any(not os.getenv(key) for key in required):
        return None
    starttls = os.getenv("TESTPILOT_SMTP_STARTTLS", "true").casefold()
    if starttls not in {"true", "false"}:
        raise ValueError("TESTPILOT_SMTP_STARTTLS must be true or false")
    return EmailVerificationSettings(
        database_url=os.environ["TESTPILOT_DATABASE_URL"],
        token_ttl_seconds=int(os.environ["TESTPILOT_EMAIL_TOKEN_TTL_SECONDS"]),
        rate_limit_max_requests=int(os.environ["TESTPILOT_EMAIL_RATE_LIMIT_MAX_REQUESTS"]),
        rate_limit_window_seconds=int(os.environ["TESTPILOT_EMAIL_RATE_LIMIT_WINDOW_SECONDS"]),
        smtp_host=os.environ["TESTPILOT_SMTP_HOST"],
        smtp_port=int(os.environ["TESTPILOT_SMTP_PORT"]),
        smtp_sender=os.environ["TESTPILOT_SMTP_SENDER"],
        verification_url=os.environ["TESTPILOT_EMAIL_VERIFY_URL"],
        email_outbox_encryption_keys=os.environ["TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS"],
        jwt_signing_key=os.environ["TESTPILOT_AUTH_JWT_SIGNING_KEY"],
        allowed_origins=tuple(
            origin.strip().rstrip("/")
            for origin in os.environ["TESTPILOT_AUTH_ALLOWED_ORIGINS"].split(",")
            if origin.strip()
        ),
        smtp_username=os.getenv("TESTPILOT_SMTP_USERNAME"),
        smtp_password=os.getenv("TESTPILOT_SMTP_PASSWORD"),
        smtp_starttls=starttls == "true",
    )


def build_auth_components(*, settings: EmailVerificationSettings, redis_client) -> AuthComponents:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = SqlAlchemyVerificationRepository(sessions)
    tenancy_repository = OrganizationProjectRepository(sessions)
    environment_repository = EnvironmentRepository(sessions)
    environment_snapshots = EnvironmentSnapshotRepository(sessions)
    lifecycle_repository = LifecycleRepository(sessions)
    case_repository = CaseRepository(sessions)
    planning_repository = PlanningRepository(sessions)
    generation_repository = GenerationRepository(sessions)
    limiter = RedisVerificationRateLimiter(
        redis_client,
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    service = EmailVerificationService(
        repository=repository,
        token_encryptor=VerificationTokenCipher(settings.email_outbox_encryption_keys),
        limiter=limiter,
        token_ttl_seconds=settings.token_ttl_seconds,
    )
    password_hasher = ScryptPasswordHasher()
    auth_sessions = AuthSessionService(
        repository=repository,
        limiter=limiter,
        password_verifier=password_hasher.verify,
        jwt_signing_key=settings.jwt_signing_key,
        access_ttl_seconds=900,
        refresh_ttl_seconds=2_592_000,
    )
    return AuthComponents(
        service=service,
        sessions=auth_sessions,
        password_hasher=password_hasher,
        engine=engine,
        tenancy_repository=tenancy_repository,
        environment_repository=environment_repository,
        environment_snapshots=environment_snapshots,
        lifecycle_repository=lifecycle_repository,
        case_repository=case_repository,
        planning_repository=planning_repository,
        generation_repository=generation_repository,
    )
