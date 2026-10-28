# TestPilot Browser Worker

The Worker is a separate mTLS service. It keeps Playwright browser processes and contexts outside the API/Celery process. The M03 HTTP health probe remains available separately.

## Registration and session affinity

Set `TESTPILOT_WORKER_INSTANCE` to the registration client certificate common name and to a key in the Gateway's `TESTPILOT_GATEWAY_WORKER_POOL_JSON`. Configure `TESTPILOT_WORKER_GATEWAY_URLS_JSON` as a JSON array of direct Gateway HTTPS origins, plus `TESTPILOT_WORKER_GATEWAY_CA_CERT`, `TESTPILOT_WORKER_GATEWAY_CLIENT_CERT`, and `TESTPILOT_WORKER_GATEWAY_CLIENT_KEY` for mTLS registration. A Worker heartbeats each Gateway instance so independent Gateway processes can discover it without sharing local memory. The Worker reports readiness, drain state, active sessions, and capacity every 15 seconds. The Gateway expires registrations after 45 seconds and assigns new sessions to a ready Worker. Signed job tokens bind the selected Worker ID so actions remain on the same instance.

The Gateway client certificate and Browser Worker job secret must be provisioned separately from public application credentials. The Worker accepts browser traffic only through the Gateway's mTLS identity.

## Browser sessions and observations

Each job token is scoped to one Run, RunCase, browser, attempt token, base URL, exact domain allowlist, and selected Worker. Session keys include RunCase, browser, and attempt token. Action IDs are idempotent within a live Worker session. The Worker returns only bounded page-control observations: visible controls and short labels, with form values excluded and common email/token/number patterns redacted. The Planner and Explorer can only echo published action/assertion IDs and locators; deterministic code controls execution and outcomes.

All browser traffic is routed through a per-session HTTP CONNECT proxy. The proxy resolves each exact allowlisted host through `TESTPILOT_BROWSER_WORKER_DNS_SERVER`, rejects any DNS answer outside globally routable unicast space, and connects to a selected resolved IP. Playwright request routing also blocks redirects and subresources outside the signed domain allowlist. The configured Gateway origins must resolve to addresses on the dedicated Worker subnet so registration remains reachable under the ACL.

## Runtime limits and validation

`compose.yaml` is a candidate runtime profile with a non-root user, read-only filesystem, bounded tmpfs, dropped Linux capabilities, no-new-privileges, and CPU, memory, PID, and session limits. Adjust those limits only with resource evidence. The profile uses a dedicated IPv4 bridge subnet, disables IPv6 for the Worker network, and requires the approved IPv4 DNS resolver in `TESTPILOT_BROWSER_WORKER_DNS_SERVER`. `configure-egress-acl.sh` is a host-side Docker `DOCKER-USER` candidate: it permits DNS only to that resolver, permits traffic within the dedicated subnet for Worker-to-Gateway registration, blocks other private/special-use IPv4 destinations, permits TCP 80/443 to public destinations, and drops other Worker egress. Its managed chain is rebuilt with `iptables-restore`; the script has not been applied. The `remove` mode removes that subnet's egress filter and is intended only after its Worker containers have stopped. Review the subnet and resolver before use.

Docker image builds, Compose/runtime checks, browser smoke tests, DNS-rebinding/SSRF tests, host ACL application, certificate lifecycle checks, and Linux isolation validation remain deferred to unified validation.
