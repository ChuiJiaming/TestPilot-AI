# TestPilot API foundation

## Authentication configuration

The default `app` remains available for local contract work when authentication configuration is absent. Auth endpoints return `503` in that mode; they do not create accounts or issue placeholder tokens.

To enable M01 registration/login/session endpoints, set all of the following before starting Uvicorn:

| Variable | Meaning |
|---|---|
| `TESTPILOT_DATABASE_URL` | Synchronous SQLAlchemy DSN, normally `postgresql+psycopg://...`; migrations must already be applied. |
| `TESTPILOT_REDIS_URL` | Redis connection URL for distributed rate limits. |
| `TESTPILOT_EMAIL_TOKEN_TTL_SECONDS` | Positive one-time verification token lifetime. |
| `TESTPILOT_EMAIL_RATE_LIMIT_MAX_REQUESTS` | Positive maximum requests in the configured window, per normalized email and client IP. |
| `TESTPILOT_EMAIL_RATE_LIMIT_WINDOW_SECONDS` | Positive rate-limit window. |
| `TESTPILOT_SMTP_HOST` / `TESTPILOT_SMTP_PORT` | SMTP server endpoint. |
| `TESTPILOT_SMTP_SENDER` | Verified sender address. |
| `TESTPILOT_EMAIL_VERIFY_URL` | Absolute HTTPS URL used in the verification message. |
| `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS` | Comma-separated Fernet URL-safe base64 keys; first key encrypts new outbox tokens, later keys decrypt older rows during rotation. Generate each key with `Fernet.generate_key()` and keep it in the deployment secret store. |
| `TESTPILOT_AUTH_JWT_SIGNING_KEY` | At least 32 bytes of secret key material; inject from the deployment secret store. |
| `TESTPILOT_AUTH_ALLOWED_ORIGINS` | Comma-separated exact origins accepted for refresh/logout, including scheme and optional port. |

Optional SMTP settings are `TESTPILOT_SMTP_USERNAME`, `TESTPILOT_SMTP_PASSWORD`, and `TESTPILOT_SMTP_STARTTLS` (`true` by default). Registration and resend now enqueue encrypted mail intent transactionally; the Celery Beat dispatcher sends it with bounded retry/backoff and dead-letters permanent failures or exhausted retries. The access token lifetime is 900 seconds. Refresh sessions last 2,592,000 seconds and use an HttpOnly, Secure, SameSite=Lax `__Host-testpilot_refresh` cookie. Never put secrets in source control or logs.

## Local checks

From the repository root:

```powershell
python -m unittest tests.test_auth_routes tests.test_auth_sessions_service tests.test_email_verification_service tests.test_email_verification_persistence -v
python -m compileall -q apps/api tests
python -m unittest tests.test_migration_runner -v
```

These tests use SQLite and in-process adapters. They do not verify PostgreSQL migration locking, Redis Lua behavior against a live server, SMTP delivery, HTTPS cookie behavior in a browser, or the full M01–M03 acceptance suite.

The migration command defaults to read-only hash verification. Set `TESTPILOT_DATABASE_URL` and provide any database password through the process environment, then run `python -m apps.api.scripts.migrate --check`. `--apply` applies missing numbered migrations under a PostgreSQL advisory lock and writes the source checksum in the same transaction; the read-only `005_preflight_and_validate.sql` is intentionally excluded. Never edit an applied migration; a checksum mismatch stops the runner.

## Current limits

- Verification messages are committed to the encrypted email Outbox with the user/token record; Celery Beat retries delivery with a lease and moves exhausted or expired attempts out of the active queue. The worker must receive the same current and previous Fernet key set during key rotation.
- Candidate migrations 001–007 were applied in order to fresh disposable PostgreSQL 18.3 and 15 databases and, with user authorization, to the local `testpilot` PostgreSQL 18.3 database on 2026-10-26. The populated upgrade fixture, targeted PostgreSQL integration, Redis/Mailpit adapters, refresh replay concurrency and Chrome cookie flow have recorded local evidence dated 2026-10-27. This does not establish rollback/failure-injection or production-provider behavior. Candidate migrations 008–011 remain unapplied. See `db/migrations/README.md` and `docs/TestPilot_AI_V10.3_验证记录.txt`.
- M02 organization/project services and routes enforce organization membership, project roles, immediate project authorization checks, and organization-member discovery. Environment list/create/update routes use the project-derived scope and validate SecretRef organization ownership. See `docs/decisions/phase-1-open-issues.md` for policy items still awaiting review.
- The object-storage readiness probe remains unconfigured, so readiness is not `ready` even when PostgreSQL and Redis respond.

## M03 isolated environment health check (candidate)

The environment screen exposes `POST /api/v1/environments/{id}/health-check`. The request body keeps the V10.3 optional `name` as a display label; the target URL and network policy always come from the saved environment. The API never connects to the target. It validates tenant/project write access and requires `network_policy.allowed_domains` to contain the exact IDNA-normalized host; wildcards and IP-literal targets are rejected. Network-policy writes require an organization or project administrator. A successful probe requires an HTTP 2xx response. Redirects are blocked, non-global DNS answers are denied, and the isolated worker pins the selected public DNS answer for the request. Response codes are sanitized to `HEALTHY`, `UNHEALTHY`, `BLOCKED`, or `ERROR`; response bodies, credentials, and raw exception text are not returned.

The current candidate allows only the scheme-default port (HTTP 80 / HTTPS 443) and rejects URL fragments; the port decision still needs owner confirmation. Results are returned synchronously and are not persisted as check history because retention and audit requirements are not defined.

The API, Node Browser Gateway, and separate Node health-probe Worker communicate over mutual TLS. Configure the API with `TESTPILOT_BROWSER_GATEWAY_URL`, `TESTPILOT_BROWSER_GATEWAY_CLIENT_CERT`, `TESTPILOT_BROWSER_GATEWAY_CLIENT_KEY`, and `TESTPILOT_BROWSER_GATEWAY_CA_CERT`. The Gateway requires `TESTPILOT_GATEWAY_TLS_CERT`, `TESTPILOT_GATEWAY_TLS_KEY`, `TESTPILOT_GATEWAY_API_CLIENT_CA_CERT`, `TESTPILOT_GATEWAY_WORKER_CERT`, `TESTPILOT_GATEWAY_WORKER_KEY`, `TESTPILOT_GATEWAY_WORKER_CA_CERT`, and `TESTPILOT_WORKER_URL`. The Worker requires `TESTPILOT_WORKER_TLS_CERT`, `TESTPILOT_WORKER_TLS_KEY`, and `TESTPILOT_WORKER_CLIENT_CA_CERT`. Each trust bundle must contain only the certificates intended for that hop. These are code/configuration contracts; no certificate or service start has been run.

The health-probe Worker is a minimal independent HTTP probe process, not the full M08 Playwright session/action Worker. It does not resolve SecretRef values; a health check for an environment with a SecretRef currently returns `SECRET_PROVIDER_UNAVAILABLE` until a provider resolver contract is implemented. Gateway/Worker process isolation, production egress ACLs, certificate issuance/rotation, and SSRF negative tests remain deployment/unified-validation gates. The candidate response DTO intentionally replaces the contradictory baseline `Environment` response with a `HealthCheckResult`; see `P1-23`.

## M04 Agent Worker configuration (candidate, not yet run)

The generation API persists a job and an Outbox event. The separate Celery process publishes those events and calls the configured LangChain adapter; it is not started by Uvicorn. Configure `TESTPILOT_DATABASE_URL`, `TESTPILOT_CELERY_BROKER_URL` (or reuse `TESTPILOT_REDIS_URL`), `TESTPILOT_LLM_API_KEY`, optional `TESTPILOT_LLM_BASE_URL` (defaults to `https://api.openai.com/v1`), and `TESTPILOT_LLM_PRICING_JSON`. Pricing is operator-supplied and keyed by exact model name, for example `{"model-id":{"input_usd_per_million":1.0,"output_usd_per_million":2.0}}`; the model response is never trusted to report cost. Missing token usage or a missing price entry fails the job rather than recording a fabricated cost. Inject the API key through the secret store and do not place actual rates or credentials in the repository.

After candidate migrations are applied in a separately authorized validation window, the process entry points are:

```powershell
celery -A apps.api.app.cases.worker:celery_app worker --loglevel=INFO
celery -A apps.api.app.cases.worker:celery_app beat --loglevel=INFO
```

These commands are implementation guidance; neither process nor a provider request has been run. Beat scans due schedules every 15 seconds, enqueues schedule trigger events within the approved 60-second grace window, and the Outbox consumer creates a queued Run with an immutable environment snapshot and one RunCase per published case version and selected browser. Candidate migration 010 is required for the browser and snapshot pins and is not applied. Consumption stops at the `RUN_REQUESTED` event; M09 Run execution, Run API/event/SSE, and independent Browser Worker are unfinished.
