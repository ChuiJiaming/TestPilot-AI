# Phase 1–4 Unified Validation Record

Date: 2026-10-30  
Scope: locally runnable acceptance for the execution, recovery, browser-isolation, and quality paths needed before Phase 5. This record does not claim full Phase 1–4 acceptance.

## Results

| Area | Evidence | Result |
|---|---|---|
| Backend local suite | `apps/api/.venv/Scripts/python.exe -m pytest tests --ignore=tests/test_m10_healing_routes.py --ignore=tests/test_lifecycle_routes.py -q` | **151 passed, 4 skipped**, 3 existing warnings. Includes M06/M09 concurrency admission, duplicate `RUN_REQUESTED` delivery, idempotent schedule trigger, Phase 3 recovery persistence, and M10–M12 local tests. |
| Browser Worker | Worker Node test suite | **7 passed**. |
| Web | Vitest suite | **15 passed** across 4 files. |
| Web static/build checks | `npm --prefix apps/web run typecheck`, `lint`, and `build` | Passed. |
| Python syntax | `apps/api/.venv/Scripts/python.exe -m compileall -q apps/api tests` | Passed. |
| Patch hygiene | `git diff --check` | Passed; Git reported existing LF-to-CRLF working-copy notices. |

The four skipped backend cases require optional external services or PostgreSQL configuration. The M06/M09-focused assertions added in this pass all pass: an active Run holds a serial plan slot, a duplicate `RUN_REQUESTED` event cannot claim twice or emit a second start event, and repeating a manual Schedule trigger with the same key creates one trigger and one Outbox event.

## Environment-limited checks

- The full command cannot include `tests/test_lifecycle_routes.py` or `tests/test_m10_healing_routes.py` in this Windows environment. A thread dump shows Starlette/AnyIO blocked before route execution while Python's Windows Proactor loop creates its local `socketpair`; a standalone `socket.socketpair()` reproduces the hang. These two route suites are **blocked by the local test runtime**, not counted as passes or product failures. Re-run them on a Linux runner or a Windows environment where asyncio local socket pairs work.
- `python -m apps.api.scripts.migrate --check` could not reach migration-ledger validation because `TESTPILOT_DATABASE_URL` is unset. No migration was applied.
- PostgreSQL 15/18 fresh/upgrade/rollback and concurrency validation, the independent Browser Worker image, real mTLS and egress tests, browser end-to-end flows, and S3/MinIO acceptance were not run. Docker is unavailable in this environment.
- No claim is made that Phase 1–4 are fully accepted, production-ready, or deployed. Existing candidate migrations 008–016 remain unapplied here.

## Disposition

The locally runnable execution and recovery checks found no Phase 4 dependency blocker in admission, duplicate delivery, or Schedule trigger idempotency. Source and local deterministic coverage are sufficient to begin Phase 5 implementation planning. External service, database, browser, and the two Windows route-test gaps remain tracked acceptance work and must not be marked passed by static checks.

Phase 5 contract discovery is recorded in [the Phase 5 design draft](superpowers/specs/2026-10-30-phase-5-reporting-integrations-operations-design.md).
