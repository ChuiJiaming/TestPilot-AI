import { createHmac, timingSafeEqual } from "node:crypto";

const maxBodyBytes = 32 * 1024;

function json(res, status, value) {
  const body = Buffer.from(JSON.stringify(value));
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
  const expected = createHmac("sha256", secret).update(parts[0] + "." + parts[1]).digest();
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
      || claims.aud !== "testpilot-browser-gateway"
      || !Number.isInteger(claims.exp) || claims.exp <= now
      || !claims.run_id || !claims.run_case_id || !claims.attempt_token
      || !claims.browser || !claims.base_url || !Array.isArray(claims.allowed_domains)) {
    throw Object.assign(new Error("JOB_TOKEN_SCOPE_INVALID"), { status: 403 });
  }
  return claims;
}

function verifyScope(claims, payload, sessionId, create) {
  if (claims.run_id !== payload.run_id || claims.run_case_id !== payload.run_case_id
      || claims.browser !== payload.browser || claims.attempt_token !== payload.attempt_token) {
    throw Object.assign(new Error("JOB_TOKEN_SCOPE_MISMATCH"), { status: 403 });
  }
  if (sessionId && payload.session_id && sessionId !== payload.session_id) {
    throw Object.assign(new Error("BROWSER_SESSION_SCOPE_MISMATCH"), { status: 403 });
  }
  if ((!create && (!claims.worker_instance || claims.worker_instance !== payload.worker_instance))
      || (create && claims.worker_instance && claims.worker_instance !== payload.worker_instance)) {
    throw Object.assign(new Error("WORKER_AFFINITY_MISMATCH"), { status: 403 });
  }
}

export async function handleBrowserGatewayRequest(req, res, forward) {
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
      const result = await forward(path, payload, { workerInstance: drainMatch[1], drain: true });
      json(res, 200, result);
      return true;
    }
    const claims = verifyJobToken(payload.job_token);
    const sessionId = create ? payload.session_id : match[1];
    verifyScope(claims, payload, sessionId, create);
    const result = await forward(path, payload, {
      create, workerInstance: claims.worker_instance,
    });
    json(res, 200, result);
  } catch (error) {
    const status = error.status || 503;
    json(res, status, { error: error.status ? error.message : "BROWSER_WORKER_UNAVAILABLE" });
  }
  return true;
}
