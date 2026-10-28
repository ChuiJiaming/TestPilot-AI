import https from "node:https";
import { readFile } from "node:fs/promises";
import { handleBrowserGatewayRequest } from "./browser_gateway_sessions.mjs";

const port = Number(process.env.TESTPILOT_GATEWAY_PORT ?? "9443");
const maxBodyBytes = 32 * 1024;
const registrationTtlMs = 45000;
const workerUrl = new URL(process.env.TESTPILOT_WORKER_URL ?? "https://127.0.0.1:9444/internal/v1/health-checks");
if (workerUrl.protocol !== "https:" || workerUrl.username || workerUrl.password) throw new Error("Worker URL must use HTTPS without userinfo");

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
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw Object.assign(new Error("INVALID_JSON"), { status: 400 }); }
}

function workerOrigin(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password
      || parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error("Worker pool entries must be HTTPS origins without userinfo or paths");
  }
  return parsed.origin;
}

function loadWorkerPool() {
  const raw = process.env.TESTPILOT_GATEWAY_WORKER_POOL_JSON;
  const configured = raw ? JSON.parse(raw) : {
    [process.env.TESTPILOT_GATEWAY_DEFAULT_WORKER_INSTANCE || "browser-worker"]: workerUrl.origin,
  };
  if (!configured || typeof configured !== "object" || Array.isArray(configured)) throw new Error("Worker pool must be a JSON object");
  const pool = new Map();
  for (const [instance, origin] of Object.entries(configured)) {
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(instance) || typeof origin !== "string") throw new Error("Invalid Worker pool entry");
    pool.set(instance, workerOrigin(origin));
  }
  if (!pool.size) throw new Error("At least one Worker pool entry is required");
  return pool;
}

function forward(payload, targetUrl = workerUrl, method = "POST") {
  return new Promise((resolve, reject) => {
    const data = method === "GET" ? Buffer.alloc(0) : Buffer.from(JSON.stringify(payload ?? {}));
    const request = https.request(targetUrl, {
      method,
      cert: gatewayWorkerCert,
      key: gatewayWorkerKey,
      ca: workerCa,
      rejectUnauthorized: true,
      timeout: 105000,
      headers: method === "GET" ? { "connection": "close" } : {
        "content-type": "application/json", "content-length": data.length, "connection": "close",
      },
    }, (response) => {
      const chunks = []; let bytes = 0;
      response.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes > 64 * 1024) { response.destroy(new Error("WORKER_RESPONSE_TOO_LARGE")); return; }
        chunks.push(chunk);
      });
      response.on("end", () => {
        let body;
        try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
        catch { return reject(Object.assign(new Error("WORKER_INVALID_RESPONSE"), { status: 503 })); }
        if (response.statusCode !== 200) {
          const workerError = typeof body?.error === "string" ? body.error : "WORKER_REJECTED_REQUEST";
          return reject(Object.assign(new Error(workerError), { status: 503, workerError }));
        }
        resolve(body);
      });
    });
    request.on("timeout", () => request.destroy(new Error("WORKER_TIMEOUT")));
    request.on("error", reject);
    request.end(data);
  });
}

const gatewayCert = await readFile(process.env.TESTPILOT_GATEWAY_TLS_CERT);
const gatewayKey = await readFile(process.env.TESTPILOT_GATEWAY_TLS_KEY);
const apiClientCa = await readFile(process.env.TESTPILOT_GATEWAY_API_CLIENT_CA_CERT);
const workerClientCa = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_CLIENT_CA_CERT);
const gatewayWorkerCert = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_CERT);
const gatewayWorkerKey = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_KEY);
const workerCa = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_CA_CERT);
const workerPool = loadWorkerPool();
const registrations = new Map();
const sessionAssignments = new Map();
const allocationReservations = new Map();

function peerCommonName(req) {
  const value = req.socket.getPeerCertificate()?.subject?.CN;
  return Array.isArray(value) ? value[0] : value;
}

async function registerWorker(req, res) {
  let payload;
  try { payload = await readJson(req); }
  catch (error) { json(res, error.status ?? 400, { error: error.message }); return; }
  const instance = payload.worker_instance;
  const peerName = peerCommonName(req);
  if (typeof instance !== "string" || !workerPool.has(instance) || peerName !== instance) {
    json(res, 403, { error: "WORKER_IDENTITY_MISMATCH" }); return;
  }
  if (typeof payload.ready !== "boolean" || typeof payload.draining !== "boolean"
      || !Number.isInteger(payload.active_sessions) || payload.active_sessions < 0
      || !Number.isInteger(payload.capacity) || payload.capacity < 1
      || payload.active_sessions > payload.capacity) {
    json(res, 422, { error: "INVALID_WORKER_READINESS" }); return;
  }
  const registration = {
    worker_instance: instance,
    ready: payload.ready,
    draining: payload.draining,
    active_sessions: payload.active_sessions,
    capacity: payload.capacity,
    worker_version: typeof payload.worker_version === "string" ? payload.worker_version.slice(0, 80) : "unknown",
    last_seen: Date.now(),
  };
  registrations.set(instance, registration);
  json(res, 200, { registered: true, worker_instance: instance, status: registration.draining ? "DRAINING" : "READY" });
}

function liveRegistrations() {
  const now = Date.now();
  return [...registrations.values()].filter((item) => now - item.last_seen <= registrationTtlMs);
}

function assignedSessionCount(instance) {
  let count = 0;
  for (const worker of sessionAssignments.values()) if (worker === instance) count += 1;
  return count;
}

function effectiveLoad(registration) {
  const assigned = assignedSessionCount(registration.worker_instance);
  const reservations = allocationReservations.get(registration.worker_instance) || 0;
  return Math.max(registration.active_sessions, assigned) + reservations;
}

function chooseWorker(excluded = new Set()) {
  const available = liveRegistrations().filter((item) => item.ready && !item.draining
    && effectiveLoad(item) < item.capacity && !excluded.has(item.worker_instance)
    && workerPool.has(item.worker_instance));
  available.sort((left, right) => effectiveLoad(left) - effectiveLoad(right)
    || left.worker_instance.localeCompare(right.worker_instance));
  if (!available.length) throw Object.assign(new Error("NO_READY_BROWSER_WORKER"), { status: 503 });
  return available[0].worker_instance;
}

async function routeWorkerRequest(path, payload, options = {}) {
  let instance = options.workerInstance;
  const canSelectAnother = options.create && !instance && !payload.resume_only;
  const excluded = new Set();
  while (true) {
    if (options.create && !instance) instance = chooseWorker(excluded);
    if (typeof instance !== "string" || !workerPool.has(instance)) {
      throw Object.assign(new Error("BROWSER_WORKER_NOT_REGISTERED"), { status: 503 });
    }
    const registration = registrations.get(instance);
    if (!options.drain && (!registration || Date.now() - registration.last_seen > registrationTtlMs)) {
      throw Object.assign(new Error("BROWSER_WORKER_UNAVAILABLE"), { status: 503 });
    }
    const reserve = options.create && !payload.resume_only;
    const reservedInstance = reserve ? instance : null;
    if (reserve) allocationReservations.set(reservedInstance, (allocationReservations.get(reservedInstance) || 0) + 1);
    try {
      const result = await forward(payload, new URL(path, workerPool.get(instance)));
      if (options.create && result.status === "READY" && result.worker_instance !== instance) {
        throw Object.assign(new Error("WORKER_IDENTITY_MISMATCH"), { status: 503 });
      }
      if (options.create && result.status === "READY" && payload.session_id) {
        sessionAssignments.set(payload.session_id, instance);
      }
      if (options.drain) {
        const current = registrations.get(instance);
        if (current) { current.draining = true; current.ready = false; }
      }
      if (path.endsWith("/close") && payload.worker_instance) {
        sessionAssignments.delete(path.split("/").at(-2));
      }
      return result;
    } catch (error) {
      if (canSelectAnother && ["WORKER_CAPACITY_REACHED", "WORKER_DRAINING"].includes(error.workerError)) {
        excluded.add(instance);
        if (registration) {
          if (error.workerError === "WORKER_DRAINING") registration.draining = true;
          else registration.active_sessions = registration.capacity;
          registration.last_seen = Date.now();
        }
        instance = undefined;
        continue;
      }
      throw error;
    } finally {
      if (reserve) {
        const remaining = (allocationReservations.get(reservedInstance) || 1) - 1;
        if (remaining > 0) allocationReservations.set(reservedInstance, remaining);
        else allocationReservations.delete(reservedInstance);
      }
    }
  }
}

async function getWorkerReadiness(instance, origin) {
  const target = new URL("/internal/health/ready", origin);
  const body = await forward({}, target, "GET");
  return { worker_instance: instance, ...body };
}

const server = https.createServer({
  cert: gatewayCert, key: gatewayKey,
  ca: Buffer.concat([apiClientCa, workerClientCa]),
  requestCert: true, rejectUnauthorized: true,
}, async (req, res) => {
  if (req.method === "POST" && req.url === "/internal/workers/register") return registerWorker(req, res);
  if (req.method === "GET" && req.url === "/internal/health/ready") {
    const workers = await Promise.all(liveRegistrations().map(async (entry) => {
      try { return await getWorkerReadiness(entry.worker_instance, workerPool.get(entry.worker_instance)); }
      catch { return { worker_instance: entry.worker_instance, ready: false, error: "WORKER_UNAVAILABLE" }; }
    }));
    const readyWorkers = workers.filter((entry) => entry.ready === true && !entry.draining);
    return json(res, readyWorkers.length ? 200 : 503, {
      ready: readyWorkers.length > 0, gateway_version: "browser-gateway-v1",
      browser_sessions: true, workers,
    });
  }
  if (await handleBrowserGatewayRequest(req, res, (path, payload, options) => routeWorkerRequest(path, payload, options))) return;
  if (req.method !== "POST" || req.url !== "/internal/v1/health-checks") return json(res, 404, { error: "NOT_FOUND" });
  try {
    const payload = await readJson(req);
    if (!Array.isArray(payload.allowed_domains)) return json(res, 422, { error: "INVALID_HEALTH_CHECK_REQUEST" });
    return json(res, 200, await forward(payload));
  } catch (error) {
    return json(res, error.status ?? 503, { error: error.status ? error.message : "BROWSER_WORKER_UNAVAILABLE" });
  }
});
server.requestTimeout = 45000;
server.listen(port, "0.0.0.0");
