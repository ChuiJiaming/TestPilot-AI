from collections.abc import Callable, Mapping
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from apps.api.app.core.errors import install_error_handlers
from apps.api.app.auth.routes import PasswordHash, create_auth_router, create_me_router
from apps.api.app.auth.verification import EmailVerificationService
from apps.api.app.auth.bootstrap import build_auth_components, settings_from_environment
from apps.api.app.auth.sessions import AuthSessionService
from apps.api.app.tenancy.persistence import OrganizationProjectRepository
from apps.api.app.tenancy.routes import create_tenancy_router
from apps.api.app.environments.persistence import EnvironmentRepository, EnvironmentSnapshotRepository
from apps.api.app.environments.routes import create_environment_router
from apps.api.app.environments.health_check import BrowserGatewayHealthClient
from apps.api.app.auth.lifecycle import LifecycleRepository
from apps.api.app.auth.lifecycle_routes import create_lifecycle_router
from apps.api.app.cases.persistence import CaseRepository
from apps.api.app.cases.routes import create_case_router
from apps.api.app.cases.planning import GenerationRepository, PlanningRepository
from apps.api.app.cases.planning_routes import create_planning_router


ReadinessProbe = Callable[[], bool]


def create_app(
    readiness_probes: Mapping[str, ReadinessProbe] | None = None,
    *,
    email_verification_service: EmailVerificationService | None = None,
    password_hasher: PasswordHash | None = None,
    auth_sessions: AuthSessionService | None = None,
    allowed_origins: tuple[str, ...] = (),
    tenancy_repository: OrganizationProjectRepository | None = None,
    environment_repository: EnvironmentRepository | None = None,
    environment_snapshots: EnvironmentSnapshotRepository | None = None,
    health_check_gateway: BrowserGatewayHealthClient | None = None,
    lifecycle_repository: LifecycleRepository | None = None,
    case_repository: CaseRepository | None = None,
    planning_repository: PlanningRepository | None = None,
    generation_repository: GenerationRepository | None = None,
) -> FastAPI:
    app = FastAPI(
        title="TestPilot AI API",
        version="1.0.0",
        description="Enterprise AI web automation testing platform API.",
    )
    install_error_handlers(app)
    app.include_router(
        create_auth_router(
            service=email_verification_service,
            password_hash=password_hasher,
            auth_sessions=auth_sessions,
            allowed_origins=allowed_origins,
        )
    )
    app.include_router(create_me_router(auth_sessions=auth_sessions))
    app.include_router(
        create_tenancy_router(repository=tenancy_repository, auth_sessions=auth_sessions)
    )
    app.include_router(
        create_environment_router(
            environments=environment_repository,
            snapshots=environment_snapshots,
            tenancy=tenancy_repository,
            auth_sessions=auth_sessions,
            health_check_gateway=health_check_gateway,
        )
    )
    app.include_router(create_lifecycle_router(
        repository=lifecycle_repository, auth_sessions=auth_sessions
    ))
    app.include_router(create_case_router(
        cases=case_repository, tenancy=tenancy_repository, auth_sessions=auth_sessions
    ))
    app.include_router(create_planning_router(
        planning=planning_repository, generations=generation_repository,
        auth_sessions=auth_sessions,
    ))
    app.state.readiness_probes = dict(readiness_probes or {})
    app.state.expected_readiness_dependencies = (
        "postgresql",
        "redis",
        "object_storage",
    )

    @app.get("/api/v1/health/ready", tags=["Health"])
    def readiness() -> JSONResponse:
        probes: Mapping[str, ReadinessProbe] = app.state.readiness_probes
        checks: dict[str, str] = {}
        for name in app.state.expected_readiness_dependencies:
            probe = probes.get(name)
            if probe is None:
                checks[name] = "not_configured"
                continue
            try:
                result = probe()
                checks[name] = "ready" if result else "unavailable"
            except Exception:
                checks[name] = "unavailable"

        ready = all(value == "ready" for value in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    return app


def create_default_app() -> FastAPI:
    health_check_gateway = BrowserGatewayHealthClient.from_environment()
    settings = settings_from_environment()
    if settings is None:
        return create_app(health_check_gateway=health_check_gateway)

    import redis

    redis_client = redis.Redis.from_url(os.environ["TESTPILOT_REDIS_URL"], socket_connect_timeout=2)
    components = build_auth_components(settings=settings, redis_client=redis_client)

    def database_ready() -> bool:
        with components.engine.connect():
            return True

    def redis_ready() -> bool:
        return bool(redis_client.ping())

    return create_app(
        {"postgresql": database_ready, "redis": redis_ready},
        email_verification_service=components.service,
        password_hasher=components.password_hasher.hash,
        auth_sessions=components.sessions,
        allowed_origins=settings.allowed_origins,
        tenancy_repository=components.tenancy_repository,
        environment_repository=components.environment_repository,
        environment_snapshots=components.environment_snapshots,
        health_check_gateway=health_check_gateway,
        lifecycle_repository=components.lifecycle_repository,
        case_repository=components.case_repository,
        planning_repository=components.planning_repository,
        generation_repository=components.generation_repository,
    )


app = create_default_app()
