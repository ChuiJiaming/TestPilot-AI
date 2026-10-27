# TestPilot AI Web

React + TypeScript + Vite workspace shell for the confirmed Phase 1 M01–M03 scope. Local UI primitives follow the shadcn/ui composition pattern and are kept in `src/components/ui`; visual tokens and responsive layouts are in `src/styles.css`.

## Run

Install dependencies with `npm install`, then:

```powershell
npm run dev
npm run typecheck
npm run lint
npm test
npm run build
```

Use Node.js 20.19+, 22.13+, or 24+. The dependency graph includes ESLint tooling that declares these supported runtime ranges.

Vite proxies `/api` to `http://127.0.0.1:8000` by default. Override with `TESTPILOT_API_PROXY`. The API refresh-cookie policy requires a configured HTTPS Origin allowlist. Do not weaken that production setting for local development; use a local HTTPS origin included in the development allowlist.

## Phase 1 coverage

- M01: login, pending-email registration and verification, resend, in-memory access token, HttpOnly refresh cookie flow, logout and device-session listing/revocation.
- M02: organization and project listing/creation using the user-confirmed explicit organization slug and project creator role; project membership listing, role assignment and removal. Member selection temporarily uses organization member user IDs until the existing organization-member listing API is implemented (P1-25).
- M03: snapshot lookup by environment ID. Environment/SecretRef CRUD and health check remain unavailable until the contracts and SSRF/egress rules in the issue log are resolved.

The browser keeps access tokens in React memory and sends refresh cookies with `credentials: include`. UI counts represent API records only; run metrics and system health are not fabricated. Frontend build/lint/tests must be run after dependencies are available and a lockfile is generated.
