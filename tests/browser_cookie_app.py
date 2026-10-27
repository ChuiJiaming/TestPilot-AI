"""Local-only real-browser app composition for auth cookie integration tests."""

import os
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app.main import create_app
from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.passwords import ScryptPasswordHasher
from apps.api.app.auth.persistence import SqlAlchemyVerificationRepository
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.environments.persistence import EnvironmentRepository, EnvironmentSnapshotRepository
from apps.api.app.tenancy.persistence import OrganizationProjectRepository


class AllowLocalTestRequests:
    def allow(self, email_key, client_ip):
        return True


database_url = os.environ["TESTPILOT_TEST_DATABASE_URL"]
database_target = urlsplit(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
if database_target.hostname not in {"127.0.0.1", "localhost", "::1"} or database_target.path != "/testpilot":
    raise RuntimeError("browser cookie test app may only use the loopback testpilot database")
engine = create_engine(database_url, pool_pre_ping=True)
session_factory = sessionmaker(engine, expire_on_commit=False)
verification_repository = SqlAlchemyVerificationRepository(session_factory)
hasher = ScryptPasswordHasher()
sessions = AuthSessionService(
    repository=verification_repository,
    limiter=AllowLocalTestRequests(),
    password_verifier=hasher.verify,
    jwt_signing_key="test-only-local-browser-cookie-signing-key-32-bytes",
    access_ttl_seconds=900,
    refresh_ttl_seconds=2_592_000,
)
tenancy = OrganizationProjectRepository(session_factory)
environments = EnvironmentRepository(session_factory)
snapshots = EnvironmentSnapshotRepository(session_factory)
lifecycle = LifecycleRepository(session_factory)

# The insecure HTTP origin is accepted only by this disposable test app bound to loopback.
app = create_app(
    password_hasher=hasher.hash,
    auth_sessions=sessions,
    allowed_origins=("http://127.0.0.1:5173",),
    tenancy_repository=tenancy,
    environment_repository=environments,
    environment_snapshots=snapshots,
    lifecycle_repository=lifecycle,
)
