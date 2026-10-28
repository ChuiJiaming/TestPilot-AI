import http from "node:http";
import net from "node:net";
import { createHmac, timingSafeEqual } from "node:crypto";
import { domainToASCII } from "node:url";
import ipaddr from "ipaddr.js";
import { chromium, firefox, webkit } from "playwright";
import { resolveWorkerHostname } from "./worker_dns.mjs";

const sessions = new Map();
let draining = false;
let drainTimer;
const browserTypes = { chromium, firefox, webkit };
function boundedInteger(name, fallback, minimum, maximum) {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isSafeInteger(value) || value < minimum) return fallback;
  return Math.min(value, maximum);
}
const maxSessions = boundedInteger("TESTPILOT_BROWSER_MAX_SESSIONS", 4, 1, 64);
const sessionTtlMs = boundedInteger("TESTPILOT_BROWSER_SESSION_TTL_MS", 900000, 60000, 3600000);
const actionTimeoutMs = boundedInteger("TESTPILOT_BROWSER_ACTION_TIMEOUT_MS", 30000, 1000, 120000);
const maxBodyBytes = 32 * 1024;
const workerInstance = process.env.TESTPILOT_WORKER_INSTANCE || "browser-worker";
if (!/^[A-Za-z0-9._-]{1,128}$/.test(workerInstance)) throw new Error("Invalid TESTPILOT_WORKER_INSTANCE");

export function browserWorkerReadiness() {
  return {
    ready: !draining && sessions.size < maxSessions,
    draining,
    active_sessions: sessions.size,
    capacity: maxSessions,
    worker_instance: workerInstance,
    worker_version: "browser-worker-v1",
  };
}

function reply(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": body.length,
    "cache-control": "no-store",
  });
  res.end(body);
}

async function readJson(req) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > maxBodyBytes) throw Object.assign(new Error("BODY_TOO_LARGE"), { status: 413 });
    chunks.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value;
  } catch {
    throw Object.assign(new Error("INVALID_JSON"), { status: 400 });
  }
}

function verifyJobToken(token) {
  if (typeof token !== "string" || token.length > 8192) throw Object.assign(new Error("JOB_TOKEN_INVALID"), { status: 401 });
  const secret = process.env.TESTPILOT_BROWSER_JOB_TOKEN_SECRET;
  if (!secret) throw Object.assign(new Error("JOB_TOKEN_NOT_CONFIGURED"), { status: 503 });
  const parts = token.split(".");
  if (parts.length !== 3) throw Object.assign(new Error("JOB_TOKEN_INVALID"), { status: 401 });
  const signingInput = parts[0] + "." + parts[1];
  const expected = createHmac("sha256", secret).update(signingInput).digest();
  let supplied;
  try { supplied = Buffer.from(parts[2], "base64url"); } catch { supplied = Buffer.alloc(0); }
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
    throw Object.assign(new Error("JOB_TOKEN_INVALID"), { status: 401 });
  }
  let header;
  let claims;
  try {
    header = JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8"));
    claims = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
  } catch {
    throw Object.assign(new Error("JOB_TOKEN_INVALID"), { status: 401 });
  }
  const now = Math.floor(Date.now() / 1000);
  if (header.alg !== "HS256" || claims.iss !== "testpilot-agent-worker"
      || claims.aud !== "testpilot-browser-gateway" || !Number.isInteger(claims.exp)
      || claims.exp <= now || !Number.isInteger(claims.iat) || claims.iat > now + 30
      || !claims.run_id || !claims.run_case_id || !claims.attempt_token
      || !claims.browser || !Array.isArray(claims.allowed_domains)) {
    throw Object.assign(new Error("JOB_TOKEN_SCOPE_INVALID"), { status: 403 });
  }
  return claims;
}

function normalizeDomain(value) {
  if (typeof value !== "string" || value.includes("*") || value.includes("/") || value.includes(":")) return null;
  const ascii = domainToASCII(value.trim().replace(/\.$/, "")).toLowerCase();
  if (!ascii || ascii.length > 253 || ascii.split(".").some((part) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(part))) return null;
  return ascii;
}

function publicAddress(address) {
  try { return ipaddr.process(address).range() === "unicast"; }
  catch { return false; }
}

async function resolvePinned(hostname) {
  let timer;
  const answers = await Promise.race([
    resolveWorkerHostname(hostname),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(Object.assign(new Error("DNS_TIMEOUT"), { code: "DNS_TIMEOUT" })), 3000);
    }),
  ]).finally(() => clearTimeout(timer));
  if (!answers.length || answers.some((entry) => !publicAddress(entry.address))) {
    throw Object.assign(new Error("DNS_ADDRESS_BLOCKED"), { code: "DNS_ADDRESS_BLOCKED" });
  }
  return answers[0];
}

async function assertTarget(rawUrl, claims) {
  let target;
  try { target = new URL(rawUrl); }
  catch { throw Object.assign(new Error("INVALID_TARGET_URL"), { code: "INVALID_TARGET_URL" }); }
  if (!["http:", "https:"].includes(target.protocol) || target.username || target.password || target.hash) {
    throw Object.assign(new Error("TARGET_URL_BLOCKED"), { code: "TARGET_URL_BLOCKED" });
  }
  const defaultPort = target.protocol === "https:" ? "443" : "80";
  if (target.port && target.port !== defaultPort) {
    throw Object.assign(new Error("TARGET_PORT_BLOCKED"), { code: "TARGET_PORT_BLOCKED" });
  }
  const hostname = domainToASCII(target.hostname.replace(/\.$/, "")).toLowerCase();
  if (!hostname || ipaddr.isValid(hostname)) {
    throw Object.assign(new Error("IP_LITERAL_TARGET_BLOCKED"), { code: "IP_LITERAL_TARGET_BLOCKED" });
  }
  const allowed = claims.allowed_domains.map(normalizeDomain);
  if (allowed.some((item) => item === null) || !allowed.includes(hostname)) {
    throw Object.assign(new Error("DOMAIN_NOT_ALLOWLISTED"), { code: "DOMAIN_NOT_ALLOWLISTED" });
  }
  const address = await resolvePinned(hostname);
  return { target, hostname, address };
}

class PinnedEgressProxy {
  constructor(claims) {
    this.claims = claims;
    this.sockets = new Set();
    this.server = http.createServer((req, res) => { void this.handleHttp(req, res); });
    this.server.on("connect", (req, client, head) => { void this.handleConnect(req, client, head); });
    this.server.on("connection", (socket) => {
      this.sockets.add(socket);
      socket.on("close", () => this.sockets.delete(socket));
    });
    this.server.requestTimeout = actionTimeoutMs + 5000;
  }

  async listen() {
    await new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(0, "127.0.0.1", resolve);
    });
    return this.server.address().port;
  }

  async handleConnect(req, client, head) {
    let parsed;
    try { parsed = new URL("https://" + req.url); }
    catch { client.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"); return; }
    const port = Number(parsed.port || 443);
    if (port !== 443) { client.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return; }
    let pinned;
    try { pinned = await assertTarget(parsed.href, this.claims); }
    catch { client.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return; }
    const upstream = net.connect({
      host: pinned.address.address, family: pinned.address.family, port,
    });
    this.sockets.add(upstream);
    upstream.on("close", () => this.sockets.delete(upstream));
    upstream.setTimeout(actionTimeoutMs, () => upstream.destroy());
    upstream.once("connect", () => {
      client.write("HTTP/1.1 200 Connection Established\r\nProxy-Agent: TestPilot\r\n\r\n");
      if (head.length) upstream.write(head);
      upstream.pipe(client);
      client.pipe(upstream);
    });
    upstream.on("error", () => client.destroy());
  }

  async handleHttp(req, client) {
    let pinned;
    try {
      pinned = await assertTarget(req.url, this.claims);
      if (pinned.target.protocol !== "http:") throw new Error("HTTPS_REQUIRES_CONNECT");
      if (pinned.target.port && pinned.target.port !== "80") throw new Error("TARGET_PORT_BLOCKED");
    } catch {
      client.writeHead(403, { "connection": "close", "content-length": "0" });
      client.end();
      req.destroy();
      return;
    }
    const headers = { ...req.headers };
    delete headers["proxy-connection"];
    const upstream = http.request({
      protocol: "http:",
      hostname: pinned.hostname,
      port: Number(pinned.target.port || 80),
      method: req.method,
      path: pinned.target.pathname + pinned.target.search,
      headers,
      timeout: actionTimeoutMs,
      lookup: (_host, _options, callback) => callback(null, pinned.address.address, pinned.address.family),
    }, (response) => {
      client.writeHead(response.statusCode || 502, response.headers);
      response.pipe(client);
    });
    this.sockets.add(upstream);
    upstream.on("close", () => this.sockets.delete(upstream));
    upstream.on("timeout", () => upstream.destroy(new Error("TARGET_TIMEOUT")));
    upstream.on("error", () => {
      if (!client.headersSent) client.writeHead(502, { "content-length": "0", "connection": "close" });
      client.end();
    });
    req.pipe(upstream);
  }

  async close() {
    for (const socket of this.sockets) socket.destroy();
    await new Promise((resolve) => this.server.close(() => resolve()));
  }
}

function locatorFor(page, value) {
  if (!value || typeof value !== "object") throw Object.assign(new Error("INVALID_LOCATOR"), { code: "INVALID_LOCATOR" });
  if (value.by === "role") return page.getByRole(value.value, { name: value.name ?? undefined, exact: Boolean(value.exact) });
  if (value.by === "label") return page.getByLabel(value.value);
  if (value.by === "placeholder") return page.getByPlaceholder(value.value);
  if (value.by === "text") return page.getByText(value.value);
  if (value.by === "test_id") return page.getByTestId(value.value);
  if (value.by === "css") return page.locator(value.value);
  throw Object.assign(new Error("UNSUPPORTED_LOCATOR"), { code: "UNSUPPORTED_LOCATOR" });
}

function verifyRequestScope(claims, payload, sessionId) {
  if (claims.run_id !== payload.run_id || claims.run_case_id !== payload.run_case_id
      || claims.browser !== payload.browser || claims.attempt_token !== payload.attempt_token) {
    throw Object.assign(new Error("JOB_TOKEN_SCOPE_MISMATCH"), { status: 403 });
  }
  if (sessionId && payload.session_id && sessionId !== payload.session_id) {
    throw Object.assign(new Error("BROWSER_SESSION_SCOPE_MISMATCH"), { status: 403 });
  }
  if ((claims.worker_instance && claims.worker_instance !== workerInstance)
      || (payload.worker_instance && payload.worker_instance !== workerInstance)) {
    throw Object.assign(new Error("WORKER_AFFINITY_MISMATCH"), { status: 403 });
  }
}

async function createSession(payload) {
  const sessionId = payload.session_id;
  const claims = verifyJobToken(payload.job_token);
  verifyRequestScope(claims, payload, sessionId);
  if (!/^[0-9a-f-]{36}$/i.test(sessionId || "")) throw Object.assign(new Error("INVALID_SESSION_ID"), { status: 422 });
  if (sessions.has(sessionId)) {
    const existing = sessions.get(sessionId);
    if (existing.claims.attempt_token !== claims.attempt_token) throw Object.assign(new Error("SESSION_SCOPE_MISMATCH"), { status: 403 });
    existing.expiresAt = Date.now() + sessionTtlMs;
    return { session_id: sessionId, status: "READY", worker_instance: workerInstance, resumed: true };
  }
  if (payload.resume_only) {
    return { session_id: sessionId, status: "ERROR", error_code: "SESSION_NOT_RESTORABLE" };
  }
  if (draining) throw Object.assign(new Error("WORKER_DRAINING"), { status: 503 });
  if (sessions.size >= maxSessions) throw Object.assign(new Error("WORKER_CAPACITY_REACHED"), { status: 503 });
  const browserType = browserTypes[claims.browser];
  if (!browserType) throw Object.assign(new Error("UNSUPPORTED_BROWSER"), { status: 422 });
  await assertTarget(claims.base_url, claims);
  const proxy = new PinnedEgressProxy(claims);
  const proxyPort = await proxy.listen();
  let browser;
  try {
    browser = await browserType.launch({
      headless: true,
      chromiumSandbox: true,
      proxy: { server: "http://127.0.0.1:" + proxyPort },
      timeout: 60000,
    });
    const context = await browser.newContext({ serviceWorkers: "block" });
    await context.route("**/*", async (route) => {
      const url = route.request().url();
      if (url === "about:blank") return route.continue();
      try { await assertTarget(url, claims); return route.continue(); }
      catch { return route.abort("blockedbyclient"); }
    });
    const page = await context.newPage();
    page.setDefaultTimeout(actionTimeoutMs);
    await page.goto(claims.base_url, { waitUntil: "domcontentloaded", timeout: actionTimeoutMs });
    const session = {
      id: sessionId, claims, browser, context, page, proxy,
      actions: new Map(), expiresAt: Date.now() + sessionTtlMs,
    };
    sessions.set(sessionId, session);
    return { session_id: sessionId, status: "READY", worker_instance: workerInstance, resumed: false };
  } catch (error) {
    await browser?.close().catch(() => {});
    await proxy.close().catch(() => {});
    throw error;
  }
}

async function withActionDeduplication(session, actionId, operation) {
  if (typeof actionId !== "string" || !/^[0-9a-f-]{36}$/i.test(actionId)) {
    throw Object.assign(new Error("INVALID_ACTION_ID"), { status: 422 });
  }
  const existing = session.actions.get(actionId);
  if (existing) return await existing;
  const pending = Promise.resolve().then(operation).catch((error) => ({
    action_id: actionId, status: "ERROR",
    error_code: error.code || "BROWSER_ACTION_FAILED",
  }));
  session.actions.set(actionId, pending);
  const result = await pending;
  session.actions.set(actionId, Promise.resolve(result));
  return result;
}

async function performAction(session, action) {
  if (!action || typeof action !== "object" || Array.isArray(action)) {
    throw Object.assign(new Error("INVALID_ACTION"), { status: 422 });
  }
  return withActionDeduplication(session, action.action_id, async () => {
    const startedAt = Date.now();
    try {
      if (action.action === "navigate") {
        if (typeof action.url !== "string" || action.url.length > 2048) throw Object.assign(new Error("INVALID_NAVIGATION"), { code: "INVALID_NAVIGATION" });
        const target = new URL(action.url, session.claims.base_url);
        await assertTarget(target.href, session.claims);
        await session.page.goto(target.href, { waitUntil: "domcontentloaded", timeout: actionTimeoutMs });
      } else if (action.action === "click") {
        const target = locatorFor(session.page, action.locator);
        if (await target.count() !== 1) throw Object.assign(new Error("LOCATOR_NOT_UNIQUE"), { code: "LOCATOR_NOT_UNIQUE" });
        await target.click({ timeout: actionTimeoutMs });
      } else if (action.action === "fill") {
        if (typeof action.value !== "string" || action.value.length > 12000) throw Object.assign(new Error("INVALID_FILL_VALUE"), { code: "INVALID_FILL_VALUE" });
        const target = locatorFor(session.page, action.locator);
        if (await target.count() !== 1) throw Object.assign(new Error("LOCATOR_NOT_UNIQUE"), { code: "LOCATOR_NOT_UNIQUE" });
        await target.fill(action.value, { timeout: actionTimeoutMs });
      } else {
        throw Object.assign(new Error("UNSUPPORTED_ACTION"), { code: "UNSUPPORTED_ACTION" });
      }
      session.expiresAt = Date.now() + sessionTtlMs;
      return { action_id: action.action_id, status: "SUCCEEDED", duration_ms: Date.now() - startedAt };
    } catch (error) {
      return {
        action_id: action.action_id, status: "FAILED",
        error_code: error.code || (error.name === "TimeoutError" ? "ACTION_TIMEOUT" : "BROWSER_ACTION_FAILED"),
        duration_ms: Date.now() - startedAt,
      };
    }
  });
}

async function performAssertion(session, assertion) {
  if (!assertion || typeof assertion !== "object" || Array.isArray(assertion)) {
    throw Object.assign(new Error("INVALID_ASSERTION"), { status: 422 });
  }
  return withActionDeduplication(session, assertion.assert_id, async () => {
    try {
      let matched = false;
      if (assertion.type === "url") {
        const current = session.page.url();
        matched = assertion.match === "exact"
          ? current === assertion.expected : current.includes(assertion.expected);
      } else if (assertion.type === "text") {
        const target = locatorFor(session.page, assertion.locator);
        if (await target.count() !== 1) return { assert_id: assertion.assert_id, matched: false, error_code: "LOCATOR_NOT_UNIQUE" };
        const actual = await target.innerText({ timeout: actionTimeoutMs });
        matched = assertion.match === "exact"
          ? actual === assertion.expected : actual.includes(assertion.expected);
      } else if (assertion.type === "visible") {
        const target = locatorFor(session.page, assertion.locator);
        matched = await target.count() === 1 && await target.isVisible();
      } else {
        throw Object.assign(new Error("UNSUPPORTED_ASSERTION"), { code: "UNSUPPORTED_ASSERTION" });
      }
      session.expiresAt = Date.now() + sessionTtlMs;
      return { assert_id: assertion.assert_id, matched: Boolean(matched) };
    } catch (error) {
      return { assert_id: assertion.assert_id, matched: false, error_code: error.code || "ASSERTION_EVALUATION_FAILED" };
    }
  });
}

async function observePage(session) {
  const page = await session.page.evaluate(() => {
    const redact = (value) => String(value || "")
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted-email]")
      .replace(/\b[A-F0-9]{32,}\b/gi, "[redacted-token]")
      .replace(/\b(?:\d[ -]?){13,19}\b/g, "[redacted-number]")
      .trim().slice(0, 160);
    const visible = (element) => {
      const style = getComputedStyle(element);
      const bounds = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none"
        && bounds.width > 0 && bounds.height > 0;
    };
    const controls = Array.from(document.querySelectorAll(
      "button, a[href], input, select, textarea, [role=button], [role=link]",
    )).filter(visible).slice(0, 80).map((element) => ({
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute("role") || "",
      type: element.getAttribute("type") || "",
      label: redact(element.getAttribute("aria-label") || element.getAttribute("placeholder")
        || element.innerText || element.getAttribute("title") || ""),
    }));
    return { url_origin: location.origin, controls };
  });
  session.expiresAt = Date.now() + sessionTtlMs;
  return { status: "OBSERVED", page };
}

async function closeSession(sessionId) {
  const session = sessions.get(sessionId);
  if (!session) return { session_id: sessionId, status: "CLOSED" };
  sessions.delete(sessionId);
  await session.context.close().catch(() => {});
  await session.browser.close().catch(() => {});
  await session.proxy.close().catch(() => {});
  return { session_id: sessionId, status: "CLOSED" };
}

async function handle(req, res) {
  const path = req.url || "";
  const create = req.method === "POST" && path === "/internal/v1/browser-sessions/create";
  const match = req.method === "POST"
    ? path.match(/^\/internal\/v1\/browser-sessions\/([0-9a-f-]{36})\/(actions|assertions|observe|close)$/i)
    : null;
  const drainMatch = req.method === "POST"
    ? path.match(/^\/internal\/workers\/([A-Za-z0-9._-]{1,128})\/drain$/)
    : null;
  if (!create && !match && !drainMatch) return false;
  try {
    const payload = await readJson(req);
    if (drainMatch) {
      if (drainMatch[1] !== workerInstance) return reply(res, 404, { error: "WORKER_NOT_FOUND" }), true;
      const deadline = Date.parse(payload.deadline);
      if (!Number.isFinite(deadline) || typeof payload.reason !== "string"
          || !payload.reason.trim() || payload.reason.length > 255) {
        return reply(res, 422, { error: "INVALID_DRAIN_REQUEST" }), true;
      }
      draining = true;
      if (drainTimer) clearTimeout(drainTimer);
      if (sessions.size > 0) {
        drainTimer = setTimeout(() => {
          for (const id of sessions.keys()) void closeSession(id);
        }, Math.max(0, deadline - Date.now()));
        drainTimer.unref();
      }
      return reply(res, 200, {
        worker_id: workerInstance, status: "DRAINING", active_jobs: sessions.size,
      }), true;
    }
    const claims = verifyJobToken(payload.job_token);
    const sessionId = create ? payload.session_id : match[1];
    verifyRequestScope(claims, payload, sessionId);
    if (create) return reply(res, 200, await createSession(payload)), true;
    const session = sessions.get(sessionId);
    if (!session) return reply(res, 410, { error: "BROWSER_SESSION_EXPIRED" }), true;
    if (session.claims.run_id !== claims.run_id
        || session.claims.run_case_id !== claims.run_case_id
        || session.claims.browser !== claims.browser
        || session.claims.attempt_token !== claims.attempt_token) {
      return reply(res, 403, { error: "BROWSER_SESSION_SCOPE_MISMATCH" }), true;
    }
    session.expiresAt = Date.now() + sessionTtlMs;
    if (match[2] === "actions") {
      return reply(res, 200, await performAction(session, payload.action)), true;
    }
    if (match[2] === "assertions") {
      return reply(res, 200, await performAssertion(session, payload.assertion)), true;
    }
    if (match[2] === "observe") {
      return reply(res, 200, await observePage(session)), true;
    }
    return reply(res, 200, await closeSession(sessionId)), true;
  } catch (error) {
    return reply(res, error.status || 500, {
      error: error.status ? error.message : "BROWSER_WORKER_INTERNAL_ERROR",
    }), true;
  }
}

const cleanupTimer = setInterval(() => {
  const now = Date.now();
  for (const [id, session] of sessions) {
    if (session.expiresAt <= now) void closeSession(id);
  }
}, 30000);
cleanupTimer.unref();

export async function handleBrowserWorkerRequest(req, res) {
  const path = req.url || "";
  if (!path.startsWith("/internal/v1/browser-sessions")
      && !path.startsWith("/internal/workers/")) return false;
  return handle(req, res);
}
