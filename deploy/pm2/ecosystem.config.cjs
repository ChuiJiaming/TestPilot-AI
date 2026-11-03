// TestPilot AI — PM2 process ecosystem
//
// Deploys the three business processes that run on the PM2 host:
//   1. api          — FastAPI (uvicorn), tenant-scoped business API
//   2. agent-worker — Celery worker (bounded generation / run orchestration)
//   3. agent-beat   — Celery beat (single leader; schedules Outbox dispatch,
//                     run approval expiry, schedule scan, queue sweeps)
//
// The Browser Gateway is intentionally NOT listed here by default. In the
// reference topology the Gateway terminates mTLS and is the only component
// that talks to the isolated Browser Worker; it is started separately (see
// deploy/README.md) so its TLS identity is not coupled to the PM2 lifecycle.
//
// Secrets MUST come from the environment (TESTPILOT_*), never from this file.
// Do not commit real credentials, signing keys, or cert paths to the repo.

module.exports = {
  apps: [
    {
      name: "testpilot-api",
      script: "python",
      args: "-m uvicorn apps.api.app.main:app --host 127.0.0.1 --port 8000 --workers 2",
      cwd: "/srv/testpilot",
      interpreter: "none",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 4000,
      kill_timeout: 30000,
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      name: "testpilot-agent-worker",
      script: "celery",
      args: "-A apps.api.app.cases.worker:celery_app worker --loglevel=INFO --concurrency=2",
      cwd: "/srv/testpilot",
      interpreter: "none",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 4000,
      kill_timeout: 30000,
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      // Exactly one beat leader must run across the deployment. PM2 `instances: 1`
      // is not sufficient on its own — when scaling to multiple hosts, run beat
      // on exactly one node. Beat owns the idempotent Outbox dispatch and the
      // 60-second approval/queue sweeps; duplicate beats only waste cycles, they
      // must not corrupt state because every consumer is idempotent.
      name: "testpilot-agent-beat",
      script: "celery",
      args: "-A apps.api.app.cases.worker:celery_app beat --loglevel=INFO",
      cwd: "/srv/testpilot",
      interpreter: "none",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 4000,
      kill_timeout: 15000,
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
