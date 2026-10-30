import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { Readable } from "node:stream";
import test from "node:test";

import { handleBrowserGatewayRequest } from "./browser_gateway_sessions.mjs";

const secret = "m10-gateway-test-secret";
const runId = "b5a3a598-0e9f-4f9a-a6aa-b29c0119f26e";
const runCaseId = "89a8eafe-2dba-4d3f-aacf-7206c28b4db8";
const sessionId = "30964104-49e9-4f6a-8a91-58011f93a963";
const attemptToken = "408fae39-61ce-40db-96f3-c2a609b829ee";

function jobToken(overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const claims = Buffer.from(JSON.stringify({
    iss: "testpilot-agent-worker", aud: "testpilot-browser-gateway",
    run_id: runId, run_case_id: runCaseId, browser: "chromium",
    attempt_token: attemptToken, base_url: "https://example.com",
    allowed_domains: ["example.com"], worker_instance: "worker-a",
    iat: now, exp: now + 60, ...overrides,
  })).toString("base64url");
  const signingInput = `${header}.${claims}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

async function invokeGateway(path, payload) {
  process.env.TESTPILOT_BROWSER_JOB_TOKEN_SECRET = secret;
  const req = Readable.from([Buffer.from(JSON.stringify(payload))]);
  req.method = "POST";
  req.url = path;
  let statusCode;
  let body;
  const res = {
    writeHead(status) { statusCode = status; },
    end(value) { body = JSON.parse(value.toString("utf8")); },
  };
  const forwarded = [];
  const handled = await handleBrowserGatewayRequest(req, res, async (...args) => {
    forwarded.push(args);
    return { status: "SUCCEEDED" };
  });
  return { handled, statusCode, body, forwarded };
}

function requestPayload(overrides = {}) {
  return {
    run_id: runId, run_case_id: runCaseId, browser: "chromium",
    attempt_token: attemptToken, worker_instance: "worker-a",
    job_token: jobToken(), ...overrides,
  };
}

function healingToken(nextAttemptToken, handoffOverrides = {}) {
  const grant = {
    healing_id: "7673e52a-8140-429a-849e-1417da57a434",
    session_id: sessionId, run_id: runId, run_case_id: runCaseId,
    browser: "chromium", previous_attempt_token: attemptToken,
    attempt_token: nextAttemptToken, worker_instance: "worker-a",
    ...handoffOverrides,
  };
  return jobToken({ attempt_token: nextAttemptToken, worker_instance: "worker-a", recovery_handoff: grant });
}

test("forwards locator verification only to the signed worker instance", async () => {
  const path = `/internal/v1/browser-sessions/${sessionId}/locators/verify`;
  const result = await invokeGateway(path, requestPayload({
    locator: { by: "role", value: "button", name: "Save" },
  }));
  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 200);
  assert.equal(result.forwarded[0][0], path);
  assert.equal(result.forwarded[0][2].workerInstance, "worker-a");
});

test("forwards an approved healing rebind to the original signed worker", async () => {
  const path = `/internal/v1/browser-sessions/${sessionId}/rebind`;
  const nextAttemptToken = "7c0fcb91-e0fc-4a62-8508-405d49d6d45d";
  const result = await invokeGateway(path, requestPayload({
    attempt_token: nextAttemptToken,
    job_token: healingToken(nextAttemptToken),
  }));
  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 200);
  assert.equal(result.forwarded.length, 1);
  assert.equal(result.forwarded[0][0], path);
  assert.equal(result.forwarded[0][2].workerInstance, "worker-a");
  assert.equal(result.forwarded[0][2].rebind, true);
});

test("rejects a rebind without a signed Healing grant before forwarding", async () => {
  const path = `/internal/v1/browser-sessions/${sessionId}/rebind`;
  const nextAttemptToken = "7c0fcb91-e0fc-4a62-8508-405d49d6d45d";
  const result = await invokeGateway(path, requestPayload({
    attempt_token: nextAttemptToken,
    job_token: jobToken({ attempt_token: nextAttemptToken, worker_instance: "worker-a" }),
  }));
  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 403);
  assert.equal(result.body.error, "SESSION_HANDOFF_REQUIRED");
  assert.equal(result.forwarded.length, 0);
});

test("rejects a Healing grant bound to another session before forwarding", async () => {
  const path = `/internal/v1/browser-sessions/${sessionId}/rebind`;
  const nextAttemptToken = "7c0fcb91-e0fc-4a62-8508-405d49d6d45d";
  const result = await invokeGateway(path, requestPayload({
    attempt_token: nextAttemptToken,
    job_token: healingToken(nextAttemptToken, {
      session_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }),
  }));
  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 403);
  assert.equal(result.body.error, "SESSION_HANDOFF_SCOPE_MISMATCH");
  assert.equal(result.forwarded.length, 0);
});
