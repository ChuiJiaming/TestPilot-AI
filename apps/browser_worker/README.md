# TestPilot isolated health-probe Worker

This separate Node process implements only the M03 one-shot HTTP health probe. It requires a Gateway client certificate and performs one GET to the persisted environment URL. It accepts only an exact host present in `network_policy.allowed_domains`, resolves DNS once, rejects every non-global DNS answer, pins the selected address for the connection, blocks redirects, discards the response body, and returns only a sanitized status/error code. The candidate implementation allows only the scheme's default port (80 for HTTP, 443 for HTTPS); this rule is recorded for owner review and must remain aligned across API and Worker. It does not receive secret values. If a SecretRef is attached, it returns `SECRET_PROVIDER_UNAVAILABLE` until an external provider resolver is designed and configured.

Run with Node 22 or newer after installing the package:

```powershell
npm --prefix apps/browser_worker install --omit=dev
npm --prefix apps/browser_worker start
```

Required settings: `TESTPILOT_WORKER_TLS_CERT`, `TESTPILOT_WORKER_TLS_KEY`, and `TESTPILOT_WORKER_CLIENT_CA_CERT`. The default bind is port 9444. `GET /internal/health/ready` requires a trusted Gateway client certificate.

This is not the full M08 Playwright session/action Worker. Production egress ACLs, non-root/container resource isolation, certificate rotation, Browser Gateway registration, and SSRF negative tests remain release gates and have not been run.
