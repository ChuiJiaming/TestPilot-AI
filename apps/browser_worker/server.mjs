import https from "node:https";
import http from "node:http";
import { readFile } from "node:fs/promises";
import { domainToASCII } from "node:url";
import ipaddr from "ipaddr.js";
import { browserWorkerReadiness, handleBrowserWorkerRequest } from "./browser_sessions.mjs";
import { resolveWorkerHostname } from "./worker_dns.mjs";

const port = Number(process.env.TESTPILOT_WORKER_PORT ?? "9444");
const maxBodyBytes = 32 * 1024;
const requestTimeoutMs = 6000;

function json(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, { "content-type": "application/json", "content-length": data.length, "cache-control": "no-store" });
  res.end(data);
}

async function readJson(req) {
  const chunks = []; let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > maxBodyBytes) throw Object.assign(new Error("BODY_TOO_LARGE"), { status: 413 });
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw Object.assign(new Error("INVALID_JSON"), { status: 400 }); }
}

function publicAddress(address) {
  try {
    const parsed = ipaddr.process(address);
    return parsed.range() === "unicast";
  } catch { return false; }
}

function normalizeDomain(value) {
  if (typeof value !== "string" || value.includes("*") || value.includes("/") || value.includes(":")) return null;
  const ascii = domainToASCII(value.trim().replace(/\.$/, "")).toLowerCase();
  if (!ascii || ascii.length > 253 || ascii.split(".").some((part) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(part))) return null;
  return ascii;
}

async function pinnedResolution(hostname) {
  let timer;
  const answers = await Promise.race([
    resolveWorkerHostname(hostname),
    new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error("DNS_TIMEOUT"), { code: "DNS_TIMEOUT" })), 3000); }),
  ]).finally(() => clearTimeout(timer));
  if (!answers.length || answers.some((entry) => !publicAddress(entry.address))) {
    throw Object.assign(new Error("DNS_ADDRESS_BLOCKED"), { code: "DNS_ADDRESS_BLOCKED" });
  }
  return answers[0];
}

async function probe(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw Object.assign(new Error("INVALID_REQUEST"), { status: 422 });
  if (typeof payload.environment_id !== "string" || !/^[0-9a-f-]{36}$/i.test(payload.environment_id)) throw Object.assign(new Error("INVALID_ENVIRONMENT_ID"), { status: 422 });
  if (payload.secret_ref_id || payload.secret_ref_version_id) {
    return { id: payload.environment_id, status: "ERROR", checked_at: new Date().toISOString(), http_status: null, error_code: "SECRET_PROVIDER_UNAVAILABLE" };
  }
  let target;
  try { target = new URL(payload.base_url); }
  catch { throw Object.assign(new Error("INVALID_ENVIRONMENT_URL"), { status: 422 }); }
  const expectedPort = target.protocol === "https:" ? "443" : "80";
  if (!(["http:", "https:"].includes(target.protocol)) || target.username || target.password || target.hash || target.port && target.port !== expectedPort) {
    throw Object.assign(new Error("INVALID_ENVIRONMENT_URL"), { status: 422 });
  }
  const hostname = domainToASCII(target.hostname.replace(/\.$/, "")).toLowerCase();
  if (!hostname || ipaddr.isValid(hostname)) throw Object.assign(new Error("IP_LITERAL_TARGET_BLOCKED"), { code: "IP_LITERAL_TARGET_BLOCKED" });
  const domains = payload.allowed_domains;
  const policyDomains = payload.network_policy?.allowed_domains;
  if (!Array.isArray(domains) || domains.length === 0 || domains.some((item) => !normalizeDomain(item))) {
    throw Object.assign(new Error("INVALID_DOMAIN_ALLOWLIST"), { status: 422 });
  }
  if (!Array.isArray(policyDomains) || policyDomains.length === 0
      || policyDomains.some((item) => !normalizeDomain(item))
      || JSON.stringify([...new Set(policyDomains.map(normalizeDomain))].sort())
        !== JSON.stringify([...new Set(domains.map(normalizeDomain))].sort())) {
    throw Object.assign(new Error("NETWORK_POLICY_MISMATCH"), { status: 403 });
  }
  if (!domains.map(normalizeDomain).includes(hostname)) throw Object.assign(new Error("DOMAIN_NOT_ALLOWLISTED"), { status: 403 });

  let address;
  try { address = await pinnedResolution(hostname); }
  catch (error) {
    return { id: payload.environment_id, status: "BLOCKED", checked_at: new Date().toISOString(), http_status: null, error_code: error.code ?? "DNS_RESOLUTION_FAILED" };
  }

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => { if (!settled) { settled = true; resolve(result); } };
    const transport = target.protocol === "https:" ? https : awaitImportHttp();
    const options = {
      protocol: target.protocol, hostname, port: target.port || undefined,
      path: `${target.pathname || "/"}${target.search}`,
      method: "GET", headers: { host: target.host, "user-agent": "TestPilot-HealthCheck/1.0", range: "bytes=0-0", connection: "close" },
      timeout: requestTimeoutMs,
      lookup: (_host, _options, callback) => callback(null, address.address, address.family),
      ...(target.protocol === "https:" ? { servername: hostname, rejectUnauthorized: true } : {}),
    };
    const request = transport.request(options, (response) => {
      const status = response.statusCode ?? 0;
      const redirected = status >= 300 && status < 400;
      finish({
        id: payload.environment_id,
        status: redirected ? "BLOCKED" : status >= 200 && status < 300 ? "HEALTHY" : "UNHEALTHY",
        checked_at: new Date().toISOString(),
        http_status: status || null,
        error_code: redirected ? "REDIRECT_BLOCKED" : status >= 200 && status < 300 ? null : "HTTP_STATUS_NOT_HEALTHY",
      });
      response.destroy();
    });
    request.on("timeout", () => { request.destroy(Object.assign(new Error("TARGET_TIMEOUT"), { code: "TARGET_TIMEOUT" })); });
    request.on("error", (error) => finish({
      id: payload.environment_id, status: "UNHEALTHY", checked_at: new Date().toISOString(),
      http_status: null, error_code: ["TARGET_TIMEOUT", "ECONNREFUSED", "ECONNRESET", "CERT_HAS_EXPIRED", "UNABLE_TO_VERIFY_LEAF_SIGNATURE"].includes(error.code) ? error.code : "TARGET_CONNECTION_FAILED",
    }));
    request.end();
  });
}

function awaitImportHttp() {
  // Lazy module value keeps the same pinned lookup behavior for HTTP and HTTPS.
  return http;
}

const cert = await readFile(process.env.TESTPILOT_WORKER_TLS_CERT);
const key = await readFile(process.env.TESTPILOT_WORKER_TLS_KEY);
const ca = await readFile(process.env.TESTPILOT_WORKER_CLIENT_CA_CERT);
const rawGatewayUrls = process.env.TESTPILOT_WORKER_GATEWAY_URLS_JSON;
const gatewayRegistrationUrls = rawGatewayUrls
  ? JSON.parse(rawGatewayUrls)
  : process.env.TESTPILOT_WORKER_GATEWAY_URL
    ? [process.env.TESTPILOT_WORKER_GATEWAY_URL] : [];
if (!Array.isArray(gatewayRegistrationUrls)
    || gatewayRegistrationUrls.some((value) => typeof value !== "string")) {
  throw new Error("Worker Gateway URLs must be a JSON array of HTTPS origins");
}
const registrationTargets = gatewayRegistrationUrls.map((value) => {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password
      || parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error("Worker Gateway registration URLs must be HTTPS origins");
  }
  return new URL("/internal/workers/register", parsed.origin);
});
let registrationCert = null;
let registrationKey = null;
let registrationCa = null;
if (registrationTargets.length) {
  registrationCert = await readFile(process.env.TESTPILOT_WORKER_GATEWAY_CLIENT_CERT || process.env.TESTPILOT_WORKER_TLS_CERT);
  registrationKey = await readFile(process.env.TESTPILOT_WORKER_GATEWAY_CLIENT_KEY || process.env.TESTPILOT_WORKER_TLS_KEY);
  registrationCa = await readFile(process.env.TESTPILOT_WORKER_GATEWAY_CA_CERT);
}

const server = https.createServer({ cert, key, ca, requestCert: true, rejectUnauthorized: true }, async (req, res) => {
  if (req.method === "GET" && req.url === "/internal/health/ready") {
    const status = browserWorkerReadiness();
    return json(res, status.ready ? 200 : 503, { ...status, browser_session_actions: true });
  }
  if (await handleBrowserWorkerRequest(req, res)) return;
  if (req.method !== "POST" || req.url !== "/internal/v1/health-checks") return json(res, 404, { error: "NOT_FOUND" });
  try { return json(res, 200, await probe(await readJson(req))); }
  catch (error) { return json(res, error.status ?? 500, { error: error.status ? error.message : "WORKER_INTERNAL_ERROR" }); }
});
server.requestTimeout = requestTimeoutMs + 1000;
server.listen(port, "0.0.0.0", () => {
  if (!registrationTargets.length) return;
  const registerAt = (target) => new Promise((resolve) => {
    const readiness = browserWorkerReadiness();
    const payload = Buffer.from(JSON.stringify(readiness));
    const request = https.request(target, {
      method: "POST", cert: registrationCert, key: registrationKey,
      ca: registrationCa, rejectUnauthorized: true, timeout: 5000,
      headers: { "content-type": "application/json", "content-length": payload.length, "connection": "close" },
    }, (response) => {
      response.resume();
      if (response.statusCode !== 200) process.stderr.write("Worker registration rejected\n");
      resolve();
    });
    request.on("timeout", () => request.destroy(new Error("GATEWAY_REGISTRATION_TIMEOUT")));
    request.on("error", () => { process.stderr.write("Worker registration unavailable\n"); resolve(); });
    request.end(payload);
  });
  const register = () => {
    void Promise.allSettled(registrationTargets.map(registerAt)).then((results) => {
      if (results.some((result) => result.status === "rejected")) {
        process.stderr.write("Worker registration unavailable\n");
      }
    });
  };
  register();
  const timer = setInterval(register, 15000);
  timer.unref();
});
