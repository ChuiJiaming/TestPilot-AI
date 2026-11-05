from collections.abc import Callable, Mapping
import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

# Register every ORM table before any mapper is configured, so string foreign keys
# resolve regardless of which modules this process happens to import.
from apps.api.app import orm_models

orm_models.load_all_models()

from apps.api.app.core.errors import install_error_handlers
from apps.api.app.core.observability import (
    MetricsRegistry,
    configure_logging,
    install_observability,
)
from apps.api.app.auth.routes import (
    PasswordHash,
    create_auth_router,
    create_me_router,
    create_verification_admin_router,
)
from apps.api.app.auth.verification import EmailVerificationService
from apps.api.app.auth.password_reset import PasswordResetService
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
from apps.api.app.runs.repository import RunRepository
from apps.api.app.runs.routes import create_run_router
from apps.api.app.quality.artifacts import S3ArtifactStorage
from apps.api.app.quality.persistence import QualityRepository
from apps.api.app.quality.routes import create_quality_router
from apps.api.app.reports.persistence import ReportRepository
from apps.api.app.reports.routes import create_report_router
from apps.api.app.reports.storage import S3ReportStorage
from apps.api.app.integrations.persistence import IntegrationRepository
from apps.api.app.integrations.routes import create_integration_router
from apps.api.app.integrations.secrets import SecretRefResolver
from apps.api.app.integrations.secrets import from_environment as secret_resolver_from_environment
from apps.api.app.operations.notifications import NotificationService
from apps.api.app.operations.routes import create_operations_router
from apps.api.app.operations.audit import AuditRepository
from apps.api.app.operations.model_config import ModelConfigService
from apps.api.app.operations.quotas import QuotaPolicyService
from apps.api.app.operations.settings import SystemSettingService


ReadinessProbe = Callable[[], bool]

APP_VERSION = "1.0.0"


def create_app(
    readiness_probes: Mapping[str, ReadinessProbe] | None = None,
    *,
    email_verification_service: EmailVerificationService | None = None,
    password_reset_service: PasswordResetService | None = None,
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
    run_repository: RunRepository | None = None,
    quality_repository: QualityRepository | None = None,
    report_repository: ReportRepository | None = None,
    integration_repository: IntegrationRepository | None = None,
    secret_ref_resolver: SecretRefResolver | None = None,
    notification_service: NotificationService | None = None,
    audit_repository: AuditRepository | None = None,
    quota_policy_service: QuotaPolicyService | None = None,
    system_setting_service: SystemSettingService | None = None,
    model_config_service: ModelConfigService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="TestPilot AI API",
        version=APP_VERSION,
        description="Enterprise AI web automation testing platform API.",
    )
    install_error_handlers(app)

    # Correlation first: every log line, error trace_id and metrics sample below
    # inherits the request ID installed by this middleware.
    metrics = MetricsRegistry()
    install_observability(app, metrics=metrics)
    app.state.metrics = metrics
    app.include_router(
        create_auth_router(
            service=email_verification_service,
            password_hash=password_hasher,
            auth_sessions=auth_sessions,
            allowed_origins=allowed_origins,
            password_reset=password_reset_service,
        )
    )
    app.include_router(create_verification_admin_router(
        service=email_verification_service, auth_sessions=auth_sessions
    ))
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
        model_catalog=(
            model_config_service.options if model_config_service is not None else None
        ),
    ))
    app.include_router(create_run_router(
        repository=run_repository, case_repository=case_repository,
        auth_sessions=auth_sessions,
        allowed_origins=allowed_origins,
    ))
    app.include_router(create_quality_router(
        repository=quality_repository, auth_sessions=auth_sessions,
    ))
    app.include_router(create_report_router(
        repository=report_repository, auth_sessions=auth_sessions,
    ))
    app.include_router(create_integration_router(
        integration_repository=integration_repository,
        auth_sessions=auth_sessions,
        secret_ref_resolver=secret_ref_resolver,
    ))
    app.include_router(create_operations_router(
        notification_service=notification_service,
        auth_sessions=auth_sessions,
        audit_repository=audit_repository,
        quota_policy_service=quota_policy_service,
        system_setting_service=system_setting_service,
        model_config_service=model_config_service,
    ))
    app.state.readiness_probes = dict(readiness_probes or {})
    app.state.expected_readiness_dependencies = (
        "postgresql",
        "redis",
        "object_storage",
    )

    @app.get(
        "/api/v1/health/ready",
        tags=["Health"],
        operation_id="get_api_v1_health_ready",
    )
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

    @app.get(
        "/api/v1/health/live",
        tags=["Health"],
        operation_id="get_api_v1_health_live",
    )
    def liveness() -> dict[str, str]:
        """Liveness must never depend on PostgreSQL/Redis/S3.

        If it did, a database blip would restart every API pod and turn a
        recoverable dependency outage into a self-inflicted restart loop.
        """
        return {"status": "alive"}

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(authorization: str | None = Header(default=None)):
        expected = os.getenv("TESTPILOT_METRICS_TOKEN")
        if expected:
            presented = (authorization or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(presented, expected):
                raise HTTPException(status_code=403, detail="METRICS_TOKEN_INVALID")
        return PlainTextResponse(
            metrics.render(build_version=APP_VERSION),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


def create_default_app() -> FastAPI:
    configure_logging()
    health_check_gateway = BrowserGatewayHealthClient.from_environment()
    settings = settings_from_environment()
    if settings is None:
        return create_app(health_check_gateway=health_check_gateway)

    import redis

    redis_client = redis.Redis.from_url(os.environ["TESTPILOT_REDIS_URL"], socket_connect_timeout=2)
    components = build_auth_components(settings=settings, redis_client=redis_client)
    object_storage = S3ArtifactStorage.from_environment()
    report_storage = S3ReportStorage.from_environment()
    quality_repository = QualityRepository(
        components.environment_snapshots.session_factory,
        storage=object_storage,
        report_storage=report_storage,
    )
    report_repository = ReportRepository(components.environment_snapshots.session_factory)

    def database_ready() -> bool:
        with components.engine.connect():
            return True

    def redis_ready() -> bool:
        return bool(redis_client.ping())

    def object_storage_ready() -> bool:
        return bool(object_storage and object_storage.check())

    return create_app(
        {"postgresql": database_ready, "redis": redis_ready,
         "object_storage": object_storage_ready},
        email_verification_service=components.service,
        password_reset_service=components.password_reset,
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
        run_repository=RunRepository(
            components.environment_snapshots.session_factory,
            components.environment_snapshots,
        ),
        quality_repository=quality_repository,
        report_repository=report_repository,
        # Without TESTPILOT_SECRETREFS_JSON/FILE this stays fail-closed; with a
        # backend configured, CI integrations can actually resolve credentials.
        secret_ref_resolver=secret_resolver_from_environment(
            components.environment_snapshots.session_factory
        ),
        notification_service=NotificationService(
            components.environment_snapshots.session_factory,
        ),
        audit_repository=AuditRepository(
            components.environment_snapshots.session_factory,
        ),
        quota_policy_service=QuotaPolicyService(
            components.environment_snapshots.session_factory,
        ),
        system_setting_service=SystemSettingService(
            components.environment_snapshots.session_factory,
        ),
        model_config_service=ModelConfigService(
            components.environment_snapshots.session_factory,
        ),
    )


app = create_default_app()
