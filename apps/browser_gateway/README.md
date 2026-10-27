# TestPilot Browser Gateway (M03 health probe)

This Node service is a small, independent Gateway entry point for the M03 HTTP health probe. It accepts only `POST /internal/v1/health-checks`, requires an API client certificate signed by `TESTPILOT_GATEWAY_API_CLIENT_CA_CERT`, then forwards the bounded request to the configured independent Worker over mutual TLS. It does not connect to customer sites or execute Playwright actions.

Run with Node 22 or newer after installing the package (there are no direct Gateway npm dependencies):

```powershell
npm --prefix apps/browser_gateway start
```

Required settings: `TESTPILOT_GATEWAY_TLS_CERT`, `TESTPILOT_GATEWAY_TLS_KEY`, `TESTPILOT_GATEWAY_API_CLIENT_CA_CERT`, `TESTPILOT_GATEWAY_WORKER_CERT`, `TESTPILOT_GATEWAY_WORKER_KEY`, `TESTPILOT_GATEWAY_WORKER_CA_CERT`, and `TESTPILOT_WORKER_URL`. The default bind is port 9443. `GET /internal/health/ready` also requires a trusted client certificate.

Certificates, network ACLs, service isolation, and end-to-end mTLS have not been provisioned or verified. Do not expose this listener publicly.
